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

use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair, SharedShaper};
use support::{
    combined_stats, cyclic_payload, gilbert_elliott_loss, mux_client_connect, percentile,
    print_perf, rtp_connect, send_timestamped_messages, spawn_mux_latency_bulk_server,
    spawn_rtp_bulk_upload, spawn_rtp_byte_sink_server, with_timeout,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// One-way delay for the rtt100 rows.
const OWD_100MS: Duration = Duration::from_millis(50);

/// Default interactive message size.
const DEFAULT_MSG_BYTES: usize = 256;
/// Default cadence between interactive messages.
const DEFAULT_CADENCE: Duration = Duration::from_millis(25);
/// Default interactive run time.
const DEFAULT_RUN_FOR: Duration = Duration::from_secs(15);
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
    Split(NetemConfig, NetemConfig),
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
pub async fn run_hol_probe(
    label: &str,
    c2s: NetemConfig,
    s2c: NetemConfig,
    bulk: BulkMode,
    fec: bool,
    msg_bytes: usize,
    cadence: Duration,
    run_for: Duration,
    grace: Duration,
) -> HolSummary {
    let base = Instant::now();
    let (server_addr, mut latencies, mux_bulk_counter) =
        spawn_mux_latency_bulk_server(fec, base).await.unwrap();

    // Set up the interactive NetemPair and, for split modes, a bulk pair.
    let (interactive_pair, bulk_pair_opt, bulk_counter) = match &bulk {
        BulkMode::None | BulkMode::Shared => {
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
            (pair, None, Arc::clone(&mux_bulk_counter))
        }
        BulkMode::Split(c2s2, s2c2) => {
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
            let (sink_addr, counter) = spawn_rtp_byte_sink_server(fec).await.unwrap();
            let bulk_pair = NetemPair::spawn(sink_addr, c2s2.clone(), s2c2.clone()).unwrap();
            (pair, Some(bulk_pair), counter)
        }
        BulkMode::SplitSharedBneck(shaper_c2s, shaper_s2c) => {
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
                c2s,
                s2c,
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
        loop {
            match rr_read.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    });

    // For Shared mode, open a second mux stream for the bulk flow.
    let mut shared_bulk_write = None;
    if matches!(bulk, BulkMode::Shared) {
        let (bulk_read, bulk_write) = opener.open().await.unwrap();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 8 * 1024];
            loop {
                match bulk_read.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
        });
        shared_bulk_write = Some(bulk_write);
    }

    let active_for = run_for - BULK_RAMP;
    let payload = Arc::new(cyclic_payload(64 * 1024 * 1024));

    // Run the interactive sender and the bulk sender concurrently.
    let rr_fut = run_mux_interactive_stream(&mut rr_write, base, msg_bytes, cadence, run_for);
    let bulk_fut = async {
        if let Some(mut w) = shared_bulk_write {
            tokio::time::sleep(BULK_RAMP).await;
            run_mux_bulk_stream(&mut w, Arc::clone(&payload), active_for).await
        } else {
            0u64
        }
    };
    let (sent, _shared_bulk_written) = tokio::join!(rr_fut, bulk_fut);

    // For split modes, the bulk flow runs independently on a separate pair.
    let split_bulk_handle = match &bulk {
        BulkMode::Split(_, _) | BulkMode::SplitSharedBneck(_, _) => {
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
    if let Some(h) = split_bulk_handle {
        let _ = h.await;
    }

    // Allow stragglers to arrive before draining the latency channel.
    tokio::time::sleep(grace).await;
    let mut samples = Vec::new();
    while let Ok(lat) = latencies.try_recv() {
        samples.push(lat);
    }

    let received = samples.len() as u64;
    let bulk_bytes = bulk_counter.load(Ordering::Relaxed);
    let bulk_secs = active_for.as_secs_f64();
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

/// Send timestamped `b'L'` frames through a mux stream write half.
async fn run_mux_interactive_stream(
    write: &mut mux::StreamWriter,
    base: Instant,
    msg_bytes: usize,
    cadence: Duration,
    run_for: Duration,
) -> u64 {
    if write.write_all(&[b'L']).await.is_err() {
        return 0;
    }
    send_timestamped_messages(write, base, msg_bytes, cadence, run_for).await
}

/// Send a deterministic `b'B'` bulk stream through a mux stream write half.
async fn run_mux_bulk_stream(
    write: &mut mux::StreamWriter,
    payload: Arc<Vec<u8>>,
    active_for: Duration,
) -> u64 {
    if write.write_all(&[b'B']).await.is_err() {
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
        loss_model: gilbert_elliott_loss(5.0, 8.0),
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
        $gates:block
    ) => {
        #[tokio::test(flavor = "multi_thread")]
        #[ignore]
        async fn $name() {
            let summary = with_timeout(
                $timeout,
                $label,
                run_hol_probe(
                    $label, $c2s, $s2c, $bulk, false, $msg_bytes, $cadence, $run_for, $grace,
                ),
            )
            .await;
            $gates
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
        assert!(summary.p50 <= 250.0, "p50 {summary.p50:.1} ms > 250 ms");
        assert!(summary.p99 <= 800.0, "p99 {summary.p99:.1} ms > 800 ms");
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
        assert!(summary.p50 <= 250.0, "p50 {summary.p50:.1} ms > 250 ms");
        assert!(summary.p99 <= 800.0, "p99 {summary.p99:.1} ms > 800 ms");
    }
);

hol_test!(
    hol_rtt100_clean_split,
    "rtt100 clean split",
    rtt100_clean(31),
    rtt100_clean(32),
    BulkMode::Split(rtt100_clean(33), rtt100_clean(34)),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
        assert!(summary.p50 <= 250.0, "p50 {summary.p50:.1} ms > 250 ms");
        assert!(summary.p99 <= 800.0, "p99 {summary.p99:.1} ms > 800 ms");
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
    }
);

hol_test!(
    hol_rtt100_ge5_split,
    "rtt100 GE5 split",
    rtt100_ge5(31),
    rtt100_ge5(32),
    BulkMode::Split(rtt100_ge5(33), rtt100_ge5(34)),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
    }
);

hol_test!(
    hol_rtt100_ge1_loss1_split,
    "rtt100 GE1+loss1 split",
    rtt100_ge1_loss1(31),
    rtt100_ge1_loss1(32),
    BulkMode::Split(rtt100_ge1_loss1(33), rtt100_ge1_loss1(34)),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
        assert!(summary.p50 <= 100.0, "p50 {summary.p50:.1} ms > 100 ms");
        assert!(summary.p99 <= 400.0, "p99 {summary.p99:.1} ms > 400 ms");
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
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
        assert!(summary.p50 <= 500.0, "p50 {summary.p50:.1} ms > 500 ms");
        assert!(summary.p99 <= 1200.0, "p99 {summary.p99:.1} ms > 1200 ms");
    }
);

hol_test!(
    hol_cap400_split,
    "cap400 split",
    cap400(31),
    cap400(32),
    BulkMode::Split(cap400(33), cap400(34)),
    DEFAULT_MSG_BYTES,
    DEFAULT_CADENCE,
    DEFAULT_RUN_FOR,
    DEFAULT_GRACE,
    Duration::from_secs(120),
    {
        assert!(
            summary.delivery_pct >= 0.95,
            "delivery {summary.delivery_pct:.3} < 0.95"
        );
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
            BulkMode::SplitSharedBneck(c2s_shaper, s2c_shaper),
            false,
            DEFAULT_MSG_BYTES,
            DEFAULT_CADENCE,
            DEFAULT_RUN_FOR,
            DEFAULT_GRACE,
        ),
    )
    .await;
}
