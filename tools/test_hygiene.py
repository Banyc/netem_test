#!/usr/bin/env python3
"""Tests for ``tools/hygiene.py``.

Run as ``python3 tools/test_hygiene.py``, in the style of the sibling suites
(``test_render_graph.py``, ``test_shape_report.py``, ...); pytest-style flags
such as ``-k`` do not work through this entry point.

Every check is exercised in both directions: the condition is *built* and the
check must fire, then it is removed and the check must stay silent.  A check
that cannot fire is worse than no check, so the "fires" half of each pair is
the point of the test, not the "stays silent" half.

Fixtures are built under a fresh ``mkdtemp`` directory (``$HYGIENE_TEST_TMPDIR``
when set, otherwise the platform temp directory), never against the host's real
crate tree, and are removed on teardown.
"""

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("hygiene.py")
SPEC = importlib.util.spec_from_file_location("hygiene", MODULE_PATH)
HYGIENE = importlib.util.module_from_spec(SPEC)
# Register before executing: dataclasses resolves annotations through
# sys.modules[cls.__module__] on recent Pythons.
sys.modules[SPEC.name] = HYGIENE
SPEC.loader.exec_module(HYGIENE)

JJ = shutil.which("jj")
GIT = shutil.which("git")
NEEDS_JJ = unittest.skipUnless(JJ and GIT, "jj and git are required")

# A process table with nothing under the crate root: the process check runs,
# and finds nothing.
UNRELATED_PROCESSES = """\
    1     0   0.0 20718-13:11:44 /sbin/launchd
  314     1   0.0    15-10:37:19 /usr/libexec/logd
"""


def tmp_base() -> Path:
    override = os.environ.get("HYGIENE_TEST_TMPDIR")
    base = Path(override) if override else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_jj(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, JJ_EDITOR="true")
    return subprocess.run(
        ["jj", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def jj_init_repo(path: Path, bookmark: str = "dev") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    result = run_jj(path, "git", "init")
    if result.returncode != 0:
        raise AssertionError(f"jj git init failed: {result.stderr}")
    if bookmark:
        result = run_jj(path, "bookmark", "create", bookmark)
        if result.returncode != 0:
            raise AssertionError(f"jj bookmark create failed: {result.stderr}")
    return path


def run_cli(argv: list[str]):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = HYGIENE.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    """A fresh throwaway root per test, removed on teardown."""

    def setUp(self):
        # Resolve immediately: the tool resolves the crate root it is given, and
        # on macOS $TMPDIR lives under the /var -> /private/var symlink, so an
        # unresolved fixture path would never string-match what the tool prints.
        self.root = Path(
            tempfile.mkdtemp(prefix="hygiene-test-", dir=tmp_base())
        ).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def process_table(self, text: str, name: str = "processes.txt") -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def clean_argv(self, *extra: str) -> list[str]:
        return [
            "--crate-root",
            str(self.root),
            "--crates",
            "demo",
            "--process-table",
            str(self.process_table(UNRELATED_PROCESSES)),
            *extra,
        ]

    def run_json(self, *extra: str):
        code, out, err = run_cli(self.clean_argv("--json", *extra))
        try:
            report = json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"--json did not print JSON (exit {code}): {out!r} {err!r}")
        return code, report

    @staticmethod
    def of_check(report: dict, check: str) -> list[dict]:
        return [finding for finding in report["findings"] if finding["check"] == check]

    def make_workspace_with_binary(self, name: str) -> Path:
        """A live-looking test binary path under an existing workspace dir."""
        binary = self.root / name / "target" / "debug" / "deps" / "rtp-0000"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        return binary


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------


class TestParsing(unittest.TestCase):
    def test_parse_etime_handles_every_ps_form(self):
        self.assertEqual(HYGIENE.parse_etime("00:28"), 28)
        self.assertEqual(HYGIENE.parse_etime("14:19"), 14 * 60 + 19)
        self.assertEqual(HYGIENE.parse_etime("01:02:03"), 3723)
        self.assertEqual(
            HYGIENE.parse_etime("15-10:37:19"), 15 * 86400 + 10 * 3600 + 37 * 60 + 19
        )
        self.assertEqual(HYGIENE.parse_etime("2-23:00:01"), 2 * 86400 + 23 * 3600 + 1)
        self.assertEqual(HYGIENE.parse_etime("42"), 42)

    def test_parse_etime_rejects_garbage(self):
        for text in ("", "  ", "abc", "1:x:3", "1-"):
            self.assertIsNone(HYGIENE.parse_etime(text), text)

    def test_format_bytes_and_duration(self):
        self.assertEqual(HYGIENE.format_bytes(512), "512 B")
        self.assertEqual(HYGIENE.format_bytes(2048), "2.0 KiB")
        self.assertEqual(HYGIENE.format_bytes(3 * 1024**3), "3.0 GiB")
        self.assertEqual(HYGIENE.format_duration(45), "00:45")
        self.assertEqual(HYGIENE.format_duration(3723), "01:02:03")
        self.assertEqual(HYGIENE.format_duration(90061), "1d 01:01:01")

    def test_parse_workspace_list(self):
        text = (
            "default\t/tmp/x/demo\tabc123\n"
            "ws_gone\t\tdef456\n"
            "ws_good\t/tmp/x/ws_good\tghi789\n"
            "\n"
        )
        self.assertEqual(
            HYGIENE.parse_workspace_list(text),
            [
                ("default", "/tmp/x/demo", "abc123"),
                ("ws_gone", None, "def456"),
                ("ws_good", "/tmp/x/ws_good", "ghi789"),
            ],
        )

    def test_parse_workspace_list_tolerates_missing_fields(self):
        self.assertEqual(HYGIENE.parse_workspace_list("default\n"), [("default", None, "")])


class TestProcessParsing(unittest.TestCase):
    def test_parse_process_table(self):
        processes = HYGIENE.parse_process_table(UNRELATED_PROCESSES)
        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0]["pid"], 1)
        self.assertEqual(processes[0]["ppid"], 0)
        self.assertEqual(processes[0]["args"], "/sbin/launchd")
        self.assertEqual(
            processes[1]["age_seconds"], 15 * 86400 + 10 * 3600 + 37 * 60 + 19
        )

    def test_parse_process_table_skips_short_lines(self):
        self.assertEqual(HYGIENE.parse_process_table("nonsense\n\n"), [])

    def test_process_ancestors_walks_the_parent_chain(self):
        processes = [
            {"pid": 10, "ppid": 5},
            {"pid": 5, "ppid": 1},
            {"pid": 99, "ppid": 1},
        ]
        self.assertEqual(HYGIENE.process_ancestors(processes, 10), {10, 5, 1})

    def test_path_tokens_strips_quoting(self):
        self.assertIn("/tmp/x", HYGIENE.path_tokens("a '/tmp/x, y' (z)"))


# --------------------------------------------------------------------------
# check 1: workspaces
# --------------------------------------------------------------------------


@NEEDS_JJ
class TestWorkspaces(TempCase):
    def build_repo(self, name: str = "demo") -> Path:
        return jj_init_repo(self.root / name)

    def test_clean_repo_reports_no_findings_and_exits_zero(self):
        self.build_repo()
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["findings"], [])
        checks = report["checks"]["workspaces"]
        self.assertEqual(checks["stale_registrations"], 0)
        self.assertEqual(checks["unregistered_workspace_dirs"], [])
        self.assertEqual(checks["repositories_checked"], 1)
        self.assertEqual(checks["registered_workspaces"], 1)

    def test_stale_registration_fires_then_clears(self):
        repo = self.build_repo()
        result = run_jj(repo, "workspace", "add", "../gone_ws")
        self.assertEqual(result.returncode, 0, result.stderr)
        gone = self.root / "gone_ws"
        self.assertTrue(gone.is_dir())
        # Present: the registration outlives the directory.
        shutil.rmtree(gone)
        code, report = self.run_json()
        self.assertEqual(code, 2, report)
        self.assertEqual(
            [f["subject"] for f in self.of_check(report, "workspaces")],
            ["demo: stale registration 'gone_ws'"],
        )
        self.assertEqual(report["checks"]["workspaces"]["stale_registrations"], 1)
        # Absent: forgetting the registration clears the finding.
        result = run_jj(repo, "workspace", "forget", "gone_ws")
        self.assertEqual(result.returncode, 0, result.stderr)
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual([f for f in report["findings"] if "stale" in f["subject"]], [])

    def test_unregistered_workspace_directory_fires_then_clears(self):
        repo = self.build_repo()
        result = run_jj(repo, "workspace", "add", "../orphan_ws")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Absent: while it is registered, nothing is reported.
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.of_check(report, "workspaces"), [])
        # Present: unregister it and the directory is left behind.
        result = run_jj(repo, "workspace", "forget", "orphan_ws")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "orphan_ws").is_dir())
        code, report = self.run_json()
        self.assertEqual(code, 2, report)
        self.assertEqual(
            [f["subject"] for f in self.of_check(report, "workspaces")],
            [f"{self.root / 'orphan_ws'} is an unregistered workspace of demo"],
        )
        self.assertEqual(
            report["checks"]["workspaces"]["unregistered_workspace_dirs"],
            [str(self.root / "orphan_ws")],
        )

    def test_repository_named_like_a_workspace_fires(self):
        self.build_repo()
        # A directory whose name says "workspace" but whose store is a whole
        # repository: the shape that makes `rm -rf ../$name` destroy history.
        danger = jj_init_repo(self.root / "danger_ws", bookmark="")
        code, report = self.run_json()
        self.assertEqual(code, 2, report)
        self.assertEqual(
            [f["subject"] for f in self.of_check(report, "workspaces")],
            [f"{danger} is a repository, not a workspace"],
        )
        self.assertEqual(
            report["checks"]["workspaces"]["repositories_where_a_workspace_was_expected"],
            [str(danger)],
        )

    def test_other_repository_is_context_not_a_finding(self):
        self.build_repo()
        other = jj_init_repo(self.root / "other_crate", bookmark="")
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(
            report["checks"]["workspaces"]["other_repositories_in_crate_root"],
            [str(other)],
        )
        self.assertEqual(self.of_check(report, "workspaces"), [])
        self.assertIn(
            f"{other} is a jj repository, not a workspace: never delete it",
            report["context"],
        )

    def test_workspace_shaped_directory_without_jj_fires_then_clears(self):
        self.build_repo()
        (self.root / "leftover_ws").mkdir()
        code, report = self.run_json()
        self.assertEqual(code, 1, report)
        self.assertEqual(
            [f["subject"] for f in self.of_check(report, "workspaces")],
            [f"{self.root / 'leftover_ws'} looks like a workspace but has no .jj"],
        )
        shutil.rmtree(self.root / "leftover_ws")
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.of_check(report, "workspaces"), [])


# --------------------------------------------------------------------------
# check 2: leaked processes
# --------------------------------------------------------------------------


class TestProcessLeaks(TempCase):
    def table(self, *lines: str) -> list[dict]:
        return HYGIENE.parse_process_table("\n".join(lines) + "\n")

    def test_vanished_path_fires_then_clears(self):
        dead = self.root / "dead_ws" / "target" / "debug" / "deps" / "rtp-0000"
        processes = self.table(f"  700     1   0.0 2-23:00:01 {dead} --exact foo")
        leaks, live = HYGIENE.find_process_leaks(processes, self.root, set(), 10.0)
        self.assertEqual(live, [])
        self.assertEqual(len(leaks), 1)
        self.assertIn("directory is gone", leaks[0]["reason"])
        # Absent: the same command line under an existing directory, with a
        # live parent, is not a leak.
        live_binary = self.make_workspace_with_binary("live_ws")
        processes = self.table(
            f"  700   31337   0.0 2-23:00:01 {live_binary} --exact foo",
            "31337     1   0.0 10-00:00:00 pi",
        )
        leaks, live = HYGIENE.find_process_leaks(processes, self.root, set(), 10.0)
        self.assertEqual(leaks, [])
        self.assertEqual([p["pid"] for p in live], [700])

    def test_orphan_fires_only_once_old_enough(self):
        binary = self.make_workspace_with_binary("live_ws")
        young = self.table(f"  700     1   0.0 00:01 {binary}")
        self.assertEqual(HYGIENE.find_process_leaks(young, self.root, set(), 10.0)[0], [])
        old = self.table(f"  700     1   0.0 2-23:00:01 {binary}")
        leaks, live = HYGIENE.find_process_leaks(old, self.root, set(), 10.0)
        self.assertEqual(live, [])
        self.assertEqual(len(leaks), 1)
        self.assertIn("orphaned (parent init)", leaks[0]["reason"])

    def test_explicit_orphan_threshold_of_zero_fires_immediately(self):
        binary = self.make_workspace_with_binary("live_ws")
        young = self.table(f"  700     1   0.0 00:01 {binary}")
        self.assertEqual(len(HYGIENE.find_process_leaks(young, self.root, set(), 0.0)[0]), 1)

    def test_missing_parent_counts_as_orphaned(self):
        binary = self.make_workspace_with_binary("live_ws")
        processes = self.table(f"  700   31337   0.0 3-00:00:00 {binary}")
        leaks, _live = HYGIENE.find_process_leaks(processes, self.root, set(), 10.0)
        self.assertEqual(len(leaks), 1)
        self.assertIn("missing pid 31337", leaks[0]["reason"])

    def test_child_of_a_live_agent_is_not_a_leak(self):
        binary = self.make_workspace_with_binary("live_ws")
        processes = self.table(
            f"  700   31337   0.0 3-00:00:00 {binary}",
            "31337     1   0.0 10-00:00:00 pi",
        )
        leaks, live = HYGIENE.find_process_leaks(processes, self.root, set(), 10.0)
        self.assertEqual(leaks, [])
        self.assertEqual(len(live), 1)

    def test_own_process_tree_is_excluded(self):
        binary = self.make_workspace_with_binary("live_ws")
        processes = self.table(f"  700     1   0.0 2-23:00:01 {binary}")
        leaks, live = HYGIENE.find_process_leaks(processes, self.root, {700}, 10.0)
        self.assertEqual(leaks, [])
        self.assertEqual(live, [])

    def test_process_outside_the_crate_tree_is_ignored(self):
        processes = self.table("  700     1   0.0 2-23:00:01 /usr/bin/true")
        leaks, live = HYGIENE.find_process_leaks(processes, self.root, set(), 10.0)
        self.assertEqual(leaks, [])
        self.assertEqual(live, [])

    def test_check_processes_reports_leaks_and_keeps_live_for_context(self):
        dead = self.root / "dead_ws" / "target" / "debug" / "deps" / "rtp-0000"
        live_binary = self.make_workspace_with_binary("live_ws")
        table = self.process_table(
            f"  700     1   0.0 2-23:00:01 {dead}\n"
            f"  701   31337   0.1 3-00:00:00 {live_binary}\n"
            "31337     1   0.0 10-00:00:00 pi\n"
        )
        report = HYGIENE.Report(self.root, ["demo"], "dev")
        HYGIENE.check_processes(report, process_table=table, orphan_minutes=10.0)
        self.assertEqual(len(report.blocking()), 1)
        self.assertEqual(report.advisory(), [])
        self.assertEqual(report.checks["processes"]["leaks"], 1)
        self.assertEqual(report.checks["processes"]["live_in_tree"], 1)

    def test_check_processes_through_the_cli_fires(self):
        dead = self.root / "dead_ws" / "target" / "debug" / "deps" / "rtp-0000"
        # A distinct file name: clean_argv rewrites processes.txt on every call.
        table = self.process_table(f"  700     1   0.0 2-23:00:01 {dead} --exact foo", "dead.txt")
        code, report = self.run_json("--process-table", str(table))
        self.assertEqual(code, 2, report)
        subjects = [f["subject"] for f in self.of_check(report, "processes")]
        self.assertEqual(subjects, ["leaked process 700"])
        self.assertEqual(report["checks"]["processes"]["leaks"], 1)


# --------------------------------------------------------------------------
# check 3: scratch and disk
# --------------------------------------------------------------------------


class TestScratch(TempCase):
    def test_directory_size_measures_a_known_tree(self):
        big = self.root / "demo" / "target"
        big.mkdir(parents=True)
        (big / "blob").write_bytes(b"x" * 65536)
        size = HYGIENE.directory_size(big)
        self.assertIsNotNone(size)
        self.assertGreaterEqual(size, 65536)

    def test_repo_markers_finds_nested_git_and_jj(self):
        scratch = self.root / ".old-copy"
        (scratch / "rtp" / ".git").mkdir(parents=True)
        (scratch / "mux" / ".jj").mkdir(parents=True)
        markers = sorted(os.path.relpath(m, scratch) for m in HYGIENE.repo_markers(scratch))
        self.assertEqual(markers, ["mux/.jj", "rtp/.git"])
        self.assertEqual(HYGIENE.repo_markers(self.root / "demo"), [])

    def test_large_scratch_fires_then_clears(self):
        big = self.root / "demo" / "target"
        big.mkdir(parents=True)
        (big / "blob").write_bytes(b"x" * 262144)
        code, report = self.run_json("--scratch-limit-gb", "0.0001")
        self.assertEqual(code, 1, report)
        self.assertEqual(len(self.of_check(report, "scratch")), 1)
        self.assertIn("exceeds the 0.0001 GiB limit", self.of_check(report, "scratch")[0]["detail"])
        code, report = self.run_json()
        self.assertEqual(self.of_check(report, "scratch"), [])

    def test_old_scratch_fires_then_clears(self):
        scratch = self.root / ".old-copy"
        scratch.mkdir()
        (scratch / "file").write_text("x")
        old = 100 * 86400
        os.utime(scratch, (scratch.stat().st_atime - old, scratch.stat().st_mtime - old))
        code, report = self.run_json("--scratch-age-days", "7")
        self.assertEqual(code, 1, report)
        self.assertEqual(len(self.of_check(report, "scratch")), 1)
        self.assertIn("old (limit 7 days)", self.of_check(report, "scratch")[0]["detail"])
        code, report = self.run_json("--scratch-age-days", "365")
        self.assertEqual(self.of_check(report, "scratch"), [])

    def test_scratch_holding_a_repository_is_called_out(self):
        scratch = self.root / ".it-copy"
        (scratch / "rtp" / ".git").mkdir(parents=True)
        code, report = self.run_json()
        self.assertEqual(code, 1, report)
        findings = self.of_check(report, "scratch")
        self.assertEqual(len(findings), 1)
        self.assertIn("contains a repository store", findings[0]["detail"])
        self.assertIn("deleting it destroys copied history", findings[0]["detail"])

    def test_low_free_disk_fires_then_clears(self):
        code, report = self.run_json("--min-free-gb", "1000000")
        self.assertEqual(code, 1, report)
        self.assertIn("low free disk space", self.of_check(report, "scratch")[0]["subject"])
        code, report = self.run_json("--min-free-gb", "0.001")
        self.assertEqual(self.of_check(report, "scratch"), [])

    def test_target_directories_are_measured_but_not_dot_dirs(self):
        (self.root / "demo" / "target").mkdir(parents=True)
        (self.root / ".scratch").mkdir()
        items = HYGIENE.scratch_items(self.root, ["demo"])
        self.assertEqual(
            sorted((kind, path.name) for kind, path in items),
            [("scratch", ".scratch"), ("target", "target")],
        )


# --------------------------------------------------------------------------
# check 4: repository state
# --------------------------------------------------------------------------


def clean_state(repo: str) -> dict:
    return {
        "repo": repo,
        "bookmark": "abc",
        "bookmark_error": None,
        "conflicts": [],
        "conflicts_error": None,
        "working_copy": {
            "empty": True,
            "conflict": False,
            "change_id": "x",
            "description": "",
        },
        "working_copy_error": None,
        "dirty_stat": None,
        "bookmark_conflicts": [],
    }


class TestRepoState(TempCase):
    def evaluate(self, state: dict) -> HYGIENE.Report:
        report = HYGIENE.Report(self.root, ["demo"], "dev")
        HYGIENE.evaluate_repo_state(report, "demo", state)
        return report

    def test_clean_state_reports_nothing(self):
        self.assertEqual(self.evaluate(clean_state("demo")).findings, [])

    def test_missing_bookmark_fires_then_clears(self):
        state = clean_state("demo")
        state["bookmark"] = None
        state["bookmark_error"] = "Error: Revision `dev` doesn't exist"
        report = self.evaluate(state)
        self.assertEqual(len(report.blocking()), 1)
        self.assertIn("does not resolve", report.blocking()[0].subject)
        self.assertEqual(self.evaluate(clean_state("demo")).findings, [])

    def test_dirty_working_copy_fires_with_its_stat(self):
        state = clean_state("demo")
        state["working_copy"] = {
            "empty": False,
            "conflict": False,
            "change_id": "xwsoxpuq",
            "description": "",
        }
        state["dirty_stat"] = "7 files changed, 16 insertions(+), 0 deletions(-)"
        report = self.evaluate(state)
        self.assertEqual(len(report.blocking()), 1)
        self.assertIn("uncommitted changes", report.blocking()[0].subject)
        self.assertIn("7 files changed", report.blocking()[0].detail)
        self.assertEqual(self.evaluate(clean_state("demo")).findings, [])

    def test_conflicted_commit_fires_then_clears(self):
        state = clean_state("demo")
        state["conflicts"] = ["deadbeef abcd1234 topic"]
        report = self.evaluate(state)
        self.assertEqual(len(report.blocking()), 1)
        self.assertIn("1 conflicted commit(s)", report.blocking()[0].subject)
        self.assertEqual(self.evaluate(clean_state("demo")).findings, [])

    def test_conflicted_bookmark_fires_then_clears(self):
        state = clean_state("demo")
        state["bookmark_conflicts"] = ["dev: abcd1234 conflict"]
        report = self.evaluate(state)
        self.assertEqual(len(report.blocking()), 1)
        self.assertIn("conflicted bookmark", report.blocking()[0].subject)
        self.assertEqual(self.evaluate(clean_state("demo")).findings, [])

    def test_error_reading_the_working_copy_fires(self):
        state = clean_state("demo")
        state["working_copy"] = None
        state["working_copy_error"] = "unexpected `jj log` output"
        report = self.evaluate(state)
        self.assertEqual(len(report.blocking()), 1)
        self.assertIn("could not read the working copy", report.blocking()[0].subject)


@NEEDS_JJ
class TestRepoStateWithJj(TempCase):
    def test_snapshot_sees_an_uncommitted_file_and_read_only_mode_does_not(self):
        jj_init_repo(self.root / "demo")
        (self.root / "demo" / "scratch.txt").write_text("uncommitted\n")
        # Read-only (the default): jj has not observed the file, so the check
        # is silent — the documented limitation, not a hidden one.
        code, report = self.run_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.of_check(report, "repo-state"), [])
        self.assertFalse(report["checks"]["repo-state"]["snapshotted"])
        # Present: --snapshot lets jj record the file, so @ is no longer empty.
        code, report = self.run_json("--snapshot")
        self.assertEqual(code, 2, report)
        self.assertEqual(
            [f["subject"] for f in self.of_check(report, "repo-state")],
            ["demo: working copy has uncommitted changes"],
        )
        self.assertIn("1 file changed", self.of_check(report, "repo-state")[0]["detail"])


# --------------------------------------------------------------------------
# check 5: agent logs
# --------------------------------------------------------------------------


class TestAgentLogs(TempCase):
    def logs_argv(self, logs: Path, *extra: str) -> list[str]:
        return [
            "--crate-root",
            str(self.root),
            "--crates",
            "demo",
            "--process-table",
            str(self.process_table(UNRELATED_PROCESSES)),
            "--agent-logs",
            str(logs),
            *extra,
        ]

    def make_logs(self) -> Path:
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "fresh.log").write_text("working\n")
        stale = logs / "stale.log"
        stale.write_text("stopped here\n")
        old = 3 * 3600
        os.utime(stale, (stale.stat().st_atime - old, stale.stat().st_mtime - old))
        return logs

    def test_stalled_log_fires_then_clears(self):
        logs = self.make_logs()
        code, out, err = run_cli(self.logs_argv(logs, "--json", "--stale-minutes", "30"))
        report = json.loads(out)
        self.assertEqual(code, 1, report)
        findings = self.of_check(report, "agent-logs")
        self.assertEqual([f["subject"] for f in findings], ["stale.log: no writes for 03:00:00"])
        self.assertEqual(report["checks"]["agent-logs"]["count"], 2)
        self.assertEqual(report["checks"]["agent-logs"]["stalled"], 1)
        # Absent: a threshold above every log's age is silent.
        code, out, err = run_cli(self.logs_argv(logs, "--json", "--stale-minutes", "600"))
        report = json.loads(out)
        self.assertEqual(self.of_check(report, "agent-logs"), [])
        self.assertEqual(report["checks"]["agent-logs"]["stalled"], 0)

    def test_without_the_argument_the_check_is_reported_as_skipped(self):
        code, report = self.run_json()
        self.assertEqual(report["checks"]["agent-logs"]["status"], "skipped")
        self.assertTrue(any("agent logs" in item for item in report["skipped"]))

    def test_missing_directory_names_the_problem(self):
        code, out, _err = run_cli(self.clean_argv("--agent-logs", str(self.root / "nope")))
        self.assertEqual(code, 2, out)
        self.assertIn("is not a directory", out)


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


class TestCliContract(TempCase):
    def test_crates_parser_accepts_spaces_and_commas(self):
        self.assertEqual(HYGIENE.parse_crates("a, b"), ["a", "b"])
        self.assertEqual(HYGIENE.parse_crates("a b"), ["a", "b"])
        with self.assertRaises(argparse.ArgumentTypeError):
            HYGIENE.parse_crates("")

    def test_crates_parser_rejects_a_path(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            HYGIENE.parse_crates("../evil")

    def test_usage_error_exits_64_not_a_finding_code(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                HYGIENE.main(["--nope"])
        self.assertEqual(caught.exception.code, 64)

    def test_crate_root_defaults_to_this_files_location(self):
        self.assertEqual(HYGIENE.DEFAULT_CRATE_ROOT, MODULE_PATH.resolve().parents[2])
        self.assertEqual(HYGIENE.DEFAULT_CRATE_ROOT.name, "crates")

    def test_source_has_no_host_specific_paths(self):
        text = MODULE_PATH.read_text()
        for needle in ("/Users/", "/home/", "/tmp/", "it33"):
            self.assertNotIn(needle, text)

    def test_dangling_workspace_pointer_is_blocking_in_a_tree_without_jj(self):
        # A `.jj/repo` file that does not resolve to a repository is a blocking
        # finding, and this path needs neither jj nor a real repo to reach it.
        pointer = self.root / "broken_ws" / ".jj" / "repo"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("../nope")
        code, report = self.run_json()
        self.assertEqual(code, 2, report)
        pointers = [f for f in self.of_check(report, "workspaces") if "unresolvable" in f["subject"]]
        self.assertEqual(
            [f["subject"] for f in pointers],
            [f"{self.root / 'broken_ws'}: unresolvable workspace pointer"],
        )

    def test_exit_code_is_one_when_only_advisory(self):
        (self.root / "leftover_ws").mkdir()
        code, report = self.run_json()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["blocking"], 0)
        self.assertGreater(report["advisory"], 0)

    def test_text_output_names_the_repositories_never_to_delete(self):
        (self.root / "plain_repo" / ".jj" / "repo").mkdir(parents=True)
        code, out, _err = run_cli(self.clean_argv())
        self.assertNotIn("BLOCKING", out)
        self.assertIn(
            f"context: {self.root / 'plain_repo'} is a jj repository, not a workspace: never delete it",
            out,
        )
        self.assertEqual(code, 1, out)  # only the "demo has no .jj/repo" advisory

    def test_missing_crate_root_is_reported_not_crashed_on(self):
        missing = self.root / "does-not-exist"
        code, out, _err = run_cli(
            [
                "--crate-root",
                str(missing),
                "--process-table",
                str(self.process_table(UNRELATED_PROCESSES)),
            ]
        )
        self.assertEqual(code, 2, out)
        self.assertIn("does not exist", out)

    def test_report_only_never_mutates_the_tree(self):
        repo = self.root / "demo"
        (repo / "target").mkdir(parents=True)
        (repo / "target" / "blob").write_bytes(b"x" * 1024)
        (self.root / ".scratch").mkdir()
        before = sorted(
            (str(p.relative_to(self.root)), p.stat().st_mode, p.stat().st_size)
            for p in self.root.rglob("*")
        )
        run_cli(self.clean_argv("--json"))
        after = sorted(
            (str(p.relative_to(self.root)), p.stat().st_mode, p.stat().st_size)
            for p in self.root.rglob("*")
        )
        # The process table the helper writes is the only file the run touches.
        self.assertEqual(
            [entry for entry in before if not entry[0].startswith("processes.txt")],
            [entry for entry in after if not entry[0].startswith("processes.txt")],
        )


if __name__ == "__main__":
    unittest.main()
