#!/usr/bin/env python3
"""One-command paired performance capture loop for netem_test workspaces.

Runs the hostile/clean goodput probe against a baseline and a candidate
frozen netem_test workspace for a set of seeds, writes every output and
temporary beneath $TMPDIR, calls rtp_trace_compare, and reports a
consistency verdict.  Diagnostic mode is always enabled so the absolute
hostile-goodput floor cannot abort evidence collection, but payload
integrity, task outcome, probe exit status, and comparison health stay
enforced.
"""

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SAFE_TEMP_ROOT = Path.home() / "code" / "tmp"
DEFAULT_SEEDS = (11, 21)
PERF_TEST = "probe_hostile_goodput_30s"
LINK_PROFILES = ("hostile", "clean", "direct")
COMPONENTS = ("netem_test", "rtp", "mux", "rtp_mux", "tokio_udp", "udp_listener")


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


def safe_output_dir(requested=None):
    safe_root = SAFE_TEMP_ROOT.expanduser().resolve()
    safe_root.mkdir(parents=True, exist_ok=True)
    if requested is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        requested = safe_root / f"net-perf-loop-{stamp}-{os.getpid()}"
    else:
        requested = Path(requested)
    resolved = requested.expanduser().resolve()
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
    resolved = requested.expanduser().resolve()
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


def jj_revision(directory):
    """Current jj commit id of a workspace or sibling component repo."""
    directory = Path(directory)
    if not directory.is_dir():
        return "unknown"
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
    """Build one role's frozen perf_probe executable once, before any timed
    run.  Errors when the build fails or the executable is absent or
    nonexistent."""
    workspace = validate_workspace(workspace, role)
    profile = "release" if release else "debug"
    build_root = safe_build_dir(target_dir, workspace.parent, profile)
    build_log = output_root / f"build-{role}.log"
    completed = stream_build_command(
        role, workspace, build_root, build_log, release=release
    )
    executable = cargo_test_executable(build_log)
    if (
        completed.returncode != 0
        or executable is None
        or not Path(executable).is_file()
    ):
        raise ValueError(
            f"failed to build the frozen {role} perf_probe executable "
            f"(cargo exit {completed.returncode}); see {build_log}"
        )
    return str(Path(executable).resolve())


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
    link_profile="hostile",
    mss_bytes=8192,
    window_seconds=30,
    revision="unspecified",
    diagnostic_mode="1",
    subprocess_runner=subprocess.run,
):
    """Run one role/seed probe directly from its frozen executable;
    returns a manifest row."""
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
    env["NETEM_PERF_LINK_PROFILE"] = link_profile
    env["NETEM_PERF_MSS_BYTES"] = str(mss_bytes)
    env["NETEM_PERF_REVISION"] = revision
    env["NETEM_PERF_DIAGNOSTIC_MODE"] = diagnostic_mode

    command = [str(executable), PERF_TEST, "--ignored", "--nocapture", "--test-threads=1"]
    with open(log_path, "wb") as log:
        completed = subprocess_runner(
            command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    components = suite_revisions(workspace)
    append_trace_manifest(
        trace_dir,
        [
            ["perf_loop_runner_exit_code", completed.returncode],
            ["perf_loop_role", role],
            ["perf_loop_profile", profile],
            ["perf_loop_components_json", json.dumps(components, sort_keys=True)],
        ],
    )
    row = {
        "runner_exit": int(completed.returncode),
        "role": role,
        "cargo_profile": profile,
        "components": json.dumps(components, sort_keys=True),
        "seed": str(seed),
        "link_profile": link_profile,
        "mss_bytes": str(mss_bytes),
        "executable": str(executable),
        "trace_dir": str(trace_dir),
    }
    return row


def append_trace_manifest(trace_dir, rows):
    manifest = trace_dir / "manifest.csv"
    with manifest.open("a", newline="", encoding="utf-8") as output:
        csv.writer(output).writerows(rows)


def write_manifest(output_root, rows):
    with (output_root / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "runner_exit", "role", "cargo_profile", "components",
                "seed", "link_profile", "mss_bytes", "executable", "trace_dir",
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


def command_run(args):
    if args.mss_bytes <= 0:
        raise argparse.ArgumentTypeError("--mss-bytes must be positive")
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
    output_root = safe_output_dir(args.output)
    revisions = {
        "baseline": jj_revision(baseline),
        "candidate": jj_revision(candidate),
    }

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
    if (
        args.same_binary_control
        and executables["baseline"] != executables["candidate"]
    ):
        raise SystemExit(
            "--same-binary-control requires identical executable paths for both roles"
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

    run_json = {
        "pair_execution_order": "alternating",
        "link_profile": args.link_profile,
        "mss_bytes": args.mss_bytes,
        "baseline": str(baseline),
        "candidate": str(candidate),
        "label": args.label,
        "seeds": list(args.seeds),
        "window_seconds": args.window_seconds,
        "release": args.release,
        "same_binary_control": bool(args.same_binary_control),
        "control_calibration": calibration,
        "builds": {
            "baseline": {
                "executable": executables["baseline"],
                "log": str(output_root / "build-baseline.log"),
            },
            "candidate": {
                "executable": executables["candidate"],
                "log": str(output_root / "build-candidate.log"),
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
    run.add_argument("--window-seconds", type=int, default=30, help="measurement window in seconds (must be positive)")
    run.add_argument("--link-profile", choices=LINK_PROFILES, default="hostile")
    run.add_argument("--mss-bytes", type=int, default=8192)
    run.add_argument("--release", action="store_true", default=True, help=argparse.SUPPRESS)
    run.add_argument("--no-release", dest="release", action="store_false")
    run.add_argument("--target-dir", default=None, help="shared Cargo target directory")
    run.add_argument("--output", default=None, help="output directory beneath $TMPDIR")
    run.add_argument("--fail-on-regression", action="store_true")
    run.add_argument("--same-binary-control", action="store_true", help="compare a workspace with itself to calibrate run-to-run variance")
    run.add_argument("--fail-on-control-instability", action="store_true", help="exit 4 when same-binary control calibration is unstable")
    run.set_defaults(handler=command_run)

    compare = subparsers.add_parser("compare", help="compare existing trace directories")
    compare.add_argument("--baseline", action="append", nargs=2, metavar=("SEED", "TRACE_DIR"), required=True)
    compare.add_argument("--candidate", action="append", nargs=2, metavar=("SEED", "TRACE_DIR"), required=True)
    compare.add_argument("--link-profile", choices=LINK_PROFILES, default="hostile")
    compare.add_argument("--mss-bytes", type=int, default=8192)
    compare.add_argument("--output", default=None)
    compare.add_argument("--fail-on-regression", action="store_true")
    compare.set_defaults(handler=command_compare)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mss_bytes <= 0:
        parser.error("--mss-bytes must be positive")
    try:
        return args.handler(args)
    except SystemExit as error:
        raise
    except (ValueError, argparse.ArgumentTypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
