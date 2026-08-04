//! Generic network-emulation harness: a reusable, deterministic in-process
//! UDP proxy that applies netem-style impairment (delay, jitter, loss,
//! duplication, reordering, rate-limiting) to forwarded datagrams.
//!
//! The loss math mirrors the four-state Markov chain ("GI model") used by the
//! Linux `sch_netem` qdisc, and the PRNG is the same Tausworthe `prandom`
//! generator the kernel uses, so behaviour is reproducible from a seed.
//!
//! The actual UDP transport is abstracted behind a [`UdpTransport`] trait so
//! tests can substitute an in-memory implementation; the default
//! [`StdUdpTransport`] wraps the standard library socket.

#![forbid(unsafe_code)]

pub mod report;

mod loss;
mod queue;
mod rng;
mod shaper;

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::io;
use std::net::{SocketAddr, SocketAddrV4};
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use loss::FourStateState;
use queue::Queued;
use rng::CorRng;
use shaper::{sample_delay, serialization_delay};

pub use loss::{FourStateLoss, LossModel};
pub use rng::RndState;
pub use shaper::BottleneckShaper;

// ──────────────────────────── UDP transport ────────────────────────────

/// Abstract UDP datagram transport.
///
/// Implemented as a trait object (`Box<dyn UdpTransport>`) so the harness can
/// run against either real OS sockets or an in-memory loopback in tests.
pub trait UdpTransport: Send + Sync + 'static {
    /// Receive a datagram into `buf`, returning `(len, from)`.
    ///
    /// If no datagram is available, the call should block until either a
    /// datagram arrives or the transport's configured receive timeout
    /// expires, returning [`io::ErrorKind::TimedOut`] / [`WouldBlock`]
    /// accordingly.
    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)>;

    /// Receive a datagram into `buf`, waiting at most `timeout`.
    ///
    /// Returns `Ok((len, from))` on success, `Err(WouldBlock)` / `Err(TimedOut)`
    /// if no datagram arrived before the deadline, and other errors for
    /// transport failures. The transport should be left in a usable state so
    /// subsequent receives work.
    fn recv_from_timeout(
        &self,
        buf: &mut [u8],
        timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)>;

    /// Send `data` to `dst`.
    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()>;

    /// Local address of the bound socket.
    fn local_addr(&self) -> io::Result<SocketAddr>;

    /// Set the receive timeout used by [`recv_from`] when no explicit timeout
    /// is supplied. Passing [`Duration::ZERO`] disables the timeout (block
    /// indefinitely). Implementations may fall back to a short polling timeout
    /// internally.
    fn set_recv_timeout(&self, timeout: Duration) -> io::Result<()>;

    /// Current receive timeout, if any.
    fn recv_timeout(&self) -> io::Result<Option<Duration>>;
}

impl<T: UdpTransport + ?Sized> UdpTransport for Arc<T> {
    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
        (**self).recv_from(buf)
    }
    fn recv_from_timeout(
        &self,
        buf: &mut [u8],
        timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)> {
        (**self).recv_from_timeout(buf, timeout)
    }
    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
        (**self).send_to(data, dst)
    }
    fn local_addr(&self) -> io::Result<SocketAddr> {
        (**self).local_addr()
    }
    fn set_recv_timeout(&self, timeout: Duration) -> io::Result<()> {
        (**self).set_recv_timeout(timeout)
    }
    fn recv_timeout(&self) -> io::Result<Option<Duration>> {
        (**self).recv_timeout()
    }
}

/// Standard-library `UdpSocket` backed transport.
#[derive(Debug)]
pub struct StdUdpTransport {
    sock: std::net::UdpSocket,
}

impl StdUdpTransport {
    pub fn bind(addr: SocketAddr) -> io::Result<Self> {
        let sock = std::net::UdpSocket::bind(addr)?;
        sock.set_nonblocking(false)?;
        sock.set_read_timeout(Some(Duration::from_millis(5)))?;
        Ok(Self { sock })
    }
}

impl UdpTransport for StdUdpTransport {
    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
        self.sock.recv_from(buf)
    }

    fn recv_from_timeout(
        &self,
        buf: &mut [u8],
        timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)> {
        self.sock.set_read_timeout(Some(timeout))?;
        let res = self.sock.recv_from(buf);
        self.sock.set_read_timeout(Some(Duration::from_millis(5)))?;
        res
    }

    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
        self.sock.send_to(data, dst)?;
        Ok(())
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.sock.local_addr()
    }

    fn set_recv_timeout(&self, timeout: Duration) -> io::Result<()> {
        if timeout.is_zero() {
            self.sock.set_read_timeout(None)
        } else {
            self.sock.set_read_timeout(Some(timeout))
        }
    }

    fn recv_timeout(&self) -> io::Result<Option<Duration>> {
        self.sock.read_timeout()
    }
}

/// Injectable monotonic clock for deterministic tests.
///
/// Time-sensitive paths in [`NetemState`] (delay-heap readiness, rate shaping,
/// shared-bottleneck serialization) read the current instant through the
/// [`Clock::now`] seam. When a runner is constructed without a clock it falls
/// back to the wall clock (`Instant::now`); tests install a [`Clock`] and call
/// [`Clock::advance`] to move emulated time forward directly instead of
/// sleeping on `thread::sleep`, keeping the unit tests deterministic and fast.
#[derive(Clone, Debug)]
struct Clock {
    /// Real instant corresponding to emulated time `0`.
    base: Instant,
    /// Emulated nanoseconds elapsed since `base`.
    nanos: Arc<AtomicU64>,
}

impl Clock {
    #[cfg(test)]
    fn new() -> Self {
        Self {
            base: Instant::now(),
            nanos: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Current emulated instant.
    fn now(&self) -> Instant {
        self.base + Duration::from_nanos(self.nanos.load(Ordering::Relaxed))
    }

    #[cfg(test)]
    /// Advance emulated time forward by `d`.
    fn advance(&self, d: Duration) {
        self.nanos.fetch_add(d.as_nanos() as u64, Ordering::Relaxed);
    }
}

// ──────────────────────────── config & counters ─────────────────────────

/// Configuration for one emulated direction.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct NetemConfig {
    /// Fixed delay added to every packet.
    pub latency: Duration,
    /// Jitter ±; applied as a uniform spread around `latency`.
    pub jitter: Duration,
    /// Correlation of delay samples (0..=u32::MAX).
    pub delay_corr: u32,
    /// Independent-loss threshold (0 = none, `u32::MAX` = all). Used by
    /// `LossModel::Random`.
    pub loss: u32,
    /// Correlation of loss decisions.
    pub loss_corr: u32,
    /// Duplication threshold.
    pub duplicate: u32,
    /// Correlation of duplication decisions.
    pub dup_corr: u32,
    /// Reorder threshold: a packet is sent immediately when `reorder >=
    /// get_crandom`.
    pub reorder: u32,
    /// Correlation of reorder decisions.
    pub reorder_corr: u32,
    /// Gap for the reorder counter (`gap` in sch_netem), in packets.
    #[serde(alias = "gap")]
    pub reorder_gap_pkts: u32,
    /// Loss model.
    pub loss_model: LossModel,
    /// Rate limit in bits/s; `0` disables rate-limiting.
    pub rate: u64,
    /// PRNG seed for deterministic behaviour.
    pub seed: u64,
    /// sch_netem-style packet queue limit in packets. `0` means unbounded
    /// legacy behaviour. When non-zero, it bounds the whole delay heap
    /// including latency/jitter-held in-flight packets, so latency×rate
    /// configs must size `queue_limit_pkts` above the latency×rate (in
    /// packets) plus the intended bottleneck buffer. Kernel `sch_netem`
    /// defaults to 1000.
    #[serde(alias = "limit")]
    pub queue_limit_pkts: usize,
    /// Maximum datagram size in bytes. Datagrams larger than this value are
    /// deterministically dropped before any other impairment (loss, delay,
    /// rate-limiting, etc.) and counted as `dropped`. `0` disables the filter
    /// (all sizes pass).
    pub max_datagram_size: usize,
}

impl Default for NetemConfig {
    fn default() -> Self {
        Self {
            latency: Duration::ZERO,
            jitter: Duration::ZERO,
            delay_corr: 0,
            loss: 0,
            loss_corr: 0,
            duplicate: 0,
            dup_corr: 0,
            reorder: 0,
            reorder_corr: 0,
            reorder_gap_pkts: 0,
            loss_model: LossModel::default(),
            rate: 0,
            seed: 0xC0FF_EEBE_EFC0_FFEE,
            queue_limit_pkts: 0,
            max_datagram_size: 0,
        }
    }
}

/// Live impairment counters – mirrors `struct tc_netem_xstats` plus the
/// separate overflow-drop counter required by the packet queue limit.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Counters {
    pub delayed: u64,
    pub dropped: u64,
    pub duplicated: u64,
    pub reordered: u64,
    pub rate_limited: u64,
    pub forwarded: u64,
    pub received: u64,
    /// Packets dropped because the per-direction queue exceeded `limit`.
    pub overflow_dropped: u64,
}

/// Atomic backing store for [`Counters`] so the runner thread can update
/// counters without taking a lock. `snapshot()` produces a plain `Counters`
/// for the public API.
#[derive(Default)]
struct AtomicCounters {
    delayed: AtomicU64,
    dropped: AtomicU64,
    duplicated: AtomicU64,
    reordered: AtomicU64,
    rate_limited: AtomicU64,
    forwarded: AtomicU64,
    received: AtomicU64,
    overflow_dropped: AtomicU64,
}

impl AtomicCounters {
    #[inline]
    fn inc(&self, f: impl Fn(&AtomicCounters) -> &AtomicU64) {
        f(self).fetch_add(1, Ordering::Relaxed);
    }

    fn snapshot(&self) -> Counters {
        Counters {
            delayed: self.delayed.load(Ordering::Relaxed),
            dropped: self.dropped.load(Ordering::Relaxed),
            duplicated: self.duplicated.load(Ordering::Relaxed),
            reordered: self.reordered.load(Ordering::Relaxed),
            rate_limited: self.rate_limited.load(Ordering::Relaxed),
            forwarded: self.forwarded.load(Ordering::Relaxed),
            received: self.received.load(Ordering::Relaxed),
            overflow_dropped: self.overflow_dropped.load(Ordering::Relaxed),
        }
    }
}

/// Read-only snapshot of a link's state at a point in time.
#[derive(Clone, Copy, Debug, Default)]
pub struct CountersSnapshot {
    pub stats: Counters,
    pub queue_len: usize,
}

// ───────────────────────────── the link ─────────────────────────────────

/// A running emulated link. Dropping the handle does *not* stop the proxy
/// thread; call [`NetemLink::stop`] for that.
pub struct NetemLink {
    client_addr: SocketAddr,
    server_addr: SocketAddr,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<Mutex<bool>>,
}

impl NetemLink {
    /// Spawn a proxy: bind a UDP socket (the "client" address clients send
    /// to), forward impaired datagrams to `server_addr`. The proxy listens on
    /// the loopback IPv4 address with an ephemeral port.
    pub fn spawn(server_addr: SocketAddr, config: NetemConfig) -> io::Result<Self> {
        Self::spawn_on(
            server_addr,
            config,
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0)),
        )
    }

    /// Spawn with a custom transport (for in-memory tests). The transport
    /// must already be bound; its local address becomes the client address.
    pub fn spawn_with_transport(
        server_addr: SocketAddr,
        config: NetemConfig,
        transport: Box<dyn UdpTransport>,
    ) -> io::Result<Self> {
        Self::spawn_from_transport(server_addr, config, transport)
    }

    /// Spawn a proxy bound to a specific `bind` address.
    pub fn spawn_on(
        server_addr: SocketAddr,
        config: NetemConfig,
        bind: SocketAddr,
    ) -> io::Result<Self> {
        let transport = StdUdpTransport::bind(bind)?;
        Self::spawn_from_transport(server_addr, config, Box::new(transport))
    }

    fn spawn_from_transport(
        server_addr: SocketAddr,
        config: NetemConfig,
        transport: Box<dyn UdpTransport>,
    ) -> io::Result<Self> {
        let client_addr = transport.local_addr()?;
        let stats = Arc::new(AtomicCounters::default());
        let queue_len = Arc::new(AtomicU64::new(0));
        let blackout = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(Mutex::new(false));

        let link = Self {
            client_addr,
            server_addr,
            stats: Arc::clone(&stats),
            queue_len: Arc::clone(&queue_len),
            blackout: Arc::clone(&blackout),
            stop: Arc::clone(&stop),
        };

        let runner = LinkRunner::new(RunnerConfig {
            netem: config,
            server_addr,
            stats,
            queue_len,
            blackout,
            stop,
            transport,
            clock: None,
        });
        std::thread::Builder::new()
            .name("netem-link".into())
            .spawn(move || runner.run())?;

        Ok(link)
    }

    /// Address clients should send to.
    pub fn client_addr(&self) -> SocketAddr {
        self.client_addr
    }

    /// Address the proxy forwards to.
    pub fn server_addr(&self) -> SocketAddr {
        self.server_addr
    }

    /// Current impairment counters.
    pub fn stats(&self) -> Counters {
        self.stats.snapshot()
    }

    /// Current queue depth.
    pub fn queue_len(&self) -> usize {
        self.queue_len.load(Ordering::Relaxed) as usize
    }

    /// Atomic snapshot.
    pub fn snapshot(&self) -> CountersSnapshot {
        CountersSnapshot {
            stats: self.stats(),
            queue_len: self.queue_len(),
        }
    }

    /// Total drop gate.
    /// Enable or disable the 100% loss blackout gate. Packets already queued
    /// continue to drain; newly received packets are counted as dropped while
    /// the gate is closed.
    pub fn set_blackout(&self, on: bool) {
        self.blackout.store(on, Ordering::Relaxed);
    }

    /// Signal the proxy thread to stop after the next iteration.
    pub fn stop(&self) {
        *self.stop.lock().unwrap() = true;
    }
}

// ───────────────────────────── state & runner ─────────────────────────

/// Shared per-direction impairment pipeline (state bag).
///
/// Both [`LinkRunner`] and [`SharedLinkRunner`] drive a single [`NetemState`]: it
/// owns the PRNG state, loss model, delay heap, and the per-direction
/// send-time shaper clock, and applies duplication / loss / reorder / delay /
/// rate shaping to each incoming datagram. The two runner flavours differ only
/// in where datagrams come from and go to (a fixed server address vs. a
/// learned client address, with an optional shared-bottleneck shaper).
struct NetemState {
    config: NetemConfig,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<Mutex<bool>>,
    /// Optional shared-bottleneck shaper. When set, it replaces the per-
    /// direction `config.rate` serialization clock.
    shared: Option<BottleneckShaper>,
    rng: RndState,
    delay_cor: CorRng,
    loss_cor: CorRng,
    dup_cor: CorRng,
    reorder_cor: CorRng,
    state: FourStateState,
    /// Earliest time the next packet may be serialized (send-time shaper).
    /// Tracks the per-direction serialization backlog for rate limiting.
    link_free_at: Instant,
    /// Injectable clock; when set, all time reads go through it so tests can
    /// drive emulated time deterministically instead of sleeping.
    clock: Option<Clock>,
    queue: BinaryHeap<Reverse<Queued>>,
    reorder_counter: u32,
    seq: u64,
}

impl NetemState {
    fn new(
        config: NetemConfig,
        stats: Arc<AtomicCounters>,
        queue_len: Arc<AtomicU64>,
        blackout: Arc<AtomicBool>,
        stop: Arc<Mutex<bool>>,
        shared: Option<BottleneckShaper>,
        clock: Option<Clock>,
    ) -> Self {
        let rng = RndState::seed(config.seed);
        let link_free_at = match &clock {
            Some(c) => c.now(),
            None => Instant::now(),
        };
        Self {
            delay_cor: CorRng::new(config.delay_corr),
            loss_cor: CorRng::new(config.loss_corr),
            dup_cor: CorRng::new(config.dup_corr),
            reorder_cor: CorRng::new(config.reorder_corr),
            link_free_at,
            config,
            stats,
            queue_len,
            blackout,
            stop,
            shared,
            rng,
            state: FourStateState::default(),
            clock,
            queue: BinaryHeap::new(),
            reorder_counter: 0,
            seq: 0,
        }
    }

    /// Current emulated time: the injected [`Clock`] when present, else the
    /// wall clock.
    fn now(&self) -> Instant {
        match &self.clock {
            Some(c) => c.now(),
            None => Instant::now(),
        }
    }

    fn should_stop(&self) -> bool {
        *self.stop.lock().unwrap()
    }

    fn handle_datagram(&mut self, data: &[u8], now: Instant, dst: Option<SocketAddr>) {
        self.stats.inc(|s| &s.received);

        // ── max datagram size filter ──────────────────────────────────
        // Drop oversized datagrams before any other processing.
        if self.config.max_datagram_size > 0 && data.len() > self.config.max_datagram_size {
            self.stats.inc(|s| &s.dropped);
            return;
        }

        // ── blackout gate ────────────────────────────────────────────
        // Drop every incoming packet while the gate is closed. This runs after
        // the packet is counted as received so Counters.received includes gated
        // packets and Counters.dropped counts them.
        if self.blackout.load(Ordering::Relaxed) {
            self.stats.inc(|s| &s.dropped);
            return;
        }

        // ── duplication ──────────────────────────────────────────────
        let mut count = 1u32;
        if self.config.duplicate != 0 && self.config.duplicate >= self.dup_cor.next(&mut self.rng) {
            count += 1;
            self.stats.inc(|s| &s.duplicated);
        }

        // ── loss ─────────────────────────────────────────────────────
        if self.config.loss_model.loss(
            &mut self.state,
            &mut self.loss_cor,
            &mut self.rng,
            self.config.loss,
        ) {
            self.stats.inc(|s| &s.dropped);
            // A lost packet still consumes a duplication slot.
            count = count.saturating_sub(1);
        }

        if count == 0 {
            return;
        }

        // ── rate limit (send-time shaping) ──────────────────────────────
        // netem rate delays packets by serialization time, never drops them.
        for _ in 0..count {
            self.enqueue(data, now, dst);
        }
    }

    /// Compute the per-packet delay (uniform spread around `latency` when
    /// jitter is non-zero, matching the kernel's default branch).
    fn sample_delay(&mut self) -> Duration {
        sample_delay(&self.config, &mut self.rng, &mut self.delay_cor)
    }

    fn enqueue(&mut self, data: &[u8], now: Instant, dst: Option<SocketAddr>) {
        // ── queue limit (tail-drop) ──────────────────────────────────
        // Check before the reorder/schedule logic so a tail-dropped packet
        // consumes no PRNG draw, never advances link_free_at, and leaves the
        // reorder_counter untouched.
        if self.config.queue_limit_pkts != 0 && self.queue.len() >= self.config.queue_limit_pkts {
            self.stats.inc(|s| &s.overflow_dropped);
            return;
        }

        // ── reorder ──────────────────────────────────────────────────
        // Reorder only when reorder_gap_pkts != 0 and only after the reorder
        // counter reaches reorder_gap_pkts - 1; use "reorder >= random" like
        // sch_netem.
        //
        // Mirrors the Linux `sch_netem` branch structure: the normal
        // branch applies delay and (optionally) rate shaping, while the
        // reorder branch schedules the packet for immediate send (`now`)
        // and resets the reorder counter — rate shaping is *not* applied
        // to reordered packets, so they always jump ahead of the shaped
        // tail.
        let reorder = self.config.reorder_gap_pkts != 0
            && self.reorder_counter >= self.config.reorder_gap_pkts - 1
            && self.config.reorder >= self.reorder_cor.next(&mut self.rng);

        let time_to_send = if reorder {
            self.reorder_counter = 0;
            self.stats.inc(|s| &s.reordered);
            // Reordered packet is scheduled immediately; no rate shaping.
            now
        } else {
            let delay = self.sample_delay();
            self.reorder_counter = self.reorder_counter.wrapping_add(1);
            if !delay.is_zero() {
                self.stats.inc(|s| &s.delayed);
            }

            // ── shared bottleneck (normal branch only) ──────────────────
            // Shape at packet arrival time, then add per-flow propagation
            // delay. This avoids the latency×rate phantom buffer headroom
            // that the kernel's delay-first order would give long-RTT flows.
            if let Some(shared) = &self.shared {
                match shared.schedule(now, data.len()) {
                    Some(t) => {
                        if t != now {
                            self.stats.inc(|s| &s.rate_limited);
                        }
                        t + delay
                    }
                    None => {
                        self.stats.inc(|s| &s.overflow_dropped);
                        return;
                    }
                }
            } else {
                let base = now + delay;

                // ── rate shaping (normal branch only) ───────────────────
                // Schedule after max(now + configured_delay, previous
                // scheduled send time) + packet_bits / rate_bps. Send-time
                // shaping only delays packets; it never drops them.
                if let Some(serialize) = serialization_delay(data.len(), self.config.rate) {
                    let earliest = base.max(self.link_free_at);
                    let t = earliest + serialize;
                    self.link_free_at = t;
                    if t != base {
                        self.stats.inc(|s| &s.rate_limited);
                    }
                    t
                } else {
                    base
                }
            }
        };

        let Some(dst) = dst else {
            // No known destination yet (s2c before the first client packet).
            return;
        };

        let item = Queued {
            time_to_send,
            seq: self.seq,
            data: data.to_vec(),
            dst,
        };
        self.seq = self.seq.wrapping_add(1);
        self.queue.push(Reverse(item));
        self.queue_len
            .store(self.queue.len() as u64, Ordering::Relaxed);
    }

    fn drain_ready(&mut self, now: Instant, send: &dyn UdpTransport) {
        loop {
            let ready = self
                .queue
                .peek()
                .map(|q| q.0.time_to_send <= now)
                .unwrap_or(false);
            if !ready {
                break;
            }
            let Reverse(Queued { data, dst, .. }) = self.queue.pop().unwrap();
            self.queue_len
                .store(self.queue.len() as u64, Ordering::Relaxed);
            if send.send_to(&data, dst).is_ok() {
                self.stats.inc(|s| &s.forwarded);
            }
        }
    }
}

struct LinkRunner {
    server_addr: SocketAddr,
    transport: Box<dyn UdpTransport>,
    pipeline: NetemState,
}

struct RunnerConfig {
    netem: NetemConfig,
    server_addr: SocketAddr,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<Mutex<bool>>,
    transport: Box<dyn UdpTransport>,
    clock: Option<Clock>,
}

impl Deref for LinkRunner {
    type Target = NetemState;
    fn deref(&self) -> &Self::Target {
        &self.pipeline
    }
}

impl DerefMut for LinkRunner {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.pipeline
    }
}

impl LinkRunner {
    fn new(config: RunnerConfig) -> Self {
        let RunnerConfig {
            netem,
            server_addr,
            stats,
            queue_len,
            blackout,
            stop,
            transport,
            clock,
        } = config;
        Self {
            server_addr,
            transport,
            pipeline: NetemState::new(netem, stats, queue_len, blackout, stop, None, clock),
        }
    }

    fn run(mut self) {
        let mut buf = [0u8; 64 * 1024];
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            // Drain ready packets first so latency is honoured.
            self.pipeline
                .drain_ready(self.pipeline.now(), &*self.transport);

            // Block briefly on recv so we don't spin. Use the explicit
            // timeout API so the receive deadline is decoupled from the
            // transport's default read timeout and can be asserted by tests.
            match self
                .transport
                .recv_from_timeout(&mut buf, Duration::from_millis(5))
            {
                Ok((n, _from)) => {
                    self.pipeline.handle_datagram(
                        &buf[..n],
                        self.pipeline.now(),
                        Some(self.server_addr),
                    );
                }
                Err(e)
                    if e.kind() == io::ErrorKind::WouldBlock
                        || e.kind() == io::ErrorKind::TimedOut =>
                {
                    // keep draining
                }
                Err(_) => break,
            }
        }
    }

    #[cfg(test)]
    fn handle_datagram(&mut self, data: &[u8], _from: SocketAddr, now: Instant) {
        self.pipeline
            .handle_datagram(data, now, Some(self.server_addr));
    }

    #[cfg(test)]
    fn drain_ready(&mut self, now: Instant) {
        self.pipeline.drain_ready(now, &*self.transport);
    }
}

// ─────────────────────── bidirectional link ──────────────────────────────

/// A running bidirectional emulated link. Datagrams arriving on the
/// client-side socket are impaired per `c2s` and forwarded to the real
/// server; datagrams arriving on the server-side socket (i.e. replies from
/// the real server) are impaired per `s2c` and forwarded back to the client
/// whose address is learned from the first client→server packet.
///
/// Dropping the handle does not stop the proxy threads; call
/// [`NetemPair::stop`] for that.
pub struct NetemPair {
    client_addr: SocketAddr,
    server_addr: SocketAddr,
    stats_c2s: Arc<AtomicCounters>,
    stats_s2c: Arc<AtomicCounters>,
    queue_len_c2s: Arc<AtomicU64>,
    queue_len_s2c: Arc<AtomicU64>,
    blackout_c2s: Arc<AtomicBool>,
    blackout_s2c: Arc<AtomicBool>,
    stop: Arc<Mutex<bool>>,
}

struct NetemPairConfig {
    c2s: NetemConfig,
    s2c: NetemConfig,
    c2s_shared: Option<BottleneckShaper>,
    s2c_shared: Option<BottleneckShaper>,
    clock: Option<Clock>,
}

impl NetemPair {
    /// Spawn a bidirectional proxy. Clients send to the returned
    /// [`NetemPair::client_addr`]; the proxy forwards to `server_addr` with
    /// `c2s` impairment and forwards the server's replies back to the client
    /// with `s2c` impairment. The client's return address is learned from the
    /// first packet received on the client-side socket.
    pub fn spawn(server_addr: SocketAddr, c2s: NetemConfig, s2c: NetemConfig) -> io::Result<Self> {
        let localhost = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
        Self::spawn_on(server_addr, c2s, s2c, localhost, localhost)
    }

    /// Spawn with explicit bind addresses for the two proxy sockets.
    pub fn spawn_on(
        server_addr: SocketAddr,
        c2s: NetemConfig,
        s2c: NetemConfig,
        client_bind: SocketAddr,
        server_bind: SocketAddr,
    ) -> io::Result<Self> {
        let client_sock: Box<dyn UdpTransport> = Box::new(StdUdpTransport::bind(client_bind)?);
        let server_sock: Box<dyn UdpTransport> = Box::new(StdUdpTransport::bind(server_bind)?);
        let client_addr = client_sock.local_addr()?;
        Self::spawn_from_sockets(
            server_addr,
            client_sock,
            server_sock,
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: None,
                s2c_shared: None,
                clock: None,
            },
        )
    }

    /// Spawn a bidirectional proxy with optional shared shapers.
    ///
    /// This is the same as [`NetemPair::spawn`], but a direction can be given a
    /// [`BottleneckShaper`] so that multiple flows contend for one bottleneck
    /// rate. The corresponding direction's `config.rate` must be `0`; otherwise
    /// the call panics with a "double-shape" message.
    pub fn spawn_shared(
        server_addr: SocketAddr,
        c2s: NetemConfig,
        s2c: NetemConfig,
        c2s_shared: Option<BottleneckShaper>,
        s2c_shared: Option<BottleneckShaper>,
    ) -> io::Result<Self> {
        let localhost = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
        Self::spawn_shared_on(
            server_addr,
            c2s,
            s2c,
            c2s_shared,
            s2c_shared,
            localhost,
            localhost,
        )
    }

    /// Spawn with shared shapers and explicit bind addresses.
    pub fn spawn_shared_on(
        server_addr: SocketAddr,
        c2s: NetemConfig,
        s2c: NetemConfig,
        c2s_shared: Option<BottleneckShaper>,
        s2c_shared: Option<BottleneckShaper>,
        client_bind: SocketAddr,
        server_bind: SocketAddr,
    ) -> io::Result<Self> {
        let client_sock: Box<dyn UdpTransport> = Box::new(StdUdpTransport::bind(client_bind)?);
        let server_sock: Box<dyn UdpTransport> = Box::new(StdUdpTransport::bind(server_bind)?);
        let client_addr = client_sock.local_addr()?;
        Self::spawn_from_sockets(
            server_addr,
            client_sock,
            server_sock,
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared,
                s2c_shared,
                clock: None,
            },
        )
    }

    fn spawn_from_sockets(
        server_addr: SocketAddr,
        client_sock: Box<dyn UdpTransport>,
        server_sock: Box<dyn UdpTransport>,
        client_addr: SocketAddr,
        config: NetemPairConfig,
    ) -> io::Result<Self> {
        let NetemPairConfig {
            c2s,
            s2c,
            c2s_shared,
            s2c_shared,
            clock,
        } = config;
        if c2s_shared.is_some() {
            assert_eq!(
                c2s.rate, 0,
                "double-shape: c2s has both config.rate and a BottleneckShaper"
            );
        }
        if s2c_shared.is_some() {
            assert_eq!(
                s2c.rate, 0,
                "double-shape: s2c has both config.rate and a BottleneckShaper"
            );
        }
        let stats_c2s = Arc::new(AtomicCounters::default());
        let stats_s2c = Arc::new(AtomicCounters::default());
        let queue_len_c2s = Arc::new(AtomicU64::new(0));
        let queue_len_s2c = Arc::new(AtomicU64::new(0));
        let blackout_c2s = Arc::new(AtomicBool::new(false));
        let blackout_s2c = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(Mutex::new(false));
        let learned_client = Arc::new(Mutex::<Option<SocketAddr>>::new(None));

        let pair = Self {
            client_addr,
            server_addr,
            stats_c2s: Arc::clone(&stats_c2s),
            stats_s2c: Arc::clone(&stats_s2c),
            queue_len_c2s: Arc::clone(&queue_len_c2s),
            queue_len_s2c: Arc::clone(&queue_len_s2c),
            blackout_c2s: Arc::clone(&blackout_c2s),
            blackout_s2c: Arc::clone(&blackout_s2c),
            stop: Arc::clone(&stop),
        };

        // Both sockets are shared between the two runners via `Arc`: each
        // direction uses one socket to recv and the other to send.
        let client_sock: Arc<dyn UdpTransport> = Arc::from(client_sock);
        let server_sock: Arc<dyn UdpTransport> = Arc::from(server_sock);

        // c2s: recv on client_sock, send on server_sock to server_addr; learn
        // the client's address from the first packet.
        let c2s_runner = SharedLinkRunner::new(
            Arc::clone(&client_sock),
            Arc::clone(&server_sock),
            DirectionRunnerConfig {
                netem: c2s,
                stats: stats_c2s,
                queue_len: queue_len_c2s,
                blackout: blackout_c2s,
                stop: Arc::clone(&stop),
                fixed_dst: Some(server_addr),
                learned_dst: Arc::clone(&learned_client),
                shared: c2s_shared,
                clock: clock.clone(),
            },
        );
        std::thread::Builder::new()
            .name("netem-c2s".into())
            .spawn(move || c2s_runner.run())?;

        // s2c: recv on server_sock, send on client_sock to the learned client
        // address.
        let s2c_runner = SharedLinkRunner::new(
            server_sock,
            client_sock,
            DirectionRunnerConfig {
                netem: s2c,
                stats: stats_s2c,
                queue_len: queue_len_s2c,
                blackout: blackout_s2c,
                stop,
                fixed_dst: None,
                learned_dst: learned_client,
                shared: s2c_shared,
                clock,
            },
        );
        std::thread::Builder::new()
            .name("netem-s2c".into())
            .spawn(move || s2c_runner.run())?;

        Ok(pair)
    }

    /// Address clients should send to.
    pub fn client_addr(&self) -> SocketAddr {
        self.client_addr
    }

    /// Address the proxy forwards client packets to (the real server).
    pub fn server_addr(&self) -> SocketAddr {
        self.server_addr
    }

    /// Counters for the client→server direction.
    pub fn stats_c2s(&self) -> Counters {
        self.stats_c2s.snapshot()
    }

    /// Counters for the server→client direction.
    pub fn stats_s2c(&self) -> Counters {
        self.stats_s2c.snapshot()
    }

    /// Queue depth for the client→server direction.
    pub fn queue_len_c2s(&self) -> usize {
        self.queue_len_c2s.load(Ordering::Relaxed) as usize
    }

    /// Queue depth for the server→client direction.
    pub fn queue_len_s2c(&self) -> usize {
        self.queue_len_s2c.load(Ordering::Relaxed) as usize
    }

    /// Combined stats across both directions.
    pub fn stats(&self) -> Counters {
        let a = self.stats_c2s();
        let b = self.stats_s2c();
        Counters {
            delayed: a.delayed + b.delayed,
            dropped: a.dropped + b.dropped,
            duplicated: a.duplicated + b.duplicated,
            reordered: a.reordered + b.reordered,
            rate_limited: a.rate_limited + b.rate_limited,
            forwarded: a.forwarded + b.forwarded,
            received: a.received + b.received,
            overflow_dropped: a.overflow_dropped + b.overflow_dropped,
        }
    }

    /// Per-direction snapshots.
    pub fn snapshot_c2s(&self) -> CountersSnapshot {
        CountersSnapshot {
            stats: self.stats_c2s(),
            queue_len: self.queue_len_c2s(),
        }
    }

    pub fn snapshot_s2c(&self) -> CountersSnapshot {
        CountersSnapshot {
            stats: self.stats_s2c(),
            queue_len: self.queue_len_s2c(),
        }
    }

    /// Enable or disable the 100% loss blackout gate for a direction. Packets
    /// already queued continue to drain; newly received packets are counted as
    /// dropped while the gate is closed.
    pub fn set_blackout_c2s(&self, on: bool) {
        self.blackout_c2s.store(on, Ordering::Relaxed);
    }

    pub fn set_blackout_s2c(&self, on: bool) {
        self.blackout_s2c.store(on, Ordering::Relaxed);
    }

    /// Signal both proxy threads to stop after the next iteration.
    pub fn stop(&self) {
        *self.stop.lock().unwrap() = true;
    }
}

// ─────────────────────── per-direction runner ────────────────────────────

struct SharedLinkRunner {
    recv: Arc<dyn UdpTransport>,
    send: Arc<dyn UdpTransport>,
    /// Fixed destination (the real server for c2s). When `None`, the runner
    /// uses the learned client address (`learned_dst`).
    fixed_dst: Option<SocketAddr>,
    learned_dst: Arc<Mutex<Option<SocketAddr>>>,
    pipeline: NetemState,
}

struct DirectionRunnerConfig {
    netem: NetemConfig,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<Mutex<bool>>,
    fixed_dst: Option<SocketAddr>,
    learned_dst: Arc<Mutex<Option<SocketAddr>>>,
    shared: Option<BottleneckShaper>,
    clock: Option<Clock>,
}

impl SharedLinkRunner {
    fn new(
        recv: Arc<dyn UdpTransport>,
        send: Arc<dyn UdpTransport>,
        config: DirectionRunnerConfig,
    ) -> Self {
        let DirectionRunnerConfig {
            netem,
            stats,
            queue_len,
            blackout,
            stop,
            fixed_dst,
            learned_dst,
            shared,
            clock,
        } = config;
        Self {
            recv,
            send,
            fixed_dst,
            learned_dst,
            pipeline: NetemState::new(netem, stats, queue_len, blackout, stop, shared, clock),
        }
    }

    fn run(mut self) {
        let mut buf = [0u8; 64 * 1024];
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            self.pipeline.drain_ready(self.pipeline.now(), &*self.send);

            // Use the explicit timeout API so the receive deadline is
            // decoupled from the transport's default read timeout.
            match self
                .recv
                .recv_from_timeout(&mut buf, Duration::from_millis(5))
            {
                Ok((n, from)) => {
                    // For c2s, learn the client address so the s2c runner can
                    // send replies back to it.
                    if self.fixed_dst.is_some() {
                        *self.learned_dst.lock().unwrap() = Some(from);
                    }
                    let dst = self.fixed_dst.or_else(|| *self.learned_dst.lock().unwrap());
                    self.pipeline
                        .handle_datagram(&buf[..n], self.pipeline.now(), dst);
                }
                Err(e)
                    if e.kind() == io::ErrorKind::WouldBlock
                        || e.kind() == io::ErrorKind::TimedOut =>
                {
                    // keep draining
                }
                Err(_) => break,
            }
        }
    }
}

// ───────────────────────────── tests ────────────────────────────────────

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;

    use super::*;

    #[test]
    fn prng_is_deterministic() {
        let mut a = RndState::seed(0xDEAD_BEEF_CAFE_F00D);
        let mut b = RndState::seed(0xDEAD_BEEF_CAFE_F00D);
        for _ in 0..1024 {
            assert_eq!(a.next_u32(), b.next_u32());
        }
    }

    #[test]
    fn prng_differs_for_different_seeds() {
        let mut a = RndState::seed(1);
        let mut b = RndState::seed(2);
        let mut diffs = 0;
        for _ in 0..64 {
            if a.next_u32() != b.next_u32() {
                diffs += 1;
            }
        }
        assert!(diffs > 60);
    }

    #[test]
    fn large_jitter_still_jitters() {
        for secs in [2, 3, 4, 5, 10] {
            let config = NetemConfig {
                latency: Duration::from_millis(300),
                jitter: Duration::from_secs(secs),
                ..NetemConfig::default()
            };
            let mut rng = RndState::seed(config.seed);
            let mut cor = CorRng::new(config.delay_corr);
            let mut nonzero = 0;
            for _ in 0..256 {
                if !sample_delay(&config, &mut rng, &mut cor).is_zero() {
                    nonzero += 1;
                }
            }
            assert!(
                nonzero > 0,
                "{secs} s of jitter produced 256 consecutive zero delays - the sigma truncation collapsed the spread window below the subtracted jitter"
            );
        }
    }

    #[test]
    fn sub_clamp_jitter_is_untouched_by_the_ceiling() {
        let config = NetemConfig {
            latency: Duration::from_millis(300),
            jitter: Duration::from_millis(500),
            ..NetemConfig::default()
        };
        let mut rng = RndState::seed(config.seed);
        let mut cor = CorRng::new(config.delay_corr);
        let sigma = config.jitter.as_nanos() as i64;
        let mu = config.latency.as_nanos() as i64;
        for _ in 0..256 {
            let ns = sample_delay(&config, &mut rng, &mut cor).as_nanos() as i64;
            assert!(
                (mu - sigma..=mu + sigma).contains(&ns),
                "delay {ns} ns escaped [mu-sigma, mu+sigma]"
            );
        }
    }

    #[test]
    fn four_state_loss_can_drop_and_transmit() {
        let p = FourStateLoss {
            p13: 0,
            p31: u32::MAX,
            p32: 0,
            p14: u32::MAX, // from gap-Tx always go to isolated loss
            p23: 0,
        };
        let model = LossModel::FourState(p);
        let mut state = FourStateState::TxInGap;
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(42);
        // first packet: rnd < p14 => lost, transition to LostInGap
        assert!(model.loss(&mut state, &mut cor, &mut rng, 0));
        assert_eq!(state, FourStateState::LostInGap);
        // next packet: LostInGap -> TxInGap, transmit
        assert!(!model.loss(&mut state, &mut cor, &mut rng, 0));
        assert_eq!(state, FourStateState::TxInGap);
    }

    #[test]
    fn random_loss_zero_never_drops() {
        let model = LossModel::Random;
        let mut state = FourStateState::default();
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(1);
        for _ in 0..1000 {
            assert!(!model.loss(&mut state, &mut cor, &mut rng, 0));
        }
    }

    #[test]
    fn random_loss_max_always_drops() {
        let model = LossModel::Random;
        let mut state = FourStateState::default();
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(1);
        for _ in 0..1000 {
            assert!(model.loss(&mut state, &mut cor, &mut rng, u32::MAX));
        }
    }

    #[test]
    fn config_default_is_no_impairment() {
        let c = NetemConfig::default();
        assert!(c.latency.is_zero());
        assert!(c.jitter.is_zero());
        assert_eq!(c.loss, 0);
        assert_eq!(c.duplicate, 0);
        assert_eq!(c.rate, 0);
        assert_eq!(c.queue_limit_pkts, 0);
        assert_eq!(c.max_datagram_size, 0);
    }

    #[test]
    fn max_datagram_size_drops_only_oversized_datagrams() {
        let config = NetemConfig {
            max_datagram_size: 512,
            ..Default::default()
        };
        let (mut runner, _sent) = one_packet_runner(
            &[0u8; 100],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config.clone(),
        );
        // one_packet_runner already sent the initial packet: received = 1
        // Oversized packet: dropped.
        runner.handle_datagram(
            &[0u8; 513],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            Instant::now(),
        );
        // Small packet: enqueued.
        runner.handle_datagram(
            &[0u8; 512],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            Instant::now(),
        );
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 3);
        assert_eq!(s.dropped, 1);
        // Two packets enqueued (not yet forwarded since drain isn't called).
        assert_eq!(runner.queue.len(), 2);
    }

    /// In-memory transport that records sent payloads and can return them on
    /// `recv_from`. Used to drive [`LinkRunner`] deterministically in unit tests.
    /// It also owns an injectable [`Clock`] so tests can advance emulated time
    /// directly instead of `thread::sleep`.
    #[derive(Debug)]
    struct MockTransport {
        recv: Mutex<VecDeque<(Vec<u8>, SocketAddr)>>,
        sent: Mutex<Vec<(Vec<u8>, SocketAddr)>>,
        local_addr: SocketAddr,
        clock: Clock,
    }

    impl MockTransport {
        fn new(local_addr: SocketAddr) -> Self {
            Self {
                recv: Mutex::new(VecDeque::new()),
                sent: Mutex::new(Vec::new()),
                local_addr,
                clock: Clock::new(),
            }
        }

        fn with_clock(local_addr: SocketAddr, clock: Clock) -> Self {
            Self {
                recv: Mutex::new(VecDeque::new()),
                sent: Mutex::new(Vec::new()),
                local_addr,
                clock,
            }
        }

        fn clock(&self) -> Clock {
            self.clock.clone()
        }

        fn push_recv(&self, data: Vec<u8>, from: SocketAddr) {
            self.recv.lock().unwrap().push_back((data, from));
        }
    }

    impl UdpTransport for MockTransport {
        fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
            let mut q = self.recv.lock().unwrap();
            let (data, from) = q
                .pop_front()
                .ok_or_else(|| io::Error::new(io::ErrorKind::WouldBlock, "no queued datagrams"))?;
            let n = data.len().min(buf.len());
            buf[..n].copy_from_slice(&data[..n]);
            Ok((n, from))
        }

        fn recv_from_timeout(
            &self,
            buf: &mut [u8],
            _timeout: Duration,
        ) -> io::Result<(usize, SocketAddr)> {
            self.recv_from(buf)
        }

        fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
            self.sent.lock().unwrap().push((data.to_vec(), dst));
            Ok(())
        }

        fn local_addr(&self) -> io::Result<SocketAddr> {
            Ok(self.local_addr)
        }

        fn set_recv_timeout(&self, _timeout: Duration) -> io::Result<()> {
            Ok(())
        }

        fn recv_timeout(&self) -> io::Result<Option<Duration>> {
            Ok(None)
        }
    }

    /// Build a [`LinkRunner`] directly and expose it via `handle_datagram` /
    /// `drain_ready`. The runner's clock is owned by the returned mock so
    /// tests can advance emulated time through it.
    fn mock_runner(config: NetemConfig) -> (LinkRunner, Arc<MockTransport>) {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let captured = Arc::new(MockTransport::new(server_addr));
        let clock = captured.clock();
        let runner = LinkRunner::new(RunnerConfig {
            netem: config,
            server_addr,
            stats: Arc::new(AtomicCounters::default()),
            queue_len: Arc::new(AtomicU64::new(0)),
            blackout: Arc::new(AtomicBool::new(false)),
            stop: Arc::new(Mutex::new(false)),
            transport: Box::new(Arc::clone(&captured) as Arc<dyn UdpTransport>),
            clock: Some(clock),
        });
        (runner, captured)
    }

    /// Helper: run a single datagram through a [`LinkRunner`] and return the runner
    /// plus the send-side mock.
    fn one_packet_runner(
        data: &[u8],
        from: SocketAddr,
        config: NetemConfig,
    ) -> (LinkRunner, Arc<MockTransport>) {
        let (mut runner, sent) = mock_runner(config);
        runner.handle_datagram(data, from, sent.clock().now());
        (runner, sent)
    }

    /// Wait for a condition that the runner threads make true, bounded by a
    /// wall-clock deadline so a wedged runner fails the test instead of
    /// hanging. Only used to absorb thread-scheduling latency after the
    /// runner has consumed a `push_recv`; emulated time itself is driven by
    /// advancing the shared [`Clock`].
    fn wait_until(what: &str, cond: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !cond() {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            std::thread::yield_now();
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    #[test]
    fn limit_zero_is_unbounded() {
        let config = NetemConfig {
            latency: Duration::from_secs(1),
            queue_limit_pkts: 0,
            ..Default::default()
        };
        let (mut runner, sent) = one_packet_runner(
            b"x",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config,
        );
        // Pile 10 packets on the delayed heap with no draining.
        for i in 0..10u8 {
            runner.handle_datagram(
                &[i],
                SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
                Instant::now(),
            );
        }
        assert_eq!(runner.queue.len(), 11);
        assert_eq!(runner.stats.snapshot().overflow_dropped, 0);
        assert_eq!(runner.stats.snapshot().received, 11);
        drop(sent);
    }

    #[test]
    fn limit_tail_drops_and_counts_overflow() {
        let config = NetemConfig {
            latency: Duration::from_secs(1),
            queue_limit_pkts: 4,
            ..Default::default()
        };
        let (mut runner, sent) = one_packet_runner(
            b"x",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config,
        );
        // With queue_limit_pkts=4, the first 4 packets fill the queue; the 6th-10th are tail-dropped.
        for i in 0..10u8 {
            runner.handle_datagram(
                &[i],
                SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
                Instant::now(),
            );
        }
        assert_eq!(runner.queue.len(), 4);
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 11);
        assert_eq!(s.overflow_dropped, 7);
        assert_eq!(s.dropped, 0); // no loss model drops
        drop(sent);
    }

    #[test]
    fn overflow_drops_do_not_advance_shaper_clock_or_reorder_slot() {
        let config = NetemConfig {
            rate: 8_000, // 1 byte per ms
            queue_limit_pkts: 2,
            ..Default::default()
        };
        // Fill queue to capacity.
        let (mut runner, sent) = one_packet_runner(
            b"a",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config,
        );
        runner.handle_datagram(
            b"b",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            Instant::now(),
        );
        let first_link_free_at = runner.link_free_at;
        let first_counter = runner.reorder_counter;
        // The next packet must be tail-dropped, leaving state unchanged.
        runner.handle_datagram(
            b"overflow",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            Instant::now(),
        );
        assert_eq!(runner.link_free_at, first_link_free_at);
        assert_eq!(runner.reorder_counter, first_counter);
        assert_eq!(runner.stats.snapshot().overflow_dropped, 1);
        drop(sent);
    }

    #[test]
    fn limit_decisions_consume_no_prng_draws() {
        let config = NetemConfig {
            latency: Duration::from_secs(1),
            queue_limit_pkts: 1,
            ..Default::default()
        };
        // Gap/reorder/reorder_corr are zero so no reorder draws happen anyway;
        // this test primarily exercises that tail-drop returns early.
        let (mut runner, sent) = one_packet_runner(
            b"a",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config,
        );
        let rng_before = runner.rng;
        runner.handle_datagram(
            b"overflow",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            Instant::now(),
        );
        assert_eq!(runner.rng.s1, rng_before.s1);
        assert_eq!(runner.rng.s2, rng_before.s2);
        assert_eq!(runner.rng.s3, rng_before.s3);
        assert_eq!(runner.rng.s4, rng_before.s4);
        drop(sent);
    }

    #[test]
    fn blackout_gates_at_forward_time_and_toggles_instantly() {
        let config = NetemConfig {
            // Latency small enough that the first packet has drained by the time we
            // inspect, while the second is gated.
            latency: Duration::from_millis(1),
            ..Default::default()
        };
        let (mut runner, sent) = one_packet_runner(
            b"before",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            config.clone(),
        );
        let clock = sent.clock();
        runner.blackout.store(true, Ordering::Relaxed);
        runner.handle_datagram(
            b"after",
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 1234)),
            clock.now(),
        );
        // Advance past the 1 ms latency so the first packet drains.
        clock.advance(Duration::from_millis(20));
        runner.drain_ready(clock.now());
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 2);
        assert_eq!(s.dropped, 1);
        assert_eq!(s.forwarded, 1);
        assert_eq!(sent.sent.lock().unwrap().len(), 1);
        drop(sent);
    }

    #[test]
    fn netem_pair_exposes_per_direction_queue_depth() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 3000));
        let c2s = NetemConfig {
            latency: Duration::from_secs(1),
            queue_limit_pkts: 5,
            ..Default::default()
        };
        let s2c = NetemConfig {
            latency: Duration::from_millis(500),
            queue_limit_pkts: 2,
            ..Default::default()
        };

        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 4001)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let client_addr = client_sock.local_addr().unwrap();
        let pair = NetemPair::spawn_from_sockets(
            server_addr,
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: None,
                s2c_shared: None,
                clock: Some(clock),
            },
        )
        .unwrap();

        // Pump c2s: send 3 client packets; with queue_limit_pkts 5 they all queue.
        for i in 0..3u8 {
            client_sock.push_recv(
                vec![i],
                SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5000)),
            );
        }
        // The runner ingests them at clock time 0 behind the 1 s latency; they
        // stay queued because we never advance the clock.
        wait_until("c2s runner to receive all 3 packets", || {
            pair.stats_c2s().received == 3
        });
        assert_eq!(pair.queue_len_c2s(), 3);
        assert_eq!(pair.queue_len_s2c(), 0);
        pair.stop();
    }

    #[test]
    fn pair_blackout_toggles_at_runtime() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 3100));
        let c2s = NetemConfig {
            latency: Duration::from_millis(2),
            ..Default::default()
        };
        let s2c = NetemConfig {
            latency: Duration::from_millis(2),
            ..Default::default()
        };

        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 4002)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let client_addr = client_sock.local_addr().unwrap();
        let pair = NetemPair::spawn_from_sockets(
            server_addr,
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: None,
                s2c_shared: None,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();

        // First packet forwarded normally.
        client_sock.push_recv(
            vec![1],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001)),
        );
        wait_until("first packet received", || pair.stats_c2s().received >= 1);
        clock.advance(Duration::from_millis(5));
        wait_until("first packet forwarded", || pair.stats_c2s().forwarded >= 1);

        // Enable blackout and send more packets.
        pair.set_blackout_c2s(true);
        client_sock.push_recv(
            vec![2],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001)),
        );
        client_sock.push_recv(
            vec![3],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001)),
        );
        wait_until("gated packets received", || pair.stats_c2s().received == 3);
        let gated = pair.stats_c2s();
        assert_eq!(gated.received, 3);
        assert_eq!(gated.dropped, 2);

        // Disable blackout; subsequent packets forward again.
        pair.set_blackout_c2s(false);
        client_sock.push_recv(
            vec![4],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001)),
        );
        wait_until("final packet received", || pair.stats_c2s().received == 4);
        clock.advance(Duration::from_millis(5));
        wait_until("final packet forwarded", || pair.stats_c2s().forwarded >= 2);
        let final_stats = pair.stats_c2s();
        assert!(final_stats.forwarded >= 2);
        assert_eq!(final_stats.received, 4);
        pair.stop();
    }

    // ─────────────────────────── shared-shaper tests ────────────────────────

    #[test]
    fn shared_shaper_rate_must_be_nonzero() {
        let result = std::panic::catch_unwind(|| BottleneckShaper::new(0, 0));
        assert!(result.is_err());
    }

    #[test]
    fn shared_shaper_exact_fifo_arithmetic() {
        let shaper = BottleneckShaper::new(800_000, 0);
        let base = Instant::now();
        let len = 1000usize;
        let mut last = None;
        for i in 0..10 {
            let t = shaper.schedule(base, len).unwrap();
            let expected = base + Duration::from_millis(10 * (i + 1));
            let diff = if t >= expected {
                t - expected
            } else {
                expected - t
            };
            assert!(
                diff <= Duration::from_micros(1),
                "packet {i} expected {expected:?} got {t:?}"
            );
            last = Some(t);
        }
        assert_eq!(shaper.dropped(), 0);
        let aggregate = last.unwrap() - base;
        assert!(
            aggregate >= Duration::from_millis(100)
                && aggregate <= Duration::from_millis(100) + Duration::from_micros(10),
            "aggregate serialization {aggregate:?}"
        );
    }

    #[test]
    fn shared_shaper_tail_drop_at_byte_limit() {
        let shaper = BottleneckShaper::new(8_000, 120);
        let base = Instant::now();
        // 100 B serializes in 100 ms and fits.
        let t1 = shaper.schedule(base, 100).unwrap();
        assert_eq!(shaper.dropped(), 0);
        assert!(t1 >= base);
        // 50 B at the same instant would exceed the 120 B shared buffer.
        assert!(shaper.schedule(base, 50).is_none());
        assert_eq!(shaper.dropped(), 1);
        // After the first packet drains, another 100 B is accepted.
        let base2 = base + Duration::from_millis(100);
        let t2 = shaper.schedule(base2, 100).unwrap();
        assert_eq!(shaper.dropped(), 1);
        assert!(t2 >= base2);
    }

    #[test]
    fn shared_shaper_backlog_bytes_reports_and_drains() {
        let shaper = BottleneckShaper::new(8_000, 0);
        let base = Instant::now();
        assert_eq!(shaper.backlog_bytes(base), 0);
        shaper.schedule(base, 100);
        assert_eq!(shaper.backlog_bytes(base), 100);
        assert_eq!(shaper.backlog_bytes(base + Duration::from_millis(50)), 50);
        assert_eq!(shaper.backlog_bytes(base + Duration::from_millis(100)), 0);
    }

    #[test]
    fn shared_shaper_clone_shares_state() {
        let a = BottleneckShaper::new(800_000, 0);
        let b = a.clone();
        let base = Instant::now();
        a.schedule(base, 1000);
        assert_eq!(b.backlog_bytes(base), 1000);
        assert_eq!(b.dropped(), 0);
        assert_eq!(b.rate_bps(), 800_000);
    }

    #[test]
    fn shared_shaper_overflow_counts_in_direction_stats() {
        let shared = BottleneckShaper::new(8_000, 80);
        let c2s = NetemConfig {
            rate: 0,
            ..Default::default()
        };
        let s2c = NetemConfig::default();
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6000));
        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6001)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let client_addr = client_sock.local_addr().unwrap();
        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7000));
        let pair = NetemPair::spawn_from_sockets(
            server_addr,
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: Some(shared),
                s2c_shared: None,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();

        // 100 B > 80 B limit: drop.
        client_sock.push_recv(vec![0u8; 100], from);
        // 60 B fits and serializes for 60 ms.
        client_sock.push_recv(vec![1u8; 60], from);
        // 30 B while the 60 B packet is still draining: 60 + 30 > 80 B: drop.
        client_sock.push_recv(vec![2u8; 30], from);
        // Wait for the runner to ingest all three at clock time 0, so the
        // overflow decisions happen against the fresh 80 B shared buffer.
        wait_until("c2s runner to receive all 3 packets", || {
            pair.stats_c2s().received == 3
        });
        // Advance past the 60 ms serialization so the 60 B packet drains.
        clock.advance(Duration::from_millis(150));
        wait_until("60 B packet forwarded", || pair.stats_c2s().forwarded == 1);
        pair.stop();

        let stats = pair.stats_c2s();
        assert_eq!(stats.received, 3);
        assert_eq!(stats.forwarded, 1, "only the 60 B packet should exit");
        assert_eq!(
            stats.overflow_dropped, 2,
            "shared-buffer overflow should count as overflow_dropped"
        );
        assert_eq!(stats.dropped, 0, "overflow drops are not loss-model drops");
    }

    #[test]
    #[should_panic(expected = "double-shape")]
    fn spawn_shared_panics_on_c2s_double_shape() {
        let c2s = NetemConfig {
            rate: 1_000_000,
            ..Default::default()
        };
        let server = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6100));
        let _ = NetemPair::spawn_shared(
            server,
            c2s,
            NetemConfig::default(),
            Some(BottleneckShaper::new(1_000_000, 0)),
            None,
        );
    }

    #[test]
    #[should_panic(expected = "double-shape")]
    fn spawn_shared_panics_on_s2c_double_shape() {
        let s2c = NetemConfig {
            rate: 1_000_000,
            ..Default::default()
        };
        let server = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6200));
        let _ = NetemPair::spawn_shared(
            server,
            NetemConfig::default(),
            s2c,
            None,
            Some(BottleneckShaper::new(1_000_000, 0)),
        );
    }

    #[test]
    fn spawn_shared_two_udp_flows_serialize_to_shared_rate_and_route_correctly() {
        let shaper = BottleneckShaper::new(400 * 1024 * 8, 0);
        let packet_size = 1024usize;
        let packets_per_flow = 50usize;

        let server_a = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        let server_b = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        let addr_a = server_a.local_addr().unwrap();
        let addr_b = server_b.local_addr().unwrap();

        std::thread::spawn(move || {
            let mut buf = [0u8; 2048];
            while let Ok((n, from)) = server_a.recv_from(&mut buf) {
                let _ = server_a.send_to(&buf[..n], from);
            }
        });
        std::thread::spawn(move || {
            let mut buf = [0u8; 2048];
            while let Ok((n, from)) = server_b.recv_from(&mut buf) {
                let _ = server_b.send_to(&buf[..n], from);
            }
        });

        let pair_a = NetemPair::spawn_shared(
            addr_a,
            NetemConfig::default(),
            NetemConfig::default(),
            Some(shaper.clone()),
            None,
        )
        .unwrap();
        let pair_b = NetemPair::spawn_shared(
            addr_b,
            NetemConfig::default(),
            NetemConfig::default(),
            Some(shaper.clone()),
            None,
        )
        .unwrap();

        let client_a = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        let client_b = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        client_a
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        client_b
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();

        // Tag each flow with a distinct marker byte so replies can be verified.
        let mut payload_a: Vec<u8> = vec![0xAA; packet_size];
        payload_a[0] = 0xAA;
        let mut payload_b: Vec<u8> = vec![0xBB; packet_size];
        payload_b[0] = 0xBB;

        let start = Instant::now();
        for _ in 0..packets_per_flow {
            client_a.send_to(&payload_a, pair_a.client_addr()).unwrap();
            client_b.send_to(&payload_b, pair_b.client_addr()).unwrap();
            // Pace sends to avoid kernel local-LAN drop. This test drives real
            // OS sockets (no MockTransport), so there is no injectable clock:
            // the wall-clock sleep both paces the loop and lets the shared
            // shaper's real-time serialization clock run.
            std::thread::sleep(Duration::from_micros(200));
        }

        let phase_start = Instant::now();
        let mut buf = [0u8; 2048];
        let mut got_a: Vec<Vec<u8>> = Vec::new();
        let mut got_b: Vec<Vec<u8>> = Vec::new();
        while got_a.len() < packets_per_flow || got_b.len() < packets_per_flow {
            assert!(
                phase_start.elapsed() < Duration::from_secs(5),
                "replies not received within 5 s phase deadline"
            );
            if let Ok((n, _)) = client_a.recv_from(&mut buf) {
                assert_eq!(n, packet_size, "flow A reply size mismatch");
                assert_eq!(buf[0], 0xAA, "flow A reply routed to wrong socket");
                got_a.push(buf[..n].to_vec());
            }
            if let Ok((n, _)) = client_b.recv_from(&mut buf) {
                assert_eq!(n, packet_size, "flow B reply size mismatch");
                assert_eq!(buf[0], 0xBB, "flow B reply routed to wrong socket");
                got_b.push(buf[..n].to_vec());
            }
        }
        let elapsed = start.elapsed();
        pair_a.stop();
        pair_b.stop();

        assert_eq!(got_a.len(), packets_per_flow);
        assert_eq!(got_b.len(), packets_per_flow);
        // Verify every reply carries the correct tag and size.
        for reply in &got_a {
            assert_eq!(reply.len(), packet_size);
            assert_eq!(reply[0], 0xAA);
        }
        for reply in &got_b {
            assert_eq!(reply.len(), packet_size);
            assert_eq!(reply[0], 0xBB);
        }
        let stats_a = pair_a.stats_c2s();
        let stats_b = pair_b.stats_c2s();
        assert_eq!(stats_a.forwarded, packets_per_flow as u64);
        assert_eq!(stats_b.forwarded, packets_per_flow as u64);
        assert_eq!(stats_a.overflow_dropped + stats_b.overflow_dropped, 0);

        assert!(
            elapsed >= Duration::from_millis(200),
            "shared shaper should serialize both flows, elapsed {elapsed:?}"
        );
        assert!(
            elapsed <= Duration::from_secs(3),
            "shared shaper should finish within 3 s, elapsed {elapsed:?}"
        );
    }

    /// Two flows queue behind one bottleneck in arrival order.
    /// Packets from different source addresses are forwarded in the order
    /// they arrive at the single paired runner.
    #[test]
    fn two_flows_queue_arrival_order_shared_bottleneck() {
        let shaper = BottleneckShaper::new(8_000_000, 0);
        let c2s = NetemConfig {
            rate: 0,
            ..Default::default()
        };
        let s2c = NetemConfig::default();
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6300));
        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6301)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let client_addr = client_sock.local_addr().unwrap();

        let pair = NetemPair::spawn_from_sockets(
            server_addr,
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: Some(shaper.clone()),
                s2c_shared: None,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();

        let from_a = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7000));
        let from_b = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7001));

        // Push interleaved from two different source addresses.
        client_sock.push_recv(vec![0xAA, 0x01], from_a);
        client_sock.push_recv(vec![0xBB, 0x01], from_b);
        client_sock.push_recv(vec![0xAA, 0x02], from_a);
        client_sock.push_recv(vec![0xBB, 0x02], from_b);
        client_sock.push_recv(vec![0xAA, 0x03], from_a);

        // Wait for the runner to ingest all five at clock time 0 so the shared
        // bottleneck sees them in arrival order, then advance well past the
        // ~10 µs it takes to serialize five 2-byte packets at 8 Mbps.
        wait_until("c2s runner to receive all 5 packets", || {
            pair.stats_c2s().received == 5
        });
        clock.advance(Duration::from_millis(5));
        wait_until("all 5 packets forwarded", || {
            server_sock.sent.lock().unwrap().len() == 5
        });
        pair.stop();

        let sent = server_sock.sent.lock().unwrap();
        let tags: Vec<u8> = sent.iter().map(|(data, _)| data[0]).collect();
        assert_eq!(
            tags,
            vec![0xAA, 0xBB, 0xAA, 0xBB, 0xAA],
            "packets must be forwarded in arrival order: {tags:?}"
        );
    }

    /// Bottleneck overflow (tail-drop at shared byte limit) increments
    /// `overflow_dropped` on the direction carrying the overflowed packet.
    #[test]
    fn shared_bottleneck_overflow_counts_overflow_dropped() {
        let shaper = BottleneckShaper::new(8_000, 80);
        let c2s = NetemConfig {
            rate: 0,
            ..Default::default()
        };
        let s2c = NetemConfig::default();
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6400));
        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 6401)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let client_addr = client_sock.local_addr().unwrap();

        let pair = NetemPair::spawn_from_sockets(
            server_addr,
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: Some(shaper.clone()),
                s2c_shared: None,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();

        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7000));

        // 50 B + 40 B = 90 > 80 B limit: second packet overflows.
        client_sock.push_recv(vec![0u8; 50], from);
        client_sock.push_recv(vec![0u8; 40], from);

        // Wait for the runner to ingest both at clock time 0 so the 40 B packet
        // overflows against the fresh 80 B shared buffer; then advance past the
        // 50 ms it takes to serialize the 50 B packet.
        wait_until("c2s runner to receive both packets", || {
            pair.stats_c2s().received == 2
        });
        clock.advance(Duration::from_millis(100));
        wait_until("50 B packet forwarded", || pair.stats_c2s().forwarded == 1);
        pair.stop();

        let stats = pair.stats_c2s();
        let sent = server_sock.sent.lock().unwrap();
        let fwd: Vec<usize> = sent.iter().map(|(data, _)| data.len()).collect();
        assert_eq!(fwd, vec![50], "only the 50 B packet should forward");
        assert_eq!(stats.forwarded, 1);
        assert_eq!(
            stats.overflow_dropped, 1,
            "the overflowed 40 B packet must increment overflow_dropped"
        );
    }
}
