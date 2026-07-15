//! Shared-bottleneck scenarios for `rtp` through [`netem_test::NetemPair`].
//!
//! These tests exercise the new [`SharedShaper`] primitive: multiple RTP flows
//! are routed through separate [`NetemPair`]s that share one serialization
//! clock, so they contend for a single bottleneck rate. They are marked
//! `#[ignore]` because they probe contested latency and need the in-flight
//! `rtp`/`mux` path dependencies; run them with:
//!
//! ```sh
//! cargo test --test shared_bottleneck -- --ignored --nocapture --test-threads=1
//! ```

#![allow(dead_code)]

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair, SharedShaper};
use support::{
    combined_stats, cyclic_payload, percentile, print_perf, rtp_connect, spawn_rtp_bulk_upload,
    spawn_rtp_byte_sink_server, spawn_rtp_echo_server,
};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::task::JoinHandle;

use crate::support::with_timeout;

mod support;

/// One-way delay applied to both directions in the shared-bottleneck
/// scenarios. The shared shaper sits on the client→server path; propagation
/// delay is applied *after* the shared bottleneck.
const OWD_MS: u64 = 50;

/// Build a per-flow [`NetemConfig`] with the desired OWD and no per-flow
/// rate. The actual bottleneck is supplied separately as a [`SharedShaper`].
fn flow_config(owd: Duration, seed: u64) -> NetemConfig {
    NetemConfig {
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

/// Round-robin echo flow: send a `msg_bytes` message every `gap` and measure
/// the echo RTT in milliseconds. Samples recorded before `warmup` are discarded.
async fn rr_echo_samples<R, W>(
    mut read: R,
    mut write: W,
    msg_bytes: usize,
    gap: Duration,
    run_for: Duration,
    warmup: Duration,
) -> Vec<f64>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let payload = cyclic_payload(msg_bytes);
    let mut samples = Vec::new();
    let mut interval = tokio::time::interval(gap);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut buf = vec![0u8; msg_bytes];
    let start = Instant::now();
    while start.elapsed() < run_for {
        interval.tick().await;
        let record = start.elapsed() >= warmup;
        let t0 = Instant::now();
        if write.write_all(&payload).await.is_err() {
            break;
        }
        if read.read_exact(&mut buf).await.is_err() {
            break;
        }
        if record {
            samples.push(t0.elapsed().as_secs_f64() * 1000.0);
        }
    }
    samples
}

/// Run a bulk upload through one shared-shaper [`NetemPair`] for `run_for`.
/// Returns a handle that completes when the run ends.
fn spawn_bulk_flow(
    proxy_client_addr: std::net::SocketAddr,
    payload: Arc<Vec<u8>>,
    run_for: Duration,
    stop: Arc<AtomicBool>,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let Ok(mut writer) = spawn_rtp_bulk_upload(proxy_client_addr, false).await else {
            return;
        };
        let start = Instant::now();
        let mut offset = 0usize;
        while start.elapsed() < run_for && !stop.load(Ordering::Relaxed) {
            match writer.write(&payload[offset..]).await {
                Ok(0) => break,
                Ok(n) => offset = (offset + n) % payload.len(),
                Err(_) => break,
            }
        }
    })
}

/// A/B scenario: sparse rr echo alone vs rr echo competing with a bulk upload,
/// both through a shared bottleneck.
///
/// `rate_bps`/`limit_bytes` define the shared shaper. `contested_run_s` is
/// the duration of the contested phase. The solo phase always runs 10 s.
///
/// Prints the contested/solo p99 inflation. Asserts structural bounds only;
/// the exact latency numbers are intentionally report-only until the in-flight
/// `rtp` congestion-control work lands.
async fn rr_under_bulk_ab(
    label: &str,
    rate_bps: u64,
    limit_bytes: u64,
    contested_run_s: u64,
    contested_p99_ceiling_ms: f64,
) {
    let owd = Duration::from_millis(OWD_MS);
    let msg_bytes = 2048usize;
    let gap = Duration::from_millis(100);
    let solo_run = Duration::from_secs(10);
    let contested_run = Duration::from_secs(contested_run_s);
    let warmup = Duration::from_secs(3);

    // ── solo phase ────────────────────────────────────────────────────────
    let echo_addr = spawn_rtp_echo_server(false).await.unwrap();
    let solo_pair = NetemPair::spawn_shared(
        echo_addr,
        flow_config(owd, 11),
        flow_config(owd, 12),
        Some(SharedShaper::new(rate_bps, limit_bytes)),
        None,
    )
    .unwrap();
    let solo_rr = with_timeout(
        Duration::from_secs(15),
        "solo rr setup",
        rtp_connect(solo_pair.client_addr(), false),
    )
    .await;
    let solo_samples =
        rr_echo_samples(solo_rr.0, solo_rr.1, msg_bytes, gap, solo_run, warmup).await;
    solo_pair.stop();

    // ── contested phase ───────────────────────────────────────────────────
    let (sink_addr, delivered) = spawn_rtp_byte_sink_server(false).await.unwrap();
    let echo_addr = spawn_rtp_echo_server(false).await.unwrap();
    let shaper = SharedShaper::new(rate_bps, limit_bytes);
    let bulk_pair = NetemPair::spawn_shared(
        sink_addr,
        flow_config(owd, 21),
        flow_config(owd, 22),
        Some(shaper.clone()),
        None,
    )
    .unwrap();
    let rr_pair = NetemPair::spawn_shared(
        echo_addr,
        flow_config(owd, 23),
        flow_config(owd, 24),
        Some(shaper.clone()),
        None,
    )
    .unwrap();

    let rr_conn = with_timeout(
        Duration::from_secs(15),
        "contested rr setup",
        rtp_connect(rr_pair.client_addr(), false),
    )
    .await;

    let bulk_payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let _bulk_handle = spawn_bulk_flow(
        bulk_pair.client_addr(),
        bulk_payload,
        contested_run,
        Arc::clone(&bulk_stop),
    );

    let contested_samples =
        rr_echo_samples(rr_conn.0, rr_conn.1, msg_bytes, gap, contested_run, warmup).await;

    // Signal the bulk flow to stop and let final bytes drain.
    bulk_stop.store(true, Ordering::Relaxed);
    tokio::time::sleep(Duration::from_secs(2)).await;
    let delivered_bytes = delivered.load(Ordering::Relaxed);
    bulk_pair.stop();
    rr_pair.stop();

    // ── analysis ──────────────────────────────────────────────────────────
    let mut solo = solo_samples;
    solo.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mut contested = contested_samples;
    contested.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let solo_p99 = percentile(&solo, 0.99);
    let contested_p99 = percentile(&contested, 0.99);

    eprintln!(
        "[shared_bneck {label}] solo p99={solo_p99:.1} ms n={solo_n} | contested p99={contested_p99:.1} ms n={contested_n}",
        solo_n = solo.len(),
        contested_n = contested.len()
    );
    if solo_p99 > 0.0 {
        eprintln!(
            "[shared_bneck {label}] contested/solo p99 inflation = {ratio:.2}x",
            ratio = contested_p99 / solo_p99
        );
    }

    assert!(
        solo_p99 <= 1500.0,
        "solo rr p99 {solo_p99:.1} ms exceeds 1500 ms slack ceiling"
    );
    assert!(
        contested_p99 <= contested_p99_ceiling_ms,
        "contested rr p99 {contested_p99:.1} ms exceeds {ceiling:.0} ms ceiling",
        ceiling = contested_p99_ceiling_ms
    );
    assert!(
        solo.len() >= 15,
        "solo phase should record at least 15 post-warmup samples, got {}",
        solo.len()
    );
    assert!(
        contested.len() >= 5,
        "contested phase should record at least 5 post-warmup samples, got {}",
        contested.len()
    );

    // Bulk goodput must stay inside (0, 1.15× link capacity].
    let cap_bps = rate_bps as f64;
    let cap_bytes_per_sec = cap_bps / 8.0;
    let goodput_bytes_per_sec = delivered_bytes as f64 / contested_run.as_secs_f64();
    assert!(
        goodput_bytes_per_sec > 0.0,
        "bulk flow should deliver a non-zero amount of data"
    );
    assert!(
        goodput_bytes_per_sec <= cap_bytes_per_sec * 1.15,
        "bulk goodput {goodput_bytes_per_sec:.0} B/s exceeds 1.15× cap {cap_bytes_per_sec:.0} B/s"
    );
    print_perf(
        &format!("{label} bulk goodput"),
        delivered_bytes as usize,
        contested_run,
    );
    eprintln!(
        "[shared_bneck {label}] pair stats = {:?}",
        combined_stats(&rr_pair)
    );
}

/// 10 Mbps / 128 KiB shared bottleneck: rr echo under a competing bulk flow.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn shared_bneck_rr_under_bulk_10mbps() {
    let _ = tokio::time::timeout(
        Duration::from_secs(180),
        rr_under_bulk_ab("10mbps", 10_000_000, 128 * 1024, 20, 8000.0),
    )
    .await;
}

/// 2 Mbps / 64 KiB shared bottleneck: rr echo under a competing bulk flow.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn shared_bneck_rr_under_bulk_2mbps() {
    let _ = tokio::time::timeout(
        Duration::from_secs(180),
        rr_under_bulk_ab("2mbps", 2_000_000, 64 * 1024, 20, 12000.0),
    )
    .await;
}

/// Late-joiner fairness probe: two bulk flows share one 10 Mbps / 128 KiB
/// shaper. Flow A starts at t=0; flow B joins at t=3 s. The test samples
/// per-flow goodput in 500 ms bins and reports overlap-window fairness
/// metrics. Only structural/liveness bounds are asserted; the precise fairness
/// ratios are left as report-only data until the in-flight `rtp` CC fairness
/// work lands.
///
/// TIGHTEN: once `rtp` CC fairness lands, assert `starve_max < 6` bins and a
/// tight convergence time for the slower flow.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn shared_bneck_late_joiner_fairness() {
    let rate_bps = 10_000_000u64;
    let limit_bytes = 128 * 1024u64;
    let owd = Duration::from_millis(20);
    let total_run = Duration::from_secs(18);
    let b_join = Duration::from_secs(3);
    let overlap_start = Duration::from_secs(6);
    let bin_width = Duration::from_millis(500);

    let (sink_a_addr, delivered_a) = spawn_rtp_byte_sink_server(false).await.unwrap();
    let (sink_b_addr, delivered_b) = spawn_rtp_byte_sink_server(false).await.unwrap();

    let shaper = SharedShaper::new(rate_bps, limit_bytes);
    let pair_a = NetemPair::spawn_shared(
        sink_a_addr,
        flow_config(owd, 31),
        flow_config(owd, 32),
        Some(shaper.clone()),
        None,
    )
    .unwrap();
    let pair_b = NetemPair::spawn_shared(
        sink_b_addr,
        flow_config(owd, 33),
        flow_config(owd, 34),
        Some(shaper.clone()),
        None,
    )
    .unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(AtomicBool::new(false));
    let _handle_a = spawn_bulk_flow(
        pair_a.client_addr(),
        Arc::clone(&payload),
        total_run,
        Arc::clone(&bulk_stop),
    );
    tokio::time::sleep(b_join).await;
    let _handle_b = spawn_bulk_flow(
        pair_b.client_addr(),
        Arc::clone(&payload),
        total_run - b_join,
        Arc::clone(&bulk_stop),
    );

    // Sample both counters every 500 ms for the whole run.
    let mut bins_a = Vec::new();
    let mut bins_b = Vec::new();
    let mut last_a = 0u64;
    let mut last_b = 0u64;
    let sample_start = Instant::now();
    while sample_start.elapsed() < total_run {
        tokio::time::sleep(bin_width).await;
        let now_a = delivered_a.load(Ordering::Relaxed);
        let now_b = delivered_b.load(Ordering::Relaxed);
        bins_a.push(now_a - last_a);
        bins_b.push(now_b - last_b);
        last_a = now_a;
        last_b = now_b;
    }

    // Wait for the final bytes to drain through the shaped c2s path.
    tokio::time::sleep(Duration::from_secs(2)).await;
    pair_a.stop();
    pair_b.stop();

    let total_a = delivered_a.load(Ordering::Relaxed);
    let total_b = delivered_b.load(Ordering::Relaxed);
    let total_bytes = total_a + total_b;
    let cap_bytes_per_sec = rate_bps as f64 / 8.0;
    let aggregate_goodput = total_bytes as f64 / total_run.as_secs_f64();

    eprintln!("[shared_bneck late_joiner] bins A: {bins_a:?}");
    eprintln!("[shared_bneck late_joiner] bins B: {bins_b:?}");

    // Overlap-window analysis: from t=6 s onward both flows are active.
    let overlap_bins = bins_a
        .len()
        .saturating_sub((overlap_start.as_millis() / bin_width.as_millis()) as usize);
    if overlap_bins > 0 {
        let start_idx = bins_a.len() - overlap_bins;
        let mut starve_runs = 0usize;
        let mut max_starve_runs = 0usize;
        let mut converged_at: Option<Duration> = None;
        for i in start_idx..bins_a.len() {
            let a = bins_a[i];
            let b = bins_b[i];
            let t = overlap_start + bin_width * (i - start_idx) as u32;
            // A flow is starved if its bin is tiny.
            let starved = a < 2048 || b < 2048;
            if starved {
                starve_runs += 1;
            } else {
                max_starve_runs = max_starve_runs.max(starve_runs);
                starve_runs = 0;
                // Convergence: slower flow is unstarved and gets >= 25% of the faster flow.
                let min_bin = a.min(b);
                let max_bin = a.max(b);
                if converged_at.is_none() && max_bin > 0 && min_bin * 4 >= max_bin {
                    converged_at = Some(t);
                }
            }
            eprintln!(
                "[shared_bneck late_joiner csv] t={:.1}s,a={a},b={b},starved={starved}",
                t.as_secs_f64()
            );
        }
        max_starve_runs = max_starve_runs.max(starve_runs);
        let overlap_ratio = if total_bytes > 0 {
            let overlap_a: u64 = bins_a[start_idx..].iter().sum();
            let overlap_b: u64 = bins_b[start_idx..].iter().sum();
            (overlap_a.min(overlap_b) * 2) as f64 / (overlap_a + overlap_b).max(1) as f64
        } else {
            0.0
        };
        eprintln!(
            "[shared_bneck late_joiner] overlap_ratio={overlap_ratio:.2} max_starve_runs={max_starve_runs} convergence={conv:?}",
            conv = converged_at
        );
    }

    eprintln!(
        "[shared_bneck late_joiner] total_a={total_a} total_b={total_b} aggregate={agg:.0} B/s cap={cap:.0} B/s",
        agg = aggregate_goodput,
        cap = cap_bytes_per_sec
    );

    // Structural / liveness assertions only.
    assert!(total_a > 0, "flow A must deliver data");
    assert!(total_b > 0, "flow B must deliver data");
    assert!(
        aggregate_goodput <= cap_bytes_per_sec * 1.15,
        "aggregate goodput {aggregate_goodput:.0} B/s exceeds 1.15× cap"
    );
    assert!(
        aggregate_goodput >= cap_bytes_per_sec * 0.20,
        "aggregate goodup {aggregate_goodput:.0} B/s below 20% cap floor"
    );
    let stats = combined_stats(&pair_a).forwarded + combined_stats(&pair_b).forwarded;
    eprintln!("[shared_bneck late_joiner] combined forwarded={stats}");
}
