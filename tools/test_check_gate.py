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
        "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
        "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
        "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
        "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | probe-runner@impairment=none",
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
        self.assertIn(
            "gate-perf-relations: 1 orthogonal, 1 composite, 1 re-measurement, "
            "1 baseline of 4 row(s), stated against beta::t_beta",
            output,
        )
        self.assertIn(
            "gate-perf-composite: alpha::t_ig varies metric, scale", output
        )

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

    # -- the relation to the baseline (the coverage half's attribution) ----

    def test_an_unlabelled_multi_dimension_row_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
                "alpha::t_ok = default | 0.1 | conformance-alpha@lane=loopback+layer=link",
            )
        )
        output = self.rejects("alpha::t_ok: it declares no relation to the baseline")
        self.assertIn("its cells vary 2 dimension(s), so write `composite(lane,layer)`", output)

    def test_a_row_differing_in_no_dimension_without_a_re_measurement_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat)",
                "lib::tests::probe = perf | 0.3 | orthogonal",
            )
        )
        output = self.rejects(
            "lib::tests::probe: its cells name no dimension that differs from the baseline"
        )
        self.assertIn("write `re-measurement(<reason>)`", output)

    def test_an_unlabelled_row_differing_in_no_dimension_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | probe-runner@impairment=none",
                "lib::tests::probe = perf | 0.3 | probe-runner@impairment=none",
            )
        )
        output = self.rejects(
            "lib::tests::probe: it declares no relation to the baseline"
        )
        self.assertIn("its cells vary 0 dimension(s), so write `re-measurement(<reason>)`", output)

    def test_an_ambiguous_row_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "probe-alpha@metric=throughput+scale=200-pkt",
                "probe-alpha@metric=throughput+metric=latency",
            )
        )
        output = self.rejects(
            "alpha::t_ig: its relation to the baseline cannot be determined"
        )
        self.assertIn("states 'metric' as both 'throughput' and 'latency'", output)
        self.assertIn("the dimensions it varies are ambiguous", output)

    def test_a_baseline_that_is_not_one_point_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "conformance-beta@impairment=none",
                "conformance-beta@impairment=none+impairment=delay20ms",
            )
        )
        output = self.rejects(
            "baseline row beta::t_beta: its cells are not one point"
        )
        self.assertIn("no row's relation to it can be determined", output)

    def test_a_composite_label_that_misnames_the_dimensions_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "composite(metric,scale)", "composite(metric,lane)"
            )
        )
        self.rejects(
            "alpha::t_ig: it is labelled composite(metric,lane), but its cells vary metric, scale"
        )

    def test_a_composite_label_on_a_one_dimension_row_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | composite(lane,layer)",
            )
        )
        output = self.rejects(
            "alpha::t_ok: it varies exactly one dimension from the baseline (impairment)"
        )
        self.assertIn("write `orthogonal`, not `composite`", output)

    def test_a_re_measurement_label_on_a_multi_dimension_row_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ig = perf | 0.2 | composite(metric,scale)",
                "alpha::t_ig = perf | 0.2 | re-measurement(second-tier-repeat)",
            )
        )
        self.rejects(
            "alpha::t_ig: it is labelled a re-measurement, but its cells vary 2 "
            "dimension(s) from the baseline (metric, scale)"
        )

    def test_the_baseline_row_must_be_labelled_baseline(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "beta::t_beta = default | 0.4 | baseline |",
                "beta::t_beta = default | 0.4 | orthogonal |",
            )
        )
        output = self.rejects(
            "beta::t_beta: it is the gate-budgets baseline, so its relation is the reference"
        )
        self.assertIn("write `baseline`", output)

    def test_a_non_baseline_row_may_not_be_labelled_baseline(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | baseline",
            )
        )
        output = self.rejects(
            "alpha::t_ok: it is labelled `baseline`, but the baseline is beta::t_beta"
        )
        self.assertIn("so write `orthogonal`", output)

    def test_a_re_measurement_without_a_reason_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "re-measurement(second-tier-repeat)", "re-measurement()"
            )
        )
        self.rejects("re-measurement(<reason>)` names no reason")

    def test_a_composite_relation_naming_one_dimension_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace("composite(metric,scale)", "composite(metric)")
        )
        self.rejects("`composite(...)` must name at least two dimensions")

    def test_an_unknown_relation_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | mostly-orthogonal",
            )
        )
        self.rejects("is not a relation this grammar knows")

    def test_a_row_without_a_relation_field_reports_it_as_a_field_count(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none | extra",
            )
        )
        self.rejects("found 5 field(s)")

    def test_a_row_covering_two_cells_of_one_dimension_is_orthogonal(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "orthogonal | conformance-alpha@impairment=delay20ms",
                "orthogonal | conformance-alpha@impairment=delay20ms,conformance-alpha@impairment=delay25ms",
            )
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("1 orthogonal", output)

    def test_a_row_whose_cells_vary_different_dimensions_is_composite(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "orthogonal | conformance-alpha@impairment=delay20ms",
                "composite(impairment,scale) | conformance-alpha@impairment=delay20ms,conformance-alpha@scale=128-pkt",
            )
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("alpha::t_ok varies impairment, scale", output)

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

    # -- several named baselines (one per measurement family) --------------

    def test_a_row_naming_an_undeclared_baseline_family_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | orthogonal@nope",
            )
        )
        output = self.rejects(
            "alpha::t_ok: it names the baseline family 'nope', which gate-budgets "
            "does not declare"
        )
        self.assertIn("declare `baseline.nope = <row>`", output)

    def test_a_named_baseline_naming_no_row_fails(self):
        self.write_gate(
            budgets=BASE_BUDGETS + "\nbaseline.ghost = alpha::t_missing"
        )
        self.rejects(
            "baseline.ghost names 'alpha::t_missing', which is not a "
            "gate-perf-design row"
        )

    def test_a_declared_baseline_no_row_uses_fails(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | baseline@widow | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | probe-runner@impairment=none",
                ]
            ),
            budgets=BASE_BUDGETS + "\nbaseline.widow = alpha::t_ig",
        )
        self.rejects(
            "baseline.widow = alpha::t_ig is declared but no row states a "
            "relation against it"
        )

    def test_a_row_labelled_the_wrong_familys_baseline_fails(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@fam-a | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | baseline@fam-b | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | orthogonal@fam-a | probe-runner@metric=throughput",
                ]
            ),
            budgets=BASE_BUDGETS
            + "\nbaseline.fam-a = alpha::t_ig\nbaseline.fam-b = alpha::t_ok",
        )
        output = self.rejects(
            "alpha::t_ok: it is labelled `baseline@fam-a`, but baseline.fam-a "
            "is alpha::t_ig"
        )
        self.assertIn("a baseline label names only the family", output)

    def test_a_named_familys_reference_must_carry_its_family_label(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
                "alpha::t_ok = default | 0.1 | baseline | conformance-alpha@impairment=delay20ms",
            ),
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        output = self.rejects(
            "alpha::t_ok: it is the reference row of baseline.delay "
            "(alpha::t_ok)"
        )
        self.assertIn("write `baseline@delay`", output)

    def test_a_row_that_is_two_families_reference_row_fails(self):
        self.write_gate(
            design=BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | baseline@fam-a",
            ),
            budgets=BASE_BUDGETS
            + "\nbaseline.fam-a = alpha::t_ok\nbaseline.fam-b = alpha::t_ok",
        )
        self.rejects("alpha::t_ok' is the reference row of baseline.fam-a, baseline.fam-b")

    def test_a_named_family_is_derived_against_its_own_baseline(self):
        """The member is orthogonal to its family's reference, not the default.

        Against the default (``impairment=none``) the member would vary two
        dimensions (``metric``, ``scale``); against its own family's reference
        (``impairment=delay20ms``) it varies exactly one. The family's two rows
        share the ``conformance-alpha`` cell name, which is what
        ``members.delay`` declares as the family's namespace.
        """
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | orthogonal@delay | conformance-alpha@impairment=none",
                ]
            ),
            budgets=BASE_BUDGETS
            + "\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance-alpha",
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "gate-perf-relations: 1 orthogonal, 1 composite, 0 re-measurement, "
            "2 baseline of 4 row(s) across 2 baseline(s)",
            output,
        )
        self.assertIn(
            "gate-perf-family: delay(alpha::t_ok) 1 orthogonal, 0 composite, "
            "0 re-measurement, 1 baseline",
            output,
        )
        self.assertIn(
            "gate-perf-composite: alpha::t_ig varies metric, scale against "
            "beta::t_beta",
            output,
        )
        self.assertIn(
            "gate-perf-namespace: delay(conformance-alpha) 2 row(s), cells: "
            "conformance-alpha",
            output,
        )
        self.assertIn(
            "gate-perf-membership: 1 cell-name namespace(s) declared, 1 cell "
            "name(s) claimed, 0 row(s) stated outside the namespace of the "
            "family they name",
            output,
        )

    def test_a_member_of_a_named_family_is_refused_the_wrong_label(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | oriented@delay | probe-runner@impairment=none",
                ]
            ),
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        self.rejects("lib::tests::probe: relation 'oriented@delay' is not a relation")

    def test_a_composite_label_mismatching_its_own_family_baseline_fails(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | composite(metric,lane)@delay | probe-runner@impairment=other+transport=std-udp",
                ]
            ),
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        output = self.rejects("lib::tests::probe: it is labelled composite(metric,lane)@delay")
        self.assertIn("its cells vary impairment, transport", output)

    def test_a_one_dimension_member_of_a_named_family_must_be_orthogonal(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | composite(metric,lane)@delay | probe-runner@impairment=none",
                ]
            ),
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        output = self.rejects(
            "lib::tests::probe: it varies exactly one dimension from baseline "
            "'delay' (impairment)"
        )
        self.assertIn("so write `orthogonal@delay`, not `composite`", output)

    # -- family membership: the cells decide the family ---------------------

    # One named family whose two rows share the `conformance-alpha` cell name,
    # so the family has a namespace and the declaration is well formed.
    MEMBER_DESIGN = "\n".join(
        [
            "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
            "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
            "alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt",
            "lib::tests::probe = perf | 0.3 | orthogonal@delay | conformance-alpha@impairment=none",
        ]
    )
    MEMBER_BUDGETS = BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance-alpha"

    def test_a_named_family_without_a_namespace_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        output = self.rejects(
            "gate-budgets: baseline.delay declares a family whose cell-name "
            "namespace is not declared"
        )
        self.assertIn("write `members.delay = conformance-alpha`", output)

    def test_a_family_whose_cells_share_no_prefix_says_so(self):
        self.write_gate(
            design=self.MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none", "probe-runner@impairment=none"
            ),
            budgets=BASE_BUDGETS + "\nbaseline.delay = alpha::t_ok",
        )
        output = self.rejects(
            "baseline.delay declares a family whose cell-name namespace is not "
            "declared"
        )
        self.assertIn(
            "its rows' cells are named conformance-alpha, probe-runner and share "
            "no prefix",
            output,
        )

    def test_a_namespace_for_an_undeclared_family_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=self.MEMBER_BUDGETS + "\nmembers.ghost = probe-*",
        )
        output = self.rejects(
            "gate-budgets: members.ghost = probe-* declares the cell-name "
            "namespace of a family gate-budgets does not declare"
        )
        self.assertIn("declare `baseline.ghost = <row>` or remove the line", output)

    def test_a_malformed_namespace_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=BASE_BUDGETS
            + "\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance alpha",
        )
        output = self.rejects("members.delay = 'conformance alpha' does not name a cell-name namespace")
        self.assertIn("optionally followed by '*' to make it the prefix", output)

    def test_a_bare_members_line_declares_no_family(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=self.MEMBER_BUDGETS + "\nmembers = conformance-*",
        )
        output = self.rejects("'members' names no family")
        self.assertIn("the default family's namespace is the residual", output)
        self.assertIn("write `members.<family> = <prefix>`", output)

    def test_a_duplicate_namespace_for_a_family_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=self.MEMBER_BUDGETS + "\nmembers.delay = conformance-alpha*",
        )
        self.rejects("duplicate members line for family 'delay'")

    def test_a_namespace_matching_no_row_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=BASE_BUDGETS
            + "\nbaseline.delay = alpha::t_ok\nmembers.delay = zzz-*",
        )
        output = self.rejects(
            "members.delay = zzz-* matches no row's cell name, so it declares a "
            "namespace nothing occupies"
        )
        self.assertIn("write the prefix the family's cells carry (conformance-alpha)", output)

    def test_a_namespace_not_covering_its_own_family_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none", "probe-runner@impairment=none"
            ),
            budgets=self.MEMBER_BUDGETS,
        )
        output = self.rejects(
            "members.delay = conformance-alpha does not cover the family's own "
            "cells (probe-runner)"
        )
        self.assertIn("write `members.delay = <prefix>`", output)

    def test_a_reference_row_outside_its_familys_namespace_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none", "probe-runner@impairment=none"
            ),
            budgets=BASE_BUDGETS
            + "\nbaseline.delay = alpha::t_ok\nmembers.delay = probe-runner",
        )
        output = self.rejects(
            "gate-perf-design row alpha::t_ok is the reference of baseline.delay, "
            "but its cells are named conformance-alpha, which members.delay = "
            "probe-runner does not claim"
        )
        self.assertIn("the family's namespace must contain its own reference", output)

    def test_a_row_outside_its_familys_namespace_fails(self):
        """The row's cell name is unique, so the fix is the family's declaration."""
        self.write_gate(
            design=self.MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none", "probe-runner@impairment=none"
            ),
            budgets=self.MEMBER_BUDGETS,
        )
        output = self.rejects(
            "gate-perf-design row lib::tests::probe: its cells are named "
            "probe-runner, which members.delay = conformance-alpha does not "
            "claim"
        )
        self.assertIn(
            "declare it as this family's own cell name: `members.delay = <prefix>`",
            output,
        )

    def test_a_row_whose_cell_name_belongs_to_another_family_fails(self):
        self.write_gate(
            design="\n".join(
                [
                    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                    "alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms",
                    "alpha::t_ig = perf | 0.2 | baseline@other | probe-alpha@metric=throughput+scale=200-pkt",
                    "lib::tests::probe = perf | 0.3 | orthogonal@delay | probe-alpha@impairment=none",
                ]
            ),
            budgets=BASE_BUDGETS
            + "\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance-alpha"
            + "\nbaseline.other = alpha::t_ig\nmembers.other = probe-alpha",
        )
        output = self.rejects(
            "gate-perf-design row lib::tests::probe: its cells are named "
            "probe-alpha, which members.delay = conformance-alpha does not claim"
        )
        self.assertIn(
            "those cells belong to family 'other' (members.other = probe-alpha), "
            "so state the row against `@other` or move the name out",
            output,
        )

    def test_a_cell_name_claimed_by_two_families_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=self.MEMBER_BUDGETS + "\nmembers.other = conformance-*",
        )
        output = self.rejects(
            "gate-budgets: the cell name 'conformance-alpha' is claimed by 2 "
            "families (members.delay = conformance-alpha, members.other = "
            "conformance-*)"
        )
        self.assertIn("a cell name belongs to exactly one family", output)
        self.assertIn(
            "gate-perf-design row alpha::t_ok: its cells are named "
            "conformance-alpha, which 2 families claim",
            output,
        )

    def test_a_default_row_inside_a_named_namespace_fails(self):
        self.write_gate(
            design=self.MEMBER_DESIGN,
            budgets=self.MEMBER_BUDGETS.replace(
                "members.delay = conformance-alpha", "members.delay = conformance-*"
            ),
        )
        output = self.rejects(
            "gate-perf-design row beta::t_beta names no family, but its cells are "
            "named conformance-beta, which belong to family 'delay'"
        )
        self.assertIn("write `@delay`", output)

    def test_the_default_familys_residual_namespace_is_a_derivation(self):
        """A default row whose cell name no family claims is in the family."""
        self.write_gate(design=self.MEMBER_DESIGN, budgets=self.MEMBER_BUDGETS)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "gate-perf-namespace: default(residual) 2 row(s), cells: "
            "conformance-beta, probe-alpha",
            output,
        )
        self.assertIn(
            "gate-perf-membership: 1 cell-name namespace(s) declared, 1 cell "
            "name(s) claimed, 0 row(s) stated outside the namespace of the "
            "family they name",
            output,
        )


if __name__ == "__main__":
    unittest.main()
