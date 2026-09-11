//! Minimal reproduction of the clean-link `mux`-over-`rtp` bulk stall.
//!
//! `hol_verify4::v4_clean_muxbulk` writes a bulk stream for a fixed window and
//! only completes when the writer drains; on a *clean* link it never does,
//! while the same bulk over a lossy link (`mux_over_rtp_perf`) and raw `rtp`
//! bulk on a clean link both complete.  This scenario isolates the stall: a
//! single mux stream, a clean `NetemPair`, a bounded write window, and an
//! outer timeout.  If the writer wedges, the timeout fires and the test fails
//! with the bytes delivered so far.
//!
//! Run with:
//!
//! ```sh
//! cargo test --release --test mux_bulk_clean_stall -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::{mux_client_connect_via, spawn_mux_over_rtp_server_with_mss_via};
use support::payload::cyclic_payload;
use support::rtp::rtp_connect_with_mss_via;
use support::{TEST_TASK_QUEUE_BOUND, TestScope, submit_test_task};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

const CHUNK: usize = 262_044;
const WRITE_WINDOW: Duration = Duration::from_secs(20);
const STALL_TIMEOUT: Duration = Duration::from_secs(60);

/// A clean (no-loss) 50 ms RTT link, matching the `hol_verify4` preset that
/// exposes the stall: the mux stream's flow-control window fills before the
/// sender drains, and the writer wedges.
fn clean_link(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(50),
        seed,
        ..NetemConfig::default()
    }
}

/// A clean-link mux bulk stream must make progress and complete its write
/// window.  The delivered byte count is asserted to be non-trivial, so a
/// writer that stalls immediately (before any progress) and a writer that
/// wedges mid-stream are both caught.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "clean-link mux bulk stall reproduction; run with --ignored --nocapture --test-threads=1"]
async fn clean_link_mux_bulk_completes_within_timeout() {
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TEST_TASK_QUEUE_BOUND);

    let delivered = Arc::new(AtomicU64::new(0));
    let delivered_for_server = Arc::clone(&delivered);

    let run = tasks.run(async {
        let server_addr = spawn_mux_over_rtp_server_with_mss_via(
            &task_tx,
            false,
            rtp::udp::NO_FEC_MSS,
            move |mut stream_read, mut stream_write| {
                let delivered = Arc::clone(&delivered_for_server);
                async move {
                    let mut buf = vec![0u8; 64 * 1024];
                    while let Ok(n) = stream_read.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                        delivered.fetch_add(n as u64, Ordering::Relaxed);
                    }
                    let _ = stream_write.shutdown();
                }
            },
        )
        .await
        .unwrap();

        let pair = NetemPair::spawn(server_addr, clean_link(11), clean_link(22)).unwrap();
        let (connected_read, connected_write) =
            rtp_connect_with_mss_via(&task_tx, pair.client_addr(), false, rtp::udp::NO_FEC_MSS)
                .await;
        let opener = mux_client_connect_via(&task_tx, connected_read, connected_write);
        let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

        // Drain the read half so flow-control ACKs keep moving.
        submit_test_task(
            &task_tx,
            Box::pin(async move {
                let mut buf = vec![0u8; 8 * 1024];
                while let Ok(n) = stream_read.read(&mut buf).await {
                    if n == 0 {
                        break;
                    }
                }
            }),
        );

        let payload = cyclic_payload(CHUNK);
        let start = Instant::now();
        let mut writes = 0u64;
        while start.elapsed() < WRITE_WINDOW {
            match stream_write.write_all(&payload[..CHUNK]).await {
                Ok(()) => writes += 1,
                Err(error) => {
                    eprintln!("[mux-clean-stall] write error after {writes} chunks: {error}");
                    break;
                }
            }
            if writes % 8 == 0 {
                eprintln!(
                    "[mux-clean-stall] wrote {writes} chunks, delivered={} B, elapsed={:?}",
                    delivered.load(Ordering::Relaxed),
                    start.elapsed()
                );
            }
        }
        let _ = stream_write.shutdown();
        drop(opener);
        pair.stop();
        (writes, start.elapsed(), delivered.load(Ordering::Relaxed))
    });

    match tokio::time::timeout(STALL_TIMEOUT, run).await {
        Ok((writes, elapsed, bytes)) => {
            eprintln!(
                "[mux-clean-stall] completed: writes={writes} elapsed={elapsed:?} delivered={bytes} B"
            );
            assert!(writes > 0, "the clean-link mux writer made no progress");
            assert!(bytes > 0, "the clean-link mux sink delivered nothing");
        }
        Err(_) => panic!(
            "clean-link mux bulk did not complete within {STALL_TIMEOUT:?} — the stall is reproduced"
        ),
    }
}
