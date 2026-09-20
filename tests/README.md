# The netem_test scenario gate scope

This package (`tests`) is the **impairment harness**, not the owner of any
application performance contract. It consumes the `netem-test` instrument (the
kernel-faithful `NetemPair`/`NetemConfig` impairment plumbing and the
`test-kit` generic helpers) and hosts the instrument's own conformance suite
(`netem_scenarios`, `raw_netem_pair`, the `perf_probe` probes and seeding
tests, `contested_latency`, `hol_verify4`, `shared_bottleneck`).

The rtp-owned scenario suites relocated into the `rtp` crate with step 5 of
the relocation (`rtp_bufferbloat`, `rtp_burst_loss`, `rtp_fec`, `rtp_gentle`,
`rtp_liveness`, `rtp_loss`, `rtp_mss`, `rtp_padding_bench`; see
`rtp/GATE.md`), the `mux` suites into `mux` (step 3) and the `rtp_mux` suites
into `rtp_mux` (step 4). Their gates and the tri-mandate constitution live
**in those crates**, so every mandate has exactly one asserting authority —
see `tests/GATE.md` for the manifest/tier mechanics of THIS package only.

## The tri-mandate constitution: one authority per mandate

| mandate | asserted by (crate) | gate | run (M1/M3 opt-in `full`; M2 default) |
| --- | --- | --- | --- |
| **M1** low latency of the interactive lane (p99 floor + zero >250 ms spikes, median-of-3) | `rtp_mux` | `rtp_mux_jitter::jitter_duallane_constitution_gate_p99` | `cargo test --release -p rtp_mux --test rtp_mux_jitter -- --ignored jitter_duallane_constitution_gate_p99 --nocapture --test-threads=1` |
| **M2** reasonable goodput of the interactive lane (`delivery == 1.000` + own-wire ≤ 6× offered) | `rtp_mux` | `rtp_mux_jitter::jitter_duallane_constitution_gate` (default tier — runs on every `cargo test -p rtp_mux`; deterministic counts) | `cargo test -p rtp_mux` |
| **M3** high goodput of the bulk lane (≥ 0.35 × configured link rate, median-of-3) | `rtp_mux` | `dual_lane_mandates::bulk_lane_goodput_stays_above_capacity_fraction` | `cargo test --release -p rtp_mux --test dual_lane_mandates -- --ignored bulk_lane_goodput_stays_above_capacity_fraction --nocapture --test-threads=1` |

The bounds and their derivations, and the interactive scaling boundary
(`hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery`), are stated in
`rtp_mux/GATE.md` ("Performance") and module-level in
`rtp_mux/tests/dual_lane_mandates.rs` — never restated here. The mux layer's
per-stream contributions are in `mux/GATE.md` (default tier: delivered == the
offered payload; standard tier: loopback bulk ceilings; full tier: fairness
floors). The harness tooling gates stay with the tooling: `perf-loop run|analyze
--fail-on-phase-drift` and `--fail-on-wakes-cap` (see
`tools/PERF_LOOP.md`).

## Running the gate

`python3 tools/check-gate.py` verifies this package's manifest and the
perf-loop lane roles against the compiled test binaries. The per-crate gates
run with the parameterized checker from each crate checkout:

```sh
python3 ../netem_test/tools/check-gate.py --crate . rtp tests GATE.md
python3 ../netem_test/tools/check-gate.py --crate . mux tests GATE.md
python3 ../netem_test/tools/check-gate.py --crate . rtp_mux tests GATE.md
```

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

## The rtp-side in-process oracle

The `rtp` crate carries its own deterministic in-process oracles for the
per-packet interactive repair path (faster and lower-noise than the netem
harness, and they read the sender/receiver FEC counters directly):

- `src/socket/stream.rs::probe_single_symbol_interactive_fec_repair` — the
  single-symbol interactive repair path for the depth-1 `interactive_prompt`
  preset vs depth-3 `max_diversity`.
- `src/socket/stream.rs::probe_fresh_tail_armor_latency` — the fresh-tail armor
  duplicate's repair latency vs the ARQ fallback.

```sh
cargo test --lib probe_ -- --ignored --nocapture   # from crates/rtp
```

Use these to attribute a repair-latency change before/after a fix; the
end-to-end dual-lane latency/throughput constitution gates live in `rtp_mux`
(pointer table above), not here.

## Notes

- The harness holds only the instrument and its own conformance tests; every
  rtp/mux/rtp_mux floor is asserted by the owning crate's gate and stated in
  that crate's `GATE.md` (the constitution is never restated here).
- FEC/redundancy counters and per-lane wire counters are printed per arm for
  attribution by the `rtp_mux_jitter` arms, which now run from `rtp_mux`.