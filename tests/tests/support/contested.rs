//! Shim: the contested helpers live in the harness kit (`netem_test::kit::contested`,
//! behind the `test-kit` feature); scenarios consume the single authority
//! through this view, which is removed as the scenarios relocate into their
//! owning crates. The wildcard re-export is consumed by whichever scenario
//! targets import `support::contested::…`; targets that compile the module while
//! using none of its items legitimately leave it unused, so the import is
//! deliberately allowed.
#[allow(unused_imports)]
pub use netem_test::kit::contested::*;
