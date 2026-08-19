// ───────────────────────────── loss models ─────────────────────────────

use crate::rng::{CorRng, RndState};

/// Probability parameters for the four-state Gilbert-Elliot-style loss model
/// used by `sch_netem` (the "GI model"). All probabilities are in `u32`
/// units where `u32::MAX == 1.0` to match the kernel's `p13`/`p31`/…
/// representation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default, serde::Serialize, serde::Deserialize)]
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
pub(crate) enum FourStateState {
    #[default]
    TxInGap = 1,
    TxInBurst = 2,
    LostInGap = 3,
    LostInBurst = 4,
}

/// Which loss model to apply.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum LossModel {
    /// Independent per-packet loss with correlation, `loss` field of
    /// [`NetemConfig`] is the threshold.
    #[default]
    Random,
    /// Four-state Markov chain ([`FourStateLoss`]).
    FourState(FourStateLoss),
    /// Deterministic schedule: drop a contiguous prefix of `losses` packets
    /// at the start of every `period`-packet window, advancing a wrapping
    /// per-direction packet index initialized from [`NetemConfig::seed`].
    Periodic { period: u32, losses: u32 },
    /// Deterministic schedule: distribute `losses` drops evenly across every
    /// `period`-packet window, again advancing the wrapping packet index.
    PeriodicSpread { period: u32, losses: u32 },
    /// Drop a packet identity (an eight-byte big-endian key plus the command
    /// byte immediately before it) only on its first sighting, so paired
    /// FEC-off / FEC-on protocol treatments lose the same logical RTP
    /// sequence despite the FEC data envelope. Retransmissions and short
    /// packets are forwarded; remembered identities are bounded.
    PacketKeyed { key_offset: u16 },
}

use std::collections::{HashSet, VecDeque};

/// Upper bound on remembered packet identities in [`PacketKeyedLossState`].
const PACKET_KEYED_HISTORY_CAPACITY: usize = 65_536;

/// Remembers which packet identities have already been dropped, so a
/// retransmission of a selected first transmission is always forwarded.
#[derive(Debug, Default)]
pub(crate) struct PacketKeyedLossState {
    dropped: HashSet<u64>,
    order: VecDeque<u64>,
}

impl PacketKeyedLossState {
    /// Record `key` as dropped on its first sighting, returning whether this
    /// sighting is the first one (and should therefore be lost). Retains at
    /// most [`PACKET_KEYED_HISTORY_CAPACITY`] identities.
    fn drop_first(&mut self, key: u64) -> bool {
        if !self.dropped.insert(key) {
            return false;
        }
        self.order.push_back(key);
        if self.order.len() > PACKET_KEYED_HISTORY_CAPACITY {
            let oldest = self.order.pop_front().unwrap();
            self.dropped.remove(&oldest);
        }
        true
    }
}

impl LossModel {
    /// Decide whether a packet is lost. Faithfully reproduces
    /// `loss_4state` and the `CLG_RANDOM` branch of `loss_event` in
    /// `sch_netem.c` for the random and four-state models, and implements
    /// the deterministic periodic and packet-identity-keyed schedules.
    #[inline(always)]
    pub(crate) fn loss(
        &self,
        state: &mut FourStateState,
        loss_cor: &mut CorRng,
        rng: &mut RndState,
        loss: u32,
        packet_index: &mut u64,
        packet: &[u8],
        packet_keyed_state: &mut PacketKeyedLossState,
    ) -> bool {
        match self {
            LossModel::Random => loss != 0 && loss >= loss_cor.next(rng),
            LossModel::FourState(p) => four_state_loss(p, state, rng),
            LossModel::Periodic { period, losses } => {
                if *period == 0 {
                    return false;
                }
                let slot = (*packet_index % u64::from(*period)) as u32;
                *packet_index = packet_index.wrapping_add(1);
                slot < (*losses).min(*period)
            }
            LossModel::PeriodicSpread { period, losses } => {
                if *period == 0 {
                    return false;
                }
                let slot = *packet_index % u64::from(*period);
                *packet_index = packet_index.wrapping_add(1);
                let losses = u64::from((*losses).min(*period));
                let period = u64::from(*period);
                (slot + 1) * losses / period > slot * losses / period
            }
            LossModel::PacketKeyed { key_offset } => {
                let Some(key) =
                    packet_keyed_loss_key(packet, usize::from(*key_offset), *packet_index, loss)
                else {
                    return false;
                };
                packet_keyed_state.drop_first(key)
            }
        }
    }
}

/// Extract and hash the packet identity for [`LossModel::PacketKeyed`]: the
/// eight-byte big-endian key at `key_offset` mixed with the command byte
/// immediately before it, salted with the per-direction seed so decisions
/// stay reproducible. Returns `None` when no loss is configured or the
/// packet is too short to carry a key; a packet is selected only when the
/// loss threshold covers the mixed value.
#[inline(always)]
fn packet_keyed_loss_key(packet: &[u8], key_offset: usize, seed: u64, loss: u32) -> Option<u64> {
    if loss == 0 {
        return None;
    }
    let Some(key) = packet.get(key_offset..key_offset.saturating_add(8)) else {
        return None;
    };
    let key = u64::from_be_bytes(key.try_into().unwrap());
    let command = u64::from(packet[key_offset.saturating_sub(1)]);
    let identity = key ^ command.rotate_left(56);
    let mut mixed = identity ^ seed.wrapping_add(0x9e37_79b9_7f4a_7c15);
    mixed = (mixed ^ (mixed >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    mixed = (mixed ^ (mixed >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    mixed = mixed ^ (mixed >> 31);
    (loss >= (mixed >> 32) as u32).then_some(identity)
}

/// Four-state Gilbert-Elliot loss decision. Extracted from the inline
/// [`LossModel::loss`] arm so the hot path stays small; draws exactly one
/// `u32` per packet from `rng` with transitions and return values identical
/// to the arm it replaced.
#[inline(never)]
fn four_state_loss(p: &FourStateLoss, state: &mut FourStateState, rng: &mut RndState) -> bool {
    let rnd = rng.next_u32();
    match state {
        FourStateState::TxInGap => {
            if rnd < p.p14 {
                *state = FourStateState::LostInGap;
                return true;
            } else if rnd < p.p13.saturating_add(p.p14) {
                *state = FourStateState::LostInBurst;
                return true;
            }
        }
        FourStateState::TxInBurst => {
            if rnd < p.p23 {
                *state = FourStateState::LostInBurst;
                return true;
            }
        }
        FourStateState::LostInBurst => {
            if rnd < p.p32 {
                *state = FourStateState::TxInBurst;
            } else if rnd < p.p31.saturating_add(p.p32) {
                *state = FourStateState::TxInGap;
            } else {
                *state = FourStateState::LostInBurst;
                return true;
            }
        }
        FourStateState::LostInGap => {
            *state = FourStateState::TxInGap;
        }
    }
    false
}
