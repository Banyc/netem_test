# Paired performance capture loop

`tools/perf-loop` is a safe one-command paired performance capture loop. It
runs the hostile/clean goodput probe against a baseline and a candidate
frozen `netem_test` workspace (each with sibling `rtp`, `mux`, `rtp_mux`,
`tokio_udp`, and `udp_listener` repositories), and compares the traces.

## Default hostile command

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
    --seeds 11,21 --window-seconds 30
```

The historical/default scenario is hostile with MSS 8192
(`--link-profile hostile --mss-bytes 8192`).

## Clean packet-processing-ceiling quick path

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
    --link-profile clean --mss-bytes 1400 --seeds 11,21 --window-seconds 10
```

`--link-profile` and `--mss-bytes` are identical across each pair and are
recorded in every manifest (`run.json`, both trace manifests, and the
paired `manifest.csv`), while the defaults remain hostile/8192.

## Artifacts

Every output, temporary, trace, log, and Cargo target resolves beneath
`$TMPDIR`:

- `run.json` — top-level run record: `link_profile`, `mss_bytes`,
  workspaces, seeds, window, target directories, per-role/seed runs and
  their artifact paths, and the comparison verdict.
- `manifest.csv` — one row per role/seed probe: runner exit, role, Cargo
  profile, sorted component-revision JSON, seed, link profile, MSS, trace
  dir.
- `trace-<role>-<seed>/` — the probe's trace directory (manifest/rtp/rtp_
  peer/netem/progress) with the link profile and MSS recorded in its
  manifest.
- `<role>-<seed>.log` — streamed probe log.
- `comparison.json` / `comparison.html` — schema-2 comparison and report.

## Verdicts and exit codes

- `0` — the paired run completed and the comparison is valid.
- `2` — probe/evidence failure: any probe exited non-zero, the comparison
  evidence is invalid, or the comparison tool itself failed.
- `3` — only with `--fail-on-regression` and a `likely_regression` verdict.

The verdict is a consistency label over valid seed-paired evidence only; it
is not statistical confidence or causality. Every agent hint carries a
`does_not_prove` constraint and no hint identifies a specific branch as
causal.

## Diagnostic-mode limits

The paired runner always sets `NETEM_PERF_DIAGNOSTIC_MODE=1`, which may
bypass only the absolute hostile-goodput floor
(`HOSTILE_GOODPUT_FLOOR_MIB_S`) so evidence collection cannot be aborted.
It never bypasses payload corruption, task failure, probe exit failure, or
invalid evidence.

## Safe paths and workspace topology

- Baseline and candidate must be distinct resolved workspaces; a mutable
  workspace must not be used as both roles, and debug/release evidence must
  not be mixed.
- A frozen workspace has a `netem_test/` checkout with sibling `rtp`,
  `mux`, `rtp_mux`, `tokio_udp`, and `udp_listener` repositories; each
  component's jj revision is recorded in the manifest.

## Samply sampling

When a workload's cost is dominated by CPU, record a Samply profile instead
of (or alongside) the RTP traces:

```sh
samply record --save-only --unstable --presymbolicate \
    -- <workload command and its arguments>
```

Keep both the profile and its `.syms.json` sidecar beneath tmp dir

Summarize the profile into deterministic owning-symbol hotspots:

```sh
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json \
    --contains tokio --limit 20 --json
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json \
    --contains tokio --thread tokio-runtime-worker --limit 20 --json
```

When the profile also samples unrelated runtime threads, filter to the
workload-owning thread with the exact-name `--thread` option (repeatable;
all threads with any requested name are selected and a requested name that
matches nothing is an error).  The workload-owning thread is required when
it avoids unrelated runtime samples.

When comparing workloads that differ, compare samples per forwarded packet
rather than raw sample counts.
