//! Raw bidirectional UDP echo through [`netem_test::NetemPair`].
//!
//! These tests are the lowest layer of the cross-crate scenario stack: they
//! verify the bidirectional impairment proxy itself forwards and impairs
//! datagrams correctly when driven by plain OS UDP sockets, before any
//! `rtp`/`mux` behaviour is layered on top.
//!
//! Run with:
//!
//! ```sh
//! cargo test --test raw_netem_pair -- --ignored --nocapture --test-threads=1
//! ```

use std::time::Duration;

use netem_test::NetemPair;
use support::payload::with_timeout;
use support::presets::{clean, latency};
use support::stats::combined_stats;
use tokio::net::UdpSocket;

mod support;

/// Sanity check: raw UDP echo through the bidirectional proxy works on a
/// clean link.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn netem_pair_raw_udp_echo_clean_link() {
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
    let n = with_timeout(Duration::from_secs(2), "raw echo", client.recv(&mut buf))
        .await
        .unwrap();
    assert_eq!(&buf[..n], b"bidir-hello");

    pair.stop();
    let stats = combined_stats(&pair);
    assert!(
        stats.forwarded >= 2,
        "both directions should forward, got {stats:?}"
    );
    assert_eq!(stats.dropped, 0, "clean link should not drop");
}

/// The netem latency must be observable end-to-end for raw UDP: a ping should
/// take at least the configured one-way delay (the round trip crosses both
/// directions, so roughly `2 * latency`).
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn netem_pair_raw_udp_latency_is_observable() {
    let echo = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let echo_addr = echo.local_addr().unwrap();
    tokio::spawn(async move {
        let mut buf = [0u8; 64];
        while let Ok((n, from)) = echo.recv_from(&mut buf).await {
            let _ = echo.send_to(&buf[..n], from).await;
        }
    });

    let one_way = Duration::from_millis(60);
    let pair = NetemPair::spawn(echo_addr, latency(60), latency(60)).unwrap();
    let proxy_addr = pair.client_addr();

    let client = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let start = std::time::Instant::now();
    client.send_to(b"ping", proxy_addr).await.unwrap();
    let mut buf = [0u8; 64];
    let n = with_timeout(
        Duration::from_secs(2),
        "raw latency echo",
        client.recv(&mut buf),
    )
    .await
    .unwrap();
    let elapsed = start.elapsed();

    assert_eq!(&buf[..n], b"ping");
    assert!(
        elapsed >= one_way,
        "round trip {elapsed:?} should be >= one-way latency {one_way:?}",
    );

    pair.stop();
}
