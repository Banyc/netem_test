#!/usr/bin/env python3
"""Verify the netem_test scenario gate manifest in tests/GATE.md.

`cargo test -p tests` silently skips every `#[ignore]`d scenario, so the set of
opt-in scenarios and their tiers is recorded in tests/GATE.md. This script
re-derives that set from the compiled test binaries and exits non-zero when the
manifest and reality disagree, so a scenario can never be added, removed, or
re-ignored without the gate documentation being updated.

It also enforces the `gate-default-required` block: the asserting scenarios
that MUST run in the default (`cargo test`, non-`#[ignore]`d) tier. The default
tier is defined by the absence of `#[ignore]`, so without this an asserting
scenario can silently be re-ignored and stop running.

Finally it enforces the report-only/asserting split. `standard` and `full`
scenarios assert a property; `perf` scenarios are report-only by definition and
must not contain an assertion in their own body. The `gate-asserting` block
records the asserting scenarios, and it must equal the tier-derived set
(every `standard`/`full` scenario plus every `gate-default-required` entry).
Each `perf` scenario's own source body is scanned for assertion tokens, so an
asserting check filed under the report-only tier -- which would never run -- is
an error that names the scenario.

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
ASSERTING_TIERS = {"standard", "full"}
ASSERTION_TOKENS = re.compile(
    r"(assert!|assert_eq!|assert_ne!|panic!|unreachable!)"
)
FN_RE = re.compile(r"\b(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\(")


def manifest_block(name: str) -> str | None:
    """Return the body of the ```<name> fenced block, or None."""
    text = MANIFEST.read_text(encoding="utf-8")
    block = re.search(rf"```{re.escape(name)}\n(.*?)```", text, re.S)
    return block.group(1) if block else None


def manifest_entries() -> dict[str, str]:
    block = manifest_block("gate-manifest")
    if block is None:
        sys.exit(f"{MANIFEST}: no ```gate-manifest block found")
    entries: dict[str, str] = {}
    for raw in block.splitlines():
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


def required_default_entries() -> list[str]:
    """`target::test` names that must run in the default (non-ignored) tier."""
    block = manifest_block("gate-default-required")
    if block is None:
        return []
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def asserting_entries() -> list[str]:
    """`target::test` names recorded as asserting a gate property."""
    block = manifest_block("gate-asserting")
    if block is None:
        sys.exit(f"{MANIFEST}: no ```gate-asserting block found")
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_bodies(target: str) -> dict[str, str]:
    """Map each top-level `fn NAME` to its brace-balanced body."""
    text = (TEST_DIR / f"{target}.rs").read_text(encoding="utf-8")
    bodies: dict[str, str] = {}
    for match in FN_RE.finditer(text):
        start = text.find("{", match.end())
        if start == -1:
            continue
        depth = 0
        idx = start
        while idx < len(text):
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            idx += 1
        bodies.setdefault(match.group(1), text[start : idx + 1])
    return bodies


def body_asserts(target: str, name: str, bodies: dict[str, str]) -> bool:
    """True when the test function's own body contains an assertion token."""
    body = bodies.get(name)
    return bool(body) and ASSERTION_TOKENS.search(body) is not None


def listed_scenarios(target: str, *, ignored: bool) -> set[str]:
    cmd = ["cargo", "test", "-p", "tests", "--test", target, "--", "--list"]
    if ignored:
        cmd.append("--ignored")
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        mode = " --list --ignored" if ignored else " --list"
        sys.exit(f"cargo test --test {target}{mode} failed")
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"(.+): test$", line)
        if not match:
            continue
        name = match.group(1)
        # `support` is shared test scaffolding compiled into every target; its
        # unit tests are not scenarios and are not gated.
        if "::support::" in name or name.startswith("support::"):
            continue
        found.add(f"{target}::{name}")
    return found


def ignored_scenarios(target: str) -> set[str]:
    return listed_scenarios(target, ignored=True)


def main() -> int:
    manifest = manifest_entries()
    targets = sorted(p.stem for p in TEST_DIR.glob("*.rs"))
    actual: set[str] = set()
    for target in targets:
        actual |= ignored_scenarios(target)

    bad = False
    missing = sorted(actual - manifest.keys())
    stale = sorted(manifest.keys() - actual)
    if missing or stale:
        for name in missing:
            print(f"UNCLASSIFIED ignored scenario: {name}")
        for name in stale:
            print(f"STALE manifest entry (no longer ignored): {name}")
        bad = True

    required = required_default_entries()
    for entry in required:
        target, _, name = entry.partition("::")
        if not target or not name:
            print(f"MALFORMED gate-default-required entry: {entry}")
            bad = True
            continue
        default = listed_scenarios(target, ignored=False) - ignored_scenarios(target)
        if entry not in default:
            print(
                f"REQUIRED default scenario is not in the default tier "
                f"(re-ignored or removed?): {entry}"
            )
            bad = True

    # The report-only/asserting split: a scenario asserts a gate iff it is in
    # the default tier, or it is `#[ignore]`d under an asserting opt-in tier
    # (`standard`/`full`). `perf` is report-only by definition. The gate-asserting
    # block records that split and must match it exactly.
    expected_asserting = {
        name for name, tier in manifest.items() if tier in ASSERTING_TIERS
    } | set(required)
    recorded_asserting = asserting_entries()
    if len(recorded_asserting) != len(set(recorded_asserting)):
        print("DUPLICATE entry in gate-asserting")
        bad = True
    recorded_set = set(recorded_asserting)
    for name in sorted(expected_asserting - recorded_set):
        print(f"ASSERTING scenario missing from gate-asserting: {name}")
        bad = True
    for name in sorted(recorded_set - expected_asserting):
        print(
            f"gate-asserting entry is not an asserting scenario "
            f"(perf-tier or unknown): {name}"
        )
        bad = True

    # A `perf` scenario must be report-only: an assertion in its own body is an
    # asserting check filed under the tier that never runs, so it is an error.
    for name, tier in sorted(manifest.items()):
        if tier != "perf":
            continue
        target, _, test = name.partition("::")
        if body_asserts(target, test, test_bodies(target)):
            print(
                f"ASSERTING scenario in report-only perf tier "
                f"(re-tier to standard/full/default or make it report-only): {name}"
            )
            bad = True

    if bad:
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
    print(f"  default-required: {len(required)} asserting scenario(s) present")
    print(f"  gate-asserting: {len(expected_asserting)} asserting scenario(s) recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
