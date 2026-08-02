//! `rtp` loss-recovery scenarios through [`netem_test::NetemPair`].
//!
//! Verifies the reliable UDP layer recovers from mild bidirectional packet
//! loss introduced by the proxy, both for a 400 KiB loss payload, and that
//! the proxy reports non-zero drops.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test rtp_loss -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::NetemPair;
use support::payload::{payload, with_timeout};
use support::presets::mild_loss;
use support::rtp::{rtp_connect, rtp_echo_payload, spawn_rtp_echo_server};
use support::stats::combined_stats;

mod support;

/// `rtp`'s reliable layer should recover from mild packet loss introduced by
/// the netem proxy — a 400 KiB byte stream must arrive intact despite ~5%
/// loss in both directions.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_over_netem_survives_mild_loss_400kib() {
    let server_addr = spawn_rtp_echo_server(false).await.unwrap();

    let pair = NetemPair::spawn(server_addr, mild_loss(), mild_loss()).unwrap();
    let (read, write) = rtp_connect(pair.client_addr(), false).await;

    let payload = payload(400 * 1024);
    let got = with_timeout(
        Duration::from_secs(60),
        "rtp lossy 400KiB echo",
        rtp_echo_payload(read, write, &payload),
    )
    .await;

    assert_eq!(got, payload, "reliable layer must recover all 400KiB");

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.dropped > 0,
        "proxy should have dropped some packets, got {stats:?}"
    );
}
