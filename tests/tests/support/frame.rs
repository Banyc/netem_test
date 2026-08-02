// ═══════════════════════════════════════════════════════════════════════════════
// Frame‑delivery adapter
// ═══════════════════════════════════════════════════════════════════════════════

use rtp::transmission::fec_tuning::FecTuning;

pub type RtpFrameReader = rtp::socket::FrameReader;
pub type RtpFrameDeliveryWriter = rtp::socket::FrameWriter;

/// Connect an rtp client using frame delivery.  Returns the frame-preserving
/// reader and writer adapters that guarantee one-mux-frame-per-one-rtp-frame.
pub async fn rtp_frame_delivery_connect(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_with_mss_config(proxy_client_addr, fec, rtp::udp::MssConfig::Default)
        .await
}

/// Connect an rtp client using frame delivery with a custom MSS.
pub async fn rtp_frame_delivery_connect_with_mss(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: usize,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    rtp_frame_delivery_connect_with_mss_config(
        proxy_client_addr,
        fec,
        rtp::udp::MssConfig::Custom(mss),
    )
    .await
}

async fn rtp_frame_delivery_connect_with_mss_config(
    proxy_client_addr: std::net::SocketAddr,
    fec: bool,
    mss: rtp::udp::MssConfig,
) -> (RtpFrameReader, RtpFrameDeliveryWriter) {
    let connected = rtp::udp::FrameDeliveryIo::connect(
        "0.0.0.0:0",
        &proxy_client_addr.to_string(),
        rtp::udp::FrameDeliveryConnectConfig {
            log_config: None,
            handshake: false,
            fec,
            mss,
            fec_tuning: FecTuning::default(),
        },
    )
    .await
    .unwrap();
    (connected.read, connected.write)
}
