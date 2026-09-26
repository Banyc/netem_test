# The pending `rtp_mux` perf declaration

`rtp_mux` owns perf tests but has no perf declaration, so the dual mandate is
unenforced there. This file is the exact set of blocks that crate's `GATE.md`
needs: the `gate-perf-design` rows, the `gate-budgets` block and the
`gate-coverage-gaps` lines. Every row also declares its relation to the
baseline (see "The relation each row declares" below). It is a draft, not an
authority — the numbers become `rtp_mux`'s when that crate's iteration lands
them in its own `GATE.md`, where `python3 ../netem_test/tools/check-gate.py
--crate . rtp_mux tests GATE.md` will enforce them.

**Nothing here retunes an arm, threshold, window, cadence or tier.** The rows
only name tests that already exist, the tier each already has in
`rtp_mux/GATE.md`, the cells each already covers, and now the dimensions each
varies from the stated baseline.

Derived from the landed `crates/rtp_mux` tree at commit
`5c3ab564ecd1a036347e75aae508e7ca2fced41f` (`dev`, "test(rtp_mux): measure the
interactive lane's per-flow fairness"), read without running any `rtp_mux`
test. The form and the checker's failure modes are in `tools/PERF_INFRA.md`
("The perf-test dual mandate (time and coverage)").

## The rule that selects a row

A test is a row here when it exists to **measure a perf quantity** — latency,
goodput, capacity fraction, own-wire, per-flow fairness, throughput, or an
instrument-sanity property a measurement depends on. The 94 rows below cover
the eleven measurement targets: `mandate_smoke`, `rtp_mux_jitter`,
`dual_lane_mandates`, `hol_probe`, `hol_verify4`, `contested_latency`,
`perf_probe`, `mux_ceiling_probe`, `mux_over_rtp_perf`, `mux_stream_fairness`
and `rtp_longrun`.

Twelve targets are **not** perf rows — correctness scenarios and unit targets
whose cost the perf budget does not pay for. Named here so the exclusion is a
record and not an omission:

- `bidirectional` (5 tests), `duplex` (1), `explorer` (2), `lane_rejection`
  (7), `session_stats` (1), `xsession` (1), `bind_race` (1) — payload-integrity
  and binding-correctness unit/scenario targets, no perf measurement.
- `mux_over_rtp` (2), `rtp_and_mux` (3), `rtp_mux` (6) — echo/reliability
  scenarios; they assert correctness on engineered links and report no perf
  quantity the dual mandate governs.
- `dynamic_contested` (12) — scheduling-decision scenarios: they assert which
  lane a transfer takes, not how fast it went.
- `mux_bulk_clean_stall` (4) — stall/watchdog and backpressure correctness.

## Costs

A cost is a number only where a document already records one; the rest are
`TBD` and need one measurement each (the row's own command: the tier's
`--ignored` invocation for `standard`/`full`/`perf`, a plain
`cargo test --release -p rtp_mux --test <target> -- --exact <test>` for
`default`). Sources used:

- **`#[ignore]` reason strings** in the landed sources, which state the arm
  count and the per-arm wall-clock: the cost is their product, e.g.
  `tests/rtp_mux_jitter.rs:1370` says "eight ~35 s arms" → `280` s.
  `tests/dual_lane_mandates.rs` says "three 15 s dual-lane saturated runs" →
  `45` s.
- **`rtp_mux/GATE.md`** ("Tiers") records the constitution gate as "a ~40 s
  wall-clock dual-lane run" → `40` s.
- **`mandate_smoke`'s four rows** are the per-test measurements of one
  `tools/mandate-check` full run (release, warm build), read from that report's
  `timings.tests` (schema `mandate-check/2`, see `tools/MANDATE_SMOKE.md`).
  Re-measure them on the landed revision: they are one sample, and
  `tools/mandate-check` is what a `rtp_mux` change runs anyway.

## The relation each row declares

Every row carries its relation to the baseline named in `gate-budgets`
(`mandate_smoke::m1_interactive_tail_latency`): `baseline` for that row itself,
`orthogonal` when the row's cells vary exactly one dimension from it,
`composite(<dimension>[,<dimension>…])` when they vary several, and
`re-measurement(<reason>)` when they vary none. `check-gate.py` derives the
varied dimensions from the row's own cells — a dimension whose value differs
from the baseline's, or that the baseline does not state at all (a dimension
the row does not name is inherited) — and refuses a label that disagrees with
that derivation, so these are not a judgement call. Against that baseline the
94 rows are **1 baseline**, **2 orthogonal**
(`mandate_smoke::m2_interactive_delivery_and_wire`, which varies only `metric`,
and `rtp_mux_jitter::jitter_burst_loss_arms`, which varies only `impairment`),
**91 composite**, and no re-measurement — nothing here repeats the baseline's
cell, so no row is labelled one rather than guessed at.

That split is the finding, not a re-cut of the arms. The set is anchored on one
M1-clean cadence cell, and every other family moves several of the axes its own
cell names: an M3/M4 row moves `lane` with `rate`/`flows` and `metric`; a
`perf_probe` or `mux_ceiling_probe` row moves `layer`, `shape`, `scale` and
`metric`; a `hol_probe` row moves `latency`, `loss`, `bulk` and often
`variant`. Read against that one baseline, a result on almost any row here
cannot be attributed to a single dimension, and the declaration now says so
instead of leaving it to prose. Nothing here needs its windows, cadences or
tiers retuned to fix that: an attributable arm for a family is a **new** arm
stated one axis away from a reference, and that is a change to the tests, not
to these labels. Nothing the draft cannot determine was guessed: the labels are
the checker's derivation, and the `TBD` costs stay `TBD`.

## The blocks to paste into `crates/rtp_mux/GATE.md`

```gate-perf-design
mandate_smoke::m1_interactive_tail_latency = default | 50.0 | baseline | baseline@impairment=clean2pct-iid+latency=25ms+jitter=5ms+lane=dual+shape=cadence+flows=1+scale=256B+metric=p99
mandate_smoke::m2_interactive_delivery_and_wire = default | 54.1 | orthogonal | M2@impairment=clean2pct-iid+latency=25ms+jitter=5ms+lane=dual+shape=cadence+flows=1+metric=own-wire
mandate_smoke::m3_bulk_goodput_fraction = default | 61.6 | composite(lane,metric,rate,scale) | M3@lane=bulk+rate=1MiBps+scale=2MiB+metric=capacity-fraction
mandate_smoke::m4_interactive_lane_fairness = default | 31.2 | composite(arm-set,flows,metric) | M4@lane=dual+flows=4+arm-set=clean-and-hostile+metric=per-flow-share
rtp_mux_jitter::jitter_duallane_constitution_gate = default | 40 | composite(arm-set,metric) | M2@lane=dual+shape=cadence+arm-set=clean-and-hostile+metric=own-wire-budget
rtp_mux_jitter::jitter_duallane_constitution_gate_p99 = full | 105 | composite(arm-set,metric) | M1@lane=dual+shape=cadence+arm-set=clean-and-hostile+metric=p99-median-of-3
rtp_mux_jitter::jitter_decomposition = perf | 280 | composite(arms,impairment,load,metric) | loss-vs-queue@impairment=loss2pct-iid+jitter=5ms+load=bulk-burst+arms=solo-loss-bulk-combined+metric=p99-decomposition
rtp_mux_jitter::jitter_frame_reorder_decomposition = perf | 140 | composite(impairment,layer,load,metric,reorder) | frame-reorder@layer=rtp-frame+reorder=receiver-fast-forward+impairment=loss2pct-iid+load=bulk-burst+metric=p99-decomposition
rtp_mux_jitter::jitter_frame_reorder_fec_arms = perf | 210 | composite(fec,impairment,layer,reorder) | frame-reorder-fec@layer=rtp-frame+reorder=fast-forward+fec=on+impairment=loss2pct-iid
rtp_mux_jitter::jitter_frame_reorder_fec_bulk_loss_reorder = perf | 140 | composite(fec,impairment,layer,load,reorder) | frame-reorder-fec@layer=rtp-frame+reorder=fast-forward+fec=on+load=bulk+impairment=loss2pct-iid
rtp_mux_jitter::jitter_fec_arms_2pct = perf | 175 | composite(fec,impairment,metric) | fec-tuning@impairment=loss2pct-iid+fec=off-stock-prompt+metric=parity-and-latency
rtp_mux_jitter::jitter_fec_arms_6pct = perf | 175 | composite(fec,impairment,metric) | fec-tuning@impairment=loss6pct-iid+fec=off-stock-prompt+metric=parity-and-latency
rtp_mux_jitter::jitter_nonloss_impairments = perf | 210 | composite(impairment,loss) | non-loss-impairment@impairment=jitter-reorder-dup-rate+loss=none+metric=p99
rtp_mux_jitter::jitter_reorder_rate_curve = perf | 140 | composite(impairment,rate) | reorder-rate@impairment=reorder+rate=curve+metric=p99
rtp_mux_jitter::jitter_reorder_direction = perf | 70 | composite(direction,impairment) | reorder-direction@impairment=reorder+direction=c2s-and-s2c+metric=p99
rtp_mux_jitter::jitter_interactive_solo = perf | 35 | composite(impairment,lane) | M1@lane=interactive+flows=1+impairment=loss2pct-iid+jitter=5ms+shape=cadence
rtp_mux_jitter::jitter_interactive_with_loss = perf | 35 | composite(impairment,lane) | M1@lane=interactive+flows=1+impairment=loss2pct-iid
rtp_mux_jitter::jitter_interactive_with_bulk = perf | 130 | composite(impairment,lane,load) | M1@lane=interactive+flows=1+load=bulk-burst+impairment=loss2pct-iid
rtp_mux_jitter::jitter_interactive_bulk_and_loss = perf | 35 | composite(impairment,lane,load,metric) | M2@lane=interactive+flows=1+load=bulk-burst+impairment=loss2pct-iid+metric=own-wire
rtp_mux_jitter::jitter_duallane_arms = perf | 280 | composite(load,reorder) | dual-lane-matched-load@lane=dual+load=bulk-matched+reorder=fast-forward-and-strict
rtp_mux_jitter::jitter_burst_loss_arms = perf | 420 | orthogonal | M1@impairment=gilbert-elliott-burst+jitter=5ms+lane=dual+metric=p99
rtp_mux_jitter::jitter_request_response_arms = perf | 1170 | composite(depth,impairment,jitter,shape) | M1@shape=request-response+depth=1+impairment=loss5pct-ge+jitter=100ms+metric=p99
rtp_mux_jitter::jitter_latency_dimension_arms = perf | 385 | composite(impairment,latency) | M1@latency=sweep+impairment=loss2pct-iid+metric=p99
rtp_mux_jitter::jitter_cellular_timeline_arms = perf | 70 | composite(impairment,jitter) | M1@impairment=cellular-timeline+jitter=bursty+metric=p99
rtp_mux_jitter::jitter_bulk_idle_restart_arm = perf | 35 | composite(lane,load,metric,rate) | M3@lane=bulk+load=bulk-idle-restart+rate=1MiBps+metric=capacity-fraction
rtp_mux_jitter::jitter_shared_bottleneck_arms = perf | TBD | composite(latency,load) | M1@load=shared-bottleneck+latency=sweep+metric=p99
dual_lane_mandates::bulk_lane_goodput_stays_above_capacity_fraction = full | 45 | composite(lane,metric,rate,scale) | M3@lane=bulk+rate=link+scale=saturated+metric=capacity-fraction
hol_probe::fec_gaming_treatment_has_bad_path_and_large_capacity_headroom = default | TBD | composite(layer,metric) | instrument-sanity@layer=rtp-fec+metric=path-and-headroom
hol_probe::fec_saturated_pair_keys_loss_to_the_same_rtp_sequence = default | TBD | composite(layer,metric) | instrument-sanity@layer=rtp-fec+metric=sequence-keying
hol_probe::dual_lane_asym_frame_delivers_and_tears_down = full | TBD | composite(asym,layer,metric) | hol-dual-lane@lane=dual+asym=yes+layer=rtp-frame+metric=delivery-and-teardown
hol_probe::hol_cap400_solo = full | TBD | composite(bulk,loss,rate) | hol@rate=400kbps+loss=iid1+bulk=none+metric=p99
hol_probe::hol_cap400_shared = full | TBD | composite(bulk,loss,rate) | hol@rate=400kbps+loss=iid1+bulk=shared+metric=p99
hol_probe::hol_cap400_fec_solo = perf | TBD | composite(bulk,fec,loss,rate) | hol-fec@rate=400kbps+loss=iid1+fec=on+bulk=none+metric=p99
hol_probe::hol_cap400_loss1_split_shared = perf | TBD | composite(bulk,loss,rate) | hol@rate=400kbps+loss=iid1+bulk=split-and-shared+metric=p99
hol_probe::hol_cap400_shared_frame_delivery_diag = full | TBD | composite(bulk,layer,loss,rate,report) | hol-frame@rate=400kbps+loss=iid1+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_clean_solo = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=none+bulk=none+flows=1+metric=p99
hol_probe::hol_rtt100_clean_shared = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=none+bulk=shared+flows=1+metric=p99
hol_probe::hol_rtt100_clean_split = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=none+bulk=split+flows=1+metric=p99
hol_probe::hol_rtt100_clean_shared_frame_delivery_diag = full | TBD | composite(bulk,latency,layer,loss,report) | hol-frame@latency=100ms+loss=none+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_solo = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge5-burst+bulk=none+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_shared = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge5-burst+bulk=shared+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_split = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge5-burst+bulk=split+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_shared_frame_delivery = full | TBD | composite(bulk,latency,layer,loss) | hol-frame@latency=100ms+loss=ge5-burst+bulk=shared+layer=rtp-frame+metric=p99
hol_probe::hol_rtt100_ge5_v2_solo = full | TBD | composite(bulk,latency,loss,variant) | hol@latency=100ms+loss=ge5-burst+bulk=none+variant=v2+metric=p99
hol_probe::hol_rtt100_ge5_v2_shared = full | TBD | composite(bulk,latency,loss,variant) | hol@latency=100ms+loss=ge5-burst+bulk=shared+variant=v2+metric=p99
hol_probe::hol_rtt100_ge5_v3_solo = full | TBD | composite(bulk,latency,loss,variant) | hol@latency=100ms+loss=ge5-burst+bulk=none+variant=v3+metric=p99
hol_probe::hol_rtt100_ge5_v3_shared = full | TBD | composite(bulk,latency,loss,variant) | hol@latency=100ms+loss=ge5-burst+bulk=shared+variant=v3+metric=p99
hol_probe::hol_rtt100_ge5_v3_split = full | TBD | composite(bulk,latency,loss,variant) | hol@latency=100ms+loss=ge5-burst+bulk=split+variant=v3+metric=p99
hol_probe::hol_rtt100_ge1_loss1_solo = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=none+metric=p99
hol_probe::hol_rtt100_ge1_loss1_shared = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=shared+metric=p99
hol_probe::hol_rtt100_ge1_loss1_split = full | TBD | composite(bulk,latency,loss) | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=split+metric=p99
hol_probe::hol_rtt100_ge1_shared_frame_delivery_diag = full | TBD | composite(bulk,latency,layer,loss,report) | hol-frame@latency=100ms+loss=ge1-burst+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_two_interactive_frame_delivery = full | TBD | composite(flows,latency,layer,loss,metric) | hol@latency=100ms+loss=ge5-burst+flows=2+layer=rtp-frame+metric=delivery
hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery = full | TBD | composite(flows,latency,layer,loss,metric) | M4@latency=100ms+loss=ge5-burst+flows=4+layer=rtp-frame+metric=per-flow-delivery
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_frame_diag = full | TBD | composite(flows,latency,layer,loss,report) | hol-dual-lane@latency=100ms+loss=ge5-burst+flows=2+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_stock_diag = full | TBD | composite(fec,flows,latency,loss,report) | hol-dual-lane@latency=100ms+loss=ge5-burst+flows=2+fec=stock+report=diag
hol_probe::hol_rtt100_ge5_shared_dual_lane = full | TBD | composite(bulk,latency,loss) | hol-dual-lane@latency=100ms+loss=ge5-burst+bulk=shared+metric=p99
hol_probe::hol_rtt100_ge5_shared_dual_lane_frame_delivery = full | TBD | composite(latency,layer,loss) | hol-dual-lane@latency=100ms+loss=ge5-burst+layer=rtp-frame+metric=p99
hol_probe::hol_rtt100_ge5_shared_dual_lane_asym_frame_diag = full | TBD | composite(asym,latency,layer,loss,report) | hol-dual-lane@latency=100ms+loss=ge5-burst+asym=yes+layer=rtp-frame+report=diag
hol_probe::hol_rtp_mux_fec_default_on_recovery = full | TBD | composite(fec,impairment,layer,loss) | hol-fec-recovery@layer=rtp-mux+fec=default-on+impairment=fec-gaming-fat-pipe+loss=20pct
hol_probe::hol_paced_bulk_median_p99_regression = full | TBD | composite(bulk,metric,shape) | hol-paced@bulk=shared+shape=paced-bulk+metric=p99-median
hol_probe::hol_hostile_solo = full | TBD | composite(bulk,impairment) | hol@impairment=hostile-preset+bulk=none+metric=p99
hol_probe::hol_hostile_shared = full | TBD | composite(bulk,impairment) | hol@impairment=hostile-preset+bulk=shared+metric=p99
hol_probe::hol_hostile_split = full | TBD | composite(bulk,impairment) | hol@impairment=hostile-preset+bulk=split+metric=p99
hol_probe::hol_hostile_shared_frame_delivery_diag = full | TBD | composite(bulk,impairment,layer,report) | hol-frame@impairment=hostile-preset+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt40_ge1_solo = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst+bulk=none+metric=p99
hol_probe::hol_rtt40_ge1_shared = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst+bulk=shared+metric=p99
hol_probe::hol_rtt40_ge1_split = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst+bulk=split+metric=p99
hol_probe::hol_rtt40_ge1_loss1_solo = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=none+metric=p99
hol_probe::hol_rtt40_ge1_loss1_shared = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=shared+metric=p99
hol_probe::hol_rtt40_ge1_loss1_split = full | TBD | composite(bulk,latency,loss) | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=split+metric=p99
hol_verify4::v4_clean_muxbulk = perf | TBD | composite(impairment,load,metric) | bulk-lane-ab@lane=dual+impairment=clean+load=bulk+metric=goodput-ab
hol_verify4::v4_ge5_muxbulk = perf | TBD | composite(impairment,load,metric) | bulk-lane-ab@lane=dual+impairment=ge5-burst+load=bulk+metric=goodput-ab
contested_latency::contested_capped_clean = full | TBD | composite(jitter,loss,metric,rate) | contested@rate=cap+jitter=0+loss=0+metric=p99-with-bulk
contested_latency::contested_capped_jitter_loss = perf | TBD | composite(jitter,loss,metric,rate) | contested@rate=cap+jitter=on+loss=on+metric=p99-with-bulk
contested_latency::contested_hostile = perf | TBD | composite(impairment,metric) | contested@impairment=hostile-preset+metric=p99-with-bulk
perf_probe::controller_fat_pipe_has_only_fixed_shaping = default | TBD | composite(lane,metric) | instrument-sanity@lane=controller-fat-pipe+metric=shaping-determinism
perf_probe::deterministic_iid_loss_fat_pipe_is_fixed_seeded_iid_loss = default | TBD | composite(lane,metric) | instrument-sanity@lane=deterministic-iid-loss-fat-pipe+metric=loss-determinism
perf_probe::probe_rtp_echo_4mib_direct = standard | TBD | composite(layer,metric,scale,shape,transport) | ceiling@layer=rtp+transport=direct+shape=echo+scale=4MiB+metric=throughput
perf_probe::probe_rtp_echo_4mib_mss8k = standard | TBD | composite(layer,metric,mss,scale,shape,transport) | ceiling@layer=rtp+transport=direct+shape=echo+mss=8k+scale=4MiB+metric=throughput
perf_probe::probe_hostile_goodput_30s = full | TBD | composite(impairment,layer,metric,scale) | ceiling@layer=rtp+impairment=hostile-preset+scale=30s+metric=goodput
perf_probe::probe_hostile_message_latency = full | TBD | composite(impairment,layer,metric,shape) | ceiling@layer=rtp+impairment=hostile-preset+shape=request-response+metric=latency
mux_ceiling_probe::probe_mux_echo_1mib_direct = standard | TBD | composite(layer,metric,scale,shape,transport) | loopback-ceiling@layer=mux+transport=direct+shape=echo+scale=1MiB+metric=throughput
mux_ceiling_probe::probe_mux_echo_1mib_mss8k = standard | TBD | composite(layer,metric,mss,scale,shape,transport) | loopback-ceiling@layer=mux+transport=direct+shape=echo+mss=8k+scale=1MiB+metric=throughput
mux_ceiling_probe::probe_mux_sink_4mib_direct = standard | TBD | composite(layer,metric,scale,shape,transport) | loopback-ceiling@layer=mux+transport=direct+shape=sink+scale=4MiB+metric=throughput
mux_ceiling_probe::probe_mux_sink_4mib_mss8k = standard | TBD | composite(layer,metric,mss,scale,shape,transport) | loopback-ceiling@layer=mux+transport=direct+shape=sink+mss=8k+scale=4MiB+metric=throughput
mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke = default | TBD | composite(impairment,metric,scale) | mux-over-rtp@impairment=lossy+scale=400KiB+metric=delivery-and-goodput
mux_over_rtp_perf::mux_over_rtp_400kib_lossy_contended_perf = default | TBD | composite(impairment,load,metric,scale) | mux-over-rtp@impairment=lossy+load=contended+scale=400KiB+metric=delivery-and-goodput
mux_over_rtp_perf::mux_over_rtp_small_stream_while_bulk_perf = default | TBD | composite(load,metric) | small-stream-while-bulk@load=bulk+shape=cadence+metric=ordering
mux_over_rtp_perf::mux_over_rtp_400mib_hostile_perf = full | TBD | composite(impairment,metric,scale) | mux-over-rtp@impairment=hostile-preset+scale=400MiB+metric=goodput
mux_stream_fairness::mux_stream_fairness_sweep = full | TBD | composite(arm-set,flows,metric) | fairness-sweep@flows=multi+arm-set=homogeneous-heterogeneous-throttled+metric=jain
mux_stream_fairness::mux_stream_fairness_longrun = full | TBD | composite(flows,metric,scale) | fairness-longrun@flows=multi+scale=multi-minute+metric=per-flow-share
rtp_longrun::longrun_duallane = full | TBD | composite(metric,scale) | dual-lane-longrun@lane=dual+scale=multi-minute+metric=goodput-and-tail
rtp_longrun::multiflow_duallane = full | TBD | composite(flows,metric,scale) | multi-flow-longrun@lane=dual+flows=multi+scale=multi-minute+metric=per-flow-share
```

```gate-budgets
default = 300
standard = 600
full = 7200
perf = 7200
baseline = mandate_smoke::m1_interactive_tail_latency
drift = 0.5
drift_floor_s = 10.0
```

These budgets are proposals, not measurements. The rows with a known cost
declare 237 s in `default` (the four `mandate_smoke` rows and the constitution
gate) and 4135 s in `perf` (26 rows, 7 of them still `TBD`); `standard` and
`full` hold only ceilings until their rows are measured. Set each tier's budget
to its measured sum plus headroom **as a declared change** once the `TBD` rows
have been measured — a tier sum over its budget is a checker failure, so an
unmeasured budget is what forces the measurement.

```gate-coverage-gaps
M1@lane=single = the constitution's M1 arms run on the production dual-lane topology; a single-connection transport tail is covered by rtp's burst-loss and bufferbloat gates, whose perf declarations are pending, and is not claimed here.
M3@lane=single = M3 is asserted on the deployment's bulk lane (dual_lane_mandates); the single-connection goodput floor is rtp's, and its declaration is pending.
cpu-cost@metric=cpu = per-datagram CPU cost is measured by owning-symbol attribution (tools/samply_hotspots.py), not by a scenario in this crate.
large-scale@scale=over400MiB = the hostile bulk arm caps at 400 MiB; steady-state transfer beyond that is not claimed by any row here.
soak@scale=multi-hour = rtp_longrun is multi-minute; a multi-hour soak fits no tier's budget, so it exists as no test here.
multipath@impairment=multipath = the multi-path UDP transport (rtp's mpudp) has no rtp_mux arm; a cell for it belongs to the layer that owns that transport.
rate-asymmetry@impairment=rate-asymmetry = only the asym frame-delivery diag arm varies lane asymmetry; an asymmetric *rate* with symmetric latency is not covered.
loaded-lone-tail@shape=request-response+load=bulk = the lone-tail arms run with the bulk lane idle; the request/response shape under a loaded bulk lane is not covered.
cellular-request-response@lane=cellular-timeline+shape=request-response = the cellular timeline arms use the cadence shape only.
policer@impairment=policer = a token-bucket policer (as opposed to the shaper and queue the harness models) is not in the impairment instrument, so no arm can cover it.
```

## How to make the draft real

1. Measure the `TBD` rows: one invocation per row as described above, rounding
   up to the nearest second, and replace each `TBD` with the number. The
   `#[ignore]` reasons already give estimates for `rtp_mux_jitter`; the ones
   without a number are the `hol_probe`, ceiling-probe and longrun rows.
2. Paste the three blocks into `crates/rtp_mux/GATE.md` next to the existing
   ones, and set each tier budget from the measured sums.
3. Run `cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture`
   through `tools/mandate-check`, then
   `python3 ../netem_test/tools/check-gate.py --crate . rtp_mux tests GATE.md
   --mandate-check-json <run>/mandate-check.json`. The checker resolves every
   row against the compiled test set and compares the four `mandate_smoke`
   costs with the report's per-test wall-clock, so a stale or invented cost
   fails rather than passing quietly.
