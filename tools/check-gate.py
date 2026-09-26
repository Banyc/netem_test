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
non-empty and well-formed.

Each row must also declare how it relates to a named baseline row:
`baseline` for the reference row itself, `orthogonal` when the row's cells
vary exactly one dimension from that baseline, `composite(<dimension>[,<dimension>...])`
when they vary several, and `re-measurement(<reason>)` when they vary none.
The dimensions a row varies are derived from its own cells (a key the baseline
states differently, or does not state at all; a key the row does not name is
inherited from the baseline), and the declared relation must agree with that
derivation. An unlabelled row, a label that disagrees with the cells, and a row
whose cells state one dimension twice (so its relation cannot be determined)
are all errors that name the row and what to write instead. That check is what
makes a composite arm visible: without it, a row varying four dimensions at
once passes exactly like a row varying one.

A declaration may carry several baselines, because one reference cannot serve
every measurement family: `baseline = <row>` is the **default** baseline a row
inherits when its relation names none, and each `baseline.<family> = <row>`
line declares a named baseline a row opts into with a trailing `@<family>` on
its relation (`orthogonal@m3`, `composite(a,b)@m3`, `baseline@m3`,
`re-measurement(reason)@m3`). A row's relation is then derived against *its own
family's* reference, so an M3 arm is not reported confounded merely because the
M1-clean baseline states four dimensions it does not. A relation that names an
undeclared family, a row whose `baseline`-label does not name the family it is
the reference of, and a declared baseline no row states a relation against are
all errors; the last one is how a stale reference is caught before it rots.

Stating a relation against a family is itself a claim that the row belongs to
that family, so each named family must also declare the **cell-name namespace**
its rows live in: `members.<family> = <prefix>` names the prefix a cell's
property (the part before `@`) starts with, one namespace per family. Membership
is then a property of the row rather than a free label: every cell of a row must
be named by the namespace of the family it names, a cell name may not be claimed
by two families, the family's own reference row must be inside its namespace, and
a row whose cells occupy a family's namespace must state against it. The default
family is the **residual** - every cell name no `members.<family>` claims - which
is what a crate's own conformance (or mandate) vocabulary is; the run summary
prints those residual names so a new one is visible rather than silent.
When a fresh `mandate-check.json` is supplied
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
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {"standard", "full", "perf"}
# The tiers a perf design row may name: the three opt-in tiers plus the
# always-run default tier (a row's `default` tier is resolved from the test not
# being `#[ignore]`d, matching `gate-default-required`).
PERF_TIERS = frozenset(TIERS | {"default"})
# The relation a `gate-perf-design` row declares to the `gate-budgets`
# baseline. `baseline` is the reference row itself; `orthogonal` means the
# row's cells vary exactly one dimension from the baseline; `composite` means
# they vary several and the row names them; `re-measurement` means they vary
# none and the row says why it repeats the baseline's cell.
RELATION_KINDS = frozenset({"baseline", "orthogonal", "composite", "re-measurement"})
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
# A dimension's bare name, as used by a `composite(...)` relation.
CELL_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
# A family's cell-name namespace: a cell property name (`[A-Za-z][A-Za-z0-9_.-]*`,
# the part before the cell's `@`) optionally followed by one `*`, which makes it
# a prefix. A bare name is its own (exact) namespace; `*` alone declares nothing.
MEMBERSHIP_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*\*?$")
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


def fenced_block(text: str, name: str) -> str | None:
    """Return the body of the ```<name> fenced block in ``text``, or None."""
    block = re.search(rf"```{re.escape(name)}\n(.*?)```", text, re.S)
    return block.group(1) if block else None


def manifest_block(name: str) -> str | None:
    """Return the body of the ```<name> fenced block, or None."""
    return fenced_block(layout().manifest.read_text(encoding="utf-8"), name)


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


# ---------------------------------------------------------------------------
# The documented counts.
#
# A number written into prose that a command already determines is a defect
# waiting to happen: the code moves, the sentence does not, and a reader acts
# on the stale number. `tools/PERF_INFRA.md` carried one twice - a probe was
# added in a sibling crate and the transcription of that crate's opt-in
# inventory went wrong with no signal. Every rottable count is therefore in
# exactly one of two honest forms:
#
# - **verified** (`DOC_COUNTS`): the sentence keeps the number and this check
#   fails when it disagrees with the source that determines it, naming the
#   written value and the derived one. A count that leaves the prose without
#   its entry being removed fails too, so the check cannot quietly lose its
#   subject.
# - **derived** (`DOC_DERIVED`): the number leaves the prose and the sentence
#   names the command that prints it. The pointer must still be there - a
#   derived count whose command is no longer named is unreadable - and the
#   transcribed tally must not come back, so the decision is enforced rather
#   than merely made once.
#
# Prefer `verified` where a command in this repository determines the value
# before the check runs (`tools/mandate-producers.json`, `tools/mandate-arms.json`,
# `tools/mandate-baseline.json`, the harness's own `gate-*` blocks, and the
# `mandate_compare.py` constants the documents quote). Prefer `derived` where
# the value belongs to a crate this gate cannot run - a sibling's ignored-test
# inventory - because the checker that owns it there is the only authority, and
# transcribing its output is exactly how the number rots.

DOC_COUNT_TOLERANCE = 0.05

# A prose count is written either as digits or as a numeral word. The class is
# only ever the capture group's alphabet, so "report-only arms" cannot be
# mistaken for "only arms".
_NUMERAL = (
    r"(?:[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
)
_NUMERAL_WORDS = {
    word: value
    for value, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve")
    )
}
# The ordinals the producer-count sentence uses ("the third producer is ...").
_NUMERAL_WORDS.update({"third": 3, "fourth": 4, "fifth": 5})


@dataclass(frozen=True)
class DocCount:
    """One number in prose that a command already determines.

    ``docs`` are repository-relative paths (all of which must state the count);
    ``pattern`` is a regex over the whole document with one ``_NUMERAL`` group
    per entry of ``keys``; ``authority`` names, for the diagnostic, the
    command or source that determines each value.
    """

    label: str
    docs: tuple[str, ...]
    pattern: str
    keys: tuple[str, ...]
    authority: str


@dataclass(frozen=True)
class DocDerived:
    """One inventory claim whose tally was replaced by the command.

    ``marker`` locates the sentence (the region runs from the marker to the
    first ``until`` match after it); ``pointer`` is the command the sentence
    must name; ``forbidden`` pairs a regex with why it may not appear in the
    region - the transcribed tally the derivation replaced.
    """

    label: str
    doc: str
    marker: str
    until: str
    pointer: str
    forbidden: tuple[tuple[str, str], ...]


DOC_COUNTS: tuple[DocCount, ...] = (
    DocCount(
        label="producers declared",
        docs=("tools/PERF_INFRA.md", "tools/MANDATE_SMOKE.md"),
        pattern=rf"\b({_NUMERAL}) producers are declared",
        keys=("producers",),
        authority="the producers[] array of tools/mandate-producers.json",
    ),
    DocCount(
        label="the ordinal after the declared producers",
        docs=("tools/MANDATE_SMOKE.md",),
        pattern=rf"\bA (third|fourth|fifth) producer is a registry entry",
        keys=("next_producer",),
        authority="one past the producers[] array of tools/mandate-producers.json",
    ),
    DocCount(
        label="evidence files per run",
        docs=("tools/PERF_INFRA.md", "tools/MANDATE_SMOKE.md"),
        pattern=rf"(?:the|all|its) ({_NUMERAL}) (?:expected )?evidence\s+files",
        keys=("evidence_files",),
        authority=(
            "two files (<id>.json, <id>.csv) per verdict section of the rtp_mux "
            "producer in tools/mandate-producers.json"
        ),
    ),
    DocCount(
        label="MANDATE lines per run",
        docs=("tools/MANDATE_SMOKE.md",),
        pattern=rf"all ({_NUMERAL}) `MANDATE` lines",
        keys=("verdicts",),
        authority="the verdicts of the rtp_mux producer in tools/mandate-producers.json",
    ),
    DocCount(
        label="panels and plot files of a mandate-check run",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"\(({_NUMERAL}) panels, ({_NUMERAL}) SVG\+PNG plot\s+files\)",
        keys=("baseline_panels", "baseline_plot_files"),
        authority=(
            "the summed mandates[*].panels of tools/mandate-baseline.json, and two "
            "files per panel (SVG + PNG)"
        ),
    ),
    DocCount(
        label="mandates and verified panels of the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"passed all ({_NUMERAL}) mandates with ({_NUMERAL})\s+verified SVG panels",
        keys=("verdicts", "baseline_panels"),
        authority=(
            "the verdicts of the rtp_mux producer in tools/mandate-producers.json, "
            "and the summed mandates[*].panels of tools/mandate-baseline.json"
        ),
    ),
    DocCount(
        label="arms recorded in the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"recorded \*\*({_NUMERAL}) arms from both producers\*\*",
        keys=("arms_total",),
        authority="len(arms) of tools/mandate-baseline.json",
    ),
    DocCount(
        label="rtp_mux arms in the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"\*\*: ({_NUMERAL}) for\s+`rtp_mux`",
        keys=("arms_rtp_mux",),
        authority="the arms of tools/mandate-baseline.json whose producer is rtp_mux",
    ),
    DocCount(
        label="per-mandate arm counts in the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=(
            rf"\(({_NUMERAL}) M1, ({_NUMERAL}) M2, ({_NUMERAL}) M3 reps and "
            rf"({_NUMERAL}) M4 arms\)"
        ),
        keys=("arms_M1", "arms_M2", "arms_M3", "arms_M4"),
        authority="the arms of tools/mandate-baseline.json grouped by mandate",
    ),
    DocCount(
        label="probes recorded in the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"and the ({_NUMERAL}) probes\b",
        keys=("baseline_probes",),
        authority="the arms of tools/mandate-baseline.json whose producer is netem_test",
    ),
    DocCount(
        label="probe arms in the probe section",
        docs=("tools/PERF_INFRA.md", "tools/MANDATE_SMOKE.md"),
        pattern=rf"\b({_NUMERAL})(?:\s+[A-Za-z-]+){{0,3}}\s+arms in the `probe` section",
        keys=("arms_probe",),
        authority="the probe/<arm> keys of tools/mandate-arms.json",
    ),
    DocCount(
        label="perf-tier probes of the harness",
        docs=("tools/PERF_INFRA.md", "tools/MANDATE_SMOKE.md"),
        pattern=rf"\b({_NUMERAL}) perf-tier probes",
        keys=("arms_probe",),
        authority="the probe/<arm> keys of tools/mandate-arms.json",
    ),
    DocCount(
        label="probe-* rows of the harness declaration",
        docs=("tools/PERF_INFRA.md",),
        pattern=rf"their ({_NUMERAL}) `probe-\*` rows",
        keys=("harness_probe_rows",),
        authority=(
            "the gate-perf-design rows of tests/GATE.md whose cells are in the "
            "probe-* namespace"
        ),
    ),
    DocCount(
        label="duration of the baseline run",
        docs=("tools/PERF_INFRA.md",),
        pattern=r"The run took \*\*([0-9.]+) s\*\*",
        keys=("baseline_duration",),
        authority="duration_seconds of tools/mandate-baseline.json",
    ),
    DocCount(
        label="producers a two-producer case runs",
        docs=("tools/MANDATE_SMOKE.md",),
        pattern=rf"Its ({_NUMERAL})-producer cases",
        keys=("producers",),
        authority="the producers[] array of tools/mandate-producers.json",
    ),
    DocCount(
        label="counted floors applied to an unstated lane",
        docs=("tools/MANDATE_SMOKE.md",),
        pattern=rf"the ({_NUMERAL}) bulk-lane byte counters",
        keys=("count_floor_counters",),
        authority="len(COUNT_FLOORS_BYTES) in tools/mandate_compare.py",
    ),
    DocCount(
        label="reference families of the rtp_mux draft",
        docs=("tools/PERF_PENDING_rtp_mux.md",),
        pattern=(
            rf"splits into \*\*({_NUMERAL}) reference families\*\* "
            rf"\(\*\*({_NUMERAL}) named plus the default\*\*\)"
        ),
        keys=("draft_families", "draft_named_families"),
        authority=(
            "the baseline/baseline.<family> lines of the gate-budgets block of "
            "tools/PERF_PENDING_rtp_mux.md (one family per reference row, plus the "
            "default)"
        ),
    ),
    DocCount(
        label="relation counts of the rtp_mux draft",
        docs=("tools/PERF_PENDING_rtp_mux.md",),
        pattern=(
            rf"the ({_NUMERAL}) rows are \*\*({_NUMERAL}) orthogonal\*\*, "
            rf"\*\*({_NUMERAL}) composite\*\*, \*\*({_NUMERAL})\s+re-measurement\*\* "
            rf"and \*\*({_NUMERAL}) baseline\*\*"
        ),
        keys=(
            "draft_rows",
            "draft_orthogonal",
            "draft_composite",
            "draft_re_measurement",
            "draft_baseline_rows",
        ),
        authority=(
            "the gate-perf-relations summary the checker derives from the draft's "
            "own blocks, with its deliberately-unmeasured `TBD` costs substituted "
            "(a relation is derived from the cells, never from the cost)"
        ),
    ),
    DocCount(
        label="cost sums of the rtp_mux draft",
        docs=("tools/PERF_PENDING_rtp_mux.md",),
        pattern=(
            rf"declare ({_NUMERAL}) s in `default` \(the four `mandate_smoke` rows "
            rf"and the constitution\s+gate\) and ({_NUMERAL}) s in `perf` "
            rf"\(({_NUMERAL}) rows, ({_NUMERAL}) of them still `TBD`\)"
        ),
        keys=(
            "draft_cost_default_rounded",
            "draft_cost_perf_rounded",
            "draft_rows_perf",
            "draft_unmeasured_perf",
        ),
        authority=(
            "the summed nominal costs, row count and `TBD` count of the draft's "
            "gate-perf-design rows per tier (the default figure to the whole "
            "second, as the sentence states it)"
        ),
    ),
    DocCount(
        label="measurement targets of the rtp_mux draft",
        docs=("tools/PERF_PENDING_rtp_mux.md",),
        pattern=rf"cover\s+the ({_NUMERAL}) measurement targets",
        keys=("draft_targets",),
        authority=(
            "the distinct targets of the draft's gate-perf-design rows, with its "
            "deliberately-unmeasured `TBD` costs substituted so every row parses"
        ),
    ),
    DocCount(
        label="every declared reference, harness plus draft",
        docs=("tools/PERF_PENDING_rtp_mux.md",),
        pattern=rf"Every\s+declared reference \(all ({_NUMERAL})\)",
        keys=("references_total",),
        authority=(
            "the reference rows of tests/GATE.md plus those of "
            "tools/PERF_PENDING_rtp_mux.md"
        ),
    ),
    DocCount(
        label="baselines and namespaces of the harness declaration",
        docs=("tests/GATE.md",),
        pattern=(
            rf"The harness declares ({_NUMERAL}) baselines, one per measurement "
            rf"family, and ({_NUMERAL})\s+namespaces"
        ),
        keys=("harness_families", "harness_namespaces"),
        authority=(
            "the baseline/baseline.<family> lines and the members.<family> lines of "
            "the gate-budgets block of tests/GATE.md"
        ),
    ),
    DocCount(
        label="netem_scenarios rows beside the default baseline",
        docs=("tests/GATE.md",),
        pattern=rf"the ({_NUMERAL}) other `netem_scenarios` rows are stated against it",
        keys=("harness_netem_rows",),
        authority=(
            "the netem_scenarios:: rows of the gate-perf-design block of "
            "tests/GATE.md, less their baseline"
        ),
    ),
    DocCount(
        label="declared relation counts of the harness declaration",
        docs=("tests/GATE.md",),
        pattern=(
            rf"Declared: \*\*({_NUMERAL}) orthogonal\*\* rows, \*\*({_NUMERAL}) "
            rf"composite\*\* rows and \*\*({_NUMERAL})\s+re-measurement\*\*, "
            rf"plus the ({_NUMERAL}) baseline rows"
        ),
        keys=(
            "harness_orthogonal",
            "harness_composite",
            "harness_re_measurement",
            "harness_baseline_rows",
        ),
        authority=(
            "the gate-perf-relations summary the checker derives from the "
            "gate-perf-design and gate-budgets blocks of tests/GATE.md"
        ),
    ),
    DocCount(
        label="probe rows other than the probe family's reference",
        docs=("tests/GATE.md",),
        pattern=rf"and the ({_NUMERAL}) probes\s+differ from the probe cell",
        keys=("harness_other_probe_rows",),
        authority=(
            "the probe-* rows of tests/GATE.md, less the probe family's reference row"
        ),
    ),
)

DOC_DERIVED: tuple[DocDerived, ...] = (
    DocDerived(
        label="the rtp opt-in inventory",
        doc="tools/PERF_INFRA.md",
        marker=r"^- \*\*`rtp`\*\*",
        until=r"\n\*\*`proxy`\*\*",
        pointer="crates/rtp/tools/check-ignored.py",
        forbidden=(
            (
                r"[0-9]+\s+`#\[ignore\]`d\s+tests",
                "a transcribed total of #[ignore]d tests",
            ),
            (
                r"`(?:perf-lane|probe|standard|full|perf)`\s+[0-9]+",
                "a transcribed per-tier tally",
            ),
            (
                r"[0-9]+\s+in-crate opt-ins",
                "a transcribed in-crate opt-in count",
            ),
            (
                r"[0-9]+\s+of\s+[0-9]+\s+probes?\s+record(?:ed)?",
                "a transcribed probe-selfcheck count",
            ),
        ),
    ),
    DocDerived(
        label="the mux perf-test tier tally",
        doc="tools/PERF_INFRA.md",
        marker=r"^- \*\*`mux`\*\*",
        until=r"\n\nThe checker's treatment",
        pointer="--crate ../mux",
        forbidden=(
            (
                r"\b(?:one|two|[0-9]+)\s+`standard`-tier scenario",
                "a transcribed tier tally",
            ),
            (
                r"\bno\s+`perf`-tier scenario",
                "a transcribed tier tally",
            ),
        ),
    ),
    DocDerived(
        label="the rtp probe-selfcheck tally",
        doc="tools/PERF_INFRA.md",
        marker=r"^\*\*Read report-only output",
        until=r"\n\n",
        pointer="crates/rtp/tools/check-ignored.py",
        forbidden=(
            (
                r"[0-9]+\s+of\s+[0-9]+\s+probes?\s+record(?:ed)?",
                "a transcribed probe-selfcheck count",
            ),
            (
                r"`(?:perf-lane|probe|standard|full|perf)`\s+[0-9]+",
                "a transcribed per-tier tally",
            ),
        ),
    ),
)


def _doc_number(text: str) -> float | None:
    """The value of a prose count, written as digits or as a numeral word."""
    word = text.strip().lower()
    if word in _NUMERAL_WORDS:
        return float(_NUMERAL_WORDS[word])
    try:
        return float(word)
    except ValueError:
        return None


def _doc_json(root: Path, relpath: str, problems: list[str]) -> dict | None:
    """Load a declaration the documented counts are derived from.

    A missing or malformed source is a problem, never a silent skip: a count
    whose source has gone cannot be verified, and a check that passes because
    it could not look is the defect this whole section exists to remove.
    """
    try:
        return json.loads((root / relpath).read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"DOC COUNT: cannot derive: {relpath} does not exist")
    except json.JSONDecodeError as error:
        problems.append(f"DOC COUNT: cannot derive: {relpath} is not JSON ({error})")
    return None


def _gate_block_counts(
    text: str,
    source: str,
    prefix: str,
    problems: list[str],
    *,
    null_cost: tuple[str, str] | None = None,
) -> dict[str, float]:
    """Rows, families, the relation tally and the cost sums of one block set.

    The relation counts are read from the checker's own derivation summary
    rather than recounted here, so the number a document states and the number
    the gate prints are the same number by construction.

    ``null_cost`` is ``(placeholder, value)`` and replaces a placeholder cost
    before parsing, which the draft declaration needs: its rows carry a ``TBD``
    cost by design (the measurement is still owed), and a relation is derived
    from the cells, never from the cost, so the substitution cannot change a
    relation count. The placeholder and how many rows carry it are counted
    separately, because a document states them too.
    """
    values: dict[str, float] = {}
    design = fenced_block(text, "gate-perf-design")
    budgets_text = fenced_block(text, "gate-budgets")
    if design is None or budgets_text is None:
        problems.append(
            f"DOC COUNT: cannot derive: {source} has no gate-perf-design/"
            "gate-budgets block"
        )
        return values
    unmeasured: dict[str, int] = {}
    if null_cost is not None:
        placeholder, replacement = null_cost
        for line in design.splitlines():
            if placeholder not in line:
                continue
            tier = line.split("=", 1)[1].split("|", 1)[0].strip()
            unmeasured[tier] = unmeasured.get(tier, 0) + 1
        design = re.sub(rf"\b{re.escape(placeholder)}\b", replacement, design)
    rows = parse_perf_design(design, [])
    budgets = parse_perf_budgets(budgets_text, [])
    values[f"{prefix}_rows"] = float(len(rows))
    values[f"{prefix}_targets"] = float(
        len({row.name.split("::", 1)[0] for row in rows})
    )
    for tier in sorted({row.tier for row in rows}):
        tier_rows = [row for row in rows if row.tier == tier]
        total = sum(row.cost for row in tier_rows)
        values[f"{prefix}_cost_{tier}"] = total
        values[f"{prefix}_cost_{tier}_rounded"] = float(round(total))
        values[f"{prefix}_rows_{tier}"] = float(len(tier_rows))
        values[f"{prefix}_unmeasured_{tier}"] = float(unmeasured.get(tier, 0))
    values[f"{prefix}_families"] = float(1 + len(budgets.named))
    values[f"{prefix}_named_families"] = float(len(budgets.named))
    values[f"{prefix}_namespaces"] = float(len(budgets.members))
    summary = check_perf_relations(rows, budgets, [])
    relations = next(
        (line for line in summary if "gate-perf-relations:" in line), None
    )
    match = relations and re.search(
        r"gate-perf-relations: ([0-9]+) orthogonal, ([0-9]+) composite, "
        r"([0-9]+) re-measurement, ([0-9]+) baseline of",
        relations,
    )
    if match is None:
        problems.append(
            f"DOC COUNT: cannot derive: the checker printed no gate-perf-relations "
            f"summary for {source}"
        )
        return values
    (
        values[f"{prefix}_orthogonal"],
        values[f"{prefix}_composite"],
        values[f"{prefix}_re_measurement"],
        values[f"{prefix}_baseline_rows"],
    ) = (float(group) for group in match.groups())
    return values


def _harness_gate_counts(root: Path, problems: list[str]) -> dict[str, float]:
    """The counts the harness's own `gate-*` blocks determine."""
    gate_path = root / "tests" / "GATE.md"
    try:
        text = gate_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append("DOC COUNT: cannot derive: tests/GATE.md does not exist")
        return {}
    design = fenced_block(text, "gate-perf-design")
    budgets_text = fenced_block(text, "gate-budgets")
    if design is None or budgets_text is None:
        problems.append(
            "DOC COUNT: cannot derive: tests/GATE.md has no gate-perf-design/"
            "gate-budgets block"
        )
        return {}
    inner: list[str] = []
    rows = parse_perf_design(design, inner)
    budgets = parse_perf_budgets(budgets_text, inner)
    if inner:
        # The perf declaration itself does not parse; the perf check above
        # names every reason, so the derived counts are noted, not re-failed.
        problems.append(
            "note: the documented harness counts are not derived this run: the "
            "gate-perf-design/gate-budgets blocks do not parse (see the perf "
            "declaration problems above)"
        )
        return {}
    values = _gate_block_counts(text, "tests/GATE.md", "harness", problems)
    values["harness_netem_rows"] = float(
        max(
            0,
            sum(
                1
                for row in rows
                if row.name.startswith("netem_scenarios::")
                and row.relation is not None
                and row.relation.family is None
            )
            - 1,
        )
    )
    probe_rows = [row for row in rows if any(c.startswith("probe-") for c in row.cells)]
    values["harness_probe_rows"] = float(len(probe_rows))
    probe_reference = budgets.named.get("probe")
    values["harness_other_probe_rows"] = float(
        len([row for row in probe_rows if row.name != probe_reference])
    )
    return values


def _draft_gate_counts(root: Path, problems: list[str]) -> dict[str, float]:
    """The counts the `rtp_mux` draft declaration's own blocks determine."""
    path = root / "tools" / "PERF_PENDING_rtp_mux.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(
            "DOC COUNT: cannot derive: tools/PERF_PENDING_rtp_mux.md does not "
            "exist; if the draft was applied and deleted, drop its entries from "
            "DOC_COUNTS in tools/check-gate.py"
        )
        return {}
    return _gate_block_counts(
        text,
        "tools/PERF_PENDING_rtp_mux.md",
        "draft",
        problems,
        null_cost=("TBD", "0"),
    )


def _count_floor_counters(root: Path, problems: list[str]) -> dict[str, float]:
    """`len(COUNT_FLOORS_BYTES)` from `tools/mandate_compare.py`."""
    path = root / "tools" / "mandate_compare.py"
    spec = importlib.util.spec_from_file_location("mandate_compare_doc_counts", path)
    if spec is None or spec.loader is None:
        problems.append(
            f"DOC COUNT: cannot derive: {path} cannot be loaded for "
            "COUNT_FLOORS_BYTES"
        )
        return {}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - any import failure is the point
        problems.append(
            f"DOC COUNT: cannot derive: {path} failed to import ({error!r}) for "
            "COUNT_FLOORS_BYTES"
        )
        return {}
    floors = getattr(module, "COUNT_FLOORS_BYTES", None)
    if not isinstance(floors, dict):
        problems.append(
            f"DOC COUNT: cannot derive: {path} defines no COUNT_FLOORS_BYTES dict"
        )
        return {}
    return {"count_floor_counters": float(len(floors))}


def doc_count_values(root: Path, problems: list[str]) -> dict[str, float]:
    """Every value the verified prose counts are checked against."""
    values: dict[str, float] = {}
    producers = _doc_json(root, "tools/mandate-producers.json", problems)
    arms = _doc_json(root, "tools/mandate-arms.json", problems)
    baseline = _doc_json(root, "tools/mandate-baseline.json", problems)

    if producers is not None:
        entries = producers.get("producers") or []
        values["producers"] = float(len(entries))
        values["next_producer"] = float(len(entries) + 1)
        verdicts = [
            entry for entry in entries if entry.get("id") == "rtp_mux"
        ]
        if not verdicts:
            problems.append(
                "DOC COUNT: cannot derive: tools/mandate-producers.json declares no "
                "rtp_mux producer, so its verdict sections are unknown"
            )
        else:
            count = float(len(verdicts[0].get("verdicts") or []))
            values["verdicts"] = count
            values["evidence_files"] = 2 * count

    if arms is not None:
        cells = arms.get("cells") or {}
        values["arms_probe"] = float(
            sum(1 for key in cells if key.startswith("probe/"))
        )

    if baseline is not None:
        recorded = baseline.get("arms") or []
        values["baseline_arms"] = float(len(recorded))
        values["arms_total"] = float(len(recorded))
        values["arms_rtp_mux"] = float(
            sum(1 for arm in recorded if arm.get("producer") == "rtp_mux")
        )
        values["baseline_probes"] = float(
            sum(1 for arm in recorded if arm.get("producer") == "netem_test")
        )
        by_mandate: dict[str, int] = {}
        for arm in recorded:
            mandate = arm.get("mandate")
            by_mandate[mandate] = by_mandate.get(mandate, 0) + 1
        for mandate in ("M1", "M2", "M3", "M4"):
            values[f"arms_{mandate}"] = float(by_mandate.get(mandate, 0))
        mandates = baseline.get("mandates") or {}
        panels = sum(int(record.get("panels") or 0) for record in mandates.values())
        values["baseline_panels"] = float(panels)
        values["baseline_plot_files"] = float(2 * panels)
        duration = baseline.get("duration_seconds")
        if isinstance(duration, (int, float)):
            values["baseline_duration"] = float(duration)

    values.update(_harness_gate_counts(root, problems))
    draft = _draft_gate_counts(root, problems)
    values.update(draft)
    if "draft_families" in draft and "harness_families" in values:
        # "every declared reference": both crates' baseline rows.
        values["references_total"] = (
            draft["draft_families"] + values["harness_families"]
        )
    values.update(_count_floor_counters(root, problems))
    return values


def check_doc_counts(root: Path) -> tuple[list[str], list[str]]:
    """Verify every documented count that a command determines.

    Returns ``(problems, summary)``. A count whose sentence or source is gone
    is a problem, not a skip: the alternative is a check that cannot fail once
    the number it guarded has been deleted.
    """
    problems: list[str] = []
    values = doc_count_values(root, problems)
    checked = 0
    for entry in DOC_COUNTS:
        for relpath in entry.docs:
            path = root / relpath
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                problems.append(f"DOC COUNT: {relpath} does not exist")
                continue
            matches = list(re.finditer(entry.pattern, text, re.IGNORECASE))
            if not matches:
                problems.append(
                    f"DOC COUNT: {relpath} no longer states {entry.label!r}; a "
                    "verified count that has left the prose cannot be checked - "
                    "restore the sentence or drop this entry from DOC_COUNTS in "
                    "tools/check-gate.py"
                )
                continue
            for match in matches:
                for index, key in enumerate(entry.keys):
                    written = match.group(index + 1)
                    given = _doc_number(written)
                    derived = values.get(key)
                    if given is None:
                        problems.append(
                            f"DOC COUNT: {relpath}: {entry.label!r} reads "
                            f"{written!r}, which is not a number"
                        )
                        continue
                    if derived is None:
                        problems.append(
                            f"DOC COUNT: {relpath}: {entry.label!r} cannot be "
                            f"checked: nothing determined a value for {key!r} "
                            f"({entry.authority})"
                        )
                        continue
                    checked += 1
                    if abs(given - derived) > DOC_COUNT_TOLERANCE:
                        problems.append(
                            f"DOC COUNT: {relpath}: {entry.label!r} says "
                            f"{written!r}, but {derived:g} is what determines it "
                            f"({entry.authority}); update the document, or the "
                            "declaration if the change is intended"
                        )
    for entry in DOC_DERIVED:
        path = root / entry.doc
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            problems.append(f"DOC COUNT: {entry.doc} does not exist")
            continue
        marker = re.search(entry.marker, text, re.MULTILINE)
        if marker is None:
            problems.append(
                f"DOC COUNT: {entry.doc} no longer has the sentence {entry.label!r} "
                "was derived into; restore it or drop this entry from DOC_DERIVED "
                "in tools/check-gate.py"
            )
            continue
        end = re.search(entry.until, text[marker.end() :], re.MULTILINE)
        stop = marker.end() + end.start() if end else len(text)
        region = text[marker.start() : stop]
        if entry.pointer not in region:
            problems.append(
                f"DOC COUNT: {entry.doc}: {entry.label!r} no longer names "
                f"{entry.pointer!r}; a derived count whose command is not named is "
                "unreadable - point the sentence at the command that prints it"
            )
        for pattern, why in entry.forbidden:
            transcribed = re.search(pattern, region)
            if transcribed:
                problems.append(
                    f"DOC COUNT: {entry.doc}: {entry.label!r} has a transcription "
                    f"back: {transcribed.group(0)!r} is {why}, and the number is "
                    f"already printed by {entry.pointer!r}"
                )
    summary = [
        f"  gate-doc-counts: {checked} verified count(s) across "
        f"{len({d for e in DOC_COUNTS for d in e.docs})} doc(s), "
        f"{len(DOC_DERIVED)} derived inventory claim(s) pinned to their checker"
    ]
    return problems, summary


@dataclass(frozen=True)
class Relation:
    """A row's declared relation to a `gate-budgets` baseline row.

    ``kind`` is one of `RELATION_KINDS`; ``keys`` is the dimensions a
    ``composite`` row names it varies; ``reason`` is why a ``re-measurement``
    row deliberately repeats the baseline's cell; ``family`` is the named
    baseline the relation is stated against, or None for the default
    `baseline = <row>`.
    """

    kind: str
    keys: tuple[str, ...] = ()
    reason: str = ""
    family: str | None = None


@dataclass(frozen=True)
class PerfRow:
    """One `gate-perf-design` row: a perf test, its tier, cost and coverage."""

    name: str
    tier: str
    cost: float
    cells: tuple[str, ...]
    relation: Relation | None = None


@dataclass(frozen=True)
class PerfBudgets:
    """The `gate-budgets` block: one budget per tier, plus the baselines.

    ``baseline`` is the default reference every row inherits when its relation
    names no family; ``named`` maps each `baseline.<family>` line's family to
    the row it references; ``members`` maps each named family to the cell-name
    prefix (`members.<family> = <prefix>`) its rows' cells are named by. The
    default family has no entry: its namespace is the residual, every cell name
    no ``members`` prefix claims.
    """

    tiers: dict[str, float]
    baseline: str | None
    drift: float
    drift_floor_seconds: float
    named: dict[str, str] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)


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


# `<kind>[(<args>)][@<family>]`. `<family>` is the name of a
# `gate-budgets: baseline.<family>` line the relation is stated against; no
# suffix means the default `baseline = <row>`. The arguments may not contain
# `@`, so the family suffix is never ambiguous with a composite dimension or a
# re-measurement reason.
RELATION_RE = re.compile(
    r"^(?P<kind>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?:\((?P<argument>[^()@]*)\))?"
    r"(?:@(?P<family>[A-Za-z][A-Za-z0-9_-]*))?$"
)


def relation_display(kind: str, family: str | None) -> str:
    """The relation text a diagnostic tells the author to write."""
    return f"{kind}@{family}" if family else kind


def parse_relation(text: str) -> tuple[Relation | None, str | None]:
    """Parse a row's relation field, or say exactly what to write instead.

    A relation is `<kind>[@<family>]` or `<kind>(<argument>)@<family>`, where
    `<family>` names a `baseline.<family>` row in `gate-budgets`; without the
    suffix the row is stated against the default `baseline = <row>`.
    """
    text = text.strip()
    match = RELATION_RE.match(text)
    if match is None:
        kind, opened, _rest = text.partition("(")
        if opened and not text.endswith(")"):
            return None, (
                f"relation {text!r} is missing its closing ')'; write "
                f"{kind.strip()}(...)"
            )
        return None, (
            f"relation {text!r} is not a relation this grammar knows; write "
            "`baseline`, `orthogonal`, `composite(<dimension>[,<dimension>...])` "
            "or `re-measurement(<reason>)`, each optionally followed by "
            "`@<baseline-family>` to state it against a named baseline"
        )
    kind = match.group("kind")
    argument = match.group("argument")
    family = match.group("family")
    if kind not in RELATION_KINDS:
        return None, (
            f"relation {text!r} is not a relation this grammar knows; write "
            "`baseline`, `orthogonal`, `composite(<dimension>[,<dimension>...])` "
            "or `re-measurement(<reason>)`, each optionally followed by "
            "`@<baseline-family>` to state it against a named baseline"
        )
    if argument is None:
        if kind in ("baseline", "orthogonal"):
            return Relation(kind, family=family), None
        return None, (
            f"relation {text!r} gives no argument; write "
            + (
                "`composite(<dimension>[,<dimension>...])` naming the dimensions "
                "the row's cells vary"
                if kind == "composite"
                else "`re-measurement(<reason>)` naming why the row repeats the "
                "baseline's cell"
            )
        )
    inner = argument.strip()
    if kind in ("baseline", "orthogonal"):
        return None, f"relation {text!r} takes no argument; write `{relation_display(kind, family)}`"
    if kind == "re-measurement":
        if not inner:
            return None, (
                "`re-measurement(<reason>)` names no reason; say why the row "
                "deliberately repeats the baseline's cell (a second tier, a "
                "stability re-run, a seed sweep)"
            )
        if any(character in inner for character in ",()@"):
            return None, (
                f"re-measurement reason {inner!r} contains ',', a parenthesis or "
                "'@'; write one reason token (e.g. "
                "`re-measurement(full-tier-rerun)@<family>`)"
            )
        return Relation(kind, (), inner, family), None
    keys: list[str] = []
    for part in inner.split(","):
        key = part.strip()
        if not key:
            continue
        if not CELL_KEY_RE.match(key):
            return None, f"composite dimension {key!r} is not a name ([A-Za-z][A-Za-z0-9_-]*)"
        if key in keys:
            return None, f"composite(...) names the dimension {key!r} twice"
        keys.append(key)
    if len(keys) < 2:
        return None, (
            "`composite(...)` must name at least two dimensions; a row that "
            "varies exactly one dimension from the baseline is `orthogonal`"
        )
    return Relation(kind, tuple(keys), "", family), None


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
                "<nominal_cost_s> | <relation> | <coverage>'",
            )
            continue
        name = name.strip()
        parts = [part.strip() for part in rest.split("|")]
        if len(parts) not in (3, 4):
            problems.append(
                f"gate-perf-design row {name}: expected '<tier> | <cost_s> | "
                "<relation> | <coverage>', found "
                f"{len(parts)} field(s); a row without its relation to the "
                "baseline is not a declaration"
            )
            continue
        if len(parts) == 4:
            tier, cost_text, relation_text, coverage_text = parts
        else:
            tier, cost_text, coverage_text = parts
            relation_text = None
        relation: Relation | None = None
        if relation_text is not None:
            relation, reason = parse_relation(relation_text)
            if reason is not None:
                problems.append(f"gate-perf-design row {name}: {reason}")
                relation = None
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
        rows.append(PerfRow(name, tier, cost, cells, relation))
    return rows


def parse_perf_budgets(text: str, problems: list[str]) -> PerfBudgets:
    """Parse `gate-budgets`: `<tier> = <budget_s>` plus the baseline families.

    `baseline = <row>` is the default reference a row inherits when its
    relation names no family; each `baseline.<family> = <row>` line declares a
    named reference a row opts into with `@<family>`, and each
    `members.<family> = <prefix>` line declares that family's cell-name
    namespace. The default family's namespace is the residual and is not
    declared: a bare `members` line is refused with that reason.
    """
    tiers: dict[str, float] = {}
    baseline: str | None = None
    named: dict[str, str] = {}
    members: dict[str, str] = {}
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
        if key.startswith("baseline."):
            family = key[len("baseline.") :].strip()
            if not CELL_KEY_RE.match(family):
                problems.append(
                    f"gate-budgets line {number}: {key!r} does not name a baseline "
                    "family; write `baseline.<family> = <row>` with a family name "
                    "([A-Za-z][A-Za-z0-9_-]*)"
                )
                continue
            if not value:
                problems.append(f"gate-budgets: baseline.{family} names no row")
                continue
            if family in named:
                problems.append(f"gate-budgets: duplicate baseline family {family!r}")
                continue
            named[family] = value
            continue
        if key == "members":
            problems.append(
                f"gate-budgets line {number}: {key!r} names no family, and the "
                "default family's namespace is the residual (every cell name no "
                "`members.<family>` claims), so it needs no declaration; write "
                "`members.<family> = <prefix>` for each named family"
            )
            continue
        if key.startswith("members."):
            family = key[len("members.") :].strip()
            if not CELL_KEY_RE.match(family):
                problems.append(
                    f"gate-budgets line {number}: {key!r} does not name a family; "
                    "write `members.<family> = <prefix>` with a family name "
                    "([A-Za-z][A-Za-z0-9_-]*)"
                )
                continue
            if not MEMBERSHIP_PREFIX_RE.match(value):
                problems.append(
                    f"gate-budgets line {number}: members.{family} = {value!r} does "
                    "not name a cell-name namespace; write a cell name "
                    "([A-Za-z][A-Za-z0-9_.-]*), optionally followed by '*' to make "
                    "it the prefix its family's cell names start with"
                )
                continue
            if family in members:
                problems.append(
                    f"gate-budgets: duplicate members line for family {family!r}"
                )
                continue
            members[family] = value
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
                "of baseline/drift/drift_floor_s (nor a `baseline.<family>`, "
                "`members.<family>` line)"
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
    return PerfBudgets(tiers, baseline, drift, floor, named, members)


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


def check_perf_relations(
    rows: list[PerfRow], budgets: PerfBudgets, problems: list[str]
) -> list[str]:
    """Verify each row's declared relation against the dimensions its cells vary.

    Each row is stated against the baseline of its own family: a relation with
    a trailing ``@<family>`` is derived against the ``baseline.<family>`` row,
    and a relation that names none against the default
    ``baseline = <row>``. The dimensions a row varies are derived from its own
    cells: a dimension whose value differs from its own family baseline's, or
    that the baseline does not state at all, is varied; a dimension the row
    does not name is inherited from that baseline. A row labelled `composite`
    must name exactly those dimensions. Returns the summary lines for the
    passing case; every failure is appended to ``problems`` with the row and
    what to write instead.
    """
    summary: list[str] = []
    if budgets.baseline is None:
        return summary
    by_name = {row.name: row for row in rows}
    if budgets.baseline not in by_name:
        # The baseline is not a row; that is already a failure of its own.
        return summary

    def cell_state(cell: str, ambiguous: list[str]) -> dict[str, str]:
        """`dimension -> value` for one cell, naming a dimension stated twice."""
        if "@" not in cell:
            # A malformed cell is already named by `cell_problem`; it states no
            # dimension this check can read.
            return {}
        state: dict[str, str] = {}
        for part in cell.split("@", 1)[1].split("+"):
            key, _, value = part.partition("=")
            if key in state and state[key] != value:
                ambiguous.append(
                    f"the cell {cell!r} states {key!r} as both {state[key]!r} and "
                    f"{value!r}"
                )
                continue
            state[key] = value
        return state

    # family -> reference row name. None is the default, unnamed family; every
    # other key is a `baseline.<family>` line.
    families: dict[str | None, str] = {None: budgets.baseline}
    families.update(budgets.named)
    reference_of: dict[str, set[str | None]] = {}
    base_state: dict[str | None, dict[str, str]] = {}
    for family, name in families.items():
        row = by_name.get(name)
        if row is None:
            if family is not None:
                problems.append(
                    f"gate-budgets: baseline.{family} names {name!r}, which is not "
                    "a gate-perf-design row, so the rows stated against it are "
                    "stated against nothing"
                )
            continue
        reference_of.setdefault(name, set()).add(family)
        conflicts: list[str] = []
        state: dict[str, str] = {}
        for cell in row.cells:
            for key, value in cell_state(cell, conflicts).items():
                state[key] = value
        if conflicts:
            problems.append(
                f"gate-perf-design baseline row {name}: its cells are not one "
                f"point, so no row's relation to it can be determined "
                f"({'; '.join(conflicts)}); state the baseline once per "
                "dimension, with one value each"
            )
            continue
        base_state[family] = state
    for name, references in sorted(reference_of.items()):
        if len(references) > 1:
            labels = ", ".join(
                "the default baseline" if family is None else f"baseline.{family}"
                for family in sorted(references, key=lambda f: (f is not None, f or ""))
            )
            problems.append(
                f"gate-budgets: {name!r} is the reference row of {labels}; a row "
                "carries one relation, so give each family its own reference row"
            )

    def against_phrase(family: str | None) -> str:
        """How a diagnostic names the family baseline, without the row."""
        return "the baseline" if family is None else f"baseline {family!r}"

    varied_by_row: dict[str, tuple[str, ...]] = {}
    counts = {kind: 0 for kind in sorted(RELATION_KINDS)}
    family_counts: dict[str | None, dict[str, int]] = {}
    for row in rows:
        relation = row.relation
        family = relation.family if relation is not None else None
        if family is not None and family not in budgets.named:
            problems.append(
                f"gate-perf-design row {row.name}: it names the baseline family "
                f"{family!r}, which gate-budgets does not declare; declare "
                f"`baseline.{family} = <row>` or state the row against the "
                f"default baseline ({budgets.baseline})"
            )
            continue
        if family not in base_state:
            # This family's reference row is missing or ambiguous; already named.
            continue
        against = against_phrase(family)
        referenced = families[family]
        conflicted: list[str] = []
        varied: set[str] = set()
        for cell in row.cells:
            state = cell_state(cell, conflicted)
            for key, value in state.items():
                if key not in base_state[family] or base_state[family][key] != value:
                    varied.add(key)
        derived = tuple(sorted(varied))
        varied_by_row[row.name] = derived
        if conflicted:
            problems.append(
                f"gate-perf-design row {row.name}: its relation to {against} "
                f"cannot be determined ({'; '.join(conflicted)}), so the "
                "dimensions it varies are ambiguous; state each dimension once, "
                "with one value (split the row if it covers two points)"
            )
            continue
        wanted = relation_display(
            "orthogonal"
            if len(derived) == 1
            else "composite(" + ",".join(derived) + ")"
            if derived
            else "re-measurement(<reason>)",
            family,
        )

        def bump(kind: str) -> None:
            counts[kind] = counts.get(kind, 0) + 1
            tally = family_counts.setdefault(family, {})
            tally[kind] = tally.get(kind, 0) + 1

        if family in reference_of.get(row.name, ()):
            # This row is the reference of the family it is stated against, so
            # its relation is the `baseline` label for that family.
            if relation is not None and relation.kind == "baseline":
                bump("baseline")
                continue
            if len(reference_of[row.name]) == 1:
                label = relation_display("baseline", family)
                if family is None:
                    problems.append(
                        f"gate-perf-design row {row.name}: it is the gate-budgets "
                        "baseline, so its relation is the reference every other "
                        f"row is stated against; write `{label}`"
                    )
                else:
                    problems.append(
                        f"gate-perf-design row {row.name}: it is the reference "
                        f"row of baseline.{family} ({referenced}), so its "
                        "relation is what every other row in that family is "
                        f"stated against; write `{label}`"
                    )
            continue
        if relation is None:
            problems.append(
                f"gate-perf-design row {row.name}: it declares no relation to "
                f"{against} {referenced}; its cells vary "
                f"{len(derived)} dimension(s), so write `{wanted}`"
            )
            continue
        if relation.kind == "baseline":
            if family is None and reference_of.get(row.name):
                # Reached only for a row that is a *named* family's reference
                # but carries the plain `baseline` label, which names the
                # default family instead.
                only = next(iter(reference_of[row.name]))
                named = (
                    "the default baseline"
                    if only is None
                    else f"baseline.{only} ({families[only]})"
                )
                problems.append(
                    f"gate-perf-design row {row.name}: it is the reference row of "
                    f"{named}, so its relation is what every other row in that "
                    f"family is stated against; write "
                    f"`{relation_display('baseline', only)}`"
                )
                continue
            if family is None:
                problems.append(
                    f"gate-perf-design row {row.name}: it is labelled `baseline`, "
                    f"but the baseline is {budgets.baseline}; a row's cells vary "
                    f"{len(derived)} dimension(s) from it, so write `{wanted}`"
                )
            else:
                problems.append(
                    f"gate-perf-design row {row.name}: it is labelled "
                    f"`baseline@{family}`, but baseline.{family} is "
                    f"{budgets.named[family]}, so a baseline label names only "
                    "the family whose reference row the row is; its cells vary "
                    f"{len(derived)} dimension(s) from {against} {referenced}, "
                    f"so write `{wanted}`"
                )
            continue
        problem = None
        if not derived and relation.kind != "re-measurement":
            problem = (
                f"gate-perf-design row {row.name}: its cells name no dimension "
                f"that differs from {against} {referenced} "
                "(every dimension it names repeats the baseline's value), so "
                "it is a deliberate repeat and must say why; write "
                "`re-measurement(<reason>)` (e.g. a second tier or a "
                f"stability re-run), not `{relation.kind}`"
            )
        elif derived and len(derived) == 1 and relation.kind != "orthogonal":
            problem = (
                f"gate-perf-design row {row.name}: it varies exactly one "
                f"dimension from {against} ({derived[0]}), so write "
                f"`{relation_display('orthogonal', family)}`, not `{relation.kind}`"
            )
        elif derived and len(derived) > 1 and relation.kind == "re-measurement":
            problem = (
                f"gate-perf-design row {row.name}: it is labelled a "
                f"re-measurement, but its cells vary {len(derived)} dimension(s) "
                f"from {against} ({', '.join(derived)}), so write `{wanted}`"
            )
        elif derived and len(derived) > 1 and relation.kind != "composite":
            problem = (
                f"gate-perf-design row {row.name}: its cells vary {len(derived)} "
                f"dimension(s) from {against} ({', '.join(derived)}), so write "
                f"`{wanted}`"
            )
        elif relation.kind == "composite" and set(relation.keys) != set(derived):
            problem = (
                f"gate-perf-design row {row.name}: it is labelled "
                f"composite({','.join(relation.keys)}){relation_display('', family)}, "
                f"but its cells vary "
                f"{', '.join(derived) or 'nothing'}; name exactly the dimensions "
                "the cells vary, or fix the cells"
            )
        if problem is not None:
            problems.append(problem)
            continue
        bump(relation.kind)

    # A baseline no row states a relation against is a stale reference: either
    # the family was emptied by a shortening or the `@<family>` suffixes were
    # dropped, and either way the declaration has rotted past what it enforces.
    for family, name in sorted(families.items(), key=lambda item: (item[0] is not None, item[0] or "")):
        if family not in base_state:
            continue
        used = sum(
            1
            for row in rows
            if row.name != name
            and row.relation is not None
            and row.relation.family == family
        )
        if used:
            continue
        label = f"baseline = {name}" if family is None else f"baseline.{family} = {name}"
        problems.append(
            f"gate-budgets: {label} is declared but no row states a relation "
            "against it; a baseline no row uses is a stale reference - remove "
            "the line, or state the row that belongs to the family"
        )
        continue

    order = ("orthogonal", "composite", "re-measurement", "baseline")
    totals = ", ".join(f"{counts[kind]} {kind}" for kind in order)
    if not budgets.named:
        summary.append(
            f"  gate-perf-relations: {totals} of {len(rows)} row(s), stated "
            f"against {budgets.baseline}"
        )
    else:
        summary.append(
            f"  gate-perf-relations: {totals} of {len(rows)} row(s) across "
            f"{len(families)} baseline(s)"
        )
        for family in sorted(families, key=lambda f: (f is not None, f or "")):
            if family not in base_state:
                continue
            tally = family_counts.get(family, {})
            name = families[family]
            label = f"default({name})" if family is None else f"{family}({name})"
            summary.append(
                f"  gate-perf-family: {label} {tally.get('orthogonal', 0)} "
                f"orthogonal, {tally.get('composite', 0)} composite, "
                f"{tally.get('re-measurement', 0)} re-measurement, "
                f"{tally.get('baseline', 0)} baseline"
            )
    for row in rows:
        if row.relation is not None and row.relation.kind == "composite":
            family = row.relation.family
            reference = budgets.baseline if family is None else budgets.named.get(family)
            summary.append(
                f"  gate-perf-composite: {row.name} varies "
                f"{', '.join(varied_by_row.get(row.name, ()))}"
                f" against {reference}"
            )
    return summary


def cell_name(cell: str) -> str:
    """A cell's property name: the part before its `@<dimension>=<value>`."""
    return cell.partition("@")[0]


def prefix_matches(pattern: str, name: str) -> bool:
    """Whether a cell name falls in a `members.<family>` namespace.

    `foo` matches only itself; `foo*` matches every name starting with `foo`.
    """
    return name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern


def prefix_hint(names: list[str]) -> str:
    """The narrowest prefix namespace covering ``names``, or '' if none does.

    The longest common prefix of the names, made a prefix namespace with `*`.
    An empty common prefix has no namespace at all (`*` alone declares
    nothing), so the diagnostic falls back to naming the cells instead.
    """
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        index = 0
        while index < min(len(prefix), len(name)) and prefix[index] == name[index]:
            index += 1
        prefix = prefix[:index]
    if not prefix:
        return ""
    return prefix if len(set(names)) == 1 else prefix + "*"


def check_perf_membership(
    rows: list[PerfRow], budgets: PerfBudgets, problems: list[str]
) -> list[str]:
    """Verify each row's cells name the family its relation states against.

    Membership is a property of the row, derived from its own cells: a cell's
    property is the name, a named family declares the namespace those names
    start with (`members.<family> = <prefix>`), and the default family owns the
    residual. A row's every cell name must fall in the namespace of the family
    it names, a cell name may not be claimed by two families, a family's
    namespace must contain its own reference row (the family is identified by
    the cells its reference carries), and a row whose cells occupy a family's
    namespace must name that family. Every failure names the row or the line
    and what to write instead. Returns the summary lines for the passing case.
    """
    summary: list[str] = []
    if budgets.baseline is None:
        return summary
    by_name = {row.name: row for row in rows}
    families: dict[str | None, str] = {None: budgets.baseline}
    families.update(budgets.named)

    row_names = [cell_name(cell) for row in rows for cell in row.cells]
    all_names = sorted(set(row_names))
    family_rows: dict[str | None, list[PerfRow]] = {}
    for row in rows:
        family = row.relation.family if row.relation is not None else None
        family_rows.setdefault(family, []).append(row)

    def names_of(family: str | None) -> list[str]:
        return sorted({cell_name(cell) for row in family_rows.get(family, ()) for cell in row.cells})

    def write_members(family: str | None) -> str:
        label = "members" if family is None else f"members.{family}"
        hint = prefix_hint(names_of(family))
        return f"`{label} = {hint}`" if hint else f"`{label} = <prefix>`"

    # Declaration side: a namespace for every named family, none for an
    # undeclared one, and a namespace that its own cells actually occupy.
    for family in sorted(budgets.named):
        if family in budgets.members:
            continue
        names = names_of(family)
        hint = prefix_hint(names)
        wanted = (
            f"`members.{family} = {hint}`"
            if hint
            else "a shared cell name (`members.<family> = <prefix>`), because its "
            f"rows' cells are named {', '.join(names)} and share no prefix"
        )
        problems.append(
            f"gate-budgets: baseline.{family} declares a family whose cell-name "
            f"namespace is not declared; write {wanted} so a row's cells decide "
            "whether it belongs to the family"
        )
    for family in sorted(set(budgets.members) - set(budgets.named)):
        problems.append(
            f"gate-budgets: members.{family} = {budgets.members[family]} declares "
            "the cell-name namespace of a family gate-budgets does not declare; "
            f"declare `baseline.{family} = <row>` or remove the line"
        )
    claimed: dict[str, list[str]] = {}
    for family, pattern in budgets.members.items():
        for name in all_names:
            if prefix_matches(pattern, name):
                claimed.setdefault(name, []).append(family)
    for family in sorted(budgets.members):
        pattern = budgets.members[family]
        matches = [name for name in all_names if prefix_matches(pattern, name)]
        if not matches:
            problems.append(
                f"gate-budgets: members.{family} = {pattern} matches no row's cell "
                "name, so it declares a namespace nothing occupies; write the "
                "prefix the family's cells carry "
                f"({', '.join(names_of(family)) or 'none'}) or remove the line"
            )
            continue
        if not family_rows.get(family):
            continue
        outside = [name for name in names_of(family) if not prefix_matches(pattern, name)]
        if outside:
            problems.append(
                f"gate-budgets: members.{family} = {pattern} does not cover the "
                f"family's own cells ({', '.join(outside)}); a family's namespace "
                f"is where its rows live, so write {write_members(family)}"
            )
    for name, owners in sorted(claimed.items()):
        if len(owners) < 2:
            continue
        declared = ", ".join(
            f"members.{family} = {budgets.members[family]}" for family in owners
        )
        problems.append(
            f"gate-budgets: the cell name {name!r} is claimed by {len(owners)} "
            f"families ({declared}); a cell name belongs to exactly one family, "
            "so narrow one namespace until the cell names are disjoint"
        )

    # Row side: the reference row of a family is inside its namespace, and
    # every row's cells decide the family it belongs to.
    for family, name in sorted(families.items(), key=lambda item: (item[0] is not None, item[0] or "")):
        row = by_name.get(name)
        pattern = budgets.members.get(family or "")
        if row is None or family is None or pattern is None:
            continue
        outside = sorted({cell_name(cell) for cell in row.cells if not prefix_matches(pattern, cell_name(cell))})
        if outside:
            problems.append(
                f"gate-perf-design row {row.name} is the reference of "
                f"baseline.{family}, but its cells are named {', '.join(outside)}, "
                f"which members.{family} = {pattern} does not claim; the family's "
                f"namespace must contain its own reference, so write "
                f"{write_members(family)}"
            )

    def owner_phrase(owners: list[str]) -> str:
        return ", ".join(
            f"family {family!r} (members.{family} = {budgets.members[family]})"
            for family in owners
        )

    misfiled = 0
    for row in rows:
        family = row.relation.family if row.relation is not None else None
        names = sorted({cell_name(cell) for cell in row.cells})
        owners: list[str] = []
        for name in names:
            for owner in claimed.get(name, ()):
                if owner not in owners:
                    owners.append(owner)
        pattern = budgets.members.get(family or "")
        if family is not None and pattern is None:
            # The missing declaration is already named; the row cannot be
            # checked against a namespace that does not exist.
            continue
        if family is not None and any(
            not prefix_matches(pattern, name) for name in names
        ):
            misfiled += 1
            outside = [name for name in names if not prefix_matches(pattern, name)]
            if len(owners) == 1:
                hint = (
                    f"; those cells belong to {owner_phrase(owners)}, so state the "
                    f"row against `@{owners[0]}` or move the name out of that "
                    "namespace"
                )
            elif len(owners) > 1:
                hint = (
                    f", and {len(owners)} families claim them "
                    f"({owner_phrase(owners)}), so one namespace must be narrowed "
                    "before the row can be filed"
                )
            else:
                hint = (
                    f"; declare it as this family's own cell name: "
                    f"{write_members(family)}"
                )
            problems.append(
                f"gate-perf-design row {row.name}: its cells are named "
                f"{', '.join(outside)}, which members.{family} = {pattern} does "
                "not claim, so the row's cells and the family it names "
                f"disagree{hint}"
            )
            continue
        if len(owners) > 1:
            misfiled += 1
            problems.append(
                f"gate-perf-design row {row.name}: its cells are named "
                f"{', '.join(names)}, which {len(owners)} families claim "
                f"({owner_phrase(owners)}); a cell name belongs to one family, so "
                "the row cannot be filed by its cells until one namespace is "
                "narrowed"
            )
            continue
        if owners and family != owners[0]:
            misfiled += 1
            if family is None:
                problems.append(
                    f"gate-perf-design row {row.name} names no family, but its "
                    f"cells are named {', '.join(names)}, which belong to "
                    f"{owner_phrase(owners)}; a row whose cells occupy a family's "
                    f"namespace must state against it - write `@{owners[0]}`"
                )
            else:
                problems.append(
                    f"gate-perf-design row {row.name}: its cells are named "
                    f"{', '.join(names)}, which belong to {owner_phrase(owners)} "
                    f"rather than family {family!r}; state the row against "
                    f"`@{owners[0]}` or move the name out of that namespace"
                )

    if not budgets.members:
        return summary
    residual = sorted(name for name in all_names if name not in claimed)
    summary.append(
        f"  gate-perf-membership: {len(budgets.members)} cell-name namespace(s) "
        f"declared, {len(claimed)} cell name(s) claimed, {misfiled} row(s) stated "
        "outside the namespace of the family they name"
    )
    for family in sorted(budgets.members):
        rows_in = family_rows.get(family, [])
        summary.append(
            f"  gate-perf-namespace: {family}({budgets.members[family]}) "
            f"{len(rows_in)} row(s), cells: {', '.join(names_of(family))}"
        )
    if residual:
        summary.append(
            "  gate-perf-namespace: default(residual) "
            f"{len(family_rows.get(None, []))} row(s), cells: {', '.join(residual)}"
        )
    return summary


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
    relation_summary = check_perf_relations(rows, budgets, problems)
    membership_summary = check_perf_membership(rows, budgets, problems)

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
    named = (
        f" (+{len(budgets.named)} named: {', '.join(sorted(budgets.named))})"
        if budgets.named
        else ""
    )
    summary = [
        f"  gate-perf-design: {len(rows)} perf test row(s), {cells} coverage "
        f"cell(s), {len(gaps)} gap(s), baseline "
        f"{budgets.baseline or 'unset'}{named}"
    ]
    summary.extend(relation_summary)
    summary.extend(membership_summary)
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

    # The documented counts: a number in prose that a command already
    # determines is verified against the source that determines it, or has left
    # the prose for the command that prints it. Harness-only, because the
    # documents are the harness's operating doc and the counts are this
    # repository's declarations.
    if layout().is_harness:
        doc_problems, doc_summary = check_doc_counts(layout().root)
        for problem in doc_problems:
            print(problem)
        bad = bad or bool(doc_problems)
    else:
        doc_summary = []

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
    for line in doc_summary:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())