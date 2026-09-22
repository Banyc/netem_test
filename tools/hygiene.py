#!/usr/bin/env python3
"""Report the hygiene state of the crate tree — one command, every check.

Work in this tree happens in sibling jj repositories that share a parent
directory, spawned one background agent per iteration.  Five conditions have
each cost a real session time, and none of them was checked by anything:

* a jj workspace registration whose directory no longer exists.  Worse than
  clutter: every agent brief refuses to start when a ``<crate>_it*_ws``
  directory already exists, so one dead registration silently blocks all
  future work on that crate.
* a *leaked process*: a test binary or stress loop reparented to init (or left
  behind by a workspace that no longer exists), still sleeping — or still
  burning CPU and distorting every measurement taken beside it.
* stale scratch inside the crate tree: an old session's copy experiment, whole
  crate trees with their ``.git``/``.jj`` stores inside, which a later session
  mistook for the real tree.
* a jj repository where a workspace was expected (``.jj/repo`` is a directory,
  not a file).  Deleting one destroys history.
* a dead agent that never stopped being reported as running because nothing
  looked at whether its task log was still being written.

This tool reports all of them in one run so a session can see the state at the
start of a cycle.  It is strictly **report-only**: it never deletes, moves,
forgets or kills anything, and it makes no attempt to repair what it finds.
Destructive cleanup — ``jj workspace forget``, ``rm -rf``, ``kill`` — stays a
human decision, made on a path this tool has printed.

Exit status
-----------

``0``  no findings.
``1``  advisory findings only (large or old scratch, a stalled log, ...).
``2``  at least one blocking finding (stale registration, an unregistered
       workspace directory, a repository where a workspace was expected, a
       conflicted commit or bookmark, a dirty shared working copy, ``dev`` not
       resolving, a leaked process).
``64`` usage error (not a finding).

A non-zero exit is meant to be resolved before spawning more work, so the
report separates *blocking* from *advisory* and never folds one into the other.

What is checked
---------------

1. **jj workspace registrations** for every in-scope crate repository:
   a registration whose recorded directory does not exist (blocking); a
   workspace-shaped directory (``*_ws``) under the crate root that is not
   registered in its repository (blocking — it blocks agents the same way);
   and any directory whose ``.jj/repo`` is a *directory* rather than a file
   (blocking, loudly — that is a repository, never delete it).
2. **Leaked processes**: anything whose command line names a path under the
   crate root, excluding this tool's own process tree.  Reported pid, age, CPU
   and whether the parent is init.  A process is a *leak* when it references a
   path whose directory is gone, or when it is orphaned (parent init, or a
   parent that is not in the process table) for longer than
   ``--orphan-minutes``.  A process that is merely live and in-tree is
   context, not a finding — it is indistinguishable from a concurrent agent's
   legitimate work, and flagging it would make the report useless.
3. **Scratch and disk**: the size of each crate's ``target`` directory, each
   dot-prefixed directory under the crate root, free disk space, and a flag on
   any single item above ``--scratch-limit-gb`` or older than
   ``--scratch-age-days``.  A scratch item that itself contains a ``.git`` or
   ``.jj`` store is called out, because deleting it would destroy a copied
   repository's history.
4. **Repo state sanity** for the in-scope crates: ``--bookmark`` (default
   ``dev``) resolves, no conflicted commits or bookmarks, and the crate's own
   working copy has no changes.
5. **Agent task logs**, only when ``--agent-logs DIR`` is given: each log's age,
   and a flag on any log not written for longer than ``--stale-minutes``.
   There is no fixed location for these; guessing one would be worse than
   skipping the check, so the directory must be supplied.

Read-only by default
--------------------

Every jj query runs with ``--ignore-working-copy`` unless ``--snapshot`` is
passed, so the check neither writes to the repositories nor walks the working
copies.  The consequence is explicit: the working-copy check sees the last
*snapshotted* state, so edits that jj has never observed are invisible to it.
Pass ``--snapshot`` to let jj refresh each working copy first (a filesystem
walk, and a write to the repo's operation log).

Scope
-----

The crate root is derived from this file's own location
(``<crate root>/netem_test/tools/hygiene.py``); the in-scope crates default to
the sibling repositories and can be overridden with ``--crates``.  Nothing
here is path-, user- or revision-specific, so the same command works from any
checkout of this tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The crate root is the directory that holds the crate repositories.  This file
# lives at <crate root>/netem_test/tools/hygiene.py, so it is three levels up.
DEFAULT_CRATE_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CRATES = ("rtp", "mux", "rtp_mux", "netem_test", "udp_listener", "tokio_udp")

DEFAULT_BOOKMARK = "dev"
WORKSPACE_SUFFIX = "_ws"

JJ_TIMEOUT_SECONDS = 30.0
PS_TIMEOUT_SECONDS = 30.0
DU_TIMEOUT_SECONDS = 180.0

DEFAULT_SCRATCH_LIMIT_GB = 1.0
DEFAULT_SCRATCH_AGE_DAYS = 7.0
DEFAULT_MIN_FREE_GB = 10.0
DEFAULT_STALE_MINUTES = 30.0
DEFAULT_ORPHAN_MINUTES = 10.0

BLOCKING = "blocking"
ADVISORY = "advisory"

WORKSPACE_TEMPLATE = (
    'name ++ "\\t" ++ self.root() ++ "\\t" ++ self.target().change_id().short() ++ "\\n"'
)
WORKING_COPY_TEMPLATE = (
    'empty ++ "\\t" ++ conflict ++ "\\t" ++ change_id.short() ++ "\\t"'
    ' ++ description.first_line()'
)
CONFLICT_TEMPLATE = (
    'commit_id.short() ++ " " ++ change_id.short() ++ " " ++ description.first_line()'
)

LAYOUT = ("check", "severity", "subject", "detail")


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------


def format_bytes(size: int) -> str:
    """Human-readable size; exact for small values, one decimal above KiB."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def format_duration(seconds: float) -> str:
    """Compact age/duration, coarse above an hour."""
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_etime(text: str) -> int | None:
    """Parse a ``ps`` etime field (``[[dd-]hh:]mm:ss``) into seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0
    head, separator, rest = text.partition("-")
    if separator:
        if not head.isdigit():
            return None
        days = int(head)
        text = rest
    parts = text.split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 3:
        hours, minutes, seconds = values
    elif len(values) == 2:
        hours, minutes, seconds = 0, values[0], values[1]
    elif len(values) == 1:
        hours, minutes, seconds = 0, 0, values[0]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    subject: str
    detail: str = ""


class Report:
    """Findings plus one machine-readable summary per check."""

    def __init__(self, crate_root: Path, crates: list[str], bookmark: str) -> None:
        self.crate_root = crate_root
        self.crates = list(crates)
        self.bookmark = bookmark
        self.findings: list[Finding] = []
        self.checks: "dict[str, dict]" = {}
        self.skipped: list[str] = []
        self.context: list[str] = []

    def add(self, check: str, severity: str, subject: str, detail: str = "") -> None:
        self.findings.append(Finding(check, severity, subject, detail))

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ADVISORY]

    def skip(self, text: str) -> None:
        self.skipped.append(text)

    def set_check(self, name: str, summary: str, **data) -> None:
        """Record one check's one-line summary plus its machine-readable data."""
        entry = {"summary": summary}
        entry.update(data)
        self.checks[name] = entry

    def exit_code(self) -> int:
        if self.blocking():
            return 2
        if self.advisory():
            return 1
        return 0

    def as_dict(self) -> dict:
        return {
            "crate_root": str(self.crate_root),
            "crates": self.crates,
            "bookmark": self.bookmark,
            "findings": [
                {
                    "check": f.check,
                    "severity": f.severity,
                    "subject": f.subject,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "blocking": len(self.blocking()),
            "advisory": len(self.advisory()),
            "checks": self.checks,
            "skipped": self.skipped,
            "context": self.context,
            "exit_code": self.exit_code(),
        }

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"hygiene: crate root {self.crate_root}")
        lines.append(f"hygiene: in-scope crates {', '.join(self.crates)}")
        lines.append("")
        if self.findings:
            for severity in (BLOCKING, ADVISORY):
                for finding in self.findings:
                    if finding.severity != severity:
                        continue
                    lines.append(f"{severity.upper():8s} [{finding.check}] {finding.subject}")
                    if finding.detail:
                        lines.append(f"         {finding.detail}")
        else:
            lines.append("findings: none")
        if self.skipped:
            lines.append("")
            for text in self.skipped:
                lines.append(f"skipped: {text}")
        if self.context:
            lines.append("")
            for text in self.context:
                lines.append(f"context: {text}")
        lines.append("")
        lines.append("checks:")
        for name, check in self.checks.items():
            lines.append(f"  {name:12s} {check.get('summary', '')}")
        lines.append("")
        lines.append(
            f"summary: {len(self.blocking())} blocking, {len(self.advisory())} advisory"
        )
        lines.append(f"exit: {self.exit_code()}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# subprocess helpers
# --------------------------------------------------------------------------


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def run_jj(repo: Path, args: list[str], *, ignore_working_copy: bool = True):
    """Run a read-only jj query against ``repo``."""
    argv = [
        "jj",
        "--repository",
        str(repo),
        "--no-pager",
        "--color=never",
    ]
    if ignore_working_copy:
        argv.append("--ignore-working-copy")
    argv.extend(args)
    return _run(argv, JJ_TIMEOUT_SECONDS)


def parse_workspace_list(text: str) -> list[tuple[str, str | None, str]]:
    """Parse the tab-separated ``jj workspace list`` template output.

    Each line is ``name <TAB> root-or-empty <TAB> change-id``.  An empty root
    means jj has no recorded, resolvable root for that workspace: either the
    workspace directory is gone, or the workspace predates root recording.
    """
    entries: list[tuple[str, str | None, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        while len(fields) < 3:
            fields.append("")
        name = fields[0].strip()
        root = fields[1].strip() or None
        change_id = fields[2].strip()
        if name:
            entries.append((name, root, change_id))
    return entries


def jj_workspaces(repo: Path, *, ignore_working_copy: bool = True):
    """Return ``(entries, error)`` for one repository."""
    result = run_jj(
        repo,
        ["workspace", "list", "-T", WORKSPACE_TEMPLATE],
        ignore_working_copy=ignore_working_copy,
    )
    if result is None:
        return [], "jj could not be run"
    if result.returncode != 0:
        return [], (result.stderr.strip().splitlines() or ["jj failed"])[0]
    return parse_workspace_list(result.stdout), None


# --------------------------------------------------------------------------
# check 1: jj workspaces
# --------------------------------------------------------------------------


def workspace_owner(directory: Path) -> Path | None:
    """Resolve the repository a workspace directory belongs to.

    A workspace's ``.jj/repo`` is a file holding the path of the owning
    repository's ``.jj/repo``; a repository's ``.jj/repo`` is a directory.
    Returns ``None`` when the pointer is missing or does not resolve to a
    ``<repo>/.jj/repo`` path.
    """
    pointer = directory / ".jj" / "repo"
    if not pointer.is_file():
        return None
    try:
        text = pointer.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    target = (pointer.parent / text).resolve()
    if target.parent.name == ".jj":
        return target.parent.parent
    return None


def check_workspaces(report: Report, *, ignore_working_copy: bool) -> None:
    root = report.crate_root
    registered: dict[Path, list[str]] = {}
    stale = 0
    unregistered_dirs: list[str] = []
    repository_dirs: list[str] = []
    other_repositories: list[str] = []
    unverified: list[str] = []
    workspaces_seen = 0
    repos_checked = 0

    for crate in report.crates:
        repo = root / crate
        if not (repo / ".jj" / "repo").exists():
            report.add(
                "workspaces",
                ADVISORY,
                f"{crate}: no jj repository",
                f"{repo} has no .jj/repo; workspace state could not be checked",
            )
            continue
        repos_checked += 1
        entries, error = jj_workspaces(repo, ignore_working_copy=ignore_working_copy)
        if error is not None:
            report.add(
                "workspaces",
                BLOCKING,
                f"{crate}: `jj workspace list` failed",
                error,
            )
            continue
        for name, recorded_root, _change_id in entries:
            workspaces_seen += 1
            if recorded_root is None:
                if name == "default":
                    # Pre-0.38 repositories do not record the default root; the
                    # default workspace is the repository root itself.
                    resolved = repo
                else:
                    # No recorded root: the directory is gone, or the workspace
                    # predates root recording.  The convention here is
                    # <crate root>/<workspace name>, so use it only if it exists.
                    conventional = root / name
                    if conventional.is_dir():
                        unverified.append(f"{crate}:{name} (root not recorded; assumed {conventional})")
                        resolved = conventional
                    else:
                        stale += 1
                        report.add(
                            "workspaces",
                            BLOCKING,
                            f"{crate}: stale registration '{name}'",
                            "registered workspace has no recorded root and no "
                            f"directory at {conventional}; `jj workspace forget "
                            f"{name}` (after reading this) unblocks the crate",
                        )
                        continue
            else:
                resolved = Path(recorded_root)
                if not resolved.is_dir():
                    stale += 1
                    report.add(
                        "workspaces",
                        BLOCKING,
                        f"{crate}: stale registration '{name}'",
                        f"registered workspace points at {resolved}, which does "
                        f"not exist; `jj workspace forget {name}` (after reading "
                        "this) unblocks the crate",
                    )
                    continue
            if not (resolved / ".jj").exists():
                report.add(
                    "workspaces",
                    ADVISORY,
                    f"{crate}: workspace '{name}' has no .jj metadata",
                    f"{resolved} exists but holds no .jj directory; the "
                    "workspace may be half-deleted",
                )
            registered.setdefault(resolved.resolve(), []).append(crate)

    # Directories under the crate root that are not in-scope crate repositories.
    if not root.is_dir():
        report.add(
            "workspaces",
            BLOCKING,
            f"crate root {root} does not exist",
            "the derived crate root is wrong; pass --crate-root",
        )
    else:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name in report.crates:
                continue
            repo_pointer = child / ".jj" / "repo"
            looks_like_workspace = child.name.endswith(WORKSPACE_SUFFIX)
            if repo_pointer.is_dir():
                if child.name.startswith("."):
                    # Scratch; the scratch check reports it, and reports the
                    # repositories inside it.
                    continue
                if looks_like_workspace:
                    # The name says workspace, the store says repository.  This
                    # is the shape that turned an `rm -rf ../$name` into the
                    # destruction of a repository.
                    repository_dirs.append(str(child))
                    report.add(
                        "workspaces",
                        BLOCKING,
                        f"{child} is a repository, not a workspace",
                        ".jj/repo is a directory: the history lives here. "
                        "Never delete it, and never derive a delete path from "
                        "a workspace name.",
                    )
                else:
                    # Another crate repository: expected, and here only so the
                    # report names it as never-delete context.
                    other_repositories.append(str(child))
                continue
            if repo_pointer.is_file():
                owner = workspace_owner(child)
                if owner is None:
                    report.add(
                        "workspaces",
                        BLOCKING,
                        f"{child}: unresolvable workspace pointer",
                        ".jj/repo does not resolve to a repository; the "
                        "workspace is dangling",
                    )
                    continue
                owner_name = owner.name if owner.parent == root else str(owner)
                if owner_name not in report.crates or not (owner / ".jj" / "repo").exists():
                    unverified.append(f"{child} (workspace of out-of-scope repository {owner})")
                    continue
                if child.resolve() not in registered:
                    unregistered_dirs.append(str(child))
                    report.add(
                        "workspaces",
                        BLOCKING,
                        f"{child} is an unregistered workspace of {owner_name}",
                        "no registration in that repository names this "
                        "directory; agents refuse to start when this name "
                        "exists, so the crate is blocked until it is resolved",
                    )
                continue
            if looks_like_workspace:
                report.add(
                    "workspaces",
                    ADVISORY,
                    f"{child} looks like a workspace but has no .jj",
                    "workspace-shaped directory with no jj metadata; leftover "
                    "or incomplete workspace",
                )

    summary = (
        f"{repos_checked} repositories, {workspaces_seen} registrations, "
        f"{stale} stale, {len(unregistered_dirs)} unregistered; "
        f"{len(other_repositories)} other repositories present (never delete)"
    )
    for repository in other_repositories:
        report.context.append(
            f"{repository} is a jj repository, not a workspace: never delete it"
        )
    report.set_check(
        "workspaces",
        summary,
        repositories_checked=repos_checked,
        registered_workspaces=workspaces_seen,
        stale_registrations=stale,
        unregistered_workspace_dirs=unregistered_dirs,
        repositories_where_a_workspace_was_expected=repository_dirs,
        other_repositories_in_crate_root=other_repositories,
        unverified_workspace_roots=unverified,
    )


# --------------------------------------------------------------------------
# check 2: leaked processes
# --------------------------------------------------------------------------


def parse_process_table(text: str) -> list[dict]:
    """Parse ``ps -axo pid=,ppid=,pcpu=,etime=,args=`` output."""
    processes: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        pid_text, ppid_text, cpu_text, etime_text, args = fields
        if not pid_text.isdigit() or not ppid_text.isdigit():
            continue
        try:
            cpu = float(cpu_text)
        except ValueError:
            cpu = 0.0
        processes.append(
            {
                "pid": int(pid_text),
                "ppid": int(ppid_text),
                "cpu": cpu,
                "age_seconds": parse_etime(etime_text),
                "args": args.strip(),
            }
        )
    return processes


def read_process_table(path: Path | None) -> list[dict]:
    if path is not None:
        text = path.read_text()
        return parse_process_table(text)
    result = _run(["ps", "-axo", "pid=,ppid=,pcpu=,etime=,args="], PS_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return []
    return parse_process_table(result.stdout)


def process_ancestors(processes: list[dict], pid: int) -> set[int]:
    """This tool's own process tree, so it never reports itself."""
    parents = {p["pid"]: p["ppid"] for p in processes}
    tree = {pid}
    current = pid
    for _ in range(64):
        current = parents.get(current, 0)
        if current <= 0 or current in tree:
            break
        tree.add(current)
    return tree


def path_tokens(args: str) -> list[str]:
    return [token.strip("'\"(),;") for token in args.split() if token]


def vanished_paths(args: str, crate_root: Path) -> list[str]:
    """Paths under the crate root whose directory no longer exists."""
    prefix = str(crate_root)
    vanished: list[str] = []
    for token in path_tokens(args):
        if not token.startswith(prefix):
            continue
        path = Path(token)
        if path.exists():
            continue
        if not path.parent.is_dir():
            vanished.append(str(path))
    return vanished


def find_process_leaks(
    processes: list[dict],
    crate_root: Path,
    exclude_pids: set[int],
    orphan_minutes: float,
) -> tuple[list[dict], list[dict]]:
    """Split in-tree processes into ``(leaks, live)``.

    A leak is a process that references a directory that no longer exists, or
    that has been orphaned for longer than ``orphan_minutes``.  A live in-tree
    process is context: it cannot be distinguished from a concurrent agent's
    legitimate work, so it is not a finding.
    """
    pids = {p["pid"] for p in processes}
    leaks: list[dict] = []
    live: list[dict] = []
    for process in processes:
        if process["pid"] in exclude_pids or process["pid"] <= 1:
            continue
        if str(crate_root) not in process["args"]:
            continue
        reason = None
        vanished = vanished_paths(process["args"], crate_root)
        if vanished:
            reason = f"references a path whose directory is gone: {vanished[0]}"
        else:
            age = process["age_seconds"] or 0
            orphaned = process["ppid"] == 1 or process["ppid"] not in pids
            if orphaned and age >= orphan_minutes * 60.0:
                parent = "init" if process["ppid"] == 1 else f"missing pid {process['ppid']}"
                reason = f"orphaned (parent {parent}) for {format_duration(age)}"
        if reason is None:
            live.append(process)
        else:
            entry = dict(process)
            entry["reason"] = reason
            leaks.append(entry)
    return leaks, live


def describe_process(process: dict) -> str:
    age = process["age_seconds"]
    age_text = format_duration(age) if age is not None else "unknown"
    return (
        f"pid {process['pid']} ppid {process['ppid']} cpu {process['cpu']:.1f}% "
        f"age {age_text}: {process['args'][:200]}"
    )


def check_processes(report: Report, *, process_table: Path | None, orphan_minutes: float) -> None:
    processes = read_process_table(process_table)
    if not processes:
        report.skip("processes: no process table could be read")
        report.set_check("processes", "unavailable (no process table)", status="unavailable")
        return
    exclude = process_ancestors(processes, os.getpid())
    leaks, live = find_process_leaks(processes, report.crate_root, exclude, orphan_minutes)
    for leak in leaks:
        report.add(
            "processes",
            BLOCKING,
            f"leaked process {leak['pid']}",
            f"{leak['reason']}; {describe_process(leak)}",
        )
    live.sort(key=lambda p: (p["age_seconds"] or 0), reverse=True)
    in_tree = [
        {
            "pid": p["pid"],
            "ppid": p["ppid"],
            "cpu": p["cpu"],
            "age_seconds": p["age_seconds"],
            "args": p["args"],
        }
        for p in live
    ]
    oldest = ""
    if in_tree and in_tree[0]["age_seconds"] is not None:
        oldest = f", oldest {format_duration(in_tree[0]['age_seconds'])}"
    report.set_check(
        "processes",
        f"{len(leaks)} leaks; {len(in_tree)} live in-tree processes"
        f"{oldest} (context, not findings)",
        leaks=len(leaks),
        live_in_tree=len(in_tree),
        orphan_minutes=orphan_minutes,
        crates_root=str(report.crate_root),
        live=in_tree,
    )


# --------------------------------------------------------------------------
# check 3: scratch and disk
# --------------------------------------------------------------------------


def directory_size(path: Path) -> int | None:
    """Size of ``path`` in bytes, or ``None`` when it cannot be measured."""
    if shutil.which("du"):
        result = _run(["du", "-sk", str(path)], DU_TIMEOUT_SECONDS)
        if result is not None and result.returncode == 0 and result.stdout.split():
            try:
                return int(result.stdout.split()[0]) * 1024
            except ValueError:
                return None
        return None
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def repo_markers(path: Path, max_depth: int = 2) -> list[str]:
    """``.git``/``.jj`` stores at or just below ``path`` (shallow, early-exit)."""
    found: list[str] = []
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name in (".jj", ".git"):
                found.append(entry.path)
                continue
            if entry.is_dir(follow_symlinks=False) and depth + 1 < max_depth:
                stack.append((Path(entry.path), depth + 1))
    return found


def scratch_items(crate_root: Path, crates: list[str]) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for crate in crates:
        target = crate_root / crate / "target"
        if target.is_dir():
            items.append(("target", target))
    if crate_root.is_dir():
        for child in sorted(crate_root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.name.startswith("."):
                items.append(("scratch", child))
    return items


def check_scratch(
    report: Report,
    *,
    limit_gb: float,
    age_days: float,
    min_free_gb: float,
    now: float,
) -> None:
    limit = limit_gb * 1024.0**3
    items = scratch_items(report.crate_root, report.crates)
    recorded: list[dict] = []
    target_total = 0
    scratch_total = 0
    for kind, path in items:
        size = directory_size(path)
        try:
            age = now - path.stat().st_mtime
        except OSError:
            age = None
        markers = repo_markers(path) if kind == "scratch" else []
        if size is not None:
            if kind == "target":
                target_total += size
            else:
                scratch_total += size
        entry = {
            "kind": kind,
            "path": str(path),
            "size_bytes": size,
            "age_seconds": age,
            "repository_markers": markers,
        }
        recorded.append(entry)
        if size is None:
            report.add(
                "scratch",
                ADVISORY,
                f"{path}: size unavailable",
                "the size measuring command failed or timed out",
            )
            continue
        reasons: list[str] = []
        if size > limit:
            reasons.append(f"{format_bytes(size)} exceeds the {limit_gb:g} GiB limit")
        if age is not None and age > age_days * 86400.0:
            reasons.append(f"{format_duration(age)} old (limit {age_days:g} days)")
        if markers:
            reasons.append(
                "contains a repository store ("
                + ", ".join(sorted(os.path.relpath(m, path) for m in markers)[:3])
                + "): deleting it destroys copied history"
            )
        if reasons:
            report.add("scratch", ADVISORY, f"{path}", "; ".join(reasons))

    free = None
    total = None
    try:
        usage = shutil.disk_usage(report.crate_root)
        free, total = usage.free, usage.total
    except OSError:
        pass
    if free is not None and free < min_free_gb * 1024.0**3:
        report.add(
            "scratch",
            ADVISORY,
            f"low free disk space: {format_bytes(free)} free",
            f"below the {min_free_gb:g} GiB limit on {report.crate_root}",
        )
    free_text = format_bytes(free) if free is not None else "unknown"
    total_text = format_bytes(total) if total is not None else "unknown"
    report.set_check(
        "scratch",
        f"target {format_bytes(target_total)} in {sum(1 for k, _ in items if k == 'target')} dirs; "
        f"scratch {format_bytes(scratch_total)} in {sum(1 for k, _ in items if k == 'scratch')} dirs; "
        f"free {free_text} of {total_text}",
        target_bytes=target_total,
        scratch_bytes=scratch_total,
        free_bytes=free,
        total_bytes=total,
        limit_gb=limit_gb,
        age_days=age_days,
        min_free_gb=min_free_gb,
        items=recorded,
    )


# --------------------------------------------------------------------------
# check 4: repository state
# --------------------------------------------------------------------------


def resolve_bookmark(repo: Path, bookmark: str, ignore_working_copy: bool):
    result = run_jj(
        repo,
        ["log", "-r", bookmark, "--no-graph", "-T", "commit_id.short()"],
        ignore_working_copy=ignore_working_copy,
    )
    if result is None:
        return None, "jj could not be run"
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()
        return None, (message[0] if message else "jj failed")
    return result.stdout.strip(), None


def conflicted_commits(repo: Path, ignore_working_copy: bool):
    result = run_jj(
        repo,
        ["log", "-r", "conflicts()", "--no-graph", "-T", CONFLICT_TEMPLATE],
        ignore_working_copy=ignore_working_copy,
    )
    if result is None:
        return None, "jj could not be run"
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()
        return None, (message[0] if message else "jj failed")
    return [line for line in result.stdout.splitlines() if line.strip()], None


def working_copy_state(repo: Path, ignore_working_copy: bool):
    result = run_jj(
        repo,
        ["log", "-r", "@", "--no-graph", "-T", WORKING_COPY_TEMPLATE],
        ignore_working_copy=ignore_working_copy,
    )
    if result is None:
        return None, "jj could not be run"
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()
        return None, (message[0] if message else "jj failed")
    fields = result.stdout.strip().split("\t")
    if len(fields) < 3:
        return None, "unexpected `jj log` output"
    return (
        {
            "empty": fields[0].strip() == "true",
            "conflict": fields[1].strip() == "true",
            "change_id": fields[2].strip(),
            "description": fields[3].strip() if len(fields) > 3 else "",
        },
        None,
    )


def working_copy_diff_summary(repo: Path, ignore_working_copy: bool) -> str | None:
    result = run_jj(
        repo, ["diff", "--stat"], ignore_working_copy=ignore_working_copy
    )
    if result is None or result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def bookmark_conflict_text(repo: Path, ignore_working_copy: bool) -> list[str]:
    result = run_jj(
        repo,
        ["bookmark", "list", "--all-remotes"],
        ignore_working_copy=ignore_working_copy,
    )
    if result is None or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if "conflict" in line.lower()]


def inspect_repo(repo: Path, bookmark: str, ignore_working_copy: bool) -> dict:
    state: dict = {"repo": str(repo)}
    state["bookmark"], state["bookmark_error"] = resolve_bookmark(
        repo, bookmark, ignore_working_copy
    )
    state["conflicts"], state["conflicts_error"] = conflicted_commits(
        repo, ignore_working_copy
    )
    state["working_copy"], state["working_copy_error"] = working_copy_state(
        repo, ignore_working_copy
    )
    state["dirty_stat"] = None
    if state["working_copy"] is not None and not state["working_copy"]["empty"]:
        state["dirty_stat"] = working_copy_diff_summary(repo, ignore_working_copy)
    state["bookmark_conflicts"] = bookmark_conflict_text(repo, ignore_working_copy)
    return state


def evaluate_repo_state(report: Report, crate: str, state: dict) -> None:
    bookmark = report.bookmark
    if state.get("bookmark_error"):
        report.add(
            "repo-state",
            BLOCKING,
            f"{crate}: '{bookmark}' does not resolve",
            state["bookmark_error"],
        )
    if state.get("conflicts_error"):
        report.add(
            "repo-state",
            BLOCKING,
            f"{crate}: could not list conflicted commits",
            state["conflicts_error"],
        )
    conflicts = state.get("conflicts") or []
    if conflicts:
        report.add(
            "repo-state",
            BLOCKING,
            f"{crate}: {len(conflicts)} conflicted commit(s)",
            "; ".join(conflicts[:5]) + (" ..." if len(conflicts) > 5 else ""),
        )
    for line in state.get("bookmark_conflicts") or []:
        report.add("repo-state", BLOCKING, f"{crate}: conflicted bookmark", line.strip())
    working_copy = state.get("working_copy")
    if state.get("working_copy_error"):
        report.add(
            "repo-state",
            BLOCKING,
            f"{crate}: could not read the working copy",
            state["working_copy_error"],
        )
    elif working_copy is not None and not working_copy["empty"]:
        stat = state.get("dirty_stat") or "change set is non-empty"
        report.add(
            "repo-state",
            BLOCKING,
            f"{crate}: working copy has uncommitted changes",
            f"{working_copy['change_id']} ({stat})",
        )
        if working_copy["conflict"]:
            report.add(
                "repo-state",
                BLOCKING,
                f"{crate}: working copy is conflicted",
                working_copy["change_id"],
            )


def check_repo_state(report: Report, *, ignore_working_copy: bool) -> None:
    states: "dict[str, dict]" = {}
    clean = 0
    for crate in report.crates:
        repo = report.crate_root / crate
        if not (repo / ".jj" / "repo").exists():
            continue
        state = inspect_repo(repo, report.bookmark, ignore_working_copy)
        states[crate] = state
        evaluate_repo_state(report, crate, state)
        if (
            not state.get("bookmark_error")
            and not state.get("conflicts_error")
            and not state.get("conflicts")
            and not state.get("bookmark_conflicts")
            and state.get("working_copy") is not None
            and state["working_copy"]["empty"]
        ):
            clean += 1
    conflicted = sum(len(state.get("conflicts") or []) for state in states.values())
    dirty = sum(
        1
        for state in states.values()
        if state.get("working_copy") is not None and not state["working_copy"]["empty"]
    )
    unresolved = sum(1 for state in states.values() if state.get("bookmark_error"))
    report.set_check(
        "repo-state",
        f"{clean}/{len(states)} clean; '{report.bookmark}' resolves in "
        f"{len(states) - unresolved}/{len(states)}; {conflicted} conflicted "
        f"commit(s); {dirty} dirty working copy/copies"
        + ("" if ignore_working_copy else " (after snapshot)"),
        bookmark=report.bookmark,
        checked=len(states),
        clean=clean,
        snapshotted=not ignore_working_copy,
        repos=states,
    )


# --------------------------------------------------------------------------
# check 5: agent task logs
# --------------------------------------------------------------------------


def agent_log_entries(directory: Path, now: float) -> list[dict]:
    entries: list[dict] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return entries
    for child in children:
        if not child.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": str(child),
                "name": child.name,
                "size_bytes": stat.st_size,
                "age_seconds": now - stat.st_mtime,
            }
        )
    return entries


def check_agent_logs(
    report: Report, *, directory: Path | None, stale_minutes: float, now: float
) -> None:
    if directory is None:
        report.skip(
            "agent logs: not checked (pass --agent-logs DIR; there is no fixed "
            "location for them and guessing one would check nothing real)"
        )
        report.checks["agent-logs"] = {"summary": "skipped (no --agent-logs DIR)", "status": "skipped"}
        return
    if not directory.is_dir():
        report.add(
            "agent-logs",
            BLOCKING,
            f"{directory} is not a directory",
            "--agent-logs must name a directory of agent task logs",
        )
        report.set_check(
            "agent-logs",
            f"missing: {directory}",
            status="missing",
            directory=str(directory),
        )
        return
    entries = agent_log_entries(directory, now)
    stalled = [
        entry
        for entry in entries
        if entry["age_seconds"] > stale_minutes * 60.0
    ]
    for entry in stalled:
        report.add(
            "agent-logs",
            ADVISORY,
            f"{entry['name']}: no writes for {format_duration(entry['age_seconds'])}",
            "stalled past the "
            f"{stale_minutes:g} minute threshold — verify the agent is alive "
            "before trusting it",
        )
    entries.sort(key=lambda entry: entry["age_seconds"], reverse=True)
    report.set_check(
        "agent-logs",
        f"{len(entries)} logs; {len(stalled)} stalled past {stale_minutes:g} min",
        directory=str(directory),
        count=len(entries),
        stalled=len(stalled),
        stale_minutes=stale_minutes,
        logs=entries,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 64 so they cannot be read as a blocking finding."""

    def error(self, message: str):  # pragma: no cover - exercised via CLI test
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(64)


def parse_crates(text: str) -> list[str]:
    names = [part.strip() for part in text.replace(",", " ").split()]
    if not names:
        raise argparse.ArgumentTypeError("at least one crate name is required")
    for name in names:
        if os.sep in name or name in (".", ".."):
            raise argparse.ArgumentTypeError(f"{name!r} is not a crate name")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Report the hygiene state of the crate tree: jj workspace "
            "registrations, leaked processes, scratch and disk, repository "
            "state, and (optionally) agent task-log staleness. Report-only: it "
            "never deletes, moves, forgets or kills anything."
        ),
        epilog=(
            "exit status: 0 clean, 1 advisory findings only, 2 blocking "
            "findings, 64 usage error.  A non-zero exit is meant to be "
            "resolved before spawning more work.  Cleanup stays a human "
            "decision, taken on a path this tool has printed.  Read-only by "
            "default: jj runs with --ignore-working-copy, so the "
            "working-copy check sees the last snapshot; pass --snapshot to let "
            "jj refresh each working copy first (a filesystem walk)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--crate-root",
        type=Path,
        default=DEFAULT_CRATE_ROOT,
        help=(
            "directory holding the crate repositories "
            f"(default: derived from this file's location, {DEFAULT_CRATE_ROOT})"
        ),
    )
    parser.add_argument(
        "--crates",
        type=parse_crates,
        default=list(DEFAULT_CRATES),
        help=(
            "comma-separated in-scope crate names, each a subdirectory of the "
            "crate root (default: " + ", ".join(DEFAULT_CRATES) + ")"
        ),
    )
    parser.add_argument(
        "--bookmark",
        default=DEFAULT_BOOKMARK,
        help=f"bookmark that must resolve in every in-scope crate (default: {DEFAULT_BOOKMARK})",
    )
    parser.add_argument(
        "--scratch-limit-gb",
        type=float,
        default=DEFAULT_SCRATCH_LIMIT_GB,
        help=(
            "flag a single scratch item (a crate's target directory, or a "
            "dot-prefixed directory under the crate root) larger than this "
            f"(default: {DEFAULT_SCRATCH_LIMIT_GB:g} GiB)"
        ),
    )
    parser.add_argument(
        "--scratch-age-days",
        type=float,
        default=DEFAULT_SCRATCH_AGE_DAYS,
        help=(
            "flag a scratch item not modified for longer than this "
            f"(default: {DEFAULT_SCRATCH_AGE_DAYS:g} days)"
        ),
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=(
            "flag the crate root's filesystem when free space drops below this "
            f"(default: {DEFAULT_MIN_FREE_GB:g} GiB)"
        ),
    )
    parser.add_argument(
        "--stale-minutes",
        type=float,
        default=DEFAULT_STALE_MINUTES,
        help=(
            "with --agent-logs, flag a log not written for longer than this "
            f"(default: {DEFAULT_STALE_MINUTES:g} minutes)"
        ),
    )
    parser.add_argument(
        "--orphan-minutes",
        type=float,
        default=DEFAULT_ORPHAN_MINUTES,
        help=(
            "flag an in-tree process with no live parent once it is older than "
            f"this (default: {DEFAULT_ORPHAN_MINUTES:g} minutes; 0 flags any "
            "orphan)"
        ),
    )
    parser.add_argument(
        "--agent-logs",
        type=Path,
        default=None,
        help=(
            "directory of agent task logs to age-check; without it the check "
            "is skipped (there is no fixed location to guess)"
        ),
    )
    parser.add_argument(
        "--process-table",
        type=Path,
        default=None,
        help=(
            "read `ps -axo pid=,ppid=,pcpu=,etime=,args=` output from this "
            "file instead of running ps (for tests and for replaying a "
            "captured host state)"
        ),
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help=(
            "let jj snapshot each working copy before checking it; slower, and "
            "it writes to the repositories' operation logs"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the whole report as JSON instead of text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = Report(Path(args.crate_root).resolve(), list(args.crates), args.bookmark)
    ignore_working_copy = not args.snapshot

    check_workspaces(report, ignore_working_copy=ignore_working_copy)
    check_processes(
        report, process_table=args.process_table, orphan_minutes=args.orphan_minutes
    )
    now = time.time()
    check_scratch(
        report,
        limit_gb=args.scratch_limit_gb,
        age_days=args.scratch_age_days,
        min_free_gb=args.min_free_gb,
        now=now,
    )
    check_repo_state(report, ignore_working_copy=ignore_working_copy)
    check_agent_logs(
        report, directory=args.agent_logs, stale_minutes=args.stale_minutes, now=now
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        # Keep stdout free of partial output on a BrokenPipe.
        try:
            print(report.render())
        except BrokenPipeError:  # pragma: no cover
            return report.exit_code()
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
