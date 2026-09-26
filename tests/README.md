# The netem_test scenario gate scope

This package (`tests`) is the **impairment harness**, not the owner of any
application performance contract. It consumes the `netem-test` instrument (the
kernel-faithful `NetemPair`/`NetemConfig` impairment plumbing and the
`test-kit` generic helpers) and hosts the instrument's own conformance suite
(`netem_scenarios`, `raw_netem_pair`) plus the impairment-regime suite
(`lane_regime_coverage`) that measures the jitter and thin-link presets the
perf battery's lanes cannot reach.

Every application scenario relocated out of this package into the crate whose
code it exercises, together with its tier, its assertions, and its gate
records:

| suite | now in | gate |
| --- | --- | --- |
| `rtp_bufferbloat`, `rtp_burst_loss`, `rtp_fec`, `rtp_gentle`, `rtp_liveness`, `rtp_loss`, `rtp_mss`, `rtp_padding_bench` | `rtp/tests` | `rtp/GATE.md` |
| `hol_verify4` (raw `rtp` arms), `shared_bottleneck` | `rtp/tests` | `rtp/GATE.md` |
| `contested_latency`, `perf_probe` | `rtp_mux/tests` | `rtp_mux/GATE.md` |
| `mux_over_rtp`, `mux_over_rtp_perf`, `rtp_and_mux`, `mux_bulk_clean_stall`, `mux_stream_fairness`, `hol_verify4` (mux bulk arms) | `rtp_mux/tests` | `rtp_mux/GATE.md` |
| `dynamic_contested`, `hol_probe`, `rtp_longrun`, `rtp_mux_jitter`, `dual_lane_mandates`, `rtp_mux`, `explorer`, … | `rtp_mux/tests` | `rtp_mux/GATE.md` |

Each mandate has exactly one asserting authority, so the tri-mandate
constitution (low interactive-lane latency, interactive goodput without wire
inflation, high bulk-lane goodput) lives in the owning crate's `GATE.md` and is
never restated here. See `tests/GATE.md` for the manifest/tier mechanics of
THIS package, and each owning crate's `GATE.md` for the floors.

## One authority per mandate

| mandate | asserted by (crate) | gate |
| --- | --- | --- |
| **M1** low latency of the interactive lane (p99 floor + zero >250 ms spikes, median-of-3) | `rtp_mux` | `rtp_mux_jitter::jitter_duallane_constitution_gate_p99` |
| **M2** reasonable goodput of the interactive lane (`delivery == 1.000` + own-wire ≤ 6× offered) | `rtp_mux` | `rtp_mux_jitter::jitter_duallane_constitution_gate` (default tier) |
| **M3** high goodput of the bulk lane (≥ 0.35 × configured link rate, median-of-3) | `rtp_mux` | `dual_lane_mandates::bulk_lane_goodput_stays_above_capacity_fraction` |

The bounds and their derivations are stated in `rtp_mux/GATE.md`
("Performance") and module-level in `rtp_mux/tests/dual_lane_mandates.rs` —
never restated here. The mux layer keeps only the memory floor and its
mux-only targets in `mux/GATE.md`; mux has no opt-in scenario left, so the
loopback bulk ceilings, the fairness floors and the mux-over-rtp scenarios
moved with those topologies to `rtp_mux/GATE.md`. The harness tooling gates
stay with the tooling: `perf-loop run|analyze --fail-on-phase-drift` and
`--fail-on-wakes-cap` (see `tools/PERF_LOOP.md`).

## Running the gate

`python3 tools/check-gate.py` verifies this package's manifest and the
perf-loop lane roles against the compiled test binaries. The per-crate gates
run with the parameterized checker from each crate checkout:

```sh
python3 ../netem_test/tools/check-gate.py --crate . rtp tests GATE.md
python3 ../netem_test/tools/check-gate.py --crate . mux tests GATE.md
python3 ../netem_test/tools/check-gate.py --crate . rtp_mux tests GATE.md
```

## The perf-loop probe is rtp_mux's

`tools/perf-loop` builds its probe with `cargo test -p rtp_mux --test
perf_probe` from the frozen suite's exported **`rtp_mux`** component, because
the probe's code lives in `rtp_mux/tests/perf_probe.rs` and drives
`mux`-over-`rtp` lanes — the `rtp`+`mux` cooperation that only `rtp_mux` may
hold. The harness owns the tooling and the lane taxonomy; a
`--component-revision rtp_mux=<commit>` pin therefore selects the probe that
runs, and a component without the probe target is refused rather than silently
built from elsewhere.

## Perf-loop lane roles: verdict vs diagnostic

A `tools/perf-loop` lane is either a **verdict** lane (a change may be retained
or rejected on its paired evidence) or **diagnostic-only** (its numbers are
reported and may guide follow-up work, but a change must never be retained or
rejected on it). The lane's role is stamped as `link_role` into `run.json` by
`perf_loop.lane_classification`.

**`hostile` is diagnostic-only.** In every one of the 70 recorded `hostile`
lane runs — both the 5 s and the 20 s warmup — the lane was `not_ready`
(`within_run_phase_not_stable`), and the spread was the lane's own stochastic
first/second-half goodput phase variance rather than a candidate effect (the
baseline and candidate trees were byte-identical in the control). The lane
cannot attribute a delta to a candidate, so it must not be read as a verdict.
Report its paired numbers as a diagnostic only.

The full lane-role table lives in `tests/GATE.md` (`gate-lane-roles`) and is
machine-checked by `python3 tools/check-gate.py` against
`perf_loop.lane_classification`, so a verdict lane cannot be mis-declared
diagnostic (or the reverse) without the checker failing.

## Notes

- The harness holds only the instrument and its own conformance tests; every
  rtp/mux/rtp_mux floor is asserted by the owning crate's gate and stated in
  that crate's `GATE.md`.
- `netem-test` is a leaf crate: a pinned harness revision cannot put two
  versions of the harness in one dependency graph.
