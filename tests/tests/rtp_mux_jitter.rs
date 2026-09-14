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
//!   measure the deployment's `frame_reassembly` path alongside byte-stream.
//! * [`jitter_fec_arms_2pct`] / [`jitter_fec_arms_6pct`] — the interactive
//!   lane with FEC `off` / stock (`default`) / prompt parity
//!   (`instream_flush=true, small_group_parity_count=1`) at 2% and 6% loss,
//!   plus `bulk_and_loss_fec_prompt`, with the RTP FEC counters (parity sent,
//!   loss-gate skips) captured so the 5% gate's effect is visible directly.
//!
//! The remaining single-scenario tests keep the original arms for continuity.
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
use support::frame::rtp_frame_delivery_connect_via;
use support::mux::{
    mux_client_connect_frame_delivery_via, mux_client_connect_via, send_timestamped_messages,
    spawn_mux_frame_delivery_latency_bulk_server_via,
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
/// (`rtp_mux` sets `frame_reassembly: true`). FEC is off because the
/// frame-delivery server helper takes no tuning; the frame arms therefore use
/// the no-FEC [`scen`] defaults.
async fn run_jitter_frame(scenario: JitterScenario) -> JitterRun {
    let JitterScenario {
        label,
        c2s,
        s2c,
        bulk,
        ..
    } = scenario;
    let base = Instant::now();
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, mut latencies, bulk_counter) =
                spawn_mux_frame_delivery_latency_bulk_server_via(&task_tx, false, base)
                    .await
                    .unwrap();
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

            let (reader, writer) =
                rtp_frame_delivery_connect_via(&task_tx, pair.client_addr(), false).await;
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
            let mut samples = Vec::new();
            while let Ok((tag, lat)) = latencies.try_recv() {
                if tag == b'L' {
                    samples.push(lat);
                }
            }
            let received = samples.len() as u64;
            let summary = summarize(
                samples,
                sent,
                received,
                bulk_counter.load(Ordering::Relaxed),
                RUN_FOR.as_secs_f64(),
            );

            print_summary(&label, &summary);
            eprintln!("[jitter {label}] pair stats = {counters:?}");

            pair.stop();
            JitterRun {
                summary,
                counters,
                fec: None,
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
