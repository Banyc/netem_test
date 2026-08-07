//! RTP bufferbloat regression scenario.
//!
//! This test verifies that a latency-plus-rate-limited link with a bounded
//! per-direction queue (`limit`) does not catastrophically inflate latency or
//! drop below a reasonable goodput floor once the bottleneck buffer fills.
//!
//! Run with:
//!
//! ```sh
//! cargo test --release --test rtp_bufferbloat -- --ignored --nocapture --test-threads=1
//! ```

use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::send_timestamped_messages;
use support::payload::{cyclic_payload, with_timeout};
use support::rtp::{
    spawn_rtp_bulk_upload_with_mss, spawn_rtp_byte_sink_server_with_mss, spawn_rtp_msg_latency_sink,
};
use support::stats::{combined_stats, percentile, print_perf};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// RTP MSS for the bufferbloat probe. We use the default loopback MSS rather
/// than an oversized value because the RTP codec/FEC overhead consumes most of
/// a user-provided 8 KiB datagram ceiling, leaving almost no user payload.
const MSS: usize = rtp::udp::NO_FEC_MSS;

/// Link profile: 20 ms base latency, 10 Mbit/s bottleneck, 256-packet queue
/// limit. This is deliberately sized so the delay-gate/queue-limit build takes
/// zero overflow drops (the in-flight bound is below 256 packets).
pub fn bufferbloat_link(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(20),
        rate: 10_000_000, // 10 Mbit/s
        queue_limit_pkts: 256,
        seed,
        ..NetemConfig::default()
    }
}

/// Minimum acceptable goodput as a fraction of bottleneck capacity. The floor is
/// deliberately slack (~35% of 10 Mbit/s ≈ 0.42 MiB/s) so the test survives the
/// concurrent `rtp` branches without asserting the measured stock number.
const GOODPUT_CAPACITY_FLOOR: f64 = 0.35;
/// Maximum acceptable per-direction queue depth.
const MAX_QUEUE_FLOOR: usize = 256;

/// Run a bulk upload and sparse pings through a bufferbloat link for 15 s.
/// Assert that:
/// * max observed queue_len_c2s <= 256
/// * goodput >= 35% of capacity
/// * overflow_dropped == 0 (the limit is sized above the in-flight bound)
#[tokio::test(flavor = "multi_thread")]
#[ignore = "bufferbloat regression; slow; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn rtp_bulk_bounded_buffer_goodput_and_queue_bound() {
    let capacity_bps: f64 = 10_000_000.0;
    let capacity_mib_s: f64 = capacity_bps / 8.0 / (1024.0 * 1024.0);
    let floor_mib_s: f64 = capacity_mib_s * GOODPUT_CAPACITY_FLOOR;

    let base = Instant::now();
    let mut server_tasks = tokio::task::JoinSet::new();
    let (sink_addr, delivered) = spawn_rtp_byte_sink_server_with_mss(&mut server_tasks, false, MSS)
        .await
        .unwrap();
    let (latency_addr, mut latencies) = spawn_rtp_msg_latency_sink(&mut server_tasks, false, base)
        .await
        .unwrap();

    let pair = NetemPair::spawn(sink_addr, bufferbloat_link(4), bufferbloat_link(5)).unwrap();

    // Bulk upload sender. Write a repeating deterministic stream large enough
    // that the measurement window is receive-limited, not send-limited.
    let mut writer =
        spawn_rtp_bulk_upload_with_mss(&mut server_tasks, pair.client_addr(), false, MSS)
            .await
            .unwrap();
    let bulk_start = Instant::now();
    let data = cyclic_payload(64 * 1024 * 1024);
    let mut pump_tasks = tokio::task::JoinSet::new();
    pump_tasks.spawn(async move {
        let mut offset = 0usize;
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(15) {
            match writer.write(&data[offset..]).await {
                Ok(0) => break,
                Ok(n) => offset = (offset + n) % data.len(),
                Err(_) => break,
            }
        }
    });

    // Sparse latency sender on a separate connection through the same pair.
    let latency_pair =
        NetemPair::spawn(latency_addr, bufferbloat_link(4), bufferbloat_link(5)).unwrap();
    let connected = rtp::udp::connect_with(
        "0.0.0.0:0",
        &latency_pair.client_addr().to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            mss: rtp::udp::MssConfig::Custom(MSS),
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    let mut write = connected.write.into_async_write();
    let mut read = connected.read.into_async_read();
    // Keep the read half alive so ACKs keep moving; parked until the
    // connection closes, so the owning JoinSet aborts it at scope end.
    server_tasks.spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });

    let ping_sent = with_timeout(
        Duration::from_secs(25),
        "send sparse pings during bufferbloat",
        send_timestamped_messages(
            &mut write,
            base,
            128,
            Duration::from_millis(500),
            Duration::from_secs(15),
        ),
    )
    .await;

    // Sample the queue length every 50 ms while the transfer runs.
    let mut max_queue = 0usize;
    let sample_window = Duration::from_secs(15);
    let sample_start = Instant::now();
    while sample_start.elapsed() < sample_window {
        let q = pair.queue_len_c2s();
        if q > max_queue {
            max_queue = q;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    // Give the final bulk bytes time to drain through the shaped link before
    // measuring elapsed goodput.
    tokio::time::sleep(Duration::from_secs(2)).await;
    let delivered_bytes = delivered.load(std::sync::atomic::Ordering::Relaxed);
    let elapsed = bulk_start.elapsed();
    // The pump has completed its 15s window; drain it so any panic surfaces.
    while let Some(result) = pump_tasks.join_next().await {
        result.unwrap();
    }
    pair.stop();
    latency_pair.stop();

    let stats = combined_stats(&pair);
    print_perf(
        "rtp bufferbloat bulk goodput",
        delivered_bytes as usize,
        elapsed,
    );
    eprintln!("[rtp_bufferbloat] max_queue={max_queue} stats={stats:?}");

    assert!(
        max_queue <= MAX_QUEUE_FLOOR,
        "max c2s queue {max_queue} > {MAX_QUEUE_FLOOR}"
    );

    let goodput_mib_s = delivered_bytes as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64();
    assert!(
        goodput_mib_s >= floor_mib_s,
        "goodput {goodput_mib_s:.3} MiB/s < floor {floor_mib_s:.3} MiB/s"
    );

    // The delay-gate rtp build is expected to take zero overflow drops.
    assert_eq!(
        stats.overflow_dropped, 0,
        "delay-gate build must take zero overflow drops, got {stats:?}"
    );

    // Drain the sparse latency samples and assert tail bounds.
    tokio::time::sleep(Duration::from_secs(2)).await;
    let mut samples = Vec::new();
    while let Ok(latency_ms) = latencies.try_recv() {
        samples.push(latency_ms);
    }
    let received = samples.len() as u64;
    if received > 0 {
        samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let p50 = percentile(&samples, 0.50);
        let p99 = percentile(&samples, 0.99);
        eprintln!(
            "[rtp_bufferbloat] pings sent={ping_sent} received={received} p50={p50:.1} ms p99={p99:.1} ms"
        );
        // Floor: p50 ping latency should stay under ~0.8× the max queue build-up.
        assert!(p50 <= 800.0, "bufferbloat ping p50 {p50:.1} ms > 800 ms");
    }
}
