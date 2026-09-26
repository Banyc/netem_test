#!/usr/bin/env python3

import io
import json
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("mandate_check.py")
SPEC = importlib.util.spec_from_file_location("mandate_check", MODULE_PATH)
MANDATE_CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANDATE_CHECK)

WORKSPACE = Path(__file__).resolve().parents[1]

# A stand-in cargo. Everything it does comes from a JSON plan, so the tool can
# be exercised end to end without a Rust build: it records the argv, cwd and
# the two contract environment variables it was handed, optionally sleeps (to
# reach the timeout path), prints the plan's stdout/stderr, writes each
# mandate's declaration and data CSV into $MANDATE_CHECK_DIR, and exits with
# the plan's status.
FAKE_CARGO = '''#!/usr/bin/env python3
"""A stand-in cargo that produces the smoke set's evidence from a JSON plan."""

import json
import os
import pathlib
import sys
import time

NEWLINE = chr(10)


def main():
    plan_path = os.environ.get("FAKE_CARGO_PLAN")
    if not plan_path:
        print("fake cargo: FAKE_CARGO_PLAN is not set", file=sys.stderr)
        return 97
    plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    out = os.environ.get("MANDATE_CHECK_DIR")
    if out is None:
        print("fake cargo: MANDATE_CHECK_DIR is not set", file=sys.stderr)
        return 98
    pathlib.Path(os.environ["FAKE_CARGO_RECORD"]).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "mandate_check_dir": out,
                "quick": os.environ.get("MANDATE_SMOKE_QUICK"),
            }
        ),
        encoding="utf-8",
    )
    if plan.get("sleep"):
        time.sleep(plan["sleep"])
    for line in plan.get("stdout") or []:
        # Flush per line: the real smoke set streams its output, and the
        # per-test timing is an observation of that stream.
        print(line, flush=True)
        if plan.get("line_sleep"):
            time.sleep(plan["line_sleep"])
    for line in plan.get("stderr") or []:
        print(line, file=sys.stderr, flush=True)
    for mandate, spec in (plan.get("mandates") or {}).items():
        if spec.get("json") is not None:
            pathlib.Path(out, mandate + ".json").write_text(
                json.dumps(spec["json"]), encoding="utf-8"
            )
        csv = spec.get("csv")
        if csv is not None:
            if isinstance(csv, str):
                text = csv
            else:
                text = NEWLINE.join(",".join(str(cell) for cell in row) for row in csv)
                if text:
                    text += NEWLINE
            pathlib.Path(out, mandate + ".csv").write_text(text, encoding="utf-8")
    return plan.get("exit", 0)


if __name__ == "__main__":
    raise SystemExit(main())
'''

M1_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "RTT (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "impaired", "role": "impaired"}],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        },
        {
            "id": "cdf",
            "chart": "cdf",
            "series": [{"name": "impaired"}],
            "bounds": [],
        },
    ],
}

M1_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "impaired", 0.0, 12.5],
    ["latency", "impaired", 1.0, 31.5],
    ["latency", "impaired", 2.0, 88.25],
    ["cdf", "impaired", 12.5, 33.3],
    ["cdf", "impaired", 31.5, 66.7],
    ["cdf", "impaired", 88.25, 100.0],
]

M2_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and wire",
    "x_label": "seed",
    "y_label": "value",
    "panels": [
        {
            "id": "delivery",
            "chart": "line",
            "series": [{"name": "interactive"}],
            "bounds": [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        },
        {
            "id": "wire",
            "chart": "bar",
            "series": [{"name": "interactive"}],
            "bounds": [{"y": 6.0, "label": "M2 wire budget 6x"}],
        },
    ],
}

M2_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "interactive", 11.0, 1.0],
    ["delivery", "interactive", 21.0, 1.0],
    ["delivery", "interactive", 31.0, 1.0],
    ["wire", "interactive", 11.0, 3.4],
    ["wire", "interactive", 21.0, 3.61],
    ["wire", "interactive", 31.0, 3.8],
]

M3_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput",
    "x_label": "seed",
    "y_label": "goodput (MiB/s)",
    "panels": [
        {
            "id": "goodput",
            "chart": "bar",
            "series": [{"name": "bulk"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

M3_ROWS = [
    ["panel", "series", "x", "y"],
    ["goodput", "bulk", 11.0, 0.52],
    ["goodput", "bulk", 21.0, 0.61],
    ["goodput", "bulk", 31.0, 0.47],
]

# M4 is the interactive lane's split across several flows. The fixture mirrors
# the real declaration's four panels and its clean/hostile arms, at four flows.
M4_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.25, "label": "fair share 25.0%"}],
        },
        {
            "id": "imbalance",
            "chart": "bar",
            "y_label": "departure from the fair share",
            "x_label": "flow (1..4)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.01, "label": "fair-share bound 1.0%"}],
        },
        {
            "id": "delivery",
            "chart": "bar",
            "y_label": "delivery (received / offered)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}],
        },
        {
            "id": "latency",
            "chart": "bar",
            "y_label": "latency (ms)",
            "series": [
                {"name": "clean_p50"},
                {"name": "clean_p99"},
                {"name": "hostile_p50"},
                {"name": "hostile_p99"},
            ],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        },
    ],
}


def _m4_rows():
    rows = [["panel", "series", "x", "y"]]
    per_arm = {
        "clean": {
            "shares": [0.251, 0.249, 0.250, 0.250],
            "imbalance": [0.004, -0.004, 0.002, -0.002],
            "delivery": [1.0, 1.0, 1.0, 1.0],
        },
        "hostile": {
            "shares": [0.248, 0.252, 0.251, 0.249],
            "imbalance": [-0.008, 0.008, 0.004, -0.004],
            "delivery": [0.998, 0.999, 0.998, 0.999],
        },
    }
    latency = {
        "clean_p50": [22.0, 23.5, 21.8, 24.1],
        "clean_p99": [118.4, 121.0, 116.7, 119.9],
        "hostile_p50": [96.0, 102.5, 88.4, 110.2],
        "hostile_p99": [402.0, 388.7, 431.5, 399.2],
    }
    for arm, panel_values in per_arm.items():
        for panel, values in panel_values.items():
            for flow, value in enumerate(values, start=1):
                rows.append([panel, arm, flow, value])
    for series, values in latency.items():
        for flow, value in enumerate(values, start=1):
            rows.append(["latency", series, flow, value])
    return rows


M4_ROWS = _m4_rows()

PASS_LINES = [
    "running 4 tests",
    # The smoke set's per-arm lines, in the producer's own padded shape, before
    # that arm's mandate's MANDATE line — the ordering the runner attributes by.
    "[mandate-smoke clean    ] sent=  800 recv=  800 delivery=1.000 p50=   25.3 "
    "p90=   43.0 p99=   89.0 p999=   97.9 max=   102.5 over250=   0 wire=     42300B "
    "x=2.16 bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke hostile  ] sent=  800 recv=  800 delivery=1.000 p50=   39.5 "
    "p90=  150.6 p99=  245.1 p999=  283.6 max=   289.9 over250=  21 wire=     42300B "
    "x=2.16 bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke lone_tail] sent=  240 recv=  240 delivery=1.000 p50=    0.3 "
    "p90=   69.6 p99=  174.7 p999=  681.9 max=  2693.3 over250=   4 wire=    120000B "
    "x=5.40 bulk_sink=         0B bulk_wire=         0B wall=15.3s window=15s",
    "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0",
    "test m1_interactive_tail_latency ... ok",
    "[mandate-smoke clean    ] sent=  800 recv=  800 delivery=1.000 p50=   25.3 "
    "p90=   43.0 p99=   89.0 p999=   97.9 max=   102.5 over250=   0 wire=     42300B "
    "x=2.16 bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke hostile  ] sent=  800 recv=  800 delivery=1.000 p50=   39.5 "
    "p90=  150.6 p99=  245.1 p999=  283.6 max=   289.9 over250=  21 wire=     42300B "
    "x=2.16 bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke lone_tail] sent=  240 recv=  240 delivery=1.000 p50=    0.3 "
    "p90=   69.6 p99=  174.7 p999=  681.9 max=  2693.3 over250=   4 wire=    120000B "
    "x=5.40 bulk_sink=         0B bulk_wire=         0B wall=15.3s window=15s",
    "MANDATE M2 PASS delivery=1.000 amp=3.61 budget=6.0",
    "test m2_interactive_delivery_and_wire ... ok",
    "[mandate-smoke m3/rep1] delivered 0.963 MiB/s over 2.0004s, shaper forwarded "
    "0.972 MiB/s, capacity 1.000 MiB/s, fraction 0.963 (820148 / 992240 bytes)",
    "[mandate-smoke m3/rep2] delivered 0.971 MiB/s over 2.0011s, shaper forwarded "
    "0.980 MiB/s, capacity 1.000 MiB/s, fraction 0.971 (826960 / 994352 bytes)",
    "[mandate-smoke m3/rep3] delivered 0.958 MiB/s over 2.0008s, shaper forwarded "
    "0.967 MiB/s, capacity 1.000 MiB/s, fraction 0.958 (815872 / 990128 bytes)",
    "MANDATE M3 PASS goodput=0.52 floor=0.35 link_mib_s=8.0",
    "test m3_bulk_goodput_fraction ... ok",
    "[mandate-smoke m4/clean flow A] sent=  120 recv=  120 delivery=1.000 "
    "share=0.2502 offered=1269600B delivered=1269600B p50=   22.0 p90=   41.0 "
    "p99=  118.4 max=  240.0",
    "[mandate-smoke m4/clean flow B] sent=  120 recv=  120 delivery=1.000 "
    "share=0.2498 offered=1269600B delivered=1269600B p50=   23.5 p90=   42.0 "
    "p99=  121.0 max=  244.0",
    "[mandate-smoke m4/clean flow C] sent=  120 recv=  120 delivery=1.000 "
    "share=0.2501 offered=1269600B delivered=1269600B p50=   21.8 p90=   40.0 "
    "p99=  116.7 max=  238.0",
    "[mandate-smoke m4/clean flow D] sent=  120 recv=  120 delivery=1.000 "
    "share=0.2499 offered=1269600B delivered=1269600B p50=   24.1 p90=   43.0 "
    "p99=  119.9 max=  246.0",
    "[mandate-smoke m4/clean  ] ideal_share=0.2500 min_share=0.2498 "
    "max_share=+0.2502 imbalance=0.0016 window=12s wall=24.3s",
    "[mandate-smoke m4/hostile flow A] sent=  120 recv=  120 delivery=0.998 "
    "share=0.2480 offered=1269600B delivered=1267000B p50=   96.0 p90=  180.0 "
    "p99=  402.0 max=  900.0",
    "[mandate-smoke m4/hostile] ideal_share=0.2500 min_share=0.2480 "
    "max_share=+0.2520 imbalance=0.0080 window=12s wall=24.1s",
    "MANDATE M4 PASS flows=4 clean_delivery_min=1.000 hostile_delivery_min=0.998 "
    "clean_imbalance=0.004 hostile_imbalance=0.008 imbalance_bound=0.010 "
    "fair_share=0.2500 delivery_floor=0.995 clean_p99_max=121.0 ceiling=250.0",
    "test m4_interactive_lane_fairness ... ok",
    "test result: ok. 4 passed; 0 failed",
]


def arm_lines(lines):
    """Just the `[mandate-smoke ...]` lines of a plan's stdout, in order."""
    return [line for line in lines if line.startswith("[mandate-smoke ")]


class MandateCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)
        self.crate = self.root / "rtp_mux"
        (self.crate / "tests").mkdir(parents=True)
        (self.crate / "Cargo.toml").write_text(
            '[package]\nname = "rtp_mux"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (self.crate / "tests" / "mandate_smoke.rs").write_text(
            "// the mandate smoke set\n", encoding="utf-8"
        )
        self.cargo = self.root / "fake-cargo"
        self.cargo.write_text(FAKE_CARGO, encoding="utf-8")
        self.cargo.chmod(0o755)
        self.out = self.root / "run"
        self.record = self.root / "cargo-record.json"
        self.plan_path = self.root / "plan.json"

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def healthy_plan(self, **overrides):
        plan = {
            "exit": 0,
            "stdout": list(PASS_LINES),
            "stderr": [],
            "mandates": {
                "M1": {"json": M1_DECLARATION, "csv": M1_ROWS},
                "M2": {"json": M2_DECLARATION, "csv": M2_ROWS},
                "M3": {"json": M3_DECLARATION, "csv": M3_ROWS},
                "M4": {"json": M4_DECLARATION, "csv": M4_ROWS},
            },
        }
        plan.update(overrides)
        return plan

    def run_tool(self, plan, *extra, no_rasterize=True, browser=None, extra_env=None):
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        env = {
            "FAKE_CARGO_PLAN": str(self.plan_path),
            "FAKE_CARGO_RECORD": str(self.record),
        }
        env.update(extra_env or {})
        arguments = [
            "--cargo",
            str(self.cargo),
            "--rtp-mux",
            str(self.crate),
            "--dir",
            str(self.out),
        ]
        if no_rasterize:
            arguments.append("--no-rasterize")
        if browser is not None:
            arguments += ["--browser", browser]
        arguments += list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(stdout), redirect_stderr(
            stderr
        ):
            code = MANDATE_CHECK.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self):
        return json.loads((self.out / MANDATE_CHECK.REPORT_NAME).read_text(encoding="utf-8"))

    def cargo_record(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def reject(self, plan, fragment, *extra, **kwargs):
        """Assert one run is refused non-zero, naming the problem."""
        code, stdout, stderr = self.run_tool(plan, *extra, **kwargs)
        self.assertNotEqual(code, 0, f"expected a non-zero exit; stdout={stdout}")
        self.assertIn(fragment, stdout + stderr)
        return code, stdout, stderr

    # -- the success path --------------------------------------------------

    def test_healthy_run_passes_and_writes_machine_checkable_evidence(self):
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        for mandate in ("M1", "M2", "M3", "M4"):
            self.assertIn(f"{mandate} PASS", stdout)
        self.assertIn("p99=31.5 ceiling=250.0 over250=0", stdout)
        self.assertIn("delivery=1.0 amp=3.61 budget=6.0", stdout)
        self.assertIn("flows=4 clean_delivery_min=1.0", stdout)
        report = self.report()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["schema"], MANDATE_CHECK.REPORT_SCHEMA)
        self.assertGreater(report["duration_seconds"], 0)
        self.assertEqual(report["smoke"]["exit_code"], 0)
        self.assertFalse(report["smoke"]["timed_out"])
        self.assertEqual(
            report["command"],
            [
                str(self.cargo),
                "test",
                "--release",
                "-p",
                "rtp_mux",
                "--test",
                "mandate_smoke",
                "--",
                "--nocapture",
            ],
        )
        record = self.cargo_record()
        self.assertEqual(
            record["argv"],
            [
                "test",
                "--release",
                "-p",
                "rtp_mux",
                "--test",
                "mandate_smoke",
                "--",
                "--nocapture",
            ],
        )
        self.assertEqual(Path(record["cwd"]).resolve(), self.crate.resolve())
        self.assertEqual(record["mandate_check_dir"], report["out_dir"])
        self.assertIsNone(record["quick"])

    def test_report_records_parsed_values_and_plot_paths(self):
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        m1 = report["mandates"]["M1"]
        self.assertEqual(m1["verdict"], "PASS")
        self.assertEqual(m1["values"], {"p99": 31.5, "ceiling": 250.0, "over250": 0})
        self.assertEqual(m1["raw_line"], "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0")
        self.assertEqual(m1["panels"], 2)
        self.assertEqual(len(m1["plots"]), 2)
        self.assertEqual(m1["series_counts"], [1, 1])
        for path in m1["plots"]:
            self.assertTrue(Path(path).is_file(), path)
            self.assertGreater(Path(path).stat().st_size, 0)
        self.assertEqual(
            Path(m1["plots"][0]).parent.resolve(), (self.out / "plots").resolve()
        )
        m3 = report["mandates"]["M3"]
        self.assertEqual(m3["values"], {"goodput": 0.52, "floor": 0.35, "link_mib_s": 8.0})
        self.assertEqual(len(m3["plots"]), 1)
        self.assertEqual(report["mandates"]["M2"]["values"]["delivery"], 1.0)
        m4 = report["mandates"]["M4"]
        self.assertEqual(m4["verdict"], "PASS")
        self.assertEqual(m4["values"]["flows"], 4)
        self.assertEqual(m4["values"]["clean_p99_max"], 121.0)
        self.assertEqual(m4["panels"], 4)
        self.assertEqual(len(m4["plots"]), 4)
        self.assertEqual(m4["series_counts"], [8, 8, 8, 16])

    def test_verdict_block_names_every_plot_and_the_report(self):
        code, stdout, _ = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0)
        for mandate, panel in (
            ("M1", "latency"),
            ("M1", "cdf"),
            ("M2", "delivery"),
            ("M2", "wire"),
            ("M3", "goodput"),
            ("M4", "shares"),
            ("M4", "imbalance"),
            ("M4", "delivery"),
            ("M4", "latency"),
        ):
            path = (self.out / "plots" / f"{mandate}-{panel}.svg").resolve()
            self.assertIn(f"plot: {path}", stdout)
        self.assertIn(
            f"report:  {(self.out / MANDATE_CHECK.REPORT_NAME).resolve()}", stdout
        )

    def test_report_records_per_test_and_per_mandate_timings(self):
        plan = self.healthy_plan()
        plan["line_sleep"] = 0.05
        code, stdout, stderr = self.run_tool(plan)
        self.assertEqual(code, 0, stderr)
        report = self.report()
        timings = report["timings"]
        self.assertIn("streamed-line-arrival", timings["method"])
        self.assertIn("gap before the test started", timings["method"])
        self.assertEqual(timings["origin"], "smoke-child-start")
        self.assertEqual(
            [entry["name"] for entry in timings["tests"]],
            [
                "m1_interactive_tail_latency",
                "m2_interactive_delivery_and_wire",
                "m3_bulk_goodput_fraction",
                "m4_interactive_lane_fairness",
            ],
        )
        for entry in timings["tests"]:
            self.assertEqual(entry["target"], "mandate_smoke")
            self.assertEqual(entry["state"], "ok")
            self.assertGreater(entry["duration_seconds"], 0)
        measured = {
            mandate: report["mandates"][mandate]["duration_seconds"]
            for mandate in ("M1", "M2", "M3", "M4")
        }
        self.assertEqual(set(measured), {"M1", "M2", "M3", "M4"})
        for duration in measured.values():
            self.assertGreater(duration, 0)
        self.assertIn("duration: ", stdout)
        self.assertIn("bracketed wall-clock", stdout)

    def test_derive_timings_brackets_results_and_mandates(self):
        events = [
            {"seconds": 1.0, "line": "running 3 tests"},
            {"seconds": 3.0, "line": "MANDATE M1 PASS p99=1.0"},
            {"seconds": 3.5, "line": "test m1_x ... ok"},
            {"seconds": 4.0, "line": "test m2_y has been running for over 60 seconds"},
            {"seconds": 9.5, "line": "test m2_y ... FAILED"},
            {"seconds": 10.0, "line": "test m3_z ... ignored, perf tier"},
            {"seconds": 12.0, "line": "MANDATE M2 FAIL delivery=0.0"},
        ]
        timings = MANDATE_CHECK.derive_timings(events, "mandate_smoke")
        self.assertEqual(
            [entry["name"] for entry in timings["tests"]], ["m1_x", "m2_y", "m3_z"]
        )
        first, second, third = timings["tests"]
        self.assertEqual((first["state"], first["duration_seconds"]), ("ok", 3.5))
        self.assertEqual(first["started_at_seconds"], 0.0)
        self.assertEqual((second["state"], second["duration_seconds"]), ("FAILED", 6.0))
        self.assertEqual((third["state"], third["duration_seconds"]), ("ignored", None))
        self.assertEqual(
            [(entry["mandate"], entry["duration_seconds"]) for entry in timings["mandates"]],
            [("M1", 3.0), ("M2", 9.0)],
        )

    def test_quick_asks_the_smoke_set_for_its_shortest_windows(self):
        code, _, stderr = self.run_tool(self.healthy_plan(), "--quick")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.cargo_record()["quick"], "1")
        self.assertTrue(self.report()["quick"])

    def test_an_inherited_quick_or_out_dir_cannot_reach_the_smoke_set(self):
        code, _, stderr = self.run_tool(
            self.healthy_plan(),
            extra_env={
                "MANDATE_SMOKE_QUICK": "1",
                "MANDATE_CHECK_DIR": str(self.root / "somewhere-else"),
            },
        )
        self.assertEqual(code, 0, stderr)
        record = self.cargo_record()
        self.assertIsNone(record["quick"])
        self.assertEqual(record["mandate_check_dir"], self.report()["out_dir"])

    def test_stale_evidence_from_an_earlier_run_is_cleared_first(self):
        (self.out / "plots").mkdir(parents=True)
        (self.out / "M1.json").write_text("{ not json", encoding="utf-8")
        (self.out / "M1.csv").write_text("panel,series,x,y\nstale,stale,0,1\n", encoding="utf-8")
        (self.out / "plots" / "M1-latency.svg").write_text("<svg/>", encoding="utf-8")
        (self.out / MANDATE_CHECK.REPORT_NAME).write_text("{}", encoding="utf-8")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.report()["mandates"]["M1"]["series_counts"], [1, 1])
        rendered = (self.out / "plots" / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertNotEqual(rendered, "<svg/>")
        self.assertNotIn("stale", (self.out / "M1.csv").read_text(encoding="utf-8"))

    def test_a_run_that_writes_no_report_leaves_no_earlier_runs_report(self):
        # `tools/mandate-compare` reads `<dir>/mandate-check.json`, so a report
        # left behind by an earlier run is compared as if it were this run's
        # measurement. A run that cannot write its own report must therefore
        # leave none at all: not the report, not the log that explains what the
        # smoke set printed, and not an evidence file.
        self.out.mkdir(parents=True)
        stale_report = self.out / MANDATE_CHECK.REPORT_NAME
        stale_report.write_text(
            json.dumps({"schema": "mandate-check/3", "ok": True, "verdict": "PASS"}),
            encoding="utf-8",
        )
        stale_log = self.out / MANDATE_CHECK.LOG_NAME
        stale_log.write_text("an earlier run's smoke-set output\n", encoding="utf-8")
        (self.out / "M1.csv").write_text(
            "panel,series,x,y\nstale,stale,0,1\n", encoding="utf-8"
        )
        # The run cannot do its job: the smoke set source is gone, so the
        # command fails before it writes anything.
        (self.crate / "tests" / "mandate_smoke.rs").unlink()
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, MANDATE_CHECK.EXIT_EVIDENCE_FAILURE)
        self.assertIn("does not exist", stderr)
        self.assertFalse(
            stale_report.exists(),
            "the earlier run's report survived a run that wrote none, so a "
            "later comparison would read it as this run's measurement",
        )
        self.assertFalse(
            stale_log.exists(),
            "the earlier run's log survived a run that wrote none, so it "
            "describes a different run than the one that just failed",
        )
        self.assertFalse((self.out / "M1.csv").exists())
        self.assertFalse((self.out / "plots").exists())

    def test_a_foreign_non_empty_dir_is_refused_rather_than_cleared(self):
        self.out.mkdir(parents=True)
        stranger = self.out / "notes.txt"
        stranger.write_text("someone else's file\n", encoding="utf-8")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 2)
        self.assertIn("is not this command's directory to clear", stderr)
        self.assertTrue(stranger.is_file())
        self.assertEqual(stranger.read_text(encoding="utf-8"), "someone else's file\n")

    def test_revision_is_resolved_from_git_when_jj_cannot(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        environment = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        run = subprocess.run(
            ["git", "init", "-q", str(self.crate)],
            capture_output=True,
            text=True,
            env=dict(os.environ, **environment),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        run = subprocess.run(
            ["git", "-C", str(self.crate), "commit", "-q", "--allow-empty", "-m", "smoke set"],
            capture_output=True,
            text=True,
            env=dict(os.environ, **environment),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        head = subprocess.run(
            ["git", "-C", str(self.crate), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "-C", str(self.crate), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.report()["rtp_mux"]["revision"], head)
        self.assertEqual(self.report()["rtp_mux"]["revision_source"], "git")
        self.assertEqual(self.report()["rtp_mux"]["tree_id"], head_tree)
        self.assertEqual(self.report()["rtp_mux"]["tree_id_source"], "git")
        self.assertIn(f"tree:     {head_tree} (git)", stdout)

    def test_an_unresolvable_tree_id_is_null_in_the_report_not_fabricated(self):
        # The commit id resolved; the tree did not. The field stays null and the
        # block says so, because a fabricated tree id would make a committed
        # baseline name content it never built.
        head = "b" * 40

        def capture(command, *, cwd):
            if "log" in command:
                return {"exit_code": 0, "stdout": head + "\n" + "c" * 32 + "\n"}
            return {"exit_code": 128, "stdout": ""}

        with mock.patch.object(MANDATE_CHECK, "_capture", capture):
            code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["rtp_mux"]["revision"], head)
        self.assertEqual(report["rtp_mux"]["revision_source"], "jj")
        self.assertIsNone(report["rtp_mux"]["tree_id"])
        self.assertIsNone(report["rtp_mux"]["tree_id_source"])
        self.assertIn("tree:     unresolved (no jj or git)", stdout)

    def test_tree_id_follows_the_tree_and_not_the_commit_id(self):
        # The commit id is not the content. An empty commit on top of a tree
        # changes the commit id and leaves the tree id alone; a content change
        # moves the tree id. This is the shape jj's `@` puts a baseline in —
        # an auto-snapshot jj rewrites, whose tree is what a build reads.
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        environment = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*arguments):
            run = subprocess.run(
                ["git", "-C", str(self.crate), *arguments],
                capture_output=True,
                text=True,
                env=dict(os.environ, **environment),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout.strip()

        git("init", "-q")
        (self.crate / "tracked.txt").write_text("first\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "-m", "first")
        first = git("rev-parse", "HEAD")
        first_tree = git("rev-parse", "HEAD^{tree}")
        self.assertEqual(
            MANDATE_CHECK.resolve_tree_id(self.crate, first, "git"), (first_tree, "git")
        )

        git("commit", "-q", "--allow-empty", "-m", "empty")
        empty = git("rev-parse", "HEAD")
        self.assertNotEqual(empty, first)
        self.assertEqual(
            MANDATE_CHECK.resolve_tree_id(self.crate, empty, "git"),
            (first_tree, "git"),
            "an empty commit changed the commit id and the tree id with it: the "
            "tree id is not describing the content",
        )

        (self.crate / "tracked.txt").write_text("second\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "-m", "second")
        second = git("rev-parse", "HEAD")
        second_tree = git("rev-parse", "HEAD^{tree}")
        self.assertNotEqual(second_tree, first_tree)
        self.assertEqual(
            MANDATE_CHECK.resolve_tree_id(self.crate, second, "git"),
            (second_tree, "git"),
            "the tree id did not move when the content did",
        )

    def test_tree_id_is_stable_across_jj_rewrites_of_the_working_copy(self):
        # jj rewrites `@` on every operation, so a commit id read from it is
        # throwaway: `jj new` produces a different commit id with the same
        # tree id, and only an edit moves the tree id. A baseline that records
        # the commit id alone therefore cannot name what it measured.
        if shutil.which("jj") is None:
            self.skipTest("jj is not installed")
        repo = self.root / "jj-crate"
        repo.mkdir()
        (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
        environment = {
            "JJ_EDITOR": "true",
            "JJ_USER": "tree id test",
            "JJ_EMAIL": "tree-id@example.invalid",
        }

        def jj(*arguments):
            run = subprocess.run(
                ["jj", *arguments],
                cwd=str(repo),
                capture_output=True,
                text=True,
                env=dict(os.environ, **environment),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout.strip()

        jj("git", "init")
        first = jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        first_tree = MANDATE_CHECK.resolve_tree_id(repo, first, "jj")
        self.assertEqual(first_tree[1], "jj")
        self.assertEqual(len(first_tree[0]), 40)

        # An empty commit on top: a new commit id, the same content.
        jj("new")
        second = jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        self.assertNotEqual(second, first)
        self.assertEqual(
            MANDATE_CHECK.resolve_tree_id(repo, second, "jj"),
            first_tree,
            "rewriting the working-copy commit moved the tree id: the tree id "
            "is not naming the content",
        )

        # An edit: the content moved, so the tree id must too.
        (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        third = jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        third_tree = MANDATE_CHECK.resolve_tree_id(repo, third, "jj")
        self.assertNotEqual(third_tree[0], first_tree[0])

    def test_an_unresolvable_tree_id_is_recorded_as_null_not_fabricated(self):
        # No answer from jj or git, and a tree jj reports as unresolved, both
        # leave the field null: a fabricated tree id would make a baseline
        # name content it never built.
        with mock.patch.object(
            MANDATE_CHECK, "_capture", lambda command, *, cwd: {"exit_code": 1, "stdout": ""}
        ):
            self.assertEqual(
                MANDATE_CHECK.resolve_tree_id(self.crate, "0" * 40, "jj"), (None, None)
            )
            self.assertEqual(
                MANDATE_CHECK.resolve_tree_id(self.crate, "0" * 40, "git"), (None, None)
            )
        with mock.patch.object(
            MANDATE_CHECK,
            "_capture",
            lambda command, *, cwd: {
                "exit_code": 0,
                "stdout": "    root_tree: Unresolved(Conflict),\n",
            },
        ):
            self.assertEqual(
                MANDATE_CHECK.resolve_tree_id(self.crate, "0" * 40, "jj"), (None, None)
            )
        self.assertEqual(MANDATE_CHECK.resolve_tree_id(self.crate, None, None), (None, None))

    def test_report_records_each_arm_measurement_schema_four(self):
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["schema"], "mandate-check/4")
        arms = {arm["id"]: arm for arm in report["arms"]}
        self.assertEqual(
            sorted(arms),
            [
                "M1/clean",
                "M1/hostile",
                "M1/lone_tail",
                "M2/clean",
                "M2/hostile",
                "M2/lone_tail",
                "M3/m3/rep1",
                "M3/m3/rep2",
                "M3/m3/rep3",
                "M4/m4/clean",
                "M4/m4/clean flow A",
                "M4/m4/clean flow B",
                "M4/m4/clean flow C",
                "M4/m4/clean flow D",
                "M4/m4/hostile",
                "M4/m4/hostile flow A",
            ],
        )
        clean = arms["M1/clean"]
        self.assertEqual(clean["mandate"], "M1")
        self.assertEqual(clean["label"], "clean")
        self.assertEqual(clean["dialect"], "kv")
        self.assertEqual(clean["sample_count"], 800)
        self.assertEqual(clean["stats"]["p99"], 89.0)
        self.assertEqual(clean["stats"]["over250"], 0)
        self.assertEqual(clean["counters"]["received"], 800)
        self.assertEqual(clean["counters"]["sent"], 800)
        self.assertEqual(clean["counters"]["wire_bytes"], 42300)
        self.assertEqual(clean["counters"]["bulk_wire_bytes"], 9123456)
        self.assertEqual(clean["windows"]["window_seconds"], 12)
        self.assertEqual(clean["values"]["x"], 2.16)
        self.assertEqual(
            clean["cells"],
            [
                "M1@impairment=loss2pct-iid+latency=25ms+jitter=5ms+lane=dual"
                "+shape=cadence+flows=1+scale=256B+metric=p99",
            ],
        )
        self.assertIn("sent=", clean["raw_line"])
        lone = arms["M1/lone_tail"]
        self.assertEqual(lone["counters"]["wire_bytes"], 120000)
        self.assertEqual(lone["stats"]["over250"], 4)
        self.assertEqual(lone["cells"][0].split("@")[1].split("+")[3], "shape=request-response")
        # The bulk-rep dialect: the M3 rep line carries no `recv`, so its
        # sample count is absent rather than invented, and its shaper counter is
        # the forwarded bytes.
        rep = arms["M3/m3/rep2"]
        self.assertEqual(rep["dialect"], "bulk-rep")
        self.assertIsNone(rep["sample_count"])
        self.assertEqual(rep["stats"]["fraction"], 0.971)
        self.assertEqual(rep["counters"]["delivered_bytes"], 826960)
        self.assertEqual(rep["counters"]["forwarded_bytes"], 994352)
        self.assertEqual(rep["windows"]["elapsed_seconds"], 2.0011)
        self.assertEqual(rep["cells"][0].split("@")[0], "M3")
        # The M4 flow arms inherit the arm family's cells by longest prefix.
        flow = arms["M4/m4/clean flow C"]
        self.assertEqual(flow["sample_count"], 120)
        self.assertEqual(flow["counters"]["offered_bytes"], 1269600)
        self.assertEqual(flow["counters"]["delivered_bytes"], 1269600)
        self.assertIn("flows=4", flow["cells"][0])
        aggregate = arms["M4/m4/clean"]
        self.assertIsNone(aggregate["sample_count"])
        self.assertEqual(aggregate["stats"]["imbalance"], 0.0016)
        self.assertEqual(aggregate["windows"]["wall_seconds"], 24.3)
        self.assertEqual(report["arm_notes"], [])
        self.assertEqual(
            report["arm_declaration"]["schema"],
            MANDATE_CHECK.ARMS_DECLARATION_SCHEMA,
        )
        self.assertGreater(report["arm_declaration"]["declared_cells"], 0)
        self.assertIn("arms: 16 measured", stdout)
        self.assertIn("M1 arms: 3 (clean, hostile, lone_tail), 1840 sample(s)", stdout)

    def test_a_prose_arm_line_is_kept_as_a_note_rather_than_dropped(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES)
            + ["[mandate-smoke m4/clean] noted 3 flows in flight, see the panel"]
        )
        code, _, stderr = self.run_tool(plan)
        # The note follows the last MANDATE line, so it is kept with no mandate
        # rather than attributed to one by guesswork.
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(
            report["arm_notes"],
            [
                {
                    "mandate": None,
                    "label": "m4/clean",
                    "body": "noted 3 flows in flight, see the panel",
                }
            ],
        )
        self.assertEqual(len(report["arms"]), 16)

    def test_an_arm_without_a_declared_cell_is_refused(self):
        plan = self.healthy_plan(
            stdout=[line.replace("clean    ", "brand_new", 1) for line in PASS_LINES]
        )
        code, stdout, _ = self.reject(plan, "covers no declared cell")
        self.assertEqual(code, 2)
        self.assertIn("'M1/brand_new'", stdout)

    def test_a_dangling_arm_line_is_refused_with_the_line_named(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["[mandate-smoke trailing] sent=1 recv=1 p99=1.0"]
        )
        code, stdout, _ = self.reject(plan, "cannot be attributed to a mandate")
        self.assertEqual(code, 2)
        self.assertIn("[mandate-smoke trailing]", stdout)

    def test_one_absent_arm_is_recorded_as_absent_rather_than_failing_the_run(self):
        plan = self.healthy_plan(
            stdout=[
                line
                for line in PASS_LINES
                if not line.startswith("[mandate-smoke clean    ]")
            ]
        )
        # The reader fails a run whose arm lines cannot be attributed or whose
        # mandate lost every arm; it does not invent an expected arm set, so a
        # single absent arm is reported by the comparison against the baseline
        # (an arm the baseline covered and the candidate did not) rather than
        # refusing the run here.
        code, _, stderr = self.run_tool(plan)
        self.assertEqual(code, 0, stderr)
        ids = [arm["id"] for arm in self.report()["arms"]]
        self.assertNotIn("M1/clean", ids)
        self.assertNotIn("M2/clean", ids)
        self.assertIn("M1/hostile", ids)

    def test_a_run_that_measured_no_arm_at_all_is_refused(self):
        plan = self.healthy_plan(
            stdout=[
                line for line in PASS_LINES if not line.startswith("[mandate-smoke ")
            ]
        )
        code, stdout, _ = self.reject(plan, "no '[mandate-smoke <arm>] ...' arm measurement")
        self.assertEqual(code, 2)
        for mandate in MANDATE_CHECK.MANDATE_IDS:
            self.assertIn(f"{mandate}: no '[mandate-smoke", stdout)

    def test_an_arm_line_before_a_missing_mandate_line_is_named(self):
        plan = self.healthy_plan(
            stdout=[
                line for line in PASS_LINES if line != "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0"
            ]
        )
        code, stdout, _ = self.reject(plan, "M1: no '[mandate-smoke")
        self.assertEqual(code, 2)

    def test_a_missing_arm_declaration_is_refused(self):
        with mock.patch.object(
            MANDATE_CHECK, "ARMS_DECLARATION_NAME", "mandate-arms-absent.json"
        ):
            code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 2)
        self.assertIn("arm coverage declaration", stderr)
        self.assertIn("does not exist", stderr)

    def test_a_malformed_arm_declaration_is_refused(self):
        malformed = {
            "not an object": "[]",
            "wrong schema": json.dumps(
                {"schema": "mandate-arms/2", "cells": {"M1/clean": ["M1@x=1"]}}
            ),
            "no cells": json.dumps({"schema": "mandate-arms/1", "cells": {}}),
            "empty cell list": json.dumps(
                {"schema": "mandate-arms/1", "cells": {"M1/clean": []}}
            ),
            "non-string cell": json.dumps(
                {"schema": "mandate-arms/1", "cells": {"M1/clean": [7]}}
            ),
        }
        for case, text in malformed.items():
            with self.subTest(case=case):
                path = self.root / "mandate-arms-bad.json"
                path.write_text(text, encoding="utf-8")
                problems = []
                self.assertIsNone(MANDATE_CHECK.load_arm_declaration(path, problems))
                self.assertTrue(problems, case)
        good = self.root / "mandate-arms-good.json"
        good.write_text(
            json.dumps({"schema": "mandate-arms/1", "cells": {"M1/clean": ["M1@x=1"]}}),
            encoding="utf-8",
        )
        problems = []
        self.assertIsNotNone(MANDATE_CHECK.load_arm_declaration(good, problems))
        self.assertEqual(problems, [])

    def test_grammar_normalises_units_stats_counters_and_windows(self):
        arm = MANDATE_CHECK.parse_arm_line(
            "[mandate-smoke m4/clean flow A] sent=  120 recv=  120 delivery=1.000 "
            "share=0.2502 offered=1269600B delivered=1269600B p50=   22.0 wall=24.3s"
        )
        self.assertEqual(arm["dialect"], "kv")
        self.assertEqual(arm["sample_count"], 120)
        self.assertEqual(arm["counters"]["offered_bytes"], 1269600)
        self.assertEqual(arm["counters"]["delivered_bytes"], 1269600)
        self.assertEqual(arm["stats"]["share"], 0.2502)
        self.assertEqual(arm["windows"]["wall_seconds"], 24.3)
        self.assertNotIn("wall", arm["counters"])
        rep = MANDATE_CHECK.parse_arm_line(
            "[mandate-smoke m3/rep1] delivered 0.963 MiB/s over 2.0004s, shaper "
            "forwarded 0.972 MiB/s, capacity 1.000 MiB/s, fraction 0.963 "
            "(820148 / 992240 bytes)"
        )
        self.assertEqual(rep["dialect"], "bulk-rep")
        self.assertEqual(rep["counters"]["forwarded_bytes"], 992240)
        self.assertEqual(rep["stats"]["delivered_mib_s"], 0.963)
        self.assertIsNone(
            MANDATE_CHECK.parse_arm_line("a line that is not an arm line at all")
        )
        note = MANDATE_CHECK.parse_arm_line("[mandate-smoke x] three flows in flight")
        self.assertTrue(note["note"])

    def test_declared_cells_match_the_longest_arm_prefix(self):
        cells = {"M1": ["mandate"], "M1/clean": ["arm"], "M1/cleanup": ["other"]}
        self.assertEqual(MANDATE_CHECK.declared_cells("M1/clean", cells), ["arm"])
        self.assertEqual(MANDATE_CHECK.declared_cells("M1/hostile", cells), ["mandate"])
        self.assertEqual(MANDATE_CHECK.declared_cells("M1/cleanup", cells), ["other"])
        self.assertEqual(MANDATE_CHECK.declared_cells("M1/clean/flow-A", cells), ["arm"])
        self.assertEqual(MANDATE_CHECK.declared_cells("M2/clean", cells), [])

    def test_parse_arm_lines_attributes_by_the_next_mandate_line(self):
        events = [
            {"seconds": 1.0, "line": "[mandate-smoke clean] sent=1 recv=1 p99=1.0"},
            {"seconds": 2.0, "line": "MANDATE M1 PASS p99=1.0"},
            {"seconds": 3.0, "line": "[mandate-smoke m3/rep1] delivered 1.0 MiB/s "
             "over 2.0s, shaper forwarded 1.0 MiB/s, capacity 1.0 MiB/s, fraction "
             "1.0 (1 / 2 bytes)"},
            {"seconds": 4.0, "line": "MANDATE M2 PASS delivery=1.0"},
        ]
        problems = []
        arms, notes = MANDATE_CHECK.parse_arm_lines(events, problems)
        self.assertEqual(problems, [])
        self.assertEqual([arm["id"] for arm in arms], ["M1/clean", "M2/m3/rep1"])
        self.assertEqual([arm["mandate"] for arm in arms], ["M1", "M2"])
        self.assertEqual(notes, [])
        guard = []
        MANDATE_CHECK.check_arm_coverage(arms, notes, guard)
        self.assertEqual(len(guard), 2)
        self.assertIn("M3: no '[mandate-smoke", guard[0])
        self.assertIn("M4: no '[mandate-smoke", guard[1])

    # -- a measured mandate failure is not an evidence failure -------------

    def test_failing_mandate_exit_is_three_and_names_the_measured_values(self):
        # The per-arm lines stay in the plan: the extended contract requires a
        # measured arm for every mandate, and this case is about a measured
        # FAIL being a verdict rather than an evidence failure.
        plan = self.healthy_plan(
            stdout=[
                line.replace(
                    "MANDATE M2 PASS delivery=1.000",
                    "MANDATE M2 FAIL delivery=0.998",
                )
                for line in PASS_LINES
            ]
        )
        code, stdout, stderr = self.run_tool(plan)
        self.assertEqual(code, 3, stderr)
        self.assertIn("M2 FAIL  delivery=0.998 amp=3.61 budget=6.0", stdout)
        self.assertIn("exit=3", stdout)
        report = self.report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["mandates"]["M2"]["verdict"], "FAIL")
        self.assertEqual(report["mandates"]["M2"]["values"]["amp"], 3.61)
        self.assertEqual(len(report["mandates"]["M2"]["plots"]), 2)

    # -- every rejection: non-zero, and naming the problem -----------------

    def test_missing_mandate_line_is_refused(self):
        # Extended from M1/M2/M3 to every id in MANDATE_IDS: a missing M4 line is
        # the failure mode the fourth id introduces, and it must be refused with
        # the same non-zero, evidence-incomplete treatment and name M4.
        for mandate in MANDATE_CHECK.MANDATE_IDS:
            with self.subTest(mandate=mandate):
                plan = self.healthy_plan(
                    stdout=[
                        line
                        for line in PASS_LINES
                        if not line.startswith(f"MANDATE {mandate} ")
                    ]
                )
                code, stdout, _ = self.reject(plan, f"no 'MANDATE {mandate}")
                self.assertEqual(code, MANDATE_CHECK.EXIT_EVIDENCE_FAILURE)
                self.assertIn(f"{mandate}: the smoke set printed no", stdout)
                self.assertIn("never measured", stdout)
                self.assertEqual(
                    self.report()["mandates"][mandate]["declared"], False
                )

    def test_no_mandate_lines_at_all_is_refused(self):
        plan = self.healthy_plan(stdout=["running 4 tests", "test result: ok. 4 passed"])
        code, stdout, _ = self.reject(plan, "no 'MANDATE M1")
        self.assertEqual(code, 2)
        for mandate in MANDATE_CHECK.MANDATE_IDS:
            self.assertIn(f"{mandate}: the smoke set printed no", stdout)

    def test_malformed_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["MANDATE M1 MAYBE p99=1.0", "MANDATE M9 PASS x=1"]
        )
        code, stdout, _ = self.reject(plan, "does not match the contract grammar")
        self.assertEqual(code, 2)
        self.assertIn("not one of M1, M2, M3, M4", stdout)

    def test_mandate_line_without_a_measurement_is_refused(self):
        plan = self.healthy_plan(
            stdout=[
                "MANDATE M1 PASS",
                "MANDATE M2 PASS delivery=1.000",
                "MANDATE M3 PASS goodput=0.52",
                "MANDATE M4 PASS flows=4",
            ]
        )
        self.reject(plan, "without a single key=value measurement")

    def test_duplicate_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["MANDATE M1 PASS p99=2.0"]
        )
        self.reject(plan, "M1 is declared twice")

    def test_missing_declaration_file_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M2"]["json"] = None
        code, stdout, _ = self.reject(plan, "M2.json was not written by the smoke set")
        self.assertEqual(code, 2)
        self.assertIn("MANDATE_CHECK_DIR", stdout)

    def test_missing_csv_file_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M3"]["csv"] = None
        self.reject(plan, "M3.csv was not written by the smoke set")

    def test_empty_csv_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = ""
        _, stdout, _ = self.reject(plan, "is empty")
        self.assertIn("M1: data CSV", stdout)

    def test_header_only_csv_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = [["panel", "series", "x", "y"]]
        self.reject(plan, "has no data rows")

    def test_a_declared_series_with_no_rows_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = [
            ["panel", "series", "x", "y"],
            ["latency", "impaired", 0.0, 12.5],
            ["latency", "impaired", 1.0, 31.5],
        ]
        self.reject(plan, "M1: the declaration and the data do not agree")
        self.assertEqual(self.report()["mandates"]["M1"]["plots"], [])

    def test_compile_failure_is_refused_and_the_log_is_kept(self):
        plan = self.healthy_plan(
            exit=101,
            stdout=[],
            stderr=["error[E0425]: cannot find value `nope` in this scope"],
            mandates={},
        )
        code, stdout, stderr = self.reject(plan, "the smoke set exited 101")
        self.assertEqual(code, 2)
        self.assertIn("cannot find value `nope`", stderr)
        self.assertIn("mandate-smoke.log", stdout)
        log = (self.out / MANDATE_CHECK.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("cannot find value `nope`", log)
        report = self.report()
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["smoke"]["exit_code"], 101)
        self.assertTrue(report["problems"])

    def test_timeout_is_refused(self):
        plan = self.healthy_plan(sleep=30)
        code, stdout, _ = self.reject(plan, "did not finish within 1s", "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertTrue(self.report()["smoke"]["timed_out"])

    def test_missing_crate_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE_CHECK.main(["--rtp-mux", str(empty), "--dir", str(self.root / "run2")])
        self.assertEqual(code, 2)
        self.assertIn("has no Cargo.toml", stderr.getvalue())

    def test_missing_smoke_source_is_refused(self):
        (self.crate / "tests" / "mandate_smoke.rs").unlink()
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 2)
        self.assertIn("mandate_smoke", stderr)
        self.assertIn("does not exist", stderr)

    def test_missing_cargo_is_refused(self):
        # The previous run's report is seeded first: a run refused after the
        # run directory was prepared must not leave it behind for a later
        # comparison to read as this run's measurement.
        self.out.mkdir(parents=True)
        stale_report = self.out / MANDATE_CHECK.REPORT_NAME
        stale_report.write_text(
            json.dumps({"schema": "mandate-check/3", "ok": True}), encoding="utf-8"
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE_CHECK.main(
                [
                    "--cargo",
                    str(self.root / "no-such-cargo"),
                    "--rtp-mux",
                    str(self.crate),
                    "--dir",
                    str(self.out),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("was not found on PATH", stderr.getvalue())
        self.assertFalse(stale_report.exists())

    def test_a_plot_that_cannot_be_produced_is_refused(self):
        code, stdout, stderr = self.run_tool(
            self.healthy_plan(),
            no_rasterize=False,
            browser=str(self.root / "no-such-browser"),
        )
        self.assertEqual(code, 2, stdout)
        self.assertIn("no headless browser", stdout + stderr)
        self.assertIn("M1: ", stdout)

    # -- grammar unit coverage --------------------------------------------

    def test_grammar_accepts_the_documented_shape(self):
        records, problems = MANDATE_CHECK.parse_mandate_lines(
            "MANDATE M1 FAIL p99=478.1 ceiling=250.0 over250=46\n"
            "MANDATE M2 PASS delivery=1.000\n"
            "MANDATE M3 PASS goodput=0.52 floor=0.35 note=clean-lane\n"
            "MANDATE M4 PASS flows=4 clean_imbalance=0.004 fair_share=0.2500\n"
            "noise: MANDATE-ish text is ignored\n"
        )
        self.assertEqual(problems, [])
        self.assertEqual(records["M1"]["verdict"], "FAIL")
        self.assertEqual(
            records["M1"]["values"], {"p99": 478.1, "ceiling": 250.0, "over250": 46}
        )
        self.assertEqual(records["M3"]["values"]["note"], "clean-lane")
        self.assertEqual(
            records["M4"]["values"],
            {"flows": 4, "clean_imbalance": 0.004, "fair_share": 0.25},
        )

    def test_grammar_rejects_repeated_keys_and_unparsable_tokens(self):
        _, problems = MANDATE_CHECK.parse_mandate_lines(
            "MANDATE M1 PASS p99=1.0 p99=2.0\nMANDATE M2 PASS p99\n"
        )
        self.assertEqual(len(problems), 3)
        self.assertIn("'p99' is repeated", problems[0])
        self.assertIn("is not a <key>=<value> token", problems[1])
        self.assertIn("without a single key=value measurement", problems[2])

    def test_default_rtp_mux_is_the_sibling_checkout(self):
        self.assertEqual(
            MANDATE_CHECK.default_crate_path(), (WORKSPACE.parent / "rtp_mux").resolve()
        )

    def test_default_out_dir_is_beneath_tmpdir(self):
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            created = MANDATE_CHECK.default_out_dir()
        self.assertEqual(created.parent.resolve(), self.root.resolve())
        self.assertTrue(created.is_dir())


if __name__ == "__main__":
    unittest.main()
