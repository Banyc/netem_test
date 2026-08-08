//! RTP regression scenarios for burst-loss goodput and sparse-message tail
//! latency.
//!
//! These tests are `#[ignore]`-d by default so they do not slow normal builds.
//! Run them with:
//!
//! ```sh
//! cargo test --release --test rtp_burst_loss -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::{
    mux_client_connect_via, send_timestamped_messages, spawn_mux_msg_latency_sink_via,
};
use support::payload::{cyclic_payload, with_timeout};
use support::presets::{burst_loss_link, random_loss_link};
use support::rtp::{
    rtp_connect_with_mss_via, spawn_rtp_bulk_upload_with_mss_via,
    spawn_rtp_byte_sink_server_with_mss_via,
};
use support::stats::{percentile, print_perf};
use support::submit_test_task;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// OWD used by both burst-loss tests so the comparison is apples-to-apples.
const OWD: Duration = Duration::from_millis(50);
/// RTP MSS for the burst-loss probes. Use the default loopback MSS so the
/// codec/FEC overhead leaves a reasonable user payload per packet.
const MSS: usize = rtp::udp::NO_FEC_MSS;

/// Number of seconds the bulk-goodput probe keeps the sender busy.
const BULK_WINDOW_S: u64 = 12;
const BULK_REPETITIONS: usize = 3;
/// Minimum anti-stall floor: burst loss must outperform independent random loss
/// of the same average rate.
const MIN_BURST_VS_RANDOM_RATIO: f64 = 0.90;
/// Absolute anti-stall floor for the random-loss baseline. The ratio assertion
/// above passes even when both 12 s runs stall near zero, so the random-loss
/// baseline must also make real progress.
const RANDOM_GOODPUT_STALL_FLOOR_MIB_S: f64 = 1.0;

/// Number of messages sent by the sparse-message tail-latency probe.
const SPARSE_MSG_COUNT: u64 = 200;
/// Interval between sparse messages.
const SPARSE_MSG_INTERVAL: Duration = Duration::from_millis(300);
/// Message size used by the sparse-message probe.
const SPARSE_MSG_BYTES: usize = 64;
/// Floor: at least 98% of sparse messages must be delivered.
const SPARSE_DELIVERY_FLOOR_PCT: f64 = 0.98;
/// Floor: p99 one-way latency under burst loss must stay below ~2.5 s.
const SPARSE_P99_LATENCY_MS: f64 = 2500.0;
/// Floor: p50 one-way latency under burst loss must stay below ~300 ms. The
/// median is not RTO-quantized; stock behaviour is ~55 ms at this test's 100 ms
/// RTT (OWD = 50 ms), so this leaves a generous allowance.
const SPARSE_P50_LATENCY_MS: f64 = 300.0;

/// Burst-loss profile: long-term 5% loss, mean burst length 8.
const BURST_LOSS_PCT: f64 = 5.0;
const BURST_LOSS_MEAN_LEN: f64 = 8.0;

/// Random-loss profile: same average 5% loss.
const RANDOM_LOSS_PCT: f64 = 5.0;

/// Bulk goodput through a burst-loss link should not collapse below the
/// equivalent independent random loss link.
///
/// The burst link uses a Gilbert-Elliott model with 5% long-term loss and an
/// average burst length of 8. The random link uses the same average rate. We
/// run a 12-second byte-sink upload through each and assert that the ratio is
/// at least `MIN_BURST_VS_RANDOM_RATIO` (i.e. burst loss is not catastrophically
/// worse than random loss).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "burst-loss goodput/tail-latency regression; slow; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_bulk_goodput_burst_loss_retains_ninety_percent_of_random() {
    let window = Duration::from_secs(BULK_WINDOW_S);
    let data: &'static [u8] = Box::leak(cyclic_payload(256 * 1024 * 1024).into_boxed_slice());

    let mut ratios = Vec::with_capacity(BULK_REPETITIONS);
    for rep in 0..BULK_REPETITIONS {
        let seed_offset = rep * 100;
        let (burst_delivered, random_delivered, burst_pair, random_pair) = if rep.is_multiple_of(2)
        {
            let burst = run_rtp_sink_upload(
                burst_loss_link(
                    BURST_LOSS_PCT,
                    BURST_LOSS_MEAN_LEN,
                    OWD,
                    (11 + seed_offset) as u64,
                ),
                burst_loss_link(
                    BURST_LOSS_PCT,
                    BURST_LOSS_MEAN_LEN,
                    OWD,
                    (22 + seed_offset) as u64,
                ),
                data,
                window,
            )
            .await;
            let random = run_rtp_sink_upload(
                random_loss_link(RANDOM_LOSS_PCT, OWD, (33 + seed_offset) as u64),
                random_loss_link(RANDOM_LOSS_PCT, OWD, (44 + seed_offset) as u64),
                data,
                window,
            )
            .await;
            (burst.1, random.1, burst.0, random.0)
        } else {
            let random = run_rtp_sink_upload(
                random_loss_link(RANDOM_LOSS_PCT, OWD, (33 + seed_offset) as u64),
                random_loss_link(RANDOM_LOSS_PCT, OWD, (44 + seed_offset) as u64),
                data,
                window,
            )
            .await;
            let burst = run_rtp_sink_upload(
                burst_loss_link(
                    BURST_LOSS_PCT,
                    BURST_LOSS_MEAN_LEN,
                    OWD,
                    (11 + seed_offset) as u64,
                ),
                burst_loss_link(
                    BURST_LOSS_PCT,
                    BURST_LOSS_MEAN_LEN,
                    OWD,
                    (22 + seed_offset) as u64,
                ),
                data,
                window,
            )
            .await;
            (burst.1, random.1, burst.0, random.0)
        };

        let burst_goodput = burst_delivered as f64 / (1024.0 * 1024.0) / BULK_WINDOW_S as f64;
        let random_goodput = random_delivered as f64 / (1024.0 * 1024.0) / BULK_WINDOW_S as f64;
        print_perf(
            &format!("rtp bulk goodput burst rep {rep}"),
            burst_delivered as usize,
            window,
        );
        print_perf(
            &format!("rtp bulk goodput random rep {rep}"),
            random_delivered as usize,
            window,
        );

        assert!(
            random_goodput >= RANDOM_GOODPUT_STALL_FLOOR_MIB_S,
            "random-loss baseline stalled: {random_goodput:.3} MiB/s < {RANDOM_GOODPUT_STALL_FLOOR_MIB_S} MiB/s"
        );

        let ratio = if random_goodput > 0.0 {
            burst_goodput / random_goodput
        } else if burst_goodput > 0.0 {
            f64::INFINITY
        } else {
            0.0
        };
        eprintln!("[rtp_burst_loss] rep {rep} burst/random ratio = {ratio:.3}");
        ratios.push(ratio);
        burst_pair.stop();
        random_pair.stop();
    }
    ratios.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median_ratio = ratios[BULK_REPETITIONS / 2];
    eprintln!(
        "[rtp_burst_loss] median burst/random ratio (N={BULK_REPETITIONS}) = {median_ratio:.3}"
    );
    assert!(
        median_ratio >= MIN_BURST_VS_RANDOM_RATIO,
        "median burst/random ratio {median_ratio:.3} < {MIN_BURST_VS_RANDOM_RATIO}"
    );
}

/// Run one direction of the byte-sink upload and return the pair plus the
/// number of verified payload bytes delivered by the server.
async fn run_rtp_sink_upload(
    c2s: NetemConfig,
    s2c: NetemConfig,
    data: &'static [u8],
    window: Duration,
) -> (NetemPair, u64) {
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, delivered) =
                spawn_rtp_byte_sink_server_with_mss_via(&task_tx, false, MSS)
                    .await
                    .unwrap();
            let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
            let mut writer =
                spawn_rtp_bulk_upload_with_mss_via(&task_tx, pair.client_addr(), false, MSS)
                    .await
                    .unwrap();

            let (stop_tx, mut stop_rx) = tokio::sync::watch::channel(false);
            let mut pump_tasks = tokio::task::JoinSet::new();
            pump_tasks.spawn(async move {
                let mut offset = 0usize;
                let start = Instant::now();
                while start.elapsed() < window {
                    tokio::select! {
                        _ = stop_rx.changed() => break,
                        result = writer.write(&data[offset..]) => match result {
                            Ok(0) => break,
                            Ok(n) => offset = (offset + n) % data.len(),
                            Err(_) => break,
                        },
                    }
                }
            });

            let d = tokio::select! {
                joined = pump_tasks.join_next(), if !pump_tasks.is_empty() => {
                    // The pump ended before the measurement window completed:
                    // fail the test instead of measuring against a dead upload.
                    joined.expect("pump task exists").unwrap();
                    panic!("bulk pump ended before the measurement window completed");
                }
                _ = tokio::time::sleep(window) => {
                    let d = delivered.load(Ordering::Relaxed);
                    stop_tx.send(true).unwrap();
                    // Epilog: join the pump so any panic surfaces.
                    while let Some(result) = pump_tasks.join_next().await {
                        result.unwrap();
                    }
                    d
                }
            };
            (pair, d)
        })
        .await
}

/// Sparse timestamped messages through a burst-loss link must deliver most
/// messages within a bounded tail latency.
///
/// We send 64-byte messages every 300 ms for 60 s through a GE burst-loss
/// link (5% loss, mean burst length 3). The messages travel over a `mux`
/// stream on top of RTP; `mux` heartbeats keep the underlying RTP connection
/// alive during the sparse traffic, so this exercises RTP recovery under burst
/// loss rather than RTP's idle broken-pipe heuristic. The server records
/// one-way latency for every delivered message. After draining, we assert:
/// * received / sent >= 98%
/// * p50 <= 300 ms
/// * p99 <= 2500 ms
#[tokio::test(flavor = "multi_thread")]
#[ignore = "burst-loss goodput/tail-latency regression; slow; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_sparse_message_tail_latency_under_burst_loss() {
    let base = Instant::now();
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let (sent, mut samples) = tasks
        .run(async {
            let (server_addr, mut latencies) = spawn_mux_msg_latency_sink_via(&task_tx, false, base)
                .await
                .unwrap();
            let pair = NetemPair::spawn(
                server_addr,
                burst_loss_link(5.0, 3.0, OWD, 11),
                burst_loss_link(5.0, 3.0, OWD, 22),
            )
            .unwrap();

            let (connected_read, connected_write) =
                rtp_connect_with_mss_via(&task_tx, pair.client_addr(), false, MSS).await;
            let opener = mux_client_connect_via(&task_tx, connected_read, connected_write);
            let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

            // Keep the stream read half alive for the duration of the test so the mux
            // connection is not closed while we are only sending pings. Parked until
            // the connection closes; the owning JoinSet aborts it at scope end.
            submit_test_task(&task_tx, Box::pin(async move {
                let mut buf = vec![0u8; 8 * 1024];
                loop {
                    match stream_read.read(&mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                }
            }));

            let sent = with_timeout(
                Duration::from_secs(80),
                "send sparse timestamped messages",
                send_timestamped_messages(
                    &mut stream_write,
                    base,
                    SPARSE_MSG_BYTES,
                    SPARSE_MSG_INTERVAL,
                    SPARSE_MSG_INTERVAL * SPARSE_MSG_COUNT as u32,
                ),
            )
            .await;

            // Give stragglers a few RTTs to arrive, then drain the latency
            // channel.
            tokio::time::sleep(Duration::from_secs(4)).await;
            let mut samples = Vec::new();
            while let Ok(latency_ms) = latencies.try_recv() {
                samples.push(latency_ms);
            }
            pair.stop();
            (sent, samples)
        })
        .await;

    let received = samples.len() as u64;
    let delivery_pct = received as f64 / sent.max(1) as f64;
    eprintln!("[rtp_burst_loss] sparse sent={sent} received={received} delivery={delivery_pct:.3}");
    assert!(
        delivery_pct >= SPARSE_DELIVERY_FLOOR_PCT,
        "sparse delivery {delivery_pct:.3} < {SPARSE_DELIVERY_FLOOR_PCT}"
    );

    if !samples.is_empty() {
        samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let p50 = percentile(&samples, 0.50);
        let p99 = percentile(&samples, 0.99);
        eprintln!("[rtp_burst_loss] sparse latencies p50={p50:.1} ms p99={p99:.1} ms");
        assert!(
            p50 <= SPARSE_P50_LATENCY_MS,
            "sparse p50 latency {p50:.1} ms > {SPARSE_P50_LATENCY_MS} ms"
        );
        assert!(
            p99 <= SPARSE_P99_LATENCY_MS,
            "sparse p99 latency {p99:.1} ms > {SPARSE_P99_LATENCY_MS} ms"
        );
    }
}
