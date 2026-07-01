//! `mux` over `rtp` performance scenarios through [`netem_test::NetemPair`].
//!
//! These exercise the multiplexed byte stream over a contended, lossy 400
//! KiB/s link and a small-interactive-stream-while-bulk scenario. Perf tests
//! print elapsed/throughput/stats with `--nocapture`.
//!
//! # Known upstream limitation (`#[ignore]`)
//!
//! The current `mux`-over-`rtp` stack trips `rtp`'s broken-pipe heuristic
//! for payloads above ~1 KiB on a single stream: once the mux control plane
//! stalls the ACK path under bulk write pressure, `rtp` declares the pipe
//! broken after ~16 s and the stream read returns `BrokenPipe`. This is a
//! property of the upstream `rtp`/`mux` crates, not of the `netem-test`
//! harness, and it reproduces on an entirely clean link with no impairment.
//! Consequently the 400 KiB bulk scenarios below are expected to **fail**
//! (surfacing as a timeout/broken-pipe, never an indefinite hang thanks to
//! the `with_timeout` guards). They are retained as `#[ignore]`d regression
//! targets so that, when the upstream broken-pipe heuristic is fixed, these
//! tests will start passing and lock in the 400 KiB/s perf contract.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test mux_over_rtp_perf -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};
use support::{
    combined_stats, lossy_400kib_per_sec, mux_client_connect, mux_timed_echo_round_trip, payload,
    print_perf, rtp_connect, spawn_mux_over_rtp_echo_server, with_timeout,
};

mod support;

/// `mux` over `rtp` should deliver a 400 KiB payload intact through a lossy,
/// rate-limited 400 KiB/s netem link, and report throughput with
/// `--nocapture`. The rate is `400 * 1024 * 8` bits/s plus small
/// loss/latency/jitter so the link is contended but not hopeless.
///
/// **Known failure**: see the module-level note on the upstream broken-pipe
/// heuristic. Expect a `BrokenPipe`/timeout failure rather than a pass until
/// the upstream `rtp`/`mux` crates can sustain bulk single-stream transfers.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn mux_over_rtp_400kib_lossy_contended_perf() {
    let server_addr = spawn_mux_over_rtp_echo_server(false).await.unwrap();

    let c2s = lossy_400kib_per_sec();
    let s2c = lossy_400kib_per_sec();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();
    let (read, write) = rtp_connect(pair.client_addr(), false).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    let payload = payload(400 * 1024);
    let (got, elapsed) = with_timeout(
        Duration::from_secs(120),
        "mux-over-rtp 400KiB lossy perf",
        mux_timed_echo_round_trip(&opener, &payload),
    )
    .await;

    assert_eq!(got, payload, "mux stream must deliver all 400KiB intact");
    print_perf(
        "mux-over-rtp 400KiB lossy/contended",
        payload.len(),
        elapsed,
    );

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
/// multiplexer does not starve small streams under bulk load. Both streams
/// are echoed back through the same `rtp` connection and the same proxy.
///
/// **Known failure**: see the module-level note on the upstream broken-pipe
/// heuristic. The bulk leg is expected to break the pipe; the small leg
/// would otherwise succeed in isolation.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn mux_over_rtp_small_stream_while_bulk_perf() {
    let server_addr = spawn_mux_over_rtp_echo_server(false).await.unwrap();

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
    let (read, write) = rtp_connect(pair.client_addr(), false).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    // Open two streams concurrently: a bulk 400 KiB stream and a small
    // interactive stream. Both must echo back intact. The opener's `open`
    // takes `&self`, so the two futures can share a reference.
    let bulk = payload(400 * 1024);
    let small = b"small-interactive-stream".to_vec();

    let bulk_fut = mux_timed_echo_round_trip(&opener, &bulk);
    let small_fut = mux_timed_echo_round_trip(&opener, &small);

    let (bulk_res, small_res) = with_timeout(
        Duration::from_secs(120),
        "mux-over-rtp small-while-bulk perf",
        futures(bulk_fut, small_fut),
    )
    .await;
    let (bulk_got, bulk_elapsed) = bulk_res;
    let small_got = small_res.0;

    assert_eq!(bulk_got, bulk, "bulk stream must deliver all 400KiB intact");
    assert_eq!(small_got, small, "small stream must deliver intact");
    print_perf(
        "mux-over-rtp bulk 400KiB (small-while-bulk)",
        bulk.len(),
        bulk_elapsed,
    );

    pair.stop();
    let stats = combined_stats(&pair);
    eprintln!("[perf] mux-over-rtp small-while-bulk stats: {stats:?}");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// Concurrently await two futures and return both results.
async fn futures<A, B>(a: A, b: B) -> (A::Output, B::Output)
where
    A: std::future::Future + Send,
    B: std::future::Future + Send,
    A::Output: Send,
    B::Output: Send,
{
    tokio::join!(a, b)
}
