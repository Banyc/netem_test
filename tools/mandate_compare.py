#!/usr/bin/env python3
"""Diff a mandate-check run's per-arm measurements against a committed baseline.

`tools/mandate-check` records what each perf *arm* measured — its sample count,
its distribution statistics, its delivery and wire counters, its measured
windows and the coverage cells it is declared to exercise — so that a change
which shortens a perf test can be *shown* coverage-neutral instead of argued
so. This command is the other half of that instrument: it compares a fresh
run's report with the committed baseline (`tools/mandate-baseline.json`) and
reports, per arm, exactly which quantities moved.

    ./tools/mandate-compare <run>/mandate-check.json

## The two kinds of movement

They are not the same kind of claim and must not be read as one.

- **A coverage regression** is a quantity whose movement means the arm no longer
  covers what the baseline covered: the arm is gone, its sample count fell, a
  delivery or wire counter fell, a measurement window shrank, a statistic the
  assertions read stopped being measured, its delivery ratio fell past its
  tolerance, or a coverage cell the baseline exercised is exercised by no arm
  any more. These are failures (exit ``4``): a shortened test that halves an
  arm's samples has moved its p99's statistical power, and a p99 that is still
  inside its bound does not say otherwise.
- **A value change** is a statistics move — a latency percentile, a goodput
  rate, a share. On a shared host these move run to run, so by default they are
  *reported* and the comparison stays green; with ``--fail-on-value-drift`` a
  move past ``--value-tolerance`` is a failure (exit ``5``). The tolerance is
  deliberately loose (50 %, the same relative tolerance ``tools/check-gate.py``
  applies to a declared cost) because the point of the default is to be usable
  on a loaded host.

Counts are compared with a **50 % relative tolerance** rather than exactly,
because a window-driven arm's counts vary between runs of the same binary on a
contended host: across two recorded full runs of the unchanged tree the
request/response arm's sample count moved 819 -> 421 (-48.6 %) and 514 -> 882
(+71.6 %), and a hostile arm's wire bytes moved -10.7 %, while the cadence and
per-flow arms moved under 4 %. A fall of half or more is a regression; a
smaller fall is reported as a move. **The measured window is what carries the
sharp shortening signal** and is compared at 1 %, so a window reduced from 12 s
to 4 s is a regression whoever reports the sample counts. Everything the
comparison reads comes from the two reports; nothing is re-measured here.

**The detection limit**, stated so a green diff is not read as more than it is:
this comparison sees an arm that disappeared, a sample count or delivery/wire
counter that fell by half or more, a counter that stopped being measured, a
measured window that shrank by more than 1 %, a statistic the assertions read
that stopped being measured, the delivery ratio falling past its tolerance, a
coverage cell no arm covers any more, and a mandate that vanished. It does
**not** see an arm that keeps its sample count and its counters while its
impairment was quietly weakened — a 2 % loss arm retuned to 1 % measures the
same shape under a milder regime. That is a change to a frozen perf-test
setting, and the guard against it is the setting's immutability and reading the
arm against its declared coverage cell in `tools/mandate-arms.json`, not this
comparison. Nor does it see a shortening that leaves the window in place, keeps
at least half the samples and drops no cell.

## What it refuses

A comparison between runs that are not comparable is refused (exit ``2``)
rather than reported as agreement: a candidate whose schema predates the
per-arm record, a baseline or candidate with no arms at all, a candidate whose
``--quick`` flag differs from the baseline's, or a declared coverage cell whose
syntax the gate checker rejects. An empty comparison is never a pass.

## What it writes

The verdict block on stdout, and with ``--json-out`` the same diff as JSON:
the two reports, the tolerances, every arm compared with its changes, the
coverage regressions, the value moves and the exit code.

## Exit codes

- ``0`` — every arm the baseline covered is still covered, with no coverage
  regression and no value move past the tolerance.
- ``2`` — the comparison could not be made.
- ``4`` — at least one coverage regression.
- ``5`` — no coverage regression, and (only with ``--fail-on-value-drift``) a
  value move past the tolerance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE_NAME = "mandate-baseline.json"
DEFAULT_REPORT_NAME = "mandate-check.json"
DEFAULT_COUNT_TOLERANCE = 0.50
DEFAULT_WINDOW_TOLERANCE = 0.01
DEFAULT_DELIVERY_TOLERANCE = 0.005
DEFAULT_VALUE_TOLERANCE = 0.5
MINIMUM_SCHEMA = 3
# How many arms and moves the printed block lists before it summarises the
# rest, so a large run cannot bury its verdict in a wall of lines.
MAX_ARM_LINES = 200

EXIT_OK = 0
EXIT_UNCOMPARABLE = 2
EXIT_COVERAGE_REGRESSION = 4
EXIT_VALUE_DRIFT = 5

# The quantities a coverage regression is decided on, per arm. A key present in
# the baseline and absent in the candidate is a regression for the same reason
# a fall is: the arm stopped measuring it.
COVERAGE_COUNTER_KEYS = (
    "sent",
    "received",
    "wire_bytes",
    "bulk_wire_bytes",
    "bulk_sink_bytes",
    "offered_bytes",
    "delivered_bytes",
    "forwarded_bytes",
)
COVERAGE_WINDOW_KEYS = ("window_seconds", "elapsed_seconds")
COVERAGE_WALL_KEYS = ("wall_seconds",)
VALUE_STAT_KEYS = (
    "p50",
    "p90",
    "p99",
    "p999",
    "max",
    "min",
    "mean",
    "std",
    "over250",
    "fraction",
    "share",
    "imbalance",
    "min_share",
    "max_share",
    "ideal_share",
    "delivered_mib_s",
    "shaper_mib_s",
    "capacity_mib_s",
)
# The delivery ratio is a coverage quantity, not a statistic: a lane that
# delivered 1.000 and now delivers 0.980 has lost delivery, however static its
# p99 is. It is compared absolutely, with the slope the M2/M4 floors allow.
DELIVERY_KEY = "delivery"

SCHEMA_RE = re.compile(r"^mandate-check/(?P<version>[0-9]+)$")


class MandateCompareError(Exception):
    """A failure that must surface as a non-zero exit, naming the problem."""


def _load_gate_checker():
    """`tools/check-gate.py`, the authority for the coverage-cell grammar."""
    path = MODULE_DIR / "check-gate.py"
    spec = importlib.util.spec_from_file_location("mandate_gate_checker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_report(path, role):
    """One ``mandate-check.json`` as a dict, or a named failure."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise MandateCompareError(f"the {role} report {resolved} does not exist")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MandateCompareError(f"the {role} report {resolved} cannot be read: {error}")
    if not isinstance(payload, dict):
        raise MandateCompareError(f"the {role} report {resolved} is not a JSON object")
    schema = payload.get("schema")
    match = SCHEMA_RE.match(schema) if isinstance(schema, str) else None
    if match is None:
        raise MandateCompareError(
            f"the {role} report {resolved} declares schema {schema!r}, not a "
            "'mandate-check/<version>' this command can read"
        )
    if int(match.group("version")) < MINIMUM_SCHEMA:
        raise MandateCompareError(
            f"the {role} report {resolved} is schema {schema}, which predates the "
            f"per-arm record (mandate-check/{MINIMUM_SCHEMA}); re-record it with "
            "tools/mandate-check before comparing"
        )
    arms = payload.get("arms")
    if not isinstance(arms, list) or not arms:
        raise MandateCompareError(
            f"the {role} report {resolved} carries no arm records, so there is "
            "nothing to compare; a report with no arms cannot certify coverage"
        )
    return {"path": resolved, "payload": payload, "arms": arm_index(arms)}


def arm_index(arms):
    """``id -> arm record``, refusing a repeated id (which cannot be diffed)."""
    index = {}
    for arm in arms:
        if not isinstance(arm, dict) or not isinstance(arm.get("id"), str):
            raise MandateCompareError("an arm record has no id, so it cannot be compared")
        if arm["id"] in index:
            raise MandateCompareError(
                f"the arm {arm['id']!r} is recorded twice; a reader cannot tell "
                "which record the comparison should read"
            )
        index[arm["id"]] = arm
    return index


def _numbers(mapping):
    return {
        key: value
        for key, value in (mapping or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def relative_change(baseline, candidate):
    """The candidate's relative change from the baseline, or ``None``."""
    if not isinstance(baseline, (int, float)) or isinstance(baseline, bool):
        return None
    if baseline == 0:
        return None
    return (candidate - baseline) / baseline


def compare_counter(key, baseline, candidate, tolerance):
    """One counted quantity as ``("regression"|"move"|None, text)``.

    The fall is compared with ``<=`` so that a shortening which takes exactly
    half an arm's samples — the case the instrument exists for — is a
    regression, and the tolerance is the run-to-run band the comparison is not
    allowed to read as coverage loss.
    """
    if candidate is None:
        return "regression", f"{key} {baseline} -> not measured"
    change = relative_change(baseline, candidate)
    if change is None:
        return (None, None) if candidate == baseline else ("move", f"{key} {baseline} -> {candidate}")
    if change <= -tolerance:
        return (
            "regression",
            f"{key} {baseline} -> {candidate} ({change:+.1%}), past the {tolerance:.0%} tolerance",
        )
    if candidate != baseline:
        return "move", f"{key} {baseline} -> {candidate} ({change:+.1%})"
    return None, None


def compare_stat(key, baseline, candidate, tolerance):
    """One statistic's movement as ``("regression"|"drift"|"move"|None, text)``.

    A statistic the baseline measured and the candidate did not is a coverage
    regression: the assertion that reads it has nothing to read. A statistic
    that moved is a *value* change — reported always, and a failure only when
    the caller asks for strictness.
    """
    if candidate is None:
        return "regression", f"{key} {baseline} -> not measured"
    if candidate == baseline:
        return None, None
    change = relative_change(baseline, candidate)
    text = f"{key} {baseline} -> {candidate}" + (f" ({change:+.1%})" if change is not None else "")
    if change is not None and abs(change) > tolerance:
        return "drift", f"{text} [past the {tolerance:.0%} value tolerance]"
    return "move", text


def compare_delivery(baseline, candidate, tolerance):
    """The delivery ratio: a fall is coverage loss, a rise is a value move."""
    if candidate is None:
        return "regression", f"delivery {baseline} -> not measured"
    if candidate == baseline:
        return None, None
    change = candidate - baseline
    text = f"delivery {baseline} -> {candidate} ({change:+.3f})"
    if change < -tolerance:
        return "regression", f"{text}, past the {tolerance:.3f} delivery tolerance"
    return "move", text


def compare_arm(baseline_arm, candidate_arm, tolerances):
    """One arm's changes, split into coverage regressions and value moves."""
    regressions = []
    moves = []
    drifts = []
    baseline_samples = baseline_arm.get("sample_count")
    candidate_samples = candidate_arm.get("sample_count") if candidate_arm else None
    if baseline_samples is not None:
        outcome, text = compare_counter(
            "sample_count", baseline_samples, candidate_samples, tolerances["count"]
        )
        if outcome is not None:
            (regressions if outcome == "regression" else moves).append(text)
    baseline_counters = _numbers(baseline_arm.get("counters"))
    candidate_counters = _numbers(candidate_arm.get("counters")) if candidate_arm else {}
    for key in COVERAGE_COUNTER_KEYS:
        if key not in baseline_counters:
            continue
        outcome, text = compare_counter(
            key, baseline_counters[key], candidate_counters.get(key), tolerances["count"]
        )
        if outcome is not None:
            (regressions if outcome == "regression" else moves).append(text)
    baseline_windows = _numbers(baseline_arm.get("windows"))
    candidate_windows = _numbers(candidate_arm.get("windows")) if candidate_arm else {}
    # The measured geometry (`window_seconds`, `elapsed_seconds`) is the one
    # deterministic shortening signal, so it is compared tightly; the observed
    # wall-clock includes fixture setup and teardown and varies like the other
    # counts.
    for key in COVERAGE_WINDOW_KEYS:
        if key not in baseline_windows:
            continue
        outcome, text = compare_counter(
            key, baseline_windows[key], candidate_windows.get(key), tolerances["window"]
        )
        if outcome is not None:
            (regressions if outcome == "regression" else moves).append(text)
    for key in COVERAGE_WALL_KEYS:
        if key not in baseline_windows:
            continue
        outcome, text = compare_counter(
            key, baseline_windows[key], candidate_windows.get(key), tolerances["count"]
        )
        if outcome is not None:
            (regressions if outcome == "regression" else moves).append(text)
    baseline_stats = _numbers(baseline_arm.get("stats"))
    candidate_stats = _numbers(candidate_arm.get("stats")) if candidate_arm else {}
    for key in VALUE_STAT_KEYS:
        if key not in baseline_stats:
            continue
        outcome, text = compare_stat(
            key,
            baseline_stats[key],
            candidate_stats.get(key),
            tolerances["value"],
        )
        if outcome == "regression":
            regressions.append(text)
        elif outcome == "drift":
            drifts.append(text)
        elif outcome == "move":
            moves.append(text)
    if DELIVERY_KEY in baseline_stats:
        outcome, text = compare_delivery(
            baseline_stats[DELIVERY_KEY],
            candidate_stats.get(DELIVERY_KEY),
            tolerances["delivery"],
        )
        if outcome == "regression":
            regressions.append(text)
        elif text is not None:
            moves.append(text)
    return {"regressions": regressions, "drifts": drifts, "moves": moves}


def compare_cells(baseline, candidate, gate_checker, problems):
    """Every cell the baseline exercised must still be exercised by an arm.

    The cells are the declared coverage the arms carry; a cell whose every arm
    has been removed is the coverage the shortening dropped, however green the
    remaining arms are.
    """
    baseline_cells = cells_for(baseline)
    candidate_cells = cells_for(candidate)
    for role, report in (("baseline", baseline), ("candidate", candidate)):
        for cell, covered_by in sorted(cells_for(report).items()):
            problem = gate_checker.cell_problem(cell)
            if problem is not None:
                problems.append(
                    f"the {role} declares the coverage cell {cell!r} (covered by "
                    f"{', '.join(covered_by)}), which is not "
                    f"<property>@<dimension>=<value>: {problem}"
                )
    return [
        {
            "cell": cell,
            "covered_by": covered_by,
            "regression": cell not in candidate_cells,
        }
        for cell, covered_by in sorted(baseline_cells.items())
    ]


def cells_for(report):
    """``cell -> sorted arm ids`` over a report's arms."""
    covered = {}
    for arm in report["arms"].values():
        for cell in arm.get("cells") or []:
            covered.setdefault(cell, []).append(arm["id"])
    return {cell: sorted(ids) for cell, ids in covered.items()}


def _schemas(payload):
    schema = payload.get("schema")
    version = int(SCHEMA_RE.match(schema).group("version"))
    return schema, version


def compare(args):
    """The whole diff, as a dict, or a ``MandateCompareError``."""
    problems = []
    baseline = load_report(args.baseline, "baseline")
    candidate = load_report(args.report, "candidate")
    tolerances = {
        "count": args.count_tolerance,
        "window": args.window_tolerance,
        "delivery": args.delivery_tolerance,
        "value": args.value_tolerance,
    }
    for name, value in tolerances.items():
        if value is None or value < 0:
            raise MandateCompareError(f"--{name}-tolerance must not be negative")
    if bool(baseline["payload"].get("quick")) != bool(candidate["payload"].get("quick")):
        raise MandateCompareError(
            "the baseline was recorded with "
            f"{'--quick' if baseline['payload'].get('quick') else 'the full windows'} and the "
            f"candidate with {'--quick' if candidate['payload'].get('quick') else 'the full windows'}, "
            "so their sample counts and windows measure different sets; record the "
            "candidate the same way as the baseline"
        )
    gate_checker = _load_gate_checker()
    baseline_cells = compare_cells(baseline, candidate, gate_checker, problems)
    if problems:
        raise MandateCompareError("; ".join(problems))

    arms = []
    for arm_id in sorted(baseline["arms"]):
        candidate_arm = candidate["arms"].get(arm_id)
        entry = {
            "id": arm_id,
            "mandate": baseline["arms"][arm_id].get("mandate"),
            "missing": candidate_arm is None,
            "sample_count": baseline["arms"][arm_id].get("sample_count"),
            "candidate_sample_count": (
                candidate_arm.get("sample_count") if candidate_arm is not None else None
            ),
        }
        if candidate_arm is None:
            entry["regressions"] = [f"the arm is in the baseline and absent from the candidate"]
            entry["drifts"] = []
            entry["moves"] = []
        else:
            entry.update(compare_arm(baseline["arms"][arm_id], candidate_arm, tolerances))
        arms.append(entry)
    new_arms = sorted(set(candidate["arms"]) - set(baseline["arms"]))
    regressions = [
        {"arm": arm["id"], "changes": arm["regressions"]}
        for arm in arms
        if arm["regressions"]
    ]
    regressions.extend(
        {
            "arm": None,
            "changes": [
                f"the coverage cell {cell['cell']!r} was exercised by "
                f"{', '.join(cell['covered_by'])} and is exercised by no arm now"
            ],
        }
        for cell in baseline_cells
        if cell["regression"]
    )
    missing_mandates = sorted(
        set(baseline["payload"].get("mandates") or {})
        - set(candidate["payload"].get("mandates") or {})
    )
    regressions.extend(
        {"arm": None, "changes": [f"the mandate {mandate} is in the baseline and not in the candidate"]}
        for mandate in missing_mandates
    )
    drifts = [
        {"arm": arm["id"], "changes": arm["drifts"]} for arm in arms if arm["drifts"]
    ]
    return {
        "baseline": str(baseline["path"]),
        "candidate": str(candidate["path"]),
        "tolerances": tolerances,
        "baseline_summary": summary_of(baseline),
        "candidate_summary": summary_of(candidate),
        "arms": arms,
        "new_arms": new_arms,
        "cells": baseline_cells,
        "regressions": regressions,
        "value_drifts": drifts,
        "exit_code": 0,
    }


def summary_of(report):
    payload = report["payload"]
    schema, _ = _schemas(payload)
    samples = sum(
        arm.get("sample_count") or 0 for arm in report["arms"].values()
    )
    return {
        "path": str(report["path"]),
        "schema": schema,
        "quick": bool(payload.get("quick")),
        "arms": len(report["arms"]),
        "samples": samples,
        "revision": (payload.get("rtp_mux") or {}).get("revision"),
    }


def _quantities(entry):
    """The short 'what moved' tail of an arm line."""
    parts = list(entry["moves"])
    parts.extend(f"{text} [value drift]" for text in entry["drifts"])
    return "; ".join(parts)


def verdict_lines(diff):
    lines = [
        f"mandate-compare: baseline {diff['baseline']}",
        f"                 candidate {diff['candidate']}",
    ]
    for role in ("baseline_summary", "candidate_summary"):
        summary = diff[role]
        lines.append(
            f"  {role.split('_')[0]:>9}: schema={summary['schema']} "
            f"quick={'yes' if summary['quick'] else 'no'} arms={summary['arms']} "
            f"samples={summary['samples']} revision={summary['revision'] or 'unresolved'}"
        )
    tolerances = diff["tolerances"]
    lines.append(
        f"  tolerances: count {tolerances['count']:.0%}, window "
        f"{tolerances['window']:.1%}, delivery {tolerances['delivery']:.3f}, "
        f"value {tolerances['value']:.0%}"
    )
    lines.append("arms:")
    for entry in diff["arms"][:MAX_ARM_LINES]:
        status = "LOSS" if entry["regressions"] else "ok  "
        samples = entry["sample_count"]
        if entry["missing"]:
            lines.append(
                f"  {status} {entry['id']:<24} {samples} sample(s) -> absent: "
                "coverage regression"
            )
            continue
        parts = [f"samples {samples} -> {entry['candidate_sample_count']}"]
        moved = _quantities(entry)
        verdict = "; ".join(entry["regressions"]) or moved or "nothing moved"
        lines.append(f"  {status} {entry['id']:<24} {'; '.join(parts)}: {verdict}")
    if len(diff["arms"]) > MAX_ARM_LINES:
        lines.append(f"  ... {len(diff['arms']) - MAX_ARM_LINES} more arm(s)")
    regressed_cells = [cell for cell in diff["cells"] if cell["regression"]]
    lines.append(
        f"cells: {len(diff['cells'])} declared cell(s) exercised, "
        f"{len(regressed_cells)} no longer covered"
    )
    for cell in regressed_cells:
        lines.append(f"  LOSS {cell['cell']} (was {', '.join(cell['covered_by'])})")
    if diff["new_arms"]:
        lines.append("new arms (reported, not a regression): " + ", ".join(diff["new_arms"]))
    regressions = sum(len(entry["changes"]) for entry in diff["regressions"])
    drifts = sum(len(entry["changes"]) for entry in diff["value_drifts"])
    lines.append(
        f"verdict: {'COVERAGE-LOSS' if regressions else 'OK'}  exit={diff['exit_code']}  "
        + f"coverage regression(s)={regressions}  value drift(s)={drifts}"
    )
    for entry in diff["regressions"]:
        for change in entry["changes"]:
            lines.append(f"problem: {change}")
    return lines


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mandate-compare",
        description=(
            "Compare a fresh mandate-check report's per-arm measurements against a "
            "committed baseline and report which quantities moved: a fall in an "
            "arm's coverage (its sample count, delivery, wire or a measured window) "
            "is a failure, a statistics move is reported and bounded by a tolerance."
        ),
    )
    parser.add_argument(
        "report",
        nargs="?",
        type=Path,
        default=None,
        help=f"the fresh report to compare (default: ./{DEFAULT_REPORT_NAME})",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=f"the committed baseline report (default: this tool's {DEFAULT_BASELINE_NAME})",
    )
    parser.add_argument(
        "--count-tolerance",
        type=float,
        default=DEFAULT_COUNT_TOLERANCE,
        help=(
            "the relative fall in a sample count, delivery counter or observed "
            "wall-clock that counts as coverage loss (default: "
            f"{DEFAULT_COUNT_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--window-tolerance",
        type=float,
        default=DEFAULT_WINDOW_TOLERANCE,
        help=(
            "the relative fall in a measured window that counts as coverage loss "
            f"(default: {DEFAULT_WINDOW_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--delivery-tolerance",
        type=float,
        default=DEFAULT_DELIVERY_TOLERANCE,
        help=(
            "the absolute fall in an arm's delivery ratio that counts as coverage "
            f"loss (default: {DEFAULT_DELIVERY_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--value-tolerance",
        type=float,
        default=DEFAULT_VALUE_TOLERANCE,
        help=(
            "the relative movement in a statistic that is reported as a value "
            f"drift (default: {DEFAULT_VALUE_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--fail-on-value-drift",
        action="store_true",
        help=(
            "exit 5 when a statistic moved past --value-tolerance; by default such "
            "a move is reported and does not fail, because a latency percentile on "
            "a shared host is noise"
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="write the comparison as JSON to this path as well as printing it",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.report is None:
        args.report = Path(DEFAULT_REPORT_NAME)
    if args.baseline is None:
        args.baseline = MODULE_DIR / DEFAULT_BASELINE_NAME
    try:
        diff = compare(args)
    except MandateCompareError as error:
        print(f"mandate-compare: error: {error}", file=sys.stderr)
        return EXIT_UNCOMPARABLE
    if diff["regressions"]:
        diff["exit_code"] = EXIT_COVERAGE_REGRESSION
    elif diff["value_drifts"] and args.fail_on_value_drift:
        diff["exit_code"] = EXIT_VALUE_DRIFT
    for line in verdict_lines(diff):
        print(line)
    if args.json_out is not None:
        # The diff is written even on a coverage regression: it is the evidence.
        args.json_out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.json_out.expanduser().write_text(
            json.dumps(diff, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"diff:     {args.json_out.expanduser().resolve()}")
    return diff["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
