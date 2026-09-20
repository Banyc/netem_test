// ═══════════════════════════════════════════════════════════════════════════════
// SplitMix64 PRNG (tiny, seeded, deterministic)
// ═══════════════════════════════════════════════════════════════════════════════

/// Tiny seeded SplitMix64 PRNG for deterministic traffic models.
#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }

    /// Uniform random usize in `[lo, hi]` (inclusive).
    pub fn uniform_usize(&mut self, lo: usize, hi: usize) -> usize {
        assert!(lo <= hi, "uniform_usize: lo > hi");
        let range = (hi - lo + 1) as u64;
        lo + (self.next_u64() % range) as usize
    }
}
