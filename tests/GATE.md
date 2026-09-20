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
  perf target's absence from a run as coverage of the property. The`contested_latency`/`hol_verify4` report-only arms call shared helpers whose
  assertions are setup/sanity guards, not gates.

The long-running `perf-loop` battery (`tools/perf-loop`, lanes `clean`,
`controller-fat-pipe`, `hostile`, `lossy-400kib`, `hostile-fat-pipe`) is a
separate, much slower evidence path and is not part of `cargo test`. Its
mandatory rendered-graph evidence is produced by `tools/render_graph.py`; a
graph that cannot be produced is a non-zero-exit error, not an empty file to
skim past (see `tools/PERF_LOOP.md`, "Rendered graph evidence (mandatory)").

## Default tier (runs in `cargo test -p tests`)

`netem_scenarios` and `raw_netem_pair` are the instrument's own conformance
suite (every impairment knob fires, the four-state loss model matches the
`sch_netem` semantics, the pair echoes and reports), plus the two `perf_probe`
seeding tests (fixed-shaping / deterministic-loss profiles assert their
classification, not a wall-clock) and the un-ignored `shared_bottleneck`
resynchronisation tests. The rtp-owned floor tests that used to live here
(`rtp_clean`, `rtp_loss`, `rtp_mss`, the default-FEC recovery case, the
padding distribution/ACK-hiding trio, the `rtp_liveness` unit test) moved to
the owning crate with the relocation's step 5: they now run in
`cargo test -p rtp` and are pinned in `rtp/GATE.md` (`gate-default-required`),
checked with `python3 ../netem_test/tools/check-gate.py --crate . rtp tests
GATE.md`.

The mux-owned scenarios (the clean/latency `mux_over_rtp` echoes,
`mux_over_rtp_perf`'s lossy smoke / contended transfer / small-before-bulk
fairness, the reassigned `rtp_and_mux` smoke trio, and the
`mux_bulk_clean_stall` progress + teardown gates) moved to the owning crate
with the mux layer kit: they now run in `cargo test -p mux` and are recorded
in `mux/GATE.md` (checked with `python3 ../netem_test/tools/check-gate.py
--crate . mux tests GATE.md`).

The rtp_mux-owned scenarios (the `dynamic_contested` battery, the `hol_probe`
head-of-line battery, `rtp_longrun`, the `rtp_mux` explorer/migration suite,
the `rtp_mux_jitter` oracle with its two constitution gates, and the new
`dual_lane_mandates` bulk-lane constitution gate) moved to the owning crate
with the rtp_mux layer kit (`support/{dual,rtp_mux}.rs` →
`rtp_mux/src/testkit/`): they now run in `cargo test -p rtp_mux` and are
recorded in `rtp_mux/GATE.md`, which states the tri-mandate constitution
(one authority per mandate). The two `hol_probe` seeding tests and the
`hol_rtt100_ge5_four_interactive_frame_delivery` scaling gate moved with the
rest of the target.

The `gate-default-required` block names the asserting scenarios that must stay in
this tier; `check-gate.py` fails if one is re-`#[ignore]`d or removed. The
`gate-asserting` block records the full report-only/asserting split.

```gate-default-required
netem_scenarios::netem_delay_adds_latency
netem_scenarios::netem_duplicate_produces_extra_packets
netem_scenarios::netem_four_state_loss_drops_some
netem_scenarios::netem_passes_traffic_unimpaired
netem_scenarios::netem_rate_limit_throttles_burst
netem_scenarios::netem_reorder_with_rate_jumps_ahead
netem_scenarios::netem_drops_all_with_max_random_loss
netem_scenarios::netem_snapshot_reports_queue_and_stats
perf_probe::controller_fat_pipe_has_only_fixed_shaping
perf_probe::deterministic_iid_loss_fat_pipe_is_fixed_seeded_iid_loss
raw_netem_pair::netem_pair_raw_udp_echo_clean_link
raw_netem_pair::netem_pair_raw_udp_latency_is_observable
shared_bottleneck::absolute_starvation_floor_fires_on_a_jain_perfect_collapse
shared_bottleneck::a_slow_reply_resynchronizes_instead_of_ending_the_phase
```

## Opt-in manifest

Each line is `target::test_name = tier`. The set must equal the set of
non-`support` tests reported by `cargo test -p tests --test <target> -- --list
--ignored`.

```gate-manifest
contested_latency::contested_capped_clean = full
contested_latency::contested_capped_jitter_loss = perf
contested_latency::contested_hostile = perf
hol_verify4::v4_clean_rawbulk = perf
hol_verify4::v4_ge5_rawbulk = perf
perf_probe::probe_hostile_goodput_30s = full
perf_probe::probe_hostile_message_latency = full
perf_probe::probe_rtp_echo_4mib_direct = standard
perf_probe::probe_rtp_echo_4mib_mss8k = standard
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
netem_scenarios::netem_delay_adds_latency
netem_scenarios::netem_duplicate_produces_extra_packets
netem_scenarios::netem_four_state_loss_drops_some
netem_scenarios::netem_passes_traffic_unimpaired
netem_scenarios::netem_rate_limit_throttles_burst
netem_scenarios::netem_reorder_with_rate_jumps_ahead
netem_scenarios::netem_drops_all_with_max_random_loss
netem_scenarios::netem_snapshot_reports_queue_and_stats
perf_probe::probe_hostile_goodput_30s
perf_probe::probe_hostile_message_latency
perf_probe::probe_rtp_echo_4mib_direct
perf_probe::probe_rtp_echo_4mib_mss8k
perf_probe::controller_fat_pipe_has_only_fixed_shaping
perf_probe::deterministic_iid_loss_fat_pipe_is_fixed_seeded_iid_loss
raw_netem_pair::netem_pair_raw_udp_echo_clean_link
raw_netem_pair::netem_pair_raw_udp_latency_is_observable
shared_bottleneck::shared_bneck_fairness_longrun
shared_bottleneck::shared_bneck_fairness_sweep
shared_bottleneck::shared_bneck_late_joiner_fairness
shared_bottleneck::shared_bneck_reorder_tolerant_fairness
shared_bottleneck::shared_bneck_rr_under_bulk_10mbps
shared_bottleneck::shared_bneck_rr_under_bulk_2mbps
shared_bottleneck::shared_bneck_rr_under_dedicated_bulk_10mbps
shared_bottleneck::absolute_starvation_floor_fires_on_a_jain_perfect_collapse
shared_bottleneck::a_slow_reply_resynchronizes_instead_of_ending_the_phase
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
`gilbert_elliott_loss`) and the sparse-ping frame encoder's setup guard
(`send_timestamped_messages`). The rtp padding A/B benches that used to be
gated here relocated with the rest of the rtp scenarios into `rtp/GATE.md`
(step 5); the harness no longer hosts them.

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

Since the shared scaffolding relocated into the harness `test-kit` feature
the helpers that used to live in `tests/tests/support/**` are no longer
inlined in the scenario crate: `with_timeout`, `gilbert_elliott_loss`,
`percentile`, `try_send_observation`, and the `TestScope` reaper machinery
now live in `netem-test/src/kit/**` (behind `netem_test::kit`), the rtp
echo/connect/sink/frame/perf-trace scaffolding lives in
`rtp/src/testkit/**` behind rtp's `testing` feature, the mux-over-rtp
scaffolding in `mux/src/testkit/**` behind mux's `testing`, and the
dual-lane server/connector plumbing in `rtp_mux/src/testkit/**` behind
rtp_mux's `testing` (the rtp_mux kit moved with the rtp_mux scenarios, so
this harness is left with the rtp/rtp kit + mux kit views). The checker
follows the `pub use` shim views in `support/**` into the kit source sets,
so the reachable asserting helpers below are declared exactly as they were
before the relocation. What remains genuinely outside the scan: assertions
inside the `netem-test` library itself (e.g. `NetemPair` internals) and
rtp's own lib internals (e.g. `crate::metrics`), which the scenario crate
can reach but whose sources belong to other crates and are not parsed here -
the same boundary the scan always had. The perf tier's kit-home helpers keep
their report-only role, and the kit unit tests that enforce them run in the
harness's own default test invocation via the `test-kit` self dev-dependency.

```gate-perf-guard-helpers
mux/src/testkit/mux.rs::mux_client_connect_core = 1
mux/src/testkit/mux.rs::spawn_mux_over_rtp_server_core = 1
netem_test/netem-test/src/kit/mod.rs::try_send_observation = 1
netem_test/netem-test/src/kit/payload.rs::with_timeout = 1
netem_test/netem-test/src/kit/presets.rs::gilbert_elliott_loss = 2
netem_test/netem-test/src/kit/stats.rs::percentile = 1
netem_test/netem-test/src/kit/task_scope.rs::run = 1
netem_test/netem-test/src/kit/task_scope.rs::spawn_required = 1
netem_test/netem-test/src/kit/task_scope.rs::submit_test_task = 2
netem_test/netem-test/src/kit/task_scope.rs::submit_test_task_required = 1
rtp/src/testkit/rtp.rs::send_timestamped_messages = 1
rtp/src/testkit/rtp.rs::spawn_rtp_byte_sink_server_core = 1
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

### Midpoint phase assertion (time-to-steady)

`perf_loop.py` exposes the within-run midpoint phase analysis as an asserting
gate: `perf-loop run|analyze --fail-on-phase-drift` exits non-zero when the
capture is `not_ready` with `within_run_phase_not_stable` — the first
half's goodput moving >= 20 % from the second half's at the exact measurement
midpoint, the same 20 % material bound the readiness gate and the same-binary
control use. This is the asserting time-to-steady check for the deterministic
`controller-fat-pipe` lane (pure fixed shaping, no stochastic loss or jitter,
so phase drift there is a controller/queue-growth defect, not noise): an arm
that is not `ready` is inconclusive, never a pass, and the flag turns that
inconclusive phase drift into a failure. Opt-in — stochastic lanes are
expected to phase-drift and must not be run with the flag (the deterministic
lane is where it must always hold).

### Wakeups-per-GiB cap (structural wake ceiling)

`perf-loop run --fail-on-wakes-cap <WAKES_PER_GIB>` exits 2 when any valid
pair's per-endpoint protocol-timer wakes per GiB of delivered application
bytes exceeds the cap. The counters already exist in the trace (schema 28)
and were compared only; the flag makes the absolute ceiling asserting. On the
deterministic controller-fat-pipe lane the delivered rate is link-shaped
(~12 MiB/s), so wakes/GiB is a fixed ratio — measured 0 sender / ~21.5k peer
on a 30 s window — and the documented cap (100k/GiB, ~4.7x the measured peer
band) is the ceiling a wakeup regression must respect; absent values (an
endpoint never observed protocol-timer wakes) stay `null` and never fail.

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
