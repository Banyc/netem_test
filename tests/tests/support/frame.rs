// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery adapter
// ═══════════════════════════════════════════════════════════════════════════════

pub type RtpFrameReader = rtp::socket::FrameByteReader;
pub type RtpFrameDeliveryWriter = rtp::socket::FrameByteWriter;

/// Connect an rtp client using frame delivery.  Returns the frame-preserving
/// reader and writer adapters that guarantee one-mux-frame-per-one-rtp-frame.
///
/// `tasks` owns the rtp session-owner keepalive; the caller must keep it alive
/// while the returned halves are in use and drop it to abort the session.
pub async fn rtp_frame_delivery_connect(
    tasks: &mut tokio::task::JoinSet<()>,
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

/// Connect an rtp client using frame delivery with a custom MSS.
pub async fn rtp_frame_delivery_connect_with_mss(
    tasks: &mut tokio::task::JoinSet<()>,
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

async fn rtp_frame_delivery_connect_with_mss_config(
    tasks: &mut tokio::task::JoinSet<()>,
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
    // Hold the rtp session owner for the connection's lifetime; dropping it
    // aborts the session.
    tasks.spawn(async move {
        let _ = connected.supervisor.await;
    });
    (connected.read, connected.write)
}
