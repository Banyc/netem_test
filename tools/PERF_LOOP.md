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
mutable working copy. `--source-revision` (default `@-`) is resolved
independently in every component; repeat `--component-revision
COMPONENT=REVISION` to pin individual components (duplicates and unknown
component names are rejected). When supplied, `--output` must resolve
beneath `$TMPDIR`; otherwise the tool creates a unique safe-root output directory. `suite-revisions.json` records the manifest schema
(`PROBE_SOURCE_MANIFEST_SCHEMA`), the source workspace, the requested
revision, `component_revision_overrides`, and each component's exact
40-character `commit_id` plus `change_id`. Paired runs read this manifest so
archived trees remain revision-identifiable even though they are
deliberately not mutable JJ workspaces.

A component's committed manifests name its siblings through their published
git tags, so an exported tree compiled as committed would build the *tagged*
sibling rather than the sibling exported beside it and a component pin would
select no code. `snapshot` therefore rewrites each inter-component source
locator in the frozen manifests to the exported sibling's relative path: the
tagged sibling is replaced by `path = "../<component>"` (or
`../netem_test/netem-test` for the harness crate). The component trees stay
byte-exact exports of their committed revisions; only the frozen build recipe
changes, and each rewrite is recorded in `suite-revisions.json` under
`frozen_dep_rewrites` as `{manifest, table, crate, from: {git, tag}, to:
{path}}`. A dependency edge naming a suite component in a shape the rewrite
does not model - an unmodelled dependency table, a registry or
workspace-inherited source, a renamed crate, a fork, or a `[patch]` entry
onto a git repository - fails the snapshot instead of silently resolving from
a tag. `run` refuses a frozen suite whose sibling edges still resolve from
git tags, so a snapshot taken before the rewrite cannot report a comparison
of the tag against itself.

## Default hostile command

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile hostile --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

The historical/default scenario is hostile with MSS 8192
(`--link-profile hostile --mss-bytes 8192`).

**The `hostile` lane is diagnostic-only: report its numbers, never
retain/reject on it.** In every one of the 70 recorded `hostile` lane runs — at
both the 5 s and the 20 s warmup — the lane was `not_ready`
(`within_run_phase_not_stable`), and the spread was the lane's own stochastic
first/second-half goodput phase variance rather than a candidate effect (the
baseline and candidate trees were byte-identical in the control). It remains
useful as a diagnostic, but it cannot attribute a delta to a candidate. A run
records the lane's role as `link_role` (`verdict` or `diagnostic`) in
`run.json`; the full role table lives in `tests/GATE.md` (`gate-lane-roles`) and
is machine-checked by `python3 tools/check-gate.py` against
`perf_loop.lane_classification`.

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

## Deterministic iid-loss fat-pipe lane

Use the fixed 100 Mbit/s, 150 ms shaped fat pipe with a fixed-seed ~1 %
independent per-packet loss when loss-recovery behaviour must be compared
without the stochastic Gilbert-Elliott state stream of `hostile-fat-pipe`:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile deterministic-iid-loss-fat-pipe --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

Like the `controller-fat-pipe` lane this keeps the bandwidth-delay product and
the 16k-packet queue limit, and adds only a seeded iid loss: the exact drop
pattern is reproducible from the seed, so a paired run no longer amplifies
small timing differences through an evolving loss state. It still exercises
the recovery path (retransmit reasons, app/wire ratio, and recovery idle
time), but it is a deterministic lane rather than a retention gate; follow a
retained recovery change with the stochastic `hostile-fat-pipe` and
adversarial `hostile` lanes.

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

## Impairment-regime lanes

Two lanes reach the jitter and thin-link regimes the shaped, zero-jitter lanes
cannot. They are defined in `netem-test/src/kit/presets.rs`, measured by
`tests/tests/lane_regime_coverage.rs`, and selectable by `--link-profile`:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile jittery-short-rtt --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile high-rtt-low-rate-bottleneck --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30
```

The name is carried by `perf_loop.LINK_PROFILES` and by the probe's
`NETEM_PERF_LINK_PROFILE` allowlist and match arm
(`rtp_mux/tests/perf_probe.rs`); both are what make a profile selectable by
`--link-profile`, and a name in one list but not the other is rejected by the
probe. Both regime lanes are diagnostic-only (`gate-lane-roles` in
`tests/GATE.md`): `jittery-short-rtt` because an unshaped lane's goodput is
host-limited rather than link-limited, and `high-rtt-low-rate-bottleneck`
because its transport session deterministically tears down about 36 s into a
run (see below).

### `jittery_short_rtt_link`

Unshaped, 20 ms one-way, +/-15 ms uniform per-packet jitter, 1024-packet queue,
no loss, seed 4. A round trip is triangular on `[10 ms, 70 ms]`, mean 40 ms.

- **What it can see.** The reorder-vs-fast-loss decision, and the jitter half
  of `rtp`'s fast-loss arming gate (`RtxTimer::fast_loss_armed`, i.e.
  `4 * rttvar < srtt / 4`). Measured on 200 echoes: 141-145 inverted
  deliveries, `rttvar` 7.4-8.7 ms, `4 * rttvar` 29.7-34.8 ms against
  `srtt / 4` 12.4-12.5 ms, so the gate is **unarmed** — a verdict the two
  battery lanes cannot produce, since both measured `rttvar` below 0.5 ms and
  `4 * rttvar` below 2.0 ms against `srtt / 4` above 75 ms. The same samples
  also separate a gate that drops the RFC 6298 `K` factor (`rttvar <
  srtt / 4`): that variant arms here (7.4 < 12.4) while the real gate does not,
  and on both battery lanes every variant arms, so those lanes cannot tell the
  two apart.
- **What it cannot see.** Rate-shaped throughput behaviour (no rate is
  configured), loss recovery (no loss), any queue-limit regime (35 ms of
  delay never fills the queue), and a goodput verdict: with no rate the link
  is host-limited, so the goodput is not phase-stable enough to attribute a
  delta to a candidate.
- **Battery-measured shape.** A four-seed same-binary control (30 s window,
  MSS 8192) measured RTT p50 36.8 ms, p90 54.0 ms, p99 65.1 ms, max 75.7 ms
  over 21,433 samples; the RTO stayed on the 1 s `MIN_RTO` floor for all
  4,771 samples (the raw `srtt + 4 * rttvar` is below the floor, so the
  fast-loss gate's disarm is not visible in the RTO); the sender armed its
  reorder deadline 132,403 times (`retransmission_reorder_reason`) against
  14,387 fast-loss arms. The netem `reordered` counter stayed 0: it records
  the gap-based reorder *feature*, not jitter-induced inversions.
- **Its control is unstable.** Two independent four-seed controls both returned
  `mixed_results` (`not_ready`, `within_run_phase_not_stable`), with per-pair
  goodput moving 11–14 % in one and up to 32 % in the other, so the lane is
  diagnostic-only.
- **Why it must leave `rate` unset.** With a rate and no reorder gap, the
  send-time shaper schedules each packet at `max(now + delay, previous_send) +
  serialization`, so the deadlines are monotone and the lane cannot reorder
  however much jitter it configures. The same delay sampling with a 100 Mbit/s
  rate measured 0 inverted deliveries and a 6.5 ms interquartile round-trip
  spread, against 141-145 inversions and 15-17 ms unshaped.

### `high_rtt_low_rate_bottleneck`

200 kbit/s, 400 ms one-way, 128-packet queue, no loss, jitter, or reordering,
seed 4.

- **What it can see.** RFC 6298 RTO growth into the tens of seconds from the
  link alone. `serialization_delay(8192, 200_000) = 327.68 ms` per queued
  datagram, so a full queue holds `128 * 327.68 ms = 41.9 s` on top of the
  800 ms round-trip floor. Measured on a 40-datagram burst: RTT p50 7.96 s,
  max 14.14 s, `srtt` 11.88 s, max raw RTO **22.18 s**.
- **What it cannot see.** Jitter or reordering (none configured) and
  production goodput (the link delivers 25 KB/s).
- **Battery-measured shape.** A four-seed same-binary control (30 s window,
  MSS 8192) measured RTT p50 19.27 s, p90 30.11 s, p99 32.35 s, max 32.70 s;
  the RFC 6298 RTO p50 28.09 s and max **40.70 s** (0 of 3,294 samples on the
  floor), with a 17.14 s maximum packet RTO overdue. This is the
  tens-of-seconds RTO read from the transport's own trace, not the estimator
  arithmetic.
- **Its session deterministically tears down about 36 s into a run, so it is
  diagnostic-only.** The 200 kbit/s direction's 128-packet queue is already at
  its limit at the first sample of a run (the client offers ~27–30 packets per
  second against a 3.05 pkt/s drain), and from then on its tail-drop discards
  every packet the client sends — *including the ACKs the server is waiting
  for*. Over 32 recorded 30 s-window runs, `forwarded_bytes / forwarded` on
  that direction is 8191.0 in 29 and 8111.6 in the other three — a single small
  packet slips through in those — while the arrival mix (`received_bytes /
  received` ~ 7.4 kB) implies ~72–100 ACK packets per window. The queue head
  still delivers data, so the server keeps receiving (its
  `next_receive_sequence` reaches 105–111) while its peer-liveness
  `no_response` watchdog — refreshed only by a peer ACK (`pkt_send_space.rs`,
  `if !peer_response { return; }`) — counts down from the last ACK that got
  through, ~5–6 s into the run. It fires at `min_no_response` = 30 s, i.e. at
  t ~ 35.0–35.9 s, and terminates the session with
  `proactive_stall`/`no_response`/`broken_pipe`; the mux sink then reads
  `read_error/BrokenPipe` and its `delivered` counter freezes. Eight runs at a
  20 s warmup all terminated inside a 0.85 s band, and the teardown deadline
  reconstructed from a 5 s-warmup run falls in the same band, so it is a
  wall-clock event and not load-dependent.
- **The `verdict` classification was a sub-second knife-edge on the warmup.**
  The four-seed control recorded when the lane landed used the 5 s default
  warmup (30 s window), so each run ended at t = 35.0 s and every teardown
  deadline fell 0.0–0.9 s *after* it: the lane reported `ready` with
  `no_material_change`, a 0.006 % median absolute goodput delta, no false
  material change, and stable phase, with all eight sinks still `running`.
  Re-run with one second more warmup (6 s, same 30 s window, same seeds) every
  deadline falls 0.1–1.0 s *inside* the window, five of the eight sinks end in
  `read_error/BrokenPipe`, and the lane is `not_ready` on
  `trace_evidence_not_healthy` alone — with its goodput axis still `stable`
  (3.7 % median) and its phase still `stable`. At a 20 s warmup the stall is
  mid-window and the lane is additionally `not_ready`
  (`within_run_phase_not_stable`) with an `unstable` `control_calibration`
  (3.85 % median absolute goodput delta): all eight runs deliver ~384 KiB at
  the link rate in the window's first half and then stall, six of them
  delivering nothing in the second half. The lane's latency axis stayed quiet
  (worst per-percentile median 0.91 %, worst single pair 1.31 %), so no window
  can carry a goodput verdict on it: the teardown time is set by the same
  early ACK/queue dynamics a candidate can move, and the whole margin between
  a usable window and a broken one is one second. Its RTO regime is real and
  its numbers stay useful as a diagnostic, and
  `lane_regime_coverage::high_rtt_low_rate_lane_reaches_a_tens_of_seconds_rto_the_battery_lanes_cannot`
  still measures that regime in the `standard` tier.
- **A long enough run fails the probe outright.** The client's application
  write does not observe the teardown until ~17 s after it (measured at
  t ~ 53.4 s, with the client's own last peer response at t ~ 36.2 s and its
  mux session ending `io_reader/TimedOut`), so a run that lasts past t ~ 53 s —
  a 5 s warmup with a 60 s window, for example — ends in
  `bulk pump failed: Kind(BrokenPipe)` and a probe panic (exit 101), and the
  comparison evidence is `invalid` rather than merely `degraded`. The lane
  cannot carry a verdict at any window length that reaches the stall.
- **The negative control.** The `controller-fat-pipe` lane fed the identical
  burst kept its round trip at 302 ms and its RTO on the 1 s `MIN_RTO` floor
  for all 40 samples, so the same estimator arithmetic is unobservable there.

### Measured blind spots of the shaped verdict lanes

On 200 echoes through `controller-fat-pipe` and
`deterministic-iid-loss-fat-pipe`:

- neither lane reordered (0 inverted deliveries of 200) — with zero jitter the
  deadlines are monotonic, and a configured rate's shaper would monotone them
  anyway;
- neither lane's round trip left the 300 ms floor (max 301.6 ms and 304.0 ms);
- the whole `srtt + 4 * rttvar` expression stayed below the 1 s `MIN_RTO`
  floor (max raw RTO 902 ms), so every RTO sample those lanes can produce is
  the floor itself: the expression is unobservable there.

## Frozen executables and counterbalanced order

Both roles are prebuilt once, baseline before candidate
(`cargo test -j1 [--release] -p rtp_mux --test perf_probe --no-run
--message-format=json-render-diagnostics` per role, streamed to
`build-ROLE.log`) before any timed run. The build runs in the suite component
that owns the probe's code — the exported `rtp_mux` tree beside the netem_test
workspace, so a `--component-revision rtp_mux=<commit>` pin selects exactly the
probe that runs; a component that does not carry `tests/perf_probe.rs` is
refused instead of silently building another tree's probe. Every build runs
with `CARGO_TARGET_DIR` set to a canonical hash-suffixed directory beneath
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

## Deterministic-lane asserting gates

Two opt-in asserting flags (recorded in `tests/GATE.md`) turn the
controller-fat-pipe midpoint phase analysis and the wakes-per-GiB counters
into hard failures instead of report-only evidence:

```sh
./tools/perf-loop run --baseline <workspace>/netem_test --candidate . \
--link-profile controller-fat-pipe --mss-bytes 8192 \
--seeds 11,21 --window-seconds 30 --fail-on-phase-drift \
--fail-on-wakes-cap 100000
```

`--fail-on-phase-drift` (also on `analyze`) exits 2 when the capture is
`not_ready` with `within_run_phase_not_stable` — the first half's goodput
moving >= 20 % from the second half's at the exact measurement midpoint. An
arm that is not `ready` is inconclusive, never a pass, and on the
deterministic lane that inconclusive drift is a controller/queue defect.

`--fail-on-wakes-cap WAKES_PER_GIB` exits 2 when any valid pair's
per-endpoint protocol-timer wakes per GiB of delivered bytes exceed the cap.
On the link-shaped controller-fat-pipe lane the delivered rate is fixed
(~12 MiB/s), so wakes/GiB is a fixed ratio: measured 0 sender / ~21.5k peer
on a 30 s window, and 100k/GiB leaves ~4.7x margin. Absent values stay `null`
and never fail.

Both flags are opt-in and meant for the deterministic lane; stochastic lanes
(whose phase drift is expected) must not be run with `--fail-on-phase-drift`.

## Same-binary control

`--same-binary-control` compares a workspace with itself (the only mode that
permits identical resolved workspaces; the resolved executable paths must
also match) to calibrate run-to-run variance. `run.json` then records
`control_calibration`, classified stable only when every absolute valid
paired goodput delta is below 10%, the comparison's `latency_direction` is
not material, AND the within-run phase analysis is not
`unstable_phase_drift` — with the median/maximum absolute delta, the number
of pairs that crossed the material-change threshold (false material changes
on an identical binary), and the recorded `latency_direction` /
`latency_material` pair. An artifact written before the latency axis existed
carries no `latency_direction` and is not treated as material.
`within_run_phase_analysis` is also written
into every `run.json`: it compares each run's first-half and second-half
goodput at the exact midpoint, marking a `material_phase_drift` whenever one
half differs from the other by at least `MATERIAL_PHASE_DRIFT_PERCENT`
(20%). Only runs whose two halves both delivered a positive rate enter the
analysis: a run whose second half delivered nothing at all is dropped rather
than scored as maximal drift, because on a bounded-transfer lane a finished
probe and a stalled transport are indistinguishable from the half totals
alone. The role summaries can therefore carry fewer runs than the pair set,
and a pair set whose every run stalls in its second half reaches
`insufficient_evidence` instead of `unstable_phase_drift`; the dead half is
still reported by the trace-health check that feeds `comparison_readiness`,
because a bulk run whose sink ends in `read_error/BrokenPipe` has no
`running` endpoint outcome and fails `endpoint_lifecycle_accounted`. A
same-binary pair whose paired deltas are all under 10% is still
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
- `graphs/g*.svg` and `graphs/g*.png` — the verified rendered graph panels
  produced from `comparison.html` by `tools/render_graph.py` (see "Rendered
  graph evidence (mandatory)"). Missing or data-free panels are an error.
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
`forwarded_bytes` totals pricing the wire cost per delivered byte:
`udp_payload_bytes_per_delivered_byte` is the measured UDP-payload total over
the window's delivered bytes, split into
`forward_udp_payload_bytes_per_delivered_byte` and
`reverse_udp_payload_bytes_per_delivered_byte` with
`reverse_udp_payload_share_percent` naming how much of it is the reverse
direction's acknowledgement traffic, and
`derived_ipv4_udp_header_bytes_per_delivered_byte` adding the 28-byte IPv4+UDP
header each forwarded datagram costs on the wire as an estimate.
`netem_forwarded_bytes_window_relative` says whether those counters were
rebased to the measurement boundary, and `wire_window_source` names the
denominator's span. Both sides of every wire ratio are span-matched to the
netem tick window, which removes a bias of up to one tick; the residual
run-to-run spread of the ratio within one lane is 0.010-0.101 pp on the
`controller-fat-pipe` lanes and 1.208-1.881 pp on the
`deterministic-iid-loss-fat-pipe` lanes, so a delta below roughly 0.1 pp on
`controller-fat-pipe` and 1 pp on `deterministic-iid-loss-fat-pipe` is not
signal. Inspect the trace health, the paired deltas,
and the one largest material change before trusting any `does_not_prove`-
bounded verdict.

## Rendered graph evidence (mandatory)

The comparison HTML carries the graph evidence a reader must inspect before
accepting a verdict: one `<svg>` panel per chart (rolling application goodput,
the global raw-RTT CDF, and one baseline/candidate raw-RTT CDF per valid seed
pair). Rendering those panels is a mandatory step of the capture loop, and it
is performed by `tools/render_graph.py`:

```sh
python3 tools/render_graph.py $TMPDIR/<run-output>/comparison.html \
  --out $TMPDIR/<run-output>/graphs
```

The tool extracts every `<svg>` panel, asserts that at least one panel was
produced, asserts that every panel carries series data (a polyline with at
least two points, a non-empty `<path>`, or a drawable bar rect - one that is
not the plot background and carries a positive, numeric width and height - an
empty chart with only axes and a background is rejected), writes each panel as
a standalone SVG, and prints the panel count. A panel spans from its own
opening tag to its own close tag: when a close is removed mid-document, the
next panel's close must not be allowed to terminate it, so the damaged panel
is reported as truncated instead of being emitted as a concatenation of two
charts. **A graph that cannot be produced is an error, not an empty file to
skim past:** with no `<svg>` panel, a truncated or concatenated panel, or a
missing/empty input, the tool exits non-zero and names the problem instead of
reporting success.

Rasterization to PNG needs an external headless browser (Chrome/Chromium). The
SVG extraction and verification run fully in-repo and do not need one. When
rasterization is requested (the default) and no browser can be found, the PNG
step **fails loudly** with a non-zero exit and a message naming the missing
browser, after the SVGs have already been written and verified; pass
`--browser <path>` or set `NETEM_RENDER_BROWSER` to point at one, or pass
`--no-rasterize` to accept SVG-only evidence explicitly. When a browser is used,
every produced PNG is verified to be a real, non-degenerate PNG before the tool
reports success.

Do **not** rely on the old manual `render-graph.sh` that lived outside the
repository in an unversioned scratch directory: it degraded silently - with no
`<svg>` in the
HTML the `g*.svg` glob stayed literal, Chrome rasterized the literal name into
a blank PNG, and the script still exited zero while printing `svgs: N` without
checking `N > 0`. The in-repo tool replaces it, and its checks are exercised by
`python3 -m pytest tools/ -q`.

## Verdicts and exit codes

The `perf-loop` subcommands:

- `0` — the paired run completed and the comparison produced valid paired
  evidence (a verdict other than `insufficient_evidence`).
- `2` — probe/evidence failure: any probe exited non-zero, the comparison
  evidence is invalid, the comparison wrote no `comparison.json`, the
  comparison tool itself failed, or no valid paired evidence remains
  (verdict `insufficient_evidence`, including zero seed pairs or a trace
  excluded for a malformed CSV field).
- `3` — only with `--fail-on-regression` and a `likely_regression` or
  `latency_regression` verdict.
- `4` — only with `--fail-on-control-instability` and an unstable
  same-binary `control_calibration`.

The raw comparison tool (`tools/rtp_trace_compare.py`) carries the same
evidence contract when invoked directly: it exits `0` only when it produced a
comparison that can support a verdict, and exits `2` with a message naming the
problem when the comparison cannot produce one (overall `evidence_quality`
`invalid`, or verdict `insufficient_evidence`). The artifacts are still
written on the failing path so a caller can inspect them; pass `--report-only`
to accept those artifacts and exit `0` anyway. `perf_loop.call_compare` always
passes `--report-only` because the loop inspects the written `comparison.json`
and enforces (and names) the evidence contract itself, so its own exit codes
and reasons are unchanged.

A bulk pair set is classified on two axes, goodput and latency. Goodput
keeps the paired 10% rule. Latency is the consensus of the RTT p50/p90/p99
percentiles, each material only when its median paired movement is at least
5% *and* at least 1 ms; the 1 ms floor is what keeps the `clean` lane's
sub-millisecond scheduler jitter out of the verdict. A movement on only the
latency axis is `latency_improvement` or `latency_regression`, never
`no_material_change`; a pair that improved on one axis while regressing on
the other is `mixed_results`; and a pair set with no RTT percentile falls
back to the goodput-only verdict.

The verdict is a consistency label over valid seed-paired evidence only; it
is not statistical confidence or causality. Every agent hint carries a
`does_not_prove` constraint and no hint identifies a specific branch as
causal. The verdict of a **diagnostic** lane (recorded as `link_role =
"diagnostic"` in `run.json`) is reported for diagnosis only: a change must
never be retained or rejected on it. This is the lane role, not the
`NETEM_PERF_DIAGNOSTIC_MODE` evidence-collection bypass below.

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
  jj revision is recorded in the manifest. The probe's code is an `rtp_mux`
  test target, so the `rtp_mux` sibling is the component the
  `--component-revision rtp_mux=<commit>` pin selects and the one the probe is
  compiled from.

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
