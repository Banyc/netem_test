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

## What a run writes

Into `--dir` (the path is printed, and recorded in the report):

- `mandate-smoke.log` — the smoke set's combined stdout and stderr, so a
  compile failure or a panic is inspectable after the fact;
- `plots/<mandate>-<panel>.svg`, and `.png` unless `--no-rasterize` — the
  verified panels, each checked for series geometry and for every declared
  bound;
- `mandate-check.json` — `schema`, `ok`, `exit_code`, `verdict`,
  `started_at`, `duration_seconds`, the exact `command` and `cwd`,
  `rtp_mux.path` with its `revision`/`change_id`/`revision_source` (`jj` or
  `git`, `null` when neither resolves — never fabricated), the smoke set's
  exit code and `timed_out` flag, a `mandates` record per mandate
  (`declared`, `verdict`, the parsed `values`, the verbatim `raw_line`, the
  `plots` paths, `series_counts`, `panels`), and every `problem` found.

The verdict block printed on stdout carries the same information: the
revision and command, one line per mandate with its measured values, the
plot paths, the verdict, and every problem.

The six expected evidence files and the `plots` directory are removed from
`--dir` before the smoke set runs, so evidence found afterwards was produced
by this run and not left behind by an earlier one. Only a directory that is
empty or that carries an earlier run's `mandate-check.json` or
`mandate-smoke.log` is cleared; a directory holding anything else is refused
rather than trimmed.

## Exit codes

| code | meaning |
| --- | --- |
| `0` | all four mandates `PASS`, every series and plot present |
| `2` | the command could not do its job — missing `rtp_mux` checkout, missing smoke-set source, cargo not found, compile or test failure, timeout, a missing/malformed/duplicated `MANDATE` line, a missing/empty/mis-shaped declaration or data file, or a panel that could not be rendered or verified. The evidence is not trustworthy whatever the verdicts said |
| `3` | the evidence is complete and at least one mandate reports `FAIL` |

`2` always outranks `3`: an incomplete run is not a measurement.

## Tests

`tools/test_mandate_check.py` exercises the whole command against a fake
cargo and a fake `rtp_mux` checkout, so it needs no Rust build and no
network; `python3 -m pytest tools/ -q` runs it with the rest of the tooling
suite. It covers the passing run with its written report, a failing mandate,
a missing mandate line, a missing or empty CSV, a declaration whose series
has no rows, a compile failure, a timeout, a missing checkout and a plot that
cannot be produced, and that a directory holding someone else's files is
refused instead of cleared.
