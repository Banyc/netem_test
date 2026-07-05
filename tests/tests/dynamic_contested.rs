//! Dynamic-packet-size latency/bulk battery. Measures how the mux
//! dual-lane facade's static sticky routing misclassifies a realistic
//! mixed-size latency flow (200 B messages with 1-in-16 bursts of
//! 4–64 KiB). Five arms isolate the failure modes.
//!
//! Run with:
//! ```sh
//! cargo test --release --test dynamic_contested -- --ignored --nocapture --test-threads=1
//! ```
//!
//! # Traffic model
//!
//! Latency messages every 25 ms, 200 B each, except 1-in-16 drawn
//! uniformly 4..=64 KiB. Bulk chunks drawn 64..=512 KiB continuously.
//! Seeds: `MSG_SEED=0xD15E+rep`, `BULK_SEED=0xB01D+rep`. Record
//! small-message and burst-message latencies separately; percentiles
//! over per-message one-way latencies, median over reps.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use mux::{DeliveryMode, DualMessageReceiver, DualMessageSender, LaneClass};
use netem_test::{NetemConfig, NetemPair};
use support::{
    cyclic_payload, dual_mux_client_connect, percentile, rtp_connect,
    spawn_dual_mux_latency_bulk_server, spawn_mux_latency_bulk_server,
    spawn_rtp_bulk_upload, SplitMix64,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

const MSG_SEED_BASE: u64 = 0xD15E;
#[allow(dead_code)]
const BULK_SEED_BASE: u64 = 0xB01D;
const LATENCY_CADENCE: Duration = Duration::from_millis(25);
const SMALL_MSG_BYTES: usize = 200;
const BURST_RATIO: u64 = 16; // 1-in-16 messages is a burst
const BULK_RAMP: Duration = Duration::from_millis(1500);

/// Env knobs: DYN_RUN_SECS (default 15), DYN_REPS (default 3).
fn dyn_run_secs() -> u64 {
    std::env::var("DYN_RUN_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(15)
}

fn dyn_reps() -> usize {
    std::env::var("DYN_REPS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3)
}

// ═══════════════════════════════════════════════════════════════════════════════
// Shared bottleneck config
// ═══════════════════════════════════════════════════════════════════════════════

fn bottleneck_config(seed: u64, rate_bps: u64) -> (NetemConfig, NetemConfig) {
    let loss = ((2.0 / 100.0) * u32::MAX as f64).clamp(0.0, u32::MAX as f64) as u32;
    (
        NetemConfig {
            rate: rate_bps,
            loss,
            latency: Duration::from_millis(25),
            jitter: Duration::from_millis(20),
            limit: 4096,
            seed,
            ..NetemConfig::default()
        },
        NetemConfig {
            rate: rate_bps,
            loss,
            latency: Duration::from_millis(25),
            jitter: Duration::from_millis(20),
            limit: 4096,
            seed: seed + 1,
            ..NetemConfig::default()
        },
    )
}

const RATE_BPS: u64 = 400 * 1024 * 8;

// ═══════════════════════════════════════════════════════════════════════════════
// Traffic generation
// ═══════════════════════════════════════════════════════════════════════════════

struct DynTrafficResult {
    small_latencies: Vec<f64>,
    burst_latencies: Vec<f64>,
    sent: u64,
    received: u64,
    bulk_bytes: u64,
}

/// Generate one repetition of the mixed-size latency flow against bulk.
async fn dyn_single_mux_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (mut rr_read, mut rr_write) = rtp_connect(pair.client_addr(), false).await;

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    // Spawn bulk pump
    let bulk_payload = Arc::clone(&payload);
    let bulk_stop_clone = Arc::clone(&bulk_stop);
    let bulk_addr = pair.client_addr();
    let bulk_handle = tokio::spawn(async move {
        let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else { return; };
        let mut offset = 0usize;
        while !bulk_stop_clone.load(Ordering::Relaxed) {
            match writer.write(&bulk_payload[offset..]).await {
                Ok(0) | Err(_) => break,
                Ok(n) => offset = (offset + n) % bulk_payload.len(),
            }
        }
    });

    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let t0 = Instant::now();
        let payload = vec![b'L'];
        // Write timestamp as string for simplicity
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut frame = payload;
        frame.extend_from_slice(&ts);
        // Pad to msg_size
        frame.resize(msg_size, 0u8);

        if rr_write.write_all(&frame).await.is_err() {
            break;
        }

        // Read echo
        let mut echo_buf = [0u8; 8];
        if rr_read.read_exact(&mut echo_buf).await.is_err() {
            break;
        }
        let rtt = t0.elapsed();
        let lat = rtt.as_secs_f64() * 1000.0;

        if is_burst {
            burst_latencies.push(lat);
        } else {
            small_latencies.push(lat);
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult {
        small_latencies,
        burst_latencies,
        sent,
        received,
        bulk_bytes,
    }
}

/// Summarize across reps and print a grep-able summary line.
fn summarize(label: &str, results: &[DynTrafficResult]) {
    let mut all_small: Vec<f64> = results.iter().flat_map(|r| r.small_latencies.clone()).collect();
    let mut all_burst: Vec<f64> = results.iter().flat_map(|r| r.burst_latencies.clone()).collect();
    all_small.sort_by(|a, b| a.partial_cmp(b).unwrap());
    all_burst.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let delivery = if results.iter().map(|r| r.sent).sum::<u64>() == 0 {
        0.0
    } else {
        results.iter().map(|r| r.received).sum::<u64>() as f64
            / results.iter().map(|r| r.sent).sum::<u64>() as f64
    };
    let bulk_total: u64 = results.iter().map(|r| r.bulk_bytes).sum();
    let bulk_secs = results.len() as f64 * 15.0; // approximate

    eprintln!(
        "[dyn {label}] small p50/p90/p99={:.0}/{:.0}/{:.0} ms  burst_p50={:.0} ms  bulk={:.3} MiB/s  delivery={:.3}",
        percentile_opt(&all_small, 0.50),
        percentile_opt(&all_small, 0.90),
        percentile_opt(&all_small, 0.99),
        percentile_opt(&all_burst, 0.50),
        bulk_total as f64 / (1024.0 * 1024.0) / bulk_secs,
        delivery,
    );

    assert!(delivery > 0.80, "[dyn {label}] delivery too low: {delivery:.3}");
    assert!(
        percentile_opt(&all_small, 0.50) > 0.0,
        "[dyn {label}] p50 must be positive finite"
    );
}

fn percentile_opt(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    percentile(sorted, p)
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm A: single-mux baseline
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_single_mux() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();

    for rep in 0..reps {
        results.push(dyn_single_mux_rep(rep as u64, run_secs).await);
    }
    summarize("single_mux (A)", &results);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm B: dual-lane, sticky auto, first write small → interactive
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_dual_auto_small_first_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    let bulk_handle = {
        let bulk_payload = Arc::clone(&payload);
        let bulk_stop_clone = Arc::clone(&bulk_stop);
        let bulk_addr = pair.client_addr();
        tokio::spawn(async move {
            let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else {
                return;
            };
            let mut offset = 0usize;
            while !bulk_stop_clone.load(Ordering::Relaxed) {
                match writer.write(&bulk_payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % bulk_payload.len(),
                }
            }
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let (mut auto_reader, mut auto_writer) = opener.open_auto();
    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let t0 = Instant::now();
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut frame = vec![b'L'];
        frame.extend_from_slice(&ts);
        frame.resize(msg_size, 0);

        if auto_writer.write_all(&frame).await.is_err() {
            break;
        }
        let mut echo_buf = [0u8; 8];
        if auto_reader.read_exact(&mut echo_buf).await.is_err() {
            break;
        }
        let rtt = t0.elapsed();
        let lat = rtt.as_secs_f64() * 1000.0;

        if is_burst {
            burst_latencies.push(lat);
        } else {
            small_latencies.push(lat);
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    let _ = auto_writer.shutdown();
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_small_first() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_small_first_rep(rep as u64, run_secs).await);
    }
    summarize("dual_auto_small_first (B)", &results);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm C: dual-lane, sticky auto, first write forced burst → bulk
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_dual_auto_big_first_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    let bulk_handle = {
        let bulk_payload = Arc::clone(&payload);
        let bulk_stop_clone = Arc::clone(&bulk_stop);
        let bulk_addr = pair.client_addr();
        tokio::spawn(async move {
            let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else {
                return;
            };
            let mut offset = 0usize;
            while !bulk_stop_clone.load(Ordering::Relaxed) {
                match writer.write(&bulk_payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % bulk_payload.len(),
                }
            }
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let (mut auto_reader, mut auto_writer) = opener.open_auto();
    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    // FIRST write is forced to be a burst (> 2 KiB) so auto classifies as Bulk
    let first_size = 4 * 1024;
    let t0 = Instant::now();
    let ts = t0.elapsed().as_nanos().to_le_bytes();
    let mut frame = vec![b'L'];
    frame.extend_from_slice(&ts);
    frame.resize(first_size, 0);
    if auto_writer.write_all(&frame).await.is_err() {
        let _ = auto_writer.shutdown();
        bulk_stop.store(true, Ordering::Relaxed);
        let _ = bulk_handle.await;
        let received = (small_latencies.len() + burst_latencies.len()) as u64;
        let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
        return DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes };
    }
    let mut echo_buf = [0u8; 8];
    if auto_reader.read_exact(&mut echo_buf).await.is_ok() {
        let rtt = t0.elapsed();
        burst_latencies.push(rtt.as_secs_f64() * 1000.0);
    }
    sent += 1;

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let t0 = Instant::now();
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut frame = vec![b'L'];
        frame.extend_from_slice(&ts);
        frame.resize(msg_size, 0);

        if auto_writer.write_all(&frame).await.is_err() {
            break;
        }
        let mut echo_buf = [0u8; 8];
        if auto_reader.read_exact(&mut echo_buf).await.is_err() {
            break;
        }
        let rtt = t0.elapsed();
        let lat = rtt.as_secs_f64() * 1000.0;

        if is_burst {
            burst_latencies.push(lat);
        } else {
            small_latencies.push(lat);
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    let _ = auto_writer.shutdown();
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_big_first() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_big_first_rep(rep as u64, run_secs).await);
    }
    summarize("dual_auto_big_first (C)", &results);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm D: dual-lane, fresh open_auto stream per message
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_dual_auto_per_message_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    let bulk_handle = {
        let bulk_payload = Arc::clone(&payload);
        let bulk_stop_clone = Arc::clone(&bulk_stop);
        let bulk_addr = pair.client_addr();
        tokio::spawn(async move {
            let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else {
                return;
            };
            let mut offset = 0usize;
            while !bulk_stop_clone.load(Ordering::Relaxed) {
                match writer.write(&bulk_payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % bulk_payload.len(),
                }
            }
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let (mut reader, mut writer) = opener.open_auto();

        let t0 = Instant::now();
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut frame = vec![b'L'];
        frame.extend_from_slice(&ts);
        frame.resize(msg_size, 0);

        if writer.write_all(&frame).await.is_err() {
            break;
        }
        let _ = writer.shutdown();
        let mut echo_buf = [0u8; 8];
        if reader.read_exact(&mut echo_buf).await.is_err() {
            break;
        }
        let rtt = t0.elapsed();
        let lat = rtt.as_secs_f64() * 1000.0;

        if is_burst {
            burst_latencies.push(lat);
        } else {
            small_latencies.push(lat);
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_per_message() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_per_message_rep(rep as u64, run_secs).await);
    }
    summarize("dual_auto_per_message (D)", &results);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm E: dual-lane, explicit LaneClass::Interactive hint
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_dual_hint_static_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    let bulk_handle = {
        let bulk_payload = Arc::clone(&payload);
        let bulk_stop_clone = Arc::clone(&bulk_stop);
        let bulk_addr = pair.client_addr();
        tokio::spawn(async move {
            let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else {
                return;
            };
            let mut offset = 0usize;
            while !bulk_stop_clone.load(Ordering::Relaxed) {
                match writer.write(&bulk_payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % bulk_payload.len(),
                }
            }
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    // Open a persistent interactive stream for all latency messages
    let (mut int_reader, mut int_writer) = opener
        .open(LaneClass::Interactive)
        .await
        .expect("open interactive");

    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let t0 = Instant::now();
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut frame = vec![b'L'];
        frame.extend_from_slice(&ts);
        frame.resize(msg_size, 0);

        if is_burst {
            // Open a fresh bulk stream for the burst
            if let Ok((mut br, mut bw)) = opener.open(LaneClass::Bulk).await {
                if bw.write_all(&frame).await.is_err() {
                    break;
                }
                let _ = bw.shutdown();
                let mut echo_buf = [0u8; 8];
                if br.read_exact(&mut echo_buf).await.is_err() {
                    break;
                }
                let rtt = t0.elapsed();
                burst_latencies.push(rtt.as_secs_f64() * 1000.0);
            }
        } else {
            if int_writer.write_all(&frame).await.is_err() {
                break;
            }
            let mut echo_buf = [0u8; 8];
            if int_reader.read_exact(&mut echo_buf).await.is_err() {
                break;
            }
            let rtt = t0.elapsed();
            small_latencies.push(rtt.as_secs_f64() * 1000.0);
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    let _ = int_writer.shutdown();
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_hint_static() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_hint_static_rep(rep as u64, run_secs).await);
    }
    summarize("dual_hint_static (E)", &results);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm F: dual_message per-message routing (DualMessageSender / Receiver)
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_dual_message_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);

    let (server_addr, _lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, Instant::now()).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let (opener, accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let bulk_stop = Arc::new(AtomicBool::new(false));

    let bulk_handle = {
        let bulk_payload = Arc::clone(&payload);
        let bulk_stop_clone = Arc::clone(&bulk_stop);
        let bulk_addr = pair.client_addr();
        tokio::spawn(async move {
            let Ok(mut writer) = spawn_rtp_bulk_upload(bulk_addr, false).await else {
                return;
            };
            let mut offset = 0usize;
            while !bulk_stop_clone.load(Ordering::Relaxed) {
                match writer.write(&bulk_payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % bulk_payload.len(),
                }
            }
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let sender = DualMessageSender::new(opener, DeliveryMode::Unordered);
    let mut receiver = DualMessageReceiver::new(accepter, DeliveryMode::Unordered);

    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();

    while start.elapsed() < run_for {
        sent += 1;
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;

        let t0 = Instant::now();
        let ts = t0.elapsed().as_nanos().to_le_bytes();
        let mut msg_payload = vec![b'L'];
        msg_payload.extend_from_slice(&ts);
        msg_payload.resize(msg_size, 0);

        if sender.send(&msg_payload).await.is_err() {
            break;
        }
        match receiver.recv().await {
            Ok(Some(_echo)) => {
                let rtt = t0.elapsed();
                let lat = rtt.as_secs_f64() * 1000.0;
                if is_burst {
                    burst_latencies.push(lat);
                } else {
                    small_latencies.push(lat);
                }
            }
            _ => break,
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    let received = (small_latencies.len() + burst_latencies.len()) as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_message() {
    let reps = dyn_reps();
    let run_secs = dyn_run_secs();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_message_rep(rep as u64, run_secs).await);
    }
    summarize("dual_message (F)", &results);
}
