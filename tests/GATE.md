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
  Do not treat a perf target's absence from a run as coverage of the property.

The long-running `perf-loop` battery (`tools/perf-loop`, lanes `clean`,
`controller-fat-pipe`, `hostile`, `lossy-400kib`, `hostile-fat-pipe`) is a
separate, much slower evidence path and is not part of `cargo test`.

## Default tier (runs in `cargo test -p tests`)

`netem_scenarios`, `raw_netem_pair`, `rtp_clean`, `rtp_loss`, `rtp_mss`,
`rtp_fec` (the seeded default-FEC recovery case, which reaches the sender's
in-stream FEC capacity gate), `rtp_and_mux`, and `mux_over_rtp`. Plus the two
`hol_probe` and two `perf_probe` seeding tests, and the single
`rtp_liveness` / `shared_bottleneck` resynchronisation tests that were already
un-ignored. The clean-link mux bulk progress gate
(`mux_bulk_clean_stall::clean_link_mux_bulk_completes_within_timeout`) is also
default: it is seeded, deterministic, and bounds itself with a wall-clock
deadline so a wedged transport cannot hang the suite.

## Opt-in manifest

Each line is `target::test_name = tier`. The set must equal the set of
non-`support` tests reported by `cargo test -p tests --test <target> -- --list
--ignored`.

```gate-manifest
contested_latency::contested_capped_clean = full
contested_latency::contested_capped_jitter_loss = full
contested_latency::contested_hostile = full
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
hol_probe::hol_cap400_fec_solo = full
hol_probe::hol_cap400_loss1_split_shared = full
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
mux_over_rtp_perf::mux_over_rtp_400kib_lossy_contended_perf = perf
mux_over_rtp_perf::mux_over_rtp_400mib_hostile_perf = perf
mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke = perf
mux_over_rtp_perf::mux_over_rtp_small_stream_while_bulk_perf = perf
mux_stream_fairness::mux_stream_fairness_longrun = full
mux_stream_fairness::mux_stream_fairness_sweep = full
perf_probe::probe_hostile_goodput_30s = perf
perf_probe::probe_hostile_message_latency = perf
perf_probe::probe_mux_echo_1mib_direct = perf
perf_probe::probe_mux_echo_1mib_mss8k = perf
perf_probe::probe_mux_sink_4mib_direct = perf
perf_probe::probe_mux_sink_4mib_mss8k = perf
perf_probe::probe_rtp_echo_4mib_direct = perf
perf_probe::probe_rtp_echo_4mib_mss8k = perf
rtp_bufferbloat::rtp_bulk_bounded_buffer_goodput_and_queue_bound = standard
rtp_burst_loss::rtp_bulk_goodput_burst_loss_does_not_collapse_vs_random = full
rtp_burst_loss::rtp_sparse_message_tail_latency_under_burst_loss = full
rtp_fec::rtp_max_diversity_fec_covers_single_packet_messages_under_loss = standard
rtp_gentle::gentle_mode_exits_via_gate_open_after_a_standing_queue_drains = standard
rtp_liveness::rtp_fresh_sacks_beyond_permanent_mtu_hole_do_not_keep_connection_alive = standard
rtp_liveness::rtp_permanent_hole_liveness_smoke = standard
rtp_longrun::longrun_duallane = perf
rtp_longrun::multiflow_duallane = perf
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
rtp_padding_bench::ack_padding_hides_ack_packets_among_data = perf
rtp_padding_bench::padded_wire_sizes_converge_to_one_peak = perf
rtp_padding_bench::padding_throughput_overhead = perf
rtp_padding_bench::unpadded_wire_sizes_stay_multimodal = perf
shared_bottleneck::shared_bneck_fairness_longrun = full
shared_bottleneck::shared_bneck_fairness_sweep = full
shared_bottleneck::shared_bneck_late_joiner_fairness = full
shared_bottleneck::shared_bneck_reorder_tolerant_fairness = full
shared_bottleneck::shared_bneck_rr_under_bulk_10mbps = full
shared_bottleneck::shared_bneck_rr_under_bulk_2mbps = full
shared_bottleneck::shared_bneck_rr_under_dedicated_bulk_10mbps = full
```
