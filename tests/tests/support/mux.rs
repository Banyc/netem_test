//! Shim: the mux-over-rtp scaffolding (echo/connect/sink servers, frame
//! delivery, counting sinks, transient connects, probe floors) lives in the
//! owning crate (`mux::testkit::mux`, behind mux's `testing` feature);
//! scenarios consume the single authority through this view, which is removed
//! as the mux scenarios relocate into the crate. The wildcard re-export is
//! consumed by whichever scenario targets import `support::mux::…`; targets
//! that compile the module while using none of its items legitimately leave
//! it unused, so the import is deliberately allowed.
#[allow(unused_imports)]
pub use mux::testkit::mux::*;
