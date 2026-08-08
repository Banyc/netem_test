// ─────────────────────────── rtp echo helpers ────────────────────────────

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::support::{
    LATENCY_SAMPLE_CAPACITY, TestScope, TestTask, submit_test_task, submit_test_task_required,
    try_send_observation,
};

/// Shared core for [`spawn_rtp_echo_server_with_mss`] and its `_via`
/// variant: binds the listener and hands the required echo-server future to
/// `spawn_required` (either a [`TestScope`] spawn or the bounded reaper
/// submission).
async fn spawn_rtp_echo_server_core(
    spawn_required: impl FnOnce(&'static str, TestTask),
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    spawn_required(
        "rtp echo server",
        Box::pin(async move {
            let mut handlers = tokio::task::JoinSet::new();
            loop {
                tokio::select! {
                    accepted = listener
                        .accept_without_handshake_with(rtp::udp::AcceptConfig {
                            fec,
                            mss: rtp::udp::MssConfig::Custom(mss),
                            ..rtp::udp::AcceptConfig::default()
                        }) => {
                        let accepted = match accepted {
                            Ok(a) => a,
                            Err(_) => break,
                        };
                        handlers.spawn(async move {
                            let mut read = accepted.read.into_async_read();
                            let mut write = accepted.write.into_async_write();
                            // The supervisor owns the session drivers; poll it from the
                            // echo loop so a panicked driver terminates this handler.
                            let supervisor = accepted.supervisor;
                            tokio::pin!(supervisor);
                            let mut buf = vec![0u8; 8 * 1024];
                            loop {
                                tokio::select! {
                                    () = &mut supervisor => break, // session drivers exited; terminate the echo handler
                                    n = read.read(&mut buf) => {
                                        match n {
                                            Ok(0) => break,
                                            Ok(n) => {
                                                if write.write_all(&buf[..n]).await.is_err() {
                                                    break;
                                                }
                                            }
                                            Err(_) => break,
                                        }
                                    }
                                }
                            }
                            let _ = write.shutdown().await;
                        });
                    }
                    joined = handlers.join_next(), if !handlers.is_empty() => {
                        joined.unwrap().unwrap();
                    }
                }
            }
            while let Some(result) = handlers.join_next().await {
                result.unwrap();
            }
        }),
    );
    Ok(addr)
}

/// Spawn an `rtp` server that accepts one connection and echoes back
/// everything it receives until the peer closes. Returns the server's
/// listening address.
///
/// `mss` controls the RTP maximum segment size passed to
/// [`Listener::accept_without_handshake_with_mss`]. Use [`rtp::udp::NO_FEC_MSS`]
/// for the default size.
///
/// `tasks` (a [`TestScope`]) owns the server and its per-connection echo
/// handlers; the caller must keep it alive for the server's lifetime. The
/// server task is registered as required, so an accept-loop exit before the
/// test body completes fails the test instead of silently tearing down the
/// server.
pub async fn spawn_rtp_echo_server_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_rtp_echo_server_core(|name, fut| tasks.spawn_required(name, fut), fec, mss).await
}

/// Spawn an `rtp` echo server through the bounded task-submission handle,
/// for use inside [`TestScope::run`] bodies where `&mut TestScope` is
/// unavailable. The server task is submitted as required through the handle.
pub async fn spawn_rtp_echo_server_with_mss_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_rtp_echo_server_core(
        |name, fut| submit_test_task_required(tx, name, fut),
        fec,
        mss,
    )
    .await
}

/// Spawn an `rtp` echo server using the default MSS.
pub async fn spawn_rtp_echo_server(
    tasks: &mut TestScope,
    fec: bool,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_rtp_echo_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` echo server using the default MSS through the bounded
/// task-submission handle (for use inside run bodies).
pub async fn spawn_rtp_echo_server_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    fec: bool,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_rtp_echo_server_with_mss_via(tx, fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an `rtp` client to a proxy's client-side address and return the
/// resulting reliable byte-stream halves. Pass [`NetemPair::client_addr`] as
/// `proxy_client_addr`; `fec` must match the server's FEC setting.
///
/// `tasks` owns the rtp session supervisor: it is awaited by a REQUIRED
/// scope task, so the session must stay alive for the whole test body — a
/// session that ends early fails the test. Callers that intentionally tear
/// the connection down mid-body must not use this helper (see
/// `perf_probe::rtp_connect_transient`).
pub async fn rtp_connect(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (
    impl AsyncRead + Unpin + Send + use<>,
    impl AsyncWrite + Unpin + Send + use<>,
) {
    rtp_connect_with_mss(tasks, proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// Connect an `rtp` client with a custom MSS.
///
/// `mss` is passed to [`rtp::udp::connect_with`] via a custom
/// [`rtp::udp::MssConfig`]; `proxy_client_addr` should be
/// [`NetemPair::client_addr`]. The supervisor is awaited by a required scope
/// task like [`rtp_connect`].
pub async fn rtp_connect_with_mss(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (
    impl AsyncRead + Unpin + Send + use<>,
    impl AsyncWrite + Unpin + Send + use<>,
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
    // The supervisor owns the session drivers; poll it from a required scope
    // task so the session ending before the test body completes is a panic.
    tasks.spawn_required("rtp client session", async move {
        let _ = connected.supervisor.await;
    });
    (read, write)
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

/// Shared core for [`spawn_rtp_byte_sink_server_with_mss`] and its `_via`
/// variant: binds the listener and hands the sink-server future to `spawn`
/// (either a [`TestScope`] spawn or the bounded reaper submission).
async fn spawn_rtp_byte_sink_server_core(
    spawn: impl FnOnce(TestTask),
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    let listener = Arc::new(listener);

    let delivered = Arc::new(AtomicU64::new(0));
    let delivered_for_server = Arc::clone(&delivered);
    spawn(Box::pin({
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
            let mut drainer = tokio::task::JoinSet::new();
            drainer.spawn({
                let listener = Arc::clone(&listener);
                async move {
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
                }
            });
            let mut read = accepted.read.into_async_read();
            // Hold the write half alive so the connection stays open while we
            // only receive.
            let _write = accepted.write;
            // The supervisor owns the session drivers; poll it from the read
            // loop so a panicked driver terminates this server.
            let supervisor = accepted.supervisor;
            tokio::pin!(supervisor);
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset: u64 = 0;
            loop {
                tokio::select! {
                    () = &mut supervisor => break, // session drivers exited; terminate the server
                    n = read.read(&mut buf) => {
                        match n {
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
            }
        }
    }));
    Ok((addr, delivered))
}

/// Spawn an `rtp` byte sink server using a custom MSS.
pub async fn spawn_rtp_byte_sink_server_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    spawn_rtp_byte_sink_server_core(|fut| tasks.spawn(fut), fec, mss).await
}

/// Spawn an `rtp` byte sink server through the bounded task-submission
/// handle, for use inside [`TestScope::run`] bodies where `&mut TestScope`
/// is unavailable.
pub async fn spawn_rtp_byte_sink_server_with_mss_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    spawn_rtp_byte_sink_server_core(|fut| submit_test_task(tx, fut), fec, mss).await
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
    tasks: &mut TestScope,
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    spawn_rtp_byte_sink_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` byte sink server using the default MSS through the bounded
/// task-submission handle (for use inside run bodies).
pub async fn spawn_rtp_byte_sink_server_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, Arc<AtomicU64>)> {
    spawn_rtp_byte_sink_server_with_mss_via(tx, fec, rtp::udp::NO_FEC_MSS).await
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<f64>)> {
    spawn_rtp_msg_latency_sink_with_mss(tasks, fec, base, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_rtp_msg_latency_sink`] with a custom RTP MSS.
pub async fn spawn_rtp_msg_latency_sink_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<f64>)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr();
    let listener = Arc::new(listener);

    tasks.spawn({
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
            let mut drainer = tokio::task::JoinSet::new();
            drainer.spawn({
                let listener = Arc::clone(&listener);
                async move {
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
                }
            });
            let mut read = accepted.read.into_async_read();
            let _write = accepted.write;
            // The supervisor owns the session drivers; poll it from the read
            // loop so a panicked driver terminates this server.
            let supervisor = accepted.supervisor;
            tokio::pin!(supervisor);
            let mut buf = vec![0u8; 64 * 1024];
            let mut offset = 0usize;
            loop {
                if offset >= buf.len() {
                    buf.resize(buf.len().saturating_mul(2), 0u8);
                }
                tokio::select! {
                    () = &mut supervisor => break, // session drivers exited; terminate the server
                    n = read.read(&mut buf[offset..]) => {
                        let n = match n {
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
                            if !try_send_observation(&tx, latency_ms, "latency sample") {
                                return;
                            }
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                }
            }
        }
    });
    Ok((addr, rx))
}

/// Shared core for [`spawn_rtp_bulk_upload_with_mss`] and its `_via`
/// variant: opens the connection and hands the read-keepalive future to
/// `spawn` (either a [`TestScope`] spawn or the bounded reaper submission).
async fn spawn_rtp_bulk_upload_core(
    spawn: impl FnOnce(TestTask),
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
    // The supervisor owns the session drivers; poll it from the keepalive
    // task so a panicked driver ends the keepalive.
    let supervisor = connected.supervisor;
    spawn(Box::pin(async move {
        tokio::pin!(supervisor);
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            tokio::select! {
                () = &mut supervisor => break, // session drivers exited; end the keepalive
                n = read.read(&mut buf) => {
                    match n {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                }
            }
        }
    }));
    Ok(write)
}

/// [`spawn_rtp_bulk_upload`] with a custom MSS.
pub async fn spawn_rtp_bulk_upload_with_mss(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
    spawn_rtp_bulk_upload_core(|fut| tasks.spawn(fut), proxy_client_addr, fec, mss).await
}

/// [`spawn_rtp_bulk_upload_with_mss`] through the bounded task-submission
/// handle, for use inside [`TestScope::run`] bodies where `&mut TestScope`
/// is unavailable.
pub async fn spawn_rtp_bulk_upload_with_mss_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
    spawn_rtp_bulk_upload_core(|fut| submit_test_task(tx, fut), proxy_client_addr, fec, mss).await
}

/// Open a fresh `rtp` connection through the proxy and return the async write
/// half. A background task keeps the read half alive so ACKs are processed and
/// the sender does not stall.
///
/// This is the sender side for the live-goodput probes. The caller writes data
/// through the returned write half and drops it (or aborts the writing task)
/// when done; the read-keepalive task exits automatically when the peer closes.
pub async fn spawn_rtp_bulk_upload(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
    spawn_rtp_bulk_upload_with_mss(tasks, proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_rtp_bulk_upload`] through the bounded task-submission handle, for
/// use inside [`TestScope::run`] bodies.
pub async fn spawn_rtp_bulk_upload_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> std::io::Result<rtp::socket::AsyncWriteAdapter> {
    spawn_rtp_bulk_upload_with_mss_via(tx, proxy_client_addr, fec, rtp::udp::NO_FEC_MSS).await
}
