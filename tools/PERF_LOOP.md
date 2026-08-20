# Paired performance capture loop

`tools/perf-loop` is a safe one-command paired performance capture loop. It
runs the hostile/clean goodput probe against a baseline and a candidate
frozen `netem_test` workspace (each with sibling `rtp`, `mux`, `rtp_mux`,
`tokio_udp`, and `udp_listener` repositories), and compares the traces.

Freeze the current completed suite without creating additional JJ workspaces:

```sh
./tools/perf-loop snapshot --source . --source-revision @- \
--component-revision rtp=<40-char-commit> --output $TMPDIR/rtp-before
```

The snapshot exports the exact committed tree of every sibling component
(`jj --no-pager log -r <revision>` resolving the 40-character `commit_id`
plus `change_id`) into its own directory beneath the output, never the
mutable working copy. `--source-revision` (default `-`) is resolved
independently in every component; repeat `--component-revision
COMPONENT=REVISION` to pin individual components (duplicates and unknown
component names are rejected). When supplied, `--output` must resolve
beneath `$TMPDIR`; otherwise the tool creates a unique safe-root output directory. `suite-revisions.json` records the manifest schema
(`PROBE_SOURCE_MANIFEST_SCHEMA`), the source workspace, the requested
revision, `component_revision_overrides`, and each component's exact
40-character `commit_id` plus `change_id`. Paired runs read this manifest so
archived trees remain revision-identifiable even though they are
deliberately not mutable JJ workspaces.

## Default hostile command

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile hostile --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

The historical/default scenario is hostile with MSS 8192
(`--link-profile hostile --mss-bytes 8192`).

# Reanalyze an existing capture

Recompute phase stability, execution-order effects, AB/BA role effects, readiness, and same-binary calibration from a preserved result without building or running the network again:

```sh
./tools/perf-loop analyze --result $TMPDIR/existing-perf-result
```

The command prints the recomputed analysis and leaves the artifact untouched. Pass '--update-run-json' to replace only those five derived fields in the existing 'run.json'. Both modes require the result directory to remain beneath `$TMPDIR`; neither mode edits 'comparison.json' or its trace inputs.

## Lossy narrow-link safety lane

Use the existing 400 KiB/s lossy narrow-link preset as a safety lane when
retention must hold under a constrained link:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile lossy-400kib --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

## Stochastic fat-pipe recovery lane

Use the existing 100 Mbit/s, 150 ms, 30 ms-jitter Gilbert-Elliott preset when
recovery behavior is under test:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile hostile-fat-pipe --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

Unlike the historical `hostile` profile, this lane does not clamp a 500 ms
jitter distribution against a 300 ms mean delay. It remains stochastic and can
amplify small timing differences through its stateful loss stream, so do not use
it as the primary retention gate.

## Deterministic controller-retention lane

Use the fixed 100 Mbit/s, 150 ms shaped link for congestion-controller and
queue-growth changes:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile controller-fat-pipe --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

This lane retains the bandwidth-delay product and queue limit but has no
random loss or jitter. Calibrate it with `--same-binary-control` before relying
on a small delta, and follow retained recovery changes with the stochastic
`hostile-fat-pipe` and adversarial `hostile` lanes.

Use at least a 30-second measurement window for retention decisions. Shorter
windows can straddle only one phase of RTP's delay-drain/recovery cycle even on
fixed inputs; they are useful for smoke tests, not comparative conclusions.

Every timed run first drives the same live session for a five-second unmeasured
warmup (`--warmup-seconds`, set to `0` to reproduce the legacy boundary). The
runner records the warmup duration, while the probe subtracts warmup delivery
and clears setup/warmup RTP observations at the measurement boundary. Trace
schema 22 also subtracts the last pre-boundary RTP snapshot from every
connection-lifetime repair and controller counter. The baseline is sampled at
most one 50 ms observer interval before the boundary. Its presence is recorded
for each endpoint; a warmed schema-22 trace without both baselines is degraded.
This reduces startup-ramp bias; it does not remove thermal, power, scheduling,
or nonlinear time drift, so same-binary calibration remains required.

Trace schema 19 defines that measurement boundary: RTP rows captured during
setup or warmup are excluded, and paired comparison rejects runs with different
warmup durations. A missing duration in an older artifact means zero warmup.

Comparison schema 18 also interpolates `progress.csv` at the exact midpoint of
the measurement window and reports first-half and second-half application
goodput separately. Use the two phase rates to distinguish startup or
controller convergence from sustained behavior before interpreting a short
whole-window delta. The values are unavailable when progress samples do
not bracket the midpoint within one second; the analyzer never assumes a sparse
historical trace was stationary.

Comparison schema 19 keeps the familiar baseline-relative percentage and
metric-native difference, but ranks `largest_changes` and `agent_guidance`
with a separate signed two-sided percentage: `difference / max(abs(baseline),
abs(candidate))`. The ranking value is bounded to +100%, so a near-zero
baseline cannot outrank every other signal with an arbitrarily large ratio;
an actual appearance or disappearance still ranks as a full-scale change in
its pair. Guidance aggregates each metric across all valid pairs and ranks the
median bounded change. A transition isolated to one seed remains visible in
the pair table but cannot monopolize the five-item summary; guidance also
records pair count, changed-pair count, and directional consistency.

Comparison schema 20 adds `behavior_conditioned_observations`. For each
gentle-mode exit counter, it reports candidate-higher and candidate-lower
pairs separately, then summarizes goodput, second-half goodput, RTT, and
retransmission changes inside each group. This prevents event additions and
removals from cancelling each other and keeps a one-pair observation visible
as isolated support. Repeated, directionally consistent goodput shifts of at
least 10% are marked for attention. These are post-selection diagnostics, not
causal evidence; every hint retains its does_not_prove constraint.

Comparison schema 28 extends the normalized event metrics and hardens the
acceptance rules. Send-driver protocol-timer wakes are exported per GiB of
application data delivered for the sender and the peer
(`sender_protocol_timer_wakes_per_gib_delivered`,
`peer_protocol_timer_wakes_per_gib_delivered`); ACK-schedule signal wakes
(`ack_schedule_signal` in each `send_driver_wakes` breakdown) are scheduler
work that re-arms the driver's wait and are never counted as application
progress. Retransmission-armor duplicates are aggregated outside trace rows
and exposed raw plus per GiB for both endpoints
(`sender_retransmission_armor_duplicates[_per_gib_delivered]` and peer
equivalents); they count successful duplicate wire copies of an already
encoded recovery datagram and are distinct from retransmission attempts.
Application-data resume requests are normalized per GiB
(`sender_application_data_resume_requests_per_gib_delivered`), and raw plus
per-GiB sender/peer data-send WouldBlocks are exposed
(`sender_data_send_would_blocks[_per_gib_delivered]`,
`peer_data_send_would_blocks[_per_gib_delivered]`). A zero WouldBlock count
only means no exhausted underlay outcome was observed under this bounded
readiness policy; it does not prove the underlay never blocked. Event
normalization is rejected unless both a concrete counter and positive
delivered bytes exist, so absent schema fields stay `null` rather than being
misread as zero. Split-window goodput is computed at the exact measurement
midpoint only when progress samples bracket it; paired effects are ranked by
the bounded median across pairs rather than a single outlier, and every
guidance item retains its `does_not_prove` constraint.

The same schema promotes raw RTT p90 and p99 into paired metrics and the
run-health table alongside p50. These quantiles use the same
measurement-window samples as the empirical CDF, so an unchanged median cannot
hide an upper-tail regression. Quantiles do not establish distribution shape
nor cause; inspect the RTT histogram, CDF, and timeline when they disagree.

Current comparison schema 38 reports exact ACK-flush claims for both endpoints
by trigger ('initial', 'age', 'count', 'fin', and 'explicit'), including totals,
per-GiB rates, and each trigger's share. These are successful transactional
claims rather than schedule notifications, so notification coalescing
and superseded deadlines cannot masquerade as flush work. The trigger mix shows
which policy gate is active; it does not prove that changing that gate improves
delivery.

The comparison HTML also renders a baseline/candidate empirical RTT CDF for
each valid matched seed pair. These panels keep pair-local shoulders,
plateaus, and tail separation from being hidden by the global overlay.
Distribution shape guides follow-up timeline inspection; it does not establish
modality or cause.

## Clean lane

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile clean --mss-bytes 1400 --seeds 11,21 --window-seconds 10
```

`--link-profile` and `--mss-bytes` are identical across each pair and are
recorded in every manifest (`run.json`, both trace manifests, and the paired
`manifest.csv`), while the defaults remain hostile/8192.

## Direct lane

`--link-profile direct` bypasses NetemPair entirely: the client connects
straight to the probe server and the trace records zero-valued netem
placeholders, so artifacts stay schema-compatible while the measurement
isolates endpoint and host throughput from proxy overhead.

## Frozen executables and counterbalanced order

Both roles are prebuilt once, baseline before candidate
(`cargo test -j1 [--release] -p tests --test perf_probe --no-run
--message-format=json-render-diagnostics` per role, streamed to
`build-ROLE.log`) before any timed run. Every build runs with
`CARGO_TARGET_DIR` set to a canonical hash-suffixed directory beneath
`$TMPDIR` whose final path component is literally `target`, and with
`RUST_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER`, `RUSTC_WRAPPER`, and `RUSTFLAGS`
cleared so sccache or other inherited compiler wrappers or flags cannot
alter the build. The executable is parsed
only from `compiler-artifact` messages whose target is `perf_probe` with a
`test` kind. Immediately after each build — and before the next role builds —
the runner copies the artifact to a SHA-256-addressed sibling in the same
Cargo artifact directory (`perf_probe-<hash>.perf-loop-<sha256>`). This
prevents a shared target directory from overwriting the baseline executable
during the candidate build. Every seed invokes only that preserved
`perf_probe` executable directly, so no timed run compiles.

Each built role also writes a probe source manifest
(`{baseline,candidate}-probe-source.json`) binding the exact executable
SHA-256 to the workspace's exact component commit revisions. A prebuilt pair
runs only when both `--baseline-executable` and `--candidate-executable` are
supplied; paired `--baseline-source-manifest`/`--candidate-source-manifest`
are accepted only in that mode and must match the executable bytes on load. A
prebuilt binary without a matching source manifest runs only with its
execution workspace recorded as the explicit `execution_workspace_fallback`
component source.

Pair execution order alternates per seed-major pair (baseline/candidate then
candidate/baseline) to counterbalance first/second position across adjacent
two-pair blocks; stored rows keep their baseline/candidate labels, and
`run.json` records `pair_execution_order: "alternating"`, the `builds`, and
the per-run `executable` paths.

It also records `execution_order_analysis`, which converts each
candidate-minus-baseline goodput delta into a role-independent
later-minus-earlier delta. A consistent later-faster or later-slower result
marks the role comparison as directionally confounded and carries an explicit
`does_not_prove` guard; it does not guess whether warm-up, thermal/power state,
scheduling, or adjacent load caused the drift.

`counterbalanced_goodput_analysis` then converts each valid candidate/baseline
goodput ratio to log space. Within every complete adjacent AB/BA block it
reports the geometric candidate-role effect separately from the multiplicative
later-run position effect. Invalid, incomplete, and non-counterbalanced blocks
are excluded explicitly. This decomposition exactly cancels only a stable
multiplicative position effect inside the block; its `does_not_prove` guard
states that nonlinear drift, stochastic path divergence, role/order
interaction, and unrelated machine load remain possible. At least two complete
blocks (four valid pairs) are required for a classification.

When exact binaries are already preserved, bypass compilation with both
`--baseline-executable <path>` and `--candidate-executable <path>`. The runner
validates both executables and records their paths and SHA-256 digests as
prebuilt in `run.json`. Supply both flags or neither; the caller is responsible
for matching each binary to its workspace revisions.

A `--same-workspace-treatment` run builds and freezes exactly one executable
before any timed run (into `build-treatment.log` and
`output_root/frozen/treatment/...`) and reuses those exact bytes for both
roles, so baseline and candidate are never compiled separately; role-specific
source manifests are written against that one SHA-256 and the role hashes are
verified identical.

## FEC and retransmission-armor pairing

`--fec` and `--retransmission-armor` are paired run flags: when set, every
probe runs with `NETEM_PERF_FEC=1` and `RTP_RTX_DUP=1` respectively (0
otherwise). `--instream-group-fec` is the paired in-stream group FEC switch
(`NETEM_PERF_INSTREAM_GROUP_FEC=1`). All three values are recorded in
`run.json`, in every `manifest.csv` row, and in each `trace-<role>-<seed>`
trace manifest, so paired comparisons and archived traces stay
config-honest. The probe parses both FEC variables strictly as `0|1|false|true`
and passes FEC to both RTP endpoints.

## Same-executable FEC treatment

A treatment run compares one workspace with itself using ONE frozen
executable; only explicit runtime FEC settings may differ between the
baseline and candidate roles. `--same-workspace-treatment` requires
identical resolved workspaces, refuses to ride `--same-binary-control`,
rejects prebuilt role executables, and demands an explicit role difference:
`--candidate-fec on|off` moves only the candidate's runtime FEC setting
relative to the `--fec` baseline, and the comparator allowlist admits only
`--allow-config-mismatch` keys that actually differ (`fec`, never anything
else). The same-binary control remains a variance calibration and cannot
carry a treatment.

The runner builds and freezes the executable once before any timed run,
reuses those exact bytes for both roles, writes role-specific source
manifests (`{baseline,candidate}-probe-source.json`) against the same
SHA-256, fails if the role hashes differ, and continues the counterbalanced
role order and safe target/temp roots. Every build, run, and temporary
artifact stays beneath `~/code/tmp`, and both roles record exact executable
hashes, component revisions, scenario, FEC settings, and allowed mismatches.

Treatment contracts: never compile baseline and candidate separately
(different bytes would confound the runtime treatment), and do not use the
`clean` lane as the primary FEC-win lane (without erasures it measures
overhead, not recovery value).

### Stochastic interactive default-on validation

Validate the stochastic gaming lane with the default-on interactive FEC
policy:

```sh
./tools/perf-loop run --baseline . --candidate . --same-workspace-treatment \
--candidate-fec on --link-profile fec-gaming-fat-pipe --mss-bytes 1400 \
--scenario bulk --seeds 11,21 --window-seconds 30
```

### Same-workspace paired-saturated treatment

The paired-saturated lane keys loss to the same logical RTP sequence with
and without the 10-byte FEC data envelope, so the treatment isolates
recovery value rather than packetization. The deterministic erasure
complement is `--link-profile fec-recoverable-bottleneck`; the
hostile-steady bottleneck lanes `hostile-bottleneck-20ms`/`-100ms`/`-300ms`
add a rate-shaped 15% loss floor on top.

```sh
./tools/perf-loop run --baseline . --candidate . --same-workspace-treatment \
--candidate-fec on --link-profile fec-paired-saturated --mss-bytes 8192 \
--scenario bulk --seeds 11,21 --window-seconds 30
```

### 20/100/300 ms RTT lanes and sparse-message scenarios

Sparse-message calibration runs the message-latency probe on the periodic
bottleneck lanes; it sends 64-byte timestamped messages every 100 ms and
records delivery plus p50/p95/p99 latency. Copy-paste the 300 ms lane and
swap `hostile-periodic-bottleneck-300ms` for `-100ms` or `-20ms`:

```sh
./tools/perf-loop run --baseline . --candidate . --same-workspace-treatment \
--candidate-fec on --link-profile hostile-periodic-bottleneck-300ms \
--mss-bytes 1400 --scenario message-latency --seeds 11,21 --window-seconds 30
```

The comparison classifies message scenarios by the p95/p99 tail direction
together with wire-byte cost, so a near-zero goodput change is never treated
as neutral when latency or wire overhead moved materially.

### Reanalysis, exact evidence limits, and what to inspect

Re-run the analysis on a preserved treatment result without rerunning any
probe:

```sh
./tools/perf-loop analyze --result $TMPDIR/<run-output> [--update-run-json]
```

Before reading the verdict, inspect the trace health (`evidence_quality`),
the paired deltas, the one largest material change in `largest_changes`, and
`comparison.json`; every guidance item retains its `does_not_prove`
boundary and no hint identifies a branch as causal. The allowlist recorded
in `comparison.json` (`allowed_config_mismatches`) lists exactly the keys
that differed (`fec`), so a treatment can never paper over an accidental
`mss_bytes`, seed, link, or workspace difference.

## Same-binary control

`--same-binary-control` compares a workspace with itself (the only mode that
permits identical resolved workspaces; the resolved executable paths must
also match) to calibrate run-to-run variance. `run.json` then records
`control_calibration`, classified stable only when every absolute valid
paired goodput delta is below 10% AND the within-run phase analysis is not
`unstable_phase_drift` — with the median/maximum absolute delta and the
number of pairs that crossed the material-change threshold (false material
changes on an identical binary). `within_run_phase_analysis` is also written
into every `run.json`: it compares each run's first-half and second-half
goodput at the exact midpoint, marking a `material_phase_drift` whenever one
half differs from the other by at least `MATERIAL_PHASE_DRIFT_PERCENT`
(20%). A same-binary pair whose paired deltas are all under 10% is still
rejected as a control when either arm changed controller phase between its
halves, because the delta can no longer be attributed to a stationary
baseline. The same analysis includes `by_role.baseline` and
`by_role.candidate` summaries, which distinguish role-local convergence from
role-local instability without manually filtering individual runs. The global
classification stays conservative: material drift in either role makes the
evidence unstable. With `--fail-on-control-instability`, an unstable
calibration exits 4.

## Artifacts

Every output, temporary, trace, log, and Cargo target resolves beneath
`$TMPDIR`:

- `run.json` — top-level run record: `pair_execution_order` (`alternating`),
  role-independent `execution_order_analysis`, AB/BA
  `counterbalanced_goodput_analysis`, `within_run_phase_analysis`, `link_profile`,
  `mss_bytes`, workspaces, seeds, window, target directories, warmup duration,
  scenario, candidate FEC treatment, in-stream group FEC, retransmission-armor,
  same-workspace-treatment flag, `allowed_config_mismatches`, same-binary control
  flag, control calibration, per-role builds (frozen
  executable, SHA-256, built/prebuilt source, exact component revisions,
  component-revision source, source-manifest path, build log), per-role/seed
  runs (with the `executable`, per-role `fec`, and `scenario`) and their
  artifact paths, and the comparison verdict.
- `manifest.csv` — one row per role/seed probe: runner exit, role, Cargo
  profile, sorted component-revision JSON, seed, link profile, MSS,
  scenario, FEC, in-stream group FEC, candidate FEC treatment, resolved
  executable, trace dir.
- `trace-<role>-<seed>/` — the probe's trace directory (manifest/rtp/rtp_
  peer/netem/progress) with the link profile and MSS recorded in its
  manifest.
- `<role>-<seed>.log` — streamed probe log.
- `comparison.json` / `comparison.html` — schema-34 comparison and report.
- `{baseline,candidate}-probe-source.json` — probe source manifests binding
  each preserved executable's SHA-256 to exact component revisions.

Time-boxed goodput traces record `measurement_end_reason=timebox_elapsed`.
When the probe completed and all three endpoint outcome trackers are still
`running`, schema 16 defines that as an intentional live snapshot at the
measurement boundary. The comparator accepts that explicit lifecycle evidence
without requiring session-termination rows; early pump completion and missing
lifecycle evidence remain degraded or invalid as appropriate.

Netem counters are cumulative at each 50 ms sample. Comparison schema 4
reports the final snapshot for each direction; it never sums cumulative rows.

Comparison schema 5 converts raw RTP RTT observations from trace microseconds
to the documented millisecond report unit.

Trace schema 17 added the instantaneous number of application writers blocked
on RTP staging capacity plus cumulative accepted and waiter-suppressed
application-limited detections. Comparison schema 7 reports their final counts,
sampled waiting-writer occupancy, application-limited occupancy, and the
suppressed share of all classification attempts as first-class paired metrics.

Trace schema 18 adds cumulative retransmission attempts, first/repeat attempt
counts, every independent scheduler reason armed on selection, and tail probes.
Reason counts are intentionally non-exclusive: one repair can be both RTO- and
reorder-ready. Comparison summaries expose the final counters for each run.

Comparison schema 9 additionally reports total and repeat retransmission
attempts per GiB of application data delivered. These normalized rates are
diagnostic-only, do not affect the goodput verdict, and are `null` when no
application bytes were delivered.

Comparison schema 10 classifies RTT, low-send-rate occupancy, un-feedbacked
probe share, and normalized repair load as lower-is-better when producing
human/agent guidance; controller-state occupancy remains direction-neutral.

Comparison schema 11 adds a signed, metric-native difference beside every
relative delta. Changes from a zero baseline therefore remain visible in JSON,
HTML, and guidance instead of becoming an unranked `null` percentage; occupancy
differences are percentage points, while other metrics retain their named unit.

Comparison schema 12 promotes gentle draining, drain-floor binding, and outage
recovery occupancy to first-class paired metrics. Gentle draining and floor
binding remain direction-neutral diagnostics; outage recovery is lower-is-better.

Comparison schema 13 adds total gentle drain-guard exits across both RTP
endpoints. Older manifests without either typed-event counter remain `null`
rather than being misreported as zero.

Comparison schema 14 replaces that guard-only metric with four exact
gentle-mode exit causes: loss, clean gate reopening, ineffective drain guard,
and outage reset. Each paired metric sums both endpoints and remains `null`
unless both endpoint counters are present.

Comparison schema 15 recognizes trace schema 22 and checks that both RTP
endpoints captured the counter baseline required to make cumulative repair and
controller fields measurement-relative after a nonzero warmup.

Comparison schema 16 adds direction-neutral persistent-queue occupancy,
maximum continuous duration, and reset-count diagnostics from trace schema 23.
They distinguish a gate that never arms from one repeatedly broken by jitter;
none of the three is independently evidence of better or worse transport
behavior.

Comparison schema 17 adds `gentle_gate_open_streak_max_ms`, the longest
observed continuous run of gentle_probe controller actions. This is a sampled
lower bound on how long the clean-gate exit timer accumulated without reset,
not the controller's private timer value, and remains direction-neutral.

RTP metrics schema 17 adds send-driver wake-source events. The perf observer
counts resume notifications, pacing timers, protocol timers, and kill requests
without retaining a row for every wake. Measurement-boundary resets exclude
setup and warmup, and each endpoint's counts are written to `manifest.csv` and
the comparison run summaries. These counters identify what actually resumed
the driver; they do not infer why a notification was sent or whether a timer
was avoidable.

## RTP trace evidence

The 73-column RTP trace (schema 26) carries the complete
congestion-controller and retransmission-scheduler snapshot on every state
row; event-only raw RTP rows leave all snapshot columns empty. The
controller evidence:

- `congestion_control_rtt_us` — the control RTT last used by the controller;
  it paces probe spacing and the queue gate.
- `congestion_rtt_floor_us` / `congestion_queue_tolerance_us` — the
  controller's RTT floor and queue-gate tolerance: smoothed RTT above
  floor + tolerance is what marks `queue_building`.
- `congestion_persistent_queue_for_us` — how long the wider queue-growth
  signal has remained continuously armed at the current rate sample; an empty
  value means it is clear. `congestion_persistent_queue_resets` counts observed
  armed-to-clear transitions and is rebased at the measurement boundary. The
  comparison reports occupancy, maximum continuous duration, and resets as
  direction-neutral diagnostics.
- `congestion_delivery_peak_packets_per_second` /
  `congestion_drain_floor_packets_per_second` /
  `congestion_drain_target_packets_per_second` — the delivery peak feeding
  the drain floor, the floor itself, and the drain target the controller
  applies during gentle draining.
- `congestion_bandwidth_probe_increases` /
  `congestion_bandwidth_probe_decreases` / `congestion_delay_drains` —
  cumulative controller decision counters, rebased to the measurement boundary
  in trace schema 22.
- `rtp_gentle_mode_exit_{loss, gate_open, drain_guard, outage_reset}` and
  matching `rtp_peer_*` keys in `manifest.csv` — cumulative typed transitions
  that explain exactly why each gentle-mode episode ended. The capture
  aggregates and skips these rare events without storing another row or
  enlarging every state snapshot. Paired comparisons sum the two endpoint
  counters per cause, so a peer-only exit remains visible.
- `congestion_bandwidth_probe_before_feedback` — increases applied before a
  previous increase received feedback. This is a timing classification, not
  proof that a probe caused queue growth; the paired report only emits
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
- `retransmission_attempts`, `retransmission_first_attempts`,
  `retransmission_repeat_attempts` — cumulative scheduler-selected repairs,
  rebased to the measurement boundary in trace schema 22.
- `retransmission_rto_reason`, `retransmission_reorder_reason`,
  `retransmission_fast_loss_reason`, and `retransmission_pre_outage_reason` —
  independent reasons armed when each repair was selected; their sum can
  exceed `retransmission_attempts`.
- `tail_probe_attempts` — tail-loss probes emitted outside the ordinary
  retransmission-ready path.

### FEC treatment evidence

The typed FEC counters (`fec_parity_sent`, `fec_groups_flushed`, the four
`fec_flushed_groups_*` size buckets, `fec_groups_skipped_*` with their four
size buckets each, `fec_recovered_symbols`, `fec_dropped_malformed_packets`,
and `fec_dropped_decoder_panics`) are connection-lifetime counters rebased at
the measurement boundary. A lane without FEC records `None`, never a
fabricated zero, so a treatment with FEC on must show real `parity_sent`
work and, under erasure lanes, positive `recovered_symbols`. Sparse-message
lanes additionally carry `message_latency_p50_ms`/`p95`/`p99`,
`message_delivery_percent`, and `delivered_bytes`, with the netem
`forwarded_bytes`/`received_bytes` totals letting the comparison price the
wire cost per delivered byte. Inspect the trace health, the paired deltas,
and the one largest material change before trusting any `does_not_prove`-
bounded verdict.

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
  same-binary control and same-workspace-treatment modes;
  `--same-binary-control` and `--same-workspace-treatment` are the only
  modes that permit identical workspaces (the former for variance
  calibration, the latter for a single-binary FEC treatment), and
  `--same-binary-control` additionally requires the resolved executable
  paths to match. A mutable workspace must not be used as both roles, and
  debug/release evidence must not be mixed.
- A frozen workspace has a `netem_test/` checkout with sibling `rtp`, `mux`,
  `rtp_mux`, `tokio_udp`, and `udp_listener` repositories; each component's
  jj revision is recorded in the manifest.

## Re-running analysis on a preserved result

A finished run can be re-analyzed without rerunning any probe (preserved
probes are never re-executed during `analyze`):

```sh
./tools/perf-loop analyze --result $TMPDIR/<run-output> [--update-run-json]
```

`--result` must resolve beneath `$TMPDIR` and contain `run.json` plus
`comparison.json`; the subcommand recomputes `execution_order_analysis`,
`within_run_phase_analysis`, `counterbalanced_goodput_analysis`,
`comparison_readiness`, and (for a same-binary control run) the control
calibration, printing the report as JSON. `--update-run-json` writes the
recomputed analysis back into the preserved `run.json` atomically.

Readiness is a structural gate: healthy trace evidence, at least two complete
adjacent AB/BA blocks, and stable first/second-half behaviour for both roles
are required; execution-order association is a caution, not a blocker.
Readiness means the capture passed structural evidence checks and does_not_prove
causality, practical benefit, or the absence of an unmeasured regression.

## Sampling

Alongside the RTP traces, `samply` can create a one-shot profile of a
workload command and keep both the profile and its `syms.json` sidecar
beneath the tmp dir:

```sh
samply record --save-only --unstable-presymbolicate -- <workload command and its arguments>
```

Summarize the profile into deterministic owning-symbol hotspots:

```sh
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json --contains tokio --limit 20 --json
python3 tools/samply_hotspots.py tmp/dir/netem-samply-handoff/profile.json --contains tokio --thread tokio-rt-worker --limit 20 --json
```

Every schema-9 summary includes a whole-profile thread inventory before any
`--thread` filter is applied. It groups equal thread names, reports nonempty
and CPU-active sample counts plus total CPU delta, and ranks each name's
share of all CPU-active samples. Hotspot ranking is selected with
`--rank-by {inclusive,leaf}` (default `inclusive`): both the count-ranked
`hotspots` and the CPU-weighted `cpu_hotspots` sort by the selected primary
dimension, then the other dimension, then name. In CPU-active mode
(`--cpu-active-only`),
aligned nonnegative `threadCPUDelta` values are mandatory for every selected
thread and zero-delta samples are excluded; the summary returns both
count-ranked `hotspots` and CPU-ranked `cpu_hotspots` whose inclusive and
leaf ownership are weighted by the sample delta. A profile without valid CPU
deltas is an error, never a silent fallback to wall samples.
Inspect this inventory before filtering: proxy workloads use dedicated
`netem-c2s` and `netem-s2c` workers, so a Tokio-only summary does not cover the
packet-forwarding path.

When the profile also samples unrelated runtime threads, filter to the
workload-owning thread with the exact-name `--thread` option (repeatable;
all threads with any requested name are selected and a requested name that
matches nothing is an error). The workload-owning thread is required when
it avoids unrelated runtime samples.

When comparing workloads that differ, compare samples per forwarded packet
rather than raw sample counts. For sparse-message calibration lanes
(`--scenario message-latency`), compare samples per delivered message and
inspect p50/p95/p99 message latency and `message_delivery_percent` rather
than raw sample counts or near-zero goodput; a tail shift with a wire-byte
cost is material even when the delivered-byte rate barely moves.
