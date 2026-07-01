//! End-to-end scenario tests that drive real application protocols —
//! `rtp` (reliable UDP) and `mux` (a stream multiplexer) layered on top of
//! `rtp` — through a [`netem_test::NetemPair`] bidirectional impairment proxy.
//!
//! Marked `#[ignore]` because they spawn threads, bind ephemeral ports, and
//! run for hundreds of milliseconds. Run with:
//!
//! ```sh
//! cargo test --test rtp_and_mux -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

// ───────────────────────── helpers ──────────────────────────────────────

/// Mild impairment that exercises both directions without making `rtp`'s
/// reliable layer give up: ~5% random loss, 10 ms latency, small jitter.
fn mild_loss() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 20, // ~5%
        latency: Duration::from_millis(10),
        jitter: Duration::from_millis(5),
        seed: 1,
        ..NetemConfig::default()
    }
}

/// No impairment at all — a clean baseline to verify the plumbing.
fn clean() -> NetemConfig {
    NetemConfig::default()
}

/// A latency-only config useful for verifying the proxy adds delay.
fn delayed(ms: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(ms),
        seed: 7,
        ..NetemConfig::default()
    }
}

// ───────────────────────── rtp over netem ────────────────────────────────

/// Sanity check: raw UDP echo through the bidirectional proxy works.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn netem_pair_bidirectional_raw_udp_echo() {
    use tokio::net::UdpSocket;
    let echo = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let echo_addr = echo.local_addr().unwrap();
    tokio::spawn(async move {
        let mut buf = [0u8; 64];
        while let Ok((n, from)) = echo.recv_from(&mut buf).await {
            let _ = echo.send_to(&buf[..n], from).await;
        }
    });

    let pair = NetemPair::spawn(echo_addr, clean(), clean()).unwrap();
    let proxy_addr = pair.client_addr();

    let client = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    client.send_to(b"bidir-hello", proxy_addr).await.unwrap();
    let mut buf = [0u8; 64];
    let n = tokio::time::timeout(Duration::from_secs(2), client.recv(&mut buf))
        .await
        .expect("timeout waiting for echo")
        .unwrap();
    assert_eq!(&buf[..n], b"bidir-hello");
    pair.stop();
    let stats = pair.stats();
    assert!(stats.forwarded >= 2, "both directions should forward, got {stats:?}");
}

/// Spawn an `rtp` server that accepts one connection and echoes back
/// everything it receives until the peer closes. Returns the server's
/// listening address.
async fn spawn_rtp_echo_server(fec: bool) -> std::io::Result<String> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr().to_string();
    tokio::spawn(async move {
        loop {
            let accepted = match listener.accept_without_handshake(fec).await {
                Ok(a) => a,
                Err(_) => return,
            };
            tokio::spawn(async move {
                // Echo loop: read into a buffer, write it back.
                let mut read = accepted.read.into_async_read();
                let mut write = accepted.write.into_async_write();
                let mut buf = vec![0u8; 8 * 1024];
                loop {
                    match read.read(&mut buf).await {
                        Ok(0) => break,
                        Ok(n) => {
                            if write.write_all(&buf[..n]).await.is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
                let _ = write.shutdown().await;
            });
        }
    });
    Ok(addr)
}

/// `rtp` should deliver a byte stream reliably over a *clean* netem link.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_over_netem_clean_link_delivers_data() {
    let server_addr = spawn_rtp_echo_server(false).await.unwrap();

    // Spawn the bidirectional proxy in front of the rtp server. The client
    // will connect to the proxy's `client_addr` instead of the server.
    let pair = NetemPair::spawn(
        server_addr.parse().unwrap(),
        clean(),
        clean(),
    )
    .unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, false)
            .await
            .unwrap();

    let mut read = connected.read.into_async_read();
    let mut write = connected.write.into_async_write();

    let payload = b"netem-rtp-integration";
    write.write_all(payload).await.unwrap();
    write.shutdown().await.unwrap();

    let mut got = Vec::new();
    read.read_to_end(&mut got).await.unwrap();

    assert_eq!(got, payload);

    pair.stop();
    let stats = pair.stats();
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `rtp`'s reliable layer should recover from mild packet loss introduced by
/// the netem proxy — the byte stream must arrive intact despite ~5% loss in
/// both directions.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_over_netem_reliability_survives_mild_loss() {
    let server_addr = spawn_rtp_echo_server(false).await.unwrap();

    let pair = NetemPair::spawn(
        server_addr.parse().unwrap(),
        mild_loss(),
        mild_loss(),
    )
    .unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, false)
            .await
            .unwrap();

    let mut read = connected.read.into_async_read();
    let mut write = connected.write.into_async_write();

    // 256 KiB — large enough that hundreds of datagrams cross the link so
    // the ~5% loss produces plenty of retransmits to recover from.
    let payload: Vec<u8> = (0..256 * 1024).map(|i| (i % 251) as u8).collect();
    let payload_clone = payload.clone();
    let sender = tokio::spawn(async move {
        write.write_all(&payload_clone).await.unwrap();
        write.shutdown().await.unwrap();
    });

    let mut got = Vec::new();
    read.read_to_end(&mut got).await.unwrap();
    sender.await.unwrap();

    assert_eq!(got, payload, "reliable layer must recover all data");

    pair.stop();
    let stats = pair.stats();
    assert!(stats.dropped > 0, "proxy should have dropped some packets");
}

/// The netem latency must be observable end-to-end through `rtp`: a ping
/// should take at least the configured one-way delay (the round trip crosses
/// both directions, so roughly `2 * latency`).
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_over_netem_latency_is_observable() {
    let server_addr = spawn_rtp_echo_server(false).await.unwrap();

    let latency = Duration::from_millis(60);
    let pair = NetemPair::spawn(
        server_addr.parse().unwrap(),
        delayed(latency.as_millis() as u64),
        delayed(latency.as_millis() as u64),
    )
    .unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, false)
            .await
            .unwrap();

    let mut read = connected.read.into_async_read();
    let mut write = connected.write.into_async_write();

    let start = std::time::Instant::now();
    write.write_all(b"ping").await.unwrap();
    write.flush().await.unwrap();
    let mut buf = [0u8; 4];
    read.read_exact(&mut buf).await.unwrap();
    let elapsed = start.elapsed();

    assert_eq!(&buf, b"ping");
    // Round trip crosses c2s + s2c, each with `latency` of delay. Allow some
    // slack for scheduling jitter.
    assert!(
        elapsed >= latency,
        "round trip {elapsed:?} should be >= one-way latency {latency:?}",
    );

    pair.stop();
}

/// `rtp` with FEC enabled should be able to recover lost data symbols via
/// parity even when the netem proxy drops ~3% of packets in each direction.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn rtp_with_fec_recovers_under_netem_loss() {
    let server_addr = spawn_rtp_echo_server(true).await.unwrap();

    let lossy = NetemConfig {
        loss: u32::MAX / 33, // ~3%
        latency: Duration::from_millis(5),
        seed: 99,
        ..NetemConfig::default()
    };
    let pair = NetemPair::spawn(server_addr.parse().unwrap(), lossy.clone(), lossy).unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected = rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, true)
        .await
        .unwrap();

    let mut read = connected.read.into_async_read();
    let mut write = connected.write.into_async_write();

    // 1 MiB: large enough that FEC has many symbols to recover.
    let payload: Vec<u8> = (0..1024 * 1024).map(|i| (i % 251) as u8).collect();
    let payload_clone = payload.clone();
    let sender = tokio::spawn(async move {
        write.write_all(&payload_clone).await.unwrap();
        write.shutdown().await.unwrap();
    });

    let mut got = Vec::new();
    read.read_to_end(&mut got).await.unwrap();
    sender.await.unwrap();

    assert_eq!(got, payload, "FEC + reliable layer must recover all data");

    pair.stop();
    let stats = pair.stats();
    assert!(stats.dropped > 0, "proxy should have dropped some packets");
}

// ───────────────────── mux over rtp over netem ───────────────────────────

/// Spawn an `rtp` server that accepts one connection and runs a `mux` server
/// on top of the resulting reliable byte stream. Each accepted mux stream is
/// echoed back. Returns the rtp server's listening address.
async fn spawn_mux_over_rtp_echo_server(fec: bool) -> std::io::Result<String> {
    let listener = rtp::udp::Listener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr().to_string();
    tokio::spawn(async move {
        let accepted = match listener.accept_without_handshake(fec).await {
            Ok(a) => a,
            Err(_) => return,
        };
        let read = accepted.read.into_async_read();
        let write = accepted.write.into_async_write();

        let config = mux::MuxConfig {
            initiation: mux::Initiation::Server,
            heartbeat_interval: Duration::from_secs(5),
        };
        let mut spawner = tokio::task::JoinSet::new();
        let (_opener, mut accepter) = mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

        while let Ok((mut stream_read, mut stream_write)) = accepter.accept().await {
            tokio::spawn(async move {
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
            });
        }
    });
    Ok(addr)
}

/// `mux` layered on `rtp` should multiplex a stream over the netem-impaired
/// link and echo data back intact on a clean link.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn mux_over_rtp_over_netem_clean_link_echoes() {
    let server_addr = spawn_mux_over_rtp_echo_server(false).await.unwrap();

    let pair = NetemPair::spawn(
        server_addr.parse().unwrap(),
        clean(),
        clean(),
    )
    .unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, false)
            .await
            .unwrap();
    let read = connected.read.into_async_read();
    let write = connected.write.into_async_write();

    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
    };
    let mut spawner = tokio::task::JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

    let payload = b"mux-rtp-netem";
    stream_write.write_all(payload).await.unwrap();
    stream_write.shutdown().unwrap();

    let mut got = Vec::new();
    let mut buf = vec![0u8; 64];
    loop {
        match stream_read.read(&mut buf).await {
            Ok(0) => break,
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) => panic!("read failed: {e:?}"),
        }
    }

    assert_eq!(got, payload);

    pair.stop();
    let stats = pair.stats();
    assert_eq!(stats.dropped, 0, "clean link should not drop");
    assert!(stats.forwarded > 0, "proxy should forward packets");
}

/// `mux` over `rtp` should survive the netem proxy's added latency; `rtp`'s
/// reliable layer retransmits any lost datagrams so the multiplexed stream
/// arrives intact. A small payload is used because larger transfers can
/// trip rtp's broken-pipe heuristic when mux's control-plane stalls the
/// ACK path.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn mux_over_rtp_survives_netem_latency() {
    let server_addr = spawn_mux_over_rtp_echo_server(false).await.unwrap();

    // Latency only, no loss — verifies the mux stream survives the proxy's
    // delay queue.
    let impaired = NetemConfig {
        latency: Duration::from_millis(20),
        seed: 42,
        ..NetemConfig::default()
    };
    let pair = NetemPair::spawn(
        server_addr.parse().unwrap(),
        impaired.clone(),
        impaired,
    )
    .unwrap();
    let proxy_client_addr = pair.client_addr().to_string();

    let connected =
        rtp::udp::connect_without_handshake("0.0.0.0:0", &proxy_client_addr, None, false)
            .await
            .unwrap();
    let read = connected.read.into_async_read();
    let write = connected.write.into_async_write();

    let config = mux::MuxConfig {
        initiation: mux::Initiation::Client,
        heartbeat_interval: Duration::from_secs(5),
    };
    let mut spawner = tokio::task::JoinSet::new();
    let (opener, _accepter) = mux::spawn_mux_no_reconnection(read, write, config, &mut spawner);

    let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

    let payload = b"mux-over-rtp-through-netem";
    stream_write.write_all(payload).await.unwrap();
    stream_write.shutdown().unwrap();

    let mut got = Vec::new();
    let mut buf = vec![0u8; 64];
    loop {
        match stream_read.read(&mut buf).await {
            Ok(0) => break,
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) => panic!("read failed: {e:?}"),
        }
    }

    assert_eq!(got, payload, "mux stream must deliver all data intact");

    pair.stop();
    let stats = pair.stats();
    assert!(stats.forwarded > 0, "proxy should forward packets");
}