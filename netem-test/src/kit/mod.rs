//! Shared scenario-testing kit: the generic helpers every scenario crate
//! consumes over the [`crate::NetemPair`] harness (deterministic payloads,
//! impairment presets, reporting stats, task scopes, per-flow fans, and the
//! module-core constants/observation channel).
//!
//! This is the single home for the shared scaffolding: the application
//! scenario packages depend on the harness by local path and consume the kit
//! through `netem_test::kit` directly, importing the helpers each target uses.
//!
//! The kit exists behind the `test-kit` feature so a plain library build of
//! the leaf harness does not pull in tokio.

pub mod contested;
pub mod emulated;
pub mod fan;
pub mod payload;
pub mod presets;
pub mod prng;
pub mod stats;
pub mod task_scope;

// Re-exported for the scenario targets.
pub use task_scope::{
    TestScope, TestTask, TestTaskSubmitter, abort_and_reap_test_tasks,
    spawn_test_task_reaper_with_shutdown, submit_test_task, submit_test_task_required,
};

pub const LATENCY_SAMPLE_CAPACITY: usize = 4096;
pub const TEST_ACCEPT_CAPACITY: usize = 64;
pub const LANE_EVENT_CAPACITY: usize = 64;
pub const TEST_TASK_QUEUE_BOUND: usize = 256;

pub fn try_send_observation<T>(
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
