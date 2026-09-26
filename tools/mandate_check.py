#!/usr/bin/env python3
"""Run the tri-mandate performance smoke set, render its panels, and keep proof.

The mandate smoke set is ``rtp_mux``'s ``mandate_smoke`` test target. This
command runs it with ``--release``, renders each mandate's panels through
``tools/mandate_plot.py``, prints one verdict line per mandate, and writes
``mandate-check.json`` into the run directory so a reader (or a master agent)
can verify from a machine, not from prose, that the mandated checks ran and
what they measured.

It also times the run per test and per mandate, and records what each perf
*arm* measured. The smoke set is read as a stream (not waited on and then read
whole), so the arrival of every libtest completion line, every ``MANDATE`` line
and every ``[mandate-smoke <arm>]`` line is timestamped against the child's
start. Those arrivals give each test and each mandate a wall-clock duration
bracketed between two observed lines: the smoke set serialises its own
measurements, so the completions arrive in run order, and a per-test duration
includes the gap before the test started (fixture setup, lock wait). The
report records the method alongside the numbers so a reader knows what was
measured, and a declared nominal cost that has drifted from this measured
wall-clock is visible rather than assumed (the owning crate's
``gate-perf-design`` block states the declared cost, ``check-gate.py``
compares them).

The per-arm record is the other half. A mandate verdict says a bound was
crossed; it does not say what the arm *measured*, so a shortened arm can pass
every assertion while quietly halving its own sample count. ``arms`` therefore
records, per arm, the sample count, the distribution statistics the assertions
read, the delivery and wire counters, the measured windows and the coverage
cells the arm is declared to exercise (``tools/mandate-arms.json``), so that a
later run's arms can be diffed against a committed baseline and a sample count
that fell can be called a coverage regression rather than noise. ``arms`` is a
record of what the producer printed; it invents nothing.

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

5. **The per-arm measurement lines.** The smoke set must print, on stdout or
   stderr, one line per arm it measured, before that arm's mandate's
   ``MANDATE`` line, in one of two shapes:

   - a key/value arm line, ``[mandate-smoke <arm>] <key>=<value> ...`` with at
     least one measurement token, ``<key>`` matching
     ``[A-Za-z_][A-Za-z0-9_]*`` and ``<value>`` a whitespace-free token, an
     optional trailing ``B`` or ``s`` unit and leading spaces allowed
     (``recv=  800``, ``wire=   12345B``, ``wall=12.3s``);
   - the M3 bulk-rep line,
     ``[mandate-smoke <arm>] delivered <f> MiB/s over <f>s, shaper forwarded
     <f> MiB/s, capacity <f> MiB/s, fraction <f> (<n> / <n> bytes)``.

   ``<arm>`` is the producer's own label (``clean``, ``hostile``,
   ``lone_tail``, ``m3/rep1``, ``m4/clean flow A``, ...). An arm line belongs
   to the mandate whose ``MANDATE`` line next follows it. For a key/value arm
   the sample count is its ``recv`` — the producer's own sample count.

   This is a record of the arms a run measured, so it is required, not
   optional: a key/value arm line that no ``MANDATE`` line follows cannot be
   attributed and is a failure, a mandate with no arm line at all is a failure
   (its arms were never measured), an arm whose coverage cell is not declared
   in ``tools/mandate-arms.json`` is a failure (a cell may be knowingly empty,
   never silently empty), and a run with no arm measurement at all is a
   failure. A ``[mandate-smoke `` line that matches neither shape is recorded
   as an arm *note*: prose the command does not depend on, kept visible in the
   report — and, when every arm line degrades that way, the missing-arm
   failures above fire rather than the record quietly emptying.

## What it writes

Into ``--dir`` (default: a fresh directory beneath ``$TMPDIR``):

- ``mandate-smoke.log`` — the smoke set's combined stdout/stderr;
- ``plots/<mandate>-<panel>.svg`` (and ``.png`` unless ``--no-rasterize``) —
  the verified panels;
- ``mandate-check.json`` — per mandate its pass/fail verdict, the measured
  values parsed from the ``MANDATE`` lines, the per-test and per-mandate
  wall-clock timings observed on the child's output stream, the plot paths
  and the panel series counts, plus the run's exact command, the ``rtp_mux``
  source revision (its ``jj`` or ``git`` commit when resolvable), the
  wall-clock duration, every problem found and the exit code.
  ``schema`` is ``mandate-check/3``: the record adds ``arms`` (one entry per
  measured arm: ``id``, ``mandate``, ``label``, ``dialect``, ``sample_count``,
  the normalised ``stats``/``counters``/``windows``, every parsed ``values``
  token verbatim, the declared coverage ``cells`` and ``raw_line``),
  ``arm_notes`` (the prose-only arm lines, with their mandate when one can be
  attributed) and ``arm_declaration`` (the declaration the cells were read
  from). ``timings`` and ``mandates`` are unchanged, so a reader of
  ``mandate-check/2`` keeps working.

The eight expected evidence files, the ``plots`` directory, and this
command's own ``mandate-check.json`` and ``mandate-smoke.log`` are removed from
``--dir`` before the smoke set runs, so evidence found afterwards was produced
by this run rather than left behind by an earlier one. The previous report is
removed for the same reason the evidence is: ``tools/mandate-compare`` reads
``<dir>/mandate-check.json``, so a report surviving a run that wrote none would
be compared as if it were that run's measurement.

## Exit codes

- ``0`` — all four mandates ``PASS``, every series and plot present.
- ``2`` — the command could not do its job: missing/empty ``rtp_mux``
  checkout, missing smoke-set source, cargo not found, a compile or test
  failure, a timeout, a missing/malformed/multiple ``MANDATE`` line, a
  missing/empty/mis-shaped declaration or data file, a malformed,
  unattributable, undeclared or absent arm line, or a panel that could not be
  rendered or verified. The evidence is not trustworthy, whatever the
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
import threading
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
REPORT_SCHEMA = "mandate-check/3"
ARMS_DECLARATION_NAME = "mandate-arms.json"
ARMS_DECLARATION_SCHEMA = "mandate-arms/1"
REVISION_TIMEOUT_SECONDS = 30.0
LOG_TAIL_LINES = 20
# How long the line reader may take to drain after the child exits or is
# killed, before the run is reported with whatever the reader captured.
READER_JOIN_SECONDS = 10.0

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
# A libtest result line: `test <name> ... ok` / `... FAILED` / `... ignored,
# <reason>`. The marker and the result are flushed together, so the result's
# arrival is the test's end. A libtest progress note
# (`test <name> has been running for over 60 seconds`) and the smoke set's own
# lines do not match a state and are not completions.
TEST_RESULT_RE = re.compile(r"^test (?P<name>\S+) \.\.\. ?(?P<tail>.*)$")
TEST_STATES = ("ok", "FAILED", "ignored")
MANDATE_TIMING_RE = re.compile(r"^MANDATE (?P<mandate>M[0-9]+) ")
# An arm line: `[mandate-smoke <label>] <body>`. The label is the producer's
# own arm name and may carry alignment padding, so it is stripped.
ARM_LINE_RE = re.compile(r"^\[mandate-smoke (?P<label>[^\]]*)\](?:[ \t]+(?P<body>.*?))?[ \t]*$")
# `recv=  800`, `wire=   12345B`, `max_share=+0.2502`: the value's own leading
# spaces are alignment from the producer's format string, not a separator.
ARM_TOKEN_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=[ \t]*(?P<value>[^\s=]+)")
# The M3 per-rep line, the one arm line whose body is prose rather than
# key=value tokens. `shaper forwarded` is the shaper's forwarded-byte counter.
ARM_BULK_REP_RE = re.compile(
    r"^delivered (?P<delivered_mib_s>[0-9.]+) MiB/s over (?P<elapsed_seconds>[0-9.]+)s, "
    r"shaper forwarded (?P<shaper_mib_s>[0-9.]+) MiB/s, "
    r"capacity (?P<capacity_mib_s>[0-9.]+) MiB/s, "
    r"fraction (?P<fraction>[0-9.]+) \((?P<delivered_bytes>[0-9]+) / (?P<forwarded_bytes>[0-9]+) bytes\)$"
)
# How a parsed token is normalised. A key here is a *measurement* the arm
# record distinguishes from the verbatim `values` map; anything not named is
# still recorded verbatim. `stats` are the distribution/rate quantities whose
# movement is a value (latency, goodput, shares); `counters` are the delivery
# and wire quantities whose *fall* is a coverage regression; `windows` are the
# measurement geometry, also coverage.
ARM_STAT_KEYS = (
    "p50", "p90", "p99", "p999", "max", "min", "mean", "std", "over250",
    "delivery", "fraction", "share", "imbalance", "min_share", "max_share",
    "ideal_share", "delivered_mib_s", "shaper_mib_s", "capacity_mib_s",
)
ARM_COUNTER_KEYS = {
    "sent": "sent",
    "recv": "received",
    "received": "received",
    "wire": "wire_bytes",
    "wire_bytes": "wire_bytes",
    "bulk_sink": "bulk_sink_bytes",
    "bulk_sink_bytes": "bulk_sink_bytes",
    "bulk_wire": "bulk_wire_bytes",
    "bulk_wire_bytes": "bulk_wire_bytes",
    "forwarded": "forwarded_bytes",
    "forwarded_bytes": "forwarded_bytes",
    "delivered": "delivered_bytes",
    "delivered_bytes": "delivered_bytes",
    "offered": "offered_bytes",
    "offered_bytes": "offered_bytes",
}
ARM_WINDOW_KEYS = {
    "window": "window_seconds",
    "window_s": "window_seconds",
    "wall": "wall_seconds",
    "wall_s": "wall_seconds",
    "elapsed": "elapsed_seconds",
    "elapsed_seconds": "elapsed_seconds",
    "measured_s": "measured_seconds",
}
# A value's optional unit suffix: the producer prints `12345B` and `12.3s`. The
# number is recorded; the unit is not a second quantity to compare.
ARM_UNIT_SUFFIXES = ("B", "s")
TIMING_METHOD = (
    "streamed-line-arrival: a test's completion is the arrival of its libtest "
    "result line, and its duration is bracketed against the previous "
    "completion (0 for the first, the child's start); a mandate's duration is "
    "bracketed the same way against its neighbouring MANDATE lines. A bracket "
    "includes the gap before the test started"
)


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


def _coerce_measure(token):
    """A measurement token as ``(value, unit)``: `12345B` -> (12345, "B").

    The producer's format strings pad a value (`recv=  800`) and suffix a unit
    (`wire=   12345B`, `wall=12.3s`); the number is what a comparison reads, so
    the unit is returned alongside rather than kept in the token.
    """
    unit = None
    body = token
    if len(token) > 1 and token[-1] in ARM_UNIT_SUFFIXES:
        candidate = token[:-1]
        if _is_number(candidate):
            body, unit = candidate, token[-1]
    return _coerce_value(body), unit


def _is_number(text):
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def parse_arm_line(line):
    """One ``[mandate-smoke <arm>]`` line as an arm record, or ``None``.

    Either shape in the contract is an arm: the key/value form, whose
    measurement tokens are normalised into ``stats``/``counters``/``windows``,
    and the M3 bulk-rep form, whose prose fields are the same three kinds. A
    ``[mandate-smoke ...]`` line matching neither is a *note* — prose the
    command does not depend on — and is returned as ``{"note": ...}`` so the
    caller can keep it visible instead of dropping it silently. A line that is
    not an arm line at all is ``None``.
    """
    match = ARM_LINE_RE.match(line)
    if match is None:
        return None
    label = match.group("label").strip()
    body = (match.group("body") or "").strip()
    if not label:
        return {"label": label, "body": body, "values": {}, "dialect": None, "note": True}
    repeated = []
    bulk = ARM_BULK_REP_RE.match(body)
    if bulk is not None and len(bulk.group(0)) == len(body):
        values = {
            key: _coerce_value(text) for key, text in bulk.groupdict().items()
        }
        dialect = "bulk-rep"
    else:
        values = {}
        for token in ARM_TOKEN_RE.finditer(body):
            key, raw = token.group("key"), token.group("value")
            if key in values:
                repeated.append(key)
                continue
            values[key] = _coerce_measure(raw)[0]
        if not values:
            return {
                "label": label,
                "body": body,
                "values": {},
                "dialect": None,
                "note": True,
            }
        dialect = "kv"
    record = _arm_record(label, body, dialect, values)
    if repeated:
        record["repeated_keys"] = sorted(set(repeated))
    return record


def _arm_record(label, body, dialect, values):
    """The normalised arm record for a parsed arm line.

    ``sample_count`` is the producer's own sample count — for a cadence or
    request/response arm that is exactly its ``recv`` (the smoke set sets
    ``received = samples.len()``), and ``null`` where the arm measures no
    per-sample distribution (the M4 per-arm aggregate).
    """
    stats = {}
    counters = {}
    windows = {}
    for key, value in values.items():
        if key in ARM_STAT_KEYS:
            stats[key] = value
        if key in ARM_COUNTER_KEYS:
            counters[ARM_COUNTER_KEYS[key]] = value
        if key in ARM_WINDOW_KEYS:
            windows[ARM_WINDOW_KEYS[key]] = value
    sample_count = counters.get("received")
    if isinstance(sample_count, bool) or not isinstance(sample_count, (int, float)):
        sample_count = None
    else:
        sample_count = int(sample_count)
    return {
        "id": None,
        "mandate": None,
        "label": label,
        "dialect": dialect,
        "sample_count": sample_count,
        "stats": stats,
        "counters": counters,
        "windows": windows,
        "cells": [],
        "values": values,
        "raw_line": f"[mandate-smoke {label}]" + (f" {body}" if body else ""),
    }


def parse_arm_lines(events, problems):
    """The run's arms, attributed to the mandate each one precedes.

    Every ``[mandate-smoke ...]`` line is a candidate; the mandate is the id
    of the ``MANDATE`` line that next arrives, which is the order the smoke set
    emits (one mandate's arm lines, then its ``MANDATE`` line). Returns
    ``(arms, notes)`` with ``arms`` sorted by id.

    The guards are vacuity guards: an arm line that can never be attributed, a
    mandate with no arm line, an arm with no declared coverage cell, and a run
    with no arm measurement at all are all problems, so an arm set that
    quietly empties is a failure rather than a report with an empty ``arms``.
    """
    arms = []
    notes = []
    pending = []

    def flush(mandate):
        for entry in pending:
            if entry.get("note"):
                notes.append(_public_arm_note(entry, mandate))
                continue
            entry.pop("note", None)
            entry["mandate"] = mandate
            entry["id"] = f"{mandate}/{entry['label']}"
            arms.append(entry)
        pending.clear()

    for event in events:
        line = event["line"].rstrip()
        if line != "MANDATE" and line.startswith("MANDATE "):
            matched = MANDATE_LINE_RE.match(line)
            mandate = matched.group("mandate") if matched is not None else None
            if mandate in MANDATE_IDS:
                flush(mandate)
            continue
        parsed = parse_arm_line(line)
        if parsed is not None:
            pending.append(parsed)
    for entry in pending:
        if entry.get("note"):
            notes.append(_public_arm_note(entry, None))
            continue
        problems.append(
            f"the arm line {entry['raw_line']!r} follows the last 'MANDATE' line "
            "and cannot be attributed to a mandate; the smoke set must print an "
            "arm's line before that arm's mandate's MANDATE line"
        )
    pending.clear()
    arms.sort(key=lambda arm: arm["id"])
    return arms, notes


def _public_arm_note(entry, mandate):
    """A prose ``[mandate-smoke ...]`` line, kept visible rather than dropped."""
    return {
        "mandate": mandate,
        "label": entry["label"],
        "body": entry["body"],
    }

def declared_cells(arm_id, cells):
    """The declared cells for an arm id: the longest matching prefix.

    A declaration key names an arm id or an arm-id prefix (`M1/clean`,
    `M4/m4/clean`, `M3/m3`), so a family of arms — one per flow, one per rep,
    `m4/clean flow A` — is declared once and the most specific entry wins. The
    match ends at a word boundary: the character after the prefix must be
    neither alphanumeric nor `_`/`-`, so `M4/m4/clean` covers `M4/m4/clean flow
    A` while `M1/clean` does not cover `M1/cleanup`. An arm no key matches has
    no declared cell, which the caller reports: a cell may be knowingly empty,
    never silently empty.
    """
    best = None
    for key in cells:
        if not arm_id.startswith(key):
            continue
        if len(arm_id) > len(key) and (arm_id[len(key)].isalnum() or arm_id[len(key)] in "_-"):
            continue
        if best is None or len(key) > len(best):
            best = key
    return sorted(cells[best]) if best is not None else []


def load_arm_declaration(path, problems):
    """The arm coverage declaration, or a named failure.

    It is this command's own file (it declares what the producer's arms cover),
    so a missing or malformed one is a failure: without it, no arm's coverage
    cells can be recorded and the comparison would have nothing to compare.
    """
    if not path.is_file():
        problems.append(
            f"the arm coverage declaration {path} does not exist, so no arm's "
            "coverage cells can be recorded; it travels with this command"
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        problems.append(f"the arm coverage declaration {path} cannot be read: {error}")
        return None
    if not isinstance(payload, dict):
        problems.append(f"the arm coverage declaration {path} is not a JSON object")
        return None
    if payload.get("schema") != ARMS_DECLARATION_SCHEMA:
        problems.append(
            f"the arm coverage declaration {path} declares schema "
            f"{payload.get('schema')!r}, not {ARMS_DECLARATION_SCHEMA!r}"
        )
        return None
    cells = payload.get("cells")
    if not isinstance(cells, dict) or not cells:
        problems.append(
            f"the arm coverage declaration {path} declares no cells, so no arm "
            "can claim a covered cell"
        )
        return None
    for key, value in cells.items():
        if not isinstance(key, str) or not key:
            problems.append(f"the arm coverage declaration {path} has a non-string key")
            return None
        if not isinstance(value, list) or not value or not all(
            isinstance(cell, str) and cell for cell in value
        ):
            problems.append(
                f"the arm coverage declaration {path} gives {key!r} no cell; a "
                "cell may be knowingly empty but never silently empty"
            )
            return None
    return payload


def stamp_arm_coverage(arms, declaration, problems):
    """Attach each arm's declared coverage cells, failing on an undeclared arm."""
    if declaration is None:
        return
    cells = declaration["cells"]
    for arm in arms:
        arm["cells"] = declared_cells(arm["id"], cells)
        if not arm["cells"]:
            problems.append(
                f"the arm {arm['id']!r} covers no declared cell; add it to the "
                f"arm coverage declaration ({ARMS_DECLARATION_NAME}), because a "
                "cell may be knowingly empty but never silently empty"
            )


def check_arm_coverage(arms, notes, problems):
    """Require every mandate's arms to have been measured, or say which not.

    A mandate whose arm lines are absent has had its coverage silently deleted,
    so it is a problem; a run with no arm measurement at all is the same
    failure stated once.
    """
    measured = {arm["mandate"] for arm in arms}
    seen = {note["mandate"] for note in notes if note["mandate"] is not None}
    for mandate in MANDATE_IDS:
        if mandate not in measured:
            problems.append(
                f"{mandate}: no '[mandate-smoke <arm>]' measurement line was "
                "attributed to it, so the mandate's arms were never measured"
            )
    if not arms:
        problems.append(
            "the run printed no '[mandate-smoke <arm>] ...' arm measurement at "
            "all, so no arm's coverage was recorded; an instrument that "
            "returns nothing has deleted the coverage it exists to provide"
        )


def _arm_summary(arms, notes):
    """One line per mandate: how many arms it measured and how many samples."""
    lines = []
    for mandate in MANDATE_IDS:
        of_mandate = [arm for arm in arms if arm["mandate"] == mandate]
        if not of_mandate:
            lines.append(f"  {mandate} arms: none measured")
            continue
        counts = [arm["sample_count"] for arm in of_mandate]
        known = [count for count in counts if count is not None]
        samples = f"{sum(known)} sample(s)" if known else "no per-sample count"
        labels = ", ".join(arm["label"] for arm in of_mandate)
        lines.append(
            f"  {mandate} arms: {len(of_mandate)} ({labels}), {samples}"
        )
    if notes:
        lines.append(f"  arm notes (prose, not compared): {len(notes)}")
    return lines


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
    """Create the run directory and clear what an earlier run left in it.

    Every file this command's run writes is removed: the eight evidence files
    the smoke set produces, the ``plots`` directory, this command's report and
    the smoke set's log. The report is removed for the same reason the
    evidence is — a reader (``tools/mandate-compare``) reads
    ``<dir>/mandate-check.json``, so one surviving a run that wrote none would
    be read as that run's measurement. Removal happens before the smoke set is
    built and before the checkout is validated, so no exit path can leave a
    previous run's report standing.

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
    for name in (REPORT_NAME, LOG_NAME):
        stale = out_dir / name
        if stale.is_file():
            stale.unlink()
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
    """Run the smoke set, returning its output, how it ended and its timeline.

    The child gets its own process group so a timeout kills the test binary
    and not just the cargo that spawned it. Its combined output is read as a
    stream and every line is timestamped against the child's start, so the
    per-test and per-mandate timings are observations of the run rather than
    proxies for it. ``events`` is that timeline; ``output`` is the same lines
    rejoined, so every reader that only wants the text is unaffected.
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
        bufsize=1,
        start_new_session=True,
    )
    started = time.monotonic()
    events = []

    def pump():
        for line in process.stdout:
            events.append(
                {"seconds": round(time.monotonic() - started, 3), "line": line.rstrip("\n")}
            )

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        timed_out = True
    reader.join(timeout=READER_JOIN_SECONDS)
    output = "\n".join(event["line"] for event in events)
    if output:
        output += "\n"
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output": output,
        "events": events,
    }


def derive_timings(events, target):
    """The run's per-test and per-mandate wall-clock, from the line timeline.

    Every libtest result line and every ``MANDATE`` line is an observed
    instant. A test's duration is the bracket between its result's arrival and
    the previous result's, a mandate's the bracket between its ``MANDATE``
    line and the previous one; the first bracket runs from the child's start.
    A ``FAILED`` or ``ignored`` result is recorded with its state and, for
    ``ignored``, a null duration (it never ran). Nothing here is inferred: an
    absent line stays absent.
    """
    tests = []
    mandates = []
    previous_completion = 0.0
    previous_mandate = 0.0
    for event in events:
        line = event["line"].strip()
        seconds = event["seconds"]
        mandate = MANDATE_TIMING_RE.match(line)
        if mandate is not None:
            mandates.append(
                {
                    "mandate": mandate.group("mandate"),
                    "finished_at_seconds": seconds,
                    "duration_seconds": round(max(seconds - previous_mandate, 0.0), 3),
                }
            )
            previous_mandate = seconds
            continue
        result = TEST_RESULT_RE.match(line)
        if result is None:
            continue
        tail = result.group("tail").strip()
        state = next(
            (
                candidate
                for candidate in TEST_STATES
                if tail == candidate or tail.startswith(candidate + " ")
                or tail.startswith(candidate + ",")
            ),
            None,
        )
        if state is None:
            # A libtest progress note (`has been running for over 60
            # seconds`) is not a completion.
            continue
        entry = {
            "target": target,
            "name": result.group("name"),
            "state": state,
            "started_at_seconds": previous_completion,
            "finished_at_seconds": seconds,
            "duration_seconds": None,
        }
        if state != "ignored":
            entry["duration_seconds"] = round(
                max(seconds - previous_completion, 0.0), 3
            )
            previous_completion = seconds
        tests.append(entry)
    return {
        "method": TIMING_METHOD,
        "origin": "smoke-child-start",
        "tests": tests,
        "mandates": mandates,
    }


def apply_mandate_timings(report, timings):
    """Attach each mandate's measured wall-clock to its record in the report."""
    by_mandate = {entry["mandate"]: entry for entry in timings["mandates"]}
    for mandate, record in report["mandates"].items():
        entry = by_mandate.get(mandate)
        if entry is None:
            continue
        record["finished_at_seconds"] = entry["finished_at_seconds"]
        record["duration_seconds"] = entry["duration_seconds"]
    report["timings"] = timings


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
                "finished_at_seconds": None,
                "duration_seconds": None,
            }
            for mandate in MANDATE_IDS
        },
        "timings": {"method": TIMING_METHOD, "origin": "smoke-child-start", "tests": [], "mandates": []},
        "arms": [],
        "arm_notes": [],
        "arm_declaration": {
            "path": str(MODULE_DIR / ARMS_DECLARATION_NAME),
            "schema": ARMS_DECLARATION_SCHEMA,
            "source": None,
            "declared_cells": 0,
        },
        "problems": [],
    }


def write_report(out_dir, report):
    path = out_dir / REPORT_NAME
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def apply_arm_records(report, events, declaration):
    """Record the run's per-arm measurements, or name why they are missing.

    Every guard here is a vacuity guard: the record must be non-empty, every
    arm must be attributable to a mandate, and every arm must claim a declared
    coverage cell. An empty or partial record is a failure, not an empty list.
    """
    problems = report["problems"]
    if declaration is not None:
        report["arm_declaration"]["source"] = declaration.get("source")
        report["arm_declaration"]["declared_cells"] = sum(
            1 for cells in declaration["cells"].values() for cell in cells
        )
    arms, notes = parse_arm_lines(events, problems)
    stamp_arm_coverage(arms, declaration, problems)
    check_arm_coverage(arms, notes, problems)
    report["arms"] = arms
    report["arm_notes"] = notes
    return arms


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
        duration = record.get("duration_seconds")
        if duration is not None:
            lines.append(f"  duration: {duration:.2f}s (bracketed wall-clock)")
        for path in record["plots"]:
            lines.append(f"  plot: {path}")
    lines.append(f"arms: {len(report.get('arms') or [])} measured")
    lines.extend(_arm_summary(report.get("arms") or [], report.get("arm_notes") or []))
    declaration = report.get("arm_declaration") or {}
    if declaration.get("source") is not None:
        lines.append(
            f"  cells: {declaration.get('declared_cells', 0)} declared in "
            f"{Path(declaration.get('path') or '').name}"
        )
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


def evaluate(args, out_dir, report, run, declaration):
    """Parse and verify the run, filling ``report`` and returning the exit code."""
    log_path = out_dir / LOG_NAME
    log_path.write_text(run["output"], encoding="utf-8")
    report["smoke"] = {
        "exit_code": run["exit_code"],
        "timed_out": run["timed_out"],
        "log": str(log_path),
    }
    # The per-test and per-mandate wall-clock observed on the child's output
    # stream, so a cost the declaration claims can be compared with what the
    # run actually took, per test, rather than only in total.
    apply_mandate_timings(report, derive_timings(run.get("events") or [], SMOKE_TARGET))
    # What each arm measured, so a later run's coverage can be diffed against a
    # committed baseline instead of argued about.
    apply_arm_records(report, run.get("events") or [], declaration)
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
        if args.dir is not None:
            out_dir = args.dir.expanduser().resolve()
        else:
            out_dir = default_out_dir()
        # The run directory is cleared before anything is validated, so that
        # no later failure can leave an earlier run's report standing where a
        # comparison would read it as this run's.
        prepare_output_dir(out_dir)
        crate = resolve_crate(args.rtp_mux)
        cargo = shutil.which(args.cargo)
        if cargo is None:
            raise MandateCheckError(
                f"the cargo executable {args.cargo!r} was not found on PATH, so the "
                "smoke set cannot be built"
            )
    except MandateCheckError as error:
        print(f"mandate-check: error: {error}", file=sys.stderr)
        return EXIT_EVIDENCE_FAILURE

    declaration_problems = []
    declaration = load_arm_declaration(
        MODULE_DIR / ARMS_DECLARATION_NAME, declaration_problems
    )
    if declaration_problems:
        for problem in declaration_problems:
            print(f"mandate-check: error: {problem}", file=sys.stderr)
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
        exit_code = evaluate(args, out_dir, report, run, declaration)
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
