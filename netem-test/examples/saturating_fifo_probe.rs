//! A saturating loopback lane through a real [`NetemPair`], for measuring the
//! harness's CPU per forwarded datagram with `samply`.
//!
//! The perf-loop battery lanes are rate-limited (100 Mbit/s), so the harness is
//! never the bottleneck there and its own CPU is invisible. This probe instead
//! drives the harness into saturation on loopback, in the same shape the
//! `probe_hostile_goodput_30s` capture used: a bulk client->server flood of
//! MSS-sized datagrams plus a sparse server->client reply stream.
//!
//! `PROBE_LATENCY_MS=0` exercises the direct (no-clock, no-queue) runner path;
//! any non-zero latency exercises the FIFO-scheduled path, which is the only
//! path that installs a per-receive socket timeout.
//!
//! ```sh
//! PROBE_LATENCY_MS=1 PROBE_SECONDS=25 \
//!   samply record --save-only -o /tmp/fifo.json -- \
//!   cargo run --release -p netem-test --example saturating_fifo_probe
//! ```
//!
//! The summary line reports the per-direction forwarded-datagram counts, which
//! are the denominator of the CPU-per-forwarded-datagram metric.

use std::net::UdpSocket;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use netem_test::NetemPair;
use netem_test::kit::presets::clean_delay_link;

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn main() {
    let seconds = env_usize("PROBE_SECONDS", 25) as f64;
    let latency_ms = env_usize("PROBE_LATENCY_MS", 1) as u64;
    let mss = env_usize("PROBE_MSS", 8192);
    let yield_every = env_usize("PROBE_YIELD_EVERY", 16).max(1);
    let ack_every = env_usize("PROBE_ACK_EVERY", 13).max(1);

    let server = UdpSocket::bind("127.0.0.1:0").expect("bind sink socket");
    server
        .set_read_timeout(Some(Duration::from_millis(100)))
        .expect("sink read timeout");
    let server_addr = server.local_addr().expect("sink addr");

    let c2s = clean_delay_link(Duration::from_millis(latency_ms), 11);
    let s2c = clean_delay_link(Duration::from_millis(latency_ms), 12);
    let pair = NetemPair::spawn(server_addr, c2s, s2c).expect("spawn NetemPair");
    let client_addr = pair.client_addr();

    let stop = Arc::new(AtomicBool::new(false));

    let sender_stop = Arc::clone(&stop);
    let sender = std::thread::spawn(move || {
        let sock = UdpSocket::bind("127.0.0.1:0").expect("bind client socket");
        sock.connect(client_addr).expect("connect client socket");
        let payload = vec![0xABu8; mss];
        let mut sent = 0u64;
        while !sender_stop.load(Ordering::Relaxed) {
            if sock.send(&payload).is_ok() {
                sent += 1;
                if sent.is_multiple_of(yield_every as u64) {
                    std::thread::yield_now();
                }
            }
        }
        sent
    });

    let sink_stop = Arc::clone(&stop);
    let sink = std::thread::spawn(move || {
        let mut buf = vec![0u8; 64 * 1024];
        let ack = [0u8; 22];
        let mut received = 0u64;
        let mut acks = 0u64;
        while !sink_stop.load(Ordering::Relaxed) {
            if let Ok((_len, from)) = server.recv_from(&mut buf) {
                received += 1;
                if received.is_multiple_of(ack_every as u64) {
                    let _ = server.send_to(&ack, from);
                    acks += 1;
                }
            }
        }
        (received, acks)
    });

    let start = Instant::now();
    std::thread::sleep(Duration::from_secs_f64(seconds));
    stop.store(true, Ordering::Relaxed);
    let sent = sender.join().expect("sender thread");
    let (sink_received, acks) = sink.join().expect("sink thread");
    let elapsed = start.elapsed();

    let c2s = pair.stats_c2s();
    let s2c = pair.stats_s2c();
    drop(pair);

    println!(
        "saturating-probe latency_ms={latency_ms} mss={mss} seconds={:.3} \
sender_sent={sent} sink_received={sink_received} acks={acks}",
        elapsed.as_secs_f64()
    );
    println!(
        "saturating-probe c2s_forwarded={} c2s_received={} c2s_dropped={} \
s2c_forwarded={} s2c_received={} s2c_dropped={} forwarded_total={}",
        c2s.forwarded,
        c2s.received,
        c2s.dropped,
        s2c.forwarded,
        s2c.received,
        s2c.dropped,
        c2s.forwarded + s2c.forwarded
    );
}
