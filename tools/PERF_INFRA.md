# The performance infrastructure: the intention, and how to measure it

This is the operating document for anyone changing the interactive path
(`rtp`, `mux`, `rtp_mux`). It has two halves: **why** the performance
infrastructure exists — the intention behind the tri-mandate constitution —
and **what** to run, what each tool writes and what to read.

It is not an authority. The mandate bounds, their values and their derivations
live in `rtp_mux/GATE.md` §Performance (one authority per mandate). The smoke
command's contract — its invocation, evidence files, verdict grammar and exit
codes — lives in `tools/MANDATE_SMOKE.md`. Neither is restated here; follow the
pointers.

Three entry points lead here: the tri-mandate section of `AGENTS.md`,
`tools/PERF_LOOP.md` and `tools/MANDATE_SMOKE.md`.

## Half 1 — the intention

### The mandates

The product's acceptance criterion is a **tri-mandate constitution**, jointly
binding: a change that improves one mandate while violating another is a
failure, not a win.

- **M1 — interactive tail latency.** The interactive lane's tail (p99, plus a
  spike bound) must stay at its floor **including on hostile networks** —
  burst loss, jitter — **and for the request/response shape a real client
  sends**, not only a steady cadence. M1 is the operator's stated **top
  priority** where mandates conflict.
- **M2 — the interactive lane still delivers, without inflating its own
  wire.** M2 is not a byte count for its own sake. The operator's statement of
  why it exists:

  > the idea of M2 is to preserve normal throughput for interactive lane when
  > we doing aggressive latency improvement by M1, to prevent the case where
  > the interactive lane is ultra-low-latency but it can't sustain any real
  > traffic.

  So M2 exists to stop M1 from winning at the lane's expense.
- **M3 — bulk goodput** as a fraction of the configured link rate, on the same
  production dual-lane topology.
- **M4 — the interactive lane's split across several flows.** M1 and M2 each
  measure **one** interactive flow, so a design that starves one of several
  flows sharing the interactive lane can satisfy all three of the mandates
  above. M4 measures four interactive flows on the same lane, so a mandate
  result achieved by starving a flow is not a pass. Its arms, faults and bounds
  are in `rtp_mux/GATE.md` §M4; they are not restated here.

### The perf-test dual mandate (time and coverage)

The tri-mandate (and M4) constitution above bounds the **product**. Every perf
test in this workspace is additionally bound by **two mandates of its own**,
stated in `AGENTS.md` ("The perf-test dual mandate — time and coverage") and
not restated here:

1. **Time.** A test's cost is bounded, declared and paid for by its tier. The
always-run tier has a budget, and a test that cannot fit it belongs in a
slower tier — not in the always-run set with a quiet overrun. Raising a test's
cost (a longer window, more reps, another arm) is a change to its tier's
budget and must be declared as one.
2. **Coverage.** The space is `impairment × load shape × lane × layer ×
metric × scale`. Arms vary **one dimension** from a stated baseline so a
failure attributes to it; an arm that varies several is a **composite** and
must be labelled one (the `composite(<dimension>…)` relation in the block
below, whose dimensions the checker derives from the row's own cells and
refuses to take on trust). Every claimed cell names the asserting test; every
cell the set does not cover records **why**. A cell may be knowingly empty; it
may never be *silently* empty.

**The numbers are not here.** Each crate that owns perf tests states its own
tier budgets, its own nominal per-test costs and its own baseline row in its
own `GATE.md` — one authority per number, the same rule the product mandates
follow. This document states the form and the check.

The declarative form is three fenced blocks in that crate's `GATE.md`,
alongside the existing ones:

    gate-perf-design       <target>::<test> = <tier> | <nominal_cost_s> | <relation> | <coverage>
    gate-budgets           <tier> = <budget_s>, plus baseline/baseline.<family>/drift/drift_floor_s
    gate-coverage-gaps     <cell> = <non-empty reason>

`<coverage>` is a comma-separated list of cells, each
`<mandate-or-property>@<dimension>=<value>[+<dimension>=<value>…]` — for
example `M1@loss=ge5+jitter=100ms+shape=request-response` — stated relative to
the baseline of the row's family. A row's tier is the tier the
compiled test set puts the test in (`default` means not `#[ignore]`d; the other
tiers are the `gate-manifest` tiers), and the reserved target name `lib` names
the package's `--lib` target, which is where the harness's own wall-clock
probes live.

**One reference cannot serve every family, so a declaration may carry several
baselines.** `baseline = <row>` is the **default** reference a row inherits
when its relation names none; each `baseline.<family> = <row>` line declares a
named reference a row opts into with a trailing `@<family>` on its relation.
A row's relation is therefore derived against *its own family's* reference: an
M3 row measured against an M1-clean default moved four dimensions because it
is an M3 row, not because anyone built a confounded arm, and only stating it
against the M3 arm can say so.

`<relation>` is how the row stands to that baseline, and it is the coverage
half's attribution rule made checkable:

    baseline[( @<family> )]          the reference row itself
    orthogonal[( @<family> )]        its cells vary exactly one dimension from it
    composite(<dim>[,<dim>…])[@<family>]   they vary several; the row names which
    re-measurement(<reason>)[@<family>]    they vary none; the row says why it repeats it

The `@<family>` suffix names a `baseline.<family>` line; without it the row is
stated against the default, and the default's row name is printed in the run's
summary. A row that is a family's reference row carries `baseline@<family>`
(plain `baseline` for the default), and a `baseline` label that does not name
the family whose reference the row is, a relation naming a family the block
does not declare, and a declared baseline no row states a relation against are
all errors.

**Membership is a property of the row, not of the label.** Stating a relation
against a family is a claim that the row belongs to it, so every named family
also declares the **cell-name namespace** its rows live in:
`members.<family> = <prefix>` names the prefix a cell's property (the part
before the `@`) starts with — a bare name for an exact namespace, `name*` for a
prefix. The checker then derives membership from the row's own cells: every
cell of a row must be named by the namespace of the family it names, a cell
name may not be claimed by two families, a family's namespace must contain its
own reference row (the family is identified by the cells its reference
carries), and a row whose cells are named by a family's namespace must state
against that family. The **default** family is the *residual*: it owns every
cell name no `members.<family>` claims, which is the shape a crate's own
conformance (or mandate) vocabulary has — the harness's default spans
`baseline`, `blackout` and `conformance-*` and shares no prefix — so it needs
no line, and the run summary prints those residual names so a new one is
visible rather than silent. Every failure names the line or the row and what
to write instead: the checker computes the prefix the family's own cells
support, or names the family whose namespace already claims the row's cells.

The checker derives the dimensions the row varies from the row's own cells
against that family's reference. A
dimension whose value differs from the baseline's — **including one the
baseline does not state at all** — is varied; a dimension the row does not
name is inherited from the baseline. A row that varies more than one dimension
must be `composite` and must name exactly the dimensions its cells vary (that
is what makes a confounded arm impossible to file as an orthogonal one); a
row that varies none must be a `re-measurement` with a reason, since a
repeated cell is a deliberate act — a second tier, a stability re-run — and
not a silent duplicate; a row whose cells state one dimension twice has no
determinable relation and fails as ambiguous. Every one of those failures
names the row and what to write instead, and the passing run prints the
per-relation counts, one line per baseline family, and one line per composite
row naming the dimensions it varies and the reference it was derived against,
so the attribution a crate actually has is a visible number rather
than a claim in prose.

The exact grammar, the derivation rule, the checker's failure modes and the
fixture tests that pin them are in `tools/check-gate.py` and
`tools/test_check_gate.py`.

**What the relation check cannot see**, stated so its green is not read as more
than it is. It verifies the **declaration**, never the code: it cannot tell
whether an arm's implementation still measures the cell the row names, so an
arm retuned to a milder impairment while its declared cell keeps the old value
reads exactly like an unchanged one — that is what the settings' immutability
and `tools/mandate-compare`'s per-arm record are for. Its dimensions are the
cell's own keys, so a row that spells one physical axis under two names (a
`loss=none` beside an `impairment=…`, or an `impairment` that already carries
the loss) is derived as varying two dimensions: the confound is reported, the
redundancy is not. The derivation assumes a dimension the row does not name is
inherited from its family's reference: a row whose cell silently omits an axis
it actually moved is invisible here, while a row that names an axis the
reference never states is counted as varying it even when the value is the
reference's own state.

The family labels are themselves a declaration, and this is the limit the
several-baselines form **adds**. The checker derives membership from the row's
cells against the family's declared cell-name namespace, so a row cannot be
filed under a family whose cells are not named like its own: the label has to
agree with the cells. What it still cannot see is whether the *declaration* is
apt — the namespace is written by the author, so a family whose name does not
match its cells is caught only when the cells of two families collide, and a
cell name quietly added to a family's namespace is exactly as honest as its
author. It also cannot see the code: a row whose cells are foreign to its
family is reported, but whether the arm really measures the cell it names is
not, which is what the settings' immutability and `tools/mandate-compare`'s
per-arm record are for. What is **partition-invariant**, and therefore the
number to read for shortening, is how many rows have *some* other row one
dimension away at all: a row with a one-dimension relative is attributable to
that dimension whichever family it is filed under, and a row with none — every
other arm two or more declared dimensions away — cannot be attributed however
the families are cut. The declared composites are the rows that landed on the
second side of that line; the rows that landed on the first but serve as their
family's reference are the baseline rows. A family whose only sibling is two
or more dimensions away has **no usable baseline**, and the only honest
outcomes are a new single-axis arm beside it or a composite label naming what
it does vary. The **reference row** is still a declaration: the obvious way to
derive it — the member that makes the most of its family orthogonal — does not
pin one (several members tie in every family of both landed declarations) and
points the wrong way, because the varied dimensions are counted from *the row's
own keys*, so a sparser reference scores at least as well as a richer one. The
orthogonal count of a family is therefore the declared reference's, and where
tied references split differently the composite/re-measurement counts move with
the choice, so the reference stays visible in the run summary and in
`tools/mandate-compare`'s per-arm record rather than being claimed derivable.

`tools/check-gate.py` enforces the declaration for any crate whose `GATE.md`
carries the blocks: an unknown target, an unknown test, a test declared in the
wrong tier, a tier sum over its budget, an empty or malformed coverage cell, a
gap without a reason, a missing `baseline`, a named baseline whose row does not
exist, a baseline no row states a relation against, a relation naming an
undeclared family, a `baseline` label on a row that is not that family's
reference, a row that is the reference of more than one family, an unlabelled
row, a relation that disagrees with the row's cells, an ambiguous row, a family
with no `members.<family>` namespace, a namespace naming an undeclared family,
a malformed namespace, a bare `members` line, a duplicate namespace, a
namespace no row's cell name falls in, a namespace that misses its own rows, a
reference row outside its own namespace, a row whose cells are not named by the
namespace of the family it names, a cell name claimed by two families, and a
default-family row whose cells a named family claims are all failures that name
the problem and what to write instead. It runs the same way as the other gate
checks:

```sh
python3 tools/check-gate.py
```

**The measured side** is the runner's report. `tools/mandate-check` times each
test and each mandate as their output lines arrive (see
`tools/MANDATE_SMOKE.md`, "The per-test timings"), so a declared cost can be
compared with what the run took rather than with a whole-run proxy. Pass the
report to the checker (`--mandate-check-json <run>/mandate-check.json`, or
leave a `mandate-check.json` in the crate root) and it reports a drift past
the crate's declared tolerance and any measured test over its tier budget;
a report older than the declaration is skipped with a note rather than
silently believed. A declared row the report did not time is not compared,
and the checker prints how many of the declared rows it compared, so an
absent measurement is visible rather than read as agreement.

**The mechanism is central; the rows are not.** `tools/check-gate.py` is the
enforcement and it lives with the harness tooling, but a crate's budgets, its
nominal costs, its baseline and its coverage cells are that crate's own
declaration in its own `GATE.md`. The checker can name a row it cannot resolve;
it cannot know what another crate's tests cost or which cells they cover. The
perf tests this mandate governs therefore mostly live **outside** the harness:
the tri-mandate arms are `rtp_mux/tests/mandate_smoke.rs` plus that crate's
sweep and constitution targets, `rtp` holds its burst-loss, bufferbloat and FEC
tiers, and `mux` its benches. Those rows belong in those crates' `GATE.md`.
Where the migration stands:

- **`netem_test`** (this repository) — declared, in `tests/GATE.md`, and its
  perf-tier probes are a recorded producer: their four `probe-*` rows are the
  `probe` family, and `tools/mandate-check` records their arms alongside
  `rtp_mux`'s (see "The producers: which are covered").
- **`rtp_mux`** — pending. The exact rows, budgets and gap lines for it are
drafted in `tools/PERF_PENDING_rtp_mux.md`, read from the landed
`crates/rtp_mux` tree, so that crate's own iteration can apply them verbatim
and needs only to fill the costs the draft marks for measurement.
- **`rtp`** — pending, with no draft yet. What a declaration would owe is the
crate's own opt-in inventory and its per-tier tally, both re-derived from that
crate's source and printed by `crates/rtp/tools/check-ignored.py` (run
`python3 tools/check-ignored.py` from inside `crates/rtp`); the in-crate
`src/` tiers it classifies are `perf-lane` and `probe`, and the relocated
`tests/` scenario tiers are `standard`, `full` and `perf`. The command's
output is the tally — no count is transcribed here, so a probe added in that
crate cannot make this sentence wrong, which is how the count went stale
twice.
**`proxy`** (its `tests/src/stream.rs` perf scenario) — pending, with no draft
yet.
- **`mux`** — owes no perf-test declaration, not "no opt-in scenario". Its own
manifest is the authority: run
`python3 tools/check-gate.py --crate ../mux mux tests GATE.md` from here and it
prints the tiers that manifest classifies, reporting the `PENDING` note only
when a perf-tier scenario is present — which it is not, so the checker has no
perf row to enforce there. The fairness and perf probes that used to live in
`mux` moved to `rtp_mux`.

The checker's treatment of an undeclared crate is explicit and advisory — not
silent, and not fatal. A crate whose manifest has perf-tier scenarios and whose
`GATE.md` has no `gate-perf-design` block is reported with a one-line
`PENDING` note, so the migration stays visible without blocking the rest of the
gate; the checker enforces a crate's declaration the moment its blocks appear.

### Why the infrastructure exists

The smoke set, the hostile and lone-tail arms, and the rule that **the plots
must be read** all exist because of one hard-won lesson. A shipped `rtp`
change passed **714 unit tests**, the default tier, the burst-loss and standard
tiers, `check-gate.py`, `fmt` and `clippy` — and was still a **−37 % goodput /
+60–280 % tail** regression. It passed because it was validated only on a
low-loss, low-jitter arm. A gate roster that does not include the hostile
regime, and a verdict read without its panel, cannot see that class of
regression.

The change was the **fast-loss re-arm** — `rtp` change
`vluwlmkwwrkxlusvyrkppvnookxttpux` (commit `9b276c82`), abandoned, whose
description begins "re-arm fast loss for a lost retransmission on fresh SACK
evidence". It let the evidence-gated fast-loss path re-declare a lost
retransmission, which had previously fallen through to the time-based
reorder window. On the paired battery (four seeds) it measured goodput
**11.75 → 7.44 MiB/s (−36.7 %)** and RTT p99 **355 → 445 ms (+25.7 %)**,
with per-GiB retransmission attempts rising **1.3 k → 20 k**; it was
abandoned rather than landed, and is recoverable in the `rtp` repo by that
commit id.

### The field problem it targets

On one unchanged deployed build, a real client's ping showed:

| session | min | avg | max |
| --- | --- | --- | --- |
| 1 | 190 ms | 332 ms | 1063 ms |
| 2 | 195 ms | 846 ms | 3205 ms |

while every gate reported p99 ≈ 30 ms. The gates were green and the product
was not. The infrastructure exists to close that distance.

### Fairness is part of the obligation

A mandate result achieved by starving a flow is **not** a pass. Fairness is a
first-class thing to measure alongside M1/M2/M3, not an optional extra.

Where it is covered today:

- `mux`'s `fair_queue` — the per-receiver byte-fair scheduler the mux layer
  drains streams through.
- `mux_stream_fairness` (sweep and longrun; tiers in `rtp_mux/GATE.md`) —
  per-stream Jain fairness and a per-stream starvation share across
  homogeneous, heterogeneous and throttled arms.
- `hol_probe::hol_rtt100_ge5_four_interactive_frame_delivery` — four
  interactive streams sharing one lane must keep per-flow delivery and a
  bounded p50 relative to the solo reference (`rtp_mux/GATE.md`).
- `mux/GATE.md` records that the fairness floors moved with these scenarios to
  `rtp_mux`; the floors themselves are the constants in the owning tests.

**Where the always-run set covers it:** M4 in the smoke set —
`rtp_mux`'s `mandate_smoke::m4_interactive_lane_fairness`, default tier and
listed in `gate-default-required` — measures the interactive lane's split across
four flows on both the clean and hostile arms, so a plain `cargo test -p rtp_mux`
and `tools/mandate-check` both gate on per-flow fairness. Its bounds, arms and
faults are in `rtp_mux/GATE.md` (§M4) and are not restated here. The bulleted
instruments above remain opt-in additional coverage.

### What is frozen, and what is not

Two different things, and confusing them blocks work that is allowed.

**Frozen — the perf test settings.** The gates, arms, windows, cadences,
thresholds and tiers in `rtp_mux/tests/`, `rtp/tests/`, the harness's lane
taxonomy (`tests/GATE.md`, `gate-lane-roles`) and the bounds in every
`GATE.md`. Never retune a threshold, arm, window, cadence or tier to make
something pass. If a regime is missing — a hostile or jittered arm, a
lone-tail/request-response arm, a fairness arm — **write a new test alongside
the old one and record it in the owning crate's `GATE.md`.**

**Not frozen — the RFC-derived transport parameters.** This protocol is **not
RFC compliant**; it follows TCP for baseline best practice, and that does not
mean the parameters cannot be nudged or that a better idea is off limits.
Specifically fair game: `MIN_RTO`, the pre-first-RTT-sample PTO (`1 s`),
`TAIL_PROBED_MIN_RTO` (`300 ms`), the tail-probe budget, and the unit tests
that pin them (`pkt_send_space::tail_probe_abstains_when_all_acked_or_before_first_rtt_sample`,
`tlp::pre_probe_rto_uses_1s_floor`).

The discipline that replaces the freeze: **a change to one of them must be
justified by a measurement, and any test it invalidates must be rewritten to
assert the NEW intended behaviour with a vacuity check — never deleted, and
never loosened to fit.** A test asserting superseded RFC-derived arithmetic is
not a reason to leave a measured field defect in place.

**How to make a consumer build a local `rtp` for a measurement.** The
checked-in manifests keep the published git tag — `rtp_mux/Cargo.toml` pins
`rtp v0.0.94` and only comments out `# rtp = { path = "../rtp" }` — and must
not be repointed for a measurement: uncommenting that line changes what every
later run builds, and the crates-level `DEPENDENCY_SOURCES.md` calls the
resulting tag-vs-local mismatch the "silent verdict" trap. The supported
route is the frozen-suite snapshot, which exports each sibling's committed
tree and rewrites every inter-component git-tag locator to the exported
sibling's relative path: `tools/perf-loop snapshot` (the rewrite, its record
in `frozen_dep_rewrites` and the refusal to run a suite whose sibling edges
still resolve from a tag are in `tools/PERF_LOOP.md`). For a one-off
measurement outside that flow, copy the consumer and the sibling `rtp` into a
scratch directory under the build tmp root and repoint **only the copy's**
manifest; a copy left inside the crate tree is the scratch `tools/hygiene.py`
reports.

### Where the path stands today

Facts and numbers, as recorded at this writing.

The interactive path's transport pin is `rtp v0.0.94` (`rtp_mux/Cargo.toml`).
On that pin:

- **M1 holds on the arm the bound is asserted on** — the `clean` smoke arm —
  and `tools/mandate-check` exits zero.
- **The hostile and lone-tail defects are open, and they are M1 defects.**
  The breach is the 250 ms ceiling and the samples over it. Their arms assert
  derived regression guards, not the mandate ceiling, so a green run does
  **not** mean the field tail is fixed. Recorded signatures:
  - lone-tail p99 ≈ 0.22–1.54 s, p99.9 ≈ 0.80–3.53 s, worst sample
    ≈ 6.0–6.3 s, with a handful of samples per short window over the M1
    ceiling;
  - hostile (cadence) p99 ≈ 0.21–0.33 s, with tens of samples over the
    ceiling per short window.
- **The lone-tail arm's own wire is informational, not a defect.** It reads
  6.07–6.41× on the smoke arm and 6.22–7.17× in the field. That multiple is
  the accepted cost of the better tail at that impairment, not a budget to
  close; the `clean` arm is where M2's budget is asserted (see "What \"done\"
  means").
- **The mechanism** is a compounding repair ladder. Once the lone tail's
  six-datagram cover is exhausted, each further rung waits
  `TAIL_PROBED_MIN_RTO` (`300 ms`) compounded onto the current RTO, so one
  losing episode's latency is a multiple of 300 ms.
- **Known-wrong in `rtp_mux`, recorded 2026-09-26.**
  `crates/rtp_mux/GATE.md:158` names the hostile defect's mechanism as "the
  1 s `MIN_RTO` repair floor plus exponential backoff", which understates what
  was measured — the compounding `TAIL_PROBED_MIN_RTO` (`300 ms`) rung ladder
  above (a seeded probe produces 613/918/1222/1520 ms rungs; the field's 1063
  and 3205 ms maxima are 3 and 10 rungs). `GATE.md:66` and
  `tests/dual_lane_mandates.rs:17` there also cite "the README's zero >250 ms
  spikes criterion", but no README in `rtp_mux`, `rtp` or `mux` states it —
  the phrase is in `netem_test/tests/README.md`. Both are corrections for the
  next agent in that crate, which was held by another agent at this writing.
- **Clean-arm own wire is back at ~3.63×** after reverting the **fresh-tail
  cover split** — `rtp` change `tnlxylvslomrkyzyozlroqmkrovowomt` (commit
  `f0b22e2e5301`), reverted by `xnzwoywuszqrylnpxsllsspluusporpr` (commit
  `7f68486688dd`), which is `rtp v0.0.94`. The split stopped paying the
  interactive fresh-tail cover on a pipelined tail; measured on the dual-lane
  arms it cost p99 **29.6 → 83.0 ms** at 6 % iid and **231.8 → 478.1 ms**
  under Gilbert-Elliot burst with the bulk lane loaded, with samples over
  250 ms rising 12 → 46. Own wire is back at its pre-regression level; the
  constitution arm's `both` case is what `rtp_mux/GATE.md` records as ~3.6×.
  (The `clean` smoke arm reads ~2.2× on the same pin — a different cadence
  and load, so the two are not interchangeable.)

The M1-vs-M2 trade under jitter is **decided**, not open: the deployed
configuration stands. On the jittered arm a 2-shard design measured p99
≈ 108 ms at 5.5–5.9× own-wire, against the deployed build's ≈ 37 ms at
≈ 6.8×. The lower-wire alternative was measured and **not taken** — the tail
is worth the wire — and those two numbers are recorded here once, as a closed
matter. It is not a candidate, and the wire multiple it would have saved is
not a defect to close. **M1 is the standing priority** where the mandates
conflict; that priority is settled and is not re-decided per regime.

The open item is the field tail itself (see "Where the path stands today").
It is cut by either **armouring the repair**, which spends M2 own-wire, or
**shortening the `300 ms` rung and/or the tail-probe budget**, which tightens
a recovery parameter. The RFC 8985 §7.2/§7.3-aligned unit tests are sometimes
read as a blocker here; under the rule above they are **not** one — they are
re-writable with a measurement and a vacuity check.

### What "done" means

Done means: **every smoke-set arm meets M1's mandate bound** — p99 ≤ 250 ms
and **zero** samples over 250 ms — with `delivery == 1.000`. Full stop.

**M2's 6× own-wire budget** is asserted where it is meaningful, on the
`clean` arm, which carries the real budget assertion. In the impaired regimes
the measured wire multiple is **informational**: it says what the better tail
costs there, and it is not a target and not a defect to close. The hostile and
lone-tail arms' guards (900 ms / 3200 ms / 8000 ms / 8 % / 10× / 14×) are the
**intended permanent shape** on those arms — a regression tripwire that fires
if an impaired arm gets worse — not a placeholder waiting to be tightened to
the mandate bound. The M1 breach those arms keep visible stays the open
defect (see "Where the path stands today"); the guards are not that defect and
do not change shape when it is fixed.

**M1 is the standing priority** where the mandates conflict. That is settled,
and there is nothing here to decide or sign off: in the jitter regime the
deployed configuration buys p99 ≈ 37 ms at ≈ 6.8×, where the lower-wire
alternative measures p99 ≈ 108 ms at ≈ 5.5–5.9×, and the product takes the
tail.

## Half 2 — the infrastructure

### `tools/mandate-check` — the one command

Run from this workspace root, with no arguments:

```sh
tools/mandate-check
```

It runs `cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture`,
renders one validated SVG+PNG per panel through `tools/mandate_plot.py`, prints
a verdict block and writes `mandate-check.json` alongside the eight evidence
files. Measured cost: **216.3 s** on a warm build (10 panels, 20 SVG+PNG plot
files). It **refuses
loudly rather than reporting success on absent evidence** — it fails when it
cannot produce the evidence as well as when a mandate fails. The contract,
what it writes and its exit codes are in `tools/MANDATE_SMOKE.md`.

### The smoke set

`rtp_mux/tests/mandate_smoke.rs`, run over three arms in the shape the field
sends — all on the production dual-lane topology:

| arm | impairment | interactive load | bulk |
| --- | --- | --- | --- |
| `clean` | 2 % iid, 25 ms one-way, 5 ms jitter | 256 B cadence | 2 MiB / 3 s |
| `hostile` | GE `gilbert_elliott_loss(5, 8)`, 25 ms one-way, 100 ms jitter | 256 B cadence | 2 MiB / 3 s |
| `lone_tail` | same GE + jitter | request/response, depth 1 | none |

`clean` asserts the mandate bounds — M1's ceiling and M2's 6× budget — so it
is the arm that carries the real budget assertion. `hostile` and `lone_tail`
assert derived regression guards, which are the **intended permanent shape**
on those arms: a tripwire that fires when an impaired arm gets worse, not a
bound waiting to be tightened, and not the place M2 is enforced. On those arms
the ceiling does not hold today (the open M1 field-tail defect) and the wire
multiple is informational. The panels draw the real mandate bounds regardless,
so a breach stays visible even where the assertion is only a guard: **the
assertion is a tripwire, the panel is the evidence.** The guard values and the
arms asserting them live in `rtp_mux` (`rtp_mux/GATE.md`,
`rtp_mux/tests/mandate_smoke.rs`) and are unchanged. `rtp_mux/GATE.md` lists
the three tests in `gate-default-required`, so a plain `cargo test -p rtp_mux`
runs them too; `tools/mandate-check` is the release, evidence-producing
invocation.

### The producers: which are covered

`rtp_mux`'s smoke set is the producer the instrument was built for, and it is
no longer the only one. A **producer** is one entry of
`tools/mandate-producers.json`: its cargo invocation, its source, its log, the
sections its arms are attributed to, and which of those sections print a
`MANDATE` line and write plots. `tools/mandate-check` runs **every** declared
producer by default, so a single invocation produces per-arm records for all
of them — that is what makes the coverage-preservation comparison available to
a crate that is not `rtp_mux`. Two producers are declared and covered today:

- **`rtp_mux`** — the tri-mandate smoke set above
  (`cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture`): its
  `M1`-`M4` arms, its verdict lines and the eight evidence files.
- **`netem_test`** — this workspace's four perf-tier probes, in the harness
  `lib` target
  (`cargo test --release -p netem-test --lib -- --ignored --test-threads=1 --nocapture`):
  four report-only arms in the `probe` section. They assert no bound, so they
  print no `MANDATE` line and write no evidence; each arm line carries its own
  section (`section=probe`), which is how a producer with no verdict line
  still reports attributable arms.

The contract a producer owes, and the form a crate's author follows to make a
new test binary recordable, is in `tools/MANDATE_SMOKE.md`; the cells each
producer's arms cover are declared in `tools/mandate-arms.json`.

### The per-arm record and the coverage comparison

The dual mandate's coverage half needs an instrument, not an argument. A
`MANDATE` line is a verdict: it says a bound was crossed, never what the arm
measured, so a shortening that halves an arm's samples can pass every assertion
while quietly weakening the p99 that assertion reads. Two tools close that
hole.

**`tools/mandate-check` records the arms.** Each `[mandate-smoke <arm>]` line
a producer printed becomes an `arms` entry in `mandate-check.json` (schema
`mandate-check/5`, which over `/4` adds the `producers` map — one record per
declared producer, with its invocation, revision, tree, log, exit status and
arm count — and a `producer` field on every arm; `/4` over `/3` added the
`rtp_mux` `tree_id`, the content a run built, since a commit id read from
`jj`'s `@` is an auto-snapshot jj rewrites; the record's fields are in
`tools/MANDATE_SMOKE.md`):
the arm id and mandate, its **sample count** (the producer's own `recv`), the
**statistics** the assertions read (`p50`/`p90`/`p99`/`p999`/`max`/`over250`, the
bulk rates), the **delivery and wire counters** (`sent`, `received`,
`wire_bytes`, `bulk_wire_bytes`, `bulk_sink_bytes`, and the
offered/delivered/forwarded bytes where the arm has them), the **measured
windows**, every parsed token verbatim, and the **declared coverage cells** the
arm exercises, read from `tools/mandate-arms.json` (a declaration naming what
each smoke arm covers, matched by the longest arm-id prefix). The record is
required, not decorative: a run that measured no arm, a mandate whose arm lines
are gone, an arm line that cannot be attributed, and an arm that claims no
declared cell each fail the run.

**`tools/mandate-compare` turns a difference into a verdict.** It compares a
fresh report with the committed `tools/mandate-baseline.json`:

```sh
./tools/mandate-compare <run>/mandate-check.json
```

A **coverage regression** (exit `4`) is a movement that means the arm no longer
covers what the baseline covered: the arm is gone, its sample count fell by
half or more, a delivery or wire counter fell that far or stopped being
measured, a measured window shrank by more than 1 %, a statistic the assertions
read stopped being measured, the delivery ratio fell past its tolerance, a
declared cell is covered by no arm any more, or a mandate vanished. A **value
change** is a statistics move — a latency percentile, a goodput rate, a share —
which on a shared host is run-to-run noise: it is reported always and is a
failure (exit `5`) only under `--fail-on-value-drift`. Every arm of every
producer the report covers is compared, and the verdict block names both runs'
producers: a producer whose arms the candidate did not record is a coverage
regression, not a green diff over the arms that happen to be left.

The count tolerance is 50 % rather than tight because that is the run-to-run
band two recorded full runs of the unchanged tree showed on the
request/response arm (819 -> 421 samples and 514 -> 882) — a tolerance tight
enough to catch a 20 % shortening would red-flag a healthy run on a contended
host. The **measured window** is what carries the sharp shortening signal and
is compared at 1 %. That split is the comparison's **detection limit**, stated
rather than left implicit: it sees a dropped arm, a halved sample count, a
shortened window, a delivery or wire counter that fell or stopped being
measured, a statistic that disappeared, and a coverage cell no longer covered.
It cannot see an arm that keeps its sample count and counters while its
**impairment was quietly weakened** — a 2 % loss arm retuned to 1 % measures the
same shape under a milder regime. That is a change to a frozen perf-test
setting, and the guard against it is the setting's immutability plus reading the
arm against its declared cell, not this comparison. Nor is every count
regression one: the counts have no absolute floor, so a counter small enough
that half of it is a handful of datagrams can cross the tolerance on an
*unchanged* tree — measured on the `M1/lone_tail` arm of two real full runs,
where `bulk_wire_bytes` read 1920 and then 785, a counter that arm does not
claim (it declares no bulk lane). `tools/mandate_compare.py`'s docstring states
that false-positive mode and the floor that would remove it.

### The checked-in baseline

`tools/mandate-baseline.json` is the `mandate-check.json` of one real
`tools/mandate-check` run, checked in so that `tools/mandate-compare` has a
reference: it records the per-arm measurements a later run's coverage is
compared against. It was taken with `tools/mandate-check --no-rasterize` (no
`--quick`, and no other argument that reaches a producer — `--no-rasterize`
only skips the PNG step, so the arms are those of a default run) on
2026-09-26, with the runner at commit `9c4fcabe` (change
`nnozsvltxvvnuqzkmvkoknvsrquuwttr`; the working-copy snapshot read at that
moment was empty, so the recorded runner `tree_id`
`d72f0d5824461f0eded3b0af4d8b5a7024b114cd` is what actually names the content
it built) and the sibling `rtp_mux` at commit `4632257a` (change
`xwwmprmsltkkkuwpuvkvwkoukusvuooo`), whose tree `937a25b0` pins `rtp v0.0.95`
and `mux v0.0.31`. The run took **147.1 s**, passed all four mandates with 10
verified SVG panels, and recorded **23 arms from both producers**: 19 for
`rtp_mux` (3 M1, 3 M2, 3 M3 reps and 10 M4 arms) and the 4 probes
(`probe/forwarding` over 200 000 iterations, `probe/deadline` over 1 000,
`probe/std-udp` over 21 paired medians, `probe/dest-cache` over 5 000 000).
Its `timings` cover both producers, so `tools/check-gate.py
--mandate-check-json` drift-checks the four `lib::tests::*` rows as well as
`rtp_mux`'s. The checked-in copy is the run's JSON with machine-local absolute
paths replaced by tokens (`<baseline run dir>`, `<rtp_mux checkout>`,
`<netem_test checkout>`, `<cargo>`); every
measured value, the command, the revisions, the trees and the duration are
verbatim. The plots themselves are not committed; re-run the command to
regenerate them.
Because the comparison refuses a baseline whose schema predates the per-arm
record, this file has to be re-recorded with a current `tools/mandate-check`
(no `--quick`) whenever the runner or a producer changes shape; the checked-in
file is schema `mandate-check/5`. A `/4` file is still read, but it carries
one producer's arms only, so a run compared against it cannot show the second
producer's coverage — re-record rather than compare across the addition.

### `tools/mandate_plot.py` — the validated panel renderer

Reads a `<mandate>.json` panel declaration and its `<mandate>.csv`, writes one
verified SVG per panel (and a PNG by default). It **refuses** a malformed
declaration, a zero-series or empty panel, a CSV row naming an undeclared
panel or series, and a declared bound that did not reach the SVG. A panel that
cannot be produced is an error, not a file to skim past.

### `tools/perf-loop` — the paired A/B battery (`tools/PERF_LOOP.md`)

Validates a **change**, not a state: it freezes a baseline and a candidate
suite (each with sibling `rtp`, `mux`, `rtp_mux`, `tokio_udp`,
`udp_listener`), runs the same probe against both under one link profile and
seed set, and compares traces. `snapshot` exports the committed trees and
rewrites inter-component locators to the exported siblings; `run` builds one
frozen executable per role, then runs each seed in both roles and reports
readiness and a verdict; `analyze` recomputes the analysis on a preserved
result without rerunning anything.

That one-liner is a digest of `tools/PERF_LOOP.md`: its introduction and
`snapshot` section (the freeze and the locator rewrite), plus `Frozen
executables and counterbalanced order`, `Rendered graph evidence
(mandatory)`, `Safe paths and workspace topology` and `Verdicts and exit
codes`.

Its cost is the freeze plus one timed run per role per seed (the documented
defaults are `--seeds 11,21 --window-seconds 30` with a warmup), so it is much
slower than `tools/mandate-check`. Use it when a delta needs attribution
under the lane taxonomy in `tests/GATE.md` (`gate-lane-roles`); use
`tools/mandate-check` to check the mandates themselves. Its mandatory rendered
graph is produced by `tools/render_graph.py`.

### One line each, the supporting tools

- **`tools/render_graph.py`** — extracts and verifies the `<svg>` panels of a
  perf-loop `comparison.html` and writes standalone SVG+PNG; an unproducible
  or data-free panel is a non-zero-exit error, not a blank file.
- **`tools/check-gate.py`** — re-derives each crate's opt-in scenario set and
  tiers from the compiled test binaries and fails when a crate's `GATE.md`
  manifest disagrees, so an `#[ignore]` skip cannot go unnoticed; it also
  enforces the report-only/asserting split.
- **`rtp/tools/check-ignored.py`** — the same inventory check for `rtp`'s lib
tests and scenario targets: it re-derives every `#[ignore]`d test and
requires `rtp/GATE.md`'s `ignored-manifest` to classify it — `perf-lane` and
`probe` for the in-crate `src/` set, `standard`/`full`/`perf` for the
relocated `tests/` scenario targets, with the split enforced. A `perf-lane`
body must still carry an assertion token; a `probe` body must carry one
**and** match the assertion-token count recorded for it in `rtp/GATE.md`'s
`gate-probe-selfchecks` block, so a probe that stopped validating its own
measurement, or quietly gained or lost a check under the ignore flag, fails
with the probe and the recorded count named; the tier that must carry
**no** assertion token is the report-only `perf` scenario, and the asserting
helpers it reaches are `netem_test/tools/check-gate.py`'s
`gate-perf-guard-helpers` closure, not this checker's.
- **`tools/hygiene.py`** — report-only sweep for stale jj registrations,
  leaked test processes, stale scratch, wrong jj layout and stalled logs;
  non-zero when blocking findings exist, and it never repairs anything.
- **`tools/samply_hotspots.py`** — reduces a presymbolicated Samply profile to
  deterministic owning-symbol hotspots (count- and CPU-ranked), so a
  per-datagram CPU cost is measured by attribution rather than by a goodput
  delta it cannot move.

### The existing (frozen) gates and sweeps

These own the mandate outcomes today; their settings are immutable.

- `rtp_mux_jitter` (`rtp_mux/GATE.md`) — the constitution gates
  `jitter_duallane_constitution_gate` (default tier; deterministic counts) and
  `jitter_duallane_constitution_gate_p99` (`full` tier; median of three
  seeded runs), plus the report-only arm families: loss/queue decomposition,
  frame-reorder, FEC at 2 % and 6 %, non-loss impairments, reorder rate and
  direction, interactive solo/with-bulk/with-loss, dual-lane arms, burst-loss
  arms, **request-response (lone-tail) arms**, shared-bottleneck,
  latency-dimension, cellular-timeline and bulk-idle-restart arms.
- `dual_lane_mandates` (`full`) — the M3 capacity-fraction floor on the
  deployment's own bulk-lane configuration.
- `rtp/tests/rtp_burst_loss.rs` (`full`) and `rtp/tests/rtp_bufferbloat.rs`
  (`standard`) — the transport-level bulk goodput and tail-latency floors,
  plus `rtp`'s report-only `probe_*` tests and its `perf-lane` sub-linear
  scaling tests (`rtp/GATE.md`).

A missing regime gets a **new** test written down in the owning crate's
`GATE.md` — never a retuned old one.

### Workspaces during active work

Work on the interactive path runs in sibling jj workspaces, each registered
by the repo of the crate it tracks, so a `crates/` listing may hold several
`<crate>_ws` directories at once — one per crate being worked on, plus a
preserved candidate — beside the crates' default checkouts; such a directory
is a registered workspace, not a stale copy. A crate's local checkout can
also be ahead of what its consumers build: `crates/rtp`'s `dev` bookmark sits
at commit `61fc7ba9` (`test(rtp): measure the lone tail's repair deadline on
a seeded connection`), one commit past `rtp v0.0.94`. The authority for what a
run actually built is the **tag the consumer pins** — `rtp_mux/Cargo.toml`
pins `rtp v0.0.94`, and `mandate-check.json` records the resolved `rtp_mux`
revision — while the local `dev` is the authority only for local edits, not
for the measurement.

### Running a single arm or a single probe

Arm families and probes are `#[ignore]`d, so name the test and serialise:

```sh
cargo test --release -p rtp_mux --test rtp_mux_jitter -- \
    --ignored jitter_request_response_arms --nocapture --test-threads=1
cargo test --release -p rtp --lib -- \
    --ignored probe_lone_tail_repair_deadline_latency --nocapture
```

**Read report-only output; do not rely on it.** A probe's printed counters and
percentiles are its deliverable, and it asserts nothing about the *product* —
no bound from `rtp/GATE.md` §Performance appears in a probe body. It is not an
inert instrument, though: a probe asserts its own measurement (the arm ran its
whole load, no echo missed its deadline, the counters it prints actually
arrived and agree with the arm's label), so a zero-sample or dead-instrument
run fails instead of printing a table of zeros a reader could mistake for a
measurement. `rtp/GATE.md` carries the probe inventory in its
`ignored-manifest` and each probe's assertion-token count in
`gate-probe-selfchecks`; how many probes record one, and whether each count is
at least one and equals the probe body's token count, is what
`crates/rtp/tools/check-ignored.py` prints and exits 0 on — no tally is
transcribed here. It requires the count to be at least one **and** to equal
the probe body's token count, so a probe that lost its validation, or quietly
gained, lost or moved a check under the ignore flag, is an error that names
the probe. The checkers refuse
an assertion token in the report-only **`perf` scenario** tier —
`netem_test/tools/check-gate.py` scans a relocated scenario's own body and the
asserting helpers it reaches (`gate-perf-guard-helpers`) — never in a `probe`.
