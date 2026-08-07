//! `DualMux`-v4 bulk-lane verification: four A/B report-only probes that compare
//! a single-mux-session bulk lane against raw `rtp` over identically-seeded
//! netem links.
//!
//! The dual-lane `DualMux` architecture intends its bulk lane to be a full
//! `mux` session over its own `rtp` connection. These report-only probes answer
//! whether the claimed `+47-49%` bulk goodput gain over raw `rtp` transfers to
//! that design, separating the cap-escape effect from the ACK/loss-fate
//! effect.
//!
//! Run with:
//!
//! ```sh
//! cargo test --release --test hol_verify4 -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::mux::{mux_client_connect, spawn_mux_over_rtp_server_with_mss};
use support::payload::{cyclic_payload, with_timeout};
use support::presets::burst_loss_link;
use support::rtp::{spawn_rtp_bulk_upload, spawn_rtp_byte_sink_server};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// Wall-clock budget for each probe (1.5 s ramp + 15 s run + 3 s grace + slack).
const BULK_WINDOW: Duration = Duration::from_millis(19_500);

/// Bulk write chunk size: 251-aligned (~256 KiB).
const CHUNK: usize = 262_044;

/// Spawn a mux-over-RTP server that accepts a single `mux` session and treats
/// every accepted stream as a deterministic bulk byte sink.
///
/// Returns the RTP listener address and an atomic counter that is incremented
/// by the number of bytes read from each stream until `Ok(0)`/Err. There is
/// no payload verification; all bytes are counted.
async fn spawn_mux_bulk_sink(
    tasks: &mut tokio::task::JoinSet<()>,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    let delivered = Arc::new(AtomicU64::new(0));
    let delivered_for_server = Arc::clone(&delivered);
    let addr = spawn_mux_over_rtp_server_with_mss(
        tasks,
        false,
        rtp::udp::NO_FEC_MSS,
        move |mut stream_read, mut stream_write| {
            let delivered_for_stream = Arc::clone(&delivered_for_server);
            async move {
                let mut buf = vec![0u8; 64 * 1024];
                while let Ok(n) = stream_read.read(&mut buf).await {
                    if n == 0 {
                        break;
                    }
                    delivered_for_stream.fetch_add(n as u64, Ordering::Relaxed);
                }
                let _ = stream_write.shutdown();
            }
        },
    )
    .await?;
    Ok((addr, delivered))
}

/// Open a full `mux` session through a `NetemPair`, then write a single bulk
/// stream of deterministic cyclic payload for `BULK_WINDOW`.
///
/// `label` is used only for logging. Returns the total bytes delivered at the
/// server-side sink.
async fn run_muxbulk(label: &str, c2s: NetemConfig, s2c: NetemConfig) -> u64 {
    let mut tasks = tokio::task::JoinSet::new();
    let (server_addr, delivered) = spawn_mux_bulk_sink(&mut tasks).await.unwrap();
    let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

    let connected = rtp::udp::connect_with(
        "0.0.0.0:0",
        &pair.client_addr().to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            fec: false,
            mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    let (opener, mut spawner) = mux_client_connect(
        connected.read.into_async_read(),
        connected.write.into_async_write(),
    );

    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

    // Drain the stream read half in the background so flow-control ACKs keep
    // moving and the writer does not stall. Parked for the bulk window; the
    // owning JoinSet aborts it at scope end.
    tasks.spawn(async move {
        let mut buf = vec![0u8; 8 * 1024];
        while let Ok(n) = stream_read.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    });

    let stop = Arc::new(AtomicBool::new(false));
    let payload = cyclic_payload(CHUNK);
    let start = Instant::now();
    while !stop.load(Ordering::Relaxed) {
        if start.elapsed() >= BULK_WINDOW {
            stop.store(true, Ordering::Relaxed);
            break;
        }
        match stream_write.write_all(&payload[..CHUNK]).await {
            Ok(()) => {}
            Err(_) => break,
        }
    }
    let elapsed = start.elapsed();
    let delivered_at_window = delivered.load(Ordering::Relaxed);
    let _ = stream_write.shutdown();

    // Wait for the mux supervision tasks to settle before stopping the pair.
    spawner.shutdown().await;
    pair.stop();

    let total = delivered.load(Ordering::Relaxed);
    let mibps = total as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64().max(f64::EPSILON);
    let mibps_window =
        delivered_at_window as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64().max(f64::EPSILON);
    eprintln!(
        "[v4 {label}] delivered={total}B (at-window={delivered_at_window}B) \
         elapsed={elapsed:?} bulk={mibps:.3} MiB/s bulk-window={mibps_window:.3} MiB/s",
    );
    total
}

/// Open a raw `rtp` connection through a `NetemPair` and pump the same
/// deterministic cyclic payload for `BULK_WINDOW`.
///
/// The sink is [`support::spawn_rtp_byte_sink_server`]. Returns the total bytes
/// delivered at the server-side sink.
async fn run_rawbulk(label: &str, c2s: NetemConfig, s2c: NetemConfig) -> u64 {
    let mut tasks = tokio::task::JoinSet::new();
    let (sink_addr, delivered) = spawn_rtp_byte_sink_server(&mut tasks, false).await.unwrap();
    let pair = NetemPair::spawn(sink_addr, c2s, s2c).unwrap();

    let mut writer = spawn_rtp_bulk_upload(&mut tasks, pair.client_addr(), false)
        .await
        .unwrap();
    let payload = cyclic_payload(CHUNK);
    let start = Instant::now();
    while start.elapsed() < BULK_WINDOW {
        match writer.write_all(&payload[..CHUNK]).await {
            Ok(()) => {}
            Err(_) => break,
        }
    }
    let elapsed = start.elapsed();
    let delivered_at_window = delivered.load(Ordering::Relaxed);

    // Explicitly drop the writer so the server sees EOF and stops counting.
    drop(writer);
    // Give stragglers time to drain before stopping the proxy.
    tokio::time::sleep(Duration::from_secs(3)).await;
    pair.stop();

    let total = delivered.load(Ordering::Relaxed);
    let mibps = total as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64().max(f64::EPSILON);
    let mibps_window =
        delivered_at_window as f64 / (1024.0 * 1024.0) / elapsed.as_secs_f64().max(f64::EPSILON);
    eprintln!(
        "[v4 {label}] delivered={total}B (at-window={delivered_at_window}B) \
         elapsed={elapsed:?} bulk={mibps:.3} MiB/s bulk-window={mibps_window:.3} MiB/s",
    );
    total
}

/// Build a clean delay-only link profile used by the `v4_clean_*` probes.
fn clean_link(owd: Duration, seed: u64) -> NetemConfig {
    NetemConfig {
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

// ────────────────────────────── report-only probes ───────────────────────────

/// `v4` bulk lane over Gilbert-Elliott 5% burst loss vs raw `rtp` on the same
/// seeded link (c2s seed 33, s2c seed 44).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "DualMux-v4 bulk-lane A/B report-only probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn v4_ge5_muxbulk() {
    with_timeout(
        Duration::from_secs(120),
        "v4 ge5 muxbulk",
        run_muxbulk(
            "ge5 muxbulk",
            burst_loss_link(5.0, 3.0, Duration::from_millis(50), 33),
            burst_loss_link(5.0, 3.0, Duration::from_millis(50), 44),
        ),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "DualMux-v4 bulk-lane A/B report-only probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn v4_ge5_rawbulk() {
    with_timeout(
        Duration::from_secs(120),
        "v4 ge5 rawbulk",
        run_rawbulk(
            "ge5 rawbulk",
            burst_loss_link(5.0, 3.0, Duration::from_millis(50), 33),
            burst_loss_link(5.0, 3.0, Duration::from_millis(50), 44),
        ),
    )
    .await;
}

/// `v4` bulk lane on a clean 50 ms RTT link vs raw `rtp` on the same seeded
/// link (c2s seed 11, s2c seed 22).
#[tokio::test(flavor = "multi_thread")]
#[ignore = "DualMux-v4 bulk-lane A/B report-only probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn v4_clean_muxbulk() {
    with_timeout(
        Duration::from_secs(120),
        "v4 clean muxbulk",
        run_muxbulk(
            "clean muxbulk",
            clean_link(Duration::from_millis(50), 11),
            clean_link(Duration::from_millis(50), 22),
        ),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "DualMux-v4 bulk-lane A/B report-only probe; run with --ignored --nocapture --test-threads=1 (see module header)"]
async fn v4_clean_rawbulk() {
    with_timeout(
        Duration::from_secs(120),
        "v4 clean rawbulk",
        run_rawbulk(
            "clean rawbulk",
            clean_link(Duration::from_millis(50), 11),
            clean_link(Duration::from_millis(50), 22),
        ),
    )
    .await;
}
