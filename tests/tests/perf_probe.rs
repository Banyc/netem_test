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
    mux_send_payload, mux_timed_echo_round_trip, spawn_mux_over_rtp_counting_sink_server_via,
    spawn_mux_over_rtp_echo_server_via, spawn_mux_over_rtp_echo_server_with_mss_via,
    spawn_mux_over_rtp_sink_server_via, spawn_mux_over_rtp_sink_server_with_mss_via,
};
use support::payload::{cyclic_payload, payload, with_timeout};
use support::presets::clean;
use support::rtp::{
    rtp_echo_payload, spawn_rtp_echo_server_via, spawn_rtp_echo_server_with_mss_via,
};
use support::stats::{combined_stats, print_median_worst, print_perf};
use tokio::io::AsyncWriteExt;

mod support;

/// Connect a `mux` client whose session is intentionally torn down
/// mid-body: each one-shot probe stops its pair (cutting the link) right
/// after measuring, so the supervision drain must be transient rather
/// than `spawn_required`. JoinErrors are unwrapped so a panicked
/// supervision task still fails the test; a `MuxError` session-end is the
/// expected teardown here, not a failure.
fn mux_client_connect_transient<R, W>(
    task_tx: &tokio::sync::mpsc::Sender<support::TestTask>,
    read: R,
    write: W,
) -> mux::StreamOpener
where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
    W: tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: false,
    };
    let mut spawner = tokio::task::JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
    // Transient drain (see doc comment): unwrap JoinErrors so panics
    // surface; a `MuxError` session-end ends the drain normally. Submitted
    // through the already-active bounded outer submitter instead of an
    // unpolled body-local scope, so a panicked supervision task fails the
    // test immediately rather than disappearing when the scope is dropped.
    support::submit_test_task(
        task_tx,
        Box::pin(async move {
            if let Some(result) = spawner.join_next().await {
                result.unwrap();
            }
        }),
    );
    opener
}

/// Connect an `rtp` client whose session is intentionally torn down
/// mid-body: each one-shot probe stops its pair (cutting the link) right
/// after measuring, so the supervisor keepalive must be transient (ordinary
/// spawn, ending when the connection closes) rather than `spawn_required`
/// (which would panic when the session ends before the body completes).
async fn rtp_connect_transient(
    task_tx: &tokio::sync::mpsc::Sender<support::TestTask>,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (
    impl tokio::io::AsyncRead + Unpin + Send + use<>,
    impl tokio::io::AsyncWrite + Unpin + Send + use<>,
) {
    let connected = rtp::udp::connect_with(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            fec,
            mss: rtp::udp::MssConfig::Custom(mss),
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    let read = connected.read.into_async_read();
    let write = connected.write.into_async_write();
    // The supervisor owns the session drivers; an ordinary submit keeps it
    // alive only until the session ends (the expected teardown here).
    // Routing it through the already-active bounded outer submitter means a
    // panicked supervisor fails the test immediately instead of being
    // stored in an unpolled body-local scope until it is dropped.
    support::submit_test_task(
        task_tx,
        Box::pin(async move {
            let _ = connected.supervisor.await;
        }),
    );
    (read, write)
}

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
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);

    let data = payload(BULK);
    let (samples, pair) = tasks
        .run(async {
            let server_addr = spawn_rtp_echo_server_via(&task_tx, false).await.unwrap();
            let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for _ in 0..PROBE_ITERS {
                let (read, write) = rtp_connect_transient(
                    &task_tx,
                    pair.client_addr(),
                    false,
                    rtp::udp::NO_FEC_MSS,
                )
                .await;
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
            pair.stop();
            (samples, pair)
        })
        .await;

    print_median_worst("rtp 4MiB direct echo (one-way bytes)", BULK, samples);

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
    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);

    let data = payload(BULK);
    let (samples, pair) = tasks
        .run(async {
            let server_addr = spawn_rtp_echo_server_with_mss_via(&task_tx, false, LOOPBACK_MSS)
                .await
                .unwrap();
            let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for _ in 0..PROBE_ITERS {
                let (read, write) =
                    rtp_connect_transient(&task_tx, pair.client_addr(), false, LOOPBACK_MSS).await;
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
            pair.stop();
            (samples, pair)
        })
        .await;

    print_median_worst("rtp 4MiB 8KiB-MSS echo (one-way bytes)", BULK, samples);

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
    let mut tasks = support::TestScope::new();

    // Pre-spawn one one-shot mux sink server per iteration; each accepts its
    // first (and only) RTP connection during that iteration and its task
    // completes once the client closes the session afterwards.
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let samples = tasks
        .run(async {
            let mut servers = Vec::new();
            for _ in 0..PROBE_ITERS {
                let (server_addr, received) = spawn_mux_over_rtp_sink_server_via(&task_tx, false)
                    .await
                    .unwrap();
                servers.push((server_addr, received));
            }

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for (server_addr, mut received) in servers {
                let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
                let (read, write) = rtp_connect_transient(
                    &task_tx,
                    pair.client_addr(),
                    false,
                    rtp::udp::NO_FEC_MSS,
                )
                .await;
                let config = mux::MuxConfig {
                    initiation: mux::Initiation::Client,
                    heartbeat_interval: Duration::from_secs(5),
                    frame_reassembly: false,
                };
                let mut spawner = tokio::task::JoinSet::new();
                let (opener, _accepter) =
                    mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
                // Transient supervision drain submitted to the test-owned
                // reaper: the session is intentionally torn down mid-body
                // (pair.stop() cuts the link after each one-shot probe), so
                // the drain must not be required. The reaper unwraps every
                // completion, so a panicked supervision task still fails the
                // test; a MuxError session-end is the expected teardown.
                support::submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        if let Some(result) = spawner.join_next().await {
                            result.unwrap();
                        }
                    }),
                );

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
            samples
        })
        .await;

    print_median_worst("mux sink 4MiB direct", BULK, samples);
}

/// `mux`-over-`rtp` 4 MiB sink upload using the loopback-sized MSS.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "loopback perf-ceiling probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn probe_mux_sink_4mib_mss8k() {
    let data = payload(BULK);
    let mut tasks = support::TestScope::new();

    // Pre-spawn one one-shot mux sink server per iteration; each accepts its
    // first (and only) RTP connection during that iteration.
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let samples = tasks
        .run(async {
            let mut servers = Vec::new();
            for _ in 0..PROBE_ITERS {
                let (server_addr, received) =
                    spawn_mux_over_rtp_sink_server_with_mss_via(&task_tx, false, LOOPBACK_MSS)
                        .await
                        .unwrap();
                servers.push((server_addr, received));
            }

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for (server_addr, mut received) in servers {
                let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
                let (read, write) =
                    rtp_connect_transient(&task_tx, pair.client_addr(), false, LOOPBACK_MSS).await;
                let config = mux::MuxConfig {
                    initiation: mux::Initiation::Client,
                    heartbeat_interval: Duration::from_secs(5),
                    frame_reassembly: false,
                };
                let mut spawner = tokio::task::JoinSet::new();
                let (opener, _accepter) =
                    mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
                // Transient supervision drain submitted to the test-owned
                // reaper: the session is intentionally torn down mid-body
                // (pair.stop() cuts the link after each one-shot probe), so
                // the drain must not be required. The reaper unwraps every
                // completion, so a panicked supervision task still fails the
                // test; a MuxError session-end is the expected teardown.
                support::submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        if let Some(result) = spawner.join_next().await {
                            result.unwrap();
                        }
                    }),
                );

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
            samples
        })
        .await;

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
    let mut tasks = support::TestScope::new();

    // Pre-spawn one one-shot mux echo server per iteration; each accepts its
    // first (and only) RTP connection during that iteration.
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let samples = tasks
        .run(async {
            let mut servers = Vec::new();
            for _ in 0..PROBE_ITERS {
                let server_addr = spawn_mux_over_rtp_echo_server_via(&task_tx, false)
                    .await
                    .unwrap();
                servers.push(server_addr);
            }

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for server_addr in servers {
                let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
                let (read, write) = rtp_connect_transient(
                    &task_tx,
                    pair.client_addr(),
                    false,
                    rtp::udp::NO_FEC_MSS,
                )
                .await;
                let config = mux::MuxConfig {
                    initiation: mux::Initiation::Client,
                    heartbeat_interval: Duration::from_secs(5),
                    frame_reassembly: false,
                };
                let mut spawner = tokio::task::JoinSet::new();
                let (opener, _accepter) =
                    mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
                // Transient supervision drain submitted to the test-owned
                // reaper: the session is intentionally torn down mid-body
                // (pair.stop() cuts the link after each one-shot probe), so
                // the drain must not be required. The reaper unwraps every
                // completion, so a panicked supervision task still fails the
                // test; a MuxError session-end is the expected teardown.
                support::submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        if let Some(result) = spawner.join_next().await {
                            result.unwrap();
                        }
                    }),
                );

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
            samples
        })
        .await;

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
    let mut tasks = support::TestScope::new();

    // Pre-spawn one one-shot mux echo server per iteration; each accepts its
    // first (and only) RTP connection during that iteration.
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    let samples = tasks
        .run(async {
            let mut servers = Vec::new();
            for _ in 0..PROBE_ITERS {
                let server_addr =
                    spawn_mux_over_rtp_echo_server_with_mss_via(&task_tx, false, LOOPBACK_MSS)
                        .await
                        .unwrap();
                servers.push(server_addr);
            }

            let mut samples = Vec::with_capacity(PROBE_ITERS);
            for server_addr in servers {
                let pair = NetemPair::spawn(server_addr, clean(), clean()).unwrap();
                let (read, write) =
                    rtp_connect_transient(&task_tx, pair.client_addr(), false, LOOPBACK_MSS).await;
                let config = mux::MuxConfig {
                    initiation: mux::Initiation::Client,
                    heartbeat_interval: Duration::from_secs(5),
                    frame_reassembly: false,
                };
                let mut spawner = tokio::task::JoinSet::new();
                let (opener, _accepter) =
                    mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
                // Transient supervision drain submitted to the test-owned
                // reaper: the session is intentionally torn down mid-body
                // (pair.stop() cuts the link after each one-shot probe), so
                // the drain must not be required. The reaper unwraps every
                // completion, so a panicked supervision task still fails the
                // test; a MuxError session-end is the expected teardown.
                support::submit_test_task(
                    &task_tx,
                    Box::pin(async move {
                        if let Some(result) = spawner.join_next().await {
                            result.unwrap();
                        }
                    }),
                );

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
            samples
        })
        .await;

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

    let mut tasks = support::TestScope::new();
    let task_tx = tasks.submitter(support::TEST_TASK_QUEUE_BOUND);
    tasks
        .run(async {
            let (server_addr, progress) =
                spawn_mux_over_rtp_counting_sink_server_via(&task_tx, false, LOOPBACK_MSS)
                    .await
                    .unwrap();
            let pair = NetemPair::spawn(
                server_addr,
                support::presets::hostile_real_link(),
                support::presets::hostile_real_link(),
            )
            .unwrap();
            let pair_ref = &pair;
            let (read, write) =
                rtp_connect_transient(&task_tx, pair.client_addr(), false, LOOPBACK_MSS).await;
            let opener = mux_client_connect_transient(&task_tx, read, write);

            // Open the stream under a generous timeout before we start the clock.
            let (stream_read, mut stream_write) = with_timeout(
                Duration::from_secs(30),
                "open mux stream for hostile goodput",
                async { opener.open().await.unwrap() },
            )
            .await;

            // A cyclic payload never exhausts: the pump keeps writing until the
            // measurement owner signals it to stop, so the fixed window is never
            // sender-limited and an early pump exit remains observable.
            let data = cyclic_payload(1024 * 1024);
            let start = Instant::now();

            let (pump_stop_tx, mut pump_stop_rx) = tokio::sync::watch::channel(false);
            let mut pump_tasks = tokio::task::JoinSet::new();
            pump_tasks.spawn(async move {
                loop {
                    tokio::select! {
                        _ = pump_stop_rx.changed() => break,
                        result = stream_write.write_all(&data) => {
                            result.unwrap();
                        }
                    }
                }
            });

            let (delivered, elapsed) = tokio::select! {
                joined = pump_tasks.join_next(), if !pump_tasks.is_empty() => {
                    // The bulk pump ended before the measurement window
                    // completed: fail the test.
                    joined.expect("bulk pump exists").unwrap();
                    panic!("bulk pump ended before the measurement window completed");
                }
                _ = tokio::time::sleep(Duration::from_secs_f64(WINDOW)) => {
                    let delivered = progress.delivered_bytes();
                    let elapsed = start.elapsed();
                    pump_stop_tx.send(true).unwrap();
                    // Epilog: join the pump so any panic surfaces.
                    while let Some(result) = pump_tasks.join_next().await {
                        result.unwrap();
                    }
                    (delivered, elapsed)
                }
            };

            pair_ref.stop();
            let stats = combined_stats(pair_ref);
            assert!(
                stats.dropped > 0 && stats.delayed > 0,
                "hostile link should drop and delay packets, got {stats:?}"
            );

            // The sink verifies every accepted byte but a corrupt byte only
            // freezes the counter, so the freeze must be asserted explicitly
            // rather than left to the goodput floor.
            assert!(
                !progress.is_corrupt(),
                "sink saw bytes diverging from the payload pattern"
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

            // Keep the stream read half alive until after the delivered
            // snapshot.
            let _ = stream_read;
        })
        .await;
}
