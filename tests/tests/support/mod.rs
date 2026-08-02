//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.

#![allow(dead_code)]

pub(crate) mod dual;
pub(crate) mod fan;
pub(crate) mod frame;
pub(crate) mod mux;
pub(crate) mod payload;
pub(crate) mod presets;
pub(crate) mod prng;
pub(crate) mod rtp;
pub(crate) mod rtp_mux;
pub(crate) mod stats;

pub mod contested;
