// ───────────────────────────── loss models ─────────────────────────────

use crate::rng::{CorRng, RndState};

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
pub(crate) enum FourState {
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
    pub(crate) fn loss(
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
