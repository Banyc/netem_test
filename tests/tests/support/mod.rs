//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.

use std::time::{Duration, Instant};

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
pub async fn spawn_rtp_echo_server(fec: bool) -> std::io::Result<std::net::SocketAddr> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    tokio::spawn(async move {
        loop {
            let accepted = match listener.accept_without_handshake(fec).await {
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
    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr.to_string(), None, fec)
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
/// echoed back. Returns the rtp server's listening address.
pub async fn spawn_mux_over_rtp_echo_server(fec: bool) -> std::io::Result<std::net::SocketAddr> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    tokio::spawn(async move {
        let accepted = match listener.accept_without_handshake(fec).await {
            Ok(a) => a,
            Err(_) => return,
        };
        let read = accepted.read.into_async_read();
        let write = accepted.write.into_async_write();

        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
        };
        let mut spawner = tokio::task::JoinSet::new();
        let (_opener, mut accepter) =
            mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

        while let Ok((mut stream_read, mut stream_write)) = accepter.accept().await {
            tokio::spawn(async move {
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
            });
        }
    });
    Ok(addr)
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
pub async fn mux_echo_round_trip(opener: &mux::StreamOpener, payload: &[u8]) -> Vec<u8> {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    stream_write.write_all(payload).await.unwrap();
    stream_write.shutdown().unwrap();
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
}

/// Open a mux stream, write `payload`, shut the stream down, read the full
/// echo back, and return `(received, elapsed)` where `elapsed` is measured
/// from just before the write to the completion of the read. Used by perf
/// and latency tests to print throughput with `--nocapture`.
pub async fn mux_timed_echo_round_trip(
    opener: &mux::StreamOpener,
    payload: &[u8],
) -> (Vec<u8>, Duration) {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    let start = Instant::now();
    stream_write.write_all(payload).await.unwrap();
    stream_write.shutdown().unwrap();
    let mut got = Vec::new();
    let mut buf = vec![0u8; 8 * 1024];
    loop {
        match stream_read.read(&mut buf).await {
            Ok(0) => break,
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) => panic!("mux stream read failed: {e:?}"),
        }
    }
    (got, start.elapsed())
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
