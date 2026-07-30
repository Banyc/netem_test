use std::{io, net::SocketAddr, sync::Arc, time::Duration};

use netem_test::{NetemConfig, NetemPair};
use rtp_mux::{
    BindSelector, BulkAddrSelector, LaneClass, RtpMuxConnector, RtpMuxConnectorConfig, RtpMuxServer,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;
use support::{clean, combined_stats, payload, with_timeout};

fn connector_with(bulk_proxy_addr: SocketAddr, response_migration: bool) -> RtpMuxConnector {
    let bind: BindSelector = Arc::new(|addr| match addr {
        SocketAddr::V4(_) => "0.0.0.0:0".parse().unwrap(),
        SocketAddr::V6(_) => "[::1]:0".parse().unwrap(),
    });
    let bulk_addr: BulkAddrSelector = Arc::new(move |_| Ok(bulk_proxy_addr));
    RtpMuxConnector::with_config(RtpMuxConnectorConfig {
        bind,
        bulk_addr,
        fec: false,
        response_migration,
    })
}

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
        response_migration: false,
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

const DOWNLOAD_LEN: usize = 8 * 1024 * 1024;
const PING_LEN: usize = 8;
const PING_INTERVAL: Duration = Duration::from_millis(40);
const CMD_DOWNLOAD: u8 = b'D';
const CMD_PING: u8 = b'P';
async fn spawn_cmd_server(response_migration: bool) -> io::Result<(SocketAddr, SocketAddr)> {
    let server = RtpMuxServer::bind("127.0.0.1:0", false)
        .await?
        .with_response_migration(response_migration);
    let interactive_addr = server.listener().local_addr();
    let bulk_addr = server.bulk_listener().local_addr();
    tokio::spawn(async move {
        let _ = server
            .serve(|stream| {
                tokio::spawn(async move {
                    let (mut reader, mut writer) = tokio::io::split(stream);
                    let mut cmd = [0u8; 1];
                    if reader.read_exact(&mut cmd).await.is_err() {
                        return;
                    }
                    match cmd[0] {
                        CMD_DOWNLOAD => {
                            let chunk = vec![0xCDu8; 64 * 1024];
                            let mut sent = 0;
                            while sent < DOWNLOAD_LEN {
                                if writer.write_all(&chunk).await.is_err() {
                                    return;
                                }
                                sent += chunk.len();
                            }
                            let _ = writer.shutdown().await;
                        }
                        CMD_PING => {
                            let mut buf = [0u8; PING_LEN];
                            while reader.read_exact(&mut buf).await.is_ok() {
                                if writer.write_all(&buf).await.is_err()
                                    || writer.flush().await.is_err()
                                {
                                    break;
                                }
                            }
                            let _ = writer.shutdown().await;
                        }
                        _ => {}
                    }
                });
            })
            .await;
    });
    Ok((interactive_addr, bulk_addr))
}
fn contended_lane() -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(20),
        rate: 16_000_000,
        limit: 120,
        seed: 811,
        ..NetemConfig::default()
    }
}
struct ResponseArm {
    ping_rtts_ms: Vec<f64>,
    downloaded: usize,
    download_secs: f64,
    bulk_lane_wire_pkts: u64,
}
async fn run_response_arm(response_migration: bool) -> ResponseArm {
    let (interactive_server, bulk_server) = spawn_cmd_server(response_migration).await.unwrap();
    let interactive_pair =
        NetemPair::spawn(interactive_server, contended_lane(), contended_lane()).unwrap();
    let bulk_pair = NetemPair::spawn(bulk_server, contended_lane(), contended_lane()).unwrap();
    let connector = connector_with(bulk_pair.client_addr(), response_migration);
    let mut ping = connector
        .connect_stream(interactive_pair.client_addr())
        .await
        .unwrap();
    ping.write_all(&[CMD_PING]).await.unwrap();
    let mut download = connector
        .connect_stream(interactive_pair.client_addr())
        .await
        .unwrap();
    let download_task = tokio::spawn(async move {
        let started = std::time::Instant::now();
        download.write_all(&[CMD_DOWNLOAD]).await.unwrap();
        let mut buf = vec![0u8; 64 * 1024];
        let mut total = 0usize;
        loop {
            match download.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => total += n,
            }
        }
        (total, started.elapsed().as_secs_f64())
    });
    let mut rtts = Vec::new();
    let mut seq = 0u64;
    let mut buf = [0u8; PING_LEN];
    while !download_task.is_finished() {
        seq += 1;
        let sent = std::time::Instant::now();
        ping.write_all(&seq.to_le_bytes()).await.unwrap();
        ping.read_exact(&mut buf).await.unwrap();
        assert_eq!(u64::from_le_bytes(buf), seq, "ping echo out of sequence");
        rtts.push(sent.elapsed().as_secs_f64() * 1e3);
        tokio::time::sleep(PING_INTERVAL).await;
    }
    let (downloaded, download_secs) = download_task.await.unwrap();
    let bulk_lane_wire_pkts = combined_stats(&bulk_pair).forwarded;
    interactive_pair.stop();
    bulk_pair.stop();
    ResponseArm {
        ping_rtts_ms: rtts,
        downloaded,
        download_secs,
        bulk_lane_wire_pkts,
    }
}
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_mux_response_migration_pinned_vs_migrating() {
    let (pinned, migrating) =
        with_timeout(Duration::from_secs(180), "response migration A/B", async {
            let pinned = run_response_arm(false).await;
            let migrating = run_response_arm(true).await;
            (pinned, migrating)
        })
        .await;
    for (label, arm) in [("pinned", &pinned), ("migrating", &migrating)] {
        eprintln!(
            "[resp-mig {label}] download {} B in {:.1}s ({:.2} MiB/s) pings={} bulk_lane_wire={} pkts",
            arm.downloaded,
            arm.download_secs,
            arm.downloaded as f64 / (1024.0 * 1024.0) / arm.download_secs,
            arm.ping_rtts_ms.len(),
            arm.bulk_lane_wire_pkts
        );
        assert_eq!(arm.downloaded, DOWNLOAD_LEN, "[{label}] download truncated");
        assert!(
            arm.ping_rtts_ms.len() >= 20,
            "[{label}] too few ping samples"
        );
    }
    let arms = [
        ("pinned", pinned.ping_rtts_ms.as_slice()),
        ("migrating", migrating.ping_rtts_ms.as_slice()),
    ];
    if let Ok(path) = netem_test::dist::dump_csv("rtp_mux_response_migration", &arms) {
        eprintln!("[resp-mig] samples: {}", path.display());
    }
    eprintln!(
        "{}",
        netem_test::dist::ab_report("response migration ping RTT", "ms", &arms)
    );
    assert!(
        migrating.bulk_lane_wire_pkts > 2000,
        "migrating arm should carry the download on the bulk lane (saw {} pkts)",
        migrating.bulk_lane_wire_pkts
    );
    assert!(
        pinned.bulk_lane_wire_pkts < 1000,
        "pinned arm should keep the download off the bulk lane (saw {} pkts)",
        pinned.bulk_lane_wire_pkts
    );
    let mut p = pinned.ping_rtts_ms.clone();
    let mut m = migrating.ping_rtts_ms.clone();
    p.sort_by(|a, b| a.partial_cmp(b).unwrap());
    m.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let (p90_pinned, p90_migrating) = (
        netem_test::dist::percentile(&p, 0.90),
        netem_test::dist::percentile(&m, 0.90),
    );
    assert!(
        p90_migrating < p90_pinned * 0.8,
        "response migration should cut p90 ping RTT: pinned={p90_pinned:.1}ms migrating={p90_migrating:.1}ms"
    );
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
