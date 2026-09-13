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
//! Four cases:
//! * [`jitter_interactive_solo`] — ping stream alone on a delay+jitter link.
//!   The floor.
//! * [`jitter_interactive_with_bulk`] — ping stream plus a periodic bulk burst
//!   on a rate-limited link, run twice (bulk on / bulk off) to attribute the
//!   added latency to queueing under the same bottleneck. No loss.
//! * [`jitter_interactive_with_loss`] — ping stream on a lossy link, no bulk.
//!   Isolates loss/HOL repair latency.
//! * [`jitter_interactive_bulk_and_loss`] — all three together (realistic).
//!
//! These tests are `#[ignore]`-d by default so they do not slow normal builds.
//! Run them with (release is expected; multi-threaded scenario):
//!
//! ```sh
//! cargo test --release -p tests --test rtp_mux_jitter -- \
//!     --ignored --nocapture --test-threads=1
//! ```

use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::{
    mux_client_connect_via, send_timestamped_messages, spawn_mux_latency_bulk_server_via,
};
use support::payload::{cyclic_payload, with_timeout};
use support::rtp::rtp_connect_with_mss_via;
use support::stats::{HolSummary, combined_stats, summarize};
use support::{TestScope, submit_test_task};
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::time::MissedTickBehavior;

mod support;

/// One-way delay applied to every packet in both directions.
const OWD: Duration = Duration::from_millis(25);
/// Uniform jitter around [`OWD`].
const JITTER: Duration = Duration::from_millis(5);
/// Independent per-packet loss for the lossy cases (2%).
const LOSS: u32 = (u32::MAX / 100) * 2;
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

/// Build one impairment direction: fixed delay + jitter, optional 2% loss, and
/// an optional rate cap.
fn link(seed: u64, loss: bool, rate_bps: u64) -> NetemConfig {
    NetemConfig {
        latency: OWD,
        jitter: JITTER,
        rate: rate_bps,
        loss: if loss { LOSS } else { 0 },
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

/// Everything one jitter scenario needs.
#[derive(Clone, Debug)]
struct JitterScenario {
    label: String,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: Option<BulkSpec>,
}

/// Run one interactive-vs-bulk/loss jitter scenario and return its summary.
///
/// Spawns the combined mux-over-RTP server (it classifies each mux stream by
/// its first byte: `b'L'` = timestamped latency frames, any other byte = bulk
/// sink), one [`NetemPair`], and one mux connection carrying both streams. The
/// interactive `b'L'` stream sends [`MSG_BYTES`] messages every [`CADENCE`] for
/// [`RUN_FOR`], optionally contested by a periodic `b'B'` bulk burst.
async fn run_jitter(scenario: JitterScenario) -> HolSummary {
    let JitterScenario {
        label,
        c2s,
        s2c,
        bulk,
    } = scenario;
    let base = Instant::now();
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, mut latencies, bulk_counter) =
                spawn_mux_latency_bulk_server_via(&task_tx, false, base)
                    .await
                    .unwrap();
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

            let (connected_read, connected_write) =
                rtp_connect_with_mss_via(&task_tx, pair.client_addr(), false, rtp::udp::NO_FEC_MSS)
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
            let mut samples = Vec::new();
            while let Ok(lat) = latencies.try_recv() {
                samples.push(lat);
            }
            let received = samples.len() as u64;
            let summary = summarize(
                samples,
                sent,
                received,
                bulk_counter.load(std::sync::atomic::Ordering::Relaxed),
                RUN_FOR.as_secs_f64(),
            );

            print_summary(&label, &summary);
            eprintln!("[jitter {label}] pair stats = {:?}", combined_stats(&pair));

            pair.stop();
            summary
        })
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

/// The floor: interactive pings on a delay+jitter link, no loss, no bulk.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_solo() {
    let label = "solo".to_owned();
    let summary = with_timeout(
        Duration::from_secs(120),
        &label,
        run_jitter(JitterScenario {
            label: label.clone(),
            c2s: link(11, false, 0),
            s2c: link(12, false, 0),
            bulk: None,
        }),
    )
    .await;
    assert_sane(&label, &summary);
}

/// Isolates loss/HOL repair latency: interactive pings on a 2% lossy link,
/// no bulk.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_with_loss() {
    let label = "loss".to_owned();
    let summary = with_timeout(
        Duration::from_secs(120),
        &label,
        run_jitter(JitterScenario {
            label: label.clone(),
            c2s: link(21, true, 0),
            s2c: link(22, true, 0),
            bulk: None,
        }),
    )
    .await;
    assert_sane(&label, &summary);
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
    let bulk_on_label = "bulk-on".to_owned();
    let bulk_on = with_timeout(
        Duration::from_secs(120),
        &bulk_on_label,
        run_jitter(JitterScenario {
            label: bulk_on_label.clone(),
            c2s: link(31, false, BULK_RATE_BPS),
            s2c: link(32, false, BULK_RATE_BPS),
            bulk: Some(BulkSpec {
                burst_bytes: BULK_BURST_BYTES,
                period: BULK_PERIOD,
            }),
        }),
    )
    .await;

    let bulk_off_label = "bulk-off".to_owned();
    let bulk_off = with_timeout(
        Duration::from_secs(120),
        &bulk_off_label,
        run_jitter(JitterScenario {
            label: bulk_off_label.clone(),
            c2s: link(31, false, BULK_RATE_BPS),
            s2c: link(32, false, BULK_RATE_BPS),
            bulk: None,
        }),
    )
    .await;

    eprintln!(
        "[jitter attribution] bulk-on minus bulk-off: p50={:.1} ms p90={:.1} ms \
         p99={:.1} ms max={:.1} ms over250={:+.3}",
        bulk_on.p50 - bulk_off.p50,
        bulk_on.p90 - bulk_off.p90,
        bulk_on.p99 - bulk_off.p99,
        bulk_on.max - bulk_off.max,
        bulk_on.over250_pct - bulk_off.over250_pct,
    );

    assert_sane(&bulk_on_label, &bulk_on);
    assert_sane(&bulk_off_label, &bulk_off);
}

/// The realistic case: interactive pings plus the periodic bulk burst on a 2%
/// lossy, rate-limited link.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "spawns threads and binds ephemeral ports; ~35 s measurement; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn jitter_interactive_bulk_and_loss() {
    let label = "bulk+loss".to_owned();
    let summary = with_timeout(
        Duration::from_secs(120),
        &label,
        run_jitter(JitterScenario {
            label: label.clone(),
            c2s: link(41, true, BULK_RATE_BPS),
            s2c: link(42, true, BULK_RATE_BPS),
            bulk: Some(BulkSpec {
                burst_bytes: BULK_BURST_BYTES,
                period: BULK_PERIOD,
            }),
        }),
    )
    .await;
    assert_sane(&label, &summary);
}
