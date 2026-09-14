//! Interactive-message latency-jitter probe for the game-relay topology:
//! `game client -TCP-> access-server -rtp_mux(rtp+mux)-> proxy-server -TCP->
//! game server`.
//!
//! A game client's ping graph shows a steady one-way latency with regular
//! sudden spikes. Three mechanisms are suspected:
//!
//! (a) global head-of-line blocking at the RTP frame layer when a packet is
//!     lost/reordered (frame-mode delivery withholds every frame behind an
//!     unrepaired front hole; each mux frame maps 1:1 to an RTP frame);
//! (b) congestion-controller bufferbloat oscillation (a `+50%` probe vs a
//!     `-10%` drain);
//! (c) bursty sending (a 64-packet pacer burst floor; retransmits bypass the
//!     token bucket).
//!
//! These scenarios REPRODUCE and QUANTIFY the latency jitter so fixes can be
//! validated. They are deliberately report-only measurement harnesses: the
//! assertions are loose sanity guards, and the `HolSummary` printed with
//! `--nocapture` is the deliverable, not a pass/fail gate.
//!
//! Passes:
//! * [`jitter_decomposition`] — runs `solo`, `loss-only`, `bulk-only`, and
//!   `bulk+loss` on the same seeded impairment and prints the loss-vs-queueing
//!   decomposition (`loss_delta`, `queue_delta`, `combined_delta`, and whether
//!   combined is additive or super-additive) for p50/p90/p99/max and for the
//!   episode/spike counts. This is the separation of the loss tail from the
//!   queue tail. The same four arms are then repeated in RTP frame-delivery
//!   mode (`solo_frame`, `loss_frame`, `bulk_frame`, `bulk_and_loss_frame`) to
//!   measure the deployment's `frame_reassembly` path alongside byte-stream,
//!   and once more with receiver-side fast-forward
//!   ([`jitter_frame_reorder_decomposition`]: `solo_frame_reorder`,
//!   `loss_frame_reorder`, `bulk_frame_reorder`, `bulk_and_loss_frame_reorder`)
//!   to measure the deployment's interactive-lane `allow_reorder` mode against
//!   the strict frame-delivery table.
//! * [`jitter_frame_reorder_fec_arms`] — the deployment's real interactive-lane
//!   configuration: frame fast-forward **and** FEC (prompt tuning, permissive
//!   loss gate) at 2% loss, printed beside the strict-frame + FEC and the
//!   reorder FEC-off arms, with the FEC counters and the c2s wire bulk load so
//!   the offered load can be checked matched across all three.
//! * [`jitter_fec_arms_2pct`] / [`jitter_fec_arms_6pct`] — the interactive
//!   lane with FEC `off` / stock (`default`) / prompt parity
//!   (`instream_flush=true, small_group_parity_count=1`) at 2% and 6% loss,
//!   plus `bulk_and_loss_fec_prompt`, with the RTP FEC counters (parity sent,
//!   loss-gate skips) captured so the 5% gate's effect is visible directly.
//!
//! The remaining single-scenario tests keep the original arms for continuity.
//!
//! * [`jitter_duallane_arms`] — the deployment topology at MATCHED bulk load:
//!   the interactive lane (frame mode + FEC, prompt tuning) and the bulk lane
//!   (a second, independent, strict byte-stream, FEC-free RTP connection) run
//!   on separate `NetemPair` links. The `both` arm prints the interactive
//!   decomposition plus the bulk lane's wire goodput, so receiver-side
//!   fast-forward (`bulk_and_loss_duallane_reorder_fec`) can be compared with
//!   strict frame delivery (`bulk_and_loss_duallane_strict_fec`) at the SAME
//!   bulk load instead of the confounded single-connection arms.
//!
//! These tests are `#[ignore]`-d by default so they do not slow normal builds.
//! Run them with (release is expected; multi-threaded scenarios):
//!
//! ```sh
//! cargo test --release -p tests --test rtp_mux_jitter -- \
//!     --ignored --nocapture --test-threads=1
//! ```

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use netem_test::{Counters, NetemConfig, NetemPair};
use support::dual::{
    LaneRtpConfig, dual_mux_client_connect_lane_rtp_via,
    spawn_dual_mux_latency_bulk_server_two_listeners_lane_rtp_via,
};
use support::frame::{
    rtp_frame_delivery_connect_reorder_with_fec_tuning_via,
    rtp_frame_delivery_connect_with_fec_tuning_via,
};
use support::mux::{
    mux_client_connect_frame_delivery_via, mux_client_connect_via, send_timestamped_messages,
    spawn_mux_frame_delivery_latency_bulk_server_reorder_with_fec_tuning_via,
    spawn_mux_frame_delivery_latency_bulk_server_with_fec_tuning_via,
    spawn_mux_latency_bulk_server_with_fec_tuning_via,
};
use support::payload::{cyclic_payload, with_timeout};
use support::rtp::rtp_connect_with_mss_fec_tuning_and_observer_via;
use support::stats::{HolSummary, combined_stats, summarize};
use support::{TestScope, submit_test_task};
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::time::MissedTickBehavior;

mod support;

/// One-way delay applied to every packet in both directions.
const OWD: Duration = Duration::from_millis(25);
/// Uniform jitter around [`OWD`].
const JITTER: Duration = Duration::from_millis(5);
/// `u32` loss threshold equal to `pct` percent per packet.
const fn loss_pct(pct: u32) -> u32 {
    (u32::MAX / 100) * pct
}
/// The original interactive-loss probe's 2% independent per-packet loss.
const LOSS_2: u32 = loss_pct(2);
/// A loss level above FEC's 5% enable threshold (see
/// `rtp/src/traffic_shaping/redundancy/fec_gate.rs`).
const LOSS_6: u32 = loss_pct(6);
/// Bottleneck rate for the bulk cases (1 MiB/s): chosen so a 2 MiB burst takes
/// ~2 s to drain, producing several hundred ms of queueing without the queue
/// growing without bound across periods.
const BULK_RATE_BPS: u64 = 1024 * 1024 * 8;
/// Bytes offered per bulk burst.
const BULK_BURST_BYTES: usize = 2 * 1024 * 1024;
/// Interval between bulk bursts.
const BULK_PERIOD: Duration = Duration::from_secs(3);
/// Let the ping stream establish a solo floor before the first burst.
const BULK_RAMP: Duration = Duration::from_millis(1500);
/// Interactive message size and cadence (a typical game ping).
const MSG_BYTES: usize = 256;
const CADENCE: Duration = Duration::from_millis(25);
/// Interactive run time — long enough to observe several bulk bursts.
const RUN_FOR: Duration = Duration::from_secs(30);
/// Drain stragglers before reading the latency channel.
const GRACE: Duration = Duration::from_secs(3);
/// Bounded queue for test-owned tasks (mirrors the sibling scenarios).
const TASK_QUEUE_BOUND: usize = support::TEST_TASK_QUEUE_BOUND;

/// Build one impairment direction: fixed delay + jitter, an independent-loss
/// threshold (`0` = none), and an optional rate cap.
fn link(seed: u64, loss: u32, rate_bps: u64) -> NetemConfig {
    NetemConfig {
        latency: OWD,
        jitter: JITTER,
        rate: rate_bps,
        loss,
        seed,
        ..NetemConfig::default()
    }
}

/// A periodic bulk burst: `burst_bytes` offered every `period`, as fast as the
/// transport accepts, for the duration of the run.
#[derive(Clone, Copy, Debug)]
struct BulkSpec {
    burst_bytes: usize,
    period: Duration,
}

/// The periodic 2 MiB / 3 s bulk burst used by every bulk arm.
const BULK: BulkSpec = BulkSpec {
    burst_bytes: BULK_BURST_BYTES,
    period: BULK_PERIOD,
};

/// Everything one jitter scenario needs.
#[derive(Clone, Debug)]
struct JitterScenario {
    label: String,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
    /// FEC enabled on both ends of the RTP connection.
    fec: bool,
    /// Per-connection FEC tuning; both ends must set the same value.
    fec_tuning: rtp::FecTuning,
}

/// Build a scenario with both FEC settings off (the original measurement arms).
fn scen(label: &str, c2s: NetemConfig, s2c: NetemConfig, bulk: Option<BulkSpec>) -> JitterScenario {
    JitterScenario {
        label: label.to_owned(),
        c2s,
        s2c,
        bulk,
        fec: false,
        fec_tuning: rtp::FecTuning::default(),
    }
}

/// The prompt-parity preset: force-flush each interactive data burst's open
/// FEC group at the burst tail, with a single parity symbol.
fn prompt_tuning() -> rtp::FecTuning {
    rtp::FecTuning {
        instream_flush: true,
        small_group_parity_count: 1,
    }
}

/// Build a scenario with FEC enabled on both ends and the given per-connection
/// tuning (the deployment's interactive-lane configuration).
fn scen_fec(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
    fec_tuning: rtp::FecTuning,
) -> JitterScenario {
    JitterScenario {
        label: label.to_owned(),
        c2s,
        s2c,
        bulk,
        fec: true,
        fec_tuning,
    }
}

/// One measured arm: latency summary, wire counters, and the FEC counters the
/// observer last captured (`None` means FEC is disabled on the connection).
struct JitterRun {
    summary: HolSummary,
    counters: Counters,
    fec: Option<rtp::metrics::MetricsFecCounters>,
}

/// A lightweight metrics observer that retains the latest FEC counter snapshot
/// at a coarse 250 ms cadence, so the observer never perturbs the measured
/// traffic while still letting the arm report whether parity was emitted and
/// whether the loss gate skipped it.
fn fec_observer() -> (
    rtp::metrics::MetricsObserver,
    Arc<Mutex<Option<rtp::metrics::MetricsFecCounters>>>,
) {
    let cell = Arc::new(Mutex::new(None));
    let sink = Arc::clone(&cell);
    let last_ms = Arc::new(AtomicU64::new(0));
    let observer = rtp::metrics::MetricsObserver::filtered(
        move |_event, elapsed| {
            let now = elapsed.as_millis() as u64;
            let previous = last_ms.load(Ordering::Relaxed);
            if now >= previous.saturating_add(250) {
                last_ms.store(now, Ordering::Relaxed);
                true
            } else {
                false
            }
        },
        move |observation| {
            if let Some(fec) = observation.snapshot.and_then(|s| s.fec_counters) {
                *sink.lock().unwrap() = Some(fec);
            }
        },
    );
    (observer, cell)
}

/// Run one interactive-vs-bulk/loss jitter scenario and return its measurements.
///
/// Spawns the combined mux-over-RTP server (it classifies each mux stream by
/// its first byte: `b'L'` = timestamped latency frames, any other byte = bulk
/// sink), one [`NetemPair`], and one mux connection carrying both streams. The
/// interactive `b'L'` stream sends [`MSG_BYTES`] messages every [`CADENCE`] for
/// [`RUN_FOR`], optionally contested by a periodic `b'B'` bulk burst. Both the
/// client and server RTP connections use the scenario's `fec`/`fec_tuning`.
async fn run_jitter(scenario: JitterScenario) -> JitterRun {
    let JitterScenario {
        label,
        c2s,
        s2c,
        bulk,
        fec,
        fec_tuning,
    } = scenario;
    let base = Instant::now();
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, mut latencies, bulk_counter) =
                spawn_mux_latency_bulk_server_with_fec_tuning_via(&task_tx, fec, base, fec_tuning)
                    .await
                    .unwrap();
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

            let (observer, fec_cell) = fec_observer();
            let (connected_read, connected_write) =
                rtp_connect_with_mss_fec_tuning_and_observer_via(
                    &task_tx,
                    pair.client_addr(),
                    fec,
                    rtp::udp::NO_FEC_MSS,
                    fec_tuning,
                    observer,
                )
                .await;
            let opener = mux_client_connect_via(&task_tx, connected_read, connected_write);

            // Interactive latency stream (`b'L'`). Its read half stays parked
            // until the stream closes; the owning scope aborts it at teardown.
            let (mut lat_read, mut lat_write) = opener.open().await.unwrap();
            submit_test_task(
                &task_tx,
                Box::pin(async move {
                    let mut buf = vec![0u8; 8 * 1024];
                    while let Ok(n) = lat_read.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                    }
                }),
            );

            // Optional bulk stream (`b'B'`) on the same mux connection.
            let bulk_write = if bulk.is_some() {
                let (mut bulk_read, bulk_write) = opener.open().await.unwrap();
                submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        let mut buf = vec![0u8; 64 * 1024];
                        while let Ok(n) = bulk_read.read(&mut buf).await {
                            if n == 0 {
                                break;
                            }
                        }
                    }),
                );
                Some(bulk_write)
            } else {
                None
            };

            let interactive = async {
                if lat_write.write_all(b"L").await.is_err() {
                    return 0;
                }
                send_timestamped_messages(&mut lat_write, base, MSG_BYTES, CADENCE, RUN_FOR).await
            };
            let bulk_fut = async {
                let Some(mut write) = bulk_write else {
                    return 0;
                };
                if write.write_all(b"B").await.is_err() {
                    return 0;
                }
                let spec = bulk.expect("bulk_write is Some iff bulk is Some");
                let payload = cyclic_payload(spec.burst_bytes);
                periodic_burst(
                    &mut write,
                    &payload,
                    spec.burst_bytes,
                    spec.period,
                    BULK_RAMP,
                    RUN_FOR,
                )
                .await
            };
            let (sent, _bulk_written) = tokio::join!(interactive, bulk_fut);

            // Let stragglers arrive before draining the latency channel.
            tokio::time::sleep(GRACE).await;
            let counters = combined_stats(&pair);
            let mut samples = Vec::new();
            while let Ok(lat) = latencies.try_recv() {
                samples.push(lat);
            }
            let received = samples.len() as u64;
            let summary = summarize(
                samples,
                sent,
                received,
                bulk_counter.load(Ordering::Relaxed),
                RUN_FOR.as_secs_f64(),
            );
            let fec = *fec_cell.lock().unwrap();

            print_summary(&label, &summary);
            eprintln!("[jitter {label}] pair stats = {counters:?}");
            if let Some(fec) = fec {
                eprintln!(
                    "[jitter {label}] fec parity_sent={} groups_flushed={} \
                     loss_gate_skips={} no_spare_capacity_skips={} burst_end_skips={} \
                     recovered={}",
                    fec.parity_sent,
                    fec.groups_flushed,
                    fec.groups_skipped_loss_gate,
                    fec.groups_skipped_no_spare_capacity,
                    fec.groups_skipped_burst_end,
                    fec.recovered_symbols,
                );
            }

            pair.stop();
            JitterRun {
                summary,
                counters,
                fec,
            }
        })
        .await
}

/// Wrap a single-decision [`JitterScenario`] in the standard timeout and run it.
async fn run_one(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
) -> JitterRun {
    with_timeout(
        Duration::from_secs(120),
        label,
        run_jitter(scen(label, c2s, s2c, bulk)),
    )
    .await
}

/// Run one interactive-vs-bulk/loss jitter scenario in RTP frame-delivery mode.
/// Mirrors [`run_jitter`] but connects through the frame-delivery accept/connect
/// helpers and a `frame_reassembly` mux client, matching the deployment path
/// (`rtp_mux` sets `frame_reassembly: true`). The scenario's `fec`/`fec_tuning`
/// are threaded to both peers (the FEC-off [`scen`] defaults leave them off), so
/// the deployment's frame-mode-plus-FEC path is measurable.
async fn run_jitter_frame(scenario: JitterScenario) -> JitterRun {
    run_jitter_frame_mode(scenario, false).await
}

/// [`run_jitter_frame`] with receiver-side fast-forward: both the frame-delivery
/// server and the frame-delivery client use
/// [`rtp::FrameMode::enabled_reordering`](rtp::FrameMode::enabled_reordering),
/// so a complete frame starting past an unrepaired in-order hole is delivered
/// immediately. Mirrors [`run_jitter_frame`] in every other respect.
async fn run_jitter_frame_reorder(scenario: JitterScenario) -> JitterRun {
    run_jitter_frame_mode(scenario, true).await
}

/// Shared body for [`run_jitter_frame`] (strict) and [`run_jitter_frame_reorder`]
/// (fast-forward): `reorder` selects the matching server/client frame-mode
/// helpers; both peers flip together because the mode is not negotiated.
async fn run_jitter_frame_mode(scenario: JitterScenario, reorder: bool) -> JitterRun {
    let JitterScenario {
        label,
        c2s,
        s2c,
        bulk,
        fec,
        fec_tuning,
    } = scenario;
    let base = Instant::now();
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, mut latencies, bulk_counter) = if reorder {
                spawn_mux_frame_delivery_latency_bulk_server_reorder_with_fec_tuning_via(
                    &task_tx, fec, base, fec_tuning,
                )
                .await
                .unwrap()
            } else {
                spawn_mux_frame_delivery_latency_bulk_server_with_fec_tuning_via(
                    &task_tx, fec, base, fec_tuning,
                )
                .await
                .unwrap()
            };
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

            let (observer, fec_cell) = fec_observer();
            let (reader, writer) = if reorder {
                rtp_frame_delivery_connect_reorder_with_fec_tuning_via(
                    &task_tx,
                    pair.client_addr(),
                    fec,
                    fec_tuning,
                    Some(observer),
                )
                .await
            } else {
                rtp_frame_delivery_connect_with_fec_tuning_via(
                    &task_tx,
                    pair.client_addr(),
                    fec,
                    fec_tuning,
                    Some(observer),
                )
                .await
            };
            let opener = mux_client_connect_frame_delivery_via(&task_tx, reader, writer);

            // Interactive latency stream (`b'L'`). Its read half stays parked
            // until the stream closes; the owning scope aborts it at teardown.
            let (mut lat_read, mut lat_write) = opener.open().await.unwrap();
            submit_test_task(
                &task_tx,
                Box::pin(async move {
                    let mut buf = vec![0u8; 8 * 1024];
                    while let Ok(n) = lat_read.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                    }
                }),
            );

            // Optional bulk stream (`b'B'`) on the same mux connection.
            let bulk_write = if bulk.is_some() {
                let (mut bulk_read, bulk_write) = opener.open().await.unwrap();
                submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        let mut buf = vec![0u8; 64 * 1024];
                        while let Ok(n) = bulk_read.read(&mut buf).await {
                            if n == 0 {
                                break;
                            }
                        }
                    }),
                );
                Some(bulk_write)
            } else {
                None
            };

            let interactive = async {
                if lat_write.write_all(b"L").await.is_err() {
                    return 0;
                }
                send_timestamped_messages(&mut lat_write, base, MSG_BYTES, CADENCE, RUN_FOR).await
            };
            let bulk_fut = async {
                let Some(mut write) = bulk_write else {
                    return 0;
                };
                if write.write_all(b"B").await.is_err() {
                    return 0;
                }
                let spec = bulk.expect("bulk_write is Some iff bulk is Some");
                let payload = cyclic_payload(spec.burst_bytes);
                periodic_burst(
                    &mut write,
                    &payload,
                    spec.burst_bytes,
                    spec.period,
                    BULK_RAMP,
                    RUN_FOR,
                )
                .await
            };
            let (sent, _bulk_written) = tokio::join!(interactive, bulk_fut);

            // Let stragglers arrive before draining the latency channel.
            tokio::time::sleep(GRACE).await;
            let counters = combined_stats(&pair);
            let c2s = pair.stats_c2s();
            let mut samples = Vec::new();
            while let Ok((tag, lat)) = latencies.try_recv() {
                if tag == b'L' {
                    samples.push(lat);
                }
            }
            let received = samples.len() as u64;
            // The bulk sink's byte stream stalls at its first unrepaired hole
            // under frame fast-forward (the mux per-stream reader is in-order),
            // so the sink count collapses in the reorder arms even while the
            // wire carries the full offered load. Report the client->server
            // wire bytes as the load measure so strict and reorder arms are
            // compared at matched offered load; keep the sink count (now an
            // order-independent raw read total) as a goodput diagnostic.
            let sink_delivered = bulk_counter.load(Ordering::Relaxed);
            let summary = summarize(
                samples,
                sent,
                received,
                c2s.forwarded_bytes,
                RUN_FOR.as_secs_f64(),
            );

            print_summary(&label, &summary);
            eprintln!(
                "[jitter {label}] bulk sink delivered = {sink_delivered} bytes; \
                 wire c2s forwarded = {} bytes / {} pkts",
                c2s.forwarded_bytes, c2s.forwarded
            );
            eprintln!("[jitter {label}] pair stats = {counters:?}");
            let fec = *fec_cell.lock().unwrap();
            if let Some(fec) = fec {
                eprintln!(
                    "[jitter {label}] fec parity_sent={} groups_flushed={} \
                     loss_gate_skips={} no_spare_capacity_skips={} burst_end_skips={} \
                     recovered={}",
                    fec.parity_sent,
                    fec.groups_flushed,
                    fec.groups_skipped_loss_gate,
                    fec.groups_skipped_no_spare_capacity,
                    fec.groups_skipped_burst_end,
                    fec.recovered_symbols,
                );
            }

            pair.stop();
            JitterRun {
                summary,
                counters,
                fec,
            }
        })
        .await
}

/// Wrap a single-decision frame-delivery [`JitterScenario`] in the standard
/// timeout and run it through [`run_jitter_frame`].
async fn run_one_frame(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
) -> JitterRun {
    with_timeout(
        Duration::from_secs(120),
        label,
        run_jitter_frame(scen(label, c2s, s2c, bulk)),
    )
    .await
}

/// Wrap a single-decision fast-forward frame-delivery [`JitterScenario`] in the
/// standard timeout and run it through [`run_jitter_frame_reorder`].
async fn run_one_frame_reorder(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
) -> JitterRun {
    with_timeout(
        Duration::from_secs(120),
        label,
        run_jitter_frame_reorder(scen(label, c2s, s2c, bulk)),
    )
    .await
}

/// Wrap a single-decision strict frame-delivery [`JitterScenario`] with FEC on
/// and the given tuning in the standard timeout, running it through
/// [`run_jitter_frame`].
async fn run_one_frame_fec(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
    fec_tuning: rtp::FecTuning,
) -> JitterRun {
    with_timeout(
        Duration::from_secs(120),
        label,
        run_jitter_frame(scen_fec(label, c2s, s2c, bulk, fec_tuning)),
    )
    .await
}

/// Wrap a single-decision fast-forward frame-delivery [`JitterScenario`] with
/// FEC on and the given tuning in the standard timeout, running it through
/// [`run_jitter_frame_reorder`].
async fn run_one_frame_reorder_fec(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
    fec_tuning: rtp::FecTuning,
) -> JitterRun {
    with_timeout(
        Duration::from_secs(120),
        label,
        run_jitter_frame_reorder(scen_fec(label, c2s, s2c, bulk, fec_tuning)),
    )
    .await
}

/// Offer `burst_bytes` every `period` through `write`, as fast as the transport
/// accepts, starting after `ramp` and stopping at `run_for`. Returns the number
/// of payload bytes written.
///
/// The burst is offered in back-to-back `write_all` chunks with no pacing: the
/// transport's flow control and the link's rate cap are the only throttle, so
/// the ping stream observes the real queue the burst creates. When a burst
/// takes longer than `period`, the interval's [`MissedTickBehavior::Delay`]
/// coalesces missed ticks instead of firing a catch-up storm.
async fn periodic_burst(
    write: &mut (impl AsyncWrite + Unpin),
    payload: &[u8],
    burst_bytes: usize,
    period: Duration,
    ramp: Duration,
    run_for: Duration,
) -> u64 {
    let start = Instant::now();
    tokio::time::sleep(ramp).await;
    let mut interval = tokio::time::interval(period);
    interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
    // The first tick is immediate; consume it so bursts start at `ramp`.
    interval.tick().await;

    let mut cursor = 0usize;
    let mut written = 0u64;
    loop {
        if start.elapsed() >= run_for {
            break;
        }
        let mut remaining = burst_bytes;
        while remaining > 0 {
            if start.elapsed() >= run_for {
                return written;
            }
            let avail = payload.len() - cursor;
            let take = remaining.min(avail);
            if write
                .write_all(&payload[cursor..cursor + take])
                .await
                .is_err()
            {
                return written;
            }
            cursor = (cursor + take) % payload.len();
            remaining -= take;
            written += take as u64;
        }
        interval.tick().await;
    }
    written
}

/// Print the measurement summary so the numbers are visible with
/// `--nocapture`.
fn print_summary(label: &str, s: &HolSummary) {
    eprintln!(
        "[jitter {label}] sent={sent} recv={recv} delivery={del:.3} \
         p50={p50:.1} p90={p90:.1} p99={p99:.1} max={max:.1} \
         over250={o25:.3} over1000={o1k:.3} episodes={ep} max_run={mr} \
         bulk={bulk:.3} MiB/s",
        sent = s.sent,
        recv = s.received,
        del = s.delivery_pct,
        p50 = s.p50,
        p90 = s.p90,
        p99 = s.p99,
        max = s.max,
        o25 = s.over250_pct,
        o1k = s.over1000_pct,
        ep = s.episodes,
        mr = s.max_run,
        bulk = s.bulk_mibps,
    );
}

/// Loose sanity guard: the scenario must have sent and received a sane number
/// of messages. This is documentation/regression insurance, not a latency
/// bound — the numbers above are the deliverable.
fn assert_sane(label: &str, s: &HolSummary) {
    assert!(s.sent >= 100, "[{label}] only {} sent", s.sent);
    assert!(s.received >= 50, "[{label}] only {} received", s.received);
}

/// Classify the p99 combined/delta-sum ratio as sub-additive, additive, or
/// super-additive.
fn classify_ratio(ratio: f64) -> &'static str {
    if !ratio.is_finite() {
        "n/a"
    } else if ratio > 1.15 {
        "super-additive"
    } else if ratio < 0.85 {
        "sub-additive"
    } else {
        "additive"
    }
}

/// A `HolSummary` latency extractor used by the decomposition table rows.
type SummaryMetric = fn(&HolSummary) -> f64;

/// Print the loss-vs-queueing decomposition table (the deliverable for
/// [`jitter_decomposition`]): the four absolute arms and the derived deltas,
/// for both the latency percentiles and the spike/episode counts. `mode`
/// labels the RTP delivery path (`byte-stream` or `frame-delivery`) so the two
/// tables are directly comparable.
fn print_decomposition(
    mode: &str,
    solo: &JitterRun,
    loss: &JitterRun,
    bulk: &JitterRun,
    both: &JitterRun,
) {
    eprintln!(
        "[decomp {mode}] interactive latency decomposition (2% loss, 1 MiB/s rate, 2 MiB/3 s bulk)"
    );
    eprintln!(
        "[decomp {mode}] metric       solo     loss     bulk     both   loss_d  queue_d   comb_d  comb/(loss+q)"
    );
    let mut p99_ratio = f64::NAN;
    let percentile_rows: [(&str, SummaryMetric); 4] = [
        ("p50", |s| s.p50),
        ("p90", |s| s.p90),
        ("p99", |s| s.p99),
        ("max", |s| s.max),
    ];
    for (name, get) in percentile_rows {
        let s = get(&solo.summary);
        let l = get(&loss.summary);
        let b = get(&bulk.summary);
        let c = get(&both.summary);
        let loss_delta = l - s;
        let queue_delta = b - s;
        let combined_delta = c - s;
        let denominator = loss_delta + queue_delta;
        let ratio = if denominator.abs() > f64::EPSILON {
            combined_delta / denominator
        } else {
            f64::NAN
        };
        if name == "p99" {
            p99_ratio = ratio;
        }
        eprintln!(
            "[decomp {mode}] {name:<8} {s:8.1} {l:8.1} {b:8.1} {c:8.1} \
             {loss_delta:8.1} {queue_delta:8.1} {combined_delta:8.1}     {ratio:6.2}"
        );
    }
    #[derive(Clone, Copy)]
    enum Spike {
        Pct(fn(&HolSummary) -> f64),
        Count(fn(&HolSummary) -> u64),
    }
    let spike_rows: [(&str, Spike); 3] = [
        ("over250", Spike::Pct(|s| s.over250_pct)),
        ("episodes", Spike::Count(|s| s.episodes)),
        ("max_run", Spike::Count(|s| s.max_run)),
    ];
    eprintln!(
        "[decomp {mode}] spikes       solo     loss     bulk     both   loss_d  queue_d   comb_d"
    );
    for (name, kind) in spike_rows {
        let (s, l, b, c) = match kind {
            Spike::Pct(get) => (
                get(&solo.summary),
                get(&loss.summary),
                get(&bulk.summary),
                get(&both.summary),
            ),
            Spike::Count(get) => (
                get(&solo.summary) as f64,
                get(&loss.summary) as f64,
                get(&bulk.summary) as f64,
                get(&both.summary) as f64,
            ),
        };
        eprintln!(
            "[decomp {mode}] {name:<8} {s:8.3} {l:8.3} {b:8.3} {c:8.3} \
             {:8.3} {:8.3} {:8.3}",
            l - s,
            b - s,
            c - s
        );
    }
    eprintln!(
        "[decomp {mode}] p99 combined/(loss_delta+queue_delta) = {p99_ratio:.2} => {}",
        classify_ratio(p99_ratio)
    );
    eprintln!(
        "[decomp {mode}] p99 loss_only_delta={:.1} bqueue_only_delta={:.1} sum={:.1} combined={:.1}",
        loss.summary.p99 - solo.summary.p99,
        bulk.summary.p99 - solo.summary.p99,
        (loss.summary.p99 - solo.summary.p99) + (bulk.summary.p99 - solo.summary.p99),
        both.summary.p99 - solo.summary.p99,
    );
}

/// Print one FEC-arm table: latency percentiles plus the RTP FEC counters that
/// say whether parity was actually emitted or the loss gate skipped it.
fn print_fec_table(level: &str, loss: u32, runs: &[(&str, JitterRun)]) {
    let pct = loss as f64 / u32::MAX as f64 * 100.0;
    eprintln!("[fec] --- interactive lane at {pct:.0}% per-packet loss (seeded link) ---");
    eprintln!(
        "[fec] arm                        p50     p90     p99     max  over250  ep  run  parity  flushed  gate_skip  spare_skip  burst_skip  recovered"
    );
    for (name, run) in runs {
        let s = &run.summary;
        let f = run.fec.unwrap_or_default();
        eprintln!(
            "[fec] {name:<27} {p50:7.1} {p90:7.1} {p99:7.1} {max:7.1} {o25:7.3} {ep:3} {mr:3} \
             {parity:6} {flushed:8} {gate:9} {spare:11} {burst:11} {recovered:9}",
            p50 = s.p50,
            p90 = s.p90,
            p99 = s.p99,
            max = s.max,
            o25 = s.over250_pct,
            ep = s.episodes,
            mr = s.max_run,
            parity = f.parity_sent,
            flushed = f.groups_flushed,
            gate = f.groups_skipped_loss_gate,
            spare = f.groups_skipped_no_spare_capacity,
            burst = f.groups_skipped_burst_end,
            recovered = f.recovered_symbols,
        );
    }
    let baseline = runs
        .iter()
        .find(|(name, _)| *name == "loss_fec_off")
        .map(|(_, run)| run)
        .expect("the FEC table always includes the loss_fec_off baseline arm");
    for (name, run) in runs {
        if *name == "solo" || *name == "loss_fec_off" {
            continue;
        }
        let s = &run.summary;
        let off = &baseline.summary;
        eprintln!(
            "[fec] {level} {name} - loss_fec_off: p50={:+.1} p90={:+.1} p99={:+.1} max={:+.1} \
             over250={:+.3} episodes={:+}",
            s.p50 - off.p50,
            s.p90 - off.p90,
            s.p99 - off.p99,
            s.max - off.max,
            s.over250_pct - off.over250_pct,
            s.episodes as i64 - off.episodes as i64,
        );
    }
    eprintln!("[fec] {level} wire forwarded (both directions; FEC parity inflates this):");
    for (name, run) in runs {
        eprintln!(
            "[fec] {level} {name:<27} forwarded_pkts={:>8} forwarded_bytes={:>10}",
            run.counters.forwarded, run.counters.forwarded_bytes,
        );
    }
}

/// Print the frame-delivery + FEC arm table: latency percentiles, the RTP FEC
/// counters (so it is visible whether parity was actually emitted), and the
/// client->server wire bulk load. The `_fec_off` reorder rows are the FEC-off
/// baselines and the `_fec` rows are the deployment's frame-mode-plus-FEC
/// path; the bulk column is the load-match check. It is the c2s wire bytes
/// (not the sink's delivered bytes): the frame fast-forward leaves the bulk
/// sink's byte stream stalled at its first hole, so the sink count collapses
/// in the reorder arms even though the wire carries the full offered load.
fn print_frame_fec_table(runs: &[(&str, JitterRun)]) {
    eprintln!(
        "[framefec] frame-delivery interactive lane, 2% per-packet loss; *_fec rows use prompt \
         tuning (instream_flush, small_group_parity_count=1)"
    );
    eprintln!(
        "[framefec] arm                              p50     p90     p99     max  over250  ep  run  \
         parity  flushed   gate  recovered     bulk"
    );
    for (name, run) in runs {
        let s = &run.summary;
        let f = run.fec.unwrap_or_default();
        eprintln!(
            "[framefec] {name:<32} {p50:7.1} {p90:7.1} {p99:7.1} {max:7.1} {o25:7.3} {ep:3} {mr:3} \
             {parity:7} {flushed:8} {gate:6} {recovered:9} {bulk:8.3}",
            p50 = s.p50,
            p90 = s.p90,
            p99 = s.p99,
            max = s.max,
            o25 = s.over250_pct,
            ep = s.episodes,
            mr = s.max_run,
            parity = f.parity_sent,
            flushed = f.groups_flushed,
            gate = f.groups_skipped_loss_gate,
            recovered = f.recovered_symbols,
            bulk = s.bulk_mibps,
        );
    }
    let find = |name: &str| -> Option<&JitterRun> {
        runs.iter()
            .find(|(candidate, _)| *candidate == name)
            .map(|(_, run)| run)
    };
    for (fec_arm, off_arm) in [
        ("loss_frame_reorder_fec", "loss_frame_reorder_fec_off"),
        (
            "bulk_and_loss_frame_reorder_fec",
            "bulk_and_loss_frame_reorder_fec_off",
        ),
    ] {
        let (Some(on), Some(off)) = (find(fec_arm), find(off_arm)) else {
            continue;
        };
        let s = &on.summary;
        let o = &off.summary;
        eprintln!(
            "[framefec] {fec_arm} - {off_arm}: p50={:+.1} p90={:+.1} p99={:+.1} max={:+.1} \
             over250={:+.3} episodes={:+} bulk={:+.3}",
            s.p50 - o.p50,
            s.p90 - o.p90,
            s.p99 - o.p99,
            s.max - o.max,
            s.over250_pct - o.over250_pct,
            s.episodes as i64 - o.episodes as i64,
            s.bulk_mibps - o.bulk_mibps,
        );
    }
}

/// Run the four FEC treatments (plus a loss-free `solo` reference on the same
/// seeds) at one loss level and print the table.
async fn run_fec_level(level: &str, loss: u32, c2s_seed: u64, s2c_seed: u64) {
    let stock = rtp::FecTuning::default();
    let prompt = prompt_tuning();
    let arms: [(&str, bool, rtp::FecTuning, bool, bool); 5] = [
        // name, fec, tuning, bulk, loss-applied
        ("solo", false, stock, false, false),
        ("loss_fec_off", false, stock, false, true),
        ("loss_fec_default", true, stock, false, true),
        ("loss_fec_prompt", true, prompt, false, true),
        ("bulk_and_loss_fec_prompt", true, prompt, true, true),
    ];
    let mut runs: Vec<(&str, JitterRun)> = Vec::new();
    for (name, fec, tuning, with_bulk, with_loss) in arms {
        let label = format!("{level}/{name}");
        let applied = if with_loss { loss } else { 0 };
        let rate = if with_bulk { BULK_RATE_BPS } else { 0 };
        let run = with_timeout(
            Duration::from_secs(120),
            &label,
            run_jitter(JitterScenario {
                label: label.clone(),
                c2s: link(c2s_seed, applied, rate),
                s2c: link(s2c_seed, applied, rate),
                bulk: with_bulk.then_some(BULK),
                fec,
                fec_tuning: tuning,
            }),
        )
        .await;
        assert_sane(&label, &run.summary);
        runs.push((name, run));
    }
    print_fec_table(level, loss, &runs);
}

/// Deliverable 1: the loss-vs-queueing decomposition on the same seeded link,
/// printed as the table that separates the loss tail from the queue tail.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; eight ~35 s arms (byte-stream + frame-delivery); run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_decomposition() {
    let solo = run_one("solo", link(11, 0, 0), link(12, 0, 0), None).await;
    let loss_only = run_one("loss-only", link(21, LOSS_2, 0), link(22, LOSS_2, 0), None).await;
    let bulk_only = run_one(
        "bulk-only",
        link(31, 0, BULK_RATE_BPS),
        link(32, 0, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;
    let bulk_and_loss = run_one(
        "bulk+loss",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;

    for (label, run) in [
        ("solo", &solo),
        ("loss-only", &loss_only),
        ("bulk-only", &bulk_only),
        ("bulk+loss", &bulk_and_loss),
    ] {
        assert_sane(label, &run.summary);
    }
    print_decomposition("byte-stream", &solo, &loss_only, &bulk_only, &bulk_and_loss);

    // The same four arms in the deployment's frame-delivery path.
    let solo_frame = run_one_frame("solo_frame", link(11, 0, 0), link(12, 0, 0), None).await;
    let loss_frame =
        run_one_frame("loss_frame", link(21, LOSS_2, 0), link(22, LOSS_2, 0), None).await;
    let bulk_frame = run_one_frame(
        "bulk_frame",
        link(31, 0, BULK_RATE_BPS),
        link(32, 0, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;
    let bulk_and_loss_frame = run_one_frame(
        "bulk_and_loss_frame",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;

    for (label, run) in [
        ("solo_frame", &solo_frame),
        ("loss_frame", &loss_frame),
        ("bulk_frame", &bulk_frame),
        ("bulk_and_loss_frame", &bulk_and_loss_frame),
    ] {
        assert_sane(label, &run.summary);
    }
    print_decomposition(
        "frame-delivery",
        &solo_frame,
        &loss_frame,
        &bulk_frame,
        &bulk_and_loss_frame,
    );
}

/// Deliverable 1b: the same four frame-delivery arms with receiver-side
/// fast-forward (`FrameMode::enabled_reordering`), the deployment's
/// interactive-lane mode. Both the accept side (`*_reorder_via`) and the
/// connect side (`rtp_frame_delivery_connect_reorder_via`) select
/// `allow_reorder`, so a complete interactive frame is delivered as soon as
/// it arrives instead of waiting behind a bulk hole. The table is printed with
/// the `frame-reorder` label and is directly comparable to the strict
/// `frame-delivery` table from [`jitter_decomposition`].
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; four ~35 s fast-forward frame arms; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_frame_reorder_decomposition() {
    let solo_frame_reorder =
        run_one_frame_reorder("solo_frame_reorder", link(11, 0, 0), link(12, 0, 0), None).await;
    let loss_frame_reorder = run_one_frame_reorder(
        "loss_frame_reorder",
        link(21, LOSS_2, 0),
        link(22, LOSS_2, 0),
        None,
    )
    .await;
    let bulk_frame_reorder = run_one_frame_reorder(
        "bulk_frame_reorder",
        link(31, 0, BULK_RATE_BPS),
        link(32, 0, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;
    let bulk_and_loss_frame_reorder = run_one_frame_reorder(
        "bulk_and_loss_frame_reorder",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;

    for (label, run) in [
        ("solo_frame_reorder", &solo_frame_reorder),
        ("loss_frame_reorder", &loss_frame_reorder),
        ("bulk_frame_reorder", &bulk_frame_reorder),
        ("bulk_and_loss_frame_reorder", &bulk_and_loss_frame_reorder),
    ] {
        assert_sane(label, &run.summary);
    }
    print_decomposition(
        "frame-reorder",
        &solo_frame_reorder,
        &loss_frame_reorder,
        &bulk_frame_reorder,
        &bulk_and_loss_frame_reorder,
    );
}

/// Deliverable 1c: the deployment's real interactive-lane configuration — RTP
/// frame-delivery with receiver-side fast-forward **and** FEC. Runs the strict
/// frame + FEC and reorder frame + FEC arms beside the reorder FEC-off
/// baselines on the same seeded 2% link, so the three configurations can be
/// read off one table. The `_fec` arms use the interactive prompt tuning
/// (`instream_flush`, `small_group_parity_count = 1`), which selects FEC's
/// permissive loss gate so parity actually opens at 2%.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; six ~35 s frame+FEC arms; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_frame_reorder_fec_arms() {
    let prompt = prompt_tuning();

    // Strict frame delivery + FEC.
    let loss_frame_fec = run_one_frame_fec(
        "loss_frame_fec",
        link(21, LOSS_2, 0),
        link(22, LOSS_2, 0),
        None,
        prompt,
    )
    .await;
    let bulk_and_loss_frame_fec = run_one_frame_fec(
        "bulk_and_loss_frame_fec",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
        prompt,
    )
    .await;

    // Frame fast-forward, FEC off (the measured baselines).
    let loss_frame_reorder_fec_off = run_one_frame_reorder(
        "loss_frame_reorder_fec_off",
        link(21, LOSS_2, 0),
        link(22, LOSS_2, 0),
        None,
    )
    .await;
    let bulk_and_loss_frame_reorder_fec_off = run_one_frame_reorder(
        "bulk_and_loss_frame_reorder_fec_off",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;

    // Frame fast-forward + FEC (the deployment's real path).
    let loss_frame_reorder_fec = run_one_frame_reorder_fec(
        "loss_frame_reorder_fec",
        link(21, LOSS_2, 0),
        link(22, LOSS_2, 0),
        None,
        prompt,
    )
    .await;
    let bulk_and_loss_frame_reorder_fec = run_one_frame_reorder_fec(
        "bulk_and_loss_frame_reorder_fec",
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
        prompt,
    )
    .await;

    let runs = [
        ("loss_frame_fec", loss_frame_fec),
        ("bulk_and_loss_frame_fec", bulk_and_loss_frame_fec),
        ("loss_frame_reorder_fec_off", loss_frame_reorder_fec_off),
        ("loss_frame_reorder_fec", loss_frame_reorder_fec),
        (
            "bulk_and_loss_frame_reorder_fec_off",
            bulk_and_loss_frame_reorder_fec_off,
        ),
        (
            "bulk_and_loss_frame_reorder_fec",
            bulk_and_loss_frame_reorder_fec,
        ),
    ];
    for (label, run) in &runs {
        assert_sane(label, &run.summary);
    }
    print_frame_fec_table(&runs);
}

/// Deliverable 2: FEC arms at 2% loss (below FEC's 5% enable gate).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; five ~35 s arms; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_fec_arms_2pct() {
    run_fec_level("2pct", LOSS_2, 51, 52).await;
}

/// Deliverable 2: FEC arms at 6% loss (above FEC's 5% enable gate).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; five ~35 s arms; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_fec_arms_6pct() {
    run_fec_level("6pct", LOSS_6, 61, 62).await;
}

/// The floor: interactive pings on a delay+jitter link, no loss, no bulk.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_solo() {
    let label = "solo";
    let run = run_one(label, link(11, 0, 0), link(12, 0, 0), None).await;
    assert_sane(label, &run.summary);
}

/// Isolates loss/HOL repair latency: interactive pings on a 2% lossy link,
/// no bulk.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_with_loss() {
    let label = "loss";
    let run = run_one(label, link(21, LOSS_2, 0), link(22, LOSS_2, 0), None).await;
    assert_sane(label, &run.summary);
}

/// Isolates bufferbloat/queueing: interactive pings plus a periodic 2 MiB bulk
/// burst on a 1 MiB/s rate-limited link, no loss.
///
/// Runs the same rate-limited link twice — bulk on, then bulk off — so the
/// added latency can be attributed to the burst's queue. The delta is printed
/// explicitly.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~65 s measurement (two arms); run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_with_bulk() {
    let bulk_on = run_one(
        "bulk-on",
        link(31, 0, BULK_RATE_BPS),
        link(32, 0, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;
    let bulk_off = run_one(
        "bulk-off",
        link(31, 0, BULK_RATE_BPS),
        link(32, 0, BULK_RATE_BPS),
        None,
    )
    .await;

    eprintln!(
        "[jitter attribution] bulk-on minus bulk-off: p50={:.1} ms p90={:.1} ms \
         p99={:.1} ms max={:.1} ms over250={:+.3}",
        bulk_on.summary.p50 - bulk_off.summary.p50,
        bulk_on.summary.p90 - bulk_off.summary.p90,
        bulk_on.summary.p99 - bulk_off.summary.p99,
        bulk_on.summary.max - bulk_off.summary.max,
        bulk_on.summary.over250_pct - bulk_off.summary.over250_pct,
    );

    assert_sane("bulk-on", &bulk_on.summary);
    assert_sane("bulk-off", &bulk_off.summary);
}

/// The realistic case: interactive pings plus the periodic bulk burst on a 2%
/// lossy, rate-limited link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_bulk_and_loss() {
    let label = "bulk+loss";
    let run = run_one(
        label,
        link(41, LOSS_2, BULK_RATE_BPS),
        link(42, LOSS_2, BULK_RATE_BPS),
        Some(BULK),
    )
    .await;
    assert_sane(label, &run.summary);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dual-lane interactive latency: the deployment topology at matched bulk load
// ═══════════════════════════════════════════════════════════════════════════════

/// Which impairments a dual-lane arm applies. Each lane rides its own
/// [`NetemPair`], so the interactive and bulk impairments are independent: the
/// bulk lane's rate cap and loss never touch the interactive lane's RTP
/// connection.
#[derive(Clone, Copy, Debug)]
enum DualImpairment {
    /// No loss, no bulk: the interactive-lane floor.
    Solo,
    /// 2% loss on both lanes, no bulk.
    Loss,
    /// No loss, bulk burst contesting only the bulk lane.
    Bulk,
    /// 2% loss plus the bulk burst: the deployment case.
    Both,
}

impl DualImpairment {
    fn has_loss(self) -> bool {
        matches!(self, Self::Loss | Self::Both)
    }

    fn has_bulk(self) -> bool {
        matches!(self, Self::Bulk | Self::Both)
    }
}

/// One measured dual-lane arm: the interactive-lane [`JitterRun`] (latency +
/// FEC evidence) plus the bulk lane's wire load and sink goodput.
struct DualRun {
    run: JitterRun,
    /// Bulk-lane client->server wire bytes forwarded by the impairment proxy:
    /// the offered bulk load, i.e. the matched-load check across the two
    /// interactive frame modes.
    bulk_wire_bytes: u64,
    /// Bulk-lane sink-delivered payload bytes (independent of wire load).
    bulk_sink_bytes: u64,
    /// Combined bulk-pair counters (both directions).
    bulk_counters: Counters,
}

/// Run one dual-lane arm: the interactive lane on its own RTP connection
/// (frame mode + FEC per `interactive_reorder`) and the bulk lane on a second,
/// independent, strict byte-stream FEC-free RTP connection. The two lanes ride
/// two separate [`NetemPair`]s so the bulk burst cannot head-of-line block the
/// interactive RTP connection, and the bulk lane is byte-for-byte identical in
/// the fast-forward and strict arms.
async fn run_duallane(
    label: &str,
    interactive_reorder: bool,
    impairment: DualImpairment,
) -> DualRun {
    let interactive_loss = if impairment.has_loss() { LOSS_2 } else { 0 };
    let bulk_loss = if impairment.has_loss() { LOSS_2 } else { 0 };
    let bulk_rate = if impairment.has_bulk() {
        BULK_RATE_BPS
    } else {
        0
    };
    // The interactive lane never takes the rate cap (the cap is the bulk
    // lane's own connection); its loss is the interactive `2%`.
    let int_c2s = link(41, interactive_loss, 0);
    let int_s2c = link(42, interactive_loss, 0);
    // The bulk lane is byte-for-byte identical in the fast-forward and strict
    // arms (same seeds, same loss, same rate cap), so the wire bulk goodput is
    // the matched-load evidence.
    let bulk_c2s = link(43, bulk_loss, bulk_rate);
    let bulk_s2c = link(44, bulk_loss, bulk_rate);

    let prompt = prompt_tuning();
    let int_rtp = if interactive_reorder {
        LaneRtpConfig::frame_reordering(true, prompt)
    } else {
        LaneRtpConfig::frame_strict_tuned(true, prompt)
    };
    let bulk_rtp = LaneRtpConfig::byte_stream();

    let base = Instant::now();
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (int_addr, bulk_addr, mut latencies, bulk_counter, _sink_streams) =
                spawn_dual_mux_latency_bulk_server_two_listeners_lane_rtp_via(
                    &task_tx, base, int_rtp, bulk_rtp,
                )
                .await
                .unwrap();
            let int_pair = NetemPair::spawn(int_addr, int_c2s, int_s2c).unwrap();
            let bulk_pair = NetemPair::spawn(bulk_addr, bulk_c2s, bulk_s2c).unwrap();

            let (observer, fec_cell) = fec_observer();
            let (opener, _accepter) = dual_mux_client_connect_lane_rtp_via(
                &task_tx,
                int_pair.client_addr(),
                bulk_pair.client_addr(),
                int_rtp,
                bulk_rtp,
                Some(observer),
                None,
            )
            .await
            .unwrap();

            // Interactive latency stream (`b'L'`) on the interactive lane.
            let (mut lat_read, mut lat_write) =
                opener.open(mux::LaneClass::Interactive).await.unwrap();
            submit_test_task(
                &task_tx,
                Box::pin(async move {
                    let mut buf = vec![0u8; 8 * 1024];
                    while let Ok(n) = lat_read.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                    }
                }),
            );

            // Bulk stream (`b'B'`) on the separate bulk lane, only when offered.
            let bulk_write = if impairment.has_bulk() {
                let (mut bulk_read, bulk_write) = opener.open(mux::LaneClass::Bulk).await.unwrap();
                submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        let mut buf = vec![0u8; 64 * 1024];
                        while let Ok(n) = bulk_read.read(&mut buf).await {
                            if n == 0 {
                                break;
                            }
                        }
                    }),
                );
                Some(bulk_write)
            } else {
                None
            };

            let interactive = async {
                if lat_write.write_all(b"L").await.is_err() {
                    return 0;
                }
                send_timestamped_messages(&mut lat_write, base, MSG_BYTES, CADENCE, RUN_FOR).await
            };
            let bulk_fut = async {
                let Some(mut write) = bulk_write else {
                    return 0;
                };
                if write.write_all(b"B").await.is_err() {
                    return 0;
                }
                let payload = cyclic_payload(BULK_BURST_BYTES);
                periodic_burst(
                    &mut write,
                    &payload,
                    BULK_BURST_BYTES,
                    BULK_PERIOD,
                    BULK_RAMP,
                    RUN_FOR,
                )
                .await
            };
            let (sent, _bulk_written) = tokio::join!(interactive, bulk_fut);

            tokio::time::sleep(GRACE).await;
            let int_counters = int_pair.stats();
            let bulk_counters = bulk_pair.stats();
            let bulk_wire_bytes = bulk_pair.stats_c2s().forwarded_bytes;
            let mut samples = Vec::new();
            while let Ok((tag, lat)) = latencies.try_recv() {
                if tag == b'L' {
                    samples.push(lat);
                }
            }
            let received = samples.len() as u64;
            let bulk_active_secs = (RUN_FOR - BULK_RAMP).as_secs_f64();
            let summary = summarize(samples, sent, received, bulk_wire_bytes, bulk_active_secs);
            let fec = *fec_cell.lock().unwrap();
            let bulk_sink_bytes = bulk_counter.load(Ordering::Relaxed);

            print_summary(label, &summary);
            eprintln!(
                "[duallane {label}] interactive={} bulk wire c2s forwarded = {bulk_wire_bytes} \
                 bytes / {} pkts; sink delivered = {bulk_sink_bytes} bytes",
                if interactive_reorder {
                    "fast-forward"
                } else {
                    "strict"
                },
                bulk_counters.forwarded,
            );
            eprintln!("[duallane {label}] interactive pair = {int_counters:?}");
            eprintln!("[duallane {label}] bulk pair = {bulk_counters:?}");
            if let Some(fec) = fec {
                eprintln!(
                    "[duallane {label}] fec parity_sent={} groups_flushed={} \
                     loss_gate_skips={} no_spare_capacity_skips={} burst_end_skips={} \
                     recovered={}",
                    fec.parity_sent,
                    fec.groups_flushed,
                    fec.groups_skipped_loss_gate,
                    fec.groups_skipped_no_spare_capacity,
                    fec.groups_skipped_burst_end,
                    fec.recovered_symbols,
                );
            }

            int_pair.stop();
            bulk_pair.stop();
            DualRun {
                run: JitterRun {
                    summary,
                    counters: int_counters,
                    fec,
                },
                bulk_wire_bytes,
                bulk_sink_bytes,
                bulk_counters,
            }
        })
        .await
}

/// Print the dual-lane headline table: the interactive-lane latency
/// percentiles plus the bulk lane's wire goodput (the matched-load check) and
/// the interactive lane's RTP FEC counters.
fn print_duallane_table(runs: &[(&str, &DualRun)]) {
    eprintln!(
        "[duallane] interactive lane at 2% per-packet loss, prompt FEC tuning; bulk lane is a \
         separate strict byte-stream FEC-free RTP connection"
    );
    eprintln!(
        "[duallane] arm                       p50     p90     p99     max  over250  ep  run  \
         bulk_wire_MiB/s  sink_MiB  parity  recovered"
    );
    for (name, r) in runs {
        let s = &r.run.summary;
        let f = r.run.fec.unwrap_or_default();
        let sink_mib = r.bulk_sink_bytes as f64 / (1024.0 * 1024.0);
        eprintln!(
            "[duallane] {name:<27} {p50:7.1} {p90:7.1} {p99:7.1} {max:7.1} {o25:7.3} {ep:3} {mr:3} \
             {bulk:>15.3} {sink:>9.3} {parity:>7} {recovered:>9}",
            p50 = s.p50,
            p90 = s.p90,
            p99 = s.p99,
            max = s.max,
            o25 = s.over250_pct,
            ep = s.episodes,
            mr = s.max_run,
            bulk = s.bulk_mibps,
            sink = sink_mib,
            parity = f.parity_sent,
            recovered = f.recovered_symbols,
        );
    }
    let find = |name: &str| runs.iter().find(|(n, _)| *n == name).map(|(_, r)| *r);
    let (Some(reorder), Some(strict)) = (find("both_reorder"), find("both_strict")) else {
        return;
    };
    let (a, b) = (&reorder.run.summary, &strict.run.summary);
    eprintln!(
        "[duallane] both_reorder - both_strict: p50={:+.1} p90={:+.1} p99={:+.1} max={:+.1} \
         over250={:+.3} episodes={:+} bulk_wire_MiB/s={:+.3}",
        a.p50 - b.p50,
        a.p90 - b.p90,
        a.p99 - b.p99,
        a.max - b.max,
        a.over250_pct - b.over250_pct,
        a.episodes as i64 - b.episodes as i64,
        a.bulk_mibps - b.bulk_mibps,
    );
}

/// Print the bulk lane's wire load and sink goodput for each arm of one
/// interactive frame mode — the matched-load evidence for the headline
/// comparison.
fn print_duallane_loads(mode: &str, runs: &[(&str, &DualRun)]) {
    eprintln!("[duallane {mode}] bulk-lane load (matched-load evidence):");
    for (name, r) in runs {
        eprintln!(
            "[duallane {mode}] {name:<6} wire_c2s={:>10} bytes ({:.3} MiB/s over {:?}) \
             pkts={:>7} sink={:>10} bytes",
            r.bulk_wire_bytes,
            r.run.summary.bulk_mibps,
            RUN_FOR - BULK_RAMP,
            r.bulk_counters.forwarded,
            r.bulk_sink_bytes,
        );
    }
}

/// Dual-lane interactive-latency decomposition. The deployment's topology is
/// reproduced at MATCHED bulk load: the interactive lane runs frame mode + FEC
/// on its own RTP connection (receiver-side fast-forward in the `reorder` arm,
/// strict in the `strict` arm) and the bulk lane is a SECOND, independent,
/// strict, FEC-free byte-stream RTP connection. Because the lanes are separate
/// connections the bulk burst cannot head-of-line block the interactive lane,
/// and because the two arms share the identical bulk lane the fast-forward win
/// — if any — is read at the same offered bulk load.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; eight ~35 s dual-lane arms (fast-forward + strict); run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_duallane_arms() {
    let impairments = [
        ("solo", DualImpairment::Solo),
        ("loss", DualImpairment::Loss),
        ("bulk", DualImpairment::Bulk),
        ("both", DualImpairment::Both),
    ];

    let mut reorder: Vec<(&str, DualRun)> = Vec::new();
    for (name, impairment) in impairments {
        let label = format!("duallane_reorder_fec/{name}");
        let run = with_timeout(
            Duration::from_secs(120),
            &label,
            run_duallane(&label, true, impairment),
        )
        .await;
        assert_sane(&label, &run.run.summary);
        reorder.push((name, run));
    }
    let mut strict: Vec<(&str, DualRun)> = Vec::new();
    for (name, impairment) in impairments {
        let label = format!("duallane_strict_fec/{name}");
        let run = with_timeout(
            Duration::from_secs(120),
            &label,
            run_duallane(&label, false, impairment),
        )
        .await;
        assert_sane(&label, &run.run.summary);
        strict.push((name, run));
    }

    // The requested arms, named for the headline comparison.
    eprintln!("[duallane] bulk_and_loss_duallane_reorder_fec = duallane_reorder_fec/both");
    eprintln!("[duallane] bulk_and_loss_duallane_strict_fec = duallane_strict_fec/both");

    let reorder_view: Vec<(&str, &DualRun)> = reorder.iter().map(|(n, r)| (*n, r)).collect();
    let strict_view: Vec<(&str, &DualRun)> = strict.iter().map(|(n, r)| (*n, r)).collect();
    let headline: Vec<(&str, &DualRun)> = vec![
        ("both_reorder", &reorder[3].1),
        ("both_strict", &strict[3].1),
    ];
    print_duallane_table(&headline);

    print_decomposition(
        "duallane-reorder-fec",
        &reorder[0].1.run,
        &reorder[1].1.run,
        &reorder[2].1.run,
        &reorder[3].1.run,
    );
    print_duallane_loads("duallane-reorder-fec", &reorder_view);
    print_decomposition(
        "duallane-strict-fec",
        &strict[0].1.run,
        &strict[1].1.run,
        &strict[2].1.run,
        &strict[3].1.run,
    );
    print_duallane_loads("duallane-strict-fec", &strict_view);
}
