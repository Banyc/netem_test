# The netem_test validation gate

This file is the authoritative scope of the harness's own scenario gate. It
covers the `tests` package's remaining targets — the `netem-test` instrument's
conformance suite — and the perf-loop lane roles, which belong to the harness
tooling. `cargo test -p tests` silently skips every `#[ignore]`d scenario, so
the gate is defined in tiers and the `gate-manifest` block below names every
opt-in scenario and its tier. The manifest is machine-checked by
`python3 tools/check-gate.py`, which fails if a scenario is added or removed
without the manifest being updated, making an unnoticed `#[ignore]` skip
impossible.

Run the checker after adding, removing, or re-tiering any scenario:

```sh
python3 tools/check-gate.py
```

The application scenarios that consume `rtp`, `mux` or `rtp_mux` are asserted
by **those crates'** own gates, never here: the harness keeps the impairment
instrument and its conformance suite, and each layer's floors are stated and
checked in the crate that owns the code they exercise
(`rtp/GATE.md`, `mux/GATE.md`, `rtp_mux/GATE.md`), each with
`python3 ../netem_test/tools/check-gate.py --crate . <crate> tests GATE.md`.
`netem_test` itself consumes none of `rtp`/`mux`/`rtp_mux`, so a pinned harness
revision cannot put two versions of the harness in one graph.

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
  declared report-only in the `gate-perf-guard-helpers` block.

The long-running `perf-loop` battery (`tools/perf-loop`, lanes `clean`,
`controller-fat-pipe`, `hostile`, `lossy-400kib`, `hostile-fat-pipe`) is a
separate, much slower evidence path and is not part of `cargo test`. Its
mandatory rendered-graph evidence is produced by `tools/render_graph.py`; a
graph that cannot be produced is a non-zero-exit error, not an empty file to
skim past (see `tools/PERF_LOOP.md`, "Rendered graph evidence (mandatory)").

## Default tier (runs in `cargo test -p tests`)

`netem_scenarios` and `raw_netem_pair` are the instrument's own conformance
suite: every impairment knob fires, the four-state loss model matches the
`sch_netem` semantics, and the pair echoes, applies observable latency, and
reports its counters. `lane_regime_coverage` is the impairment-regime suite: it
measures the two presets (`jittery_short_rtt_link`,
`high_rtt_low_rate_bottleneck`) that reach the jitter and thin-link regimes the
perf battery's lanes cannot, and asserts that each lane separates a decision the
battery lanes cannot separate. Nothing else lives here — the layer scenarios are
in the crates that own the code they exercise.

The `gate-default-required` block names the asserting scenarios that must stay
in this tier; `check-gate.py` fails if one is re-`#[ignore]`d or removed. The
`gate-asserting` block records the report-only/asserting split.

```gate-default-required
netem_scenarios::netem_delay_adds_latency
netem_scenarios::netem_duplicate_produces_extra_packets
netem_scenarios::netem_four_state_loss_drops_some
netem_scenarios::netem_passes_traffic_unimpaired
netem_scenarios::netem_rate_limit_throttles_burst
netem_scenarios::netem_reorder_with_rate_jumps_ahead
netem_scenarios::netem_drops_all_with_max_random_loss
netem_scenarios::netem_snapshot_reports_queue_and_stats
raw_netem_pair::netem_pair_raw_udp_echo_clean_link
raw_netem_pair::netem_pair_raw_udp_latency_is_observable
lane_regime_coverage::jittery_lane_reorders_where_every_battery_lane_and_a_rate_shaped_jitter_lane_cannot
lane_regime_coverage::jittery_lane_moves_the_variance_the_fast_loss_gate_decides_on
```

## Opt-in manifest

Each line is `target::test_name = tier`. The set must equal the set of tests
reported by `cargo test -p tests --test <target> -- --list --ignored`.

```gate-manifest
lane_regime_coverage::high_rtt_low_rate_lane_reaches_a_tens_of_seconds_rto_the_battery_lanes_cannot = standard
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
netem_scenarios::netem_delay_adds_latency
netem_scenarios::netem_duplicate_produces_extra_packets
netem_scenarios::netem_four_state_loss_drops_some
netem_scenarios::netem_passes_traffic_unimpaired
netem_scenarios::netem_rate_limit_throttles_burst
netem_scenarios::netem_reorder_with_rate_jumps_ahead
netem_scenarios::netem_drops_all_with_max_random_loss
netem_scenarios::netem_snapshot_reports_queue_and_stats
raw_netem_pair::netem_pair_raw_udp_echo_clean_link
raw_netem_pair::netem_pair_raw_udp_latency_is_observable
lane_regime_coverage::jittery_lane_reorders_where_every_battery_lane_and_a_rate_shaped_jitter_lane_cannot
lane_regime_coverage::jittery_lane_moves_the_variance_the_fast_loss_gate_decides_on
lane_regime_coverage::high_rtt_low_rate_lane_reaches_a_tens_of_seconds_rto_the_battery_lanes_cannot
```

## Perf-tier reach into asserting helpers

The direct-body scan only sees assertions in a `perf` scenario's own body, so
it would miss an assertion moved one call away into a helper. The
`gate-perf-guard-helpers` block below closes that hole as far as a regex-level
tool can. For every `perf` scenario the checker builds a crate-local call graph
(functions in `tests/<target>.rs` and the kit sources the target imports; a
call is resolved against the caller file's `use` declarations first, then the
caller's own module, then a bare-name fallback) and takes the transitive
closure. Every asserting function the closure reaches must be listed here as
`RELATIVE_PATH::fn = assertion-token-count`, together with the number of
`assert!`/`assert_eq!`/`assert_ne!`/`panic!`/`unreachable!` tokens in its body
(the debug-only `debug_assert!`/`debug_assert_eq!`/`debug_assert_ne!` forms
count as assertion tokens too). The checker fails when a reachable asserting
helper is unrecorded, when a recorded helper's token count changes, when a
recorded helper is no longer reachable, or when a `perf` scenario's own body
cannot be located in its source file.

The harness has no `perf`-tier scenario left, so the block is empty: every
report-only scenario, and every asserting helper one reached, now live in the
owning crate's gate. The kit sources the closure used to follow
(`netem-test/src/kit/**`, `rtp/src/testkit/**`, `mux/src/testkit/**`,
`rtp_mux/src/testkit/**`) are still scanned per target, so a `perf` scenario
added here cannot silently reach an undeclared asserting helper.

Residual limitations, stated so they are not mistaken for coverage. The graph
is name-based and over-approximates whenever a call cannot be narrowed, so a
new helper whose name collides with a method name can be flagged for review. It
cannot see an edge created by passing a function by name (e.g.
`let check: fn(..) = $gates`), through a trait object, or generated by a macro;
an assertion reached only through such an indirection is invisible to the scan.
It records a guard helper's assertion *token count*, not the text of the
assertions, so replacing an assertion in a recorded guard with a different
assertion of the same token count is not detected. Assertions inside the
`netem-test` library itself (e.g. `NetemPair` internals) remain outside the
scan: the scenario crate can reach them but their sources are not parsed here.
The kit unit tests that enforce the helpers run in the harness's own default
test invocation via the `test-kit` self dev-dependency.

```gate-perf-guard-helpers
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

The perf-loop probe itself is an **rtp_mux** target: `tools/perf-loop` builds
`cargo test -p rtp_mux --test perf_probe` from the frozen `rtp_mux` component,
because the battery lanes it drives are `mux`-over-`rtp` sessions — the
`rtp`+`mux` cooperation that only `rtp_mux` may hold — and the probe's code is
`rtp_mux`'s. The harness owns the tooling and its lane taxonomy, not the probe.

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
