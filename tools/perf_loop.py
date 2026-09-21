#!/usr/bin/env python3
"""One-command paired performance capture loop for netem_test workspaces.

Runs the hostile/clean goodput probe against a baseline and a candidate
frozen netem_test workspace for a set of seeds, writes every output and
temporary beneath $TMPDIR, calls rtp_trace_compare, and reports a
consistency verdict. Diagnostic mode is always enabled so the absolute
hostile-goodput floor cannot abort evidence collection, but payload
integrity, task outcome, probe exit status, and comparison health stay
enforced.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
SAFE_TEMP_ROOT = Path.home() / "code" / "tmp"
DEFAULT_SEEDS = (11, 21)
DEFAULT_WARMUP_SECONDS = 5.0
MATERIAL_PHASE_DRIFT_PERCENT = 20.0
PERF_TEST = "probe_hostile_goodput_30s"
# Executable probes driven by the perf loop. The bulk goodput probe covers
# the hostile-steady bottleneck, recoverable, gaming, and paired-saturated
# lanes; the message-latency probe covers the 20/100/300 ms periodic
# bottleneck lanes (both are ignored release-mode executable tests).
PROBE_TESTS = {
    "bulk": "probe_hostile_goodput_30s",
    "message-latency": "probe_hostile_message_latency",
}
LINK_PROFILES = (
    "hostile",
    "hostile-steady",
    "hostile-steady-bottleneck",
    "hostile-steady-bottleneck-20ms",
    "hostile-steady-bottleneck-100ms",
    "hostile-periodic-bottleneck",
    "lossy-400kib",
    "hostile-fat-pipe",
    "controller-fat-pipe",
    "deterministic-iid-loss-fat-pipe",
    "clean",
    "direct",
    "hostile-bottleneck-20ms",
    "hostile-bottleneck-100ms",
    "hostile-bottleneck-300ms",
    "fec-recoverable-bottleneck",
    "fec-gaming-fat-pipe",
    "fec-paired-saturated",
    "fec-paired-saturated-bottleneck",
    "hostile-periodic-bottleneck-20ms",
    "hostile-periodic-bottleneck-100ms",
    "hostile-periodic-bottleneck-300ms",
)
# Lane roles for a retention decision.  `verdict` lanes may retain or reject a
# change; `diagnostic` lanes report their numbers only and must never be read as
# a verdict instrument.  `hostile` is diagnostic-only: every one of the 70
# recorded `hostile` lane runs (both 5 s and 20 s warmup, with a byte-identical
# baseline/candidate control tree) returned `not_ready`
# (`within_run_phase_not_stable`), and the spread was the lane's own stochastic
# phase variance rather than a candidate effect, so the lane cannot produce a
# verdict.  The role of every lane is documented in tests/GATE.md
# (`gate-lane-roles`) and machine-checked by tools/check-gate.py against
# `lane_classification`, so a lane cannot be mis-declared verdict or diagnostic.
DIAGNOSTIC_LANES = ("hostile",)


def lane_classification(profile):
    """The role a `--link-profile` lane plays in a retention decision.

    A `diagnostic` lane is a report-only instrument: its paired numbers are
    recorded and may guide follow-up work, but a change must never be retained
    or rejected on it, because the lane is not phase-stable enough to attribute
    a delta to the candidate.
    """
    return "diagnostic" if profile in DIAGNOSTIC_LANES else "verdict"


COMPONENTS = ("netem_test", "rtp", "mux", "rtp_mux", "tokio_udp", "udp_listener")
# The suite component whose tree owns the perf-loop probe's code. The probe
# drives `mux`-over-`rtp` lanes - the `rtp`+`mux` cooperation - and lives in
# `rtp_mux`'s own test targets, so the frozen suite builds it from the exported
# `rtp_mux` tree: the same tree a `--component-revision rtp_mux=<commit>` pin
# selects. Building it from the harness instead would compile the instrument's
# conformance package and make that pin select no probe code at all: a silent
# false negative.
PROBE_COMPONENT = "rtp_mux"
PROBE_PACKAGE = "rtp_mux"
PROBE_TARGET = "perf_probe"
SUITE_REVISION_MANIFEST = "suite-revisions.json"
SUITE_REVISION_MANIFEST_SCHEMA = 2
PROBE_SOURCE_MANIFEST_SCHEMA = 1


def parse_seeds(value):
    try:
        seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be comma-separated u64 values") from error
    if not seeds or any(seed < 0 or seed > 2**64 - 1 for seed in seeds):
        raise argparse.ArgumentTypeError("seeds must be comma-separated u64 values")
    if len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("seeds must not contain duplicates")
    return seeds


def parse_label(value):
    if not value or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    ):
        raise argparse.ArgumentTypeError(
            "Labels may contain only letters, digits, dot, underscore, and hyphen"
        )
    if value in (".", ".."):
        raise argparse.ArgumentTypeError("Label must name a run")
    return value


def parse_nonnegative_seconds(value):
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "seconds must be a finite non-negative number"
        ) from error
    if not math.isfinite(seconds) or seconds < 0.0:
        raise argparse.ArgumentTypeError("seconds must be a finite non-negative number")
    return seconds


def parse_component_revision(value):
    """Parse one repeatable snapshot 'COMPONENT=REVISION' override.

    Partition exactly once on '='; any missing part or an empty revision
    part is a usage error, and the result is the (component, revision) pair.
    """
    component, separator, revision = value.partition("=")
    if not separator or not component or not revision:
        raise argparse.ArgumentTypeError(
            "component revision must use COMPONENT=REVISION"
        )
    if component not in COMPONENTS:
        raise argparse.ArgumentTypeError(f"unknown suite component: {component}")
    return (component, revision)


def safe_output_dir(requested=None):
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    if requested is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        requested = safe_root / f"net-perf-loop-{stamp}-{os.getpid()}"
    resolved = Path(requested).expanduser().resolve()
    if not resolved.is_relative_to(safe_root):
        raise ValueError(f"performance output must remain beneath {safe_root}")
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def probe_component_workspace(workspace):
    """The suite component that owns the perf-loop probe's code.

    The probe is a `mux` test target beside the harness workspace, so it is
    built from the `PROBE_COMPONENT` sibling of the named netem_test workspace
    (mutable or frozen). The probe's source file must exist there: a probe
    built from a component that does not carry it cannot reflect that
    component's revision, and the run would report the pin against nothing.
    """
    probe_workspace = Path(workspace).parent / PROBE_COMPONENT
    if not (probe_workspace / "Cargo.toml").is_file():
        raise ValueError(
            f"the perf-loop probe is built from the {PROBE_COMPONENT!r} suite "
            f"component, whose workspace {probe_workspace} has no Cargo.toml"
        )
    source = probe_workspace / "tests" / f"{PROBE_TARGET}.rs"
    if not source.is_file():
        raise ValueError(
            f"the perf-loop probe source {source} does not exist: the "
            f"{PROBE_TARGET!r} target belongs to another suite component than "
            f"{PROBE_COMPONENT!r}"
        )
    return probe_workspace


def safe_build_dir(requested, workspace, profile):
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    requested = requested if requested is not None else safe_root / "net-perf-targets" / f"{hashlib.sha256(str(workspace).encode()).hexdigest()[:16]}-{profile}" / "target"
    resolved = Path(requested).expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"Cargo target directory must remain beneath {safe_root}")) if not resolved.is_relative_to(safe_root) else None
    (_ for _ in ()).throw(ValueError("Cargo target directory must use a final 'target' path component; managed hosts may reject generated executables from custom layouts")) if resolved.name != "target" else None
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def safe_temp_dir(role, seed):
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    resolved = (safe_root / f"net-perf-tmp-{stamp}-{os.getpid()}-{role}-{seed}").resolve()
    (_ for _ in ()).throw(ValueError(f"probe temporary directory must remain beneath {safe_root}")) if not resolved.is_relative_to(safe_root) else None
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def validate_workspace(path, role):
    workspace = path.expanduser().resolve()
    if not (workspace / "Cargo.toml").is_file() or not (workspace / "tests" / "Cargo.toml").is_file():
        raise ValueError(f"{role} is not a netem_test workspace: {workspace}")
    return workspace


def frozen_suite_manifest(directory):
    """Return validated frozen-suite metadata for a component, when present."""
    directory = Path(directory).resolve()
    manifest_path = directory.parent / SUITE_REVISION_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    components = manifest.get("components")
    if manifest.get("schema") != SUITE_REVISION_MANIFEST_SCHEMA or not isinstance(components, dict):
        return None
    if set(components) != set(COMPONENTS):
        return None
    for record in components.values():
        if not isinstance(record, dict):
            return None
        commit_id = record.get("commit_id")
        if not isinstance(commit_id, str) or len(commit_id) != 40:
            return None
    return manifest


def jj_revision(directory, revision="@"):
    """Exact jj identity (40-char commit_id plus change_id) of a workspace
    or sibling component repo at a resolved revision."""
    directory = Path(directory)
    frozen = frozen_suite_manifest(directory)
    if frozen is not None and directory.name in frozen["components"]:
        return frozen["components"][directory.name]
    if not directory.is_dir():
        return {"commit_id": "unknown", "change_id": "unknown"}
    template = 'commit_id ++ "\n" ++ change_id'
    try:
        result = subprocess.run(
            ["jj", "--no-pager", "log", "-r", revision, "--no-graph", "-T", template],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {"commit_id": "unknown", "change_id": "unknown"}
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return {"commit_id": "unknown", "change_id": "unknown"}
    return {"commit_id": lines[0], "change_id": lines[1]}


def jj_identity(directory, revision):
    """Resolve an exact JJ commit/change identity without changing a workspace."""
    template = 'commit_id ++ "\n" ++ change_id'
    result = subprocess.run(
        ["jj", "--no-pager", "log", "-r", revision, "--no-graph", "-T", template],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown JJ error"
        raise ValueError(f"cannot resolve {revision!r} in {directory}: {detail}")
    return {"commit_id": lines[0], "change_id": lines[1]}


def safe_extract_tar(archive_path, destination):
    """Extract a trusted Git archive while rejecting traversal or link entries."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r") as archive:
        for member in archive.getmembers():
            resolved = (destination / member.name).resolve()
            if not resolved.is_relative_to(destination):
                raise ValueError(f"archive path escapes destination: {member.name!r}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive contains a link: {member.name!r}")
        archive.extractall(destination)


def snapshot_component(source, destination, revision):
    """Export one exact JJ-backed Git tree into a non-workspace directory."""
    identity = jj_identity(source, revision)
    git_root = subprocess.run(
        ["jj", "--no-pager", "git", "root"],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if git_root.returncode != 0:
        detail = git_root.stderr.strip() or git_root.stdout.strip() or "unknown JJ error"
        raise ValueError(f"cannot locate Git store for {source}: {detail}")
    archive_path = destination.parent / f".{destination.name}.tar"
    try:
        archive = subprocess.run(
            ["git",
             f"--git-dir={git_root.stdout.strip()}",
             "archive",
             "--format=tar",
             f"--output={archive_path}",
             identity["commit_id"]],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if archive.returncode != 0:
            detail = archive.stderr.strip() or archive.stdout.strip() or "unknown Git error"
            raise ValueError(f"cannot archive {source}: {detail}")
        safe_extract_tar(archive_path, destination)
    finally:
        archive_path.unlink(missing_ok=True)
    return identity


# Every component's committed manifests name their sibling components through
# published git tags.  A tree exported exactly as committed therefore fetches
# the *tagged* sibling instead of the sibling exported beside it, so
# `--component-revision rtp=<commit>` selects no code at all and a paired run
# compares the tag against itself: a silent false negative.  `snapshot`
# rewrites each inter-component source locator to the exported sibling's
# relative path, so the pinned revision is what compiles and runs.  The
# component trees stay byte-exact exports of their committed revisions; only
# the frozen build recipe changes, and every rewrite is recorded in
# `suite-revisions.json`.  A dependency edge that names a suite component in a
# shape this rewrite does not model is refused, never left to resolve from its
# tag.
DEPENDENCY_TABLE_KINDS = (
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
)
# The keys that name a dependency's *source*; one of them decides whether a
# frozen edge resolves to an exported sibling or to a published tag.
DEPENDENCY_SOURCE_KEYS = ("git", "path", "workspace")
DEPENDENCY_GIT_TAG_KEYS = ("git", "tag", "rev", "branch")
_TOML_ASSIGNMENT = re.compile(
    r'^(?P<key>[A-Za-z0-9_.-]+|"[^"]*"|\'[^\']*\')[ \t]*(?P<equals>=)'
)


def _toml_unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_top_level(text, separator):
    """Split on `separator`, ignoring separators inside strings/brackets."""
    parts = []
    stack = []
    quote = None
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\" and quote == '"':
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character in "{[(":
            stack.append(character)
        elif character in "}])":
            if stack:
                stack.pop()
        elif character == separator and not stack:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _toml_table_path(header):
    """The dotted key path of a `[table]` header line, or None for arrays."""
    stripped = header.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    closing = stripped.rfind("]")
    if closing <= 0:
        return None
    return tuple(
        _toml_unquote(part)
        for part in _split_top_level(stripped[1:closing], ".")
    )


def _toml_value_end(text, start):
    """The offset just past the TOML value that begins at `start`."""
    if start >= len(text):
        return start
    opener = text[start]
    if opener in "{[":
        depth = 0
        quote = None
        index = start
        while index < len(text):
            character = text[index]
            if quote is not None:
                if character == "\\" and quote == '"':
                    index += 2
                    continue
                if character == quote:
                    quote = None
            elif character in "\"'":
                quote = character
            elif character in "{[":
                depth += 1
            elif character in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ValueError("unterminated TOML inline value")
    if opener in "\"'":
        quote = opener
        index = start + 1
        while index < len(text):
            if text[index] == "\\" and quote == '"':
                index += 2
                continue
            if text[index] == quote:
                return index + 1
            index += 1
        raise ValueError("unterminated TOML string")
    end = start
    while end < len(text) and text[end] not in "\n#":
        end += 1
    return end


def _toml_entries(text):
    """`(table, key, key_start, line_start, value, value_start, value_end)`
    for every key/value assignment of a TOML document, in document order.

    A dependency value may span lines (an inline table holding a multi-line
    array), so each value is consumed in full before the scan advances to the
    next line.
    """
    entries = []
    table = ()
    position = 0
    length = len(text)
    while position < length:
        line_end = text.find("\n", position)
        if line_end == -1:
            line_end = length
        line = text[position:line_end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            position = line_end + 1
            continue
        if stripped.startswith("["):
            table = _toml_table_path(stripped) or ()
            position = line_end + 1
            continue
        match = _TOML_ASSIGNMENT.match(line)
        if match is None:
            position = line_end + 1
            continue
        value_start = position + match.end("equals")
        while value_start < length and text[value_start] in " \t":
            value_start += 1
        value_end = _toml_value_end(text, value_start)
        entries.append(
            (
                table,
                _toml_unquote(match.group("key")),
                position + match.start("key"),
                position,
                text[value_start:value_end],
                value_start,
                value_end,
            )
        )
        following = text.find("\n", value_end)
        position = length if following == -1 else following + 1
    return entries


def _inline_table_fields(value, base):
    """The declared fields of an inline table, keyed by field name."""
    body_start = value.find("{") + 1
    body_end = value.rfind("}")
    body = value[body_start:body_end]
    fields = {}
    offset = 0
    for chunk in _split_top_level(body, ","):
        chunk_start = base + body_start + offset
        offset += len(chunk) + 1
        if not chunk.strip():
            continue
        key, separator, _ = chunk.partition("=")
        if not separator:
            raise ValueError(
                f"unrecognised inline dependency field: {chunk.strip()!r}"
            )
        value_offset = chunk.index("=") + 1
        raw_value = chunk[value_offset:]
        stripped_value = raw_value.strip()
        leading = len(raw_value) - len(raw_value.lstrip(" \t"))
        fields[_toml_unquote(key)] = {
            "value": stripped_value,
            "value_start": chunk_start + value_offset + leading,
            "value_end": chunk_start + value_offset + leading + len(stripped_value),
            "key_start": chunk_start,
            "line_start": None,
            "raw": chunk.strip(),
        }
    return fields


def _frozen_dependency_edges(text):
    """Every dependency a manifest declares, grouped by `(table, crate)`.

    Both the inline form (`rtp = { git = ..., tag = ... }`) and the expanded
    form (`[dependencies.rtp]` followed by `git = ...`, or a dotted
    `rtp.workspace = true`) are read into the same shape, so the rewrite sees
    the same edges cargo would resolve.
    """

    def field(value, value_start, value_end, key_start, line_start, raw=None):
        return {
            "value": value,
            "value_start": value_start,
            "value_end": value_end,
            "key_start": key_start,
            "line_start": line_start,
            "raw": raw,
        }

    edges = {}
    for table, key, key_start, line_start, value, value_start, value_end in _toml_entries(text):
        if table and table[-1] in DEPENDENCY_TABLE_KINDS:
            crate, separator, dotted = key.partition(".")
            edge = edges.setdefault(
                (table, crate),
                {"table": table, "crate": crate, "style": None, "span": None, "fields": {}},
            )
            if separator:
                edge["style"] = "expanded"
                edge["fields"][dotted] = field(
                    value, value_start, value_end, key_start, line_start
                )
                continue
            if edge["span"] is not None:
                raise ValueError(f"duplicate dependency entry for {crate!r}")
            edge["span"] = (value_start, value_end)
            edge["style"] = "inline" if value.startswith("{") else "scalar"
            if edge["style"] == "inline":
                edge["fields"] = _inline_table_fields(value, value_start)
            else:
                edge["fields"] = {
                    "version": field(value, value_start, value_end, key_start, line_start)
                }
        elif len(table) >= 2 and table[-2] in DEPENDENCY_TABLE_KINDS:
            edge = edges.setdefault(
                (table[:-1], table[-1]),
                {
                    "table": table[:-1],
                    "crate": table[-1],
                    "style": "expanded",
                    "span": None,
                    "fields": {},
                },
            )
            edge["fields"][key] = field(
                value, value_start, value_end, key_start, line_start
            )
    return list(edges.values())


def _declared_dependencies(document):
    """`(table, crate) -> declared field names` from a parsed manifest.

    The parsed view is the authority the scanner is asserted against: a
    dependency shape the scanner does not model would otherwise be dropped
    silently, taking its sibling pin with it.
    """
    declared = {}

    def collect(table, entries):
        if not isinstance(entries, dict):
            return
        for crate, spec in entries.items():
            declared[(table, crate)] = set(spec) if isinstance(spec, dict) else {"version"}

    for kind in DEPENDENCY_TABLE_KINDS:
        collect((kind,), document.get(kind))
    targets = document.get("target")
    if isinstance(targets, dict):
        for target, tables in targets.items():
            if not isinstance(tables, dict):
                continue
            for kind in DEPENDENCY_TABLE_KINDS:
                collect(("target", target, kind), tables.get(kind))
    workspace = document.get("workspace")
    if isinstance(workspace, dict):
        collect(("workspace", "dependencies"), workspace.get("dependencies"))
    return declared


def assert_dependency_inventory(manifest, text):
    """Refuse a manifest whose dependency shapes the rewrite does not model."""
    declared = _declared_dependencies(tomllib.loads(text))
    scanned = {
        (edge["table"], edge["crate"]): set(edge["fields"])
        for edge in _frozen_dependency_edges(text)
    }
    if declared != scanned:
        raise ValueError(
            f"frozen manifest {manifest} declares dependency shapes the "
            "inter-component rewrite does not model "
            f"(unscanned={sorted(str(key) for key in declared.keys() - scanned.keys())}, "
            f"unknown={sorted(str(key) for key in scanned.keys() - declared.keys())}, "
            f"fields={sorted(str(key) for key in declared.keys() & scanned.keys() if declared[key] != scanned[key])}); "
            "refusing to snapshot a suite whose sibling pin could be "
            "silently ignored"
        )


def dependency_repository_component(url):
    """The suite component a git URL names, or None.

    Only `Banyc`'s own repositories are the suite's published siblings.  A
    repository that merely shares a component's name (a fork, another host)
    is a different source and is never silently redirected to the export.
    """
    parts = [part for part in re.split(r"[/:]", url.strip().rstrip("/")) if part]
    if len(parts) < 2 or parts[-2].lower() != "banyc":
        return None
    name = parts[-1].removesuffix(".git").replace("-", "_").lower()
    return name if name in COMPONENTS else None


def _toml_string(value, manifest):
    try:
        parsed = tomllib.loads(f"value = {value}\n")["value"]
    except tomllib.TOMLDecodeError as error:
        raise ValueError(
            f"cannot read a dependency field of {manifest}: {value!r}"
        ) from error
    if not isinstance(parsed, str):
        raise ValueError(f"dependency field of {manifest} is not a string: {value!r}")
    return parsed


def _frozen_manifests(component_root):
    component_root = Path(component_root)
    if not component_root.is_dir():
        return []
    return sorted(
        path
        for path in component_root.rglob("Cargo.toml")
        if not {"target", ".git"} & set(path.relative_to(component_root).parts)
    )


def frozen_suite_crates(export_root):
    """`crate name -> (component, crate directory)` for an exported suite."""
    crates = {}
    for component in COMPONENTS:
        for manifest in _frozen_manifests(Path(export_root) / component):
            document = tomllib.loads(manifest.read_text(encoding="utf-8"))
            name = (document.get("package") or {}).get("name")
            if not isinstance(name, str):
                continue
            existing = crates.get(name)
            if existing is not None:
                raise ValueError(
                    f"two exported components provide the crate {name!r}: "
                    f"{existing[0]} and {component}"
                )
            crates[name] = (component, manifest.parent)
    return crates


def _apply_replacements(text, replacements):
    ordered = sorted(replacements, key=lambda item: item[0])
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError("overlapping frozen manifest dependency rewrites")
    for start, end, value in reversed(ordered):
        text = text[:start] + value + text[end:]
    return text


def _source_locator_replacements(text, edge, fields, relative):
    """Replace a dependency's published-git locator with the sibling path."""
    quoted = json.dumps(relative)
    if edge["style"] != "expanded":
        retained = [
            field["raw"]
            for name, field in fields.items()
            if name not in DEPENDENCY_GIT_TAG_KEYS and field["raw"]
        ]
        value = "{ path = " + quoted + "".join(
            f", {raw}" for raw in retained
        ) + " }"
        return [(edge["span"][0], edge["span"][1], value)]
    replacements = [
        (
            fields["git"]["key_start"],
            fields["git"]["value_end"],
            "path = " + quoted,
        )
    ]
    for name in ("tag", "rev", "branch"):
        field = fields.get(name)
        if field is None:
            continue
        line_start = field["line_start"]
        line_end = text.find("\n", field["value_end"])
        replacements.append(
            (line_start, len(text) if line_end == -1 else line_end + 1, "")
        )
    return replacements


def _require_exported_sibling_path(manifest, value, export_root):
    resolved = (manifest.parent / value).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"frozen manifest {manifest} declares the path dependency "
            f"{value!r}, which does not exist in the export"
        )
    if not resolved.is_relative_to(export_root):
        raise ValueError(
            f"frozen manifest {manifest} declares the path dependency "
            f"{value!r}, which escapes the frozen suite"
        )
    return resolved


def _assert_no_patched_suite_source(manifest, text):
    """Refuse a patch/replace entry that re-sources a suite crate from git."""
    for table, key, _key_start, _line_start, value, _value_start, _value_end in _toml_entries(text):
        if not table or table[0] not in ("patch", "replace"):
            continue
        if value.startswith("{"):
            raw = (_inline_table_fields(value, 0).get("git") or {}).get("value")
        elif key == "git" and len(table) >= 3:
            # The expanded `[patch."<url>".<crate>]` form.
            raw = value
        else:
            continue
        if raw is None:
            continue
        if dependency_repository_component(_toml_string(raw, manifest)) is not None:
            raise ValueError(
                f"frozen manifest {manifest} patches a suite crate onto a "
                f"git repository ({raw}); refusing to snapshot a suite whose "
                "sibling pin could be silently ignored"
            )


def _edge_table_label(table):
    return ".".join(table)


def rewrite_frozen_manifest_dependencies(manifest, export_root, crates):
    """Point one frozen manifest's suite edges at the exported sibling trees."""
    manifest = Path(manifest)
    text = manifest.read_text(encoding="utf-8")
    assert_dependency_inventory(manifest, text)
    _assert_no_patched_suite_source(manifest, text)
    relative_manifest = manifest.relative_to(Path(export_root)).as_posix()
    replacements = []
    records = []
    for edge in _frozen_dependency_edges(text):
        fields = edge["fields"]
        crate = edge["crate"]
        base = crate.split(".", 1)[0]
        named_source = next(
            (key for key in DEPENDENCY_SOURCE_KEYS if key in fields), None
        )
        if named_source == "git":
            url = _toml_string(fields["git"]["value"], manifest)
            component = dependency_repository_component(url)
            if component is not None:
                sibling = crates.get(base)
                if sibling is None or sibling[0] != component:
                    raise ValueError(
                        f"frozen manifest {manifest} names the suite crate "
                        f"{crate!r} from the {component!r} repository, which "
                        "this rewrite cannot map to an exported sibling crate; "
                        "refusing to snapshot a suite whose sibling pin could "
                        "be silently ignored"
                    )
                relative = Path(os.path.relpath(sibling[1], manifest.parent)).as_posix()
                records.append(
                    {
                        "manifest": relative_manifest,
                        "table": _edge_table_label(edge["table"]),
                        "crate": crate,
                        "from": {
                            key: _toml_string(fields[key]["value"], manifest)
                            for key in DEPENDENCY_GIT_TAG_KEYS
                            if key in fields
                        },
                        "to": {"path": relative},
                    }
                )
                replacements.extend(
                    _source_locator_replacements(text, edge, fields, relative)
                )
                continue
            if base in crates:
                raise ValueError(
                    f"frozen manifest {manifest} sources the suite crate "
                    f"{base!r} from {url} instead of the exported sibling; "
                    "refusing to snapshot a suite whose sibling pin could be "
                    "silently ignored"
                )
            continue
        if named_source == "path":
            _require_exported_sibling_path(
                manifest,
                _toml_string(fields["path"]["value"], manifest),
                Path(export_root),
            )
            continue
        if named_source == "workspace":
            if base in crates:
                raise ValueError(
                    f"frozen manifest {manifest} inherits the suite crate "
                    f"{base!r} from a workspace dependency, whose source this "
                    "rewrite cannot see; refusing to snapshot a suite whose "
                    "sibling pin could be silently ignored"
                )
            continue
        if base in crates:
            raise ValueError(
                f"frozen manifest {manifest} declares the suite crate "
                f"{base!r} without a git or path source ({fields.get('version', {}).get('value')!r}); "
                "refusing to snapshot a suite whose sibling pin could be "
                "silently ignored"
            )
    if replacements:
        manifest.write_text(_apply_replacements(text, replacements), encoding="utf-8")
    rewritten = manifest.read_text(encoding="utf-8")
    assert_dependency_inventory(manifest, rewritten)
    rewritten_edges = {
        (_edge_table_label(edge["table"]), edge["crate"]): edge
        for edge in _frozen_dependency_edges(rewritten)
    }
    for record in records:
        edge = rewritten_edges.get((record["table"], record["crate"]))
        declared = None
        if edge is not None and "path" in edge["fields"]:
            declared = _toml_string(edge["fields"]["path"]["value"], manifest)
        if declared != record["to"]["path"]:
            raise ValueError(
                f"frozen manifest {manifest} did not rewrite {record['crate']!r} "
                f"to the exported sibling path {record['to']['path']!r} "
                f"(found {declared!r} instead)"
            )
    residual = sorted(
        edge["crate"]
        for edge in _frozen_dependency_edges(rewritten)
        if "git" in edge["fields"]
        and dependency_repository_component(
            _toml_string(edge["fields"]["git"]["value"], manifest)
        )
        is not None
    )
    if residual:
        raise ValueError(
            f"frozen manifest {manifest} still resolves {residual} from a "
            "suite repository after the rewrite"
        )
    return records


def rewrite_frozen_suite_dependencies(export_root):
    """Make a frozen suite build the sibling trees exported beside it.

    Returns the recorded rewrites, sorted; raises when a manifest names a
    suite component in a shape the rewrite does not model, so a pin that
    could be silently ignored can never produce a snapshot.
    """
    export_root = Path(export_root).resolve()
    crates = frozen_suite_crates(export_root)
    records = []
    for component in COMPONENTS:
        for manifest in _frozen_manifests(export_root / component):
            records.extend(
                rewrite_frozen_manifest_dependencies(manifest, export_root, crates)
            )
    return sorted(
        records, key=lambda record: (
            record["manifest"], record["table"], record["crate"]
        )
    )


def assert_frozen_suite_builds_exported_siblings(workspace):
    """Refuse a frozen suite whose suite edges still resolve from tags.

    A snapshot materialised before the rewrite carries no rewrite record and
    resolves its siblings from their published tags, so a
    `--component-revision` pin would select no code and the comparison would
    report the tag against itself.  Fail loudly instead of reporting that.
    """
    workspace = Path(workspace)
    export_root = workspace.parent
    if not (export_root / SUITE_REVISION_MANIFEST).is_file():
        return []
    residual = []
    for component in COMPONENTS:
        for manifest in _frozen_manifests(export_root / component):
            text = manifest.read_text(encoding="utf-8")
            assert_dependency_inventory(manifest, text)
            for edge in _frozen_dependency_edges(text):
                raw = (edge["fields"].get("git") or {}).get("value")
                if raw is None:
                    continue
                if dependency_repository_component(_toml_string(raw, manifest)) is not None:
                    residual.append(
                        f"{manifest.relative_to(export_root).as_posix()} ({edge['crate']})"
                    )
    if residual:
        raise ValueError(
            f"frozen suite {export_root} resolves suite components from git "
            "tags, so a component pin would silently select no code: "
            + ", ".join(sorted(residual))
        )
    return []


def command_snapshot(args):
    source = validate_workspace(Path(args.source), "source")
    output_root = safe_output_dir(Path(args.output) if args.output else None)
    source_root = source.parent
    pairs = list(args.component_revision)
    component_revisions = dict(pairs)
    (_ for _ in ()).throw(ValueError("duplicate component revision")) if len(component_revisions) != len(pairs) else None
    unknown = sorted(set(component_revisions) - set(COMPONENTS))
    (_ for _ in ()).throw(ValueError(f"unknown component revision overrides: {', '.join(unknown)}")) if unknown else None
    missing = next((source_root / component for component in COMPONENTS if not (source_root / component).is_dir()), None)
    (_ for _ in ()).throw(ValueError(f"suite component is missing: {missing}")) if missing is not None else None
    components = {component: snapshot_component(source_root / component, output_root / component, component_revisions.get(component, args.revision)) for component in COMPONENTS}
    validate_workspace(output_root / "netem_test", "snapshot")
    frozen_dep_rewrites = rewrite_frozen_suite_dependencies(output_root)
    manifest = {"schema": SUITE_REVISION_MANIFEST_SCHEMA, "source": str(source), "requested_revision": args.revision, "component_revision_overrides": dict(sorted(component_revisions.items())), "components": dict(sorted(components.items())), "frozen_dep_rewrites": frozen_dep_rewrites}
    (output_root / SUITE_REVISION_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output_root)
    return 0


def suite_revisions(workspace):
    """netem_test plus every COMPONENTS sibling revision, sorted keys."""
    workspace = Path(workspace).resolve()
    revisions = {}
    for component in COMPONENTS:
        if component == "netem_test":
            revisions[component] = jj_revision(workspace)
        else:
            revisions[component] = jj_revision(workspace.parent / component)
    return dict(sorted(revisions.items()))


def component_revisions(workspace):
    """Revision of every suite component for a workspace, sorted keys."""
    components = suite_revisions(workspace)
    seen = {}
    for component, identity in components.items():
        commit_id = identity["commit_id"]
        if commit_id == "unknown":
            continue
        other = seen.get(commit_id)
        if other is not None:
            raise ValueError(
                f"workspace {workspace} component {component} and "
                f"{other} "
                f"share the same commit_id {commit_id!r}"
            )
        seen[commit_id] = component
    return components


def validate_component_revisions(components, context, *, allow_unknown=False):
    (_ for _ in ()).throw(ValueError(f"{context} must contain exactly the suite components")) if not isinstance(components, dict) or set(components) != set(COMPONENTS) else None
    invalid = next(((component, components[component]) for component in COMPONENTS if not isinstance(components[component], str) or not (len(components[component]) == 40 or (allow_unknown and components[component] == "unknown"))), None)
    (_ for _ in ()).throw(ValueError(f"{context} has an invalid commit id for {invalid[0]}: {invalid[1]!r}")) if invalid else None
    return dict(sorted(components.items()))


def write_probe_source_manifest(path, executable, components, source):
    path = Path(path)
    payload = {"schema": PROBE_SOURCE_MANIFEST_SCHEMA, "executable_sha256": executable_sha256(executable), "components": validate_component_revisions(components, "probe source manifest components", allow_unknown=True), "source": source}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_probe_source_manifest(path, executable, role):
    path = Path(path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    (_ for _ in ()).throw(ValueError(f"unsupported {role} probe source manifest schema: {path}")) if payload.get("schema") != PROBE_SOURCE_MANIFEST_SCHEMA else None
    expected = executable_sha256(executable)
    (_ for _ in ()).throw(ValueError(f"{role} probe source manifest does not match executable SHA-256: {path}")) if payload.get("executable_sha256") != expected else None
    return validate_component_revisions(payload.get("components"), f"{role} probe source manifest")


def paired_execution_specs(suite_specs, pair_index):
    """Yield the counterbalanced ``(role, spec)`` execution order for one
    seed-major pair.

    Even pair indexes run baseline before candidate; odd pair indexes run
    candidate before baseline, so execution order cannot bias the roles.
    The stored rows keep their baseline/candidate role labels either way.
    """
    spec = suite_specs[pair_index]
    roles = (
        ("baseline", "candidate")
        if pair_index % 2 == 0
        else ("candidate", "baseline")
    )
    for role in roles:
        yield role, spec


def cargo_test_executable(build_log):
    """Extract the perf_probe test executable from a cargo JSON build log.

    Accepts only compiler-artifact records whose target name is
    ``perf_probe`` and whose kind contains ``test``; returns the executable
    filename of the last matching artifact, or None when absent.
    """
    found = None
    with build_log.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("reason") != "compiler-artifact":
                continue
            target = message.get("target") or {}
            if target.get("name") != "perf_probe":
                continue
            kinds = target.get("kind") or []
            if not any("test" in kind for kind in kinds):
                continue
            filenames = message.get("filenames") or []
            if filenames:
                found = filenames[0]
    return found


def executable_sha256(executable):
    digest = hashlib.sha256()
    with Path(executable).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preserve_built_probe(executable, destination=None):
    executable = Path(executable).expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"built perf_probe is not a file: {executable}")) if not executable.is_file() else None
    (_ for _ in ()).throw(ValueError(f"built perf_probe is not executable: {executable}")) if not os.access(executable, os.X_OK) else None
    digest = executable_sha256(executable)
    destination_given = destination is not None
    destination = executable.parent if destination is None else Path(destination).expanduser().resolve()
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"frozen probe destination must remain beneath {safe_root}")) if destination_given and not Path(destination).is_relative_to(safe_root) else None
    Path(destination).mkdir(parents=True, exist_ok=True)
    preserved = Path(destination) / f"{executable.name}.perf-loop-{digest}"
    (_ for _ in ()).throw(ValueError(f"preserved perf_probe does not match its content address: {preserved}")) if preserved.exists() and (not preserved.is_file() or executable_sha256(preserved) != digest) else None
    shutil.copy2(executable, preserved) if not preserved.exists() else None
    (_ for _ in ()).throw(ValueError(f"failed to preserve exact perf_probe bytes: {preserved}")) if executable_sha256(preserved) != digest else None
    (_ for _ in ()).throw(ValueError(f"preserved perf_probe is not executable: {preserved}")) if not os.access(preserved, os.X_OK) else None
    return str(preserved.resolve())


def prune_built_role_target(target_dir, frozen_executable):
    """Remove a disposable role Cargo target after verifying the frozen
    probe executable lives outside it.

    The role's frozen executable is preserved under
    ``output_root/frozen/<role>/target/<profile>/deps`` before this runs, so
    the build target holds only disposable artifacts; pruning it is refused
    when the frozen executable is (still) inside it.
    """
    target = Path(target_dir).expanduser().resolve()
    frozen = Path(frozen_executable).expanduser().resolve()
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"role target must remain beneath {safe_root}")) if not target.is_relative_to(safe_root) else None
    (_ for _ in ()).throw(ValueError("role target must use a final 'target' path component")) if target.name != "target" else None
    (_ for _ in ()).throw(ValueError(f"cannot prune the role target while the frozen perf_probe lives inside it: {frozen}")) if frozen.is_relative_to(target) else None
    if target.is_dir():
        shutil.rmtree(target)
    return target


def use_prebuilt_probe(executable, role):
    """Validate and identify an exact probe executable without rebuilding it."""
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"{role} prebuilt perf_probe is not a file: {executable}")
    if not os.access(executable, os.X_OK):
        raise ValueError(f"{role} prebuilt perf_probe is not executable: {executable}")
    return str(executable)


def effective_toolchain_pin() -> str | None:
    """The toolchain the invoking environment resolves, as a rustup pin.

    The frozen probe is compiled in a snapshot workspace (beneath the safe
    temp root) that is *outside* the source tree, so rustup's directory-walk
    does not find the source tree's `rust-toolchain.toml` there and the
    build silently falls back to the *default* toolchain - a frozen probe
    compiled with a different rustc than the in-tree gates use. Pin the
    frozen build to the toolchain the invocation site resolves (the same
    override the in-tree `cargo test` uses), so the probe bytes match what
    the source tree's own gates would produce. An explicit
    `RUSTUP_TOOLCHAIN` in the environment is honoured as-is; when rustup is
    absent or reports nothing usable, the probe is built unpinned (the
    historical behaviour), because there is no pin to carry.
    """
    explicit = os.environ.get("RUSTUP_TOOLCHAIN")
    if explicit:
        return explicit
    try:
        completed = subprocess.run(
            ["rustup", "show", "active-toolchain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    name = (completed.stdout or "").split()[0] if (completed.stdout or "").strip() else ""
    return name or None


def stream_build_command(role, workspace, build_root, build_log, *, release=True, toolchain=None):
    """Run the frozen ``cargo test --no-run`` build once for one role,
    streaming JSON diagnostics into ``build-ROLE.log``.

    The build runs in the suite component that owns the probe's code
    ([`probe_component_workspace`]), so each role compiles its own exported
    `mux` tree. ``toolchain`` is the invocation-site rustup pin captured by
    [`command_run`] (see [`effective_toolchain_pin`]); when present it is
    exported as `RUSTUP_TOOLCHAIN` for the build so the frozen probe is
    compiled with the same toolchain the source tree's gates resolve.
    """
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(build_root)
    env["RUST_WRAPPER"] = ""
    env["RUSTC_WORKSPACE_WRAPPER"] = ""
    env["RUSTC_WRAPPER"] = ""
    env["RUSTFLAGS"] = ""
    if toolchain:
        env["RUSTUP_TOOLCHAIN"] = toolchain
    command = ["cargo", "test", "-j1"]
    if release:
        command.append("--release")
    command += [
        "-p", PROBE_PACKAGE, "--test", PROBE_TARGET, "--no-run",
        "--message-format=json-render-diagnostics",
    ]
    probe_workspace = probe_component_workspace(workspace)
    with build_log.open("wb") as log:
        completed = subprocess.run(
            command, cwd=probe_workspace, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    return completed


def build_probe(workspace, role, output_root, *, release=True, target_dir=None, toolchain=None):
    """Build the frozen perf_probe for a role once, before any timed run.

    Fails when the build fails or the executable is absent, so the run
    errors instead of compiling inside a timed window; a nonexistent
    workspace also errors. ``toolchain`` (the invocation-site rustup pin,
    see [`command_run`]) is forwarded to the build so the frozen probe is
    compiled with the same toolchain the source tree's gates use.
    """
    workspace = validate_workspace(workspace, role)
    profile = "release" if release else "debug"
    build_root = safe_build_dir(target_dir, workspace.parent, profile)
    build_log = output_root / f"build-{role}.log"
    completed = stream_build_command(
        role, workspace, build_root, build_log, release=release, toolchain=toolchain
    )
    if completed.returncode != 0:
        raise ValueError(
            f"failed to build the frozen {role} perf_probe executable "
            f"(cargo exit {completed.returncode}); see {build_log}"
        )
    executable = cargo_test_executable(build_log)
    if executable is None or not Path(executable).is_file():
        raise ValueError(
            f"failed to build the frozen {role} perf_probe executable "
            f"(cargo exit {completed.returncode}); see {build_log}"
        )
    frozen_destination = output_root / "frozen" / role / "target" / profile / "deps"
    return preserve_built_probe(executable, frozen_destination)


def run_probe(
    workspace,
    seed,
    role,
    output_root,
    *,
    executable,
    release=True,
    capture_rtp=True,
    target_dir=None,
    window_seconds=30,
    link_profile="hostile",
    mss_bytes=8192,
    fec=False,
    retransmission_armor=False,
    warmup_seconds=DEFAULT_WARMUP_SECONDS,
    revision="unspecified",
    components=None,
    diagnostic_mode="1",
    subprocess_runner=subprocess.run,
    scenario="bulk",
    instream_group_fec=False,
    candidate_fec="same",
):
    """Run one role/seed probe directly from its frozen executable;
    returns a manifest row.

    The scenario selects the executable probe test; FEC, in-stream group
    FEC, and retransmission-armor are explicit runtime settings recorded in
    the run evidence, never inferred from the binary or the lane.
    """
    workspace = validate_workspace(workspace, role)
    executable = Path(executable).expanduser().resolve()
    trace_dir = output_root / f"trace-{role}-{seed}"
    temp_dir = safe_temp_dir(role, seed)
    log_path = output_root / f"{role}-{seed}.log"
    trace_dir.mkdir(parents=True, exist_ok=False)
    profile = "release" if release else "debug"
    build_root = safe_build_dir(target_dir, workspace.parent, profile)

    env = dict(os.environ)
    env["TMPDIR"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["TEMP"] = str(temp_dir)
    env["CARGO_TARGET_DIR"] = str(build_root)
    env["NETEM_PERF_TRACE_DIR"] = str(trace_dir)
    env["NETEM_PERF_TRACE_RTP"] = "1" if capture_rtp else "0"
    env["NETEM_PERF_SEED"] = str(seed)
    env["NETEM_PERF_WINDOW_SECONDS"] = str(window_seconds)
    env["NETEM_PERF_WARMUP_SECONDS"] = str(warmup_seconds)
    env["NETEM_PERF_LINK_PROFILE"] = link_profile
    env["NETEM_PERF_MSS_BYTES"] = str(mss_bytes)
    env["NETEM_PERF_FEC"] = "1" if fec else "0"
    env["NETEM_PERF_INSTREAM_GROUP_FEC"] = "1" if instream_group_fec else "0"
    env["RTP_INSTREAM_GROUP_FEC"] = "1" if instream_group_fec else "0"
    env["NETEM_PERF_SCENARIO"] = scenario
    env["RTP_RTX_DUP"] = "1" if retransmission_armor else "0"
    env["NETEM_PERF_REVISION"] = revision
    env["NETEM_PERF_DIAGNOSTIC_MODE"] = diagnostic_mode

    test = PROBE_TESTS[scenario]
    command = [str(executable), test, "--ignored", "--nocapture", "--test-threads=1"]
    with open(log_path, "wb") as log:
        completed = subprocess_runner(
            command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    components = (
        {component: identity["commit_id"]
        for component, identity in component_revisions(workspace).items()}
        if components is None
        else validate_component_revisions(
            components,
            f"{role} probe components",
            allow_unknown=True,
        )
    )
    append_trace_manifest(
        trace_dir,
        ["perf_loop_runner_exit_code", completed.returncode],
        ["perf_loop_role", role],
        ["perf_loop_profile", profile],
        ["perf_loop_components_json", json.dumps(components, sort_keys=True)],
        ["scenario", scenario],
        ["instream_group_fec", "true" if instream_group_fec else "false"],
        ["candidate_fec", candidate_fec],
    )
    row = {
        "runner_exit": int(completed.returncode),
        "role": role,
        "cargo_profile": profile,
        "components": json.dumps(components, sort_keys=True),
        "seed": str(seed),
        "link_profile": link_profile,
        "mss_bytes": str(mss_bytes),
        "fec": "true" if fec else "false",
        "retransmission_armor": "true" if retransmission_armor else "false",
        "scenario": scenario,
        "instream_group_fec": "true" if instream_group_fec else "false",
        "candidate_fec": candidate_fec,
        "warmup_seconds": str(warmup_seconds),
        "executable": str(executable),
        "trace_dir": str(trace_dir),
    }
    return row


def append_trace_manifest(trace_dir, *rows):
    manifest = trace_dir / "manifest.csv"
    with manifest.open("a", newline="", encoding="utf-8") as output:
        csv.writer(output).writerows(rows)


def write_manifest(output_root, rows):
    with (output_root / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "runner_exit",
                "role",
                "cargo_profile",
                "components",
                "seed",
                "link_profile",
                "mss_bytes",
                "fec",
                "retransmission_armor",
                "scenario",
                "instream_group_fec",
                "candidate_fec",
                "executable",
                "trace_dir",
                "warmup_seconds",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def call_compare(
    baseline_dirs,
    candidate_dirs,
    output_root,
    allowed_config_mismatches=(),
    allowed_config_fields=None,
):
    """Run the paired comparison tool; the frozen allowlist suppresses only
    the named CONFIG_KEYS that actually differ between the roles, and each
    declared config field admits only its exact signed leaf delta."""
    command = ["python3", str(Path(__file__).with_name("rtp_trace_compare.py"))]
    for label, trace_dir in baseline_dirs:
        command += ["--baseline", f"{label}={trace_dir}"]
    for label, trace_dir in candidate_dirs:
        command += ["--candidate", f"{label}={trace_dir}"]
    for key in sorted(set(allowed_config_mismatches)):
        command += ["--allow-config-mismatch", key]
    for path, delta in sorted(dict(allowed_config_fields or {}).items()):
        command += ["--allow-config-field", f"{path}={delta}"]
    command += ["--out", str(output_root)]
    # The loop enforces the evidence contract itself (it inspects the written
    # comparison.json and names the exact reason), so it asks the raw tool for
    # the artifacts on every outcome rather than short-circuiting on its exit.
    command += ["--report-only"]
    result = subprocess.run(command, capture_output=True, text=True)
    return result


def _phase_stability_summary(observations):
    absolute = [abs(item["second_minus_first_percent"]) for item in observations]
    material = [item for item in observations if item["material_phase_drift"]]
    classification = (
        "insufficient_evidence"
        if not observations
        else "unstable_phase_drift"
        if material
        else "stable"
    )
    return {
        "classification": classification,
        "valid_runs": len(observations),
        "material_run_count": len(material),
        "median_absolute_shift_percent": statistics.median(absolute) if absolute else None,
        "max_absolute_shift_percent": max(absolute) if absolute else None,
    }


def within_run_phase_analysis(comparison):
    """Classify first/second-half goodput drift inside each run's window.

    Only finite positive first-half and finite positive second-half goodput
    are accepted; the shift is the multiplicative
    ``((second - first) / first) * 100``. An exact/near-threshold shift is
    material via ``math.isclose(..., rel_tol=1e-12, abs_tol=1e-12)``. Returns
    overall plus by_role summaries, per-run observations, the threshold, and
    does_not_prove.
    """
    observations = [
        {
            "label": run.get("label"),
            "role": run.get("role"),
            "first_half_mib_per_second": first,
            "second_half_mib_per_second": second,
            "second_minus_first_percent": shift,
            "material_phase_drift": (
                abs(shift) >= MATERIAL_PHASE_DRIFT_PERCENT
                or math.isclose(
                    abs(shift),
                    MATERIAL_PHASE_DRIFT_PERCENT,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ),
        }
        for run in comparison.get("runs", [])
        for summary in (run.get("summary", {}),)
        if isinstance(summary.get("goodput_first_half_mib_per_second"), (int, float))
        and isinstance(summary.get("goodput_second_half_mib_per_second"), (int, float))
        for first in (float(summary["goodput_first_half_mib_per_second"]),)
        for second in (float(summary["goodput_second_half_mib_per_second"]),)
        if math.isfinite(first) and math.isfinite(second) and first > 0.0 and second > 0.0
        for shift in (((second - first) / first) * 100.0,)
    ]
    overall = _phase_stability_summary(observations)
    roles = {}
    for role in ("baseline", "candidate"):
        role_observations = [
            item for item in observations if item["role"] == role
        ]
        roles[role] = _phase_stability_summary(role_observations)
    return {
        "classification": overall["classification"],
        "material_threshold_percent": MATERIAL_PHASE_DRIFT_PERCENT,
        "valid_runs": overall["valid_runs"],
        "material_run_count": overall["material_run_count"],
        "median_absolute_shift_percent": overall["median_absolute_shift_percent"],
        "max_absolute_shift_percent": overall["max_absolute_shift_percent"],
        "runs": observations,
        "by_role": roles,
        "does_not_prove": (
            "A first/second-half shift exposes non-stationary goodput inside "
            "the measurement window but does_not_prove its cause; controller "
            "cycles, loss timing, scheduling, thermal state, or adjacent load "
            "may contribute."
        ),
    }


def wakes_cap_failures(comparison, cap_wakes_per_gib):
    """The wakes/GiB cap: every valid pair's per-endpoint protocol-timer
    wakes per GiB of delivered application bytes must stay under the cap. The
    deterministic controller-fat-pipe lane delivers at the link-shaped rate
    (~12 MiB/s), so wakes/GiB is a fixed ratio (~21.5k peer / 0 sender on the
    measured band); the cap is the absolute ceiling a regression that adds
    protocol-timer wakes must respect. Absent values (the event was never
    observed) stay None and never fail."""
    failures = []
    for index, pair in enumerate(comparison.get("pairs", [])):
        if not pair.get("valid"):
            continue
        for side in ("baseline", "candidate"):
            for key in (
                "sender_protocol_timer_wakes_per_gib_delivered",
                "peer_protocol_timer_wakes_per_gib_delivered",
            ):
                metric = pair.get("metrics", {}).get(key, {})
                value = metric.get(side)
                if value is None:
                    continue
                if value > cap_wakes_per_gib:
                    failures.append(
                        f"controller-fat-pipe wakes/GiB cap: pair {index} "
                        f"{side} {key}={value:.0f} wakes/GiB exceeds the "
                        f"{cap_wakes_per_gib:.0f} wakes/GiB cap "
                        "(--fail-on-wakes-cap): protocol-timer wakeups on the "
                        "deterministic lane must stay bounded per delivered "
                        "byte"
                    )
    return failures


def phase_drift_failure(readiness, link_profile):
    """The asserting midpoint-phase gate: None unless the capture is
    phase-unstable; otherwise a non-zero-exit error naming the property. The
    deterministic controller-fat-pipe lane has no stochastic loss or jitter,
    so first/second-half goodput moving >= 20% at the exact midpoint is a
    controller or queue-growth defect, not noise: an arm that is not `ready`
    is inconclusive, never a pass, and --fail-on-phase-drift turns that
    inconclusive phase drift into a hard failure."""
    if readiness.get("classification") != "not_ready":
        return None
    if "within_run_phase_not_stable" not in readiness.get("blocking_reasons", []):
        return None
    return (
        "controller-fat-pipe midpoint phase assertion: --link-profile "
        f"{link_profile} is not phase-stable (first/second-half goodput "
        "differs >= 20% at the exact midpoint: within_run_phase_not_stable); "
        "a deterministic lane must be phase-stable to support a verdict, so "
        "the run is inconclusive and fails rather than passing"
    )


def comparison_readiness(
    comparison, phase_analysis, order_analysis, counterbalanced_analysis
):
    """Structural readiness gate over healthy evidence, two complete
    counterbalanced AB/BA blocks, and stable within-run phase behaviour.

    Execution-order association is a caution, not a blocker. Readiness means
    the capture passed structural evidence checks; it does_not_prove
    causality, practical benefit, or the absence of an unmeasured regression.
    """
    blocking_reasons = []
    blocking_reasons.append("trace_evidence_not_healthy") if comparison.get("evidence_quality") != "healthy" else None
    blocking_reasons.append("fewer_than_two_counterbalanced_blocks") if counterbalanced_analysis.get("complete_blocks", 0) < 2 else None
    blocking_reasons.append("within_run_phase_not_stable") if phase_analysis.get("classification") != "stable" else None
    cautions = ["directional_execution_order_effect"] if order_analysis.get("directionally_confounded") else []
    return {
        "classification": "ready" if not blocking_reasons else "not_ready",
        "blocking_reasons": blocking_reasons,
        "cautions": cautions,
        "does_not_prove": (
            "Readiness means the capture passed structural evidence checks; "
            "it does_not_prove causality, practical benefit, or the absence "
            "of an unmeasured regression."
        ),
    }


def paired_result_analysis(comparison, runs, *, same_binary_control=False):
    """Derived aggregation over a completed paired run: execution order,
    within-run phase drift, counterbalanced goodput, and the readiness gate;
    control calibration only for a same-binary control run."""
    order_analysis = execution_order_analysis(comparison, runs)
    phase_analysis = within_run_phase_analysis(comparison)
    counterbalanced_analysis = counterbalanced_goodput_analysis(
        comparison, runs, order_analysis
    )
    return {
        "execution_order_analysis": order_analysis,
        "within_run_phase_analysis": phase_analysis,
        "counterbalanced_goodput_analysis": counterbalanced_analysis,
        "comparison_readiness": comparison_readiness(
            comparison, phase_analysis, order_analysis, counterbalanced_analysis
        ),
        "control_calibration": (
            control_calibration(comparison) if same_binary_control else None
        ),
    }


def checked_result_dir(result_dir):
    """Validate a preserved result directory and return it resolved."""
    result_dir = Path(result_dir).expanduser().resolve()
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"result directory must remain beneath {safe_root}")) if not result_dir.is_relative_to(safe_root) else None
    (_ for _ in ()).throw(ValueError(f"result directory is missing: {result_dir}")) if not result_dir.is_dir() else None
    return result_dir


def read_json_object(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_object_atomic(path, value):
    """Write ``value`` to ``path`` atomically via a sibling temp file."""
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def command_analyze(args):
    result_dir = checked_result_dir(args.result)
    comparison = read_json_object(result_dir / "comparison.json")
    run_json_path = result_dir / "run.json"
    run_json = read_json_object(run_json_path)
    runs = run_json.get("runs")
    (_ for _ in ()).throw(ValueError(f"expected a runs array in {run_json_path}")) if not isinstance(runs, list) else None
    analysis = paired_result_analysis(
        comparison,
        runs,
        same_binary_control=bool(run_json.get("same_binary_control")),
    )
    report = {
        "result_dir": str(result_dir),
        "evidence_quality": comparison.get("evidence_quality", "invalid"),
        "verdict": comparison.get("verdict", "insufficient_evidence"),
        **analysis,
    }
    run_json.update(analysis) if args.update_run_json else None
    write_json_object_atomic(run_json_path, run_json) if args.update_run_json else None
    print(json.dumps(report, indent=2, sort_keys=True))
    phase_failure = phase_drift_failure(
        analysis["comparison_readiness"],
        run_json.get("link_profile") or "(unknown)",
    )
    if args.fail_on_phase_drift and phase_failure is not None:
        print(f"error: {phase_failure}", file=sys.stderr)
        return 2
    return 0


def control_calibration(comparison):
    deltas = [pair["metrics"]["goodput_mib_per_second"]["delta_percent"] for pair in comparison.get("pairs", []) if pair.get("valid")]
    absolute = [abs(value) for value in deltas if value is not None]
    phase_stability = within_run_phase_analysis(comparison)
    paired_stable = len(absolute) > 0 and all(value < 10.0 for value in absolute)
    stable = paired_stable and phase_stability["classification"] != "unstable_phase_drift"
    return {"classification": "stable" if stable else "unstable", "paired_classification": "stable" if paired_stable else "unstable", "valid_pairs": len(absolute), "median_absolute_delta_percent": statistics.median(absolute) if absolute else None, "max_absolute_delta_percent": max(absolute) if absolute else None, "false_material_change_count": sum(1 for value in absolute if value >= 10.0), "within_run_phase_analysis": phase_stability}


def execution_order_analysis(comparison, runs):
    """Describe role-independent first/second-run movement within each pair.

    The paired runner alternates role order, but a directional warm-up,
    thermal, power, or adjacent-load drift can still make every second run
    faster or slower and therefore flip the apparent candidate effect with
    it. Convert each candidate-minus-baseline delta into a
    later-minus-earlier delta so that confounding is explicit.
    """
    positions = {
        (run["role"], int(run["seed"])): index
        for index, run in enumerate(runs)
    }
    ordered_seeds = list(dict.fromkeys(int(run["seed"]) for run in runs))
    observations = []
    for pair_position, pair in enumerate(comparison.get("pairs", [])):
        if not pair.get("valid"):
            continue
        delta = (
            pair.get("metrics", {})
            .get("goodput_mib_per_second", {})
            .get("delta_percent")
        )
        if delta is None:
            continue
        try:
            seed = int(str(pair["baseline"]).rsplit("-", 1)[1])
        except (KeyError, TypeError, ValueError):
            try:
                seed = ordered_seeds[int(pair.get("index", pair_position))]
            except (IndexError, TypeError, ValueError):
                continue
        try:
            baseline_position = positions[("baseline", seed)]
            candidate_position = positions[("candidate", seed)]
        except KeyError:
            continue
        candidate_later = candidate_position > baseline_position
        later_delta = delta if candidate_later else -delta
        observations.append(
            {
                "pair_index": pair_position,
                "seed": seed,
                "first_role": "baseline" if candidate_later else "candidate",
                "second_role": "candidate" if candidate_later else "baseline",
                "candidate_minus_baseline_percent": delta,
                "later_minus_earlier_percent": later_delta,
            }
        )

    later_deltas = [item["later_minus_earlier_percent"] for item in observations]
    if len(later_deltas) < 2:
        classification = "insufficient_evidence"
        directionally_confounded = False
    elif all(value > 0.0 for value in later_deltas):
        classification = "consistent_later_faster"
        directionally_confounded = True
    elif all(value < 0.0 for value in later_deltas):
        classification = "consistent_later_slower"
        directionally_confounded = True
    else:
        classification = "mixed_order_effect"
        directionally_confounded = False

    return {
        "classification": classification,
        "directionally_confounded": directionally_confounded,
        "valid_pairs": len(later_deltas),
        "median_later_minus_earlier_percent": (
            statistics.median(later_deltas) if later_deltas else None
        ),
        "max_absolute_later_effect_percent": (
            max(abs(value) for value in later_deltas) if later_deltas else None
        ),
        "material_pair_count": sum(abs(value) >= 10.0 for value in later_deltas),
        "pairs": observations,
        "does_not_prove": (
            "A directional association with execution order does not prove its "
            "cause or the absence of a candidate effect; warm-up, thermal or "
            "power state, scheduling, and adjacent load can all move later runs. "
        ),
    }


def counterbalanced_goodput_analysis(comparison, runs, order_analysis=None):
    """Decompose adjacent AB/BA blocks into role and position effects.

    Goodput effects are multiplicative, so candidate/baseline ratios are
    transformed to log space. In one adjacent counterbalanced block the
    candidate runs second once and first once; averaging the two log ratios
    estimates the candidate role effect, while their signed half-difference
    estimates a stable later-run effect. Invalid, incomplete, and
    non-counterbalanced blocks are never silently combined.
    """
    if order_analysis is None:
        order_analysis = execution_order_analysis(comparison, runs)
    observations = {
        item["pair_index"]: item
        for item in order_analysis.get("pairs", [])
    }
    total_pairs = len(comparison.get("pairs", []))
    blocks = []
    excluded_pair_indexes = []
    for start in range(0, total_pairs, 2):
        indexes = (start, start + 1)
        if indexes[1] >= total_pairs:
            excluded_pair_indexes.append(indexes[0])
            continue
        try:
            pair = [observations[index] for index in indexes]
        except KeyError:
            excluded_pair_indexes.extend(indexes)
            continue
        if {item["first_role"] for item in pair} != {"baseline", "candidate"}:
            excluded_pair_indexes.extend(indexes)
            continue
        ratios = []
        valid = True
        for item in pair:
            ratio = 1.0 + item["candidate_minus_baseline_percent"] / 100.0
            if not math.isfinite(ratio) or ratio <= 0.0:
                valid = False
                break
            ratios.append(math.log(ratio))
        if not valid:
            excluded_pair_indexes.extend(indexes)
            continue
        role_log = statistics.fmean(ratios)
        position_logs = [
            log_ratio if item["second_role"] == "candidate" else -log_ratio
            for item, log_ratio in zip(pair, ratios)
        ]
        position_log = statistics.fmean(position_logs)
        blocks.append(
            {
                "pair_indexes": list(indexes),
                "seeds": [item["seed"] for item in pair],
                "candidate_role_effect_percent": math.expm1(role_log) * 100.0,
                "later_position_effect_percent": math.expm1(position_log) * 100.0,
            }
        )

    effects = [block["candidate_role_effect_percent"] for block in blocks]
    if len(effects) < 2:
        classification = "insufficient_evidence"
    elif all(effect >= 10.0 for effect in effects):
        classification = "consistent_material_improvement"
    elif all(effect <= -10.0 for effect in effects):
        classification = "consistent_material_regression"
    elif all(abs(effect) < 10.0 for effect in effects):
        classification = "no_material_role_effect"
    else:
        classification = "mixed_role_effect"

    aggregate_log = None
    if effects:
        aggregate_log = statistics.fmean(
            math.log1p(effect / 100.0) for effect in effects
        )
    position_effects = [block["later_position_effect_percent"] for block in blocks]
    return {
        "classification": classification,
        "complete_blocks": len(blocks),
        "included_pairs": len(blocks) * 2,
        "excluded_pair_indexes": sorted(set(excluded_pair_indexes)),
        "aggregate_candidate_role_effect_percent": (
            math.expm1(aggregate_log) * 100.0
            if aggregate_log is not None
            else None
        ),
        "median_block_role_effect_percent": (
            statistics.median(effects) if effects else None
        ),
        "median_block_position_effect_percent": (
            statistics.median(position_effects) if position_effects else None
        ),
        "material_improvement_blocks": sum(effect >= 10.0 for effect in effects),
        "material_regression_blocks": sum(effect <= -10.0 for effect in effects),
        "blocks": blocks,
        "does_not_prove": (
            "The AB/BA log-ratio decomposition cancels only a stable multiplicative "
            "first/second-run effect within each adjacent block. It does not prove "
            "causality or remove nonlinear drift, stochastic path divergence, "
            "candidate/order interactions, or unrelated machine load. "
        ),
    }


def role_fec_configuration(args, role):
    """Explicit runtime FEC setting for one role of a paired run.

    '--fec' fixes the baseline role; '--candidate-fec' selects whether the
    candidate runs same/on/off relative to that baseline. The treatment is
    only the explicit runtime FEC setting this creates, never the build.
    """
    candidate_fec = {"same": bool(args.fec), "on": True, "off": False}[args.candidate_fec]
    fec = candidate_fec if role == "candidate" else bool(args.fec)
    return {
        "fec": fec,
        "instream_group_fec": bool(args.instream_group_fec and fec),
    }


# The FEC data envelope prepended to each data packet; it shifts the
# packet-keyed netem loss key on the FEC-on arm of the paired-saturated lane.
# Must match `FEC_DATA_ENVELOPE_BYTES` in
# `tests/tests/support/presets.rs::fec_paired_saturated_bottleneck`.
FEC_DATA_ENVELOPE_BYTES = 10

# Lanes whose netem loss is keyed to the logical RTP sequence and therefore
# shifts with the FEC envelope. The lane set only decides *whether* the
# treatment has a mechanical consequence; the consequence itself is declared
# below as a value-checked leaf delta, so an undeclared netem change on these
# lanes is still a mismatch.
FEC_PACKET_KEYED_LANES = frozenset(
    {"fec-paired-saturated", "fec-paired-saturated-bottleneck"}
)


def treatment_config_differences(args):
    """CONFIG_KEYS that actually differ between the treatment roles.

    Only explicit runtime FEC settings may differ in a same-workspace
    treatment; the allowlist never admits a key whose values match.
    """
    baseline = role_fec_configuration(args, "baseline")
    candidate = role_fec_configuration(args, "candidate")
    return tuple(
        key
        for key in ("fec", "instream_group_fec")
        if baseline[key] != candidate[key]
    )


def treatment_config_field_differences(args):
    """Declared netem leaf differences the FEC treatment mechanically shifts.

    The runtime FEC envelope prepends bytes to the data packet, so a
    packet-keyed loss model on the paired-saturated lanes must move its key
    offset by exactly the envelope size on the FEC-on arm. Only that exact
    signed delta on the exact leaf is declared; every other netem leaf remains
    an undeclared mismatch naming its field. The declaration tracks the actual
    role FEC flags, so a treatment that does not move FEC declares nothing.
    """
    if args.link_profile not in FEC_PACKET_KEYED_LANES:
        return {}
    baseline = role_fec_configuration(args, "baseline")
    candidate = role_fec_configuration(args, "candidate")
    moved = int(bool(candidate["fec"])) - int(bool(baseline["fec"]))
    if moved == 0:
        return {}
    delta = moved * FEC_DATA_ENVELOPE_BYTES
    return {
        "netem_c2s.loss_model.key_offset": delta,
        "netem_s2c.loss_model.key_offset": delta,
    }


def command_run(args):
    if args.mss_bytes <= 0:
        raise argparse.ArgumentTypeError("-mss-bytes must be positive")
    baseline = validate_workspace(Path(args.baseline), "baseline")
    candidate = validate_workspace(Path(args.candidate), "candidate")
    # A frozen suite is only a pin if its siblings resolve to the exported
    # trees; a suite whose sibling edges still point at published tags would
    # build the tag for both roles and report it against itself.
    assert_frozen_suite_builds_exported_siblings(baseline)
    assert_frozen_suite_builds_exported_siblings(candidate)
    treatment = bool(args.same_workspace_treatment)
    # Pin the frozen probe builds to the toolchain the invocation site
    # resolves (see [`effective_toolchain_pin`]): the snapshot workspaces
    # live outside the source tree, where rustup's directory-walk would not
    # find the tree's `rust-toolchain.toml`, so without the pin the frozen
    # probe could be compiled with a *different* rustc than the in-tree
    # gates use.
    toolchain = effective_toolchain_pin()
    role_configuration = {
        role: role_fec_configuration(args, role)
        for role in ("baseline", "candidate")
    }
    if treatment:
        if args.same_binary_control:
            raise SystemExit(
                "--same-workspace-treatment cannot carry a treatment on "
                "--same-binary-control; a same-binary control calibrates "
                "variance only"
            )
        if baseline != candidate:
            raise SystemExit(
                "--same-workspace-treatment requires identical resolved workspaces"
            )
        if any((args.baseline_executable, args.candidate_executable)):
            raise SystemExit(
                "--same-workspace-treatment builds and freezes one executable; "
                "prebuilt role executables would break the single-binary contract"
            )
        mismatches = treatment_config_differences(args)
        field_mismatches = treatment_config_field_differences(args)
        if not mismatches:
            raise SystemExit(
                "--same-workspace-treatment requires an explicit role difference; "
                "use --candidate-fec on|off so only the runtime FEC setting "
                "actually differs"
            )
    else:
        mismatches = ()
        field_mismatches = {}
        if args.candidate_fec != "same":
            raise SystemExit(
                "--candidate-fec on|off requires --same-workspace-treatment; a "
                "runtime FEC difference across distinct workspaces or binaries "
                "would confound the treatment"
            )
        if args.same_binary_control:
            if baseline != candidate:
                raise SystemExit(
                    "--same-binary-control requires identical resolved workspaces"
                )
        elif baseline == candidate:
            raise SystemExit("refusing to compare a workspace with itself")
    if args.label and args.label in (".", ".."):
        raise SystemExit("label must name a run")
    prebuilt = {
        "baseline": args.baseline_executable,
        "candidate": args.candidate_executable,
    }
    if any(prebuilt.values()) and not all(prebuilt.values()):
        raise ValueError(
            "--baseline-executable and --candidate-executable must be supplied together"
        )
    source_manifests = {
        "baseline": args.baseline_source_manifest,
        "candidate": args.candidate_source_manifest,
    }
    if any(source_manifests.values()):
        if not all(prebuilt.values()):
            raise ValueError(
                "--baseline-source-manifest and --candidate-source-manifest require "
                "--baseline-executable and --candidate-executable"
            )
        if not all(source_manifests.values()):
            raise ValueError(
                "--baseline-source-manifest and --candidate-source-manifest "
                "must be supplied together"
            )
    output_root = safe_output_dir(args.output)
    revisions = {
        "baseline": jj_revision(baseline),
        "candidate": jj_revision(candidate),
    }
    workspace_components = {
        role: {
            component: identity["commit_id"]
            for component, identity in component_revisions(workspace).items()
        }
        for role, workspace in (("baseline", baseline), ("candidate", candidate))
    }
    source_manifest_paths = {}
    build_logs = {}
    if all(prebuilt.values()):
        executables = {
            role: use_prebuilt_probe(prebuilt[role], role)
            for role in ("baseline", "candidate")
        }
        build_sources = {role: "prebuilt" for role in executables}
        build_logs = {role: None for role in executables}
        if all(source_manifests.values()):
            for role in ("baseline", "candidate"):
                manifest_path = Path(source_manifests[role]).expanduser().resolve()
                workspace_components[role] = load_probe_source_manifest(
                    manifest_path,
                    executables[role],
                    role,
                )
                source_manifest_paths[role] = manifest_path
        else:
            source_manifest_paths = {role: None for role in executables}
    elif treatment:
        # Build and freeze ONE exact executable before any timed run; both
        # roles reuse those same bytes so only the explicit runtime FEC
        # treatment differs. Never compile baseline and candidate separately.
        executable = build_probe(
            baseline,
            "treatment",
            output_root,
            release=args.release,
            target_dir=args.target_dir,
            toolchain=toolchain,
        )
        executables = {"baseline": executable, "candidate": executable}
        build_sources = {role: "built" for role in executables}
        build_logs = {
            role: output_root / "build-treatment.log"
            for role in executables
        }
        for role in ("baseline", "candidate"):
            # Role-specific source manifests bound to the same executable bytes.
            source_manifest_paths[role] = write_probe_source_manifest(
                output_root / f"{role}-probe-source.json",
                executable,
                workspace_components[role],
                str(baseline),
            )
        if getattr(args, "prune_role_target", False):
            profile = "release" if args.release else "debug"
            build_root = safe_build_dir(
                args.target_dir, baseline.parent, profile
            )
            # Prune only after the frozen probe lives outside the target.
            prune_built_role_target(build_root, executables["baseline"])
    else:
        # Prebuild both frozen executables once, before any timed run.
        executables = {}
        for role, workspace in (("baseline", baseline), ("candidate", candidate)):
            executables[role] = build_probe(
                workspace,
                role,
                output_root,
                release=args.release,
                target_dir=args.target_dir,
                toolchain=toolchain,
            )
        build_sources = {role: "built" for role in executables}
        build_logs = {
            role: output_root / f"build-{role}.log"
            for role in executables
        }
        for role, workspace in (("baseline", baseline), ("candidate", candidate)):
            source_manifest_paths[role] = write_probe_source_manifest(
                output_root / f"{role}-probe-source.json",
                executables[role],
                workspace_components[role],
                str(workspace),
            )
        if getattr(args, "prune_role_target", False):
            profile = "release" if args.release else "debug"
            for role, workspace in (("baseline", baseline), ("candidate", candidate)):
                build_root = safe_build_dir(
                    args.target_dir, workspace.parent, profile
                )
                prune_built_role_target(build_root, executables[role])
    executable_hashes = {
        role: executable_sha256(executable)
        for role, executable in executables.items()
    }
    if treatment and executable_hashes["baseline"] != executable_hashes["candidate"]:
        raise SystemExit(
            "--same-workspace-treatment requires executables with identical contents"
        )
    if args.same_binary_control and (
        executable_hashes["baseline"] != executable_hashes["candidate"]
    ):
        raise SystemExit(
            "--same-binary-control requires executables with identical contents"
        )
    rows = []
    runs = []
    suite_specs = [{"seed": seed} for seed in args.seeds]
    for pair_index in range(len(suite_specs)):
        for role, spec in paired_execution_specs(suite_specs, pair_index):
            seed = spec["seed"]
            workspace = baseline if role == "baseline" else candidate
            row = run_probe(
                workspace,
                seed,
                role,
                output_root,
                executable=executables[role],
                release=args.release,
                capture_rtp=True,
                target_dir=args.target_dir,
                link_profile=args.link_profile,
                mss_bytes=args.mss_bytes,
                fec=role_configuration[role]["fec"],
                retransmission_armor=args.retransmission_armor,
                scenario=args.scenario,
                instream_group_fec=role_configuration[role]["instream_group_fec"],
                candidate_fec=args.candidate_fec,
                window_seconds=args.window_seconds,
                warmup_seconds=args.warmup_seconds,
                revision=revisions[role]["commit_id"],
                components=workspace_components[role],
            )
            rows.append(row)
            runs.append(
                {
                    "role": role,
                    "seed": seed,
                    "runner_exit": row["runner_exit"],
                    "executable": row["executable"],
                    "trace_dir": row["trace_dir"],
                    "fec": row["fec"],
                    "scenario": row["scenario"],
                    "log": str(output_root / f"{role}-{seed}.log"),
                }
            )
    write_manifest(output_root, rows)

    baseline_dirs = [
        (f"base-{row['seed']}", row["trace_dir"])
        for row in rows
        if row["role"] == "baseline"
    ]
    candidate_dirs = [
        (f"cand-{row['seed']}", row["trace_dir"])
        for row in rows
        if row["role"] == "candidate"
    ]
    compare = call_compare(
        baseline_dirs,
        candidate_dirs,
        output_root,
        allowed_config_mismatches=mismatches,
        allowed_config_fields=field_mismatches,
    )
    comparison_path = output_root / "comparison.json"
    comparison = {}
    if comparison_path.exists():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    verdict = comparison.get("verdict", "insufficient_evidence")
    analysis = paired_result_analysis(
        comparison,
        runs,
        same_binary_control=bool(args.same_binary_control),
    )
    phase_analysis = analysis["within_run_phase_analysis"]
    calibration = analysis["control_calibration"]
    order_analysis = analysis["execution_order_analysis"]
    counterbalanced_analysis = analysis["counterbalanced_goodput_analysis"]
    readiness = analysis["comparison_readiness"]
    build_components = {}
    components_sources = {}
    for role, workspace in (("baseline", baseline), ("candidate", candidate)):
        build_components[role] = workspace_components[role]
        if source_manifests[role] is not None:
            components_sources[role] = "probe_source_manifest"
        else:
            components_sources[role] = (
                "build_workspace"
                if build_sources[role] == "built"
                else "execution_workspace_fallback"
            )
    run_json = {
        "pair_execution_order": "alternating",
        "execution_order_analysis": order_analysis,
        "counterbalanced_goodput_analysis": counterbalanced_analysis,
        "within_run_phase_analysis": phase_analysis,
        "comparison_readiness": readiness,
        "link_profile": args.link_profile,
        "link_role": lane_classification(args.link_profile),
        "mss_bytes": args.mss_bytes,
        "fec": bool(args.fec),
        "candidate_fec": args.candidate_fec,
        "instream_group_fec": bool(args.instream_group_fec),
        "role_configuration": role_configuration,
        "retransmission_armor": bool(args.retransmission_armor),
        "scenario": args.scenario,
        "same_workspace_treatment": bool(args.same_workspace_treatment),
        "allowed_config_mismatches": sorted(mismatches),
        "allowed_config_fields": [
            f"{path}={delta}"
            for path, delta in sorted(field_mismatches.items())
        ],
        "baseline": str(baseline),
        "candidate": str(candidate),
        "label": args.label,
        "seeds": list(args.seeds),
        "window_seconds": args.window_seconds,
        "warmup_seconds": args.warmup_seconds,
        "release": args.release,
        "toolchain_pin": toolchain,
        "same_binary_control": bool(args.same_binary_control),
        "control_calibration": calibration,
        "builds": {
            "baseline": {
                "executable": executables["baseline"],
                "sha256": executable_hashes["baseline"],
                "source": build_sources["baseline"],
                "components_source": components_sources["baseline"],
                "components": build_components["baseline"],
                "source_manifest": (
                    str(source_manifest_paths["baseline"])
                    if source_manifest_paths.get("baseline") is not None
                    else None
                ),
                "log": (
                    str(build_logs["baseline"])
                    if build_sources["baseline"] == "built"
                    else None
                ),
            },
            "candidate": {
                "executable": executables["candidate"],
                "sha256": executable_hashes["candidate"],
                "source": build_sources["candidate"],
                "components_source": components_sources["candidate"],
                "components": build_components["candidate"],
                "source_manifest": (
                    str(source_manifest_paths["candidate"])
                    if source_manifest_paths.get("candidate") is not None
                    else None
                ),
                "log": (
                    str(build_logs["candidate"])
                    if build_sources["candidate"] == "built"
                    else None
                ),
            },
        },
        "target_dirs": {
            "baseline": str(safe_build_dir(args.target_dir, baseline.parent, "release" if args.release else "debug")),
            "candidate": str(safe_build_dir(args.target_dir, candidate.parent, "release" if args.release else "debug")),
        },
        "runs": runs,
        "artifacts": {
            "manifest": str(output_root / "manifest.csv"),
            "comparison_json": str(output_root / "comparison.json"),
            "comparison_html": str(output_root / "comparison.html"),
            "output_root": str(output_root),
        },
        "verdict": verdict,
        "evidence_quality": comparison.get("evidence_quality", "invalid"),
        "compare_exit": compare.returncode,
    }
    write_json_object_atomic(output_root / "run.json", run_json)
    probe_failed = any(row["runner_exit"] != 0 for row in rows)
    evidence_invalid = comparison.get("evidence_quality") == "invalid"
    no_valid_evidence = (
        not comparison_path.exists() or verdict == "insufficient_evidence"
    )
    if (
        probe_failed
        or evidence_invalid
        or compare.returncode != 0
        or no_valid_evidence
    ):
        reasons = []
        if probe_failed:
            reasons.append("a probe exited non-zero")
        if compare.returncode != 0:
            reasons.append(f"the comparison tool exited {compare.returncode}")
        if not comparison_path.exists():
            reasons.append("the comparison wrote no comparison.json")
        elif evidence_invalid:
            reasons.append("the comparison evidence is invalid")
        elif verdict == "insufficient_evidence":
            reasons.append(
                "no valid paired evidence remains (verdict "
                "insufficient_evidence)"
            )
        print(
            "error: refusing to report success: " + "; ".join(reasons),
            file=sys.stderr,
        )
        return 2
    if args.fail_on_regression and verdict == "likely_regression":
        return 3
    phase_failure = phase_drift_failure(readiness, args.link_profile)
    if args.fail_on_phase_drift and phase_failure is not None:
        print(f"error: {phase_failure}", file=sys.stderr)
        return 2
    wakes_cap = getattr(args, "fail_on_wakes_cap", None)
    if wakes_cap is not None:
        for failure in wakes_cap_failures(comparison, wakes_cap):
            print(f"error: {failure}", file=sys.stderr)
            return 2
    if (
        args.fail_on_control_instability
        and calibration is not None
        and calibration["classification"] == "unstable"
    ):
        return 4
    return 0


def command_compare(args):
    if args.mss_bytes <= 0:
        raise argparse.ArgumentTypeError("--mss-bytes must be positive")
    output_root = safe_output_dir(args.output)
    baseline_dirs = [(f"base-{seed}", trace) for seed, trace in args.baseline]
    candidate_dirs = [(f"cand-{seed}", trace) for seed, trace in args.candidate]
    result = call_compare(baseline_dirs, candidate_dirs, output_root)
    comparison_path = output_root / "comparison.json"
    comparison = {}
    if comparison_path.exists():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    insufficient = (
        comparison.get("verdict", "insufficient_evidence") == "insufficient_evidence"
    )
    if (
        result.returncode != 0
        or not comparison_path.exists()
        or comparison.get("evidence_quality") == "invalid"
        or insufficient
    ):
        reasons = []
        if result.returncode != 0:
            reasons.append(f"the comparison tool exited {result.returncode}")
        if not comparison_path.exists():
            reasons.append("the comparison wrote no comparison.json")
        elif comparison.get("evidence_quality") == "invalid":
            reasons.append("the comparison evidence is invalid")
        elif insufficient:
            reasons.append(
                "no valid paired evidence remains (verdict "
                "insufficient_evidence)"
            )
        print(
            "error: refusing to report success: " + "; ".join(reasons),
            file=sys.stderr,
        )
        return 2
    if args.fail_on_regression and comparison.get("verdict") == "likely_regression":
        return 3
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="perf-loop",
        description="Paired performance capture loop for netem_test workspaces.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="freeze a complete suite at exact committed revisions",
    )
    snapshot.add_argument(
        "--source", required=True, help="source netem_test workspace"
    )
    snapshot.add_argument(
        "--source-revision",
        dest="revision",
        default="-",
        help="jj revision resolved independently in every component (default: -)",
    )
    snapshot.add_argument(
        "--component-revision",
        action="append",
        default=[],
        type=parse_component_revision,
        metavar="COMPONENT=REVISION",
        help="override one component's jj revision (repeatable)",
    )
    snapshot.add_argument("--output", default=None, help="output directory beneath $TMPDIR (default: a safe-root output dir)")
    snapshot.set_defaults(handler=command_snapshot)

    analyze = subparsers.add_parser(
        "analyze", help="recompute readiness/phase/order analysis from a preserved result"
    )
    analyze.add_argument(
        "--result",
        required=True,
        help="preserved result directory beneath $TMPDIR containing run.json and comparison.json",
    )
    analyze.add_argument(
        "--update-run-json",
        action="store_true",
        help="write the recomputed analysis back into the preserved run.json",
    )
    analyze.add_argument(
        "--fail-on-phase-drift",
        action="store_true",
        default=False,
        help="exit non-zero when the within-run midpoint phase analysis is "
        "unstable (first/second-half goodput differs >= 20% at the exact "
        "midpoint); the asserting phase gate for the deterministic "
        "controller-fat-pipe lane",
    )
    analyze.set_defaults(handler=command_analyze)

    run = subparsers.add_parser("run", help="run a paired baseline/candidate capture")
    run.add_argument("--baseline", required=True, help="baseline netem_test workspace")
    run.add_argument("--candidate", required=True, help="candidate netem_test workspace")
    run.add_argument("--label", type=parse_label, default=None, help="run label")
    run.add_argument(
        "--seeds",
        type=parse_seeds,
        default=DEFAULT_SEEDS,
        help="comma-separated u64 seeds (default: %(default)s)",
    )
    run.add_argument(
        "--window-seconds", type=int, default=30,
        help="measurement window in seconds (must be positive)",
    )
    run.add_argument(
        "--warmup-seconds",
        type=parse_nonnegative_seconds,
        default=DEFAULT_WARMUP_SECONDS,
        help="unmeasured per-run steady-state warmup (default: %(default)s)",
    )
    run.add_argument("--link-profile", choices=LINK_PROFILES, default="hostile")
    run.add_argument("--mss-bytes", type=int, default=8192)
    run.add_argument(
        "--fec",
        action="store_true",
        default=False,
        help="enable FEC for every probe (NETEM_PERF_FEC=1)",
    )
    run.add_argument(
        "--candidate-fec",
        choices=("same", "on", "off"),
        default="same",
        help=(
            "candidate FEC treatment relative to --fec; on|off require "
            "--same-workspace-treatment so only the runtime FEC setting differs"
        ),
    )
    run.add_argument(
        "--instream-group-fec",
        action="store_true",
        default=False,
        help="enable in-stream group FEC for every probe (NETEM_PERF_INSTREAM_GROUP_FEC=1)",
    )
    run.add_argument(
        "--scenario",
        choices=("bulk", "message-latency"),
        default="bulk",
        help="probe scenario: bulk goodput or sparse-message latency",
    )
    run.add_argument(
        "--same-workspace-treatment",
        action="store_true",
        default=False,
        help=(
            "compare one workspace with itself using one frozen executable; "
            "only the explicit runtime FEC treatment may differ"
        ),
    )
    run.add_argument(
        "--retransmission-armor",
        dest="retransmission_armor",
        action="store_true",
        default=False,
        help="enable retransmission-armor duplicate protection for every probe (RTP_RTX_DUP=1)",
    )
    run.add_argument("--release", action="store_true", default=True, help=argparse.SUPPRESS)
    run.add_argument("--no-release", dest="release", action="store_false")
    run.add_argument(
        "--target-dir", type=Path, default=None,
        help="shared Cargo target directory",
    )
    run.add_argument(
        "--prune-role-target",
        action="store_true",
        default=False,
        help=(
            "prune each role's disposable Cargo target after freezing its "
            "probe executable (the frozen executable must live outside the "
            "target)"
        ),
    )
    run.add_argument(
        "--baseline-executable",
        default=None,
        help="exact prebuilt baseline perf_probe; requires --candidate-executable",
    )
    run.add_argument(
        "--candidate-executable",
        default=None,
        help="exact prebuilt candidate perf_probe; requires --baseline-executable",
    )
    run.add_argument(
        "--baseline-source-manifest",
        type=Path,
        default=None,
        help="probe source manifest for the prebuilt baseline; requires --candidate-source-manifest",
    )
    run.add_argument(
        "--candidate-source-manifest",
        type=Path,
        default=None,
        help="probe source manifest for the prebuilt candidate; requires --baseline-source-manifest",
    )
    run.add_argument("--output", type=Path, default=None, help="output directory beneath $TMPDIR")
    run.add_argument("--fail-on-regression", action="store_true")
    run.add_argument(
        "--fail-on-wakes-cap",
        type=float,
        default=None,
        metavar="WAKES_PER_GIB",
        help="exit 2 when any valid pair's per-endpoint protocol-timer wakes "
        "per GiB of delivered bytes exceeds WAKES_PER_GIB; the absolute "
        "wakeup ceiling for the deterministic controller-fat-pipe lane",
    )
    run.add_argument(
        "--fail-on-phase-drift",
        action="store_true",
        default=False,
        help="exit 2 when the within-run midpoint phase analysis is "
        "unstable (within_run_phase_not_stable: first/second-half goodput "
        "differs >= 20% at the exact midpoint); the asserting phase gate for "
        "the deterministic controller-fat-pipe lane",
    )
    run.add_argument(
        "--same-binary-control", action="store_true",
        help="compare a workspace with itself to calibrate run-to-run variance",
    )
    run.add_argument(
        "--fail-on-control-instability", action="store_true",
        help="exit 4 when same-binary control calibration is unstable",
    )
    run.set_defaults(handler=command_run)

    compare = subparsers.add_parser("compare", help="compare existing trace directories")
    compare.add_argument(
        "--baseline", action="append", nargs=2, metavar=("SEED", "TRACE_DIR"), required=True
    )
    compare.add_argument(
        "--candidate", action="append", nargs=2, metavar=("SEED", "TRACE_DIR"), required=True
    )
    compare.add_argument("--link-profile", choices=LINK_PROFILES, default="hostile")
    compare.add_argument("--mss-bytes", type=int, default=8192)
    compare.add_argument("--output", type=Path, default=None)
    compare.add_argument("--fail-on-regression", action="store_true")
    compare.set_defaults(handler=command_compare)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "mss_bytes", 1) <= 0:
        parser.error("--mss-bytes must be positive")
    try:
        return args.handler(args)
    except SystemExit as error:
        # error codes must be positive
        raise
    except (ValueError, argparse.ArgumentTypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
