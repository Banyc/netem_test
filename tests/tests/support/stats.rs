// ─────────────────────────── mux-sink progress machinery ─────────────────
//
// The pure reporting helpers (combined_stats, print_perf, print_median_worst,
// percentile, HolSummary, summarize) live in the harness kit
// (`netem_test::kit::stats`, behind the `test-kit` feature) and are
// re-exported here so existing scenario imports keep resolving. The mux-sink
// session-progress machinery now lives in the mux layer kit
// (`mux::testkit::stats`, behind mux's `testing` feature): it converts
// `mux::MuxError`, which the leaf harness must never depend on, so it moved
// to the owning crate with the mux scenarios. Both sides are shim views of
// their single authority and are removed as the scenarios relocate into their
// owning crates.

#[allow(unused_imports)]
pub use netem_test::kit::stats::{
    HolSummary, combined_stats, percentile, print_median_worst, print_perf, summarize,
};

#[allow(unused_imports)]
pub use mux::testkit::stats::{
    MuxSessionOutcome, MuxSessionProgress, SinkProgress, SinkReadOutcome,
};
