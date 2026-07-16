use std::{io, net::SocketAddr, sync::Arc, time::Duration};

use netem_test::{NetemConfig, NetemPair};
use rtp_mux::{
    BindSelector, BulkAddrSelector, LaneClass, RtpMuxConnector, RtpMuxConnectorConfig, RtpMuxServer,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;
use support::{clean, combined_stats, payload, with_timeout};

async fn spawn_echo_server() -> io::Result<(
    SocketAddr,
    SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<LaneClass>,
)> {
    let server = RtpMuxServer::bind("127.0.0.1:0", false).await?;
    let interactive_addr = server.listener().local_addr();
    let bulk_addr = server.bulk_listener().local_addr();
    let (lane_tx, lane_rx) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        let _ = server
            .serve(move |stream| {
                let _ = lane_tx.send(stream.source_lane());
                tokio::spawn(async move {
                    let (mut reader, mut writer) = tokio::io::split(stream);
                    let _ = tokio::io::copy(&mut reader, &mut writer).await;
                    let _ = writer.shutdown().await;
                });
            })
            .await;
    });
    Ok((interactive_addr, bulk_addr, lane_rx))
}

fn connector(bulk_proxy_addr: SocketAddr) -> RtpMuxConnector {
    let bind: BindSelector = Arc::new(|addr| match addr {
        SocketAddr::V4(_) => "0.0.0.0:0".parse().unwrap(),
        SocketAddr::V6(_) => "[::]:0".parse().unwrap(),
    });
    let bulk_addr: BulkAddrSelector = Arc::new(move |_| Ok(bulk_proxy_addr));
    RtpMuxConnector::with_config(RtpMuxConnectorConfig {
        bind,
        bulk_addr,
        fec: false,
    })
}

async fn echo_round_trip(
    connector: &RtpMuxConnector,
    interactive_proxy_addr: SocketAddr,
    lane: LaneClass,
    payload: &[u8],
) -> Vec<u8> {
    let mut stream = connector
        .connect_stream_with_lane(interactive_proxy_addr, lane)
        .await
        .unwrap();
    let first = payload.len().min(1);
    stream.write_all(&payload[..first]).await.unwrap();
    stream.write_all(&payload[first..]).await.unwrap();
    stream.shutdown().await.unwrap();
    let mut echoed = Vec::with_capacity(payload.len());
    stream.read_to_end(&mut echoed).await.unwrap();
    echoed
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_mux_clean_dual_lane_echoes_interactive_and_bulk_streams() {
    let (interactive_server, bulk_server, mut accepted_lanes) = spawn_echo_server().await.unwrap();
    let interactive_pair = NetemPair::spawn(interactive_server, clean(), clean()).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_server, clean(), clean()).unwrap();
    let connector = connector(bulk_pair.client_addr());
    let interactive_payload = payload(64 * 1024);
    let bulk_payload = payload(512 * 1024);
    let (interactive_echo, bulk_echo) = with_timeout(
        Duration::from_secs(30),
        "rtp_mux clean dual-lane echo",
        async {
            tokio::join!(
                echo_round_trip(
                    &connector,
                    interactive_pair.client_addr(),
                    LaneClass::Interactive,
                    &interactive_payload
                ),
                echo_round_trip(
                    &connector,
                    interactive_pair.client_addr(),
                    LaneClass::Bulk,
                    &bulk_payload
                ),
            )
        },
    )
    .await;
    assert_eq!(interactive_echo, interactive_payload);
    assert_eq!(bulk_echo, bulk_payload);
    let first_lane = accepted_lanes.recv().await.unwrap();
    let second_lane = accepted_lanes.recv().await.unwrap();
    assert!(
        (first_lane == LaneClass::Interactive && second_lane == LaneClass::Bulk)
            || (first_lane == LaneClass::Bulk && second_lane == LaneClass::Interactive)
    );
    assert!(combined_stats(&interactive_pair).forwarded > 0);
    assert!(combined_stats(&bulk_pair).forwarded > 0);
    interactive_pair.stop();
    bulk_pair.stop();
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_mux_survives_independent_impaired_lanes() {
    let (interactive_server, bulk_server, _accepted_lanes) = spawn_echo_server().await.unwrap();
    let interactive_impairment = NetemConfig {
        latency: Duration::from_millis(15),
        jitter: Duration::from_millis(3),
        loss: u32::MAX / 100,
        seed: 801,
        ..NetemConfig::default()
    };
    let bulk_impairment = NetemConfig {
        latency: Duration::from_millis(35),
        jitter: Duration::from_millis(8),
        loss: u32::MAX / 50,
        seed: 802,
        ..NetemConfig::default()
    };
    let interactive_pair = NetemPair::spawn(
        interactive_server,
        interactive_impairment.clone(),
        interactive_impairment,
    )
    .unwrap();
    let bulk_pair =
        NetemPair::spawn(bulk_server, bulk_impairment.clone(), bulk_impairment).unwrap();
    let connector = connector(bulk_pair.client_addr());
    let interactive_payload = payload(16 * 1024);
    let bulk_payload = payload(256 * 1024);
    let (interactive_echo, bulk_echo) = with_timeout(
        Duration::from_secs(90),
        "rtp_mux independently impaired lanes",
        async {
            tokio::join!(
                echo_round_trip(
                    &connector,
                    interactive_pair.client_addr(),
                    LaneClass::Interactive,
                    &interactive_payload
                ),
                echo_round_trip(
                    &connector,
                    interactive_pair.client_addr(),
                    LaneClass::Bulk,
                    &bulk_payload
                ),
            )
        },
    )
    .await;
    assert_eq!(interactive_echo, interactive_payload);
    assert_eq!(bulk_echo, bulk_payload);
    assert!(combined_stats(&interactive_pair).forwarded > 0);
    assert!(combined_stats(&bulk_pair).forwarded > 0);
    interactive_pair.stop();
    bulk_pair.stop();
}
