//! Shared integration-test helpers rebuilt on top of [`netem_test::NetemPair`].
//!
//! Every cross-crate network scenario goes through the bidirectional
//! [`NetemPair`] harness; these helpers centralise the reusable plumbing
//! (impairment presets, deterministic payloads, timeout guards) so each
//! focused test target stays small and readable.
//!
//! The generic helpers (payload, presets, stats reporting, prng, task_scope,
//! and the module core) live in the harness kit behind the `test-kit`
//! feature; this module is a thin re-export view of that single authority.
//! The application scenarios that consume `rtp`, `mux` or `rtp_mux` live in
//! those crates' own test targets, so this workspace depends on none of them.

#![allow(dead_code)]

pub(crate) mod payload;
pub(crate) mod presets;
pub(crate) mod prng;
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
