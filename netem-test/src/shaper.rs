// ───────────────────────────── shared shaper ───────────────────────────

use crate::NetemConfig;
use crate::rng::{CorRng, RndState};
use std::sync::{Arc, Mutex};
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
/// Several [`NetemPair`] directions can share one [`SharedShaper`] so that
/// N flows contend for a single link rate instead of each flow getting its
/// own independent cap. Per-packet propagation delay, loss, jitter, and the
/// per-direction queue limit stay with each [`DirectionRunner`]; only the
/// send-time serialization clock and the optional shared tail-drop buffer are
/// shared.
#[derive(Clone, Debug)]
pub struct SharedShaper(Arc<Mutex<ShaperState>>);

#[derive(Debug)]
struct ShaperState {
    /// Shared rate in bits per second.
    rate: u64,
    /// Shared tail-drop buffer in bytes. `0` means unbounded.
    limit_bytes: u64,
    /// Earliest time the next packet may leave the shared bottleneck.
    next_send: Instant,
    /// Packets dropped because they exceeded `limit_bytes`.
    dropped: u64,
}

impl SharedShaper {
    /// Create a shared shaper. `rate_bps` must be greater than zero.
    /// `limit_bytes` is the shared tail-drop buffer; use `0` for unbounded.
    pub fn new(rate_bps: u64, limit_bytes: u64) -> Self {
        assert!(rate_bps > 0, "SharedShaper rate must be greater than zero");
        Self(Arc::new(Mutex::new(ShaperState {
            rate: rate_bps,
            limit_bytes,
            next_send: Instant::now(),
            dropped: 0,
        })))
    }

    /// Current configured rate in bits per second.
    pub fn rate_bps(&self) -> u64 {
        self.0.lock().unwrap().rate
    }

    /// Number of packets tail-dropped by the shared shaper.
    pub fn dropped(&self) -> u64 {
        self.0.lock().unwrap().dropped
    }

    /// Bytes currently sitting in the shared serialization backlog as of `now`.
    pub fn backlog_bytes(&self, now: Instant) -> u64 {
        let state = self.0.lock().unwrap();
        let backlog_ns = state.next_send.saturating_duration_since(now).as_nanos();
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
        let mut state = self.0.lock().unwrap();
        let backlog_ns = state.next_send.saturating_duration_since(base).as_nanos();
        let backlog = (backlog_ns * state.rate as u128 / 1_000_000_000 / 8) as u64;
        if state.limit_bytes != 0 && backlog.saturating_add(len as u64) > state.limit_bytes {
            state.dropped += 1;
            return None;
        }
        let packet_bits = (len as u64).saturating_mul(8);
        let serialize_ns = (packet_bits as u128).saturating_mul(1_000_000_000) / state.rate as u128;
        let t = base.max(state.next_send) + Duration::from_nanos(serialize_ns as u64);
        state.next_send = t;
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
