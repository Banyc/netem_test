//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.

#![allow(dead_code)]

use std::{
    collections::HashMap,
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use netem_test::{FourStateLoss, LossModel, NetemConfig, NetemPair, Stats};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc::{self, UnboundedReceiver};
use tokio::task::JoinSet;

// ─────────────────────────── impairment presets ───────────────────────────

/// No impairment at all — a clean baseline to verify the plumbing.
pub fn clean() -> NetemConfig {
    NetemConfig::default()
}

/// A latency-only config useful for verifying the proxy adds delay.
pub fn latency(ms: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(ms),
        seed: 7,
        ..NetemConfig::default()
    }
}

/// Mild impairment that exercises both directions without making `rtp`'s
/// reliable layer give up: ~5% random loss, 10 ms latency, small jitter.
pub fn mild_loss() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 20, // ~5%
        latency: Duration::from_millis(10),
        jitter: Duration::from_millis(5),
        seed: 1,
        ..NetemConfig::default()
    }
}

/// A lossy rate-limited config sized for the 400 KiB perf scenario. The rate
/// is `400 * 1024 * 8` bits/s with a small loss/latency/jitter so the link is
/// contended but not hopeless.
pub fn lossy_400kib_per_sec() -> NetemConfig {
    NetemConfig {
        rate: 400 * 1024 * 8,
        loss: u32::MAX / 100, // ~1%
        latency: Duration::from_millis(5),
        jitter: Duration::from_millis(2),
        seed: 4,
        ..NetemConfig::default()
    }
}

/// A hostile link profiled from real ICMP measurements against `google.com`
/// and `8.8.8.8` from this machine: ~15% loss, ~300 ms latency, ~500 ms
/// jitter (stddev). Used by the 400 MiB perf scenario to expose proxy-level
/// bottlenecks (queue insertion, per-packet locking, allocation) that the
/// mild synthetic presets cannot reach.
pub fn hostile_real_link() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 100 * 15, // ~15%
        latency: Duration::from_millis(300),
        jitter: Duration::from_millis(500),
        seed: 4,
        ..NetemConfig::default()
    }
}

/// Two-state Gilbert-Elliott loss model on top of the four-state `sch_netem`
/// representation.
///
/// `loss_pct` is the long-term loss probability (0–100). `mean_burst_len` is the
/// average number of consecutive lost packets (must be ≥ 1.0). The model is
/// parameterised so that bursts have `mean_burst_len` losses and gaps have a
/// geometrically-distributed number of deliveries between bursts.
///
/// Maps to [`FourStateLoss`] probabilities scaled so `u32::MAX == 1.0`:
/// * `p13` = probability a delivered packet in the gap state moves into the
///   burst-loss state (that packet is lost).
/// * `p31` = probability the burst-loss state returns to the gap state (that
///   delivered packet ends the burst).
/// * `p14` = `p23` = `p32` = 0.
///
/// Staying in burst-loss loses every packet, so burst and gap lengths are
/// geometric with means `1/p31` and `1/p13` respectively. `p14` cannot express
/// bursts because its successor state `LostInGap` always delivers the next
/// packet.
pub fn gilbert_elliott_loss(loss_pct: f64, mean_burst_len: f64) -> LossModel {
    assert!(
        (0.0..=100.0).contains(&loss_pct),
        "loss_pct must be in [0, 100]"
    );
    assert!(mean_burst_len >= 1.0, "mean_burst_len must be >= 1.0");

    // Long-term probability of being in the burst (loss) state.
    let p_burst = loss_pct / 100.0;
    // Mean number of delivered packets between isolated burst triggers, given
    // mean_burst_len and p_burst. Derived from the steady-state equations for
    // the two-state model.
    let mean_gap_len = if p_burst == 0.0 {
        f64::INFINITY
    } else {
        mean_burst_len * (1.0 - p_burst) / p_burst
    };

    let scale = |p: f64| -> u32 {
        let clamped = p.clamp(0.0, 1.0);
        (clamped * u32::MAX as f64).round() as u32
    };

    LossModel::FourState(FourStateLoss {
        p13: scale(1.0 / mean_gap_len),
        p31: scale(1.0 / mean_burst_len),
        p32: 0,
        p14: 0,
        p23: 0,
    })
}

/// Bidirectional `NetemPair` config with the Gilbert-Elliott burst loss model.
///
/// No rate cap, no latency beyond the optional one-way `owd`, and a
/// deterministic seed so the tests are reproducible.
pub fn burst_loss_link(
    loss_pct: f64,
    mean_burst_len: f64,
    owd: Duration,
    seed: u64,
) -> NetemConfig {
    NetemConfig {
        loss_model: gilbert_elliott_loss(loss_pct, mean_burst_len),
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

/// Bidirectional `NetemPair` config with independent random loss.
///
/// `loss_pct` is the per-packet drop probability (0–100). This is useful as a
/// baseline in the burst-vs-random comparison tests.
pub fn random_loss_link(loss_pct: f64, owd: Duration, seed: u64) -> NetemConfig {
    let loss = ((loss_pct / 100.0) * u32::MAX as f64).clamp(0.0, u32::MAX as f64) as u32;
    NetemConfig {
        loss,
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

// ─────────────────────────── deterministic payload ───────────────────────

/// Generate a deterministic payload of `n` bytes. The modulus is a prime so
/// the byte pattern is non-trivial and reproducible across runs.
pub fn payload(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i % 251) as u8).collect()
}

/// Deterministic payload whose length is a multiple of the 251-byte pattern
/// period. When the caller loops over this buffer with partial writes, the
/// wrapped stream still matches the `(global_offset % 251)` pattern expected by
/// the byte-counting sinks.
pub fn cyclic_payload(n: usize) -> Vec<u8> {
    let n = n - (n % 251);
    payload(n)
}

// ─────────────────────────── timeout wrapper ─────────────────────────────

/// Run `fut` to completion, panicking with `label` if it does not finish
/// within `dur`. This is the timeout guard every ignored async test uses so
/// failures surface instead of hanging the harness.
pub async fn with_timeout<T, F>(dur: Duration, label: &str, fut: F) -> T
where
    F: std::future::Future<Output = T>,
{
    tokio::time::timeout(dur, fut)
        .await
        .unwrap_or_else(|_| panic!("timeout ({dur:?}) waiting for {label}"))
}

// ─────────────────────────── rtp echo helpers ────────────────────────────

/// Spawn an `rtp` server that accepts one connection and echoes back
/// everything it receives until the peer closes. Returns the server's
/// listening address.
///
/// `mss` controls the RTP maximum segment size passed to
/// [`Listener::accept_without_handshake_with_mss`]. Use [`rtp::udp::NO_FEC_MSS`]
/// for the default size.
pub async fn spawn_rtp_echo_server_with_mss(
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    tokio::spawn(async move {
        loop {
            let accepted = match listener.accept_without_handshake_with_mss(fec, mss).await {
                Ok(a) => a,
                Err(_) => return,
            };
            tokio::spawn(async move {
                let mut read = accepted.read.into_async_read();
                let mut write = accepted.write.into_async_write();
                let mut buf = vec![0u8; 8 * 1024];
                loop {
                    match read.read(&mut buf).await {
                        Ok(0) => break,
                        Ok(n) => {
                            if write.write_all(&buf[..n]).await.is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
                let _ = write.shutdown().await;
            });
        }
    });
    Ok(addr)
}

/// Spawn an `rtp` echo server using the default MSS.
pub async fn spawn_rtp_echo_server(fec: bool) -> std::io::Result<std::net::SocketAddr> {
    spawn_rtp_echo_server_with_mss(fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an `rtp` client to a proxy's client-side address and return the
/// resulting reliable byte-stream halves. Pass [`NetemPair::client_addr`] as
/// `proxy_client_addr`; `fec` must match the server's FEC setting.
pub async fn rtp_connect(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (
    impl AsyncRead + Unpin + Send,
    impl AsyncWrite + Unpin + Send,
) {
    rtp_connect_with_mss(proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an `rtp` client with a custom MSS.
///
/// `mss` is passed to [`rtp::udp::connect_without_handshake_with_mss`];
/// `proxy_client_addr` should be [`NetemPair::client_addr`].
pub async fn rtp_connect_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (
    impl AsyncRead + Unpin + Send,
    impl AsyncWrite + Unpin + Send,
) {
    let connected = rtp::udp::connect_without_handshake_with_mss(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        None,
        fec,
        mss,
    )
    .await
    .unwrap();
    (
        connected.read.into_async_read(),
        connected.write.into_async_write(),
    )
}

/// Write `payload` through `write`, shut the writer down, and read the full
/// echo back from `read` until EOF. Panics on any IO error.
pub async fn rtp_echo_payload<R, W>(mut read: R, mut write: W, payload: &[u8]) -> Vec<u8>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    write.write_all(payload).await.unwrap();
    write.shutdown().await.unwrap();
    let mut got = Vec::new();
    read.read_to_end(&mut got).await.unwrap();
    got
}

// ─────────────────────────── mux-over-rtp helpers ────────────────────────

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// handed to `handle_stream`, which owns its read/write halves. Returns the
/// rtp server's listening address.
///
/// `mss` is passed to the RTP accept helpers; use [`rtp::udp::NO_FEC_MSS`] for
/// the default size.
///
/// The listener is wrapped in an [`Arc`] so a background `accept()`-loop can
/// keep driving `udp_listener`'s dispatcher for the server's lifetime:
/// `accept()` both establishes new connections *and* dispatches packets to
/// existing ones (via `try_send` to their per-conn channels). Without a
/// background accept-loop, the dispatcher stops after the first connection
/// and subsequent datagrams are never forwarded to it, so the reliable
/// layer stalls. This is required by `udp_listener`'s docs ("You still need
/// to put `accept()` in a loop to drive the packet dispatch among the
/// sub-connections").
pub async fn spawn_mux_over_rtp_server_with_mss<F, Fut>(
    fec: bool,
    mss: usize,
    handle_stream: F,
) -> std::io::Result<std::net::SocketAddr>
where
    F: Fn(mux::StreamReader, mux::StreamWriter) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    let listener = Arc::new(listener);
    tokio::spawn({
        let listener = Arc::clone(&listener);
        async move {
            // First (and only) rtp connection.
            let accepted = match listener.accept_without_handshake_with_mss(fec, mss).await {
                Ok(a) => a,
                Err(_) => return,
            };
            // Keep driving `udp_listener`'s dispatcher for the lifetime of the
            // server: `accept()` both establishes new connections and
            // dispatches packets to existing ones. Without a background
            // accept-loop, the dispatcher stops after the first connection
            // and subsequent datagrams are never forwarded to it, so the
            // reliable layer stalls.
            tokio::spawn({
                let listener = Arc::clone(&listener);
                async move {
                    loop {
                        if listener
                            .accept_without_handshake_with_mss(fec, mss)
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                }
            });

            let read = accepted.read.into_async_read();
            let write = accepted.write.into_async_write();

            let config = mux::MuxConfig {
                initiation: mux::Initiation::Server,
                heartbeat_interval: Duration::from_secs(5),
                frame_reassembly: false,
            };
            let mut spawner = tokio::task::JoinSet::new();
            let (_opener, mut accepter) =
                mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

            while let Ok((stream_read, stream_write)) = accepter.accept().await {
                let handle_stream = &handle_stream;
                tokio::spawn(handle_stream(stream_read, stream_write));
            }
        }
    });
    Ok(addr)
}

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// echoed back. Returns the rtp server's listening address.
pub async fn spawn_mux_over_rtp_echo_server_with_mss(
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_mux_over_rtp_server_with_mss(fec, mss, |mut stream_read, mut stream_write| async move {
        let mut buf = vec![0u8; 8 * 1024];
        loop {
            match stream_read.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if stream_write.write_all(&buf[..n]).await.is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = stream_write.shutdown();
    })
    .await
}

/// Spawn a mux-over-RTP echo server using the default MSS.
pub async fn spawn_mux_over_rtp_echo_server(fec: bool) -> std::io::Result<std::net::SocketAddr> {
    spawn_mux_over_rtp_echo_server_with_mss(fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// read to EOF into a `Vec<u8>` and sent on the returned channel (capacity
/// 16) if the read succeeded or the buffer is non-empty, then the write half
/// is shut down. Returns the rtp server's listening address and the receiver
/// for completed payloads.
pub async fn spawn_mux_over_rtp_sink_server_with_mss(
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<Vec<u8>>)> {
    let (tx, rx) = tokio::sync::mpsc::channel(16);
    let addr =
        spawn_mux_over_rtp_server_with_mss(fec, mss, move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            async move {
                let mut buf = Vec::new();
                let read_ok = stream_read.read_to_end(&mut buf).await.is_ok();
                if read_ok || !buf.is_empty() {
                    let _ = tx.send(buf).await;
                }
                let _ = stream_write.shutdown();
            }
        })
        .await?;
    Ok((addr, rx))
}

/// Spawn a mux-over-RTP sink server using the default MSS.
pub async fn spawn_mux_over_rtp_sink_server(
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<Vec<u8>>)> {
    spawn_mux_over_rtp_sink_server_with_mss(fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` server that accepts one connection and reads into a 64 KiB
/// buffer, verifying the deterministic payload pattern byte-by-byte as it
/// arrives. The read half keeps running until the peer closes.
///
/// The listener is wrapped in an [`Arc`] so a background `accept()`-loop can
/// keep driving `udp_listener`'s dispatcher for the server's lifetime: without
/// a loop, the dispatcher stops forwarding datagrams to the accepted
/// connection and the reliable layer stalls.
///
/// Returns the server's listening address and an [`AtomicU64`] counter that is
/// incremented with the number of *new* verified payload bytes on every
/// successful read. This lets tests observe live goodput without waiting for an
/// EOF.
pub async fn spawn_rtp_byte_sink_server(
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    spawn_rtp_byte_sink_server_with_mss(fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` byte sink server using a custom MSS.
pub async fn spawn_rtp_byte_sink_server_with_mss(
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    let listener = Arc::new(listener);

    let delivered = Arc::new(AtomicU64::new(0));
    let delivered_for_server = Arc::clone(&delivered);
    tokio::spawn({
        let listener = Arc::clone(&listener);
        async move {
            let accepted = match listener.accept_without_handshake_with_mss(fec, mss).await {
                Ok(a) => a,
                Err(_) => return,
            };
            // Keep driving the listener's dispatcher so packets keep flowing to
            // the accepted connection. Extra incoming connections are accepted
            // and ignored.
            let listener = Arc::clone(&listener);
            tokio::spawn(async move {
                loop {
                    if listener
                        .accept_without_handshake_with_mss(fec, mss)
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
            let mut read = accepted.read.into_async_read();
            // Hold the write half alive so the connection stays open while we
            // only receive.
            let _write = accepted.write;
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset: u64 = 0;
            loop {
                match read.read(&mut buf).await {
                    Ok(0) => break,
                    Ok(n) => {
                        let mut ok = true;
                        for (j, &actual) in buf[..n].iter().enumerate() {
                            let expected = ((offset + j as u64) % 251) as u8;
                            if actual != expected {
                                ok = false;
                                break;
                            }
                        }
                        if ok {
                            offset += n as u64;
                            delivered_for_server.fetch_add(n as u64, Ordering::Relaxed);
                        }
                    }
                    Err(_) => break,
                }
            }
        }
    });
    Ok((addr, delivered))
}

/// Spawn an `rtp` server that accepts one connection and parses the same
/// per-message framing as [`send_timestamped_messages`]: `[4-byte LE total frame
/// length][payload][8-byte LE send timestamp in micros since `base]`].
///
/// The server records the one-way latency of every received message as
/// `now_us.saturating_sub(sent_us) / 1000.0` milliseconds and pushes the sample
/// into the returned channel. The read loop continues until the peer closes.
/// The write half is kept alive until then so the connection is not garbage
/// collected while only pings are flowing.
///
/// The listener is wrapped in an [`Arc`] and driven by a background accept loop
/// so the `udp_listener` dispatcher keeps forwarding datagrams to the accepted
/// connection for the server's lifetime.
pub async fn spawn_rtp_msg_latency_sink(
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<f64>,
)> {
    spawn_rtp_msg_latency_sink_with_mss(fec, base, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_rtp_msg_latency_sink`] with a custom RTP MSS.
pub async fn spawn_rtp_msg_latency_sink_with_mss(
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<f64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<f64>();
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    let listener = Arc::new(listener);

    tokio::spawn({
        let listener = Arc::clone(&listener);
        async move {
            let accepted = match listener.accept_without_handshake_with_mss(fec, mss).await {
                Ok(a) => a,
                Err(_) => return,
            };
            let listener = Arc::clone(&listener);
            tokio::spawn(async move {
                loop {
                    if listener
                        .accept_without_handshake_with_mss(fec, mss)
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
            let mut read = accepted.read.into_async_read();
            let _write = accepted.write;
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset = 0usize;
            loop {
                if offset >= buf.len() {
                    buf.resize(buf.len().saturating_mul(2), 0u8);
                }
                let n = match read.read(&mut buf[offset..]).await {
                    Ok(n) => n,
                    Err(_) => break,
                };
                if n == 0 {
                    break;
                }
                offset += n;
                loop {
                    if offset < 4 {
                        break;
                    }
                    let frame_len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                    if frame_len < 12 {
                        // Invalid frame; drop the connection.
                        break;
                    }
                    if offset < frame_len {
                        break;
                    }
                    let payload_end = frame_len - 8;
                    let sent_us = u64::from_le_bytes([
                        buf[payload_end],
                        buf[payload_end + 1],
                        buf[payload_end + 2],
                        buf[payload_end + 3],
                        buf[payload_end + 4],
                        buf[payload_end + 5],
                        buf[payload_end + 6],
                        buf[payload_end + 7],
                    ]);
                    let now_us = base.elapsed().as_micros() as u64;
                    let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                    let _ = tx.send(latency_ms);
                    buf.copy_within(frame_len..offset, 0);
                    offset -= frame_len;
                }
            }
        }
    });
    Ok((addr, rx))
}

/// Open a fresh `rtp` connection through the proxy and return the async write
/// half. A background task keeps the read half alive so ACKs are processed and
/// the sender does not stall.
///
/// This is the sender side for the live-goodput probes. The caller writes data
/// through the returned write half and drops it (or aborts the writing task)
/// when done; the read-keepalive task exits automatically when the peer closes.
pub async fn spawn_rtp_bulk_upload(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> std::io::Result<rtp::socket::WriteStream> {
    spawn_rtp_bulk_upload_with_mss(proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_rtp_bulk_upload`] with a custom MSS.
pub async fn spawn_rtp_bulk_upload_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> std::io::Result<rtp::socket::WriteStream> {
    let connected = rtp::udp::connect_without_handshake_with_mss(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        None,
        fec,
        mss,
    )
    .await?;
    let mut read = connected.read.into_async_read();
    let write = connected.write.into_async_write();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            match read.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    });
    Ok(write)
}

/// Spawn a mux-over-RTP server that accepts one connection and parses a simple
/// per-message framing on each accepted mux stream: each record carries a
/// 4-byte little-endian total frame length (including the 4-byte length itself
/// and the 8-byte timestamp trailer), followed by the payload, followed by an
/// 8-byte little-endian send timestamp in microseconds since `base`.
///
/// The server records the one-way latency of every received message as
/// `now_us.saturating_sub(sent_us) / 1000.0` milliseconds and pushes the sample
/// into the returned channel. The read loop continues until the peer closes.
///
/// Using `mux` over RTP is important for sparse-message streams: `mux` emits
/// periodic heartbeat frames that keep the underlying RTP connection alive and
/// acknowledged, avoiding the RTP layer's proactive broken-pipe heuristic that
/// fires on quiet unidirectional streams.
pub async fn spawn_mux_msg_latency_sink(
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<f64>,
)> {
    spawn_mux_msg_latency_sink_with_mss(fec, base, rtp::udp::NO_FEC_MSS).await
}

/// Spawn a mux-over-RTP server that accepts one connection and classifies each
/// accepted mux stream by its first byte.
///
/// * `b'L'`: timestamped latency frames (`[4 LE total len][payload][8 LE
///   micros since base]`). One-way latency in milliseconds is pushed into the
///   returned unbounded channel.
/// * any other byte: deterministic bulk byte sink. Bytes after the tag are
///   verified against the `(offset % 251)` pattern and counted in the returned
///   [`AtomicU64`]; they are then discarded.
///
/// This combined server lets HOL and contested-latency scenarios open an
/// interactive ping stream and a competing bulk sink stream on the same mux
/// connection while using a single server address.
pub async fn spawn_mux_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(fec, rtp::udp::NO_FEC_MSS, {
        let tx = tx.clone();
        let bulk_delivered = Arc::clone(&bulk_delivered);
        move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            let bulk_delivered = Arc::clone(&bulk_delivered);
            async move {
                let mut tag = [0u8; 1];
                let n = match stream_read.read(&mut tag).await {
                    Ok(n) => n,
                    Err(_) => {
                        let _ = stream_write.shutdown();
                        return;
                    }
                };
                if n == 0 {
                    let _ = stream_write.shutdown();
                    return;
                }

                if tag[0] == b'L' {
                    // Timestamped latency frame parser.
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset = 0usize;
                    loop {
                        let n = match stream_read.read(&mut buf[offset..]).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        offset += n;
                        loop {
                            if offset < 4 {
                                break;
                            }
                            let frame_len =
                                u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                            if frame_len < 12 {
                                break;
                            }
                            if offset < frame_len {
                                break;
                            }
                            let payload_end = frame_len - 8;
                            let sent_us = u64::from_le_bytes([
                                buf[payload_end],
                                buf[payload_end + 1],
                                buf[payload_end + 2],
                                buf[payload_end + 3],
                                buf[payload_end + 4],
                                buf[payload_end + 5],
                                buf[payload_end + 6],
                                buf[payload_end + 7],
                            ]);
                            let now_us = base.elapsed().as_micros() as u64;
                            let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                            let _ = tx.send(latency_ms);
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    // Bulk byte sink: verify the deterministic pattern and
                    // count verified bytes. The tag byte itself is excluded.
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    loop {
                        let n = match stream_read.read(&mut buf).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        let mut ok = true;
                        for (j, &actual) in buf[..n].iter().enumerate() {
                            let expected = ((offset + j as u64) % 251) as u8;
                            if actual != expected {
                                ok = false;
                                break;
                            }
                        }
                        if ok {
                            offset += n as u64;
                            bulk_delivered.fetch_add(n as u64, Ordering::Relaxed);
                        }
                    }
                }

                let _ = stream_write.shutdown();
            }
        }
    })
    .await?;
    Ok((addr, rx, bulk_delivered))
}

/// Like [`spawn_mux_latency_bulk_server`] but with a custom RTP MSS.
pub async fn spawn_mux_sized_latency_bulk_server(
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(fec, mss, {
        let tx = tx.clone();
        let bulk_delivered = Arc::clone(&bulk_delivered);
        move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            let bulk_delivered = Arc::clone(&bulk_delivered);
            async move {
                let mut tag = [0u8; 1];
                let n = match stream_read.read(&mut tag).await {
                    Ok(n) => n,
                    Err(_) => {
                        let _ = stream_write.shutdown();
                        return;
                    }
                };
                if n == 0 {
                    let _ = stream_write.shutdown();
                    return;
                }

                if tag[0] == b'L' {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset = 0usize;
                    loop {
                        let n = match stream_read.read(&mut buf[offset..]).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        offset += n;
                        loop {
                            if offset < 4 {
                                break;
                            }
                            let frame_len =
                                u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                            if frame_len < 12 {
                                break;
                            }
                            if offset < frame_len {
                                break;
                            }
                            let payload_end = frame_len - 8;
                            let sent_us = u64::from_le_bytes([
                                buf[payload_end],
                                buf[payload_end + 1],
                                buf[payload_end + 2],
                                buf[payload_end + 3],
                                buf[payload_end + 4],
                                buf[payload_end + 5],
                                buf[payload_end + 6],
                                buf[payload_end + 7],
                            ]);
                            let now_us = base.elapsed().as_micros() as u64;
                            let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                            let _ = tx.send(latency_ms);
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    loop {
                        let n = match stream_read.read(&mut buf).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        let mut ok = true;
                        for (j, &actual) in buf[..n].iter().enumerate() {
                            let expected = ((offset + j as u64) % 251) as u8;
                            if actual != expected {
                                ok = false;
                                break;
                            }
                        }
                        if ok {
                            offset += n as u64;
                            bulk_delivered.fetch_add(n as u64, Ordering::Relaxed);
                        }
                    }
                }

                let _ = stream_write.shutdown();
            }
        }
    })
    .await?;
    Ok((addr, rx, bulk_delivered))
}

/// [`spawn_mux_msg_latency_sink`] with a custom RTP MSS.
pub async fn spawn_mux_msg_latency_sink_with_mss(
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<f64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<f64>();
    let addr =
        spawn_mux_over_rtp_server_with_mss(fec, mss, move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            async move {
                let mut buf = vec![0u8; 64 * 1024];
                let mut offset = 0usize;
                loop {
                    let n = match stream_read.read(&mut buf[offset..]).await {
                        Ok(n) => n,
                        Err(_) => break,
                    };
                    if n == 0 {
                        break;
                    }
                    offset += n;
                    // Parse complete frames from the accumulated buffer.
                    loop {
                        if offset < 4 {
                            break;
                        }
                        let frame_len =
                            u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                        if frame_len < 12 {
                            // Invalid frame; drop the whole connection.
                            break;
                        }
                        if offset < frame_len {
                            break;
                        }
                        let payload_end = frame_len - 8;
                        let sent_us = u64::from_le_bytes([
                            buf[payload_end],
                            buf[payload_end + 1],
                            buf[payload_end + 2],
                            buf[payload_end + 3],
                            buf[payload_end + 4],
                            buf[payload_end + 5],
                            buf[payload_end + 6],
                            buf[payload_end + 7],
                        ]);
                        let now_us = base.elapsed().as_micros() as u64;
                        let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                        let _ = tx.send(latency_ms);
                        buf.copy_within(frame_len..offset, 0);
                        offset -= frame_len;
                    }
                }
                let _ = stream_write.shutdown();
            }
        })
        .await?;
    Ok((addr, rx))
}

/// Send timestamped messages through a reliable byte-stream write half.
///
/// Each message is framed as `[4-byte LE total frame length][payload][8-byte
/// LE send timestamp in micros since `base`]`. Messages are sent at `interval`
/// using [`tokio::time::interval`] with [`MissedTickBehavior::Delay`] so a
/// missed deadline does not burst the offered load. The payload is a repeated
/// 12-byte sequence (rest-padded) so the server can verify message integrity.
///
/// Returns the number of messages sent. The caller is responsible for keeping
/// `write` alive for the duration of the test (e.g. by not shutting down the
/// underlying stream early).
pub async fn send_timestamped_messages(
    write: &mut (impl AsyncWrite + Unpin),
    base: Instant,
    msg_bytes: usize,
    interval: Duration,
    run_for: Duration,
) -> u64 {
    assert!(msg_bytes >= 12, "message framing needs at least 12 bytes");
    let mut interval = tokio::time::interval(interval);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut sent = 0u64;
    let payload_bytes = msg_bytes - 12;
    let payload: Vec<u8> = (0..payload_bytes).map(|i| (i % 251) as u8).collect();
    let start = Instant::now();
    loop {
        interval.tick().await;
        if start.elapsed() >= run_for {
            break;
        }
        let sent_us = base.elapsed().as_micros() as u64;
        let mut frame = Vec::with_capacity(msg_bytes);
        frame.extend_from_slice(&((msg_bytes as u32).to_le_bytes()));
        frame.extend_from_slice(&payload);
        frame.extend_from_slice(&sent_us.to_le_bytes());
        if write.write_all(&frame).await.is_err() {
            break;
        }
        sent += 1;
    }
    sent
}

/// Wrap a reliable byte-stream pair in a `mux` client and return the stream
/// opener plus the supervision `JoinSet` (kept alive for the test duration).
pub fn mux_client_connect<R, W>(read: R, write: W) -> (mux::StreamOpener, JoinSet<mux::MuxError>)
where
    R: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: false,
    };
    let mut spawner = JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);
    (opener, spawner)
}

/// Open a mux stream, write `payload`, shut the stream down, and read the
/// full echo back until EOF.
///
/// The write and the read run concurrently with `tokio::join!` instead of
/// write-then-read: once the payload exceeds the mux flow-control window a
/// sequential write blocks forever waiting for the peer to drain (which it
/// can only do by echoing), deadlocking the stream.
pub async fn mux_echo_round_trip(opener: &mux::StreamOpener, payload: &[u8]) -> Vec<u8> {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    let write_fut = async {
        stream_write.write_all(payload).await.unwrap();
        stream_write.shutdown().unwrap();
    };
    let read_fut = async {
        let mut got = Vec::new();
        let mut buf = vec![0u8; 8 * 1024];
        loop {
            match stream_read.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => got.extend_from_slice(&buf[..n]),
                Err(e) => panic!("mux stream read failed: {e:?}"),
            }
        }
        got
    };
    let (_, got) = tokio::join!(write_fut, read_fut);
    got
}

/// Open a mux stream, write `payload`, shut the stream down, read the full
/// echo back, and return `(received, elapsed)` where `elapsed` is measured
/// from just before the write to the completion of the read. Used by perf
/// and latency tests to print throughput with `--nocapture`.
///
/// The write and the read run concurrently with `tokio::join!` instead of
/// write-then-read: once the payload exceeds the mux flow-control window a
/// sequential write blocks forever waiting for the peer to drain (which it
/// can only do by echoing), deadlocking the stream.
pub async fn mux_timed_echo_round_trip(
    opener: &mux::StreamOpener,
    payload: &[u8],
) -> (Vec<u8>, Duration) {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    let start = Instant::now();
    let write_fut = async {
        stream_write.write_all(payload).await.unwrap();
        stream_write.shutdown().unwrap();
    };
    let read_fut = async {
        let mut got = Vec::new();
        let mut buf = vec![0u8; 8 * 1024];
        loop {
            match stream_read.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => got.extend_from_slice(&buf[..n]),
                Err(e) => panic!("mux stream read failed: {e:?}"),
            }
        }
        got
    };
    let (_, got) = tokio::join!(write_fut, read_fut);
    (got, start.elapsed())
}

/// Open a mux stream, write `payload`, shut the write half down, then read
/// to EOF to wait for the peer to finish draining. Returns the elapsed time
/// measured from just before the write to the completion of the peer-EOF
/// read — the wall-clock time the peer needed to receive the whole payload.
///
/// `ErrorKind::BrokenPipe` during the peer-EOF wait is tolerated: `rtp`'s
/// broken-pipe heuristic can fire after a completed upload (the ACK path
/// stalls once the peer has no more data to send), and delivery is already
/// verified by the sink-side equality assert. In that case the returned
/// duration may undercount delivery, so a warning is printed. Any other
/// read error panics.
pub async fn mux_send_payload(opener: &mux::StreamOpener, payload: &[u8]) -> Duration {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    let start = Instant::now();
    stream_write.write_all(payload).await.unwrap();
    stream_write.shutdown().unwrap();
    let mut sink = Vec::new();
    match stream_read.read_to_end(&mut sink).await {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::BrokenPipe => {
            eprintln!(
                "[mux_send_payload] BrokenPipe during peer-EOF wait; \
                 duration may undercount delivery"
            );
        }
        Err(e) => panic!("mux_send_payload peer-EOF read failed: {e:?}"),
    }
    start.elapsed()
}

/// Combined stats across both directions of a [`NetemPair`].
pub fn combined_stats(pair: &NetemPair) -> Stats {
    pair.stats()
}

/// Print a perf summary line for a payload of `bytes` that completed in
/// `elapsed`, using `label` to identify the scenario.
pub fn print_perf(label: &str, bytes: usize, elapsed: Duration) {
    let secs = elapsed.as_secs_f64().max(f64::EPSILON);
    let mib = bytes as f64 / (1024.0 * 1024.0);
    let throughput_mibps = mib / secs;
    eprintln!("[perf] {label}: {bytes} bytes in {elapsed:?} ({throughput_mibps:.3} MiB/s)");
}

/// Shared progress counter for the [`spawn_mux_over_rtp_counting_sink_server`]
/// goodput sink. It tracks how many payload bytes were delivered and whether
/// any byte failed the deterministic payload check.
pub struct SinkProgress {
    /// Total payload bytes accepted by the sink and verified against the
    /// deterministic `(offset % 251)` pattern.
    pub delivered: AtomicU64,
    /// Set to `true` if any accepted byte did not match the expected pattern.
    pub corrupt: AtomicBool,
}

impl SinkProgress {
    /// Create a fresh counter with zero delivered bytes and no corruption flag.
    pub fn new() -> Self {
        Self {
            delivered: AtomicU64::new(0),
            corrupt: AtomicBool::new(false),
        }
    }

    /// Number of payload bytes successfully delivered to the sink so far.
    pub fn delivered_bytes(&self) -> u64 {
        self.delivered.load(Ordering::Relaxed)
    }

    /// Whether the sink has observed any corrupted payload byte.
    pub fn is_corrupt(&self) -> bool {
        self.corrupt.load(Ordering::Relaxed)
    }
}

impl Default for SinkProgress {
    fn default() -> Self {
        Self::new()
    }
}

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of it. Each accepted mux stream is read chunk-by-chunk into a 64 KiB
/// buffer and verified against the deterministic payload pattern. Verified
/// bytes are atomically added to the returned [`SinkProgress::delivered`];
/// a mismatch sets [`SinkProgress::corrupt`] and stops counting that stream.
///
/// This sink is intentionally kept mid-flight: it does *not* buffer the full
/// payload or read to EOF, so a snapshot of `delivered_bytes()` taken while
/// the transfer is still alive reflects true goodput without an inflated
/// delivery snapshot.
pub async fn spawn_mux_over_rtp_counting_sink_server(
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<SinkProgress>)> {
    let progress = Arc::new(SinkProgress::new());
    let addr = spawn_mux_over_rtp_server_with_mss(fec, mss, {
        let progress = Arc::clone(&progress);
        move |mut stream_read, mut stream_write| {
            let progress = Arc::clone(&progress);
            async move {
                let mut buf = vec![0u8; 64 * 1024];
                let mut offset: u64 = 0;
                loop {
                    match stream_read.read(&mut buf).await {
                        Ok(0) => break,
                        Ok(n) => {
                            if progress.is_corrupt() {
                                continue;
                            }
                            let mut corrupt = false;
                            for (j, &actual) in buf[..n].iter().enumerate() {
                                let expected = ((offset + j as u64) % 251) as u8;
                                if actual != expected {
                                    corrupt = true;
                                    break;
                                }
                            }
                            if corrupt {
                                progress.corrupt.store(true, Ordering::Relaxed);
                            } else {
                                offset += n as u64;
                                progress.delivered.fetch_add(n as u64, Ordering::Relaxed);
                            }
                        }
                        Err(_) => break,
                    }
                }
                let _ = stream_write.shutdown();
            }
        }
    })
    .await?;
    Ok((addr, progress))
}

/// Convenience wrapper using the default RTP MSS.
pub async fn spawn_mux_over_rtp_counting_sink_server_default(
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, Arc<SinkProgress>)> {
    spawn_mux_over_rtp_counting_sink_server(fec, rtp::udp::NO_FEC_MSS).await
}

/// Sort `samples` and print perf summaries for the median and worst
/// (slowest) runs. Used by the ceiling probes so a single slow warm-up episode
/// does not distort the reported throughput.
pub fn print_median_worst(label: &str, bytes: usize, mut samples: Vec<Duration>) {
    samples.sort();
    let n = samples.len();
    assert!(n > 0, "print_median_worst called with empty samples");
    let median_label = format!("{label} [median of {n}]");
    let worst_label = format!("{label} [worst of {n}]");
    print_perf(&median_label, bytes, samples[n / 2]);
    print_perf(&worst_label, bytes, samples[n - 1]);
}

/// Return the p-th percentile of `sorted` using nearest-rank, truncating the
/// index. `sorted` must be sorted ascending and non-empty.
pub fn percentile(sorted: &[f64], p: f64) -> f64 {
    assert!(!sorted.is_empty(), "percentile called on empty samples");
    assert!(
        (0.0..=1.0).contains(&p),
        "percentile p must be in [0.0, 1.0]"
    );
    let rank = ((sorted.len() as f64 - 1.0) * p).floor() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dual‑mux helpers
// ═══════════════════════════════════════════════════════════════════════════════

/// Server that accepts two RTP connections (lane‑hello paired) and handles
/// both latency‑echo (tag byte `b'L'`) and bulk‑sink streams on the paired
/// dual‑lane mux. Returns `(addr, lat_rx, bulk_counter)` like
/// [`spawn_mux_latency_bulk_server`].
pub async fn spawn_dual_mux_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    spawn_dual_mux_latency_bulk_server_with_mss(fec, base, rtp::udp::NO_FEC_MSS).await
}

/// Like [`spawn_dual_mux_latency_bulk_server`] but with a custom RTP MSS.
pub async fn spawn_dual_mux_sized_latency_bulk_server(
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    spawn_dual_mux_latency_bulk_server_with_mss(fec, base, mss).await
}

async fn spawn_dual_mux_latency_bulk_server_with_mss(
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();

    let listener_bg = Arc::clone(&listener);
    tokio::spawn(async move {
        loop {
            match listener_bg
                .accept_without_handshake_with_mss(fec, mss)
                .await
            {
                Ok(accepted) => {
                    let _ = accept_tx.send(accepted);
                }
                Err(_) => break,
            }
        }
    });

    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::PendingAcceptor>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        while let Some(accepted) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();

            let result = mux::spawn_dual_mux_acceptor(
                reader,
                writer,
                config.clone(),
                Duration::from_secs(3),
            )
            .await;

            match result {
                Ok((_class, nonce, pa)) => {
                    let entries = pending.entry(nonce).or_default();
                    entries.push(pa);
                    if entries.len() == 2 {
                        let pa2 = entries.pop().unwrap();
                        let pa1 = entries.pop().unwrap();
                        pending.remove(&nonce);

                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, mut accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;
                                while let Ok((mut reader, mut writer, _class)) =
                                    accepter.accept().await
                                {
                                    let bulk = Arc::clone(&bulk);
                                    let tx = tx.clone();
                                    tokio::spawn(async move {
                                        let mut tag = [0u8; 1];
                                        if reader.read_exact(&mut tag).await.is_err() {
                                            let _ = writer.shutdown();
                                            return;
                                        }
                                        if tag[0] == b'L' {
                                            let mut buf = vec![0u8; 64 * 1024];
                                            let mut offset = 0usize;
                                            loop {
                                                let n = match reader.read(&mut buf[offset..]).await
                                                {
                                                    Ok(n) => n,
                                                    Err(_) => break,
                                                };
                                                if n == 0 {
                                                    break;
                                                }
                                                offset += n;
                                                loop {
                                                    if offset < 4 {
                                                        break;
                                                    }
                                                    let frame_len = u32::from_le_bytes([
                                                        buf[0], buf[1], buf[2], buf[3],
                                                    ])
                                                        as usize;
                                                    if frame_len < 12 {
                                                        break;
                                                    }
                                                    if offset < frame_len {
                                                        break;
                                                    }
                                                    let payload_end = frame_len - 8;
                                                    let sent_us = u64::from_le_bytes([
                                                        buf[payload_end],
                                                        buf[payload_end + 1],
                                                        buf[payload_end + 2],
                                                        buf[payload_end + 3],
                                                        buf[payload_end + 4],
                                                        buf[payload_end + 5],
                                                        buf[payload_end + 6],
                                                        buf[payload_end + 7],
                                                    ]);
                                                    let now_us = base.elapsed().as_micros() as u64;
                                                    let latency_ms = now_us.saturating_sub(sent_us)
                                                        as f64
                                                        / 1000.0;
                                                    let _ = tx.send(latency_ms);
                                                    buf.copy_within(frame_len..offset, 0);
                                                    offset -= frame_len;
                                                }
                                            }
                                        } else {
                                            let mut buf = vec![0u8; 64 * 1024];
                                            let mut offset: u64 = 0;
                                            loop {
                                                match reader.read(&mut buf).await {
                                                    Ok(0) | Err(_) => break,
                                                    Ok(n) => {
                                                        let mut ok = true;
                                                        for (j, &actual) in
                                                            buf[..n].iter().enumerate()
                                                        {
                                                            let expected =
                                                                ((offset + j as u64) % 251) as u8;
                                                            if actual != expected {
                                                                ok = false;
                                                                break;
                                                            }
                                                        }
                                                        if ok {
                                                            offset += n as u64;
                                                            bulk.fetch_add(
                                                                n as u64,
                                                                Ordering::Relaxed,
                                                            );
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        let _ = writer.shutdown();
                                    });
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Dual‑mux server that accepts the bulk stream out‑of‑band as a raw lane
/// stream, then drives a [`mux::DualMessageReceiver`] loop for latency
/// messages. Latency is computed from the embedded send timestamp and
/// pushed to the returned [`UnboundedReceiver`].
pub async fn spawn_dual_msg_channel_server(
    fec: bool,
    base: Instant,
    mode: mux::DeliveryMode,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();

    let listener_bg = Arc::clone(&listener);
    tokio::spawn(async move {
        loop {
            match listener_bg
                .accept_without_handshake_with_mss(fec, rtp::udp::NO_FEC_MSS)
                .await
            {
                Ok(accepted) => {
                    let _ = accept_tx.send(accepted);
                }
                Err(_) => break,
            }
        }
    });

    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::PendingAcceptor>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        while let Some(accepted) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();

            let result = mux::spawn_dual_mux_acceptor(
                reader,
                writer,
                config.clone(),
                Duration::from_secs(3),
            )
            .await;

            match result {
                Ok((_class, nonce, pa)) => {
                    let entries = pending.entry(nonce).or_default();
                    entries.push(pa);
                    if entries.len() == 2 {
                        let pa2 = entries.pop().unwrap();
                        let pa1 = entries.pop().unwrap();
                        pending.remove(&nonce);

                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, mut accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;

                                let bulk = Arc::clone(&bulk);
                                if let Ok((mut reader, writer, _class)) = accepter.accept().await {
                                    tokio::spawn(async move {
                                        let _w = writer;
                                        let mut buf = vec![0u8; 64 * 1024];
                                        let mut offset: u64 = 0;
                                        loop {
                                            match reader.read(&mut buf).await {
                                                Ok(0) | Err(_) => break,
                                                Ok(n) => {
                                                    let mut ok = true;
                                                    for (j, &actual) in buf[..n].iter().enumerate()
                                                    {
                                                        let expected =
                                                            ((offset + j as u64) % 251) as u8;
                                                        if actual != expected {
                                                            ok = false;
                                                            break;
                                                        }
                                                    }
                                                    if ok {
                                                        offset += n as u64;
                                                        bulk.fetch_add(n as u64, Ordering::Relaxed);
                                                    }
                                                }
                                            }
                                        }
                                    });
                                }

                                let mut receiver = mux::DualMessageReceiver::new(accepter, mode);
                                loop {
                                    match receiver.recv().await {
                                        Ok(Some(payload)) => {
                                            if payload.len() >= 12 {
                                                let frame_len = u32::from_le_bytes([
                                                    payload[0], payload[1], payload[2], payload[3],
                                                ])
                                                    as usize;
                                                if frame_len >= 12 && payload.len() >= frame_len {
                                                    let payload_end = frame_len - 8;
                                                    let sent_us = u64::from_le_bytes([
                                                        payload[payload_end],
                                                        payload[payload_end + 1],
                                                        payload[payload_end + 2],
                                                        payload[payload_end + 3],
                                                        payload[payload_end + 4],
                                                        payload[payload_end + 5],
                                                        payload[payload_end + 6],
                                                        payload[payload_end + 7],
                                                    ]);
                                                    let now_us = base.elapsed().as_micros() as u64;
                                                    let latency_ms = now_us.saturating_sub(sent_us)
                                                        as f64
                                                        / 1000.0;
                                                    let _ = tx.send(latency_ms);
                                                }
                                            }
                                        }
                                        Ok(None) => break,
                                        Err(_) => break,
                                    }
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Like [`spawn_dual_mux_latency_bulk_server`] but the accepter is
/// [`mux::MigratingCapableAccepter`] so that streams opened with
/// [`mux::MigratingStreamWriter::open_migrating`] are handled
/// correctly (successor generations arrive through the accept loop).
pub async fn spawn_dual_mux_migrating_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();

    let listener_bg = Arc::clone(&listener);
    tokio::spawn(async move {
        loop {
            match listener_bg
                .accept_without_handshake_with_mss(fec, rtp::udp::NO_FEC_MSS)
                .await
            {
                Ok(accepted) => {
                    let _ = accept_tx.send(accepted);
                }
                Err(_) => break,
            }
        }
    });

    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::PendingAcceptor>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        while let Some(accepted) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();

            let result = mux::spawn_dual_mux_acceptor(
                reader,
                writer,
                config.clone(),
                Duration::from_secs(3),
            )
            .await;

            match result {
                Ok((_class, nonce, pa)) => {
                    let entries = pending.entry(nonce).or_default();
                    entries.push(pa);
                    if entries.len() == 2 {
                        let pa2 = entries.pop().unwrap();
                        let pa1 = entries.pop().unwrap();
                        pending.remove(&nonce);

                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;
                                let mut mac = accepter.into_migrating_capable();
                                loop {
                                    match mac.accept().await {
                                        Ok(mux::AcceptedStream::Migrating {
                                            reader,
                                            writer,
                                            ..
                                        }) => {
                                            let bulk = Arc::clone(&bulk);
                                            let tx = tx.clone();
                                            tokio::spawn(handle_latency_bulk_stream(
                                                reader, writer, base, bulk, tx,
                                            ));
                                        }
                                        Ok(mux::AcceptedStream::Plain {
                                            reader, writer, ..
                                        }) => {
                                            let bulk = Arc::clone(&bulk);
                                            let tx = tx.clone();
                                            tokio::spawn(handle_latency_bulk_stream(
                                                reader, writer, base, bulk, tx,
                                            ));
                                        }
                                        Err(_) => break,
                                    }
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });

    Ok((addr, rx, bulk_delivered))
}

async fn handle_latency_bulk_stream<R: AsyncRead + Unpin + Send + 'static>(
    mut reader: R,
    mut writer: mux::StreamWriter,
    base: Instant,
    bulk: Arc<AtomicU64>,
    tx: mpsc::UnboundedSender<f64>,
) {
    let mut tag = [0u8; 1];
    if reader.read_exact(&mut tag).await.is_err() {
        let _ = writer.shutdown();
        return;
    }
    if tag[0] == b'L' {
        let mut buf = vec![0u8; 64 * 1024];
        let mut offset = 0usize;
        loop {
            let n = match reader.read(&mut buf[offset..]).await {
                Ok(n) => n,
                Err(_) => break,
            };
            if n == 0 {
                break;
            }
            offset += n;
            loop {
                if offset < 4 {
                    break;
                }
                let frame_len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                if frame_len < 12 || offset < frame_len {
                    break;
                }
                let payload_end = frame_len - 8;
                let sent_us = u64::from_le_bytes([
                    buf[payload_end],
                    buf[payload_end + 1],
                    buf[payload_end + 2],
                    buf[payload_end + 3],
                    buf[payload_end + 4],
                    buf[payload_end + 5],
                    buf[payload_end + 6],
                    buf[payload_end + 7],
                ]);
                let now_us = base.elapsed().as_micros() as u64;
                let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                let _ = tx.send(latency_ms);
                buf.copy_within(frame_len..offset, 0);
                offset -= frame_len;
            }
        }
    } else {
        let mut buf = vec![0u8; 64 * 1024];
        let mut offset: u64 = 0;
        loop {
            match reader.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let mut ok = true;
                    for (j, &actual) in buf[..n].iter().enumerate() {
                        let expected = ((offset + j as u64) % 251) as u8;
                        if actual != expected {
                            ok = false;
                            break;
                        }
                    }
                    if ok {
                        offset += n as u64;
                        bulk.fetch_add(n as u64, Ordering::Relaxed);
                    }
                }
            }
        }
    }
    let _ = writer.shutdown();
}

/// Dual‑mux server for the gaming-pattern test: the first stream tagged
/// `b'G'` is the game stream (3 MiB state-sync followed by 200 B delta
/// frames); all other streams are bulk. Uses
/// [`mux::MigratingCapableAccepter`] so it works with both sticky
/// (`open_auto`) and migrating (`open_migrating`) clients.
pub async fn spawn_dual_mux_gaming_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();

    let listener_bg = Arc::clone(&listener);
    tokio::spawn(async move {
        loop {
            match listener_bg
                .accept_without_handshake_with_mss(fec, rtp::udp::NO_FEC_MSS)
                .await
            {
                Ok(accepted) => {
                    let _ = accept_tx.send(accepted);
                }
                Err(_) => break,
            }
        }
    });

    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::PendingAcceptor>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        while let Some(accepted) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();

            let result = mux::spawn_dual_mux_acceptor(
                reader,
                writer,
                config.clone(),
                Duration::from_secs(3),
            )
            .await;

            match result {
                Ok((_class, nonce, pa)) => {
                    let entries = pending.entry(nonce).or_default();
                    entries.push(pa);
                    if entries.len() == 2 {
                        let pa2 = entries.pop().unwrap();
                        let pa1 = entries.pop().unwrap();
                        pending.remove(&nonce);

                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;
                                let mut mac = accepter.into_migrating_capable();
                                loop {
                                    match mac.accept().await {
                                        Ok(mux::AcceptedStream::Migrating {
                                            reader,
                                            writer,
                                            ..
                                        }) => {
                                            let bulk = Arc::clone(&bulk);
                                            let tx = tx.clone();
                                            tokio::spawn(handle_gaming_stream(
                                                reader, writer, base, bulk, tx,
                                            ));
                                        }
                                        Ok(mux::AcceptedStream::Plain {
                                            reader, writer, ..
                                        }) => {
                                            let bulk = Arc::clone(&bulk);
                                            let tx = tx.clone();
                                            tokio::spawn(handle_gaming_stream(
                                                reader, writer, base, bulk, tx,
                                            ));
                                        }
                                        Err(_) => break,
                                    }
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });

    Ok((addr, rx, bulk_delivered))
}

async fn handle_gaming_stream<R: AsyncRead + Unpin + Send + 'static>(
    mut reader: R,
    mut writer: mux::StreamWriter,
    base: Instant,
    bulk: Arc<AtomicU64>,
    tx: mpsc::UnboundedSender<f64>,
) {
    let mut tag = [0u8; 1];
    if reader.read_exact(&mut tag).await.is_err() {
        let _ = writer.shutdown();
        return;
    }
    if tag[0] == b'G' {
        const SYNC_BYTES: usize = 8 * 1024;
        let mut remaining = SYNC_BYTES;
        let mut buf = vec![0u8; 64 * 1024];
        while remaining > 0 {
            let to_read = remaining.min(buf.len());
            match reader.read(&mut buf[..to_read]).await {
                Ok(0) | Err(_) => break,
                Ok(_n) => remaining -= _n,
            };
        }
        let mut offset = 0usize;
        loop {
            let n = match reader.read(&mut buf[offset..]).await {
                Ok(n) => n,
                Err(_) => break,
            };
            if n == 0 {
                break;
            }
            offset += n;
            loop {
                if offset < 4 {
                    break;
                }
                let frame_len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                if frame_len < 12 || offset < frame_len {
                    break;
                }
                let payload_end = frame_len - 8;
                let sent_us = u64::from_le_bytes([
                    buf[payload_end],
                    buf[payload_end + 1],
                    buf[payload_end + 2],
                    buf[payload_end + 3],
                    buf[payload_end + 4],
                    buf[payload_end + 5],
                    buf[payload_end + 6],
                    buf[payload_end + 7],
                ]);
                let now_us = base.elapsed().as_micros() as u64;
                let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                let _ = tx.send(latency_ms);
                buf.copy_within(frame_len..offset, 0);
                offset -= frame_len;
            }
        }
    } else {
        let mut buf = vec![0u8; 64 * 1024];
        let mut offset: u64 = 0;
        loop {
            match reader.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let mut ok = true;
                    for (j, &actual) in buf[..n].iter().enumerate() {
                        let expected = ((offset + j as u64) % 251) as u8;
                        if actual != expected {
                            ok = false;
                            break;
                        }
                    }
                    if ok {
                        offset += n as u64;
                        bulk.fetch_add(n as u64, Ordering::Relaxed);
                    }
                }
            }
        }
    }
    let _ = writer.shutdown();
}

/// Single‑mux gaming server: the first stream tagged `b'G'` is the game
/// stream (3 MiB state-sync followed by 200 B delta frames); all other
/// streams are bulk.
pub async fn spawn_mux_gaming_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, UnboundedReceiver<f64>, Arc<AtomicU64>)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<f64>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(fec, rtp::udp::NO_FEC_MSS, {
        let tx = tx.clone();
        let bulk_delivered = Arc::clone(&bulk_delivered);
        move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            let bulk_delivered = Arc::clone(&bulk_delivered);
            async move {
                let mut tag = [0u8; 1];
                let n = match stream_read.read(&mut tag).await {
                    Ok(n) => n,
                    Err(_) => {
                        let _ = stream_write.shutdown();
                        return;
                    }
                };
                if n == 0 {
                    let _ = stream_write.shutdown();
                    return;
                }
                if tag[0] == b'G' {
                    const SYNC_BYTES: usize = 8 * 1024;
                    let mut remaining = SYNC_BYTES;
                    let mut buf = vec![0u8; 64 * 1024];
                    while remaining > 0 {
                        let to_read = remaining.min(buf.len());
                        match stream_read.read(&mut buf[..to_read]).await {
                            Ok(0) | Err(_) => break,
                            Ok(_n) => remaining -= _n,
                        };
                    }
                    let mut offset = 0usize;
                    loop {
                        let n = match stream_read.read(&mut buf[offset..]).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        offset += n;
                        loop {
                            if offset < 4 {
                                break;
                            }
                            let frame_len =
                                u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                            if frame_len < 12 || offset < frame_len {
                                break;
                            }
                            let payload_end = frame_len - 8;
                            let sent_us = u64::from_le_bytes([
                                buf[payload_end],
                                buf[payload_end + 1],
                                buf[payload_end + 2],
                                buf[payload_end + 3],
                                buf[payload_end + 4],
                                buf[payload_end + 5],
                                buf[payload_end + 6],
                                buf[payload_end + 7],
                            ]);
                            let now_us = base.elapsed().as_micros() as u64;
                            let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                            let _ = tx.send(latency_ms);
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    loop {
                        let n = match stream_read.read(&mut buf).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        let mut ok = true;
                        for (j, &actual) in buf[..n].iter().enumerate() {
                            let expected = ((offset + j as u64) % 251) as u8;
                            if actual != expected {
                                ok = false;
                                break;
                            }
                        }
                        if ok {
                            offset += n as u64;
                            bulk_delivered.fetch_add(n as u64, Ordering::Relaxed);
                        }
                    }
                }
                let _ = stream_write.shutdown();
            }
        }
    })
    .await?;
    Ok((addr, rx, bulk_delivered))
}

/// Connect to a dual‑mux server by opening two RTP connections, writing
/// lane hellos, and spawning mux sessions over each. Returns the dual‑lane
/// facade and the [`JoinSet`] that must be kept alive.
/// Connect a dual-mux client where each lane rides its OWN proxy
/// (`int_proxy_addr` / `bulk_proxy_addr`).  Both proxies share one
/// [`netem_test::SharedShaper`] per direction upstream, so the two lanes
/// funnel through ONE bottleneck capacity — the point of the battery.
pub async fn dual_mux_client_connect(
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> Result<
    (
        mux::DualStreamOpener,
        mux::DualStreamAccepter,
        JoinSet<mux::MuxError>,
    ),
    mux::DualMuxError,
> {
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: false,
    };
    let mut spawner = JoinSet::new();

    let connect = |adr: std::net::SocketAddr, f: bool| async move {
        let (r, w) = rtp_connect(adr, f).await;
        Some((r, w))
    };

    let (opener, accepter) = mux::spawn_dual_mux_connector(
        || connect(int_proxy_addr, fec),
        || connect(bulk_proxy_addr, fec),
        config,
        &mut spawner,
    )
    .await?;

    Ok((opener, accepter, spawner))
}

// ═══════════════════════════════════════════════════════════════════════════════
// SplitMix64 PRNG (tiny, seeded, deterministic)
// ═══════════════════════════════════════════════════════════════════════════════

/// Tiny seeded SplitMix64 PRNG for deterministic traffic models.
#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }

    /// Uniform random usize in `[lo, hi]` (inclusive).
    pub fn uniform_usize(&mut self, lo: usize, hi: usize) -> usize {
        assert!(lo <= hi, "uniform_usize: lo > hi");
        let range = (hi - lo + 1) as u64;
        lo + (self.next_u64() % range) as usize
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery adapter
// ═══════════════════════════════════════════════════════════════════════════════

use rtp::transmission::fec_tuning::FecTuning;
use rtp::transmission::frame_delivery::FrameDelivery;

type FrameSendFut =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<usize, std::io::ErrorKind>> + Send>>;
type FrameRecvFut = std::pin::Pin<
    Box<dyn std::future::Future<Output = Result<Option<Vec<u8>>, std::io::ErrorKind>> + Send>,
>;

/// AsyncWrite adapter over [`rtp::socket::WriteSocket`] that guarantees
/// ONE mux frame = ONE rtp frame.  Each `poll_write` sends the *whole*
/// incoming buffer as a single RTP frame by polling a boxed future that
/// calls [`WriteSocket::send_frame`] directly (no background task / channel),
/// so latency measurement is tied to the actual RTP I/O path.
pub struct RtpFrameDeliveryWriter {
    socket: std::sync::Arc<tokio::sync::Mutex<rtp::socket::WriteSocket>>,
    inflight: Option<(usize, FrameSendFut)>,
}

impl RtpFrameDeliveryWriter {
    fn new(socket: rtp::socket::WriteSocket) -> Self {
        Self {
            socket: std::sync::Arc::new(tokio::sync::Mutex::new(socket)),
            inflight: None,
        }
    }
}

impl std::marker::Unpin for RtpFrameDeliveryWriter {}

impl tokio::io::AsyncWrite for RtpFrameDeliveryWriter {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        let len = buf.len();
        if self.inflight.is_none() {
            let data = buf.to_vec();
            let socket = self.socket.clone();
            self.inflight = Some((
                len,
                Box::pin(async move { socket.lock().await.send_frame(&data).await }),
            ));
        }
        let (sent_len, fut) = self.inflight.as_mut().unwrap();
        debug_assert_eq!(
            *sent_len, len,
            "caller changed buf across Pending poll_write"
        );
        let result = std::task::ready!(fut.as_mut().poll(cx));
        let sent_len = *sent_len;
        self.inflight = None;
        match result {
            Ok(n) if n == sent_len => std::task::Poll::Ready(Ok(n)),
            Ok(_) => std::task::Poll::Ready(Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "RtpFrameDeliveryWriter: partial rtp frame send",
            ))),
            Err(e) => std::task::Poll::Ready(Err(std::io::Error::from(e))),
        }
    }

    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Ready(Ok(()))
    }

    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Ready(Ok(()))
    }
}

/// AsyncRead adapter over [`rtp::socket::ReadSocket`] that yields each
/// delivered RTP frame's bytes in RTP delivery order.  In frame-delivery
/// mode every `recv_frame()` call returns one complete frame (possibly out
/// of order across mux frames when holes exist); this adapter buffers one
/// frame at a time and drains it through [`tokio::io::AsyncRead`].
///
/// `recv_frame()` is polled directly inside `poll_read` (no background
/// task / channel), so latency measurement is tied to the actual RTP I/O
/// path.
pub struct RtpFrameReader {
    socket: std::sync::Arc<rtp::socket::ReadSocket>,
    inflight: Option<FrameRecvFut>,
    buf: Vec<u8>,
    pos: usize,
    eof: bool,
}

impl RtpFrameReader {
    fn new(socket: rtp::socket::ReadSocket) -> Self {
        Self {
            socket: std::sync::Arc::new(socket),
            inflight: None,
            buf: Vec::new(),
            pos: 0,
            eof: false,
        }
    }
}

impl std::marker::Unpin for RtpFrameReader {}

impl tokio::io::AsyncRead for RtpFrameReader {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        target: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        if self.eof {
            return std::task::Poll::Ready(Ok(()));
        }
        if self.pos < self.buf.len() {
            let remain = self.buf.len() - self.pos;
            let n = remain.min(target.remaining());
            target.put_slice(&self.buf[self.pos..self.pos + n]);
            self.pos += n;
            return std::task::Poll::Ready(Ok(()));
        }
        if self.inflight.is_none() {
            let socket = self.socket.clone();
            self.inflight = Some(Box::pin(async move { socket.recv_frame().await }));
        }
        let result = std::task::ready!(self.inflight.as_mut().unwrap().as_mut().poll(cx));
        self.inflight = None;
        match result {
            Ok(Some(frame)) => {
                self.buf = frame;
                self.pos = 0;
                let n = self.buf.len().min(target.remaining());
                target.put_slice(&self.buf[..n]);
                self.pos = n;
                std::task::Poll::Ready(Ok(()))
            }
            Ok(None) => {
                self.eof = true;
                std::task::Poll::Ready(Ok(()))
            }
            Err(e) => std::task::Poll::Ready(Err(std::io::Error::from(e))),
        }
    }
}

/// Connect an rtp client using frame delivery.  Returns the frame-preserving
/// reader and writer adapters that guarantee one-mux-frame-per-one-rtp-frame.
pub async fn rtp_frame_delivery_connect(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_with_mss(proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an rtp client using frame delivery with a custom MSS.
pub async fn rtp_frame_delivery_connect_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    let connected = rtp::udp::connect_with_mss_fec_tuning_and_frame_delivery(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::ConnectConfig {
            log_config: None,
            handshake: false,
            fec,
            mss,
            fec_tuning: FecTuning::default(),
            frame_delivery: FrameDelivery::enabled(),
        },
    )
    .await
    .unwrap();
    (
        RtpFrameReader::new(connected.read),
        RtpFrameDeliveryWriter::new(connected.write),
    )
}

/// Connect a dual-mux client with frame reassembly enabled on both lanes.
/// Each lane rides its own frame-delivery RTP connection.
pub async fn dual_mux_client_connect_frame_reassembly(
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> Result<
    (
        mux::DualStreamOpener,
        mux::DualStreamAccepter,
        JoinSet<mux::MuxError>,
    ),
    mux::DualMuxError,
> {
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: true,
    };
    let mut spawner = JoinSet::new();

    async fn frame_connect(
        addr: std::net::SocketAddr,
        fec: bool,
    ) -> Option<(RtpFrameReader, RtpFrameDeliveryWriter)> {
        let (r, w) = rtp_frame_delivery_connect(addr, fec).await;
        Some((r, w))
    }

    let (opener, accepter) = mux::spawn_dual_mux_connector(
        || frame_connect(int_proxy_addr, fec),
        || frame_connect(bulk_proxy_addr, fec),
        config,
        &mut spawner,
    )
    .await?;

    Ok((opener, accepter, spawner))
}

/// Connect a dual-mux client with per-lane mode flags.
///
/// `interactive_frame` enables frame-reassembly on the interactive lane;
/// `bulk_frame` enables frame-reassembly on the bulk lane.  When a lane's
/// flag is true its RTP connection uses frame delivery; when false the
/// stock byte-stream RTP connection is used.
pub async fn dual_mux_client_connect_with_lane_modes(
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
    interactive_frame: bool,
    bulk_frame: bool,
) -> Result<
    (
        mux::DualStreamOpener,
        mux::DualStreamAccepter,
        JoinSet<mux::MuxError>,
    ),
    mux::DualMuxError,
> {
    let int_config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: interactive_frame,
    };
    let bulk_config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: bulk_frame,
    };

    type BoxedRead = Box<dyn tokio::io::AsyncRead + Unpin + Send>;
    type BoxedWrite = Box<dyn tokio::io::AsyncWrite + Unpin + Send>;

    async fn connect_lane(
        addr: std::net::SocketAddr,
        fec: bool,
        frame: bool,
    ) -> Option<(BoxedRead, BoxedWrite)> {
        if frame {
            let (r, w) = rtp_frame_delivery_connect(addr, fec).await;
            Some((Box::new(r), Box::new(w)))
        } else {
            let (r, w) = rtp_connect(addr, fec).await;
            Some((Box::new(r), Box::new(w)))
        }
    }

    let mut super_spawner = JoinSet::new();

    let nonce = mux::PairingNonce::generate();

    let Some((int_reader, mut int_writer)) =
        connect_lane(int_proxy_addr, fec, interactive_frame).await
    else {
        return Err(mux::DualMuxError::LaneHello(mux::LaneHelloError::Io(
            std::io::ErrorKind::ConnectionRefused,
        )));
    };
    mux::write_lane_hello(&mut int_writer, mux::LaneClass::Interactive, nonce)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let Some((bulk_reader, mut bulk_writer)) = connect_lane(bulk_proxy_addr, fec, bulk_frame).await
    else {
        return Err(mux::DualMuxError::LaneHello(mux::LaneHelloError::Io(
            std::io::ErrorKind::ConnectionRefused,
        )));
    };
    mux::write_lane_hello(&mut bulk_writer, mux::LaneClass::Bulk, nonce)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let mut int_spawner = JoinSet::new();
    let (int_opener, int_accepter) =
        mux::spawn_mux_no_reconnection(int_reader, int_writer, int_config, &mut int_spawner);
    let mut bulk_spawner = JoinSet::new();
    let (bulk_opener, bulk_accepter) =
        mux::spawn_mux_no_reconnection(bulk_reader, bulk_writer, bulk_config, &mut bulk_spawner);

    let (opener, accepter) = mux::spawn_dual_mux_paired_supervised(
        int_opener,
        int_accepter,
        int_spawner,
        bulk_opener,
        bulk_accepter,
        bulk_spawner,
        &mut super_spawner,
    );

    Ok((opener, accepter, super_spawner))
}

fn spawn_tagged_stream_sink(
    mut reader: impl tokio::io::AsyncRead + Unpin + Send + 'static,
    mut writer: impl tokio::io::AsyncWrite + Unpin + Send + 'static,
    tx: mpsc::UnboundedSender<(u8, f64)>,
    bulk: Arc<AtomicU64>,
    base: Instant,
    is_interactive: bool,
) {
    tokio::spawn(async move {
        let mut tag = [0u8; 1];
        if reader.read_exact(&mut tag).await.is_err() {
            let _ = writer.shutdown().await;
            return;
        }
        let is_latency = is_interactive || tag[0] != b'B';
        if is_latency {
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset = 0usize;
            loop {
                let n = match reader.read(&mut buf[offset..]).await {
                    Ok(n) => n,
                    Err(_) => break,
                };
                if n == 0 {
                    break;
                }
                offset += n;
                loop {
                    if offset < 4 {
                        break;
                    }
                    let frame_len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                    if frame_len < 12 {
                        break;
                    }
                    if offset < frame_len {
                        break;
                    }
                    let payload_end = frame_len - 8;
                    let sent_us = u64::from_le_bytes([
                        buf[payload_end],
                        buf[payload_end + 1],
                        buf[payload_end + 2],
                        buf[payload_end + 3],
                        buf[payload_end + 4],
                        buf[payload_end + 5],
                        buf[payload_end + 6],
                        buf[payload_end + 7],
                    ]);
                    let now_us = base.elapsed().as_micros() as u64;
                    let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                    let _ = tx.send((tag[0], latency_ms));
                    buf.copy_within(frame_len..offset, 0);
                    offset -= frame_len;
                }
            }
        } else {
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset: u64 = 0;
            loop {
                match reader.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let mut ok = true;
                        for (j, &actual) in buf[..n].iter().enumerate() {
                            let expected = ((offset + j as u64) % 251) as u8;
                            if actual != expected {
                                ok = false;
                                break;
                            }
                        }
                        if ok {
                            offset += n as u64;
                            bulk.fetch_add(n as u64, Ordering::Relaxed);
                        }
                    }
                }
            }
        }
        let _ = writer.shutdown().await;
    });
}

// ─────────────── dual‑lane frame‑delivery server helpers ───────────────

/// Dual‑mux latency‑bulk server with frame‑reassembly enabled.
///
/// Like [`spawn_dual_mux_latency_bulk_server`] but the server’s mux sessions
/// use `frame_reassembly: true` so that extended‑data frames from a
/// frame‑delivery RTP connection are reassembled correctly.
pub async fn spawn_dual_mux_frame_delivery_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    spawn_dual_mux_latency_bulk_server_with_config(
        fec,
        base,
        mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: true,
        },
    )
    .await
}

/// Dual‑mux latency‑bulk server with per‑lane mode flags.
///
/// `interactive_frame` enables frame‑reassembly on the interactive lane’s
/// mux session; `bulk_frame` does the same for the bulk lane.
pub async fn spawn_dual_mux_latency_bulk_server_with_lane_modes(
    fec: bool,
    base: Instant,
    interactive_frame: bool,
    bulk_frame: bool,
) -> std::io::Result<(
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let int_config = mux::MuxConfig {
        initiation: mux::Initiation::Server,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: interactive_frame,
    };
    let bulk_config = mux::MuxConfig {
        initiation: mux::Initiation::Server,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: bulk_frame,
    };
    spawn_dual_mux_latency_bulk_server_with_per_lane_configs(fec, base, int_config, bulk_config)
        .await
}

/// Shared implementation: dual‑mux server with per‑lane [`mux::MuxConfig`]s.
async fn spawn_dual_mux_latency_bulk_server_with_config(
    fec: bool,
    base: Instant,
    config: mux::MuxConfig,
) -> std::io::Result<(
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    spawn_dual_mux_latency_bulk_server_with_per_lane_configs(fec, base, config.clone(), config)
        .await
}

async fn spawn_dual_mux_latency_bulk_server_with_per_lane_configs(
    fec: bool,
    base: Instant,
    int_config: mux::MuxConfig,
    bulk_config: mux::MuxConfig,
) -> std::io::Result<(
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<(u8, f64)>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();

    let listener_bg = Arc::clone(&listener);
    tokio::spawn(async move {
        loop {
            match listener_bg
                .accept_without_handshake_with_mss(fec, rtp::udp::NO_FEC_MSS)
                .await
            {
                Ok(accepted) => {
                    let _ = accept_tx.send(accepted);
                }
                Err(_) => break,
            }
        }
    });

    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<(mux::PendingAcceptor, mux::MuxConfig)>> =
            HashMap::new();

        while let Some(accepted) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();

            let result = mux::spawn_dual_mux_acceptor(
                reader,
                writer,
                int_config.clone(),
                Duration::from_secs(3),
            )
            .await;

            match result {
                Ok((class, nonce, pa)) => {
                    let cfg = match class {
                        mux::LaneClass::Interactive => int_config.clone(),
                        mux::LaneClass::Bulk => bulk_config.clone(),
                    };
                    let entries = pending.entry(nonce).or_default();
                    entries.push((pa, cfg));
                    if entries.len() == 2 {
                        let (pa2, cfg2) = entries.pop().unwrap();
                        let (pa1, cfg1) = entries.pop().unwrap();
                        pending.remove(&nonce);

                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, mut accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;
                                let _cfg1 = cfg1;
                                let _cfg2 = cfg2;
                                while let Ok((mut reader, mut writer, _class)) =
                                    accepter.accept().await
                                {
                                    let bulk = Arc::clone(&bulk);
                                    let tx = tx.clone();
                                    tokio::spawn(async move {
                                        let mut tag = [0u8; 1];
                                        if reader.read_exact(&mut tag).await.is_err() {
                                            let _ = writer.shutdown();
                                            return;
                                        }
                                        if tag[0] == b'L' {
                                            let mut buf = vec![0u8; 64 * 1024];
                                            let mut offset = 0usize;
                                            loop {
                                                let n = match reader.read(&mut buf[offset..]).await
                                                {
                                                    Ok(n) => n,
                                                    Err(_) => break,
                                                };
                                                if n == 0 {
                                                    break;
                                                }
                                                offset += n;
                                                loop {
                                                    if offset < 4 {
                                                        break;
                                                    }
                                                    let frame_len = u32::from_le_bytes([
                                                        buf[0], buf[1], buf[2], buf[3],
                                                    ])
                                                        as usize;
                                                    if frame_len < 12 {
                                                        break;
                                                    }
                                                    if offset < frame_len {
                                                        break;
                                                    }
                                                    let payload_end = frame_len - 8;
                                                    let sent_us = u64::from_le_bytes([
                                                        buf[payload_end],
                                                        buf[payload_end + 1],
                                                        buf[payload_end + 2],
                                                        buf[payload_end + 3],
                                                        buf[payload_end + 4],
                                                        buf[payload_end + 5],
                                                        buf[payload_end + 6],
                                                        buf[payload_end + 7],
                                                    ]);
                                                    let now_us = base.elapsed().as_micros() as u64;
                                                    let latency_ms = now_us.saturating_sub(sent_us)
                                                        as f64
                                                        / 1000.0;
                                                    let _ = tx.send((tag[0], latency_ms));
                                                    buf.copy_within(frame_len..offset, 0);
                                                    offset -= frame_len;
                                                }
                                            }
                                        } else {
                                            let mut buf = vec![0u8; 64 * 1024];
                                            let mut offset: u64 = 0;
                                            loop {
                                                match reader.read(&mut buf).await {
                                                    Ok(0) | Err(_) => break,
                                                    Ok(n) => {
                                                        let mut ok = true;
                                                        for (j, &actual) in
                                                            buf[..n].iter().enumerate()
                                                        {
                                                            let expected =
                                                                ((offset + j as u64) % 251) as u8;
                                                            if actual != expected {
                                                                ok = false;
                                                                break;
                                                            }
                                                        }
                                                        if ok {
                                                            offset += n as u64;
                                                            bulk.fetch_add(
                                                                n as u64,
                                                                Ordering::Relaxed,
                                                            );
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        let _ = writer.shutdown();
                                    });
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Dual-lane latency-bulk server with two listeners. Continuously pumps each
/// listener's accept loop and applies per-lane frame delivery at accept time,
/// then spawns the mux handshake in the pairing loop.
///
/// Returns `(int_addr, bulk_addr, lat_rx, bulk_counter)` so the client can
/// connect each lane to its dedicated listener.
pub async fn spawn_dual_mux_latency_bulk_server_two_listeners(
    fec: bool,
    base: Instant,
    interactive_frame: bool,
    bulk_frame: bool,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let int_listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let bulk_listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let int_addr = int_listener.local_addr();
    let bulk_addr = bulk_listener.local_addr();
    let (tx, rx) = mpsc::unbounded_channel::<(u8, f64)>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let (accept_tx, mut accept_rx) = mpsc::unbounded_channel();
    for (listener, lane_frame) in [
        (int_listener, interactive_frame),
        (bulk_listener, bulk_frame),
    ] {
        let accept_tx = accept_tx.clone();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: lane_frame,
        };
        tokio::spawn(async move {
            loop {
                let fd = if lane_frame {
                    FrameDelivery::enabled()
                } else {
                    FrameDelivery::default()
                };
                match listener
                    .accept_without_handshake_with_mss_fec_tuning_and_frame_delivery(
                        fec,
                        rtp::udp::NO_FEC_MSS,
                        FecTuning::default(),
                        fd,
                    )
                    .await
                {
                    Ok(accepted) => {
                        if accept_tx.send((accepted, config.clone())).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::PendingAcceptor>> = HashMap::new();
        while let Some((accepted, config)) = accept_rx.recv().await {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            match mux::spawn_dual_mux_acceptor(reader, writer, config, Duration::from_secs(3)).await
            {
                Ok((_class, nonce, pa)) => {
                    let entries = pending.entry(nonce).or_default();
                    entries.push(pa);
                    if entries.len() == 2 {
                        let pa2 = entries.pop().unwrap();
                        let pa1 = entries.pop().unwrap();
                        pending.remove(&nonce);
                        let mut pair_spawner = JoinSet::new();
                        if let Ok((_opener, mut accepter)) =
                            mux::complete_pairing(pa1, pa2, &mut pair_spawner)
                        {
                            let bulk = Arc::clone(&bulk_for_main);
                            let tx = tx.clone();
                            tokio::spawn(async move {
                                let _spawner = pair_spawner;
                                while let Ok((reader, writer, class)) = accepter.accept().await {
                                    spawn_tagged_stream_sink(
                                        reader,
                                        writer,
                                        tx.clone(),
                                        Arc::clone(&bulk),
                                        base,
                                        class == mux::LaneClass::Interactive,
                                    );
                                }
                            });
                        }
                    }
                }
                Err(_) => {}
            }
        }
    });
    Ok((int_addr, bulk_addr, rx, bulk_delivered))
}

/// Spawn a frame-delivery RTP server that accepts one connection, wraps it
/// in a frame-reassembly mux server, and handles latency/bulk streams.
/// Returns `(addr, lat_rx, bulk_counter)` like [`spawn_mux_latency_bulk_server`]
/// but the server uses `frame_reassembly: true` and each RTP connection is
/// accepted in frame-delivery mode.
pub async fn spawn_mux_frame_delivery_latency_bulk_server(
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    UnboundedReceiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<(u8, f64)>();
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();

    let fd = FrameDelivery::enabled();
    let listener_accept = Arc::clone(&listener);
    let bulk_delivered_for_server = Arc::clone(&bulk_delivered);
    tokio::spawn(async move {
        let accepted = match listener_accept
            .accept_without_handshake_with_mss_fec_tuning_and_frame_delivery(
                fec,
                rtp::udp::NO_FEC_MSS,
                FecTuning::default(),
                fd,
            )
            .await
        {
            Ok(a) => a,
            Err(_) => return,
        };
        tokio::spawn({
            let listener = Arc::clone(&listener);
            async move {
                loop {
                    if listener
                        .accept_without_handshake_with_mss_fec_tuning_and_frame_delivery(
                            fec,
                            rtp::udp::NO_FEC_MSS,
                            FecTuning::default(),
                            fd,
                        )
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            }
        });

        let read = accepted.read.into_async_read();
        let write = accepted.write.into_async_write();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: true,
        };
        let mut spawner = JoinSet::new();
        let (_opener, mut accepter) =
            mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

        while let Ok((mut reader, mut writer)) = accepter.accept().await {
            let tx = tx.clone();
            let bulk = Arc::clone(&bulk_delivered_for_server);
            tokio::spawn(async move {
                let mut tag = [0u8; 1];
                if reader.read_exact(&mut tag).await.is_err() {
                    let _ = writer.shutdown();
                    return;
                }
                if tag[0] != b'B' {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset = 0usize;
                    loop {
                        let n = match reader.read(&mut buf[offset..]).await {
                            Ok(n) => n,
                            Err(_) => break,
                        };
                        if n == 0 {
                            break;
                        }
                        offset += n;
                        loop {
                            if offset < 4 {
                                break;
                            }
                            let frame_len =
                                u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
                            if frame_len < 12 || offset < frame_len {
                                break;
                            }
                            let payload_end = frame_len - 8;
                            let sent_us = u64::from_le_bytes([
                                buf[payload_end],
                                buf[payload_end + 1],
                                buf[payload_end + 2],
                                buf[payload_end + 3],
                                buf[payload_end + 4],
                                buf[payload_end + 5],
                                buf[payload_end + 6],
                                buf[payload_end + 7],
                            ]);
                            let now_us = base.elapsed().as_micros() as u64;
                            let latency_ms = now_us.saturating_sub(sent_us) as f64 / 1000.0;
                            let _ = tx.send((tag[0], latency_ms));
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    loop {
                        match reader.read(&mut buf).await {
                            Ok(0) | Err(_) => break,
                            Ok(n) => {
                                let mut ok = true;
                                for (j, &actual) in buf[..n].iter().enumerate() {
                                    let expected = ((offset + j as u64) % 251) as u8;
                                    if actual != expected {
                                        ok = false;
                                        break;
                                    }
                                }
                                if ok {
                                    offset += n as u64;
                                    bulk.fetch_add(n as u64, Ordering::Relaxed);
                                }
                            }
                        }
                    }
                }
                let _ = writer.shutdown();
            });
        }
    });

    Ok((addr, rx, bulk_delivered))
}
