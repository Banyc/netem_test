//! `mux` over `rtp` performance scenarios through [`netem_test::NetemPair`].
//!
//! These exercise the multiplexed byte stream over a contended, lossy 400
//! KiB/s link and a small-interactive-stream-while-bulk scenario. Perf tests
//! print elapsed/throughput/stats with `--nocapture`.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test mux_over_rtp_perf -- --ignored --nocapture --test-threads=1
//! ```

use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::{
    mux_client_connect, mux_send_payload, mux_send_repeated, mux_timed_echo_round_trip,
    spawn_mux_over_rtp_counting_sink_server, spawn_mux_over_rtp_echo_server,
    spawn_mux_over_rtp_sink_server,
};
use support::payload::{cyclic_payload, payload, with_timeout};
use support::presets::{hostile_fat_pipe, lossy_400kib_per_sec};
use support::rtp::rtp_connect;
use support::stats::{combined_stats, print_perf};

mod support;

/// `mux` over `rtp` should deliver a 1 KiB payload intact through a lossy,
/// rate-limited 400 KiB/s netem link — a smoke test that the multiplexed
/// stream survives a contended link without tripping `rtp`'s broken-pipe
/// heuristic. `stats.rate_limited > 0` is deterministic (every non-reordered
/// forwarded packet increments it when `rate != 0`), so this is not flaky.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "perf scenario over a contended, lossy link; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_lossy_perf_smoke() {
    let mut tasks = support::TestScope::new();
    let server_addr = spawn_mux_over_rtp_echo_server(&mut tasks, false)
        .await
        .unwrap();

    let c2s = lossy_400kib_per_sec();
    let s2c = lossy_400kib_per_sec();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let opener = mux_client_connect(&mut tasks, read, write);

    let payload = payload(1024);
    tasks
        .run(async {
            let (got, elapsed) = with_timeout(
                Duration::from_secs(30),
                "mux-over-rtp 1KiB lossy perf smoke",
                mux_timed_echo_round_trip(&opener, &payload),
            )
            .await;

            assert_eq!(got, payload, "mux stream must deliver all 1KiB intact");
            print_perf("mux-over-rtp 1KiB lossy perf smoke", payload.len(), elapsed);
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    eprintln!("[perf] mux-over-rtp 1KiB lossy perf smoke stats: {stats:?}");
    assert!(
        stats.forwarded > 0,
        "proxy should forward packets, got {stats:?}"
    );
    assert!(
        stats.rate_limited > 0,
        "rate-limited link should shape packets, got {stats:?}"
    );
}

/// `mux` over `rtp` should deliver a 400 KiB payload intact through a lossy,
/// rate-limited 400 KiB/s netem link to a read-only sink, and report
/// throughput with `--nocapture`. The rate is `400 * 1024 * 8` bits/s plus
/// small loss/latency/jitter so the link is contended but not hopeless.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "perf scenario over a contended, lossy link; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_400kib_lossy_contended_perf() {
    let mut tasks = support::TestScope::new();
    let (server_addr, mut received) = spawn_mux_over_rtp_sink_server(&mut tasks, false)
        .await
        .unwrap();

    let c2s = lossy_400kib_per_sec();
    let s2c = lossy_400kib_per_sec();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let opener = mux_client_connect(&mut tasks, read, write);

    let payload = payload(400 * 1024);
    tasks
        .run(async {
            let elapsed = with_timeout(
                Duration::from_secs(120),
                "mux-over-rtp 400KiB lossy perf",
                mux_send_payload(&opener, &payload),
            )
            .await;

            let got = with_timeout(
                Duration::from_secs(120),
                "mux-over-rtp 400KiB lossy perf receive",
                async { received.recv().await.expect("sink channel closed") },
            )
            .await;

            assert_eq!(got, payload, "mux stream must deliver all 400KiB intact");
            print_perf(
                "mux-over-rtp 400KiB lossy/contended",
                payload.len(),
                elapsed,
            );
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    eprintln!("[perf] mux-over-rtp 400KiB lossy/contended stats: {stats:?}");
    assert!(
        stats.dropped > 0,
        "lossy link should drop some, got {stats:?}"
    );
    assert!(
        stats.rate_limited > 0,
        "rate-limited link should shape packets, got {stats:?}"
    );
}

/// A small interactive mux stream should complete while a bulk 400 KiB/s
/// transfer is also in flight over the same contended link — verifying the
/// multiplexer does not starve small streams under bulk load. Both payloads
/// are sent to a read-only sink through the same `rtp` connection and the
/// same proxy; the small payload must arrive within 5 s of the start.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "perf scenario over a contended, lossy link; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_small_stream_while_bulk_perf() {
    let mut tasks = support::TestScope::new();
    let (server_addr, mut received) = spawn_mux_over_rtp_sink_server(&mut tasks, false)
        .await
        .unwrap();

    // A mildly contended link: latency + small loss, no harsh rate limit so
    // the bulk and small streams can both make progress.
    let impaired = NetemConfig {
        latency: Duration::from_millis(10),
        jitter: Duration::from_millis(3),
        loss: u32::MAX / 200, // ~0.5%
        rate: 400 * 1024 * 8,
        seed: 17,
        ..NetemConfig::default()
    };
    let pair = NetemPair::spawn(server_addr, impaired.clone(), impaired).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let opener = mux_client_connect(&mut tasks, read, write);

    // Send the bulk 400 KiB payload on one stream, then after a short delay
    // send the small interactive payload on a second stream. The opener's
    // `open` takes `&self`, so a cloned opener can share the connection.
    let bulk = payload(400 * 1024);
    let small = b"small-interactive-stream".to_vec();

    let start = Instant::now();
    let bulk_opener = opener.clone();
    let bulk_for_compare = bulk.clone();
    let mut bulk_tasks: tokio::task::JoinSet<Duration> = tokio::task::JoinSet::new();
    bulk_tasks.spawn(async move { mux_send_payload(&bulk_opener, &bulk).await });

    tasks
        .run(async {
            tokio::time::sleep(Duration::from_millis(25)).await;
            let small_elapsed = mux_send_payload(&opener, &small).await;

            // Drain the sink channel until both payloads have arrived,
            // matching by equality. Panic on any unexpected payload.
            let mut got_bulk = false;
            let mut got_small = false;
            let mut small_arrived_at: Option<Instant> = None;
            let deadline = tokio::time::Instant::now() + Duration::from_secs(120);
            while !got_bulk || !got_small {
                let recv = tokio::time::timeout_at(deadline, received.recv())
                    .await
                    .unwrap_or_else(|_| panic!("timeout waiting for sink payloads"))
                    .expect("sink channel closed");
                if recv == bulk_for_compare {
                    got_bulk = true;
                } else if recv == small {
                    got_small = true;
                    small_arrived_at = Some(Instant::now());
                } else {
                    panic!("unexpected payload from sink: len {}", recv.len());
                }
            }

            let bulk_elapsed = with_timeout(
                Duration::from_secs(120),
                "mux-over-rtp small-while-bulk bulk join",
                async {
                    bulk_tasks
                        .join_next()
                        .await
                        .expect("bulk send task ended without a result")
                        .expect("bulk send task panicked")
                },
            )
            .await;

            assert!(got_bulk, "bulk stream must deliver all 400KiB intact");
            assert!(got_small, "small stream must deliver intact");
            let small_arrived_after = small_arrived_at
                .expect("small payload arrived")
                .duration_since(start);
            assert!(
                small_arrived_after < Duration::from_secs(5),
                "small stream should arrive < 5 s after start, took {small_arrived_after:?}"
            );
            let _ = small_elapsed;
            print_perf(
                "mux-over-rtp bulk 400KiB (small-while-bulk)",
                bulk_for_compare.len(),
                bulk_elapsed,
            );
        })
        .await;

    pair.stop();
    let stats = combined_stats(&pair);
    eprintln!("[perf] mux-over-rtp small-while-bulk stats: {stats:?}");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `mux` over `rtp` should deliver a 400 MiB payload intact through a hostile
/// link profiled from real ICMP measurements against `google.com` /
/// `8.8.8.8` (~15% loss, 300 ms latency, 500 ms jitter stddev) to a read-only
/// sink, and report throughput with `--nocapture`. The fat pipe + long latency
/// inflates the bandwidth-delay product, growing the proxy's internal queue
/// past tens of thousands of entries and exposing O(n²) insertion, per-packet
/// lock contention, and per-packet allocation — bottlenecks the mild synthetic
/// presets cannot reach.
///
/// At 15% loss the throughput is dominated by `rtp`'s ARQ, not the proxy: a
/// 10 MiB run takes ~10 min (~0.016 MiB/s), so 400 MiB takes many hours. The
/// timeout is set generously; run this only when investigating proxy-level
/// behaviour under a real fat, hostile pipe.
///
/// Run with:
///
/// ```sh
/// cargo test --release --test mux_over_rtp_perf \
///     mux_over_rtp_400mib_hostile_perf -- --ignored --nocapture --test-threads=1
/// ```
#[tokio::test(flavor = "multi_thread")]
#[ignore = "perf scenario over a contended, lossy link; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn mux_over_rtp_400mib_hostile_perf() {
    const TARGET_BYTES: usize = 400 * 1024 * 1024;
    const BUDGET: Duration = Duration::from_secs(335);
    let mut tasks = support::TestScope::new();
    let (server_addr, progress) =
        spawn_mux_over_rtp_counting_sink_server(&mut tasks, false, rtp::udp::NO_FEC_MSS)
            .await
            .unwrap();
    let impaired = hostile_fat_pipe();
    let pair = NetemPair::spawn(server_addr, impaired.clone(), impaired).unwrap();
    let (read, write, _supervisor) = rtp_connect(pair.client_addr(), false).await;
    let opener = mux_client_connect(&mut tasks, read, write);
    let chunk = cyclic_payload(1024 * 1024);
    let repeat = TARGET_BYTES.div_ceil(chunk.len());
    let sent = chunk.len() * repeat;
    tasks
        .run(async {
            let elapsed = with_timeout(
                BUDGET,
                "mux-over-rtp 400MiB hostile perf",
                mux_send_repeated(&opener, &chunk, repeat),
            )
            .await;
            assert!(
                !progress.is_corrupt(),
                "sink saw bytes diverging from the payload pattern"
            );
            assert_eq!(
                progress.delivered_bytes(),
                sent as u64,
                "mux stream must deliver every byte sent"
            );
            print_perf("mux-over-rtp 400MiB hostile", sent, elapsed);
        })
        .await;
    pair.stop();
    let stats = combined_stats(&pair);
    eprintln!("[perf] mux-over-rtp 400MiB hostile stats: {stats:?}");
    assert!(
        stats.dropped > 0,
        "hostile link should drop some, got {stats:?}"
    );
    assert!(
        stats.delayed > 0,
        "hostile link should delay packets, got {stats:?}"
    );
    assert!(
        stats.rate_limited > 0,
        "rate-capped link should shape packets, got {stats:?}"
    );
}
