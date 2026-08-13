// ─────────────────────────── reporting helpers ───────────────────────────

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use netem_test::{Counters, NetemPair};
use tokio::sync::Notify;

/// Combined stats across both directions of a [`NetemPair`].
pub fn combined_stats(pair: &NetemPair) -> Counters {
    pair.stats()
}

/// Print a perf summary line for a payload of `bytes` that completed in
/// `elapsed`, using `label` to identify the scenario.
pub fn print_perf(label: &str, bytes: usize, elapsed: Duration) {
    let secs = elapsed.as_secs_f64().max(f64::EPSILON);
    let mib = bytes as f64 / (1024.0 * 1024.0);
    let throughput_mibps = mib / secs;
    eprintln!("[perf] {label}: {bytes} bytes in {elapsed:?} ({throughput_mibps:.3} MiB/s)");
}

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

/// Sort `samples` and print perf summaries for the median and worst
/// (slowest) runs. Used by the ceiling probes so a single slow warm-up episode
/// does not distort the reported throughput.
pub fn print_median_worst(label: &str, bytes: usize, mut samples: Vec<Duration>) {
    samples.sort();
    let n = samples.len();
    assert!(n > 0, "print_median_worst called with empty samples");
    let median_label = format!("{label} [median of {n}]");
    let worst_label = format!("{label} [worst of {n}]");
    print_perf(&median_label, bytes, samples[n / 2]);
    print_perf(&worst_label, bytes, samples[n - 1]);
}

/// Return the p-th percentile of `sorted` using nearest-rank, truncating the
/// index. `sorted` must be sorted ascending; an empty slice yields `NaN`.
pub fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    assert!(
        (0.0..=1.0).contains(&p),
        "percentile p must be in [0.0, 1.0]"
    );
    let rank = ((sorted.len() as f64 - 1.0) * p).floor() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

/// Summary returned by [`run_hol_probe`].
#[derive(Clone, Debug, Default)]
pub struct HolSummary {
    /// Interactive messages sent.
    pub sent: u64,
    /// Interactive messages received.
    pub received: u64,
    /// `received / sent`.
    pub delivery_pct: f64,
    /// Median one-way latency in ms.
    pub p50: f64,
    /// 90th percentile one-way latency in ms.
    pub p90: f64,
    /// 99th percentile one-way latency in ms.
    pub p99: f64,
    /// Maximum one-way latency in ms.
    pub max: f64,
    /// Fraction of samples > 250 ms.
    pub over250_pct: f64,
    /// Fraction of samples > 1000 ms.
    pub over1000_pct: f64,
    /// Number of contiguous episodes with latency > 250 ms.
    pub episodes: u64,
    /// Longest contiguous run of samples > 250 ms.
    pub max_run: u64,
    /// Bulk goodput in MiB/s (0 if no bulk flow).
    pub bulk_mibps: f64,
}

/// Compute a [`HolSummary`] from raw latency samples.
pub fn summarize(
    mut samples: Vec<f64>,
    sent: u64,
    received: u64,
    bulk_bytes: u64,
    bulk_secs: f64,
) -> HolSummary {
    let n = samples.len();
    let (episodes, max_run) = {
        let mut episodes = 0u64;
        let mut max_run = 0u64;
        let mut current = 0u64;
        let mut in_run = false;
        for &x in &samples {
            if x > 250.0 {
                if !in_run {
                    episodes += 1;
                    in_run = true;
                }
                current += 1;
            } else {
                in_run = false;
                max_run = max_run.max(current);
                current = 0;
            }
        }
        max_run = max_run.max(current);
        (episodes, max_run)
    };

    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let delivery_pct = if sent == 0 {
        0.0
    } else {
        received as f64 / sent as f64
    };
    let p50 = if n > 0 {
        percentile(&samples, 0.50)
    } else {
        0.0
    };
    let p90 = if n > 0 {
        percentile(&samples, 0.90)
    } else {
        0.0
    };
    let p99 = if n > 0 {
        percentile(&samples, 0.99)
    } else {
        0.0
    };
    let max = samples.last().copied().unwrap_or(0.0);
    let over250 = if n > 0 {
        samples.iter().filter(|&&x| x > 250.0).count() as f64 / n as f64
    } else {
        0.0
    };
    let over1000 = if n > 0 {
        samples.iter().filter(|&&x| x > 1000.0).count() as f64 / n as f64
    } else {
        0.0
    };
    let bulk_mibps = if bulk_secs > 0.0 {
        bulk_bytes as f64 / (1024.0 * 1024.0) / bulk_secs
    } else {
        0.0
    };

    HolSummary {
        sent,
        received,
        delivery_pct,
        p50,
        p90,
        p99,
        max,
        over250_pct: over250,
        over1000_pct: over1000,
        episodes,
        max_run,
        bulk_mibps,
    }
}
