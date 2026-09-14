// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery adapter
// ═══════════════════════════════════════════════════════════════════════════════

use crate::support::{TestScope, TestTask, TestTaskSubmitter, submit_test_task};

pub type RtpFrameReader = rtp::socket::FrameByteReader;
pub type RtpFrameDeliveryWriter = rtp::socket::FrameByteWriter;

/// Connect an rtp client using frame delivery.  Returns the frame-preserving
/// reader and writer adapters that guarantee one-mux-frame-per-one-rtp-frame.
///
/// `tasks` owns the rtp session supervisor as a non-required keepalive; the
/// supervisor is awaited in the background and the test body owns teardown.
/// A normal FIN shutdown does not fail the test.
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
/// unavailable. The supervisor keepalive is submitted as a non-required
/// keepalive through the handle.
pub async fn rtp_frame_delivery_connect_via(
    tx: &TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_core(
        |fut| submit_test_task(tx, fut),
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Default,
        rtp::FrameMode::enabled(),
    )
    .await
}

/// [`rtp_frame_delivery_connect_via`] with receiver-side fast-forward
/// enabled: the connection uses [`rtp::FrameMode::enabled_reordering`], so a
/// complete frame starting past an unrepaired in-order hole is delivered
/// immediately instead of being withheld behind the hole. Both peers must use
/// the reordering mode; the frame-mode `*_reorder` server helper is the
/// matching accept side.
pub async fn rtp_frame_delivery_connect_reorder_via(
    tx: &TestTaskSubmitter,
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_core(
        |fut| submit_test_task(tx, fut),
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Default,
        rtp::FrameMode::enabled_reordering(),
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
        |fut| submit_test_task(tx, fut),
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Custom(mss),
        rtp::FrameMode::enabled(),
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
        |fut| tasks.spawn(fut),
        proxy_client_addr,
        fec,
        mss,
        rtp::FrameMode::enabled(),
    )
    .await
}

/// Shared core for [`rtp_frame_delivery_connect_with_mss_config`] and the
/// `_via` variants: opens the connection and hands the supervisor keepalive to
/// `spawn` (either a [`TestScope`] spawn or the bounded reaper submission) as
/// a non-required keepalive.
async fn rtp_frame_delivery_connect_core(
    spawn: impl FnOnce(TestTask),
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: rtp::udp::MssConfig,
    frame_mode: rtp::FrameMode,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    let connected = rtp::udp::FrameDeliveryIo::connect(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::ConnectConfig {
            handshake: false,
            fec,
            mss,
            frame_delivery: frame_mode,
            ..rtp::udp::ConnectConfig::default()
        },
    )
    .await
    .unwrap();
    // Hold the rtp session owner for the connection's lifetime; the supervisor
    // is polled as a non-required keepalive so a normal FIN shutdown does not
    // fail the test. An early session end still surfaces through the
    // higher-level supervision (mux session / pump write errors).
    spawn(Box::pin(async move {
        let _ = connected.supervisor.await;
    }));
    (connected.read, connected.write)
}
