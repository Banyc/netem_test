// ─────────────────────────── impairment presets ───────────────────────────

use std::time::Duration;

use netem_test::{FourStateLoss, LossModel, NetemConfig};

/// No impairment at all — a clean baseline to verify the plumbing.
pub fn clean() -> NetemConfig {
    NetemConfig::default()
}

/// A latency-only config useful for verifying the proxy adds delay.
pub fn latency(ms: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(ms),
        seed: 7,
        ..NetemConfig::default()
    }
}

/// Mild impairment that exercises both directions without making `rtp`'s
/// reliable layer give up: ~5% random loss, 10 ms latency, small jitter.
pub fn mild_loss() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 20, // ~5%
        latency: Duration::from_millis(10),
        jitter: Duration::from_millis(5),
        seed: 1,
        ..NetemConfig::default()
    }
}

/// A lossy rate-limited config sized for the 400 KiB perf scenario. The rate
/// is `400 * 1024 * 8` bits/s with a small loss/latency/jitter so the link is
/// contended but not hopeless.
pub fn lossy_400kib_per_sec() -> NetemConfig {
    NetemConfig {
        rate: 400 * 1024 * 8,
        loss: u32::MAX / 100, // ~1%
        latency: Duration::from_millis(5),
        jitter: Duration::from_millis(2),
        seed: 4,
        ..NetemConfig::default()
    }
}

/// A hostile link profiled from real ICMP measurements against `google.com`
/// and `8.8.8.8` from this machine: ~15% loss, ~300 ms latency, ~500 ms
/// jitter (stddev). Used by the 400 MiB perf scenario to expose proxy-level
/// bottlenecks (queue insertion, per-packet locking, allocation) that the
/// mild synthetic presets cannot reach.
pub fn hostile_real_link() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 100 * 15, // ~15%
        latency: Duration::from_millis(300),
        jitter: Duration::from_millis(500),
        seed: 4,
        ..NetemConfig::default()
    }
}

pub fn hostile_fat_pipe() -> NetemConfig {
    NetemConfig {
        rate: 100 * 1000 * 1000,
        latency: Duration::from_millis(150),
        jitter: Duration::from_millis(30),
        loss_model: gilbert_elliott_loss(2.0, 4.0),
        limit: 16 * 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// Two-state Gilbert-Elliott loss model on top of the four-state `sch_netem`
/// representation.
///
/// `loss_pct` is the long-term loss probability (0–100). `mean_burst_len` is the
/// average number of consecutive lost packets (must be ≥ 1.0). The model is
/// parameterised so that bursts have `mean_burst_len` losses and gaps have a
/// geometrically-distributed number of deliveries between bursts.
///
/// Maps to [`FourStateLoss`] probabilities scaled so `u32::MAX == 1.0`:
/// * `p13` = probability a delivered packet in the gap state moves into the
///   burst-loss state (that packet is lost).
/// * `p31` = probability the burst-loss state returns to the gap state (that
///   delivered packet ends the burst).
/// * `p14` = `p23` = `p32` = 0.
///
/// Staying in burst-loss loses every packet, so burst and gap lengths are
/// geometric with means `1/p31` and `1/p13` respectively. `p14` cannot express
/// bursts because its successor state `LostInGap` always delivers the next
/// packet.
pub fn gilbert_elliott_loss(loss_pct: f64, mean_burst_len: f64) -> LossModel {
    assert!(
        (0.0..=100.0).contains(&loss_pct),
        "loss_pct must be in [0, 100]"
    );
    assert!(mean_burst_len >= 1.0, "mean_burst_len must be >= 1.0");

    // Long-term probability of being in the burst (loss) state.
    let p_burst = loss_pct / 100.0;
    // Mean number of delivered packets between isolated burst triggers, given
    // mean_burst_len and p_burst. Derived from the steady-state equations for
    // the two-state model.
    let mean_gap_len = if p_burst == 0.0 {
        f64::INFINITY
    } else {
        mean_burst_len * (1.0 - p_burst) / p_burst
    };

    let scale = |p: f64| -> u32 {
        let clamped = p.clamp(0.0, 1.0);
        (clamped * u32::MAX as f64).round() as u32
    };

    LossModel::FourState(FourStateLoss {
        p13: scale(1.0 / mean_gap_len),
        p31: scale(1.0 / mean_burst_len),
        p32: 0,
        p14: 0,
        p23: 0,
    })
}

/// Bidirectional `NetemPair` config with the Gilbert-Elliott burst loss model.
///
/// No rate cap, no latency beyond the optional one-way `owd`, and a
/// deterministic seed so the tests are reproducible.
pub fn burst_loss_link(
    loss_pct: f64,
    mean_burst_len: f64,
    owd: Duration,
    seed: u64,
) -> NetemConfig {
    NetemConfig {
        loss_model: gilbert_elliott_loss(loss_pct, mean_burst_len),
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}

/// Bidirectional `NetemPair` config with independent random loss.
///
/// `loss_pct` is the per-packet drop probability (0–100). This is useful as a
/// baseline in the burst-vs-random comparison tests.
pub fn random_loss_link(loss_pct: f64, owd: Duration, seed: u64) -> NetemConfig {
    let loss = ((loss_pct / 100.0) * u32::MAX as f64).clamp(0.0, u32::MAX as f64) as u32;
    NetemConfig {
        loss,
        latency: owd,
        seed,
        ..NetemConfig::default()
    }
}
