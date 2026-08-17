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
import shutil
import statistics
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
SAFE_TEMP_ROOT = Path.home() / "code" / "tmp"
DEFAULT_SEEDS = (11, 21)
DEFAULT_WARMUP_SECONDS = 5.0
PERF_TEST = "probe_hostile_goodput_30s"
LINK_PROFILES = (
    "hostile",
    "lossy-400kib",
    "hostile-fat-pipe",
    "controller-fat-pipe",
    "clean",
    "direct",
)
COMPONENTS = ("netem_test", "rtp", "mux", "rtp_mux", "tokio_udp", "udp_listener")
SUITE_REVISION_MANIFEST = "suite-revisions.json"


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


def safe_build_dir(requested, workspace, profile):
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    if requested is None:
        identity = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
        requested = safe_root / "net-perf-targets" / f"{identity}-{profile}"
    resolved = Path(requested).expanduser().resolve()
    if not resolved.is_relative_to(safe_root):
        raise ValueError(f"Cargo target directory must remain beneath {safe_root}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def safe_temp_dir(role, seed):
    """A unique per-probe temporary directory beneath $TMPDIR."""
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    requested = safe_root / f"net-perf-tmp-{stamp}-{os.getpid()}-{role}-{seed}"
    resolved = requested.expanduser().resolve()
    if not resolved.is_relative_to(safe_root):
        raise ValueError(f"probe temporary directory must remain beneath {safe_root}")
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
    if manifest.get("schema") != 1 or not isinstance(components, dict):
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


def jj_revision(directory):
    """Current jj commit id of a workspace or sibling component repo."""
    directory = Path(directory)
    if not directory.is_dir():
        return "unknown"
    frozen = frozen_suite_manifest(directory)
    if frozen is not None and directory.name in frozen["components"]:
        return frozen["components"][directory.name]["commit_id"]
    try:
        result = subprocess.run(
            ["jj", "--no-pager", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


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


def command_snapshot(args):
    """Freeze a complete sibling-component suite at exact committed revisions."""
    source = validate_workspace(Path(args.source), "source")
    output_root = safe_output_dir(Path(args.output) if args.output else None)
    source_root = source.parent
    components = {}
    for component in COMPONENTS:
        component_source = source_root / component
        if not component_source.is_dir():
            raise ValueError(f"suite component is missing: {component_source}")
        components[component] = snapshot_component(
            component_source,
            output_root / component,
            args.revision,
        )
    validate_workspace(output_root / "netem_test", "snapshot")
    manifest = {
        "schema": 1,
        "source": str(source),
        "requested_revision": args.revision,
        "components": dict(sorted(components.items())),
    }
    (output_root / SUITE_REVISION_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
    return suite_revisions(workspace)


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


def preserve_built_probe(executable):
    """Copy a Cargo-built probe to an immutable content-addressed sibling.

    Cargo can reuse the same artifact path when baseline and candidate builds
    share a target directory. Preserve each artifact immediately, before the
    next role builds, so later compilation cannot replace the bytes that a
    timed run is supposed to execute. Keeping the copy beside Cargo's output
    preserves the host execution policy that applies to that directory.
    """
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"built perf_probe is not a file: {executable}")
    if not os.access(executable, os.X_OK):
        raise ValueError(f"built perf_probe is not executable: {executable}")

    digest = executable_sha256(executable)
    preserved = executable.with_name(
        f"{executable.name}.perf-loop-{digest}"
    )
    if preserved.exists():
        if not preserved.is_file() or executable_sha256(preserved) != digest:
            raise ValueError(
                f"preserved perf_probe does not match its content address: {preserved}"
            )
    else:
        shutil.copy2(executable, preserved)

    if executable_sha256(preserved) != digest:
        raise ValueError(f"failed to preserve exact perf_probe bytes: {preserved}")
    if not os.access(preserved, os.X_OK):
        raise ValueError(f"preserved perf_probe is not executable: {preserved}")
    return str(preserved.resolve())


def use_prebuilt_probe(executable, role):
    """Validate and identify an exact probe executable without rebuilding it."""
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"{role} prebuilt perf_probe is not a file: {executable}")
    if not os.access(executable, os.X_OK):
        raise ValueError(f"{role} prebuilt perf_probe is not executable: {executable}")
    return str(executable)


def stream_build_command(role, workspace, build_root, build_log, *, release=True):
    """Run the frozen ``cargo test --no-run`` build once for one role,
    streaming JSON diagnostics into ``build-ROLE.log``."""
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(build_root)
    command = ["cargo", "test"]
    if release:
        command.append("--release")
    command += [
        "-p", "tests", "--test", "perf_probe", "--no-run",
        "--message-format=json-render-diagnostics",
    ]
    with build_log.open("wb") as log:
        completed = subprocess.run(
            command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    return completed


def build_probe(workspace, role, output_root, *, release=True, target_dir=None):
    """Build the frozen perf_probe for a role once, before any timed run.

    Fails when the build fails or the executable is absent, so the run
    errors instead of compiling inside a timed window; a nonexistent
    workspace also errors.
    """
    workspace = validate_workspace(workspace, role)
    profile = "release" if release else "debug"
    build_root = safe_build_dir(target_dir, workspace.parent, profile)
    build_log = output_root / f"build-{role}.log"
    completed = stream_build_command(
        role, workspace, build_root, build_log, release=release
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
    return preserve_built_probe(executable)


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
    warmup_seconds=DEFAULT_WARMUP_SECONDS,
    revision="unspecified",
    diagnostic_mode="1",
    subprocess_runner=subprocess.run,
):
    """Run one role/seed probe directly from its frozen executable;
    returns a manifest row.
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
    env["NETEM_PERF_REVISION"] = revision
    env["NETEM_PERF_DIAGNOSTIC_MODE"] = diagnostic_mode

    command = [str(executable), PERF_TEST, "--ignored", "--nocapture", "--test-threads=1"]
    with open(log_path, "wb") as log:
        completed = subprocess_runner(
            command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    components = component_revisions(workspace)
    append_trace_manifest(
        trace_dir,
        ["perf_loop_runner_exit_code", completed.returncode],
        ["perf_loop_role", role],
        ["perf_loop_profile", profile],
        ["perf_loop_components_json", json.dumps(components, sort_keys=True)],
    )
    row = {
        "runner_exit": int(completed.returncode),
        "role": role,
        "cargo_profile": profile,
        "components": json.dumps(components, sort_keys=True),
        "seed": str(seed),
        "link_profile": link_profile,
        "mss_bytes": str(mss_bytes),
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
                "executable",
                "trace_dir",
                "warmup_seconds",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def call_compare(baseline_dirs, candidate_dirs, output_root):
    command = ["python3", str(Path(__file__).with_name("rtp_trace_compare.py"))]
    for label, trace_dir in baseline_dirs:
        command += ["--baseline", f"{label}={trace_dir}"]
    for label, trace_dir in candidate_dirs:
        command += ["--candidate", f"{label}={trace_dir}"]
    command += ["--out", str(output_root)]
    result = subprocess.run(command, capture_output=True, text=True)
    return result


def control_calibration(comparison):
    """Classify same-binary control stability from valid paired goodput
    deltas.  Stable only when every absolute valid paired goodput delta is
    below 10%; records the median/maximum absolute delta and how many pairs
    crossed the material-change threshold (false material changes on an
    identical binary)."""
    deltas = [
        pair["metrics"]["goodput_mib_per_second"]["delta_percent"]
        for pair in comparison.get("pairs", [])
        if pair.get("valid")
    ]
    absolute = [abs(value) for value in deltas if value is not None]
    stable = len(absolute) > 0 and all(value < 10.0 for value in absolute)
    return {
        "classification": "stable" if stable else "unstable",
        "valid_pairs": len(absolute),
        "median_absolute_delta_percent": (
            statistics.median(absolute) if absolute else None
        ),
        "max_absolute_delta_percent": max(absolute) if absolute else None,
        "false_material_change_count": sum(
            1 for value in absolute if value >= 10.0
        ),
    }


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


def command_run(args):
    if args.mss_bytes <= 0:
        raise argparse.ArgumentTypeError("-mss-bytes must be positive")
    baseline = validate_workspace(Path(args.baseline), "baseline")
    candidate = validate_workspace(Path(args.candidate), "candidate")
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
    output_root = safe_output_dir(args.output)
    revisions = {
        "baseline": jj_revision(baseline),
        "candidate": jj_revision(candidate),
    }
    if all(prebuilt.values()):
        executables = {
            role: use_prebuilt_probe(prebuilt[role], role)
            for role in ("baseline", "candidate")
        }
        build_sources = {role: "prebuilt" for role in executables}
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
            )
        build_sources = {role: "built" for role in executables}
    executable_hashes = {
        role: executable_sha256(executable)
        for role, executable in executables.items()
    }
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
                window_seconds=args.window_seconds,
                warmup_seconds=args.warmup_seconds,
                revision=revisions[role],
            )
            rows.append(row)
            runs.append(
                {
                    "role": role,
                    "seed": seed,
                    "runner_exit": row["runner_exit"],
                    "executable": row["executable"],
                    "trace_dir": row["trace_dir"],
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
    compare = call_compare(baseline_dirs, candidate_dirs, output_root)
    comparison = {}
    if (output_root / "comparison.json").exists():
        comparison = json.loads((output_root / "comparison.json").read_text(encoding="utf-8"))
    verdict = comparison.get("verdict", "insufficient_evidence")
    calibration = control_calibration(comparison) if args.same_binary_control else None
    order_analysis = execution_order_analysis(comparison, runs)
    counterbalanced_analysis = counterbalanced_goodput_analysis(
        comparison, runs, order_analysis
    )
    run_json = {
        "pair_execution_order": "alternating",
        "execution_order_analysis": order_analysis,
        "counterbalanced_goodput_analysis": counterbalanced_analysis,
        "link_profile": args.link_profile,
        "mss_bytes": args.mss_bytes,
        "baseline": str(baseline),
        "candidate": str(candidate),
        "label": args.label,
        "seeds": list(args.seeds),
        "window_seconds": args.window_seconds,
        "warmup_seconds": args.warmup_seconds,
        "release": args.release,
        "same_binary_control": bool(args.same_binary_control),
        "control_calibration": calibration,
        "builds": {
            "baseline": {
                "executable": executables["baseline"],
                "sha256": executable_hashes["baseline"],
                "source": build_sources["baseline"],
                "log": (
                    str(output_root / "build-baseline.log")
                    if build_sources["baseline"] == "built"
                    else None
                ),
            },
            "candidate": {
                "executable": executables["candidate"],
                "sha256": executable_hashes["candidate"],
                "source": build_sources["candidate"],
                "log": (
                    str(output_root / "build-candidate.log")
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
    (output_root / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    probe_failed = any(row["runner_exit"] != 0 for row in rows)
    evidence_invalid = comparison.get("evidence_quality") == "invalid"
    if probe_failed or evidence_invalid or compare.returncode != 0:
        verdict = "likely_regression"
        return 2
    if args.fail_on_regression and verdict == "likely_regression":
        return 3
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
    comparison = {}
    if (output_root / "comparison.json").exists():
        comparison = json.loads((output_root / "comparison.json").read_text(encoding="utf-8"))
    if result.returncode != 0 or comparison.get("evidence_quality") == "invalid":
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
        "--revision",
        default="@-",
        help="jj revision resolved independently in every component (default: @-)",
    )
    snapshot.add_argument("--output", default=None, help="output directory beneath $TMPDIR")
    snapshot.set_defaults(handler=command_snapshot)

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
    run.add_argument("--release", action="store_true", default=True, help=argparse.SUPPRESS)
    run.add_argument("--no-release", dest="release", action="store_false")
    run.add_argument(
        "--target-dir", type=Path, default=None,
        help="shared Cargo target directory",
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
    run.add_argument("--output", type=Path, default=None, help="output directory beneath $TMPDIR")
    run.add_argument("--fail-on-regression", action="store_true")
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
