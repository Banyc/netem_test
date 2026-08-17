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
MATERIAL_PHASE_DRIFT_PERCENT = 20.0
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
    manifest = {"schema": SUITE_REVISION_MANIFEST_SCHEMA, "source": str(source), "requested_revision": args.revision, "component_revision_overrides": dict(sorted(component_revisions.items())), "components": dict(sorted(components.items()))}
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
    if components is None:
        return None
    identity = {component: commit_id for component, commit_id in components.items()}
    for component in COMPONENTS:
        if component not in identity:
            continue
        for other in COMPONENTS:
            if other == component or other not in identity:
                continue
            if identity[component] == identity[other] and component != other:
                raise ValueError(
                    f"workspace {workspace} component {component} and {other} "
                    f"share the same commit_id {identity[component]!r}"
                )
    return validate_component_revisions(components, "workspace components", allow_unknown=True)


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


def preserve_built_probe(executable):
    executable = Path(executable).expanduser().resolve()
    (_ for _ in ()).throw(ValueError(f"built perf_probe is not a file: {executable}")) if not executable.is_file() else None
    (_ for _ in ()).throw(ValueError(f"built perf_probe is not executable: {executable}")) if not os.access(executable, os.X_OK) else None
    digest = executable_sha256(executable)
    preserved = executable.with_name(f"{executable.name}.perf-loop-{digest}")
    (_ for _ in ()).throw(ValueError(f"preserved perf_probe does not match its content address: {preserved}")) if preserved.exists() and (not preserved.is_file() or executable_sha256(preserved) != digest) else None
    shutil.copy2(executable, preserved) if not preserved.exists() else None
    (_ for _ in ()).throw(ValueError(f"failed to preserve exact perf_probe bytes: {preserved}")) if executable_sha256(preserved) != digest else None
    (_ for _ in ()).throw(ValueError(f"preserved perf_probe is not executable: {preserved}")) if not os.access(preserved, os.X_OK) else None
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
    env["RUST_WRAPPER"] = ""
    env["RUSTC_WORKSPACE_WRAPPER"] = ""
    command = ["cargo", "test", "-j1"]
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


def within_run_phase_analysis(comparison):
    observations = [{"label": run.get("label"), "role": run.get("role"), "first_half_mib_per_second": first, "second_half_mib_per_second": second, "second_minus_first_percent": shift, "material_phase_drift": abs(shift) >= MATERIAL_PHASE_DRIFT_PERCENT} for run in comparison.get("runs", []) for summary in (run.get("summary", {}),) if isinstance(summary.get("goodput_first_half_mib_per_second"), (int, float)) and isinstance(summary.get("goodput_second_half_mib_per_second"), (int, float)) for first in (float(summary["goodput_first_half_mib_per_second"]),) for second in (float(summary["goodput_second_half_mib_per_second"]),) if math.isfinite(first) and math.isfinite(second) and first > 0.0 for shift in ((second / first - 1.0) * 100.0,)]; absolute = [abs(item["second_minus_first_percent"]) for item in observations]; material = [item for item in observations if item["material_phase_drift"]]; classification = "insufficient_evidence" if not observations else "unstable_phase_drift" if material else "stable"; return {"classification": classification, "material_threshold_percent": MATERIAL_PHASE_DRIFT_PERCENT, "valid_runs": len(observations), "material_run_count": len(material), "median_absolute_shift_percent": statistics.median(absolute) if absolute else None, "max_absolute_shift_percent": max(absolute) if absolute else None, "runs": observations, "does_not_prove": "A first/second-half shift exposes non-stationary goodput inside the measurement window but does_not_prove its cause; controller cycles, loss timing, scheduling, thermal state, or adjacent load may contribute."}


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
    if all(prebuilt.values()):
        executables = {
            role: use_prebuilt_probe(prebuilt[role], role)
            for role in ("baseline", "candidate")
        }
        build_sources = {role: "prebuilt" for role in executables}
        if all(source_manifests.values()):
            for role in ("baseline", "candidate"):
                source_manifest_paths[role] = load_probe_source_manifest(
                    source_manifests[role], executables[role], role
                )
        else:
            source_manifest_paths = {role: None for role in executables}
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
        for role, workspace in (("baseline", baseline), ("candidate", candidate)):
            source_manifest_paths[role] = write_probe_source_manifest(
                output_root / f"{role}-probe-source.json",
                executables[role],
                workspace_components[role],
                str(workspace),
            )
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
                revision=revisions[role]["commit_id"],
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
    phase_analysis = within_run_phase_analysis(comparison)
    calibration = control_calibration(comparison) if args.same_binary_control else None
    order_analysis = execution_order_analysis(comparison, runs)
    counterbalanced_analysis = counterbalanced_goodput_analysis(
        comparison, runs, order_analysis
    )
    build_components = {}
    components_sources = {}
    for role, workspace in (("baseline", baseline), ("candidate", candidate)):
        if source_manifest_paths.get(role) is not None:
            build_components[role] = source_manifest_paths[role]
            components_sources[role] = "probe_source_manifest"
        else:
            build_components[role] = workspace_components[role]
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
                "components_source": components_sources["baseline"],
                "components": build_components["baseline"],
                "source_manifest": (
                    str(source_manifest_paths["baseline"])
                    if source_manifest_paths.get("baseline") is not None
                    else None
                ),
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
                "components_source": components_sources["candidate"],
                "components": build_components["candidate"],
                "source_manifest": (
                    str(source_manifest_paths["candidate"])
                    if source_manifest_paths.get("candidate") is not None
                    else None
                ),
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
        "--source-revision",
        dest="revision",
        default="-",
        help="jj revision resolved independently in every component (default: -)",
    )
    snapshot.add_argument(
        "--component-revision",
        action="append",
        default=[],
        metavar="COMPONENT=REVISION",
        help="override one component's jj revision (repeatable)",
    )
    snapshot.add_argument("--output", required=True, help="output directory beneath $TMPDIR")
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
    run.add_argument(
        "--baseline-source-manifest",
        default=None,
        help="probe source manifest for the prebuilt baseline; requires --candidate-source-manifest",
    )
    run.add_argument(
        "--candidate-source-manifest",
        default=None,
        help="probe source manifest for the prebuilt candidate; requires --baseline-source-manifest",
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
