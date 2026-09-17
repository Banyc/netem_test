//! Shared-bottleneck scenarios for `rtp` through [`netem_test::NetemPair`].
//!
//! These tests exercise the new [`BottleneckShaper`] primitive: multiple RTP flows
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

use netem_test::{BottleneckShaper, NetemConfig, NetemPair};
use support::payload::cyclic_payload;
use support::rtp::{
    spawn_rtp_bulk_upload_with_lane_via, spawn_rtp_byte_sink_server_via, spawn_rtp_echo_server_via,
};
use support::stats::{HolSummary, combined_stats, print_perf, summarize};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::support::payload::with_timeout;
use crate::support::submit_test_task;

mod support;

/// One-way delay applied to both directions in the shared-bottleneck
/// scenarios. The shared shaper sits on the client→server path; propagation
/// delay is applied *after* the shared bottleneck.
const OWD_MS: u64 = 50;

/// Per-round-trip bound for the interactive echo. A round trip that exceeds
/// this is not counted as a sample, but it no longer ends the phase: the
/// committed request echo is drained and the phase continues, so one slow reply
/// under host or bottleneck pressure cannot discard the rest of the window. The
/// bound still guarantees the scenario terminates when the session is dead
/// instead of hanging until an outer timeout erases every report line.
const ROUND_TRIP_BOUND: Duration = Duration::from_secs(4);

/// Time allowed to resynchronize the byte stream after a bounded round trip
/// exceeded [`ROUND_TRIP_BOUND`]: the committed-but-unread echo bytes must be
/// drained before the next attempt, otherwise every later read would consume a
/// stale prefix and report a falsely low latency. A stream that cannot be
/// resynchronized within this budget is treated as dead.
const DRAIN_BOUND: Duration = Duration::from_secs(4);

/// Timing budget for the bounded echo: how long one round trip may take before
/// it is treated as a slow, unsampled attempt, and how long resynchronization
/// after such an attempt may take.
#[derive(Clone, Copy, Debug)]
struct EchoBounds {
    round_trip: Duration,
    drain: Duration,
}

impl EchoBounds {
    /// Production timing; tests shorten it to exercise slow replies quickly.
    const PRODUCTION: Self = Self {
        round_trip: ROUND_TRIP_BOUND,
        drain: DRAIN_BOUND,
    };
}

/// Connect an `rtp` client for the interactive echo, on an explicit `Shared`
/// lane: it shares the bottleneck with the competing bulk upload.
///
/// The supervisor keepalive is submitted as an ordinary (transient) task. The
/// body owns the connection read/write halves for exactly the measured window
/// and closes the session when `rr_echo_samples` consumes them; a REQUIRED
/// submission would treat that deliberate close as an early session death and
/// race the body's own completion. A session that dies during the window still
/// shows up as a short sample count and a delivery shortfall.
async fn rtp_connect_transient(
    tx: &crate::support::TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
) -> (
    impl AsyncRead + Unpin + Send + use<>,
    impl AsyncWrite + Unpin + Send + use<>,
) {
    let connected = rtp::udp::connect_with(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            fec: false,
            mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),
            // The interactive echo is a `Shared` lane by definition: it shares
            // the bottleneck with the competing bulk upload.
            congestion_lane: rtp::CongestionLane::Shared,
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    let read = connected.read.into_async_read();
    let write = connected.write.into_async_write();
    // The supervisor owns the session drivers; a transient submission keeps it
    // alive until the session ends (the expected teardown after the phase).
    submit_test_task(
        tx,
        Box::pin(async move {
            let _ = connected.supervisor.await;
        }),
    );
    (read, write)
}

/// Build a per-flow [`NetemConfig`] with the desired OWD and no per-flow
/// rate. The actual bottleneck is supplied separately as a [`BottleneckShaper`].
fn flow_config(owd: Duration, seed: u64) -> NetemConfig {
    NetemConfig {
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

/// Outcome of one bounded stop-and-wait echo attempt.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EchoAttempt {
    /// The full echo arrived within the bound; the duration is the round trip.
    Complete(Duration),
    /// The attempt exceeded its bound and the committed request echo was
    /// drained, so the byte stream is aligned for the next attempt. The phase
    /// continues instead of ending on one slow reply.
    Slow,
    /// The stream ended or errored; the phase cannot continue.
    Failed,
}

/// Send `payload` and read its echo, bounding the whole attempt.
///
/// The write is tracked byte by byte because `AsyncWrite::write` commits
/// exactly the bytes it returns and commits nothing while it is still pending;
/// a timeout therefore never hides how much of the request reached the peer.
/// When the bound fires, the committed request echo is drained under
/// `drain_bound` so the next attempt starts on a message boundary, and
/// [`EchoAttempt::Slow`] is returned rather than ending the phase. A stream
/// that cannot be resynchronized is [`EchoAttempt::Failed`].
async fn bounded_echo<R, W>(
    read: &mut R,
    write: &mut W,
    payload: &[u8],
    buf: &mut [u8],
    bound: Duration,
    drain_bound: Duration,
) -> EchoAttempt
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    assert!(
        buf.len() >= payload.len(),
        "echo buffer {} bytes is smaller than the {} byte payload",
        buf.len(),
        payload.len(),
    );
    let started = Instant::now();
    let deadline = started + bound;

    // Commit the request, tracking exactly the bytes the writer accepted.
    let mut committed = 0usize;
    while committed < payload.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, write.write(&payload[committed..])).await {
            Ok(Ok(0)) | Ok(Err(_)) => return EchoAttempt::Failed,
            Ok(Ok(n)) => committed += n,
            Err(_) => break,
        }
    }

    // Read the echo of exactly the committed bytes.
    let mut consumed = 0usize;
    while consumed < committed {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, read.read(&mut buf[consumed..committed])).await {
            Ok(Ok(0)) | Ok(Err(_)) => return EchoAttempt::Failed,
            Ok(Ok(n)) => consumed += n,
            Err(_) => break,
        }
    }

    if committed == payload.len() && consumed == committed {
        return EchoAttempt::Complete(started.elapsed());
    }

    // Slow or short: drain the remaining committed echo so the stream stays
    // aligned before the next attempt.
    let drain_deadline = Instant::now() + drain_bound;
    while consumed < committed {
        let remaining = drain_deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return EchoAttempt::Failed;
        }
        match tokio::time::timeout(remaining, read.read(&mut buf[consumed..committed])).await {
            Ok(Ok(0)) | Ok(Err(_)) => return EchoAttempt::Failed,
            Ok(Ok(n)) => consumed += n,
            Err(_) => return EchoAttempt::Failed,
        }
    }
    EchoAttempt::Slow
}

/// Round-robin echo flow: send a `msg_bytes` message every `gap` and measure
/// the echo RTT in milliseconds. Samples recorded before `warmup` are
/// discarded. Returns the measured samples and the number of post-warmup
/// round trips attempted; the last attempt may have timed out, so
/// `samples.len()` is the delivered count.
async fn rr_echo_samples<R, W>(
    read: R,
    write: W,
    msg_bytes: usize,
    gap: Duration,
    run_for: Duration,
    warmup: Duration,
) -> (Vec<f64>, u64)
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    rr_echo_samples_with_bounds(
        read,
        write,
        msg_bytes,
        gap,
        run_for,
        warmup,
        EchoBounds::PRODUCTION,
    )
    .await
}

/// [`rr_echo_samples`] with explicit round-trip and drain bounds, so a test can
/// exercise the slow-reply resynchronization without waiting seconds.
async fn rr_echo_samples_with_bounds<R, W>(
    mut read: R,
    mut write: W,
    msg_bytes: usize,
    gap: Duration,
    run_for: Duration,
    warmup: Duration,
    bounds: EchoBounds,
) -> (Vec<f64>, u64)
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let payload = cyclic_payload(msg_bytes);
    // `cyclic_payload` trims the buffer to a whole number of 251-byte periods
    // (a 2048-byte request yields 2008 bytes), so the read buffer must match
    // the *payload* length. Reading `msg_bytes` instead stalls every round trip
    // forever waiting for the 40 trailing bytes that never arrive.
    let msg_len = payload.len();
    let mut samples = Vec::new();
    let mut sent = 0u64;
    let mut interval = tokio::time::interval(gap);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut buf = vec![0u8; msg_len];
    let start = Instant::now();
    while start.elapsed() < run_for {
        interval.tick().await;
        let record = start.elapsed() >= warmup;
        if record {
            sent += 1;
        }
        match bounded_echo(
            &mut read,
            &mut write,
            &payload,
            &mut buf,
            bounds.round_trip,
            bounds.drain,
        )
        .await
        {
            EchoAttempt::Complete(rtt) => {
                if record {
                    samples.push(rtt.as_secs_f64() * 1000.0);
                }
            }
            // The attempt was slow but resynchronized: keep the phase alive
            // instead of ending it on one slow reply.
            EchoAttempt::Slow => {}
            EchoAttempt::Failed => break,
        }
    }
    (samples, sent)
}

/// Print the p50/p90/p99/max latency summary plus a per-sample CSV line.
fn print_latency(label: &str, summary: &HolSummary, samples: &[f64]) {
    eprintln!(
        "[shared_bneck {label}] n={} p50={:.1} p90={:.1} p99={:.1} max={:.1} ms | delivery={:.1}%",
        summary.received,
        summary.p50,
        summary.p90,
        summary.p99,
        summary.max,
        summary.delivery_pct * 100.0,
    );
    let csv = samples
        .iter()
        .map(|x| format!("{x:.1}"))
        .collect::<Vec<_>>()
        .join(",");
    eprintln!("[shared_bneck {label} samples_ms] {csv}");
}

/// Run a bulk upload through one shared-shaper [`NetemPair`] for `run_for`.
///
/// `tx` (the bounded task-submission handle) owns the bulk upload's
/// read-keepalive and the pump task; the pump completes once `run_for`
/// elapses or `stop` is set and is drained by the reaper.
async fn spawn_bulk_flow(
    tx: &crate::support::TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
    payload: Arc<Vec<u8>>,
    run_for: Duration,
    stop: Arc<AtomicBool>,
    congestion_lane: rtp::CongestionLane,
) {
    let Ok(mut writer) =
        spawn_rtp_bulk_upload_with_lane_via(tx, proxy_client_addr, false, congestion_lane).await
    else {
        return;
    };
    submit_test_task(
        tx,
        Box::pin(async move {
            let start = Instant::now();
            let mut offset = 0usize;
            while start.elapsed() < run_for && !stop.load(Ordering::Relaxed) {
                match writer.write(&payload[offset..]).await {
                    Ok(0) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                    Err(_) => break,
                }
            }
        }),
    );
}

/// A/B scenario: sparse rr echo alone vs rr echo competing with a bulk upload,
/// both through a shared bottleneck.
///
/// `rate_bps`/`limit_bytes` define the shared shaper. `contested_run_s` is
/// the duration of the contested phase. The solo baseline phase runs 8 s; both
/// phases discard a 2 s warmup.
///
/// Prints the solo and contested p50/p90/p99/max latency, the per-sample
/// latency CSV, the bulk delivery/goodput, and the wire counters for both
/// pairs, so a `Shared` bulk upload can be compared against a `Dedicated` one
/// on the same echo. Asserts structural bounds only.
async fn rr_under_bulk_ab(
    label: &str,
    rate_bps: u64,
    limit_bytes: u64,
    contested_run_s: u64,
    contested_p99_ceiling_ms: f64,
    bulk_lane: rtp::CongestionLane,
) {
    let owd = Duration::from_millis(OWD_MS);
    let msg_bytes = 2048usize;
    let gap = Duration::from_millis(100);
    let solo_run = Duration::from_secs(8);
    let contested_run = Duration::from_secs(contested_run_s);
    let warmup = Duration::from_secs(2);

    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);

    // The whole flow — solo setup + 8 s baseline + teardown, then
    // contested setup + measurement + teardown — runs inside one actively
    // driven scope. Every dynamic child (echo/sink servers, session
    // supervisors, the bulk pump) is submitted through the bounded `task_tx`
    // handle, so the reaper actively drives them (and surfaces panics)
    // throughout, including during the long solo measurement.
    let (
        solo_samples,
        solo_sent,
        contested_samples,
        contested_sent,
        delivered_bytes,
        rr_pair,
        bulk_pair,
        shaper,
    ) = tasks
        .run(async {
            // solo phase
            let echo_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();
            let solo_pair = NetemPair::spawn_shared(
                echo_addr,
                flow_config(owd, 11),
                flow_config(owd, 12),
                Some(BottleneckShaper::new(rate_bps, limit_bytes)),
                None,
            )
            .unwrap();
            let solo_rr = with_timeout(
                Duration::from_secs(15),
                "solo rr setup",
                rtp_connect_transient(&task_tx, solo_pair.client_addr()),
            )
            .await;
            let (solo_samples, solo_sent) =
                rr_echo_samples(solo_rr.0, solo_rr.1, msg_bytes, gap, solo_run, warmup).await;
            solo_pair.stop();

            // contested phase
            let (sink_addr, delivered) = spawn_rtp_byte_sink_server_via(&task_tx, false)
                .await
                .unwrap();
            let echo_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();
            let shaper = BottleneckShaper::new(rate_bps, limit_bytes);
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
                rtp_connect_transient(&task_tx, rr_pair.client_addr()),
            )
            .await;

            let bulk_payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
            let bulk_stop = Arc::new(AtomicBool::new(false));
            spawn_bulk_flow(
                &task_tx,
                bulk_pair.client_addr(),
                bulk_payload,
                contested_run,
                Arc::clone(&bulk_stop),
                bulk_lane,
            )
            .await;

            let (contested_samples, contested_sent) =
                rr_echo_samples(rr_conn.0, rr_conn.1, msg_bytes, gap, contested_run, warmup).await;

            // Signal the bulk flow to stop and let final bytes drain.
            bulk_stop.store(true, Ordering::Relaxed);
            tokio::time::sleep(Duration::from_secs(2)).await;
            let delivered_bytes = delivered.load(Ordering::Relaxed);
            (
                solo_samples,
                solo_sent,
                contested_samples,
                contested_sent,
                delivered_bytes,
                rr_pair,
                bulk_pair,
                shaper,
            )
        })
        .await;
    // Stop the contested pairs after the scope has reaped its task children.
    // The echo session is a transient child, so the epilog cancels its
    // supervisor first and the pair threads are joined here; stopping a pair
    // from inside the body would tear the session down while the reaper is
    // still polling it.
    rr_pair.stop();
    bulk_pair.stop();
    // ── analysis ──────────────────────────────────────────────────────────
    let solo_summary = summarize(
        solo_samples.clone(),
        solo_sent,
        solo_samples.len() as u64,
        0,
        0.0,
    );
    let contested_summary = summarize(
        contested_samples.clone(),
        contested_sent,
        contested_samples.len() as u64,
        delivered_bytes,
        contested_run.as_secs_f64(),
    );

    print_latency(&format!("{label} solo"), &solo_summary, &solo_samples);
    print_latency(
        &format!("{label} contested"),
        &contested_summary,
        &contested_samples,
    );
    let inflation = contested_summary.p99 / solo_summary.p99.max(f64::EPSILON);
    eprintln!("[shared_bneck {label}] contested/solo p99 inflation = {inflation:.2}x");

    let cap_bytes_per_sec = rate_bps as f64 / 8.0;
    let goodput_bytes_per_sec = delivered_bytes as f64 / contested_run.as_secs_f64();
    eprintln!(
        "[shared_bneck {label}] bulk delivery={delivered_bytes} bytes goodput={goodput_bytes_per_sec:.0} B/s cap={cap_bytes_per_sec:.0} B/s ({pct:.0}%)",
        pct = goodput_bytes_per_sec / cap_bytes_per_sec * 100.0,
    );
    eprintln!(
        "[shared_bneck {label}] rr wire = {:?}",
        combined_stats(&rr_pair)
    );
    eprintln!(
        "[shared_bneck {label}] bulk wire = {:?}",
        combined_stats(&bulk_pair)
    );
    eprintln!(
        "[shared_bneck {label}] shaper rate={} bps dropped={} backlog_now={} bytes",
        shaper.rate_bps(),
        shaper.dropped(),
        shaper.backlog_bytes(Instant::now()),
    );

    assert!(
        solo_summary.p99 <= 1500.0,
        "solo rr p99 {:.1} ms exceeds 1500 ms slack ceiling",
        solo_summary.p99
    );
    assert!(
        contested_summary.p99 <= contested_p99_ceiling_ms,
        "contested rr p99 {:.1} ms exceeds {contested_p99_ceiling_ms:.0} ms ceiling",
        contested_summary.p99
    );
    assert!(
        solo_summary.received >= 15,
        "solo phase should record at least 15 post-warmup samples, got {}",
        solo_summary.received
    );
    assert!(
        contested_summary.received >= 15,
        "contested phase should record at least 15 post-warmup samples, got {}",
        contested_summary.received
    );

    // Bulk goodput must stay inside (0, 1.15× link capacity].
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
}

/// 10 Mbps / 128 KiB shared bottleneck: rr echo under a competing bulk flow.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "probes contested latency and needs the in-flight rtp/mux path dependencies; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn shared_bneck_rr_under_bulk_10mbps() {
    with_timeout(
        Duration::from_secs(90),
        "shared_bneck 10mbps",
        rr_under_bulk_ab(
            "10mbps",
            10_000_000,
            128 * 1024,
            15,
            8000.0,
            rtp::CongestionLane::default(),
        ),
    )
    .await;
}

/// 10 Mbps / 128 KiB shared bottleneck: the interactive rr echo is `Shared` (as
/// always) while the competing bulk upload declares the dedicated-pipe intent.
/// The companion [`shared_bneck_rr_under_bulk_10mbps`] runs the same scenario
/// with a `Shared` bulk upload.  Comparing the two printed p99/goodput pairs is
/// what decides whether the dedicated tuning is safe when the two lanes share a
/// link; the assertions are the same structural bounds.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "probes contested latency and needs the in-flight rtp/mux path dependencies; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn shared_bneck_rr_under_dedicated_bulk_10mbps() {
    with_timeout(
        Duration::from_secs(90),
        "shared_bneck 10mbps-dedicated-bulk",
        rr_under_bulk_ab(
            "10mbps-dedicated-bulk",
            10_000_000,
            128 * 1024,
            15,
            8000.0,
            rtp::CongestionLane::Dedicated,
        ),
    )
    .await;
}

/// 2 Mbps / 64 KiB shared bottleneck: rr echo under a competing bulk flow.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "probes contested latency and needs the in-flight rtp/mux path dependencies; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn shared_bneck_rr_under_bulk_2mbps() {
    with_timeout(
        Duration::from_secs(90),
        "shared_bneck 2mbps",
        rr_under_bulk_ab(
            "2mbps",
            2_000_000,
            64 * 1024,
            15,
            12000.0,
            rtp::CongestionLane::default(),
        ),
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
#[ignore = "probes contested latency and needs the in-flight rtp/mux path dependencies; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn shared_bneck_late_joiner_fairness() {
    let rate_bps = 10_000_000u64;
    let limit_bytes = 128 * 1024u64;
    let owd = Duration::from_millis(20);
    let total_run = Duration::from_secs(18);
    let b_join = Duration::from_secs(3);
    let overlap_start = Duration::from_secs(6);
    let bin_width = Duration::from_millis(500);

    let mut tasks = support::TestScope::new();
    // The whole scenario — sink servers, pairs, both bulk flows, and the
    // late-joiner gap — runs inside one `run` body so the reaper is already
    // polling: a setup-time failure surfaces immediately instead of waiting
    // for the measurement body.
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);

    let (bins_a, bins_b, delivered_a, delivered_b, pair_a, pair_b) = tasks
        .run(async {
            // The sink servers are spawned through the bounded handle (no
            // `&mut TestScope` inside the run body).
            let (sink_a_addr, delivered_a) = spawn_rtp_byte_sink_server_via(&task_tx, false)
                .await
                .unwrap();
            let (sink_b_addr, delivered_b) = spawn_rtp_byte_sink_server_via(&task_tx, false)
                .await
                .unwrap();

            let shaper = BottleneckShaper::new(rate_bps, limit_bytes);
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
            spawn_bulk_flow(
                &task_tx,
                pair_a.client_addr(),
                Arc::clone(&payload),
                total_run,
                Arc::clone(&bulk_stop),
                rtp::CongestionLane::default(),
            )
            .await;
            tokio::time::sleep(b_join).await;
            spawn_bulk_flow(
                &task_tx,
                pair_b.client_addr(),
                Arc::clone(&payload),
                total_run - b_join,
                Arc::clone(&bulk_stop),
                rtp::CongestionLane::default(),
            )
            .await;

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
            (bins_a, bins_b, delivered_a, delivered_b, pair_a, pair_b)
        })
        .await;

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

/// Regression: one reply slower than the round-trip bound must not end the
/// phase. Before the resynchronizing round trip, the first slow reply broke the
/// echo loop and the phase reported zero post-warmup samples; with it, the
/// phase drains the committed echo and keeps measuring the later prompt
/// replies.
#[tokio::test(flavor = "multi_thread")]
async fn a_slow_reply_resynchronizes_instead_of_ending_the_phase() {
    let (client, server) = tokio::io::duplex(1 << 20);
    let (client_read, client_write) = tokio::io::split(client);
    let (mut server_read, mut server_write) = tokio::io::split(server);

    // Echo server: delay the first reply past the round-trip bound, echo every
    // later read promptly. Mirrors the RTP echo server, which echoes each
    // partial read rather than waiting for a whole message.
    let echo = tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        let mut first = true;
        loop {
            match server_read.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if first {
                        first = false;
                        tokio::time::sleep(Duration::from_millis(300)).await;
                    }
                    if server_write.write_all(&buf[..n]).await.is_err() {
                        break;
                    }
                }
            }
        }
    });

    let (samples, sent) = rr_echo_samples_with_bounds(
        client_read,
        client_write,
        1024,
        Duration::from_millis(20),
        Duration::from_millis(700),
        Duration::ZERO,
        EchoBounds {
            round_trip: Duration::from_millis(150),
            drain: Duration::from_secs(2),
        },
    )
    .await;
    echo.abort();

    assert!(
        sent > 1,
        "the phase must attempt more than one round trip, got {sent}"
    );
    assert!(
        samples.len() >= 5,
        "a reply slower than the bound must resynchronize and keep sampling, \
         got {} of {sent} attempts delivered",
        samples.len()
    );
}
