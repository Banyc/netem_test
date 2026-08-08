// ═══════════════════════════════════════════════════════════════════════════════
// Dual‑mux helpers
// ═══════════════════════════════════════════════════════════════════════════════

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::sync::mpsc;
use tokio::task::JoinSet;

use rtp::FecTuning;
use rtp::FrameMode;

use super::frame::rtp_frame_delivery_connect;
use super::rtp::rtp_connect;
use super::rtp_mux::spawn_tagged_stream_sink;
use crate::support::{
    LATENCY_SAMPLE_CAPACITY, TEST_ACCEPT_CAPACITY, TEST_TASK_QUEUE_BOUND, TestScope, TestTask,
    spawn_test_task_reaper, try_send_observation,
};

/// Server that accepts two RTP connections (lane‑hello paired) and handles
/// both latency‑echo (tag byte `b'L'`) and bulk‑sink streams on the paired
/// dual‑lane mux. Returns `(addr, lat_rx, bulk_counter)` like
/// [`spawn_mux_latency_bulk_server`].
pub async fn spawn_dual_mux_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    spawn_dual_mux_latency_bulk_server_with_mss(tasks, fec, base, rtp::udp::NO_FEC_MSS).await
}

/// Like [`spawn_dual_mux_latency_bulk_server`] but with a custom RTP MSS.
pub async fn spawn_dual_mux_sized_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    spawn_dual_mux_latency_bulk_server_with_mss(tasks, fec, base, mss).await
}

async fn spawn_dual_mux_latency_bulk_server_with_mss(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mss: usize,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Parked accept loop (aborted when `tasks` drops at scope end).
    let listener_bg = Arc::clone(&listener);
    tasks.spawn_required("dual-mux server task", async move {
        while let Ok(accepted) = listener_bg
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,
                mss: rtp::udp::MssConfig::Custom(mss),
                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            if accept_tx.send(accepted).await.is_err() {
                break;
            }
        }
    });

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::UnpairedLane>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some(accepted) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });

            let result =
                mux::begin_lane_pairing(reader, writer, config.clone(), Duration::from_secs(3))
                    .await;

            if let Ok((_class, nonce, pa)) = result {
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
                        pair_handlers.spawn(async move {
                            // Per-stream handlers owned by the pair handler's
                            // scope; drained after the accept loop ends so
                            // panics surface.
                            let mut stream_handlers = JoinSet::new();
                            loop {
                                tokio::select! {
                                    accepted = accepter.accept() => {
                                        match accepted {
                                            Ok((mut reader, mut writer, _class)) => {
                                let bulk = Arc::clone(&bulk);
                                let tx = tx.clone();
                                stream_handlers.spawn(async move {
                                    let mut tag = [0u8; 1];
                                    if reader.read_exact(&mut tag).await.is_err() {
                                        let _ = writer.shutdown();
                                        return;
                                    }
                                    if tag[0] == b'L' {
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
                                                let latency_ms =
                                                    now_us.saturating_sub(sent_us) as f64 / 1000.0;
                                                if !try_send_observation(
                                                    &tx,
                                                    latency_ms,
                                                    "latency sample",
                                                ) {
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
                                    }
                                    let _ = writer.shutdown();
                                                });
                                            }
                                            Err(_) => break,
                                        }
                                    }
                                    Some(joined) = stream_handlers.join_next(), if !stream_handlers.is_empty() => {
                                        joined.unwrap();
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            while let Some(result) = stream_handlers.join_next().await {
                                result.unwrap();
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Dual‑mux server that accepts the bulk stream out‑of‑band as a raw lane
/// stream, then drives a [`mux::DualMessageReceiver`] loop for latency
/// messages. Latency is computed from the embedded send timestamp and
/// pushed to the returned [`mpsc::Receiver`].
pub async fn spawn_dual_msg_channel_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    mode: mux::DeliveryMode,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Parked accept loop (aborted when `tasks` drops at scope end).
    let listener_bg = Arc::clone(&listener);
    tasks.spawn_required("dual-mux server task", async move {
        while let Ok(accepted) = listener_bg
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,

                mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),

                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            if accept_tx.send(accepted).await.is_err() {
                break;
            }
        }
    });

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::UnpairedLane>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some(accepted) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });

            let result =
                mux::begin_lane_pairing(reader, writer, config.clone(), Duration::from_secs(3))
                    .await;

            if let Ok((_class, nonce, pa)) = result {
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
                        pair_handlers.spawn(async move {

                            // Per-stream handlers owned by the pair handler's
                            // scope; drained when the pair ends.
                            let mut stream_handlers = JoinSet::new();

                            let bulk = Arc::clone(&bulk);
                            if let Ok((mut reader, writer, _class)) = accepter.accept().await {
                                stream_handlers.spawn(async move {
                                    let _w = writer;
                                    let mut buf = vec![0u8; 64 * 1024];
                                    let mut offset: u64 = 0;
                                    loop {
                                        match reader.read(&mut buf).await {
                                            Ok(0) | Err(_) => break,
                                            Ok(n) => {
                                                let mut ok = true;
                                                for (j, &actual) in buf[..n].iter().enumerate() {
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
                                tokio::select! {
                                    msg = receiver.recv() => {
                                        match msg {
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
                                                let latency_ms =
                                                    now_us.saturating_sub(sent_us) as f64 / 1000.0;
                                                if !try_send_observation(
                                                    &tx,
                                                    latency_ms,
                                                    "latency sample",
                                                ) {
                                                    break;
                                                }
                                            }
                                        }
                                    }
                                    Ok(None) => break,
                                    Err(_) => break,
                                        }
                                    }
                                    Some(joined) = stream_handlers.join_next(), if !stream_handlers.is_empty() => {
                                        joined.unwrap();
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            while let Some(result) = stream_handlers.join_next().await {
                                result.unwrap();
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Like [`spawn_dual_mux_latency_bulk_server`] but the accepter is
/// [`mux::MigratingCapableAccepter`] so that streams opened with
/// [`mux::MigratingStreamWriter::open_migrating`] are handled
/// correctly (successor generations arrive through the accept loop).
pub async fn spawn_dual_mux_migrating_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Parked accept loop (aborted when `tasks` drops at scope end).
    let listener_bg = Arc::clone(&listener);
    tasks.spawn_required("dual-mux server task", async move {
        while let Ok(accepted) = listener_bg
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,

                mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),

                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            if accept_tx.send(accepted).await.is_err() {
                break;
            }
        }
    });

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::UnpairedLane>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some(accepted) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });

            let result =
                mux::begin_lane_pairing(reader, writer, config.clone(), Duration::from_secs(3))
                    .await;

            if let Ok((_class, nonce, pa)) = result {
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
                        pair_handlers.spawn(async move {
                            let mut mac = accepter.into_migrating_capable();
                            // Per-stream handlers owned by the pair handler's
                            // scope; drained after the accept loop ends.
                            let mut stream_handlers = JoinSet::new();
                            loop {
                                tokio::select! {
                                    accepted = mac.accept() => {
                                        match accepted {
                                    Ok(mux::AcceptedStream::Migrating {
                                        reader, writer, ..
                                    }) => {
                                        let bulk = Arc::clone(&bulk);
                                        let tx = tx.clone();
                                        stream_handlers.spawn(handle_latency_bulk_stream(
                                            reader, writer, base, bulk, tx,
                                        ));
                                    }
                                    Ok(mux::AcceptedStream::MigratingDuplex { .. }) => {
                                        unreachable!("duplex accept mode is not used here")
                                    }
                                    Ok(mux::AcceptedStream::Plain { reader, writer, .. }) => {
                                        let bulk = Arc::clone(&bulk);
                                        let tx = tx.clone();
                                        stream_handlers.spawn(handle_latency_bulk_stream(
                                            reader, writer, base, bulk, tx,
                                        ));
                                    }
                                    Err(_) => break,
                                        }
                                    }
                                    Some(joined) = stream_handlers.join_next(), if !stream_handlers.is_empty() => {
                                        joined.unwrap();
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            while let Some(result) = stream_handlers.join_next().await {
                                result.unwrap();
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}

async fn handle_latency_bulk_stream<R: AsyncRead + Unpin + Send + 'static>(
    mut reader: R,
    mut writer: mux::StreamWriter,
    base: Instant,
    bulk: Arc<AtomicU64>,
    tx: mpsc::Sender<f64>,
) {
    let mut tag = [0u8; 1];
    if reader.read_exact(&mut tag).await.is_err() {
        let _ = writer.shutdown();
        return;
    }
    if tag[0] == b'L' {
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(std::net::SocketAddr, mpsc::Receiver<f64>, Arc<AtomicU64>)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Parked accept loop (aborted when `tasks` drops at scope end).
    let listener_bg = Arc::clone(&listener);
    tasks.spawn_required("dual-mux server task", async move {
        while let Ok(accepted) = listener_bg
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,

                mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),

                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            if accept_tx.send(accepted).await.is_err() {
                break;
            }
        }
    });

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::UnpairedLane>> = HashMap::new();
        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
            frame_reassembly: false,
        };

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some(accepted) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });

            let result =
                mux::begin_lane_pairing(reader, writer, config.clone(), Duration::from_secs(3))
                    .await;

            if let Ok((_class, nonce, pa)) = result {
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
                        pair_handlers.spawn(async move {
                            let mut mac = accepter.into_migrating_capable();
                            // Per-stream handlers owned by the pair handler's
                            // scope; drained after the accept loop ends.
                            let mut stream_handlers = JoinSet::new();
                            loop {
                                tokio::select! {
                                    accepted = mac.accept() => {
                                        match accepted {
                                    Ok(mux::AcceptedStream::Migrating {
                                        reader, writer, ..
                                    }) => {
                                        let bulk = Arc::clone(&bulk);
                                        let tx = tx.clone();
                                        stream_handlers.spawn(handle_gaming_stream(
                                            reader, writer, base, bulk, tx,
                                        ));
                                    }
                                    Ok(mux::AcceptedStream::MigratingDuplex { .. }) => {
                                        unreachable!("duplex accept mode is not used here")
                                    }
                                    Ok(mux::AcceptedStream::Plain { reader, writer, .. }) => {
                                        let bulk = Arc::clone(&bulk);
                                        let tx = tx.clone();
                                        stream_handlers.spawn(handle_gaming_stream(
                                            reader, writer, base, bulk, tx,
                                        ));
                                    }
                                    Err(_) => break,
                                        }
                                    }
                                    Some(joined) = stream_handlers.join_next(), if !stream_handlers.is_empty() => {
                                        joined.unwrap();
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            while let Some(result) = stream_handlers.join_next().await {
                                result.unwrap();
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}

async fn handle_gaming_stream<R: AsyncRead + Unpin + Send + 'static>(
    mut reader: R,
    mut writer: mux::StreamWriter,
    base: Instant,
    bulk: Arc<AtomicU64>,
    tx: mpsc::Sender<f64>,
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

/// Connect to a dual‑mux server by opening two RTP connections, writing
/// lane hellos, and spawning mux sessions over each. Returns the dual‑lane
/// facade. The dual‑lane supervision `JoinSet` is drained by a required
/// scope task: the session must survive the whole test body, a panicked
/// lane surfaces immediately, and the session ending before the body
/// completes is a panic.
/// Connect a dual-mux client where each lane rides its OWN proxy
/// (`int_proxy_addr` / `bulk_proxy_addr`).  Both proxies share one
/// [`netem_test::BottleneckShaper`] per direction upstream, so the two lanes
/// funnel through ONE bottleneck capacity — the point of the battery.
pub async fn dual_mux_client_connect(
    tasks: &mut TestScope,
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> Result<(mux::DualStreamOpener, mux::DualStreamAccepter), mux::DualMuxError> {
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: false,
    };
    let nonce = mux::PairingNonce::generate();
    let group = mux::GroupToken::generate();

    let (int_reader, mut int_writer) = rtp_connect(tasks, int_proxy_addr, fec).await;
    mux::write_lane_hello(&mut int_writer, mux::LaneClass::Interactive, nonce, group)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let (bulk_reader, mut bulk_writer) = rtp_connect(tasks, bulk_proxy_addr, fec).await;
    mux::write_lane_hello(&mut bulk_writer, mux::LaneClass::Bulk, nonce, group)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let mut int_spawner = JoinSet::new();
    let (int_opener, int_accepter) =
        mux::spawn_mux_no_reconnection(int_reader, int_writer, config.clone(), &mut int_spawner);
    let mut bulk_spawner = JoinSet::new();
    let (bulk_opener, bulk_accepter) =
        mux::spawn_mux_no_reconnection(bulk_reader, bulk_writer, config.clone(), &mut bulk_spawner);
    let mut super_spawner = JoinSet::new();
    let (opener, accepter) = mux::spawn_dual_mux_paired_supervised(
        int_opener,
        int_accepter,
        int_spawner,
        bulk_opener,
        bulk_accepter,
        bulk_spawner,
        &mut super_spawner,
    );
    // The dual-lane supervision is drained by a required scope task (see the
    // doc comment): a panicked lane surfaces immediately, and the session
    // ending before the test body completes is a panic.
    tasks.spawn_required("dual-mux client session", async move {
        while let Some(result) = super_spawner.join_next().await {
            let err = result.unwrap();
            panic!("dual-mux client session ended before the test body: {err:?}");
        }
    });
    Ok((opener, accepter))
}

/// Connect a dual-mux client with frame reassembly enabled on both lanes.
/// Each lane rides its own frame-delivery RTP connection. The dual-lane
/// supervision `JoinSet` is drained by a required scope task (see
/// [`dual_mux_client_connect`]).
pub async fn dual_mux_client_connect_frame_reassembly(
    tasks: &mut TestScope,
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
) -> Result<(mux::DualStreamOpener, mux::DualStreamAccepter), mux::DualMuxError> {
    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
        frame_reassembly: true,
    };
    let nonce = mux::PairingNonce::generate();
    let group = mux::GroupToken::generate();

    let (int_reader, mut int_writer) = rtp_frame_delivery_connect(tasks, int_proxy_addr, fec).await;
    mux::write_lane_hello(&mut int_writer, mux::LaneClass::Interactive, nonce, group)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let (bulk_reader, mut bulk_writer) =
        rtp_frame_delivery_connect(tasks, bulk_proxy_addr, fec).await;
    mux::write_lane_hello(&mut bulk_writer, mux::LaneClass::Bulk, nonce, group)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;

    let mut int_spawner = JoinSet::new();
    let (int_opener, int_accepter) =
        mux::spawn_mux_no_reconnection(int_reader, int_writer, config.clone(), &mut int_spawner);
    let mut bulk_spawner = JoinSet::new();
    let (bulk_opener, bulk_accepter) =
        mux::spawn_mux_no_reconnection(bulk_reader, bulk_writer, config.clone(), &mut bulk_spawner);
    let mut super_spawner = JoinSet::new();
    let (opener, accepter) = mux::spawn_dual_mux_paired_supervised(
        int_opener,
        int_accepter,
        int_spawner,
        bulk_opener,
        bulk_accepter,
        bulk_spawner,
        &mut super_spawner,
    );
    // The dual-lane supervision is drained by a required scope task (see
    // [`dual_mux_client_connect`]): a panicked lane surfaces immediately,
    // and the session ending before the test body completes is a panic.
    tasks.spawn_required("dual-mux client session", async move {
        while let Some(result) = super_spawner.join_next().await {
            let err = result.unwrap();
            panic!("dual-mux client session ended before the test body: {err:?}");
        }
    });
    Ok((opener, accepter))
}

pub async fn dual_mux_client_connect_with_lane_modes(
    tasks: &mut TestScope,
    int_proxy_addr: std::net::SocketAddr,
    bulk_proxy_addr: std::net::SocketAddr,
    fec: bool,
    interactive_frame: bool,
    bulk_frame: bool,
) -> Result<(mux::DualStreamOpener, mux::DualStreamAccepter), mux::DualMuxError> {
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
        tasks: &mut TestScope,
        addr: std::net::SocketAddr,
        fec: bool,
        frame: bool,
    ) -> Option<(BoxedRead, BoxedWrite)> {
        if frame {
            let (r, w) = rtp_frame_delivery_connect(tasks, addr, fec).await;
            Some((Box::new(r), Box::new(w)))
        } else {
            let (r, w) = rtp_connect(tasks, addr, fec).await;
            Some((Box::new(r), Box::new(w)))
        }
    }
    let mut super_spawner = JoinSet::new();
    let nonce = mux::PairingNonce::generate();
    let group = mux::GroupToken::generate();
    let Some((int_reader, mut int_writer)) =
        connect_lane(tasks, int_proxy_addr, fec, interactive_frame).await
    else {
        return Err(mux::DualMuxError::LaneHello(mux::LaneHelloError::Io(
            std::io::ErrorKind::ConnectionRefused,
        )));
    };
    mux::write_lane_hello(&mut int_writer, mux::LaneClass::Interactive, nonce, group)
        .await
        .map_err(mux::DualMuxError::LaneHello)?;
    let Some((bulk_reader, mut bulk_writer)) =
        connect_lane(tasks, bulk_proxy_addr, fec, bulk_frame).await
    else {
        return Err(mux::DualMuxError::LaneHello(mux::LaneHelloError::Io(
            std::io::ErrorKind::ConnectionRefused,
        )));
    };
    mux::write_lane_hello(&mut bulk_writer, mux::LaneClass::Bulk, nonce, group)
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
    // The dual-lane supervision is drained by a required scope task (see
    // [`dual_mux_client_connect`]): a panicked lane surfaces immediately,
    // and the session ending before the test body completes is a panic.
    tasks.spawn_required("dual-mux client session", async move {
        while let Some(result) = super_spawner.join_next().await {
            let err = result.unwrap();
            panic!("dual-mux client session ended before the test body: {err:?}");
        }
    });
    Ok((opener, accepter))
}

// ─────────────── dual‑lane frame‑delivery server helpers ───────────────

/// Dual‑mux latency‑bulk server with frame‑reassembly enabled.
///
/// Like [`spawn_dual_mux_latency_bulk_server`] but the server’s mux sessions
/// use `frame_reassembly: true` so that extended‑data frames from a
/// frame‑delivery RTP connection are reassembled correctly.
pub async fn spawn_dual_mux_frame_delivery_latency_bulk_server(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
) -> std::io::Result<(
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    spawn_dual_mux_latency_bulk_server_with_config(
        tasks,
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
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    interactive_frame: bool,
    bulk_frame: bool,
) -> std::io::Result<(
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
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
    spawn_dual_mux_latency_bulk_server_with_per_lane_configs(
        tasks,
        fec,
        base,
        int_config,
        bulk_config,
    )
    .await
}

/// Shared implementation: dual‑mux server with per‑lane [`mux::MuxConfig`]s.
async fn spawn_dual_mux_latency_bulk_server_with_config(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    config: mux::MuxConfig,
) -> std::io::Result<(
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    spawn_dual_mux_latency_bulk_server_with_per_lane_configs(
        tasks,
        fec,
        base,
        config.clone(),
        config,
    )
    .await
}

async fn spawn_dual_mux_latency_bulk_server_with_per_lane_configs(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    int_config: mux::MuxConfig,
    bulk_config: mux::MuxConfig,
) -> std::io::Result<(
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
)> {
    let listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let addr = listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));

    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Parked accept loop (aborted when `tasks` drops at scope end).
    let listener_bg = Arc::clone(&listener);
    tasks.spawn_required("dual-mux server task", async move {
        while let Ok(accepted) = listener_bg
            .accept_without_handshake_with(rtp::udp::AcceptConfig {
                fec,

                mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),

                ..rtp::udp::AcceptConfig::default()
            })
            .await
        {
            if accept_tx.send(accepted).await.is_err() {
                break;
            }
        }
    });

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<(mux::UnpairedLane, mux::MuxConfig)>> =
            HashMap::new();

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some(accepted) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });

            let result =
                mux::begin_lane_pairing(reader, writer, int_config.clone(), Duration::from_secs(3))
                    .await;

            if let Ok((class, nonce, pa)) = result {
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
                        pair_handlers.spawn(async move {
                            let _cfg1 = cfg1;
                            let _cfg2 = cfg2;
                            // Per-stream handlers owned by the pair handler's
                            // scope; drained after the accept loop ends.
                            let mut stream_handlers = JoinSet::new();
                            loop {
                                tokio::select! {
                                    accepted = accepter.accept() => {
                                        match accepted {
                                            Ok((mut reader, mut writer, _class)) => {
                                let bulk = Arc::clone(&bulk);
                                let tx = tx.clone();
                                stream_handlers.spawn(async move {
                                    let mut tag = [0u8; 1];
                                    if reader.read_exact(&mut tag).await.is_err() {
                                        let _ = writer.shutdown();
                                        return;
                                    }
                                    if tag[0] == b'L' {
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
                                                let latency_ms =
                                                    now_us.saturating_sub(sent_us) as f64 / 1000.0;
                                                if !try_send_observation(
                                                    &tx,
                                                    (tag[0], latency_ms),
                                                    "latency sample",
                                                ) {
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
                                    }
                                    let _ = writer.shutdown();
                                                });
                                            }
                                            Err(_) => break,
                                        }
                                    }
                                    Some(joined) = stream_handlers.join_next(), if !stream_handlers.is_empty() => {
                                        joined.unwrap();
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            while let Some(result) = stream_handlers.join_next().await {
                                result.unwrap();
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });

    Ok((addr, rx, bulk_delivered))
}

/// Dual-lane latency-bulk server with two listeners. Continuously pumps each
/// listener's accept loop and applies per-lane frame delivery at accept time,
/// then spawns the mux handshake in the pairing loop.
///
/// Returns `(int_addr, bulk_addr, lat_rx, bulk_counter, task_tx)` so the client
/// can connect each lane to its dedicated listener. `task_tx` is the bounded
/// submission channel feeding the test-owned reaper; the tagged-stream sink
/// tasks are submitted through it, and the reaper unwraps every completion so
/// panics surface immediately.
pub async fn spawn_dual_mux_latency_bulk_server_two_listeners(
    tasks: &mut TestScope,
    fec: bool,
    base: Instant,
    interactive_frame: bool,
    bulk_frame: bool,
) -> std::io::Result<(
    std::net::SocketAddr,
    std::net::SocketAddr,
    mpsc::Receiver<(u8, f64)>,
    Arc<AtomicU64>,
    mpsc::Sender<TestTask>,
)> {
    let int_listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let bulk_listener = Arc::new(rtp::udp::Listener::bind("127.0.0.1:0").await?);
    let int_addr = int_listener.local_addr();
    let bulk_addr = bulk_listener.local_addr();
    let (tx, rx) = mpsc::channel(LATENCY_SAMPLE_CAPACITY);
    let bulk_delivered = Arc::new(AtomicU64::new(0));
    let (accept_tx, mut accept_rx) = mpsc::channel(TEST_ACCEPT_CAPACITY);

    // Tagged-stream sink tasks spawned by the pairing handler are submitted
    // through a bounded channel feeding one test-owned reaper (spawned into
    // `tasks`), which selects between submissions and join_next() completions
    // and unwraps every completion so panics surface immediately.
    let task_tx = spawn_test_task_reaper(tasks, TEST_TASK_QUEUE_BOUND);

    // Parked accept loops (aborted when `tasks` drops at scope end).
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
        tasks.spawn_required("dual-mux server task", async move {
            loop {
                let fd = if lane_frame {
                    FrameMode::enabled()
                } else {
                    FrameMode::default()
                };
                match listener
                    .accept_without_handshake_with(rtp::udp::AcceptConfig {
                        fec,
                        mss: rtp::udp::MssConfig::Custom(rtp::udp::NO_FEC_MSS),
                        fec_tuning: FecTuning::default(),
                        frame_delivery: fd,
                        ..rtp::udp::AcceptConfig::default()
                    })
                    .await
                {
                    Ok(accepted) => {
                        if accept_tx.send((accepted, config.clone())).await.is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }

    // Parked pairing task (aborted when `tasks` drops at scope end).
    let bulk_for_main = Arc::clone(&bulk_delivered);
    let task_tx_for_pair = task_tx.clone();
    tasks.spawn_required("dual-mux server task", async move {
        let mut pending: HashMap<mux::PairingNonce, Vec<mux::UnpairedLane>> = HashMap::new();

        // Accepted-lane rtp-session keepalives owned by this task's scope;
        // never drained (scope-drop abort).
        let mut lane_keepers = JoinSet::new();
        // Per-pair handlers owned by this task's scope; drained after the
        // accept loop ends so panics surface.
        let mut pair_handlers = JoinSet::new();

        loop {
            tokio::select! {
                accepted = accept_rx.recv() => {
                    match accepted {
                        None => break, // all accept loops closed
                        Some((accepted, config)) => {
            let reader = accepted.read.into_async_read();
            let writer = accepted.write.into_async_write();
            // Hold the accepted lane's rtp session for its whole life;
            // dropping it aborts the session.
            lane_keepers.spawn(async move {
                let _ = accepted.supervisor.await;
            });
            if let Ok((_class, nonce, pa)) =
                mux::begin_lane_pairing(reader, writer, config, Duration::from_secs(3)).await
            {
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
                        let task_tx = task_tx_for_pair.clone();
                        pair_handlers.spawn(async move {
                            loop {
                                tokio::select! {
                                    accepted = accepter.accept() => {
                                        match accepted {
                                            Ok((reader, writer, class)) => {
                                                spawn_tagged_stream_sink(
                                                    &task_tx,
                                                    reader,
                                                    writer,
                                                    tx.clone(),
                                                    Arc::clone(&bulk),
                                                    base,
                                                    class == mux::LaneClass::Interactive,
                                                );
                                            }
                                            Err(_) => break, // peer closed; stop accepting
                                        }
                                    }
                                    Some(joined) = pair_spawner.join_next(), if !pair_spawner.is_empty() => {
                                        // The pair's mux session supervision ended: unwrap so a
                                        // panicked supervision task cascades, and a normal
                                        // MuxError session-end stops accepting.
                                        joined.unwrap();
                                        break;
                                    }
                                }
                            }
                            // Drain the pair's mux supervision tasks, unwrapping so panics surface.
                            while let Some(result) = pair_spawner.join_next().await {
                                result.unwrap();
                            }
                        });
                    }
                }
            }
                        }
                    }
                }
                Some(joined) = lane_keepers.join_next(), if !lane_keepers.is_empty() => {
                    // A lane rtp-session keepalive ended (session closed):
                    // unwrap so a panic surfaces immediately; a normal
                    // completion is a legitimate shutdown.
                    joined.unwrap();
                }
                Some(joined) = pair_handlers.join_next(), if !pair_handlers.is_empty() => {
                    // A pair handler ended: unwrap so a panic surfaces now.
                    joined.unwrap();
                }
            }
        }
        // Drain any remaining lane/session joins so panics surface.
        while let Some(result) = lane_keepers.join_next().await {
            result.unwrap();
        }

        while let Some(result) = pair_handlers.join_next().await {
            result.unwrap();
        }
    });
    Ok((int_addr, bulk_addr, rx, bulk_delivered, task_tx))
}
