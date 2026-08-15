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
#![warn(clippy::disallowed_methods, clippy::disallowed_types)]

pub mod report;

mod loss;
mod queue;
mod rng;
mod shaper;

use std::cmp::Reverse;
use std::collections::{BinaryHeap, VecDeque};
use std::io;
use std::net::{SocketAddr, SocketAddrV4};
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use parking_lot::Mutex as ParkingMutex;

use loss::FourStateState;
use queue::Queued;
use rng::CorRng;
#[cfg(test)]
use shaper::exceeds_byte_limit;
use shaper::{sample_delay, serialization_delay};

pub use loss::{FourStateLoss, LossModel};
pub use rng::RndState;
pub use shaper::BottleneckShaper;

/// Default receive poll used when nothing is queued, and the upper bound for
/// how long the runner sleeps before it must re-check the queue for a packet
/// whose deadline is about to arrive.
const RUNNER_IDLE_POLL: Duration = Duration::from_millis(5);
/// Hard per-direction cap on recycled packet payload buffers; drained buffers
/// beyond this bound are dropped so a fast flow cannot grow the cache forever.
const MAX_REUSED_PACKET_BUFFERS: usize = 64;
/// Total payload capacity bound for the FIFO recycle pool; a drained buffer is
/// recycled only when both the count and the total capacity bounds remain
/// satisfied, so a flow of large packets cannot grow the cache unboundedly.
const MAX_FIFO_REUSED_PACKET_BUFFER_BYTES: usize = 4 * 1024 * 1024;
/// Per-direction cap on recycled FIFO payload buffers.
const MAX_FIFO_REUSED_PACKET_BUFFERS: usize = 4096;

/// FIFO queue used by the monotonic-deadline forwarding path.
///
/// When jitter and reorder-gap scheduling are both zero, deadlines are
/// monotonic in arrival order, so a `VecDeque` preserves exactly the same
/// packet order as the delay heap while avoiding per-packet heap insertion
/// cost. Drained payload buffers are recycled subject to both the count and
/// the total-capacity bounds.
#[derive(Default)]
struct FifoQueue {
    packets: VecDeque<Queued>,
    reused_packet_buffers: Vec<Vec<u8>>,
    reused_capacity_bytes: usize,
}

impl FifoQueue {
    /// Recycle a drained payload buffer when both the count and the total
    /// capacity bounds remain satisfied; otherwise drop it so a fast flow
    /// cannot grow the cache forever.
    fn recycle_packet_buffer(&mut self, mut data: Vec<u8>) {
        if self.reused_packet_buffers.len() < MAX_FIFO_REUSED_PACKET_BUFFERS
            && self.reused_capacity_bytes.saturating_add(data.capacity())
                <= MAX_FIFO_REUSED_PACKET_BUFFER_BYTES
        {
            data.clear();
            self.reused_capacity_bytes += data.capacity();
            self.reused_packet_buffers.push(data);
        }
    }
}

// ──────────────────────────── UDP transport ────────────────────────────

/// Abstract UDP datagram transport.
///
/// Implemented as a trait object (`Box<dyn UdpTransport>`) so the harness can
/// run against either real OS sockets or an in-memory loopback in tests.
pub trait UdpTransport: Send + Sync + 'static {
    /// Connect the transport to a fixed peer. Once connected, receives report
    /// that peer as the source and sends to any other address fail. The
    /// default implementation is a no-op so multi-source transports (e.g. the
    /// in-memory test mock) are unaffected.
    fn connect_peer(&self, _peer: SocketAddr) -> io::Result<()> {
        Ok(())
    }

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
    fn connect_peer(&self, peer: SocketAddr) -> io::Result<()> {
        (**self).connect_peer(peer)
    }
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
///
/// Receive timeouts are serialized through a [`ParkingMutex`]-protected
/// [`ReceiveTimeoutState`] so that repeated receives with the already-
/// installed timeout perform no syscall, and temporary timeouts used by
/// [`recv_from_timeout`](UdpTransport::recv_from_timeout) restore the
/// configured default afterwards.
#[derive(Debug)]
pub struct StdUdpTransport {
    sock: std::net::UdpSocket,
    connect_lock: ParkingMutex<()>,
    connected_peer: OnceLock<SocketAddr>,
    receive_timeout: ParkingMutex<ReceiveTimeoutState>,
    #[cfg(test)]
    receive_timeout_installs: AtomicU64,
}

#[derive(Debug)]
struct ReceiveTimeoutState {
    configured: Option<Duration>,
    effective: Option<Duration>,
}

impl StdUdpTransport {
    pub fn bind(addr: SocketAddr) -> io::Result<Self> {
        let sock = std::net::UdpSocket::bind(addr)?;
        sock.set_nonblocking(false)?;
        sock.set_read_timeout(Some(RUNNER_IDLE_POLL))?;
        Ok(Self {
            sock,
            connect_lock: ParkingMutex::new(()),
            connected_peer: OnceLock::new(),
            receive_timeout: ParkingMutex::new(ReceiveTimeoutState {
                configured: Some(RUNNER_IDLE_POLL),
                effective: Some(RUNNER_IDLE_POLL),
            }),
            #[cfg(test)]
            receive_timeout_installs: AtomicU64::new(0),
        })
    }

    /// Install `timeout` on the socket unless it is already effective,
    /// tracking the effective value so repeated identical installs are no-ops.
    fn install_receive_timeout(
        &self,
        state: &mut ReceiveTimeoutState,
        timeout: Option<Duration>,
    ) -> io::Result<()> {
        if state.effective == timeout {
            return Ok(());
        }
        self.sock.set_read_timeout(timeout)?;
        state.effective = timeout;
        #[cfg(test)]
        self.receive_timeout_installs
            .fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

impl UdpTransport for StdUdpTransport {
    fn connect_peer(&self, peer: SocketAddr) -> io::Result<()> {
        let _connect = self.connect_lock.lock();
        if let Some(connected) = self.connected_peer.get() {
            return if *connected == peer {
                Ok(())
            } else {
                Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("UDP transport is already connected to {connected}"),
                ))
            };
        }
        self.sock.connect(peer)?;
        self.connected_peer
            .set(peer)
            .expect("connect lock serializes connected-peer initialization");
        Ok(())
    }

    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
        let mut state = self.receive_timeout.lock();
        let configured = state.configured;
        self.install_receive_timeout(&mut state, configured)?;
        if let Some(peer) = self.connected_peer.get().copied() {
            self.sock.recv(buf).map(|len| (len, peer))
        } else {
            self.sock.recv_from(buf)
        }
    }

    fn recv_from_timeout(
        &self,
        buf: &mut [u8],
        timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)> {
        let mut state = self.receive_timeout.lock();
        self.install_receive_timeout(&mut state, Some(timeout))?;
        let res = if let Some(peer) = self.connected_peer.get().copied() {
            self.sock.recv(buf).map(|len| (len, peer))
        } else {
            self.sock.recv_from(buf)
        };
        let configured = state.configured;
        self.install_receive_timeout(&mut state, configured)?;
        res
    }

    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
        match self.connected_peer.get() {
            Some(peer) if *peer == dst => {
                self.sock.send(data)?;
            }
            Some(peer) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("UDP transport is connected to {peer}, not {dst}"),
                ));
            }
            None => {
                self.sock.send_to(data, dst)?;
            }
        }
        Ok(())
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.sock.local_addr()
    }

    fn set_recv_timeout(&self, timeout: Duration) -> io::Result<()> {
        let mut state = self.receive_timeout.lock();
        state.configured = if timeout.is_zero() {
            None
        } else {
            Some(timeout)
        };
        let configured = state.configured;
        self.install_receive_timeout(&mut state, configured)
    }

    fn recv_timeout(&self) -> io::Result<Option<Duration>> {
        Ok(self.receive_timeout.lock().configured)
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

    /// Increment a counter owned by exactly one writer (the direction's runner
    /// thread): a relaxed load + store instead of a fetch_add round-trip.
    #[inline]
    fn inc_single_writer(&self, f: impl Fn(&AtomicCounters) -> &AtomicU64) {
        let counter = f(self);
        counter.store(
            counter.load(Ordering::Relaxed).wrapping_add(1),
            Ordering::Relaxed,
        );
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

/// A running emulated link. Dropping the link stops the proxy thread and
/// joins it (equivalent to calling [`NetemLink::stop`]).
pub struct NetemLink {
    client_addr: SocketAddr,
    server_addr: SocketAddr,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    /// Handle of the spawned runner thread; `None` once joined.
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
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
        let stop = Arc::new(AtomicBool::new(false));

        let link = Self {
            client_addr,
            server_addr,
            stats: Arc::clone(&stats),
            queue_len: Arc::clone(&queue_len),
            blackout: Arc::clone(&blackout),
            stop: Arc::clone(&stop),
            thread: Mutex::new(None),
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
        let thread = std::thread::Builder::new()
            .name("netem-link".into())
            .spawn(move || runner.run())?;
        *link.thread.lock().unwrap() = Some(thread);

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

    /// Signal the proxy thread to stop and wait for it to exit. Idempotent:
    /// once joined, subsequent calls are no-ops.
    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for NetemLink {
    fn drop(&mut self) {
        self.stop();
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
    /// True when the config has no shared shaper and no stochastic or
    /// scheduling impairment, so packets can be forwarded without touching
    /// the heap at all. `max_datagram_size` is a deterministic filter and does
    /// not disqualify the direct path.
    direct_forward: bool,
    /// True when the config has stochastic work (duplication or loss) but no
    /// scheduling, so duplicate/loss draws can be applied directly without
    /// enqueueing into a delay queue.
    direct_stochastic: bool,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
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
    /// Recycled drained payload buffers, bounded at MAX_REUSED_PACKET_BUFFERS.
    reused_packet_buffers: Vec<Vec<u8>>,
    reorder_counter: u32,
    seq: u64,
}

impl NetemState {
    fn new(
        config: NetemConfig,
        stats: Arc<AtomicCounters>,
        queue_len: Arc<AtomicU64>,
        blackout: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
        shared: Option<BottleneckShaper>,
        clock: Option<Clock>,
    ) -> Self {
        let no_scheduling = shared.is_none()
            && config.latency.is_zero()
            && config.jitter.is_zero()
            && config.reorder == 0
            && config.reorder_gap_pkts == 0
            && config.rate == 0
            && config.queue_limit_pkts == 0;
        let has_stochastic_work = config.duplicate != 0
            || config.loss != 0
            || !matches!(config.loss_model, LossModel::Random);
        let direct_stochastic = no_scheduling && has_stochastic_work;
        let direct_forward = no_scheduling && !has_stochastic_work;
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
            direct_forward,
            direct_stochastic,
            stats,
            queue_len,
            blackout,
            stop,
            shared,
            rng,
            state: FourStateState::default(),
            clock,
            queue: BinaryHeap::new(),
            reused_packet_buffers: Vec::new(),
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
        self.stop.load(Ordering::Relaxed)
    }

    /// Forward `data` to `dst` without touching the heap when the config is
    /// clean. Returns `true` when the packet was handled (forwarded, dropped
    /// by the deterministic size filter, or gated by blackout); `false` when
    /// the caller must fall through to the queued impairment path.
    fn try_direct_forward(
        &self,
        data: &[u8],
        dst: Option<SocketAddr>,
        send: &dyn UdpTransport,
    ) -> bool {
        if !self.direct_forward {
            return false;
        }
        self.forward_direct(data, dst, send)
    }

    /// Clean no-clock direct loop: count the datagram, apply the deterministic
    /// size filter and the blackout gate, then forward immediately without
    /// touching the heap or reading the clock.
    fn forward_direct(
        &self,
        data: &[u8],
        dst: Option<SocketAddr>,
        send: &dyn UdpTransport,
    ) -> bool {
        self.stats.inc_single_writer(|s| &s.received);
        if self.config.max_datagram_size > 0 && data.len() > self.config.max_datagram_size {
            self.stats.inc_single_writer(|s| &s.dropped);
            return true;
        }
        if self.blackout.load(Ordering::Relaxed) {
            self.stats.inc_single_writer(|s| &s.dropped);
            return true;
        }
        if let Some(dst) = dst
            && send.send_to(data, dst).is_ok()
        {
            self.stats.inc_single_writer(|s| &s.forwarded);
        }
        true
    }

    /// Apply duplication and loss to a received datagram, returning the number
    /// of surviving copies (0 = lost, 1 = forwarded once, 2 = duplicated).
    /// Preserves the kernel's duplicate-before-loss draw order and the
    /// duplicated/dropped counters; shared by the heap, FIFO, and direct
    /// stochastic paths so every path consumes PRNG draws identically.
    fn surviving_copies(&mut self) -> u32 {
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
        count
    }

    /// Stochastic-only direct loop: apply duplication then loss directly with
    /// no scheduling, forwarding every surviving copy immediately. Retains the
    /// `max_datagram_size`/blackout checks and the duplicate-before-loss PRNG
    /// and counter order of the queued pipeline.
    fn forward_stochastic_direct(
        &mut self,
        data: &[u8],
        dst: Option<SocketAddr>,
        send: &dyn UdpTransport,
    ) -> bool {
        self.stats.inc_single_writer(|s| &s.received);
        if self.config.max_datagram_size > 0 && data.len() > self.config.max_datagram_size {
            self.stats.inc_single_writer(|s| &s.dropped);
            return true;
        }
        if self.blackout.load(Ordering::Relaxed) {
            self.stats.inc_single_writer(|s| &s.dropped);
            return true;
        }
        let count = self.surviving_copies();
        for _ in 0..count {
            if let Some(dst) = dst
                && send.send_to(data, dst).is_ok()
            {
                self.stats.inc_single_writer(|s| &s.forwarded);
            }
        }
        true
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

        let count = self.surviving_copies();
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

        // Reuse a recycled drained payload buffer instead of allocating a
        // fresh one; `extend_from_slice` keeps the pooled allocation when its
        // capacity is sufficient.
        let mut packet = self.reused_packet_buffers.pop().unwrap_or_default();
        packet.extend_from_slice(data);
        let item = Queued {
            time_to_send,
            seq: self.seq,
            data: packet,
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
            let Reverse(Queued { mut data, dst, .. }) = self.queue.pop().unwrap();
            self.queue_len
                .store(self.queue.len() as u64, Ordering::Relaxed);
            if send.send_to(&data, dst).is_ok() {
                self.stats.inc(|s| &s.forwarded);
            }
            data.clear();
            if self.reused_packet_buffers.len() < MAX_REUSED_PACKET_BUFFERS {
                self.reused_packet_buffers.push(data);
            }
        }
    }

    /// How long the runner may sleep before it must wake up again: until the
    /// next queued packet's deadline, capped at [`RUNNER_IDLE_POLL`] (and at
    /// the idle poll when the queue is empty). A zero wait means a packet is
    /// due right now and the caller should re-drain instead of polling.
    fn next_receive_wait(&self, now: Instant) -> Duration {
        self.queue
            .peek()
            .map(|queued| queued.0.time_to_send.saturating_duration_since(now))
            .unwrap_or(RUNNER_IDLE_POLL)
            .min(RUNNER_IDLE_POLL)
    }

    // ─────────────────────── FIFO scheduling path ───────────────────────

    /// True when every scheduled deadline is monotonic in arrival order, so a
    /// FIFO queue preserves the exact packet order of the delay heap: jitter
    /// and reorder-gap scheduling are both zero. Direct paths are dispatched
    /// before this is consulted.
    fn uses_fifo_scheduling(&self) -> bool {
        !self.direct_forward
            && !self.direct_stochastic
            && self.config.jitter.is_zero()
            && self.config.reorder_gap_pkts == 0
    }

    /// Same impairment pipeline as [`NetemState::handle_datagram`] but
    /// enqueueing into a [`FifoQueue`]: duplicate-before-loss PRNG draws and
    /// counters are identical, only the storage differs.
    fn handle_datagram_fifo(
        &mut self,
        data: &[u8],
        now: Instant,
        dst: Option<SocketAddr>,
        fifo: &mut FifoQueue,
    ) {
        self.stats.inc(|s| &s.received);

        // ── max datagram size filter ──────────────────────────────────
        if self.config.max_datagram_size > 0 && data.len() > self.config.max_datagram_size {
            self.stats.inc(|s| &s.dropped);
            return;
        }

        // ── blackout gate ────────────────────────────────────────────
        if self.blackout.load(Ordering::Relaxed) {
            self.stats.inc(|s| &s.dropped);
            return;
        }

        let count = self.surviving_copies();
        if count == 0 {
            return;
        }
        for _ in 0..count {
            self.enqueue_fifo(data, now, dst, fifo);
        }
    }

    /// FIFO analogue of [`NetemState::enqueue`]. With jitter and reorder-gap
    /// both zero, the delay is the fixed `latency` (no PRNG draw), deadlines
    /// are monotonic, and the queue limit tail-drops before any scheduling
    /// arithmetic.
    fn enqueue_fifo(
        &mut self,
        data: &[u8],
        now: Instant,
        dst: Option<SocketAddr>,
        fifo: &mut FifoQueue,
    ) {
        // ── queue limit (tail-drop) ──────────────────────────────────
        if self.config.queue_limit_pkts != 0 && fifo.packets.len() >= self.config.queue_limit_pkts {
            self.stats.inc(|s| &s.overflow_dropped);
            return;
        }

        let delay = self.config.latency;
        if !delay.is_zero() {
            self.stats.inc(|s| &s.delayed);
        }

        let time_to_send = if let Some(shared) = &self.shared {
            // ── shared bottleneck (normal branch only) ──────────────────
            // Shape at packet arrival time, then add the fixed propagation
            // delay; identical scheduling to the heap path.
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
        } else if let Some(serialize) = serialization_delay(data.len(), self.config.rate) {
            // ── rate shaping (normal branch only) ───────────────────
            let base = now + delay;
            let earliest = base.max(self.link_free_at);
            let t = earliest + serialize;
            self.link_free_at = t;
            if t != base {
                self.stats.inc(|s| &s.rate_limited);
            }
            t
        } else {
            now + delay
        };

        let Some(dst) = dst else {
            // No known destination yet (s2c before the first client packet).
            return;
        };

        // Reuse a recycled drained payload buffer; `extend_from_slice` keeps
        // the pooled allocation when its capacity is sufficient.
        let mut packet = fifo.reused_packet_buffers.pop().unwrap_or_default();
        packet.extend_from_slice(data);
        let item = Queued {
            time_to_send,
            seq: self.seq,
            data: packet,
            dst,
        };
        self.seq = self.seq.wrapping_add(1);
        fifo.packets.push_back(item);
        self.queue_len
            .store(fifo.packets.len() as u64, Ordering::Relaxed);
    }

    /// Drain every FIFO packet whose deadline has passed, forwarding in
    /// arrival order and recycling drained payload buffers subject to the FIFO
    /// count and capacity bounds.
    fn drain_ready_fifo(&mut self, fifo: &mut FifoQueue, now: Instant, send: &dyn UdpTransport) {
        while let Some(front) = fifo.packets.front() {
            if front.time_to_send > now {
                break;
            }
            let Queued { data, dst, .. } = fifo.packets.pop_front().unwrap();
            self.queue_len
                .store(fifo.packets.len() as u64, Ordering::Relaxed);
            if send.send_to(&data, dst).is_ok() {
                self.stats.inc(|s| &s.forwarded);
            }
            fifo.recycle_packet_buffer(data);
        }
    }

    /// How long the runner may sleep before it must re-check the FIFO: until
    /// the front packet's deadline, capped at [`RUNNER_IDLE_POLL`] (and at the
    /// idle poll when the FIFO is empty).
    fn next_receive_wait_fifo(&self, fifo: &FifoQueue, now: Instant) -> Duration {
        fifo.packets
            .front()
            .map(|queued| queued.time_to_send.saturating_duration_since(now))
            .unwrap_or(RUNNER_IDLE_POLL)
            .min(RUNNER_IDLE_POLL)
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
    stop: Arc<AtomicBool>,
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
        // Dispatch once: the config never changes, so each runner picks the
        // cheapest loop that preserves its impairment semantics. Stochastic-
        // only and clean configs avoid the delay heap entirely; monotonic
        // deadlines use the FIFO queue; jitter/reorder stay on the heap.
        if self.pipeline.direct_stochastic {
            self.run_stochastic_direct(&mut buf);
        } else if self.pipeline.uses_fifo_scheduling() {
            self.run_fifo(&mut buf);
        } else if self.pipeline.direct_forward {
            self.run_direct(&mut buf);
        } else {
            self.run_heap(&mut buf);
        }
    }

    /// Clean no-clock direct loop: receives with the transport's default
    /// timeout and forwards every datagram without reading the clock or
    /// touching a queue.
    fn run_direct(&mut self, buf: &mut [u8]) {
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.transport.recv_from(buf) {
                Ok((n, _from)) => {
                    self.pipeline.forward_direct(
                        &buf[..n],
                        Some(self.server_addr),
                        &*self.transport,
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

    /// Stochastic-only direct loop: applies duplicate then loss directly
    /// without scheduling, again without reading the clock.
    fn run_stochastic_direct(&mut self, buf: &mut [u8]) {
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.transport.recv_from(buf) {
                Ok((n, _from)) => {
                    self.pipeline.forward_stochastic_direct(
                        &buf[..n],
                        Some(self.server_addr),
                        &*self.transport,
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

    /// FIFO loop for configs with scheduling but monotonic deadlines: drains
    /// the front packet when it is due, then sleeps only until its deadline.
    /// An empty FIFO performs no clock read at all.
    fn run_fifo(&mut self, buf: &mut [u8]) {
        let mut fifo = FifoQueue::default();
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            if let Some(front) = fifo.packets.front() {
                let now = self.pipeline.now();
                if front.time_to_send <= now {
                    self.pipeline
                        .drain_ready_fifo(&mut fifo, now, &*self.transport);
                    continue;
                }
                let receive_wait = self.pipeline.next_receive_wait_fifo(&fifo, now);
                if receive_wait.is_zero() {
                    continue;
                }
                match self.transport.recv_from_timeout(buf, receive_wait) {
                    Ok((n, _from)) => {
                        let now = self.pipeline.now();
                        self.pipeline.handle_datagram_fifo(
                            &buf[..n],
                            now,
                            Some(self.server_addr),
                            &mut fifo,
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
            } else {
                // Empty FIFO: no clock read, block on the default timeout.
                match self.transport.recv_from(buf) {
                    Ok((n, _from)) => {
                        let now = self.pipeline.now();
                        self.pipeline.handle_datagram_fifo(
                            &buf[..n],
                            now,
                            Some(self.server_addr),
                            &mut fifo,
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
    }

    /// Heap loop for configs whose deadlines can be non-monotonic (jitter or
    /// reorder-gap scheduling): the existing delay-heap pipeline.
    fn run_heap(&mut self, buf: &mut [u8]) {
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            // Drain ready packets first so latency is honoured.
            self.pipeline
                .drain_ready(self.pipeline.now(), &*self.transport);

            // Sleep only until the next queued deadline (capped at the idle
            // poll) so a packet that becomes ready is drained on the next
            // iteration instead of waiting out a fixed 5 ms poll.
            let receive_wait = self.pipeline.next_receive_wait(self.pipeline.now());
            if receive_wait.is_zero() {
                continue;
            }

            // Block briefly on recv so we don't spin. Use the explicit
            // timeout API so the receive deadline is decoupled from the
            // transport's default read timeout and can be asserted by tests.
            match self.transport.recv_from_timeout(&mut buf[..], receive_wait) {
                Ok((n, _from)) => {
                    let dst = Some(self.server_addr);
                    // Clean configs skip the heap entirely; only packets that
                    // need impairment reach the queued path.
                    if !self
                        .pipeline
                        .try_direct_forward(&buf[..n], dst, &*self.transport)
                    {
                        self.pipeline
                            .handle_datagram(&buf[..n], self.pipeline.now(), dst);
                    }
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
/// Dropping the link stops both proxy threads and joins them (equivalent to
/// calling [`NetemPair::stop`]).
pub struct NetemPair {
    client_addr: SocketAddr,
    server_addr: SocketAddr,
    stats_c2s: Arc<AtomicCounters>,
    stats_s2c: Arc<AtomicCounters>,
    queue_len_c2s: Arc<AtomicU64>,
    queue_len_s2c: Arc<AtomicU64>,
    blackout_c2s: Arc<AtomicBool>,
    blackout_s2c: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    /// Handle of the c2s runner thread; `None` once joined.
    thread_c2s: Mutex<Option<std::thread::JoinHandle<()>>>,
    /// Handle of the s2c runner thread; `None` once joined.
    thread_s2c: Mutex<Option<std::thread::JoinHandle<()>>>,
}

struct NetemPairConfig {
    c2s: NetemConfig,
    s2c: NetemConfig,
    c2s_shared: Option<BottleneckShaper>,
    s2c_shared: Option<BottleneckShaper>,
    pin_client_peer: bool,
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

    /// Spawn with custom transports (for in-memory tests). Both transports
    /// must already be bound; the client transport's local address becomes
    /// the client address.
    pub fn spawn_with_transports(
        server_addr: SocketAddr,
        c2s: NetemConfig,
        s2c: NetemConfig,
        client_transport: Box<dyn UdpTransport>,
        server_transport: Box<dyn UdpTransport>,
    ) -> io::Result<Self> {
        let client_addr = client_transport.local_addr()?;
        Self::spawn_from_sockets(
            server_addr,
            client_transport,
            server_transport,
            client_addr,
            NetemPairConfig {
                c2s,
                s2c,
                c2s_shared: None,
                s2c_shared: None,
                pin_client_peer: false,
                clock: None,
            },
        )
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
                pin_client_peer: true,
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
                pin_client_peer: true,
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
            pin_client_peer,
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
        server_sock.connect_peer(server_addr)?;
        let stats_c2s = Arc::new(AtomicCounters::default());
        let stats_s2c = Arc::new(AtomicCounters::default());
        let queue_len_c2s = Arc::new(AtomicU64::new(0));
        let queue_len_s2c = Arc::new(AtomicU64::new(0));
        let blackout_c2s = Arc::new(AtomicBool::new(false));
        let blackout_s2c = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let learned_client = Arc::new(LearnedDestination::default());

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
            thread_c2s: Mutex::new(None),
            thread_s2c: Mutex::new(None),
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
                connect_client_on_first_packet: pin_client_peer,
                shared: c2s_shared,
                clock: clock.clone(),
            },
        );
        let c2s_thread = std::thread::Builder::new()
            .name("netem-c2s".into())
            .spawn(move || c2s_runner.run())?;
        *pair.thread_c2s.lock().unwrap() = Some(c2s_thread);

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
                connect_client_on_first_packet: false,
                shared: s2c_shared,
                clock,
            },
        );
        let s2c_thread = std::thread::Builder::new()
            .name("netem-s2c".into())
            .spawn(move || s2c_runner.run())?;
        *pair.thread_s2c.lock().unwrap() = Some(s2c_thread);

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

    /// Signal both proxy threads to stop and wait for them to exit.
    /// Idempotent: once joined, subsequent calls are no-ops.
    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread_c2s.lock().unwrap().take() {
            thread.join().unwrap();
        }
        if let Some(thread) = self.thread_s2c.lock().unwrap().take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for NetemPair {
    fn drop(&mut self) {
        self.stop();
    }
}

// ─────────────────────── per-direction runner ────────────────────────────

/// Client destination learned from the first c2s packet, published by the c2s
/// runner and read by the s2c runner. A generation counter lets the reader
/// skip the lock entirely until the publisher actually changes the address.
#[derive(Default)]
struct LearnedDestination {
    address: Mutex<Option<SocketAddr>>,
    generation: AtomicU64,
    #[cfg(test)]
    publish_locks: AtomicU64,
    #[cfg(test)]
    refresh_locks: AtomicU64,
}

impl LearnedDestination {
    /// Publish `address` and update the caller's cache when it differs from
    /// what the caller has already seen, bumping the generation only on an
    /// actual change.
    fn publish_if_changed(&self, cached: &mut Option<SocketAddr>, address: SocketAddr) {
        if *cached == Some(address) {
            return;
        }
        #[cfg(test)]
        self.publish_locks.fetch_add(1, Ordering::Relaxed);
        *self.address.lock().unwrap() = Some(address);
        *cached = Some(address);
        self.generation.fetch_add(1, Ordering::Release);
    }

    /// Refresh the caller's cached destination only when the generation
    /// counter moved, taking the lock only on an actual change.
    fn refresh_if_changed(
        &self,
        cached: &mut Option<SocketAddr>,
        observed_generation: &mut u64,
    ) -> Option<SocketAddr> {
        let generation = self.generation.load(Ordering::Acquire);
        if generation != *observed_generation {
            #[cfg(test)]
            self.refresh_locks.fetch_add(1, Ordering::Relaxed);
            *cached = *self.address.lock().unwrap();
            *observed_generation = generation;
        }
        *cached
    }
}

struct SharedLinkRunner {
    recv: Arc<dyn UdpTransport>,
    send: Arc<dyn UdpTransport>,
    /// Fixed destination (the real server for c2s). When `None`, the runner
    /// uses the learned client address (`learned_dst`).
    fixed_dst: Option<SocketAddr>,
    learned_dst: Arc<LearnedDestination>,
    /// Pin the receive transport to the first client tuple seen (standard
    /// pairs). When false (custom transports), the transport keeps
    /// multi-source behaviour and the destination is still published to
    /// `learned_dst`.
    connect_client_on_first_packet: bool,
    pipeline: NetemState,
}

struct DirectionRunnerConfig {
    netem: NetemConfig,
    stats: Arc<AtomicCounters>,
    queue_len: Arc<AtomicU64>,
    blackout: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    fixed_dst: Option<SocketAddr>,
    learned_dst: Arc<LearnedDestination>,
    connect_client_on_first_packet: bool,
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
            connect_client_on_first_packet,
            shared,
            clock,
        } = config;
        Self {
            recv,
            send,
            fixed_dst,
            learned_dst,
            connect_client_on_first_packet,
            pipeline: NetemState::new(netem, stats, queue_len, blackout, stop, shared, clock),
        }
    }

    /// Connect the receive transport to the first client tuple seen, exactly
    /// once, when the pair pins its client peer. Later source tuples are
    /// rejected by the connected socket; errors are ignored because the
    /// connection is best-effort for custom transports.
    fn connect_client_once(&mut self, source: SocketAddr) {
        if !self.connect_client_on_first_packet {
            return;
        }
        let _ = self.recv.connect_peer(source);
        self.connect_client_on_first_packet = false;
    }

    fn run(mut self) {
        let mut buf = [0u8; 64 * 1024];
        // Dispatch once to the cheapest loop that preserves the direction's
        // semantics: stochastic-only and clean configs skip the queue
        // entirely (with fixed- or learned-destination variants), monotonic
        // deadlines use the FIFO queue, and jitter/reorder stay on the heap.
        if self.pipeline.direct_stochastic {
            if self.fixed_dst.is_some() {
                self.run_stochastic_fixed_direct(&mut buf);
            } else {
                self.run_stochastic_learned_direct(&mut buf);
            }
        } else if self.pipeline.uses_fifo_scheduling() {
            self.run_fifo(&mut buf);
        } else if self.pipeline.direct_forward {
            if self.fixed_dst.is_some() {
                self.run_fixed_direct(&mut buf);
            } else {
                self.run_learned_direct(&mut buf);
            }
        } else {
            self.run_heap(&mut buf);
        }
    }

    /// Clean no-clock c2s direct loop: publish the client address, then
    /// forward every datagram to the fixed server without reading the clock.
    fn run_fixed_direct(&mut self, buf: &mut [u8]) {
        let mut published_source = None;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.recv.recv_from(buf) {
                Ok((n, from)) => {
                    self.connect_client_once(from);
                    self.learned_dst
                        .publish_if_changed(&mut published_source, from);
                    self.pipeline
                        .forward_direct(&buf[..n], self.fixed_dst, &*self.send);
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

    /// Clean no-clock s2c direct loop: refresh the learned client address
    /// without locking until the publisher changes it, then forward replies.
    fn run_learned_direct(&mut self, buf: &mut [u8]) {
        let mut cached = None;
        let mut observed_generation = 0;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.recv.recv_from(buf) {
                Ok((n, _from)) => {
                    // Read the destination at packet-processing time so a
                    // just-published client address is never missed.
                    let dst = self
                        .learned_dst
                        .refresh_if_changed(&mut cached, &mut observed_generation);
                    self.pipeline.forward_direct(&buf[..n], dst, &*self.send);
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

    /// Stochastic-only c2s loop: publish the client address, then apply
    /// duplicate/loss directly without scheduling or clock reads.
    fn run_stochastic_fixed_direct(&mut self, buf: &mut [u8]) {
        let mut published_source = None;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.recv.recv_from(buf) {
                Ok((n, from)) => {
                    self.connect_client_once(from);
                    self.learned_dst
                        .publish_if_changed(&mut published_source, from);
                    self.pipeline
                        .forward_stochastic_direct(&buf[..n], self.fixed_dst, &*self.send);
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

    /// Stochastic-only s2c loop: refresh the learned destination, then apply
    /// duplicate/loss directly; a reply is forwarded only once a destination
    /// has been learned.
    fn run_stochastic_learned_direct(&mut self, buf: &mut [u8]) {
        let mut cached = None;
        let mut observed_generation = 0;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            match self.recv.recv_from(buf) {
                Ok((n, from)) => {
                    let dst = self
                        .learned_dst
                        .refresh_if_changed(&mut cached, &mut observed_generation);
                    self.pipeline
                        .forward_stochastic_direct(&buf[..n], dst, &*self.send);
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

    /// FIFO loop for directions with scheduling but monotonic deadlines.
    /// An empty FIFO performs no clock read.
    fn run_fifo(&mut self, buf: &mut [u8]) {
        let mut fifo = FifoQueue::default();
        let mut cached = None;
        let mut observed_generation = 0;
        let mut cached_from = None;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            if let Some(front) = fifo.packets.front() {
                let now = self.pipeline.now();
                if front.time_to_send <= now {
                    self.pipeline.drain_ready_fifo(&mut fifo, now, &*self.send);
                    continue;
                }
                let receive_wait = self.pipeline.next_receive_wait_fifo(&fifo, now);
                if receive_wait.is_zero() {
                    continue;
                }
                match self.recv.recv_from_timeout(buf, receive_wait) {
                    Ok((n, from)) => {
                        if self.fixed_dst.is_some() {
                            self.connect_client_once(from);
                            self.learned_dst.publish_if_changed(&mut cached_from, from);
                        }
                        let dst = self.fixed_dst.or_else(|| {
                            self.learned_dst
                                .refresh_if_changed(&mut cached, &mut observed_generation)
                        });
                        let now = self.pipeline.now();
                        self.pipeline
                            .handle_datagram_fifo(&buf[..n], now, dst, &mut fifo);
                    }
                    Err(e)
                        if e.kind() == io::ErrorKind::WouldBlock
                            || e.kind() == io::ErrorKind::TimedOut =>
                    {
                        // keep draining
                    }
                    Err(_) => break,
                }
            } else {
                // Empty FIFO: no clock read, block on the default timeout.
                match self.recv.recv_from(buf) {
                    Ok((n, from)) => {
                        if self.fixed_dst.is_some() {
                            self.connect_client_once(from);
                            self.learned_dst.publish_if_changed(&mut cached_from, from);
                        }
                        let dst = self.fixed_dst.or_else(|| {
                            self.learned_dst
                                .refresh_if_changed(&mut cached, &mut observed_generation)
                        });
                        let now = self.pipeline.now();
                        self.pipeline
                            .handle_datagram_fifo(&buf[..n], now, dst, &mut fifo);
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

    /// Heap loop for directions whose deadlines can be non-monotonic (jitter
    /// or reorder-gap scheduling): the existing delay-heap pipeline.
    fn run_heap(&mut self, buf: &mut [u8]) {
        let mut cached = None;
        let mut observed_generation = 0;
        let mut cached_from = None;
        loop {
            if self.pipeline.should_stop() {
                break;
            }
            self.pipeline.drain_ready(self.pipeline.now(), &*self.send);

            // Sleep only until the next queued deadline (capped at the idle
            // poll); a zero wait means a packet is due now, so re-drain on the
            // next iteration instead of polling.
            let receive_wait = self.pipeline.next_receive_wait(self.pipeline.now());
            if receive_wait.is_zero() {
                continue;
            }

            // Use the explicit timeout API so the receive deadline is
            // decoupled from the transport's default read timeout.
            match self.recv.recv_from_timeout(&mut buf[..], receive_wait) {
                Ok((n, from)) => {
                    // For c2s, learn the client address so the s2c runner can
                    // send replies back to it; skip the lock when unchanged.
                    if self.fixed_dst.is_some() {
                        self.connect_client_once(from);
                        self.learned_dst.publish_if_changed(&mut cached_from, from);
                    }
                    let dst = self.fixed_dst.or_else(|| {
                        self.learned_dst
                            .refresh_if_changed(&mut cached, &mut observed_generation)
                    });
                    // Clean configs skip the heap entirely; only packets that
                    // need impairment reach the queued path.
                    if !self
                        .pipeline
                        .try_direct_forward(&buf[..n], dst, &*self.send)
                    {
                        self.pipeline
                            .handle_datagram(&buf[..n], self.pipeline.now(), dst);
                    }
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
            stop: Arc::new(AtomicBool::new(false)),
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
                pin_client_peer: false,
                clock: Some(clock),
            },
        )
        .unwrap();
        for i in 0..3u8 {
            client_sock.push_recv(
                vec![i],
                SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5000)),
            );
        }
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
                pin_client_peer: false,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();
        client_sock.push_recv(
            vec![1],
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001)),
        );
        wait_until("first packet received", || pair.stats_c2s().received >= 1);
        clock.advance(Duration::from_millis(5));
        wait_until("first packet forwarded", || pair.stats_c2s().forwarded >= 1);
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
        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001));
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
                pin_client_peer: false,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();
        client_sock.push_recv(vec![0u8; 100], from);
        client_sock.push_recv(vec![1u8; 60], from);
        client_sock.push_recv(vec![2u8; 30], from);
        wait_until("c2s runner to receive all 3 packets", || {
            pair.stats_c2s().received == 3
        });
        clock.advance(Duration::from_millis(150));
        wait_until("60 B packet forwarded", || pair.stats_c2s().forwarded == 1);
        let stats = pair.stats_c2s();
        assert_eq!(stats.received, 3);
        assert_eq!(stats.forwarded, 1, "only the 60 B packet should exit");
        assert_eq!(
            stats.overflow_dropped, 2,
            "the packets that do not fit the shared bottleneck queue should count as overflow_dropped"
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
                pin_client_peer: false,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();
        let from_a = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7000));
        let from_b = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7001));
        client_sock.push_recv(vec![0xAA, 0x01], from_a);
        client_sock.push_recv(vec![0xBB, 0x01], from_b);
        client_sock.push_recv(vec![0xAA, 0x02], from_a);
        client_sock.push_recv(vec![0xBB, 0x02], from_b);
        client_sock.push_recv(vec![0xAA, 0x03], from_a);
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
                pin_client_peer: false,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();
        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 7000));
        client_sock.push_recv(vec![0u8; 50], from);
        client_sock.push_recv(vec![0u8; 40], from);
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
            "the 40 B packet that does not fit the shared bottleneck queue should count as overflow dropped"
        );
    }

    #[test]
    fn clean_config_forwards_directly_and_honors_blackout() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        // A default config (random loss model, zero loss, no scheduling) must
        // be clean: no scheduling, no stochastic work.
        let config = NetemConfig::default();
        let (runner, sent) = mock_runner(config);
        assert!(
            runner.direct_forward,
            "a clean config must be direct-forward eligible"
        );
        assert!(
            !runner.direct_stochastic,
            "a clean config must not need stochastic work"
        );
        // The direct path forwards without touching the heap.
        let dst = Some(server_addr);
        assert!(runner.forward_direct(b"hello", dst, &*sent));
        assert_eq!(runner.queue.len(), 0);
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 1);
        assert_eq!(s.forwarded, 1);
        assert_eq!(sent.sent.lock().unwrap().len(), 1);
        // Blackout gates on the direct path: counted received, dropped,
        // nothing forwarded.
        runner.blackout.store(true, Ordering::Relaxed);
        assert!(runner.forward_direct(b"gated", dst, &*sent));
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 2);
        assert_eq!(s.dropped, 1);
        assert_eq!(s.forwarded, 1);
        assert_eq!(runner.queue.len(), 0);
        drop(sent);
    }

    #[test]
    fn impaired_config_stays_on_the_queue_path() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_millis(10),
            ..Default::default()
        };
        let (mut runner, sent) = mock_runner(config);
        assert!(!runner.direct_forward);
        let dst = Some(server_addr);
        assert!(
            !runner.try_direct_forward(b"impaired", dst, &*sent),
            "an impaired config must refuse the direct path"
        );
        assert_eq!(runner.stats.snapshot().received, 0);
        // The queued path still handles the datagram.
        runner.handle_datagram(b"impaired", server_addr, sent.clock().now());
        assert_eq!(runner.queue.len(), 1);
        assert_eq!(runner.stats.snapshot().received, 1);
        drop(sent);
    }

    #[test]
    fn datagram_size_filter_stays_on_the_direct_path() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            max_datagram_size: 512,
            ..Default::default()
        };
        let (runner, sent) = mock_runner(config);
        assert!(
            runner.direct_forward,
            "max_datagram_size is a deterministic filter and must not disqualify the direct path"
        );
        assert!(runner.try_direct_forward(&[0u8; 600], Some(server_addr), &*sent));
        let s = runner.stats.snapshot();
        assert_eq!(s.received, 1);
        assert_eq!(s.dropped, 1);
        assert_eq!(
            runner.queue.len(),
            0,
            "the oversized datagram must be dropped without entering the heap"
        );
        drop(sent);
    }

    #[test]
    fn queued_payload_storage_is_reused_and_bounded() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_millis(10),
            ..Default::default()
        };
        let (mut runner, sent) = mock_runner(config);
        let clock = sent.clock();
        let payload = vec![0xABu8; 4096];
        let mut fifo = FifoQueue::default();

        // Enqueue a large payload into the FIFO and drain it: the drained
        // buffer is recycled into the FIFO's own pool.
        runner.handle_datagram_fifo(&payload, clock.now(), Some(server_addr), &mut fifo);
        let queued_ptr = fifo.packets.front().unwrap().data.as_ptr();
        clock.advance(Duration::from_millis(20));
        runner.drain_ready_fifo(&mut fifo, clock.now(), &*sent);
        assert_eq!(fifo.reused_packet_buffers.len(), 1);
        assert_eq!(fifo.reused_capacity_bytes, payload.capacity());

        // The next enqueue pops the recycled buffer: the queued payload must
        // reuse the same allocation (pointer reuse).
        runner.handle_datagram_fifo(&payload, clock.now(), Some(server_addr), &mut fifo);
        let reused_ptr = fifo.packets.front().unwrap().data.as_ptr();
        assert_eq!(
            reused_ptr, queued_ptr,
            "drained payload buffer must be recycled, not reallocated"
        );
        assert_eq!(fifo.packets.len(), 1);
        assert_eq!(fifo.reused_packet_buffers.len(), 0);

        // Drain a burst of small payloads larger than the count cap: the pool
        // is bounded at exactly MAX_FIFO_REUSED_PACKET_BUFFERS (the byte bound
        // never trips for 1-byte payloads).
        let small = [0x42u8; 1];
        for _ in 0..(MAX_FIFO_REUSED_PACKET_BUFFERS + 16) {
            runner.handle_datagram_fifo(&small, clock.now(), Some(server_addr), &mut fifo);
        }
        clock.advance(Duration::from_millis(20));
        while !fifo.packets.is_empty() {
            runner.drain_ready_fifo(&mut fifo, clock.now(), &*sent);
        }
        assert_eq!(
            fifo.reused_packet_buffers.len(),
            MAX_FIFO_REUSED_PACKET_BUFFERS,
            "the FIFO recycle pool must hold exactly MAX_FIFO_REUSED_PACKET_BUFFERS"
        );
        assert!(fifo.reused_capacity_bytes <= MAX_FIFO_REUSED_PACKET_BUFFER_BYTES);

        // The byte bound caps the pool before the count bound for large
        // payloads: 4 MiB / 64 KiB = 64 buffers.
        let mut big_fifo = FifoQueue::default();
        let big = vec![0xCDu8; 64 * 1024];
        for _ in 0..100 {
            runner.handle_datagram_fifo(&big, clock.now(), Some(server_addr), &mut big_fifo);
        }
        clock.advance(Duration::from_millis(20));
        while !big_fifo.packets.is_empty() {
            runner.drain_ready_fifo(&mut big_fifo, clock.now(), &*sent);
        }
        assert_eq!(
            big_fifo.reused_packet_buffers.len(),
            MAX_FIFO_REUSED_PACKET_BUFFER_BYTES / (64 * 1024),
            "the byte bound must stop recycling before the count bound"
        );
        assert!(big_fifo.reused_capacity_bytes <= MAX_FIFO_REUSED_PACKET_BUFFER_BYTES);
        drop(sent);
    }

    #[test]
    fn receive_wait_tracks_the_next_queued_deadline() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_millis(2),
            ..Default::default()
        };
        let (mut runner, sent) = mock_runner(config);
        let clock = sent.clock();
        let t0 = clock.now();
        runner.handle_datagram(b"a", server_addr, t0);
        // The next deadline is 2 ms out, below the 5 ms idle poll, so the wait
        // tracks the queued deadline exactly.
        assert_eq!(runner.next_receive_wait(t0), Duration::from_millis(2));
        // One ms later the remaining wait is 1 ms.
        assert_eq!(
            runner.next_receive_wait(t0 + Duration::from_millis(1)),
            Duration::from_millis(1)
        );
        // Once drained there is nothing queued: the wait falls back to the
        // idle poll.
        clock.advance(Duration::from_millis(10));
        runner.drain_ready(clock.now());
        assert_eq!(runner.queue.len(), 0);
        assert_eq!(runner.next_receive_wait(clock.now()), RUNNER_IDLE_POLL);
        drop(sent);
    }

    #[test]
    fn receive_wait_caps_long_deadlines_at_the_idle_poll() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_millis(100),
            ..Default::default()
        };
        let (mut runner, sent) = mock_runner(config);
        runner.handle_datagram(b"a", server_addr, sent.clock().now());
        assert_eq!(
            runner.next_receive_wait(sent.clock().now()),
            RUNNER_IDLE_POLL,
            "long queued deadlines must be capped at the idle poll"
        );
        drop(sent);
    }

    /// Wall-clock throughput probe helper for [`clean_forwarding_perf_probe`]:
    /// runs `op` SAMPLES times and returns millions of operations per second.
    fn probe_mpps(mut op: impl FnMut()) -> f64 {
        const SAMPLES: u32 = 200_000;
        let start = std::time::Instant::now();
        for _ in 0..SAMPLES {
            op();
        }
        let elapsed = start.elapsed().as_secs_f64();
        SAMPLES as f64 / elapsed / 1e6
    }

    #[test]
    #[ignore = "release perf probe"]
    fn clean_forwarding_perf_probe() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let payload = vec![0x42u8; 1200];

        // Direct path: a clean config forwards without touching the heap.
        let (runner, sent) = mock_runner(NetemConfig::default());
        let direct = || {
            let _ = runner.try_direct_forward(&payload, Some(server_addr), &*sent);
        };
        let direct_mpps = probe_mpps(direct);

        // Filter path: the same clean config but every datagram trips the
        // deterministic max_datagram_size filter on the direct path.
        let config = NetemConfig {
            max_datagram_size: 512,
            ..Default::default()
        };
        let (runner_f, sent_f) = mock_runner(config);
        let filter = || {
            let _ = runner_f.try_direct_forward(&payload, Some(server_addr), &*sent_f);
        };
        let filter_mpps = probe_mpps(filter);

        // Queued path: an impaired config goes through enqueue + drain.
        let config = NetemConfig {
            latency: Duration::from_millis(1),
            ..Default::default()
        };
        let (mut runner_q, sent_q) = mock_runner(config);
        let clock = sent_q.clock();
        let queued = || {
            runner_q.handle_datagram(&payload, server_addr, clock.now());
            clock.advance(Duration::from_millis(2));
            runner_q.drain_ready(clock.now());
        };
        let queued_mpps = probe_mpps(queued);

        eprintln!(
            "clean_forwarding_perf_probe: direct={direct_mpps:.3} Mpps filter={filter_mpps:.3} Mpps queued={queued_mpps:.3} Mpps direct/filter speedup={:.2}x direct/queued speedup={:.2}x",
            direct_mpps / filter_mpps.max(1e-9),
            direct_mpps / queued_mpps.max(1e-9),
        );
        drop(sent);
        drop(sent_f);
        drop(sent_q);
    }

    #[test]
    #[ignore = "release perf probe"]
    fn short_deadline_latency_perf_probe() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_micros(500),
            ..Default::default()
        };
        let (mut runner, sent) = mock_runner(config);
        let clock = sent.clock();
        const SAMPLES: usize = 1000;
        let mut latencies = Vec::with_capacity(SAMPLES);
        // Sequential 500 us deadlines: each packet must be drained soon after
        // its deadline, without waiting out the full idle poll.
        for _ in 0..SAMPLES {
            let start = std::time::Instant::now();
            runner.handle_datagram(b"x", server_addr, clock.now());
            clock.advance(Duration::from_micros(600));
            runner.drain_ready(clock.now());
            latencies.push(start.elapsed());
        }
        latencies.sort();
        let median = latencies[SAMPLES / 2];
        eprintln!(
            "short_deadline_latency_perf_probe: median={median:?} (RUNNER_IDLE_POLL={RUNNER_IDLE_POLL:?})"
        );
        assert!(
            median < RUNNER_IDLE_POLL,
            "median {median:?} must stay below the idle poll {RUNNER_IDLE_POLL:?}"
        );
        drop(sent);
    }

    // ────────────────── specialized-path equivalence tests ──────────────────

    /// The stochastic-only direct path must consume exactly the same PRNG
    /// draws and produce exactly the same counters as the queued pipeline for
    /// an identical stochastic config (duplication + loss, no scheduling).
    #[test]
    fn stochastic_direct_path_matches_the_queued_pipeline() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            duplicate: u32::MAX / 2,
            loss: u32::MAX / 3,
            ..Default::default()
        };
        let (mut direct, direct_sent) = mock_runner(config.clone());
        let (mut queued, queued_sent) = mock_runner(config);
        assert!(
            direct.direct_stochastic,
            "stochastic-only config must take the direct stochastic path"
        );
        assert!(
            !direct.direct_forward,
            "stochastic work disqualifies the clean direct path"
        );
        let now = Instant::now();
        let payload = b"stochastic-direct";
        for _ in 0..256 {
            // Both paths start each packet from an identical RNG state.
            let before_direct = direct.rng;
            let before_queued = queued.rng;
            assert_eq!(before_direct.s1, before_queued.s1);
            assert_eq!(before_direct.s2, before_queued.s2);
            assert_eq!(before_direct.s3, before_queued.s3);
            assert_eq!(before_direct.s4, before_queued.s4);

            direct.forward_stochastic_direct(payload, Some(server_addr), &*direct_sent);
            queued
                .pipeline
                .handle_datagram(payload, now, Some(server_addr));
            queued.pipeline.drain_ready(now, &*queued_sent);

            let ds = direct.stats.snapshot();
            let qs = queued.stats.snapshot();
            assert_eq!(ds.received, qs.received);
            assert_eq!(ds.dropped, qs.dropped);
            assert_eq!(ds.duplicated, qs.duplicated);
            assert_eq!(ds.forwarded, qs.forwarded);
            // Identical draw order leaves the RNG lanes bit-for-bit equal.
            assert_eq!(direct.rng.s1, queued.rng.s1);
            assert_eq!(direct.rng.s2, queued.rng.s2);
            assert_eq!(direct.rng.s3, queued.rng.s3);
            assert_eq!(direct.rng.s4, queued.rng.s4);
        }
        let ds = direct.stats.snapshot();
        let qs = queued.stats.snapshot();
        assert_eq!(ds.received, 256);
        assert_eq!(ds.forwarded, qs.forwarded);
        assert_eq!(
            direct_sent.sent.lock().unwrap().len(),
            queued_sent.sent.lock().unwrap().len()
        );
        drop(direct_sent);
        drop(queued_sent);
    }

    /// The FIFO path must schedule, order, and count packets exactly like the
    /// delay heap for an identical latency + shared-shaper config.
    #[test]
    fn fifo_scheduling_matches_heap_with_a_shared_shaper() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 2000));
        let config = NetemConfig {
            latency: Duration::from_millis(30),
            ..Default::default()
        };
        let (mut fifo_runner, fifo_sent) = mock_runner(config.clone());
        let (mut heap_runner, heap_sent) = mock_runner(config);
        assert!(
            fifo_runner.uses_fifo_scheduling(),
            "latency-only config must select the FIFO path"
        );
        // Two independent shapers with identical rates: both paths start from
        // a base later than the shapers' creation instants, so the first
        // schedule is deterministic for both.
        let fifo_shaper = BottleneckShaper::new(8_000_000, 0);
        let heap_shaper = BottleneckShaper::new(8_000_000, 0);
        fifo_runner.shared = Some(fifo_shaper);
        heap_runner.shared = Some(heap_shaper);
        let clock = fifo_sent.clock();
        // Advance past both shapers' creation instants so the first schedule
        // is deterministic for both paths.
        clock.advance(Duration::from_millis(1));
        let now = clock.now();
        let payloads: [&[u8]; 6] = [b"aa", b"bbbb", b"c", b"dd", b"eeeee", b"ff"];
        let mut fifo = FifoQueue::default();
        for p in payloads {
            fifo_runner.handle_datagram_fifo(p, now, Some(server_addr), &mut fifo);
            heap_runner
                .pipeline
                .handle_datagram(p, now, Some(server_addr));
        }
        // Identical deadlines per packet.
        let fifo_times: Vec<Instant> = fifo.packets.iter().map(|q| q.time_to_send).collect();
        let heap_times: Vec<Instant> = heap_runner.queue.iter().map(|q| q.0.time_to_send).collect();
        assert_eq!(
            fifo_times, heap_times,
            "FIFO and heap must schedule identically"
        );
        // Draining at the last deadline forwards everything in identical order.
        let last = *fifo_times.last().unwrap();
        fifo_runner.drain_ready_fifo(&mut fifo, last, &*fifo_sent);
        heap_runner.pipeline.drain_ready(last, &*heap_sent);
        let fifo_sent_payloads: Vec<Vec<u8>> = fifo_sent
            .sent
            .lock()
            .unwrap()
            .iter()
            .map(|(d, _)| d.clone())
            .collect();
        let heap_sent_payloads: Vec<Vec<u8>> = heap_sent
            .sent
            .lock()
            .unwrap()
            .iter()
            .map(|(d, _)| d.clone())
            .collect();
        assert_eq!(fifo_sent_payloads, heap_sent_payloads);
        assert_eq!(
            fifo_runner.stats.snapshot(),
            heap_runner.stats.snapshot(),
            "FIFO and heap must count identically"
        );
        drop(fifo_sent);
        drop(heap_sent);
    }

    /// The standard transport must skip redundant read-timeout installs and
    /// restore the configured default after a temporary timeout receive.
    #[test]
    fn std_udp_transport_skips_redundant_timeout_installs_and_restores_default() {
        let bind = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
        let transport = StdUdpTransport::bind(bind).unwrap();
        let installs = || transport.receive_timeout_installs.load(Ordering::Relaxed);
        // Binding installs the idle poll once; the state already matches it.
        assert_eq!(transport.recv_timeout().unwrap(), Some(RUNNER_IDLE_POLL));
        assert_eq!(installs(), 0);
        // Re-setting the same timeout performs no syscall.
        transport.set_recv_timeout(RUNNER_IDLE_POLL).unwrap();
        assert_eq!(installs(), 0);
        // A different timeout installs exactly once.
        transport.set_recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(installs(), 1);
        // A temporary timeout receive installs the temporary value and then
        // restores the configured default: two more installs.
        let mut buf = [0u8; 16];
        let _ = transport.recv_from_timeout(&mut buf, Duration::from_millis(1));
        assert_eq!(installs(), 3);
        assert_eq!(
            transport.recv_timeout().unwrap(),
            Some(Duration::from_secs(2))
        );
        // A plain receive sees the configured default already effective.
        let _ = transport.recv_from(&mut buf);
        assert_eq!(installs(), 3);
        // Duration::ZERO disables the timeout (configured None).
        transport.set_recv_timeout(Duration::ZERO).unwrap();
        assert_eq!(installs(), 4);
        assert_eq!(transport.recv_timeout().unwrap(), None);
        drop(transport);
    }

    /// A connected [`StdUdpTransport`] preserves datagram endpoints: receives
    /// report the connected peer, idempotent connects succeed, sends to the
    /// peer work, and connecting to a different peer fails.
    #[test]
    fn std_udp_transport_connected_peer_preserves_datagram_endpoints() {
        let localhost = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
        let server = std::net::UdpSocket::bind(localhost).unwrap();
        server
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let server_addr = server.local_addr().unwrap();
        let transport = StdUdpTransport::bind(localhost).unwrap();
        let transport_addr = transport.local_addr().unwrap();
        transport.connect_peer(server_addr).unwrap();
        transport.connect_peer(server_addr).unwrap();
        transport.send_to(b"ping", server_addr).unwrap();
        let mut buffer = [0; 16];
        let (received, source) = server.recv_from(&mut buffer).unwrap();
        assert_eq!(&buffer[..received], b"ping");
        assert_eq!(source, transport_addr);
        server.send_to(b"pong", source).unwrap();
        let (received, source) = transport
            .recv_from_timeout(&mut buffer, Duration::from_secs(1))
            .unwrap();
        assert_eq!(&buffer[..received], b"pong");
        assert_eq!(source, server_addr);
        let other_server = std::net::UdpSocket::bind(localhost).unwrap();
        let error = transport
            .connect_peer(other_server.local_addr().unwrap())
            .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
        let error = transport
            .send_to(b"wrong peer", other_server.local_addr().unwrap())
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }

    /// A standard pair pins the first client tuple: once the first client
    /// packet is processed, the client-side socket is connected to that
    /// source, so a later source tuple cannot replace the pinned route.
    #[test]
    fn standard_pair_pins_the_first_client_tuple() {
        let localhost = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
        let server = std::net::UdpSocket::bind(localhost).unwrap();
        server
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let pair = NetemPair::spawn(
            server.local_addr().unwrap(),
            NetemConfig::default(),
            NetemConfig::default(),
        )
        .unwrap();
        let client_a = std::net::UdpSocket::bind(localhost).unwrap();
        let client_b = std::net::UdpSocket::bind(localhost).unwrap();
        client_a
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        client_a.send_to(b"a1", pair.client_addr()).unwrap();
        let mut payload = [0; 16];
        let (received, proxy_server_addr) = server.recv_from(&mut payload).unwrap();
        assert_eq!(&payload[..received], b"a1");
        server.send_to(b"reply", proxy_server_addr).unwrap();
        let (received, _) = client_a.recv_from(&mut payload).unwrap();
        assert_eq!(&payload[..received], b"reply");
        client_b.send_to(b"client-b", pair.client_addr()).unwrap();
        client_a.send_to(b"a2", pair.client_addr()).unwrap();
        let (received, _) = server.recv_from(&mut payload).unwrap();
        assert_eq!(
            &payload[..received],
            b"a2",
            "a later source tuple must not replace the pair's pinned first client tuple"
        );
    }

    #[test]
    #[ignore = "release-only connected UDP peer performance probe"]
    fn std_udp_connected_peer_perf_probe() {
        const OPERATIONS: usize = 2_000;
        const SAMPLES: usize = 21;
        const PAYLOAD_LEN: usize = 64;
        fn run_sample(connected: bool) -> Duration {
            let localhost = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0));
            let server = std::net::UdpSocket::bind(localhost).unwrap();
            server
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let server_addr = server.local_addr().unwrap();
            let transport = StdUdpTransport::bind(localhost).unwrap();
            if connected {
                transport.connect_peer(server_addr).unwrap();
            }
            let ready = Arc::new(std::sync::Barrier::new(2));
            let server_ready = Arc::clone(&ready);
            let echo = std::thread::spawn(move || {
                let mut payload = [0; PAYLOAD_LEN];
                server_ready.wait();
                for _ in 0..OPERATIONS {
                    let (received, source) = server.recv_from(&mut payload).unwrap();
                    assert_eq!(received, PAYLOAD_LEN);
                    assert_eq!(
                        server.send_to(&payload[..received], source).unwrap(),
                        received
                    );
                }
            });
            let mut sent = [0; PAYLOAD_LEN];
            let mut received = [0; PAYLOAD_LEN];
            ready.wait();
            let started = Instant::now();
            for sequence in 0..OPERATIONS {
                sent[..size_of::<u64>()].copy_from_slice(&(sequence as u64).to_ne_bytes());
                transport.send_to(&sent, server_addr).unwrap();
                let (received_len, source) = transport
                    .recv_from_timeout(&mut received, Duration::from_secs(1))
                    .unwrap();
                assert_eq!(received_len, PAYLOAD_LEN);
                assert_eq!(source, server_addr);
                assert_eq!(received, sent);
            }
            let elapsed = started.elapsed();
            echo.join().unwrap();
            elapsed
        }
        let mut connected_samples = Vec::with_capacity(SAMPLES);
        let mut unconnected_samples = Vec::with_capacity(SAMPLES);
        for sample in 0..SAMPLES {
            if sample % 2 == 0 {
                connected_samples.push(run_sample(true));
                unconnected_samples.push(run_sample(false));
            } else {
                unconnected_samples.push(run_sample(false));
                connected_samples.push(run_sample(true));
            }
        }
        connected_samples.sort_unstable();
        unconnected_samples.sort_unstable();
        let connected_median = connected_samples[SAMPLES / 2];
        let unconnected_median = unconnected_samples[SAMPLES / 2];
        eprintln!(
            "[perf] connected UDP peer: connected={:.0} roundtrips/s; unconnected={:.0} roundtrips/s; speedup={:.3}x",
            OPERATIONS as f64 / connected_median.as_secs_f64(),
            OPERATIONS as f64 / unconnected_median.as_secs_f64(),
            unconnected_median.as_secs_f64() / connected_median.as_secs_f64(),
        );
    }

    /// [`NetemPair::spawn_with_transports`] must expose the custom client
    /// transport's address and forward c2s traffic to the fixed server.
    #[test]
    fn pair_custom_transports_expose_client_address_and_forward() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 3500));
        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 4501)),
            clock.clone(),
        ));
        let server_sock = Arc::new(MockTransport::with_clock(server_addr, clock.clone()));
        let pair = NetemPair::spawn_with_transports(
            server_addr,
            NetemConfig::default(),
            NetemConfig::default(),
            Box::new(Arc::clone(&client_sock) as Arc<dyn UdpTransport>),
            Box::new(Arc::clone(&server_sock) as Arc<dyn UdpTransport>),
        )
        .unwrap();
        assert_eq!(pair.client_addr(), client_sock.local_addr().unwrap());
        assert_eq!(pair.server_addr(), server_addr);
        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001));
        client_sock.push_recv(vec![7u8], from);
        wait_until("c2s forwarded", || {
            server_sock.sent.lock().unwrap().len() == 1
        });
        let sent = server_sock.sent.lock().unwrap();
        assert_eq!(sent[0].0, vec![7u8]);
        assert_eq!(sent[0].1, server_addr);
        pair.stop();
    }

    /// Learned-destination publication and refresh must take the shared lock
    /// only when the route actually changes; unchanged routes are served from
    /// the caller's cache and the generation counter.
    #[test]
    fn learned_destination_locks_only_when_the_route_changes() {
        let learned = LearnedDestination::default();
        let client_a = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001));
        let client_b = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5002));
        let mut published = None;
        let mut destination = None;
        let mut observed_generation = 0;
        learned.publish_if_changed(&mut published, client_a);
        for _ in 0..16 {
            learned.publish_if_changed(&mut published, client_a);
        }
        assert_eq!(
            learned.refresh_if_changed(&mut destination, &mut observed_generation),
            Some(client_a)
        );
        for _ in 0..16 {
            assert_eq!(
                learned.refresh_if_changed(&mut destination, &mut observed_generation),
                Some(client_a)
            );
        }
        assert_eq!(learned.publish_locks.load(Ordering::Relaxed), 1);
        assert_eq!(learned.refresh_locks.load(Ordering::Relaxed), 1);
        learned.publish_if_changed(&mut published, client_b);
        assert_eq!(
            learned.refresh_if_changed(&mut destination, &mut observed_generation),
            Some(client_b)
        );
        assert_eq!(learned.publish_locks.load(Ordering::Relaxed), 2);
        assert_eq!(learned.refresh_locks.load(Ordering::Relaxed), 2);
    }

    #[test]
    #[ignore = "release-only learned-destination cache performance probe"]
    fn learned_destination_cache_perf_probe() {
        const ITERATIONS: usize = 5_000_000;
        let client = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001));
        let learned = LearnedDestination::default();
        let mut published = None;
        let mut destination = None;
        let mut observed_generation = 0;
        learned.publish_if_changed(&mut published, client);
        assert_eq!(
            learned.refresh_if_changed(&mut destination, &mut observed_generation),
            Some(client)
        );
        let started = Instant::now();
        for _ in 0..ITERATIONS {
            learned.publish_if_changed(std::hint::black_box(&mut published), client);
            std::hint::black_box(learned.refresh_if_changed(
                std::hint::black_box(&mut destination),
                std::hint::black_box(&mut observed_generation),
            ));
        }
        let cached = started.elapsed();
        let started = Instant::now();
        for _ in 0..ITERATIONS {
            let mut address = learned.address.lock().unwrap();
            if *address != Some(client) {
                *address = Some(client);
            }
            drop(address);
            std::hint::black_box(*learned.address.lock().unwrap());
        }
        let locked = started.elapsed();
        let cached_ns = cached.as_secs_f64() * 1e9 / ITERATIONS as f64;
        let locked_ns = locked.as_secs_f64() * 1e9 / ITERATIONS as f64;
        eprintln!(
            "[perf] Learned destination: cached={cached_ns:.2} ns/packet; locked={locked_ns:.2} ns/packet; speedup={:.2}x",
            locked_ns / cached_ns
        );
        assert!(cached < locked, "cached={cached:?}, locked={locked:?}");
    }

    /// The impaired c2s runner must publish the learned client destination so
    /// the clean s2c runner can forward server replies back to the client.
    #[test]
    fn impaired_c2s_publishes_destination_to_clean_s2c() {
        let server_addr = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 3600));
        let clock = Clock::new();
        let client_sock = Arc::new(MockTransport::with_clock(
            SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 4601)),
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
                c2s: NetemConfig {
                    latency: Duration::from_millis(20),
                    ..Default::default()
                },
                s2c: NetemConfig::default(),
                c2s_shared: None,
                s2c_shared: None,
                pin_client_peer: false,
                clock: Some(clock.clone()),
            },
        )
        .unwrap();
        let from = SocketAddr::V4(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 5001));
        client_sock.push_recv(vec![1u8], from);
        wait_until("c2s received", || pair.stats_c2s().received == 1);
        clock.advance(Duration::from_millis(50));
        wait_until("c2s forwarded", || pair.stats_c2s().forwarded == 1);
        server_sock.push_recv(vec![2u8], server_addr);
        wait_until("s2c forwarded to learned client", || {
            client_sock.sent.lock().unwrap().len() == 1
        });
        let sent = client_sock.sent.lock().unwrap();
        assert_eq!(sent[0].0, vec![2u8]);
        assert_eq!(
            sent[0].1, from,
            "reply must go to the learned client address"
        );
        pair.stop();
    }

    /// The serialization cache must produce exactly the packet-length-scaled
    /// serialization for repeated and changing lengths.
    #[test]
    fn shared_shaper_serialization_cache_tracks_packet_size_changes() {
        let shaper = BottleneckShaper::new(800_000, 0);
        let base = Instant::now();
        // 1000 B at 800 kbit/s serializes in exactly 10 ms; repeated lengths
        // reuse the cached serialization and step by the same amount.
        let t1 = shaper.schedule(base, 1000).unwrap();
        let t2 = shaper.schedule(base, 1000).unwrap();
        assert_eq!(t2 - t1, Duration::from_millis(10));
        // A different length recomputes the serialization (2000 B => 20 ms).
        let t3 = shaper.schedule(base, 2000).unwrap();
        assert_eq!(t3 - t2, Duration::from_millis(20));
        // Back to the cached length.
        let t4 = shaper.schedule(base, 1000).unwrap();
        assert_eq!(t4 - t3, Duration::from_millis(10));
        assert_eq!(shaper.dropped(), 0);
    }

    /// The division-free backlog comparison must make exactly the same
    /// admit/reject decisions as the whole-byte backlog math, both on a sweep
    /// of values and through the shaper itself.
    #[test]
    fn shared_shaper_limit_comparison_matches_whole_byte_backlog_math() {
        fn whole_byte_exceeds(limit_bytes: u64, backlog_ns: u128, rate: u64, len: usize) -> bool {
            let backlog = (backlog_ns * rate as u128 / 1_000_000_000 / 8) as u64;
            backlog.saturating_add(len as u64) > limit_bytes
        }
        let mut rng = RndState::seed(0x5EED_C0DE);
        for _ in 0..10_000 {
            let limit_bytes = 1 + (rng.next_u32() as u64 % 100_000);
            let backlog_ns = rng.next_u32() as u128; // up to ~4.3 s of backlog
            let rate = 1 + (rng.next_u32() as u64 % 100_000_000);
            let len = 1 + (rng.next_u32() as usize % 10_000);
            assert_eq!(
                exceeds_byte_limit(limit_bytes, backlog_ns, rate, len),
                whole_byte_exceeds(limit_bytes, backlog_ns, rate, len),
                "limit={limit_bytes} backlog_ns={backlog_ns} rate={rate} len={len}"
            );
        }
        // End-to-end: drive a bounded shaper and compare each schedule
        // decision against the whole-byte backlog reported by `backlog_bytes`.
        let rate = 8_000u64;
        let limit = 120u64;
        let shaper = BottleneckShaper::new(rate, limit);
        let base = Instant::now();
        let mut len = 100usize;
        for _ in 0..12 {
            let backlog = shaper.backlog_bytes(base);
            let expect_reject = backlog.saturating_add(len as u64) > limit;
            let rejected = shaper.schedule(base, len).is_none();
            assert_eq!(
                rejected, expect_reject,
                "backlog={backlog} len={len} limit={limit}"
            );
            len = len.wrapping_mul(2) % 90 + 10;
        }
    }

    /// Concurrent callers must serialize exactly once per packet: the shared
    /// clock advances by one serialization per accepted schedule with no lost
    /// or duplicated updates.
    #[test]
    fn shared_shaper_serializes_concurrent_callers_exactly_once() {
        let shaper = Arc::new(BottleneckShaper::new(8_000_000, 0)); // 1 ms per 1000 B
        let base = Instant::now();
        const PER_THREAD: usize = 100;
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let shaper = Arc::clone(&shaper);
                std::thread::spawn(move || {
                    let mut last = base;
                    for _ in 0..PER_THREAD {
                        last = shaper.schedule(last, 1000).unwrap();
                    }
                    last
                })
            })
            .collect();
        let max_exit = threads
            .into_iter()
            .map(|t| t.join().unwrap())
            .max()
            .unwrap();
        // 4 threads * 100 packets * 1 ms: the last exit is exactly 400 ms out
        // when every schedule advanced the shared clock exactly once.
        let expected = base + Duration::from_millis(400);
        assert!(
            max_exit >= expected,
            "lost serialization: max exit {max_exit:?} before {expected:?}"
        );
        assert!(
            max_exit <= expected + Duration::from_micros(50),
            "duplicated serialization: max exit {max_exit:?} after {expected:?}"
        );
    }
}
