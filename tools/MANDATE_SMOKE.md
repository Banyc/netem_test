# The producer contract: what a perf test binary owes `tools/mandate-check`

`tools/mandate-check` is the one command that runs every **producer** of perf
arms, renders each mandate's panels, prints a verdict, and leaves evidence a
machine can check: per arm, its sample count, the statistics and counters it
measured, the windows it measured them over, and the coverage cells it is
declared to exercise. What the mandates are for, what built them, and the
wider tool and gate inventory are in `tools/PERF_INFRA.md`; this file is the
contract, and it is the form a crate's author follows to make a new test
binary recordable. Run it from this workspace root with no arguments:

```sh
./tools/mandate-check
```

It then runs every producer declared in `tools/mandate-producers.json`, so one
invocation produces per-arm records for all of them. Optional arguments, none
of them required:

| argument | default | meaning |
| --- | --- | --- |
| `--producer <id>` | every declared producer | run only this producer; repeatable |
| `--producer-path <id>=<path>` | the producer's declared `default_path` | point one producer at another checkout; repeatable, resolved against the current directory |
| `--rtp-mux <path>` | the sibling `../rtp_mux` | the documented shorthand for `--producer-path rtp_mux=<path>` |
| `--dir <out>` | a fresh directory beneath `$TMPDIR` | where the run writes its evidence |
| `--quick` | off | ask a producer that honours it for its shortest windows (`MANDATE_SMOKE_QUICK=1`) |
| `--timeout <seconds>` | 900 | how long each producer may run before it is killed |
| `--cargo <path>` | `cargo` on `PATH` | the cargo that builds and runs the producers |
| `--browser <path>`, `--no-rasterize` | rasterize with a found browser | passed through to the PNG step of `tools/mandate_plot.py` |

## The producers, and which are covered

A producer is one entry of `tools/mandate-producers.json` (schema
`mandate-producers/1`). The entry declares, and the runner honours:

- `id` — the producer's name in the report, on the command line, and the head
  of its arms' ids;
- `package`, `target`, `source` — the cargo package, the test target, and the
  source file the runner checks exists before it builds (relative to the
  producer's checkout, so a missing smoke set fails with a named file rather
  than as a cargo error);
- `cargo_args`, `test_args` — the exact invocation, `test_args` placed after
  `--`;
- `sections`, `verdicts` — every section an arm may be attributed to, and
  which of those print a `MANDATE` line and write plots (`verdicts ⊆`
  `sections`); a producer with a verdict section owes that section's evidence,
  which is derived rather than declared so a registry entry cannot declare the
  guard away;
- `log` — its own log file inside `--dir`;
- `default_path` — its checkout, resolved against this workspace (a CLI path
  is resolved against the current directory instead).

Two producers are declared today, and a default run records both:

| id | what it is | invocation | records |
| --- | --- | --- | --- |
| `rtp_mux` | the tri-mandate smoke set, `rtp_mux/tests/mandate_smoke.rs` | `cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture` | its `M1`-`M4` arms, with verdict lines and the eight evidence files |
| `netem_test` | this workspace's four perf-tier probes, in the harness `lib` target | `cargo test --release -p netem-test --lib -- --ignored --test-threads=1 --nocapture` | 4 arms in the `probe` section, report-only: no verdict line, no evidence file, no plot |

A third producer is a registry entry plus a source change in the crate that
owns the target; nothing in the runner changes. A section is an arm-id
namespace, so it may belong to only one producer: the runner refuses a
registry in which two declare the same one.

## Run it for every change to `rtp`, `mux` or `rtp_mux`, and read the plots

A change to any of the three crates on the interactive path must run this
command, and the assertion is only half of it. The `MANDATE` line is a
tripwire: it says a bound was crossed, not what moved or by how much. The
rendered panels are the evidence — latency over time and its CDF for M1,
delivery and wire for M2, per-seed goodput against the floor for M3 — and a
run whose verdict block is read without its plots has not been read at all.
A green verdict with an unread panel is how a change that halves bulk goodput
while passing every other gate gets retained.

The bounds themselves, with each one's derivation, are stated in
`rtp_mux/GATE.md` ("Performance") and in the module doc of
`rtp_mux/tests/dual_lane_mandates.rs`; they are deliberately not repeated
here.

`--quick` shortens the measurement windows, so it belongs in an edit loop,
not in a retention decision. Because these assertions are wall-clock, the
smoke set must serialise its own measurements: this command passes no
`--test-threads`, so parallel test threads would corrupt the latency numbers
it is asked to measure.

## The producer contract

`tools/mandate-check` refuses a run that does not satisfy the parts of this
contract its producer owes. It cannot tell a compliant producer from a
changed one any other way. Rule 1 and rule 6 are owed by every producer; rules
2, 3 and 4 are owed by a producer that declares a **verdict** section, which
is the subset of its sections that carries a bound, a `MANDATE` line and
plots. The `netem_test` producer declares none, so it owes rules 1, 5 and 6
only.

1. **Target and invocation** — the target is the registry entry's, run exactly
   as its `cargo_args` and `test_args` say. The command adds no test filter of
   its own and no `--test-threads`; a producer whose measurements are
   wall-clock must serialise them, either internally (the smoke set's
   assertions are wall-clock, so it holds a lock) or by declaring
   `--test-threads=1` among its `test_args`, as the probe producer does. The
   command checks the declared `source` exists before it builds, so a missing
   smoke set fails with a message naming the file instead of a cargo error.
2. **Where the evidence goes** — the smoke set writes, into the directory
   named by `$MANDATE_CHECK_DIR` (always set and cleared by this command):
   `M1.json`/`M1.csv`, `M2.json`/`M2.csv`, `M3.json`/`M3.csv`,
   `M4.json`/`M4.csv`, in exactly
   the shape `tools/mandate_plot.py` consumes — the `<mandate>.json` panel
   declaration (`mandate`, `title`, `x_label`, `y_label`, a non-empty
   `panels` list of `id`/`chart`/`series`/`bounds`) and the `<mandate>.csv`
   rows under the header `panel,series,x,y`. A producer that declares no
   verdict section owes no evidence file, and the runner requires none.
3. **The verdict line** — one line per verdict section on stdout, hence
   `--nocapture`:

   ```
   MANDATE <ID> <PASS|FAIL> <key>=<value> [<key>=<value> ...]
   ```

   `MANDATE` starts at column 1, fields are one ASCII space apart, the
   verdict is exactly `PASS` or `FAIL` in upper case, and there is at least
   one measurement token, each a `<key>=<value>` whose key matches
   `[A-Za-z_][A-Za-z0-9_]*` and whose value has no whitespace. A value that
   parses as a finite number is recorded as a number, anything else as a
   string. For example:

   ```
   MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0
   MANDATE M2 FAIL delivery=0.998 amp=7.2 budget=6.0
   ```

   A line that starts with `MANDATE ` but does not match the grammar above is
   a failure, not a line to ignore, and so are a repeated mandate, a verdict
   id the producer does not declare, and a verdict with no measurement behind
   it: a reader that skips those cannot tell a compliant producer from a
   changed one, and a verdict that measures nothing cannot be checked.
4. **`--quick`** — honour `MANDATE_SMOKE_QUICK=1` by taking the shortest
   measurement windows, while still printing all four `MANDATE` lines and
   writing all eight evidence files.
5. **The per-arm measurement lines** — one line per arm measured, in one of
   two shapes:

   - a key/value arm line, `[mandate-smoke <arm>] <key>=<value> ...` with at
     least one measurement token (values may carry the producer's own padding
     and a trailing `B`/`s` unit: `recv=  800`, `wire=   12345B`,
     `wall=12.3s`);
   - the M3 bulk-rep line, `delivered <f> MiB/s over <f>s, shaper forwarded
     <f> MiB/s, capacity <f> MiB/s, fraction <f> (<n> / <n> bytes)`.

   A key/value arm's sample count is its `recv` — the producer's own sample
   count, which the smoke set sets to the number of measured samples and a
   probe to the number of iterations it ran. This is the record of *what each
   arm measured*, as opposed to whether its bound held, and it is what a later
   run's arms are diffed against in the committed baseline.

   An arm line's **section** is how it is attributed, and there are two ways
   to say it. A producer that prints verdict lines puts each arm's line before
   that section's `MANDATE` line, which is what the smoke set does. A producer
   that asserts no bound has no `MANDATE` line to print, so its arm line
   carries the section itself as a `section=<id>` token in its body — the same
   `<key>=<value>` vocabulary the rest of the line uses, no new syntax — and
   its arm's id is `<id>/<arm>` either way:

   ```
   [mandate-smoke forwarding] section=probe recv=200000 direct_mpps=3.157 ...
   ```

   It is required rather than optional: an arm line that no `MANDATE` line
   follows and that carries no `section=` token cannot be attributed and is a
   failure, an arm attributed to a section its producer does not declare is a
   failure, a section with no arm line was never measured and is a failure, an
   arm whose coverage cell is not declared in `tools/mandate-arms.json` is a
   failure, and a run with no arm measurement at all is a failure. A
   `[mandate-smoke ...]` line matching neither shape is recorded as an arm
   *note* (prose the command does not depend on) and stays visible in the
   report instead of being dropped.

## What a run writes

Into `--dir` (the path is printed, and recorded in the report):

- one log per producer, named by its registry `log` — `mandate-smoke.log` for
  the `rtp_mux` producer and `mandate-netem_test.log` for the probes — holding
  that producer's combined stdout and stderr, so a compile failure or a panic
  is inspectable after the fact;
- `plots/<mandate>-<panel>.svg`, and `.png` unless `--no-rasterize` — the
  verified panels, each checked for series geometry and for every declared
  bound (a producer that declares no verdict section writes none);
- `mandate-check.json` — `schema` (`mandate-check/5`), `ok`, `exit_code`,
  `verdict`, `started_at`, `duration_seconds`, `producers_declared` and
  `producers_selected`, a `producers` record per *declared* producer (`id`,
  `package`, `target`, `source`, `selected`, the resolved `path`, `sections`,
  `verdicts`, `evidence`, `log`, the exact `command`, `revision`/`change_id`/
  `revision_source` (`jj` or `git`, `null` when neither resolves — never
  fabricated) and `tree_id`/`tree_id_source` (the tree the resolved revision
  points at: the identity of the **content**, which the commit id alone does
  not give, because `jj` rewrites `@` on every operation and the working-copy
  commit a build reads may be an auto-snapshot whose commit id is throwaway —
  also `null` when it cannot be resolved, never fabricated), `run.exit_code`,
  `run.timed_out`, `run.log` and the producer's arm count), the
  `mandate_order`/`section_order` the declared producers define, a `mandates`
  record per verdict section (`producer`, `declared`, `verdict`, the parsed
  `values`, the verbatim `raw_line`, `finished_at_seconds`,
  `duration_seconds`, the `plots` paths, `series_counts`, `panels`), an `arms`
  record per arm (below), the prose `arm_notes` and the `arm_declaration` the
  cells came from, and every `problem` found, each prefixed with the producer
  it came from.

The `rtp_mux` and `smoke` keys a `mandate-check/4` reader reads are kept as
that producer's identity record and run record, and are `null` when it was not
selected; `command` and `cwd` are the first selected producer's, which is the
`rtp_mux` one for the default set.

### The per-arm record

`arms` is one entry per arm a producer printed, sorted by id:

```json
{"id": "M1/clean", "mandate": "M1", "label": "clean",
 "dialect": "kv", "sample_count": 2401,
 "stats":    {"p50": 25.3, "p90": 43.0, "p99": 89.0, "p999": 97.9,
              "max": 102.5, "over250": 0, "delivery": 1.0},
 "counters": {"sent": 2401, "received": 2401, "wire_bytes": 126459,
              "bulk_wire_bytes": 4311909, "bulk_sink_bytes": 4311909},
 "windows":  {"window_seconds": 12.0, "wall_seconds": 12.4},
 "cells":    ["M1@impairment=loss2pct-iid+latency=25ms+jitter=5ms+lane=dual"],
 "values":   {"sent": 2401, "recv": 2401, "p99": 89.0, "x": 2.16, ...},
 "raw_line": "[mandate-smoke clean] sent= 2401 recv= 2401 ..."}
```

- **`producer`** — the id of the producer whose stream the arm came from, so
  two producers' arms are distinguishable in one report.
- **`mandate`** — the section the arm is attributed to: a verdict id (`M1`) or
  a section the producer declares without a verdict line (`probe`).
- **`sample_count`** — the producer's own sample count (`recv`), `null` for an
  arm that measures no per-sample distribution (the M4 per-arm aggregate).
- **`stats`** — the distribution and rate quantities the assertions read: the
  percentiles, `max`, `over250`, `delivery`, and for the bulk arm the
  delivered/shaper/capacity rates and `fraction`. Their movement is a *value*
  change. A probe's own numbers (`direct_mpps`, `cached_ns`, `median_us`, ...)
  are recorded verbatim in `values` and are not in `stats`: no assertion reads
  them, they are host values, and the record says so rather than normalising
  them into a comparison they do not carry.
- **`counters`** — the delivery and wire quantities: `sent`, `received`,
  `wire_bytes`, `bulk_wire_bytes`, `bulk_sink_bytes`, and where the arm has
  them `offered_bytes`, `delivered_bytes`, `forwarded_bytes`. Their *fall* is a
  coverage change, not noise.
- **`windows`** — the measured geometry (`window_seconds`, `wall_seconds`,
  `elapsed_seconds`). A shortened window is a coverage change.
- **`cells`** — the coverage cells the arm is declared to exercise, read from
  `tools/mandate-arms.json` by the longest matching arm-id prefix (so
  `M4/m4/clean` covers `M4/m4/clean flow A`).
- **`values`** — every parsed token verbatim, so a quantity the record does not
  normalise is still recorded rather than lost.

`arms` is a measurement, never a re-derivation: the command prints only what
it parsed from the producer's stream.

The verdict block printed on stdout carries the same information: one line
per producer with its checkout, revision, tree, command, log and exit status;
one line per verdict section with its measured values, the plot paths and the
verdict; every arm counted per producer and section; and every problem.

The eight expected evidence files, the `plots` directory, this command's own
`mandate-check.json`, and every producer's declared log are removed from
`--dir` before any producer runs — and before a checkout is validated — so
evidence found afterwards was produced by this run and not left behind by an
earlier one. Removing the earlier report is as load-bearing as removing the
evidence: `tools/mandate-compare` reads `<dir>/mandate-check.json`, so a report
that survived a run which wrote none would be compared as if it were that
run's measurement. Only a directory that is empty or that carries an earlier
run's `mandate-check.json` or `mandate-smoke.log` is cleared; a directory
holding anything else is refused rather than trimmed.

## The per-test timings

`timings` records the wall-clock the runner observed, per test and per
verdict section of every producer, so a declared nominal cost can be compared
with what the run actually took and a tier overrun is visible per test rather
than only in total:

```json
"timings": {
  "method": "streamed-line-arrival: ...",
  "origin": "smoke-child-start",
  "tests": [
    {"producer": "rtp_mux", "target": "mandate_smoke",
     "name": "m2_interactive_delivery_and_wire",
     "state": "ok", "started_at_seconds": 0.0,
     "finished_at_seconds": 24.698, "duration_seconds": 24.698}
  ],
  "mandates": [
    {"producer": "rtp_mux", "mandate": "M2", "finished_at_seconds": 24.698,
     "duration_seconds": 24.698}
  ]
}
```

It is a measurement, not a proxy: a producer's output is read as a stream,
and the arrival of each libtest result line and each `MANDATE` line is
timestamped against **that child's** start, so each entry carries the
`producer` it was observed in. A test's duration is the bracket
between its completion and the previous completion (0 for the first, i.e. the
child's start); a section's duration is bracketed the same way between its
neighbouring `MANDATE` lines. Because a producer that measures wall-clock
serialises its own measurements, the completions arrive in run order; a
bracket includes the gap
before the test started (lock wait, fixture setup), which is why the method is
recorded rather than the numbers being reported bare. A test with no result
line, or an `ignored` test, is recorded with its state and a null duration.
The `target` is the producer's registry target, which is what makes the key
`<target>::<test>` the one a crate's `gate-perf-design` row names: the probes'
rows are `lib::tests::clean_forwarding_perf_probe`, and that is the key this
records for them.

The declaring side is the owning crate's `gate-perf-design` block, and
`tools/check-gate.py` is what compares the two: it fails when a declared cost
has drifted past the tolerance the crate declares, and when a measured test
exceeds its tier budget. It only compares a row whose `<target>::<test>` the
report measured, and it says how many rows it compared.

## Comparing a run with the committed baseline

`arms` is what makes a shortening checkable, and `tools/mandate-compare` is
what checks it: it diffs a fresh report against the committed
`tools/mandate-baseline.json` and reports, per arm, exactly which quantities
moved. Every arm of every producer the report covers is compared, and the
verdict block names both runs' producers, so a variant that dropped a whole
producer's arms is not a green diff but a coverage regression.

```sh
./tools/mandate-compare <run>/mandate-check.json
```

A **coverage regression** (exit `4`) is a movement that means the arm no longer
covers what the baseline covered — the arm is gone, its sample count fell by
half or more, a delivery or wire counter fell that far or stopped being
measured, a measured window shrank by more than 1 %, a statistic the assertions
read stopped being measured, the delivery ratio fell, or a declared coverage
cell is covered by no arm any more. A **value change** is a statistics move —
a latency percentile, a goodput rate, a share — which on a shared host is
run-to-run noise: it is reported always, and is a failure (exit `5`) only under
`--fail-on-value-drift` past `--value-tolerance`.

An incomparable pair is refused (exit `2`) rather than reported as agreement: a
report whose `schema` predates `mandate-check/3` (the per-arm record; `/3`,
`/4` and `/5` are all read), a report carrying no arms, a
candidate recorded with a different window set from the baseline's, and a
coverage cell the gate checker's grammar rejects. The committed baseline must
therefore be re-recorded with a current `tools/mandate-check` before it can
certify anything — a baseline from before the per-arm record is refused, not
read as agreement.

## Exit codes

| code | meaning |
| --- | --- |
| `0` | every producer ran, every verdict section `PASS`, every series and plot present |
| `2` | the command could not do its job — a missing producer checkout or source, a registry that names no primary producer or shares a section, an unknown `--producer`, cargo not found, a producer's compile or test failure, a timeout, a missing/malformed/duplicated `MANDATE` line, a missing/empty/mis-shaped declaration or data file, a malformed, unattributable, undeclared or absent arm line, or a panel that could not be rendered or verified. The evidence is not trustworthy whatever the verdicts said |
| `3` | the evidence is complete and at least one verdict section reports `FAIL` |

`2` always outranks `3`: an incomplete run is not a measurement.

## Tests

`tools/test_mandate_check.py` exercises the whole command against a fake
cargo and fake producer checkouts, so it needs no Rust build and no
network; `python3 -m pytest tools/ -q` runs it with the rest of the tooling
suite. It covers the passing run with its written report and its per-arm
record, a failing mandate, a missing mandate line, a missing or empty CSV, a
declaration whose series has no rows, a compile failure, a timeout, a missing
checkout and a plot that cannot be produced, that a directory holding someone
else's files is refused instead of cleared, and every arm-record failure: a
run that measured no arm, a section whose arm lines are gone, an arm line that
cannot be attributed, an arm attributed to a section its producer does not
declare, an arm that claims no declared coverage cell, and a missing arm
declaration. Its two-producer cases run both producers in one invocation
against a per-package plan and assert that the report holds both producers'
arms with their own sample counts, that the `/4` records stay the primary
producer's, and that one producer's failure does not delete the other's
evidence.
