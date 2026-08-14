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

## Direct lane

`--link-profile direct` bypasses NetemPair entirely: the client connects
straight to the probe server and the trace records zero-valued netem
placeholders, so artifacts stay schema-compatible while the measurement
isolates endpoint and host throughput from proxy overhead.

## Frozen executables and counterbalanced order

Both roles are prebuilt once (`cargo test --release -p tests --test
perf_probe --no-run --message-format=json-render-diagnostics` per role,
streamed to `build-ROLE.log`) before any timed run; every seed invokes the
recorded `perf_probe` executable directly, so no timed run compiles.  Pair
execution order alternates per seed-major pair (baseline/candidate then
candidate/baseline) so scheduling order cannot bias the roles; stored rows
keep their baseline/candidate labels, and `run.json` records
`pair_execution_order="alternating"`, the `builds`, and the per-run
`executable`.

## Same-binary control

`--same-binary-control` compares a workspace with itself (the only mode
that permits identical resolved workspaces; the resolved executable paths
must also match) to calibrate run-to-run variance.  `run.json` then records
`control_calibration`, classified `stable` only when every absolute valid
paired goodput delta is below 10% — with the median/maximum absolute delta
and the number of pairs that crossed the material-change threshold (false
material changes on an identical binary).  With
`--fail-on-control-instability`, an unstable calibration exits 4.

## Artifacts

Every output, temporary, trace, log, and Cargo target resolves beneath
`$TMPDIR`:

- `run.json` — top-level run record: `pair_execution_order` (`alternating`),
  `link_profile`, `mss_bytes`, workspaces, seeds, window, target
  directories, same-binary control flag, `control_calibration`,
  per-role `builds` (frozen executable + build log), per-role/seed runs
  (with the recorded `executable`) and their artifact paths, and the
  comparison verdict.
- `manifest.csv` — one row per role/seed probe: runner exit, role, Cargo
  profile, sorted component-revision JSON, seed, link profile, MSS,
  resolved executable, trace dir.
- `trace-<role>-<seed>/` — the probe's trace directory (manifest/rtp/rtp_
  peer/netem/progress) with the link profile and MSS recorded in its
  manifest.
- `<role>-<seed>.log` — streamed probe log.
- `comparison.json` / `comparison.html` — schema-2 comparison and report.

## RTP trace schema 15 evidence

The 55-column RTP trace carries the complete congestion-controller and
retransmission-scheduler snapshot on every state row; event-only raw RTT
rows leave all snapshot columns empty. The controller evidence:

- `congestion_control_rtt_us` — the control RTT last used by the controller;
  it paces probe spacing and the queue gate.
- `congestion_rtt_floor_us` / `congestion_queue_tolerance_us` — the
  controller's RTT floor and queue-gate tolerance: smoothed RTT above
  floor + tolerance is what marks `queue_building`.
- `congestion_delivery_peak_packets_per_second` /
  `congestion_drain_floor_packets_per_second` /
  `congestion_drain_target_packets_per_second` — the delivery peak feeding
  the drain floor, the floor itself, and the drain target the controller
  applies during gentle draining.
- `congestion_rate_samples`, `congestion_bandwidth_probe_decisions`,
  `congestion_bandwidth_probe_increases`, `congestion_delay_drains` —
  cumulative controller decision counters.
- `congestion_bandwidth_probe_before_feedback` — increases applied before
  the previous increase received feedback. This is a timing classification,
  not proof that a probe caused queue growth; the paired report only emits
  `congestion_bandwidth_probe_before_feedback_percent` when
  `congestion_bandwidth_probe_increases` is nonzero, and treats it as
  lower-is-better (fewer un-feedbacked increases = more conservative probe
  discipline).
- `congestion_last_bandwidth_probe_interval_us` — spacing between the last
  two applied probe increases.

Retransmission-scheduler evidence:

- `retransmission_timeout_us` — the live RTO estimate; on snapshot rows the
  report overlays it on the RTT plot so a growing RTO tracks the estimator.
- `oldest_pipe_packet_age_us` / `maximum_packet_rto_overdue_us` — live pipe
  timing: how old the oldest in-pipe packet is and how far past its RTO
  deadline the most overdue packet has run.
- `rto_deadline_postponements` — RTO deadlines postponed by the lazy
  live-estimator floor.
- `retransmission_active_packets` / `retransmission_ready_packets` — the
  retransmission scheduler's active set and how many of those packets are
  currently due or evidence-armed; the paired report exposes their maximum
  depths and the final postponement count.

## Verdicts and exit codes

- `0` — the paired run completed and the comparison is valid.
- `2` — probe/evidence failure: any probe exited non-zero, the comparison
  evidence is invalid, or the comparison tool itself failed.
- `3` — only with `--fail-on-regression` and a `likely_regression` verdict.
- `4` — only with `--fail-on-control-instability` and an unstable
  same-binary `control_calibration`.

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

- Baseline and candidate must be distinct resolved workspaces outside
  same-binary control mode; `--same-binary-control` is the only mode that
  permits identical workspaces, and it additionally requires the resolved
  executable paths to match.  A mutable workspace must not be used as both
  roles, and debug/release evidence must not be mixed.
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
