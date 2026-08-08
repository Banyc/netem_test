// ─────────────────────────── mux-over-rtp helpers ────────────────────────

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::task::JoinSet;

use rtp::FecTuning;
use rtp::FrameMode;

use super::stats::SinkProgress;
use crate::support::{LATENCY_SAMPLE_CAPACITY, TestScope, try_send_observation};

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
    tasks: &mut TestScope,
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
    tasks.spawn({
        let listener = Arc::clone(&listener);
        async move {
            // First (and only) rtp connection.
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
            // The extra-accept drainer loop keeps driving `udp_listener`'s
            // dispatcher for the server's lifetime: `accept()` both
            // establishes new connections and dispatches packets to existing
            // ones. Without a background accept-loop, the dispatcher stops
            // after the first connection and subsequent datagrams are never
            // forwarded to it, so the reliable layer stalls. The drainer is
            // pinned and selected alongside the session supervisor and
            // handlers below, so an early drainer return ends the server.
            let drainer = {
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
            };
            tokio::pin!(drainer);

            let read = accepted.read.into_async_read();
            let write = accepted.write.into_async_write();
            // The accepted lane's rtp session supervisor owns the session
            // drivers; poll it from the select loop below so a panicked
            // driver terminates the server instead of being silently dropped.
            let supervisor = accepted.supervisor;
            tokio::pin!(supervisor);

            let config = mux::MuxConfig {
                initiation: mux::Initiation::Server,
                heartbeat_interval: Duration::from_secs(5),
                frame_reassembly: false,
            };
            let mut spawner = tokio::task::JoinSet::new();
            let (_opener, mut accepter) =
                mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

            // Per-stream handlers owned by this accept loop's scope. The loop
            // below polls acceptance, per-stream handler completion, and mux
            // session completion together, so a panicked handler or session
            // surfaces immediately instead of only once the accept loop ends;
            // any still-running handlers are aborted when the local JoinSet
            // drops at scope end.
            let mut handlers = tokio::task::JoinSet::new();
            loop {
                tokio::select! {
                    () = &mut supervisor => { break; } // rtp session drivers exited; stop accepting
                    () = &mut drainer => { break; } // extra-accept drainer exited; stop the server
                    accepted = accepter.accept() => {
                        match accepted {
                            Ok((stream_read, stream_write)) => {
                                let handle_stream = &handle_stream;
                                handlers.spawn(handle_stream(stream_read, stream_write));
                            }
                            Err(_) => break, // peer closed; stop accepting
                        }
                    }
                    Some(joined) = handlers.join_next(), if !handlers.is_empty() => {
                        // A per-stream handler ended: unwrap so a panic
                        // surfaces now; a normal completion just ends it.
                        joined.unwrap();
                    }
                    Some(joined) = spawner.join_next() => {
                        // The mux session ended: unwrap (re-raising a panic)
                        // and stop accepting.
                        joined.unwrap();
                        break;
                    }
                }
            }
            // Drain any remaining handler/supervision joins so panics surface.
            while let Some(result) = handlers.join_next().await {
                result.unwrap();
            }
            // Drain the mux supervision tasks, unwrapping so panics surface.
            while let Some(result) = spawner.join_next().await {
                result.unwrap();
            }
        }
    });
    Ok(addr)
}

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// echoed back. Returns the rtp server's listening address.
pub async fn spawn_mux_over_rtp_echo_server_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    mss: usize,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_mux_over_rtp_server_with_mss(
        tasks,
        fec,
        mss,
        |mut stream_read, mut stream_write| async move {
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
        },
    )
    .await
}

/// Spawn a mux-over-RTP echo server using the default MSS.
pub async fn spawn_mux_over_rtp_echo_server(
    tasks: &mut TestScope,
    fec: bool,
) -> std::io::Result<std::net::SocketAddr> {
    spawn_mux_over_rtp_echo_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS).await
}

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// read to EOF into a `Vec<u8>` and sent on the returned channel (capacity
/// 16) if the read succeeded or the buffer is non-empty, then the write half
/// is shut down. Returns the rtp server's listening address and the receiver
/// for completed payloads.
pub async fn spawn_mux_over_rtp_sink_server_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<Vec<u8>>)> {
    let (tx, rx) = tokio::sync::mpsc::channel(16);
    let addr = spawn_mux_over_rtp_server_with_mss(
        tasks,
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
        },
    )
    .await?;
    Ok((addr, rx))
}

/// Spawn a mux-over-RTP sink server using the default MSS.
pub async fn spawn_mux_over_rtp_sink_server(
    tasks: &mut TestScope,
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<Vec<u8>>)> {
    spawn_mux_over_rtp_sink_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS).await
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<f64>)> {
    spawn_mux_msg_latency_sink_with_mss(tasks, fec, base, rtp::udp::NO_FEC_MSS).await
}

/// [`spawn_mux_msg_latency_sink`] with a custom RTP MSS.
pub async fn spawn_mux_msg_latency_sink_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, tokio::sync::mpsc::Receiver<f64>)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let addr = spawn_mux_over_rtp_server_with_mss(
        tasks,
        fec,
        mss,
        move |mut stream_read, mut stream_write| {
            let tx = tx.clone();
            async move {
                let mut buf = vec![0u8; 64 * 1024];
                let mut offset = 0usize;
                while let Ok(n) = stream_read.read(&mut buf[offset..]).await {
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
                        if !try_send_observation(&tx, latency_ms, "latency sample") {
                            break;
                        }
                        buf.copy_within(frame_len..offset, 0);
                        offset -= frame_len;
                    }
                }
                let _ = stream_write.shutdown();
            }
        },
    )
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
/// opener. The mux supervision `JoinSet` is drained by a required scope task:
/// the session must survive the whole test body, a panicked supervision task
/// surfaces immediately, and the session ending before the body completes is
/// a panic. Callers that intentionally end the session mid-body must use an
/// ordinary-spawn drain instead (see `perf_probe`).
pub fn mux_client_connect<R, W>(tasks: &mut TestScope, read: R, write: W) -> mux::StreamOpener
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
    tasks.spawn_required("mux client session", async move {
        if let Some(result) = spawner.join_next().await {
            let err = result.unwrap();
            panic!("mux client session ended before the test body: {err:?}");
        }
    });
    opener
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
    mux_send_repeated(opener, payload, 1).await
}

pub async fn mux_send_repeated(
    opener: &mux::StreamOpener,
    chunk: &[u8],
    repeat: usize,
) -> Duration {
    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();
    let start = Instant::now();
    for _ in 0..repeat {
        stream_write.write_all(chunk).await.unwrap();
    }
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
    tasks: &mut TestScope,
    fec: bool,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, Arc<SinkProgress>)> {
    let progress = Arc::new(SinkProgress::new());
    let addr = spawn_mux_over_rtp_server_with_mss(tasks, fec, mss, {
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
    tasks: &mut TestScope,
    fec: bool,
) -> std::io::Result<(std::net::SocketAddr, Arc<SinkProgress>)> {
    spawn_mux_over_rtp_counting_sink_server(tasks, fec, rtp::udp::NO_FEC_MSS).await
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::Receiver<f64>,
    Arc<AtomicU64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS, {
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
                    while let Ok(n) = stream_read.read(&mut buf[offset..]).await {
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
                            if !try_send_observation(&tx, latency_ms, "latency sample") {
                                break;
                            }
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    // Bulk byte sink: verify the deterministic pattern and
                    // count verified bytes. The tag byte itself is excluded.
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    while let Ok(n) = stream_read.read(&mut buf).await {
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::Receiver<f64>,
    Arc<AtomicU64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(tasks, fec, mss, {
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
                    while let Ok(n) = stream_read.read(&mut buf[offset..]).await {
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
                            if !try_send_observation(&tx, latency_ms, "latency sample") {
                                break;
                            }
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    while let Ok(n) = stream_read.read(&mut buf).await {
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

/// Single‑mux gaming server: the first stream tagged `b'G'` is the game
/// stream (3 MiB state-sync followed by 200 B delta frames); all other
/// streams are bulk.
pub async fn spawn_mux_gaming_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::Receiver<f64>,
    Arc<AtomicU64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let addr = spawn_mux_over_rtp_server_with_mss(tasks, fec, rtp::udp::NO_FEC_MSS, {
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
                    while let Ok(n) = stream_read.read(&mut buf[offset..]).await {
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
                            if !try_send_observation(&tx, latency_ms, "latency sample") {
                                break;
                            }
                            buf.copy_within(frame_len..offset, 0);
                            offset -= frame_len;
                        }
                    }
                } else {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset: u64 = 0;
                    while let Ok(n) = stream_read.read(&mut buf).await {
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

/// Spawn a frame-delivery RTP server that accepts one connection, wraps it
/// in a frame-reassembly mux server, and handles latency/bulk streams.
/// Returns `(addr, lat_rx, bulk_counter)` like [`spawn_mux_latency_bulk_server`]
/// but the server uses `frame_reassembly: true` and each RTP connection is
/// accepted in frame-delivery mode.
pub async fn spawn_mux_frame_delivery_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    tokio::sync::mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let (tx, rx) = tokio::sync::mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();

    let fd = FrameMode::enabled();
    let listener_accept = Arc::clone(&listener);
    let bulk_delivered_for_server = Arc::clone(&bulk_delivered);
    tasks.spawn(async move {
        let accepted = match listener_accept
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,
                mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),
                fec_tuning: FecTuning::default(),
                frame_delivery: fd,
                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            Ok(a) => a,
            Err(_) => return,
        };
        // The extra-accept drainer loop keeps driving `udp_listener`'s
        // dispatcher for the server's lifetime: `accept()` both establishes
        // new connections and dispatches packets to existing ones. Without a
        // background accept-loop, the dispatcher stops after the first
        // connection and subsequent datagrams are never forwarded to it, so
        // the reliable layer stalls. The drainer is pinned and selected
        // alongside the session supervisor and handlers below, so an early
        // drainer return ends the server.
        let drainer = {
            let listener = Arc::clone(&listener);
            async move {
                loop {
                    if listener
                        .accept_without_handshake_with(rtp::udp::AcceptConfig {
                            fec,
                            mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),
                            fec_tuning: FecTuning::default(),
                            frame_delivery: fd,
                            ..rtp::udp::AcceptConfig::default()
                        })
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            }
        };
        tokio::pin!(drainer);

        let read = accepted.read.into_async_read();
        let write = accepted.write.into_async_write();
        // The accepted lane's rtp session supervisor owns the session
        // drivers; poll it from the select loop below so a panicked driver
        // terminates the server instead of being silently dropped.
        let supervisor = accepted.supervisor;
        tokio::pin!(supervisor);
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: true,
        };
        let mut spawner = JoinSet::new();
        let (_opener, mut accepter) =
            mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

        // Per-stream sink tasks owned by this accept loop's scope. The loop
        // below polls acceptance, per-stream handler completion, and mux
        // session completion together, so a panicked handler or session
        // surfaces immediately instead of only once the accept loop ends;
        // any still-running handlers are aborted when this JoinSet drops at
        // scope end.
        let mut handlers = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                () = &mut supervisor => { break; } // rtp session drivers exited; stop accepting
                () = &mut drainer => { break; } // extra-accept drainer exited; stop the server
                accepted = accepter.accept() => {
                    match accepted {
                        Ok((mut reader, mut writer)) => {
                            let tx = tx.clone();
                            let bulk = Arc::clone(&bulk_delivered_for_server);
                            handlers.spawn(async move {
                                let mut tag = [0u8; 1];
                if reader.read_exact(&mut tag).await.is_err() {
                    let _ = writer.shutdown();
                    return;
                }
                if tag[0] != b'B' {
                    let mut buf = vec![0u8; 64 * 1024];
                    let mut offset = 0usize;
                    while let Ok(n) = reader.read(&mut buf[offset..]).await {
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
                            if !try_send_observation(&tx, (tag[0], latency_ms), "latency sample") {
                                break;
                            }
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
                        Err(_) => break, // peer closed; stop accepting
                    }
                }
                Some(joined) = handlers.join_next(), if !handlers.is_empty() => {
                    // A per-stream handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
                Some(joined) = spawner.join_next() => {
                    // The mux session ended: unwrap (re-raising a panic) and
                    // stop accepting.
                    joined.unwrap();
                    break;
                }
            }
        }
        // Drain any remaining handler/supervision joins so panics surface.
        while let Some(result) = handlers.join_next().await {
            result.unwrap();
        }
        // Drain the mux supervision tasks, unwrapping so panics surface.
        while let Some(result) = spawner.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}
