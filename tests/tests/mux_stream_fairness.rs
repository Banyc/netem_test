//! Mux-level stream fairness over a single `rtp` connection.
//!
//! `rtp`'s congestion-control fairness governs how *separate* rtp flows share
//! a bottleneck.  Within one rtp flow every mux logical stream is opaque bytes
//! to the transport, so any per-stream bandwidth skew must come from the `mux`
//! layer's own egress scheduler.  This scenario builds the measurement that
//! isolates it: one `mux` session over one `rtp` connection through a
//! fixed-rate, seeded `NetemPair`, N bulk logical streams, and per-stream
//! delivered bytes counted by the peer.
//!
//! Each stream writes an 8-byte little-endian tag before its payload, so the
//! per-stream counters are keyed by stream identity rather than accept order.
//! A warmup window is discarded and the steady window's per-stream byte deltas
//! feed a Jain index.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test mux_stream_fairness -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};
use support::mux::{mux_client_connect_via, spawn_mux_over_rtp_server_with_mss_via};
use support::rtp::rtp_connect_via;
use support::task_scope::submit_test_task;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// Fixed offered aggregate load is far above the shaped link rate, so every
/// non-throttled stream is backlogged for the whole steady window and the split
/// is decided by the mux scheduler, not by a producer running dry.
const LINK_RATE_BPS: u64 = 4_000_000;
const LINK_LATENCY_MS: u64 = 20;
const WARMUP: Duration = Duration::from_secs(4);
const STEADY: Duration = Duration::from_secs(8);

/// What a measured arm must satisfy.
#[derive(Clone, Copy)]
enum Floor {
    /// Equal-size chunks must split the link evenly and stably.
    HomogeneousJain,
    /// Different-size bulk chunks must split the link by bytes too, not by
    /// messages: byte-fair deficit round robin keeps a small-message stream's
    /// share near its peers', so the Jain index stays high.
    ByteFairJain,
    /// Report-only: a genuinely latency-sensitive small-message stream is
    /// allowed to dominate, and a throttled stream is expected to see an
    /// unequal (offered-rate-limited) share.
    ReportOnly,
}

/// One measurement arm.
struct Arm {
    label: &'static str,
    /// Per-stream write chunk size; the stream count is its length.
    chunks: Vec<usize>,
    /// Delay inserted between opening successive streams.
    skew: Duration,
    /// When set, stream 0 offers at most this many bytes/s instead of running
    /// backlogged, so the arm exercises a per-stream rate limit.
    throttled_bps: Option<u64>,
    floor: Floor,
}

/// Jain floor for equal-size arms.
const HOMOGENEOUS_JAIN_FLOOR: f64 = 0.98;
/// Jain floor for heterogeneous arms once the round is byte-fair. The
/// pre-byte-fair scheduler measured ~0.75 on `hetero_4k_64k_64k`, so this
/// floor is a real regression guard, not a tautology.
const BYTE_FAIR_JAIN_FLOOR: f64 = 0.98;
/// Starvation floor: every arm must keep every stream above this share.
const HETERO_MIN_SHARE: f64 = 0.02;

/// Jain fairness index over per-stream delivered bytes.
fn jain(values: &[u64]) -> f64 {
    let n = values.len() as f64;
    let sum: f64 = values.iter().map(|&v| v as f64).sum();
    let sq: f64 = values.iter().map(|&v| (v as f64) * (v as f64)).sum();
    if sum <= 0.0 || sq <= 0.0 {
        return 0.0;
    }
    sum * sum / (n * sq)
}

/// Run one arm once and return the per-stream delivered bytes over the steady
/// window, indexed by the tag each client stream writes first.
async fn measure_arm(arm: &Arm, seed: u64) -> Vec<u64> {
    let n = arm.chunks.len();
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let slots: Arc<Vec<AtomicU64>> = Arc::new((0..n).map(|_| AtomicU64::new(0)).collect());
    let server_slots = Arc::clone(&slots);

    tasks
        .run(async move {
            let server_addr = spawn_mux_over_rtp_server_with_mss_via(
                &task_tx,
                false,
                rtp::udp::NO_FEC_MSS,
                move |mut read, mut write| {
                    let slots = Arc::clone(&server_slots);
                    async move {
                        // First 8 bytes identify the logical stream.
                        let mut tag = [0u8; 8];
                        if read.read_exact(&mut tag).await.is_err() {
                            return;
                        }
                        let idx = u64::from_le_bytes(tag) as usize;
                        let mut buf = vec![0u8; 64 * 1024];
                        loop {
                            match read.read(&mut buf).await {
                                Ok(0) => break,
                                Ok(got) => {
                                    if let Some(slot) = slots.get(idx) {
                                        slot.fetch_add(got as u64, Ordering::Relaxed);
                                    }
                                }
                                Err(_) => break,
                            }
                        }
                        let _ = write.shutdown();
                    }
                },
            )
            .await
            .unwrap();

            let cfg = NetemConfig {
                latency: Duration::from_millis(LINK_LATENCY_MS),
                rate: LINK_RATE_BPS,
                seed,
                ..NetemConfig::default()
            };
            let pair = NetemPair::spawn(server_addr, cfg.clone(), cfg).unwrap();
            let (read, write) = rtp_connect_via(&task_tx, pair.client_addr(), false).await;
            let opener = mux_client_connect_via(&task_tx, read, write);

            let stop = Arc::new(AtomicBool::new(false));
            for (i, &chunk_len) in arm.chunks.iter().enumerate() {
                let (stream_read, mut stream_write) = opener.open().await.unwrap();
                drop(stream_read);
                stream_write
                    .write_all(&(i as u64).to_le_bytes())
                    .await
                    .unwrap();
                let stop = Arc::clone(&stop);
                let chunk = vec![0xABu8; chunk_len];
                let throttle = if i == 0 { arm.throttled_bps } else { None };
                submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        let mut stream_write = stream_write;
                        while !stop.load(Ordering::Relaxed) {
                            if stream_write.write_all(&chunk).await.is_err() {
                                break;
                            }
                            if let Some(bps) = throttle {
                                let secs = chunk.len() as f64 / bps as f64;
                                tokio::time::sleep(Duration::from_secs_f64(secs)).await;
                            }
                        }
                        let _ = stream_write.shutdown();
                    }),
                );
                if !arm.skew.is_zero() && i + 1 < n {
                    tokio::time::sleep(arm.skew).await;
                }
            }

            tokio::time::sleep(WARMUP).await;
            let warm: Vec<u64> = slots.iter().map(|s| s.load(Ordering::Relaxed)).collect();
            tokio::time::sleep(STEADY).await;
            let hot: Vec<u64> = slots.iter().map(|s| s.load(Ordering::Relaxed)).collect();
            stop.store(true, Ordering::Relaxed);
            pair.stop();
            hot.iter()
                .zip(warm.iter())
                .map(|(h, w)| h.saturating_sub(*w))
                .collect::<Vec<u64>>()
        })
        .await
}

fn arms() -> Vec<Arm> {
    let bulk = |label, n: usize| Arm {
        label,
        chunks: vec![64 * 1024; n],
        skew: Duration::ZERO,
        throttled_bps: None,
        floor: Floor::HomogeneousJain,
    };
    vec![
        bulk("bulk2", 2),
        bulk("bulk3", 3),
        bulk("bulk4", 4),
        Arm {
            label: "bulk3_skew",
            chunks: vec![64 * 1024; 3],
            skew: Duration::from_millis(1500),
            throttled_bps: None,
            floor: Floor::HomogeneousJain,
        },
        Arm {
            label: "bulk3_low_rate_stream0",
            chunks: vec![64 * 1024; 3],
            skew: Duration::ZERO,
            throttled_bps: Some(200_000),
            floor: Floor::ReportOnly,
        },
        Arm {
            label: "hetero_1k_64k_64k",
            chunks: vec![1024, 64 * 1024, 64 * 1024],
            skew: Duration::ZERO,
            throttled_bps: None,
            floor: Floor::ByteFairJain,
        },
        Arm {
            label: "hetero_4k_64k_64k",
            chunks: vec![4096, 64 * 1024, 64 * 1024],
            skew: Duration::ZERO,
            throttled_bps: None,
            floor: Floor::ByteFairJain,
        },
    ]
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network fairness sweep; run with --ignored --nocapture --test-threads=1"]
async fn mux_stream_fairness_sweep() {
    for (arm_idx, arm) in arms().iter().enumerate() {
        for rep in 0..3u64 {
            let seed = 100 + rep * 7 + arm_idx as u64;
            let deltas = measure_arm(arm, seed).await;
            let total: u64 = deltas.iter().sum();
            let shares: Vec<f64> = deltas
                .iter()
                .map(|&v| {
                    if total == 0 {
                        0.0
                    } else {
                        v as f64 / total as f64
                    }
                })
                .collect();
            let j = jain(&deltas);
            eprintln!(
                "[mux-fair] {} seed={seed} bytes={deltas:?} shares={shares:?} jain={j:.4}",
                arm.label,
            );
            let min_share = shares.iter().cloned().fold(f64::INFINITY, f64::min);
            // The starvation guard applies to every arm: no stream may be
            // pinned near zero, whatever its message size or offered rate.
            assert!(
                deltas.iter().all(|&v| v > 0),
                "{} seed={seed}: a stream was starved to zero: {deltas:?}",
                arm.label,
            );
            assert!(
                min_share >= HETERO_MIN_SHARE,
                "{} seed={seed}: smallest share {min_share:.4} below {HETERO_MIN_SHARE}",
                arm.label,
            );
            match arm.floor {
                Floor::HomogeneousJain => {
                    assert!(
                        j >= HOMOGENEOUS_JAIN_FLOOR,
                        "{} seed={seed}: homogeneous Jain {j:.3} below {HOMOGENEOUS_JAIN_FLOOR}",
                        arm.label,
                    );
                }
                Floor::ByteFairJain => {
                    assert!(
                        j >= BYTE_FAIR_JAIN_FLOOR,
                        "{} seed={seed}: byte-fair Jain {j:.3} below {BYTE_FAIR_JAIN_FLOOR}",
                        arm.label,
                    );
                }
                Floor::ReportOnly => {}
            }
        }
    }
}
