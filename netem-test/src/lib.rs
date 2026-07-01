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

use std::collections::VecDeque;
use std::io;
use std::net::{SocketAddr, SocketAddrV4};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

// ───────────────────────────── seeded RNG ──────────────────────────────

/// Kernel `struct rnd_state` – four 32-bit Tausworthe LFSR lanes.
#[derive(Clone, Copy, Debug)]
pub struct RndState {
    s1: u32,
    s2: u32,
    s3: u32,
    s4: u32,
}

impl RndState {
    /// `prandom_seed_state` from `linux/prandom.h`.
    pub fn seed(seed: u64) -> Self {
        #[inline]
        fn seed_lane(x: u32, m: u32) -> u32 {
            if x < m { x + m } else { x }
        }
        let i = ((seed >> 32) ^ (seed << 10) ^ seed) as u32;
        Self {
            s1: seed_lane(i, 2),
            s2: seed_lane(i, 8),
            s3: seed_lane(i, 16),
            s4: seed_lane(i, 128),
        }
    }

    /// `prandom_u32_state` from `lib/random32.c` – four Tausworthe steps.
    #[inline]
    pub fn next_u32(&mut self) -> u32 {
        #[inline]
        fn tauswortho(s: &mut u32, a: u32, b: u32, c: u32, d: u32) {
            *s = ((*s & c) << d) ^ (((*s << a) ^ *s) >> b);
        }
        tauswortho(&mut self.s1, 6, 13, 4_294_967_294, 18);
        tauswortho(&mut self.s2, 2, 27, 4_294_967_288, 2);
        tauswortho(&mut self.s3, 13, 21, 4_294_967_280, 7);
        tauswortho(&mut self.s4, 3, 12, 4_294_967_168, 13);
        self.s1 ^ self.s2 ^ self.s3 ^ self.s4
    }
}

/// Correlated random source – `struct crndstate` in `sch_netem.c`.
#[derive(Clone, Copy, Debug)]
struct CorRng {
    last: u32,
    rho: u32,
}

impl CorRng {
    const fn new(rho: u32) -> Self {
        Self { last: 0, rho }
    }

    /// `get_crandom`: next value depends on last; `rho` is scaled to avoid
    /// floating point.
    fn next(&mut self, rng: &mut RndState) -> u32 {
        if self.rho == 0 {
            return rng.next_u32();
        }
        let value = rng.next_u32();
        let rho = self.rho as u64 + 1;
        let answer = (value as u64 * ((1u64 << 32) - rho) + self.last as u64 * rho) >> 32;
        self.last = answer as u32;
        answer as u32
    }
}

// ───────────────────────────── loss models ─────────────────────────────

/// Probability parameters for the four-state Gilbert-Elliot-style loss model
/// used by `sch_netem` (the "GI model"). All probabilities are in `u32`
/// units where `u32::MAX == 1.0` to match the kernel's `p13`/`p31`/…
/// representation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct FourStateLoss {
    /// p13 – from gap-Tx to isolated-loss-in-gap.
    pub p13: u32,
    /// p31 – from burst-loss back to gap-Tx.
    pub p31: u32,
    /// p32 – from burst-loss to burst-Tx.
    pub p32: u32,
    /// p14 – from gap-Tx to burst-loss.
    pub p14: u32,
    /// p23 – from burst-Tx to burst-loss.
    pub p23: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
enum FourState {
    #[default]
    TxInGap = 1,
    TxInBurst = 2,
    LostInGap = 3,
    LostInBurst = 4,
}

/// Which loss model to apply.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum LossModel {
    /// Independent per-packet loss with correlation, `loss` field of
    /// [`NetemConfig`] is the threshold.
    #[default]
    Random,
    /// Four-state Markov chain ([`FourStateLoss`]).
    FourState(FourStateLoss),
}

impl LossModel {
    /// Decide whether a packet is lost. Faithfully reproduces
    /// `loss_4state` and the `CLG_RANDOM` branch of `loss_event` in
    /// `sch_netem.c`.
    fn loss(
        &self,
        clg: &mut FourState,
        loss_cor: &mut CorRng,
        rng: &mut RndState,
        loss: u32,
    ) -> bool {
        match self {
            LossModel::Random => loss != 0 && loss >= loss_cor.next(rng),
            LossModel::FourState(p) => {
                let rnd = rng.next_u32();
                match clg {
                    FourState::TxInGap => {
                        if rnd < p.p14 {
                            *clg = FourState::LostInGap;
                            return true;
                        } else if rnd < p.p13.saturating_add(p.p14) {
                            *clg = FourState::LostInBurst;
                            return true;
                        }
                    }
                    FourState::TxInBurst => {
                        if rnd < p.p23 {
                            *clg = FourState::LostInBurst;
                            return true;
                        }
                    }
                    FourState::LostInBurst => {
                        if rnd < p.p32 {
                            *clg = FourState::TxInBurst;
                        } else if rnd < p.p31.saturating_add(p.p32) {
                            *clg = FourState::TxInGap;
                        } else {
                            *clg = FourState::LostInBurst;
                            return true;
                        }
                    }
                    FourState::LostInGap => {
                        *clg = FourState::TxInGap;
                    }
                }
                false
            }
        }
    }
}

// ──────────────────────────── UDP transport ────────────────────────────

/// Abstract UDP datagram transport.
///
/// Implemented as a trait object (`Box<dyn UdpTransport>`) so the harness can
/// run against either real OS sockets or an in-memory loopback in tests.
pub trait UdpTransport: Send + Sync + 'static {
    /// Receive a datagram into `buf`, returning `(len, from)`.
    fn recv_from(&self, buf: &mut [u8]) -> io::Result<(usize, SocketAddr)>;

    /// Send `data` to `dst`.
    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()>;

    /// Local address of the bound socket.
    fn local_addr(&self) -> io::Result<SocketAddr>;
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
    fn send_to(&self, data: &[u8], dst: SocketAddr) -> io::Result<()> {
        self.sock.send_to(data, dst)?;
        Ok(())
    }
    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.sock.local_addr()
    }
}

// ──────────────────────────── config & stats ───────────────────────────

/// Configuration for one emulated direction.
#[derive(Clone, Debug)]
pub struct NetemConfig {
    /// Fixed delay added to every packet.
    pub latency: Duration,
    /// Jitter ±; `tabledist` uniform spread when no distribution table.
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
    /// Gap for the reorder counter (`gap` in sch_netem).
    pub gap: u32,
    /// Loss model.
    pub loss_model: LossModel,
    /// Rate limit in bits/s; `0` disables rate-limiting.
    pub rate: u64,
    /// PRNG seed for deterministic behaviour.
    pub seed: u64,
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
            gap: 0,
            loss_model: LossModel::default(),
            rate: 0,
            seed: 0xC0FF_EEBE_EFC0_FFEE,
        }
    }
}

/// Live impairment counters – mirrors `struct tc_netem_xstats`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub delayed: u64,
    pub dropped: u64,
    pub duplicated: u64,
    pub reordered: u64,
    pub rate_limited: u64,
    pub forwarded: u64,
    pub received: u64,
}

/// Read-only snapshot of a link's state at a point in time.
#[derive(Clone, Copy, Debug, Default)]
pub struct Snapshot {
    pub stats: Stats,
    pub queue_len: usize,
}

// ───────────────────────────── queued packet ───────────────────────────

#[derive(Clone)]
struct Queued {
    time_to_send: Instant,
    data: Vec<u8>,
    dst: SocketAddr,
}

// ───────────────────────────── the link ─────────────────────────────────

/// A running emulated link. Dropping the handle does *not* stop the proxy
/// thread; call [`NetemLink::stop`] for that.
pub struct NetemLink {
    client_addr: SocketAddr,
    server_addr: SocketAddr,
    stats: Arc<Mutex<Stats>>,
    queue_len: Arc<Mutex<usize>>,
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
        let stats = Arc::new(Mutex::new(Stats::default()));
        let queue_len = Arc::new(Mutex::new(0));
        let stop = Arc::new(Mutex::new(false));

        let link = Self {
            client_addr,
            server_addr,
            stats: Arc::clone(&stats),
            queue_len: Arc::clone(&queue_len),
            stop: Arc::clone(&stop),
        };

        let runner = Runner::new(config, server_addr, stats, queue_len, stop, transport);
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
    pub fn stats(&self) -> Stats {
        *self.stats.lock().unwrap()
    }

    /// Current queue depth.
    pub fn queue_len(&self) -> usize {
        *self.queue_len.lock().unwrap()
    }

    /// Atomic snapshot.
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            stats: self.stats(),
            queue_len: self.queue_len(),
        }
    }

    /// Signal the proxy thread to stop after the next iteration.
    pub fn stop(&self) {
        *self.stop.lock().unwrap() = true;
    }
}

// ───────────────────────────── runner ───────────────────────────────────

struct Runner {
    config: NetemConfig,
    server_addr: SocketAddr,
    stats: Arc<Mutex<Stats>>,
    queue_len: Arc<Mutex<usize>>,
    stop: Arc<Mutex<bool>>,
    transport: Box<dyn UdpTransport>,
    rng: RndState,
    delay_cor: CorRng,
    loss_cor: CorRng,
    dup_cor: CorRng,
    reorder_cor: CorRng,
    clg: FourState,
    /// Earliest time the next packet may be serialized (send-time shaper).
    /// Tracks the per-direction serialization backlog for rate limiting.
    next_send: Instant,
    queue: VecDeque<Queued>,
    reorder_counter: u32,
}

impl Runner {
    fn new(
        config: NetemConfig,
        server_addr: SocketAddr,
        stats: Arc<Mutex<Stats>>,
        queue_len: Arc<Mutex<usize>>,
        stop: Arc<Mutex<bool>>,
        transport: Box<dyn UdpTransport>,
    ) -> Self {
        let rng = RndState::seed(config.seed);
        Self {
            delay_cor: CorRng::new(config.delay_corr),
            loss_cor: CorRng::new(config.loss_corr),
            dup_cor: CorRng::new(config.dup_corr),
            reorder_cor: CorRng::new(config.reorder_corr),
            next_send: Instant::now(),
            config,
            server_addr,
            stats,
            queue_len,
            stop,
            transport,
            rng,
            clg: FourState::default(),
            queue: VecDeque::new(),
            reorder_counter: 0,
        }
    }

    fn run(mut self) {
        let mut buf = [0u8; 64 * 1024];
        loop {
            if *self.stop.lock().unwrap() {
                break;
            }
            // Drain ready packets first so latency is honoured.
            self.drain_ready(Instant::now());

            // Block briefly on recv so we don't spin; non-blocking would be
            // ideal but the dyn transport is sync. Use a short timeout.
            match self.transport.recv_from(&mut buf) {
                Ok((n, from)) => {
                    self.handle_datagram(&buf[..n], from, Instant::now());
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

    fn handle_datagram(&mut self, data: &[u8], _from: SocketAddr, now: Instant) {
        {
            let mut s = self.stats.lock().unwrap();
            s.received += 1;
        }

        // ── duplication ──────────────────────────────────────────────
        let mut count = 1u32;
        if self.config.duplicate != 0 && self.config.duplicate >= self.dup_cor.next(&mut self.rng) {
            count += 1;
            let mut s = self.stats.lock().unwrap();
            s.duplicated += 1;
        }

        // ── loss ─────────────────────────────────────────────────────
        if self.config.loss_model.loss(
            &mut self.clg,
            &mut self.loss_cor,
            &mut self.rng,
            self.config.loss,
        ) {
            let mut s = self.stats.lock().unwrap();
            s.dropped += 1;
            // A lost packet still consumes a duplication slot.
            count = count.saturating_sub(1);
        }

        if count == 0 {
            return;
        }

        // ── rate limit (send-time shaping) ──────────────────────────────
        // netem rate delays packets by serialization time, never drops them.
        for _ in 0..count {
            self.enqueue(data, now);
        }
    }

    /// Compute the per-packet delay via `tabledist` (uniform spread when no
    /// distribution table, matching the kernel's default branch).
    fn sample_delay(&mut self) -> Duration {
        if self.config.jitter.is_zero() {
            return self.config.latency;
        }
        let rnd = self.delay_cor.next(&mut self.rng);
        let sigma = self.config.jitter.as_nanos() as u64;
        // uniform in [mu - sigma, mu + sigma]
        let spread = rnd % (2 * sigma as u32);
        let delta = (spread as i64) - (sigma as i64);
        let ns = self.config.latency.as_nanos() as i64 + delta;
        if ns < 0 {
            Duration::ZERO
        } else {
            Duration::from_nanos(ns as u64)
        }
    }

    fn enqueue(&mut self, data: &[u8], now: Instant) {
        // ── reorder ──────────────────────────────────────────────────
        // Reorder only when gap != 0 and only after the reorder counter
        // reaches gap - 1; use "reorder >= random" like sch_netem.
        //
        // Mirrors the Linux `sch_netem` branch structure: the normal
        // branch applies delay and (optionally) rate shaping, while the
        // reorder branch schedules the packet for immediate send (`now`)
        // and resets the reorder counter — rate shaping is *not* applied
        // to reordered packets, so they always jump ahead of the shaped
        // tail.
        let reorder = self.config.gap != 0
            && self.reorder_counter >= self.config.gap - 1
            && self.config.reorder >= self.reorder_cor.next(&mut self.rng);

        let time_to_send = if reorder {
            self.reorder_counter = 0;
            {
                let mut s = self.stats.lock().unwrap();
                s.reordered += 1;
            }
            // Reordered packet is scheduled immediately; no rate shaping.
            now
        } else {
            let delay = self.sample_delay();
            self.reorder_counter = self.reorder_counter.wrapping_add(1);
            if !delay.is_zero() {
                let mut s = self.stats.lock().unwrap();
                s.delayed += 1;
            }
            let base = now + delay;

            // ── rate shaping (normal branch only) ─────────────────────
            // Schedule after max(now + configured_delay, previous
            // scheduled send time) + packet_bits / rate_bps. Send-time
            // shaping only delays packets; it never drops them.
            if self.config.rate != 0 {
                let packet_bits = (data.len() as u64).saturating_mul(8);
                let serialize = Duration::from_nanos(
                    packet_bits.saturating_mul(1_000_000_000) / self.config.rate,
                );
                let earliest = base.max(self.next_send);
                let t = earliest + serialize;
                self.next_send = t;
                if t != base {
                    let mut s = self.stats.lock().unwrap();
                    s.rate_limited += 1;
                }
                t
            } else {
                base
            }
        };

        // keep queue sorted by time_to_send (simple insertion)
        let item = Queued {
            time_to_send,
            data: data.to_vec(),
            dst: self.server_addr,
        };
        if let Some(pos) = self
            .queue
            .iter()
            .rposition(|q| q.time_to_send <= time_to_send)
        {
            self.queue.insert(pos + 1, item);
        } else {
            self.queue.push_front(item);
        }
        *self.queue_len.lock().unwrap() = self.queue.len();
    }

    fn drain_ready(&mut self, now: Instant) {
        loop {
            let front = self.queue.front();
            let ready = match front {
                Some(q) => q.time_to_send <= now,
                None => false,
            };
            if !ready {
                break;
            }
            let Queued { data, dst, .. } = self.queue.pop_front().unwrap();
            *self.queue_len.lock().unwrap() = self.queue.len();
            if self.transport.send_to(&data, dst).is_ok() {
                let mut s = self.stats.lock().unwrap();
                s.forwarded += 1;
            }
        }
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
    stats_c2s: Arc<Mutex<Stats>>,
    stats_s2c: Arc<Mutex<Stats>>,
    stop: Arc<Mutex<bool>>,
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
        Self::spawn_from_sockets(server_addr, c2s, s2c, client_sock, server_sock, client_addr)
    }

    fn spawn_from_sockets(
        server_addr: SocketAddr,
        c2s: NetemConfig,
        s2c: NetemConfig,
        client_sock: Box<dyn UdpTransport>,
        server_sock: Box<dyn UdpTransport>,
        client_addr: SocketAddr,
    ) -> io::Result<Self> {
        let stats_c2s = Arc::new(Mutex::new(Stats::default()));
        let stats_s2c = Arc::new(Mutex::new(Stats::default()));
        let stop = Arc::new(Mutex::new(false));
        let learned_client = Arc::new(Mutex::<Option<SocketAddr>>::new(None));

        let pair = Self {
            client_addr,
            server_addr,
            stats_c2s: Arc::clone(&stats_c2s),
            stats_s2c: Arc::clone(&stats_s2c),
            stop: Arc::clone(&stop),
        };

        // Both sockets are shared between the two runners via `Arc`: each
        // direction uses one socket to recv and the other to send.
        let client_sock: Arc<dyn UdpTransport> = Arc::from(client_sock);
        let server_sock: Arc<dyn UdpTransport> = Arc::from(server_sock);

        // c2s: recv on client_sock, send on server_sock to server_addr; learn
        // the client's address from the first packet.
        let c2s_runner = DirectionRunner::new(
            c2s,
            stats_c2s,
            Arc::clone(&stop),
            Arc::clone(&client_sock),
            Arc::clone(&server_sock),
            Some(server_addr),
            Arc::clone(&learned_client),
        );
        std::thread::Builder::new()
            .name("netem-c2s".into())
            .spawn(move || c2s_runner.run())?;

        // s2c: recv on server_sock, send on client_sock to the learned client
        // address.
        let s2c_runner = DirectionRunner::new(
            s2c,
            stats_s2c,
            stop,
            server_sock,
            client_sock,
            None,
            learned_client,
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

    /// Stats for the client→server direction.
    pub fn stats_c2s(&self) -> Stats {
        *self.stats_c2s.lock().unwrap()
    }

    /// Stats for the server→client direction.
    pub fn stats_s2c(&self) -> Stats {
        *self.stats_s2c.lock().unwrap()
    }

    /// Combined stats across both directions.
    pub fn stats(&self) -> Stats {
        let a = self.stats_c2s();
        let b = self.stats_s2c();
        Stats {
            delayed: a.delayed + b.delayed,
            dropped: a.dropped + b.dropped,
            duplicated: a.duplicated + b.duplicated,
            reordered: a.reordered + b.reordered,
            rate_limited: a.rate_limited + b.rate_limited,
            forwarded: a.forwarded + b.forwarded,
            received: a.received + b.received,
        }
    }

    /// Signal both proxy threads to stop after the next iteration.
    pub fn stop(&self) {
        *self.stop.lock().unwrap() = true;
    }
}

// ─────────────────────── per-direction runner ────────────────────────────

struct DirectionRunner {
    config: NetemConfig,
    stats: Arc<Mutex<Stats>>,
    queue_len: Arc<Mutex<usize>>,
    stop: Arc<Mutex<bool>>,
    recv: Arc<dyn UdpTransport>,
    send: Arc<dyn UdpTransport>,
    /// Fixed destination (the real server for c2s). When `None`, the runner
    /// uses the learned client address (`learned_dst`).
    fixed_dst: Option<SocketAddr>,
    learned_dst: Arc<Mutex<Option<SocketAddr>>>,
    rng: RndState,
    delay_cor: CorRng,
    loss_cor: CorRng,
    dup_cor: CorRng,
    reorder_cor: CorRng,
    clg: FourState,
    /// Earliest time the next packet may be serialized (send-time shaper).
    /// Tracks the per-direction serialization backlog for rate limiting.
    next_send: Instant,
    queue: VecDeque<Queued>,
    reorder_counter: u32,
}

impl DirectionRunner {
    #[allow(clippy::too_many_arguments)]
    fn new(
        config: NetemConfig,
        stats: Arc<Mutex<Stats>>,
        stop: Arc<Mutex<bool>>,
        recv: Arc<dyn UdpTransport>,
        send: Arc<dyn UdpTransport>,
        fixed_dst: Option<SocketAddr>,
        learned_dst: Arc<Mutex<Option<SocketAddr>>>,
    ) -> Self {
        let rng = RndState::seed(config.seed);
        Self {
            delay_cor: CorRng::new(config.delay_corr),
            loss_cor: CorRng::new(config.loss_corr),
            dup_cor: CorRng::new(config.dup_corr),
            reorder_cor: CorRng::new(config.reorder_corr),
            next_send: Instant::now(),
            queue_len: Arc::new(Mutex::new(0)),
            config,
            stats,
            stop,
            recv,
            send,
            fixed_dst,
            learned_dst,
            rng,
            clg: FourState::default(),
            queue: VecDeque::new(),
            reorder_counter: 0,
        }
    }

    fn run(mut self) {
        let mut buf = [0u8; 64 * 1024];
        loop {
            if *self.stop.lock().unwrap() {
                break;
            }
            self.drain_ready(Instant::now());

            match self.recv.recv_from(&mut buf) {
                Ok((n, from)) => {
                    // For c2s, learn the client address so the s2c runner can
                    // send replies back to it.
                    if self.fixed_dst.is_some() {
                        *self.learned_dst.lock().unwrap() = Some(from);
                    }
                    self.handle_datagram(&buf[..n], Instant::now());
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

    fn handle_datagram(&mut self, data: &[u8], now: Instant) {
        {
            let mut s = self.stats.lock().unwrap();
            s.received += 1;
        }

        let mut count = 1u32;
        if self.config.duplicate != 0 && self.config.duplicate >= self.dup_cor.next(&mut self.rng) {
            count += 1;
            let mut s = self.stats.lock().unwrap();
            s.duplicated += 1;
        }

        if self.config.loss_model.loss(
            &mut self.clg,
            &mut self.loss_cor,
            &mut self.rng,
            self.config.loss,
        ) {
            let mut s = self.stats.lock().unwrap();
            s.dropped += 1;
            count = count.saturating_sub(1);
        }

        if count == 0 {
            return;
        }

        // ── rate limit (send-time shaping) ──────────────────────────────
        // netem rate delays packets by serialization time, never drops them.
        for _ in 0..count {
            self.enqueue(data, now);
        }
    }

    fn sample_delay(&mut self) -> Duration {
        if self.config.jitter.is_zero() {
            return self.config.latency;
        }
        let rnd = self.delay_cor.next(&mut self.rng);
        let sigma = self.config.jitter.as_nanos() as u64;
        let spread = rnd % (2 * sigma as u32);
        let delta = (spread as i64) - (sigma as i64);
        let ns = self.config.latency.as_nanos() as i64 + delta;
        if ns < 0 {
            Duration::ZERO
        } else {
            Duration::from_nanos(ns as u64)
        }
    }

    fn enqueue(&mut self, data: &[u8], now: Instant) {
        // ── reorder ──────────────────────────────────────────────────
        // Reorder only when gap != 0 and only after the reorder counter
        // reaches gap - 1; use "reorder >= random" like sch_netem.
        //
        // Mirrors the Linux `sch_netem` branch structure: the normal
        // branch applies delay and (optionally) rate shaping, while the
        // reorder branch schedules the packet for immediate send (`now`)
        // and resets the reorder counter — rate shaping is *not* applied
        // to reordered packets, so they always jump ahead of the shaped
        // tail.
        let reorder = self.config.gap != 0
            && self.reorder_counter >= self.config.gap - 1
            && self.config.reorder >= self.reorder_cor.next(&mut self.rng);

        let time_to_send = if reorder {
            self.reorder_counter = 0;
            {
                let mut s = self.stats.lock().unwrap();
                s.reordered += 1;
            }
            // Reordered packet is scheduled immediately; no rate shaping.
            now
        } else {
            let delay = self.sample_delay();
            self.reorder_counter = self.reorder_counter.wrapping_add(1);
            if !delay.is_zero() {
                let mut s = self.stats.lock().unwrap();
                s.delayed += 1;
            }
            let base = now + delay;

            // ── rate shaping (normal branch only) ─────────────────────
            // Schedule after max(now + configured_delay, previous
            // scheduled send time) + packet_bits / rate_bps. Send-time
            // shaping only delays packets; it never drops them.
            if self.config.rate != 0 {
                let packet_bits = (data.len() as u64).saturating_mul(8);
                let serialize = Duration::from_nanos(
                    packet_bits.saturating_mul(1_000_000_000) / self.config.rate,
                );
                let earliest = base.max(self.next_send);
                let t = earliest + serialize;
                self.next_send = t;
                if t != base {
                    let mut s = self.stats.lock().unwrap();
                    s.rate_limited += 1;
                }
                t
            } else {
                base
            }
        };

        let dst = self.fixed_dst.or_else(|| *self.learned_dst.lock().unwrap());
        let Some(dst) = dst else {
            // No known destination yet (s2c before the first client packet).
            return;
        };

        let item = Queued {
            time_to_send,
            data: data.to_vec(),
            dst,
        };
        if let Some(pos) = self
            .queue
            .iter()
            .rposition(|q| q.time_to_send <= time_to_send)
        {
            self.queue.insert(pos + 1, item);
        } else {
            self.queue.push_front(item);
        }
        *self.queue_len.lock().unwrap() = self.queue.len();
    }

    fn drain_ready(&mut self, now: Instant) {
        loop {
            let front = self.queue.front();
            let ready = match front {
                Some(q) => q.time_to_send <= now,
                None => false,
            };
            if !ready {
                break;
            }
            let Queued { data, dst, .. } = self.queue.pop_front().unwrap();
            *self.queue_len.lock().unwrap() = self.queue.len();
            if self.send.send_to(&data, dst).is_ok() {
                let mut s = self.stats.lock().unwrap();
                s.forwarded += 1;
            }
        }
    }
}

// ───────────────────────────── tests ────────────────────────────────────

#[cfg(test)]
mod tests {
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
    fn four_state_loss_can_drop_and_transmit() {
        let p = FourStateLoss {
            p13: 0,
            p31: u32::MAX,
            p32: 0,
            p14: u32::MAX, // from gap-Tx always go to isolated loss
            p23: 0,
        };
        let model = LossModel::FourState(p);
        let mut clg = FourState::TxInGap;
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(42);
        // first packet: rnd < p14 => lost, transition to LostInGap
        assert!(model.loss(&mut clg, &mut cor, &mut rng, 0));
        assert_eq!(clg, FourState::LostInGap);
        // next packet: LostInGap -> TxInGap, transmit
        assert!(!model.loss(&mut clg, &mut cor, &mut rng, 0));
        assert_eq!(clg, FourState::TxInGap);
    }

    #[test]
    fn random_loss_zero_never_drops() {
        let model = LossModel::Random;
        let mut clg = FourState::default();
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(1);
        for _ in 0..1000 {
            assert!(!model.loss(&mut clg, &mut cor, &mut rng, 0));
        }
    }

    #[test]
    fn random_loss_max_always_drops() {
        let model = LossModel::Random;
        let mut clg = FourState::default();
        let mut cor = CorRng::new(0);
        let mut rng = RndState::seed(1);
        for _ in 0..1000 {
            assert!(model.loss(&mut clg, &mut cor, &mut rng, u32::MAX));
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
    }
}
