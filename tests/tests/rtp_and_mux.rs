//! Small compatibility smoke test that exercises the `rtp` and
//! `mux`-over-`rtp` stacks end-to-end through a [`netem_test::NetemPair`]
//! bidirectional impairment proxy.
//!
//! This target is kept intentionally small and is a thin wrapper over the
//! shared helpers in `support/`. The focused, comprehensive scenarios live in
//! the sibling test targets (`raw_netem_pair.rs`, `rtp_clean.rs`,
//! `rtp_loss.rs`, `rtp_fec.rs`, `mux_over_rtp.rs`, `mux_over_rtp_perf.rs`).
//!
//! Marked `#[ignore]` because they spawn threads, bind ephemeral ports, and
//! run for hundreds of milliseconds. Run with:
//!
//! ```sh
//! cargo test --test rtp_and_mux -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::NetemPair;
use support::mux::{mux_client_connect, mux_echo_round_trip, spawn_mux_over_rtp_echo_server};
use support::payload::{payload, with_timeout};
use support::presets::{clean, mild_loss};
use support::rtp::{rtp_connect, rtp_echo_payload, spawn_rtp_echo_server};
use support::stats::combined_stats;

mod support;

/// `rtp` should deliver a byte stream reliably over a *clean* netem link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads, binds ephemeral ports, and runs for hundreds of milliseconds; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_over_netem_clean_link_delivers_data() {
    let mut tasks = support::TestScope::new();
    let server_addr = spawn_rtp_echo_server(&mut tasks, false).await.unwrap();

    let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;

    let payload = b"netem-rtp-integration";
    tasks
        .run(async {
            let got = with_timeout(
                Duration::from_secs(10),
                "rtp clean small echo",
                rtp_echo_payload(read, write, payload),
            )
            .await;
            assert_eq!(got, payload);
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `rtp`'s reliable layer should recover from mild packet loss introduced by
/// the netem proxy — the byte stream must arrive intact despite ~5% loss in
/// both directions.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads, binds ephemeral ports, and runs for hundreds of milliseconds; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_over_netem_reliability_survives_mild_loss() {
    let mut tasks = support::TestScope::new();
    let server_addr = spawn_rtp_echo_server(&mut tasks, false).await.unwrap();

    let pair = NetemPair::spawn(server_addr, mild_loss(), mild_loss()).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;

    let payload = payload(256 * 1024);
    tasks
        .run(async {
            let got = with_timeout(
                Duration::from_secs(60),
                "rtp lossy 256KiB echo",
                rtp_echo_payload(read, write, &payload),
            )
            .await;
            assert_eq!(got, payload, "reliable layer must recover all data");
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.dropped > 0,
        "proxy should have dropped some packets, got {stats:?}"
    );
}

/// `mux` layered on `rtp` should multiplex a stream over the netem-impaired
/// link and echo data back intact on a clean link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads, binds ephemeral ports, and runs for hundreds of milliseconds; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_over_netem_clean_link_echoes() {
    let mut tasks = support::TestScope::new();
    let server_addr = spawn_mux_over_rtp_echo_server(&mut tasks, false)
        .await
        .unwrap();

    let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    let payload = b"mux-rtp-netem";
    tasks
        .run(async {
            let got = with_timeout(
                Duration::from_secs(15),
                "mux-over-rtp clean echo",
                mux_echo_round_trip(&opener, payload),
            )
            .await;
            assert_eq!(got, payload);
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}
