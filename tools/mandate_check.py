#!/usr/bin/env python3
"""Run the tri-mandate performance smoke set, render its panels, and keep proof.

The mandate smoke set is ``rtp_mux``'s ``mandate_smoke`` test target. This
command runs it with ``--release``, renders each mandate's panels through
``tools/mandate_plot.py``, prints one verdict line per mandate, and writes
``mandate-check.json`` into the run directory so a reader (or a master agent)
can verify from a machine, not from prose, that the mandated checks ran and
what they measured.

    ./tools/mandate-check [--rtp-mux <path>] [--dir <out>] [--quick]

## The contract

The command depends on the following contract, which the ``mandate_smoke``
target owes it:

1. **The target and the invocation.** The set is the test target
   ``mandate_smoke`` of the ``rtp_mux`` crate, run exactly as

       cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture

   The command adds no test filter and no ``--test-threads``; the smoke set
   must therefore serialise its own measurements (its assertions are
   wall-clock).

2. **Where the evidence goes.** The smoke set must write, into the directory
   named by the environment variable ``MANDATE_CHECK_DIR`` (this command
   always sets and clears it, so a run never depends on an inherited value),

       <dir>/M1.json <dir>/M1.csv <dir>/M2.json <dir>/M2.csv
       <dir>/M3.json <dir>/M3.csv <dir>/M4.json <dir>/M4.csv

   in exactly the shape ``tools/mandate_plot.py`` consumes: ``<mandate>.json``
   is the panel declaration (``mandate``, ``title``, ``x_label``, ``y_label``
   and a non-empty ``panels`` list of ``id``/``chart``/``series``/``bounds``),
   and ``<mandate>.csv`` carries the plotted points under the header
   ``panel,series,x,y``. One CSV row per plotted point; a declared series with
   no row is an error, and so is a row whose panel or series is not declared.

3. **The verdict line.** The smoke set must print, on stdout (hence
   ``--nocapture``), exactly one line per mandate:

       MANDATE <ID> <PASS|FAIL> [<key>=<value> ...]

   with

   - ``MANDATE`` at column 1, one ASCII space between fields;
   - ``<ID>`` one of ``M1``, ``M2``, ``M3``, ``M4`` — no other id is
     accepted;
   - ``<PASS|FAIL>`` exactly, in upper case;
   - at least one ``<key>=<value>`` measurement token, whitespace separated,
     ``<key>`` matching ``[A-Za-z_][A-Za-z0-9_]*`` and ``<value>`` a
     non-empty token without whitespace (a value that parses as a finite
     number is recorded as a number, anything else as a string).

   Examples::

       MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0
       MANDATE M2 FAIL delivery=0.998 amp=7.2 budget=6.0

   The bounds these keys are compared against live in ``rtp_mux/GATE.md``;
   they are deliberately not restated here.

   A line that starts with ``MANDATE`` but does not match the grammar above
   is a failure, not a line to skip: it means the producer changed shape and
   the verdict is no longer machine-checkable. A mandate declared twice, or
   an unknown mandate id, is a failure for the same reason.

4. **``--quick``.** When ``--quick`` is passed this command sets
   ``MANDATE_SMOKE_QUICK=1`` in the child environment and unsets it
   otherwise. The smoke set must honour it by taking its shortest
   measurement windows — while still emitting all four ``MANDATE`` lines and
   all eight evidence files. A quick run is a tripwire on the assertions, not a
   substitute for the full set: read the plots.

## What it writes

Into ``--dir`` (default: a fresh directory beneath ``$TMPDIR``):

- ``mandate-smoke.log`` — the smoke set's combined stdout/stderr;
- ``plots/<mandate>-<panel>.svg`` (and ``.png`` unless ``--no-rasterize``) —
  the verified panels;
- ``mandate-check.json`` — per mandate its pass/fail verdict, the measured
  values parsed from the ``MANDATE`` lines, the plot paths and the panel
  series counts, plus the run's exact command, the ``rtp_mux`` source
  revision (its ``jj`` or ``git`` commit when resolvable), the wall-clock
  duration, every problem found and the exit code.

The six expected evidence files and the ``plots`` directory are removed from
``--dir`` before the smoke set runs, so evidence found afterwards was
produced by this run rather than left behind by an earlier one.

## Exit codes

- ``0`` — all four mandates ``PASS``, every series and plot present.
- ``2`` — the command could not do its job: missing/empty ``rtp_mux``
  checkout, missing smoke-set source, cargo not found, a compile or test
  failure, a timeout, a missing/malformed/multiple ``MANDATE`` line, a
  missing/empty/mis-shaped declaration or data file, or a panel that could
  not be rendered or verified. The evidence is not trustworthy, whatever the
  verdicts said.
- ``3`` — the evidence is complete and at least one mandate reports ``FAIL``.

Failing loudly is the point: a checker that cannot fail is worse than no
checker, so nothing here reports success on absent evidence.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = MODULE_DIR.parent

MANDATE_IDS = ("M1", "M2", "M3", "M4")
SMOKE_PACKAGE = "rtp_mux"
SMOKE_TARGET = "mandate_smoke"
SMOKE_SOURCE = Path("tests") / f"{SMOKE_TARGET}.rs"
OUT_DIR_ENV = "MANDATE_CHECK_DIR"
QUICK_ENV = "MANDATE_SMOKE_QUICK"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_CARGO = "cargo"
REPORT_NAME = "mandate-check.json"
LOG_NAME = "mandate-smoke.log"
PLOTS_DIRNAME = "plots"
REPORT_SCHEMA = "mandate-check/1"
REVISION_TIMEOUT_SECONDS = 30.0
LOG_TAIL_LINES = 20

EXIT_OK = 0
EXIT_EVIDENCE_FAILURE = 2
EXIT_MANDATE_FAILURE = 3

# ``MANDATE <ID> <PASS|FAIL> <key>=<value> ...``. The id is intentionally
# matched loosely (``M[0-9]+``) and then checked against ``MANDATE_IDS``, so an
# unknown mandate is a named failure instead of an ignored line.
MANDATE_LINE_RE = re.compile(
    r"^MANDATE (?P<mandate>M[0-9]+) (?P<verdict>PASS|FAIL)"
    r"(?:[ \t]+(?P<values>.*?))?[ \t]*$"
)
VALUE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)$")
COMMIT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
CHANGE_ID_RE = re.compile(r"^[a-z]{10,}$")


def _load_sibling(name):
    """Load a sibling tool by path, the way the other tools load each other."""
    path = MODULE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANDATE_PLOT = _load_sibling("mandate_plot")


class MandateCheckError(Exception):
    """A failure that must surface as a non-zero exit, naming the problem."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _coerce_value(text):
    """A finite number when the token is one, otherwise the token itself."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return text
    return number if math.isfinite(number) else text


def parse_mandate_lines(text):
    """Parse the ``MANDATE`` lines into ``({id: record}, [problems])``.

    Every line whose first token is ``MANDATE`` must match the contract
    grammar exactly; anything else that starts with ``MANDATE`` is reported
    as a problem rather than skipped.
    """
    records = {}
    problems = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        # Only a line that claims to be a mandate line is a candidate; a
        # test's own diagnostic prose that merely starts with the word is
        # not.
        if line != "MANDATE" and not line.startswith("MANDATE "):
            continue
        match = MANDATE_LINE_RE.match(line)
        if match is None:
            problems.append(
                f"line {line_number}: {line!r} starts with MANDATE but does not "
                "match the contract grammar "
                "'MANDATE <M1|M2|M3|M4> <PASS|FAIL> <key>=<value> ...'"
            )
            continue
        mandate = match.group("mandate")
        if mandate not in MANDATE_IDS:
            problems.append(
                f"line {line_number}: mandate {mandate!r} is not one of "
                f"{', '.join(MANDATE_IDS)}"
            )
            continue
        if mandate in records:
            problems.append(
                f"line {line_number}: mandate {mandate} is declared twice; a "
                "reader cannot tell which verdict is the run's"
            )
            continue
        values_text = match.group("values") or ""
        values, value_problems = _parse_values(mandate, values_text, line_number)
        problems.extend(value_problems)
        records[mandate] = {
            "verdict": match.group("verdict"),
            "values": values,
            "raw_line": line,
        }
    return records, problems


def _parse_values(mandate, values_text, line_number):
    values = {}
    problems = []
    for token in values_text.split():
        match = VALUE_RE.match(token)
        if match is None:
            problems.append(
                f"line {line_number}: {mandate} measurement {token!r} is not a "
                "<key>=<value> token"
            )
            continue
        key = match.group("key")
        if key in values:
            problems.append(
                f"line {line_number}: {mandate} measurement {key!r} is repeated"
            )
            continue
        values[key] = _coerce_value(match.group("value"))
    if not values:
        problems.append(
            f"line {line_number}: {mandate} reports a verdict without a single "
            "key=value measurement; a verdict that measures nothing cannot be "
            "checked"
        )
    return values, problems


def default_crate_path():
    """The sibling ``../rtp_mux`` of this workspace, where the smoke set lives."""
    return (WORKSPACE_ROOT.parent / SMOKE_PACKAGE).resolve()


def resolve_crate(requested):
    """The ``rtp_mux`` checkout, or a failure naming what is missing."""
    if requested is None:
        crate = default_crate_path()
    else:
        crate = Path(requested).expanduser().resolve()
    if not (crate / "Cargo.toml").is_file():
        raise MandateCheckError(
            f"the {SMOKE_PACKAGE} checkout {crate} has no Cargo.toml, so the "
            f"{SMOKE_TARGET!r} smoke set cannot be built from it"
        )
    source = crate / SMOKE_SOURCE
    if not source.is_file():
        raise MandateCheckError(
            f"the smoke set source {source} does not exist: the "
            f"{SMOKE_TARGET!r} target must live in "
            f"{crate / 'tests'}, or --rtp-mux names the wrong checkout"
        )
    return crate


def resolve_revision(crate):
    """``(commit_id, change_id, source)`` for the checkout, or ``(None, None, None)``.

    ``@`` is the revision whose tree a build of this checkout uses, so that is
    what is recorded. A revision that cannot be resolved is recorded as
    ``null``; it is never fabricated.
    """
    jj = shutil.which("jj")
    if jj is not None:
        result = _capture(
            [jj, "--no-pager", "log", "-r", "@", "--no-graph", "-T",
             'commit_id ++ "\\n" ++ change_id'],
            cwd=crate,
        )
        if result["exit_code"] == 0:
            lines = [line.strip() for line in result["stdout"].splitlines()]
            lines = [line for line in lines if line]
            if lines and COMMIT_ID_RE.match(lines[0]):
                change_id = None
                if len(lines) > 1 and CHANGE_ID_RE.match(lines[1]):
                    change_id = lines[1]
                return lines[0], change_id, "jj"
    git = shutil.which("git")
    if git is not None:
        result = _capture([git, "-C", str(crate), "rev-parse", "HEAD"], cwd=crate)
        if result["exit_code"] == 0:
            revision = result["stdout"].strip()
            if COMMIT_ID_RE.match(revision):
                return revision, None, "git"
    return None, None, None


def _capture(command, *, cwd):
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=REVISION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"exit_code": None, "stdout": ""}
    return {"exit_code": completed.returncode, "stdout": completed.stdout}


def prepare_output_dir(out_dir):
    """Create the run directory and clear the evidence a previous run left.

    Only the files this command owns are removed, and only from a directory
    that is either empty or carries a previous run's report or log. A
    directory holding anything else is refused rather than trimmed: a
    checker may not delete a reader's files on its way to writing its own.
    """
    plots = out_dir / PLOTS_DIRNAME
    if plots.is_file():
        raise MandateCheckError(
            f"the plots path {plots} is a file, so the panel directory cannot "
            "be created; --dir must name a directory this command may write"
        )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MandateCheckError(f"--dir {out_dir} cannot be created: {error}")
    ours = (out_dir / REPORT_NAME).is_file() or (out_dir / LOG_NAME).is_file()
    if not ours and any(out_dir.iterdir()):
        raise MandateCheckError(
            f"--dir {out_dir} already holds files and no earlier run of this "
            f"command ({REPORT_NAME} or {LOG_NAME}), so it is not this "
            "command's directory to clear; pass an empty --dir"
        )
    for mandate in MANDATE_IDS:
        for suffix in (".json", ".csv"):
            stale = out_dir / f"{mandate}{suffix}"
            if stale.is_file():
                stale.unlink()
    if plots.is_dir():
        shutil.rmtree(plots)


def smoke_command(cargo):
    """The contract invocation, with no filter and no extra harness flag."""
    return [
        cargo,
        "test",
        "--release",
        "-p",
        SMOKE_PACKAGE,
        "--test",
        SMOKE_TARGET,
        "--",
        "--nocapture",
    ]


def run_smoke(command, *, crate, out_dir, quick, timeout):
    """Run the smoke set, returning its combined output and how it ended.

    The child gets its own process group so a timeout kills the test binary
    and not just the cargo that spawned it.
    """
    env = dict(os.environ)
    env[OUT_DIR_ENV] = str(out_dir)
    env.pop(QUICK_ENV, None)
    if quick:
        env[QUICK_ENV] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(crate),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        output, _ = process.communicate()
        timed_out = True
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output": output or "",
    }


def _kill_process_group(process):
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def render_mandate(mandate, out_dir, *, rasterize, browser):
    """Render one mandate's declared panels, returning ``(summary, problems)``."""
    declaration = out_dir / f"{mandate}.json"
    data = out_dir / f"{mandate}.csv"
    problems = []
    for path, kind in ((declaration, "declaration"), (data, "data CSV")):
        if not path.is_file():
            problems.append(
                f"{mandate}: {kind} {path} was not written by the smoke set (the "
                f"producing test writes <mandate>.json and <mandate>.csv into "
                f"${OUT_DIR_ENV}={out_dir})"
            )
        elif path.stat().st_size == 0:
            problems.append(f"{mandate}: {kind} {path} is empty")
    if problems:
        return None, problems
    try:
        summary = MANDATE_PLOT.render_mandate(
            declaration,
            out_dir / PLOTS_DIRNAME,
            rasterize=rasterize,
            browser=browser,
        )
    except (MANDATE_PLOT.MandatePlotError, MANDATE_PLOT.RENDER.RenderGraphError) as error:
        return None, [f"{mandate}: {error}"]
    return summary, []


def _verify_plots(mandate, summary):
    """Every written panel must exist, be non-empty, and carry series geometry."""
    problems = []
    counts = summary.get("series_counts") or []
    if not summary.get("svg"):
        problems.append(f"{mandate}: rendering declared no panel")
    if len(counts) != summary.get("panels"):
        problems.append(
            f"{mandate}: {len(counts)} panel series count(s) for "
            f"{summary.get('panels')} declared panel(s)"
        )
    for count in counts:
        if count <= 0:
            problems.append(f"{mandate}: a rendered panel carries no series data")
    for path in list(summary.get("svg") or []) + list(summary.get("png") or []):
        written = Path(path)
        if not written.is_file() or written.stat().st_size == 0:
            problems.append(f"{mandate}: the plot {written} is missing or empty")
    return problems


def _log_tail(text, lines=LOG_TAIL_LINES):
    stripped = [line for line in text.splitlines() if line.strip()]
    return stripped[-lines:]


def build_report(args, crate, out_dir, command, revision, quick, timeout):
    report_path = out_dir / REPORT_NAME
    return {
        "schema": REPORT_SCHEMA,
        "ok": False,
        "exit_code": None,
        "verdict": None,
        "started_at": _now(),
        "duration_seconds": None,
        "timeout_seconds": timeout,
        "quick": quick,
        "command": command,
        "cwd": str(crate),
        "out_dir": str(out_dir),
        "report": str(report_path),
        "rtp_mux": {
            "path": str(crate),
            "revision": revision[0],
            "change_id": revision[1],
            "revision_source": revision[2],
        },
        "smoke": {"exit_code": None, "timed_out": False, "log": str(out_dir / LOG_NAME)},
        "mandates": {
            mandate: {
                "declared": False,
                "verdict": None,
                "values": {},
                "raw_line": None,
                "plots": [],
                "series_counts": [],
                "panels": 0,
            }
            for mandate in MANDATE_IDS
        },
        "problems": [],
    }


def write_report(out_dir, report):
    path = out_dir / REPORT_NAME
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verdict_block(report):
    """The human- and machine-readable block printed on every exit path."""
    lines = [
        f"mandate-check: {SMOKE_TARGET} in {report['rtp_mux']['path']}",
        f"  revision: {report['rtp_mux']['revision'] or 'unresolved'}"
        f" ({report['rtp_mux']['revision_source'] or 'no jj or git'})",
        f"  command:  {' '.join(report['command'])}",
        f"  output:   {report['out_dir']}",
        f"  quick:    {'yes' if report['quick'] else 'no'}"
        f"   timeout: {report['timeout_seconds']:.0f}s",
    ]
    for mandate in MANDATE_IDS:
        record = report["mandates"][mandate]
        verdict = record["verdict"] or "MISSING"
        measured = " ".join(f"{key}={value}" for key, value in record["values"].items())
        lines.append(f"{mandate} {verdict}  {measured}".rstrip())
        for path in record["plots"]:
            lines.append(f"  plot: {path}")
    duration = report["duration_seconds"]
    passed = sum(
        1 for mandate in MANDATE_IDS if report["mandates"][mandate]["verdict"] == "PASS"
    )
    panels = sum(len(report["mandates"][mandate]["plots"]) for mandate in MANDATE_IDS)
    if report["exit_code"] == EXIT_EVIDENCE_FAILURE:
        summary = (
            f"evidence incomplete ({passed}/{len(MANDATE_IDS)} mandate line(s) said "
            f"PASS, {panels} plot(s))"
        )
    else:
        summary = f"{passed}/{len(MANDATE_IDS)} mandate(s) passed, {panels} plot(s)"
    lines.append(
        f"verdict: {'PASS' if report['ok'] else 'FAIL'}  exit={report['exit_code']}  "
        + summary
        + (f", {duration:.1f}s" if duration is not None else "")
    )
    for problem in report["problems"]:
        lines.append(f"problem: {problem}")
    lines.append(f"report:  {report['report']}")
    return lines


def evaluate(args, out_dir, report, run):
    """Parse and verify the run, filling ``report`` and returning the exit code."""
    log_path = out_dir / LOG_NAME
    log_path.write_text(run["output"], encoding="utf-8")
    report["smoke"] = {
        "exit_code": run["exit_code"],
        "timed_out": run["timed_out"],
        "log": str(log_path),
    }
    problems = report["problems"]
    if run["timed_out"]:
        problems.append(
            f"the smoke set did not finish within {args.timeout:.0f}s and was "
            f"killed; its partial output is {log_path}"
        )
    elif run["exit_code"] != 0:
        problems.append(
            f"the smoke set exited {run['exit_code']} (compile or test failure); "
            f"its output is {log_path}"
        )

    records, parse_problems = parse_mandate_lines(run["output"])
    problems.extend(parse_problems)
    for mandate in MANDATE_IDS:
        if mandate not in records:
            problems.append(
                f"{mandate}: the smoke set printed no 'MANDATE {mandate} "
                "<PASS|FAIL> ...' line, so this mandate was never measured"
            )

    for mandate in MANDATE_IDS:
        record = report["mandates"][mandate]
        parsed = records.get(mandate)
        if parsed is not None:
            record["declared"] = True
            record["verdict"] = parsed["verdict"]
            record["values"] = parsed["values"]
            record["raw_line"] = parsed["raw_line"]
        summary, render_problems = render_mandate(
            mandate,
            out_dir,
            rasterize=args.rasterize,
            browser=args.browser,
        )
        if summary is not None:
            record["plots"] = list(summary.get("svg") or []) + list(summary.get("png") or [])
            record["series_counts"] = list(summary.get("series_counts") or [])
            record["panels"] = summary.get("panels", 0)
            problems.extend(_verify_plots(mandate, summary))
        problems.extend(render_problems)

    if problems:
        report["exit_code"] = EXIT_EVIDENCE_FAILURE
        report["ok"] = False
        report["verdict"] = "FAIL"
        return EXIT_EVIDENCE_FAILURE

    failed = [m for m in MANDATE_IDS if report["mandates"][m]["verdict"] == "FAIL"]
    report["ok"] = not failed
    report["verdict"] = "PASS" if not failed else "FAIL"
    if failed:
        report["exit_code"] = EXIT_MANDATE_FAILURE
        return EXIT_MANDATE_FAILURE
    report["exit_code"] = EXIT_OK
    return EXIT_OK


def default_out_dir():
    safe_root = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    try:
        return Path(tempfile.mkdtemp(prefix="mandate-check-", dir=str(safe_root)))
    except OSError as error:
        raise MandateCheckError(
            f"a run directory cannot be created beneath {safe_root}: {error}; "
            "pass --dir to name one this command may write"
        )


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mandate-check",
        description=(
            "Run the tri-mandate perf smoke set (rtp_mux's mandate_smoke target), "
            "render each mandate's panels, print a verdict block, and write "
            "mandate-check.json as machine-checkable evidence."
        ),
    )
    parser.add_argument(
        "--rtp-mux",
        type=Path,
        default=None,
        help=(
            "the rtp_mux checkout holding the smoke set "
            f"(default: the sibling ../{SMOKE_PACKAGE} of this workspace)"
        ),
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help=(
            "run directory for the evidence, the plots and mandate-check.json "
            "(default: a fresh directory beneath $TMPDIR)"
        ),
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            f"set {QUICK_ENV}=1 so the smoke set takes its shortest windows; "
            "the assertions and the evidence files must still be complete"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds before the smoke set is killed (default: {DEFAULT_TIMEOUT_SECONDS:.0f})",
    )
    parser.add_argument(
        "--cargo",
        default=DEFAULT_CARGO,
        help=f"cargo executable to build and run the smoke set (default: {DEFAULT_CARGO})",
    )
    parser.add_argument(
        "--browser",
        default=None,
        help=(
            "headless browser for the PNG step "
            "(default: $NETEM_RENDER_BROWSER or a known path)"
        ),
    )
    parser.add_argument(
        "--no-rasterize",
        dest="rasterize",
        action="store_false",
        default=True,
        help="verify and keep the panel SVGs only; skip the external PNG step",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    try:
        if args.timeout <= 0:
            raise MandateCheckError("--timeout must be positive")
        crate = resolve_crate(args.rtp_mux)
        if args.dir is not None:
            out_dir = args.dir.expanduser().resolve()
        else:
            out_dir = default_out_dir()
        prepare_output_dir(out_dir)
        cargo = shutil.which(args.cargo)
        if cargo is None:
            raise MandateCheckError(
                f"the cargo executable {args.cargo!r} was not found on PATH, so the "
                "smoke set cannot be built"
            )
    except MandateCheckError as error:
        print(f"mandate-check: error: {error}", file=sys.stderr)
        return EXIT_EVIDENCE_FAILURE

    revision = resolve_revision(crate)
    command = smoke_command(cargo)
    report = build_report(args, crate, out_dir, command, revision, args.quick, args.timeout)
    try:
        run = run_smoke(
            command,
            crate=crate,
            out_dir=out_dir,
            quick=args.quick,
            timeout=args.timeout,
        )
        exit_code = evaluate(args, out_dir, report, run)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        write_report(out_dir, report)
    except OSError as error:
        print(f"mandate-check: error: {error}", file=sys.stderr)
        return EXIT_EVIDENCE_FAILURE
    for line in verdict_block(report):
        print(line)
    if exit_code != EXIT_OK and (run["timed_out"] or run["exit_code"] != 0):
        tail = _log_tail(run["output"])
        if tail:
            print(f"--- last {len(tail)} line(s) of {out_dir / LOG_NAME} ---", file=sys.stderr)
            for line in tail:
                print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
