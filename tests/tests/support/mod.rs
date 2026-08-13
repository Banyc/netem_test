//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.

#![allow(dead_code)]

pub(crate) mod dual;
pub(crate) mod fan;
pub(crate) mod frame;
pub(crate) mod mux;
pub(crate) mod payload;
pub(crate) mod perf_trace;
pub(crate) mod presets;
pub(crate) mod prng;
pub(crate) mod rtp;
pub(crate) mod rtp_mux;
pub(crate) mod stats;
pub(crate) mod task_scope;

// Re-exported for the scenario files; targets that spawn no background tasks
// (e.g. `netem_scenarios`) do not reference it.
#[allow(unused_imports)]
pub(crate) use task_scope::TestScope;

pub mod contested;

pub(crate) const LATENCY_SAMPLE_CAPACITY: usize = 4096;
pub(crate) const TEST_ACCEPT_CAPACITY: usize = 64;
pub(crate) const LANE_EVENT_CAPACITY: usize = 64;
pub(crate) const TEST_TASK_QUEUE_BOUND: usize = 256;

/// A boxed test-owned task future: session supervisors, per-stream sinks,
/// and rtp_mux drivers all submit through [`spawn_test_task_reaper_with_shutdown`].
pub(crate) type TestTask =
    std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'static>>;

/// A bounded submission channel feeding one test-owned reaper task. The
/// reaper (spawned into the scope's `reapers` set) selects between new
/// submissions and `join_next()` completions, unwrapping every completion,
/// so panics surface immediately instead of being observed only at scope
/// destruction. On shutdown it closes admission, adopts any future still
/// queued, and reaps the owned set.
///
/// Returns the submission sender; clone it into every scope that spawns
/// owned tasks and keep one alive for the channel to stay open.
pub(crate) fn spawn_test_task_reaper_with_shutdown(
    reapers: &mut tokio::task::JoinSet<()>,
    bound: usize,
    mut shutdown_rx: tokio::sync::watch::Receiver<bool>,
) -> tokio::sync::mpsc::Sender<TestTask> {
    let (tx, mut rx) = tokio::sync::mpsc::channel::<TestTask>(bound);
    reapers.spawn(async move {
        let mut owned = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                biased;
                Some(fut) = rx.recv() => {
                    owned.spawn(fut);
                }
                Some(joined) = owned.join_next() => {
                    joined.unwrap();
                }
                _ = shutdown_rx.changed() => break,
                else => break,
            }
        }
        // The scope is shutting down: close admission, adopt every future
        // still queued (a submitted future is never silently dropped), and
        // reap the owned set so a completed panic still surfaces.
        rx.close();
        while let Ok(fut) = rx.try_recv() {
            owned.spawn(fut);
        }
        abort_and_reap_test_tasks(&mut owned).await;
    });
    tx
}

/// Abort and reap every child of `tasks`: owner-requested cancellation is
/// expected (cancelled joins are skipped), but a child that already
/// completed — including a panic — is unwrapped and re-raised.
pub(super) async fn abort_and_reap_test_tasks(tasks: &mut tokio::task::JoinSet<()>) {
    tasks.abort_all();
    while let Some(joined) = tasks.join_next().await {
        if joined
            .as_ref()
            .is_err_and(tokio::task::JoinError::is_cancelled)
        {
            continue;
        }
        joined.unwrap();
    }
}

/// Submit a test-owned task future; panics if the bounded submission
/// channel is full (the reaper is not draining) or closed (the reaper
/// stopped unexpectedly), mirroring [`try_send_observation`]. A closed
/// channel must fail the test too: silently dropping the submitted future
/// would hide a task the test is relying on.
pub(crate) fn submit_test_task(tx: &tokio::sync::mpsc::Sender<TestTask>, fut: TestTask) {
    match tx.try_send(fut) {
        Ok(()) => {}
        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
            panic!("test task submission channel is full; the reaper is not draining")
        }
        Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
            panic!("test task reaper stopped unexpectedly")
        }
    }
}

/// Submit a REQUIRED test-owned task: like [`submit_test_task`] but wraps
/// the future so that completing before the test body does panics the
/// reaper (mirroring [`TestScope::spawn_required`] for the bounded
/// submission handle, for helpers called inside `run` bodies).
pub(crate) fn submit_test_task_required(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    name: &'static str,
    fut: impl std::future::Future<Output = ()> + Send + 'static,
) {
    submit_test_task(
        tx,
        Box::pin(async move {
            fut.await;
            panic!("required task '{name}' exited before the test body completed");
        }),
    );
}

pub(crate) fn try_send_observation<T>(
    tx: &tokio::sync::mpsc::Sender<T>,
    value: T,
    kind: &'static str,
) -> bool {
    match tx.try_send(value) {
        Ok(()) => true,
        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
            panic!("{} channel is full; the test harness is not draining", kind)
        }
        Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[should_panic(expected = "channel is full")]
    fn try_send_observation_panics_when_channel_is_full() {
        let (tx, _rx) = tokio::sync::mpsc::channel(1);
        assert!(try_send_observation(&tx, 1u8, "latency sample"));
        try_send_observation(&tx, 2u8, "latency sample");
    }

    #[tokio::test]
    async fn bounded_send_applies_backpressure_until_drained() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        assert!(try_send_observation(&tx, 1u8, "accept"));
        let tx2 = tx.clone();
        let second = tx2.send(2u8);
        tokio::pin!(second);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(50), &mut second)
                .await
                .is_err()
        );
        assert_eq!(rx.recv().await, Some(1u8));
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(50), second)
                .await
                .is_ok()
        );
    }

    #[test]
    #[should_panic(expected = "test task submission channel is full; the reaper is not draining")]
    fn submit_test_task_panics_when_channel_is_full() {
        let (tx, _rx) = tokio::sync::mpsc::channel::<TestTask>(1);
        let parked = Box::pin(std::future::pending::<()>());
        submit_test_task(&tx, parked);
        let second = Box::pin(std::future::pending::<()>());
        // The receiver is never polled, so the single slot stays occupied and
        // the second submission must panic instead of being dropped silently.
        submit_test_task(&tx, second);
    }

    #[test]
    #[should_panic(expected = "test task reaper stopped unexpectedly")]
    fn submit_test_task_panics_when_channel_is_closed() {
        let (tx, rx) = tokio::sync::mpsc::channel::<TestTask>(1);
        drop(rx);
        let fut = Box::pin(std::future::pending::<()>());
        // The reaper stopped (the receiver is gone); submitting must panic
        // rather than silently dropping the future.
        submit_test_task(&tx, fut);
    }
}
