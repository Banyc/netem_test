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
"""A stand-in cargo that prints a plan's `--list` output and target list."""

import json
import os
import pathlib
import sys


def metadata(plan):
    """The package's own target list, as an explicit plan or from the lists.

    A plan that states `metadata` is used as given; otherwise the packages and
    their test targets are the `package|target` keys the plan already answers
    `--list` for, so a fixture's target list and its list answers cannot
    disagree, and each target's source sits under the scenario directory the
    fixture is checked with. The reserved `lib` target is not a scenario
    target and is left out.
    """
    if "metadata" in plan:
        return plan["metadata"]
    targets = {}
    for key in plan.get("lists", {}):
        package, _, target = key.partition("|")
        if not target or target == "lib":
            continue
        targets.setdefault(package, []).append(
            {
                "name": target,
                "kind": ["test"],
                "src_path": f"{{root}}/tests/tests/{target}.rs",
            }
        )
    return {
        "packages": [
            {
                "name": package,
                "targets": sorted(targets[package], key=lambda t: t["name"]),
            }
            for package in sorted(targets)
        ]
    }


def main():
    plan = json.loads(pathlib.Path(os.environ["FAKE_CARGO_PLAN"]).read_text())
    argv = sys.argv[1:]
    if argv[:1] == ["metadata"]:
        # `{root}` is the directory cargo was invoked in, so a fixture states
        # its target sources relative to its own root.
        print(json.dumps(metadata(plan)).replace("{root}", os.getcwd()))
        return 0
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

# The reserved `lib` target's tree: one report-only `#[ignore]`d probe and one
# always-run test, so a `lib::…` entry in either manifest block resolves from
# the lib source the way cargo reports it.
LIB_RS = """pub fn helper() -> u8 {
    1
}

mod tests {
    #[test]
    fn t_lib_default() {
        assert_eq!(super::helper(), 1);
    }

    #[test]
    #[ignore = "release perf probe"]
    fn probe() {
        println!("report only");
    }
}
"""

GATE_TEMPLATE = """# the fixture gate

{lib_package}```gate-manifest
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
# report-only probe. The packages' own test targets are derived from these
# lists unless a case states `metadata` itself.
BASE_PLAN = {
    "lists": {
        "tests|alpha": {"default": ["t_ok"], "ignored": ["t_ig"]},
        "tests|beta": {"default": ["t_beta"], "ignored": []},
        "tests|lib": {"default": ["tests::t_lib_default"], "ignored": ["tests::probe"]},
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


class CheckGatePerfFixture(unittest.TestCase):
    """The fake-cargo fixture: a crate root, a stand-in cargo and a GATE.md.

    It carries no cases of its own, so a case class that needs the fixture
    inherits the setup and helpers without inheriting another case class's
    cases.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name) / "fixture"
        (self.root / "tests" / "tests").mkdir(parents=True)
        (self.root / "tests" / "tests" / "alpha.rs").write_text(ALPHA_RS, encoding="utf-8")
        (self.root / "tests" / "tests" / "beta.rs").write_text(BETA_RS, encoding="utf-8")
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "lib.rs").write_text(LIB_RS, encoding="utf-8")
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
        lib_package=None,
    ):
        block = (
            ""
            if lib_package is None
            else f"```gate-lib-package\n{lib_package}\n```\n\n"
        )
        text = GATE_TEMPLATE.format(
            lib_package=block,
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


class CheckGatePerfTest(CheckGatePerfFixture):
    """The perf-test dual mandate: the declared budgets and coverage."""

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

    # -- the reserved `lib` target ------------------------------------------

    def test_a_lib_entry_in_the_manifest_resolves(self):
        """A `lib::…` opt-in is declared, not refused as an unexplained STALE."""
        self.write_gate(manifest="alpha::t_ig = perf\nlib::tests::probe = perf")
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "lib target: 1 ignored scenario(s), 1 named in gate-manifest", output
        )
        self.assertNotIn("STALE manifest entry", output)

    def test_a_lib_opt_in_a_perf_design_row_names_is_not_called_unclassified(self):
        """A design row naming the reserved target is one of the two remedies.

        The note names both remedies, and a design row resolves the opt-in on
        the same run (its family, coverage and budget are checked), so firing
        the note while the design block names it is a false alarm.
        """
        self.write_gate()
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("unclassified ignored lib scenario", output)
        self.assertIn(
            "lib target: 1 ignored scenario(s), 1 named in gate-manifest or "
            "a gate-perf-design row",
            output,
        )

    def test_an_undeclared_lib_opt_in_is_reported_by_name(self):
        """The lib target's ignored set is advisory, but it is not silent."""
        self.write_gate(
            design="\n".join(
                row for row in BASE_DESIGN.splitlines() if "lib::" not in row
            )
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "note: unclassified ignored lib scenario lib::tests::probe", output
        )
        self.assertIn("lib target: 1 ignored scenario(s), 0 named", output)

    def test_a_lib_entry_in_gate_default_required_resolves(self):
        """`gate-default-required` resolves the reserved target through `--lib`."""
        self.write_gate(
            required="alpha::t_ok\nlib::tests::t_lib_default",
            asserting="alpha::t_ok\nlib::tests::t_lib_default",
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("default-required: 2 asserting scenario(s) present", output)

    # -- the zero-row (gap-only) declaration ---------------------------------

    def test_a_zero_row_gap_only_declaration_passes(self):
        """A crate with no perf arm states the negative instead of a fake row."""
        self.write_gate(design="", budgets="", gaps=BASE_GAPS)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-perf-design: 0 perf test row(s)", output)
        self.assertIn("baseline unset", output)

    def test_a_zero_row_declaration_with_no_gap_fails(self):
        """Zero rows with zero gaps declares nothing, and says so."""
        self.write_gate(design="", budgets="", gaps="")
        self.rejects("declares no row and gate-coverage-gaps records no gap")

    def test_a_zero_row_declaration_with_a_baseline_fails(self):
        """The gap-only form references nothing, so a baseline is a mistake."""
        self.write_gate(design="", budgets="baseline = beta::t_beta", gaps=BASE_GAPS)
        self.rejects(
            "declares baseline 'beta::t_beta' while gate-perf-design declares no row"
        )

    def test_a_zero_row_declaration_still_refuses_a_reason_less_gap(self):
        """The gap grammar keeps its teeth in the zero-row form."""
        self.write_gate(design="", budgets="", gaps="M1@lane=dual-lane = ")
        self.rejects("records no reason")

    def test_a_lib_entry_that_is_not_an_ignored_test_is_still_stale(self):
        """The new resolution does not turn `gate-manifest` into a free-form list."""
        self.write_gate(
            manifest="alpha::t_ig = perf\nlib::tests::probe = perf\nlib::tests::t_nope = perf"
        )
        self.rejects("STALE manifest entry (no longer ignored): lib::tests::t_nope")

    def test_a_declared_lib_package_resolves_the_reserved_target(self):
        """A manifest may name a sibling member's lib target as the reserved one."""
        (self.root / "other" / "src").mkdir(parents=True)
        (self.root / "other" / "src" / "lib.rs").write_text(LIB_RS, encoding="utf-8")
        self.write_plan(
            {
                "lists": {
                    "tests|alpha": {"default": ["t_ok"], "ignored": ["t_ig"]},
                    "tests|beta": {"default": ["t_beta"], "ignored": []},
                    "other|lib": {
                        "default": ["tests::t_lib_default"],
                        "ignored": ["tests::probe"],
                    },
                }
            }
        )
        self.write_gate(
            lib_package="other",
            required="alpha::t_ok\nlib::tests::t_lib_default",
            asserting="alpha::t_ok\nlib::tests::t_lib_default",
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("does not report this test", output)
        self.assertIn("default-required: 2 asserting scenario(s) present", output)
        self.assertIn("gate-perf-design: 4 perf test row(s)", output)

    def test_a_declared_lib_package_that_lacks_the_test_fails(self):
        """The declaration redirects resolution; it is not a fallback to both."""
        (self.root / "other" / "src").mkdir(parents=True)
        (self.root / "other" / "src" / "lib.rs").write_text(LIB_RS, encoding="utf-8")
        # The plan keeps the lib tests under `tests`; the declared `other` lib
        # reports none, so a row that resolved against `tests` would pass here.
        self.write_gate(lib_package="other")
        self.rejects(
            "PERF DECLARATION: gate-perf-design row lib::tests::probe: the 'lib' "
            "target does not report this test"
        )

    def test_a_gate_lib_package_block_naming_no_package_fails(self):
        self.write_gate(lib_package="")
        self.rejects("must name exactly one package, found 0")

    def test_a_gate_lib_package_block_naming_two_packages_fails(self):
        self.write_gate(lib_package="other\nnetem-test")
        self.rejects("must name exactly one package, found 2")

    def test_a_gate_lib_package_block_naming_an_invalid_package_fails(self):
        self.write_gate(lib_package="not a package")
        self.rejects("which is not a cargo package name")

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


# The env-scaled opt-in surface fixture: a crate-local env reader reached
# through a wrapper (the shape a real crate uses), and a runner that names the
# variables it sets.
ENV_KNOB_RS = """fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn iterations() -> u64 {
    env_parse("FIXTURE_ITERATIONS", 10)
}

pub fn rounds() -> u64 {
    env_parse("FIXTURE_ROUNDS", 4)
}
"""

ENV_RUNNER_PY = '''#!/usr/bin/env python3
"""Runs the fixture's batches with the sizing the sweep needs."""

import os
import subprocess
import sys


def main():
    env = dict(os.environ)
    env["FIXTURE_ITERATIONS"] = "100"
    env["FIXTURE_ROUNDS"] = "8"
    return subprocess.run([sys.executable, "-c", "pass"], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''

# A knob the sources read and no script sets: the shape an in-process test knob
# has, which is the half of a surface the scripts cannot show.
WAKE_KNOB_RS = """fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn wake_bound_ms() -> u64 {
    env_parse("FIXTURE_WAKE_BOUND", 50)
}
"""


def append_block(path: Path, name: str, body: str) -> None:
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n```{name}\n{body}```\n",
        encoding="utf-8",
    )


class EnvTierFixture(unittest.TestCase):
    """The crate root an env-scaled-surface case is checked against.

    The fixture is its own crate root rather than a subclass of the perf
    fixture: the env check reads sources and scripts, not compiled test lists,
    so it needs no `gate-perf-design`/`gate-budgets` blocks at all. A case that
    needs another source reads it in through `EXTRA_SOURCES`.
    """

    GATE = (
        "# the fixture gate\n\n```gate-manifest\nalpha::t_ig = perf\n```\n\n"
        "```gate-default-required\nalpha::t_ok\n```\n\n"
        "```gate-asserting\nalpha::t_ok\n```\n\n"
        "```gate-perf-guard-helpers\n```\n"
    )

    EXTRA_SOURCES: dict[str, str] = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name) / "fixture"
        (self.root / "tests" / "tests").mkdir(parents=True)
        (self.root / "tests" / "tests" / "alpha.rs").write_text(ALPHA_RS, encoding="utf-8")
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "env_knob.rs").write_text(ENV_KNOB_RS, encoding="utf-8")
        for name, text in self.EXTRA_SOURCES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "local").mkdir()
        (self.root / "local" / "run_env.py").write_text(ENV_RUNNER_PY, encoding="utf-8")
        (self.root / "tests" / "GATE.md").write_text(self.GATE, encoding="utf-8")
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir()
        cargo = self.bin_dir / "cargo"
        cargo.write_text(FAKE_CARGO, encoding="utf-8")
        cargo.chmod(0o755)
        self.plan_path = Path(self._tmp.name) / "plan.json"
        self.plan_path.write_text(
            json.dumps({"lists": {"tests|alpha": {"default": ["t_ok"], "ignored": ["t_ig"]}}}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def declare(self, body):
        append_block(self.root / "tests" / "GATE.md", "gate-env-tier", body + "\n")

    def check(self):
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
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
        )
        return proc.returncode, proc.stdout + proc.stderr

    def rejects(self, fragment):
        code, output = self.check()
        self.assertNotEqual(code, 0, f"expected a non-zero exit; output={output}")
        self.assertIn(fragment, output)
        return output


class CheckGateEnvTierTest(EnvTierFixture):
    """The env-scaled opt-in surface: detection, and the enforcement it gets.

    Every other block keys on `#[ignore]`, so a tier that is scaled by an
    environment variable and run by a script appears in none of them. These
    cases pin both halves: the surface a crate has but has not declared is named
    (not silently absent), and a declared surface is enforced in both directions
    (a variable the runner sets and the sources read must be named, a declared
    variable the crate never reads is stale).
    """

    def test_an_undeclared_surface_is_named_rather_than_invisible(self):
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("env-scaled opt-in surface undeclared", output)
        self.assertIn("FIXTURE_ITERATIONS", output)
        self.assertIn("local/run_env.py", output)
        self.assertIn("src/env_knob.rs", output)

    def test_a_well_formed_surface_passes_and_is_summarised(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
            "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 1 env-scaled surface(s)", output)
        self.assertIn("gate-env-tier-surface: fixture-churn", output)
        self.assertNotIn("env-scaled opt-in surface undeclared", output)

    def test_a_surface_the_declaration_omits_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
        )
        self.rejects(
            "FIXTURE_ROUNDS is set by local/run_env.py and read by this "
            "crate's sources, so it scales an opt-in tier"
        )

    def test_a_declared_variable_no_source_reads_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_GHOST "
            "| local/run_env.py | per-dial loss rate | fixture-liveness@shape=churn"
        )
        self.rejects(
            "fixture-churn: FIXTURE_GHOST is passed to no env-reading function"
        )

    def test_a_runner_naming_no_declared_variable_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | tools/other.py "
            "| per-dial loss rate | fixture-liveness@shape=churn"
        )
        (self.root / "tools").mkdir()
        (self.root / "tools" / "other.py").write_text("print('nothing')\n")
        self.rejects("runner 'tools/other.py' names none of the declared variables")

    def test_a_row_with_no_runner_field_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | "
            "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
        )
        self.rejects(
            "no runner field; state the script that runs the surface, or '-' "
            "for a surface no script runs"
        )

    def test_a_row_with_three_fields_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate under a sized load"
        )
        self.rejects("got 3 field(s)")

    def test_a_runner_that_is_not_a_file_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/absent.py "
            "| per-dial loss rate | fixture-liveness@shape=churn"
        )
        self.rejects("runner 'local/absent.py' is not a file under")

    def test_a_repeated_variable_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate | fixture-liveness@shape=churn"
        )
        self.rejects("FIXTURE_ITERATIONS named more than once")

    def test_a_malformed_variable_name_fails(self):
        self.declare(
            "fixture-churn = fixture_iterations | local/run_env.py "
            "| per-dial loss rate | fixture-liveness@shape=churn"
        )
        self.rejects("is not an environment variable name")

    def test_a_surface_with_no_coverage_cell_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
            "| per-dial loss rate | "
        )
        self.rejects("covers no cell")

    def test_a_surface_that_measures_nothing_fails(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
            "| | fixture-liveness@shape=churn"
        )
        self.rejects("says nothing about what it measures")


class CheckGateEnvTierScriptlessTest(EnvTierFixture):
    """The half of a surface no script can show.

    A knob the sources read in-process is named by no script and belongs to no
    `#[ignore]` set, so the declaration is the only artifact that records it and
    an omission is otherwise silent. These cases pin that enforcement — the red
    side is the same declaration a script-named variable would have needed — and
    the advisory that names such a surface when there is no block at all.
    """

    EXTRA_SOURCES = {"src/wake_knob.rs": WAKE_KNOB_RS}

    def test_a_variable_only_the_sources_read_must_be_declared(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
            "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
        )
        self.rejects(
            "FIXTURE_WAKE_BOUND is read by src/wake_knob.rs and set by no script"
        )

    def test_a_declared_scriptless_variable_passes(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_WAKE_BOUND "
            "| local/run_env.py | per-dial loss rate and the handover wake bound "
            "| fixture-liveness@shape=churn, fixture-wake@shape=handover"
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 1 env-scaled surface(s), 3 variable(s)", output)
        self.assertIn("FIXTURE_WAKE_BOUND", output)

    def test_the_note_names_a_surface_no_script_sets(self):
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("FIXTURE_WAKE_BOUND", output)
        self.assertIn("read by src/env_knob.rs, src/wake_knob.rs", output)

    def test_the_note_names_a_surface_no_script_sets_at_all(self):
        (self.root / "local" / "run_env.py").unlink()
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "FIXTURE_ITERATIONS, FIXTURE_ROUNDS, FIXTURE_WAKE_BOUND "
            "(named by no script and read by src/env_knob.rs, src/wake_knob.rs)",
            output,
        )

    def test_a_surface_no_script_runs_is_declared_with_the_marker(self):
        self.declare(
            "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
            "| per-dial loss rate under a sized load | fixture-liveness@shape=churn\n"
            "fixture-wake = FIXTURE_WAKE_BOUND | - | the handover wake bound the "
            "sources read in-process | fixture-wake@shape=handover"
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 2 env-scaled surface(s), 3 variable(s)", output)
        self.assertIn("fixture-wake (no script runner) FIXTURE_WAKE_BOUND", output)

    def test_a_marked_surface_whose_variables_a_script_sets_fails(self):
        self.declare(
            "fixture-wake = FIXTURE_ITERATIONS,FIXTURE_WAKE_BOUND | - "
            "| the handover wake bound the sources read in-process "
            "| fixture-wake@shape=handover"
        )
        self.rejects(
            "'-' states that no script runs it, but local/run_env.py sets "
            "FIXTURE_ITERATIONS"
        )


class CheckGateEnvTierLoadTest(EnvTierFixture):
    """The load shape and cost a surface records with its declaration.

    A cost stated in prose is read by nothing; the field is written so its
    count is derived from the sizes it records, its keys are the surface's own
    variables, and those variables are the ones the runner sets -- so the
    recorded shape is the shape the runner runs.
    """

    EXTRA_SOURCES = {"src/wake_knob.rs": WAKE_KNOB_RS}

    CHURN = (
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_WAKE_BOUND "
        "| local/run_env.py | per-dial loss rate under a sized load "
        "| fixture-liveness@shape=churn"
    )

    def test_a_load_shape_is_recorded_and_evaluated(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_ITERATIONS,FIXTURE_ROUNDS "
            "| local/run_env.py | per-dial loss rate at a stated cost "
            "| fixture-liveness@shape=churn "
            "| FIXTURE_ITERATIONS=100,FIXTURE_ROUNDS=8,"
            "total=FIXTURE_ITERATIONS*FIXTURE_ROUNDS,wall=2.5s,bound=3e-4/dial"
        )
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "gate-env-tier-load: fixture-load "
            "FIXTURE_ITERATIONS*FIXTURE_ROUNDS = 800, 2.5s, bound 3e-4/dial",
            output,
        )

    def test_a_load_whose_total_is_not_arithmetic_fails(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate at a stated cost "
            "| fixture-liveness@shape=churn "
            "| FIXTURE_ITERATIONS=100,total=2*,wall=2.5s"
        )
        self.rejects(
            "load total=2* is not arithmetic over the named variables"
        )

    def test_a_load_total_no_variable_produces_fails(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate at a stated cost "
            "| fixture-liveness@shape=churn "
            "| FIXTURE_ITERATIONS=100,total=800,wall=2.5s"
        )
        self.rejects("load total=800 yields a count from no variable")

    def test_a_load_with_no_wall_clock_fails(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py "
            "| per-dial loss rate at a stated cost "
            "| fixture-liveness@shape=churn "
            "| FIXTURE_ITERATIONS=100,total=FIXTURE_ITERATIONS*8"
        )
        self.rejects("load wall=None is not a positive duration")

    def test_a_load_sizing_a_variable_the_runner_does_not_set_fails(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_WAKE_BOUND | local/run_env.py "
            "| the wake bound at a stated cost "
            "| fixture-wake@shape=handover "
            "| FIXTURE_WAKE_BOUND=50,total=FIXTURE_WAKE_BOUND*2,wall=0.5s"
        )
        self.rejects(
            "its load sizes FIXTURE_WAKE_BOUND, which the runner "
            "'local/run_env.py' does not set"
        )

    def test_a_row_with_a_sixth_field_is_refused(self):
        self.declare(
            self.CHURN
            + "\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py "
            "| the load at a stated cost | fixture-liveness@shape=churn "
            "| FIXTURE_ITERATIONS=100,total=FIXTURE_ITERATIONS*8,wall=0.5s "
            "| extra"
        )
        self.rejects("got 6 field(s)")


# A crate whose surface is read the three ways the reader half has to tell
# apart: a literal handed straight to `env::var`, a literal forwarded to a
# crate-local reader through a two-hop wrapper, and a literal that is only a
# *mention* -- in a doc comment, in a block comment, inside an ordinary string
# constant, inside a raw one. A mention is not a read: if the scan counted one,
# a declaration naming it would pass the stale-declaration rule that exists to
# catch exactly that. `CARGO_TARGET_DIR` is the runner's own environment, which
# is no crate's knob.
DIRECT_KNOB_RS = r'''//! `env::var("FIXTURE_COMMENTED")` is prose about a read, not a read.
/* and `env::var("FIXTURE_BLOCK")` is prose in a block comment. */

fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

fn hops(name: &str, default: u64) -> u64 {
    env_parse(name, default)
}

pub fn forwarded() -> u64 {
    hops("FIXTURE_FORWARDED", 16)
}

pub fn direct_cycles() -> u64 {
    std::env::var("FIXTURE_DIRECT_CYCLES")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1500)
}

pub fn direct_dist_dir() -> Option<std::ffi::OsString> {
    std::env::var_os("FIXTURE_DIST_DIR")
}

static MODULE_SCOPE: std::sync::LazyLock<Option<String>> =
    std::sync::LazyLock::new(|| std::env::var("FIXTURE_MODULE_SCOPE").ok());

static TARGET: std::sync::LazyLock<Option<String>> =
    std::sync::LazyLock::new(|| std::env::var("CARGO_TARGET_DIR").ok());

const DOC: &str = "env::var(\"FIXTURE_ESCAPED\")";
const RAW: &str = r#"env::var("FIXTURE_RAW")"#;
'''


class CheckGateEnvTierDirectReadTest(EnvTierFixture):
    """The literals the reader half has to tell from a mention.

    A variable read by a direct `std::env::var("NAME")` literal is a read, and
    without it in the reader set a declaration naming it trips the
    stale-declaration half -- the grammar refusing a true statement. The other
    side is a mention: an `env::var("NAME")` in a comment or inside a string
    constant is prose, so a declaration resting on one must still be refused.
    """

    EXTRA_SOURCES = {"src/direct_knob.rs": DIRECT_KNOB_RS}

    CHURN = (
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
        "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
    )

    REAL = (
        "fixture-direct = FIXTURE_DIRECT_CYCLES,FIXTURE_DIST_DIR,"
        "FIXTURE_FORWARDED,FIXTURE_MODULE_SCOPE | - | the load the sources read "
        "in-process | fixture-liveness@shape=direct"
    )

    MENTIONS = (
        "fixture-mentions = FIXTURE_BLOCK,FIXTURE_COMMENTED,FIXTURE_ESCAPED,"
        "FIXTURE_RAW | - | the names only a comment or a string constant "
        "mentions | fixture-liveness@shape=mention"
    )

    def test_the_direct_literal_and_the_var_os_literal_are_reads(self):
        self.declare(self.CHURN + "\n" + self.REAL)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 2 env-scaled surface(s), 6 variable(s)", output)
        self.assertIn("FIXTURE_DIRECT_CYCLES, FIXTURE_DIST_DIR", output)

    def test_a_direct_literal_the_declaration_omits_is_refused(self):
        self.declare(self.CHURN)
        self.rejects(
            "FIXTURE_DIRECT_CYCLES is read by src/direct_knob.rs and set by no "
            "script"
        )

    def test_the_reader_closure_still_reaches_a_two_hop_wrapper(self):
        """`forwarded` -> `hops` -> `env_parse` -> `env::var` is a reader."""
        self.declare(self.CHURN)
        self.rejects(
            "FIXTURE_FORWARDED is read by src/direct_knob.rs and set by no script"
        )

    def test_a_name_only_a_mention_names_is_still_stale(self):
        self.declare(self.CHURN + "\n" + self.REAL + "\n" + self.MENTIONS)
        self.rejects(
            "surface fixture-mentions: FIXTURE_BLOCK, FIXTURE_COMMENTED, "
            "FIXTURE_ESCAPED, FIXTURE_RAW is passed to no env-reading function "
            "of this crate; a declared variable the crate never reads is a "
            "stale declaration"
        )

    def test_a_toolchain_name_is_not_demanded_of_a_surface(self):
        self.declare(self.CHURN + "\n" + self.REAL)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("CARGO_TARGET_DIR", output)

    def test_a_toolchain_name_is_not_a_declarable_variable(self):
        self.declare(
            self.CHURN
            + "\n"
            + self.REAL
            + "\nfixture-runner-env = CARGO_TARGET_DIR | - | a name the "
            "toolchain sets | fixture-liveness@shape=toolchain"
        )
        self.rejects("surface fixture-runner-env: CARGO_TARGET_DIR is passed to")


# A crate whose names reach `env::var` through the two forwarders that are not a
# `fn`: a `let`-bound closure whose parameter receives the literal at the call
# site, and a `const`/`static` string alias handed to `env::var` in place of a
# literal. Both are real reads, and both are invisible to a scan that follows
# only `fn`s and string literals -- the same defect shape as the direct-literal
# gap: a declaration that is correct is refused as stale, leaving a real
# variable unrecordable.
FORWARDER_KNOB_RS = r'''fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn iterations() -> u64 {
    env_parse("FIXTURE_ITERATIONS", 10)
}

/// The closure forwarder: `env_usize` is a reader, so `closure_forwarded` is one
/// too, and the literal at its call site is the name it forwards.
pub fn closure_forwarded(key: &str) -> u64 {
    let env_usize = |name: &str, default: u64| {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    };
    env_usize(key, 7)
}

pub fn closure_read() -> u64 {
    closure_forwarded("FIXTURE_CLOSURE")
}

/// A `const` string alias handed to `env::var`: no literal stands at the call,
/// so there is nothing for the direct half to match on.
const ALIAS_NAME: &str = "FIXTURE_ALIAS";

pub fn alias_read() -> Option<String> {
    std::env::var(ALIAS_NAME).ok()
}

static OS_ALIAS_NAME: &str = "FIXTURE_OS_ALIAS";

pub fn alias_read_os() -> Option<std::ffi::OsString> {
    std::env::var_os(OS_ALIAS_NAME)
}

/// `ALIAS_NAME` declared *again* inside a raw string, after the real one: an
/// alias map built from `code` alone takes this declaration, hands
/// `FIXTURE_ALIAS_IN_STRING` to the real `env::var(ALIAS_NAME)` above, drops
/// `FIXTURE_ALIAS` from the reads, and lets a surface naming it pass.
pub const RAW_ALIAS_DECL_MENTION: &str =
    r#"const ALIAS_NAME: &str = "FIXTURE_ALIAS_IN_STRING";"#;

/// The new forms written in prose rather than in code: a closure body, an
/// `env::var(ALIAS_NAME)` call and a `const` declaration are mentions here.
pub const RAW_FORM_MENTIONS: &str = r#"let env_usize = |k: &str| std::env::var(k);
env_usize("FIXTURE_CLOSURE_MENTION");
std::env::var(ALIAS_NAME);
const FIXTURE_ALIAS_DECL_MENTION: &str = "FIXTURE_ALIAS_DECL_MENTION";"#;

// `env_usize("FIXTURE_CLOSURE_LINE_MENTION", 0)` on a doc line is prose too.
'''


class CheckGateEnvTierForwarderTest(EnvTierFixture):
    """The names a closure and a `const` alias forward to `env::var`.

    Detection of the surface is two-sided -- a script names it and a source
    reads it -- so a name the reader scan cannot see is a name the declaration
    cannot record: adding it trips the stale-declaration half, which is the
    grammar refusing a *true* statement. These cases pin both forwarders as
    reads, and pin the mention side, because counting a mention as a read makes
    the stale half fail open.
    """

    EXTRA_SOURCES = {"src/forwarder_knob.rs": FORWARDER_KNOB_RS}

    CHURN = (
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py | "
        "per-dial loss rate under a sized load | fixture-liveness@shape=churn"
    )

    FORWARDED = (
        "fixture-forwarded = FIXTURE_ALIAS,FIXTURE_CLOSURE,FIXTURE_OS_ALIAS | - "
        "| the names read through a closure forwarder and a const string alias "
        "| fixture-liveness@shape=forwarder"
    )

    FORM_MENTIONS = (
        "fixture-form-mentions = FIXTURE_ALIAS_DECL_MENTION,"
        "FIXTURE_ALIAS_IN_STRING,FIXTURE_CLOSURE_LINE_MENTION,"
        "FIXTURE_CLOSURE_MENTION | - | the new forms only prose mentions "
        "| fixture-liveness@shape=form-mention"
    )

    def test_a_closure_forwarded_name_and_a_const_alias_are_reads(self):
        self.declare(self.CHURN + "\n" + self.FORWARDED)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 2 env-scaled surface(s), 5 variable(s)", output)
        self.assertIn("FIXTURE_ALIAS, FIXTURE_CLOSURE, FIXTURE_OS_ALIAS", output)

    def test_a_closure_forwarded_name_the_declaration_omits_is_refused(self):
        self.declare(self.CHURN)
        self.rejects(
            "FIXTURE_CLOSURE is read by src/forwarder_knob.rs and set by no script"
        )

    def test_a_const_alias_name_the_declaration_omits_is_refused(self):
        self.declare(self.CHURN)
        self.rejects(
            "FIXTURE_ALIAS is read by src/forwarder_knob.rs and set by no script"
        )

    def test_a_quoted_alias_declaration_cannot_shadow_the_real_one(self):
        """`ALIAS_NAME` is re-declared inside a raw string, after the real one.

        An alias map built from `code` alone takes the quoted declaration, hands
        `FIXTURE_ALIAS_IN_STRING` to the real `env::var(ALIAS_NAME)`, drops
        `FIXTURE_ALIAS` from the reads and lets the surface below pass -- the
        stale half failing open on a mention.
        """
        self.declare(
            self.CHURN
            + "\n"
            + self.FORWARDED
            + "\nfixture-shadow = FIXTURE_ALIAS_IN_STRING | - | a name only the "
            "quoted declaration names | fixture-liveness@shape=shadow"
        )
        self.rejects(
            "surface fixture-shadow: FIXTURE_ALIAS_IN_STRING is passed to no "
            "env-reading function of this crate"
        )

    def test_the_new_forms_only_a_mention_names_are_still_stale(self):
        self.declare(
            self.CHURN + "\n" + self.FORWARDED + "\n" + self.FORM_MENTIONS
        )
        self.rejects(
            "surface fixture-form-mentions: FIXTURE_ALIAS_DECL_MENTION, "
            "FIXTURE_ALIAS_IN_STRING, FIXTURE_CLOSURE_LINE_MENTION, "
            "FIXTURE_CLOSURE_MENTION is passed to no env-reading function of "
            "this crate"
        )


# A crate whose readers do and do not hand their parameter to `env::var`. The
# propagation from "this function reads the env" to "its arguments are env
# names" is only sound for a parameter that reaches `env::var` as the key:
# `fault` reads a *fixed* literal and uses its argument as a suffix of the
# value, so an argument handed to it is an argument, and the second parameter
# of `flagged` is a label the body only prints. `hops` is the two-hop wrapper
# that *is* a key position, and `flagged`'s first position is one too.
KEYED_ARG_RS = '''fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

fn hops(name: &str, default: u64) -> u64 {
    env_parse(name, default)
}

pub fn two_hop() -> u64 {
    hops("FIXTURE_TWO_HOP", 8)
}

/// The body reads a fixed literal; `mandate` is a suffix of the value, not a key.
pub fn fault(mandate: &str) -> Option<String> {
    let value = std::env::var("FIXTURE_FAULT_KEY").ok()?;
    value.contains(mandate).then_some(mandate.to_owned())
}

pub fn faulted() -> Option<String> {
    fault("FIXTURE_NOT_A_KEY")
}

/// Two parameters, one key: `note` is printed, never handed to `env::var`.
fn flagged(key: &str, note: &str) -> u64 {
    eprintln!("{note}");
    env_parse(key, 3)
}

pub fn flagged_call() -> u64 {
    flagged("FIXTURE_KEYED", "FIXTURE_NOTE_ONLY")
}
'''


class CheckGateEnvTierKeyPositionTest(EnvTierFixture):
    """A name-shaped argument is a read only when it reaches `env::var` as a key.

    A reader that reads a *fixed* literal and uses its parameter as a suffix
    (`fault(mandate)`) forwards no name to the environment, so an argument
    handed to it is an argument: demanding a declaration for it demands a row
    for a string that is not a variable, and a row naming it is then refused as
    stale -- the grammar contradicting itself in both directions. The same rule
    read the other way is that a literal in a key position is still a read: a
    direct literal, a two-hop wrapper's forwarded parameter, and the first of
    two parameters must all be recorded.
    """

    EXTRA_SOURCES = {"src/keyed_arg.rs": KEYED_ARG_RS}

    CHURN = (
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py "
        "| per-dial loss rate under a sized load | fixture-liveness@shape=churn"
    )

    READ = (
        "fixture-keyed = FIXTURE_FAULT_KEY,FIXTURE_KEYED,FIXTURE_TWO_HOP | - "
        "| the names the read-forwarders hand to env::var as their key "
        "| fixture-liveness@shape=keyed"
    )

    def test_the_key_positions_of_the_fixture_are_the_reads(self):
        self.declare(self.CHURN + "\n" + self.READ)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("gate-env-tier: 2 env-scaled surface(s), 5 variable(s)", output)
        self.assertNotIn("FIXTURE_NOT_A_KEY", output)
        self.assertNotIn("FIXTURE_NOTE_ONLY", output)

    def test_a_name_shaped_argument_to_a_suffix_reader_is_not_demanded(self):
        """`fault("FIXTURE_NOT_A_KEY")` is an argument, not a variable."""
        self.declare(self.CHURN + "\n" + self.READ)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("no declared surface names it", output)

    def test_a_name_shaped_argument_to_a_suffix_reader_is_not_a_read(self):
        self.declare(self.CHURN)
        self.rejects(
            "FIXTURE_FAULT_KEY is read by src/keyed_arg.rs and set by no script"
        )

    def test_a_non_key_argument_declared_is_refused_as_stale(self):
        self.declare(
            self.CHURN
            + "\n"
            + self.READ
            + "\nfixture-suffix = FIXTURE_NOTE_ONLY,FIXTURE_NOT_A_KEY | - "
            "| a suffix and a printed label | fixture-liveness@shape=suffix"
        )
        self.rejects(
            "surface fixture-suffix: FIXTURE_NOTE_ONLY, FIXTURE_NOT_A_KEY is "
            "passed to no env-reading function of this crate"
        )

    def test_a_two_hop_key_position_is_still_a_read(self):
        """`hops` -> `env_parse` -> `env::var` carries the literal, so it is one."""
        self.declare(
            self.CHURN
            + "\nfixture-partial = FIXTURE_FAULT_KEY,FIXTURE_KEYED | - "
            "| the keyed fixture without the two-hop wrapper "
            "| fixture-liveness@shape=partial"
        )
        self.rejects(
            "FIXTURE_TWO_HOP is read by src/keyed_arg.rs and set by no script"
        )

    def test_the_first_of_two_parameters_is_the_key(self):
        """A literal at the key index is a read; the label index is not."""
        self.declare(
            self.CHURN
            + "\nfixture-partial = FIXTURE_FAULT_KEY,FIXTURE_TWO_HOP | - "
            "| the keyed fixture without the first-parameter key "
            "| fixture-liveness@shape=partial"
        )
        self.rejects("FIXTURE_KEYED is read by src/keyed_arg.rs and set by no script")


class CheckGateScenarioDirectoryTest(CheckGatePerfFixture):
    """`--crate`'s scenario directory is reconciled with the package's targets.

    The ignored set is derived over the package's own test targets: cargo says
    which exist, the directory says where their sources live. A directory that
    holds none of them is the wrong directory rather than an empty gate, and
    deriving the set from its glob alone reported every manifest entry as
    STALE; a directory that holds only some of them under-reports the same
    way, so both are named.
    """

    def check_dir(self, dir_name):
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
                dir_name,
                "tests/GATE.md",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
        )
        return proc.returncode, proc.stdout + proc.stderr

    def with_metadata_target(self, name, src_path):
        """A plan whose package compiles one more target, at ``src_path``."""
        plan = json.loads(json.dumps(BASE_PLAN))
        plan["metadata"] = {
            "packages": [
                {
                    "name": "tests",
                    "targets": [
                        {
                            "name": "alpha",
                            "kind": ["test"],
                            "src_path": "{root}/tests/tests/alpha.rs",
                        },
                        {
                            "name": "beta",
                            "kind": ["test"],
                            "src_path": "{root}/tests/tests/beta.rs",
                        },
                        {
                            "name": name,
                            "kind": ["test"],
                            "src_path": f"{{root}}/{src_path}",
                        },
                    ],
                }
            ]
        }
        self.write_plan(plan)

    def test_a_directory_holding_no_target_is_resolved_rather_than_blamed(self):
        """The package root is not the scenario directory; it is not an empty gate."""
        self.write_gate()
        code, output = self.check_dir("tests")
        self.assertEqual(code, 0, output)
        self.assertNotIn("STALE manifest entry", output)
        self.assertIn("holds no `*.rs` test target", output)
        self.assertIn("resolved from cargo's own target list", output)
        self.assertIn("gate manifest OK: 1 ignored scenarios classified", output)

    def test_a_directory_holding_some_of_the_targets_fails(self):
        """A partial directory reports the targets it does not hold as STALE."""
        self.with_metadata_target("gamma", "tests/tests/gamma.rs")
        self.write_gate()
        code, output = self.check_dir("tests/tests")
        self.assertNotEqual(code, 0, output)
        self.assertIn("SCENARIO DIRECTORY", output)
        self.assertIn("also compiles gamma", output)

    def test_targets_under_more_than_one_directory_are_an_error(self):
        """An ambiguous package has no single scenario directory to resolve to."""
        self.with_metadata_target("delta", "tests/elsewhere/delta.rs")
        self.write_gate()
        code, output = self.check_dir("tests")
        self.assertNotEqual(code, 0, output)
        self.assertIn("compiles its targets under 2 directories", output)


if __name__ == "__main__":
    unittest.main()
