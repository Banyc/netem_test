// ───────────────────────────── seeded RNG ──────────────────────────────

/// Kernel `struct rnd_state` – four 32-bit Tausworthe LFSR lanes.
#[derive(Clone, Copy, Debug)]
pub struct RndState {
    pub(crate) s1: u32,
    pub(crate) s2: u32,
    pub(crate) s3: u32,
    pub(crate) s4: u32,
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
        fn tausworthe(s: &mut u32, a: u32, b: u32, c: u32, d: u32) {
            *s = ((*s & c) << d) ^ (((*s << a) ^ *s) >> b);
        }
        tausworthe(&mut self.s1, 6, 13, 4_294_967_294, 18);
        tausworthe(&mut self.s2, 2, 27, 4_294_967_288, 2);
        tausworthe(&mut self.s3, 13, 21, 4_294_967_280, 7);
        tausworthe(&mut self.s4, 3, 12, 4_294_967_168, 13);
        self.s1 ^ self.s2 ^ self.s3 ^ self.s4
    }
}

/// Correlated random source – `struct crndstate` in `sch_netem.c`.
#[derive(Clone, Copy, Debug)]
pub(crate) struct CorRng {
    last: u32,
    rho: u32,
}

impl CorRng {
    pub(crate) const fn new(rho: u32) -> Self {
        Self { last: 0, rho }
    }

    /// `get_crandom`: next value depends on last; `rho` is scaled to avoid
    /// floating point.
    pub(crate) fn next(&mut self, rng: &mut RndState) -> u32 {
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
