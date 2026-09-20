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
an error that names the scenario, its file, and the token found. The scan
recognises the debug-only assertion forms (`debug_assert!`,
`debug_assert_eq!`, `debug_assert_ne!`) as assertion tokens too: they are
inert in release builds of the scenario, so an author could hide a check
under the report-only tier hoping it is ignored by the gate, and the presence
of any assertion token in a `perf` body violates the report-only contract.

A token in a scenario's own body is not the whole story: an assertion moved one
call away, into a helper the scenario calls, would escape that scan. The
checker therefore also builds a crate-local call graph (regex + brace counting,
no Rust parser) from every `perf` scenario and requires every asserting
function it can reach to be declared in the `gate-perf-guard-helpers` block,
with its assertion-token count. The graph follows the `pub use` shim views in
`tests/tests/support/**` into the kit sources they re-export -- the harness kit
(`netem-test/src/kit/**`, behind the `test-kit` feature) and the rtp layer kit
(`rtp/src/testkit/**`, behind rtp's `testing` feature) -- so a perf scenario's
reach into the relocated helpers is still declared. An unrecorded asserting
helper, a changed token count, or a stale entry is an error.

It also checks the perf-loop lane roles in the `gate-lane-roles` block against
`perf_loop.lane_classification`, the function that stamps `link_role` into a
run's `run.json`. A lane is either a verdict instrument or diagnostic-only; a
verdict lane mis-declared as diagnostic (or the reverse), an unlisted lane, or
a `hostile` lane that is no longer diagnostic-only is an error. `hostile`
returned `not_ready` (`within_run_phase_not_stable`) in all 70 recorded runs, so
it must never be read as a verdict.

Usage:
    python3 tools/check-gate.py
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tests" / "GATE.md"
PERF_LOOP = Path(__file__).resolve().parent / "perf_loop.py"
TEST_DIR = REPO / "tests" / "tests"
TIERS = {"standard", "full", "perf"}
LANE_ROLES = {"verdict", "diagnostic"}
ASSERTING_TIERS = {"standard", "full"}
ASSERTION_TOKENS = re.compile(
    r"(debug_assert_ne!|debug_assert_eq!|debug_assert!|assert_ne!|assert_eq!|assert!|panic!|unreachable!)"
)
FN_RE = re.compile(
    r"\b(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\("
)
CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)\s*\(")
SUPPORT_DIR = TEST_DIR / "support"
PKG_DIR = REPO / "tests"
# The harness kit sources: the generic scenario helpers moved out of
# `tests/tests/support/**` into the leaf harness crate behind the `test-kit`
# feature (`netem-test/src/kit/**`). The support modules are now `pub use`
# shim views of these files, so the call graph must reach into them to keep
# the perf tier's transitive assertion scan honest.
KIT_DIR = REPO / "netem-test" / "src" / "kit"
# The rtp layer kit: the rtp echo/connect/sink/frame/perf-trace scaffolding
# relocated into the owning crate behind its `testing` feature
# (`rtp/src/testkit/**`), reachable from the scenario crate through the
# `support/rtp.rs`, `support/frame.rs` shims. `tests` already path-depends on
# the sibling `rtp` checkout (Cargo cannot build the suite without it), so
# scanning its kit source adds no new coupling.
RTP_KIT_DIR = REPO.parent / "rtp" / "src" / "testkit"
# Bare `use` first segments that name external crate roots in the scanned
# sources: `netem_test::kit::…` and `rtp::testkit::…` from the shims and the
# rtp kit, plus `tokio`/`std` imports. Everything else resolves like rustc
# does for a bare `use` path: against the current module's scope (the kit's
# `pub use task_scope::…` sibling re-exports), so an in-crate bare path is
# never mistaken for an external crate.
EXTERNAL_ROOT_STEMS = {"netem_test", "rtp", "tokio", "std"}


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


def found_tokens(body: str | None) -> list[str]:
    """The assertion tokens in ``body``, in source order."""
    return list(ASSERTION_TOKENS.findall(body or ""))


def body_asserts(target: str, name: str, bodies: dict[str, str]) -> bool:
    """True when the test function's own body contains an assertion token."""
    body = bodies.get(name)
    return bool(body) and ASSERTION_TOKENS.search(body) is not None


def source_module(path: Path) -> str | None:
    """Rust module path of a source file, or None when it is not scanned.

    Scenario targets and the tests crate's `support/**` modules keep the
    historical `""` / `support`-prefixed names. The harness kit sources
    (`netem-test/src/kit/**`) and the rtp layer kit sources
    (`rtp/src/testkit/**`) are registered under their crate-qualified module
    names so the call graph can follow a `pub use` shim view into the kit
    files.
    """
    try:
        parts = path.relative_to(TEST_DIR).parts
    except ValueError:
        for base, prefix in (
            (RTP_KIT_DIR, "rtp::testkit"),
            (KIT_DIR, "netem_test::kit"),
        ):
            try:
                kit_parts = path.relative_to(base).parts
            except ValueError:
                continue
            if len(kit_parts) != 1:
                return None
            stem = kit_parts[0][:-3]
            return prefix if stem == "mod" else f"{prefix}::{stem}"
        return None
    if len(parts) == 1:
        return ""
    if len(parts) == 2 and parts[0] == "support":
        stem = parts[1][:-3]
        return "support" if stem == "mod" else f"support::{stem}"
    return None


def source_identity(path: Path) -> str:
    """Stable identity prefix for a scanned source file.

    Files inside the tests package keep their historical package-relative
    identity (`tests/<file>.rs`, `tests/support/<file>.rs`) so recorded
    manifest entries stay stable across the kit relocation; kit sources are
    identified relative to the workspace family root
    (`netem-test/src/kit/<file>.rs`, `rtp/src/testkit/<file>.rs`).
    """
    try:
        return str(path.relative_to(PKG_DIR))
    except ValueError:
        return str(path.relative_to(REPO.parent))


class SourceFunction:
    """A crate-local function: identity, owning module, name, and body."""

    __slots__ = ("identity", "module", "name", "body")

    def __init__(self, identity: str, module: str, name: str, body: str) -> None:
        self.identity = identity
        self.module = module
        self.name = name
        self.body = body


def parse_functions(path: Path) -> list[SourceFunction]:
    """Crate-local functions in ``path`` with brace-balanced bodies.

    Regex plus brace counting, not a Rust parser: shared by the direct body scan
    and the crate-local call graph so both agree on what a function body is.
    """
    module = source_module(path)
    if module is None:
        return []
    text = path.read_text(encoding="utf-8")
    seen: set[str] = set()
    functions: list[SourceFunction] = []
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
        name = match.group(1)
        ident = f"{source_identity(path)}::{name}"
        if ident in seen:
            continue
        seen.add(ident)
        functions.append(SourceFunction(ident, module, name, text[start : idx + 1]))
    return functions


def normalize_module(base: str, current_module: str) -> str:
    """Resolve a use-path prefix to a crate-root-absolute module path.

    Handles the forms present in the scanned sources: ``crate::...`` (crate
    root), one or more ``super::...`` / ``self::...`` levels (relative to the
    importing module), bare sibling module paths in a `use` declaration
    (resolved like rustc does: against the current module's scope, which is
    how the kit's ``pub use task_scope::…`` sibling re-exports work), and bare
    paths whose first segment names an external crate root
    (``netem_test::kit::…``, ``rtp::testkit::…``) for the `pub use` shim views.
    """
    segments = [segment for segment in base.split("::") if segment]
    if not segments:
        return current_module
    if segments[0] == "crate":
        return "::".join(segments[1:])
    if segments[0] in EXTERNAL_ROOT_STEMS:
        # Rejoin the filtered segments: a brace-form `use` base ends with a
        # trailing `::` (partition on `{`) that must not survive.
        return "::".join(segments)
    current = current_module.split("::") if current_module else []
    index = 0
    while index < len(segments) and segments[index] in ("super", "self"):
        if segments[index] == "super" and current:
            current = current[:-1]
        index += 1
    return "::".join(current + segments[index:])


USE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(.+?);", re.M | re.S)


def parse_imports(text: str, current_module: str) -> tuple[dict[str, str], list[str]]:
    """Map each imported bare name to its module, plus ``super::*``-style globs."""
    imports: dict[str, str] = {}
    globs: list[str] = []
    for match in USE_RE.finditer(text):
        statement = match.group(1).strip()
        if "{" in statement:
            base, _, rest = statement.partition("{")
            inner = rest.rsplit("}", 1)[0]
            base_module = normalize_module(base.strip(), current_module)
        else:
            inner = statement
            base_module = ""
        for item in inner.split(","):
            item = item.split(" as ", 1)[0].strip()
            if not item:
                continue
            if item == "*" or item.endswith("::*"):
                # Brace-form globs (`use support::{*, …}`) carry their base
                # module in `base_module`; the shim views use the non-brace
                # star form (`pub use netem_test::kit::payload::*`), where the
                # whole item is the glob path.
                if not base_module:
                    base_module = normalize_module(
                        item[:-2].strip(), current_module
                    )
                if base_module:
                    globs.append(base_module)
                continue
            if base_module:
                imports[item] = base_module
                continue
            segments = item.split("::")
            if len(segments) < 2:
                continue
            imports[segments[-1]] = normalize_module(
                "::".join(segments[:-1]), current_module
            )
    return imports, globs


def target_source_files(target: str) -> list[Path]:
    """Source files compiled into the ``tests/<target>.rs`` integration target.

    The target includes the crate-local `support/**` modules it declares plus
    the kit source files behind the shim views: the harness kit
    (`netem-test/src/kit/**`) and the rtp layer kit (`rtp/src/testkit/**`).
    The kit files belong to other crates but are scanned so the report-only
    perf tier's reach into them stays declared; `tests` already path-depends
    on the sibling `rtp` checkout, so Cargo assumes exactly that layout.
    """
    files = [TEST_DIR / f"{target}.rs"]
    text = files[0].read_text(encoding="utf-8")
    if re.search(r"^mod support;", text, re.M):
        files.extend(sorted(SUPPORT_DIR.glob("*.rs")))
    files.extend(sorted(KIT_DIR.glob("*.rs")))
    files.extend(sorted(RTP_KIT_DIR.glob("*.rs")))
    return [path for path in files if path.exists()]


def target_functions(paths: list[Path]) -> list[SourceFunction]:
    functions: list[SourceFunction] = []
    for path in paths:
        functions.extend(parse_functions(path))
    return functions


class TargetGraph:
    """Crate-local call graph for one integration target.

    Edges are resolved with the caller file's ``use`` declarations before falling
    back to a bare-name match, so same-named functions in different modules are
    not confused (e.g. ``support::stats::summarize`` vs
    ``support::contested::summarize``). A call whose target still cannot be
    narrowed (no local definition, no import, several same-named functions) keeps
    every candidate, so the graph over-approximates rather than dropping a direct
    call. It cannot see an edge created by passing a function by name, through a
    trait object, or generated by a macro.
    """

    def __init__(self, functions: list[SourceFunction], paths: list[Path]) -> None:
        self.functions = {function.identity: function for function in functions}
        self.by_name: dict[str, list[str]] = {}
        self.by_module_name: dict[tuple[str, str], list[str]] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.globs: dict[str, list[str]] = {}
        for function in functions:
            self.by_name.setdefault(function.name, []).append(function.identity)
            self.by_module_name.setdefault(
                (function.module, function.name), []
            ).append(function.identity)
        for path in paths:
            module = source_module(path)
            if module is None:
                continue
            imports, globs = parse_imports(path.read_text(encoding="utf-8"), module)
            self.imports.setdefault(module, {}).update(imports)
            self.globs.setdefault(module, []).extend(globs)

    def resolve(self, module: str, path: str) -> list[str]:
        """Candidate identities for a call written as ``path(`` inside ``module``."""
        name = path.rsplit("::", 1)[-1]
        if "::" in path:
            target_module = normalize_module(path.rsplit("::", 1)[0], module)
            found = self.by_module_name.get((target_module, name))
            if found:
                return found
        found = self.by_module_name.get((module, name))
        if found:
            return found
        imported = self.imports.get(module, {}).get(name)
        if imported is not None:
            found = self.by_module_name.get((imported, name))
            if not found:
                found = self._through_views(imported, name, set())
            if found:
                return found
        for glob in self.globs.get(module, ()):
            found = self.by_module_name.get((glob, name))
            if found:
                return found
            found = self._through_views(glob, name, set())
            if found:
                return found
        return self.by_name.get(name, [])

    def _through_views(self, module: str, name: str, seen: set[str]) -> list[str]:
        """Resolve ``name`` visible in ``module`` through its re-export views.

        A support shim (`support::stats`) is a `pub use` view of a kit module
        (`netem_test::kit::stats`) and the kit module itself re-exports from
        its children (`pub use task_scope::…`), so a name imported into a view
        resolves several hops away even though each view defines nothing. The
        lookup follows the view's own imports and globs, cycle-guarded, so the
        graph does not dead-end at the shim.
        """
        if module in seen:
            return []
        seen = seen | {module}
        imported = self.imports.get(module, {}).get(name)
        if imported is not None:
            found = self.by_module_name.get((imported, name))
            if not found:
                found = self._through_views(imported, name, seen)
            if found:
                return found
        for glob in self.globs.get(module, ()):
            found = self.by_module_name.get((glob, name))
            if found:
                return found
            found = self._through_views(glob, name, seen)
            if found:
                return found
        return []

    def reachable(self, seeds: list[str]) -> set[str]:
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            ident = stack.pop()
            function = self.functions.get(ident)
            if function is None or ident in seen:
                continue
            seen.add(ident)
            for call in CALL_RE.finditer(function.body):
                for callee in self.resolve(function.module, call.group(1)):
                    if callee not in seen:
                        stack.append(callee)
        return seen


def helper_scan(manifest: dict[str, str]) -> tuple[dict[str, int], dict[str, list[str]], list[str]]:
    """Asserting crate functions reachable from the perf tier, with token counts.

    Returns ``(identity -> assertion_count, identity -> tokens,
    unlocatable_perf_scenarios)``.
    """
    perf_by_target: dict[str, list[str]] = {}
    for name, tier in manifest.items():
        if tier == "perf":
            target, _, test = name.partition("::")
            perf_by_target.setdefault(target, []).append(test)
    reachable: dict[str, int] = {}
    tokens: dict[str, list[str]] = {}
    unlocatable: list[str] = []
    for target, tests in sorted(perf_by_target.items()):
        paths = target_source_files(target)
        graph = TargetGraph(target_functions(paths), paths)
        seeds = []
        for test in tests:
            ident = f"tests/{target}.rs::{test}"
            if ident in graph.functions:
                seeds.append(ident)
            else:
                unlocatable.append(f"{target}::{test}")
        seed_set = set(seeds)
        for ident in graph.reachable(seeds):
            if ident in seed_set:
                continue
            body = graph.functions[ident].body
            count = len(ASSERTION_TOKENS.findall(body))
            if count:
                reachable[ident] = max(reachable.get(ident, 0), count)
                found = found_tokens(body)
                if len(found) > len(tokens.get(ident, [])):
                    tokens[ident] = found
    return reachable, tokens, unlocatable


def recorded_perf_guard_helpers() -> dict[str, int]:
    """`RELATIVE_PATH::fn = assertion_count` entries from GATE.md."""
    block = manifest_block("gate-perf-guard-helpers")
    if block is None:
        sys.exit(f"{MANIFEST}: no ```gate-perf-guard-helpers block found")
    recorded: dict[str, int] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, count = line.partition(" = ")
        ident, count = ident.strip(), count.strip()
        try:
            parsed = int(count)
        except ValueError:
            sys.exit(f"{MANIFEST}: malformed perf guard helper entry: {line!r}")
        if ident in recorded:
            sys.exit(f"{MANIFEST}: duplicate perf guard helper {ident}")
        recorded[ident] = parsed
    return recorded


def load_perf_loop():
    """Import tools/perf_loop.py without letting its CLI run."""
    spec = importlib.util.spec_from_file_location("perf_loop", PERF_LOOP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lane_role_entries() -> dict[str, str]:
    """`lane = role` entries from the ```gate-lane-roles block in GATE.md."""
    block = manifest_block("gate-lane-roles")
    if block is None:
        sys.exit(f"{MANIFEST}: no ```gate-lane-roles block found")
    roles: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lane, _, role = line.partition(" = ")
        lane, role = lane.strip(), role.strip()
        if role not in LANE_ROLES:
            sys.exit(f"{MANIFEST}: lane {lane} has unknown role {role!r}")
        if lane in roles:
            sys.exit(f"{MANIFEST}: duplicate lane entry {lane}")
        roles[lane] = role
    return roles


def check_lane_roles() -> tuple[dict[str, str], list[str]]:
    """Cross-check the documented perf-loop lane roles against the classifier.

    `perf_loop.lane_classification` is the same function that stamps
    `link_role` into a run's `run.json`, so a lane can never be documented as
    verdict in one place and recorded as diagnostic in the other. `hostile` is
    pinned diagnostic-only: it was `not_ready`
    (`within_run_phase_not_stable`) in all 70 recorded runs, so it can never
    carry a retention verdict.
    """
    perf_loop = load_perf_loop()
    documented = lane_role_entries()
    errors: list[str] = []
    known = set(perf_loop.LINK_PROFILES)
    for lane in sorted(set(documented) - known):
        errors.append(f"gate-lane-roles names unknown --link-profile lane: {lane}")
    for lane in sorted(known - set(documented)):
        errors.append(f"--link-profile lane missing from gate-lane-roles: {lane}")
    for lane in sorted(known & set(documented)):
        expected = perf_loop.lane_classification(lane)
        if documented[lane] != expected:
            errors.append(
                f"lane role mismatch for {lane}: GATE.md says "
                f"{documented[lane]!r}, perf_loop.lane_classification says "
                f"{expected!r}"
            )
    if documented.get("hostile") != "diagnostic":
        errors.append(
            "the hostile lane must be declared diagnostic-only: it returned "
            "not_ready (within_run_phase_not_stable) in all 70 recorded runs"
        )
    if not any(role == "verdict" for role in documented.values()):
        errors.append("no verdict lane is declared")
    return documented, errors


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
        bodies = test_bodies(target)
        if body_asserts(target, test, bodies):
            print(
                f"ASSERTING scenario in report-only perf tier "
                f"(re-tier to standard/full/default or make it report-only): {name} "
                f"[file tests/{target}.rs, token(s): "
                f"{', '.join(sorted(set(found_tokens(bodies.get(test)))))}]"
            )
            bad = True

    # The direct-body scan only sees assertions in a `perf` scenario's own
    # body. Close the one-call-away hole: the crate-local call graph closure of
    # every `perf` scenario may only reach asserting functions that GATE.md
    # declares as report-only guards (`gate-perf-guard-helpers`), with token
    # counts so an assertion added to a guard is caught too.
    derived_helpers, helper_tokens, unlocatable = helper_scan(manifest)
    for name in unlocatable:
        print(
            f"PERF scenario body not found in source (macro-generated or moved?): {name}"
        )
        bad = True
    recorded_helpers = recorded_perf_guard_helpers()
    for ident in sorted(set(derived_helpers) - set(recorded_helpers)):
        print(
            f"PERF scenario reaches asserting helper not recorded as report-only: "
            f"{ident} ({derived_helpers[ident]} assertion token(s): "
            f"{', '.join(helper_tokens[ident])})"
        )
        bad = True
    for ident in sorted(set(recorded_helpers) - set(derived_helpers)):
        print(
            f"recorded perf guard helper is not reachable from any perf scenario "
            f"(stale entry?): {ident}"
        )
        bad = True
    for ident in sorted(set(derived_helpers) & set(recorded_helpers)):
        if derived_helpers[ident] != recorded_helpers[ident]:
            print(
                f"perf guard helper assertion count changed for {ident}: "
                f"recorded {recorded_helpers[ident]}, found {derived_helpers[ident]}"
            )
            bad = True

    # The perf-loop lane roles: a lane is either a verdict instrument or
    # diagnostic-only. The documented roles must match
    # `perf_loop.lane_classification` exactly, and `hostile` must stay
    # diagnostic-only (70/70 `not_ready` in the recorded runs).
    lane_roles, lane_errors = check_lane_roles()
    for error in lane_errors:
        print(error)
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
    print(
        f"  gate-perf-guard-helpers: {len(derived_helpers)} asserting helper(s) "
        f"reachable from the perf tier"
    )
    diagnostic = sum(1 for role in lane_roles.values() if role == "diagnostic")
    print(
        f"  gate-lane-roles: {len(lane_roles)} perf-loop lane(s), "
        f"{diagnostic} diagnostic-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
