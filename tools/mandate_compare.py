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

## What it compares, and from whom

Every arm of every producer a report covers is compared, because every arm's
``id`` is unique across the producers of a run (a section is an arm-id
namespace, so two producers cannot share one) and every arm carries the
``producer`` field that printed it. The verdict block names both runs'
producers, so a verdict can be read as covering them — and a producer whose
arms the candidate did not record at all is not a green verdict but a coverage
regression, one absent arm at a time. A producer added since the baseline is
reported as new arms, not as a regression: coverage grew.

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

That relative tolerance is only meaningful where the baseline value is
load-bearing, and whether a counted quantity is *load-bearing for an arm* is a
question about the arm's own **declaration**, not about magnitude: the arms'
coverage cells state the property, the dimensions and the values each arm
exercises, and a cell that names the lane a counter measures is that arm's
claim to be compared on it. So the comparison derives the answer
(`cell_claim`, `arm_claim`, `COUNTER_LANES`) and prints the rule with its
verdict; a claiming cell is compared **with no floor at all**, so a small
counter is a tooth exactly like a large one.

The three ways a cell can speak about a counter, and what each does:

- **claimed** — the cell names the counter's lane as the arm's own (`lane=bulk`)
  or offers a workload on it (`load=` or `bulk=`, any value but `none`). Compared,
  with no floor: a fall past the tolerance **or** a disappearance is a coverage
  regression.
- **idle** — the cell declares the lane carries no workload (`load=none` or
  `bulk=none`, the way the M4 arms declare the bulk lane connected but never
  opened). *Reported and never compared*, and that is the intended behaviour
  rather than an accident of magnitude: the arm's own declaration says the
  counter is not the coverage the arm measures.
- **unstated** — the cell names neither. The declaration does not decide and
  the comparison does not guess: the pair is recorded in the diff's
  `claim_gaps` (with what therefore did decide it: the key's measured floor, or
  the plain count tolerance where the key has no floor), and only here the
  **measured floor** (`COUNT_FLOORS_BYTES`) is what keeps a residue among them
  from failing. The floor's value and derivation are unchanged; what narrowed is
  the population it applies to, from every arm to the arm x counter pairs no
  cell claims or disclaims.

What the declaration can and cannot express is stated with `COUNTER_LANES` and
the blocker recorded there: the grammar admits two positive forms (`lane=<the
counter's lane>`, and `load=`/`bulk=` with a value that is not `none`) and both
are read, while `shape=` (the *interactive* lane's load shape) and `rate=`/`burst=`
(a rate regime, not a lane) are not, and `lane=dual` is silent about the bulk
lane — the two arms whose cells say it need opposite outcomes, which is the
specific evidence that keeps the floor at all.

**The detection limit**, stated so a green diff is not read as more than it is:
this comparison sees an arm that disappeared, a load-bearing sample count or
delivery/wire counter that fell by half or more, a load-bearing counter that
stopped being measured, a measured window that shrank by more than 1 %, a
statistic the assertions read that stopped being measured, the delivery ratio
falling past its tolerance, a coverage cell no arm covers any more, and a
mandate that vanished. It does
**not** see an arm that keeps its sample count and its counters while its
impairment was quietly weakened — a 2 % loss arm retuned to 1 % measures the
same shape under a milder regime. That is a change to a frozen perf-test
setting, and the guard against it is the setting's immutability and reading the
arm against its declared coverage cell in `tools/mandate-arms.json`, not this
comparison. Nor does it see a shortening that leaves the window in place, keeps
at least half the samples and drops no cell. And an arm whose cell *declares a
lane idle* has that lane's counters reported and never compared, deliberately,
so a real workload on a lane the declaration calls idle is not caught here: the
accuracy of the declaration is trusted, exactly as it is for the impairment it
names. That is the price of deciding relevance from the declaration; the
*unstated* pairs that used to be decided by magnitude are the ones this rule
takes away from it, and they are the ones it names.

A false positive it used to report, and what the declaration does about it. The
counts once had no absolute floor, so one small enough that half of it is a
handful of datagrams crossed the 50 % tolerance on an **unchanged** tree: on
the `M1/lone_tail`/`M2/lone_tail` arms of two real full runs of the unchanged
tree, `bulk_wire_bytes` read 1920 in one and 785 in the next (-59 %),
red-flagging two arms that drive no bulk workload — the few kilobytes are the
fixture's incidental bulk-lane traffic, not the coverage the arm exists to
measure. A magnitude floor removed the false positive but could not tell a
residue from a small real workload, which is a *relevance* question: it is the
arm's declared cells that answer it, and they are what this comparison reads
now (`cell_claim`, `arm_claim`). On those two arms the cell's `lane=dual`
leaves the bulk lane unstated, so they keep the floor and the wobble stays
*visible* in the arm's line while the verdict stays green — and the pair is
printed and written in `claim_gaps`, so the hole is named rather than implied.
A cell that *claims* the lane (`lane=bulk`, or a `load` on it) is compared with
no floor at all, so the residue arm's silence is doing real work: the same
1920 -> 785 pair fails on a claiming cell, and every claim the declarations
make is printed with the verdict.

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
# How many arms and claim gaps the printed block lists before it summarises the
# rest, so a large run cannot bury its verdict in a wall of lines.
MAX_ARM_LINES = 200
MAX_GAP_LINES = 24

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

# ─────────────────────── which counters are load-bearing ─────────────────────
#
# A counter's *fall* is coverage loss only when the arm's declaration claims the
# lane it measures. What a cell can say, and what each answer does to the
# comparison, is stated in the module docstring: a claim keeps the tooth with no
# floor, an idle declaration is reported and never compared, and an unstated
# lane keeps the measured floor and is named in `claim_gaps`.

# The lane a compared counter measures, where it is not the arm's own lane.
# Only keys that measure a *second* lane may appear: a key that measures the
# arm's own lane cannot be disclaimed by a dimension about another lane, so
# nothing of the sort is listed here. Every key absent from this map measures
# the arm's own lane — the lane its cells name and its samples are taken on.
COUNTER_LANES = {
    "bulk_wire_bytes": "bulk",
    "bulk_sink_bytes": "bulk",
}

# The cell dimensions that speak about a lane, and the one value that declares it
# idle. `lane` names the lane the arm measures on; `load` and `bulk` declare the
# workload offered on the *second* (bulk) lane, and both are read, because the
# grammar's own cells express the bulk workload either way: `rtp_mux`'s gate rows
# write `load=bulk`/`load=bulk-burst`/`load=bulk-matched` and `load=none`, and its
# `hol_probe` rows write `bulk=none`/`bulk=shared`/`bulk=split-and-shared`. Two
# forms are deliberately *not* read: `shape=` names the **interactive** lane's
# load shape (the arm table these cells restate carries a separate `bulk` column),
# so reading `shape=cadence` as a bulk claim would make M4's
# `shape=cadence+load=none` a claim of a lane it never opens; and `rate=`/`burst=`
# name a rate regime that is not a lane at all — `conformance-reorder@
# impairment=reorder+rate=rate-limit` and `reorder-rate@impairment=reorder+
# rate=curve` carry them with no bulk lane in sight.
LANE_DIMENSION = "lane"
LOAD_DIMENSIONS = ("load", "bulk")
IDLE_LOAD_VALUES = frozenset({"none"})

CLAIMED = "claimed"
IDLE = "idle"
UNSTATED = "unstated"

# How one arm's cells combine into its claim: a claim wins wherever any declared
# cell makes it (a tooth is never dropped because a second cell was silent), a
# silent cell is preferred to one that declares the lane idle, and only an arm
# whose every cell says the lane is idle is left uncompared.
CLAIM_PRECEDENCE = (CLAIMED, UNSTATED, IDLE)

# `<dimension>=<value>`, the per-dimension shape `tools/check-gate.py`'s
# `CELL_DIMENSION_RE` admits. That checker is the grammar's authority and has
# already rejected a malformed cell before a claim is read; this repeats the
# shape rather than importing the checker into every claim call.
CELL_DIMENSION_PART_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>[^=,+\s]+)$"
)

# The **measured floor** of a counted quantity, in bytes, applied to a pair the
# cells leave unstated — and only there: a claiming cell is compared with no
# floor, and an idle cell is not compared at all. The floor is an absolute
# quantity because the noise is quantisation noise (a few datagrams): it
# dominates a small counter and is invisible in a large one. Only byte counters
# may be listed here.
#
# Derived, not chosen, and unchanged by the narrowing above. Over every full-run
# report recorded on disk, the two bulk-lane counters read 0..3525 B on the
# `M1/lone_tail`/`M2/lone_tail` request-response arms — whose cells, under this
# rule, leave the lane unstated — (56 observations across 15 runs, 6 of them of
# one unchanged tree, tree 937a25b06400b83124c7b99d114379999a800a65), and
# 2097152..8484001 B on every arm that drives the bulk lane (112 observations) —
# a 595x gap. The floor is the largest power of two inside that gap (geometric
# midpoint 85978 B), leaving an 18.6x margin below the observed residue ceiling
# and a 32x margin above the smallest observed real bulk workload. Re-deriving a
# tighter value would need a new measurement of that spread; narrowing which
# pairs it applies to needs none, and that is what this rule does.
#
# It is kept, rather than replaced outright, because of one token the
# declarations cannot distinguish: `lane=dual`. The `M1/clean` cell
# (`M1@impairment=loss2pct-iid+latency=25ms+jitter=5ms+lane=dual+shape=cadence
# +flows=1+scale=256B+metric=p99`) drives 2 MiB / 3 s on the bulk lane and
# records `bulk_wire_bytes` 8482399, so reading its cell as a non-claim would
# drop that counter out of the comparison and let the arm lose its concurrent
# bulk load with a green verdict. The `M1/lone_tail` cell
# (`...lane=dual+shape=request-response+depth=1...`) runs with the bulk lane idle
# and records `bulk_wire_bytes` 1920 and then 785 between two runs of the
# unchanged tree, so reading *its* cell as a claim restores that false positive.
COUNT_FLOORS_BYTES = {
    "bulk_wire_bytes": 65536,
    "bulk_sink_bytes": 65536,
}

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


def floor_bytes(key):
    """The declared measured floor of one counted quantity, 0 when none.

    A floor is consulted only where the arm's cells leave the counter's lane
    unstated (`UNSTATED`); a claiming cell is compared with none, and an idle
    cell is not compared at all.
    """
    return COUNT_FLOORS_BYTES.get(key, 0)


def cell_dimensions(cell):
    """``dimension -> value`` for one coverage cell, or ``None`` when ambiguous.

    A cell stating one dimension twice says two things about it and the reader
    cannot tell which a claim should read, so it yields nothing rather than one
    of the two values (the arm's claim is then unstated, which is recorded).
    """
    _, _, dimensions = cell.partition("@")
    named = {}
    for part in dimensions.split("+"):
        match = CELL_DIMENSION_PART_RE.match(part)
        if match is None:
            return None
        name = match.group("name")
        if name in named:
            return None
        named[name] = match.group("value")
    return named


def cell_claim(cell, key):
    """How one cell speaks about the lane ``key`` measures, ``(claim, why)``.

    ``claimed`` when the cell says the arm measures on that lane (it names the
    lane as the arm's own) or offers a workload on it (a `load`/`bulk` dimension
    whose value is not `none`); ``idle`` when it declares that lane carries no
    workload; ``unstated`` when it names neither, which is not a claim and not a
    disclaimer. The `why` is the cell's own words, so a verdict that rests on
    the answer names it.
    """
    lane = COUNTER_LANES.get(key)
    dimensions = cell_dimensions(cell)
    if dimensions is None:
        return UNSTATED, "the cell states no readable dimension (or states one twice)"
    named = dimensions.get(LANE_DIMENSION)
    if lane is None:
        # The key measures the arm's own lane — the lane its cells name and its
        # samples are taken on. `load`/`bulk` are about the *other* lane and
        # cannot disclaim it.
        if named is None:
            return UNSTATED, "the cell names no lane dimension"
        return CLAIMED, f"the cell names the arm's own lane ({LANE_DIMENSION}={named})"
    if named == lane:
        return CLAIMED, (
            f"the cell names the counter's lane as the arm's own ({LANE_DIMENSION}={lane})"
        )
    declared = [
        (name, dimensions[name]) for name in LOAD_DIMENSIONS if name in dimensions
    ]
    for name, value in declared:
        if value not in IDLE_LOAD_VALUES:
            return CLAIMED, (
                f"the cell offers a workload on the {lane} lane ({name}={value})"
            )
    if declared:
        name, value = declared[0]
        return IDLE, f"the cell declares the {lane} lane idle ({name}={value})"
    if named is None:
        return UNSTATED, f"the cell names neither the {lane} lane nor a load on it"
    return UNSTATED, (
        f"the cell names {LANE_DIMENSION}={named} and no load on the {lane} lane"
    )


def arm_claim(arm, key):
    """``(claim, why, gap)``: what one arm's declared cells say about ``key``.

    The arm's cells combine by `CLAIM_PRECEDENCE`, so a claim any declared cell
    makes keeps the tooth. ``why`` is the decided cell's own reason, without the
    cell text (the cells are in the arm's record and in every gap this files).
    ``gap`` is true when the arm's cells leave the lane unstated: the declaration
    decides nothing for that pair, and the pair is filed so that what *did*
    decide it (a floor, or the plain count tolerance where the key has no floor)
    is visible rather than implicit.
    """
    cells = [cell for cell in (arm.get("cells") or []) if isinstance(cell, str)]
    if not cells:
        return UNSTATED, "the arm declares no coverage cell", True
    claims = [cell_claim(cell, key) for cell in cells]
    for wanted in CLAIM_PRECEDENCE:
        for claim, why in claims:
            if claim == wanted:
                return claim, why, wanted == UNSTATED
    raise AssertionError(f"no claim outcome for {key!r}: {claims!r}")


def compare_counter(key, baseline, candidate, tolerance, claim=CLAIMED, floor=0, why=None):
    """One counted quantity as ``("regression"|"move"|None, text)``.

    The fall is compared with ``<=`` so that a shortening which takes exactly
    half an arm's samples — the case the instrument exists for — is a
    regression, and the tolerance is the run-to-run spread the comparison is not
    allowed to read as coverage loss.

    ``claim`` is what the arm's declared cells say about the lane ``key``
    measures (`arm_claim`), and it alone decides whether a movement may fail:

    - ``CLAIMED`` — compared, with no floor at all, however small the baseline;
    - ``IDLE`` — reported and never compared, because the arm's own declaration
      says the lane carries no workload;
    - ``UNSTATED`` — reported, and `floor` (the key's measured floor in bytes,
      0 for a key that has none) is what keeps a residue from failing; `why`
      names the cell's own words for the silence.
    """
    if claim == IDLE:
        if candidate is None:
            return (
                "move",
                f"{key} {baseline} -> not measured [the arm's cells declare the "
                f"lane idle ({why}): reported, never compared]",
            )
        if candidate == baseline:
            return None, None
        change = relative_change(baseline, candidate)
        text = f"{key} {baseline} -> {candidate}" + (
            f" ({change:+.1%})" if change is not None else ""
        )
        return (
            "move",
            f"{text} [the arm's cells declare the lane idle ({why}): reported, "
            "never compared]",
        )
    inside = (
        claim == UNSTATED
        and floor
        and isinstance(baseline, (int, float))
        and not isinstance(baseline, bool)
        and baseline < floor
    )
    if candidate is None:
        if inside:
            return (
                "move",
                f"{key} {baseline} -> not measured [the arm's cells leave the lane "
                f"unstated ({why}), and the baseline sits inside the measured "
                f"{floor}-byte floor: absent, not a regression]",
            )
        return "regression", f"{key} {baseline} -> not measured"
    change = relative_change(baseline, candidate)
    if change is None:
        return (None, None) if candidate == baseline else ("move", f"{key} {baseline} -> {candidate}")
    if change <= -tolerance:
        if inside:
            return (
                "move",
                f"{key} {baseline} -> {candidate} ({change:+.1%}), past the "
                f"{tolerance:.0%} tolerance but inside the measured {floor}-byte "
                f"floor, which applies because the arm's cells leave the lane "
                f"unstated ({why}): reported, not a regression",
            )
        claimed_note = (
            f" [the arm's cells claim the lane ({why}), so no floor applies]"
            if claim == CLAIMED and floor_bytes(key) > 0
            else ""
        )
        return (
            "regression",
            f"{key} {baseline} -> {candidate} ({change:+.1%}), past the "
            f"{tolerance:.0%} tolerance{claimed_note}",
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


def decided_by(key, baseline):
    """What an *unstated* pair's verdict rests on, the declaration deciding nothing.

    The key's measured floor when it has one and the baseline sits inside it; the
    50 % count tolerance otherwise; and for a key with no floor at all, that same
    tolerance — which is where a claimed pair would be too, so the gap weakens
    nothing.
    """
    floor = floor_bytes(key)
    if not floor:
        return "no-floor"
    return "floor" if baseline < floor else "tolerance"


def compare_arm(baseline_arm, candidate_arm, tolerances):
    """One arm's changes, split into coverage regressions and value moves.

    The claim read for every counter is the *baseline* arm's — the declaration
    under test — and a candidate that drops the cell making a claim is caught by
    the cell-coverage comparison, which fails when a declared cell is exercised
    by no arm any more.
    """
    regressions = []
    moves = []
    drifts = []
    gaps = []
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
        claim, why, gap = arm_claim(baseline_arm, key)
        if gap:
            gaps.append(
                {
                    "arm": baseline_arm.get("id"),
                    "key": key,
                    "baseline": baseline_counters[key],
                    "cells": [cell for cell in (baseline_arm.get("cells") or [])],
                    "why": why,
                    "floor_bytes": floor_bytes(key),
                    "decided_by": decided_by(key, baseline_counters[key]),
                }
            )
        outcome, text = compare_counter(
            key,
            baseline_counters[key],
            candidate_counters.get(key),
            tolerances["count"],
            claim=claim,
            floor=floor_bytes(key) if claim == UNSTATED else 0,
            why=why,
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
    return {"regressions": regressions, "drifts": drifts, "moves": moves, "gaps": gaps}


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
            entry["gaps"] = []
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
    gaps = [gap for arm in arms for gap in arm.get("gaps") or []]
    return {
        "baseline": str(baseline["path"]),
        "candidate": str(candidate["path"]),
        "tolerances": tolerances,
        # The floors and what determines a counter's load-bearing status: a
        # claim, an idle declaration or an unstated lane. Both are printed and
        # written so a verdict can be read against the rule it applied.
        "count_floors": dict(COUNT_FLOORS_BYTES),
        "claim_rule": {
            "counter_lanes": dict(COUNTER_LANES),
            "claimed": (
                f"{LANE_DIMENSION}=<the counter's lane> (the lane is the arm's own) or "
                + " or ".join(LOAD_DIMENSIONS)
                + f"=<value other than {', '.join(sorted(IDLE_LOAD_VALUES))}> "
                "(the arm offers a workload on that lane)"
            ),
            "idle": (
                " or ".join(LOAD_DIMENSIONS)
                + f"=<{', '.join(sorted(IDLE_LOAD_VALUES))}> (the cell declares "
                "that lane carries no workload)"
            ),
            "unstated": (
                "neither; the pair is reported in claim_gaps and its counter is "
                "compared under the key's measured floor"
            ),
            "combine": " > ".join(CLAIM_PRECEDENCE) + " over an arm's cells",
        },
        "claim_gaps": gaps,
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
        "producers": producers_of(payload, report["arms"]),
    }


def producers_of(payload, arms):
    """The producer ids a report covers, in its own order.

    A ``mandate-check/5`` report names the producers it declared and selected,
    so the comparison can say which producers its verdict covers. A ``/3`` or
    ``/4`` report has no producer record at all (it predates the second
    producer), so the ids are read from the arms' own ``producer`` field — and
    when even that is absent the list is empty, which the verdict block prints
    as ``unnamed`` rather than as a producer this comparison invented.
    """
    declared = payload.get("producers")
    if isinstance(declared, dict):
        selected = payload.get("producers_selected")
        if isinstance(selected, list) and selected:
            return [str(entry) for entry in selected]
        return sorted(str(entry) for entry in declared)
    return sorted(
        {str(arm["producer"]) for arm in arms.values() if arm.get("producer")}
    )


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
        lines.append(
            f"  {'':>9}  producers: "
            f"{', '.join(summary['producers']) if summary['producers'] else 'unnamed'} "
            f"({len(summary['producers'])} covered)"
        )
    tolerances = diff["tolerances"]
    lines.append(
        f"  tolerances: count {tolerances['count']:.0%}, window "
        f"{tolerances['window']:.1%}, delivery {tolerances['delivery']:.3f}, "
        f"value {tolerances['value']:.0%}"
    )
    floors = diff["count_floors"]
    rule = diff["claim_rule"]
    lanes = ", ".join(sorted(rule["counter_lanes"]))
    lines.append(
        "   claims: a counter is load-bearing for an arm when the arm's declared cells "
        "claim the lane it measures"
    )
    lines.append(
        f"           claimed by {rule['claimed']}; idle (reported, never compared) by "
        f"{rule['idle']}; unstated otherwise. Iterated over an arm's cells: {rule['combine']}"
    )
    lines.append(
        f"           the bulk-lane keys are {lanes}; every other compared counter "
        "measures the arm's own lane"
    )
    if floors:
        lines.append(
            "    floors: "
            + ", ".join(f"{key} {value}" for key, value in sorted(floors.items()))
            + " (bytes) — applied only where the arm's cells leave the lane unstated; a "
            "claiming cell is compared with no floor, an idle one not at all"
        )
    gaps = diff["claim_gaps"]
    on_floor = sum(1 for gap in gaps if gap["decided_by"] == "floor")
    lines.append(
        f"      gaps: {len(gaps)} unstated arm x counter pair(s) — the declaration decides "
        f"nothing for these; {on_floor} of them sit inside their floor, where that (not "
        "the declaration) keeps a residue from failing"
    )
    gap_note = {
        "floor": lambda gap: f"inside the {gap['floor_bytes']}-byte floor: reported, not failed",
        "tolerance": lambda gap: "above its floor: compared by magnitude, not by the declaration",
        "no-floor": lambda gap: "no floor on this key: compared exactly as a claimed counter",
    }
    for gap in gaps[:MAX_GAP_LINES]:
        lines.append(
            f"            {gap['arm']} {gap['key']} = {gap['baseline']} "
            f"[{gap_note[gap['decided_by']](gap)}; {gap['why']}]"
        )
    if len(gaps) > MAX_GAP_LINES:
        lines.append(f"            ... {len(gaps) - MAX_GAP_LINES} more gap(s)")
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
