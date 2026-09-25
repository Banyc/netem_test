# `netem_test` workspace

A reusable, deterministic in-process UDP proxy that applies `sch_netem`-style
impairment (delay, jitter, loss, duplication, reordering, rate-limiting,
queue limit) to forwarded datagrams, plus the paired performance-capture
instrument whose probe and lanes are hosted by the crates they measure.

## Layout

- `netem-test/` — the generic harness crate (`NetemConfig`, `NetemLink`,
  `NetemPair`, `RndState`/`CorRng`, `LossModel`, `UdpTransport`, counters,
  and the `test-kit` scenario helpers).
- `tests/` — the harness's own conformance suite (`netem_scenarios`,
  `raw_netem_pair`): every impairment knob fires, the four-state loss model
  matches `sch_netem`, and the pair echoes, delays, and reports. The
  impairment-regime suite (`lane_regime_coverage`) measures the two presets
  that reach the jitter and thin-link regimes the perf battery's lanes cannot,
  and asserts that each separates a decision those lanes cannot separate. The
  application scenarios that consume `rtp`, `mux` or `rtp_mux` live in those
  crates' own test targets, where the code they exercise lives; the harness
  depends on none of them.
- `tools/` — the performance capture and comparison tooling (`perf-loop`,
  `perf_loop.py`, `mandate-check`, `mandate_plot.py`, `check-gate.py`,
  `render_graph.py`, `rtp_trace_compare.py`, `rtp_trace_report.py`,
  `samply_hotspots.py`, `calib.py`, …). The tooling stays here; the probe it
  drives is `rtp_mux/tests/perf_probe.rs`, so
  `--component-revision rtp_mux=<commit>` selects the probe that runs.

The harness is a leaf: `netem-test` depends only on `dfsql`, `serde`,
`parking_lot`, and optionally `tokio`, and the `tests` package only on
`netem-test` and `tokio`.

## Performance quick path

The ordinary paired performance workflow is the quick path. For raw
packet-processing ceilings (the common case), run the paired loop with the
clean link profile and production-sized MSS:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
    --link-profile clean --mss-bytes 1400 --seeds 11,21 --window-seconds 10
```

The historical/default profile remains hostile with MSS 8192:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
    --seeds 11,21 --window-seconds 30
```

`--link-profile` and `--mss-bytes` are always identical across each
baseline/candidate pair and recorded in every manifest (`run.json`, both
trace manifests, and the paired `manifest.csv`).

For the interactive path's tri-mandate constitution, `./tools/mandate-check`
runs `rtp_mux`'s smoke set, renders its panels and writes
`mandate-check.json`; run it and read the plots for any change to `rtp`,
`mux` or `rtp_mux` (see `tools/MANDATE_SMOKE.md`).

Every output, temporary, trace, log, and Cargo target must resolve beneath
`$TMPDIR`. The paired runner always enables diagnostic mode so the
absolute hostile-goodput floor cannot abort evidence collection, but payload
integrity, task outcome, probe exit status, and comparison health remain
enforced.

## Reporting conclusions

When a performance comparison is part of a conclusion, report:

- trace health (per-run `evidence_quality`, degraded/invalid runs excluded),
- the paired goodput delta (and agreement across seed pairs),
- one `largest_changes` signal (the biggest material metric change),
- the `comparison.json` path, and
- the attached `does_not_prove` guard — the verdict is a consistency label,
  not proof that any specific change produced the difference.

## Testing

`cargo test -p tests` runs the harness's default tier: the instrument's own
conformance scenarios. Every scenario that needs `rtp`/`mux`/`rtp_mux` runs in
the crate that owns it and is gated there (`rtp/GATE.md`, `mux/GATE.md`,
`rtp_mux/GATE.md`), each checked with the parameterized
`tools/check-gate.py --crate <root> <package> <dir> GATE.md`; the harness's own
gate is `tests/GATE.md`. An unnoticeable skip cannot happen: the checker fails
if a scenario is not classified.

```sh
cargo test -p netem-test        # harness unit tests
cargo test -p tests             # default gate (see tests/GATE.md)
python3 tools/check-gate.py     # verify the gate manifest matches reality
python3 -m pytest tools/ -q     # verify the tooling
```
