"""Tests for tools/perf_loop.py."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
TOOLS = Path(__file__).with_name("perf_loop.py")
SPEC = importlib.util.spec_from_file_location("perf_loop", TOOLS)
LOOP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOOP)

# perf_loop refuses every output/temp path that escapes its safe root, so
# unit-test scratch directories must live beneath ~/code/tmp regardless of
# the ambient TMPDIR.
_SAFE_TEST_TMP = LOOP.SAFE_TEMP_ROOT / "perf-loop-unit-tests"
_SAFE_TEST_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(_SAFE_TEST_TMP)
os.environ["TMP"] = str(_SAFE_TEST_TMP)
os.environ["TEMP"] = str(_SAFE_TEST_TMP)


class PerfLoopTest(unittest.TestCase):
    def make_workspace(self, root, name):
        workspace = root / name
        (workspace / "tests").mkdir(parents=True)
        (workspace / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
        (workspace / "tests" / "Cargo.toml").write_text("", encoding="utf-8")
        return workspace

    def test_seed_parser_and_safe_output_boundary(self):
        self.assertEqual(LOOP.parse_seeds("11, 21"), (11, 21))
        self.assertEqual(LOOP.parse_seeds(" 7, 9 "), (7, 9))
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11,11")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11, abc")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11, -1")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("18446744073709551616")
        self.assertEqual(LOOP.parse_label("run-a_1"), "run-a_1")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_label(".")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_label("a/b")
        self.assertEqual(LOOP.parse_nonnegative_seconds("0"), 0.0)
        self.assertEqual(LOOP.parse_nonnegative_seconds("2.5"), 2.5)
        for invalid in ("-1", "nan", "inf", "nope"):
            with self.assertRaises(argparse.ArgumentTypeError):
                LOOP.parse_nonnegative_seconds(invalid)

        outside = Path.home() / "code" / "net" / "not-a-safe-perf-output"
        with self.assertRaises(ValueError):
            LOOP.safe_output_dir(outside)
        with self.assertRaises(ValueError):
            LOOP.safe_build_dir(outside, outside, "release")
        string_output = LOOP.SAFE_TEMP_ROOT / f"perf-string-output-{os.getpid()}"
        try:
            self.assertEqual(
                LOOP.safe_output_dir(str(string_output)),
                string_output.resolve(),
            )
            self.assertEqual(
                LOOP.safe_build_dir(
                    str(string_output / "target"), string_output, "release"
                ),
                (string_output / "target").resolve(),
            )
        finally:
            shutil.rmtree(string_output, ignore_errors=True)

    def test_pair_execution_order_is_counterbalanced(self):
        specs = [{"seed": 11}, {"seed": 21}, {"seed": 31}]
        # Even pair indexes run baseline before candidate; odd pair indexes
        # run candidate before baseline, so execution order cannot bias roles.
        self.assertEqual(
            list(LOOP.paired_execution_specs(specs, 0)),
            [("baseline", specs[0]), ("candidate", specs[0])],
        )
        self.assertEqual(
            list(LOOP.paired_execution_specs(specs, 1)),
            [("candidate", specs[1]), ("baseline", specs[1])],
        )
        self.assertEqual(
            list(LOOP.paired_execution_specs(specs, 2)),
            [("baseline", specs[2]), ("candidate", specs[2])],
        )
        # Role labels stay baseline/candidate regardless of the order chosen.
        roles = [
            role
            for index in range(len(specs))
            for role, _ in LOOP.paired_execution_specs(specs, index)
        ]
        self.assertEqual(roles.count("baseline"), 3)
        self.assertEqual(roles.count("candidate"), 3)


    def test_execution_order_analysis_detects_role_independent_drift(self):
        runs = [
            {"role": "baseline", "seed": 11},
            {"role": "candidate", "seed": 11},
            {"role": "candidate", "seed": 21},
            {"role": "baseline", "seed": 21},
            {"role": "baseline", "seed": 31},
            {"role": "candidate", "seed": 31},
            {"role": "candidate", "seed": 41},
            {"role": "baseline", "seed": 41},
        ]
        deltas = (-7.0, 14.0, -15.0, 9.0)
        comparison = {
            "pairs": [
                {
                    "baseline": f"base-{seed}",
                    "candidate": f"cand-{seed}",
                    "valid": True,
                    "metrics": {
                        "goodput_mib_per_second": {"delta_percent": delta}
                    },
                }
                for seed, delta in zip((11, 21, 31, 41), deltas)
            ]
        }
        analysis = LOOP.execution_order_analysis(comparison, runs)
        self.assertEqual(analysis["classification"], "consistent_later_slower")
        self.assertTrue(analysis["directionally_confounded"])
        self.assertEqual(analysis["valid_pairs"], 4)
        self.assertEqual(analysis["median_later_minus_earlier_percent"], -11.5)
        self.assertEqual(analysis["max_absolute_later_effect_percent"], 15.0)
        self.assertEqual(analysis["material_pair_count"], 2)
        self.assertEqual(
            [pair["later_minus_earlier_percent"] for pair in analysis["pairs"]],
            [-7.0, -14.0, -15.0, -9.0],
        )

        # A candidate effect that keeps the same sign across alternating role
        # order is not explained by first/second position.
        for pair in comparison["pairs"]:
            pair["metrics"]["goodput_mib_per_second"]["delta_percent"] = 12.0
        analysis = LOOP.execution_order_analysis(comparison, runs)
        self.assertEqual(analysis["classification"], "mixed_order_effect")
        self.assertFalse(analysis["directionally_confounded"])


    def test_counterbalanced_goodput_separates_role_and_position_effects(self):
        runs = [
            {"role": role, "seed": seed}
            for seed, roles in (
                (11, ("baseline", "candidate")),
                (21, ("candidate", "baseline")),
                (31, ("baseline", "candidate")),
                (41, ("candidate", "baseline")),
            )
            for role in roles
        ]
        position_effects = (1.12, 0.95)
        candidate_effect = 1.15
        ratios = (
            candidate_effect * position_effects[0],
            candidate_effect / position_effects[0],
            candidate_effect * position_effects[1],
            candidate_effect / position_effects[1],
        )
        comparison = {
            "pairs": [
                {
                    "baseline": f"base-{seed}",
                    "candidate": f"cand-{seed}",
                    "valid": True,
                    "metrics": {
                        "goodput_mib_per_second": {
                            "delta_percent": (ratio - 1.0) * 100.0
                        }
                    },
                }
                for seed, ratio in zip((11, 21, 31, 41), ratios)
            ]
        }
        analysis = LOOP.counterbalanced_goodput_analysis(comparison, runs)
        self.assertEqual(analysis["classification"], "consistent_material_improvement")
        self.assertEqual(analysis["complete_blocks"], 2)
        self.assertEqual(analysis["included_pairs"], 4)
        self.assertEqual(analysis["excluded_pair_indexes"], [])
        self.assertAlmostEqual(
            analysis["aggregate_candidate_role_effect_percent"],
            15.0,
        )
        self.assertAlmostEqual(
            analysis["blocks"][0]["candidate_role_effect_percent"], 15.0
        )
        self.assertAlmostEqual(
            analysis["blocks"][0]["later_position_effect_percent"], 12.0
        )
        self.assertAlmostEqual(
            analysis["blocks"][1]["candidate_role_effect_percent"], 15.0
        )
        self.assertAlmostEqual(
            analysis["blocks"][1]["later_position_effect_percent"], -5.0
        )

        comparison["pairs"][1]["valid"] = False
        analysis = LOOP.counterbalanced_goodput_analysis(comparison, runs)
        self.assertEqual(analysis["classification"], "insufficient_evidence")
        self.assertEqual(analysis["complete_blocks"], 1)
        self.assertEqual(analysis["excluded_pair_indexes"], [0, 1])


    def test_snapshot_records_exact_component_revisions(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            source_root = root / "source"
            source = self.make_workspace(source_root, "netem_test")
            for component in LOOP.COMPONENTS:
                (source_root / component).mkdir(parents=True, exist_ok=True)
            output = LOOP.SAFE_TEMP_ROOT / f"perf-snapshot-test-{os.getpid()}"
            snapshot_calls = []

            def fake_snapshot(component_source, component_output, revision):
                snapshot_calls.append((component_output.name, revision))
                component_output.mkdir(parents=True)
                if component_output.name == "netem_test":
                    self.make_workspace(component_output.parent, "netem_test")
                return {
                    "commit_id": component_output.name[0].encode().hex().ljust(40, "0")[:40],
                    "change_id": component_output.name[0].ljust(32, "x"),
                }

            with mock.patch.object(LOOP, "snapshot_component", fake_snapshot):
                args = argparse.Namespace(
                    source=str(source),
                    revision="@-",
                    component_revision=[("rtp", "a" * 40)],
                    output=str(output),
                )
                self.assertEqual(LOOP.command_snapshot(args), 0)
            manifest = json.loads(
                (output / LOOP.SUITE_REVISION_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema"], LOOP.SUITE_REVISION_MANIFEST_SCHEMA)
            self.assertEqual(manifest["requested_revision"], "@-")
            self.assertEqual(
                manifest["component_revision_overrides"], {"rtp": "a" * 40}
            )
            self.assertEqual(set(manifest["components"]), set(LOOP.COMPONENTS))
            self.assertEqual(
                manifest["components"]["rtp"]["commit_id"],
                "r".encode().hex().ljust(40, "0")[:40],
            )
            # The override revision reaches the rtp snapshot; the requested
            # source revision is the fallback for every other component.
            self.assertIn(("rtp", "a" * 40), snapshot_calls)
            self.assertIn(("mux", "@-"), snapshot_calls)
            self.assertEqual(
                LOOP.jj_revision(output / "rtp"),
                manifest["components"]["rtp"],
            )
            # Duplicate and unknown overrides are rejected before any archive.
            for index, (revision_pairs, message) in enumerate(
                (
                    ([("rtp", "a" * 40), ("rtp", "b" * 40)], "duplicate component revision"),
                    ([("missing", "a" * 40)], "unknown component revision"),
                )
            ):
                reject_output = LOOP.SAFE_TEMP_ROOT / f"perf-snapshot-reject-{os.getpid()}-{index}"
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        LOOP.command_snapshot(
                            argparse.Namespace(
                                source=str(source),
                                revision="@-",
                                component_revision=revision_pairs,
                                output=str(reject_output),
                            )
                        )
                finally:
                    shutil.rmtree(reject_output, ignore_errors=True)

    def frozen_suite_fixture(self, root):
        """A minimal frozen suite export with one edge per supported shape.

        Every component that owns a crate the suite can pin is present, so
        the rewrite maps crates to their exported directories the way the
        real export does (including netem_test's crate living in a
        subdirectory).
        """
        manifests = {
            "netem_test/Cargo.toml": (
                "[workspace]\nresolver = \"3\"\nmembers = [\"netem-test\", \"tests\"]\n"
            ),
            "netem_test/netem-test/Cargo.toml": (
                "[package]\nname = \"netem-test\"\nversion = \"0.1.0\"\n"
            ),
            "netem_test/tests/Cargo.toml": (
                "[package]\nname = \"tests\"\nversion = \"0.1.0\"\n\n"
                "[dev-dependencies]\n"
                "netem-test = { path = \"../netem-test\", features = [\"test-kit\"] }\n"
                "rtp = { git = \"https://github.com/Banyc/rtp.git\", tag = \"v0.0.90\", features = [\n"
                "    \"testing\",\n"
                "] }\n"
                "mux = { git = \"https://github.com/Banyc/mux.git\", tag = \"v0.0.29\" }\n"
            ),
            "rtp/Cargo.toml": (
                "[package]\nname = \"rtp\"\nversion = \"0.1.0\"\n\n"
                "[dependencies]\n"
                "primitive = { git = \"https://github.com/Banyc/primitive.git\", tag = \"v0.0.59\" }\n"
                "tokio_udp = { git = \"https://github.com/Banyc/tokio_udp.git\", tag = \"v0.0.4\" }\n"
                "udp_listener = { git = \"https://github.com/Banyc/udp_listener.git\", tag = \"v0.0.18\" }\n"
                "\n[dependencies.netem-test]\n"
                "git = \"https://github.com/Banyc/netem_test.git\"\n"
                "tag = \"v0.0.1\"\n"
                "optional = true\n"
                "\n[target.'cfg(unix)'.build-dependencies]\n"
                "rtp_mux = { git = \"https://github.com/Banyc/rtp_mux.git\", tag = \"v0.0.5\" }\n"
            ),
            "mux/Cargo.toml": (
                "[package]\nname = \"mux\"\nversion = \"0.1.0\"\n\n"
                "[dependencies]\n"
                "rtp = { git = \"https://github.com/Banyc/rtp.git\", tag = \"v0.0.90\", features = [\"testing\"], optional = true }\n"
                "\n[dev-dependencies]\n"
                "netem-test = { git = \"https://github.com/Banyc/netem_test.git\", tag = \"v0.0.1\", features = [\"test-kit\"] }\n"
            ),
            "rtp_mux/Cargo.toml": (
                "[package]\nname = \"rtp_mux\"\nversion = \"0.1.0\"\n"
            ),
            "tokio_udp/Cargo.toml": (
                "[package]\nname = \"tokio_udp\"\nversion = \"0.1.0\"\n"
            ),
            "udp_listener/Cargo.toml": (
                "[package]\nname = \"udp_listener\"\nversion = \"0.1.0\"\n\n"
                "[dependencies]\n"
                "tokio_udp = { git = \"https://github.com/Banyc/tokio_udp.git\", tag = \"v0.0.4\" }\n"
            ),
        }
        for relative, text in manifests.items():
            path = Path(root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return Path(root)

    def test_frozen_manifest_rewrite_points_suite_edges_at_the_export(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = self.frozen_suite_fixture(Path(directory) / "suite")
            records = LOOP.rewrite_frozen_suite_dependencies(root)
            self.assertEqual(
                [
                    (record["manifest"], record["table"], record["crate"], record["to"]["path"])
                    for record in records
                ],
                [
                    ("mux/Cargo.toml", "dependencies", "rtp", "../rtp"),
                    ("mux/Cargo.toml", "dev-dependencies", "netem-test", "../netem_test/netem-test"),
                    ("netem_test/tests/Cargo.toml", "dev-dependencies", "mux", "../../mux"),
                    ("netem_test/tests/Cargo.toml", "dev-dependencies", "rtp", "../../rtp"),
                    ("rtp/Cargo.toml", "dependencies", "netem-test", "../netem_test/netem-test"),
                    ("rtp/Cargo.toml", "dependencies", "tokio_udp", "../tokio_udp"),
                    ("rtp/Cargo.toml", "dependencies", "udp_listener", "../udp_listener"),
                    ("rtp/Cargo.toml", "target.cfg(unix).build-dependencies", "rtp_mux", "../rtp_mux"),
                    ("udp_listener/Cargo.toml", "dependencies", "tokio_udp", "../tokio_udp"),
                ],
            )
            tests = (root / "netem_test" / "tests" / "Cargo.toml").read_text(encoding="utf-8")
            # The inline table keeps its other fields verbatim, including the
            # multi-line feature list, and drops the published tag.
            self.assertIn(
                'rtp = { path = "../../rtp", features = [\n    "testing",\n] }\n',
                tests,
            )
            self.assertIn('mux = { path = "../../mux" }\n', tests)
            self.assertIn('netem-test = { path = "../netem-test", features = ["test-kit"] }\n', tests)
            self.assertNotIn("github.com/Banyc/rtp", tests)
            self.assertNotIn("github.com/Banyc/mux", tests)
            rtp = (root / "rtp" / "Cargo.toml").read_text(encoding="utf-8")
            # A non-suite Banyc crate keeps its published source.
            self.assertIn(
                'primitive = { git = "https://github.com/Banyc/primitive.git", tag = "v0.0.59" }',
                rtp,
            )
            # The expanded table drops its tag line and keeps its other keys.
            self.assertIn(
                "[dependencies.netem-test]\npath = \"../netem_test/netem-test\"\noptional = true\n",
                rtp,
            )
            self.assertNotIn("tag = \"v0.0.1\"", rtp)
            self.assertNotIn("github.com/Banyc/netem_test", rtp)
            # Rewriting is idempotent: the second pass has nothing left to do.
            self.assertEqual(LOOP.rewrite_frozen_suite_dependencies(root), [])

    def test_frozen_manifest_rewrite_refuses_unmodelled_suite_edges(self):
        cases = (
            (
                "registry source",
                "[dependencies]\nrtp = { version = \"0.1\", features = [\"testing\"] }\n",
                "without a git or path source",
            ),
            (
                "mismatched repository",
                "[dependencies]\nrtp = { git = \"https://github.com/Banyc/mux.git\", tag = \"v0.0.29\" }\n",
                "cannot map to an exported sibling crate",
            ),
            (
                "renamed crate",
                "[dependencies]\nmy_rtp = { package = \"rtp\", git = \"https://github.com/Banyc/rtp.git\", tag = \"v0.0.90\" }\n",
                "cannot map to an exported sibling crate",
            ),
            (
                "non-Banyc fork",
                "[dependencies]\nrtp = { git = \"https://github.com/elsewhere/rtp.git\", tag = \"v0.0.90\" }\n",
                "instead of the exported sibling",
            ),
            (
                "workspace inheritance",
                "[dependencies]\nrtp.workspace = true\n",
                "inherits the suite crate",
            ),
            (
                "patched suite source",
                "[patch.crates-io]\nrtp = { git = \"https://github.com/Banyc/rtp.git\", tag = \"v0.0.90\" }\n",
                "patches a suite crate",
            ),
        )
        for label, header, message in cases:
            with self.subTest(label):
                with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
                    root = self.frozen_suite_fixture(Path(directory) / "suite")
                    (root / "mux" / "Cargo.toml").write_text(
                        "[package]\nname = \"mux\"\nversion = \"0.1.0\"\n\n" + header,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        LOOP.rewrite_frozen_suite_dependencies(root)

    def test_frozen_suite_guard_refuses_tag_resolved_siblings(self):
        suite = (
            "[package]\nname = \"tests\"\nversion = \"0.1.0\"\n\n"
            "[dev-dependencies]\n"
            "rtp = { git = \"https://github.com/Banyc/rtp.git\", tag = \"v0.0.90\" }\n"
        )
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = self.frozen_suite_fixture(Path(directory) / "suite")
            workspace = root / "netem_test"
            # A live workspace is not a frozen suite: no guard, no rewrite.
            self.assertEqual(LOOP.assert_frozen_suite_builds_exported_siblings(workspace), [])
            (root / LOOP.SUITE_REVISION_MANIFEST).write_text("{}\n", encoding="utf-8")
            LOOP.rewrite_frozen_suite_dependencies(root)
            self.assertEqual(LOOP.assert_frozen_suite_builds_exported_siblings(workspace), [])
            # A snapshot whose sibling edges still resolve from tags cannot be
            # a pin, so it is refused instead of compared against itself.
            (root / "netem_test" / "tests" / "Cargo.toml").write_text(suite, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "resolves suite components from git tags"
            ):
                LOOP.assert_frozen_suite_builds_exported_siblings(workspace)

    def test_frozen_snapshot_records_and_applies_dependency_rewrites(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            source = self.make_workspace(root / "source", "netem_test")
            for component in LOOP.COMPONENTS:
                (root / "source" / component).mkdir(parents=True, exist_ok=True)
            output = LOOP.SAFE_TEMP_ROOT / f"perf-snapshot-rewrite-{os.getpid()}"
            fixture = self.frozen_suite_fixture(Path(directory) / "fixture")

            def fake_snapshot(component_source, component_output, revision):
                for relative, text in (
                    (path.relative_to(fixture), path.read_text(encoding="utf-8"))
                    for path in fixture.rglob("Cargo.toml")
                ):
                    target = component_output.parent / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                return {
                    "commit_id": component_output.name[0].encode().hex().ljust(40, "0")[:40],
                    "change_id": component_output.name[0].ljust(32, "x"),
                }

            try:
                with mock.patch.object(LOOP, "snapshot_component", fake_snapshot):
                    self.assertEqual(
                        LOOP.command_snapshot(
                            argparse.Namespace(
                                source=str(source),
                                revision="@-",
                                component_revision=[("rtp", "a" * 40)],
                                output=str(output),
                            )
                        ),
                        0,
                    )
                manifest = json.loads(
                    (output / LOOP.SUITE_REVISION_MANIFEST).read_text(encoding="utf-8")
                )
                rewrites = manifest["frozen_dep_rewrites"]
                self.assertTrue(
                    {
                        ("netem_test/tests/Cargo.toml", "dev-dependencies", "rtp"),
                        ("netem_test/tests/Cargo.toml", "dev-dependencies", "mux"),
                        ("rtp/Cargo.toml", "dependencies", "tokio_udp"),
                    }
                    <= {
                        (record["manifest"], record["table"], record["crate"])
                        for record in rewrites
                    }
                )
                self.assertEqual(
                    len(rewrites),
                    len({
                        (record["manifest"], record["table"], record["crate"])
                        for record in rewrites
                    }),
                )
                for record in rewrites:
                    self.assertTrue(
                        (output / record["manifest"])
                        .parent.joinpath(record["to"]["path"])
                        .resolve()
                        .is_dir(),
                        record,
                    )
                self.assertEqual(
                    next(
                        record
                        for record in rewrites
                        if record["crate"] == "rtp"
                        and record["table"] == "dev-dependencies"
                    )["from"],
                    {"git": "https://github.com/Banyc/rtp.git", "tag": "v0.0.90"},
                )
                # The frozen build recipe, not the recorded revision, is what
                # names the sibling now.
                frozen = (output / "netem_test" / "tests" / "Cargo.toml").read_text(
                    encoding="utf-8"
                )
                self.assertIn('rtp = { path = "../../rtp", features = [\n', frozen)
                self.assertEqual(LOOP.assert_frozen_suite_builds_exported_siblings(output / "netem_test"), [])
            finally:
                shutil.rmtree(output, ignore_errors=True)

    def test_same_binary_control_calibration_detects_false_changes(self):
        comparison = {
            "verdict": "no_material_change",
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": -4.0}},
                },
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 12.5}},
                },
            ],
        }
        calibration = LOOP.control_calibration(comparison)
        self.assertEqual(calibration["classification"], "unstable")
        self.assertEqual(calibration["false_material_change_count"], 1)
        self.assertEqual(calibration["max_absolute_delta_percent"], 12.5)
        self.assertEqual(calibration["median_absolute_delta_percent"], 8.25)
        self.assertEqual(calibration["valid_pairs"], 2)

        # A same-binary fixture with one >= 10% absolute valid-pair delta
        # returns 4 under --fail-on-control-instability.
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            executable = root / "bin" / "perf_probe-frozen"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            args = argparse.Namespace(
                mss_bytes=8192,
                fec=False,
                retransmission_armor=False,
                candidate=str(workspace),
                baseline=str(workspace),
                same_binary_control=True,
                same_workspace_treatment=False,
                candidate_fec="same",
                instream_group_fec=False,
                scenario="bulk",
                fail_on_control_instability=True,
                fail_on_regression=False,
                fail_on_phase_drift=False,
                seeds=(11, 21),
                window_seconds=10,
                label=None,
                warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                baseline_executable=None,
                release=True,
                target_dir=None,
                candidate_executable=None,
                baseline_source_manifest=None,
                candidate_source_manifest=None,
                output=str(output_root),
                link_profile="direct",
            )

            def fake_build_probe(workspace, role, output_root, *, release=True, target_dir=None, toolchain=None):
                return str(executable.resolve())

            def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                return {
                    "runner_exit": 0,
                    "role": role,
                    "seed": str(seed),
                    "executable": kwargs["executable"],
                    "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                    "fec": "true" if kwargs["fec"] else "false",
                    "scenario": kwargs["scenario"],
                }

            def fake_call_compare(
                baseline_dirs,
                candidate_dirs,
                output_root,
                allowed_config_mismatches=(),
                allowed_config_fields=None,
            ):
                (Path(output_root) / "comparison.json").write_text(
                    json.dumps(comparison), encoding="utf-8"
                )
                return subprocess.CompletedProcess([], 0, b"", b"")

            with mock.patch.object(LOOP, "build_probe", fake_build_probe), mock.patch.object(
                LOOP, "run_probe", fake_run_probe
            ), mock.patch.object(LOOP, "call_compare", fake_call_compare):
                exit_code = LOOP.command_run(args)
            self.assertEqual(exit_code, 4)
            run_json = json.loads(
                (output_root / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_json["pair_execution_order"], "alternating")
            self.assertEqual(run_json["scenario"], "bulk")
            self.assertEqual(run_json["candidate_fec"], "same")
            self.assertFalse(run_json["instream_group_fec"])
            self.assertFalse(run_json["same_workspace_treatment"])
            self.assertEqual(run_json["allowed_config_mismatches"], [])
            self.assertTrue(run_json["same_binary_control"])
            self.assertEqual(
                run_json["control_calibration"]["classification"], "unstable"
            )
            self.assertEqual(
                run_json["control_calibration"]["false_material_change_count"], 1
            )
            self.assertEqual(
                run_json["execution_order_analysis"]["classification"],
                "consistent_later_slower",
            )
            self.assertTrue(
                run_json["execution_order_analysis"]["directionally_confounded"]
            )
            self.assertEqual(
                run_json["counterbalanced_goodput_analysis"]["complete_blocks"],
                1,
            )
            self.assertEqual(
                run_json["counterbalanced_goodput_analysis"]["classification"],
                "insufficient_evidence",
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["executable"], str(executable.resolve())
            )
            self.assertTrue(
                all(run["executable"] == str(executable.resolve()) for run in run_json["runs"])
            )

    def test_wakes_cap_fails_on_inflated_protocol_timer_wakes(self):
        comparison = {
            "pairs": [
                {
                    "valid": True,
                    "metrics": {
                        "peer_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": 21466.0,
                            "candidate": 21466.0,
                        },
                        "sender_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": 0.0,
                            "candidate": 0.0,
                        },
                    },
                },
                {
                    "valid": True,
                    "metrics": {
                        "peer_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": None,
                            "candidate": 300000.0,
                        },
                        "sender_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": None,
                            "candidate": None,
                        },
                    },
                },
            ]
        }
        failures = LOOP.wakes_cap_failures(comparison, 100000.0)
        self.assertEqual(len(failures), 1)
        self.assertIn("wakes/GiB cap", failures[0])
        self.assertIn("peer_protocol_timer_wakes_per_gib_delivered", failures[0])
        self.assertIn("300000", failures[0])
        self.assertIn("candidate", failures[0])
        self.assertIn("per delivered byte", failures[0])
        # Under the cap nothing fails; absent values never fail.
        self.assertEqual(LOOP.wakes_cap_failures(comparison, 400000.0), [])
        self.assertEqual(LOOP.wakes_cap_failures({"pairs": []}, 100000.0), [])

    def test_run_exits_nonzero_when_wakes_exceed_the_cap(self):
        comparison = {
            "verdict": "no_material_change",
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {
                        "goodput_mib_per_second": {"delta_percent": 3.0},
                        "peer_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": 21466.0,
                            "candidate": 300000.0,
                        },
                        "sender_protocol_timer_wakes_per_gib_delivered": {
                            "baseline": 0.0,
                            "candidate": 0.0,
                        },
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            executable = root / "bin" / "perf_probe-frozen"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            args = argparse.Namespace(
                mss_bytes=8192,
                fec=False,
                retransmission_armor=False,
                candidate=str(workspace),
                baseline=str(workspace),
                same_binary_control=True,
                same_workspace_treatment=False,
                candidate_fec="same",
                instream_group_fec=False,
                scenario="bulk",
                fail_on_control_instability=False,
                fail_on_regression=False,
                fail_on_phase_drift=False,
                fail_on_wakes_cap=100000.0,
                seeds=(11, 21),
                window_seconds=10,
                label=None,
                warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                baseline_executable=None,
                release=True,
                target_dir=None,
                candidate_executable=None,
                baseline_source_manifest=None,
                candidate_source_manifest=None,
                output=str(output_root),
                link_profile="controller-fat-pipe",
            )

            def fake_build_probe(workspace, role, output_root, *, release=True, target_dir=None, toolchain=None):
                return str(executable.resolve())

            def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                return {
                    "runner_exit": 0,
                    "role": role,
                    "seed": str(seed),
                    "executable": kwargs["executable"],
                    "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                    "fec": "true" if kwargs["fec"] else "false",
                    "scenario": kwargs["scenario"],
                }

            def fake_call_compare(
                baseline_dirs,
                candidate_dirs,
                output_root,
                allowed_config_mismatches=(),
                allowed_config_fields=None,
            ):
                (Path(output_root) / "comparison.json").write_text(
                    json.dumps(comparison), encoding="utf-8"
                )
                return subprocess.CompletedProcess([], 0, b"", b"")

            err = io.StringIO()
            with mock.patch.object(LOOP, "build_probe", fake_build_probe), mock.patch.object(
                LOOP, "run_probe", fake_run_probe
            ), mock.patch.object(LOOP, "call_compare", fake_call_compare), mock.patch.object(
                sys, "stderr", err
            ):
                exit_code = LOOP.command_run(args)
            self.assertEqual(exit_code, 2)
            self.assertIn("controller-fat-pipe wakes/GiB cap", err.getvalue())
            self.assertIn("300000", err.getvalue())

    def test_run_probe_sets_diagnostic_and_safe_build_environment(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            output_root.mkdir()
            executable = root / "bin" / "perf_probe-abc"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            calls = []

            def fake_run(command, cwd=None, env=None, stdout=None, stderr=None):
                calls.append({"command": command, "cwd": cwd, "env": env})
                return subprocess.CompletedProcess(command, 0, b"", b"")

            row = LOOP.run_probe(
                workspace,
                seed=11,
                role="baseline",
                output_root=output_root,
                executable=executable,
                release=True,
                capture_rtp=True,
                link_profile="clean",
                window_seconds=10,
                warmup_seconds=2.5,
                mss_bytes=1400,
                revision="abc123",
                scenario="message-latency",
                fec=True,
                instream_group_fec=True,
                retransmission_armor=True,
                candidate_fec="off",
                subprocess_runner=fake_run,
            )
            self.assertEqual(len(calls), 1)
            env = calls[0]["env"]
            self.assertEqual(env["NETEM_PERF_LINK_PROFILE"], "clean")
            self.assertEqual(env["NETEM_PERF_MSS_BYTES"], "1400")
            self.assertEqual(env["NETEM_PERF_FEC"], "1")
            self.assertEqual(env["NETEM_PERF_INSTREAM_GROUP_FEC"], "1")
            self.assertEqual(env["RTP_INSTREAM_GROUP_FEC"], "1")
            self.assertEqual(env["NETEM_PERF_SCENARIO"], "message-latency")
            self.assertEqual(env["RTP_RTX_DUP"], "1")
            self.assertEqual(env["NETEM_PERF_DIAGNOSTIC_MODE"], "1")
            self.assertEqual(env["NETEM_PERF_SEED"], "11")
            self.assertEqual(env["NETEM_PERF_WINDOW_SECONDS"], "10")
            self.assertEqual(env["NETEM_PERF_WARMUP_SECONDS"], "2.5")
            self.assertEqual(env["NETEM_PERF_REVISION"], "abc123")
            self.assertEqual(env["NETEM_PERF_TRACE_RTP"], "1")
            # Safe temp and target dirs beneath $TMPDIR.
            self.assertTrue(str(env["TMPDIR"]).startswith(str(LOOP.SAFE_TEMP_ROOT)))
            self.assertTrue(str(env["CARGO_TARGET_DIR"]).startswith(str(LOOP.SAFE_TEMP_ROOT)))
            # The frozen executable is invoked directly: the command begins
            # with its resolved path, selects the scenario's probe test, and
            # contains no cargo.
            command = calls[0]["command"]
            self.assertEqual(command[0], str(executable.resolve()))
            self.assertEqual(command[1], LOOP.PROBE_TESTS["message-latency"])
            self.assertNotEqual(command[1], LOOP.PERF_TEST)
            self.assertTrue(all("cargo" not in part for part in command))
            self.assertIn("--ignored", command)
            # Manifest component metadata is present and sorted, and the row
            # records the resolved executable plus scenario/FEC evidence.
            self.assertEqual(row["runner_exit"], 0)
            self.assertEqual(row["role"], "baseline")
            self.assertEqual(row["link_profile"], "clean")
            self.assertEqual(row["mss_bytes"], "1400")
            self.assertEqual(row["fec"], "true")
            self.assertEqual(row["instream_group_fec"], "true")
            self.assertEqual(row["retransmission_armor"], "true")
            self.assertEqual(row["scenario"], "message-latency")
            self.assertEqual(row["candidate_fec"], "off")
            self.assertEqual(row["warmup_seconds"], "2.5")
            self.assertEqual(row["executable"], str(executable.resolve()))
            components = json.loads(row["components"])
            self.assertEqual(
                list(components.keys()),
                sorted(LOOP.COMPONENTS),
            )
            # The scenario and FEC settings are recorded in the trace manifest.
            manifest = (
                output_root / f"trace-baseline-{row['seed']}" / "manifest.csv"
            ).read_text(encoding="utf-8")
            self.assertIn("scenario,message-latency", manifest)
            self.assertIn("instream_group_fec,true", manifest)
            self.assertIn("candidate_fec,off", manifest)

    def test_build_probe_freezes_executable_before_measurement(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            output_root.mkdir()
            executable = root / "target" / "release" / "deps" / "perf_probe-4a0bed5"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            calls = []

            def fake_run(command, cwd=None, env=None, stdout=None, stderr=None):
                calls.append({"command": command, "cwd": cwd, "env": env})
                stdout.write(
                    json.dumps(
                        {
                            "reason": "compiler-artifact",
                            "target": {"name": "perf_probe", "kind": ["test"]},
                            "filenames": [str(executable)],
                        }
                    ).encode()
                    + b"\n"
                )
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.object(LOOP.subprocess, "run", fake_run):
                path = LOOP.build_probe(workspace, "baseline", output_root, release=True)

            preserved = Path(path)
            self.assertNotEqual(preserved, executable.resolve())
            # The frozen executable is preserved OUTSIDE the build target
            # (under output_root/frozen/<role>/target/<profile>/deps) so the
            # disposable role target can be pruned without losing it.
            self.assertEqual(
                preserved.parent,
                (output_root / "frozen" / "baseline" / "target" / "release" / "deps").resolve(),
            )
            self.assertTrue(
                preserved.name.startswith(f"{executable.name}.perf-loop-")
            )
            self.assertEqual(preserved.read_bytes(), executable.read_bytes())
            self.assertTrue(os.access(preserved, os.X_OK))

            executable.write_text("candidate bytes\n", encoding="utf-8")
            self.assertEqual(preserved.read_text(encoding="utf-8"), "#!/bin/sh\n")
            self.assertEqual(len(calls), 1)
            command = calls[0]["command"]
            self.assertEqual(command[:2], ["cargo", "test"])
            self.assertIn("--release", command)
            self.assertIn("--no-run", command)
            self.assertIn("--message-format=json-render-diagnostics", command)
            self.assertIn("perf_probe", command)
            # No timed run compiles: the frozen build streams into
            # build-baseline.log and stops before any probe execution flags.
            self.assertTrue((output_root / "build-baseline.log").is_file())
            self.assertNotIn("--ignored", command)
            self.assertNotIn("--nocapture", command)
            self.assertTrue(
                calls[0]["env"]["CARGO_TARGET_DIR"].startswith(str(LOOP.SAFE_TEMP_ROOT))
            )
            # Inherited compiler wrappers and flags cannot alter the frozen
            # bytes: RUSTC_WRAPPER and RUSTFLAGS are cleared beside the two
            # existing wrapper clears.
            self.assertEqual(calls[0]["env"]["RUST_WRAPPER"], "")
            self.assertEqual(calls[0]["env"]["RUSTC_WORKSPACE_WRAPPER"], "")
            self.assertEqual(calls[0]["env"]["RUSTC_WRAPPER"], "")
            self.assertEqual(calls[0]["env"]["RUSTFLAGS"], "")


    def test_build_probe_preserves_roles_when_cargo_reuses_one_artifact_path(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            baseline = self.make_workspace(root, "baseline")
            candidate = self.make_workspace(root, "candidate")
            output_root = root / "out"
            output_root.mkdir()
            executable = root / "target" / "release" / "deps" / "perf_probe-shared"
            executable.parent.mkdir(parents=True)
            role_bytes = {
                "baseline": b"#!/bin/sh\n# baseline\n",
                "candidate": b"#!/bin/sh\n# candidate\n",
            }

            def fake_build(role, workspace, build_root, build_log, *, release=True, toolchain=None):
                executable.write_bytes(role_bytes[role])
                executable.chmod(0o755)
                build_log.write_text(
                    json.dumps(
                        {
                            "reason": "compiler-artifact",
                            "target": {"name": "perf_probe", "kind": ["test"]},
                            "filenames": [str(executable)],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0)

            with mock.patch.object(LOOP, "stream_build_command", fake_build):
                baseline_path = LOOP.build_probe(
                    baseline, "baseline", output_root, release=True
                )
                candidate_path = LOOP.build_probe(
                    candidate, "candidate", output_root, release=True
                )
            self.assertNotEqual(baseline_path, candidate_path)
            self.assertEqual(Path(baseline_path).read_bytes(), role_bytes["baseline"])
            self.assertEqual(Path(candidate_path).read_bytes(), role_bytes["candidate"])
            self.assertEqual(executable.read_bytes(), role_bytes["candidate"])


    def test_preserve_built_probe_reuses_only_matching_content_address(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            executable = Path(directory) / "perf_probe-exact"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)

            first = Path(LOOP.preserve_built_probe(executable))
            second = Path(LOOP.preserve_built_probe(executable))
            self.assertEqual(first, second)

            first.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "content address"):
                LOOP.preserve_built_probe(executable)

    def test_build_probe_rejects_success_without_an_executable(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            output_root.mkdir()

            def fake_run(command, cwd=None, env=None, stdout=None, stderr=None):
                # Cargo succeeds but never emits a perf_probe test artifact.
                stdout.write(
                    json.dumps(
                        {
                            "reason": "compiler-artifact",
                            "target": {"name": "netem_test", "kind": ["lib"]},
                            "filenames": [str(root / "libnetem_test.rlib")],
                        }
                    ).encode()
                    + b"\n"
                )
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.object(LOOP.subprocess, "run", fake_run):
                with self.assertRaises(ValueError):
                    LOOP.build_probe(workspace, "candidate", output_root, release=True)

            # A listed executable that does not exist on disk is also rejected.
            ghost = root / "ghost-perf_probe"

            def fake_run_ghost(command, cwd=None, env=None, stdout=None, stderr=None):
                stdout.write(
                    json.dumps(
                        {
                            "reason": "compiler-artifact",
                            "target": {"name": "perf_probe", "kind": ["test"]},
                            "filenames": [str(ghost)],
                        }
                    ).encode()
                    + b"\n"
                )
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.object(LOOP.subprocess, "run", fake_run_ghost):
                with self.assertRaises(ValueError):
                    LOOP.build_probe(workspace, "candidate", output_root, release=True)

    def test_cargo_test_executable_ignores_non_probe_artifacts(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            log_path = Path(directory) / "build-candidate.log"
            log_path.write_text(
                "\n".join(
                    [
                        "not json at all",
                        json.dumps(
                            {
                                "reason": "compiler-artifact",
                                "target": {"name": "netem_test", "kind": ["lib"]},
                                "filenames": ["/tmp/libnetem_test.rlib"],
                            }
                        ),
                        json.dumps(
                            {
                                "reason": "compiler-artifact",
                                "target": {"name": "perf_probe", "kind": ["lib"]},
                                "filenames": ["/tmp/libperf_probe.rlib"],
                            }
                        ),
                        json.dumps(
                            {
                                "reason": "compiler-artifact",
                                "target": {"name": "perf_probe", "kind": ["test"]},
                                "filenames": ["/tmp/perf_probe-abc123"],
                            }
                        ),
                        json.dumps({"reason": "build-finished", "success": True}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                LOOP.cargo_test_executable(log_path), "/tmp/perf_probe-abc123"
            )


    def test_prebuilt_probe_is_validated_without_a_build(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            executable = Path(directory) / "perf_probe-exact"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)

            self.assertEqual(
                LOOP.use_prebuilt_probe(executable, "baseline"),
                str(executable.resolve()),
            )
            self.assertEqual(
                LOOP.executable_sha256(executable),
                hashlib.sha256(executable.read_bytes()).hexdigest(),
            )
            with self.assertRaises(ValueError):
                LOOP.use_prebuilt_probe(Path(directory) / "missing", "candidate")

    def test_run_parser_accepts_fec_and_retransmission_armor_flags(self):
        parser = LOOP.build_parser()
        fec_armor = parser.parse_args(
            ["run", "--baseline", "/suite/b", "--candidate", "/suite/c",
             "--fec", "--retransmission-armor"]
        )
        self.assertTrue(fec_armor.fec)
        self.assertTrue(fec_armor.retransmission_armor)
        plain = parser.parse_args(["run", "--baseline", "/suite/b", "--candidate", "/suite/c"])
        self.assertFalse(plain.fec)
        self.assertFalse(plain.retransmission_armor)

    def test_call_compare_requests_report_only_and_the_fec_allowlist(self):
        captured = {}

        def fake_run(command, capture_output=True, text=True):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(LOOP.subprocess, "run", fake_run):
            LOOP.call_compare(
                [("base-11", "/traces/b")],
                [("cand-11", "/traces/c")],
                Path("/safe/out"),
                ("fec",),
            )
        command = captured["command"]
        self.assertEqual(command[0], "python3")
        self.assertEqual(command[1], str(TOOLS.with_name("rtp_trace_compare.py")))
        self.assertIn("base-11=/traces/b", command)
        self.assertIn("cand-11=/traces/c", command)
        self.assertEqual(command[command.index("--allow-config-mismatch") + 1], "fec")
        # The loop enforces the evidence contract itself, so it always asks
        # the raw tool for the artifacts rather than fail on its exit code.
        self.assertIn("--report-only", command)

    def test_call_compare_forwards_declared_config_field_deltas(self):
        captured = {}

        def fake_run(command, capture_output=True, text=True):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(LOOP.subprocess, "run", fake_run):
            LOOP.call_compare(
                [("b", "/b")],
                [("c", "/c")],
                Path("/safe/out"),
                ("fec",),
                {
                    "netem_s2c.loss_model.key_offset": 10,
                    "netem_c2s.loss_model.key_offset": 10,
                },
            )
        command = captured["command"]
        fields = [
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--allow-config-field"
        ]
        self.assertEqual(
            fields,
            [
                "netem_c2s.loss_model.key_offset=10",
                "netem_s2c.loss_model.key_offset=10",
            ],
        )

    def test_call_compare_sorts_and_deduplicates_the_allowlist(self):
        captured = {}

        def fake_run(command, capture_output=True, text=True):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(LOOP.subprocess, "run", fake_run):
            LOOP.call_compare(
                [("b", "/b")],
                [("c", "/c")],
                Path("/safe/out"),
                ("instream_group_fec", "fec", "fec"),
            )
        command = captured["command"]
        keys = [
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--allow-config-mismatch"
        ]
        self.assertEqual(keys, ["fec", "instream_group_fec"])

    def test_run_parser_accepts_clean_link_and_rejects_nonpositive_mss(self):
        parser = LOOP.build_parser()
        snapshot = parser.parse_args(
            ["snapshot", "--source", "suite/netem_test",
             "--source-revision", "@-", "--output", "/safe/snapshot"]
        )
        self.assertEqual(snapshot.revision, "@-")
        override = parser.parse_args(
            ["snapshot", "--source", "suite/netem_test", "--output", "/safe/snapshot",
             "--component-revision", "rtp=abc", "--component-revision", "mux=def"]
        )
        self.assertEqual(override.component_revision, [("rtp", "abc"), ("mux", "def")])
        fake_parser = mock.Mock()
        fake_parser.parse_args.return_value = argparse.Namespace(handler=lambda _: 0)
        with mock.patch.object(LOOP, "build_parser", return_value=fake_parser):
            self.assertEqual(LOOP.main(["snapshot"]), 0)
        args = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--link-profile", "clean",
                "--mss-bytes", "1400",
                "--seeds", "11,21",
                "--window-seconds", "10",
                "--warmup-seconds", "2.5",
            ]
        )
        self.assertEqual(args.link_profile, "clean")
        self.assertEqual(args.mss_bytes, 1400)
        self.assertEqual(args.warmup_seconds, 2.5)
        stochastic_shaped = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--link-profile", "hostile-fat-pipe",
            ]
        )
        self.assertEqual(stochastic_shaped.link_profile, "hostile-fat-pipe")
        narrow_shaped = parser.parse_args(["run", "--baseline", "/suite/b", "--candidate", "/suite/c", "--link-profile", "lossy-400kib"]); self.assertEqual(narrow_shaped.link_profile, "lossy-400kib")
        controller_shaped = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--link-profile", "controller-fat-pipe",
            ]
        )
        self.assertEqual(controller_shaped.link_profile, "controller-fat-pipe")
        prebuilt = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--baseline-executable", "/bins/base",
                "--candidate-executable", "/bins/candidate",
            ]
        )
        self.assertEqual(prebuilt.baseline_executable, "/bins/base")
        self.assertEqual(prebuilt.candidate_executable, "/bins/candidate")
        paths = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/baseline",
                "--candidate", "/suite/candidate",
                "--target-dir", "/safe/target",
                "--output", "/safe/output",
            ]
        )
        self.assertEqual(paths.target_dir, Path("/safe/target"))
        self.assertEqual(paths.output, Path("/safe/output"))
        compare = parser.parse_args(
            [
                "compare",
                "--baseline", "11", "/suite/baseline-11",
                "--candidate", "11", "/suite/candidate-11",
                "--output", "/safe/comparison",
            ]
        )
        self.assertEqual(compare.output, Path("/safe/comparison"))
        # The direct lane and same-binary control options parse beside them.
        direct = parser.parse_args(
            [
                "run",
                "--baseline", "/tmp/b",
                "--candidate", "/tmp/b",
                "--link-profile", "direct",
                "--mss-bytes", "1400",
                "--window-seconds", "10",
                "--seeds", "11,21",
                "--same-binary-control",
                "--fail-on-control-instability",
            ]
        )
        self.assertEqual(direct.link_profile, "direct")
        self.assertTrue(direct.same_binary_control)
        self.assertTrue(direct.fail_on_control_instability)
        self.assertEqual(direct.warmup_seconds, LOOP.DEFAULT_WARMUP_SECONDS)

        # A nonpositive MSS must exit (SystemExit from argparse error) for MSS 0.
        with self.assertRaises(SystemExit) as context:
            LOOP.main(
                [
                    "run",
                    "--baseline", "/suite/b",
                    "--candidate", "/suite/c",
                    "--mss-bytes", "0",
                    "--seeds", "11,21",
                ]
            )
        self.assertNotEqual(context.exception.code, 0)
        with self.assertRaises(SystemExit) as context:
            LOOP.main(
                [
                    "run",
                    "--baseline", "/suite/b",
                    "--candidate", "/suite/c",
                    "--warmup-seconds", "-0.1",
                ]
            )
        self.assertNotEqual(context.exception.code, 0)

    def test_probe_source_manifest_is_bound_to_exact_executable_bytes(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            executable = root / "perf_probe-exact"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)
            components = {component: "a" * 40 for component in LOOP.COMPONENTS}
            manifest_path = LOOP.write_probe_source_manifest(
                root / "probe-source.json", executable, components, "suite/netem_test"
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], LOOP.PROBE_SOURCE_MANIFEST_SCHEMA)
            self.assertEqual(
                payload["executable_sha256"], LOOP.executable_sha256(executable)
            )
            # Loading validates the bound hash and returns the exact revisions.
            self.assertEqual(
                LOOP.load_probe_source_manifest(manifest_path, executable, "baseline"),
                components,
            )
            # A same-workspace treatment writes role-specific manifests
            # against the SAME frozen executable bytes.
            baseline_manifest = LOOP.write_probe_source_manifest(
                root / "baseline-probe-source.json",
                executable,
                components,
                "suite/netem_test",
            )
            candidate_manifest = LOOP.write_probe_source_manifest(
                root / "candidate-probe-source.json",
                executable,
                components,
                "suite/netem_test",
            )
            self.assertEqual(
                json.loads(baseline_manifest.read_text(encoding="utf-8"))[
                    "executable_sha256"
                ],
                json.loads(candidate_manifest.read_text(encoding="utf-8"))[
                    "executable_sha256"
                ],
            )
            # Different bytes on the same path must fail the SHA-256 binding.
            executable.write_bytes(b"#!/bin/sh\n# changed\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                LOOP.load_probe_source_manifest(manifest_path, executable, "baseline")

    def test_within_run_phase_drift_makes_a_same_binary_control_unstable(self):
        comparison = {
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 3.0}},
                }
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 8.0,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 8.0,
                    },
                },
            ],
        }
        phase = LOOP.within_run_phase_analysis(comparison)
        self.assertEqual(phase["classification"], "unstable_phase_drift")
        self.assertEqual(phase["material_run_count"], 2)
        self.assertEqual(phase["valid_runs"], 2)
        self.assertEqual(phase["median_absolute_shift_percent"], 20.0)
        self.assertEqual(phase["max_absolute_shift_percent"], 20.0)
        self.assertIn("does_not_prove", phase["does_not_prove"])
        # A paired goodput delta below the 10% material threshold is still
        # unstable as a control because both arms drifted a material 20%
        # between their own first and second halves.
        calibration = LOOP.control_calibration(comparison)
        self.assertEqual(calibration["classification"], "unstable")
        self.assertEqual(calibration["paired_classification"], "stable")
        self.assertEqual(
            calibration["within_run_phase_analysis"]["classification"],
            "unstable_phase_drift",
        )

    def test_role_local_phase_drift_is_reported_for_both_roles(self):
        comparison = {
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 3.0}},
                }
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 8.0,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 8.0,
                    },
                },
            ],
        }
        phase = LOOP.within_run_phase_analysis(comparison)
        self.assertEqual(phase["classification"], "unstable_phase_drift")
        self.assertEqual(phase["material_run_count"], 2)
        self.assertEqual(phase["valid_runs"], 2)
        self.assertEqual(phase["median_absolute_shift_percent"], 20.0)
        self.assertEqual(phase["max_absolute_shift_percent"], 20.0)
        self.assertIn("does_not_prove", phase["does_not_prove"])
        self.assertEqual(
            phase["by_role"]["baseline"]["classification"],
            "unstable_phase_drift",
        )
        self.assertEqual(
            phase["by_role"]["candidate"]["classification"],
            "unstable_phase_drift",
        )
        self.assertEqual(
            phase["by_role"]["baseline"]["material_run_count"],
            1,
        )
        self.assertEqual(
            phase["by_role"]["candidate"]["material_run_count"],
            1,
        )

    def test_phase_drift_failure_names_the_property_on_deterministic_lanes(self):
        unstable = {
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 3.0}},
                }
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 7.5,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 7.5,
                    },
                },
            ],
        }
        readiness = LOOP.paired_result_analysis(unstable, [])["comparison_readiness"]
        self.assertEqual(readiness["classification"], "not_ready")
        failure = LOOP.phase_drift_failure(readiness, "controller-fat-pipe")
        self.assertIsNotNone(failure)
        self.assertIn("controller-fat-pipe midpoint phase assertion", failure)
        self.assertIn("within_run_phase_not_stable", failure)
        self.assertIn("inconclusive", failure)
        # A ready capture never trips the phase assertion.
        stable = {
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 3.0}},
                }
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 9.5,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 9.5,
                    },
                },
            ],
        }
        stable_readiness = LOOP.paired_result_analysis(stable, [])["comparison_readiness"]
        self.assertIsNone(LOOP.phase_drift_failure(stable_readiness, "controller-fat-pipe"))

    def test_analyze_exits_nonzero_on_phase_drift_with_the_flag(self):
        comparison = {
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 3.0}},
                }
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 7.5,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 7.5,
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            result_dir = Path(directory) / "result"
            result_dir.mkdir()
            (result_dir / "comparison.json").write_text(
                json.dumps(comparison), encoding="utf-8"
            )
            (result_dir / "run.json").write_text(
                json.dumps(
                    {
                        "runs": [],
                        "link_profile": "controller-fat-pipe",
                        "same_binary_control": True,
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                result=str(result_dir),
                update_run_json=False,
                fail_on_phase_drift=False,
            )
            self.assertEqual(LOOP.command_analyze(args), 0)
            args.fail_on_phase_drift = True
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(LOOP.command_analyze(args), 2)
            self.assertIn("controller-fat-pipe midpoint phase assertion", err.getvalue())

    def test_same_binary_run_records_and_can_fail_control_analysis(self):
        comparison = {
            "verdict": "no_material_change",
            "evidence_quality": "healthy",
            "pairs": [
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": -4.0}},
                },
                {
                    "valid": True,
                    "metrics": {"goodput_mib_per_second": {"delta_percent": 12.5}},
                },
            ],
            "runs": [
                {
                    "label": "same-11",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 10.0,
                    },
                },
                {
                    "label": "same-11",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 10.0,
                    },
                },
                {
                    "label": "same-21",
                    "role": "baseline",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 10.0,
                    },
                },
                {
                    "label": "same-21",
                    "role": "candidate",
                    "summary": {
                        "goodput_first_half_mib_per_second": 10.0,
                        "goodput_second_half_mib_per_second": 10.0,
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            executable = root / "bin" / "perf_probe-frozen"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            args = argparse.Namespace(
                mss_bytes=8192,
                fec=False,
                retransmission_armor=False,
                candidate=str(workspace),
                baseline=str(workspace),
                same_binary_control=True,
                same_workspace_treatment=False,
                candidate_fec="same",
                instream_group_fec=False,
                scenario="bulk",
                fail_on_control_instability=True,
                fail_on_regression=False,
                fail_on_phase_drift=False,
                seeds=(11, 21),
                window_seconds=10,
                label=None,
                warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                baseline_executable=None,
                release=True,
                target_dir=None,
                candidate_executable=None,
                baseline_source_manifest=None,
                candidate_source_manifest=None,
                output=str(output_root),
                link_profile="direct",
            )

            def fake_build_probe(workspace, role, output_root, *, release=True, target_dir=None, toolchain=None):
                return str(executable.resolve())

            def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                return {
                    "runner_exit": 0,
                    "role": role,
                    "seed": str(seed),
                    "executable": kwargs["executable"],
                    "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                    "fec": "true" if kwargs["fec"] else "false",
                    "scenario": kwargs["scenario"],
                }

            def fake_call_compare(
                baseline_dirs,
                candidate_dirs,
                output_root,
                allowed_config_mismatches=(),
                allowed_config_fields=None,
            ):
                (Path(output_root) / "comparison.json").write_text(
                    json.dumps(comparison), encoding="utf-8"
                )
                return subprocess.CompletedProcess([], 0, b"", b"")

            with mock.patch.object(LOOP, "build_probe", fake_build_probe), mock.patch.object(
                LOOP, "run_probe", fake_run_probe
            ), mock.patch.object(LOOP, "call_compare", fake_call_compare):
                exit_code = LOOP.command_run(args)
            self.assertEqual(exit_code, 4)
            run_json = json.loads(
                (output_root / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_json["control_calibration"]["classification"], "unstable")
            self.assertEqual(
                run_json["control_calibration"]["within_run_phase_analysis"]["classification"],
                "stable",
            )
            # The phase analysis is written into every run.json as its own key.
            self.assertEqual(
                run_json["within_run_phase_analysis"]["classification"], "stable"
            )
            self.assertEqual(run_json["within_run_phase_analysis"]["valid_runs"], 4)
            self.assertEqual(run_json["within_run_phase_analysis"]["material_run_count"], 0)
            # Execution-order and AB/BA analyses remain separate keys.
            self.assertIn("execution_order_analysis", run_json)
            self.assertIn("counterbalanced_goodput_analysis", run_json)
            self.assertEqual(
                run_json["builds"]["baseline"]["source"], "built"
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["source_manifest"],
                str((output_root / "baseline-probe-source.json").resolve()),
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["components_source"],
                "build_workspace",
            )
            self.assertTrue(
                (output_root / "baseline-probe-source.json").is_file()
            )

    def test_same_workspace_fec_treatment_builds_once_and_reuses_exact_binary(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            executable = root / "bin" / "perf_probe-frozen"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            build_roles = []
            toolchain_pins = []
            run_calls = []
            compare_allowlists = []

            def fake_build_probe(workspace, role, output_root, *, release=True, target_dir=None, toolchain=None):
                build_roles.append(role)
                toolchain_pins.append(toolchain)
                return str(executable.resolve())

            def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                run_calls.append(
                    {
                        "role": role,
                        "seed": seed,
                        "fec": kwargs["fec"],
                        "scenario": kwargs["scenario"],
                        "instream_group_fec": kwargs["instream_group_fec"],
                    }
                )
                return {
                    "runner_exit": 0,
                    "role": role,
                    "seed": str(seed),
                    "executable": kwargs["executable"],
                    "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                    "fec": "true" if kwargs["fec"] else "false",
                    "scenario": kwargs["scenario"],
                }

            def fake_call_compare(
                baseline_dirs,
                candidate_dirs,
                output_root,
                allowed_config_mismatches=(),
                allowed_config_fields=None,
            ):
                compare_allowlists.append(tuple(sorted(allowed_config_mismatches)))
                (Path(output_root) / "comparison.json").write_text(
                    json.dumps(
                        {
                            "evidence_quality": "healthy",
                            "verdict": "no_material_change",
                            "runs": [],
                            "pairs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, b"", b"")

            args = argparse.Namespace(
                mss_bytes=8192,
                fec=False,
                retransmission_armor=False,
                candidate=str(workspace),
                baseline=str(workspace),
                same_binary_control=False,
                same_workspace_treatment=True,
                candidate_fec="on",
                instream_group_fec=False,
                scenario="bulk",
                fail_on_control_instability=False,
                fail_on_regression=False,
                fail_on_phase_drift=False,
                seeds=(11, 21),
                window_seconds=10,
                label=None,
                warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                baseline_executable=None,
                release=True,
                target_dir=None,
                candidate_executable=None,
                baseline_source_manifest=None,
                candidate_source_manifest=None,
                output=str(output_root),
                link_profile="fec-paired-saturated",
            )

            # The invocation-site toolchain pin is honoured from the
            # environment and forwarded to the (frozen) build: the snapshot
            # workspaces live outside the source tree, where rustup would not
            # find its rust-toolchain.toml, so the pin must ride through to
            # the cargo invocation or the probe could be compiled with a
            # different rustc than the in-tree gates use.
            previous_toolchain = os.environ.get("RUSTUP_TOOLCHAIN")
            os.environ["RUSTUP_TOOLCHAIN"] = "it180-test-pin"
            try:
                with mock.patch.object(LOOP, "build_probe", fake_build_probe), mock.patch.object(
                    LOOP, "run_probe", fake_run_probe
                ), mock.patch.object(LOOP, "call_compare", fake_call_compare):
                    exit_code = LOOP.command_run(args)
            finally:
                if previous_toolchain is None:
                    os.environ.pop("RUSTUP_TOOLCHAIN", None)
                else:
                    os.environ["RUSTUP_TOOLCHAIN"] = previous_toolchain
            self.assertEqual(exit_code, 0)
            # The treatment builds and freezes ONE executable before timing;
            # baseline and candidate are never compiled separately.
            self.assertEqual(build_roles, ["treatment"])
            self.assertEqual(toolchain_pins, ["it180-test-pin"])
            self.assertEqual(compare_allowlists, [("fec",)])
            baseline_fec = [call["fec"] for call in run_calls if call["role"] == "baseline"]
            candidate_fec = [call["fec"] for call in run_calls if call["role"] == "candidate"]
            self.assertEqual(baseline_fec, [False, False])
            self.assertEqual(candidate_fec, [True, True])
            self.assertEqual(len(run_calls), 4)
            run_json = json.loads(
                (output_root / "run.json").read_text(encoding="utf-8")
            )
            self.assertTrue(run_json["same_workspace_treatment"])
            self.assertEqual(run_json["scenario"], "bulk")
            self.assertEqual(run_json["toolchain_pin"], "it180-test-pin")
            self.assertEqual(run_json["candidate_fec"], "on")
            self.assertFalse(run_json["instream_group_fec"])
            self.assertEqual(run_json["allowed_config_mismatches"], ["fec"])
            # The paired-saturated treatment also declares the exact netem leaf
            # the FEC envelope shifts, so the lane can yield a verdict without
            # a blanket lane exemption.
            self.assertEqual(
                run_json["allowed_config_fields"],
                [
                    "netem_c2s.loss_model.key_offset=10",
                    "netem_s2c.loss_model.key_offset=10",
                ],
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["executable"],
                str(executable.resolve()),
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["sha256"],
                run_json["builds"]["candidate"]["sha256"],
            )
            self.assertEqual(
                run_json["builds"]["baseline"]["log"],
                str((output_root / "build-treatment.log").resolve()),
            )
            self.assertEqual(
                run_json["builds"]["candidate"]["log"],
                str((output_root / "build-treatment.log").resolve()),
            )
            # Role-specific source manifests bind the same executable bytes,
            # and every timed run reuses that exact frozen binary.
            self.assertTrue((output_root / "baseline-probe-source.json").is_file())
            self.assertTrue((output_root / "candidate-probe-source.json").is_file())
            self.assertTrue(
                all(
                    run["executable"] == str(executable.resolve())
                    for run in run_json["runs"]
                )
            )
            self.assertTrue(
                all(run["fec"] == "true" for run in run_json["runs"] if run["role"] == "candidate")
            )
            self.assertTrue(
                all(run["fec"] == "false" for run in run_json["runs"] if run["role"] == "baseline")
            )
            # The per-role manifest.csv records the scenario and FEC evidence.
            manifest_rows = (output_root / "manifest.csv").read_text(encoding="utf-8")
            self.assertIn("scenario", manifest_rows)
            self.assertIn("candidate_fec", manifest_rows)

    def test_treatment_declares_only_the_netem_leaf_the_fec_envelope_shifts(self):
        def args(**overrides):
            values = dict(
                fec=False,
                candidate_fec="on",
                instream_group_fec=False,
                link_profile="fec-paired-saturated",
            )
            values.update(overrides)
            return argparse.Namespace(**values)

        self.assertEqual(
            LOOP.treatment_config_field_differences(args()),
            {
                "netem_c2s.loss_model.key_offset": LOOP.FEC_DATA_ENVELOPE_BYTES,
                "netem_s2c.loss_model.key_offset": LOOP.FEC_DATA_ENVELOPE_BYTES,
            },
        )
        # Turning FEC off on the candidate shifts the key the other way.
        self.assertEqual(
            LOOP.treatment_config_field_differences(
                args(fec=True, candidate_fec="off")
            ),
            {
                "netem_c2s.loss_model.key_offset": -LOOP.FEC_DATA_ENVELOPE_BYTES,
                "netem_s2c.loss_model.key_offset": -LOOP.FEC_DATA_ENVELOPE_BYTES,
            },
        )
        # A lane whose netem loss does not key on the envelope declares
        # nothing, and a treatment that does not move FEC declares nothing.
        self.assertEqual(
            LOOP.treatment_config_field_differences(
                args(link_profile="fec-gaming-fat-pipe")
            ),
            {},
        )
        self.assertEqual(
            LOOP.treatment_config_field_differences(args(candidate_fec="same")),
            {},
        )

    def test_parser_accepts_component_revisions_scenarios_and_treatments(self):
        parser = LOOP.build_parser()
        self.assertEqual(LOOP.parse_component_revision("rtp=abc"), ("rtp", "abc"))
        self.assertEqual(
            LOOP.parse_component_revision("rtp=rev=with=equals"),
            ("rtp", "rev=with=equals"),
        )
        for invalid in ("rtp", "=abc", "rtp=", ""):
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "COMPONENT=REVISION"):
                LOOP.parse_component_revision(invalid)
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "unknown suite component"):
            LOOP.parse_component_revision("missing=abc")
        snapshot = parser.parse_args(
            ["snapshot", "--source", "suite/netem_test", "--output", "/safe/snapshot",
             "--component-revision", "rtp=abc", "--component-revision", "mux=def"]
        )
        self.assertEqual(snapshot.component_revision, [("rtp", "abc"), ("mux", "def")])
        treatment = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--scenario", "message-latency",
                "--candidate-fec", "on",
                "--instream-group-fec",
                "--same-workspace-treatment",
            ]
        )
        self.assertEqual(treatment.scenario, "message-latency")
        self.assertEqual(treatment.candidate_fec, "on")
        self.assertTrue(treatment.instream_group_fec)
        self.assertTrue(treatment.same_workspace_treatment)
        defaults = parser.parse_args(["run", "--baseline", "/b", "--candidate", "/c"])
        self.assertEqual(defaults.scenario, "bulk")
        self.assertEqual(defaults.candidate_fec, "same")
        self.assertFalse(defaults.instream_group_fec)
        self.assertFalse(defaults.same_workspace_treatment)
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--baseline", "/b", "--candidate", "/c", "--scenario", "bogus"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--baseline", "/b", "--candidate", "/c", "--candidate-fec", "bogus"])
        source_manifests = parser.parse_args(
            [
                "run",
                "--baseline", "/suite/b",
                "--candidate", "/suite/c",
                "--baseline-source-manifest", "/safe/base.json",
                "--candidate-source-manifest", "/safe/cand.json",
            ]
        )
        self.assertEqual(source_manifests.baseline_source_manifest, Path("/safe/base.json"))
        self.assertEqual(source_manifests.candidate_source_manifest, Path("/safe/cand.json"))

    def test_treatment_rejects_every_invalid_combination(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            other = self.make_workspace(root, "other")

            def make_args(**overrides):
                values = dict(
                    mss_bytes=8192,
                    fec=False,
                    retransmission_armor=False,
                    same_binary_control=False,
                    same_workspace_treatment=False,
                    candidate_fec="same",
                    instream_group_fec=False,
                    scenario="bulk",
                    fail_on_control_instability=False,
                    fail_on_regression=False,
                fail_on_phase_drift=False,
                    seeds=(11,),
                    window_seconds=10,
                    label=None,
                    warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                    baseline_executable=None,
                    release=True,
                    target_dir=None,
                    candidate_executable=None,
                    baseline_source_manifest=None,
                    candidate_source_manifest=None,
                    output=str(root / "out"),
                    link_profile="fec-paired-saturated",
                )
                values.update(overrides)
                return argparse.Namespace(**values)

            # A treatment cannot ride a same-binary control.
            with self.assertRaisesRegex(SystemExit, "same-binary-control"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(workspace),
                        same_binary_control=True,
                        same_workspace_treatment=True,
                        candidate_fec="on",
                    )
                )
            # A treatment cannot cross distinct workspaces.
            with self.assertRaisesRegex(SystemExit, "identical resolved workspaces"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(other),
                        same_workspace_treatment=True,
                        candidate_fec="on",
                    )
                )
            # A treatment needs an explicit role difference.
            with self.assertRaisesRegex(SystemExit, "explicit role difference"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(workspace),
                        same_workspace_treatment=True,
                        candidate_fec="same",
                    )
                )
            # Requested but not actual: --candidate-fec on with --fec already on
            # leaves no runtime difference to treat.
            with self.assertRaisesRegex(SystemExit, "explicit role difference"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(workspace),
                        same_workspace_treatment=True,
                        candidate_fec="on",
                        fec=True,
                    )
                )
            # A runtime FEC difference outside treatment mode is confounded
            # across distinct workspaces or binaries.
            with self.assertRaisesRegex(SystemExit, "requires --same-workspace-treatment"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(other),
                        candidate_fec="on",
                    )
                )
            # Prebuilt role executables break the single-binary treatment contract.
            with self.assertRaisesRegex(SystemExit, "builds and freezes one executable"):
                LOOP.command_run(
                    make_args(
                        baseline=str(workspace),
                        candidate=str(workspace),
                        same_workspace_treatment=True,
                        candidate_fec="on",
                        baseline_executable="/bin/base",
                        candidate_executable="/bin/cand",
                    )
                )

    def test_suite_revisions_rejects_components_at_the_same_commit(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            for component in LOOP.COMPONENTS:
                (root / component).mkdir(parents=True, exist_ok=True)
            identity = {"commit_id": "c" * 40, "change_id": "d" * 32}

            def fake_jj_revision(directory, revision="@"):
                return identity

            with mock.patch.object(LOOP, "jj_revision", fake_jj_revision):
                with self.assertRaisesRegex(ValueError, "share the same commit_id"):
                    LOOP.component_revisions(workspace)
            # An 'unknown' identity (missing sibling) is not a collision: the
            # rejection only fires for two KNOWN components at the same commit.
            def fake_jj_unknown(directory, revision="@"):
                return {"commit_id": "unknown", "change_id": "unknown"}

            with mock.patch.object(LOOP, "jj_revision", fake_jj_unknown):
                revisions = LOOP.component_revisions(workspace)
            self.assertEqual(set(revisions), set(LOOP.COMPONENTS))

    def test_prune_built_role_target_keeps_frozen_probe(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            target = root / "target"
            frozen_dir = root / "frozen"
            deps = target / "release" / "deps"
            deps.mkdir(parents=True)
            executable = deps / "perf_probe-built"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)
            frozen = LOOP.preserve_built_probe(executable, frozen_dir)
            self.assertTrue(Path(frozen).is_file())
            # The frozen copy lives outside the target, so pruning the target
            # is allowed and leaves the frozen executable intact.
            LOOP.prune_built_role_target(target, frozen)
            self.assertFalse(target.exists())
            self.assertTrue(Path(frozen).is_file())

    def test_prune_built_role_target_rejects_executable_inside_target(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            target = root / "target"
            deps = target / "release" / "deps"
            deps.mkdir(parents=True)
            executable = deps / "perf_probe-inside"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "inside it"):
                LOOP.prune_built_role_target(target, str(executable.resolve()))
            self.assertTrue(target.is_dir())
            self.assertTrue(executable.is_file())


    def test_comparison_readiness_separates_blockers_from_order_cautions(self):
        healthy = {"evidence_quality": "healthy", "runs": []}
        phase = {"classification": "stable"}
        order = {"directionally_confounded": True}
        counterbalanced = {"complete_blocks": 2}
        readiness = LOOP.comparison_readiness(
            healthy, phase, order, counterbalanced
        )
        self.assertEqual(readiness["classification"], "ready")
        self.assertEqual(readiness["blocking_reasons"], [])
        self.assertEqual(
            readiness["cautions"], ["directional_execution_order_effect"]
        )
        # Order association is a caution, not a blocker.
        blocked = LOOP.comparison_readiness(
            {"evidence_quality": "degraded", "runs": []},
            {"classification": "unstable_phase_drift"},
            order,
            {"complete_blocks": 1},
        )
        self.assertEqual(blocked["classification"], "not_ready")
        self.assertIn("trace_evidence_not_healthy", blocked["blocking_reasons"])
        self.assertIn(
            "fewer_than_two_counterbalanced_blocks", blocked["blocking_reasons"]
        )
        self.assertIn("within_run_phase_not_stable", blocked["blocking_reasons"])
        self.assertIn(
            "does_not_prove", readiness["does_not_prove"].lower()
        )

    def test_lane_roles_match_the_documented_gate_block(self):
        # `hostile` is diagnostic-only: it was `not_ready`
        # (`within_run_phase_not_stable`) in all 70 recorded runs.
        self.assertEqual(LOOP.lane_classification("hostile"), "diagnostic")
        self.assertIn("hostile", LOOP.DIAGNOSTIC_LANES)
        self.assertLessEqual(set(LOOP.DIAGNOSTIC_LANES), set(LOOP.LINK_PROFILES))
        for profile in LOOP.LINK_PROFILES:
            expected = "diagnostic" if profile in LOOP.DIAGNOSTIC_LANES else "verdict"
            self.assertEqual(LOOP.lane_classification(profile), expected)
        self.assertEqual(LOOP.lane_classification("clean"), "verdict")

        gate_md = (Path(__file__).resolve().parent.parent / "tests" / "GATE.md").read_text(
            encoding="utf-8"
        )
        match = re.search(r"```gate-lane-roles\n(.*?)```", gate_md, re.S)
        self.assertIsNotNone(match, "tests/GATE.md must carry a gate-lane-roles block")
        documented = dict(
            line.split(" = ", 1)
            for line in (raw.strip() for raw in match.group(1).splitlines())
            if line and not line.startswith("#")
        )
        self.assertEqual(
            documented,
            {profile: LOOP.lane_classification(profile) for profile in LOOP.LINK_PROFILES},
            "the documented perf-loop lane roles must match lane_classification",
        )
        self.assertEqual(documented["hostile"], "diagnostic")

    def test_analyze_existing_result_recomputes_and_optionally_updates_run_json(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            result_dir = Path(directory)
            comparison = {
                "evidence_quality": "healthy",
                "verdict": "no_material_change",
                "runs": [],
                "pairs": [],
            }
            (result_dir / "comparison.json").write_text(
                json.dumps(comparison), encoding="utf-8"
            )
            run_json = {
                "runs": [
                    {"role": "baseline", "seed": 11},
                    {"role": "candidate", "seed": 11},
                ],
                "same_binary_control": True,
            }
            (result_dir / "run.json").write_text(
                json.dumps(run_json), encoding="utf-8"
            )
            report = LOOP.command_analyze(
                argparse.Namespace(
                    result=str(result_dir),
                    update_run_json=False,
                    fail_on_phase_drift=False,
                )
            )
            self.assertEqual(report, 0)
            stored = json.loads(
                (result_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("comparison_readiness", stored)
            # With --update-run-json the recomputed analysis is written back.
            LOOP.command_analyze(
                argparse.Namespace(
                    result=str(result_dir),
                    update_run_json=True,
                    fail_on_phase_drift=False,
                )
            )
            updated = json.loads(
                (result_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertIn("comparison_readiness", updated)
            self.assertIn("within_run_phase_analysis", updated)
            self.assertIn("execution_order_analysis", updated)
            self.assertIn("counterbalanced_goodput_analysis", updated)

    def test_analyze_existing_result_rejects_paths_outside_safe_root(self):
        outside = LOOP.SAFE_TEMP_ROOT.parent.parent / "perf-outside-safe-root-test"
        with self.assertRaisesRegex(ValueError, "beneath"):
            LOOP.checked_result_dir(str(outside))

    def test_within_run_phase_drift_tolerates_rounding_at_threshold(self):
        def comparison(first, second):
            return {
                "runs": [
                    {
                        "label": "run",
                        "role": "baseline",
                        "summary": {
                            "goodput_first_half_mib_per_second": first,
                            "goodput_second_half_mib_per_second": second,
                        },
                    }
                ]
            }

        # Exactly at the 20% threshold: material.
        analysis = LOOP.within_run_phase_analysis(comparison(1.0, 1.2))
        self.assertEqual(analysis["classification"], "unstable_phase_drift")
        self.assertTrue(analysis["runs"][0]["material_phase_drift"])
        # Just below (well outside the 1e-12 tolerance): not material.
        analysis = LOOP.within_run_phase_analysis(comparison(1.0, 1.199999))
        self.assertEqual(analysis["classification"], "stable")
        self.assertFalse(analysis["runs"][0]["material_phase_drift"])
        # Floating-point rounding at the threshold (within isclose): material.
        analysis = LOOP.within_run_phase_analysis(
            comparison(1.0, 1.2000000000000002)
        )
        self.assertTrue(analysis["runs"][0]["material_phase_drift"])
        self.assertEqual(analysis["classification"], "unstable_phase_drift")
        # Non-positive second-half goodput is not accepted as evidence.
        analysis = LOOP.within_run_phase_analysis(comparison(1.0, 0.0))
        self.assertEqual(analysis["classification"], "insufficient_evidence")

    def test_compare_refuses_a_passing_exit_without_valid_pairs(self):
        def completed():
            return subprocess.CompletedProcess([], 0, b"", b"")

        def run_with(payload, missing=False):
            with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
                root = Path(directory)
                output_root = root / "out"
                args = argparse.Namespace(
                    mss_bytes=8192,
                    output=str(output_root),
                    baseline=[["11", str(root / "b")]],
                    candidate=[["11", str(root / "c")]],
                    fail_on_regression=False,
                fail_on_phase_drift=False,
                )

                def fake_call_compare(
                    baseline_dirs,
                    candidate_dirs,
                    output_root,
                    allowed_config_mismatches=(),
                    allowed_config_fields=None,
                ):
                    if not missing:
                        (Path(output_root) / "comparison.json").write_text(
                            json.dumps(payload), encoding="utf-8"
                        )
                    return completed()

                with mock.patch.object(LOOP, "call_compare", fake_call_compare):
                    return LOOP.command_compare(args)

        # Zero valid pairs with healthy traces is insufficient evidence, not a
        # pass.
        self.assertEqual(
            run_with(
                {
                    "verdict": "insufficient_evidence",
                    "evidence_quality": "healthy",
                    "valid_pairs": 0,
                    "total_pairs": 0,
                }
            ),
            2,
        )
        # A zero-exit comparison that wrote no comparison.json is the same
        # failure.
        self.assertEqual(run_with({}, missing=True), 2)
        # A genuine comparison still exits zero.
        self.assertEqual(
            run_with(
                {
                    "verdict": "no_material_change",
                    "evidence_quality": "healthy",
                    "valid_pairs": 2,
                    "total_pairs": 2,
                }
            ),
            0,
        )

    def test_run_refuses_a_passing_exit_without_valid_pairs(self):
        def run_with(payload, missing=False):
            with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
                root = Path(directory)
                workspace = self.make_workspace(root, "netem_test_base")
                candidate_workspace = self.make_workspace(root, "netem_test_cand")
                output_root = root / "out"
                executable = root / "bin" / "perf_probe-frozen"
                executable.parent.mkdir(parents=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                args = argparse.Namespace(
                    mss_bytes=8192,
                    fec=False,
                    retransmission_armor=False,
                    candidate=str(candidate_workspace),
                    baseline=str(workspace),
                    same_binary_control=False,
                    same_workspace_treatment=False,
                    candidate_fec="same",
                    instream_group_fec=False,
                    scenario="bulk",
                    fail_on_control_instability=False,
                    fail_on_regression=False,
                fail_on_phase_drift=False,
                    seeds=(11, 21),
                    window_seconds=10,
                    label=None,
                    warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                    baseline_executable=None,
                    release=True,
                    target_dir=None,
                    candidate_executable=None,
                    baseline_source_manifest=None,
                    candidate_source_manifest=None,
                    output=str(output_root),
                    link_profile="direct",
                )

                def fake_build_probe(
                    workspace, role, output_root, *, release=True, target_dir=None, toolchain=None
                ):
                    return str(executable.resolve())

                def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                    return {
                        "runner_exit": 0,
                        "role": role,
                        "seed": str(seed),
                        "executable": kwargs["executable"],
                        "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                        "fec": "true" if kwargs["fec"] else "false",
                        "scenario": kwargs["scenario"],
                    }

                def fake_call_compare(
                    baseline_dirs,
                    candidate_dirs,
                    output_root,
                    allowed_config_mismatches=(),
                    allowed_config_fields=None,
                ):
                    if not missing:
                        (Path(output_root) / "comparison.json").write_text(
                            json.dumps(payload), encoding="utf-8"
                        )
                    return subprocess.CompletedProcess([], 0, b"", b"")

                with mock.patch.object(LOOP, "build_probe", fake_build_probe), \
                        mock.patch.object(LOOP, "run_probe", fake_run_probe), \
                        mock.patch.object(LOOP, "call_compare", fake_call_compare):
                    return LOOP.command_run(args)

        self.assertEqual(
            run_with(
                {
                    "verdict": "insufficient_evidence",
                    "evidence_quality": "healthy",
                    "pairs": [],
                    "runs": [],
                }
            ),
            2,
        )
        self.assertEqual(run_with({}, missing=True), 2)



if __name__ == "__main__":
    unittest.main()
