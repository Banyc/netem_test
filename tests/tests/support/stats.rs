//! Shim: the reporting helpers live in the harness kit
//! (`netem_test::kit::stats`, behind the `test-kit` feature) and are
//! re-exported here so the harness's own conformance targets keep resolving
//! them. The mux-sink session-progress machinery lives with the mux layer kit
//! in the owning crate, so the leaf harness never depends on `mux`.

#[allow(unused_imports)]
pub use netem_test::kit::stats::{
    HolSummary, combined_stats, percentile, print_median_worst, print_perf, summarize,
};
