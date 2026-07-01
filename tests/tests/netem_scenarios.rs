//! End-to-end scenario tests that drive a real [`netem_test::NetemLink`] over
//! loopback UDP sockets. Marked `#[ignore]` because they spawn threads and
//! bind ephemeral ports; run them with `--ignored --nocapture
//! --test-threads=1` as specified in the workspace task brief.

use std::net::{SocketAddr, UdpSocket};
use std::time::{Duration, Instant};

use netem_test::{FourStateLoss, LossModel, NetemConfig, NetemLink};

/// Bind a receiver socket on an ephemeral loopback port.
fn recv_socket() -> (UdpSocket, SocketAddr) {
    let s = UdpSocket::bind("127.0.0.1:0").unwrap();
    let a = s.local_addr().unwrap();
    (s, a)
}

/// Send `n` datagrams through `link` and collect those that arrive at `recv`
/// within `timeout`. Returns `(received_count, elapsed)`.
fn burst(
    link: &NetemLink,
    recv: &UdpSocket,
    n: u32,
    payload: &[u8],
    timeout: Duration,
) -> (u32, Duration) {
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    let dst = link.client_addr();
    let start = Instant::now();
    for i in 0..n {
        let mut p = payload.to_vec();
        p[0] = (i as u8).wrapping_add(1); // unique seq so dups are distinguishable
        client.send_to(&p, dst).unwrap();
    }
    recv.set_read_timeout(Some(timeout)).unwrap();
    let mut got = 0u32;
    let mut buf = [0u8; 1500];
    while let Ok((_, _)) = recv.recv_from(&mut buf) {
        got += 1;
        if start.elapsed() >= timeout {
            break;
        }
    }
    (got, start.elapsed())
}

#[test]
#[ignore]
fn netem_passes_traffic_unimpaired() {
    let (recv, server) = recv_socket();
    let link = NetemLink::spawn(server, NetemConfig::default()).unwrap();
    let (got, _) = burst(&link, &recv, 100, b"hello", Duration::from_millis(500));
    link.stop();
    let stats = link.stats();
    assert_eq!(
        stats.received, 100,
        "all packets should be received by proxy"
    );
    assert_eq!(stats.dropped, 0, "no drops without impairment");
    assert!(got >= 99, "expected ~all packets delivered, got {got}");
}

#[test]
#[ignore]
fn netem_drops_all_with_max_random_loss() {
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        loss: u32::MAX,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let (got, _) = burst(&link, &recv, 50, b"x", Duration::from_millis(300));
    link.stop();
    let stats = link.stats();
    assert_eq!(stats.received, 50);
    assert_eq!(stats.dropped, 50, "every packet should be dropped");
    assert_eq!(got, 0, "no packets should reach the server");
}

#[test]
#[ignore]
fn netem_four_state_loss_drops_some() {
    let (recv, server) = recv_socket();
    // p14 = ~25% chance from gap-Tx to isolated loss, p31 = max so any burst
    // immediately returns to gap. This yields a mix of drops and delivers.
    let cfg = NetemConfig {
        loss_model: LossModel::FourState(FourStateLoss {
            p13: 0,
            p31: u32::MAX,
            p32: u32::MAX,
            p14: u32::MAX / 4,
            p23: 0,
        }),
        seed: 12345,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let (got, _) = burst(&link, &recv, 200, b"q", Duration::from_millis(800));
    link.stop();
    let stats = link.stats();
    assert!(stats.dropped > 0, "four-state model should drop some");
    assert!(stats.forwarded > 0, "four-state model should forward some");
    assert!(got > 0 && got < 200, "expected partial delivery, got {got}");
}

#[test]
#[ignore]
fn netem_delay_adds_latency() {
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        latency: Duration::from_millis(40),
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    recv.set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let start = Instant::now();
    client.send_to(b"ping", link.client_addr()).unwrap();
    let mut buf = [0u8; 64];
    let (n, _) = recv.recv_from(&mut buf).unwrap();
    let elapsed = start.elapsed();
    link.stop();
    assert_eq!(&buf[..n], b"ping");
    assert!(
        elapsed >= Duration::from_millis(30),
        "delay should be observable, got {elapsed:?}"
    );
}

#[test]
#[ignore]
fn netem_duplicate_produces_extra_packets() {
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        duplicate: u32::MAX, // always duplicate
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let (got, _) = burst(&link, &recv, 10, b"d", Duration::from_millis(400));
    link.stop();
    let stats = link.stats();
    assert_eq!(stats.duplicated, 10, "each packet should be duplicated");
    assert!(
        got > 10,
        "should receive more than sent due to dups, got {got}"
    );
}

#[test]
#[ignore]
fn netem_rate_limit_throttles_burst() {
    let (recv, server) = recv_socket();
    // 8 kbit/s, 10 ms burst => capacity 80 bits. Each 8-byte payload = 64 bits.
    // So roughly one packet per ~8ms; 20 packets should take >= ~150ms.
    let cfg = NetemConfig {
        rate: 8_000,
        burst: Duration::from_millis(10),
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let (got, elapsed) = burst(&link, &recv, 20, b"r", Duration::from_secs(2));
    link.stop();
    let stats = link.stats();
    assert!(
        stats.rate_limited > 0,
        "some packets should be rate-limited"
    );
    assert!(got < 20, "not all should be delivered in time, got {got}");
    assert!(
        elapsed >= Duration::from_millis(120),
        "rate limit should spread delivery, got {elapsed:?}"
    );
}

#[test]
#[ignore]
fn netem_snapshot_reports_queue_and_stats() {
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        latency: Duration::from_millis(200),
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    // send 5 packets; with 200ms delay they should sit in the queue briefly.
    for i in 0..5u8 {
        client.send_to(&[i], link.client_addr()).unwrap();
    }
    // give the proxy a moment to enqueue.
    std::thread::sleep(Duration::from_millis(20));
    let snap = link.snapshot();
    link.stop();
    assert!(snap.stats.received <= 5);
    assert!(
        snap.stats.received > 0,
        "proxy should have received some packets"
    );
    // queue may already be partially drained; just assert it's bounded.
    assert!(snap.queue_len <= 5);
    // drain whatever arrived
    recv.set_read_timeout(Some(Duration::from_millis(50)))
        .unwrap();
    let mut buf = [0u8; 64];
    let mut delivered = 0u32;
    while recv.recv_from(&mut buf).is_ok() {
        delivered += 1;
    }
    assert!(delivered <= 5);
}
