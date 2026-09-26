#!/usr/bin/env python3

"""Exercise `tools/mandate-compare`'s per-arm coverage comparison.

The comparison reads two `mandate-check.json` reports, so every case here
writes two report fixtures and runs the command in-process: no Rust build, no
network, no timing. Each failure mode the command claims to catch has a case,
because a coverage checker that cannot fail is worse than none — a dropped arm,
a fallen sample count, a shrunk window, a fallen delivery or wire counter, a
statistic that stopped being measured, a coverage cell no arm covers any more —
and the well-formed case must pass with its value moves reported rather than
failed.

Whether a counted quantity is load-bearing is then a question about the arm's
declared cells, so each of the three answers has a case: a cell that claims the
counter's lane keeps the tooth **with no floor** (below the measured floor as
well as above it), a cell that declares the lane idle is reported and never
compared, and a cell that says neither is recorded as a gap and keeps the
measured floor. The vacuity pair is stated rather than implied: a *claiming*
arm whose counter vanishes fails, and a *non-claiming* one whose counter
vanishes stays green — the latter is the intended behaviour, not an accident.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("mandate_compare.py")
SPEC = importlib.util.spec_from_file_location("mandate_compare", MODULE_PATH)
MANDATE_COMPARE = importlib.util.module_from_spec(SPEC)
sys.modules["mandate_compare"] = MANDATE_COMPARE
SPEC.loader.exec_module(MANDATE_COMPARE)

M1_CELL = "M1@impairment=clean+lane=dual+metric=p99"
M4_CELL = "M4@lane=dual+flows=4+metric=per-flow-share"
PROBE_CELL = "probe-forwarding@metric=throughput+layer=netem-runner"
# The recorded arms whose bulk-lane counters the floor was derived from: the
# `M1/lone_tail` cell drives no bulk load (its residue read 1920 B and then
# 785 B between two runs of the unchanged tree), and the `M1/clean` cell drives
# 2 MiB / 3 s of it. Both say `lane=dual`, which is why the declaration cannot
# tell them apart and the floor survives for that pair.
LONE_TAIL_CELL = (
    "M1@impairment=gilbert-elliott-5-8+jitter=100ms+lane=dual"
    "+shape=request-response+depth=1+flows=1+scale=256B+metric=p99"
)
CLEAN_CELL = (
    "M1@impairment=loss2pct-iid+latency=25ms+jitter=5ms+lane=dual"
    "+shape=cadence+flows=1+scale=256B+metric=p99"
)
# A hypothetical future arm whose cell *does* claim the bulk lane, by each of
# the forms the grammar is used to express one.
CLAIMING_CELLS = {
    "lane=bulk": "M3@lane=bulk+rate=8MiBps+burst=2MiB+period=3s+reps=3+metric=capacity-fraction",
    "load=bulk": "M1@lane=dual+load=bulk+metric=p99",
    "bulk=shared": "hol@rate=400kbps+loss=iid1+bulk=shared+metric=p99",
}
# Cells that declare the bulk lane idle, the two forms the producers use.
IDLE_CELLS = {
    "load=none": "M4@impairment=clean+lane=dual+flows=4+shape=cadence+load=none+metric=per-flow-share",
    "bulk=none": "hol@rate=400kbps+loss=iid1+bulk=none+metric=p99",
}
IDLE_CELL = IDLE_CELLS["load=none"]


def arm_with(
    arm_id,
    cells,
    counters,
    sample_count=2400,
):
    """One arm carrying exactly the counters a case is about.

    `sent`/`received`/`wire_bytes` are always present, so `counters` names the
    quantities under test and a key left out of it is the *absence* of that key
    rather than the absence of the whole record.
    """
    record = arm(arm_id, cells=cells)
    record["counters"] = {
        "sent": sample_count,
        "received": sample_count,
        "wire_bytes": 126000,
        **counters,
    }
    record["sample_count"] = sample_count
    return record


def arm(
    arm_id,
    sample_count=2400,
    wire=126000,
    window=12.0,
    p50=25.0,
    p99=90.0,
    p999=98.0,
    delivery=1.0,
    cells=(M1_CELL,),
    counters=None,
    stats=None,
    windows=None,
):
    """One arm record in the shape `tools/mandate-check` writes."""
    mandate, label = arm_id.split("/", 1)
    record = {
        "id": arm_id,
        "mandate": mandate,
        "label": label,
        "dialect": "kv",
        "sample_count": sample_count,
        "stats": {
            "p50": p50,
            "p99": p99,
            "p999": p999,
            "over250": 0,
            "delivery": delivery,
        },
        "counters": {
            "sent": sample_count,
            "received": sample_count,
            "wire_bytes": wire,
        },
        "windows": {"window_seconds": window, "wall_seconds": window + 0.4},
        "cells": list(cells),
        "values": {},
        "raw_line": f"[mandate-smoke {label}] ...",
    }
    if counters is not None:
        record["counters"].update(counters)
    if stats is not None:
        record["stats"].update(stats)
    if windows is not None:
        record["windows"].update(windows)
    return record


def report(arms, quick=False, schema="mandate-check/3", mandates=("M1", "M2", "M3", "M4")):
    """One `mandate-check.json` fixture."""
    payload = {
        "schema": schema,
        "ok": True,
        "exit_code": 0,
        "verdict": "PASS",
        "quick": quick,
        "rtp_mux": {"revision": "0" * 40, "change_id": None, "revision_source": "jj"},
        "mandates": {
            mandate: {"declared": True, "verdict": "PASS", "values": {}} for mandate in mandates
        },
        "arms": arms,
    }
    named = sorted({entry.get("producer") for entry in arms if entry.get("producer")})
    if named:
        # A `mandate-check/5` report records the producers it covers; the arms'
        # own `producer` field is what the comparison reads when it is absent.
        payload["producers"] = {
            entry: {"id": entry, "selected": True} for entry in named
        }
        payload["producers_declared"] = named
        payload["producers_selected"] = named
    return payload


def baseline_report():
    return report(
        [
            arm("M1/clean"),
            arm("M1/hostile", sample_count=2500, p99=245.1, wire=43210),
            arm("M4/m4/clean", sample_count=None, p99=None, cells=(M4_CELL,)),
        ]
    )


class MandateCompareTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)
        self.baseline = self.root / "mandate-baseline.json"

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, candidate, *extra, baseline=None):
        path = self.root / "candidate.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        base_path = baseline or self.baseline
        if baseline is None:
            base_path.write_text(json.dumps(baseline_report()), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = [str(path), "--baseline", str(base_path), *extra]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE_COMPARE.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def reject(self, candidate, fragment, *extra, baseline=None):
        code, stdout, stderr = self.run_tool(candidate, *extra, baseline=baseline)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_UNCOMPARABLE, stdout + stderr)
        self.assertIn(fragment, stdout + stderr)
        return code, stdout, stderr

    # -- two producers in one comparison ------------------------------------

    def two_producer_report(self, probes=True):
        """A report covering `rtp_mux`'s three arms and one probe arm."""
        arms = baseline_report()["arms"]
        for entry in arms:
            entry["producer"] = "rtp_mux"
        if probes:
            probe = arm(
                "probe/forwarding",
                sample_count=200000,
                wire=0,
                cells=(PROBE_CELL,),
            )
            probe["producer"] = "netem_test"
            # A probe measures no window, so its record carries none.
            probe["windows"] = {}
            arms = arms + [probe]
        return report(arms)

    def write_baseline(self, payload, name="two-producer.json"):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_two_producers_are_compared_and_both_are_named(self):
        base = self.write_baseline(self.two_producer_report())
        code, stdout, stderr = self.run_tool(self.two_producer_report(), baseline=base)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("producers: netem_test, rtp_mux (2 covered)", stdout)
        self.assertIn("ok   probe/forwarding", stdout)

    def test_a_producer_the_candidate_dropped_is_a_coverage_regression(self):
        base = self.write_baseline(self.two_producer_report())
        code, stdout, _ = self.run_tool(
            self.two_producer_report(probes=False), baseline=base
        )
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION)
        self.assertIn("probe/forwarding", stdout)
        self.assertIn("absent: coverage regression", stdout)
        self.assertIn("producers: rtp_mux (1 covered)", stdout)

    def test_a_second_producers_halved_sample_count_is_a_coverage_regression(self):
        base = self.write_baseline(self.two_producer_report())
        candidate = self.two_producer_report()
        for entry in candidate["arms"]:
            if entry["id"] == "probe/forwarding":
                entry["sample_count"] = 100000
                entry["counters"]["received"] = 100000
                entry["counters"]["sent"] = 100000
        code, stdout, _ = self.run_tool(candidate, baseline=base)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION)
        self.assertIn("sample_count 200000 -> 100000", stdout)

    def test_a_report_without_producer_records_names_the_arms_producers(self):
        base = self.write_baseline(self.two_producer_report())
        candidate = self.two_producer_report()
        for key in ("producers", "producers_declared", "producers_selected"):
            candidate.pop(key)
        code, stdout, _ = self.run_tool(candidate, baseline=base)
        self.assertEqual(code, 0)
        self.assertIn("producers: netem_test, rtp_mux (2 covered)", stdout)

    def test_a_report_with_no_producer_information_is_named_unnamed(self):
        code, stdout, _ = self.run_tool(baseline_report())
        self.assertEqual(code, 0)
        self.assertIn("producers: unnamed (0 covered)", stdout)

    # -- the honest cases ---------------------------------------------------

    def test_an_unchanged_run_is_green_and_says_nothing_moved(self):
        code, stdout, stderr = self.run_tool(baseline_report())
        self.assertEqual(code, 0, stderr)
        self.assertIn("verdict: OK  exit=0", stdout)
        self.assertIn("nothing moved", stdout)
        self.assertIn("coverage regression(s)=0", stdout)
        self.assertIn("cells: 2 declared cell(s) exercised, 0 no longer covered", stdout)

    def test_a_schema_four_candidate_still_compares_against_a_three_baseline(self):
        # The tree id is a new provenance field, not a new comparison input: a
        # per-arm reader keeps working across the schema bump.
        candidate = baseline_report()
        candidate["schema"] = "mandate-check/4"
        candidate["rtp_mux"]["tree_id"] = "a" * 40
        candidate["rtp_mux"]["tree_id_source"] = "jj"
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("verdict: OK  exit=0", stdout)
        self.assertIn("schema=mandate-check/3", stdout)
        self.assertIn("schema=mandate-check/4", stdout)

    def test_a_statistic_move_is_reported_and_bounded_not_failed(self):
        candidate = baseline_report()
        candidate["arms"][0]["stats"]["p99"] = 150.0
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("p99 90.0 -> 150.0 (+66.7%) [past the 50% value tolerance]", stdout)
        self.assertIn("value drift(s)=1", stdout)
        self.assertIn("[value drift]", stdout)

    def test_a_statistic_move_fails_only_when_strictness_is_asked_for(self):
        candidate = baseline_report()
        candidate["arms"][0]["stats"]["p99"] = 150.0
        code, _, stderr = self.run_tool(candidate, "--fail-on-value-drift")
        self.assertEqual(code, MANDATE_COMPARE.EXIT_VALUE_DRIFT, stderr)
        self.assertIn("exit=5", self.run_tool(candidate, "--fail-on-value-drift")[1])

    def test_a_small_statistic_move_is_reported_without_drifting(self):
        candidate = baseline_report()
        candidate["arms"][0]["stats"]["p99"] = 100.0
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("p99 90.0 -> 100.0", stdout)
        self.assertNotIn("[value drift]", stdout)
        self.assertIn("value drift(s)=0", stdout)

    def test_a_sample_count_inside_the_tolerance_is_a_reported_move(self):
        candidate = baseline_report()
        candidate["arms"][0]["sample_count"] = 2440
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("sample_count 2400 -> 2440 (+1.7%)", stdout)
        self.assertIn("coverage regression(s)=0", stdout)

    def test_a_new_arm_is_reported_and_is_not_a_regression(self):
        candidate = baseline_report()
        candidate["arms"].append(arm("M2/clean"))
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("new arms (reported, not a regression): M2/clean", stdout)

    def test_json_out_writes_the_diff(self):
        out = self.root / "diff.json"
        code, _, stderr = self.run_tool(baseline_report(), "--json-out", str(out))
        self.assertEqual(code, 0, stderr)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(diff["exit_code"], 0)
        self.assertEqual(len(diff["arms"]), 3)
        self.assertEqual(diff["tolerances"]["count"], MANDATE_COMPARE.DEFAULT_COUNT_TOLERANCE)
        self.assertEqual(diff["tolerances"]["window"], MANDATE_COMPARE.DEFAULT_WINDOW_TOLERANCE)

    # -- every coverage regression: non-zero, and naming the quantity -------

    def test_a_dropped_arm_is_a_coverage_regression(self):
        candidate = baseline_report()
        candidate["arms"] = [arm for arm in candidate["arms"] if arm["id"] != "M1/hostile"]
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("LOSS M1/hostile", stdout)
        self.assertIn("the arm is in the baseline and absent from the candidate", stdout)
        self.assertIn("verdict: COVERAGE-LOSS  exit=4", stdout)

    def test_a_fallen_sample_count_is_a_coverage_regression(self):
        candidate = baseline_report()
        candidate["arms"][0]["sample_count"] = 1200
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        # Exactly half is a regression: it is the shortening the instrument
        # exists to catch, so the boundary belongs on the failing side.
        self.assertIn(
            "sample_count 2400 -> 1200 (-50.0%), past the 50% tolerance", stdout
        )

    def test_a_count_fall_inside_the_tolerance_is_a_reported_move(self):
        candidate = baseline_report()
        candidate["arms"][0]["sample_count"] = 2000
        candidate["arms"][0]["counters"]["wire_bytes"] = 100000
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("sample_count 2400 -> 2000 (-16.7%)", stdout)
        self.assertIn("wire_bytes 126000 -> 100000 (-20.6%)", stdout)
        self.assertIn("coverage regression(s)=0", stdout)

    def test_a_shrunk_window_is_a_coverage_regression_at_a_tight_tolerance(self):
        candidate = baseline_report()
        candidate["arms"][0]["windows"]["window_seconds"] = 4.0
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("window_seconds 12.0 -> 4.0 (-66.7%)", stdout)
        self.assertIn("past the 1% tolerance", stdout)

    def test_a_window_move_inside_the_tight_tolerance_is_not_a_regression(self):
        candidate = baseline_report()
        candidate["arms"][0]["windows"]["window_seconds"] = 11.95
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, 0, stderr)
        self.assertIn("coverage regression(s)=0", stdout)

    def test_a_fallen_counter_is_a_coverage_regression(self):
        candidate = baseline_report()
        candidate["arms"][0]["counters"]["wire_bytes"] = 50000
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("wire_bytes 126000 -> 50000 (-60.3%)", stdout)

    # -- the claim rule: a counter the cells claim is a tooth, with no floor -

    def one_arm(self, arm_id, cells, counters, name):
        """A baseline of one lone arm, written to disk, and its path."""
        return self.write_baseline(report([arm_with(arm_id, cells, counters)]), name=name)

    def test_a_claiming_cell_below_the_floor_is_compared_and_fails(self):
        # The future-arm case that motivated the rule: the cell claims the bulk
        # lane, so a 40 000-byte workload is a tooth exactly like 8 MiB — a
        # magnitude floor would have swallowed this fall.
        for form, cell in sorted(CLAIMING_CELLS.items()):
            with self.subTest(claim=form):
                base_path = self.one_arm(
                    "M1/clean",
                    [cell],
                    {"bulk_wire_bytes": 40000},
                    f"claim-{form.replace('=', '-')}.json",
                )
                candidate = report(
                    [arm_with("M1/clean", [cell], {"bulk_wire_bytes": 20000})]
                )
                code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
                self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
                self.assertIn("bulk_wire_bytes 40000 -> 20000 (-50.0%)", stdout)
                self.assertIn("no floor applies", stdout)
                self.assertNotIn("reported, not a regression", stdout)

    def test_a_claiming_cell_whose_counter_vanishes_fails(self):
        # The tooth the magnitude band removed, restored for a claiming arm.
        cell = CLAIMING_CELLS["lane=bulk"]
        base_path = self.one_arm(
            "M1/clean", [cell], {"bulk_sink_bytes": 40000}, "claim-absent.json"
        )
        candidate = report([arm_with("M1/clean", [cell], {})])
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
        self.assertIn("bulk_sink_bytes 40000 -> not measured", stdout)

    def test_the_vacuity_pair_a_non_claiming_cell_whose_counter_vanishes_is_ok(self):
        # Stated as intended behaviour rather than left as an accident: the
        # cell declares the lane idle, so the counter is not the coverage the
        # arm measures and its disappearance is reported, never compared.
        for form, cell in sorted(IDLE_CELLS.items()):
            with self.subTest(idle=form):
                base_path = self.one_arm(
                    "M4/m4/clean",
                    [cell],
                    {"bulk_sink_bytes": 40000},
                    f"idle-absent-{form.replace('=', '-')}.json",
                )
                candidate = report([arm_with("M4/m4/clean", [cell], {})])
                code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
                self.assertEqual(code, 0, stdout + stderr)
                self.assertIn("bulk_sink_bytes 40000 -> not measured", stdout)
                self.assertIn("declare the lane idle", stdout)
                self.assertIn("reported, never compared", stdout)
                self.assertIn("coverage regression(s)=0", stdout)

    def test_an_idle_cell_whose_counter_fell_is_reported_not_failed(self):
        base_path = self.one_arm(
            "M4/m4/clean", [IDLE_CELL], {"bulk_wire_bytes": 40000}, "idle-fell.json"
        )
        candidate = report(
            [arm_with("M4/m4/clean", [IDLE_CELL], {"bulk_wire_bytes": 20000})]
        )
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("bulk_wire_bytes 40000 -> 20000 (-50.0%)", stdout)
        self.assertIn("reported, never compared", stdout)

    def test_the_recorded_residue_pair_stays_reported_not_failed(self):
        # The false positive this rule had to preserve the fix for, on the same
        # recorded pair: the request-response arm's cell names `lane=dual` and
        # no bulk load, so the declaration is silent, the pair is a gap, and the
        # 59 % wobble in the residue is visible and green.
        base_path = self.one_arm(
            "M1/lone_tail", [LONE_TAIL_CELL], {"bulk_wire_bytes": 1920}, "residue.json"
        )
        candidate = report(
            [arm_with("M1/lone_tail", [LONE_TAIL_CELL], {"bulk_wire_bytes": 785})]
        )
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("bulk_wire_bytes 1920 -> 785 (-59.1%)", stdout)
        self.assertIn("reported, not a regression", stdout)
        self.assertIn("leave the lane unstated", stdout)
        self.assertIn("coverage regression(s)=0", stdout)
        self.assertIn("verdict: OK  exit=0", stdout)
        self.assertIn("M1/lone_tail bulk_wire_bytes = 1920", stdout)

    def test_the_same_recorded_pair_on_a_claiming_cell_would_fail(self):
        # The pair *is* a tooth once a cell claims the lane, which is what makes
        # the residue's silence the thing that rescues it.
        base_path = self.one_arm(
            "M1/lone_tail", [CLAIMING_CELLS["load=bulk"]], {"bulk_wire_bytes": 1920}, "residue-claimed.json"
        )
        candidate = report(
            [
                arm_with(
                    "M1/lone_tail",
                    [CLAIMING_CELLS["load=bulk"]],
                    {"bulk_wire_bytes": 785},
                )
            ]
        )
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
        self.assertIn("bulk_wire_bytes 1920 -> 785 (-59.1%)", stdout)
        self.assertNotIn("reported, not a regression", stdout)

    def test_a_silent_cell_above_its_floor_is_still_a_coverage_regression(self):
        # The other arm the same token blocks: `M1/clean`'s cell is silent too,
        # and its 8 MiB counter has to stay a tooth.
        base_path = self.one_arm(
            "M1/clean", [CLEAN_CELL], {"bulk_wire_bytes": 8482399}, "silent-above.json"
        )
        candidate = report(
            [arm_with("M1/clean", [CLEAN_CELL], {"bulk_wire_bytes": 4241199})]
        )
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
        self.assertIn("bulk_wire_bytes 8482399 -> 4241199 (-50.0%)", stdout)
        self.assertNotIn("reported, not a regression", stdout)
        # ... and its absence is still one, because the baseline is above the
        # floor the declaration left as the only thing that could decide.
        candidate = report([arm_with("M1/clean", [CLEAN_CELL], {})])
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
        self.assertIn("bulk_wire_bytes 8482399 -> not measured", stdout)

    def test_a_claiming_cell_at_and_below_the_floor_is_compared(self):
        # A floor is not consulted for a claimed counter: at exactly the floor
        # and one byte under it both keys are teeth, because the cell, not the
        # magnitude, decided.
        cell = CLAIMING_CELLS["load=bulk"]
        base_path = self.one_arm(
            "M1/clean",
            [cell],
            {"bulk_wire_bytes": 65536, "bulk_sink_bytes": 65535},
            "floor-edge.json",
        )
        candidate = report(
            [
                arm_with(
                    "M1/clean",
                    [cell],
                    {"bulk_wire_bytes": 32768, "bulk_sink_bytes": 32767},
                )
            ]
        )
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stdout + stderr)
        self.assertIn("bulk_wire_bytes 65536 -> 32768 (-50.0%)", stdout)
        self.assertIn("bulk_sink_bytes 65535 -> 32767 (-50.0%)", stdout)

    def test_the_claim_rule_is_derived_from_the_cells_and_ignores_shape(self):
        # `shape` names the interactive lane's load shape and `rate`/`burst` name
        # a rate regime, so neither is read as a bulk claim: the same
        # `shape=cadence` is claimed-or-idle by the load dimension alone.
        for form, cell in sorted(CLAIMING_CELLS.items()):
            self.assertEqual(
                MANDATE_COMPARE.cell_claim(cell, "bulk_wire_bytes")[0],
                MANDATE_COMPARE.CLAIMED,
                form,
            )
        for form, cell in sorted(IDLE_CELLS.items()):
            self.assertEqual(
                MANDATE_COMPARE.cell_claim(cell, "bulk_sink_bytes")[0],
                MANDATE_COMPARE.IDLE,
                form,
            )
        self.assertEqual(
            MANDATE_COMPARE.cell_claim(LONE_TAIL_CELL, "bulk_wire_bytes")[0],
            MANDATE_COMPARE.UNSTATED,
        )
        self.assertEqual(
            MANDATE_COMPARE.cell_claim(CLEAN_CELL, "bulk_wire_bytes")[0],
            MANDATE_COMPARE.UNSTATED,
        )
        # A rate regime is not a lane: the conformance and reorder rows carry
        # `rate=` with no bulk lane, and reading it as one would claim the lane.
        for cell in (
            "conformance-reorder@impairment=reorder+rate=rate-limit",
            "reorder-rate@impairment=reorder+rate=curve+metric=p99",
        ):
            self.assertEqual(
                MANDATE_COMPARE.cell_claim(cell, "bulk_wire_bytes")[0],
                MANDATE_COMPARE.UNSTATED,
                cell,
            )
        # `load`/`bulk` are about the bulk lane, so they never disclaim the
        # arm's own lane — and a cell that names no lane at all leaves that lane
        # unstated rather than claimed (the `hol` cells carry `rate`/`loss`).
        for key in ("sent", "received", "wire_bytes", "offered_bytes", "delivered_bytes"):
            for form, cell in CLAIMING_CELLS.items():
                expected = (
                    MANDATE_COMPARE.UNSTATED
                    if MANDATE_COMPARE.LANE_DIMENSION not in cell.split("@", 1)[1]
                    else MANDATE_COMPARE.CLAIMED
                )
                self.assertEqual(
                    MANDATE_COMPARE.cell_claim(cell, key)[0], expected, f"{form}/{key}"
                )
            for form, cell in IDLE_CELLS.items():
                expected = (
                    MANDATE_COMPARE.UNSTATED
                    if MANDATE_COMPARE.LANE_DIMENSION not in cell.split("@", 1)[1]
                    else MANDATE_COMPARE.CLAIMED
                )
                self.assertEqual(
                    MANDATE_COMPARE.cell_claim(cell, key)[0], expected, f"{form}/{key}"
                )
            self.assertEqual(MANDATE_COMPARE.COUNTER_LANES.get(key), None, key)

    def test_a_claiming_cell_outranks_a_silent_or_idle_one_on_the_same_arm(self):
        # An arm's cells combine by the declared precedence, so a claim any
        # declared cell makes keeps the tooth.
        arm_record = {"cells": [IDLE_CELL, LONE_TAIL_CELL, CLAIMING_CELLS["load=bulk"]]}
        claim, why, gap = MANDATE_COMPARE.arm_claim(arm_record, "bulk_wire_bytes")
        self.assertEqual(claim, MANDATE_COMPARE.CLAIMED)
        self.assertIn("load=bulk", why)
        self.assertFalse(gap)
        arm_record = {"cells": [IDLE_CELL, LONE_TAIL_CELL]}
        claim, _why, gap = MANDATE_COMPARE.arm_claim(arm_record, "bulk_wire_bytes")
        self.assertEqual(claim, MANDATE_COMPARE.UNSTATED, "silence outranks an idle cell")
        self.assertTrue(gap)
        arm_record = {"cells": [IDLE_CELL]}
        self.assertEqual(
            MANDATE_COMPARE.arm_claim(arm_record, "bulk_wire_bytes")[0], MANDATE_COMPARE.IDLE
        )

    def test_a_cell_stating_one_dimension_twice_is_unstated(self):
        self.assertEqual(
            MANDATE_COMPARE.cell_claim("M1@lane=dual+load=bulk+load=none", "bulk_wire_bytes")[0],
            MANDATE_COMPARE.UNSTATED,
        )

    def test_a_claim_on_one_load_dimension_wins_over_an_idle_on_the_other(self):
        # Two dimensions can speak, and the claim wins: a tooth is not dropped
        # by a second dimension saying the idle thing.
        self.assertEqual(
            MANDATE_COMPARE.cell_claim("hol@lane=dual+load=none+bulk=shared", "bulk_wire_bytes")[0],
            MANDATE_COMPARE.CLAIMED,
        )
        self.assertEqual(
            MANDATE_COMPARE.cell_claim("hol@lane=dual+load=bulk+bulk=none", "bulk_wire_bytes")[0],
            MANDATE_COMPARE.CLAIMED,
        )
        self.assertEqual(
            MANDATE_COMPARE.cell_claim("hol@lane=dual+load=none+bulk=none", "bulk_wire_bytes")[0],
            MANDATE_COMPARE.IDLE,
        )

    def test_the_claim_gaps_are_printed_and_written_to_the_diff(self):
        base_path = self.one_arm(
            "M1/lone_tail", [LONE_TAIL_CELL], {"bulk_wire_bytes": 1920}, "gap-write.json"
        )
        candidate = report(
            [arm_with("M1/lone_tail", [LONE_TAIL_CELL], {"bulk_wire_bytes": 785})]
        )
        out = self.root / "gaps.json"
        code, stdout, stderr = self.run_tool(
            candidate, "--json-out", str(out), baseline=base_path
        )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("gaps: 1 unstated arm x counter pair(s)", stdout)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([gap["key"] for gap in diff["claim_gaps"]], ["bulk_wire_bytes"])
        self.assertEqual(diff["claim_gaps"][0]["arm"], "M1/lone_tail")
        self.assertEqual(diff["claim_gaps"][0]["baseline"], 1920)
        self.assertEqual(diff["claim_gaps"][0]["decided_by"], "floor")
        self.assertEqual(diff["claim_rule"]["counter_lanes"]["bulk_wire_bytes"], "bulk")

    def test_a_gap_above_its_floor_is_recorded_as_magnitude_decided(self):
        # The gap is recorded whatever the magnitude: the *declaration* decided
        # nothing here either, it is the count that stands above the floor.
        base_path = self.one_arm(
            "M1/clean", [CLEAN_CELL], {"bulk_wire_bytes": 8482399}, "gap-above.json"
        )
        out = self.root / "gap-above-diff.json"
        code, stdout, stderr = self.run_tool(
            report([arm_with("M1/clean", [CLEAN_CELL], {"bulk_wire_bytes": 8482399})]),
            "--json-out",
            str(out),
            baseline=base_path,
        )
        self.assertEqual(code, 0, stdout + stderr)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(diff["claim_gaps"][0]["decided_by"], "tolerance")
        self.assertIn("above its floor: compared by magnitude, not by the declaration", stdout)

    def test_a_gap_on_a_key_with_no_floor_is_recorded_as_compared_anyway(self):
        # A cell that names no lane at all leaves its own-lane counters
        # unstated too. Nothing hangs on it — the key has no floor, so the pair
        # is compared exactly as a claimed one — and the gap says so instead of
        # implying a weakness that is not there.
        base_path = self.one_arm("probe/forwarding", [PROBE_CELL], {}, "gap-no-floor.json")
        candidate = report([arm_with("probe/forwarding", [PROBE_CELL], {})])
        out = self.root / "gap-no-floor-diff.json"
        code, stdout, stderr = self.run_tool(
            candidate, "--json-out", str(out), baseline=base_path
        )
        self.assertEqual(code, 0, stdout + stderr)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            [gap["key"] for gap in diff["claim_gaps"]],
            ["sent", "received", "wire_bytes"],
        )
        self.assertEqual(
            {gap["decided_by"] for gap in diff["claim_gaps"]}, {"no-floor"}
        )
        self.assertIn("the cell names no lane dimension", stdout)
        self.assertIn("compared exactly as a claimed counter", stdout)

    def test_the_floors_are_printed_and_written_to_the_diff(self):
        out = self.root / "floors.json"
        code, stdout, stderr = self.run_tool(baseline_report(), "--json-out", str(out))
        self.assertEqual(code, 0, stderr)
        self.assertIn("floors: bulk_sink_bytes 65536, bulk_wire_bytes 65536", stdout)
        self.assertIn("applied only where the arm's cells leave the lane unstated", stdout)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            diff["count_floors"],
            {"bulk_wire_bytes": 65536, "bulk_sink_bytes": 65536},
        )

    def test_a_counter_without_a_floor_is_compared_at_the_count_tolerance(self):
        # Only the two bulk-lane byte counters carry a floor; every other
        # counter keeps the plain 50 % criterion however small it is.
        self.assertEqual(MANDATE_COMPARE.floor_bytes("received"), 0)
        self.assertEqual(MANDATE_COMPARE.floor_bytes("wire_bytes"), 0)
        self.assertGreater(MANDATE_COMPARE.floor_bytes("bulk_wire_bytes"), 0)

    def test_every_floored_key_is_a_bulk_lane_byte_counter(self):
        # A floor is a byte quantity, and it is only ever consulted for a lane
        # the cells can claim or disclaim: the bulk lane.
        for key in MANDATE_COMPARE.COUNT_FLOORS_BYTES:
            self.assertTrue(key.endswith("_bytes"), key)
            self.assertIn(key, MANDATE_COMPARE.COVERAGE_COUNTER_KEYS)
            self.assertEqual(MANDATE_COMPARE.COUNTER_LANES.get(key), "bulk", key)
        for key, lane in MANDATE_COMPARE.COUNTER_LANES.items():
            self.assertIn(key, MANDATE_COMPARE.COVERAGE_COUNTER_KEYS, key)
            self.assertNotEqual(lane, None, key)

    def test_a_counter_that_stopped_being_measured_is_a_coverage_regression(self):
        candidate = baseline_report()
        del candidate["arms"][0]["counters"]["wire_bytes"]
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("wire_bytes 126000 -> not measured", stdout)

    def test_a_statistic_that_stopped_being_measured_is_a_coverage_regression(self):
        candidate = baseline_report()
        del candidate["arms"][0]["stats"]["p999"]
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("p999 98.0 -> not measured", stdout)

    def test_a_fallen_delivery_is_a_coverage_regression(self):
        candidate = baseline_report()
        candidate["arms"][0]["stats"]["delivery"] = 0.98
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("delivery 1.0 -> 0.98 (-0.020)", stdout)
        self.assertIn("past the 0.005 delivery tolerance", stdout)

    def test_a_cell_no_arm_covers_any_more_is_a_coverage_regression(self):
        candidate = baseline_report()
        for entry in candidate["arms"]:
            entry["cells"] = ["M2@impairment=clean+metric=own-wire"]
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("no longer covered", stdout)
        self.assertIn(M1_CELL, stdout)
        self.assertIn("is exercised by no arm now", stdout)

    def test_a_missing_mandate_is_a_coverage_regression(self):
        candidate = baseline_report()
        del candidate["mandates"]["M3"]
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("the mandate M3 is in the baseline and not in the candidate", stdout)

    def test_a_missing_sample_count_is_a_coverage_regression(self):
        candidate = baseline_report()
        candidate["arms"][0]["sample_count"] = None
        code, stdout, stderr = self.run_tool(candidate)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("sample_count 2400 -> not measured", stdout)

    # -- every refusal: exit 2, and naming the problem -----------------------

    def test_a_quick_mismatch_is_refused_rather_than_compared(self):
        self.reject(
            report(baseline_report()["arms"], quick=True),
            "record the candidate the same way as the baseline",
        )

    def test_a_baseline_that_predates_the_per_arm_record_is_refused(self):
        old = report(baseline_report()["arms"], schema="mandate-check/2")
        old["arms"] = []
        path = self.root / "old-baseline.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        self.reject(baseline_report(), "predates the per-arm record", baseline=path)

    def test_a_report_with_no_arms_is_refused(self):
        empty = report([], schema="mandate-check/3")
        path = self.root / "empty-baseline.json"
        path.write_text(json.dumps(empty), encoding="utf-8")
        self.reject(baseline_report(), "carries no arm records", baseline=path)

    def test_an_unknown_schema_is_refused(self):
        other = report(baseline_report()["arms"], schema="something/1")
        path = self.root / "other-baseline.json"
        path.write_text(json.dumps(other), encoding="utf-8")
        self.reject(baseline_report(), "not a 'mandate-check/<version>'", baseline=path)

    def test_a_repeated_arm_id_is_refused(self):
        duplicated = baseline_report()
        duplicated["arms"].append(arm("M1/clean"))
        self.reject(duplicated, "recorded twice")

    def test_a_malformed_declared_cell_is_refused(self):
        broken = baseline_report()
        broken["arms"][0]["cells"] = ["M1"]
        self.reject(broken, "not <property>@<dimension>=<value>")

    def test_a_missing_report_is_refused(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE_COMPARE.main(
                [str(self.root / "absent.json"), "--baseline", str(self.root / "also-absent.json")]
            )
        self.assertEqual(code, 2)
        self.assertIn("does not exist", stderr.getvalue())

    def test_a_negative_tolerance_is_refused(self):
        self.reject(baseline_report(), "must not be negative", "--count-tolerance", "-1")

    def test_the_default_baseline_is_the_tool_s_own_file(self):
        self.assertEqual(
            MANDATE_COMPARE.MODULE_DIR / MANDATE_COMPARE.DEFAULT_BASELINE_NAME,
            MODULE_PATH.parent / "mandate-baseline.json",
        )

    def test_the_committed_baseline_is_comparable_with_itself(self):
        # The real committed baseline must satisfy the comparison's own
        # preconditions; a baseline this command refuses would make the
        # instrument unusable the day it lands.
        committed = MODULE_PATH.parent / MANDATE_COMPARE.DEFAULT_BASELINE_NAME
        if not committed.is_file():
            self.skipTest("no committed baseline yet")
        payload = json.loads(committed.read_text(encoding="utf-8"))
        schema = payload.get("schema")
        self.assertIsNotNone(MANDATE_COMPARE.SCHEMA_RE.match(schema or ""), schema)
        version = int(MANDATE_COMPARE.SCHEMA_RE.match(schema).group("version"))
        if version < MANDATE_COMPARE.MINIMUM_SCHEMA:
            self.skipTest(
                f"the committed baseline is schema {schema}, which predates the "
                "per-arm record; tools/mandate-check must re-record it"
            )
        self.assertTrue(payload.get("arms"), "the committed baseline carries no arms")
        code, stdout, stderr = self.run_tool(payload, baseline=committed)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("verdict: OK  exit=0", stdout)


if __name__ == "__main__":
    unittest.main()
