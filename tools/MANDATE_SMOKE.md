# The tri-mandate smoke set

`tools/mandate-check` is the one command that runs the tri-mandate
performance smoke set, renders each mandate's panels, prints a verdict per
mandate, and leaves evidence a machine can check. What the mandates are for,
what built them, and the wider tool and gate inventory are in
`tools/PERF_INFRA.md`; this file is the command's contract. Run it from this
workspace root with no arguments:

```sh
./tools/mandate-check
```

Optional arguments, none of them required:

| argument | default | meaning |
| --- | --- | --- |
| `--rtp-mux <path>` | the sibling `../rtp_mux` | the `rtp_mux` checkout that holds the smoke set |
| `--dir <out>` | a fresh directory beneath `$TMPDIR` | where the run writes its evidence |
| `--quick` | off | ask the smoke set for its shortest windows (`MANDATE_SMOKE_QUICK=1`) |
| `--timeout <seconds>` | 900 | how long the smoke set may run before it is killed |
| `--cargo <path>` | `cargo` on `PATH` | the cargo that builds and runs the smoke set |
| `--browser <path>`, `--no-rasterize` | rasterize with a found browser | passed through to the PNG step of `tools/mandate_plot.py` |

It runs exactly this, with no extra test filter and no extra harness flag:

```sh
cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture
```

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

## The contract the smoke set must satisfy

`tools/mandate-check` refuses a run that does not satisfy all four parts. It
cannot tell a compliant run from a non-compliant one any other way.

1. **Target and invocation** — the target is `mandate_smoke` in the `rtp_mux`
   crate, invoked as above. The command checks that
   `<rtp-mux>/tests/mandate_smoke.rs` exists before it builds, so a missing
   smoke set fails with a message naming the file instead of a cargo error.
2. **Where the evidence goes** — the smoke set writes, into the directory
   named by `$MANDATE_CHECK_DIR` (always set and cleared by this command):
   `M1.json`/`M1.csv`, `M2.json`/`M2.csv`, `M3.json`/`M3.csv`,
   `M4.json`/`M4.csv`, in exactly
   the shape `tools/mandate_plot.py` consumes — the `<mandate>.json` panel
   declaration (`mandate`, `title`, `x_label`, `y_label`, a non-empty
   `panels` list of `id`/`chart`/`series`/`bounds`) and the `<mandate>.csv`
   rows under the header `panel,series,x,y`.
3. **The verdict line** — one line per mandate on stdout, hence
   `--nocapture`:

   ```
   MANDATE <M1|M2|M3|M4> <PASS|FAIL> <key>=<value> [<key>=<value> ...]
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
   a failure, not a line to ignore, and so are a repeated mandate, an unknown
   mandate id, and a verdict with no measurement behind it: a reader that
   skips those cannot tell a compliant producer from a changed one, and a
   verdict that measures nothing cannot be checked.
4. **`--quick`** — honour `MANDATE_SMOKE_QUICK=1` by taking the shortest
   measurement windows, while still printing all four `MANDATE` lines and
   writing all eight evidence files.
5. **The per-arm measurement lines** — one line per arm measured, before that
   arm's mandate's `MANDATE` line, in one of two shapes:

   - a key/value arm line, `[mandate-smoke <arm>] <key>=<value> ...` with at
     least one measurement token (values may carry the producer's own padding
     and a trailing `B`/`s` unit: `recv=  800`, `wire=   12345B`,
     `wall=12.3s`);
   - the M3 bulk-rep line, `delivered <f> MiB/s over <f>s, shaper forwarded
     <f> MiB/s, capacity <f> MiB/s, fraction <f> (<n> / <n> bytes)`.

   A key/value arm's sample count is its `recv` — the producer's own sample
   count, which the smoke set sets to the number of measured samples. This is
   the record of *what each arm measured*, as opposed to whether its bound
   held, and it is what a later run's arms are diffed against in the committed
   baseline. It is required rather than optional: an arm line that no `MANDATE`
   line follows cannot be attributed and is a failure, a mandate with no arm
   line was never measured and is a failure, an arm whose coverage cell is not
   declared in `tools/mandate-arms.json` is a failure, and a run with no arm
   measurement at all is a failure. A `[mandate-smoke ...]` line matching
   neither shape is recorded as an arm *note* (prose the command does not
   depend on) and stays visible in the report instead of being dropped.

## What a run writes

Into `--dir` (the path is printed, and recorded in the report):

- `mandate-smoke.log` — the smoke set's combined stdout and stderr, so a
  compile failure or a panic is inspectable after the fact;
- `plots/<mandate>-<panel>.svg`, and `.png` unless `--no-rasterize` — the
  verified panels, each checked for series geometry and for every declared
  bound;
- `mandate-check.json` — `schema` (`mandate-check/3`), `ok`, `exit_code`,
  `verdict`, `started_at`, `duration_seconds`, the exact `command` and `cwd`,
  `rtp_mux.path` with its `revision`/`change_id`/`revision_source` (`jj` or
  `git`, `null` when neither resolves — never fabricated), the smoke set's
  exit code and `timed_out` flag, a `timings` record (below), a `mandates`
  record per mandate (`declared`, `verdict`, the parsed `values`, the verbatim
  `raw_line`, `finished_at_seconds`, `duration_seconds`, the `plots` paths,
  `series_counts`, `panels`), an `arms` record per arm (below), the prose
  `arm_notes` and the `arm_declaration` the cells came from, and every
  `problem` found.

### The per-arm record

`arms` is one entry per arm the smoke set printed, sorted by id:

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

- **`sample_count`** — the producer's own sample count (`recv`), `null` for an
  arm that measures no per-sample distribution (the M4 per-arm aggregate).
- **`stats`** — the distribution and rate quantities the assertions read: the
  percentiles, `max`, `over250`, `delivery`, and for the bulk arm the
  delivered/shaper/capacity rates and `fraction`. Their movement is a *value*
  change.
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

The verdict block printed on stdout carries the same information: the
revision and command, one line per mandate with its measured values, the
plot paths, the verdict, and every problem.

The six expected evidence files and the `plots` directory are removed from
`--dir` before the smoke set runs, so evidence found afterwards was produced
by this run and not left behind by an earlier one. Only a directory that is
empty or that carries an earlier run's `mandate-check.json` or
`mandate-smoke.log` is cleared; a directory holding anything else is refused
rather than trimmed.

## The per-test timings

`timings` records the wall-clock the runner observed, per test and per
mandate, so a declared nominal cost can be compared with what the run actually
took and a tier overrun is visible per test rather than only in total:

```json
"timings": {
  "method": "streamed-line-arrival: ...",
  "origin": "smoke-child-start",
  "tests": [
    {"target": "mandate_smoke", "name": "m2_interactive_delivery_and_wire",
     "state": "ok", "started_at_seconds": 0.0,
     "finished_at_seconds": 24.698, "duration_seconds": 24.698}
  ],
  "mandates": [
    {"mandate": "M2", "finished_at_seconds": 24.698, "duration_seconds": 24.698}
  ]
}
```

It is a measurement, not a proxy: the smoke set's output is read as a stream,
and the arrival of each libtest result line and each `MANDATE` line is
timestamped against the child's start. A test's duration is the bracket
between its completion and the previous completion (0 for the first, i.e. the
child's start); a mandate's duration is bracketed the same way between its
neighbouring `MANDATE` lines. Because the smoke set serialises its own
measurements, the completions arrive in run order; a bracket includes the gap
before the test started (lock wait, fixture setup), which is why the method is
recorded rather than the numbers being reported bare. A test with no result
line, or an `ignored` test, is recorded with its state and a null duration.

The declaring side is the owning crate's `gate-perf-design` block, and
`tools/check-gate.py` is what compares the two: it fails when a declared cost
has drifted past the tolerance the crate declares, and when a measured test
exceeds its tier budget. It only compares a row whose `<target>::<test>` the
report measured, and it says how many rows it compared.

## Comparing a run with the committed baseline

`arms` is what makes a shortening checkable, and `tools/mandate-compare` is
what checks it: it diffs a fresh report against the committed
`tools/mandate-baseline.json` and reports, per arm, exactly which quantities
moved.

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
report whose `schema` predates `mandate-check/3`, a report carrying no arms, a
candidate recorded with a different window set from the baseline's, and a
coverage cell the gate checker's grammar rejects. The committed baseline must
therefore be re-recorded with a current `tools/mandate-check` before it can
certify anything — a baseline from before the per-arm record is refused, not
read as agreement.

## Exit codes

| code | meaning |
| --- | --- |
| `0` | all four mandates `PASS`, every series and plot present |
| `2` | the command could not do its job — missing `rtp_mux` checkout, missing smoke-set source, cargo not found, compile or test failure, timeout, a missing/malformed/duplicated `MANDATE` line, a missing/empty/mis-shaped declaration or data file, a malformed, unattributable, undeclared or absent arm line, or a panel that could not be rendered or verified. The evidence is not trustworthy whatever the verdicts said |
| `3` | the evidence is complete and at least one mandate reports `FAIL` |

`2` always outranks `3`: an incomplete run is not a measurement.

## Tests

`tools/test_mandate_check.py` exercises the whole command against a fake
cargo and a fake `rtp_mux` checkout, so it needs no Rust build and no
network; `python3 -m pytest tools/ -q` runs it with the rest of the tooling
suite. It covers the passing run with its written report and its per-arm
record, a failing mandate, a missing mandate line, a missing or empty CSV, a
declaration whose series has no rows, a compile failure, a timeout, a missing
checkout and a plot that cannot be produced, that a directory holding someone
else's files is refused instead of cleared, and every arm-record failure: a
run that measured no arm, a mandate whose arm lines are gone, an arm line that
cannot be attributed, an arm that claims no declared coverage cell, and a
missing arm declaration.
