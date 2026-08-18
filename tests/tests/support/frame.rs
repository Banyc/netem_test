// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery adapter
// ═══════════════════════════════════════════════════════════════════════════════

use crate::support::{TestScope, TestTask, TestTaskSubmitter, submit_test_task_required};

pub type RtpFrameReader = rtp::socket::FrameByteReader;
pub type RtpFrameDeliveryWriter = rtp::socket::FrameByteWriter;

/// Connect an rtp client using frame delivery.  Returns the frame-preserving
/// reader and writer adapters that guarantee one-mux-frame-per-one-rtp-frame.
///
/// `tasks` owns the rtp session-owner keepalive; the session must stay alive
/// for the whole test body, so the keepalive is registered as required.
pub async fn rtp_frame_delivery_connect(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_with_mss_config(
        tasks,
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Default,
    )
    .await
}

/// [`rtp_frame_delivery_connect`] through the bounded task-submission handle,
/// for use inside [`TestScope::run`] bodies where `&mut TestScope` is
/// unavailable. The supervisor keepalive is submitted as required through the
/// handle.
pub async fn rtp_frame_delivery_connect_via(
    tx: &TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_core(
        |name, fut| submit_test_task_required(tx, name, fut),
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Default,
    )
    .await
}

/// Connect an rtp client using frame delivery with a custom MSS.
pub async fn rtp_frame_delivery_connect_with_mss(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_with_mss_config(
        tasks,
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Custom(mss),
    )
    .await
}

/// [`rtp_frame_delivery_connect_with_mss`] through the bounded
/// task-submission handle, for use inside [`TestScope::run`] bodies where
/// `&mut TestScope` is unavailable.
pub async fn rtp_frame_delivery_connect_with_mss_via(
    tx: &TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_core(
        |name, fut| submit_test_task_required(tx, name, fut),
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Custom(mss),
    )
    .await
}

async fn rtp_frame_delivery_connect_with_mss_config(
    tasks: &mut TestScope,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: rtp::udp::MssConfig,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_core(
        |name, fut| tasks.spawn_required(name, fut),
        proxy_client_addr,
        fec,
        mss,
    )
    .await
}

/// Shared core for [`rtp_frame_delivery_connect_with_mss_config`] and the
/// `_via` variants: opens the connection and hands the required supervisor
/// keepalive to `spawn_required` (either a [`TestScope`] spawn or the bounded
/// reaper submission).
async fn rtp_frame_delivery_connect_core(
    spawn_required: impl FnOnce(&'static str, TestTask),
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: rtp::udp::MssConfig,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    let connected = rtp::udp::FrameDeliveryIo::connect(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            fec,
            mss,
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    // Hold the rtp session owner for the connection's lifetime; the session
    // must survive the whole test body, so the keepalive is required.
    spawn_required(
        "rtp client session",
        Box::pin(async move {
            let _ = connected.supervisor.await;
        }),
    );
    (connected.read, connected.write)
}
