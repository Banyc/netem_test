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
        print(line)
    for line in plan.get("stderr") or []:
        print(line, file=sys.stderr)
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

PASS_LINES = [
    "running 3 tests",
    "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0",
    "MANDATE M2 PASS delivery=1.000 amp=3.61 budget=6.0",
    "MANDATE M3 PASS goodput=0.52 floor=0.35 link_mib_s=8.0",
    "test result: ok. 3 passed; 0 failed",
]


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
        for mandate in ("M1", "M2", "M3"):
            self.assertIn(f"{mandate} PASS", stdout)
        self.assertIn("p99=31.5 ceiling=250.0 over250=0", stdout)
        self.assertIn("delivery=1.0 amp=3.61 budget=6.0", stdout)
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

    def test_verdict_block_names_every_plot_and_the_report(self):
        code, stdout, _ = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0)
        for mandate, panel in (
            ("M1", "latency"),
            ("M1", "cdf"),
            ("M2", "delivery"),
            ("M2", "wire"),
            ("M3", "goodput"),
        ):
            path = (self.out / "plots" / f"{mandate}-{panel}.svg").resolve()
            self.assertIn(f"plot: {path}", stdout)
        self.assertIn(
            f"report:  {(self.out / MANDATE_CHECK.REPORT_NAME).resolve()}", stdout
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
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.report()["rtp_mux"]["revision"], head)
        self.assertEqual(self.report()["rtp_mux"]["revision_source"], "git")

    # -- a measured mandate failure is not an evidence failure -------------

    def test_failing_mandate_exit_is_three_and_names_the_measured_values(self):
        plan = self.healthy_plan(
            stdout=[
                "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0",
                "MANDATE M2 FAIL delivery=0.998 amp=7.2 budget=6.0",
                "MANDATE M3 PASS goodput=0.52 floor=0.35 link_mib_s=8.0",
            ]
        )
        code, stdout, stderr = self.run_tool(plan)
        self.assertEqual(code, 3, stderr)
        self.assertIn("M2 FAIL  delivery=0.998 amp=7.2 budget=6.0", stdout)
        self.assertIn("exit=3", stdout)
        report = self.report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["mandates"]["M2"]["verdict"], "FAIL")
        self.assertEqual(report["mandates"]["M2"]["values"]["amp"], 7.2)
        self.assertEqual(len(report["mandates"]["M2"]["plots"]), 2)

    # -- every rejection: non-zero, and naming the problem -----------------

    def test_missing_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=[line for line in PASS_LINES if not line.startswith("MANDATE M3")]
        )
        code, stdout, _ = self.reject(plan, "no 'MANDATE M3")
        self.assertEqual(code, MANDATE_CHECK.EXIT_EVIDENCE_FAILURE)
        self.assertIn("M3", stdout)
        self.assertIn("never measured", stdout)
        self.assertEqual(self.report()["mandates"]["M3"]["declared"], False)

    def test_no_mandate_lines_at_all_is_refused(self):
        plan = self.healthy_plan(stdout=["running 3 tests", "test result: ok. 3 passed"])
        code, stdout, _ = self.reject(plan, "no 'MANDATE M1")
        self.assertEqual(code, 2)
        for mandate in ("M1", "M2", "M3"):
            self.assertIn(f"{mandate}: the smoke set printed no", stdout)

    def test_malformed_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["MANDATE M1 MAYBE p99=1.0", "MANDATE M9 PASS x=1"]
        )
        code, stdout, _ = self.reject(plan, "does not match the contract grammar")
        self.assertEqual(code, 2)
        self.assertIn("not one of M1, M2, M3", stdout)

    def test_mandate_line_without_a_measurement_is_refused(self):
        plan = self.healthy_plan(
            stdout=[
                "MANDATE M1 PASS",
                "MANDATE M2 PASS delivery=1.000",
                "MANDATE M3 PASS goodput=0.52",
            ]
        )
        self.reject(plan, "without a single key=value measurement")

    def test_duplicate_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=["MANDATE M1 PASS p99=1.0", "MANDATE M1 PASS p99=2.0"]
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
            "noise: MANDATE-ish text is ignored\n"
        )
        self.assertEqual(problems, [])
        self.assertEqual(records["M1"]["verdict"], "FAIL")
        self.assertEqual(
            records["M1"]["values"], {"p99": 478.1, "ceiling": 250.0, "over250": 46}
        )
        self.assertEqual(records["M3"]["values"]["note"], "clean-lane")

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
