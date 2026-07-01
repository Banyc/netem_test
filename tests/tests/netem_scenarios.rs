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
    // 8 kbit/s. Each 8-byte payload = 64 bits, so serialization time is
    // 8 ms per packet. With no latency, the send-time shaper spreads a
    // 20-packet burst across ~160 ms instead of dropping anything.
    let cfg = NetemConfig {
        rate: 8_000,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let (got, elapsed) = burst(&link, &recv, 20, b"r", Duration::from_secs(2));
    link.stop();
    let stats = link.stats();
    // Send-time shaping delays packets; it must never drop them.
    assert_eq!(
        stats.dropped, 0,
        "rate shaping must not drop packets, got {stats:?}"
    );
    assert_eq!(
        got, 20,
        "all shaped packets should eventually be delivered, got {got}"
    );
    assert!(
        stats.rate_limited > 0,
        "some packets should be rate-shaped, got {stats:?}"
    );
    assert!(
        elapsed >= Duration::from_millis(120),
        "rate shaping should spread delivery, got {elapsed:?}"
    );
}

#[test]
#[ignore]
fn netem_reorder_with_rate_jumps_ahead() {
    // Reorder + rate together: a reordered packet must be scheduled at `now`
    // and must NOT be rate-shaped, so it is delivered ahead of the shaped
    // tail. With gap=5 and reorder=u32::MAX (always reorder once the counter
    // reaches gap-1), the 5th packet of a burst is reordered. A low rate
    // (8 kbit/s ⇒ 8 ms per 8-byte payload) would otherwise delay every
    // normal packet by at least the serialization backlog.
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        gap: 5,
        reorder: u32::MAX,
        rate: 8_000,
        seed: 7,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();

    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    let dst = link.client_addr();
    // Send 6 packets quickly; seq encoded in byte 0.
    let mut payloads: Vec<Vec<u8>> = (0..6u8).map(|i| vec![i + 1, 0xAA]).collect();
    for p in &payloads {
        client.send_to(p, dst).unwrap();
    }

    recv.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut arrival_order: Vec<u8> = Vec::new();
    let mut first_arrival: Option<Duration> = None;
    let start = Instant::now();
    let mut buf = [0u8; 1500];
    loop {
        match recv.recv_from(&mut buf) {
            Ok((n, _)) => {
                if first_arrival.is_none() {
                    first_arrival = Some(start.elapsed());
                }
                if n > 0 {
                    arrival_order.push(buf[0]);
                }
            }
            Err(_) => break,
        }
        if start.elapsed() >= Duration::from_secs(2) {
            break;
        }
    }
    link.stop();
    let stats = link.stats();

    // Reorder happened.
    assert!(
        stats.reordered > 0,
        "expected at least one reordered packet, got {stats:?}"
    );
    // Rate shaping never drops.
    assert_eq!(
        stats.dropped, 0,
        "rate shaping must not drop packets, got {stats:?}"
    );
    // All packets eventually delivered.
    assert_eq!(
        arrival_order.len(),
        6,
        "all 6 packets should be delivered, got {:?}",
        arrival_order
    );
    // The reordered packet (seq 5, the 5th sent) must arrive first — ahead
    // of the rate-shaped tail. (seq is i+1, so the 5th packet has seq 5.)
    assert_eq!(
        arrival_order[0], 5,
        "reordered packet should be delivered first, got {arrival_order:?}"
    );
    // And it should arrive immediately, well before the shaped tail (~40ms
    // serialization backlog at 8 kbit/s for 5 prior packets).
    let first = first_arrival.unwrap();
    assert!(
        first < Duration::from_millis(30),
        "reordered packet should arrive immediately, got {first:?}"
    );

    // keep payloads referenced
    let _ = &mut payloads;
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
