// ─────────────────────────── rtp echo helpers ────────────────────────────

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

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
            let accepted = match listener
                .accept_without_handshake_with(rtp::udp::AcceptConfig {
                    fec,
                    mss: rtp::udp::MssConfig::Custom(mss),
                    ..rtp::udp::AcceptConfig::default()
                })
                .await
            {
                Ok(a) => a,
                Err(_) => return,
            };
            tokio::spawn(async move {
                let mut read = accepted.read.into_async_read();
                let mut write = accepted.write.into_async_write();
                // The supervisor owns the session drivers; dropping it aborts
                // the session, so keep it alive for the echo loop.
                let _supervisor = accepted.supervisor;
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
    rtp::socket::SessionHandle,
) {
    rtp_connect_with_mss(proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an `rtp` client with a custom MSS.
///
/// `mss` is passed to [`rtp::udp::connect_with`] via a custom
/// [`rtp::udp::MssConfig`]; `proxy_client_addr` should be
/// [`NetemPair::client_addr`].
pub async fn rtp_connect_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (
    impl AsyncRead + Unpin + Send,
    impl AsyncWrite + Unpin + Send,
    rtp::socket::SessionHandle,
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
    (
        connected.read.into_async_read(),
        connected.write.into_async_write(),
        // The supervisor owns the session drivers; dropping it aborts the
        // session, so return it for the caller to hold while the halves are
        // in use.
        connected.supervisor,
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
            let accepted = match listener
                .accept_without_handshake_with(rtp::udp::AcceptConfig {
                    fec,
                    mss: rtp::udp::MssConfig::Custom(mss),
                    ..rtp::udp::AcceptConfig::default()
                })
                .await
            {
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
                        .accept_without_handshake_with(rtp::udp::AcceptConfig {
                            fec,
                            mss: rtp::udp::MssConfig::Custom(mss),
                            ..rtp::udp::AcceptConfig::default()
                        })
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
            // The supervisor owns the session drivers; keep it alive.
            let _supervisor = accepted.supervisor;
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
/// length][payload][8-byte LE send timestamp in micros since `base`]`.
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
            let accepted = match listener
                .accept_without_handshake_with(rtp::udp::AcceptConfig {
                    fec,
                    mss: rtp::udp::MssConfig::Custom(mss),
                    ..rtp::udp::AcceptConfig::default()
                })
                .await
            {
                Ok(a) => a,
                Err(_) => return,
            };
            let listener = Arc::clone(&listener);
            tokio::spawn(async move {
                loop {
                    if listener
                        .accept_without_handshake_with(rtp::udp::AcceptConfig {
                            fec,
                            mss: rtp::udp::MssConfig::Custom(mss),
                            ..rtp::udp::AcceptConfig::default()
                        })
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
            let mut read = accepted.read.into_async_read();
            let _write = accepted.write;
            // The supervisor owns the session drivers; keep it alive.
            let _supervisor = accepted.supervisor;
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
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
    spawn_rtp_bulk_upload_with_mss(proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_rtp_bulk_upload`] with a custom MSS.
pub async fn spawn_rtp_bulk_upload_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
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
    .await?;
    let mut read = connected.read.into_async_read();
    let write = connected.write.into_async_write();
    // The supervisor owns the session drivers; hold it in the keepalive task
    // so the returned write half keeps working.
    let supervisor = connected.supervisor;
    tokio::spawn(async move {
        let _supervisor = supervisor;
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
