# The netem_test validation gate

This file is the authoritative scope of the scenario gate. `cargo test -p tests`
silently skips every `#[ignore]`d scenario, so the gate is defined in tiers and
the `gate-manifest` block below names every opt-in scenario and its tier. The
manifest is machine-checked by `python3 tools/check-gate.py`, which fails if a
scenario is added or removed without the manifest being updated, making an
unnoticed `#[ignore]` skip impossible.

Run the checker after adding, removing, or re-tiering any scenario:

```sh
python3 tools/check-gate.py
```

## Tiers

- **default** — not `#[ignore]`d, so a plain `cargo test -p tests` runs it.
  Every scenario here is seeded (deterministic impairment) and finishes in a
  few seconds. This is the gate that runs on every `cargo test`.
- **standard** — `#[ignore]`d, runs in well under a minute per target and
  asserts a correctness property (not just a measurement). Run with
  `cargo test -p tests -- --ignored --test-threads=1`.
- **full** — `#[ignore]`d, minutes per target; still asserts a property, but
  too slow for the default gate. Run the target explicitly.
- **perf** — `#[ignore]`d, report-only measurement or long-run tooling; these
  produce numbers (or feed `tools/perf-loop`), they do not assert a gate floor.
  A `perf` scenario must not contain an assertion in its own body; `check-gate.py`
  fails with the scenario name, its file, and the token if one does, because a
  check that never runs is not coverage. It must also not reach an assertion
  through a helper: the checker derives the crate-local call-graph closure of
  every `perf` scenario and requires every asserting helper it reaches to be
  declared report-only in the `gate-perf-guard-helpers` block. Do not treat a
  perf target's absence from a run as coverage of the property. The three
  report-only `rtp_padding_bench` A/B benches and the `rtp_mux_jitter` arms
  call shared helpers (`run_transfer*`, `assert_sane`, `assert_reportable`)
  whose assertions are setup/sanity guards, not gates; the round-trip integrity
  they check is gated by the default-tier padding tests.

The long-running `perf-loop` battery (`tools/perf-loop`, lanes `clean`,
`controller-fat-pipe`, `hostile`, `lossy-400kib`, `hostile-fat-pipe`) is a
separate, much slower evidence path and is not part of `cargo test`. Its
mandatory rendered-graph evidence is produced by `tools/render_graph.py`; a
graph that cannot be produced is a non-zero-exit error, not an empty file to
skim past (see `tools/PERF_LOOP.md`, "Rendered graph evidence (mandatory)").

## Default tier (runs in `cargo test -p tests`)

`netem_scenarios`, `raw_netem_pair`, `rtp_clean`, `rtp_loss`, `rtp_mss`,
`rtp_fec` (the seeded default-FEC recovery case, which reaches the sender's
in-stream FEC capacity gate), `rtp_and_mux`, and `mux_over_rtp`. Plus the two
`hol_probe` and two `perf_probe` seeding tests, and the single
`rtp_liveness` / `shared_bottleneck` resynchronisation tests that were already
un-ignored. The clean-link mux bulk progress gate
(`mux_bulk_clean_stall::clean_link_mux_bulk_completes_within_timeout`) is also
default: it is seeded, deterministic, and bounds itself with a wall-clock
deadline so a wedged transport cannot hang the suite. The padding bench's
fitted-ACK assertion
(`rtp_padding_bench::ack_padding_hides_ack_packets_among_data`) is default
too: it asserts a correctness property (the fitted ACK cluster is shrunken
against the unpadded baseline while the large-data peak is preserved) and its
24-trial pool keeps the pooled fitted/baseline small-cluster ratio at
0.13-0.28 across the debug default gate and release (bound 0.5, so a >1.7x
margin), so leaving it `#[ignore]`d made the assertion unreachable.
The padding distribution pair
(`rtp_padding_bench::padded_wire_sizes_converge_to_one_peak` and
`rtp_padding_bench::unpadded_wire_sizes_stay_multimodal`) and the three
`mux_over_rtp_perf` scenarios (`mux_over_rtp_lossy_perf_smoke`,
`mux_over_rtp_400kib_lossy_contended_perf`,
`mux_over_rtp_small_stream_while_bulk_perf`) are default too: each asserts a
property (a one-peaked padded wire-size distribution, a preserved multimodal
unpadded baseline, rate-limited forwarding, or small-before-bulk fairness),
passes reliably, and keeps the added default-tier cost near three seconds.

The `gate-default-required` block names the asserting scenarios that must stay in
this tier; `check-gate.py` fails if one is re-`#[ignore]`d or removed. The
`gate-asserting` block records the full report-only/asserting split.

```gate-default-required
mux_over_rtp_perf::mux_over_rtp_400kib_lossy_contended_perf
mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke
mux_over_rtp_perf::mux_over_rtp_small_stream_while_bulk_perf
rtp_padding_bench::ack_padding_hides_ack_packets_among_data
rtp_padding_bench::padded_wire_sizes_converge_to_one_peak
rtp_padding_bench::unpadded_wire_sizes_stay_multimodal
```

## Opt-in manifest

Each line is `target::test_name = tier`. The set must equal the set of
non-`support` tests reported by `cargo test -p tests --test <target> -- --list
--ignored`.

```gate-manifest
contested_latency::contested_capped_clean = full
contested_latency::contested_capped_jitter_loss = perf
contested_latency::contested_hostile = perf
dynamic_contested::dyn_dual_auto_big_first = full
dynamic_contested::dyn_dual_auto_big_first_migrating = full
dynamic_contested::dyn_dual_auto_per_message = full
dynamic_contested::dyn_dual_auto_small_first = full
dynamic_contested::dyn_dual_auto_small_first_migrating = full
dynamic_contested::dyn_dual_hint_static = full
dynamic_contested::dyn_dual_msg_channel = full
dynamic_contested::dyn_dual_msg_channel_ordered = full
dynamic_contested::dyn_game_sync_migrating = full
dynamic_contested::dyn_game_sync_single_mux = full
dynamic_contested::dyn_game_sync_sticky = full
dynamic_contested::dyn_single_mux = full
hol_probe::dual_lane_asym_frame_delivers_and_tears_down = full
hol_probe::hol_cap400_fec_solo = perf
hol_probe::hol_cap400_loss1_split_shared = perf
hol_probe::hol_cap400_shared = full
hol_probe::hol_cap400_shared_frame_delivery_diag = full
hol_probe::hol_cap400_solo = full
hol_probe::hol_hostile_shared = full
hol_probe::hol_hostile_shared_frame_delivery_diag = full
hol_probe::hol_hostile_solo = full
hol_probe::hol_hostile_split = full
hol_probe::hol_paced_bulk_median_p99_regression = full
hol_probe::hol_rtp_mux_fec_default_on_recovery = full
hol_probe::hol_rtt100_clean_shared = full
hol_probe::hol_rtt100_clean_shared_frame_delivery_diag = full
hol_probe::hol_rtt100_clean_solo = full
hol_probe::hol_rtt100_clean_split = full
hol_probe::hol_rtt100_ge1_loss1_shared = full
hol_probe::hol_rtt100_ge1_loss1_solo = full
hol_probe::hol_rtt100_ge1_loss1_split = full
hol_probe::hol_rtt100_ge1_shared_frame_delivery_diag = full
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_frame_diag = full
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_stock_diag = full
hol_probe::hol_rtt100_ge5_shared = full
hol_probe::hol_rtt100_ge5_shared_dual_lane = full
hol_probe::hol_rtt100_ge5_shared_dual_lane_asym_frame_diag = full
hol_probe::hol_rtt100_ge5_shared_dual_lane_frame_delivery = full
hol_probe::hol_rtt100_ge5_shared_frame_delivery = full
hol_probe::hol_rtt100_ge5_solo = full
hol_probe::hol_rtt100_ge5_split = full
hol_probe::hol_rtt100_ge5_two_interactive_frame_delivery = full
hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery = full
hol_probe::hol_rtt100_ge5_v2_shared = full
hol_probe::hol_rtt100_ge5_v2_solo = full
hol_probe::hol_rtt100_ge5_v3_shared = full
hol_probe::hol_rtt100_ge5_v3_solo = full
hol_probe::hol_rtt100_ge5_v3_split = full
hol_probe::hol_rtt40_ge1_loss1_shared = full
hol_probe::hol_rtt40_ge1_loss1_solo = full
hol_probe::hol_rtt40_ge1_loss1_split = full
hol_probe::hol_rtt40_ge1_shared = full
hol_probe::hol_rtt40_ge1_solo = full
hol_probe::hol_rtt40_ge1_split = full
hol_verify4::v4_clean_muxbulk = perf
hol_verify4::v4_clean_rawbulk = perf
hol_verify4::v4_ge5_muxbulk = perf
hol_verify4::v4_ge5_rawbulk = perf
mux_bulk_clean_stall::induced_stall_fires_the_watchdog = full
mux_bulk_clean_stall::slow_live_link_is_backpressure_not_a_stall = full
mux_over_rtp_perf::mux_over_rtp_400mib_hostile_perf = full
mux_stream_fairness::mux_stream_fairness_longrun = full
mux_stream_fairness::mux_stream_fairness_sweep = full
perf_probe::probe_hostile_goodput_30s = full
perf_probe::probe_hostile_message_latency = full
perf_probe::probe_mux_echo_1mib_direct = standard
perf_probe::probe_mux_echo_1mib_mss8k = standard
perf_probe::probe_mux_sink_4mib_direct = standard
perf_probe::probe_mux_sink_4mib_mss8k = standard
perf_probe::probe_rtp_echo_4mib_direct = standard
perf_probe::probe_rtp_echo_4mib_mss8k = standard
rtp_bufferbloat::rtp_bulk_bounded_buffer_goodput_and_queue_bound = standard
rtp_burst_loss::rtp_bulk_goodput_burst_loss_does_not_collapse_vs_random = full
rtp_burst_loss::rtp_sparse_message_tail_latency_under_burst_loss = full
rtp_fec::rtp_max_diversity_fec_covers_single_packet_messages_under_loss = standard
rtp_gentle::gentle_mode_exits_via_gate_open_after_a_standing_queue_drains = standard
rtp_liveness::rtp_fresh_sacks_beyond_permanent_mtu_hole_do_not_keep_connection_alive = standard
rtp_liveness::rtp_permanent_hole_liveness_smoke = standard
rtp_longrun::longrun_duallane = full
rtp_longrun::multiflow_duallane = full
rtp_mux::rtp_mux_bidirectional_contention_offloads_both_transfers = full
rtp_mux::rtp_mux_clean_dual_lane_echoes_interactive_and_bulk_streams = full
rtp_mux::rtp_mux_explorer_relays_onto_better_path = full
rtp_mux::rtp_mux_recycle_migrates_live_streams = full
rtp_mux::rtp_mux_response_migration_offloads_download = full
rtp_mux::rtp_mux_survives_independent_impaired_lanes = full
rtp_mux_jitter::jitter_bulk_idle_restart_arm = perf
rtp_mux_jitter::jitter_burst_loss_arms = perf
rtp_mux_jitter::jitter_cellular_timeline_arms = perf
rtp_mux_jitter::jitter_decomposition = perf
rtp_mux_jitter::jitter_duallane_arms = perf
rtp_mux_jitter::jitter_duallane_constitution_gate = full
rtp_mux_jitter::jitter_duallane_constitution_gate_p99 = full
rtp_mux_jitter::jitter_fec_arms_2pct = perf
rtp_mux_jitter::jitter_fec_arms_6pct = perf
rtp_mux_jitter::jitter_frame_reorder_decomposition = perf
rtp_mux_jitter::jitter_frame_reorder_fec_arms = perf
rtp_mux_jitter::jitter_frame_reorder_fec_bulk_loss_reorder = perf
rtp_mux_jitter::jitter_interactive_bulk_and_loss = perf
rtp_mux_jitter::jitter_interactive_solo = perf
rtp_mux_jitter::jitter_interactive_with_bulk = perf
rtp_mux_jitter::jitter_interactive_with_loss = perf
rtp_mux_jitter::jitter_latency_dimension_arms = perf
rtp_mux_jitter::jitter_nonloss_impairments = perf
rtp_mux_jitter::jitter_reorder_direction = perf
rtp_mux_jitter::jitter_reorder_rate_curve = perf
rtp_mux_jitter::jitter_shared_bottleneck_arms = perf
rtp_padding_bench::ab_bulk_throughput_ack_padding = perf
rtp_padding_bench::ab_bulk_throughput_across_presets = perf
rtp_padding_bench::ab_small_echo_latency = perf
rtp_padding_bench::ab_small_echo_latency_ack_padding = perf
rtp_padding_bench::ab_small_echo_latency_across_presets = perf
rtp_padding_bench::padding_throughput_overhead = perf
shared_bottleneck::shared_bneck_fairness_longrun = full
shared_bottleneck::shared_bneck_fairness_sweep = full
shared_bottleneck::shared_bneck_late_joiner_fairness = full
shared_bottleneck::shared_bneck_reorder_tolerant_fairness = full
shared_bottleneck::shared_bneck_rr_under_bulk_10mbps = full
shared_bottleneck::shared_bneck_rr_under_bulk_2mbps = full
shared_bottleneck::shared_bneck_rr_under_dedicated_bulk_10mbps = full
```

The `gate-asserting` block below records the report-only/asserting split. It
names every scenario that asserts a property (a gate): all `standard` and
`full` scenarios plus the default-tier assertions. The `perf` tier is
report-only by definition, so no `perf` scenario may appear here. The checker
derives the expected set from the manifest tiers plus `gate-default-required`
and fails if this block disagrees, and it also scans each `perf` scenario's own
body: a `perf` scenario containing `assert!`/`assert_eq!`/`assert_ne!`/
`panic!`/`unreachable!` (or the debug-only `debug_assert!`/`debug_assert_eq!`/
`debug_assert_ne!` forms) is an error, named with its file and the token found
(an asserting check filed under the report-only tier would never run).

```gate-asserting
contested_latency::contested_capped_clean
dynamic_contested::dyn_dual_auto_big_first
dynamic_contested::dyn_dual_auto_big_first_migrating
dynamic_contested::dyn_dual_auto_per_message
dynamic_contested::dyn_dual_auto_small_first
dynamic_contested::dyn_dual_auto_small_first_migrating
dynamic_contested::dyn_dual_hint_static
dynamic_contested::dyn_dual_msg_channel
dynamic_contested::dyn_dual_msg_channel_ordered
dynamic_contested::dyn_game_sync_migrating
dynamic_contested::dyn_game_sync_single_mux
dynamic_contested::dyn_game_sync_sticky
dynamic_contested::dyn_single_mux
hol_probe::dual_lane_asym_frame_delivers_and_tears_down
hol_probe::hol_cap400_shared
hol_probe::hol_cap400_shared_frame_delivery_diag
hol_probe::hol_cap400_solo
hol_probe::hol_hostile_shared
hol_probe::hol_hostile_shared_frame_delivery_diag
hol_probe::hol_hostile_solo
hol_probe::hol_hostile_split
hol_probe::hol_paced_bulk_median_p99_regression
hol_probe::hol_rtp_mux_fec_default_on_recovery
hol_probe::hol_rtt100_clean_shared
hol_probe::hol_rtt100_clean_shared_frame_delivery_diag
hol_probe::hol_rtt100_clean_solo
hol_probe::hol_rtt100_clean_split
hol_probe::hol_rtt100_ge1_loss1_shared
hol_probe::hol_rtt100_ge1_loss1_solo
hol_probe::hol_rtt100_ge1_loss1_split
hol_probe::hol_rtt100_ge1_shared_frame_delivery_diag
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_frame_diag
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_stock_diag
hol_probe::hol_rtt100_ge5_shared
hol_probe::hol_rtt100_ge5_shared_dual_lane
hol_probe::hol_rtt100_ge5_shared_dual_lane_asym_frame_diag
hol_probe::hol_rtt100_ge5_shared_dual_lane_frame_delivery
hol_probe::hol_rtt100_ge5_shared_frame_delivery
hol_probe::hol_rtt100_ge5_solo
hol_probe::hol_rtt100_ge5_split
hol_probe::hol_rtt100_ge5_two_interactive_frame_delivery
hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery
hol_probe::hol_rtt100_ge5_v2_shared
hol_probe::hol_rtt100_ge5_v2_solo
hol_probe::hol_rtt100_ge5_v3_shared
hol_probe::hol_rtt100_ge5_v3_solo
hol_probe::hol_rtt100_ge5_v3_split
hol_probe::hol_rtt40_ge1_loss1_shared
hol_probe::hol_rtt40_ge1_loss1_solo
hol_probe::hol_rtt40_ge1_loss1_split
hol_probe::hol_rtt40_ge1_shared
hol_probe::hol_rtt40_ge1_solo
hol_probe::hol_rtt40_ge1_split
mux_bulk_clean_stall::induced_stall_fires_the_watchdog
mux_bulk_clean_stall::slow_live_link_is_backpressure_not_a_stall
mux_over_rtp_perf::mux_over_rtp_400kib_lossy_contended_perf
mux_over_rtp_perf::mux_over_rtp_400mib_hostile_perf
mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke
mux_over_rtp_perf::mux_over_rtp_small_stream_while_bulk_perf
mux_stream_fairness::mux_stream_fairness_longrun
mux_stream_fairness::mux_stream_fairness_sweep
perf_probe::probe_hostile_goodput_30s
perf_probe::probe_hostile_message_latency
perf_probe::probe_mux_echo_1mib_direct
perf_probe::probe_mux_echo_1mib_mss8k
perf_probe::probe_mux_sink_4mib_direct
perf_probe::probe_mux_sink_4mib_mss8k
perf_probe::probe_rtp_echo_4mib_direct
perf_probe::probe_rtp_echo_4mib_mss8k
rtp_bufferbloat::rtp_bulk_bounded_buffer_goodput_and_queue_bound
rtp_burst_loss::rtp_bulk_goodput_burst_loss_does_not_collapse_vs_random
rtp_burst_loss::rtp_sparse_message_tail_latency_under_burst_loss
rtp_fec::rtp_max_diversity_fec_covers_single_packet_messages_under_loss
rtp_gentle::gentle_mode_exits_via_gate_open_after_a_standing_queue_drains
rtp_liveness::rtp_fresh_sacks_beyond_permanent_mtu_hole_do_not_keep_connection_alive
rtp_liveness::rtp_permanent_hole_liveness_smoke
rtp_longrun::longrun_duallane
rtp_longrun::multiflow_duallane
rtp_mux::rtp_mux_bidirectional_contention_offloads_both_transfers
rtp_mux::rtp_mux_clean_dual_lane_echoes_interactive_and_bulk_streams
rtp_mux::rtp_mux_explorer_relays_onto_better_path
rtp_mux::rtp_mux_recycle_migrates_live_streams
rtp_mux::rtp_mux_response_migration_offloads_download
rtp_mux::rtp_mux_survives_independent_impaired_lanes
rtp_mux_jitter::jitter_duallane_constitution_gate
rtp_mux_jitter::jitter_duallane_constitution_gate_p99
rtp_padding_bench::ack_padding_hides_ack_packets_among_data
rtp_padding_bench::padded_wire_sizes_converge_to_one_peak
rtp_padding_bench::unpadded_wire_sizes_stay_multimodal
shared_bottleneck::shared_bneck_fairness_longrun
shared_bottleneck::shared_bneck_fairness_sweep
shared_bottleneck::shared_bneck_late_joiner_fairness
shared_bottleneck::shared_bneck_reorder_tolerant_fairness
shared_bottleneck::shared_bneck_rr_under_bulk_10mbps
shared_bottleneck::shared_bneck_rr_under_bulk_2mbps
shared_bottleneck::shared_bneck_rr_under_dedicated_bulk_10mbps
```

The direct-body scan only sees assertions in a `perf` scenario's own body, so
it would miss an assertion moved one call away into a helper. The
`gate-perf-guard-helpers` block below closes that hole as far as a regex-level
tool can. For every `perf` scenario the checker builds a crate-local call graph
(functions in `tests/<target>.rs` and the `support/**` modules it includes; a
call is resolved against the caller file's `use` declarations first, then the
caller's own module, then a bare-name fallback, so `support::stats::summarize`
and `support::contested::summarize` are not confused) and takes the transitive
closure. A call that still cannot be narrowed keeps every same-named candidate,
so the graph over-approximates rather than dropping a direct call. Every
asserting function the closure reaches must be listed here as
`RELATIVE_PATH::fn = assertion-token-count`, together with the number of
`assert!`/`assert_eq!`/`assert_ne!`/`panic!`/`unreachable!` tokens in its body
(the debug-only `debug_assert!`/`debug_assert_eq!`/`debug_assert_ne!` forms
count as assertion tokens too).
The checker fails when a reachable asserting helper is unrecorded, when a
recorded helper's token count changes, when a recorded helper is no longer
reachable, or when a `perf` scenario's own body cannot be located in its source
file (e.g. it is macro-generated, so neither scan can see it).

Every entry is a report-only harness guard, not a gate: finalize/setup helpers
that abort on harness malfunction (`with_timeout`, `submit_test_task`,
`submit_test_task_required`, `spawn_required`, the `spawn_*_server_core`
helpers, `try_send_observation`), argument validation (`percentile`,
`gilbert_elliott_loss`), the `rtp_mux_jitter` report-only
`assert_sane`/`assert_reportable` liveness floors, and the `rtp_padding_bench`
A/B `run_transfer`/`run_transfer_preset` setup guards. The round-trip
properties they touch are gated by the default-tier padding tests named in
`gate-default-required`.

Residual limitations, stated so they are not mistaken for coverage. The graph
is name-based and over-approximates whenever a call cannot be narrowed, so a
new helper whose name collides with a method name can be flagged for review. It
cannot see an edge created by passing a function by name (e.g.
`let check: fn(..) = $gates`), through a trait object, or generated by a macro;
an assertion reached only through such an indirection is invisible to the scan.
It records a guard helper's assertion *token count*, not the text of the
assertions, so replacing an assertion in a recorded guard with a different
assertion of the same token count is not detected. The plain tokens match
inside the debug-only forms as substrings, so `debug_assert!` was never fully
invisible to the scan, but the set now names the three debug forms explicitly
(`debug_assert!`/`debug_assert_eq!`/`debug_assert_ne!`) and the error messages
report the exact token found instead of a bare count. A debug-only assertion
is inert in a release build of the scenario that carries it, so under
`--release` it would not even execute; the gate treats its presence in the
report-only tier as a violation regardless of build profile. The residual
indirection limits from the previous paragraph still apply: an assertion
reached only by passing a function by name, through a trait object, or
through a macro alias remains invisible to the graph.

```gate-perf-guard-helpers
tests/rtp_mux_jitter.rs::assert_reportable = 2
tests/rtp_mux_jitter.rs::assert_sane = 2
tests/rtp_padding_bench.rs::run_transfer = 1
tests/rtp_padding_bench.rs::run_transfer_preset = 1
tests/support/dual.rs::dual_mux_client_connect_lane_rtp_via = 1
tests/support/mod.rs::try_send_observation = 1
tests/support/mux.rs::mux_client_connect_core = 1
tests/support/mux.rs::mux_client_connect_frame_delivery_via = 1
tests/support/mux.rs::send_timestamped_messages = 1
tests/support/mux.rs::spawn_mux_frame_delivery_latency_bulk_server_core = 1
tests/support/mux.rs::spawn_mux_over_rtp_server_core = 1
tests/support/payload.rs::with_timeout = 1
tests/support/presets.rs::gilbert_elliott_loss = 2
tests/support/rtp.rs::spawn_rtp_byte_sink_server_core = 1
tests/support/stats.rs::percentile = 1
tests/support/task_scope.rs::run = 1
tests/support/task_scope.rs::spawn_required = 1
tests/support/task_scope.rs::submit_test_task = 2
tests/support/task_scope.rs::submit_test_task_required = 1
```

## Perf-loop lane roles

`tools/perf-loop` runs each paired capture on a `--link-profile` lane. Every
lane is either a **verdict** lane (a change may be retained or rejected on its
paired evidence) or **diagnostic** (the lane reports its numbers but a change
must never be retained or rejected on it). The `hostile` lane is
**diagnostic-only**: in every one of the 70 recorded `hostile` runs — at both
the 5 s and the 20 s warmup, with a byte-identical baseline/candidate control
tree — the lane was `not_ready` (`within_run_phase_not_stable`), and the spread
was the lane's own stochastic first/second-half goodput phase variance, not a
candidate effect. Its numbers stay useful as a diagnostic, but it cannot
attribute a delta to a candidate, so it must never be read as a verdict.

The `gate-lane-roles` block below records every lane's role. It is
machine-checked by `tools/check-gate.py` against `perf_loop.lane_classification`
— the same function that stamps `link_role` into `run.json` — so a lane cannot
be declared verdict in one place and diagnostic in the other. Run the checker
after adding a lane or changing a role:

```sh
python3 tools/check-gate.py
```

```gate-lane-roles
hostile = diagnostic
hostile-steady = verdict
hostile-steady-bottleneck = verdict
hostile-steady-bottleneck-20ms = verdict
hostile-steady-bottleneck-100ms = verdict
hostile-periodic-bottleneck = verdict
lossy-400kib = verdict
hostile-fat-pipe = verdict
controller-fat-pipe = verdict
deterministic-iid-loss-fat-pipe = verdict
clean = verdict
direct = verdict
hostile-bottleneck-20ms = verdict
hostile-bottleneck-100ms = verdict
hostile-bottleneck-300ms = verdict
fec-recoverable-bottleneck = verdict
fec-gaming-fat-pipe = verdict
fec-paired-saturated = verdict
fec-paired-saturated-bottleneck = verdict
hostile-periodic-bottleneck-20ms = verdict
hostile-periodic-bottleneck-100ms = verdict
hostile-periodic-bottleneck-300ms = verdict
```

## Opt-in targets outside this manifest

`check-gate.py` covers only the `tests` package. Three other opt-in sets are
never run by `cargo test` and are listed here so their skip is explicit:

- **`netem-test` harness probes** (`cargo test --release -p netem-test --
  --ignored`): `tests::clean_forwarding_perf_probe`,
  `tests::learned_destination_cache_perf_probe`,
  `tests::short_deadline_latency_perf_probe`,
  `tests::std_udp_connected_peer_perf_probe`. Report-only wall-clock probes.
- **`rtp` in-process oracles and perf lanes** (`cargo test --release --lib --
  --ignored` from `crates/rtp`): `socket::stream::tests::probe_single_symbol_
  interactive_fec_repair`, `socket::stream::tests::probe_fresh_tail_armor_
  latency`, `socket::stream::tests::probe_fresh_tail_burst_loss_latency`,
  `socket::stream::tests::probe_armor_copy_cell`, plus the four
  `traffic_shaping`/`recv_queue` perf lanes. Report-only except the perf
  lanes, which assert sub-linear scaling ratios.
- **`mux` nightly bench** (`mux` with `--features nightly`):
  `bench::profile_mux_send` is an infinite profiling loop and is never run to
  completion by any gate.

A target that is never run is an unstated gap: run these explicitly when the
property they cover is in scope, and report the numbers, not just a pass.
