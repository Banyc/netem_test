//! `mux` over `rtp` over [`netem_test::NetemPair`] — clean and latency-only
//! echo scenarios.
//!
//! Verifies the stream multiplexer layered on the reliable UDP transport
//! delivers a multiplexed stream intact over a clean link and over a
//! latency-only impaired link.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test mux_over_rtp -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};
use support::mux::{mux_client_connect, mux_echo_round_trip, spawn_mux_over_rtp_echo_server};
use support::payload::with_timeout;
use support::presets::clean;
use support::rtp::rtp_connect;
use support::stats::combined_stats;

mod support;

/// `mux` layered on `rtp` should multiplex a stream over the netem-impaired
/// link and echo data back intact on a clean link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "mux-over-rtp echo scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_over_netem_clean_link_echoes() {
    let mut tasks = tokio::task::JoinSet::new();
    let server_addr = spawn_mux_over_rtp_echo_server(&mut tasks, false)
        .await
        .unwrap();

    let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    let payload = b"mux-rtp-netem";
    let got = with_timeout(
        Duration::from_secs(15),
        "mux-over-rtp clean echo",
        mux_echo_round_trip(&opener, payload),
    )
    .await;

    assert_eq!(got, payload);

    pair.stop();
    let stats = combined_stats(&pair);
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `mux` over `rtp` should survive the netem proxy's added latency; `rtp`'s
/// reliable layer retransmits any lost datagrams so the multiplexed stream
/// arrives intact. A small payload is used because larger transfers can trip
/// rtp's broken-pipe heuristic when mux's control-plane stalls the ACK path.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "mux-over-rtp echo scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_survives_netem_latency() {
    let mut tasks = tokio::task::JoinSet::new();
    let server_addr = spawn_mux_over_rtp_echo_server(&mut tasks, false)
        .await
        .unwrap();

    let impaired = NetemConfig {
        latency: Duration::from_millis(20),
        seed: 42,
        ..NetemConfig::default()
    };
    let pair = NetemPair::spawn(server_addr, impaired.clone(), impaired).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    let payload = b"mux-over-rtp-through-netem";
    let got = with_timeout(
        Duration::from_secs(15),
        "mux-over-rtp latency echo",
        mux_echo_round_trip(&opener, payload),
    )
    .await;

    assert_eq!(got, payload, "mux stream must deliver all data intact");

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(stats.forwarded > 0, "proxy should forward packets");
}
