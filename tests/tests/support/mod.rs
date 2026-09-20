//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario in this workspace goes through the
//! bidirectional [`NetemPair`] harness; these helpers centralise the
//! reusable plumbing (impairment presets, deterministic payloads, timeout
//! guards, and `rtp` / `mux` echo servers) so each focused test target stays
//! small and readable.
//!
//! The generic helpers (payload, presets, stats reporting, prng, task_scope,
//! contested, fan, and the module core) now live in the harness kit behind
//! the `test-kit` feature; this module is a thin re-export view of that
//! single authority and is removed as the scenarios relocate into their
//! owning crates.

#![allow(dead_code)]

pub(crate) mod dual;
pub(crate) mod fan;
pub(crate) mod frame;
pub(crate) mod mux;
pub(crate) mod payload;
pub(crate) mod perf_trace;
pub(crate) mod presets;
pub(crate) mod prng;
pub(crate) mod rtp;
pub(crate) mod rtp_mux;
pub(crate) mod stats;
pub(crate) mod task_scope;

// Re-exported for the scenario files; targets that spawn no background tasks
// (e.g. `netem_scenarios`) do not reference them.
#[allow(unused_imports)]
pub(crate) use netem_test::kit::{
    LANE_EVENT_CAPACITY, LATENCY_SAMPLE_CAPACITY, TEST_ACCEPT_CAPACITY, TEST_TASK_QUEUE_BOUND,
    TestScope, TestTask, TestTaskSubmitter, abort_and_reap_test_tasks,
    spawn_test_task_reaper_with_shutdown, submit_test_task, submit_test_task_required,
    try_send_observation,
};

pub mod contested;
