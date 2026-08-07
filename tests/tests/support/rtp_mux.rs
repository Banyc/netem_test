// ═══════════════════════════════════════════════════════════════════════════════
// rtp_mux helpers
// ═══════════════════════════════════════════════════════════════════════════════

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::mpsc;

use crate::support::{LATENCY_SAMPLE_CAPACITY, try_send_observation};

/// Spawn an rtp_mux server with interactive and bulk listeners. Accepted
/// streams are classified by lane class: interactive-lane streams are treated
/// as timestamped latency streams; bulk-lane streams are treated as
/// deterministic byte sinks.
///
/// The returned last element is the shared session scope: session futures
/// spawned by the `SessionSpawner` and the per-stream sink tasks all land in
/// this `JoinSet`. The caller must keep the `Arc` alive for the server's
/// lifetime and drops it to abort those tasks.
pub async fn spawn_rtp_mux_latency_bulk_server(
    tasks: &mut tokio::task::JoinSet<()>,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
    Arc<std::sync::Mutex<tokio::task::JoinSet<()>>>,
)> {
    let server = rtp_mux::RtpMuxServer::bind("127.0.0.1:0", fec).await?;
    let interactive_addr = server.listener().local_addr();
    let bulk_addr = server.bulk_listener().local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let bulk_for_server = Arc::clone(&bulk_delivered);
    let streams: Arc<std::sync::Mutex<tokio::task::JoinSet<()>>> = Arc::default();
    tasks.spawn({
        let streams = Arc::clone(&streams);
        async move {
            let spawner = rtp_mux::SessionSpawner::new({
                let streams = Arc::clone(&streams);
                move |fut| {
                    let mut set = streams.lock().unwrap();
                    set.spawn(fut);
                }
            });
            let _ = server
                .serve(spawner, move |stream| {
                    let source_lane = stream.source_lane();
                    let (reader, writer) = tokio::io::split(stream);
                    spawn_tagged_stream_sink(
                        &streams,
                        reader,
                        writer,
                        tx.clone(),
                        Arc::clone(&bulk_for_server),
                        base,
                        source_lane == mux::LaneClass::Interactive,
                    );
                })
                .await;
        }
    });
    Ok((interactive_addr, bulk_addr, rx, bulk_delivered, streams))
}

pub fn rtp_mux_connector(
    tasks: &mut tokio::task::JoinSet<()>,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> rtp_mux::RtpMuxConnector {
    let bind: rtp_mux::BindSelector = Arc::new(|addr| match addr {
        std::net::SocketAddr::V4(_) => "0.0.0.0:0".parse().unwrap(),
        std::net::SocketAddr::V6(_) => "[::]:0".parse().unwrap(),
    });
    let bulk_addr: rtp_mux::BulkAddrSelector = Arc::new(move |_| Ok(bulk_proxy_addr));
    let (connector, driver) =
        rtp_mux::RtpMuxConnector::with_config(rtp_mux::RtpMuxConnectorConfig {
            bind,
            bulk_addr,
            fec,
            explorer: rtp_mux::ExplorerConfig {
                enabled: false,
                ..rtp_mux::ExplorerConfig::default()
            },
        });
    tasks.spawn(driver);
    connector
}

pub fn spawn_tagged_stream_sink(
    streams: &Arc<std::sync::Mutex<tokio::task::JoinSet<()>>>,
    mut reader: impl tokio::io::AsyncRead + Unpin + Send + 'static,
    mut writer: impl tokio::io::AsyncWrite + Unpin + Send + 'static,
    tx: mpsc::Sender<(u8, f64)>,
    bulk: Arc<AtomicU64>,
    base: Instant,
    is_interactive: bool,
) {
    let streams = Arc::clone(streams);
    streams.lock().unwrap().spawn(async move {
        let mut tag = [0u8; 1];
        if reader.read_exact(&mut tag).await.is_err() {
            let _ = writer.shutdown().await;
            return;
        }
        let is_latency = is_interactive || tag[0] != b'B';
        if is_latency {
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
        let _ = writer.shutdown().await;
    });
}
