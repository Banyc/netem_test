#!/usr/bin/env python3
"""Verify the netem_test scenario gate manifest in tests/GATE.md.

`cargo test -p tests` silently skips every `#[ignore]`d scenario, so the set of
opt-in scenarios and their tiers is recorded in tests/GATE.md. This script
re-derives that set from the compiled test binaries and exits non-zero when the
manifest and reality disagree, so a scenario can never be added, removed, or
re-ignored without the gate documentation being updated.

Usage:
    python3 tools/check-gate.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tests" / "GATE.md"
TEST_DIR = REPO / "tests" / "tests"
TIERS = {"standard", "full", "perf"}


def manifest_entries() -> dict[str, str]:
    text = MANIFEST.read_text(encoding="utf-8")
    block = re.search(r"```gate-manifest\n(.*?)```", text, re.S)
    if not block:
        sys.exit(f"{MANIFEST}: no ```gate-manifest block found")
    entries: dict[str, str] = {}
    for raw in block.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, tier = line.partition(" = ")
        name, tier = name.strip(), tier.strip()
        if tier not in TIERS:
            sys.exit(f"{MANIFEST}: {name} has unknown tier {tier!r}")
        if name in entries:
            sys.exit(f"{MANIFEST}: duplicate entry {name}")
        entries[name] = tier
    return entries


def ignored_scenarios(target: str) -> set[str]:
    proc = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "tests",
            "--test",
            target,
            "--",
            "--list",
            "--ignored",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(f"cargo test --test {target} --list --ignored failed")
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"(.+): test$", line)
        if not match:
            continue
        name = match.group(1)
        if "::support::" in name:
            continue
        found.add(f"{target}::{name}")
    return found


def main() -> int:
    manifest = manifest_entries()
    targets = sorted(p.stem for p in TEST_DIR.glob("*.rs"))
    actual: set[str] = set()
    for target in targets:
        actual |= ignored_scenarios(target)

    missing = sorted(actual - manifest.keys())
    stale = sorted(manifest.keys() - actual)
    if missing or stale:
        for name in missing:
            print(f"UNCLASSIFIED ignored scenario: {name}")
        for name in stale:
            print(f"STALE manifest entry (no longer ignored): {name}")
        print(
            f"\nmanifest has {len(manifest)} entries, binaries report "
            f"{len(actual)} ignored scenarios; update tests/GATE.md",
            file=sys.stderr,
        )
        return 1

    by_tier: dict[str, int] = {}
    for tier in manifest.values():
        by_tier[tier] = by_tier.get(tier, 0) + 1
    print(f"gate manifest OK: {len(actual)} ignored scenarios classified")
    for tier in sorted(by_tier):
        print(f"  {tier}: {by_tier[tier]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
