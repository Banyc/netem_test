//! The offered-byte conservation identity, measured through the **real
//! socket** path with the Minecraft arm's own link profile.
//!
//! `minecraft_contested` reads `NetemPair::stats_s2c().received_bytes` and
//! prints it as "interactive offered down". This test pins what that field
//! actually means, with both sides derived independently of the link:
//!
//! * the *offered* side is the sender's own count of successful `send_to`
//!   calls, kept by this test;
//! * the *delivered* side is the receiver's own count of `recv_from`
//!   datagrams, kept by this test;
//! * the link's `received_bytes` / `forwarded_bytes` are read from the
//!   harness and must agree with them.
//!
//! Reading `received_bytes` on both sides of the comparison would be the
//! vacuous version of this test -- a dropped increment would cancel -- so the
//! expectations never come from a harness counter.
//!
//! It also pins the **scope**: `stats_s2c` counts only the server-to-client
//! direction of its own link. A caller may not read it as "bytes an
//! application offered", because an application's offer can be split across
//! more than one link (see `PERF_PENDING_rtp_mux.md`, "per-link vs per-stream
//! wire") and bytes lost before the link never appear at all.

use std::net::UdpSocket;
use std::time::Duration;

use netem_test::{BottleneckShaper, NetemConfig, NetemPair};

/// The Minecraft arm's downstream link: 50 ms one-way, 10 ms jitter, 2 % iid
/// loss, a 4096-packet queue, and an 8 Mbit/s shared shaper.
fn minecraft_link() -> NetemConfig {
    NetemConfig {
        rate: 0,
        loss: ((2.0_f64 / 100.0) * u32::MAX as f64) as u32,
        latency: Duration::from_millis(50),
        jitter: Duration::from_millis(10),
        queue_limit_pkts: 4096,
        seed: 0x5153,
        ..NetemConfig::default()
    }
}

/// Drain a socket until it has been idle for three timeouts, returning
/// `(packets, bytes)`.
fn drain(sock: &UdpSocket, buf: &mut [u8]) -> (u64, u64) {
    sock.set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let (mut packets, mut bytes) = (0u64, 0u64);
    let mut idle = 0u32;
    loop {
        match sock.recv_from(buf) {
            Ok((n, _)) => {
                packets += 1;
                bytes += n as u64;
                idle = 0;
            }
            Err(_) => {
                idle += 1;
                if idle >= 3 {
                    break;
                }
            }
        }
    }
    (packets, bytes)
}

#[test]
fn received_bytes_conserves_the_senders_offer_under_the_minecraft_config() {
    let server = UdpSocket::bind("127.0.0.1:0").unwrap();
    let server_addr = server.local_addr().unwrap();
    let pair = NetemPair::spawn_shared(
        server_addr,
        minecraft_link(),
        minecraft_link(),
        None,
        // A live shared shaper, exactly as the arm gives the interactive pair.
        Some(BottleneckShaper::new(8 * 1024 * 1024, 0)),
    )
    .unwrap();
    let proxy = pair.client_addr();
    let client = UdpSocket::bind("127.0.0.1:0").unwrap();

    // Teach the pair the client address, and learn the proxy's server-side
    // address from the forwarded datagram's source.
    client.send_to(b"hello", proxy).unwrap();
    server
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut buf = [0u8; 4096];
    let (n, proxy_server_addr) = server.recv_from(&mut buf).unwrap();
    assert_eq!(&buf[..n], b"hello");

    // The Minecraft downstream shape: four 512 KiB bursts and a 300 B cadence,
    // carried as the ~1350 B datagrams rtp puts on the wire (the harness never
    // sees a 512 KiB datagram, only the datagrams carrying it).
    let burst = vec![7u8; 1350];
    let small = vec![9u8; 300];
    let (mut sent_packets, mut sent_bytes) = (0u64, 0u64);
    for _ in 0..4 {
        for _ in 0..389 {
            server.send_to(&burst, proxy_server_addr).unwrap();
            sent_packets += 1;
            sent_bytes += burst.len() as u64;
        }
        // Light pacing so this measures the harness's accounting, not the
        // kernel's ability to drop a burst faster than the proxy's runner can
        // accept it.
        std::thread::sleep(Duration::from_millis(2));
    }
    for _ in 0..1000 {
        server.send_to(&small, proxy_server_addr).unwrap();
        sent_packets += 1;
        sent_bytes += small.len() as u64;
    }

    let (got_packets, got_bytes) = drain(&client, &mut buf);
    let s2c = pair.stats_s2c();
    let c2s = pair.stats_c2s();

    // The link must have accepted exactly what the sender handed it.
    assert_eq!(
        s2c.received, sent_packets,
        "the link counted {} accepted datagram(s) where the sender sent {sent_packets}",
        s2c.received
    );
    assert_eq!(
        s2c.received_bytes, sent_bytes,
        "the link counted {} offered byte(s) where the sender sent {sent_bytes}",
        s2c.received_bytes
    );
    // And delivered exactly what the receiver read (no datagram is silently
    // forwarded without being counted, and none is counted as forwarded
    // without being sent).
    assert_eq!(
        s2c.forwarded, got_packets,
        "the link counted {} forwarded datagram(s) where the receiver read {got_packets}",
        s2c.forwarded
    );
    assert_eq!(
        s2c.forwarded_bytes, got_bytes,
        "the link counted {} forwarded byte(s) where the receiver read {got_bytes}",
        s2c.forwarded_bytes
    );
    // The impairment's own bookkeeping must close on the packet count: every
    // accepted datagram is either forwarded or dropped by the loss model.
    assert_eq!(
        s2c.received,
        s2c.forwarded + s2c.dropped,
        "the impairment accounted for {} received as {} forwarded + {} dropped, leaving {} unaccounted",
        s2c.received,
        s2c.forwarded,
        s2c.dropped,
        s2c.received as i64 - (s2c.forwarded + s2c.dropped) as i64,
    );
    // Non-degeneracy: the fixture must exercise both outcomes.
    assert!(
        s2c.received > 0 && s2c.dropped > 0,
        "fixture was degenerate: {s2c:?}"
    );
    // Scope: the downstream link is its own direction. The only upstream byte
    // this test sent is the five-byte hello, so a direction swap cannot hide.
    assert_eq!(
        c2s.received_bytes, 5,
        "the c2s direction must carry only the 5-byte hello, not the downstream flow"
    );

    pair.stop();
}
