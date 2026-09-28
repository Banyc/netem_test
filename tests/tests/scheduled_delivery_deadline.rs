//! Whether a scheduled datagram leaves at the time its own delay model gives
//! it.
//!
//! `netem-test`'s runner enforces a queued datagram's deadline by blocking in
//! a socket read whose timeout is `deadline_approach_wait(deadline - now)`,
//! and forwards the datagram when that read returns (`netem-test/src/lib.rs`,
//! `LinkRunner::run_heap` / `run_fifo` over
//! `NetemState::next_receive_wait[_fifo]`). The instant a datagram leaves is
//! therefore its deadline *plus* however long that read overran the timeout it
//! was given, and a platform sleep overruns the timeout it is handed. The
//! overrun is a property of the platform's sleep, not of the delay model, and
//! it is what a clean lane's maximum is really quoting.
//!
//! Each cell therefore measures the same run two ways.
//!
//! * **The forward lateness**, through a [`UdpTransport`] the cell wraps around
//!   the real socket: it stamps the instant the runner entered the receive
//!   (`recv_from` returning is the `now` the deadline is computed from) and the
//!   instant the runner asked it to send. `send - (recv + delay)` is the
//!   runner's own lateness, with no receiving endpoint in the measurement. With
//!   no jitter the delay is exactly `latency`, and with a 1 us jitter it is
//!   `latency +- 1 us`, so the draw is known in both cases and the lateness is
//!   exact rather than inferred from a distribution.
//! * **The end-to-end excess**, at a plain `std::net::UdpSocket` sink: the
//!   quantity a clean lane's maximum is quoted against. It carries the
//!   receiving endpoint's own wake-up as well, which is why the lateness above
//!   is the assertion and this is the reading.
//!
//! Two cells reach the two scheduling paths — a zero-jitter link takes the FIFO
//! queue, any jitter takes the heap — and a third carries the deployed link's
//! own numbers (`25 ms +- 5 ms`, the interactive lane's, as in
//! `crates/rtp/tests/rtp_clean_tail.rs`) so the tail rate is reported where the
//! product actually runs.
//!
//! Opt-in (`standard` tier), because the reading moves with the host's load at
//! the tail: its median is what the bound uses, and the load-dependent
//! percentiles are reported rather than bounded.
//!
//! ```sh
//! cargo test --test scheduled_delivery_deadline -- --ignored --nocapture --test-threads=1
//! ```

use std::io;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use netem_test::kit::stats::percentile;
use netem_test::{Counters, NetemConfig, NetemPair, StdUdpTransport, UdpTransport};

/// The clean interactive link's one-way delay and jitter
/// (`rtp_mux/tests/mandate_smoke.rs`).
const OWD: Duration = Duration::from_millis(25);
const JITTER: Duration = Duration::from_millis(5);
const OWD_MS: f64 = 25.0;
const JITTER_MS: f64 = 5.0;
/// The jitter the two exact-lateness cells run at. A zero-jitter link takes
/// the FIFO queue and a jittered one the heap, so reaching both paths needs a
/// non-zero jitter; 1 us makes the drawn delay known to within a microsecond,
/// which is what makes the lateness exact rather than a distribution's right
/// edge.
const PROBE_JITTER: Duration = Duration::from_micros(1);
/// The four-flow interleave of the interactive lane's 5 ms cadence.
const INTERLEAVE: Duration = Duration::from_micros(1_250);
/// The measured window per cell: 1.6 s at the four-flow interleave is ~1280
/// offers, which resolves the platform's own sleep overrun by a factor of
/// several, and the deployed-shape cell runs twice that.
const WINDOW: Duration = Duration::from_millis(1_600);
const DEPLOYED_WINDOW: Duration = Duration::from_millis(3_200);
/// The grace each cell drains in, so a datagram still in flight at the window's
/// end is counted rather than lost from the sample.
const DRAIN: Duration = Duration::from_millis(200);
/// The datagram's first 8 bytes carry a send stamp the sink differences against
/// its own read of the same process-wide monotonic clock.
const MSG_BYTES: usize = 64;

/// The runner's own forward-lateness median bound, in ms, and the second bound
/// it is read against: the same median as a fraction of what the rest of the
/// path adds.
///
/// The median is used because the 90th percentile and the maximum move by two
/// orders of magnitude with the host's load, and are reported rather than
/// bounded. A fixed bound is not enough on its own: measured over loads 6-25,
/// the median lateness is 0.011-0.038 ms with the runner's waits divided and
/// 0.052-0.167 ms without, so the two regimes' spreads leave a fixed bound only
/// about 1.4x of margin whichever side it is set. The ratio is what separates
/// them -- `path_cost` is the same cell's own measurement of what the endpoints
/// add, taken on the same kind of blocking syscall, so it moves with the load
/// the lateness moves with. Measured ratios are 0.19-0.23 divided against
/// 0.95-1.10 undivided, and the absolute bound stays as the guard that a
/// load-inflated endpoint reading cannot mask. Readings are in `tests/GATE.md`.
const FORWARD_LATENESS_P50_MS: f64 = 0.060;
/// The dividing runner's median lateness as a fraction of the median latency the
/// rest of the path adds: the shaper must not be the dominant term in a
/// delivery's own latency.
const FORWARD_LATENESS_TO_PATH_COST_P50: f64 = 0.5;

/// Records the instants one direction's runner touched its transport, and how
/// many times it touched it.
#[derive(Clone, Default)]
struct Records {
    /// One entry per datagram the runner took *out* of the transport, stamped
    /// as the receive returned: the instant the runner's own `now` reads.
    received_at: Arc<Mutex<Vec<Instant>>>,
    /// One entry per datagram the runner handed the transport to send, stamped
    /// before the send syscall.
    sent_at: Arc<Mutex<Vec<Instant>>>,
    /// How many receive entries the runner made, whether or not a datagram
    /// arrived: the runner's wake count, which is what a deadline wait shorter
    /// than the time remaining is paid for in.
    wakes: Arc<AtomicU64>,
}

impl Records {
    fn received(&self) -> Vec<Instant> {
        self.received_at.lock().unwrap().clone()
    }

    fn sent(&self) -> Vec<Instant> {
        self.sent_at.lock().unwrap().clone()
    }

    fn wakes(&self) -> u64 {
        self.wakes.load(Ordering::Relaxed)
    }
}

/// A real [`StdUdpTransport`] with the runner's own touch points stamped.
struct RecordingTransport {
    inner: StdUdpTransport,
    records: Records,
}

impl RecordingTransport {
    fn bind(addr: SocketAddr, records: Records) -> io::Result<Self> {
        Ok(Self {
            inner: StdUdpTransport::bind(addr)?,
            records,
        })
    }
}

impl UdpTransport for RecordingTransport {
    fn connect_peer(&self, peer: SocketAddr) -> io::Result<()> {
        self.inner.connect_peer(peer)
    }

    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
        self.records.wakes.fetch_add(1, Ordering::Relaxed);
        let got = self.inner.recv_from(buf)?;
        self.records
            .received_at
            .lock()
            .unwrap()
            .push(Instant::now());
        Ok(got)
    }

    fn recv_from_timeout(
        &self,
        buf: &mut [u8],
        timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)> {
        self.records.wakes.fetch_add(1, Ordering::Relaxed);
        let got = self.inner.recv_from_timeout(buf, timeout)?;
        self.records
            .received_at
            .lock()
            .unwrap()
            .push(Instant::now());
        Ok(got)
    }

    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
        let stamp = Instant::now();
        let sent = self.inner.send_to(data, dst);
        self.records.sent_at.lock().unwrap().push(stamp);
        sent
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.inner.local_addr()
    }

    fn set_recv_timeout(&self, timeout: Duration) -> io::Result<()> {
        self.inner.set_recv_timeout(timeout)
    }

    fn recv_timeout(&self) -> io::Result<Option<Duration>> {
        self.inner.recv_timeout()
    }
}

fn loopback() -> SocketAddr {
    SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
}

fn link(jitter: Duration) -> NetemConfig {
    NetemConfig {
        latency: OWD,
        jitter,
        seed: 0x5EED,
        ..NetemConfig::default()
    }
}

/// One cell's reading.
struct Cell {
    label: String,
    /// `send - (receive + delay)` per forwarded datagram, in ms, ascending: the
    /// runner's own lateness. It is exact only where the drawn delay is known
    /// -- a zero-jitter link draws the configured value, and a link jittered by
    /// [`PROBE_JITTER`] draws it to within a microsecond -- so it is asserted on
    /// those cells and reported as a mixture on the deployed one.
    forward_lateness: Vec<f64>,
    /// One-way delay per datagram observed at the sink, in ms, ascending.
    one_way: Vec<f64>,
    /// The delay the runner actually applied, `send - receive`, in ms,
    /// ascending. It is the drawn delay plus the runner's lateness, so its part
    /// above the ceiling is the shaper's contribution to the end-to-end tail.
    applied: Vec<f64>,
    /// What the one-way delay owes to everything outside the runner's own delay
    /// model, `one_way - applied`, in ms, ascending: the sender's stamp-to-send
    /// gap, the two hops, and the sink's own wake-up. It is the endpoint's
    /// contribution to the end-to-end tail -- what a lane's maximum would be
    /// quoting if the shaper were exact.
    path_cost: Vec<f64>,
    /// The cell's own delay-model ceiling, `latency + jitter`, in ms.
    ceiling_ms: f64,
    /// How many of the runner's receives could be paired with a forward. A cell
    /// whose pairing is short measured less than it claims to.
    pairs: usize,
    /// The runner's receive entries over the whole cell: the wake count a
    /// shorter deadline wait is paid for in.
    wakes: u64,
    counters: Counters,
    offered: u64,
    wall: Duration,
}

impl Cell {
    fn excess_over(&self, reference_ms: f64) -> Vec<f64> {
        let mut v: Vec<f64> = self.one_way.iter().map(|y| y - reference_ms).collect();
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        v
    }

    fn above(&self, reference_ms: f64) -> Vec<f64> {
        self.excess_over(reference_ms)
            .into_iter()
            .filter(|e| *e > 0.0)
            .collect()
    }

    fn report(&self) {
        let lateness = &self.forward_lateness;
        let ceiling = self.ceiling_ms;
        let tail = self.above(ceiling);
        let end_to_end_excess = self.excess_over(OWD_MS);
        // The end-to-end excess over the ceiling is the applied delay over the
        // ceiling plus the path cost: the shaper's share is `applied - ceiling`
        // when positive, the endpoint's is `path_cost`. Reported together so a
        // tail is attributed rather than assumed.
        let shaper_side = excess_above(&self.applied, ceiling);
        let endpoint_side = excess_above(&self.path_cost, 0.0);
        println!(
            "[scheduled-delivery {}] wall={:.2}s offered={} received={} forwarded={} dropped={} samples={} pairs={} wakes={} wakes_per_forward={:.3} | \
             forward_lateness_ms: n={} p50={:.3} p90={:.3} p99={:.3} max={:.3} | \
             one_way_ms: p50={:.3} p90={:.3} p99={:.3} max={:.3} | \
             excess_over_owd_ms: p50={:.3} p90={:.3} p99={:.3} | \
             ceiling_ms={:.3} tail_n={} tail_rate={:.3}% tail_excess_ms: p50={:.3} p90={:.3} p99={:.3} max={:.3} | \
             applied_delay_ms: p50={:.3} p90={:.3} max={:.3} | path_cost_ms: p50={:.3} p90={:.3} p99={:.3} max={:.3} | \
             tail_shaper_side_n={} tail_endpoint_side_n={}",
            self.label,
            self.wall.as_secs_f64(),
            self.offered,
            self.counters.received,
            self.counters.forwarded,
            self.counters.dropped,
            self.one_way.len(),
            self.pairs,
            self.wakes,
            self.wakes as f64 / self.counters.forwarded.max(1) as f64,
            lateness.len(),
            percentile(lateness, 0.50),
            percentile(lateness, 0.90),
            percentile(lateness, 0.99),
            lateness.last().copied().unwrap_or(f64::NAN),
            percentile(&self.one_way, 0.50),
            percentile(&self.one_way, 0.90),
            percentile(&self.one_way, 0.99),
            self.one_way.last().copied().unwrap_or(f64::NAN),
            percentile(&end_to_end_excess, 0.50),
            percentile(&end_to_end_excess, 0.90),
            percentile(&end_to_end_excess, 0.99),
            ceiling,
            tail.len(),
            100.0 * tail.len() as f64 / self.one_way.len().max(1) as f64,
            percentile(&tail, 0.50),
            percentile(&tail, 0.90),
            percentile(&tail, 0.99),
            tail.last().copied().unwrap_or(f64::NAN),
            percentile(&self.applied, 0.50),
            percentile(&self.applied, 0.90),
            self.applied.last().copied().unwrap_or(f64::NAN),
            percentile(&self.path_cost, 0.50),
            percentile(&self.path_cost, 0.90),
            percentile(&self.path_cost, 0.99),
            self.path_cost.last().copied().unwrap_or(f64::NAN),
            shaper_side.len(),
            endpoint_side.len(),
        );
    }
}

/// The entries of `values` above `reference`, ascending.
fn excess_above(values: &[f64], reference: f64) -> Vec<f64> {
    let mut v: Vec<f64> = values
        .iter()
        .map(|y| y - reference)
        .filter(|e| *e > 0.0)
        .collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v
}

/// Run one cell: the given link on the client-to-server direction, offered at
/// the four-flow interleave for `window`, measured both at the runner's own
/// transport and at the sink.
fn cell(label: &str, jitter: Duration, window: Duration) -> Cell {
    let wall = Instant::now();
    let arrival_records = Records::default();
    let forward_records = Records::default();
    let client = RecordingTransport::bind(loopback(), arrival_records.clone()).unwrap();
    let server = RecordingTransport::bind(loopback(), forward_records.clone()).unwrap();
    let sink = UdpSocket::bind(loopback()).unwrap();
    sink.set_read_timeout(Some(Duration::from_millis(20)))
        .unwrap();
    let sink_addr = sink.local_addr().unwrap();
    let pair = NetemPair::spawn_with_transports(
        sink_addr,
        link(jitter),
        link(jitter),
        Box::new(client),
        Box::new(server),
    )
    .unwrap();
    let dest = pair.client_addr();
    let source = UdpSocket::bind(loopback()).unwrap();

    let base = Instant::now();
    let writer = std::thread::spawn(move || {
        let mut frame = [0u8; MSG_BYTES];
        let start = Instant::now();
        let mut sent = 0u64;
        while start.elapsed() < window {
            frame[..8].copy_from_slice(&(base.elapsed().as_micros() as u64).to_le_bytes());
            if source.send_to(&frame, dest).is_err() {
                break;
            }
            sent += 1;
            std::thread::sleep(INTERLEAVE);
        }
        sent
    });

    let until = base + window + DRAIN;
    // One entry per datagram the sink read, all readings of the same delivery
    // kept together so the end-to-end excess can be split into the delay the
    // runner applied and everything outside it.
    let mut measured: Vec<(u64, u64)> = Vec::new();
    let mut buf = [0u8; 256];
    while Instant::now() < until {
        match sink.recv_from(&mut buf) {
            Ok((n, _)) if n >= 8 => {
                let sent_us = u64::from_le_bytes(buf[..8].try_into().unwrap());
                measured.push((base.elapsed().as_micros() as u64, sent_us));
            }
            // A short datagram cannot carry the stamp, and a read timeout is
            // the drain still running; neither is a sample.
            Ok(_) => continue,
            Err(_) => continue,
        }
    }
    let offered = writer.join().unwrap();
    let counters = pair.snapshot_c2s().stats;
    pair.stop();

    // The two directions' runners share both sockets, so the arrival log is
    // every client-to-server datagram the runner took and the forward log is
    // every one it sent. They are FIFO-related on a link whose delay spread is
    // at most the probe jitter, so index pairing is the pairing; a cell whose
    // logs disagree is rejected by `check_measured` rather than reported.
    let arrivals = arrival_records.received();
    let forwards = forward_records.sent();
    let pairs = arrivals.len().min(forwards.len());
    let applied: Vec<f64> = (0..pairs)
        .map(|i| {
            forwards[i]
                .saturating_duration_since(arrivals[i])
                .as_secs_f64()
                * 1000.0
        })
        .collect();

    // Delivery order for the one-way series, forward order for the rest; the
    // index that pairs a one-way sample with its forward is the delivery index
    // only while nothing is reordered, which `check_measured` pins by requiring
    // the sink to see every forwarded datagram.
    let one_way: Vec<f64> = measured
        .iter()
        .map(|(now_us, sent_us)| (now_us.saturating_sub(*sent_us)) as f64 / 1000.0)
        .collect();
    let path_cost: Vec<f64> = one_way
        .iter()
        .zip(applied.iter())
        .map(|(one_way, applied)| one_way - applied)
        .collect();
    let wakes = arrival_records.wakes();
    let forward_lateness: Vec<f64> = applied.iter().map(|a| a - OWD_MS).collect();

    Cell {
        label: label.to_owned(),
        forward_lateness: sorted(forward_lateness),
        one_way: sorted(one_way),
        applied: sorted(applied),
        path_cost: sorted(path_cost),
        ceiling_ms: OWD_MS + jitter.as_secs_f64() * 1000.0,
        pairs,
        wakes,
        counters,
        offered,
        wall: wall.elapsed(),
    }
}

fn sorted(mut values: Vec<f64>) -> Vec<f64> {
    values.sort_by(|a, b| a.partial_cmp(b).unwrap());
    values
}

/// Prove a cell measured what it claims before anything is asserted about it:
/// every forwarded datagram has a forward stamp, the loss-closed link dropped
/// nothing, and the sink saw what was offered. A cell that measured nothing
/// has not established that the runner is on time, it has established nothing.
fn check_measured(cell: &Cell) {
    cell.report();
    assert!(
        !cell.one_way.is_empty() && !cell.forward_lateness.is_empty(),
        "[scheduled-delivery {}] the cell delivered no sample: a lateness of nothing is not a measurement",
        cell.label,
    );
    assert_eq!(
        cell.counters.dropped, 0,
        "[scheduled-delivery {}] the loss-closed link dropped {} datagrams",
        cell.label, cell.counters.dropped,
    );
    assert_eq!(
        cell.pairs, cell.counters.forwarded as usize,
        "[scheduled-delivery {}] {} of the {} forwarded datagrams have a forward stamp: the instrument missed some",
        cell.label, cell.pairs, cell.counters.forwarded,
    );
    assert_eq!(
        cell.one_way.len(),
        cell.counters.forwarded as usize,
        "[scheduled-delivery {}] the sink saw {} of the {} forwarded datagrams, so the endpoint pair is not measuring the same run",
        cell.label,
        cell.one_way.len(),
        cell.counters.forwarded,
    );
}

/// The runner must forward a datagram at the deadline its own delay model gives
/// it. The delay the model draws is known to a microsecond in the cells this is
/// called on, so the forward lateness is the runner's and nothing else's.
fn assert_forward_lateness(cell: &Cell) {
    let p50 = percentile(&cell.forward_lateness, 0.50);
    let p90 = percentile(&cell.forward_lateness, 0.90);
    let max = cell.forward_lateness.last().copied().unwrap_or(f64::NAN);
    assert!(
        p50 <= FORWARD_LATENESS_P50_MS,
        "[scheduled-delivery {}] the runner's own forward lateness measures p50={p50:.3} ms (p90={p90:.3} max={max:.3}), over the {FORWARD_LATENESS_P50_MS:.3} ms bound: a datagram reaches the wire later than the delay model says, so a clean lane's maximum is the platform's sleep overrun and not its link",
        cell.label,
    );
    let path_cost = percentile(&cell.path_cost, 0.50);
    assert!(
        p50 <= path_cost * FORWARD_LATENESS_TO_PATH_COST_P50,
        "[scheduled-delivery {}] the runner's own forward lateness (p50={p50:.3} ms, p90={p90:.3} max={max:.3}) is {:.2}x the {path_cost:.3} ms the endpoints add, over the {FORWARD_LATENESS_TO_PATH_COST_P50:.2}x bound: the runner's deadline wait, not the path, is setting a delivery's latency",
        cell.label,
        p50 / path_cost.max(f64::MIN_POSITIVE),
    );
}

/// The runner's own forward lateness on both scheduling paths, and the
/// end-to-end reading the clean lane's maximum is quoted against.
#[test]
#[ignore = "scheduled-delivery deadline accuracy: 3 cells, ~7 s, load-sensitive at the tail; run with --ignored --nocapture --test-threads=1"]
fn scheduled_deliveries_leave_at_their_own_deadline() {
    // The zero-jitter link takes the FIFO queue; the probe-jittered one takes
    // the heap. Both are the runner's deadline path, both draw a delay the cell
    // knows, and both are asserted.
    for (label, jitter, window) in [
        ("fifo_fixed_25ms", Duration::ZERO, WINDOW),
        ("heap_jitter_1us", PROBE_JITTER, WINDOW),
    ] {
        let cell = cell(label, jitter, window);
        check_measured(&cell);
        assert_forward_lateness(&cell);
    }
    // The deployed interactive link's own numbers. Its draw is not known to the
    // cell, so its forward lateness is a mixture and is reported, not asserted;
    // what it establishes is the end-to-end split -- how much of a tail above
    // the delay model's ceiling is the shaper and how much is the endpoints.
    let deployed = cell("deployed_jitter_5ms", JITTER, DEPLOYED_WINDOW);
    check_measured(&deployed);
    assert!(
        (deployed.ceiling_ms - (OWD_MS + JITTER_MS)).abs() < 1e-9,
        "[scheduled-delivery deployed_jitter_5ms] the cell's ceiling is {} ms, not the deployed link's {} ms",
        deployed.ceiling_ms,
        OWD_MS + JITTER_MS,
    );
}
