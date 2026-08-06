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
pub(crate) mod presets;
pub(crate) mod prng;
pub(crate) mod rtp;
pub(crate) mod rtp_mux;
pub(crate) mod stats;

pub mod contested;

pub(crate) const LATENCY_SAMPLE_CAPACITY: usize = 4096;
pub(crate) const TEST_ACCEPT_CAPACITY: usize = 64;
pub(crate) const LANE_EVENT_CAPACITY: usize = 64;

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
