// ───────────────────────────── shared shaper ───────────────────────────

use crate::NetemConfig;
use crate::rng::{CorRng, RndState};
use parking_lot::Mutex;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub(crate) fn serialization_delay(len: usize, rate_bps: u64) -> Option<Duration> {
    (len as u64)
        .saturating_mul(8)
        .saturating_mul(1_000_000_000)
        .checked_div(rate_bps)
        .map(Duration::from_nanos)
}

/// Multi-flow shared-bottleneck serialization clock.
///
/// Several [`NetemPair`] directions can share one [`BottleneckShaper`] so that
/// N flows contend for a single link rate instead of each flow getting its
/// own independent cap. Per-packet propagation delay, loss, jitter, and the
/// per-direction queue limit stay with each [`SharedLinkRunner`]; only the
/// send-time serialization clock and the optional shared tail-drop buffer are
/// shared.
#[derive(Clone, Debug)]
pub struct BottleneckShaper(Arc<Mutex<BottleneckState>>);

#[derive(Debug)]
struct BottleneckState {
    /// Shared rate in bits per second.
    rate: u64,
    /// Shared tail-drop buffer in bytes. `0` means unbounded.
    limit_bytes: u64,
    /// Earliest time the next packet may leave the shared bottleneck.
    link_free_at: Instant,
    /// Packets dropped because they exceeded `limit_bytes`.
    dropped: u64,
    /// Length of the most recently serialized packet, for the serialization
    /// cache.
    last_packet_len: usize,
    /// Serialization nanoseconds for `last_packet_len` at `rate`.
    last_serialize_ns: u64,
}

/// Nanobits per byte: 8 bits/byte * 1e9 ns/s. Scaling the serialization and
/// backlog arithmetic by this constant keeps every comparison in whole
/// integers with no division.
const NANOBITS_PER_BYTE: u128 = 8_000_000_000;
/// Nanobits spanned by a full `u64`-second nanosecond counter wrap at the
/// byte scale: a backlog older than this has a whole-byte size that cannot
/// fit in `u64`, so it exceeds any byte limit.
const U64_BACKLOG_WRAP_NANOBITS: u128 = (u64::MAX as u128 + 1) * NANOBITS_PER_BYTE;

/// Whether `backlog_ns` of already-committed wire time at `rate` plus a new
/// `len`-byte packet would exceed `limit_bytes`, decided with whole-byte
/// backlog math but without division.
///
/// The whole-byte backlog is `backlog_ns * rate / NANOBITS_PER_BYTE`, so the
/// admit condition `backlog_bytes + len <= limit_bytes` is equivalent
/// (multiplying through by `NANOBITS_PER_BYTE`) to
/// `backlog_ns * rate + len * NANOBITS_PER_BYTE <= limit_bytes *
/// NANOBITS_PER_BYTE`.
pub(crate) fn exceeds_byte_limit(
    limit_bytes: u64,
    backlog_ns: u128,
    rate: u64,
    len: usize,
) -> bool {
    let backlog_nanobits = backlog_ns.saturating_mul(rate as u128);
    if backlog_nanobits > U64_BACKLOG_WRAP_NANOBITS {
        // More than 2^64 ns of committed wire time: the whole-byte backlog is
        // larger than any `u64` byte limit even before the new packet.
        return true;
    }
    let packet_nanobits = (len as u128).saturating_mul(NANOBITS_PER_BYTE);
    let limit_nanobits = (limit_bytes as u128).saturating_mul(NANOBITS_PER_BYTE);
    backlog_nanobits.saturating_add(packet_nanobits) > limit_nanobits
}

impl BottleneckShaper {
    /// Create a shared shaper. `rate_bps` must be greater than zero.
    /// `limit_bytes` is the shared tail-drop buffer; use `0` for unbounded.
    pub fn new(rate_bps: u64, limit_bytes: u64) -> Self {
        assert!(
            rate_bps > 0,
            "BottleneckShaper rate must be greater than zero"
        );
        Self(Arc::new(Mutex::new(BottleneckState {
            rate: rate_bps,
            limit_bytes,
            link_free_at: Instant::now(),
            dropped: 0,
            last_packet_len: 0,
            last_serialize_ns: 0,
        })))
    }

    /// Current configured rate in bits per second.
    pub fn rate_bps(&self) -> u64 {
        self.0.lock().rate
    }

    /// Number of packets tail-dropped by the shared shaper.
    pub fn dropped(&self) -> u64 {
        self.0.lock().dropped
    }

    /// Bytes currently sitting in the shared serialization backlog as of `now`.
    pub fn backlog_bytes(&self, now: Instant) -> u64 {
        let state = self.0.lock();
        let backlog_ns = state.link_free_at.saturating_duration_since(now).as_nanos();
        (backlog_ns * state.rate as u128 / 1_000_000_000 / 8) as u64
    }

    /// Schedule a packet of `len` bytes arriving at `base` through the shared
    /// bottleneck.
    ///
    /// Returns `Some(exit_time)` if the packet is accepted, or `None` if it is
    /// tail-dropped because `limit_bytes` would be exceeded. The returned
    /// exit time does *not* include per-flow propagation delay; the caller must
    /// add its own latency/jitter afterwards.
    pub fn schedule(&self, base: Instant, len: usize) -> Option<Instant> {
        let mut state = self.0.lock();
        // Bypass the backlog arithmetic entirely when the shared tail-drop
        // buffer is unbounded.
        if state.limit_bytes != 0 {
            let backlog_ns = state
                .link_free_at
                .saturating_duration_since(base)
                .as_nanos();
            if exceeds_byte_limit(state.limit_bytes, backlog_ns, state.rate, len) {
                state.dropped += 1;
                return None;
            }
        }
        // Serialization scales with packet length; consecutive packets of the
        // same length reuse the cached serialization instead of re-multiplying.
        let serialize_ns = if state.last_packet_len == len {
            state.last_serialize_ns
        } else {
            let ns = ((len as u128) * NANOBITS_PER_BYTE / state.rate as u128) as u64;
            state.last_packet_len = len;
            state.last_serialize_ns = ns;
            ns
        };
        let t = base.max(state.link_free_at) + Duration::from_nanos(serialize_ns);
        state.link_free_at = t;
        Some(t)
    }
}

pub(crate) fn sample_delay(
    config: &NetemConfig,
    rng: &mut RndState,
    delay_cor: &mut CorRng,
) -> Duration {
    if config.jitter.is_zero() {
        return config.latency;
    }
    let rnd = delay_cor.next(rng);
    let sigma = config.jitter.as_nanos().min(i32::MAX as u128) as u64;
    let spread = u64::from(rnd) % (2 * sigma);
    let delta = (spread as i64) - (sigma as i64);
    let ns = config.latency.as_nanos() as i64 + delta;
    if ns < 0 {
        Duration::ZERO
    } else {
        Duration::from_nanos(ns as u64)
    }
}
