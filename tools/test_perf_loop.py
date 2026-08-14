"""Tests for tools/perf_loop.py."""

import argparse
import importlib.util
import json
import os
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
        self.assertEqual(LOOP.parse_seeds("11,21"), (11, 21))
        self.assertEqual(LOOP.parse_seeds(" 7 , 9 "), (7, 9))
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11,11")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11,abc")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("11,-1")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_seeds("0,18446744073709551616")
        self.assertEqual(LOOP.parse_label("run-a_1"), "run-a_1")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_label(".")
        with self.assertRaises(argparse.ArgumentTypeError):
            LOOP.parse_label("a/b")

        outside = Path(tempfile.mkdtemp(dir="/tmp")) / "outside"
        with self.assertRaises(ValueError):
            LOOP.safe_output_dir(outside)
        with self.assertRaises(ValueError):
            LOOP.safe_build_dir(outside, outside, "release")

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

        # A same-binary fixture with one >=10% absolute valid-pair delta
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
                baseline=str(workspace),
                candidate=str(workspace),
                same_binary_control=True,
                fail_on_control_instability=True,
                fail_on_regression=False,
                label=None,
                seeds=(11, 21),
                window_seconds=10,
                release=True,
                target_dir=None,
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
                mss_bytes=1400,
                window_seconds=10,
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
            self.assertEqual(path, str(executable.resolve()))
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
                str(calls[0]["env"]["CARGO_TARGET_DIR"]).startswith(str(LOOP.SAFE_TEMP_ROOT))
            )

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

    def test_run_parser_accepts_clean_link_and_rejects_nonpositive_mss(self):
        parser = LOOP.build_parser()
        args = parser.parse_args(
            [
                "run",
                "--baseline", "/tmp/b",
                "--candidate", "/tmp/c",
                "--link-profile", "clean",
                "--mss-bytes", "1400",
                "--seeds", "11,21",
                "--window-seconds", "10",
            ]
        )
        self.assertEqual(args.link_profile, "clean")
        self.assertEqual(args.mss_bytes, 1400)
        # The direct lane and same-binary control options parse beside them.
        direct = parser.parse_args(
            [
                "run",
                "--baseline", "/tmp/b",
                "--candidate", "/tmp/b",
                "--link-profile", "direct",
                "--mss-bytes", "1400",
                "--seeds", "11,21",
                "--window-seconds", "10",
                "--same-binary-control",
                "--fail-on-control-instability",
            ]
        )
        self.assertEqual(direct.link_profile, "direct")
        self.assertTrue(direct.same_binary_control)
        self.assertTrue(direct.fail_on_control_instability)
        # main must exit (SystemExit from argparse error) for MSS 0.
        with self.assertRaises(SystemExit) as context:
            LOOP.main(
                [
                    "run",
                    "--baseline", "/tmp/b",
                    "--candidate", "/tmp/c",
                    "--mss-bytes", "0",
                    "--seeds", "11,21",
                ]
            )
        self.assertNotEqual(context.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
