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

    # -- the measured noise band: below it a count is reported, not failed -

    def test_a_residue_bulk_counter_fall_is_reported_and_stays_green(self):
        # The real false positive: a bulk-lane counter the arm's cell does not
        # claim (the request-response arms declare no bulk workload) sat at
        # 1920 B and read 785 B on the next run of the unchanged tree. Both
        # values are inside the band, so the 59 % fall is visible and green.
        base = baseline_report()
        base["arms"][0]["counters"]["bulk_wire_bytes"] = 1920
        base_path = self.write_baseline(base)
        candidate = baseline_report()
        candidate["arms"][0]["counters"]["bulk_wire_bytes"] = 785
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("bulk_wire_bytes 1920 -> 785 (-59.1%)", stdout)
        self.assertIn("reported, not a regression", stdout)
        self.assertIn("coverage regression(s)=0", stdout)
        self.assertIn("verdict: OK  exit=0", stdout)

    def test_an_absent_residue_bulk_counter_is_reported_and_stays_green(self):
        base = baseline_report()
        base["arms"][0]["counters"]["bulk_sink_bytes"] = 0
        base_path = self.write_baseline(base)
        candidate = baseline_report()
        candidate["arms"][0]["counters"].pop("bulk_sink_bytes", None)
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("bulk_sink_bytes 0 -> not measured", stdout)
        self.assertIn("absent, not a regression", stdout)

    def test_a_load_bearing_bulk_counter_fall_is_still_a_coverage_regression(self):
        # The same key on an arm that does drive the bulk lane stays a tooth:
        # 8 MiB is far above the band, so halving it is coverage loss.
        base = baseline_report()
        base["arms"][0]["counters"]["bulk_wire_bytes"] = 8482399
        base_path = self.write_baseline(base)
        candidate = baseline_report()
        candidate["arms"][0]["counters"]["bulk_wire_bytes"] = 4241199
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("bulk_wire_bytes 8482399 -> 4241199 (-50.0%)", stdout)
        self.assertNotIn("reported, not a regression", stdout)

    def test_a_load_bearing_bulk_counter_absent_is_still_a_coverage_regression(self):
        base = baseline_report()
        base["arms"][0]["counters"]["bulk_wire_bytes"] = 8482399
        base_path = self.write_baseline(base)
        candidate = baseline_report()
        candidate["arms"][0]["counters"].pop("bulk_wire_bytes", None)
        code, stdout, stderr = self.run_tool(candidate, baseline=base_path)
        self.assertEqual(code, MANDATE_COMPARE.EXIT_COVERAGE_REGRESSION, stderr)
        self.assertIn("bulk_wire_bytes 8482399 -> not measured", stdout)
        self.assertNotIn("reported, not a regression", stdout)

    def test_the_noise_bands_are_printed_and_written_to_the_diff(self):
        out = self.root / "bands.json"
        code, stdout, stderr = self.run_tool(baseline_report(), "--json-out", str(out))
        self.assertEqual(code, 0, stderr)
        self.assertIn("bands: bulk_sink_bytes 65536, bulk_wire_bytes 65536", stdout)
        diff = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            diff["count_noise_bands"],
            {"bulk_wire_bytes": 65536, "bulk_sink_bytes": 65536},
        )

    def test_a_counter_without_a_band_is_compared_at_the_count_tolerance(self):
        # Only the two bulk-lane byte counters carry a band; every other
        # counter keeps the plain 50 % criterion however small it is.
        self.assertEqual(MANDATE_COMPARE.noise_band_bytes("received"), 0)
        self.assertEqual(MANDATE_COMPARE.noise_band_bytes("wire_bytes"), 0)
        self.assertGreater(MANDATE_COMPARE.noise_band_bytes("bulk_wire_bytes"), 0)

    def test_every_banded_key_is_a_byte_counter(self):
        # The band is a byte floor and the diagnostic says so, so a key whose
        # unit is not bytes may not be listed.
        for key in MANDATE_COMPARE.COUNT_NOISE_BANDS_BYTES:
            self.assertTrue(key.endswith("_bytes"), key)
            self.assertIn(key, MANDATE_COMPARE.COVERAGE_COUNTER_KEYS)

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
