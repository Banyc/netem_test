#!/usr/bin/env python3
"""Verify a scenario gate manifest (per-crate).

`cargo test` silently skips every `#[ignore]`d scenario, so the set of
opt-in scenarios and their tiers is recorded in a crate's `GATE.md`. This
script re-derives that set from the compiled test binaries and exits non-zero
when the manifest and reality disagree, so a scenario can never be added,
removed, or re-ignored without the gate documentation being updated.

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
with its assertion-token count. The graph follows each `pub use` re-export view
and the direct kit imports through them into the kit sources those views
re-export -- the harness kit
(`netem-test/src/kit/**`, behind the `test-kit` feature), the rtp layer kit
(`rtp/src/testkit/**`, behind rtp's `testing` feature), and the mux layer kit
(`mux/src/testkit/**`, behind mux's `testing` feature) -- so a perf scenario's
reach into the relocated helpers is still declared. An unrecorded asserting
helper, a changed token count, or a stale entry is an error.

The perf-test dual mandate (time and coverage) is checked the same way. When a
crate's `GATE.md` carries the `gate-perf-design`, `gate-budgets` and
`gate-coverage-gaps` blocks, every declared row's `<target>::<test>` must exist
in the tier the row declares (resolved from the compiled test binaries for an
integration target, or from the package's `--lib` target for the reserved
target name `lib`), the sum of the declared nominal costs per tier must fit
that tier's declared budget, and every covered cell and gap reason must be
non-empty and well-formed. When a fresh `mandate-check.json` is supplied
(`--mandate-check-json`, or `mandate-check.json` in the crate root), each
declared row that the report measured per-test is compared with the report's
streamed wall-clock and a drift past the declared tolerance is an error. A
crate that has perf-tier scenarios but no perf blocks is reported with an
advisory note and no failure: the declaration is required but its migration is
visible rather than silently assumed.

In harness mode (no `--crate`) it also checks the perf-loop lane roles in the
`gate-lane-roles` block against `perf_loop.lane_classification`, the function
that stamps `link_role` into a run's `run.json`. A lane is either a verdict
instrument or diagnostic-only; a verdict lane mis-declared as diagnostic (or
the reverse), an unlisted lane, or a `hostile` lane that is no longer
diagnostic-only is an error. `hostile` returned `not_ready`
(`within_run_phase_not_stable`) in all 70 recorded runs, so it must never be
read as a verdict. Per-crate mode (mux and friends) has no perf-loop lanes;
the lane-roles check stays with the harness.

Usage:
    python3 tools/check-gate.py
    # netem_test harness: scenarios in tests/tests, manifest tests/GATE.md
    python3 tools/check-gate.py --crate <root> <package> <dir> <GATE.md>
    # e.g. mux: python3 tools/check-gate.py \
    #   --crate ../mux mux tests GATE.md   (from the netem_test root)
    # with a per-test timing report, the declared costs are also drift-checked:
    python3 tools/check-gate.py --mandate-check-json <run>/mandate-check.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TIERS = {"standard", "full", "perf"}
# The tiers a perf design row may name: the three opt-in tiers plus the
# always-run default tier (a row's `default` tier is resolved from the test not
# being `#[ignore]`d, matching `gate-default-required`).
PERF_TIERS = frozenset(TIERS | {"default"})
LANE_ROLES = {"verdict", "diagnostic"}
# The reserved perf-design target naming a package's `--lib` test target. In
# harness mode it is the `netem-test` package (whose wall-clock probes are lib
# unit tests); in per-crate mode it is the checked package.
LIB_TARGET = "lib"
# `<mandate-or-property>@<dimension>=<value>[+<dimension>=<value>...]`. A value
# may not contain `=`, `,` or `+` (those delimit the cell), and a property is a
# stable name (`M1`, `conformance-delay`, `probe-throughput`).
CELL_PROPERTY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
CELL_DIMENSION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=[^=,+\s]+$")
# The default drift tolerance (relative) and the absolute floor below which a
# difference is not reported, overridable per crate in `gate-budgets`.
DEFAULT_DRIFT_TOLERANCE = 0.5
DEFAULT_DRIFT_FLOOR_SECONDS = 2.0
DEFAULT_REPORT_NAME = "mandate-check.json"
ASSERTING_TIERS = {"standard", "full"}
ASSERTION_TOKENS = re.compile(
    r"(debug_assert_ne!|debug_assert_eq!|debug_assert!|assert_ne!|assert_eq!|assert!|panic!|unreachable!)"
)
FN_RE = re.compile(
    r"\b(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\("
)
CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)\s*\(")


@dataclass(frozen=True)
class CrateLayout:
    """One crate's gate layout: where cargo runs and where the scenarios live.

    ``root`` is the crate workspace root (the `cargo test -p <package>` cwd);
    ``package`` the package name whose test binaries are enumerated; ``dir``
    the scenario target directory (each `*.rs` is one integration target);
    ``manifest`` the `GATE.md` that records the tiers. ``crates_root`` is the
    shared parent of the sibling crate checkouts (`crates/`), which anchors
    the kit-source identities (`mux/src/testkit/…`) equally in every mode.
    Harness mode (`is_harness`) additionally enables the perf-loop lane-role
    check.

    In per-crate mode the checked crate's own layer kit is read from the
    checked root (`<root>/src/testkit`), never from the crates-root sibling:
    a scenario move lands in the checked tree, and the landed sibling may not
    yet contain the kit the moved scenarios call, so resolving the own kit
    from the sibling makes the perf-tier call-graph closure resolve nothing
    and the gate reports success while the same check fails on the landed
    trunk. The sibling kits stay crates-root-anchored (they are compiled
    through path dev-dependencies relative to the checked root, which resolve
    to the crates-root checkouts).

    ``own_kit_dir`` is the checked crate's own layer kit when it is read from
    the checked root; it is `None` in harness mode (the `tests` package has no
    layer kit) and for per-crate checks whose root has no `src/testkit`.
    """

    root: Path
    package: str
    dir: Path
    manifest: Path
    crates_root: Path
    is_harness: bool = False

    @property
    def own_kit_dir(self) -> Path | None:
        if self.package == "tests":
            return None
        own = self.root / "src" / "testkit"
        return own if own.is_dir() else None

    def kit_source_dirs(self) -> list[tuple[Path, str]]:
        """(kit dir, crate-qualified module prefix) pairs scanned per target.

        The checked crate's own kit is read from the same root as its
        scenarios (`own_kit_dir`) so a not-yet-landed kit cannot make the
        perf-tier call-graph closure resolve nothing; the remaining kits
        (harness kit and the other layer kits) resolve from the crates-root
        siblings, matching the path dev-dependencies the checked crate
        compiles against.
        """
        dirs: list[tuple[Path, str]] = []
        if self.own_kit_dir is not None:
            dirs.append((self.own_kit_dir, f"{self.package}::testkit"))
        seen_dirs = {d for d, _ in dirs}
        for kit_dir, prefix in (
            (self.rtp_kit_dir, "rtp::testkit"),
            (self.mux_kit_dir, "mux::testkit"),
            (self.rtp_mux_kit_dir, "rtp_mux::testkit"),
            (self.kit_dir, "netem_test::kit"),
        ):
            if kit_dir not in seen_dirs:
                dirs.append((kit_dir, prefix))
                seen_dirs.add(kit_dir)
        return dirs

    @property
    def lib_package(self) -> str:
        """The package whose `--lib` target the reserved `lib` design rows name.

        In harness mode the harness's own wall-clock probes are `netem-test`'s
        lib unit tests, so the reserved target resolves there; in per-crate
        mode it resolves the checked package, which is the crate whose
        scenarios and lib tests the gate covers.
        """
        return "netem-test" if self.is_harness else self.package

    @property
    def kit_dir(self) -> Path:
        return self.crates_root / "netem_test" / "netem-test" / "src" / "kit"

    @property
    def rtp_kit_dir(self) -> Path:
        return self.crates_root / "rtp" / "src" / "testkit"

    @property
    def mux_kit_dir(self) -> Path:
        return self.crates_root / "mux" / "src" / "testkit"

    @property
    def rtp_mux_kit_dir(self) -> Path:
        return self.crates_root / "rtp_mux" / "src" / "testkit"


def harness_layout(script_dir: Path) -> CrateLayout:
    """The default layout: the netem_test `tests` package."""
    repo = script_dir.parent
    return CrateLayout(
        root=repo,
        package="tests",
        dir=repo / "tests" / "tests",
        manifest=repo / "tests" / "GATE.md",
        crates_root=repo.parent,
        is_harness=True,
    )


PERF_LOOP = Path(__file__).resolve().parent / "perf_loop.py"
# Bare `use` first segments that name external crate roots in the scanned
# sources: `netem_test::kit::…`, `rtp::testkit::…`, `mux::testkit::…` and
# `rtp_mux::testkit::…` from the kit sources and the shims, plus
# `tokio`/`std` imports. Everything else
# resolves like rustc does for a bare `use` path: against the current
# module's scope (the kit's `pub use task_scope::…` sibling re-exports), so
# an in-crate bare path is never mistaken for an external crate.
EXTERNAL_ROOT_STEMS = {"netem_test", "rtp", "mux", "rtp_mux", "tokio", "std"}

LAYOUT: CrateLayout | None = None


def layout() -> CrateLayout:
    assert LAYOUT is not None, "layout() called before main() set it"
    return LAYOUT


def manifest_block(name: str) -> str | None:
    """Return the body of the ```<name> fenced block, or None."""
    text = layout().manifest.read_text(encoding="utf-8")
    block = re.search(rf"```{re.escape(name)}\n(.*?)```", text, re.S)
    return block.group(1) if block else None


def manifest_entries() -> dict[str, str]:
    block = manifest_block("gate-manifest")
    if block is None:
        sys.exit(f"{layout().manifest}: no ```gate-manifest block found")
    entries: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, tier = line.partition(" = ")
        name, tier = name.strip(), tier.strip()
        if tier not in TIERS:
            sys.exit(f"{layout().manifest}: {name} has unknown tier {tier!r}")
        if name in entries:
            sys.exit(f"{layout().manifest}: duplicate entry {name}")
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
        sys.exit(f"{layout().manifest}: no ```gate-asserting block found")
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_bodies(target: str) -> dict[str, str]:
    """Map each top-level `fn NAME` to its brace-balanced body."""
    text = (layout().dir / f"{target}.rs").read_text(encoding="utf-8")
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

    Scenario targets keep the historical `""` module name. The harness kit
    sources (`netem-test/src/kit/**`), the rtp layer kit
    sources (`rtp/src/testkit/**`), the mux layer kit sources
    (`mux/src/testkit/**`) and the rtp_mux layer kit sources
    (`rtp_mux/src/testkit/**`) are registered under their crate-qualified
    module names so the call graph can follow a `pub use` shim view or a
    direct kit import into the kit files.
    """
    try:
        parts = path.relative_to(layout().dir).parts
    except ValueError:
        for base, prefix in layout().kit_source_dirs():
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
    return None


def source_identity(path: Path) -> str:
    """Stable identity prefix for a scanned source file.

    In harness mode files inside the `tests` package keep their historical
    package-relative identity (`tests/<file>.rs`)
    so recorded manifest entries stay stable. Kit sources are identified
    relative to the shared crates root (`netem-test/src/kit/<file>.rs`,
    `rtp/src/testkit/<file>.rs`, `mux/src/testkit/<file>.rs`,
    `rtp_mux/src/testkit/<file>.rs`) so they are stable regardless of which
    crate checkout the checker runs from. In per-crate mode the crate's own
    scenario targets are identified relative to the crate root
    (`tests/<file>.rs`), mirroring the harness mode: a repo can be checked
    out at any sibling path (e.g. `crates/rtp_mux_it178_ws`) while the gate
    is authored and verified, so a `GATE.md` entry recorded against one
    checkout location must not go stale when the same tree lands at
    `crates/<crate>`.
    """
    if layout().is_harness:
        try:
            return str(path.relative_to(layout().root / layout().package))
        except ValueError:
            pass
    # Scenario targets live directly in the crate's `tests/` dir; in per-crate
    # mode they are identified relative to the crate root (`tests/<file>.rs`)
    # so recordings stay stable whether the repo is checked out at
    # `crates/<crate>` or a session worktree. Kit sources (including the
    # checked-out crate's own `src/testkit/**`, which sits under the same
    # root) are identified relative to the shared crates root below, so
    # `mux/src/testkit/mux.rs` never shifts to `src/testkit/mux.rs` or to
    # `<worktree>/src/testkit/mux.rs` depending on where the crate lives.
    if path.parent == layout().dir:
        return str(path.relative_to(layout().root))
    own = layout().own_kit_dir
    if own is not None and path.parent == own:
        # The checked crate's own kit is read from the checked root; record
        # it under the canonical crate name so a GATE.md entry written against
        # one checkout location (e.g. a session worktree) stays valid when
        # the same tree lands at `crates/<crate>`.
        return f"{layout().package}/src/testkit/{path.name}"
    return str(path.relative_to(layout().crates_root))


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
    (``netem_test::kit::…``, ``rtp::testkit::…``, ``mux::testkit::…``) for the
    `pub use` shim views and the scenarios' direct kit imports.
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
                # Brace-form globs (`use view::{*, …}`) carry their base
                # module in `base_module`; the non-brace star form
                # (`pub use netem_test::kit::payload::*`), where the
                # whole item is the glob path. The `::*` suffix is three
                # characters; a `[:-2]` strip would leave a trailing `:` that
                # no longer matches any registered module.
                if not base_module:
                    base_module = normalize_module(
                        item[:-3].strip(), current_module
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
    """Source files compiled into the ``<dir>/<target>.rs`` integration target.

    The kit source files behind the direct imports are always scanned: the
    harness kit (`netem-test/src/kit/**`), the rtp layer kit
    (`rtp/src/testkit/**`), the mux layer kit (`mux/src/testkit/**`) and the
    rtp_mux layer kit (`rtp_mux/src/testkit/**`).
    The kit files belong to other crates but are scanned so the report-only
    perf tier's reach into them stays declared; every crate shares the sibling
    checkout layout, so Cargo assumes exactly that layout.
    """
    files = [layout().dir / f"{target}.rs"]
    for kit_dir, _ in layout().kit_source_dirs():
        files.extend(sorted(kit_dir.glob("*.rs")))
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
    not confused (e.g. ``kit::stats::summarize`` vs
    ``kit::contested::summarize``). A call whose target still cannot be
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
            # A qualified path into a `pub use` re-export view (`view::mux::…`)
            # names a module that defines nothing; follow
            # the view's own re-exports before scattering over every
            # same-named function, so two kits defining the same helper cannot
            # both be dragged into the closure by one view call.
            found = self._through_views(target_module, name, set())
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

        A `pub use` re-export view (`view::stats`) is a view of a kit module
        (`netem_test::kit::stats`) or the mux kit (`mux::testkit::stats`) and
        the kit module itself re-exports from its children
        (`pub use task_scope::…`), so a name imported into a view resolves
        several hops away even though each view defines nothing. The lookup
        follows the view's own imports and globs, cycle-guarded, so the graph
        does not dead-end at the view.
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
            ident = f"{source_identity(layout().dir / f'{target}.rs')}::{test}"
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
        sys.exit(f"{layout().manifest}: no ```gate-perf-guard-helpers block found")
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
            sys.exit(f"{layout().manifest}: malformed perf guard helper entry: {line!r}")
        if ident in recorded:
            sys.exit(f"{layout().manifest}: duplicate perf guard helper {ident}")
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
        sys.exit(f"{layout().manifest}: no ```gate-lane-roles block found")
    roles: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lane, _, role = line.partition(" = ")
        lane, role = lane.strip(), role.strip()
        if role not in LANE_ROLES:
            sys.exit(f"{layout().manifest}: lane {lane} has unknown role {role!r}")
        if lane in roles:
            sys.exit(f"{layout().manifest}: duplicate lane entry {lane}")
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


@dataclass(frozen=True)
class PerfRow:
    """One `gate-perf-design` row: a perf test, its tier, cost and coverage."""

    name: str
    tier: str
    cost: float
    cells: tuple[str, ...]


@dataclass(frozen=True)
class PerfBudgets:
    """The `gate-budgets` block: one budget per tier, plus the baseline row."""

    tiers: dict[str, float]
    baseline: str | None
    drift: float
    drift_floor_seconds: float


@dataclass(frozen=True)
class PerfGap:
    """One `gate-coverage-gaps` line: an uncovered cell and why it is empty."""

    cell: str
    reason: str


def _perf_lines(block: str) -> list[tuple[int, str]]:
    """The non-empty, non-comment lines of a perf block with their numbers."""
    return [
        (number, raw.strip())
        for number, raw in enumerate(block.splitlines(), start=1)
        if raw.strip() and not raw.strip().startswith("#")
    ]


def cell_problem(cell: str) -> str | None:
    """Why ``cell`` is not `<property>@<dimension>=<value>[+...]`, or None."""
    property_name, _, dimensions = cell.partition("@")
    if not dimensions:
        return "no '@<dimension>=<value>' part"
    if not CELL_PROPERTY_RE.match(property_name):
        return f"property {property_name!r} is not a name ([A-Za-z][A-Za-z0-9_.-]*)"
    for part in dimensions.split("+"):
        if not CELL_DIMENSION_RE.match(part):
            return f"dimension {part!r} is not '<dimension>=<value>'"
    return None


def parse_perf_design(text: str, problems: list[str]) -> list[PerfRow]:
    """Parse the `gate-perf-design` rows, naming every malformed one."""
    rows: list[PerfRow] = []
    seen: set[str] = set()
    for number, line in _perf_lines(text):
        name, separator, rest = line.partition(" = ")
        if not separator:
            problems.append(
                "gate-perf-design line "
                f"{number}: {line!r} is not '<target>::<test> = <tier> | "
                "<nominal_cost_s> | <coverage>'",
            )
            continue
        name = name.strip()
        parts = [part.strip() for part in rest.split("|")]
        if len(parts) != 3:
            problems.append(
                f"gate-perf-design row {name}: expected '<tier> | <cost_s> | "
                f"<coverage>', found {len(parts)} field(s)"
            )
            continue
        tier, cost_text, coverage_text = parts
        if tier not in PERF_TIERS:
            problems.append(
                f"gate-perf-design row {name}: unknown tier {tier!r} (one of "
                f"{', '.join(sorted(PERF_TIERS))})"
            )
            continue
        try:
            cost = float(cost_text)
        except ValueError:
            problems.append(
                f"gate-perf-design row {name}: nominal cost {cost_text!r} is not "
                "a number of seconds"
            )
            continue
        if cost < 0:
            problems.append(f"gate-perf-design row {name}: nominal cost {cost} is negative")
            continue
        cells = tuple(cell.strip() for cell in coverage_text.split(",") if cell.strip())
        if not cells:
            problems.append(
                f"gate-perf-design row {name}: no coverage cell; a perf test must "
                "name the cells it covers, and a cell it does not cover belongs "
                "in gate-coverage-gaps with a reason"
            )
        for cell in cells:
            reason = cell_problem(cell)
            if reason is not None:
                problems.append(
                    f"gate-perf-design row {name}: coverage cell {cell!r} is "
                    f"malformed: {reason}"
                )
        if name in seen:
            problems.append(f"gate-perf-design: duplicate row {name}")
        seen.add(name)
        rows.append(PerfRow(name, tier, cost, cells))
    return rows


def parse_perf_budgets(text: str, problems: list[str]) -> PerfBudgets:
    """Parse the `gate-budgets` block: `<tier> = <budget_s>` plus the baseline."""
    tiers: dict[str, float] = {}
    baseline: str | None = None
    drift = DEFAULT_DRIFT_TOLERANCE
    floor = DEFAULT_DRIFT_FLOOR_SECONDS
    for number, line in _perf_lines(text):
        key, separator, value = line.partition(" = ")
        key, value = key.strip(), value.strip()
        if not separator:
            problems.append(
                f"gate-budgets line {number}: {line!r} is not '<tier> = <budget_s>'"
            )
            continue
        if key == "baseline":
            if not value:
                problems.append("gate-budgets: the baseline line names no row")
                continue
            baseline = value
            continue
        if key in ("drift", "drift_floor_s"):
            try:
                parsed = float(value)
            except ValueError:
                problems.append(f"gate-budgets: {key} {value!r} is not a number")
                continue
            if parsed < 0:
                problems.append(f"gate-budgets: {key} {parsed} is negative")
                continue
            if key == "drift":
                drift = parsed
            else:
                floor = parsed
            continue
        if key not in PERF_TIERS:
            problems.append(
                f"gate-budgets line {number}: {key!r} is neither a tier nor one "
                "of baseline/drift/drift_floor_s"
            )
            continue
        if key in tiers:
            problems.append(f"gate-budgets: duplicate budget for tier {key}")
            continue
        try:
            budget = float(value)
        except ValueError:
            problems.append(f"gate-budgets: tier {key} budget {value!r} is not a number")
            continue
        if budget < 0:
            problems.append(f"gate-budgets: tier {key} budget {budget} is negative")
            continue
        tiers[key] = budget
    if baseline is None:
        problems.append(
            "gate-budgets declares no 'baseline = <row>' line; every design row's "
            "coverage must be stated relative to a named baseline row"
        )
    return PerfBudgets(tiers, baseline, drift, floor)


def parse_perf_gaps(text: str, problems: list[str]) -> list[PerfGap]:
    """Parse the `gate-coverage-gaps` lines: `<cell> = <reason>`."""
    gaps: list[PerfGap] = []
    for number, line in _perf_lines(text):
        cell, separator, reason = line.partition(" = ")
        cell, reason = cell.strip(), reason.strip()
        if not separator:
            # A line whose reason is empty (`<cell> =`) still names a cell: it
            # is a reason-less gap, not a line that is not a gap at all.
            if line.endswith("="):
                cell, reason = line[:-1].strip(), ""
            else:
                problems.append(
                    f"gate-coverage-gaps line {number}: {line!r} is not "
                    "'<cell> = <reason>'"
                )
                continue
        if not cell:
            problems.append(f"gate-coverage-gaps line {number}: the gap names no cell")
            continue
        if not reason:
            problems.append(
                f"gate-coverage-gaps: cell {cell!r} records no reason; a cell may "
                "be knowingly empty but never silently empty"
            )
            continue
        problem = cell_problem(cell)
        if problem is not None:
            problems.append(
                f"gate-coverage-gaps: cell {cell!r} is malformed: {problem}"
            )
            continue
        gaps.append(PerfGap(cell, reason))
    return gaps


def read_mandate_report(path: Path, problems: list[str]):
    """The per-test timings of a `mandate-check.json`, or None with a problem."""
    if not path.is_file():
        problems.append(
            f"the mandate-check report {path} does not exist; pass --mandate-check-json "
            "a report this run produced, or omit it to skip the drift comparison"
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        problems.append(f"the mandate-check report {path} cannot be read: {error}")
        return None
    if not isinstance(payload, dict):
        problems.append(
            f"the mandate-check report {path} is not a JSON object, so its "
            "schema and timings cannot be read"
        )
        return None
    schema = payload.get("schema")
    if not isinstance(schema, str) or not schema.startswith("mandate-check/"):
        problems.append(
            f"the mandate-check report {path} declares schema {schema!r}, not a "
            "mandate-check report this checker can read"
        )
        return None
    return payload


def report_timings(report) -> dict[str, float]:
    """`<target>::<test> -> measured seconds` from a report's per-test timings."""
    timings = report.get("timings")
    if not isinstance(timings, dict):
        return {}
    measured: dict[str, float] = {}
    for entry in timings.get("tests") or []:
        if not isinstance(entry, dict):
            continue
        name, target = entry.get("name"), entry.get("target")
        duration = entry.get("duration_seconds")
        if not isinstance(name, str) or not isinstance(target, str):
            continue
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            continue
        measured[f"{target}::{name}"] = float(duration)
    return measured


class TargetListings:
    """The default and `#[ignore]`d test names of a design row's target.

    An integration target of the checked package resolves through
    `cargo test -p <package> --test <target>`; the reserved `lib` target
    resolves through `cargo test -p <lib_package> --lib`. Both invocations are
    cached, so a design row is never resolved twice.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, bool], set[str]] = {}

    def names(self, target: str, *, ignored: bool) -> set[str]:
        key = (target, ignored)
        if key in self._cache:
            return self._cache[key]
        found = listed_test_names(
            target,
            ignored=ignored,
            lib=(target == LIB_TARGET),
            package=layout().lib_package if target == LIB_TARGET else None,
        )
        self._cache[key] = found
        return found

    def tier(self, name: str, manifest: dict[str, str]) -> str | None:
        """The tier the compiled test set puts ``name`` in, or None if unknown."""
        target, _, _test = name.partition("::")
        # `--list` names every test, ignored or not; the default tier is what
        # remains after the `#[ignore]`d set is removed.
        default = self.names(target, ignored=False) - self.names(target, ignored=True)
        ignored = self.names(target, ignored=True)
        if target == LIB_TARGET:
            # A lib target has no `gate-manifest` of its own; its tier is read
            # from whether the test is `#[ignore]`d. An ignored lib test is
            # report-only unless the crate's manifest says otherwise.
            if name in default:
                return manifest.get(name, "default")
            if name in ignored:
                return manifest.get(name, "perf")
            return None
        if name in default:
            return "default"
        if name in manifest:
            return manifest[name]
        if name in ignored:
            return None
        return None


def check_perf_gate(
    manifest: dict[str, str], report_path: Path | None
) -> tuple[list[str], list[str], list[str]]:
    """Check the perf-design/budgets/coverage-gaps blocks of this GATE.md.

    Returns ``(problems, summary_lines, notes)``. The notes are advisory (an
    undeclared crate, a report without per-test timings) and never fail the
    check; every problem is named and fails it.
    """
    problems: list[str] = []
    notes: list[str] = []
    design_block = manifest_block("gate-perf-design")
    budgets_block = manifest_block("gate-budgets")
    gaps_block = manifest_block("gate-coverage-gaps")
    perf_tier = sorted(name for name, tier in manifest.items() if tier == "perf")
    if design_block is None:
        if budgets_block is not None or gaps_block is not None:
            problems.append(
                "gate-perf-design is missing while gate-budgets/gate-coverage-gaps "
                "is present: the perf declaration must be complete or absent"
            )
        elif perf_tier:
            notes.append(
                f"note: {layout().package} declares {len(perf_tier)} perf-tier "
                "scenario(s) but no ```gate-perf-design block; its perf-test "
                "time/coverage declaration is PENDING (advisory, not a failure)"
            )
        return problems, [], notes
    for name, block in (("gate-budgets", budgets_block), ("gate-coverage-gaps", gaps_block)):
        if block is None:
            problems.append(
                f"gate-perf-design is present without a ```{name} block"
            )
    rows = parse_perf_design(design_block, problems)
    budgets = parse_perf_budgets(budgets_block or "", problems)
    gaps = parse_perf_gaps(gaps_block or "", problems)

    listings = TargetListings()
    for row in rows:
        target = row.name.partition("::")[0]
        actual = listings.tier(row.name, manifest)
        if actual is None:
            problems.append(
                f"gate-perf-design row {row.name}: the {target!r} target does "
                "not report this test (an unknown target or an unknown test is "
                "a failure)"
            )
            continue
        if actual != row.tier:
            problems.append(
                f"gate-perf-design row {row.name} declares tier {row.tier!r} but "
                f"the test set puts it in {actual!r}"
            )

    by_tier: dict[str, list[PerfRow]] = {}
    for row in rows:
        by_tier.setdefault(row.tier, []).append(row)
    for tier in sorted(by_tier):
        total = sum(row.cost for row in by_tier[tier])
        budget = budgets.tiers.get(tier)
        if budget is None:
            problems.append(
                f"gate-perf-design uses the {tier} tier but gate-budgets declares "
                f"no budget for it; {len(by_tier[tier])} row(s) totalling "
                f"{total:.2f}s cannot be paid for"
            )
        elif total > budget:
            names = ", ".join(row.name for row in by_tier[tier])
            problems.append(
                f"gate-perf-design declares {total:.2f}s in the {tier} tier, over "
                f"its {budget:.2f}s budget ({names}); retier a test, lower a "
                "cost, or raise the budget as a declared change"
            )
    if budgets.baseline is not None and budgets.baseline not in {row.name for row in rows}:
        problems.append(
            f"gate-budgets: baseline {budgets.baseline!r} is not a gate-perf-design "
            "row, so the rows' coverage is stated against nothing"
        )

    measured = {}
    if report_path is not None:
        report = read_mandate_report(report_path, problems)
        if report is not None:
            if report_path.stat().st_mtime < layout().manifest.stat().st_mtime:
                notes.append(
                    f"note: {report_path} predates {layout().manifest}; its "
                    "per-test timings are stale and the drift comparison is skipped"
                )
            else:
                measured = report_timings(report)
                compared = 0
                for row in rows:
                    seconds = measured.get(row.name)
                    if seconds is None:
                        continue
                    compared += 1
                    delta = seconds - row.cost
                    relative = delta / row.cost if row.cost > 0 else math.inf
                    if (
                        abs(delta) > budgets.drift_floor_seconds
                        and abs(relative) > budgets.drift
                    ):
                        problems.append(
                            f"measured/declared drift for {row.name}: declared "
                            f"{row.cost:.2f}s, measured {seconds:.2f}s "
                            f"({relative:+.0%}, tolerance {budgets.drift:.0%}, "
                            f"floor {budgets.drift_floor_seconds:.1f}s)"
                        )
                    budget = budgets.tiers.get(row.tier)
                    if budget is not None and seconds > budget:
                        problems.append(
                            f"{row.name} measured {seconds:.2f}s, over its "
                            f"{row.tier} tier budget {budget:.2f}s"
                        )
                if not measured:
                    notes.append(
                        f"note: {report_path} carries no per-test timings "
                        f"(schema {report.get('schema')!r}); the declared costs "
                        "are not drift-checked"
                    )
                else:
                    notes.append(
                        f"note: drift compared {compared} of {len(rows)} declared "
                        f"row(s) against {report_path} (tolerance "
                        f"{budgets.drift:.0%}, floor "
                        f"{budgets.drift_floor_seconds:.1f}s)"
                    )

    cells = sum(len(row.cells) for row in rows)
    summary = [
        f"  gate-perf-design: {len(rows)} perf test row(s), {cells} coverage "
        f"cell(s), {len(gaps)} gap(s), baseline "
        f"{budgets.baseline or 'unset'}"
    ]
    for tier in sorted(by_tier):
        total = sum(row.cost for row in by_tier[tier])
        budget = budgets.tiers.get(tier)
        summary.append(
            f"  gate-budgets: {tier} {total:.2f}/{budget:.2f}s"
            if budget is not None
            else f"  gate-budgets: {tier} {total:.2f}/no budget"
        )
    return problems, summary, notes


def listed_test_names(
    target: str, *, ignored: bool, lib: bool = False, package: str | None = None
) -> set[str]:
    """The `<target>::<test>` names cargo reports for one test target.

    ``lib`` selects `<package> --lib` (the reserved perf-design target) instead
    of `--test <target>`; ``package`` defaults to the checked package. A cargo
    failure is a named non-zero exit: without the test list a design row cannot
    be resolved at all.
    """
    package = package or layout().package
    where = ["--lib"] if lib else ["--test", target]
    cmd = ["cargo", "test", "-p", package, *where, "--", "--list"]
    if ignored:
        cmd.append("--ignored")
    proc = subprocess.run(cmd, cwd=layout().root, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        mode = " --list --ignored" if ignored else " --list"
        sys.exit(f"cargo test -p {package} {' '.join(where)}{mode} failed")
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"(.+): test$", line)
        if not match:
            continue
        name = match.group(1)
        # A `support` module compiled into a per-crate scenario target is
        # shared test scaffolding; its unit tests are not scenarios and are not
        # gated. The harness itself no longer carries one.
        if "::support::" in name or name.startswith("support::"):
            continue
        found.add(f"{target}::{name}")
    return found


def listed_scenarios(target: str, *, ignored: bool) -> set[str]:
    return listed_test_names(target, ignored=ignored)


def ignored_scenarios(target: str) -> set[str]:
    return listed_scenarios(target, ignored=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crate",
        nargs=4,
        metavar=("ROOT", "PACKAGE", "DIR", "GATE_MD"),
        help="check <PACKAGE>'s gate in <ROOT> with scenarios in <DIR> and "
        "manifest <GATE_MD> (omitted: the netem_test harness layout)",
    )
    parser.add_argument(
        "--mandate-check-json",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "a fresh mandate-check.json whose per-test timings are compared "
            "with the declared nominal costs (default: <crate root>/"
            f"{DEFAULT_REPORT_NAME} when it exists)"
        ),
    )
    args = parser.parse_args()

    global LAYOUT
    if args.crate is not None:
        root, package, dir_name, manifest = args.crate
        root = Path(root).resolve()
        LAYOUT = CrateLayout(
            root=root,
            package=package,
            dir=root / dir_name if not Path(dir_name).is_absolute() else Path(dir_name),
            manifest=root / manifest if not Path(manifest).is_absolute() else Path(manifest),
            crates_root=root.parent.resolve(),
            is_harness=False,
        )
        for required, what in (
            (LAYOUT.dir.is_dir(), f"scenario directory {LAYOUT.dir}"),
            (LAYOUT.manifest.is_file(), f"manifest {LAYOUT.manifest}"),
        ):
            if not required:
                sys.exit(f"{what} does not exist")
    else:
        LAYOUT = harness_layout(Path(__file__).resolve().parent)

    manifest = manifest_entries()
    targets = sorted(p.stem for p in layout().dir.glob("*.rs"))
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
                f"[file {layout().dir / f'{target}.rs'}, token(s): "
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
    # diagnostic-only (70/70 `not_ready` in the recorded runs). The lane roles
    # stay with the harness; per-crate gates (mux and friends) have no lanes.
    if layout().is_harness:
        lane_roles, lane_errors = check_lane_roles()
        for error in lane_errors:
            print(error)
            bad = True
    else:
        lane_roles = {}

    # The perf-test dual mandate: time budgets and declared coverage. A crate
    # that has perf tests but no declaration is a note, not a failure, so an
    # unmigrated crate is visible without blocking the rest of the gate.
    report_path = args.mandate_check_json
    if report_path is None:
        default_report = layout().root / DEFAULT_REPORT_NAME
        report_path = default_report if default_report.is_file() else None
    perf_problems, perf_summary, perf_notes = check_perf_gate(manifest, report_path)
    for note in perf_notes:
        print(note)
    for problem in perf_problems:
        print(f"PERF DECLARATION: {problem}")
    bad = bad or bool(perf_problems)

    if bad:
        print(
            f"\nmanifest has {len(manifest)} entries, binaries report "
            f"{len(actual)} ignored scenarios; update {layout().manifest}",
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
    if layout().is_harness:
        diagnostic = sum(1 for role in lane_roles.values() if role == "diagnostic")
        print(
            f"  gate-lane-roles: {len(lane_roles)} perf-loop lane(s), "
            f"{diagnostic} diagnostic-only"
        )
    for line in perf_summary:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())