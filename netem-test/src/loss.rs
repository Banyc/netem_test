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
}

impl LossModel {
    /// Decide whether a packet is lost. Faithfully reproduces
    /// `loss_4state` and the `CLG_RANDOM` branch of `loss_event` in
    /// `sch_netem.c`.
    #[inline(always)]
    pub(crate) fn loss(
        &self,
        state: &mut FourStateState,
        loss_cor: &mut CorRng,
        rng: &mut RndState,
        loss: u32,
    ) -> bool {
        match self {
            LossModel::Random => loss != 0 && loss >= loss_cor.next(rng),
            LossModel::FourState(p) => four_state_loss(p, state, rng),
        }
    }
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
