//! `rtp` clean-delivery scenarios through [`netem_test::NetemPair`].
//!
//! Verifies the reliable UDP layer delivers byte streams intact over an
//! unimpaired bidirectional proxy, both for small payloads and for a 400 KiB
//! clean payload, and that the proxy's added latency is observable end-to-end.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test rtp_clean -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::NetemPair;
use support::payload::{payload, with_timeout};
use support::presets::{clean, latency};
use support::rtp::{rtp_connect_via, rtp_echo_payload, spawn_rtp_echo_server_via};
use support::stats::combined_stats;

mod support;

/// `rtp` should deliver a byte stream reliably over a *clean* netem link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "rtp clean-delivery end-to-end scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_over_netem_clean_link_delivers_data() {
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let stats = tasks
        .run(async {
            let server_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();

            let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
            let (read, write) = rtp_connect_via(&task_tx, pair.client_addr(), false).await;

            let payload = b"netem-rtp-integration";
            let got = with_timeout(
                Duration::from_secs(10),
                "rtp clean small echo",
                rtp_echo_payload(read, write, payload),
            )
            .await;
            assert_eq!(got, payload);

            pair.stop();
            combined_stats(&pair)
        })
        .await;
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `rtp` should deliver a 400 KiB deterministic payload intact over a clean
/// link. This is the clean baseline for the perf scenarios.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "rtp clean-delivery end-to-end scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_over_netem_clean_link_delivers_400kib() {
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let stats = tasks
        .run(async {
            let server_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();

            let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
            let (read, write) = rtp_connect_via(&task_tx, pair.client_addr(), false).await;

            let payload = payload(400 * 1024);
            let got = with_timeout(
                Duration::from_secs(30),
                "rtp clean 400KiB echo",
                rtp_echo_payload(read, write, &payload),
            )
            .await;
            assert_eq!(got, payload, "clean 400KiB delivery must be byte-exact");

            pair.stop();
            combined_stats(&pair)
        })
        .await;
    assert_eq!(
        stats.dropped, 0,
        "clean link should not drop, got {stats:?}"
    );
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// The netem latency must be observable end-to-end through `rtp`: a ping
/// should take at least the configured one-way delay (the round trip crosses
/// both directions, so roughly `2 * latency`).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "rtp clean-delivery end-to-end scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_over_netem_latency_is_observable() {
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let server_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();

            let latency_ms = 60;
            let pair = NetemPair::spawn(server_addr, latency(latency_ms), latency(latency_ms)).unwrap();
            let (read, write) = rtp_connect_via(&task_tx, pair.client_addr(), false).await;

            let start = std::time::Instant::now();
            let got = with_timeout(
                Duration::from_secs(10),
                "rtp latency ping",
                rtp_echo_payload(read, write, b"ping"),
            )
            .await;
            let elapsed = start.elapsed();
            assert_eq!(got, b"ping");
            assert!(
                elapsed >= Duration::from_millis(latency_ms),
                "round trip {elapsed:?} should be >= one-way latency {}ms",
                latency_ms,
            );
            pair.stop();
        })
        .await;
}
