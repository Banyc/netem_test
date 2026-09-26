#!/usr/bin/env python3

"""Exercise `tools/check-gate.py`'s perf-test dual-mandate checks.

The gate checker resolves a design row's `<target>::<test>` from the compiled
test binaries, so these tests run it against a fixture crate root and a
stand-in `cargo` that prints a JSON plan's test lists. No Rust build, no
network: every case writes one `tests/GATE.md`, one fake cargo and one report,
runs the checker as a subprocess and asserts the exit status and the named
diagnostic.

Each failure mode the checker claims to catch has a case here, because a
checker that cannot fail is worse than none: an unknown test, a test in the
wrong tier, an over-budget tier sum, an empty coverage cell, a gap without a
reason, and a measured/declared drift. The well-formed case must pass.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CHECK_GATE = TOOLS / "check-gate.py"
PYTHON = sys.executable

# A stand-in cargo: it reads a JSON plan of the test names each
# `(package, target, ignored)` invocation must report and prints them in the
# `--list` shape. An invocation the plan does not name prints nothing, which
# is how the fixtures express an unknown target.
FAKE_CARGO = '''#!/usr/bin/env python3
"""A stand-in cargo that prints a plan's `--list` output."""

import json
import os
import pathlib
import sys


def main():
    plan = json.loads(pathlib.Path(os.environ["FAKE_CARGO_PLAN"]).read_text())
    argv = sys.argv[1:]
    package = ""
    target = ""
    for index, item in enumerate(argv):
        if item == "-p" and index + 1 < len(argv):
            package = argv[index + 1]
        elif item == "--test" and index + 1 < len(argv):
            target = argv[index + 1]
        elif item == "--lib":
            target = "lib"
    ignored = "--ignored" in argv
    lists = plan.get("lists", {})
    entry = lists.get(f"{package}|{target}", {})
    for name in entry.get("ignored" if ignored else "default", []):
        print(f"{name}: test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

ALPHA_RS = """#[test]
fn t_ok() {
    assert!(true);
}

#[test]
#[ignore = "report-only"]
fn t_ig() {
    println!("report only");
}
"""

BETA_RS = """#[test]
fn t_beta() {
    assert!(true);
}
"""

GATE_TEMPLATE = """# the fixture gate

```gate-manifest
{manifest}
```

```gate-default-required
{required}
```

```gate-asserting
{asserting}
```

```gate-perf-guard-helpers
```

```gate-perf-design
{design}
```

```gate-budgets
{budgets}
```

```gate-coverage-gaps
{gaps}
```
"""

# The default plan: `alpha` has a default test and one `#[ignore]`d perf-tier
# scenario, `beta` one default test, and the reserved `lib` target one ignored
# report-only probe.
BASE_PLAN = {
    "lists": {
        "tests|alpha": {"default": ["t_ok"], "ignored": ["t_ig"]},
        "tests|beta": {"default": ["t_beta"], "ignored": []},
        "tests|lib": {"default": [], "ignored": ["tests::probe"]},
    }
}

BASE_DESIGN = "\n".join(
    [
        "alpha::t_ok = default | 0.1 | conformance-alpha@lane=loopback+layer=link",
        "alpha::t_ig = perf | 0.2 | probe-alpha@metric=throughput+scale=200-pkt",
        "lib::tests::probe = perf | 0.3 | probe-runner@metric=latency+layer=runner",
        "beta::t_beta = default | 0.4 | conformance-beta@impairment=none",
    ]
)

BASE_BUDGETS = "\n".join(
    [
        "default = 30",
        "perf = 30",
        "baseline = beta::t_beta",
        "drift = 0.5",
        "drift_floor_s = 2.0",
    ]
)

BASE_GAPS = "M1@lane=dual-lane = owned by rtp_mux, whose declaration is pending"


class CheckGatePerfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name) / "fixture"
        (self.root / "tests" / "tests").mkdir(parents=True)
        (self.root / "tests" / "tests" / "alpha.rs").write_text(ALPHA_RS, encoding="utf-8")
        (self.root / "tests" / "tests" / "beta.rs").write_text(BETA_RS, encoding="utf-8")
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir()
        cargo = self.bin_dir / "cargo"
        cargo.write_text(FAKE_CARGO, encoding="utf-8")
        cargo.chmod(0o755)
        self.plan_path = Path(self._tmp.name) / "plan.json"
        self.report_path = Path(self._tmp.name) / "mandate-check.json"
        self.write_plan(BASE_PLAN)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def write_plan(self, plan):
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def write_gate(
        self,
        *,
        design=BASE_DESIGN,
        budgets=BASE_BUDGETS,
        gaps=BASE_GAPS,
        manifest="alpha::t_ig = perf",
        required="alpha::t_ok",
        asserting="alpha::t_ok",
    ):
        text = GATE_TEMPLATE.format(
            manifest=manifest,
            required=required,
            asserting=asserting,
            design="" if design is None else design,
            budgets=budgets,
            gaps=gaps,
        )
        if design is None:
            text = text.replace("```gate-perf-design\n\n```\n\n", "")
        (self.root / "tests" / "GATE.md").write_text(text, encoding="utf-8")

    def write_report(self, timings, *, schema="mandate-check/2"):
        self.report_path.write_text(
            json.dumps({"schema": schema, "timings": {"tests": timings}}),
            encoding="utf-8",
        )

    def check(self, *extra):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_CARGO_PLAN"] = str(self.plan_path)
        proc = subprocess.run(
            [
                PYTHON,
                str(CHECK_GATE),
                "--crate",
                str(self.root),
                "tests",
                "tests/tests",
                "tests/GATE.md",
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
        )
        return proc.returncode, proc.stdout + proc.stderr

    def rejects(self, fragment, *extra):
        code, output = self.check(*extra)
        self.assertNotEqual(code, 0, f"expected a non-zero exit; output={output}")
        self.assertIn(fragment, output)
        return output

    # -- the well-formed declaration ---------------------------------------

    def test_well_formed_declaration_passes(self):
        self.write_gate()
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-perf-design: 4 perf test row(s)", output)
        self.assertIn("4 coverage cell(s)", output)
        self.assertIn("gate-budgets: default 0.50/30.00s", output)
        self.assertIn("gate-budgets: perf 0.50/30.00s", output)

    def test_declaration_without_perf_blocks_still_passes(self):
        (self.root / "tests" / "GATE.md").write_text(
            "# no perf blocks\n\n```gate-manifest\nalpha::t_ig = perf\n```\n\n"
            "```gate-default-required\nalpha::t_ok\n```\n\n"
            "```gate-asserting\nalpha::t_ok\n```\n\n"
            "```gate-perf-guard-helpers\n```\n",
            encoding="utf-8",
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("PENDING (advisory, not a failure)", output)

    # -- the six failure modes ---------------------------------------------

    def test_unknown_test_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("beta::t_beta = default", "beta::t_missing = default")
        )
        self.rejects("does not report this test")

    def test_unknown_target_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("beta::t_beta = default", "gamma::t_beta = default")
        )
        self.rejects("does not report this test")

    def test_test_in_the_wrong_tier_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default |", "alpha::t_ok = perf |"
            )
        )
        self.rejects("declares tier 'perf' but the test set puts it in 'default'")

    def test_wrong_tier_for_an_ignored_scenario_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("alpha::t_ig = perf |", "alpha::t_ig = standard |")
        )
        self.rejects("declares tier 'standard' but the test set puts it in 'perf'")

    def test_over_budget_tier_sum_fails(self):
        self.write_gate(
            budgets=BASE_BUDGETS.replace("default = 30", "default = 0.2")
        )
        output = self.rejects("over its 0.20s budget")
        self.assertIn("declares 0.50s in the default tier", output)

    def test_missing_tier_budget_fails(self):
        self.write_gate(budgets="default = 30\nbaseline = beta::t_beta")
        self.rejects("gate-budgets declares no budget for it")

    def test_empty_coverage_cell_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("conformance-beta@impairment=none", "")
        )
        self.rejects("no coverage cell")

    def test_malformed_coverage_cell_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "conformance-beta@impairment=none", "conformance-beta@impairment="
            )
        )
        self.rejects("is malformed")

    def test_gap_without_a_reason_fails(self):
        self.write_gate(gaps="M1@lane=dual-lane =")
        self.rejects("records no reason")

    def test_malformed_gap_cell_fails(self):
        self.write_gate(gaps="just-a-word = covered nowhere")
        self.rejects("is malformed")

    def test_measured_declared_drift_fails(self):
        self.write_gate()
        self.write_report(
            [{"target": "beta", "name": "t_beta", "duration_seconds": 30.0, "state": "ok"}]
        )
        output = self.rejects(
            "measured/declared drift for beta::t_beta", "--mandate-check-json",
            str(self.report_path),
        )
        self.assertIn("declared 0.40s, measured 30.00s", output)

    # -- the report-side behaviour -----------------------------------------

    def test_a_row_measured_over_its_tier_budget_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("beta::t_beta = default | 0.4", "beta::t_beta = default | 25")
        )
        self.write_report(
            [{"target": "beta", "name": "t_beta", "duration_seconds": 31.0, "state": "ok"}]
        )
        output = self.rejects(
            "over its default tier budget",
            "--mandate-check-json",
            str(self.report_path),
        )
        self.assertIn("measured 31.00s", output)

    def test_drift_inside_the_tolerance_passes(self):
        self.write_gate()
        self.write_report(
            [{"target": "beta", "name": "t_beta", "duration_seconds": 0.5, "state": "ok"}]
        )
        code, output = self.check("--mandate-check-json", str(self.report_path))
        self.assertEqual(code, 0, output)
        self.assertIn("drift compared 1 of 4 declared row(s)", output)

    def test_a_stale_report_is_noted_not_failed(self):
        self.write_gate()
        self.write_report(
            [{"target": "beta", "name": "t_beta", "duration_seconds": 30.0, "state": "ok"}]
        )
        stale = (self.root / "tests" / "GATE.md").stat().st_mtime - 3600
        os.utime(self.report_path, (stale, stale))
        code, output = self.check("--mandate-check-json", str(self.report_path))
        self.assertEqual(code, 0, output)
        self.assertIn("predates", output)

    def test_a_report_without_per_test_timings_is_noted_not_failed(self):
        self.write_gate()
        self.report_path.write_text(
            json.dumps({"schema": "mandate-check/1"}), encoding="utf-8"
        )
        code, output = self.check("--mandate-check-json", str(self.report_path))
        self.assertEqual(code, 0, output)
        self.assertIn("carries no per-test timings", output)

    def test_a_missing_report_named_explicitly_fails(self):
        self.write_gate()
        self.rejects(
            "does not exist", "--mandate-check-json", str(self.report_path)
        )

    def test_a_report_with_an_unknown_schema_fails(self):
        self.write_gate()
        self.write_report([], schema="something-else/1")
        self.rejects("not a mandate-check report", "--mandate-check-json", str(self.report_path))

    def test_a_report_that_is_not_an_object_fails(self):
        self.write_gate()
        self.report_path.write_text("[1, 2, 3]", encoding="utf-8")
        self.rejects("is not a JSON object", "--mandate-check-json", str(self.report_path))

    # -- the rest of the declaration ---------------------------------------

    def test_budgets_without_a_design_block_fails(self):
        self.write_gate(design=None)
        self.rejects("gate-perf-design is missing while")

    def test_baseline_naming_no_row_fails(self):
        self.write_gate(
            budgets=BASE_BUDGETS.replace("baseline = beta::t_beta", "baseline = beta::t_nope")
        )
        self.rejects("baseline 'beta::t_nope' is not a gate-perf-design row")

    def test_a_budget_block_without_a_baseline_fails(self):
        self.write_gate(
            budgets="\n".join(line for line in BASE_BUDGETS.splitlines() if "baseline" not in line)
        )
        self.rejects("declares no 'baseline = <row>' line")

    def test_a_malformed_budget_line_fails(self):
        self.write_gate(budgets=BASE_BUDGETS + "\nnonsense = fast")
        self.rejects("is neither a tier nor one of baseline/drift/drift_floor_s")


if __name__ == "__main__":
    unittest.main()
