//! Head-of-line-blocking probe: sparse interactive messages competing with a
//! bulk flow through the same bottleneck.
//!
//! These tests are `#[ignore]`-d by default so they do not slow normal builds
//! and because they depend on the in-flight `rtp`/`mux` path dependencies.
//! Run them with:
//!
//! ```sh
//! cargo test --release --test hol_probe -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::{Arc, atomic::Ordering};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair, SharedShaper};
use support::{
    combined_stats, cyclic_payload, dual_mux_client_connect_with_lane_modes, gilbert_elliott_loss,
    mux_client_connect, percentile, rtp_frame_delivery_connect, rtp_mux_connector,
    send_timestamped_messages, spawn_dual_mux_latency_bulk_server_two_listeners,
    spawn_mux_frame_delivery_latency_bulk_server, spawn_mux_latency_bulk_server,
    spawn_rtp_bulk_upload, spawn_rtp_byte_sink_server, spawn_rtp_mux_latency_bulk_server,
    with_timeout,
};
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};

mod support;

/// One-way delay for the rtt100 rows.
const OWD_100MS: Duration = Duration::from_millis(50);

/// Default interactive message size.
const DEFAULT_MSG_BYTES: usize = 256;
/// Default cadence between interactive messages.
const DEFAULT_CADENCE: Duration = Duration::from_millis(25);
/// Default interactive run time: 1.5 s ramp + 15 s steady-state ping window.
const DEFAULT_RUN_FOR: Duration = Duration::from_millis(16_500);
/// Default grace period for stragglers.
const DEFAULT_GRACE: Duration = Duration::from_secs(3);
/// Bulk ramp: interactive runs solo for this long before the bulk flow starts.
const BULK_RAMP: Duration = Duration::from_millis(1500);

/// How a competing bulk flow shares the bottleneck with the interactive stream.
#[derive(Clone, Debug)]
pub enum BulkMode {
    /// No competing bulk flow.
    None,
    /// Second mux stream on the same RTP connection / NetemPair.
    Shared,
    /// Separate independent NetemPair for the bulk flow.
    Split(Box<(NetemConfig, NetemConfig)>),
    /// Two NetemPairs sharing one [`SharedShaper`] per direction.
    SplitSharedBneck(SharedShaper, SharedShaper),
}

/// Summary returned by [`run_hol_probe`].
#[derive(Clone, Debug, Default)]
pub struct HolSummary {
    /// Interactive messages sent.
    pub sent: u64,
    /// Interactive messages received.
    pub received: u64,
    /// `received / sent`.
    pub delivery_pct: f64,
    /// Median one-way latency in ms.
    pub p50: f64,
    /// 90th percentile one-way latency in ms.
    pub p90: f64,
    /// 99th percentile one-way latency in ms.
    pub p99: f64,
    /// Maximum one-way latency in ms.
    pub max: f64,
    /// Fraction of samples > 250 ms.
    pub over250_pct: f64,
    /// Fraction of samples > 1000 ms.
    pub over1000_pct: f64,
    /// Number of contiguous episodes with latency > 250 ms.
    pub episodes: u64,
    /// Longest contiguous run of samples > 250 ms.
    pub max_run: u64,
    /// Bulk goodput in MiB/s (0 if no bulk flow).
    pub bulk_mibps: f64,
}

#[derive(Clone, Copy, Debug)]
struct TrafficConfig {
    msg_bytes: usize,
    cadence: Duration,
    run_for: Duration,
    grace: Duration,
}

#[derive(Clone, Debug)]
struct HolProbeConfig {
    bulk: BulkMode,
    fec: bool,
    traffic: TrafficConfig,
}

#[derive(Clone, Copy, Debug)]
struct DualLaneProbeConfig {
    interactive_frame: bool,
    bulk_frame: bool,
    traffic: TrafficConfig,
}

#[derive(Clone, Copy, Debug)]
struct FrameDeliveryProbeConfig {
    fec: bool,
    traffic: TrafficConfig,
}

/// Run one head-of-line-blocking probe.
///
/// Spawns a mux-over-RTP server that accepts one RTP connection. The server
/// classifies each mux stream by its first byte: `b'L'` streams push
/// timestamped one-way latencies into a channel, and any other tag is treated
/// as a deterministic bulk byte sink. An interactive `b'L'` stream sends
/// `msg_bytes`-sized timestamped messages every `cadence` for `run_for`,
/// optionally contested by a bulk flow depending on `bulk`. A `BULK_RAMP`
/// interval at the start lets the solo baseline establish before the bulk
/// flow begins.
async fn run_hol_probe(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    config: HolProbeConfig,
) -> HolSummary {
    let HolProbeConfig {
        bulk,
        fec,
        traffic:
            TrafficConfig {
                msg_bytes,
                cadence,
                run_for,
                grace,
            },
    } = config;
    let base = Instant::now();
    let (server_addr, mut latencies, mux_bulk_counter) =
        spawn_mux_latency_bulk_server(fec, base).await.unwrap();

    // Set up the interactive NetemPair and, for split modes, a bulk pair.
    let (interactive_pair, bulk_pair_opt, bulk_counter) = match &bulk {
        BulkMode::None | BulkMode::Shared => {
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
            (pair, None, Arc::clone(&mux_bulk_counter))
        }
        BulkMode::Split(box_config) => {
            let (c2s_bulk, s2c_bulk) = box_config.as_ref();
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
            let (sink_addr, counter) = spawn_rtp_byte_sink_server(fec).await.unwrap();
            let bulk_pair =
                NetemPair::spawn(sink_addr, c2s_bulk.clone(), s2c_bulk.clone()).unwrap();
            (pair, Some(bulk_pair), counter)
        }
        BulkMode::SplitSharedBneck(shaper_c2s, shaper_s2c) => {
            let c2s_bulk = c2s.clone();
            let s2c_bulk = s2c.clone();
            let pair = NetemPair::spawn_shared(
                server_addr,
                c2s,
                s2c,
                Some(shaper_c2s.clone()),
                Some(shaper_s2c.clone()),
            )
            .unwrap();
            let (sink_addr, counter) = spawn_rtp_byte_sink_server(fec).await.unwrap();
            let bulk_pair = NetemPair::spawn_shared(
                sink_addr,
                c2s_bulk,
                s2c_bulk,
                Some(shaper_c2s.clone()),
                Some(shaper_s2c.clone()),
            )
            .unwrap();
            (pair, Some(bulk_pair), counter)
        }
    };

    // Connect the interactive mux client.
    let connected = rtp::udp::connect_without_handshake_with_mss(
        "0.0.0.0:0",
        &interactive_pair.client_addr().to_string(),
        None,
        fec,
        rtp::udp::NO_FEC_MSS,
    )
    .await
    .unwrap();
    let (opener, _mux_spawner) = mux_client_connect(
        connected.read.into_async_read(),
        connected.write.into_async_write(),
    );

    // Open the interactive `b'L'` stream and keep its read half alive.
    let (mut rr_read, mut rr_write) = opener.open().await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = rr_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });

    // For Shared mode, open a second mux stream for the bulk flow.
    let mut shared_bulk_write = None;
    if matches!(bulk, BulkMode::Shared) {
        let (mut bulk_read, bulk_write) = opener.open().await.unwrap();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 8 * 1024];
            while let Ok(n) = bulk_read.read(&mut buf).await {
                if n == 0 {
                    break;
                }
            }
        });
        shared_bulk_write = Some(bulk_write);
    }

    let active_for = run_for - BULK_RAMP;
    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));

    // Run the interactive sender and the bulk sender concurrently.
    // For split modes, spawn the bulk flow on its own pair BEFORE the
    // join so it runs concurrently with the interactive sender.
    let split_bulk_handle = match &bulk {
        BulkMode::Split(_) | BulkMode::SplitSharedBneck(_, _) => {
            let client_addr = bulk_pair_opt.as_ref().unwrap().client_addr();
            Some(tokio::spawn(run_rtp_bulk_flow(
                client_addr,
                fec,
                Arc::clone(&payload),
                BULK_RAMP,
                active_for,
            )))
        }
        _ => None,
    };

    let rr_fut = run_mux_interactive_stream(&mut rr_write, base, msg_bytes, cadence, run_for);
    let bulk_fut = async {
        if let Some(mut w) = shared_bulk_write {
            tokio::time::sleep(BULK_RAMP).await;
            run_mux_bulk_stream(&mut w, Arc::clone(&payload), active_for).await
        } else {
            0u64
        }
    };
    let split_fut = async {
        if let Some(h) = split_bulk_handle {
            let _ = h.await;
        }
        0u64
    };
    let (sent, _shared_bulk_written, _) = tokio::join!(rr_fut, bulk_fut, split_fut);

    // Allow stragglers to arrive before draining the latency channel.
    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    while let Ok(lat) = latencies.try_recv() {
        samples.push(lat);
    }

    let received = samples.len() as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = (run_for - BULK_RAMP).as_secs_f64();
    let summary = summarize(samples, sent, received, bulk_bytes, bulk_secs);

    print_hol_summary(label, &summary);
    eprintln!(
        "[hol {label}] pair stats = {:?}",
        combined_stats(&interactive_pair)
    );

    interactive_pair.stop();
    if let Some(pair) = bulk_pair_opt {
        pair.stop();
    }
    summary
}

/// Send timestamped `b'L'` frames through a byte-stream write half.
async fn run_mux_interactive_stream(
    write: &mut (impl AsyncWrite + Unpin),
    base: Instant,
    msg_bytes: usize,
    cadence: Duration,
    run_for: Duration,
) -> u64 {
    if write.write_all(b"L").await.is_err() {
        return 0;
    }
    send_timestamped_messages(write, base, msg_bytes, cadence, run_for).await
}

/// Send a deterministic `b'B'` bulk stream through a byte-stream write half.
async fn run_mux_bulk_stream(
    write: &mut (impl AsyncWrite + Unpin),
    payload: Arc<Vec<u8>>,
    active_for: Duration,
) -> u64 {
    if write.write_all(b"B").await.is_err() {
        return 0;
    }
    let start = Instant::now();
    let mut offset = 0usize;
    let mut written = 0u64;
    while start.elapsed() < active_for {
        match write.write(&payload[offset..]).await {
            Ok(0) => break,
            Ok(n) => {
                offset = (offset + n) % payload.len();
                written += n as u64;
            }
            Err(_) => break,
        }
    }
    written
}

/// Pump a plain-RTP bulk flow through a separate NetemPair.
async fn run_rtp_bulk_flow(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    payload: Arc<Vec<u8>>,
    ramp: Duration,
    active_for: Duration,
) -> u64 {
    let Ok(mut writer) = spawn_rtp_bulk_upload(proxy_client_addr, fec).await else {
        return 0;
    };
    tokio::time::sleep(ramp).await;
    let start = Instant::now();
    let mut offset = 0usize;
    let mut written = 0u64;
    while start.elapsed() < active_for {
        match writer.write(&payload[offset..]).await {
            Ok(0) => break,
            Ok(n) => {
                offset = (offset + n) % payload.len();
                written += n as u64;
            }
            Err(_) => break,
        }
    }
    written
}

/// Compute a [`HolSummary`] from raw latency samples.
fn summarize(
    mut samples: Vec<f64>,
    sent: u64,
    received: u64,
    bulk_bytes: u64,
    bulk_secs: f64,
) -> HolSummary {
    let n = samples.len();
    let (episodes, max_run) = {
        let mut episodes = 0u64;
        let mut max_run = 0u64;
        let mut current = 0u64;
        let mut in_run = false;
        for &x in &samples {
            if x > 250.0 {
                if !in_run {
                    episodes += 1;
                    in_run = true;
                }
                current += 1;
            } else {
                in_run = false;
                max_run = max_run.max(current);
                current = 0;
            }
        }
        max_run = max_run.max(current);
        (episodes, max_run)
    };

    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let delivery_pct = if sent == 0 {
        0.0
    } else {
        received as f64 / sent as f64
    };
    let p50 = if n > 0 {
        percentile(&samples, 0.50)
    } else {
        0.0
    };
    let p90 = if n > 0 {
        percentile(&samples, 0.90)
    } else {
        0.0
    };
    let p99 = if n > 0 {
        percentile(&samples, 0.99)
    } else {
        0.0
    };
    let max = samples.last().copied().unwrap_or(0.0);
    let over250 = if n > 0 {
        samples.iter().filter(|&&x| x > 250.0).count() as f64 / n as f64
    } else {
        0.0
    };
    let over1000 = if n > 0 {
        samples.iter().filter(|&&x| x > 1000.0).count() as f64 / n as f64
    } else {
        0.0
    };
    let bulk_mibps = if bulk_secs > 0.0 {
        bulk_bytes as f64 / (1024.0 * 1024.0) / bulk_secs
    } else {
        0.0
    };

    HolSummary {
        sent,
        received,
        delivery_pct,
        p50,
        p90,
        p99,
        max,
        over250_pct: over250,
        over1000_pct: over1000,
        episodes,
        max_run,
        bulk_mibps,
    }
}

fn print_hol_summary(label: &str, s: &HolSummary) {
    eprintln!(
        "[hol {label}] sent={sent} recv={recv} delivery={del:.3} p50={p50:.1} p90={p90:.1} p99={p99:.1} max={max:.1} over250={o25:.3} over1000={o1k:.3} episodes={ep} max_run={mr} bulk={bulk:.3} MiB/s",
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

// ────────────────────────────── config builders ─────────────────────────────

fn rtt100_clean(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: OWD_100MS,
        seed,
        ..NetemConfig::default()
    }
}

fn rtt100_ge5(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: OWD_100MS,
        loss_model: gilbert_elliott_loss(5.0, 3.0),
        seed,
        ..NetemConfig::default()
    }
}

fn rtt100_ge1_loss1(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: OWD_100MS,
        loss_model: gilbert_elliott_loss(1.0, 3.0),
        loss: u32::MAX / 100,
        seed,
        ..NetemConfig::default()
    }
}

fn cap400(seed: u64) -> NetemConfig {
    NetemConfig {
        rate: 400 * 1024 * 8,
        loss: u32::MAX / 100,
        latency: Duration::from_millis(10),
        jitter: Duration::from_millis(2),
        seed,
        ..NetemConfig::default()
    }
}

fn rtt40_ge1(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(20),
        loss_model: gilbert_elliott_loss(1.0, 3.0),
        seed,
        ..NetemConfig::default()
    }
}

fn rtt40_ge1_loss1(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(20),
        loss_model: gilbert_elliott_loss(1.0, 3.0),
        loss: u32::MAX / 100,
        seed,
        ..NetemConfig::default()
    }
}

fn hostile_real_link_seeded(seed: u64) -> NetemConfig {
    let mut c = support::hostile_real_link();
    c.seed = seed;
    c
}

// ══════════════════════════════ reference matrix ══════════════════════════════
// ┌────────────────┬─────────┬───────────┬───────────┬─────────────┬────────────┬──────────┐
// │ link           │  rtt    │ loss      │ jitter    │ rate        │ queue      │ modes    │
// ├────────────────┼─────────┼───────────┼───────────┼─────────────┼────────────┼──────────┤
// │ rtt100 clean   │  100 ms │    0      │    0      │ unbounded   │ unbounded  │ SOL SHR  │
// │ rtt100 GE5     │  100 ms │ 5% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR  │
// │ rtt100 GE5 v2  │  100 ms │ 5% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR  │
// │ rtt100 GE5 v3  │  100 ms │ 5% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR SPL│
// │ rtt100 GE1+loss│  100 ms │ 1% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR SPL│
// │ cap400         │   20 ms │ 1% indep  │   2 ms    │ 400 kbps    │  4096 B    │ SOL SHR  │
// │ cap400 ShrShp  │   20 ms │ 1% indep  │   2 ms    │ 400 kbps shp│     0      │ SSHB RPT  │
// │ rtt40  GE1     │   40 ms │ 1% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR SPL│
// │ rtt40  GE1+loss│   40 ms │ 1% GI bus │    0      │ unbounded   │ unbounded  │ SOL SHR SPL│
// │ FEC cap400     │   20 ms │ 1% indep  │   2 ms    │ 400 kbps    │  4096 B    │ SOL (FEC) │
// │ hostile        │  varied │ burst+indp│  varied   │ varied      │  4096 B    │ SOL SHR SPL│
// └────────────────┴─────────┴───────────┴───────────┴─────────────┴────────────┴──────────┘
// ────────────────────────────── scenario macro ──────────────────────────────

macro_rules! hol_test {
    (
        $name:ident,
        $label:expr,
        $c2s:expr,
        $s2c:expr,
        $bulk:expr,
        $msg_bytes:expr,
        $cadence:expr,
        $run_for:expr,
        $grace:expr,
        $timeout:expr,
        $gates:expr
    ) => {
        #[tokio::test(flavor = "multi_thread")]
        #[ignore]
        async fn $name() {
            let summary = with_timeout(
                $timeout,
                $label,
                run_hol_probe(
                    $label,
                    $c2s,
                    $s2c,
                    HolProbeConfig {
                        bulk: $bulk,
                        fec: false,
                        traffic: TrafficConfig {
                            msg_bytes: $msg_bytes,
                            cadence: $cadence,
                            run_for: $run_for,
                            grace: $grace,
                        },
                    },
                ),
            )
            .await;
            let check: fn(&HolSummary) = $gates;
            check(&summary);
        }
    };
}

// ────────────────────────────── rtt100 clean row ────────────────────────────

hol_test!(
    hol_rtt100_clean_solo,
    "rtt100 clean solo",
    rtt100_clean(11),
    rtt100_clean(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
        assert!(summary.p50 <= 250.0, "p50 {:.1} ms > 250 ms", summary.p50);
        assert!(summary.p99 <= 800.0, "p99 {:.1} ms > 800 ms", summary.p99);
    }
);

hol_test!(
    hol_rtt100_clean_shared,
    "rtt100 clean shared",
    rtt100_clean(21),
    rtt100_clean(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
        assert!(summary.p50 <= 250.0, "p50 {:.1} ms > 250 ms", summary.p50);
        assert!(summary.p99 <= 800.0, "p99 {:.1} ms > 800 ms", summary.p99);
    }
);

hol_test!(
    hol_rtt100_clean_split,
    "rtt100 clean split",
    rtt100_clean(31),
    rtt100_clean(32),
    BulkMode::Split(Box::new((rtt100_clean(33), rtt100_clean(34)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
        assert!(summary.p50 <= 250.0, "p50 {:.1} ms > 250 ms", summary.p50);
        assert!(summary.p99 <= 800.0, "p99 {:.1} ms > 800 ms", summary.p99);
    }
);

// ────────────────────────────── rtt100 GE5 row ──────────────────────────────

hol_test!(
    hol_rtt100_ge5_solo,
    "rtt100 GE5 solo",
    rtt100_ge5(11),
    rtt100_ge5(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_shared,
    "rtt100 GE5 shared",
    rtt100_ge5(21),
    rtt100_ge5(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_split,
    "rtt100 GE5 split",
    rtt100_ge5(31),
    rtt100_ge5(32),
    BulkMode::Split(Box::new((rtt100_ge5(33), rtt100_ge5(34)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

// ─────────────────────── rtt100 GE5 seed-variant rows ────────────────────────

hol_test!(
    hol_rtt100_ge5_v2_solo,
    "rtt100 GE5 v2 solo",
    rtt100_ge5(41),
    rtt100_ge5(42),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_v2_shared,
    "rtt100 GE5 v2 shared",
    rtt100_ge5(51),
    rtt100_ge5(52),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_v3_solo,
    "rtt100 GE5 v3 solo",
    rtt100_ge5(61),
    rtt100_ge5(62),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_v3_shared,
    "rtt100 GE5 v3 shared",
    rtt100_ge5(71),
    rtt100_ge5(72),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge5_v3_split,
    "rtt100 GE5 v3 split",
    rtt100_ge5(81),
    rtt100_ge5(82),
    BulkMode::Split(Box::new((rtt100_ge5(83), rtt100_ge5(84)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

// ────────────────────────────── rtt100 GE1+loss1 row ─────────────────────────

hol_test!(
    hol_rtt100_ge1_loss1_solo,
    "rtt100 GE1+loss1 solo",
    rtt100_ge1_loss1(11),
    rtt100_ge1_loss1(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge1_loss1_shared,
    "rtt100 GE1+loss1 shared",
    rtt100_ge1_loss1(21),
    rtt100_ge1_loss1(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt100_ge1_loss1_split,
    "rtt100 GE1+loss1 split",
    rtt100_ge1_loss1(31),
    rtt100_ge1_loss1(32),
    BulkMode::Split(Box::new((rtt100_ge1_loss1(33), rtt100_ge1_loss1(34)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

// ────────────────────────────── cap400 row ──────────────────────────────────

hol_test!(
    hol_cap400_solo,
    "cap400 solo",
    cap400(11),
    cap400(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
        assert!(summary.p50 <= 100.0, "p50 {:.1} ms > 100 ms", summary.p50);
        assert!(summary.p99 <= 400.0, "p99 {:.1} ms > 400 ms", summary.p99);
    }
);

hol_test!(
    hol_cap400_shared,
    "cap400 shared",
    cap400(21),
    cap400(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
        assert!(summary.p50 <= 500.0, "p50 {:.1} ms > 500 ms", summary.p50);
        assert!(summary.p99 <= 1200.0, "p99 {:.1} ms > 1200 ms", summary.p99);
    }
);

// ────────────────────────────── cap400 shared-bottleneck report-only ──────────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_cap400_loss1_split_shared() {
    let label = "cap400 loss1 split-shared";
    let rate_bps = 400 * 1024 * 8;
    let c2s_shaper = SharedShaper::new(rate_bps, 0);
    let s2c_shaper = SharedShaper::new(rate_bps, 0);
    let mut c2s = cap400(41);
    c2s.rate = 0;
    c2s.loss = u32::MAX / 100;
    c2s.latency = Duration::from_millis(10);
    c2s.jitter = Duration::from_millis(2);
    let mut s2c = cap400(42);
    s2c.rate = 0;
    s2c.loss = u32::MAX / 100;
    s2c.latency = Duration::from_millis(10);
    s2c.jitter = Duration::from_millis(2);

    let _summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe(
            label,
            c2s,
            s2c,
            HolProbeConfig {
                bulk: BulkMode::SplitSharedBneck(c2s_shaper, s2c_shaper),
                fec: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
}

// ────────────────────────────── rtt40 GE1 rows ────────────────────────────────

hol_test!(
    hol_rtt40_ge1_solo,
    "rtt40 GE1 solo",
    rtt40_ge1(11),
    rtt40_ge1(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt40_ge1_loss1_solo,
    "rtt40 GE1+loss1 solo",
    rtt40_ge1_loss1(11),
    rtt40_ge1_loss1(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt40_ge1_shared,
    "rtt40 GE1 shared",
    rtt40_ge1(21),
    rtt40_ge1(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt40_ge1_split,
    "rtt40 GE1 split",
    rtt40_ge1(31),
    rtt40_ge1(32),
    BulkMode::Split(Box::new((rtt40_ge1(33), rtt40_ge1(34)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt40_ge1_loss1_shared,
    "rtt40 GE1+loss1 shared",
    rtt40_ge1_loss1(21),
    rtt40_ge1_loss1(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_rtt40_ge1_loss1_split,
    "rtt40 GE1+loss1 split",
    rtt40_ge1_loss1(31),
    rtt40_ge1_loss1(32),
    BulkMode::Split(Box::new((rtt40_ge1_loss1(33), rtt40_ge1_loss1(34)))),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {:.3} < 0.95",
            summary.delivery_pct
        );
    }
);

// ────────────────────────────── FEC mitigation row ────────────────────────────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_cap400_fec_solo() {
    let label = "cap400 FEC solo";
    let _summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe(
            label,
            cap400(11),
            cap400(12),
            HolProbeConfig {
                bulk: BulkMode::None,
                fec: true,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
}

// ────────────────────────────── hostile rows ──────────────────────────────────

hol_test!(
    hol_hostile_solo,
    "hostile solo",
    hostile_real_link_seeded(11),
    hostile_real_link_seeded(12),
    BulkMode::None,
    DEFAULT_MSG_BYTES,
    Duration::from_millis(200),
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(300),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.80,
            "delivery {:.3} < 0.80",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_hostile_shared,
    "hostile shared",
    hostile_real_link_seeded(21),
    hostile_real_link_seeded(22),
    BulkMode::Shared,
    DEFAULT_MSG_BYTES,
    Duration::from_millis(200),
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(300),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.80,
            "delivery {:.3} < 0.80",
            summary.delivery_pct
        );
    }
);

hol_test!(
    hol_hostile_split,
    "hostile split",
    hostile_real_link_seeded(31),
    hostile_real_link_seeded(32),
    BulkMode::Split(Box::new((
        hostile_real_link_seeded(33),
        hostile_real_link_seeded(34)
    ))),
    DEFAULT_MSG_BYTES,
    Duration::from_millis(200),
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(300),
    |summary: &HolSummary| {
        assert!(
            summary.delivery_pct >= 0.80,
            "delivery {:.3} < 0.80",
            summary.delivery_pct
        );
    }
);

// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery & dual‑lane runners
// ═══════════════════════════════════════════════════════════════════════════════

/// Run an HOL probe on a single frame-delivery RTP connection with a
/// reassembly mux on top.  Interactive and bulk streams share one RTP
/// connection whose frames are preserved end-to-end — every mux frame maps
/// to exactly one RTP frame.  Returns a [`HolSummary`].
async fn run_hol_probe_frame_delivery_shared(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    fec: bool,
    traffic: TrafficConfig,
) -> HolSummary {
    let TrafficConfig {
        msg_bytes,
        cadence,
        run_for,
        grace,
    } = traffic;
    let base = Instant::now();
    let (server_addr, mut latencies, mux_bulk_counter) =
        spawn_mux_frame_delivery_latency_bulk_server(fec, base)
            .await
            .unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (reader, writer) = rtp_frame_delivery_connect(pair.client_addr(), fec).await;
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: true,
    };
    let mut spawner = tokio::task::JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(reader, writer, config, &mut spawner);
    let (mut rr_read, mut rr_write) = opener.open().await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = rr_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    let (mut bulk_read, bulk_write) = opener.open().await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = bulk_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    let active_for = run_for - BULK_RAMP;
    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let rr_fut = run_mux_interactive_stream(&mut rr_write, base, msg_bytes, cadence, run_for);
    let bulk_fut = async {
        let mut w = bulk_write;
        tokio::time::sleep(BULK_RAMP).await;
        run_mux_bulk_stream(&mut w, Arc::clone(&payload), active_for).await
    };
    let (sent, _bulk_written) = tokio::join!(rr_fut, bulk_fut);
    tokio::time::sleep(grace).await;
    drop(spawner);
    let mut samples = Vec::new();
    while let Ok((_tag, lat)) = latencies.try_recv() {
        samples.push(lat);
    }
    let received = samples.len() as u64;
    let bulk_bytes = mux_bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = active_for.as_secs_f64();
    let summary = summarize(samples, sent, received, bulk_bytes, bulk_secs);
    print_hol_summary(label, &summary);
    eprintln!("[hol {}] pair stats = {:?}", label, combined_stats(&pair));
    pair.stop();
    summary
}

/// Run an HOL probe through production `rtp_mux`.  Both lanes use
/// frame‑delivery RTP; the connector routes the interactive stream through
/// the interactive lane and the bulk stream through the bulk lane.
async fn run_hol_probe_rtp_mux(
    label: &str,
    int_c2s: NetemConfig,
    int_s2c: NetemConfig,
    bulk_c2s: NetemConfig,
    bulk_s2c: NetemConfig,
    traffic: TrafficConfig,
) -> HolSummary {
    let TrafficConfig {
        msg_bytes,
        cadence,
        run_for,
        grace,
    } = traffic;
    let base = Instant::now();
    let (int_addr, bulk_addr, mut latencies, bulk_counter) =
        spawn_rtp_mux_latency_bulk_server(false, base)
            .await
            .unwrap();
    let int_pair = NetemPair::spawn(int_addr, int_c2s, int_s2c).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_addr, bulk_c2s, bulk_s2c).unwrap();
    let connector = Arc::new(rtp_mux_connector(bulk_pair.client_addr(), false));
    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let bulk_handle = {
        let connector = Arc::clone(&connector);
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        let int_proxy_addr = int_pair.client_addr();
        tokio::spawn(async move {
            let mut stream = match connector
                .connect_stream_with_lane(int_proxy_addr, mux::LaneClass::Bulk)
                .await
            {
                Ok(stream) => stream,
                Err(_) => return,
            };
            if stream.write_all(b"B").await.is_err() {
                return;
            }
            let mut offset = 0usize;
            while !stop.load(Ordering::Relaxed) {
                match stream.write(&payload[offset..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => offset = (offset + n) % payload.len(),
                }
            }
            let _ = stream.shutdown().await;
        })
    };
    tokio::time::sleep(BULK_RAMP).await;
    let mut stream = connector
        .connect_stream_with_lane(int_pair.client_addr(), mux::LaneClass::Interactive)
        .await
        .unwrap();
    let sent = run_mux_interactive_stream(&mut stream, base, msg_bytes, cadence, run_for).await;
    let _ = stream.shutdown().await;
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    while let Ok((_tag, latency)) = latencies.try_recv() {
        samples.push(latency);
    }
    let received = samples.len() as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = (run_for - BULK_RAMP).as_secs_f64();
    let summary = summarize(samples, sent, received, bulk_bytes, bulk_secs);
    print_hol_summary(label, &summary);
    eprintln!(
        "[hol {label}] int pair stats = {:?}  bulk pair stats = {:?}",
        combined_stats(&int_pair),
        combined_stats(&bulk_pair)
    );
    int_pair.stop();
    bulk_pair.stop();
    summary
}

/// Run an HOL probe on a dual-lane setup: two independent RTP connections
/// (one per lane), each with its own [`NetemPair`].  `interactive_frame` /
/// `bulk_frame` control per-lane frame‑delivery.
///
/// The interactive stream rides the interactive lane; the bulk stream rides
/// the bulk lane.  When both lanes request frame‑delivery the probe routes
/// through the production [`rtp_mux`] composition via
/// [`run_hol_probe_rtp_mux`]; asymmetric lane‑mode diagnostics continue
/// through the lower‑level dual‑mux helpers.
async fn run_hol_probe_dual_lane(
    label: &str,
    int_c2s: NetemConfig,
    int_s2c: NetemConfig,
    bulk_c2s: NetemConfig,
    bulk_s2c: NetemConfig,
    config: DualLaneProbeConfig,
) -> HolSummary {
    if config.interactive_frame && config.bulk_frame {
        return run_hol_probe_rtp_mux(label, int_c2s, int_s2c, bulk_c2s, bulk_s2c, config.traffic)
            .await;
    }
    let DualLaneProbeConfig {
        interactive_frame,
        bulk_frame,
        traffic,
    } = config;
    let TrafficConfig {
        msg_bytes,
        cadence,
        run_for,
        grace,
    } = traffic;
    let base = Instant::now();
    let (int_addr, bulk_addr, mut latencies, bulk_counter) =
        spawn_dual_mux_latency_bulk_server_two_listeners(
            false,
            base,
            interactive_frame,
            bulk_frame,
        )
        .await
        .unwrap();
    let int_pair = NetemPair::spawn(int_addr, int_c2s, int_s2c).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_addr, bulk_c2s, bulk_s2c).unwrap();
    let (opener, _accepter, _spawner) = dual_mux_client_connect_with_lane_modes(
        int_pair.client_addr(),
        bulk_pair.client_addr(),
        false,
        interactive_frame,
        bulk_frame,
    )
    .await
    .unwrap();
    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(mux::LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let _ = w.write_all(b"B").await;
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
    let (mut rr_read, mut rr_write) = opener.open(mux::LaneClass::Interactive).await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = rr_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    let sent = run_mux_interactive_stream(&mut rr_write, base, msg_bytes, cadence, run_for).await;
    let _ = rr_write.shutdown();
    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;
    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    while let Ok((_tag, lat)) = latencies.try_recv() {
        samples.push(lat);
    }
    let received = samples.len() as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = (run_for - BULK_RAMP).as_secs_f64();
    let summary = summarize(samples, sent, received, bulk_bytes, bulk_secs);
    print_hol_summary(label, &summary);
    eprintln!(
        "[hol {label}] int pair stats = {:?}  bulk pair stats = {:?}",
        combined_stats(&int_pair),
        combined_stats(&bulk_pair)
    );
    int_pair.stop();
    bulk_pair.stop();
    summary
}

/// Dual‑lane HOL probe with TWO interactive streams on the interactive
/// lane (tagged `b'A'` / `b'B'`) plus one bulk stream on the bulk lane.
/// Returns `(summary_a, summary_b, ` combined summary across both, `bulk_mibps)`.
async fn run_hol_probe_dual_lane_two_interactive(
    label: &str,
    int_c2s: NetemConfig,
    int_s2c: NetemConfig,
    bulk_c2s: NetemConfig,
    bulk_s2c: NetemConfig,
    config: DualLaneProbeConfig,
) -> (HolSummary, HolSummary, HolSummary, f64) {
    let DualLaneProbeConfig {
        interactive_frame,
        bulk_frame,
        traffic:
            TrafficConfig {
                msg_bytes,
                cadence,
                run_for,
                grace,
            },
    } = config;
    let base = Instant::now();
    let (int_addr, bulk_addr, mut latencies_all, bulk_counter) =
        spawn_dual_mux_latency_bulk_server_two_listeners(
            false,
            base,
            interactive_frame,
            bulk_frame,
        )
        .await
        .unwrap();

    let int_pair = NetemPair::spawn(int_addr, int_c2s, int_s2c).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_addr, bulk_c2s, bulk_s2c).unwrap();

    let (opener, _accepter, _spawner) = dual_mux_client_connect_with_lane_modes(
        int_pair.client_addr(),
        bulk_pair.client_addr(),
        false,
        interactive_frame,
        bulk_frame,
    )
    .await
    .unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(mux::LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let _ = w.write_all(b"B").await;
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

    let (mut read_a, mut write_a) = opener.open_auto();
    let (mut read_b, mut write_b) = opener.open_auto();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = read_a.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = read_b.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });

    tokio::time::sleep(BULK_RAMP).await;

    if write_a.write_all(b"A").await.is_err() {
        let _ = write_a.shutdown();
        drop(write_b);
        bulk_stop.store(true, Ordering::Relaxed);
        let _ = bulk_handle.await;
        return (
            HolSummary::default(),
            HolSummary::default(),
            HolSummary::default(),
            0.0,
        );
    }
    if write_b.write_all(b"B").await.is_err() {
        let _ = write_b.shutdown();
        let _ = write_a.shutdown();
        bulk_stop.store(true, Ordering::Relaxed);
        let _ = bulk_handle.await;
        return (
            HolSummary::default(),
            HolSummary::default(),
            HolSummary::default(),
            0.0,
        );
    }

    let fut_a = send_timestamped_messages(&mut write_a, base, msg_bytes, cadence, run_for);
    let fut_b = send_timestamped_messages(&mut write_b, base, msg_bytes, cadence, run_for);
    let (sent_a, sent_b) = tokio::join!(fut_a, fut_b);
    let _ = write_a.shutdown();
    let _ = write_b.shutdown();

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    let mut samples_a = Vec::new();
    let mut samples_b = Vec::new();
    while let Ok((tag, lat)) = latencies_all.try_recv() {
        samples.push(lat);
        if tag == b'A' {
            samples_a.push(lat);
        } else if tag == b'B' {
            samples_b.push(lat);
        }
    }

    let active_for = run_for - BULK_RAMP;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = active_for.as_secs_f64();
    let bulk_mibps = if bulk_secs > 0.0 {
        bulk_bytes as f64 / (1024.0 * 1024.0) / bulk_secs
    } else {
        0.0
    };

    let n_all = samples.len() as u64;
    let combined = summarize(samples, sent_a + sent_b, n_all, bulk_bytes, bulk_secs);
    let summary_a = summarize(samples_a.clone(), sent_a, samples_a.len() as u64, 0, 0.0);
    let summary_b = summarize(samples_b.clone(), sent_b, samples_b.len() as u64, 0, 0.0);

    eprintln!(
        "[hol {label} A] p50={p50_a:.1} p99={p99_a:.1} max={max_a:.1}",
        p50_a = summary_a.p50,
        p99_a = summary_a.p99,
        max_a = summary_a.max,
    );
    eprintln!(
        "[hol {label} B] p50={p50_b:.1} p99={p99_b:.1} max={max_b:.1}",
        p50_b = summary_b.p50,
        p99_b = summary_b.p99,
        max_b = summary_b.max,
    );
    print_hol_summary(&format!("{label}_combined"), &combined);
    eprintln!(
        "[hol {label}] int pair stats = {:?}  bulk pair stats = {:?}",
        combined_stats(&int_pair),
        combined_stats(&bulk_pair),
    );

    int_pair.stop();
    bulk_pair.stop();
    (summary_a, summary_b, combined, bulk_mibps)
}

/// Two‑interactive baseline on a single frame‑delivery RTP connection:
/// two tagged streams (b'A'/b'B'), no bulk contender.
async fn run_frame_delivery_two_interactive(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    config: FrameDeliveryProbeConfig,
) -> (HolSummary, HolSummary, HolSummary) {
    let FrameDeliveryProbeConfig {
        fec,
        traffic:
            TrafficConfig {
                msg_bytes,
                cadence,
                run_for,
                grace,
            },
    } = config;
    let base = Instant::now();
    let (server_addr, mut latencies, _bulk_counter) =
        spawn_mux_frame_delivery_latency_bulk_server(fec, base)
            .await
            .unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (reader, writer) = rtp_frame_delivery_connect(pair.client_addr(), fec).await;
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: true,
    };
    let mut spawner = tokio::task::JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(reader, writer, config, &mut spawner);
    let (mut read_a, mut write_a) = opener.open().await.unwrap();
    let (mut read_b, mut write_b) = opener.open().await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = read_a.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = read_b.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    let _ = write_a.write_all(b"A").await;
    let _ = write_b.write_all(b"L").await;
    let fut_a = send_timestamped_messages(&mut write_a, base, msg_bytes, cadence, run_for);
    let fut_b = send_timestamped_messages(&mut write_b, base, msg_bytes, cadence, run_for);
    let (sent_a, sent_b) = tokio::join!(fut_a, fut_b);
    let _ = write_a.shutdown();
    let _ = write_b.shutdown();
    tokio::time::sleep(grace).await;
    drop(spawner);
    let mut samples = Vec::new();
    let mut samples_a = Vec::new();
    let mut samples_b = Vec::new();
    while let Ok((tag, lat)) = latencies.try_recv() {
        samples.push(lat);
        if tag == b'A' {
            samples_a.push(lat);
        } else if tag == b'L' {
            samples_b.push(lat);
        }
    }
    let combined = summarize(
        samples.clone(),
        sent_a + sent_b,
        samples.len() as u64,
        0,
        0.0,
    );
    let summary_a = summarize(samples_a.clone(), sent_a, samples_a.len() as u64, 0, 0.0);
    let summary_b = summarize(samples_b.clone(), sent_b, samples_b.len() as u64, 0, 0.0);
    eprintln!(
        "[hol {} A] p50={:.1} p99={:.1} max={:.1}",
        label, summary_a.p50, summary_a.p99, summary_a.max
    );
    eprintln!(
        "[hol {} B] p50={:.1} p99={:.1} max={:.1}",
        label, summary_b.p50, summary_b.p99, summary_b.max
    );
    print_hol_summary(&format!("{}_combined", label), &combined);
    pair.stop();
    (summary_a, summary_b, combined)
}

// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery & dual‑lane scenarios (all #[ignore])
// ═══════════════════════════════════════════════════════════════════════════════

// ───── single‑connection frame‑delivery ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_shared_frame_delivery() {
    let label = "rtt100 GE5 shared frame-delivery";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_frame_delivery_shared(
            label,
            rtt100_ge5(21),
            rtt100_ge5(22),
            false,
            TrafficConfig {
                msg_bytes: DEFAULT_MSG_BYTES,
                cadence: DEFAULT_CADENCE,
                run_for: DEFAULT_RUN_FOR,
                grace: DEFAULT_GRACE,
            },
        ),
    )
    .await;
    assert!(
        summary.delivery_pct >= 0.95,
        "delivery {:.3} < 0.95",
        summary.delivery_pct
    );
    assert!(
        summary.p50 <= 100.0,
        "frame-delivery p50 {:.1} ms > 100 ms",
        summary.p50
    );
    assert!(
        summary.p99 <= 400.0,
        "frame-delivery p99 {:.1} ms > 400 ms",
        summary.p99
    );
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_two_interactive_frame_delivery() {
    let label = "rtt100 GE5 two-interactive frame-delivery";
    let (summary_a, summary_b, combined) = with_timeout(
        Duration::from_secs(120),
        label,
        run_frame_delivery_two_interactive(
            label,
            rtt100_ge5(31),
            rtt100_ge5(32),
            FrameDeliveryProbeConfig {
                fec: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(combined.delivery_pct >= 0.90, "combined delivery too low");
    let solo_ref = 50.0;
    assert!(
        summary_a.p50 <= solo_ref * 2.5,
        "stream A p50 {:.1} > {:.0}",
        summary_a.p50,
        solo_ref * 2.5,
    );
    assert!(
        summary_b.p50 <= solo_ref * 2.5,
        "stream B p50 {:.1} > {:.0}",
        summary_b.p50,
        solo_ref * 2.5,
    );
}

// ───── diagnostics: frame‑delivery shared on various link profiles ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_clean_shared_frame_delivery_diag() {
    let label = "rtt100 clean shared frame-delivery diag";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_frame_delivery_shared(
            label,
            rtt100_clean(41),
            rtt100_clean(42),
            false,
            TrafficConfig {
                msg_bytes: DEFAULT_MSG_BYTES,
                cadence: DEFAULT_CADENCE,
                run_for: DEFAULT_RUN_FOR,
                grace: DEFAULT_GRACE,
            },
        ),
    )
    .await;
    assert!(summary.delivery_pct > 0.0, "no delivery");
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge1_shared_frame_delivery_diag() {
    let label = "rtt100 GE1 shared frame-delivery diag";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_frame_delivery_shared(
            label,
            rtt100_ge1_loss1(51),
            rtt100_ge1_loss1(52),
            false,
            TrafficConfig {
                msg_bytes: DEFAULT_MSG_BYTES,
                cadence: DEFAULT_CADENCE,
                run_for: DEFAULT_RUN_FOR,
                grace: DEFAULT_GRACE,
            },
        ),
    )
    .await;
    assert!(summary.delivery_pct > 0.0, "no delivery");
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_hostile_shared_frame_delivery_diag() {
    let label = "hostile shared frame-delivery diag";
    let summary = with_timeout(
        Duration::from_secs(300),
        label,
        run_hol_probe_frame_delivery_shared(
            label,
            hostile_real_link_seeded(61),
            hostile_real_link_seeded(62),
            false,
            TrafficConfig {
                msg_bytes: DEFAULT_MSG_BYTES,
                cadence: Duration::from_millis(200),
                run_for: DEFAULT_RUN_FOR,
                grace: DEFAULT_GRACE,
            },
        ),
    )
    .await;
    assert!(summary.delivery_pct > 0.0, "no delivery");
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_cap400_shared_frame_delivery_diag() {
    let label = "cap400 shared frame-delivery diag";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_frame_delivery_shared(
            label,
            cap400(71),
            cap400(72),
            false,
            TrafficConfig {
                msg_bytes: DEFAULT_MSG_BYTES,
                cadence: DEFAULT_CADENCE,
                run_for: DEFAULT_RUN_FOR,
                grace: DEFAULT_GRACE,
            },
        ),
    )
    .await;
    assert!(summary.delivery_pct > 0.0, "no delivery");
}

// ───── dual‑lane: stock lanes ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_shared_dual_lane() {
    let label = "rtt100 GE5 shared dual-lane";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane(
            label,
            rtt100_ge5(91),
            rtt100_ge5(92),
            rtt100_ge5(93),
            rtt100_ge5(94),
            DualLaneProbeConfig {
                interactive_frame: false,
                bulk_frame: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(
        summary.delivery_pct >= 0.95,
        "delivery {:.3} < 0.95",
        summary.delivery_pct
    );
}

// ───── dual‑lane: both lanes frame‑delivery ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_shared_dual_lane_frame_delivery() {
    let label = "rtt100 GE5 shared dual-lane frame-delivery";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane(
            label,
            rtt100_ge5(101),
            rtt100_ge5(102),
            rtt100_ge5(103),
            rtt100_ge5(104),
            DualLaneProbeConfig {
                interactive_frame: true,
                bulk_frame: true,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(
        summary.delivery_pct >= 0.999,
        "delivery {:.3} < 0.999",
        summary.delivery_pct
    );
}

// ───── asymmetric: interactive frame, bulk stock ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_shared_dual_lane_asym_frame_diag() {
    let label = "rtt100 GE5 shared dual-lane asym frame diag";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane(
            label,
            rtt100_ge5(111),
            rtt100_ge5(112),
            rtt100_ge5(113),
            rtt100_ge5(114),
            DualLaneProbeConfig {
                interactive_frame: true,
                bulk_frame: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(summary.delivery_pct > 0.0, "no delivery");
}

// ───── two‑interactive intra‑lane isolation ─────

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_dual_lane_two_interactive_stock_diag() {
    let label = "rtt100 GE5 dual-lane two-interactive stock";
    let (summary_a, summary_b, _combined, _bulk) = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane_two_interactive(
            label,
            rtt100_ge5(121),
            rtt100_ge5(122),
            rtt100_ge5(123),
            rtt100_ge5(124),
            DualLaneProbeConfig {
                interactive_frame: false,
                bulk_frame: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(summary_a.delivery_pct > 0.0, "stream A no delivery");
    assert!(summary_b.delivery_pct > 0.0, "stream B no delivery");
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn hol_rtt100_ge5_dual_lane_two_interactive_frame_diag() {
    let label = "rtt100 GE5 dual-lane two-interactive frame";
    let (summary_a, summary_b, _combined, _bulk) = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane_two_interactive(
            label,
            rtt100_ge5(131),
            rtt100_ge5(132),
            rtt100_ge5(133),
            rtt100_ge5(134),
            DualLaneProbeConfig {
                interactive_frame: true,
                bulk_frame: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(summary_a.delivery_pct > 0.0, "stream A no delivery");
    assert!(summary_b.delivery_pct > 0.0, "stream B no delivery");
}

// ───── separate‑listener: asymmetric frame delivery + teardown ─────

/// Run an HOL probe on a dual-lane setup with SEPARATE interactive and bulk
/// listeners.  Each lane's RTP frame‑delivery mode is fixed at accept time
/// on its dedicated listener, so the interactive listener can use
/// frame‑delivery while the bulk listener uses stock byte‑stream — the
/// server never has to guess the lane class before the mux lane‑hello.
async fn run_hol_probe_dual_lane_separate_listeners(
    label: &str,
    int_c2s: NetemConfig,
    int_s2c: NetemConfig,
    bulk_c2s: NetemConfig,
    bulk_s2c: NetemConfig,
    config: DualLaneProbeConfig,
) -> HolSummary {
    let DualLaneProbeConfig {
        interactive_frame,
        bulk_frame,
        traffic:
            TrafficConfig {
                msg_bytes,
                cadence,
                run_for,
                grace,
            },
    } = config;
    let base = Instant::now();
    let (int_addr, bulk_addr, mut latencies, bulk_counter) =
        spawn_dual_mux_latency_bulk_server_two_listeners(
            false,
            base,
            interactive_frame,
            bulk_frame,
        )
        .await
        .unwrap();

    let int_pair = NetemPair::spawn(int_addr, int_c2s, int_s2c).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_addr, bulk_c2s, bulk_s2c).unwrap();

    let (opener, _accepter, _spawner) = dual_mux_client_connect_with_lane_modes(
        int_pair.client_addr(),
        bulk_pair.client_addr(),
        false,
        interactive_frame,
        bulk_frame,
    )
    .await
    .unwrap();

    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));
    let bulk_stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let bulk_opener = opener.clone();
    let bulk_handle = {
        let payload = Arc::clone(&payload);
        let stop = Arc::clone(&bulk_stop);
        tokio::spawn(async move {
            let (_, mut w) = match bulk_opener.open(mux::LaneClass::Bulk).await {
                Ok(v) => v,
                Err(_) => return,
            };
            let _ = w.write_all(b"B").await;
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

    let (mut rr_read, mut rr_write) = opener.open(mux::LaneClass::Interactive).await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = rr_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });
    let sent = run_mux_interactive_stream(&mut rr_write, base, msg_bytes, cadence, run_for).await;
    let _ = rr_write.shutdown();

    bulk_stop.store(true, Ordering::Relaxed);
    let _ = bulk_handle.await;

    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    while let Ok((_tag, lat)) = latencies.try_recv() {
        samples.push(lat);
    }

    let received = samples.len() as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = (run_for - BULK_RAMP).as_secs_f64();
    let summary = summarize(samples, sent, received, bulk_bytes, bulk_secs);

    print_hol_summary(label, &summary);
    eprintln!(
        "[hol {label}] int pair stats = {:?}  bulk pair stats = {:?}",
        combined_stats(&int_pair),
        combined_stats(&bulk_pair),
    );
    int_pair.stop();
    bulk_pair.stop();
    summary
}

/// Asymmetric frame‑delivery dual‑lane test: interactive lane uses
/// frame‑delivery (on its own dedicated listener), bulk lane uses stock
/// byte‑stream (on its own dedicated listener).  Under 5 % Gilbert‑Elliott
/// loss the probe must deliver messages and tear down cleanly without
/// wedging the runtime.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn dual_lane_asym_frame_delivers_and_tears_down() {
    let label = "asym separate-listener frame teardown";
    let summary = with_timeout(
        Duration::from_secs(120),
        label,
        run_hol_probe_dual_lane_separate_listeners(
            label,
            rtt100_ge5(141),
            rtt100_ge5(142),
            rtt100_ge5(143),
            rtt100_ge5(144),
            DualLaneProbeConfig {
                interactive_frame: true,
                bulk_frame: false,
                traffic: TrafficConfig {
                    msg_bytes: DEFAULT_MSG_BYTES,
                    cadence: DEFAULT_CADENCE,
                    run_for: DEFAULT_RUN_FOR,
                    grace: DEFAULT_GRACE,
                },
            },
        ),
    )
    .await;
    assert!(
        summary.delivery_pct >= 0.95,
        "delivery {:.3} < 0.95 — adapter may be wedged",
        summary.delivery_pct
    );
}
