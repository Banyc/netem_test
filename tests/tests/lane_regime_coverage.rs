//! Impairment regimes the perf battery's lanes structurally cannot reach.
//!
//! The perf battery's verdict lanes are `controller-fat-pipe` and
//! `deterministic-iid-loss-fat-pipe`: both are 100 Mbit/s with a 150 ms
//! one-way delay and **zero configured jitter**. Those two facts — a
//! configured rate, and no jitter — each make the delivered order equal the
//! sent order, for different reasons:
//!
//! * zero jitter leaves the deadlines monotonic, so `NetemState` forwards on
//!   the FIFO path;
//! * a configured rate schedules each packet at `max(now + delay,
//!   previous_send) + serialization` in the send-time shaper (with no reorder
//!   gap, which no preset sets), so a packet can never leave before the one
//!   ahead of it — the sampled jitter is realized as a running maximum, never
//!   as reordering.
//!
//! So on every lane the battery runs, the reorder-vs-fast-loss decision is
//! unreachable and the receiver's RTT variance is whatever queue noise the
//! endpoint itself creates. These scenarios measure the two lanes that restore
//! the missing regimes:
//!
//! * [`jittery_short_rtt_link`] is unshaped with a 20 ms one-way delay and
//!   +/-15 ms of uniform per-packet jitter: the only lane that reorders, and
//!   the only one whose `4 * rttvar` reaches `srtt / 4` — the boundary `rtp`'s
//!   `RtxTimer::fast_loss_armed` compares.
//! * [`high_rtt_low_rate_bottleneck`] is 200 kbit/s with a 400 ms one-way
//!   delay and a 128-packet queue. Its link alone can hold a round trip long
//!   enough to push RFC 6298's `srtt + 4 * rttvar` into the tens of seconds,
//!   the regime the battery lanes cannot produce.
//!
//! Both lanes are deterministic from their seed like every other preset:
//! `sample_delay` draws one `CorRng` value per delayed packet from the
//! Tausworthe `prandom` state seeded by `NetemConfig::seed`, the rate shaper
//! derives every deadline from `serialization_delay(len, rate)`, and the queue
//! limit tail-drops before any scheduling arithmetic. The lanes are measured
//! here through the real `NetemPair` forwarding path, so the numbers below are
//! the impairment the transport would actually see.
//!
//! Run the default tier with `cargo test -p tests`; the low-rate lane's
//! fourteen-second regime measurement is `#[ignore]`d into the `standard` tier
//! and runs with `cargo test -p tests -- --ignored --test-threads=1`.

use std::time::{Duration, Instant};

use netem_test::kit::presets::{
    clean_delay_link, controller_fat_pipe, deterministic_iid_loss_fat_pipe,
    high_rtt_low_rate_bottleneck, jittery_short_rtt_link,
};
use netem_test::kit::stats::combined_stats;
use netem_test::kit::{TEST_TASK_QUEUE_BOUND, TestScope, submit_test_task};
use netem_test::{NetemConfig, NetemPair};
use tokio::net::UdpSocket;

/// Identifier width of the echo payload: an eight-byte big-endian packet id.
const ID_BYTES: usize = 8;

/// What one lane did to a sequence of echo packets.
struct LaneObservation {
    /// Round-trip time of each delivered packet, indexed by packet id.
    rtts: Vec<Duration>,
    /// Packet id at each arrival rank, so `arrival[rank] = packet id`.
    arrival: Vec<u64>,
    /// Delivered (echoed) packet count; `sent - delivered` packets were lost.
    delivered: usize,
    sent: usize,
    reordered: u64,
    dropped: u64,
    queue_limit_overflow: u64,
}

impl LaneObservation {
    /// Number of delivered packets that arrived behind a later-sent packet:
    /// the deliveries a reorder-tolerant receiver must not call loss.
    fn inversions(&self) -> usize {
        let mut inversions = 0;
        let mut highest = 0u64;
        for &id in &self.arrival {
            if id < highest {
                inversions += 1;
            }
            highest = highest.max(id);
        }
        inversions
    }

    /// Duration under which `fraction` of the delivered round trips fall.
    fn quantile(&self, fraction: f64) -> Duration {
        let mut sorted: Vec<Duration> = self.rtts.clone();
        sorted.sort_unstable();
        if sorted.is_empty() {
            return Duration::ZERO;
        }
        let index = ((sorted.len() - 1) as f64 * fraction).round() as usize;
        sorted[index]
    }
}

/// Drive `packets` echo round trips through `config` in both directions and
/// record what came back. `spacing` paces the sends; a zero spacing sends the
/// whole burst back-to-back. `window` bounds the receive loop.
async fn observe_lane(
    config: &NetemConfig,
    packets: usize,
    payload_bytes: usize,
    spacing: Duration,
    window: Duration,
) -> LaneObservation {
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TEST_TASK_QUEUE_BOUND);
    tasks
        .run(async move {
            let echo = UdpSocket::bind("127.0.0.1:0").await.unwrap();
            let echo_addr = echo.local_addr().unwrap();
            submit_test_task(
                &task_tx,
                Box::pin(async move {
                    let mut buf = [0u8; 16 * 1024];
                    while let Ok((n, from)) = echo.recv_from(&mut buf).await {
                        let _ = echo.send_to(&buf[..n], from).await;
                    }
                }),
            );

            // The probe seeds the two directions from one seed and seed + 1;
            // the same convention keeps a lane's two directions distinguishable
            // while staying reproducible.
            let mut c2s = config.clone();
            c2s.seed = config.seed;
            let mut s2c = config.clone();
            s2c.seed = config.seed.wrapping_add(1);
            let pair = NetemPair::spawn(echo_addr, c2s, s2c).unwrap();
            let client = std::sync::Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
            client.connect(pair.client_addr()).await.unwrap();

            // Send and receive concurrently: a receive loop that only starts
            // after the last send would report the send loop's own duration as
            // round-trip time, and the kernel would have re-ordered nothing
            // because it delivers an already-buffered backlog in arrival order.
            let sends = std::sync::Arc::new(std::sync::Mutex::new(Vec::with_capacity(packets)));
            let sender_sends = std::sync::Arc::clone(&sends);
            let sender_client = std::sync::Arc::clone(&client);
            submit_test_task(
                &task_tx,
                Box::pin(async move {
                    let mut payload = vec![0u8; payload_bytes];
                    for id in 0..packets as u64 {
                        payload[..ID_BYTES].copy_from_slice(&id.to_be_bytes());
                        sender_sends
                            .lock()
                            .expect("send clock lock")
                            .push(Instant::now());
                        sender_client.send(&payload).await.unwrap();
                        if !spacing.is_zero() {
                            tokio::time::sleep(spacing).await;
                        }
                    }
                }),
            );

            let mut rtts = vec![Duration::ZERO; packets];
            let mut arrival = Vec::with_capacity(packets);
            let mut buf = vec![0u8; 16 * 1024];
            let deadline = Instant::now() + window;
            while arrival.len() < packets {
                let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                    break;
                };
                let Ok(Ok(n)) = tokio::time::timeout(remaining, client.recv(&mut buf)).await else {
                    break;
                };
                assert!(n >= ID_BYTES, "echo payload too short: {n}");
                let id = u64::from_be_bytes(buf[..ID_BYTES].try_into().unwrap());
                let received = Instant::now();
                let sent_at = sends.lock().expect("send clock lock")[id as usize];
                rtts[id as usize] = received.saturating_duration_since(sent_at);
                arrival.push(id);
            }

            pair.stop();
            let stats = combined_stats(&pair);
            LaneObservation {
                delivered: arrival.len(),
                sent: packets,
                rtts,
                arrival,
                reordered: stats.reordered,
                dropped: stats.dropped,
                queue_limit_overflow: stats.overflow_dropped,
            }
        })
        .await
}

/// The preset's shape with a rate added: the same delay sampling, but the
/// send-time shaper's serialization clock now bounds every send by the
/// previous one.
fn rate_shaped(config: &NetemConfig, rate: u64) -> NetemConfig {
    NetemConfig {
        rate,
        ..config.clone()
    }
}

/// `rtp`'s RTO recurrence, transcribed from
/// `crates/rtp/src/traffic_shaping/recovery/rto.rs` (`RtxTimer`):
///
/// * the first sample seeds `srtt = rtt` and `rttvar = rtt / 2`;
/// * afterwards `rttvar = 3/4 * rttvar + 1/4 * |srtt_before - rtt|` and
///   `srtt = 7/8 * srtt + 1/8 * rtt` (RFC 6298 with `ALPHA = 1/8`,
///   `BETA = 1/4`);
/// * `raw_rto = srtt + 4 * rttvar` (K = 4) and
///   `rto = max(raw_rto, MIN_RTO = 1 s)`.
///
/// `fast_loss_armed` is the same struct's `4 * rttvar < srtt / 4` gate. This
/// is a transcription of the estimator's arithmetic, not the transport: the
/// samples it is fed are measured through the real impairment path, so a lane
/// that cannot produce a sample large enough to matter is visible as an
/// unchanged `rto` here too.
#[derive(Default)]
struct RtoRecurrence {
    srtt: Option<Duration>,
    rttvar: Duration,
    max_rto: Duration,
    max_raw_rto: Duration,
    floor_pinned: usize,
    samples: usize,
}

impl RtoRecurrence {
    fn record(&mut self, rtt: Duration) {
        match self.srtt {
            None => {
                self.srtt = Some(rtt);
                self.rttvar = rtt / 2;
            }
            Some(srtt) => {
                let deviation = if srtt > rtt { srtt - rtt } else { rtt - srtt };
                self.rttvar = self.rttvar.mul_f64(0.75) + deviation.mul_f64(0.25);
                self.srtt = Some(srtt.mul_f64(0.875) + rtt.mul_f64(0.125));
            }
        }
        let srtt = self.srtt.expect("seeded above");
        let raw = srtt + self.rttvar.mul_f64(4.0);
        let rto = raw.max(Duration::from_secs(1));
        if raw < Duration::from_secs(1) {
            self.floor_pinned += 1;
        }
        self.max_raw_rto = self.max_raw_rto.max(raw);
        self.max_rto = self.max_rto.max(rto);
        self.samples += 1;
    }

    /// `RtxTimer::fast_loss_armed`.
    fn fast_loss_armed(&self) -> bool {
        self.rttvar.mul_f64(4.0) < self.srtt.expect("seeded") / 4
    }

    /// `4 * rttvar`, the margin the arming gate compares against `srtt / 4`.
    fn variance_margin(&self) -> Duration {
        self.rttvar.mul_f64(4.0)
    }
}

/// Feed a lane's delivered round trips into `rtp`'s recurrence in arrival
/// order, which is the order the receiver would fold them in.
fn rto_over(observation: &LaneObservation) -> RtoRecurrence {
    let mut rto = RtoRecurrence::default();
    for &id in &observation.arrival {
        rto.record(observation.rtts[id as usize]);
    }
    rto
}

fn fmt(duration: Duration) -> String {
    format!("{:.3} ms", duration.as_secs_f64() * 1e3)
}

fn report(label: &str, observation: &LaneObservation, rto: &RtoRecurrence) {
    println!(
        "{label}: delivered {}/{}, inversions {}, netem reordered {}, dropped {}, \
         queue overflow {}, RTT p25 {}, p50 {}, p75 {}, max {}, srtt {}, \
         rttvar {}, 4*rttvar {}, srtt/4 {}, armed {}, max raw RTO {}, max RTO \
         {} (floor-pinned {}/{})",
        observation.delivered,
        observation.sent,
        observation.inversions(),
        observation.reordered,
        observation.dropped,
        observation.queue_limit_overflow,
        fmt(observation.quantile(0.25)),
        fmt(observation.quantile(0.5)),
        fmt(observation.quantile(0.75)),
        fmt(observation.quantile(1.0)),
        fmt(rto.srtt.unwrap()),
        fmt(rto.rttvar),
        fmt(rto.variance_margin()),
        fmt(rto.srtt.unwrap() / 4),
        rto.fast_loss_armed(),
        fmt(rto.max_raw_rto),
        fmt(rto.max_rto),
        rto.floor_pinned,
        rto.samples,
    );
}

/// The two battery lanes deliver strictly in order, because zero jitter leaves
/// their deadlines monotonic. Adding the jitter lane's delay sampling to a
/// *rate-shaped* link still delivers in order: the shaper's serialization clock
/// schedules every packet behind the previous one. Only the unshaped lane
/// reorders, so it is the only lane on which the reorder-vs-fast-loss decision
/// is reachable at all.
#[tokio::test(flavor = "multi_thread")]
async fn jittery_lane_reorders_where_every_battery_lane_and_a_rate_shaped_jitter_lane_cannot() {
    let mut observations = Vec::new();
    for (name, config) in [
        ("controller-fat-pipe", controller_fat_pipe()),
        (
            "deterministic-iid-loss-fat-pipe",
            deterministic_iid_loss_fat_pipe(),
        ),
        (
            "jittery-short-rtt-rate-shaped",
            rate_shaped(&jittery_short_rtt_link(), 100 * 1000 * 1000),
        ),
        ("jittery-short-rtt", jittery_short_rtt_link()),
    ] {
        let observation = observe_lane(
            &config,
            200,
            128,
            Duration::from_millis(1),
            Duration::from_secs(4),
        )
        .await;
        println!(
            "{name}: delivered {}/{}, inversions {}, netem reordered {}, \
             dropped {}, queue overflow {}, RTT p25 {}, p50 {}, p75 {}, max {}",
            observation.delivered,
            observation.sent,
            observation.inversions(),
            observation.reordered,
            observation.dropped,
            observation.queue_limit_overflow,
            fmt(observation.quantile(0.25)),
            fmt(observation.quantile(0.5)),
            fmt(observation.quantile(0.75)),
            fmt(observation.quantile(1.0)),
        );
        observations.push((name, observation));
    }

    let (jittery_name, jittery) = observations.last().expect("jitter lane measured");
    assert_eq!(*jittery_name, "jittery-short-rtt");
    assert!(
        jittery.inversions() >= 50,
        "the unshaped jitter lane must reorder: {} inverted deliveries",
        jittery.inversions()
    );
    for (name, observation) in &observations[..observations.len() - 1] {
        assert_eq!(
            observation.inversions(),
            0,
            "{name} must deliver in order: {} inverted deliveries",
            observation.inversions()
        );
        assert_eq!(
            observation.reordered, 0,
            "{name} must record no netem reorder"
        );
        assert!(
            observation.delivered * 10 >= observation.sent * 9,
            "{name} must deliver essentially every packet: {} of {}",
            observation.delivered,
            observation.sent
        );
    }
    // The rate-shaped variant of the very same jitter config keeps its
    // configured jitter but loses the reordering, which is why the jitter lane
    // must leave the rate unset.
    let (_, rate_shaped_jitter) = &observations[2];
    assert!(
        rate_shaped_jitter.inversions() == 0 && jittery.inversions() > 0,
        "the shaper's serialization clock is what suppresses reordering"
    );
}

/// The jitter lane's spread is impairment-driven, not measurement noise: the
/// same shape with the jitter removed (and the same seed and pacing) has an
/// interquartile spread an order of magnitude smaller. And the same samples
/// move `rtp`'s fast-loss arming quantity across its boundary, which the
/// battery lanes' near-zero variance cannot do.
#[tokio::test(flavor = "multi_thread")]
async fn jittery_lane_moves_the_variance_the_fast_loss_gate_decides_on() {
    let jitterless = clean_delay_link(Duration::from_millis(20), 4);
    let observation = observe_lane(
        &jitterless,
        200,
        128,
        Duration::from_millis(1),
        Duration::from_secs(4),
    )
    .await;
    let rto = rto_over(&observation);
    report(
        "clean-delay-link-20ms (no jitter control)",
        &observation,
        &rto,
    );
    let control_iqr = observation.quantile(0.75) - observation.quantile(0.25);
    assert!(
        rto.fast_loss_armed(),
        "the jitterless control must arm the fast-loss gate"
    );
    assert!(
        control_iqr <= Duration::from_millis(3),
        "the control's spread must be measurement noise, got {control_iqr:?}"
    );

    for (name, config) in [
        ("controller-fat-pipe", controller_fat_pipe()),
        (
            "deterministic-iid-loss-fat-pipe",
            deterministic_iid_loss_fat_pipe(),
        ),
    ] {
        let observation = observe_lane(
            &config,
            200,
            128,
            Duration::from_millis(1),
            Duration::from_secs(4),
        )
        .await;
        let rto = rto_over(&observation);
        report(name, &observation, &rto);
        assert!(
            rto.fast_loss_armed(),
            "{name} must arm the fast-loss gate: 4*rttvar = {} vs srtt/4 = {}",
            fmt(rto.variance_margin()),
            fmt(rto.srtt.unwrap() / 4)
        );
        assert!(
            rto.variance_margin() * 20 <= rto.srtt.unwrap() / 4,
            "{name}'s variance must sit far below the arming boundary, got \
             4*rttvar = {} vs srtt/4 = {}",
            fmt(rto.variance_margin()),
            fmt(rto.srtt.unwrap() / 4)
        );
        // The negative control, stated as a rule rather than a third lane:
        // `armed_once_mature` is the gate every battery-lane measurement above
        // is consistent with, and it agrees with the real gate here.
        let armed_once_mature = true;
        assert_eq!(
            armed_once_mature,
            rto.fast_loss_armed(),
            "{name} cannot separate the two gates"
        );
        // So does a gate that drops the RFC 6298 `K` factor (`rttvar <
        // srtt / 4`): on this lane both variants arm, so the lane cannot tell
        // them apart either.
        assert!(
            rto.rttvar < rto.srtt.unwrap() / 4,
            "{name} must be consistent with the K-less gate too"
        );
    }

    let lane = jittery_short_rtt_link();
    let observation = observe_lane(
        &lane,
        200,
        128,
        Duration::from_millis(1),
        Duration::from_secs(4),
    )
    .await;
    let rto = rto_over(&observation);
    report("jittery-short-rtt", &observation, &rto);
    assert_eq!(
        observation.delivered, observation.sent,
        "the jitter lane has no loss configured, so every echo must return"
    );
    assert_eq!(observation.dropped, 0, "the jitter lane configures no loss");
    assert_eq!(
        observation.queue_limit_overflow, 0,
        "the jitter lane must not tail-drop"
    );
    let iqr = observation.quantile(0.75) - observation.quantile(0.25);
    assert!(
        iqr >= Duration::from_millis(10),
        "the jitter lane's delay spread must be impairment-driven, got IQR {} \
         against the control's {}",
        fmt(iqr),
        fmt(control_iqr)
    );
    assert!(
        !rto.fast_loss_armed(),
        "the jitter lane must leave the fast-loss gate unarmed: 4*rttvar = {} \
         vs srtt/4 = {}",
        fmt(rto.variance_margin()),
        fmt(rto.srtt.unwrap() / 4)
    );
    assert!(
        rto.variance_margin() >= rto.srtt.unwrap() / 4,
        "the jitter lane must cross the arming boundary"
    );
    // The same negative control: the rule every battery lane is consistent
    // with disagrees with the real gate here and only here.
    let armed_once_mature = true;
    assert_ne!(
        armed_once_mature,
        rto.fast_loss_armed(),
        "the gate must land on the unarmed side of the boundary that the \
         battery lanes cannot cross"
    );
    // And the plausible defect — the same gate with the RFC 6298 `K` factor
    // dropped — arms here while the real gate does not, so this lane is the
    // only measurement in the harness that can tell `rttvar < srtt / 4` from
    // `4 * rttvar < srtt / 4`.
    assert!(
        rto.rttvar < rto.srtt.unwrap() / 4,
        "the K-less gate must arm on the jitter lane"
    );
    assert!(
        !rto.fast_loss_armed(),
        "the real gate must not arm on the jitter lane"
    );
}

/// The low-rate lane reaches the round-trip regime that inflates RFC 6298's
/// estimate into the tens of seconds from the link alone. The battery lane
/// receives the identical burst shape and stays pinned at the 1 s MIN_RTO
/// floor, so the same estimator arithmetic is unobservable there.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "standard tier: the low-rate lane needs ~14 s of link time to build its standing queue"]
async fn high_rtt_low_rate_lane_reaches_a_tens_of_seconds_rto_the_battery_lanes_cannot() {
    const BULK_MSS_BYTES: usize = 8192;
    // Each queued 8192-byte datagram adds one 327.68 ms serialization at 200
    // kbit/s, so the last echo of a 40-datagram burst returns after roughly
    // 13.6 s and the recurrence is already past 20 s. The burst is paced at 1
    // ms, which is far below the link's serialization step, so the backlog
    // still accumulates exactly as an unpaced burst would.
    const BURST_PACKETS: usize = 40;
    const BURST_SPACING: Duration = Duration::from_millis(1);

    let lane = high_rtt_low_rate_bottleneck();
    let observation = observe_lane(
        &lane,
        BURST_PACKETS,
        BULK_MSS_BYTES,
        BURST_SPACING,
        Duration::from_secs(40),
    )
    .await;
    let lane_rto = rto_over(&observation);
    report("high-rtt-low-rate", &observation, &lane_rto);
    assert_eq!(
        observation.delivered, observation.sent,
        "the low-rate lane has no loss configured; its 128-packet queue must \
         hold the burst"
    );
    assert_eq!(
        observation.reordered, 0,
        "the low-rate lane does not jitter"
    );
    assert!(
        lane_rto.max_rto >= Duration::from_secs(20),
        "the low-rate lane must reach the tens-of-seconds RTO regime, got {}",
        fmt(lane_rto.max_rto)
    );

    let battery = controller_fat_pipe();
    let observation = observe_lane(
        &battery,
        BURST_PACKETS,
        BULK_MSS_BYTES,
        BURST_SPACING,
        Duration::from_secs(10),
    )
    .await;
    let battery_rto = rto_over(&observation);
    report("controller-fat-pipe burst", &observation, &battery_rto);
    assert_eq!(
        observation.delivered, observation.sent,
        "the fat pipe must deliver the identical burst"
    );
    assert!(
        battery_rto.max_rto <= Duration::from_millis(1500),
        "the fat pipe cannot leave the MIN_RTO floor, got {}",
        fmt(battery_rto.max_rto)
    );
    assert!(
        lane_rto.max_rto >= battery_rto.max_rto * 10,
        "the low-rate lane must separate from the fat pipe by an order of \
         magnitude: {} vs {}",
        fmt(lane_rto.max_rto),
        fmt(battery_rto.max_rto)
    );
}
