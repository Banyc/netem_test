# `netem_test` workspace

A reusable, deterministic in-process UDP proxy that applies `sch_netem`-style
impairment (delay, jitter, loss, duplication, reordering, rate-limiting,
queue limit) to forwarded datagrams, plus application-specific integration
scenarios consuming `netem-test`, `rtp`, and `mux`.

## Layout

- `netem-test/` — the generic harness crate (`NetemConfig`, `NetemLink`,
  `NetemPair`, `RndState`/`CorRng`, `LossModel`, `UdpTransport`, counters).
- `tests/` — application-specific scenarios for the harness, `rtp` and
  `rtp_mux` (`rtp_loss`, `rtp_fec`, `rtp_mss`, `netem_scenarios`,
  `perf_probe`, `rtp_mux_jitter`, `hol_probe`, …). The mux-owned scenarios
  (`mux_over_rtp`, `mux_over_rtp_perf`, `rtp_and_mux`, `mux_bulk_clean_stall`,
  `mux_stream_fairness`, the `probe_mux_*` ceilings and the v4 mux bulk-lane
  probes) relocated into the owning crate (`mux/tests`, `mux/GATE.md`) with
  the mux layer kit (`mux::testkit`, behind mux's `testing` feature); the
  harness reaches the same single authority through the `support/{mux,stats}.rs`
  shim views. `netem-test` stays a leaf: it consumes none of `rtp`/`mux`/`rtp_mux`.
- `tools/` — performance capture and comparison tooling
  (`perf-loop`, `perf_loop.py`, `rtp_trace_compare.py`,
  `rtp_trace_report.py`, `samply_hotspots.py`, `calib.py`, …).

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

`cargo test -p tests` runs only the default tier: the harness and support unit
tests plus the seeded sub-second scenarios (netem behaviour, clean/loss/FEC/MSS/
interactive-lane delivery). The mux scenarios run in the owning crate
(`cargo test -p mux`, gate recorded in `mux/GATE.md`). Every other scenario is
`#[ignore]`d, and its name and tier are recorded in `tests/GATE.md`; the
per-crate checker (`tools/check-gate.py`, parameterized with `--crate <root>
<package> <dir> <GATE.md>`) fails if a scenario is not classified, so an
unnoticed skip cannot happen.

```sh
cargo test -p netem-test        # harness unit tests
cargo test -p tests             # default gate (see tests/GATE.md)
cargo test -p tests -- --ignored --test-threads=1   # opt-in standard/full tiers
cargo test -p tests --test perf_probe -- --ignored --nocapture   # perf probes
python3 tools/check-gate.py     # verify the gate manifest matches reality
```
