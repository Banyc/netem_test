//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.

#![allow(dead_code)]

use std::{
    future::Future,
    sync::Arc,
    time::{Duration, Instant},
};

use netem_test::{NetemConfig, NetemPair, Stats};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
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
/// and `8.8.8.8` from this machine: ~15% loss, ~500 ms latency, ~500 ms
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

// ─────────────────────────── deterministic payload ───────────────────────

/// Generate a deterministic payload of `n` bytes. The modulus is a prime so
/// the byte pattern is non-trivial and reproducible across runs.
pub fn payload(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i % 251) as u8).collect()
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
async fn spawn_mux_over_rtp_server_with_mss<F, Fut>(
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
                        if listener.accept_without_handshake_with_mss(fec, mss).await.is_err() {
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
    let addr = spawn_mux_over_rtp_server_with_mss(
        fec,
        mss,
        move |mut stream_read, mut stream_write| {
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
