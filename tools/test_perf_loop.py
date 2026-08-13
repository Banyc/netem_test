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

    def test_run_probe_sets_diagnostic_and_safe_build_environment(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            workspace = self.make_workspace(root, "netem_test")
            output_root = root / "out"
            output_root.mkdir()
            calls = []

            def fake_run(command, cwd=None, env=None, stdout=None, stderr=None):
                calls.append({"command": command, "cwd": cwd, "env": env})
                return subprocess.CompletedProcess(command, 0, b"", b"")

            row = LOOP.run_probe(
                workspace,
                seed=11,
                role="baseline",
                output_root=output_root,
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
            self.assertIn("--release", calls[0]["command"])
            self.assertIn(LOOP.PERF_TEST, calls[0]["command"])
            # Manifest component metadata is present and sorted.
            self.assertEqual(row["runner_exit"], 0)
            self.assertEqual(row["role"], "baseline")
            self.assertEqual(row["link_profile"], "clean")
            self.assertEqual(row["mss_bytes"], "1400")
            components = json.loads(row["components"])
            self.assertEqual(
                list(components.keys()),
                sorted(LOOP.COMPONENTS),
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
