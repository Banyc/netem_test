//! Shim: the rtp frame-delivery plumbing lives in the owning crate
//! (`rtp::testkit::frame`, behind rtp's `testing` feature); scenarios consume
//! the single authority through this view, which is removed as the rtp
//! scenarios relocate into the crate. The wildcard re-export is consumed by
//! whichever scenario targets import `support::frame::…`; targets that
//! compile the module while using none of its items legitimately leave it
//! unused, so the import is deliberately allowed.
#[allow(unused_imports)]
pub use rtp::testkit::frame::*;
