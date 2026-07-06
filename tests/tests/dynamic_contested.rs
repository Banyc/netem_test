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
    cyclic_payload, dual_mux_client_connect, mux_client_connect, percentile,
    spawn_dual_mux_latency_bulk_server, spawn_mux_latency_bulk_server, SplitMix64,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

const MSG_SEED_BASE: u64 = 0xD15E;
const BULK_RAMP: Duration = Duration::from_millis(1500);
const LATENCY_CADENCE: Duration = Duration::from_millis(25);
const SMALL_MSG_BYTES: usize = 200;
const BURST_RATIO: u64 = 16;
const RATE_BPS: u64 = 400 * 1024 * 8;

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

// ═══════════════════════════════════════════════════════════════════════════════
// Results and helpers
// ═══════════════════════════════════════════════════════════════════════════════

struct DynTrafficResult {
    small_latencies: Vec<f64>,
    burst_latencies: Vec<f64>,
    sent: u64,
    received: u64,
    bulk_bytes: u64,
}

const LATENCY_TAG: &[u8] = b"L";

fn make_latency_frame(msg_size: usize, base: Instant) -> Vec<u8> {
    assert!(msg_size >= 12, "msg_size {msg_size} too small for framing");
    let sent_us = base.elapsed().as_micros() as u64;
    let frame_len = msg_size as u32;
    let payload_bytes = msg_size - 12;
    let mut frame = Vec::with_capacity(msg_size);
    frame.extend_from_slice(&frame_len.to_le_bytes());
    frame.resize(4 + payload_bytes, b'X');
    frame.extend_from_slice(&sent_us.to_le_bytes());
    frame
}

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
    let bulk_secs = results.len() as f64 * dyn_run_secs() as f64;

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

async fn run_latency_flow(
    base: Instant,
    seed_base: u64,
    run_for: Duration,
    lat_write: &mut (impl tokio::io::AsyncWrite + Unpin),
    lat_rx: &mut tokio::sync::mpsc::UnboundedReceiver<f64>,
    tag_written: bool,
) -> (Vec<f64>, Vec<f64>, u64) {
    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
    let mut small_latencies = Vec::new();
    let mut burst_latencies = Vec::new();
    let mut sent = 0u64;
    let start = Instant::now();
    while start.elapsed() < run_for {
        let msg_size = if msg_rng.next_u64() % BURST_RATIO == 0 {
            msg_rng.uniform_usize(4 * 1024, 64 * 1024)
        } else {
            SMALL_MSG_BYTES
        };
        let is_burst = msg_size > SMALL_MSG_BYTES;
        let frame = make_latency_frame(msg_size, base);
        let write_buf = if !tag_written && sent == 0 {
            let mut tagged = Vec::with_capacity(LATENCY_TAG.len() + frame.len());
            tagged.extend_from_slice(LATENCY_TAG);
            tagged.extend_from_slice(&frame);
            tagged
        } else {
            frame
        };
        if lat_write.write_all(&write_buf).await.is_err() {
            break;
        }
        sent += 1;
        match lat_rx.recv().await {
            Some(lat) => {
                if is_burst {
                    burst_latencies.push(lat);
                } else {
                    small_latencies.push(lat);
                }
            }
            None => break,
        }
        tokio::time::sleep(LATENCY_CADENCE).await;
    }
    (small_latencies, burst_latencies, sent)
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arm A: single-mux baseline
// ═══════════════════════════════════════════════════════════════════════════════

async fn dyn_single_mux_rep(
    seed_base: u64,
    run_secs: u64,
) -> DynTrafficResult {
    let run_for = Duration::from_secs(run_secs);
    let (c2s, s2c) = bottleneck_config(100 + seed_base, RATE_BPS);
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let connected = rtp::udp::connect_without_handshake_with_mss(
        "0.0.0.0:0",
        &pair.client_addr().to_string(),
        None,
        false,
        rtp::udp::NO_FEC_MSS,
    )
    .await
    .unwrap();
    let (opener, _mux_spawner) = mux_client_connect(
        connected.read.into_async_read(),
        connected.write.into_async_write(),
    );

    let (mut _lat_read, mut lat_write) = opener.open().await.unwrap();
    let (mut bulk_read, mut bulk_write) = opener.open().await.unwrap();

    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        loop { match _lat_read.read(&mut buf).await { Ok(0) | Err(_) => break, _ => {} } }
    });

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match bulk_write.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = bulk_write.shutdown();
        })
    };
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        loop { match bulk_read.read(&mut buf).await { Ok(0) | Err(_) => break, _ => {} } }
    });

    let (small, burst, sent) =
        run_latency_flow(base, seed_base, run_for, &mut lat_write, &mut lat_rx, false).await;
    let _ = lat_write.shutdown();
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let received = (small.len() + burst.len()) as u64;

    DynTrafficResult { small_latencies: small, burst_latencies: burst, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_single_mux() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_single_mux_rep(rep as u64, dyn_run_secs()).await);
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
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match w.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = w.shutdown();
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let (auto_reader, mut auto_writer) = opener.open_auto();
    let (small, burst, sent) =
        run_latency_flow(base, seed_base, run_for, &mut auto_writer, &mut lat_rx, false).await;
    let _ = auto_writer.shutdown();
    drop(auto_reader);
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let received = (small.len() + burst.len()) as u64;

    DynTrafficResult { small_latencies: small, burst_latencies: burst, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_small_first() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_small_first_rep(rep as u64, dyn_run_secs()).await);
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
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match w.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = w.shutdown();
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let (auto_reader, mut auto_writer) = opener.open_auto();

    // FIRST write is forced to be large (> 2 KiB) so auto classifies as Bulk.
    let first_size: usize = 4 * 1024;
    let first_frame = make_latency_frame(first_size, base);
    let mut first_buf = Vec::with_capacity(LATENCY_TAG.len() + first_frame.len());
    first_buf.extend_from_slice(LATENCY_TAG);
    first_buf.extend_from_slice(&first_frame);
    if auto_writer.write_all(&first_buf).await.is_err()
    {
        let _ = auto_writer.shutdown();
        drop(auto_reader);
        bulk_stop.store(true, Ordering::Relaxed);
        let _ = bulk_handle.await;
        return DynTrafficResult {
            small_latencies: vec![], burst_latencies: vec![],
            sent: 0, received: 0,
            bulk_bytes: bulk_counter.load(Ordering::Relaxed),
        };
    }
    if let Some(lat) = lat_rx.recv().await {
        let rest_run = run_for.saturating_sub(base.elapsed());
        let (small, mut burst, mut sent) =
            run_latency_flow(base, seed_base, rest_run, &mut auto_writer, &mut lat_rx, true).await;
        burst.insert(0, lat);
        sent += 1;
        let _ = auto_writer.shutdown();
        drop(auto_reader);
        bulk_stop.store(true, Ordering::Relaxed);
        let _ = bulk_handle.await;
        let received = (small.len() + burst.len()) as u64;
        return DynTrafficResult {
            small_latencies: small, burst_latencies: burst,
            sent, received,
            bulk_bytes: bulk_counter.load(Ordering::Relaxed),
        };
    }

    let _ = auto_writer.shutdown();
    drop(auto_reader);
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    DynTrafficResult {
        small_latencies: vec![], burst_latencies: vec![],
        sent: 1, received: 0,
        bulk_bytes: bulk_counter.load(Ordering::Relaxed),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_big_first() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_big_first_rep(rep as u64, dyn_run_secs()).await);
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
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match w.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = w.shutdown();
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
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
        let frame = make_latency_frame(msg_size, base);

        let (reader, mut writer) = opener.open_auto();
        let write_buf = [LATENCY_TAG, &frame[..]].concat();
        if writer.write_all(&write_buf).await.is_err()
        {
            break;
        }
        let _ = writer.shutdown();
        drop(reader);

        match lat_rx.recv().await {
            Some(lat) => {
                if is_burst {
                    burst_latencies.push(lat);
                } else {
                    small_latencies.push(lat);
                }
            }
            None => break,
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let received = (small_latencies.len() + burst_latencies.len()) as u64;

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_auto_per_message() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_auto_per_message_rep(rep as u64, dyn_run_secs()).await);
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
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (opener, _accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match w.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = w.shutdown();
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    let (int_reader, mut int_writer) =
        opener.open(LaneClass::Interactive).await.expect("open interactive");
    int_writer.write_all(LATENCY_TAG).await.expect("write latency tag");

    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
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
        let frame = make_latency_frame(msg_size, base);

        if is_burst {
            if let Ok((_, mut bw)) = opener.open(LaneClass::Bulk).await {
                let _ = bw.write_all(LATENCY_TAG).await;
                if bw.write_all(&frame).await.is_err() {
                    break;
                }
                let _ = bw.shutdown();
                match lat_rx.recv().await {
                    Some(lat) => burst_latencies.push(lat),
                    None => break,
                }
            }
        } else {
            if int_writer.write_all(&frame).await.is_err() {
                break;
            }
            match lat_rx.recv().await {
                Some(lat) => small_latencies.push(lat),
                None => break,
            }
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    let _ = int_writer.shutdown();
    drop(int_reader);
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let received = (small_latencies.len() + burst_latencies.len()) as u64;

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_hint_static() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_hint_static_rep(rep as u64, dyn_run_secs()).await);
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
    let base = Instant::now();

    let (server_addr, mut lat_rx, bulk_counter) =
        spawn_dual_mux_latency_bulk_server(false, base).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (opener, accepter, _spawner) =
        dual_mux_client_connect(pair.client_addr(), false).await.unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match w.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = w.shutdown();
        })
    };

    tokio::time::sleep(BULK_RAMP).await;

    // Open a tagged stream for the DualMessage lane.
    {
        let (_, mut tag_w) = opener.open(LaneClass::Interactive).await
            .expect("open interactive for dual_message");
        tag_w.write_all(LATENCY_TAG).await.expect("write latency tag");
        let _ = tag_w.shutdown();
    }

    let sender = DualMessageSender::new(opener, DeliveryMode::Unordered);
    let mut receiver = DualMessageReceiver::new(accepter, DeliveryMode::Unordered);

    let mut msg_rng = SplitMix64::new(MSG_SEED_BASE + seed_base);
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
        let frame = make_latency_frame(msg_size, base);

        if sender.send(&frame).await.is_err() {
            break;
        }
        match receiver.recv().await {
            Ok(Some(_echo)) => {}
            _ => break,
        }

        match lat_rx.recv().await {
            Some(lat) => {
                if is_burst {
                    burst_latencies.push(lat);
                } else {
                    small_latencies.push(lat);
                }
            }
            None => break,
        }

        tokio::time::sleep(LATENCY_CADENCE).await;
    }

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let received = (small_latencies.len() + burst_latencies.len()) as u64;

    DynTrafficResult { small_latencies, burst_latencies, sent, received, bulk_bytes }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dyn_dual_message() {
    let reps = dyn_reps();
    let mut results = Vec::new();
    for rep in 0..reps {
        results.push(dyn_dual_message_rep(rep as u64, dyn_run_secs()).await);
    }
    summarize("dual_message (F)", &results);
}
