// ─────────────────────────── mux-sink progress machinery ─────────────────
//
// The pure reporting helpers (combined_stats, print_perf, print_median_worst,
// percentile, HolSummary, summarize) now live in the harness kit
// (`netem_test::kit::stats`, behind the `test-kit` feature) and are
// re-exported here so existing scenario imports keep resolving. The mux-sink
// session-progress machinery below stays harness-local until it relocates
// into the mux layer kit: it converts `mux::MuxError`, which the leaf
// harness must never depend on.

#[allow(unused_imports)]
pub use netem_test::kit::stats::{
    HolSummary, combined_stats, percentile, print_median_worst, print_perf, summarize,
};

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
// (Duration is only used by the reporting helpers, now in the kit)

use tokio::sync::Notify;

/// Outcome of the mux sink's stream-reader loop.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SinkReadOutcome {
    Running,
    CleanEof,
    ReadError(std::io::ErrorKind),
}

impl SinkReadOutcome {
    /// Stable label used by manifest rows.
    pub fn as_label(self) -> String {
        match self {
            Self::Running => "running".to_owned(),
            Self::CleanEof => "clean_eof".to_owned(),
            Self::ReadError(kind) => format!("read_error/{kind:?}"),
        }
    }
}

/// Outcome of the mux session driving the counting sink's transport.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MuxSessionOutcome {
    Running,
    IoReader(std::io::ErrorKind),
    IoWriter(std::io::ErrorKind),
    TaskStopped(&'static str),
    ControlChannelClosed(&'static str),
}

impl MuxSessionOutcome {
    fn from_error(error: &mux::MuxError) -> Self {
        match error {
            mux::MuxError::IoReader(error) => Self::IoReader(error.kind()),
            mux::MuxError::IoWriter(error) => Self::IoWriter(error.kind()),
            mux::MuxError::DualLane { source, .. } => Self::from_error(source),
            mux::MuxError::TaskStopped { task } => Self::TaskStopped(task),
            mux::MuxError::ControlChannelClosed { task } => Self::ControlChannelClosed(task),
        }
    }

    /// Stable label used by manifest rows.
    pub fn as_label(self) -> String {
        match self {
            Self::Running => "running".to_owned(),
            Self::IoReader(kind) => format!("io_reader/{kind:?}"),
            Self::IoWriter(kind) => format!("io_writer/{kind:?}"),
            Self::TaskStopped(task) => format!("task_stopped/{task}"),
            Self::ControlChannelClosed(task) => format!("control_channel_closed/{task}"),
        }
    }
}

/// First-result latch for a mux session's terminal outcome.
#[derive(Debug)]
pub struct MuxSessionProgress {
    outcome: Mutex<MuxSessionOutcome>,
    outcome_ready: Notify,
}

impl MuxSessionProgress {
    /// Create a fresh latch still in the `Running` state.
    pub fn new() -> Self {
        Self {
            outcome: Mutex::new(MuxSessionOutcome::Running),
            outcome_ready: Notify::new(),
        }
    }

    /// The first terminal outcome recorded, or `Running` if none arrived yet.
    pub fn outcome(&self) -> MuxSessionOutcome {
        *self.outcome.lock().unwrap()
    }

    /// Record the first terminal outcome; later results never overwrite it.
    pub fn record_error(&self, error: &mux::MuxError) {
        let mut current = self.outcome.lock().unwrap();
        if *current != MuxSessionOutcome::Running {
            return;
        }
        *current = MuxSessionOutcome::from_error(error);
        drop(current);
        self.outcome_ready.notify_one();
    }

    /// Wait for the first terminal outcome.
    pub async fn wait_for_outcome(&self) -> MuxSessionOutcome {
        loop {
            let notified = self.outcome_ready.notified();
            let outcome = self.outcome();
            if outcome != MuxSessionOutcome::Running {
                return outcome;
            }
            notified.await;
        }
    }
}

impl Default for MuxSessionProgress {
    fn default() -> Self {
        Self::new()
    }
}

/// Shared progress counter for the [`spawn_mux_over_rtp_counting_sink_server`]
/// goodput sink. It tracks how many payload bytes were delivered and whether
/// any byte failed the deterministic payload check, plus first-result latches
/// for the sink read loop and the mux session that carries it.
pub struct SinkProgress {
    /// Total payload bytes accepted by the sink and verified against the
    /// deterministic `(offset % 251)` pattern.
    pub delivered: AtomicU64,
    /// Set to `true` if any accepted byte did not match the expected pattern.
    pub corrupt: AtomicBool,
    read_outcome: Mutex<SinkReadOutcome>,
    read_outcome_ready: Notify,
    mux_session: Arc<MuxSessionProgress>,
}

impl SinkProgress {
    /// Create a fresh counter with zero delivered bytes and no corruption flag.
    pub fn new() -> Self {
        Self {
            delivered: AtomicU64::new(0),
            corrupt: AtomicBool::new(false),
            read_outcome: Mutex::new(SinkReadOutcome::Running),
            read_outcome_ready: Notify::new(),
            mux_session: Arc::new(MuxSessionProgress::new()),
        }
    }

    /// Number of payload bytes successfully delivered to the sink so far.
    pub fn delivered_bytes(&self) -> u64 {
        self.delivered.load(Ordering::Relaxed)
    }

    /// Whether the sink has observed any corrupted payload byte.
    pub fn is_corrupt(&self) -> bool {
        self.corrupt.load(Ordering::Relaxed)
    }

    /// The first terminal outcome of the sink read loop, or `Running`.
    pub fn read_outcome(&self) -> SinkReadOutcome {
        *self.read_outcome.lock().unwrap()
    }

    /// Record the first sink-read outcome; later results never overwrite it.
    pub fn record_read_outcome(&self, outcome: SinkReadOutcome) {
        let mut current = self.read_outcome.lock().unwrap();
        if *current != SinkReadOutcome::Running {
            return;
        }
        *current = outcome;
        drop(current);
        self.read_outcome_ready.notify_one();
    }

    /// Wait for the first sink-read outcome.
    pub async fn wait_for_read_outcome(&self) -> SinkReadOutcome {
        loop {
            let notified = self.read_outcome_ready.notified();
            let outcome = self.read_outcome();
            if outcome != SinkReadOutcome::Running {
                return outcome;
            }
            notified.await;
        }
    }

    /// Shared first-result latch for the mux session driving this sink.
    pub fn mux_session(&self) -> Arc<MuxSessionProgress> {
        Arc::clone(&self.mux_session)
    }
}

impl Default for SinkProgress {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod sink_progress_tests {
    use super::*;
    use std::future::Future as _;

    #[tokio::test]
    async fn first_sink_terminal_outcome_is_latched_and_notified() {
        let progress = SinkProgress::new();
        let wait = progress.wait_for_read_outcome();
        tokio::pin!(wait);
        assert!(
            std::future::poll_fn(|cx| match wait.as_mut().poll(cx) {
                std::task::Poll::Pending => std::task::Poll::Ready(true),
                std::task::Poll::Ready(_) => std::task::Poll::Ready(false),
            })
            .await
        );
        progress.record_read_outcome(SinkReadOutcome::ReadError(std::io::ErrorKind::BrokenPipe));
        progress.record_read_outcome(SinkReadOutcome::CleanEof);
        assert_eq!(
            wait.await,
            SinkReadOutcome::ReadError(std::io::ErrorKind::BrokenPipe)
        );
        assert_eq!(progress.read_outcome().as_label(), "read_error/BrokenPipe");
    }

    #[tokio::test]
    async fn first_mux_session_error_is_latched_and_notified() {
        let progress = MuxSessionProgress::new();
        let wait = progress.wait_for_outcome();
        tokio::pin!(wait);
        assert!(
            std::future::poll_fn(|cx| match wait.as_mut().poll(cx) {
                std::task::Poll::Pending => std::task::Poll::Ready(true),
                std::task::Poll::Ready(_) => std::task::Poll::Ready(false),
            })
            .await
        );
        progress.record_error(&mux::MuxError::IoReader(
            std::io::ErrorKind::TimedOut.into(),
        ));
        progress.record_error(&mux::MuxError::TaskStopped { task: "later" });
        assert_eq!(
            wait.await,
            MuxSessionOutcome::IoReader(std::io::ErrorKind::TimedOut)
        );
        assert_eq!(progress.outcome().as_label(), "io_reader/TimedOut");
    }
}
