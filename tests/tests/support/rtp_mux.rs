// ═══════════════════════════════════════════════════════════════════════════════
// rtp_mux helpers
// ═══════════════════════════════════════════════════════════════════════════════

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::mpsc;

use crate::support::{
    LATENCY_SAMPLE_CAPACITY, TestScope, TestTask, submit_test_task, submit_test_task_required,
    try_send_observation,
};

/// Shared core for [`spawn_rtp_mux_latency_bulk_server`] and its `_via`
/// variant: binds the server and hands the required serve loop to
/// `spawn_required` (either a [`TestScope`] spawn or the bounded reaper
/// submission). `task_tx` is the bounded submission channel that the serve
/// loop uses to submit session and per-stream sink tasks; it is returned so
/// callers can keep it alive and submit more.
async fn spawn_rtp_mux_latency_bulk_server_core(
    spawn_required: impl FnOnce(&'static str, TestTask),
    task_tx: mpsc::Sender<TestTask>,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
    mpsc::Sender<TestTask>,
)> {
    let server = rtp_mux::RtpMuxServer::bind("127.0.0.1:0", fec).await?;
    let interactive_addr = server.listener().local_addr();
    let bulk_addr = server.bulk_listener().local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let bulk_for_server = Arc::clone(&bulk_delivered);
    // The serve loop must stay alive for the whole test body; an early exit
    // fails the test instead of silently tearing down the server.
    spawn_required("rtp_mux server serve loop", {
        let task_tx = task_tx.clone();
        Box::pin(async move {
            let spawner = rtp_mux::SessionSpawner::new({
                let task_tx = task_tx.clone();
                move |fut| {
                    submit_test_task(&task_tx, fut);
                }
            });
            let _ = server
                .serve(spawner, move |stream| {
                    let source_lane = stream.source_lane();
                    let (reader, writer) = tokio::io::split(stream);
                    spawn_tagged_stream_sink(
                        &task_tx,
                        reader,
                        writer,
                        tx.clone(),
                        Arc::clone(&bulk_for_server),
                        base,
                        source_lane == mux::LaneClass::Interactive,
                    );
                })
                .await;
        })
    });
    Ok((interactive_addr, bulk_addr, rx, bulk_delivered, task_tx))
}

/// Spawn an rtp_mux server with interactive and bulk listeners. Accepted
/// streams are classified by lane class: interactive-lane streams are treated
/// as timestamped latency streams; bulk-lane streams are treated as
/// deterministic byte sinks.
///
/// Session futures spawned by the `SessionSpawner` and the per-stream sink
/// tasks are submitted through a bounded channel feeding one test-owned
/// reaper (spawned into `tasks`), which selects between submissions and
/// `join_next()` completions and unwraps every completion. The returned
/// sender is the submission channel; keep it alive to hold the channel open
/// and submit additional test tasks.
pub async fn spawn_rtp_mux_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
    mpsc::Sender<TestTask>,
)> {
    let task_tx =
        crate::support::spawn_test_task_reaper(tasks, crate::support::TEST_TASK_QUEUE_BOUND);
    spawn_rtp_mux_latency_bulk_server_core(
        |name, fut| tasks.spawn_required(name, fut),
        task_tx.clone(),
        fec,
        base,
    )
    .await
}

/// [`spawn_rtp_mux_latency_bulk_server`] through the bounded task-submission
/// handle, for use inside [`TestScope::run`] bodies where `&mut TestScope`
/// is unavailable. The serve loop is submitted as required through the
/// handle; the returned sender is a clone of the caller's submission handle.
pub async fn spawn_rtp_mux_latency_bulk_server_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
    mpsc::Sender<TestTask>,
)> {
    spawn_rtp_mux_latency_bulk_server_core(
        |name, fut| submit_test_task_required(tx, name, fut),
        tx.clone(),
        fec,
        base,
    )
    .await
}

/// Shared core for [`rtp_mux_connector`] and its `_via` variant: builds the
/// connector and hands the required driver future to `spawn_required` (either
/// a [`TestScope`] spawn or the bounded reaper submission).
fn rtp_mux_connector_core(
    spawn_required: impl FnOnce(&'static str, TestTask),
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
    // The connector driver must stay alive for the whole test body; an early
    // exit would silently stall every connect/redial through the connector.
    spawn_required("rtp_mux connector driver", Box::pin(driver));
    connector
}

pub fn rtp_mux_connector(
    tasks: &mut TestScope,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> rtp_mux::RtpMuxConnector {
    rtp_mux_connector_core(
        |name, fut| tasks.spawn_required(name, fut),
        bulk_proxy_addr,
        fec,
    )
}

/// [`rtp_mux_connector`] through the bounded task-submission handle, for use
/// inside [`TestScope::run`] bodies where `&mut TestScope` is unavailable.
/// The connector driver is submitted as required through the handle.
pub fn rtp_mux_connector_via(
    tx: &tokio::sync::mpsc::Sender<TestTask>,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> rtp_mux::RtpMuxConnector {
    rtp_mux_connector_core(
        |name, fut| submit_test_task_required(tx, name, fut),
        bulk_proxy_addr,
        fec,
    )
}

pub fn spawn_tagged_stream_sink(
    task_tx: &mpsc::Sender<TestTask>,
    mut reader: impl tokio::io::AsyncRead + Unpin + Send + 'static,
    mut writer: impl tokio::io::AsyncWrite + Unpin + Send + 'static,
    tx: mpsc::Sender<(u8, f64)>,
    bulk: Arc<AtomicU64>,
    base: Instant,
    is_interactive: bool,
) {
    let task_tx = task_tx.clone();
    submit_test_task(
        &task_tx,
        Box::pin(async move {
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
        }),
    );
}
