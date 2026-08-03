//! Loopback perf-ceiling probes measuring raw `rtp` and `mux`-over-`rtp`
//! throughput with no netem impairment, plus a time-boxed hostile-link goodput
//! probe that verifies the proxy does not collapse on a lossy, high-latency
//! path.
//!
//! The tests are `#[ignore]` by default so they compile without running;
//! execute them with:
//!
//! ```sh
//! cargo test --release --test perf_probe -- --ignored --nocapture --test-threads=1
//! ```

use std::time::{Duration, Instant};

use netem_test::NetemPair;
use support::mux::{
    mux_client_connect, mux_send_payload, mux_timed_echo_round_trip,
    spawn_mux_over_rtp_counting_sink_server, spawn_mux_over_rtp_echo_server,
    spawn_mux_over_rtp_echo_server_with_mss, spawn_mux_over_rtp_sink_server,
    spawn_mux_over_rtp_sink_server_with_mss,
};
use support::payload::{payload, with_timeout};
use support::presets::clean;
use support::rtp::{
    rtp_connect, rtp_connect_with_mss, rtp_echo_payload, spawn_rtp_echo_server,
    spawn_rtp_echo_server_with_mss,
};
use support::stats::{combined_stats, print_median_worst, print_perf};
use tokio::io::AsyncWriteExt;

mod support;

/// Number of iterations for the ceiling probes. Reporting the median (and
/// worst) of several runs smooths out occasional warm-up / tail-visibility
/// episodes on the loopback path.
const PROBE_ITERS: usize = 5;

/// Bulk payload size: 4 MiB, a multiple of the staging payload that is small
/// enough to stay under `rtp`'s broken-pipe heuristic for raw RTP.
const BULK: usize = 4 * 1024 * 1024;

/// Loopback MSS for probes that need one: 8192 bytes, a whole multiple of the
/// staging payload and comfortably under macOS `net.inet.udp.maxdgram` ≈ 9216.
const LOOPBACK_MSS: usize = 8192;

/// Minimum acceptable goodput for the hostile-link probe. The pre-fix collapse
/// was ~0.015 MiB/s; this floor leaves a large margin while still catching a
/// severe regression.
const HOSTILE_GOODPUT_FLOOR_MIB_S: f64 = 0.5;

/// Raw `rtp` 4 MiB direct echo, default MSS.
///
/// Echo moves the payload twice, so throughput is reported as one-way bytes.
/// The server is reused across the five iterations; each iteration opens a
/// fresh RTP connection through the same `NetemPair`.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_rtp_echo_4mib_direct() {
    let server_addr = spawn_rtp_echo_server(false).await.unwrap();
    let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();

    let data = payload(BULK);
    let mut samples = Vec::with_capacity(PROBE_ITERS);
    for _ in 0..PROBE_ITERS {
        let (read, write) = rtp_connect(pair.client_addr(), false).await;
        let start = Instant::now();
        let got = with_timeout(
            Duration::from_secs(60),
            "rtp 4MiB direct echo",
            rtp_echo_payload(read, write, &data),
        )
        .await;
        let elapsed = start.elapsed();
        assert_eq!(got, data);
        samples.push(elapsed);
    }

    print_median_worst("rtp 4MiB direct echo (one-way bytes)", BULK, samples);

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.forwarded > 0,
        "proxy should forward packets, got {stats:?}"
    );
}

/// Raw `rtp` 4 MiB echo through a `NetemPair` using the loopback-sized MSS.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_rtp_echo_4mib_mss8k() {
    let server_addr = spawn_rtp_echo_server_with_mss(false, LOOPBACK_MSS)
        .await
        .unwrap();
    let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();

    let data = payload(BULK);
    let mut samples = Vec::with_capacity(PROBE_ITERS);
    for _ in 0..PROBE_ITERS {
        let (read, write) = rtp_connect_with_mss(pair.client_addr(), false, LOOPBACK_MSS).await;
        let start = Instant::now();
        let got = with_timeout(
            Duration::from_secs(60),
            "rtp 4MiB 8KiB-MSS echo",
            rtp_echo_payload(read, write, &data),
        )
        .await;
        let elapsed = start.elapsed();
        assert_eq!(got, data);
        samples.push(elapsed);
    }

    print_median_worst("rtp 4MiB 8KiB-MSS echo (one-way bytes)", BULK, samples);

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.forwarded > 0,
        "proxy should forward packets, got {stats:?}"
    );
}

/// `mux`-over-`rtp` 4 MiB sink upload, default MSS.
///
/// The sink server echoes nothing; the client writes the full payload and waits
/// for the peer-side EOF. The upload size is reported as payload bytes. A fresh
/// mux server is spawned for each iteration because the mux server only handles
/// its first accepted RTP connection.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_mux_sink_4mib_direct() {
    let data = payload(BULK);
    let mut samples = Vec::with_capacity(PROBE_ITERS);

    for _ in 0..PROBE_ITERS {
        let (server_addr, mut received) = spawn_mux_over_rtp_sink_server(false).await.unwrap();
        let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
        let (read, write) = rtp_connect(pair.client_addr(), false).await;
        let (opener, _spawner) = mux_client_connect(read, write);

        let elapsed = with_timeout(
            Duration::from_secs(60),
            "mux sink 4MiB direct upload",
            mux_send_payload(&opener, &data),
        )
        .await;

        let got = with_timeout(
            Duration::from_secs(60),
            "mux sink 4MiB direct receive",
            async { received.recv().await.expect("sink channel closed") },
        )
        .await;

        assert_eq!(got, data, "mux sink must deliver all 4MiB intact");
        samples.push(elapsed);

        pair.stop();
        let stats = combined_stats(&pair);
        assert!(
            stats.forwarded > 0,
            "proxy should forward packets, got {stats:?}"
        );
    }

    print_median_worst("mux sink 4MiB direct", BULK, samples);
}

/// `mux`-over-`rtp` 4 MiB sink upload using the loopback-sized MSS.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_mux_sink_4mib_mss8k() {
    let data = payload(BULK);
    let mut samples = Vec::with_capacity(PROBE_ITERS);

    for _ in 0..PROBE_ITERS {
        let (server_addr, mut received) =
            spawn_mux_over_rtp_sink_server_with_mss(false, LOOPBACK_MSS)
                .await
                .unwrap();
        let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
        let (read, write) = rtp_connect_with_mss(pair.client_addr(), false, LOOPBACK_MSS).await;
        let (opener, _spawner) = mux_client_connect(read, write);

        let elapsed = with_timeout(
            Duration::from_secs(60),
            "mux sink 4MiB 8KiB-MSS upload",
            mux_send_payload(&opener, &data),
        )
        .await;

        let got = with_timeout(
            Duration::from_secs(60),
            "mux sink 4MiB 8KiB-MSS receive",
            async { received.recv().await.expect("sink channel closed") },
        )
        .await;

        assert_eq!(got, data, "mux sink must deliver all 4MiB intact");
        samples.push(elapsed);

        pair.stop();
        let stats = combined_stats(&pair);
        assert!(
            stats.forwarded > 0,
            "proxy should forward packets, got {stats:?}"
        );
    }

    print_median_worst("mux sink 4MiB 8KiB-MSS", BULK, samples);
}

/// `mux`-over-`rtp` 1 MiB echo round-trip, default MSS.
///
/// Echo moves the payload twice; throughput is reported as one-way bytes.
/// This probe is known to hit `rtp`'s broken-pipe heuristic under bulk write
/// pressure once the mux ACK path stalls, so a failure here documents the
/// upstream `rtp` limitation rather than a defect in this probe.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_mux_echo_1mib_direct() {
    let data = payload(1024 * 1024);
    let mut samples = Vec::with_capacity(PROBE_ITERS);

    for _ in 0..PROBE_ITERS {
        let server_addr = spawn_mux_over_rtp_echo_server(false).await.unwrap();
        let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
        let (read, write) = rtp_connect(pair.client_addr(), false).await;
        let (opener, _spawner) = mux_client_connect(read, write);

        let (got, elapsed) = with_timeout(
            Duration::from_secs(60),
            "mux echo 1MiB direct round-trip",
            mux_timed_echo_round_trip(&opener, &data),
        )
        .await;

        assert_eq!(got, data, "mux echo must deliver all 1MiB intact");
        samples.push(elapsed);

        pair.stop();
        let stats = combined_stats(&pair);
        assert!(
            stats.forwarded > 0,
            "proxy should forward packets, got {stats:?}"
        );
    }

    print_median_worst("mux echo 1MiB direct (one-way bytes)", 1024 * 1024, samples);
}

/// `mux`-over-`rtp` 1 MiB echo round-trip using the loopback-sized MSS.
///
/// Like the direct variant, this probe is a known-failure reproduction for
/// `rtp`'s broken-pipe heuristic once the mux ACK path stalls under bulk
/// echo pressure.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_mux_echo_1mib_mss8k() {
    let data = payload(1024 * 1024);
    let mut samples = Vec::with_capacity(PROBE_ITERS);

    for _ in 0..PROBE_ITERS {
        let server_addr = spawn_mux_over_rtp_echo_server_with_mss(false, LOOPBACK_MSS)
            .await
            .unwrap();
        let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
        let (read, write) = rtp_connect_with_mss(pair.client_addr(), false, LOOPBACK_MSS).await;
        let (opener, _spawner) = mux_client_connect(read, write);

        let (got, elapsed) = with_timeout(
            Duration::from_secs(60),
            "mux echo 1MiB 8KiB-MSS round-trip",
            mux_timed_echo_round_trip(&opener, &data),
        )
        .await;

        assert_eq!(got, data, "mux echo must deliver all 1MiB intact");
        samples.push(elapsed);

        pair.stop();
        let stats = combined_stats(&pair);
        assert!(
            stats.forwarded > 0,
            "proxy should forward packets, got {stats:?}"
        );
    }

    print_median_worst(
        "mux echo 1MiB 8KiB-MSS (one-way bytes)",
        1024 * 1024,
        samples,
    );
}

/// Time-boxed `mux`-over-`rtp` goodput probe across the hostile link profile.
///
/// A 128 MiB payload is far more than the link can deliver in the 30 s window,
/// so the measured goodput is receive-limited, not send-limited. The counting
/// sink verifies every byte in-flight and is snapshotted while the transfer is
/// still mid-flight, so a reversed measurement order cannot inflate goodput.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_hostile_goodput_30s() {
    const WINDOW: f64 = 30.0;
    const HOSTILE_BULK: usize = 128 * 1024 * 1024;

    let (server_addr, progress) = spawn_mux_over_rtp_counting_sink_server(false, LOOPBACK_MSS)
        .await
        .unwrap();
    let pair = NetemPair::spawn(
        server_addr,
        support::presets::hostile_real_link(),
        support::presets::hostile_real_link(),
    )
    .unwrap();
    let pair_ref = &pair;
    let (read, write) = rtp_connect_with_mss(pair.client_addr(), false, LOOPBACK_MSS).await;
    let (opener, _spawner) = mux_client_connect(read, write);

    // Open the stream under a generous timeout before we start the clock.
    let (stream_read, mut stream_write) = with_timeout(
        Duration::from_secs(30),
        "open mux stream for hostile goodput",
        async { opener.open().await.unwrap() },
    )
    .await;

    let data = payload(HOSTILE_BULK);
    let start = Instant::now();

    // Keep the write half busy and the read half open for the full window.
    let _writer = tokio::spawn(async move {
        let _ = stream_write.write_all(&data).await;
    });

    tokio::time::sleep(Duration::from_secs_f64(WINDOW)).await;
    let delivered = progress.delivered_bytes();
    let elapsed = start.elapsed();

    pair_ref.stop();
    let stats = combined_stats(pair_ref);
    assert!(
        stats.dropped > 0 && stats.delayed > 0,
        "hostile link should drop and delay packets, got {stats:?}"
    );

    // The sink verifies every accepted byte but a corrupt byte only freezes the
    // counter, so the freeze must be asserted explicitly rather than left to the
    // goodput floor.
    assert!(
        !progress.is_corrupt(),
        "sink saw bytes diverging from the payload pattern"
    );
    assert!(
        delivered <= HOSTILE_BULK as u64,
        "sink counted more bytes than were sent: {delivered}"
    );

    let goodput_mib_s = delivered as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64();
    assert!(
        goodput_mib_s >= HOSTILE_GOODPUT_FLOOR_MIB_S,
        "goodput {goodput_mib_s:.3} MiB/s below floor {HOSTILE_GOODPUT_FLOOR_MIB_S} MiB/s"
    );

    print_perf(
        "mux-over-rtp hostile 30s goodput window",
        delivered as usize,
        elapsed,
    );
    eprintln!("[stats] {stats:?}");

    // Keep the stream read half alive until after the delivered snapshot.
    let _ = stream_read;
}
