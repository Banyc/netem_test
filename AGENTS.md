# `netem_test` workspace

A reusable, deterministic in-process UDP proxy that applies `sch_netem`-style
impairment (delay, jitter, loss, duplication, reordering, rate-limiting,
queue limit) to forwarded datagrams, plus application-specific integration
scenarios consuming `netem-test`, `rtp`, and `mux`.

## Layout

- `netem-test/` — the generic harness crate (`NetemConfig`, `NetemLink`,
  `NetemPair`, `RndState`/`CorRng`, `LossModel`, `UdpTransport`, counters).
- `tests/` — application-specific scenarios (`mux_over_rtp`, `rtp_loss`,
  `rtp_fec`, `rtp_mss`, `netem_scenarios`, `perf_probe`, …).
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

```sh
cargo test -p netem-test       # harness unit tests
cargo test -p tests            # integration scenarios (loss, delay, FEC, MSS, …)
cargo test -p tests --test perf_probe -- --ignored --nocapture   # perf probes
```
