// ────────────────── deterministic emulated forwarding ──────────────────

//! Drive one [`NetemConfig`] direction's real impairment pipeline on an
//! emulated clock through an in-memory transport.
//!
//! This is the pipeline a spawned link runs — the same PRNG draws, the same
//! delay sampler, the same tail-drop and shaper arithmetic, the same heap or
//! FIFO ordering, the same counters — with only the two things the host owns
//! replaced:
//!
//! * the clock: emulated time moves forward to the next scheduled event
//!   instead of a runner thread sleeping until a deadline, so a datagram
//!   leaves at exactly its scheduled instant rather than at the instant the
//!   scheduler got round to the thread; and
//! * the transport: forwarded datagrams are recorded in memory rather than
//!   written to a socket, so nothing is lost to a socket buffer under load.
//!
//! A caller supplies each datagram's arrival offset and gets back the
//! payloads in forwarding order with the emulated offset each left, which is
//! enough to reconstruct a round trip (`rtt = return_forward_offset -
//! send_offset`) without reading the wall clock. A measurement built on this
//! is therefore a fact about the configured impairment and the forwarding
//! arithmetic, not about how busy the host was.

use std::io;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64};
use std::time::{Duration, Instant};

use parking_lot::Mutex;

use crate::{AtomicCounters, Clock, Counters, FifoQueue, NetemConfig, NetemState, UdpTransport};

/// One datagram a direction forwarded.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Forwarded {
    /// Exact payload bytes that left the direction.
    pub payload: Vec<u8>,
    /// Emulated instant the datagram left, measured from the direction's
    /// emulated origin (the instant its pipeline was built).
    pub at: Duration,
}

/// In-memory transport: every `send_to` is appended to a queue the caller
/// drains. Receives never yield a datagram — the caller feeds arrivals
/// directly, so the direction pulls nothing on its own.
#[derive(Debug)]
struct RecordingTransport {
    sent: Mutex<Vec<Vec<u8>>>,
    local: SocketAddr,
}

impl RecordingTransport {
    fn new() -> Self {
        Self {
            sent: Mutex::new(Vec::new()),
            local: SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 1)),
        }
    }

    /// Remove and return every datagram forwarded since the last call, in
    /// forwarding order.
    fn take_sent(&self) -> Vec<Vec<u8>> {
        std::mem::take(&mut *self.sent.lock())
    }
}

impl UdpTransport for RecordingTransport {
    fn recv_from(&self, _buf: &mut [u8]) -> io::Result<(usize, SocketAddr)> {
        Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "emulated direction is fed arrivals directly",
        ))
    }

    fn recv_from_timeout(
        &self,
        _buf: &mut [u8],
        _timeout: Duration,
    ) -> io::Result<(usize, SocketAddr)> {
        self.recv_from(_buf)
    }

    fn send_to(&self, data: &[u8], _dst: SocketAddr) -> io::Result<()> {
        self.sent.lock().push(data.to_vec());
        Ok(())
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        Ok(self.local)
    }

    fn set_recv_timeout(&self, _timeout: Duration) -> io::Result<()> {
        Ok(())
    }

    fn recv_timeout(&self) -> io::Result<Option<Duration>> {
        Ok(None)
    }
}

/// One direction's impairment pipeline on an emulated clock.
struct EmulatedDirection {
    clock: Clock,
    state: NetemState,
    fifo: FifoQueue,
    transport: Arc<RecordingTransport>,
    stats: Arc<AtomicCounters>,
    dst: SocketAddr,
}

impl EmulatedDirection {
    fn new(config: &NetemConfig) -> Self {
        let clock = Clock::new();
        let stats = Arc::new(AtomicCounters::default());
        let transport = Arc::new(RecordingTransport::new());
        let state = NetemState::new(
            config.clone(),
            Arc::clone(&stats),
            Arc::new(AtomicU64::new(0)),
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
            None,
            Some(clock.clone()),
        );
        Self {
            clock,
            state,
            fifo: FifoQueue::default(),
            transport,
            stats,
            dst: SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 1)),
        }
    }

    /// Move emulated time forward to `offset` (never backward).
    fn advance_to(&self, offset: Duration) {
        let elapsed = self.clock.elapsed();
        if offset > elapsed {
            self.clock.advance(offset - elapsed);
        }
    }

    /// Deliver one arriving datagram through the path `LinkRunner::run` picks
    /// for this config: the direct loop for a config with no scheduling, the
    /// FIFO path when every deadline is monotonic, the delay heap otherwise.
    fn deliver(&mut self, payload: &[u8]) {
        let now = self.state.now();
        if self.state.direct_forward {
            self.state
                .forward_direct(payload, Some(self.dst), &*self.transport);
        } else if self.state.direct_stochastic {
            self.state
                .forward_stochastic_direct(payload, Some(self.dst), &*self.transport);
        } else if self.state.uses_fifo_scheduling() {
            self.state
                .handle_datagram_fifo(payload, now, Some(self.dst), &mut self.fifo);
        } else {
            self.state.handle_datagram(payload, now, Some(self.dst));
        }
    }

    /// Forward every datagram whose deadline has arrived, using the same drain
    /// the matching runner loop uses.
    fn drain_due(&mut self) {
        if self.state.direct_forward || self.state.direct_stochastic {
            // The direct paths forward on arrival; nothing is ever queued.
            return;
        }
        let now = self.state.now();
        if self.state.uses_fifo_scheduling() {
            self.state
                .drain_ready_fifo(&mut self.fifo, now, &*self.transport);
        } else {
            self.state.drain_ready(now, &*self.transport);
        }
    }

    /// Earliest deadline among queued datagrams, if any.
    fn next_due(&self) -> Option<Instant> {
        if self.state.uses_fifo_scheduling() {
            self.fifo.packets.front().map(|queued| queued.time_to_send)
        } else if self.state.direct_forward || self.state.direct_stochastic {
            None
        } else {
            self.state.queue.peek().map(|queued| queued.0.time_to_send)
        }
    }

    /// Deadline as an emulated offset from the pipeline's origin.
    fn next_due_offset(&self) -> Option<Duration> {
        self.next_due().map(|deadline| self.clock.offset(deadline))
    }
}

/// Forward `arrivals` through `config`'s impairment pipeline on an emulated
/// clock, where each arrival is `(payload, offset the datagram arrives at)`.
///
/// Returns the forwarded datagrams in forwarding order with their emulated
/// forwarding offsets, plus the direction's counters. Emulated time only ever
/// moves to the next event, so the result is a pure function of
/// `(config, arrivals)`: identical inputs replay identically however loaded
/// the host is.
///
/// A datagram arriving at the exact instant another's deadline expires is
/// drained first and delivered second, mirroring the runner loops, which drain
/// at the top of each iteration before they receive.
pub fn emulated_forward(
    config: &NetemConfig,
    arrivals: impl IntoIterator<Item = (Vec<u8>, Duration)>,
) -> (Vec<Forwarded>, Counters) {
    // A stable sort keeps a caller's order among arrivals at one instant while
    // letting the driver walk events in emulated-time order.
    let mut arrivals: Vec<(Vec<u8>, Duration)> = arrivals.into_iter().collect();
    arrivals.sort_by_key(|(_, at)| *at);

    let mut direction = EmulatedDirection::new(config);
    let mut forwarded = Vec::new();
    let mut index = 0;
    loop {
        let next = match (direction.next_due_offset(), arrivals.get(index)) {
            (None, None) => break,
            (Some(due), None) => due,
            (None, Some((_, at))) => *at,
            (Some(due), Some((_, at))) => due.min(*at),
        };
        direction.advance_to(next);
        if direction.next_due_offset() == Some(next) {
            direction.drain_due();
        } else {
            let payload = arrivals[index].0.clone();
            direction.deliver(&payload);
            index += 1;
        }
        for payload in direction.transport.take_sent() {
            forwarded.push(Forwarded { payload, at: next });
        }
    }
    let counters = direction.stats.snapshot();
    (forwarded, counters)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kit::presets::{
        clean_delay_link, deterministic_iid_loss_fat_pipe, jittery_short_rtt_link,
    };

    fn arrivals(count: usize, spacing: Duration) -> Vec<(Vec<u8>, Duration)> {
        (0..count)
            .map(|id| ((id as u64).to_be_bytes().to_vec(), spacing * (id as u32)))
            .collect()
    }

    fn id_of(payload: &[u8]) -> u64 {
        u64::from_be_bytes(payload[..8].try_into().expect("eight-byte id"))
    }

    /// Fixed shaping: every datagram leaves exactly `latency` after it
    /// arrives, so a replay is exact and independent of the host's load.
    #[test]
    fn fixed_latency_forwards_each_datagram_exactly_one_latency_later() {
        let config = clean_delay_link(Duration::from_millis(20), 4);
        let spacing = Duration::from_millis(1);
        let (forwarded, counters) = emulated_forward(&config, arrivals(50, spacing));
        assert_eq!(forwarded.len(), 50);
        assert_eq!(counters.received, 50);
        assert_eq!(counters.forwarded, 50);
        for (id, datagram) in forwarded.iter().enumerate() {
            assert_eq!(
                datagram.at,
                Duration::from_millis(20) + spacing * (id as u32),
                "datagram {id} left at {}",
                datagram.at.as_secs_f64() * 1e3
            );
        }
    }

    /// The same inputs produce the same forwarding sequence and instants, and
    /// the counters match the number of datagrams that left.
    #[test]
    fn seeded_lanes_replay_identically() {
        let spacing = Duration::from_millis(1);
        for config in [jittery_short_rtt_link(), deterministic_iid_loss_fat_pipe()] {
            let first = emulated_forward(&config, arrivals(200, spacing));
            let second = emulated_forward(&config, arrivals(200, spacing));
            assert_eq!(first.0, second.0, "forwarding must replay identically");
            assert_eq!(first.1, second.1, "counters must replay identically");
            assert_eq!(
                first.0.len() as u64,
                first.1.forwarded,
                "every recorded datagram was counted once"
            );
        }
    }

    /// The jitter lane reorders (its deadlines are non-monotonic) and the
    /// zero-jitter lanes do not, which is the ordering property the heap and
    /// FIFO dispatch must preserve.
    #[test]
    fn jitter_reorders_where_a_zero_jitter_lane_cannot() {
        let spacing = Duration::from_millis(1);
        let jittery = emulated_forward(&jittery_short_rtt_link(), arrivals(200, spacing));
        let mut highest = 0u64;
        let mut inversions = 0;
        for datagram in &jittery.0 {
            let id = id_of(&datagram.payload);
            if id < highest {
                inversions += 1;
            }
            highest = highest.max(id);
        }
        assert!(
            inversions >= 50,
            "the jitter lane must reorder: {inversions} inverted forwards"
        );

        let control = clean_delay_link(Duration::from_millis(20), 4);
        let ordered = emulated_forward(&control, arrivals(200, spacing));
        for (expected, datagram) in ordered.0.iter().enumerate() {
            assert_eq!(
                id_of(&datagram.payload),
                expected as u64,
                "the control must forward in order"
            );
        }
    }

    /// A deterministic loss lane drops the same datagrams on every run, so the
    /// delivered set is reproducible rather than host-dependent.
    #[test]
    fn seeded_loss_lane_drops_the_same_datagrams_every_run() {
        let spacing = Duration::from_millis(1);
        let config = deterministic_iid_loss_fat_pipe();
        let (first, counters) = emulated_forward(&config, arrivals(200, spacing));
        let (second, _) = emulated_forward(&config, arrivals(200, spacing));
        assert_eq!(first, second);
        assert_eq!(counters.received, 200);
        assert_eq!(
            counters.forwarded + counters.dropped,
            200,
            "every datagram either left or was dropped"
        );
    }
}
