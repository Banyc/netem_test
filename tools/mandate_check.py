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

    ./tools/mandate-check [--producer <id> ...] [--dir <out>] [--quick]

## The producers

A run records the arms of every **producer** it selects. A producer is one
entry of ``tools/mandate-producers.json``, the declaration that says what a
producer is: its cargo invocation, its source, the *sections* its arms are
attributed to, which of those sections print a ``MANDATE`` line and write
plots, and where its log goes. Two producers are declared. ``rtp_mux`` is the
tri-mandate smoke set this command was built for; ``netem_test`` is this
workspace's own perf-tier probes. ``--producer <id>`` selects one or more, and
with none named **every** declared producer runs, so one run produces per-arm
records for both. ``--producer-path <id>=<path>`` points one producer at
another checkout, and ``--rtp-mux <path>`` is the documented shorthand for the
``rtp_mux`` one.

The report records each producer's own invocation, revision, tree, log, exit
status and arm count under ``producers``, and each arm carries the ``producer``
that printed it. The ``rtp_mux``/``smoke`` keys a ``mandate-check/4`` reader
reads are kept as that producer's record (``null`` when it was not selected).

## The contract

The command depends on the following contract, which every declared producer
owes it. ``tools/MANDATE_SMOKE.md`` states the same contract as the form a
crate's author follows; the runners and the failure modes are these.

1. **The target and the invocation.** A producer's target and cargo arguments
   are its registry entry's, and its declared ``test_args`` are appended after
   ``--``. The command adds no test filter of its own and no
   ``--test-threads``; a producer whose measurements are wall-clock must
   serialise them, either internally or with ``--test-threads=1`` among its
   declared ``test_args``. The ``rtp_mux`` producer is run exactly as

       cargo test --release -p rtp_mux --test mandate_smoke -- --nocapture

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
   - ``<ID>`` one of the verdict sections the producing crate declares — for
     ``rtp_mux``, ``M1``, ``M2``, ``M3``, ``M4`` and no other id;
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
   ``lone_tail``, ``m3/rep1``, ``m4/clean flow A``, ...). An arm line whose
   body carries ``section=<id>`` is **self-attributing**: ``<id>`` must be one
   of the producer's declared sections and the arm's id is ``<id>/<arm>``,
   which is how a producer with no ``MANDATE`` line to print (a report-only
   perf probe, which asserts no bound) still records attributable arms. Any
   other arm line belongs to the section whose ``MANDATE`` line next follows
   it. For a key/value arm the sample count is its ``recv`` — the producer's
   own sample count.

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
  source revision (its ``jj`` or ``git`` commit and change ids, and the tree
  that revision points at, each when resolvable), the wall-clock duration,
  every problem found and the exit code. ``schema`` is ``mandate-check/4``:
  over ``mandate-check/3`` the ``rtp_mux`` record gains ``tree_id`` and
  ``tree_id_source`` — the identity of the *content* the run built, which the
  commit id alone does not give, because ``jj`` rewrites ``@`` on every
  operation and the working-copy commit a build reads may be an auto-snapshot
  whose commit id is throwaway. ``mandate-check/3`` adds ``arms`` (one entry per
  measured arm:
  ``id``, ``mandate``, ``label``, ``dialect``, ``sample_count``, the
  normalised ``stats``/``counters``/``windows``, every parsed ``values`` token
  verbatim, the declared coverage ``cells`` and ``raw_line``), ``arm_notes``
  (the prose-only arm lines, with their mandate when one can be attributed)
  and ``arm_declaration`` (the declaration the cells were read from).
  ``timings`` and ``mandates`` are unchanged, so a reader of
  ``mandate-check/2`` or ``mandate-check/3`` keeps working.

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

# The ids the ``rtp_mux`` producer's verdict lines use. They are the unit
# tests' fallback: every producer declares its own sections, and a verdict id
# is only accepted when the producer that printed it declares it.
MANDATE_IDS = ("M1", "M2", "M3", "M4")
# The producer ``--rtp-mux`` and ``default_crate_path`` name, and the one whose
# record the report keeps under the ``rtp_mux``/``smoke`` keys a
# ``mandate-check/4`` reader reads.
PRIMARY_PRODUCER = "rtp_mux"
OUT_DIR_ENV = "MANDATE_CHECK_DIR"
QUICK_ENV = "MANDATE_SMOKE_QUICK"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_CARGO = "cargo"
REPORT_NAME = "mandate-check.json"
# The log name of the ``rtp_mux`` producer. Kept as a module constant because
# it is also the run directory's ownership sentinel; every producer's own log
# name is its registry entry's.
LOG_NAME = "mandate-smoke.log"
PLOTS_DIRNAME = "plots"
REPORT_SCHEMA = "mandate-check/5"
ARMS_DECLARATION_NAME = "mandate-arms.json"
ARMS_DECLARATION_SCHEMA = "mandate-arms/1"
PRODUCERS_DECLARATION_NAME = "mandate-producers.json"
PRODUCERS_DECLARATION_SCHEMA = "mandate-producers/1"
PRODUCER_KEYS = (
    "id",
    "package",
    "target",
    "source",
    "default_path",
    "cargo_args",
    "test_args",
    "sections",
    "verdicts",
    "log",
)
REVISION_TIMEOUT_SECONDS = 30.0
LOG_TAIL_LINES = 20
# How long the line reader may take to drain after the child exits or is
# killed, before the run is reported with whatever the reader captured.
READER_JOIN_SECONDS = 10.0

EXIT_OK = 0
EXIT_EVIDENCE_FAILURE = 2
EXIT_MANDATE_FAILURE = 3

# ``MANDATE <ID> <PASS|FAIL> <key>=<value> ...``. The id is matched loosely
# (``[A-Za-z][A-Za-z0-9_]*``) and then checked against the verdict sections the
# producing crate declares, so an unknown mandate is a named failure instead of
# an ignored line.
MANDATE_LINE_RE = re.compile(
    r"^MANDATE (?P<mandate>[A-Za-z][A-Za-z0-9_]*) (?P<verdict>PASS|FAIL)"
    r"(?:[ \t]+(?P<values>.*?))?[ \t]*$"
)
VALUE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)$")
COMMIT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
CHANGE_ID_RE = re.compile(r"^[a-z]{10,}$")
# `jj debug object commit <id>` prints the commit's root tree as
# `root_tree: Resolved(\n    TreeId(\n        "<40-hex>",\n    ),\n)`; an
# unresolved tree is left null rather than guessed. The shape after the hash
# (a trailing comma inside the debug formatter) is deliberately not matched,
# so only `Resolved(TreeId("<hash>"))` counts.
JJ_ROOT_TREE_RE = re.compile(
    r"root_tree:\s*Resolved\(\s*TreeId\(\s*\"(?P<tree>[0-9a-f]{40})\"",
    re.S,
)
GIT_TREE_RE = re.compile(r"^(?P<tree>[0-9a-f]{40})$")
# A libtest result line: `test <name> ... ok` / `... FAILED` / `... ignored,
# <reason>`. The marker and the result are flushed together, so the result's
# arrival is the test's end. A libtest progress note
# (`test <name> has been running for over 60 seconds`) and the smoke set's own
# lines do not match a state and are not completions.
TEST_RESULT_RE = re.compile(r"^test (?P<name>\S+) \.\.\. ?(?P<tail>.*)$")
TEST_STATES = ("ok", "FAILED", "ignored")
# A libtest progress note (`test <name> has been running for over 60 seconds`):
# the test is still running, so the note is not a completion and not a marker
# whose result is still to come.
TEST_PROGRESS_RE = re.compile(r"^has been running for over ")
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


def parse_mandate_lines(text, ids=MANDATE_IDS):
    """Parse the ``MANDATE`` lines into ``({id: record}, [problems])``.

    Every line whose first token is ``MANDATE`` must match the contract
    grammar exactly; anything else that starts with ``MANDATE`` is reported
    as a problem rather than skipped. ``ids`` are the verdict sections the
    producing crate declares, so a line naming a section the producer does not
    declare is a named failure rather than an accepted verdict.
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
                "'MANDATE <ID> <PASS|FAIL> <key>=<value> ...'"
            )
            continue
        mandate = match.group("mandate")
        if mandate not in ids:
            because = (
                f"is not one of {', '.join(ids)}"
                if ids
                else "names a section, but the producer that printed it declares "
                "no verdict section"
            )
            problems.append(
                f"line {line_number}: mandate {mandate!r} {because}"
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
    """The run's arms, attributed to the section each one belongs to.

    Every ``[mandate-smoke ...]`` line is a candidate. An arm line whose body
    carries ``section=<id>`` is attributed by that token, and its id is
    ``<id>/<label>``; any other arm line belongs to the section named by the
    ``MANDATE`` line that next arrives, which is the order the smoke set emits
    (one section's arm lines, then its ``MANDATE`` line). The two paths exist
    because a producer that asserts no bound (a report-only perf probe) prints
    no ``MANDATE`` line for its arms to follow. Whether ``<id>`` is a section
    the producer declares is checked by the caller against the registry, and
    whether a section has arms at all by :func:`check_arm_coverage`. Returns
    ``(arms, notes)`` with ``arms`` sorted by id.

    The guards are vacuity guards: an arm line that can never be attributed, a
    section with no arm line, an arm with no declared coverage cell, and a run
    with no arm measurement at all are all problems, so an arm set that
    quietly empties is a failure rather than a report with an empty ``arms``.
    """
    arms = []
    notes = []
    pending = []

    def emit(entry, section):
        entry.pop("note", None)
        entry["mandate"] = section
        entry["id"] = f"{section}/{entry['label']}"
        arms.append(entry)

    def flush(section):
        for entry in pending:
            if entry.get("note"):
                notes.append(_public_arm_note(entry, section))
                continue
            emit(entry, section)
        pending.clear()

    for event in events:
        line = event["line"].rstrip()
        if line != "MANDATE" and line.startswith("MANDATE "):
            matched = MANDATE_LINE_RE.match(line)
            mandate = matched.group("mandate") if matched is not None else None
            if mandate is not None:
                flush(mandate)
            continue
        parsed = parse_arm_line(line)
        if parsed is not None:
            section = parsed.get("values", {}).get("section")
            if not parsed.get("note") and isinstance(section, str):
                emit(parsed, section)
            else:
                pending.append(parsed)
    for entry in pending:
        if entry.get("note"):
            notes.append(_public_arm_note(entry, None))
            continue
        problems.append(
            f"the arm line {entry['raw_line']!r} follows the last 'MANDATE' line "
            "and carries no 'section=' token, so it cannot be attributed to a "
            "mandate or a section; print the arm's line before its section's "
            "MANDATE line, or name the section in the line itself"
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


def check_arm_coverage(arms, notes, problems, sections=MANDATE_IDS):
    """Require every section's arms to have been measured, or say which not.

    A section whose arm lines are absent has had its coverage silently deleted,
    so it is a problem; a run with no arm measurement at all is the same
    failure stated once. ``sections`` is the union of the sections the selected
    producers declare, which is why it defaults to the ``rtp_mux`` producer's
    four mandate ids: the guard belongs to the producer's declaration, not to
    this function.
    """
    measured = {arm["mandate"] for arm in arms}
    for section in sections:
        if section not in measured:
            problems.append(
                f"{section}: no '[mandate-smoke <arm>]' measurement line was "
                "attributed to it, so its arms were never measured"
            )
    if not arms:
        problems.append(
            "the run printed no '[mandate-smoke <arm>] ...' arm measurement at "
            "all, so no arm's coverage was recorded; an instrument that "
            "returns nothing has deleted the coverage it exists to provide"
        )


def check_arm_sections(arms, producer, problems):
    """Every arm's section must be one the producing crate declares.

    An arm attributed to a section outside its producer's declaration is an arm
    whose id no declaration can vouch for, so it is a problem and names both.
    """
    declared = set(producer["sections"])
    for arm in arms:
        if arm["mandate"] not in declared:
            problems.append(
                f"the arm {arm['id']!r} is attributed to the section "
                f"{arm['mandate']!r}, which the {producer['id']} producer does "
                f"not declare (its sections are "
                f"{', '.join(producer['sections']) or 'none'}); declare the "
                f"section in {PRODUCERS_DECLARATION_NAME} or attribute the arm "
                "to one of the declared ones"
            )


def _arm_summary(arms, notes, report):
    """One line per producer and section: its arms and their sample count."""
    lines = []
    produced = sorted({arm.get("producer") for arm in arms if arm.get("producer")})
    for producer in produced:
        record = report["producers"].get(producer) or {}
        prefix = f"{producer} " if len(produced) > 1 else ""
        for section in record.get("sections") or []:
            of_section = [
                arm
                for arm in arms
                if arm.get("producer") == producer and arm["mandate"] == section
            ]
            if not of_section:
                lines.append(f"  {prefix}{section} arms: none measured")
                continue
            counts = [arm["sample_count"] for arm in of_section]
            known = [count for count in counts if count is not None]
            samples = f"{sum(known)} sample(s)" if known else "no per-sample count"
            labels = ", ".join(arm["label"] for arm in of_section)
            lines.append(
                f"  {prefix}{section} arms: {len(of_section)} ({labels}), {samples}"
            )
    if notes:
        lines.append(f"  arm notes (prose, not compared): {len(notes)}")
    return lines


def producer_checkout(producer):
    """A producer's declared default checkout, resolved against the workspace."""
    declared = Path(producer["default_path"]).expanduser()
    if declared.is_absolute():
        return declared.resolve()
    return (WORKSPACE_ROOT / declared).resolve()


def default_crate_path():
    """The primary producer's default checkout: the sibling ``../rtp_mux``.

    The registry's ``rtp_mux`` entry declares the same ``../rtp_mux``
    ``default_path``; ``test_mandate_check`` pins the two to each other so this
    helper cannot drift into a second authority.
    """
    return (WORKSPACE_ROOT.parent / PRIMARY_PRODUCER).resolve()


def load_producer_declaration(path, problems):
    """The producer registry, or a named failure.

    It is this command's own file — it declares which test targets are producers
    and what each owes the runner — so a missing or malformed one is a failure:
    without it there is no producer to run, and a run that silently recorded
    nothing would report success on absent evidence.
    """
    if not path.is_file():
        problems.append(
            f"the producer declaration {path} does not exist, so no producer's "
            "arms can be recorded; it travels with this command"
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        problems.append(f"the producer declaration {path} cannot be read: {error}")
        return None
    if not isinstance(payload, dict):
        problems.append(f"the producer declaration {path} is not a JSON object")
        return None
    if payload.get("schema") != PRODUCERS_DECLARATION_SCHEMA:
        problems.append(
            f"the producer declaration {path} declares schema "
            f"{payload.get('schema')!r}, not {PRODUCERS_DECLARATION_SCHEMA!r}"
        )
        return None
    entries = payload.get("producers")
    if not isinstance(entries, list) or not entries:
        problems.append(
            f"the producer declaration {path} declares no producers, so there is "
            "nothing to run"
        )
        return None
    seen = set()
    for entry in entries:
        problem = _producer_problem(entry, seen)
        if problem is not None:
            problems.append(f"the producer declaration {path}: {problem}")
            return None
    # A section is an arm-id namespace, so two producers may not share one:
    # the same id from two producers would record two different measurements
    # under one name and no comparison could tell them apart.
    owners = {}
    for entry in entries:
        for section in entry["sections"]:
            if section in owners:
                problems.append(
                    f"the producer declaration {path}: the section {section!r} is "
                    f"declared by both {owners[section]!r} and {entry['id']!r}; a "
                    "section is an arm-id namespace, so it must have one owner"
                )
                return None
            owners[section] = entry["id"]
    if PRIMARY_PRODUCER not in seen:
        problems.append(
            f"the producer declaration {path} does not declare the "
            f"{PRIMARY_PRODUCER!r} producer, whose record the report keeps under "
            "the keys a mandate-check/4 reader reads"
        )
        return None
    return payload


def _producer_problem(entry, seen):
    """One registry entry's first problem, or ``None`` when it is well formed."""
    if not isinstance(entry, dict):
        return "an entry is not a JSON object"
    missing = [key for key in PRODUCER_KEYS if key not in entry]
    if missing:
        return f"the entry {entry.get('id')!r} is missing {', '.join(missing)}"
    unknown = [key for key in entry if key not in PRODUCER_KEYS]
    if unknown:
        return f"the entry {entry['id']!r} carries unknown key(s) {', '.join(unknown)}"
    if not isinstance(entry["id"], str) or not entry["id"]:
        return "an entry's id is not a non-empty string"
    if entry["id"] in seen:
        return f"the producer {entry['id']!r} is declared twice"
    for key in ("package", "target", "source", "default_path", "log"):
        if not isinstance(entry[key], str) or not entry[key]:
            return f"the producer {entry['id']!r} gives {key!r} no string"
    for key in ("cargo_args", "test_args"):
        if not isinstance(entry[key], list) or not all(
            isinstance(token, str) and token for token in entry[key]
        ):
            return f"the producer {entry['id']!r} gives {key!r} no list of tokens"
    for key in ("sections", "verdicts"):
        if not isinstance(entry[key], list) or not all(
            isinstance(token, str) and token for token in entry[key]
        ):
            return f"the producer {entry['id']!r} gives {key!r} no list of sections"
    if not entry["sections"]:
        return f"the producer {entry['id']!r} declares no section, so no arm it prints can be attributed"
    if len(set(entry["sections"])) != len(entry["sections"]):
        return f"the producer {entry['id']!r} declares a section twice"
    outside = [section for section in entry["verdicts"] if section not in entry["sections"]]
    if outside:
        return (
            f"the producer {entry['id']!r} declares the verdict section(s) "
            f"{', '.join(outside)} it does not list among its sections"
        )
    seen.add(entry["id"])
    return None


def resolve_producer(producer, override):
    """``(checkout, None)``, or ``(None, problem)`` naming what is missing.

    ``override`` is the CLI path for this producer (resolved against the
    current directory, as a CLI path is), otherwise its declared
    ``default_path`` (resolved against this workspace).
    """
    if override is None:
        crate = producer_checkout(producer)
    else:
        crate = Path(override).expanduser().resolve()
    if not (crate / "Cargo.toml").is_file():
        return None, (
            f"the {producer['id']} checkout {crate} has no Cargo.toml, so its "
            f"{producer['target']!r} target cannot be built from it"
        )
    source = crate / producer["source"]
    if not source.is_file():
        return None, (
            f"the {producer['id']} producer's source {source} does not exist: the "
            f"{producer['target']!r} target must live in "
            f"{source.parent}, or --producer-path {producer['id']}=<path> names "
            "the wrong checkout"
        )
    return crate, None


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


def resolve_tree_id(crate, revision, revision_source):
    """``(tree_id, source)`` for a resolved commit, or ``(None, None)``.

    The commit id alone does not name the content a run built. ``jj`` rewrites
    ``@`` on every operation — an on-disk edit becomes a new commit id, and a
    fresh ``@`` on top is another one — so the working-copy commit a build
    reads may be an auto-snapshot whose commit id is throwaway while its tree
    is the content. The tree id is therefore recorded next to the commit and
    change ids, so a committed baseline names exactly what it measured. The
    tree is read from the commit with ``jj debug object commit`` (the jj-native
    route, no git checkout needed) when jj resolved the revision, and from
    git's ``<commit>^{tree}`` otherwise; a tree that cannot be resolved is
    ``null`` and never fabricated.
    """
    if revision is None:
        return None, None
    jj = shutil.which("jj")
    if jj is not None and revision_source == "jj":
        result = _capture(
            [
                jj,
                "--no-pager",
                "debug",
                "object",
                "commit",
                # The commit is addressed by id; do not snapshot the working
                # copy on the way to reading it.
                "--ignore-working-copy",
                revision,
            ],
            cwd=crate,
        )
        if result["exit_code"] == 0:
            match = JJ_ROOT_TREE_RE.search(result["stdout"])
            if match is not None:
                return match.group("tree"), "jj"
    git = shutil.which("git")
    if git is not None:
        result = _capture(
            [git, "-C", str(crate), "rev-parse", f"{revision}^{{tree}}"], cwd=crate
        )
        if result["exit_code"] == 0:
            match = GIT_TREE_RE.match(result["stdout"].strip())
            if match is not None:
                return match.group("tree"), "git"
    return None, None


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


def prepare_output_dir(out_dir, log_names=(LOG_NAME,)):
    """Create the run directory and clear what an earlier run left in it.

    Every file this command's run writes is removed: the eight evidence files
    the producers produce, the ``plots`` directory, this command's report and
    every producer's log. The report is removed for the same reason the
    evidence is — a reader (``tools/mandate-compare``) reads
    ``<dir>/mandate-check.json``, so one surviving a run that wrote none would
    be read as that run's measurement. Removal happens before a producer is
    built and before its checkout is validated, so no exit path can leave a
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
    for name in (REPORT_NAME, LOG_NAME, *log_names):
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


def producer_command(cargo, producer):
    """One producer's contract invocation: its declared argv, no filter added."""
    return [cargo, *producer["cargo_args"], "--", *producer["test_args"]]


def run_producer(command, *, crate, out_dir, quick, timeout):
    """Run one producer, returning its output, how it ended and its timeline.

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


def _result_state(tail):
    """The libtest state a result line's tail names, or ``None``."""
    return next(
        (
            candidate
            for candidate in TEST_STATES
            if tail == candidate
            or tail.startswith(candidate + " ")
            or tail.startswith(candidate + ",")
        ),
        None,
    )


def derive_timings(events, target):
    """The run's per-test and per-mandate wall-clock, from the line timeline.

    Every libtest result line and every ``MANDATE`` line is an observed
    instant. A test's duration is the bracket between its result's arrival and
    the previous result's, a mandate's the bracket between its ``MANDATE``
    line and the previous one; the first bracket runs from the child's start.
    A ``FAILED`` or ``ignored`` result is recorded with its state and, for
    ``ignored``, a null duration (it never ran). Nothing here is inferred: an
    absent line stays absent.

    A test that prints while it runs splits its own completion in two: libtest
    writes ``test <name> ... `` and flushes, the test's output follows, and the
    state arrives on a line of its own when the test ends. The completion is
    then the arrival of that **state** line — which is when the test ended —
    and the marker line, whose tail is the test's first line of output rather
    than a state, is held until the state arrives. A producer whose arms are
    printed from inside its own test (the harness's perf probes) times exactly
    like one that buffers it (the smoke set).
    """
    tests = []
    mandates = []
    previous_completion = 0.0
    previous_mandate = 0.0
    awaiting = None

    def record(name, state, seconds):
        nonlocal previous_completion
        entry = {
            "target": target,
            "name": name,
            "state": state,
            "started_at_seconds": previous_completion,
            "finished_at_seconds": seconds,
            "duration_seconds": None,
        }
        if state != "ignored":
            entry["duration_seconds"] = round(max(seconds - previous_completion, 0.0), 3)
            previous_completion = seconds
        tests.append(entry)

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
        if result is not None:
            tail = result.group("tail").strip()
            state = _result_state(tail)
            if state is None:
                # Either the marker's result is still to come because the test
                # printed (tail is its own first line), or this is a progress
                # note and the test is still running.
                if TEST_PROGRESS_RE.match(tail):
                    continue
                awaiting = result.group("name")
                continue
            awaiting = None
            record(result.group("name"), state, seconds)
            continue
        if awaiting is not None:
            state = _result_state(line)
            if state is not None:
                record(awaiting, state, seconds)
                awaiting = None
    return {
        "method": TIMING_METHOD,
        "origin": "smoke-child-start",
        "tests": tests,
        "mandates": mandates,
    }


def apply_mandate_timings(report, timings, producer):
    """Attach one producer's measured wall-clock to its mandates and the report.

    ``timings`` is one producer's own timeline, measured from its own child's
    start, so its per-test and per-mandate entries are appended to the report's
    merged lists with their ``producer`` stamped. Each of the producer's
    verdict sections takes its duration from its own ``MANDATE`` lines and not
    from another producer's.
    """
    by_mandate = {entry["mandate"]: entry for entry in timings["mandates"]}
    merged = report["timings"]
    for entry in timings["tests"]:
        entry["producer"] = producer["id"]
        merged["tests"].append(entry)
    for entry in timings["mandates"]:
        entry["producer"] = producer["id"]
        merged["mandates"].append(entry)
    for mandate in producer["verdicts"]:
        record = report["mandates"].get(mandate)
        entry = by_mandate.get(mandate)
        if record is None or entry is None:
            continue
        record["finished_at_seconds"] = entry["finished_at_seconds"]
        record["duration_seconds"] = entry["duration_seconds"]


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


def producer_record(producer, out_dir):
    """One producer's record: what it is, where it lives, what it printed."""
    return {
        "id": producer["id"],
        "package": producer["package"],
        "target": producer["target"],
        "source": producer["source"],
        "default_path": producer["default_path"],
        "selected": False,
        "path": None,
        "sections": list(producer["sections"]),
        "verdicts": list(producer["verdicts"]),
        # Derived, not declared: a producer that prints a verdict line owes its
        # evidence, so a registry entry cannot declare the guard away.
        "evidence": bool(producer["verdicts"]),
        "log": str(out_dir / producer["log"]),
        "command": None,
        "revision": None,
        "change_id": None,
        "revision_source": None,
        "tree_id": None,
        "tree_id_source": None,
        "run": {
            "exit_code": None,
            "timed_out": False,
            "log": str(out_dir / producer["log"]),
        },
        "arms": 0,
    }


def build_report(args, out_dir, declared, selected, quick, timeout):
    """The empty report: every declared producer, and every selected one's grid.

    Every declared producer gets a record so that a reader can see which
    producers exist and which this run selected, rather than reading a
    one-producer report as the whole inventory. A verdict section declared by
    two producers is refused before this point, so the ``mandates`` map and
    ``mandate_order`` list have one owner per section.
    """
    report = {
        "schema": REPORT_SCHEMA,
        "ok": False,
        "exit_code": None,
        "verdict": None,
        "started_at": _now(),
        "duration_seconds": None,
        "timeout_seconds": timeout,
        "quick": quick,
        "producers_declared": [entry["id"] for entry in declared],
        "producers_selected": list(selected),
        "producers": {
            entry["id"]: producer_record(entry, out_dir) for entry in declared
        },
        "command": None,
        "cwd": None,
        "out_dir": str(out_dir),
        "report": str(out_dir / REPORT_NAME),
        # Kept for a `mandate-check/4` reader: the primary producer's identity
        # and run, or null when it was not selected this run.
        "rtp_mux": None,
        "smoke": {
            "exit_code": None,
            "timed_out": False,
            "log": str(out_dir / LOG_NAME),
            "producer": None,
        },
        "mandates": {},
        "mandate_order": [],
        "section_order": [],
        "timings": {
            "method": TIMING_METHOD,
            "origin": "smoke-child-start",
            "tests": [],
            "mandates": [],
        },
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
    for entry in declared:
        for section in entry["sections"]:
            report["section_order"].append(section)
        if entry["id"] not in selected:
            continue
        for mandate in entry["verdicts"]:
            report["mandate_order"].append(mandate)
            report["mandates"][mandate] = {
                "producer": entry["id"],
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
    return report


def write_report(out_dir, report):
    path = out_dir / REPORT_NAME
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def apply_arm_records(report, events, declaration, producer, problems):
    """Record one producer's per-arm measurements, or name why they are missing.

    Every guard here is a vacuity guard: the record must be non-empty, every
    arm must be attributable to a section its own producer declares, and every
    arm must claim a declared coverage cell. An empty or partial record is a
    failure, not an empty list. The problems are appended to the caller's list
    so that one producer's failure is decided on its own evidence.
    """
    if report["arm_declaration"]["source"] is None and declaration is not None:
        report["arm_declaration"]["source"] = declaration.get("source")
        report["arm_declaration"]["declared_cells"] = sum(
            1 for cells in declaration["cells"].values() for cell in cells
        )
    arms, notes = parse_arm_lines(events, problems)
    for arm in arms:
        arm["producer"] = producer["id"]
    stamp_arm_coverage(arms, declaration, problems)
    check_arm_sections(arms, producer, problems)
    check_arm_coverage(arms, notes, problems, tuple(producer["sections"]))
    report["arms"].extend(arms)
    report["arm_notes"].extend(notes)
    return arms


def verdict_block(report):
    """The human- and machine-readable block printed on every exit path."""
    lines = [
        f"mandate-check: {len(report['producers_selected'])} producer(s) run"
        f" of {len(report['producers_declared'])} declared",
    ]
    for producer in report["producers_selected"]:
        record = report["producers"][producer]
        lines.append(f"producer: {producer}  {record['package']}:{record['target']}")
        lines.append(f"  checkout: {record['path'] or 'unresolved'}")
        lines.append(
            f"  revision: {record['revision'] or 'unresolved'}"
            f" ({record['revision_source'] or 'no jj or git'})"
        )
        lines.append(
            f"  tree:     {record['tree_id'] or 'unresolved'}"
            f" ({record['tree_id_source'] or 'no jj or git'})"
        )
        lines.append(f"  command:  {' '.join(record['command'] or [])}")
        lines.append(f"  log:      {record['log']}")
        lines.append(
            f"  exit:     {record['run']['exit_code']}"
            f"{' (timed out)' if record['run']['timed_out'] else ''}"
            f"   arms: {record['arms']}"
        )
    unselected = [
        entry for entry in report["producers_declared"]
        if entry not in report["producers_selected"]
    ]
    if unselected:
        lines.append(f"  not selected: {', '.join(unselected)}")
    lines.append(f"  output:   {report['out_dir']}")
    lines.append(
        f"  quick:    {'yes' if report['quick'] else 'no'}"
        f"   timeout: {report['timeout_seconds']:.0f}s"
    )
    for mandate in report["mandate_order"]:
        record = report["mandates"][mandate]
        verdict = record["verdict"] or "MISSING"
        measured = " ".join(f"{key}={value}" for key, value in record["values"].items())
        lines.append(f"{mandate} {verdict}  {measured}".rstrip())
        duration = record.get("duration_seconds")
        if duration is not None:
            lines.append(f"  duration: {duration:.2f}s (bracketed wall-clock)")
        for path in record["plots"]:
            lines.append(f"  plot: {path}")
    arms = report.get("arms") or []
    produced = sorted({arm.get("producer") for arm in arms if arm.get("producer")})
    lines.append(
        f"arms: {len(arms)} measured"
        + (f" ({', '.join(produced)})" if len(produced) > 1 else "")
    )
    lines.extend(
        _arm_summary(arms, report.get("arm_notes") or [], report)
    )
    declaration = report.get("arm_declaration") or {}
    if declaration.get("source") is not None:
        lines.append(
            f"  cells: {declaration.get('declared_cells', 0)} declared in "
            f"{Path(declaration.get('path') or '').name}"
        )
    duration = report["duration_seconds"]
    order = report["mandate_order"]
    passed = sum(
        1 for mandate in order if report["mandates"][mandate]["verdict"] == "PASS"
    )
    panels = sum(len(report["mandates"][mandate]["plots"]) for mandate in order)
    if report["exit_code"] == EXIT_EVIDENCE_FAILURE:
        summary = (
            f"evidence incomplete ({passed}/{len(order)} mandate line(s) said "
            f"PASS, {panels} plot(s))"
        )
    else:
        summary = f"{passed}/{len(order)} mandate(s) passed, {panels} plot(s)"
    lines.append(
        f"verdict: {'PASS' if report['ok'] else 'FAIL'}  exit={report['exit_code']}  "
        + summary
        + (f", {duration:.1f}s" if duration is not None else "")
    )
    for problem in report["problems"]:
        lines.append(f"problem: {problem}")
    lines.append(f"report:  {report['report']}")
    return lines


def evaluate_producer(args, producer, out_dir, report, run, declaration):
    """Parse and verify one producer's run, filling the report. Exit code back."""
    record = report["producers"][producer["id"]]
    log_path = out_dir / producer["log"]
    log_path.write_text(run["output"], encoding="utf-8")
    record["run"] = {
        "exit_code": run["exit_code"],
        "timed_out": run["timed_out"],
        "log": str(log_path),
    }
    # The per-test and per-section wall-clock observed on this child's output
    # stream, so a cost the declaration claims can be compared with what the
    # run actually took, per test, rather than only in total.
    apply_mandate_timings(
        report,
        derive_timings(run.get("events") or [], producer["target"]),
        producer,
    )
    # This producer's own problems, so a report of several producers says which
    # one broke and the exit code is decided per producer rather than by the
    # union of everyone's evidence.
    problems = []
    # What each arm measured, so a later run's coverage can be diffed against a
    # committed baseline instead of argued about.
    arms = apply_arm_records(
        report, run.get("events") or [], declaration, producer, problems
    )
    record["arms"] = len(arms)
    if run["timed_out"]:
        problems.append(
            f"the test target did not finish within {args.timeout:.0f}s and was "
            f"killed; its partial output is {log_path}"
        )
    elif run["exit_code"] != 0:
        problems.append(
            f"the test target exited {run['exit_code']} (compile or test "
            f"failure); its output is {log_path}"
        )

    records, parse_problems = parse_mandate_lines(
        run["output"], tuple(producer["verdicts"])
    )
    problems.extend(parse_problems)
    for mandate in producer["verdicts"]:
        if mandate not in records:
            problems.append(
                f"{mandate}: the smoke set printed no 'MANDATE {mandate} "
                "<PASS|FAIL> ...' line, so this mandate was never measured"
            )

    for mandate in producer["verdicts"]:
        section = report["mandates"][mandate]
        parsed = records.get(mandate)
        if parsed is not None:
            section["declared"] = True
            section["verdict"] = parsed["verdict"]
            section["values"] = parsed["values"]
            section["raw_line"] = parsed["raw_line"]
        summary, render_problems = render_mandate(
            mandate,
            out_dir,
            rasterize=args.rasterize,
            browser=args.browser,
        )
        if summary is not None:
            section["plots"] = list(summary.get("svg") or []) + list(
                summary.get("png") or []
            )
            section["series_counts"] = list(summary.get("series_counts") or [])
            section["panels"] = summary.get("panels", 0)
            problems.extend(_verify_plots(mandate, summary))
        problems.extend(render_problems)

    report["problems"].extend(
        f"{producer['id']}: {problem}" for problem in problems
    )
    if problems:
        return EXIT_EVIDENCE_FAILURE
    if any(report["mandates"][mandate]["verdict"] == "FAIL" for mandate in producer["verdicts"]):
        return EXIT_MANDATE_FAILURE
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
            "Run the perf producers declared in tools/mandate-producers.json "
            "(rtp_mux's mandate_smoke smoke set and netem_test's perf-tier "
            "probes by default), render each mandate's panels, print a verdict "
            "block, and write mandate-check.json as machine-checkable evidence "
            "for every producer's arms."
        ),
    )
    parser.add_argument(
        "--producer",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "run only this producer, repeatably; with none named every "
            "declared producer runs, so one run records every producer's arms"
        ),
    )
    parser.add_argument(
        "--producer-path",
        action="append",
        default=None,
        metavar="ID=PATH",
        help=(
            "point one producer at another checkout (repeatable), resolved "
            "against the current directory; the default is the "
            "producer's declared default_path, resolved against this workspace"
        ),
    )
    parser.add_argument(
        "--rtp-mux",
        type=Path,
        default=None,
        help=(
            "the rtp_mux checkout holding the smoke set; the documented "
            "shorthand for --producer-path rtp_mux=<path> "
            f"(default: the sibling ../{PRIMARY_PRODUCER} of this workspace)"
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
            f"set {QUICK_ENV}=1 so a producer that honours it takes its "
            "shortest windows; the assertions and the evidence files must "
            "still be complete"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "seconds before a producer is killed (default: "
            f"{DEFAULT_TIMEOUT_SECONDS:.0f}, applied per producer)"
        ),
    )
    parser.add_argument(
        "--cargo",
        default=DEFAULT_CARGO,
        help=f"cargo executable to build and run the producers (default: {DEFAULT_CARGO})",
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


def producer_overrides(args):
    """``(overrides, problems)``: the per-producer checkout paths the CLI names.

    ``--producer-path <id>=<path>`` is the general form and ``--rtp-mux
    <path>`` the documented shorthand for the primary producer; an explicit
    ``--producer-path`` for that producer wins over the shorthand.
    """
    overrides = {}
    problems = []
    for token in args.producer_path or []:
        producer, separator, path = token.partition("=")
        if not separator or not producer or not path:
            problems.append(f"--producer-path {token!r} is not <id>=<path>")
            continue
        overrides[producer] = Path(path)
    if args.rtp_mux is not None:
        overrides.setdefault(PRIMARY_PRODUCER, args.rtp_mux)
    return overrides, problems


def select_producers(declared, requested, problems):
    """The producers to run: those named, or every declared producer.

    With none named the run records **every** declared producer, so a single
    invocation produces per-arm records for all of them rather than only the
    one this command was first built for.
    """
    by_id = {entry["id"]: entry for entry in declared}
    if not requested:
        return list(declared)
    selected = []
    for name in requested:
        if name not in by_id:
            problems.append(
                f"--producer {name!r} is not one of {', '.join(by_id)}"
            )
            continue
        if by_id[name] not in selected:
            selected.append(by_id[name])
    return selected


def main(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    declaration_problems = []
    try:
        if args.timeout <= 0:
            raise MandateCheckError("--timeout must be positive")
        if args.dir is not None:
            out_dir = args.dir.expanduser().resolve()
        else:
            out_dir = default_out_dir()
        # The producer registry is only read (never written) before the run
        # directory is cleared, so every producer's log is cleared with it; the
        # clearing itself happens before any producer is built or validated, so
        # no exit path can leave an earlier run's report standing where a
        # comparison would read it as this run's.
        producers_declaration = load_producer_declaration(
            MODULE_DIR / PRODUCERS_DECLARATION_NAME, declaration_problems
        )
        logs = [
            entry["log"]
            for entry in (producers_declaration or {}).get("producers") or []
        ] or [LOG_NAME]
        prepare_output_dir(out_dir, logs)
        if declaration_problems:
            raise MandateCheckError("; ".join(declaration_problems))
        overrides, override_problems = producer_overrides(args)
        if override_problems:
            raise MandateCheckError("; ".join(override_problems))
        selection_problems = []
        selected = select_producers(
            producers_declaration["producers"], args.producer, selection_problems
        )
        if selection_problems or not selected:
            raise MandateCheckError("; ".join(selection_problems) or "no producer selected")
        resolved = {}
        for entry in selected:
            crate, problem = resolve_producer(entry, overrides.get(entry["id"]))
            if problem is not None:
                raise MandateCheckError(problem)
            resolved[entry["id"]] = crate
        cargo = shutil.which(args.cargo)
        if cargo is None:
            raise MandateCheckError(
                f"the cargo executable {args.cargo!r} was not found on PATH, so no "
                "producer can be built"
            )
    except MandateCheckError as error:
        print(f"mandate-check: error: {error}", file=sys.stderr)
        return EXIT_EVIDENCE_FAILURE

    arm_problems = []
    declaration = load_arm_declaration(
        MODULE_DIR / ARMS_DECLARATION_NAME, arm_problems
    )
    if arm_problems:
        for problem in arm_problems:
            print(f"mandate-check: error: {problem}", file=sys.stderr)
        return EXIT_EVIDENCE_FAILURE

    report = build_report(
        args,
        out_dir,
        producers_declaration["producers"],
        [entry["id"] for entry in selected],
        args.quick,
        args.timeout,
    )
    codes = []
    runs = {}
    for entry in selected:
        crate = resolved[entry["id"]]
        revision = resolve_revision(crate)
        tree = resolve_tree_id(crate, revision[0], revision[2])
        command = producer_command(cargo, entry)
        record = report["producers"][entry["id"]]
        record["selected"] = True
        record["path"] = str(crate)
        record["command"] = command
        record["revision"], record["change_id"], record["revision_source"] = revision
        record["tree_id"], record["tree_id_source"] = tree
        try:
            run = run_producer(
                command,
                crate=crate,
                out_dir=out_dir,
                quick=args.quick,
                timeout=args.timeout,
            )
        except OSError as error:
            print(f"mandate-check: error: {entry['id']}: {error}", file=sys.stderr)
            return EXIT_EVIDENCE_FAILURE
        runs[entry["id"]] = run
        codes.append(evaluate_producer(args, entry, out_dir, report, run, declaration))

    # The records a `mandate-check/4` reader reads: the primary producer's
    # identity, command and run, or null when it was not selected this run.
    primary = report["producers"].get(PRIMARY_PRODUCER) or {}
    if primary.get("selected"):
        report["rtp_mux"] = {
            key: primary[key]
            for key in (
                "path",
                "revision",
                "change_id",
                "revision_source",
                "tree_id",
                "tree_id_source",
            )
        }
        report["smoke"] = {
            "producer": PRIMARY_PRODUCER,
            "exit_code": primary["run"]["exit_code"],
            "timed_out": primary["run"]["timed_out"],
            "log": primary["run"]["log"],
        }
    first = report["producers"].get(report["producers_selected"][0]) or {}
    report["command"] = first.get("command")
    report["cwd"] = first.get("path")
    if EXIT_EVIDENCE_FAILURE in codes:
        exit_code = EXIT_EVIDENCE_FAILURE
    elif EXIT_MANDATE_FAILURE in codes:
        exit_code = EXIT_MANDATE_FAILURE
    else:
        exit_code = EXIT_OK
    report["exit_code"] = exit_code
    report["ok"] = exit_code == EXIT_OK
    report["verdict"] = "PASS" if report["ok"] else "FAIL"
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    write_report(out_dir, report)
    for line in verdict_block(report):
        print(line)
    for entry in selected:
        run = runs[entry["id"]]
        if not (run["timed_out"] or run["exit_code"] != 0):
            continue
        tail = _log_tail(run["output"])
        if not tail:
            continue
        log = report["producers"][entry["id"]]["log"]
        print(f"--- last {len(tail)} line(s) of {log} ---", file=sys.stderr)
        for line in tail:
            print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
