# The pending `rtp_mux` perf declaration

`rtp_mux` owns perf tests but has no perf declaration, so the dual mandate is
unenforced there. This file is the exact set of blocks that crate's `GATE.md`
needs: the `gate-perf-design` rows, the `gate-budgets` block and the
`gate-coverage-gaps` lines. Every row also declares its relation to the
baseline of its own family (see "The relation each row declares" below), and
the family membership those relations imply is derived from the rows' cells in
"Family membership" below. It is
a draft, not an
authority — the numbers become `rtp_mux`'s when that crate's iteration lands
them in its own `GATE.md`, where `python3 ../netem_test/tools/check-gate.py
--crate . rtp_mux tests GATE.md` will enforce them.

**Nothing here retunes an arm, threshold, window, cadence or tier.** The rows
only name tests that already exist, the tier each already has in
`rtp_mux/GATE.md`, the cells each already covers, the family baseline each is
stated against, and the dimensions each varies from that baseline.

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

Every row carries its relation to the baseline of **its own family**: a
relation with a trailing `@<family>` is derived against that
`baseline.<family>` row, and a relation naming none against the default
`baseline = mandate_smoke::m1_interactive_tail_latency` (the M1-clean cadence
cell). The kinds are `baseline` for a family's reference row itself,
`orthogonal` when the row's cells vary exactly one dimension from that
reference, `composite(<dimension>[,<dimension>…])` when they vary several, and
`re-measurement(<reason>)` when they vary none. `check-gate.py` derives the
varied dimensions from the row's own cells — a dimension whose value differs
from its family reference's, or that the reference does not state at all (a
dimension the row does not name is inherited) — and refuses a label that
disagrees with that derivation, so these are not a judgement call. Nor is a
row stated against a reference of a family it belongs to by inspection: which
family a row belongs to is exactly what the `@<family>` suffix declares, and
`check-gate.py` only enforces that the family exists, that a family's
reference row carries its `baseline@<family>` label, that every declared
family is used by some row, and that the row's cells lie in the family's own
**cell-name namespace** (`members.<family>`, see "Family membership" below).

The set splits into **29 reference families** (**28 named plus the default**): the four mandates, the
constitution gate, the per-target probe groups (the rtp and mux echo ceilings,
the two instrument-sanity arms, the hostile-preset probes), the `hol_probe`
regimes, `hol_verify4`, the decomposed frame-reorder and FEC arms, the
interactive cadence arms, the reorder, decomposition and latency-sweep arms,
the contested-latency pair, the mux-over-rtp arms, the fairness and longrun
arms, and the lone-tail arms. Each family's reference is the arm that already
states that family's common context most completely — which is what makes a
member one dimension away attributable — never a retuned arm. Against those
references the 94 rows are **46 orthogonal**, **16 composite**, **3
re-measurement** and **29 baseline** (one per family).

**The reference is still a choice, and deriving it does not work by the obvious
rule.** Membership is derived from the cells ("Family membership" below), but
the family's *reference row* is not: the natural derivation — take the member
that makes the most of its family orthogonal — fails both ways. It does not
pin one: all 4 of the harness's families and 16 of this draft's 29 have several
max-orthogonality members (`probe` has three). And it points the wrong way: the
checker derives a row's varied dimensions from *the row's own keys*, so a
sparser reference scores at least as well as a richer one — the harness's
five-dimension default baseline ties at 7 orthogonal with
`netem_reorder_with_rate_jumps_ahead`, whose cell states two dimensions. Every
declared reference (all 33) does attain its family's maximum, so no family's
orthogonal count is depressed by the choice — but for the three families whose
tied references split differently (`probe` 2/0/1 against 2/1/0; this draft's
`fairness` 1/2/1 against 1/3/0 and `interactive` 1/1/1 against 1/2/0) the
composite and re-measurement counts are reference-dependent, and the reference
remains a declaration the checker takes as given.

The composites are the 14 rows for which no arm in their own family is within
one declared dimension, plus 2 whose nearest declared arm sits in another
family: `jitter_nonloss_impairments` is one declared dimension (`impairment`)
from the `hol_probe` clean-link arms, and `jitter_interactive_bulk_and_loss` is
one (`metric`) from `jitter_interactive_with_bulk`; both are filed where the
rest of their cell context lives, and both are attributable if refiled. All 16
are listed with their dimensions in the checker's output
(`gate-perf-composite:`), and each is a candidate for a **new** single-axis arm
beside it rather than for a retuned one. The three re-measurements are arms
whose declared cells repeat their reference's own point (the member's cell
omits axes the reference states, so they are inherited): `jitter_interactive_with_loss`
against `jitter_interactive_solo`, `hol_rtt100_ge5_shared_dual_lane` against
`hol_rtt100_ge5_shared`, and `mux_stream_fairness_longrun` against
`rtp_longrun::multiflow_duallane`. Each is labelled rather than silently
orthogonal, because a declared duplicate cell is exactly what a shortening
pass needs to see.

The partition itself is the choice; the **partition-invariant** number is how
many rows have another arm exactly one declared dimension away. 61 of the 94
do; the other 33 have none. Of those 33, 11 are arms whose declared cell
states only axes a richer neighbour already states (so the neighbour is at
zero dimensions but the two are not the same arm — an omitted axis, the limit
the checker cannot see), 8 are used as their family's reference (so they are
baselines, not confounded arms) and 14 land as composites. Nothing here needs
its windows, cadences or tiers retuned to fix that: an attributable arm for a
family is a **new** arm stated one axis away from a reference, and that is a
change to the tests, not to these labels. Nothing the draft cannot determine
was guessed: the labels are the checker's derivation, and the `TBD` costs stay
`TBD`. Three counts in this section — how many rows have a one-dimension
relative (61 of 94), how many families have several max-orthogonality
references (16 of 29), and the per-family tallies under the tied references
(`probe` 2/0/1 against 2/1/0; this draft's `fairness` 1/2/1 against 1/3/0 and
`interactive` 1/1/1 against 1/2/0) — are this draft's own reading against its
own arms, and no command in `tools/` prints them, so they stay prose and are
not machine-checked; the claims stated in either block's numbers above are.

## Family membership

A `@<family>` suffix is a claim that the row **belongs** to that family, and
`check-gate.py` derives that membership from the row's own cells rather than
taking the label: each family declares the **cell-name namespace** its rows
live in (`members.<family> = <prefix>`, a cell's property name optionally
followed by `*`), a cell name may not be claimed by two families, a family's
namespace must contain its own reference row, and a row whose cells are named
by a family's namespace must state against it. The default family owns the
**residual**: every cell name no `members.<family>` claims.

Applied to this draft that derivation rejects most of it. The proposed
namespaces below are the narrowest prefix each family's own cells support, and
where they share none, the reference's own cell name:

```gate-members-proposed
members.constitution = M*
members.contested = contested
members.decomposition = frame-reorder*
members.dual-lane = hol-dual-lane*
members.fairness = multi-flow-longrun*
members.fec = fec-tuning*
members.fec-instrument-sanity = instrument-sanity
members.frame-reorder = frame-reorder-fec
members.hol-cap400 = hol*
members.hol-dual-lane-frame = hol-dual-lane*
members.hol-ge5-shared = hol*
members.hol-ge5-solo = hol
members.hol-rtt40 = hol
members.hol-shared-frame = hol-*
members.hol-solo = hol
members.hol-split = hol
members.hol-verify4 = bulk-lane-ab
members.hostile-probes = ceiling*
members.instrument-sanity = instrument-sanity
members.interactive = M*
members.latency-sweep = M1
members.lone-tail = M1*
members.m3-bulk = M3
members.mux-ceiling-echo = loopback-ceiling
members.mux-ceiling-sink = loopback-ceiling
members.mux-over-rtp = mux-over-rtp*
members.reorder = reorder-*
members.rtp-ceiling = ceiling
```

`tools/test_pending_declaration.py` runs the checker's membership derivation on
this proposal and pins the result, so the numbers below cannot rot:

- **14 cell names are claimed by two or more families**: `M1` (constitution,
  interactive, latency-sweep, lone-tail), `M2` (constitution, interactive),
  `M3` (constitution, interactive, m3-bulk), `M4` (constitution, interactive),
  `ceiling` (hostile-probes, rtp-ceiling), `contested` (contested,
  hostile-probes), `frame-reorder-fec` (decomposition, frame-reorder), `hol`
  (hol-cap400, hol-ge5-shared, hol-ge5-solo, hol-rtt40, hol-solo, hol-split),
  `hol-dual-lane` (dual-lane, hol-cap400, hol-dual-lane-frame,
  hol-ge5-shared, hol-shared-frame), `hol-fec`, `hol-fec-recovery`,
  `hol-frame` and `hol-paced` (each hol-cap400, hol-ge5-shared,
  hol-shared-frame), `instrument-sanity` (fec-instrument-sanity,
  instrument-sanity) and `loopback-ceiling` (mux-ceiling-echo,
  mux-ceiling-sink). Where a cell name recurs the families sharing it are *not*
  distinguishable by their cells: the `hol_probe` families differ in their
  dimensions' values, not in the names their cells carry.
- **8 families span more than one cell name**, so no single namespace covers
  them: `decomposition` (`frame-reorder`, `loss-vs-queue`), `dual-lane`
  (`hol-dual-lane`, `dual-lane-matched-load`), `fairness` (`M4`,
  `fairness-sweep`, `fairness-longrun`, `dual-lane-longrun`,
  `multi-flow-longrun`), `fec` (`fec-tuning`, `hol-fec-recovery`),
  `hol-dual-lane-frame` (`M4`, `hol`, `hol-dual-lane`), `hostile-probes`
  (`ceiling`, `contested`, `mux-over-rtp`), `lone-tail` (`M1`,
  `non-loss-impairment`) and `mux-over-rtp` (`mux-over-rtp`,
  `small-stream-while-bulk`).
- **81 of the 94 rows state a family whose namespace does not name their
  cells**: 13 whose cells carry a name the family does not claim, and 68 whose
  cells carry a name two or more families claim.
- **Both composite artefacts of the free label fail.**
  `rtp_mux_jitter::jitter_interactive_bulk_and_loss` carries the cell name
  `M2`, which `constitution` and `interactive` both claim, so its
  `@interactive` cannot be derived from its cells;
  `rtp_mux_jitter::jitter_nonloss_impairments` carries `non-loss-impairment`,
  which its family's `M1*` namespace does not claim, so neither `@lone-tail`
  nor any other declared family holds it. Both are one declared dimension from
  arms in other families *because their cells are foreign to the family they
  name* - the free label was hiding exactly that.

None of this is a reason to retune an arm. A row whose cells are foreign to its
family is closed by a **new** single-axis arm whose cell carries the family's
name, or by splitting the family so each half owns one name; renaming an
existing cell is a change to that arm's declaration and is out of scope here.
Re-cutting the families *by* cell name, which is what the rule would force,
replaces the 29 context families with the cell-name groups, and measured on
these 94 rows it costs the attribution: the `hol` group becomes one family of
27 rows with 7 orthogonal and 19 composite, the `M1` group one family of 9 with
1 orthogonal, 6 composite and 1 re-measurement, and 15 of the resulting groups
hold a single row, whose reference no sibling row states against (the
stale-reference failure). The draft therefore records the conflict instead of
pretending its partition is derived: the families stay as they are, and the
membership block above is a proposal whose rejection *is* the finding.

## The blocks to paste into `crates/rtp_mux/GATE.md`

```gate-perf-design
mandate_smoke::m1_interactive_tail_latency = default | 50.0 | baseline | baseline@impairment=clean2pct-iid+latency=25ms+jitter=5ms+lane=dual+shape=cadence+flows=1+scale=256B+metric=p99
mandate_smoke::m2_interactive_delivery_and_wire = default | 54.1 | orthogonal | M2@impairment=clean2pct-iid+latency=25ms+jitter=5ms+lane=dual+shape=cadence+flows=1+metric=own-wire
mandate_smoke::m3_bulk_goodput_fraction = default | 61.6 | baseline@m3-bulk | M3@lane=bulk+rate=1MiBps+scale=2MiB+metric=capacity-fraction
mandate_smoke::m4_interactive_lane_fairness = default | 31.2 | composite(arm-set,flows)@fairness | M4@lane=dual+flows=4+arm-set=clean-and-hostile+metric=per-flow-share
rtp_mux_jitter::jitter_duallane_constitution_gate = default | 40 | baseline@constitution | M2@lane=dual+shape=cadence+arm-set=clean-and-hostile+metric=own-wire-budget
rtp_mux_jitter::jitter_duallane_constitution_gate_p99 = full | 105 | orthogonal@constitution | M1@lane=dual+shape=cadence+arm-set=clean-and-hostile+metric=p99-median-of-3
rtp_mux_jitter::jitter_decomposition = perf | 280 | composite(arms,jitter)@decomposition | loss-vs-queue@impairment=loss2pct-iid+jitter=5ms+load=bulk-burst+arms=solo-loss-bulk-combined+metric=p99-decomposition
rtp_mux_jitter::jitter_frame_reorder_decomposition = perf | 140 | baseline@decomposition | frame-reorder@layer=rtp-frame+reorder=receiver-fast-forward+impairment=loss2pct-iid+load=bulk-burst+metric=p99-decomposition
rtp_mux_jitter::jitter_frame_reorder_fec_arms = perf | 210 | baseline@frame-reorder | frame-reorder-fec@layer=rtp-frame+reorder=fast-forward+fec=on+impairment=loss2pct-iid
rtp_mux_jitter::jitter_frame_reorder_fec_bulk_loss_reorder = perf | 140 | orthogonal@frame-reorder | frame-reorder-fec@layer=rtp-frame+reorder=fast-forward+fec=on+load=bulk+impairment=loss2pct-iid
rtp_mux_jitter::jitter_fec_arms_2pct = perf | 175 | baseline@fec | fec-tuning@impairment=loss2pct-iid+fec=off-stock-prompt+metric=parity-and-latency
rtp_mux_jitter::jitter_fec_arms_6pct = perf | 175 | orthogonal@fec | fec-tuning@impairment=loss6pct-iid+fec=off-stock-prompt+metric=parity-and-latency
rtp_mux_jitter::jitter_nonloss_impairments = perf | 210 | composite(impairment,loss)@lone-tail | non-loss-impairment@impairment=jitter-reorder-dup-rate+loss=none+metric=p99
rtp_mux_jitter::jitter_reorder_rate_curve = perf | 140 | orthogonal@reorder | reorder-rate@impairment=reorder+rate=curve+metric=p99
rtp_mux_jitter::jitter_reorder_direction = perf | 70 | baseline@reorder | reorder-direction@impairment=reorder+direction=c2s-and-s2c+metric=p99
rtp_mux_jitter::jitter_interactive_solo = perf | 35 | baseline@interactive | M1@lane=interactive+flows=1+impairment=loss2pct-iid+jitter=5ms+shape=cadence
rtp_mux_jitter::jitter_interactive_with_loss = perf | 35 | re-measurement(second-arm-same-declared-point)@interactive | M1@lane=interactive+flows=1+impairment=loss2pct-iid
rtp_mux_jitter::jitter_interactive_with_bulk = perf | 130 | orthogonal@interactive | M1@lane=interactive+flows=1+load=bulk-burst+impairment=loss2pct-iid
rtp_mux_jitter::jitter_interactive_bulk_and_loss = perf | 35 | composite(load,metric)@interactive | M2@lane=interactive+flows=1+load=bulk-burst+impairment=loss2pct-iid+metric=own-wire
rtp_mux_jitter::jitter_duallane_arms = perf | 280 | composite(load,reorder)@dual-lane | dual-lane-matched-load@lane=dual+load=bulk-matched+reorder=fast-forward-and-strict
rtp_mux_jitter::jitter_burst_loss_arms = perf | 420 | orthogonal | M1@impairment=gilbert-elliott-burst+jitter=5ms+lane=dual+metric=p99
rtp_mux_jitter::jitter_request_response_arms = perf | 1170 | baseline@lone-tail | M1@shape=request-response+depth=1+impairment=loss5pct-ge+jitter=100ms+metric=p99
rtp_mux_jitter::jitter_latency_dimension_arms = perf | 385 | baseline@latency-sweep | M1@latency=sweep+impairment=loss2pct-iid+metric=p99
rtp_mux_jitter::jitter_cellular_timeline_arms = perf | 70 | composite(impairment,jitter)@lone-tail | M1@impairment=cellular-timeline+jitter=bursty+metric=p99
rtp_mux_jitter::jitter_bulk_idle_restart_arm = perf | 35 | orthogonal@m3-bulk | M3@lane=bulk+load=bulk-idle-restart+rate=1MiBps+metric=capacity-fraction
rtp_mux_jitter::jitter_shared_bottleneck_arms = perf | TBD | orthogonal@latency-sweep | M1@load=shared-bottleneck+latency=sweep+metric=p99
dual_lane_mandates::bulk_lane_goodput_stays_above_capacity_fraction = full | 45 | composite(rate,scale)@m3-bulk | M3@lane=bulk+rate=link+scale=saturated+metric=capacity-fraction
hol_probe::fec_gaming_treatment_has_bad_path_and_large_capacity_headroom = default | TBD | baseline@fec-instrument-sanity | instrument-sanity@layer=rtp-fec+metric=path-and-headroom
hol_probe::fec_saturated_pair_keys_loss_to_the_same_rtp_sequence = default | TBD | orthogonal@fec-instrument-sanity | instrument-sanity@layer=rtp-fec+metric=sequence-keying
hol_probe::dual_lane_asym_frame_delivers_and_tears_down = full | TBD | baseline@dual-lane | hol-dual-lane@lane=dual+asym=yes+layer=rtp-frame+metric=delivery-and-teardown
hol_probe::hol_cap400_solo = full | TBD | baseline@hol-cap400 | hol@rate=400kbps+loss=iid1+bulk=none+metric=p99
hol_probe::hol_cap400_shared = full | TBD | orthogonal@hol-cap400 | hol@rate=400kbps+loss=iid1+bulk=shared+metric=p99
hol_probe::hol_cap400_fec_solo = perf | TBD | orthogonal@hol-cap400 | hol-fec@rate=400kbps+loss=iid1+fec=on+bulk=none+metric=p99
hol_probe::hol_cap400_loss1_split_shared = perf | TBD | orthogonal@hol-cap400 | hol@rate=400kbps+loss=iid1+bulk=split-and-shared+metric=p99
hol_probe::hol_cap400_shared_frame_delivery_diag = full | TBD | composite(bulk,layer,report)@hol-cap400 | hol-frame@rate=400kbps+loss=iid1+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_clean_solo = full | TBD | orthogonal@hol-split | hol@latency=100ms+loss=none+bulk=none+flows=1+metric=p99
hol_probe::hol_rtt100_clean_shared = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=none+bulk=shared+flows=1+metric=p99
hol_probe::hol_rtt100_clean_split = full | TBD | baseline@hol-split | hol@latency=100ms+loss=none+bulk=split+flows=1+metric=p99
hol_probe::hol_rtt100_clean_shared_frame_delivery_diag = full | TBD | orthogonal@hol-shared-frame | hol-frame@latency=100ms+loss=none+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_solo = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=ge5-burst+bulk=none+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_shared = full | TBD | baseline@hol-ge5-shared | hol@latency=100ms+loss=ge5-burst+bulk=shared+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_split = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=ge5-burst+bulk=split+flows=1+metric=p99
hol_probe::hol_rtt100_ge5_shared_frame_delivery = full | TBD | orthogonal@hol-ge5-shared | hol-frame@latency=100ms+loss=ge5-burst+bulk=shared+layer=rtp-frame+metric=p99
hol_probe::hol_rtt100_ge5_v2_solo = full | TBD | orthogonal@hol-ge5-solo | hol@latency=100ms+loss=ge5-burst+bulk=none+variant=v2+metric=p99
hol_probe::hol_rtt100_ge5_v2_shared = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=ge5-burst+bulk=shared+variant=v2+metric=p99
hol_probe::hol_rtt100_ge5_v3_solo = full | TBD | baseline@hol-ge5-solo | hol@latency=100ms+loss=ge5-burst+bulk=none+variant=v3+metric=p99
hol_probe::hol_rtt100_ge5_v3_shared = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=ge5-burst+bulk=shared+variant=v3+metric=p99
hol_probe::hol_rtt100_ge5_v3_split = full | TBD | orthogonal@hol-ge5-solo | hol@latency=100ms+loss=ge5-burst+bulk=split+variant=v3+metric=p99
hol_probe::hol_rtt100_ge1_loss1_solo = full | TBD | orthogonal@hol-solo | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=none+metric=p99
hol_probe::hol_rtt100_ge1_loss1_shared = full | TBD | orthogonal@hol-ge5-shared | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=shared+metric=p99
hol_probe::hol_rtt100_ge1_loss1_split = full | TBD | orthogonal@hol-split | hol@latency=100ms+loss=ge1-burst-and-iid1+bulk=split+metric=p99
hol_probe::hol_rtt100_ge1_shared_frame_delivery_diag = full | TBD | baseline@hol-shared-frame | hol-frame@latency=100ms+loss=ge1-burst+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_two_interactive_frame_delivery = full | TBD | orthogonal@hol-dual-lane-frame | hol@latency=100ms+loss=ge5-burst+flows=2+layer=rtp-frame+metric=delivery
hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery = full | TBD | composite(flows,metric)@hol-dual-lane-frame | M4@latency=100ms+loss=ge5-burst+flows=4+layer=rtp-frame+metric=per-flow-delivery
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_frame_diag = full | TBD | baseline@hol-dual-lane-frame | hol-dual-lane@latency=100ms+loss=ge5-burst+flows=2+layer=rtp-frame+report=diag
hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_stock_diag = full | TBD | orthogonal@hol-dual-lane-frame | hol-dual-lane@latency=100ms+loss=ge5-burst+flows=2+fec=stock+report=diag
hol_probe::hol_rtt100_ge5_shared_dual_lane = full | TBD | re-measurement(second-arm-same-declared-point)@hol-ge5-shared | hol-dual-lane@latency=100ms+loss=ge5-burst+bulk=shared+metric=p99
hol_probe::hol_rtt100_ge5_shared_dual_lane_frame_delivery = full | TBD | orthogonal@hol-ge5-shared | hol-dual-lane@latency=100ms+loss=ge5-burst+layer=rtp-frame+metric=p99
hol_probe::hol_rtt100_ge5_shared_dual_lane_asym_frame_diag = full | TBD | orthogonal@hol-dual-lane-frame | hol-dual-lane@latency=100ms+loss=ge5-burst+asym=yes+layer=rtp-frame+report=diag
hol_probe::hol_rtp_mux_fec_default_on_recovery = full | TBD | composite(fec,impairment,layer,loss)@fec | hol-fec-recovery@layer=rtp-mux+fec=default-on+impairment=fec-gaming-fat-pipe+loss=20pct
hol_probe::hol_paced_bulk_median_p99_regression = full | TBD | composite(metric,shape)@hol-shared-frame | hol-paced@bulk=shared+shape=paced-bulk+metric=p99-median
hol_probe::hol_hostile_solo = full | TBD | orthogonal@hol-solo | hol@impairment=hostile-preset+bulk=none+metric=p99
hol_probe::hol_hostile_shared = full | TBD | orthogonal@hol-ge5-shared | hol@impairment=hostile-preset+bulk=shared+metric=p99
hol_probe::hol_hostile_split = full | TBD | orthogonal@hol-split | hol@impairment=hostile-preset+bulk=split+metric=p99
hol_probe::hol_hostile_shared_frame_delivery_diag = full | TBD | orthogonal@hol-shared-frame | hol-frame@impairment=hostile-preset+bulk=shared+layer=rtp-frame+report=diag
hol_probe::hol_rtt40_ge1_solo = full | TBD | orthogonal@hol-solo | hol@latency=20ms+loss=ge1-burst+bulk=none+metric=p99
hol_probe::hol_rtt40_ge1_shared = full | TBD | baseline@hol-rtt40 | hol@latency=20ms+loss=ge1-burst+bulk=shared+metric=p99
hol_probe::hol_rtt40_ge1_split = full | TBD | orthogonal@hol-rtt40 | hol@latency=20ms+loss=ge1-burst+bulk=split+metric=p99
hol_probe::hol_rtt40_ge1_loss1_solo = full | TBD | baseline@hol-solo | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=none+metric=p99
hol_probe::hol_rtt40_ge1_loss1_shared = full | TBD | orthogonal@hol-solo | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=shared+metric=p99
hol_probe::hol_rtt40_ge1_loss1_split = full | TBD | orthogonal@hol-solo | hol@latency=20ms+loss=ge1-burst-and-iid1+bulk=split+metric=p99
hol_verify4::v4_clean_muxbulk = perf | TBD | baseline@hol-verify4 | bulk-lane-ab@lane=dual+impairment=clean+load=bulk+metric=goodput-ab
hol_verify4::v4_ge5_muxbulk = perf | TBD | orthogonal@hol-verify4 | bulk-lane-ab@lane=dual+impairment=ge5-burst+load=bulk+metric=goodput-ab
contested_latency::contested_capped_clean = full | TBD | baseline@contested | contested@rate=cap+jitter=0+loss=0+metric=p99-with-bulk
contested_latency::contested_capped_jitter_loss = perf | TBD | composite(jitter,loss)@contested | contested@rate=cap+jitter=on+loss=on+metric=p99-with-bulk
contested_latency::contested_hostile = perf | TBD | orthogonal@hostile-probes | contested@impairment=hostile-preset+metric=p99-with-bulk
perf_probe::controller_fat_pipe_has_only_fixed_shaping = default | TBD | baseline@instrument-sanity | instrument-sanity@lane=controller-fat-pipe+metric=shaping-determinism
perf_probe::deterministic_iid_loss_fat_pipe_is_fixed_seeded_iid_loss = default | TBD | composite(lane,metric)@instrument-sanity | instrument-sanity@lane=deterministic-iid-loss-fat-pipe+metric=loss-determinism
perf_probe::probe_rtp_echo_4mib_direct = standard | TBD | baseline@rtp-ceiling | ceiling@layer=rtp+transport=direct+shape=echo+scale=4MiB+metric=throughput
perf_probe::probe_rtp_echo_4mib_mss8k = standard | TBD | orthogonal@rtp-ceiling | ceiling@layer=rtp+transport=direct+shape=echo+mss=8k+scale=4MiB+metric=throughput
perf_probe::probe_hostile_goodput_30s = full | TBD | baseline@hostile-probes | ceiling@layer=rtp+impairment=hostile-preset+scale=30s+metric=goodput
perf_probe::probe_hostile_message_latency = full | TBD | composite(metric,shape)@hostile-probes | ceiling@layer=rtp+impairment=hostile-preset+shape=request-response+metric=latency
mux_ceiling_probe::probe_mux_echo_1mib_direct = standard | TBD | baseline@mux-ceiling-echo | loopback-ceiling@layer=mux+transport=direct+shape=echo+scale=1MiB+metric=throughput
mux_ceiling_probe::probe_mux_echo_1mib_mss8k = standard | TBD | orthogonal@mux-ceiling-echo | loopback-ceiling@layer=mux+transport=direct+shape=echo+mss=8k+scale=1MiB+metric=throughput
mux_ceiling_probe::probe_mux_sink_4mib_direct = standard | TBD | baseline@mux-ceiling-sink | loopback-ceiling@layer=mux+transport=direct+shape=sink+scale=4MiB+metric=throughput
mux_ceiling_probe::probe_mux_sink_4mib_mss8k = standard | TBD | orthogonal@mux-ceiling-sink | loopback-ceiling@layer=mux+transport=direct+shape=sink+mss=8k+scale=4MiB+metric=throughput
mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke = default | TBD | baseline@mux-over-rtp | mux-over-rtp@impairment=lossy+scale=400KiB+metric=delivery-and-goodput
mux_over_rtp_perf::mux_over_rtp_400kib_lossy_contended_perf = default | TBD | orthogonal@mux-over-rtp | mux-over-rtp@impairment=lossy+load=contended+scale=400KiB+metric=delivery-and-goodput
mux_over_rtp_perf::mux_over_rtp_small_stream_while_bulk_perf = default | TBD | composite(load,metric,shape)@mux-over-rtp | small-stream-while-bulk@load=bulk+shape=cadence+metric=ordering
mux_over_rtp_perf::mux_over_rtp_400mib_hostile_perf = full | TBD | orthogonal@hostile-probes | mux-over-rtp@impairment=hostile-preset+scale=400MiB+metric=goodput
mux_stream_fairness::mux_stream_fairness_sweep = full | TBD | composite(arm-set,metric)@fairness | fairness-sweep@flows=multi+arm-set=homogeneous-heterogeneous-throttled+metric=jain
mux_stream_fairness::mux_stream_fairness_longrun = full | TBD | re-measurement(second-arm-same-declared-point)@fairness | fairness-longrun@flows=multi+scale=multi-minute+metric=per-flow-share
rtp_longrun::longrun_duallane = full | TBD | orthogonal@fairness | dual-lane-longrun@lane=dual+scale=multi-minute+metric=goodput-and-tail
rtp_longrun::multiflow_duallane = full | TBD | baseline@fairness | multi-flow-longrun@lane=dual+flows=multi+scale=multi-minute+metric=per-flow-share
```

```gate-budgets
default = 300
standard = 600
full = 7200
perf = 7200
baseline = mandate_smoke::m1_interactive_tail_latency
baseline.constitution = rtp_mux_jitter::jitter_duallane_constitution_gate
baseline.contested = contested_latency::contested_capped_clean
baseline.decomposition = rtp_mux_jitter::jitter_frame_reorder_decomposition
baseline.dual-lane = hol_probe::dual_lane_asym_frame_delivers_and_tears_down
baseline.fairness = rtp_longrun::multiflow_duallane
baseline.fec = rtp_mux_jitter::jitter_fec_arms_2pct
baseline.fec-instrument-sanity = hol_probe::fec_gaming_treatment_has_bad_path_and_large_capacity_headroom
baseline.frame-reorder = rtp_mux_jitter::jitter_frame_reorder_fec_arms
baseline.hol-cap400 = hol_probe::hol_cap400_solo
baseline.hol-dual-lane-frame = hol_probe::hol_rtt100_ge5_dual_lane_two_interactive_frame_diag
baseline.hol-ge5-shared = hol_probe::hol_rtt100_ge5_shared
baseline.hol-ge5-solo = hol_probe::hol_rtt100_ge5_v3_solo
baseline.hol-rtt40 = hol_probe::hol_rtt40_ge1_shared
baseline.hol-shared-frame = hol_probe::hol_rtt100_ge1_shared_frame_delivery_diag
baseline.hol-solo = hol_probe::hol_rtt40_ge1_loss1_solo
baseline.hol-split = hol_probe::hol_rtt100_clean_split
baseline.hol-verify4 = hol_verify4::v4_clean_muxbulk
baseline.hostile-probes = perf_probe::probe_hostile_goodput_30s
baseline.instrument-sanity = perf_probe::controller_fat_pipe_has_only_fixed_shaping
baseline.interactive = rtp_mux_jitter::jitter_interactive_solo
baseline.latency-sweep = rtp_mux_jitter::jitter_latency_dimension_arms
baseline.lone-tail = rtp_mux_jitter::jitter_request_response_arms
baseline.m3-bulk = mandate_smoke::m3_bulk_goodput_fraction
baseline.mux-ceiling-echo = mux_ceiling_probe::probe_mux_echo_1mib_direct
baseline.mux-ceiling-sink = mux_ceiling_probe::probe_mux_sink_4mib_direct
baseline.mux-over-rtp = mux_over_rtp_perf::mux_over_rtp_lossy_perf_smoke
baseline.reorder = rtp_mux_jitter::jitter_reorder_direction
baseline.rtp-ceiling = perf_probe::probe_rtp_echo_4mib_direct
drift = 0.5
drift_floor_s = 10.0
```

These budgets are proposals, not measurements. The rows with a known cost
declare 237 s in `default` (the four `mandate_smoke` rows and the constitution
gate) and 4135 s in `perf` (26 rows, 7 of them still `TBD`); `standard` and
`full` hold only ceilings until their rows are measured. Set each tier's budget
to its measured sum plus headroom **as a declared change** once the `TBD` rows
have been measured — a tier sum over its budget is a checker failure, so an
unmeasured budget is what forces the measurement. The block carries no
`members.<family>` lines: the namespaces it would need are in "Family
membership" above, and 81 of the 94 rows' cells contradict their family's, so
applying them verbatim would make the crate's gate red on the day it is pasted
rather than land a declaration that enforces something.

The gaps below record two kinds of hole. The cells the product does not claim
(`M1@lane=single`, `soak@scale=multi-hour`, …) are the first. The
`attribution@baseline-family=…` lines are the second: a family whose reference
is not within one declared dimension of one of its rows, so that row is a
composite no existing arm can attribute. They name the row, the dimensions it
differs in, and the single-axis arm beside it that would close the gap. They
exist because the declaration is not allowed to imply an attribution the arms
cannot support.

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
attribution@baseline-family=fec = hol_probe::hol_rtp_mux_fec_default_on_recovery is four declared dimensions from the family's reference jitter_fec_arms_2pct (fec, impairment, layer, loss); no existing arm can attribute it, so a single-axis FEC-recovery arm beside it is what closes this.
attribution@baseline-family=decomposition = rtp_mux_jitter::jitter_decomposition is two declared dimensions (arms, jitter) from its only sibling jitter_frame_reorder_decomposition, and no arm states its own arm-set; a decomposition arm varying one axis is what closes this.
attribution@baseline-family=dual-lane = rtp_mux_jitter::jitter_duallane_arms is two declared dimensions (load, reorder) from its only sibling dual_lane_asym_frame_delivers_and_tears_down; a single-axis dual-lane arm is what closes this.
attribution@baseline-family=contested = contested_latency::contested_capped_jitter_loss is two declared dimensions (jitter, loss) from its clean reference; a one-axis contested arm (jitter alone, or loss alone) is what closes this.
attribution@baseline-family=instrument-sanity = perf_probe::deterministic_iid_loss_fat_pipe_is_fixed_seeded_iid_loss is two declared dimensions (lane, metric) from perf_probe::controller_fat_pipe_has_only_fixed_shaping; the two share neither, so a lane or metric arm beside either closes this.
attribution@baseline-family=lone-tail = rtp_mux_jitter::jitter_request_response_arms is two declared dimensions from each of its family's other rows (impairment with jitter, and impairment with loss); a request/response arm varying one axis is what closes this.
```

## How to make the draft real

1. Measure the `TBD` rows: one invocation per row as described above, rounding
   up to the nearest second, and replace each `TBD` with the number. The
   `#[ignore]` reasons already give estimates for `rtp_mux_jitter`; the ones
   without a number are the `hol_probe`, ceiling-probe and longrun rows.
2. Paste the three blocks into `crates/rtp_mux/GATE.md` next to the existing
   ones, and set each tier budget from the measured sums.
3. **Decide the families by their cells before pasting.** The three blocks do
   not carry `members.<family>` lines because the proposal in "Family
   membership" is rejected: 14 cell names are claimed by two or more of the
   draft's families and 81 of its 94 rows state a family whose namespace does
   not name their cells. Land the declaration either with the rows refiled
   into cell-name-coherent families — a change to the labels, not to any
   window, cadence or tier — or with a new single-axis arm per foreign row,
   and only then add the `members.<family>` lines `check-gate.py` requires;
   pasting them as they stand is a red gate.
4. Run `cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture`
   through `tools/mandate-check`, then
   `python3 ../netem_test/tools/check-gate.py --crate . rtp_mux tests GATE.md
   --mandate-check-json <run>/mandate-check.json`. The checker resolves every
   row against the compiled test set and compares the four `mandate_smoke`
   costs with the report's per-test wall-clock, so a stale or invented cost
   fails rather than passing quietly.
