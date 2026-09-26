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

### The three mandates

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

### Why the infrastructure exists

The smoke set, the hostile and lone-tail arms, and the rule that **the plots
must be read** all exist because of one hard-won lesson. A shipped `rtp`
change passed **714 unit tests**, the default tier, the burst-loss and standard
tiers, `check-gate.py`, `fmt` and `clippy` — and was still a **−37 % goodput /
+60–280 % tail** regression. It passed because it was validated only on a
low-loss, low-jitter arm. A gate roster that does not include the hostile
regime, and a verdict read without its panel, cannot see that class of
regression.

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

**Where it is not yet covered:** every one of those is opt-in. A plain
`cargo test -p rtp_mux` never measures fairness, and the tri-mandate smoke set
measures none at all — its three mandates can be satisfied by a design that
starves a flow. The always-run set has no fairness gate.

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

### Where the path stands today

Facts and numbers, as recorded at this writing.

The interactive path's transport pin is `rtp v0.0.94` (`rtp_mux/Cargo.toml`).
On that pin:

- **M1 holds on the arm the bound is asserted on** — the `clean` smoke arm —
  and `tools/mandate-check` exits zero.
- **The hostile and lone-tail defects are open.** Their arms assert derived
  regression guards, not the mandate ceiling, so a green run does **not** mean
  the field tail is fixed. Recorded signatures:
  - lone-tail p99 ≈ 0.22–1.54 s, p99.9 ≈ 0.80–3.53 s, worst sample
    ≈ 6.0–6.3 s, with a handful of samples per short window over the M1
    ceiling;
  - hostile (cadence) p99 ≈ 0.21–0.33 s, with tens of samples over the
    ceiling per short window;
  - lone-tail own-wire over the M2 budget: 6.07–6.41× on the smoke arm,
    6.22–7.17× in the field.
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
- **Clean-arm own wire is back at ~3.63×** after the revert, at its
  pre-regression level; the constitution arm's `both` case is what
  `rtp_mux/GATE.md` records as ~3.6×. (The `clean` smoke arm reads ~2.2× on
  the same pin — a different cadence and load, so the two are not
  interchangeable.)

Two decisions are open. Neither was taken unilaterally, because each trades
one mandate against another and the trade is a product call.

1. **How to cut the field tail.** Either **armour the repair**, which spends
   M2 own-wire, or **shorten the `300 ms` rung and/or the tail-probe budget**,
   which tightens a recovery parameter. The RFC 8985 §7.2/§7.3-aligned unit
   tests are sometimes read as a blocker here; under the rule above they are
   **not** one — they are re-writable with a measurement and a vacuity check.
2. **The M1-vs-M2 frontier.** On the jittered arm, a 2-shard design measured
   jitter p99 ≈ 108 ms at 5.5–5.9× own-wire, against the deployed build's
   ≈ 37 ms at ≈ 6.8×. A materially better tail for a materially larger wire
   budget; neither side dominates.

## Half 2 — the infrastructure

### `tools/mandate-check` — the one command

Run from this workspace root, with no arguments:

```sh
tools/mandate-check
```

It runs `cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture`,
renders one validated SVG+PNG per panel through `tools/mandate_plot.py`, prints
a verdict block and writes `mandate-check.json` alongside the six evidence
files. Measured cost: **177.7 s** on a warm build (12 plots). It **refuses
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

`clean` asserts the mandate bounds. `hostile` and `lone_tail` assert derived
regression guards, because asserting the mandate ceiling there would assert
something currently false. The panels draw the real mandate bounds regardless,
so a breach stays visible even where the assertion is only a guard: **the
assertion is a tripwire, the panel is the evidence.** `rtp_mux/GATE.md` lists
the three tests in `gate-default-required`, so a plain `cargo test -p rtp_mux`
runs them too; `tools/mandate-check` is the release, evidence-producing
invocation.

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
