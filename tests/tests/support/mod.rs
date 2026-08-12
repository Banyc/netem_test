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
/// and rtp_mux drivers all submit through [`spawn_test_task_reaper`].
pub(crate) type TestTask =
    std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'static>>;

/// A bounded submission channel feeding one test-owned reaper task. The
/// reaper (spawned into `tasks`) selects between new submissions and
/// `join_next()` completions, unwrapping every completion, so panics surface
/// immediately instead of being observed only at scope destruction.
///
/// Returns the submission sender; clone it into every scope that spawns
/// owned tasks and keep one alive for the channel to stay open.
pub(crate) fn spawn_test_task_reaper(
    tasks: &mut tokio::task::JoinSet<()>,
    bound: usize,
) -> tokio::sync::mpsc::Sender<TestTask> {
    let (tx, mut rx) = tokio::sync::mpsc::channel::<TestTask>(bound);
    tasks.spawn(async move {
        let mut owned = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                Some(fut) = rx.recv() => {
                    owned.spawn(fut);
                }
                Some(joined) = owned.join_next() => {
                    joined.unwrap();
                }
                else => break,
            }
        }
    });
    tx
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
}
