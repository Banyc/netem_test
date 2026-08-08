//! `rtp` + FEC recovery scenarios through [`netem_test::NetemPair`].
//!
//! Verifies that `rtp` with forward error correction enabled recovers lost
//! data symbols via parity even when the netem proxy drops a few percent of
//! packets in each direction.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test rtp_fec -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};
use support::payload::{payload, with_timeout};
use support::rtp::{rtp_connect, rtp_echo_payload, spawn_rtp_echo_server};
use support::stats::combined_stats;

mod support;

/// `rtp` with FEC enabled should recover under ~3% netem loss in each
/// direction — the byte stream must arrive intact.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "rtp+FEC recovery end-to-end scenario; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_with_fec_recovers_under_netem_loss() {
    let mut tasks = support::TestScope::new();
    let server_addr = spawn_rtp_echo_server(&mut tasks, true).await.unwrap();

    let lossy = NetemConfig {
        loss: u32::MAX / 33, // ~3%
        latency: Duration::from_millis(5),
        seed: 99,
        ..NetemConfig::default()
    };
    let pair = NetemPair::spawn(server_addr, lossy.clone(), lossy).unwrap();
    let (read, write) = rtp_connect(&mut tasks, pair.client_addr(), true).await;

    let payload = payload(1024 * 1024);
    tasks
        .run(async {
            let got = with_timeout(
                Duration::from_secs(90),
                "rtp+FEC 1MiB echo",
                rtp_echo_payload(read, write, &payload),
            )
            .await;
            assert_eq!(got, payload, "FEC + reliable layer must recover all data");
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.dropped > 0,
        "proxy should have dropped some packets, got {stats:?}"
    );
}
