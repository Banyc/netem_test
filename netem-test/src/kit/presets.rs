// ─────────────────────────── impairment presets ───────────────────────────

use std::time::Duration;

use crate::{FourStateLoss, LossModel, NetemConfig};

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

/// A clean delay-only link with an explicit seed — one-way delay `owd`, no
/// loss/jitter/rate, deterministic. Promoted from the `hol_verify4` A/B
/// probes' local `clean_link` helper when the bulk-lane arms relocated into
/// the owning crates (mux and rtp each consume the same single authority).
pub fn clean_delay_link(owd: Duration, seed: u64) -> NetemConfig {
    NetemConfig {
        latency: owd,
        seed,
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
        queue_limit_pkts: 16 * 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// The fixed-shaping controller-retention lane: the same bandwidth-delay
/// product and queue limit as [`hostile_fat_pipe`] but with no stochastic
/// loss or jitter, so congestion-controller and queue-growth changes are
/// measured against a deterministic link.
pub fn controller_fat_pipe() -> NetemConfig {
    NetemConfig {
        rate: 100 * 1000 * 1000,
        latency: Duration::from_millis(150),
        queue_limit_pkts: 16 * 1024,
        ..NetemConfig::default()
    }
}

/// A deterministic, fixed-seed iid-loss fat pipe: the same bandwidth-delay
/// product and queue limit as [`controller_fat_pipe`] plus a fixed ~1 %
/// independent per-packet loss. The Tausworthe PRNG is seeded, so the exact
/// drop pattern is reproducible across runs (unlike the stochastic
/// Gilbert-Elliott [`hostile_fat_pipe`]); this isolates loss-recovery
/// behaviour from burst-state divergence.
pub fn deterministic_iid_loss_fat_pipe() -> NetemConfig {
    NetemConfig {
        rate: 100 * 1000 * 1000,
        latency: Duration::from_millis(150),
        loss: u32::MAX / 100, // ~1% independent per-packet loss
        loss_model: LossModel::Random,
        queue_limit_pkts: 16 * 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// A short-RTT, jitter-dominated link: 20 ms one-way with +/-15 ms of uniform
/// per-packet jitter, no rate shaping, no loss, and a 1024-packet queue
/// (kernel `sch_netem`'s default).
///
/// The lane exists because jitter is the one impairment no other preset can
/// realize. Every other lane either configures zero jitter
/// ([`controller_fat_pipe`], [`deterministic_iid_loss_fat_pipe`], the clean and
/// `*bottleneck` lanes), or configures a rate: with a rate and no reorder gap,
/// the send-time shaper schedules each packet at `max(now + delay,
/// previous_send) + serialization`, so a packet can never leave before the one
/// ahead of it and the sampled jitter is realized only as a running maximum,
/// never as reordering. A zero-jitter lane has monotonic deadlines too. So on
/// every lane that configures a rate *or* zero jitter the delivered order equals
/// the sent order and the receiver's RTT variance is whatever queue noise the
/// endpoint itself creates; this lane is the only one that reorders.
///
/// Shape: unshaped, 20 ms one-way, +/-15 ms uniform jitter, 1024-packet queue,
/// no loss or duplication. `sample_delay` spreads uniformly over
/// `latency +/- jitter`, so one-way delay is uniform on `[5 ms, 35 ms]` and a
/// round trip is triangular on `[10 ms, 70 ms]`, mean 40 ms. Seeded, so the
/// delay stream is reproducible: one `CorRng` value per delayed packet from
/// the Tausworthe generator seeded with [`NetemConfig::seed`].
pub fn jittery_short_rtt_link() -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(20),
        jitter: Duration::from_millis(15),
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// A high-RTT, thin-link bottleneck: 200 kbit/s, 400 ms one-way delay, and a
/// 128-packet queue, with no loss, duplication, jitter, or reordering.
///
/// The rate is far below anything an endpoint can send, so the sender's whole
/// excess builds as a standing queue in the delay heap. The arithmetic, for
/// the 8192-byte datagrams the bulk probe uses: `serialization_delay(8192,
/// 200_000) = 327.68 ms`, so a full 128-packet queue is `128 * 327.68 ms =
/// 41.9 s` on top of the 800 ms round-trip floor. Only one direction's
/// serialization is realized: the c2s shaper spaces the datagrams at exactly
/// that step, so the return path never builds a backlog of its own. RFC
/// 6298's `srtt + 4 * rttvar` therefore reaches tens of seconds from the link
/// alone, with no host pause and no lost packet.
///
/// On the 100 Mbit/s fat pipes the same 8192-byte datagram serializes in
/// 0.66 ms, and a sender that cannot out-run the link never builds the
/// backlog: their measured round trip stays within a millisecond of the 300 ms
/// floor. That is why an RTO inflation of tens of seconds is not measurable
/// there.
///
/// The send-time shaper only delays packets (`link_free_at` is a serialization
/// clock), so this lane's drops come from the `queue_limit_pkts` tail-drop,
/// never from the shaper.
///
/// That tail-drop also starves the peer's ACKs, which is what makes the lane
/// unusable for a goodput verdict. The 128-packet limit is already reached at
/// the first sample of a run (the sender offers ~27–30 packets per second
/// against a 3.05 pkt/s drain), and from then on every packet the sender
/// offers is dropped — data *and* the ACKs the peer is waiting for — while the
/// queue head keeps delivering data. Over 32 recorded 30 s-window runs,
/// `forwarded_bytes / forwarded` on this direction is 8191.0 in 29 and 8111.6
/// in the other three (a single small packet slips through in those), while
/// the arrival mix implies ~72–100 ACK packets per window. The peer's
/// peer-liveness `no_response` watchdog is refreshed only by an ACK, so it
/// fires 30 s after the last ACK that got through (t ~ 35.0–35.9 s) and
/// terminates the session, leaving the mux sink with `read_error/BrokenPipe`
/// and a dead second half in any measurement window that spans that moment.
/// The deadline lands 0.0–0.9 s after the end of a 5 s-warmup / 30 s-window
/// run, so the whole margin between a usable window and a broken one is one
/// second of warmup. The client's own application write does not observe the
/// teardown until ~17 s later (t ~ 53 s), where it ends in `bulk pump failed:
/// BrokenPipe`, so a run long enough to reach that point fails the probe
/// outright. The lane is therefore **diagnostic-only** in the perf battery
/// (`tests/GATE.md`, `gate-lane-roles`); the tens-of-seconds RTO regime it
/// exists to reach is still measured by `lane_regime_coverage`. Seeded.
pub fn high_rtt_low_rate_bottleneck() -> NetemConfig {
    NetemConfig {
        rate: 200 * 1000,
        latency: Duration::from_millis(400),
        queue_limit_pkts: 128,
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

// ─────────────────────── hostile / FEC calibration presets ───────────────────────

/// Hostile steady-state link with no shaping: ~15% random loss, ~300 ms
/// latency. Used to calibrate the interactive/bulk FEC treatment pair.
pub fn hostile_steady_link() -> NetemConfig {
    NetemConfig {
        loss: u32::MAX / 100 * 15,
        latency: Duration::from_millis(300),
        seed: 4,
        ..NetemConfig::default()
    }
}

pub fn hostile_steady_bottleneck() -> NetemConfig {
    hostile_steady_bottleneck_at(Duration::from_millis(300))
}

fn hostile_steady_bottleneck_at(latency: Duration) -> NetemConfig {
    NetemConfig {
        rate: 50 * 1000 * 1000,
        latency,
        loss: u32::MAX / 100 * 15,
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

pub fn hostile_steady_bottleneck_20ms() -> NetemConfig {
    hostile_steady_bottleneck_at(Duration::from_millis(20))
}

pub fn hostile_steady_bottleneck_100ms() -> NetemConfig {
    hostile_steady_bottleneck_at(Duration::from_millis(100))
}

/// Shaped 50 Mbps lane with a single evenly-spread deterministic drop per 20
/// packets: the erasure-recovery lane FEC must transparently survive.
pub fn fec_recoverable_bottleneck() -> NetemConfig {
    NetemConfig {
        rate: 50 * 1000 * 1000,
        latency: Duration::from_millis(300),
        loss_model: LossModel::PeriodicSpread {
            period: 20,
            losses: 1,
        },
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// Fat pipe with ~5% random loss: the interactive default-on FEC gaming
/// treatment lane (no bottleneck shaping, plenty of capacity headroom).
pub fn fec_gaming_fat_pipe() -> NetemConfig {
    NetemConfig {
        rate: 50 * 1000 * 1000,
        latency: Duration::from_millis(300),
        loss: u32::MAX / 20,
        loss_model: LossModel::Random,
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

/// Paired-saturated bottleneck: FEC-off and FEC-on arms lose the same
/// logical RTP sequence. `key_offset` points at the eight-byte codec
/// sequence immediately after the command byte; the 10-byte FEC data
/// envelope shifts the key on the FEC-on arm.
pub fn fec_paired_saturated_bottleneck(fec_envelope: bool) -> NetemConfig {
    const CODEC_SEQUENCE_OFFSET: u16 = 1;
    const FEC_DATA_ENVELOPE_BYTES: u16 = 10;
    NetemConfig {
        rate: 50 * 1000 * 1000,
        latency: Duration::from_millis(300),
        loss: u32::MAX / 20,
        loss_model: LossModel::PacketKeyed {
            key_offset: CODEC_SEQUENCE_OFFSET
                + if fec_envelope {
                    FEC_DATA_ENVELOPE_BYTES
                } else {
                    0
                },
        },
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

pub fn hostile_periodic_bottleneck() -> NetemConfig {
    hostile_periodic_bottleneck_at(Duration::from_millis(300))
}

fn hostile_periodic_bottleneck_at(latency: Duration) -> NetemConfig {
    NetemConfig {
        rate: 50 * 1000 * 1000,
        latency,
        loss_model: LossModel::Periodic {
            period: 20,
            losses: 3,
        },
        queue_limit_pkts: 1024,
        seed: 4,
        ..NetemConfig::default()
    }
}

pub fn hostile_periodic_bottleneck_20ms() -> NetemConfig {
    hostile_periodic_bottleneck_at(Duration::from_millis(20))
}

pub fn hostile_periodic_bottleneck_100ms() -> NetemConfig {
    hostile_periodic_bottleneck_at(Duration::from_millis(100))
}

pub fn hostile_periodic_bottleneck_300ms() -> NetemConfig {
    hostile_periodic_bottleneck_at(Duration::from_millis(300))
}

/// The default [`NetemConfig::seed`], spelled out so a change to the default
/// cannot silently re-seed a preset that leaves it unset.
#[cfg(test)]
const DEFAULT_SEED: u64 = 0xC0FF_EEBE_EFC0_FFEE;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shaper::serialization_delay;

    /// The battery lanes are the regression baseline for every recorded
    /// verdict, so their impairment parameters are frozen field by field: an
    /// edit that moves one of these shapes fails here instead of quietly
    /// invalidating the historical comparisons it is measured against.
    #[test]
    fn battery_lane_shapes_are_frozen() {
        let clean = clean();
        assert_eq!(clean.rate, 0, "clean lane must stay unshaped");
        assert_eq!(clean.latency, Duration::ZERO);
        assert_eq!(clean.jitter, Duration::ZERO);
        assert_eq!(clean.queue_limit_pkts, 0);
        assert_eq!(clean.loss, 0);
        assert_eq!(clean.seed, DEFAULT_SEED);

        for (name, config) in [
            ("controller-fat-pipe", controller_fat_pipe()),
            (
                "deterministic-iid-loss-fat-pipe",
                deterministic_iid_loss_fat_pipe(),
            ),
        ] {
            assert_eq!(config.rate, 100 * 1000 * 1000, "{name}: rate");
            assert_eq!(
                config.latency,
                Duration::from_millis(150),
                "{name}: latency"
            );
            assert_eq!(config.jitter, Duration::ZERO, "{name}: jitter");
            assert_eq!(config.queue_limit_pkts, 16 * 1024, "{name}: queue limit");
            assert_eq!(config.reorder, 0, "{name}: reorder");
            assert_eq!(config.reorder_gap_pkts, 0, "{name}: reorder gap");
        }

        for (name, config, seed) in [
            ("hostile-fat-pipe", hostile_fat_pipe(), 4u64),
            ("lossy-400kib", lossy_400kib_per_sec(), 4u64),
            ("hostile", hostile_real_link(), 4u64),
        ] {
            assert_eq!(config.seed, seed, "{name}: seed");
        }

        let hostile_fat_pipe = hostile_fat_pipe();
        assert_eq!(hostile_fat_pipe.rate, 100 * 1000 * 1000);
        assert_eq!(hostile_fat_pipe.latency, Duration::from_millis(150));
        assert_eq!(hostile_fat_pipe.jitter, Duration::from_millis(30));
        assert_eq!(hostile_fat_pipe.queue_limit_pkts, 16 * 1024);
        assert_eq!(
            hostile_fat_pipe.loss_model,
            gilbert_elliott_loss(2.0, 4.0),
            "hostile-fat-pipe must keep the 2 % / 4-packet Gilbert-Elliott model"
        );

        let lossy = lossy_400kib_per_sec();
        assert_eq!(lossy.rate, 400 * 1024 * 8);
        assert_eq!(lossy.latency, Duration::from_millis(5));
        assert_eq!(lossy.jitter, Duration::from_millis(2));
        assert_eq!(lossy.loss, u32::MAX / 100);

        let hostile = hostile_real_link();
        assert_eq!(hostile.rate, 0);
        assert_eq!(hostile.latency, Duration::from_millis(300));
        assert_eq!(hostile.jitter, Duration::from_millis(500));
        assert_eq!(hostile.loss, u32::MAX / 100 * 15);
    }

    /// The jitter lane is a controlled contrast against the battery lanes:
    /// the only delay property that moves is the jitter, and it is the only
    /// lane that leaves `rate` unset, because a configured rate's send-time
    /// shaper would monotone the deadlines and realize the jitter as a running
    /// maximum instead of reordering.
    #[test]
    fn jittery_short_rtt_lane_is_unshaped_and_only_the_delay_shape_moves() {
        let lane = jittery_short_rtt_link();
        let controller = controller_fat_pipe();
        assert_eq!(lane.rate, 0, "a configured rate would suppress reordering");
        assert_eq!(lane.loss, controller.loss);
        assert_eq!(lane.loss_model, controller.loss_model);
        assert_eq!(lane.reorder_gap_pkts, controller.reorder_gap_pkts);

        assert_eq!(lane.latency, Duration::from_millis(20));
        assert_eq!(lane.jitter, Duration::from_millis(15));
        assert_ne!(lane.jitter, Duration::ZERO, "the lane must jitter");
        // `sample_delay` spreads uniformly over `latency +/- jitter`, so the
        // sampled one-way delay spans exactly this window.
        assert_eq!(lane.latency - lane.jitter, Duration::from_millis(5));
        assert_eq!(lane.latency + lane.jitter, Duration::from_millis(35));
        assert_eq!(
            lane.queue_limit_pkts, 1024,
            "the lane must stay bounded at the kernel's default queue"
        );
        assert_eq!(lane.seed, 4, "the lane must be seeded explicitly");
    }

    /// The low-rate lane is only meaningful if its link can hold a round trip
    /// long enough to inflate RFC 6298's `srtt + 4 * rttvar` into the tens of
    /// seconds. That is a property of the configured rate and queue, so it is
    /// checked here against the same serialization arithmetic the link uses.
    #[test]
    fn high_rtt_low_rate_lane_reaches_a_tens_of_seconds_round_trip() {
        const BULK_MSS_BYTES: usize = 8192;
        let lane = high_rtt_low_rate_bottleneck();
        assert_eq!(lane.rate, 200 * 1000);
        assert_eq!(lane.latency, Duration::from_millis(400));
        assert_eq!(lane.queue_limit_pkts, 128);
        assert_eq!(
            lane.jitter,
            Duration::ZERO,
            "the regime lane is deterministic"
        );
        assert_eq!(lane.loss, 0);
        assert_eq!(lane.seed, 4);

        let per_datagram =
            serialization_delay(BULK_MSS_BYTES, lane.rate).expect("a configured rate serializes");
        assert_eq!(per_datagram, Duration::from_nanos(327_680_000));
        // One serialization per queued datagram: the c2s shaper spaces the
        // datagrams at exactly that step, so the return path never builds a
        // backlog of its own.
        let reachable = lane.latency * 2 + per_datagram * lane.queue_limit_pkts as u32;
        assert!(
            reachable >= Duration::from_secs(40),
            "a full queue must hold a tens-of-seconds round trip, got {reachable:?}"
        );

        // The contrast is the serialization step, not the queue count: at 100
        // Mbit/s the same datagram serializes 500x faster, so a sender that
        // the link cannot out-run never grows this backlog at all.
        let fat_pipe_step = serialization_delay(BULK_MSS_BYTES, 100 * 1000 * 1000)
            .expect("a configured rate serializes");
        assert_eq!(per_datagram, fat_pipe_step * 500);
    }
}
