//! Shim: the rtp metrics perf-trace builder lives in the owning crate
//! (`rtp::testkit::perf_trace`, behind rtp's `testing` feature); scenarios
//! consume the single authority through this view, which is removed as the
//! rtp scenarios relocate into the crate. The wildcard re-export is consumed
//! by whichever scenario targets import `support::perf_trace::…`; targets
//! that compile the module while using none of its items legitimately leave
//! it unused, so the import is deliberately allowed.
#[allow(unused_imports)]
pub use rtp::testkit::perf_trace::*;
