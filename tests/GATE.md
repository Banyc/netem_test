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

Two lanes reach impairment regimes the shaped, zero-jitter lanes cannot:
`jittery-short-rtt` (unshaped, 20 ms one-way, ±15 ms uniform jitter) is the
only lane that reorders, so its `4 * rttvar` can exceed `srtt / 4` and disarm
the fast-loss gate, and `high-rtt-low-rate-bottleneck` (200 kbit/s, 400 ms
one-way, 128-packet queue) can hold a round trip long enough for RFC 6298's
`srtt + 4 * rttvar` to reach the tens of seconds. The jitter lane is
**diagnostic-only**: its four-seed same-binary control returned `mixed_results`
(`not_ready`, `within_run_phase_not_stable`, three of four pairs moving 11–14 %)
because an unshaped lane's goodput is host-limited rather than link-limited, so
a goodput delta cannot be attributed to a candidate. The thin-link lane is
**diagnostic-only** too, for a third reason: its transport session
deterministically tears down about 36 s into every run. The 200 kbit/s
direction's 128-packet queue is already at its limit at the first sample of a
run, and its tail-drop then discards every packet the client sends — including
the ACKs the server is waiting for — while the queue head keeps delivering
data. The server's peer-liveness `no_response` watchdog is refreshed only by an
ACK, so it fires 30 s after the last ACK that got through and terminates the
session with `proactive_stall`, leaving the mux sink with
`read_error/BrokenPipe` and a dead second half in any window that spans that
moment. The teardown deadline lands within 0.9 s *after* the end of the 5 s
warmup / 30 s window control that first justified `verdict`, so that control is
`ready` with stable phase; one second more warmup puts the same deadline
0.1–1.0 s *inside* the window, and the lane is then `not_ready` on
`trace_evidence_not_healthy` alone. A lane whose usable window is a sub-second
knife-edge on the warmup — and whose teardown time is set by the ACK dynamics a
candidate can move — cannot carry a verdict. Each lane's measured shape and
blind spots are recorded in `tools/PERF_LOOP.md`.

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
jittery-short-rtt = diagnostic
high-rtt-low-rate-bottleneck = diagnostic
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

## The perf-test dual mandate — time and coverage

Every perf test is bound by the dual mandate stated in `AGENTS.md` and
explained in `tools/PERF_INFRA.md` ("The perf-test dual mandate (time and
coverage)"): its **cost** must be bounded, declared and paid for by its tier,
and its **coverage** must be declared, with every cell it claims naming the
test that asserts it and every cell it does not cover recording why.

This block is the harness's own declaration. It is checked by
`python3 tools/check-gate.py`, which resolves each row's `<target>::<test>`
from the compiled test binaries and fails on an unknown target or test, a test
in the wrong tier, a tier sum over its budget, an empty coverage cell and a gap
without a reason. The reserved target name `lib` is the `netem-test` package's
`--lib` target, where the harness's wall-clock probes live. The costs are
**measured** — one release (probes) or one debug (scenarios) `cargo test`
invocation per test, wall-clock to the nearest 0.1 s, rounded up — and they
are per `cargo test` process, so each includes the binary's startup.

The baseline row is the unimpaired link, `lane=loopback layer=netem-link
load=burst metric=counters scale=64-pkt`; every other row varies **one**
dimension from it (impairment for the conformance rows, layer for the pair and
probe rows, lane for the regime rows). No row here is a composite.

The budgets are per tier, and they bound the rows declared in that tier — the
perf-relevant tests, not the crate's correctness unit tests:

```gate-perf-design
netem_scenarios::netem_blackout_gate_drops_then_resumes = default | 0.8 | blackout@control=blackout-gate
netem_scenarios::netem_delay_adds_latency = default | 0.1 | conformance-delay@impairment=delay20ms
netem_scenarios::netem_drops_all_with_max_random_loss = default | 0.4 | conformance-loss@impairment=loss100pct
netem_scenarios::netem_duplicate_produces_extra_packets = default | 0.5 | conformance-dup@impairment=dup50pct
netem_scenarios::netem_four_state_loss_drops_some = default | 0.9 | conformance-loss@impairment=four-state
netem_scenarios::netem_passes_traffic_unimpaired = default | 0.6 | baseline@lane=loopback+layer=netem-link+load=burst+metric=counters+scale=64-pkt
netem_scenarios::netem_rate_limit_throttles_burst = default | 0.3 | conformance-rate@impairment=rate-limit
netem_scenarios::netem_reorder_with_rate_jumps_ahead = default | 0.1 | conformance-reorder@impairment=reorder+rate=rate-limit
netem_scenarios::netem_snapshot_reports_queue_and_stats = default | 0.2 | conformance-queue@impairment=queue-limit
raw_netem_pair::netem_pair_raw_udp_echo_clean_link = default | 0.1 | pair-echo@layer=netem-pair+impairment=none
raw_netem_pair::netem_pair_raw_udp_latency_is_observable = default | 0.2 | pair-latency@layer=netem-pair+impairment=delay25ms
lane_regime_coverage::jittery_lane_moves_the_variance_the_fast_loss_gate_decides_on = default | 0.1 | regime-jittery@lane=jittery-short-rtt+metric=rttvar
lane_regime_coverage::jittery_lane_reorders_where_every_battery_lane_and_a_rate_shaped_jitter_lane_cannot = default | 6.2 | regime-jittery@lane=jittery-short-rtt+metric=reordering
lane_regime_coverage::high_rtt_low_rate_lane_reaches_a_tens_of_seconds_rto_the_battery_lanes_cannot = standard | 14.8 | regime-thin@lane=high-rtt-low-rate+metric=rto
lib::tests::clean_forwarding_perf_probe = perf | 0.2 | probe-forwarding@metric=throughput+layer=netem-runner
lib::tests::learned_destination_cache_perf_probe = perf | 0.2 | probe-dest-cache@metric=throughput+layer=netem-runner
lib::tests::short_deadline_latency_perf_probe = perf | 0.1 | probe-deadline@metric=latency+layer=netem-runner
lib::tests::std_udp_connected_peer_perf_probe = perf | 1.2 | probe-std-udp@metric=throughput+layer=netem-runner+transport=std-udp
```

The declared sums are `default` 10.5 s of a 60 s budget, `standard` 14.8 s of
120 s, `perf` 1.7 s of 60 s, and no row in `full`, whose 300 s budget is
declared so a later row cannot be added without one. `drift` is the relative
tolerance `tools/check-gate.py` applies when it is handed a fresh
`mandate-check.json` (50 %, with a 2 s absolute floor, so a sub-second probe
measured a second slower is a note and not a false alarm): a declared cost
that no longer matches the measured wall-clock is reported.

```gate-budgets
default = 60
standard = 120
full = 300
perf = 60
baseline = netem_scenarios::netem_passes_traffic_unimpaired
drift = 0.5
drift_floor_s = 2.0
```

Each line below is a cell the harness does **not** claim, with the reason it is
empty. The product mandates' cells are named with their pending owner rather
than left out, so the gap is a record and not an omission:

```gate-coverage-gaps
M1@lane=dual-lane+metric=p99 = owned by rtp_mux's mandate_smoke; the harness supplies the impairment instrument and asserts none of the product's mandate bounds. Declaration pending (tools/PERF_INFRA.md, "Where the migration stands").
M2@lane=dual-lane+metric=own-wire = owned by rtp_mux's mandate_smoke, as above: the harness has no lane of its own to assert a wire budget on.
M3@lane=dual-lane+metric=goodput-fraction = owned by rtp_mux's dual_lane_mandates, as above.
M4@lane=dual-lane+metric=per-flow-share = owned by rtp_mux's mandate_smoke M4 arm, as above.
conformance-delay@impairment=correlated-delay = no conformance test asserts the delay-correlation draw distribution; the correlated knobs surface only through the four-state loss test's aggregate counters.
conformance-queue@impairment=bufferbloat = queue behaviour is asserted only by netem_snapshot_reports_queue_and_stats at one scale; no standing-queue (bufferbloat) scenario exists at the harness layer, where the battery's bufferbloat arms are rtp-level.
pair-echo@impairment=loss = the raw pair is exercised only on a clean link and at a fixed delay; lossy pair behaviour is covered by the link scenarios instead.
regime-thin@lane=high-rtt-low-rate+metric=goodput = the thin-link lane's goodput is not measurable - its transport session tears down about 36 s into every run (tools/PERF_INFRA.md, the perf-loop lane roles) - so only the RTO-reach property is claimed there.
probe-forwarding@scale=multi-megabyte = the probes measure single-datagram and 200-packet cost; transfer-scale cost is measured by the perf-loop battery, not by a harness test.
probe-forwarding@metric=cpu-attribution = per-datagram CPU cost is measured by owning-symbol attribution (tools/samply_hotspots.py), not by a wall-clock probe.
probe-forwarding@load=request-response = the probes drive the runner directly, so no load shape exists at this layer; request/response is a transport-lane shape owned by rtp_mux.
```
