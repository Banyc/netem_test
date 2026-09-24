//! End-to-end scenario tests that drive a real [`netem_test::NetemLink`] over
//! loopback UDP sockets. They run in the default gate: every case is seeded
//! and finishes in a few seconds, so `cargo test -p tests` exercises the
//! harness's pass/drop/duplicate/delay/reorder/rate-limit/queue behaviour
//! without an explicit `--ignored`.

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

/// Send `n` datagrams through `link` and receive until exactly `n` packets
/// arrive at `recv`. The sequence number (1..=n) is encoded in byte 0 of each
/// 8-byte payload; the remaining bytes are copied from `payload[1..]`.
///
/// Returns `(arrival_order, arrival_timestamps)` where `arrival_order[k]` is
/// the seq of the k-th received packet and `arrival_timestamps[k]` is the
/// elapsed `Duration` since `start` at which it arrived.
///
/// The receive loop only terminates when all `n` packets have arrived. The
/// `timeout` is used only as the socket read timeout so the loop can detect
/// missing packets (e.g. dropped by loss impairment) and stop instead of
/// hanging forever; it is NOT used to bound a passing test by wait duration.
fn burst_exact(
    link: &NetemLink,
    recv: &UdpSocket,
    n: u32,
    payload: &[u8; 8],
    timeout: Duration,
) -> (Vec<u8>, Vec<Duration>) {
    assert!(
        n <= 255,
        "burst_exact encodes seq in byte 0, n must be <= 255"
    );
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    let dst = link.client_addr();
    let start = Instant::now();
    for i in 0..n {
        let mut p = *payload;
        p[0] = (i as u8).wrapping_add(1); // seq = 1..=n
        client.send_to(&p, dst).unwrap();
    }

    recv.set_read_timeout(Some(timeout)).unwrap();
    let mut order: Vec<u8> = Vec::with_capacity(n as usize);
    let mut stamps: Vec<Duration> = Vec::with_capacity(n as usize);
    let mut buf = [0u8; 1500];
    while (order.len() as u32) < n {
        match recv.recv_from(&mut buf) {
            Ok((len, _)) => {
                if len > 0 {
                    order.push(buf[0]);
                    stamps.push(start.elapsed());
                }
            }
            Err(_) => {
                // Read timeout fired before all n packets arrived. Stop and
                // return whatever we have so the caller can assert on the
                // shortfall — do not mask missing packets by waiting longer.
                break;
            }
        }
    }
    (order, stamps)
}

#[test]
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
    let (order, stamps) = burst_exact(&link, &recv, 20, b"rate1234", Duration::from_secs(2));
    link.stop();
    let stats = link.stats();
    // Send-time shaping delays packets; it must never drop them.
    assert_eq!(
        stats.dropped, 0,
        "rate shaping must not drop packets, got {stats:?}"
    );
    assert_eq!(
        order.len(),
        20,
        "all shaped packets should eventually be delivered, got {order:?}"
    );
    assert!(
        stats.rate_limited > 0,
        "some packets should be rate-shaped, got {stats:?}"
    );
    // Exact in-order delivery: seq 1..=20 in the order they were sent. The
    // shaper delays each packet but preserves FIFO order.
    assert_eq!(
        order,
        (1u8..=20).collect::<Vec<_>>(),
        "shaper should deliver packets in send order, got {order:?}"
    );
    // Elapsed is measured from the start to the timestamp of the LAST received
    // packet — i.e. real delivery completion, not the socket read timeout.
    let elapsed = *stamps.last().unwrap();
    // ~160 ms of serialization at 8 kbit/s for 20 packets; require >= 120 ms.
    assert!(
        elapsed >= Duration::from_millis(120),
        "rate shaping should spread delivery, got {elapsed:?}"
    );
    // Coarse sanity backstop: the burst must complete well before the 2 s
    // socket read timeout, so a passing test proves real delivery rather than
    // a timeout. In-order arrival of all 20 packets already proves that; this
    // bound is scheduling-tolerant (~9x the ~160 ms serialization time) so
    // host load cannot trip it while it still catches a gross regression.
    assert!(
        elapsed < Duration::from_millis(1500),
        "rate shaping should complete well under the timeout, got {elapsed:?}"
    );
}

#[test]
fn netem_reorder_with_rate_jumps_ahead() {
    // Reorder + rate together: a reordered packet must be scheduled at `now`
    // and must NOT be rate-shaped, so it is delivered ahead of the shaped
    // tail. With reorder_gap_pkts=5 and reorder=u32::MAX (always reorder once
    // the counter reaches reorder_gap_pkts-1), the 5th packet of a burst is
    // reordered. A low rate (8 kbit/s ⇒ 8 ms per 8-byte payload) would
    // otherwise delay every normal packet by at least the serialization
    // backlog.
    let (recv, server) = recv_socket();
    let cfg = NetemConfig {
        reorder_gap_pkts: 5,
        reorder: u32::MAX,
        rate: 8_000,
        seed: 7,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();

    // Send 6 packets quickly; seq encoded in byte 0 of the 8-byte payload.
    // b"reorder!" has seq byte overwritten with 1..=6 by burst_exact.
    let (order, stamps) = burst_exact(&link, &recv, 6, b"reorder!", Duration::from_secs(2));
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
    // All packet identities 1..=6 arrive (no loss).
    let mut seen = order.clone();
    seen.sort_unstable();
    assert_eq!(
        seen,
        (1u8..=6).collect::<Vec<_>>(),
        "all 6 packet identities should arrive, got {order:?}"
    );
    assert_eq!(
        order.len(),
        6,
        "all 6 packets should be delivered, got {order:?}"
    );
    // The reordered packet (seq 5, the 5th sent) must arrive first — ahead
    // of the rate-shaped tail. (seq is i+1, so the 5th packet has seq 5.)
    assert_eq!(
        order[0], 5,
        "reordered packet should be delivered first, got {order:?}"
    );
    // And it should arrive immediately, well before the shaped tail (~40ms
    // serialization backlog at 8 kbit/s for 5 prior packets). Measured from
    // the first arrival timestamp, not a socket timeout. `order[0] == 5`
    // above is the real detector (it fails when the reordered packet is not
    // scheduled ahead of the shaped tail); this is a coarse
    // scheduling-tolerant backstop against a gross re-schedule.
    let first = stamps[0];
    assert!(
        first < Duration::from_millis(500),
        "reordered packet should arrive immediately, got {first:?}"
    );
}

#[test]
fn netem_snapshot_reports_queue_and_stats() {
    let (recv, server) = recv_socket();
    // A hold far longer than this test can last: every datagram that reaches
    // the link is still queued when the snapshot is taken, so the queue depth
    // is a value the test can state exactly rather than bound from above. The
    // queue limit is smaller than the burst, so the link tail-drops the
    // overflow and the final depth is the limit itself -- a value strictly
    // below the received count, which a queue depth that merely echoed the
    // received count could not satisfy.
    let held = Duration::from_secs(30);
    let n = 5u8;
    let cap = 3usize;
    assert!(
        cap < usize::from(n),
        "the cap must bite for the depth to be distinguishable from the received count"
    );
    let cfg = NetemConfig {
        latency: held,
        queue_limit_pkts: cap,
        ..NetemConfig::default()
    };
    let link = NetemLink::spawn(server, cfg).unwrap();
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();
    // send `n` packets; the hold above keeps every one of them queued.
    for i in 0..n {
        client.send_to(&[i], link.client_addr()).unwrap();
    }
    // Wait until the runner has consumed every datagram. The wait is on the
    // independently maintained `received` counter, not on the queue depth
    // being asserted, and the deadline is a liveness guard: a datagram that
    // never arrives fails here with the counters attached instead of hanging.
    let deadline = Instant::now() + Duration::from_secs(5);
    while link.stats().received < u64::from(n) {
        assert!(
            Instant::now() < deadline,
            "runner never received all {n} datagrams within 5s: {:?}",
            link.stats()
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    // `stop()` joins the runner thread. A datagram is handled to completion
    // before the loop re-checks the stop flag, so the snapshot below is read
    // at a quiescent point and cannot race the last enqueue.
    link.stop();
    let snap = link.snapshot();
    assert_eq!(
        snap.stats.received,
        u64::from(n),
        "the link must have received every datagram sent, got {:?}",
        snap.stats
    );
    // Nothing may leave before the {held:?} hold expires, so the datagrams the
    // cap admits are still queued and the depth must equal the admitted
    // count. `forwarded`, `dropped`, and `overflow_dropped` are the production
    // counters that justify the expected depth (received - overflow_dropped,
    // since nothing is forwarded or dropped by any other impairment); they are
    // not a re-derivation of the queue depth.
    assert_eq!(
        snap.stats.forwarded, 0,
        "no datagram may be forwarded before the {held:?} latency elapses, got {:?}",
        snap.stats
    );
    assert_eq!(
        snap.stats.dropped, 0,
        "a clean config must not drop the held datagrams, got {:?}",
        snap.stats
    );
    assert_eq!(
        snap.stats.overflow_dropped,
        u64::from(n) - cap as u64,
        "the excess over the queue limit must be tail-dropped, got {:?}",
        snap.stats
    );
    // The exact value: the admitted count is `received - overflow_dropped`,
    // which is the configured cap `cap`; the queue holds every one of them
    // because nothing has drained. `cap` is an integer the config above
    // states, so an always-zero depth, a depth that echoed the received count
    // ({n}), and a depth off by one all fail here, either at the non-zero
    // check or at the equality.
    assert!(
        snap.queue_len > 0,
        "the held datagrams must be visible in the queue, got {:?}",
        snap
    );
    assert_eq!(
        snap.queue_len, cap,
        "the queue depth must be exactly the admitted datagrams, got {:?}",
        snap
    );
    assert_eq!(
        snap.queue_len as u64 + snap.stats.overflow_dropped,
        snap.stats.received,
        "every received datagram must be either queued or tail-dropped, got {:?}",
        snap
    );
    // Independent confirmation that the depth above is non-zero because the
    // datagrams are held: nothing drains inside the hold.
    recv.set_read_timeout(Some(Duration::from_millis(50)))
        .unwrap();
    let mut buf = [0u8; 64];
    let mut delivered = 0u32;
    while recv.recv_from(&mut buf).is_ok() {
        delivered += 1;
    }
    assert_eq!(
        delivered, 0,
        "no datagram may be delivered inside the {held:?} latency"
    );
    drop(link);

    // The depth must fall again as the queue drains. A second link with a
    // short hold lets every datagram leave, and the same accessor must read
    // zero once the last one has been forwarded rather than staying at its
    // high-water mark.
    let link = NetemLink::spawn(
        server,
        NetemConfig {
            latency: Duration::from_millis(20),
            ..NetemConfig::default()
        },
    )
    .unwrap();
    for i in 0..n {
        client.send_to(&[i], link.client_addr()).unwrap();
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while link.stats().forwarded < u64::from(n) {
        assert!(
            Instant::now() < deadline,
            "runner never forwarded all {n} datagrams within 5s: {:?}",
            link.stats()
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    // Each pop lowers the depth before the send that raises `forwarded`, so
    // once `forwarded` reaches `n` the depth has already been stored as zero.
    link.stop();
    let snap = link.snapshot();
    assert_eq!(
        snap.stats.received,
        u64::from(n),
        "the draining link must have received every datagram sent, got {:?}",
        snap.stats
    );
    assert_eq!(
        snap.stats.forwarded,
        u64::from(n),
        "the draining link must forward every datagram sent, got {:?}",
        snap.stats
    );
    assert_eq!(
        snap.queue_len, 0,
        "the depth must fall to zero once every datagram has drained, got {:?}",
        snap
    );
    recv.set_read_timeout(Some(Duration::from_millis(100)))
        .unwrap();
    let mut delivered = 0u32;
    while delivered < u32::from(n) {
        match recv.recv_from(&mut buf) {
            Ok(_) => delivered += 1,
            Err(_) => break,
        }
    }
    assert_eq!(
        delivered,
        u32::from(n),
        "every drained datagram must reach the server"
    );
}
