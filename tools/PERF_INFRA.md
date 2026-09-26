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
must be labelled one. Every claimed cell names the asserting test; every cell
the set does not cover records **why**. A cell may be knowingly empty; it may
never be *silently* empty.

**The numbers are not here.** Each crate that owns perf tests states its own
tier budgets, its own nominal per-test costs and its own baseline row in its
own `GATE.md` — one authority per number, the same rule the product mandates
follow. This document states the form and the check.

The declarative form is three fenced blocks in that crate's `GATE.md`,
alongside the existing ones:

    gate-perf-design       <target>::<test> = <tier> | <nominal_cost_s> | <coverage>
    gate-budgets           <tier> = <budget_s>, plus baseline/drift/drift_floor_s
    gate-coverage-gaps     <cell> = <non-empty reason>

`<coverage>` is a comma-separated list of cells, each
`<mandate-or-property>@<dimension>=<value>[+<dimension>=<value>…]` — for
example `M1@loss=ge5+jitter=100ms+shape=request-response` — stated relative to
the baseline row named in `gate-budgets`. A row's tier is the tier the
compiled test set puts the test in (`default` means not `#[ignore]`d; the other
tiers are the `gate-manifest` tiers), and the reserved target name `lib` names
the package's `--lib` target, which is where the harness's own wall-clock
probes live. The exact grammar, the checker's failure modes and the fixture
tests that pin them are in `tools/check-gate.py` and
`tools/test_check_gate.py`.

`tools/check-gate.py` enforces the declaration for any crate whose `GATE.md`
carries the blocks: an unknown target, an unknown test, a test declared in the
wrong tier, a tier sum over its budget, an empty or malformed coverage cell, a
gap without a reason, and a missing `baseline` are all failures that name the
problem. It runs the same way as the other gate checks:

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

- **`netem_test`** (this repository) — declared, in `tests/GATE.md`.
- **`rtp_mux`** — pending. The exact rows, budgets and gap lines for it are
drafted in `tools/PERF_PENDING_rtp_mux.md`, read from the landed
`crates/rtp_mux` tree, so that crate's own iteration can apply them verbatim
and needs only to fill the costs the draft marks for measurement.
- **`rtp`** (16 perf-tier scenarios) and **`proxy`** (its `tests/src/stream.rs`
perf scenario) — pending, with no draft yet.
- **`mux`** — owes none: it keeps no opt-in scenario, and the fairness and perf
probes that used to live there moved to `rtp_mux`.

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
files. Measured cost: **205.9 s** on a warm build (10 panels, 20 SVG+PNG plot
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

### The per-arm record and the coverage comparison

The dual mandate's coverage half needs an instrument, not an argument. A
`MANDATE` line is a verdict: it says a bound was crossed, never what the arm
measured, so a shortening that halves an arm's samples can pass every assertion
while quietly weakening the p99 that assertion reads. Two tools close that
hole.

**`tools/mandate-check` records the arms.** Each `[mandate-smoke <arm>]` line
the smoke set already printed becomes an `arms` entry in `mandate-check.json`
(schema `mandate-check/3`; the record's fields are in `tools/MANDATE_SMOKE.md`):
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
failure (exit `5`) only under `--fail-on-value-drift`.

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
arm against its declared cell, not this comparison.

### The checked-in baseline

`tools/mandate-baseline.json` is the `mandate-check.json` of one real
`tools/mandate-check` run, checked in so a later run's numbers have a
reference and prior panel state is recoverable. It was taken with
`tools/mandate-check` (no arguments) on 2026-09-26, with `netem_test` at
`c7c297f6` and the sibling `rtp_mux` at `b4c4faea8f08` (change
`wmrkurmovoouxvsyuwpozsuovvkwmrrx`), which pins `rtp v0.0.94`; the run took
**182.7 s**, passed all three mandates, and rendered 12 plots. The baseline
predates M4, so its report records three mandates; a fresh `tools/mandate-check`
run writes a four-mandate report. The checked-in
copy is the run's JSON with machine-local absolute paths replaced by tokens —
every measured value, the command, the revisions and the duration are
verbatim. The plots themselves are not committed; re-run the command to
regenerate them.

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
- **`tools/check-ignored.py`** (lives in `rtp/tools/`) — the same inventory
  check for `rtp`'s lib tests and scenario targets, classifying each ignored
  test `perf-lane` (asserting) or `probe` (report-only) and refusing an
  assertion token in a probe body.
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

**Read report-only output; do not rely on it.** Several probes assert nothing
by construction — their printed counters and percentiles are the deliverable,
and their exit status says only that they ran. `rtp/GATE.md` carries the probe
inventory, and `check-ignored.py` refuses an assertion token in a probe body,
so a probe that has quietly grown a check is an error rather than a gate.
