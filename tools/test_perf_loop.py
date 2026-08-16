"""Tests for tools/perf_loop.py."""
import argparse
import hashlib
import importlib.util
import json
import os
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

            def fake_snapshot(component_source, component_output, revision):
                component_output.mkdir(parents=True)
                if component_output.name == "netem_test":
                    self.make_workspace(component_output.parent, "netem_test")
                return {
                    "commit_id": component_output.name[0].encode().hex().ljust(40, "0")[:40],
                    "change_id": component_output.name[0].ljust(32, "x"),
                }

            with mock.patch.object(LOOP, "snapshot_component", fake_snapshot):
                args = argparse.Namespace(
                    source=str(source), revision="@-", output=str(output)
                )
                self.assertEqual(LOOP.command_snapshot(args), 0)
            manifest = json.loads(
                (output / LOOP.SUITE_REVISION_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(set(manifest["components"]), set(LOOP.COMPONENTS))
            self.assertEqual(
                LOOP.jj_revision(output / "rtp"),
                manifest["components"]["rtp"]["commit_id"],
            )

    def test_same_binary_control_calibration_detects_false_changes(self):
        comparison = {
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
                candidate=str(workspace),
                baseline=str(workspace),
                same_binary_control=True,
                fail_on_control_instability=True,
                fail_on_regression=False,
                seeds=(11, 21),
                window_seconds=10,
                label=None,
                warmup_seconds=LOOP.DEFAULT_WARMUP_SECONDS,
                baseline_executable=None,
                release=True,
                target_dir=None,
                candidate_executable=None,
                output=str(output_root),
                link_profile="direct",
            )

            def fake_build_probe(workspace, role, output_root, *, release=True, target_dir=None):
                return str(executable.resolve())

            def fake_run_probe(workspace, seed, role, output_root, **kwargs):
                return {
                    "runner_exit": 0,
                    "role": role,
                    "seed": str(seed),
                    "executable": kwargs["executable"],
                    "trace_dir": str(output_root / f"trace-{role}-{seed}"),
                }

            def fake_call_compare(baseline_dirs, candidate_dirs, output_root):
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
                subprocess_runner=fake_run,
            )
            self.assertEqual(len(calls), 1)
            env = calls[0]["env"]
            self.assertEqual(env["NETEM_PERF_LINK_PROFILE"], "clean")
            self.assertEqual(env["NETEM_PERF_MSS_BYTES"], "1400")
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
            # with its resolved path and contains no cargo.
            command = calls[0]["command"]
            self.assertEqual(command[0], str(executable.resolve()))
            self.assertTrue(all("cargo" not in part for part in command))
            self.assertIn(LOOP.PERF_TEST, command)
            # Manifest component metadata is present and sorted, and the row
            # records the resolved executable.
            self.assertEqual(row["runner_exit"], 0)
            self.assertEqual(row["role"], "baseline")
            self.assertEqual(row["link_profile"], "clean")
            self.assertEqual(row["mss_bytes"], "1400")
            self.assertEqual(row["warmup_seconds"], "2.5")
            self.assertEqual(row["executable"], str(executable.resolve()))
            components = json.loads(row["components"])
            self.assertEqual(
                list(components.keys()),
                sorted(LOOP.COMPONENTS),
            )

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
            self.assertEqual(preserved.parent, executable.parent.resolve())
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

            def fake_build(role, workspace, build_root, build_log, *, release=True):
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

    def test_run_parser_accepts_clean_link_and_rejects_nonpositive_mss(self):
        parser = LOOP.build_parser()
        snapshot = parser.parse_args(
            ["snapshot", "--source", "suite/netem_test", "--revision", "@-"]
        )
        self.assertEqual(snapshot.revision, "@-")
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


if __name__ == "__main__":
    unittest.main()
