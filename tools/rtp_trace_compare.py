#!/usr/bin/env python3
"""Deterministic shared-timeline comparison JSON/HTML from netem traces.

Reads manifest/rtp/rtp_peer/netem/progress evidence for a set of baseline
and candidate runs, computes per-run trace health, pairs runs by c2s/s2c
seed identity, calculates candidate-versus-baseline metric deltas, and
classifies the result.  The verdict is a consistency label, not statistical
confidence or causality; every source hint carries a does_not_prove
constraint and no hint identifies a specific branch as causal.
"""

import argparse
import csv
import html
import importlib.util
import json
import math
import statistics
from pathlib import Path

COMPARISON_SCHEMA_VERSION = 2

MODULE_PATH = Path(__file__).with_name("rtp_trace_report.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def read_manifest(trace_dir):
    return {row["key"]: row["value"] for row in read_csv(trace_dir / "manifest.csv")}


def optional_float(value):
    return None if value in (None, "") else float(value)


def boolean(value):
    return str(value).lower() in ("1", "true", "yes")


def metric_number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def state_rows(rows, manifest):
    """Rows carrying a controller-state snapshot, on the shared timeline."""
    state = []
    for row in rows:
        smoothed = REPORT.field(row, "smoothed_rtt_us")
        if smoothed == "":
            continue
        rate = REPORT.field(row, "send_rate_packets_per_second", "send_rate_bytes_per_second")
        state.append(
            {
                "time": REPORT.timeline_seconds(row, manifest),
                "send_rate": metric_number(rate, 0.0),
                "raw_rtt": metric_number(REPORT.field(row, "raw_rtt_us")),
                "action": REPORT.field(row, "congestion_action") or "none",
                "outage": boolean(REPORT.field(row, "outage_recovery")),
                "delivery_sample_app_limited": boolean(
                    REPORT.field(row, "delivery_sample_app_limited", "app_limited")
                ),
                "pending_send_bytes": optional_float(REPORT.field(row, "pending_send_bytes")),
                "accepts_new_packet": (
                    None
                    if REPORT.field(row, "accepts_new_packet") == ""
                    else boolean(REPORT.field(row, "accepts_new_packet"))
                ),
                "no_response": optional_float(REPORT.field(row, "no_response_for_us")),
                "no_progress": optional_float(REPORT.field(row, "no_progress_for_us")),
                "retransmitted": metric_number(REPORT.field(row, "retransmitted_packets"), 0.0),
                "loss": metric_number(REPORT.field(row, "loss_ratio")),
                "cc_loss": metric_number(REPORT.field(row, "congestion_loss_ratio")),
                "event": REPORT.field(row, "event"),
            }
        )
    return state


def raw_rtt_ms(rows):
    return sorted(
        value
        for value in (optional_float(REPORT.field(row, "raw_rtt_us")) for row in rows)
        if value is not None
    )


def termination_summary(rows):
    summary = {}
    for row in rows:
        event = REPORT.field(row, "event")
        if event == "proactive_termination":
            key = "proactive_stall/broken_pipe"
        elif event == "session_termination":
            cause = REPORT.field(row, "termination_cause") or "unknown"
            error = REPORT.field(row, "termination_error_kind") or "unknown"
            key = f"{cause}/{error}"
            errno = REPORT.field(row, "termination_raw_os_error")
            if errno:
                key += f"(errno={errno})"
        else:
            continue
        summary[key] = summary.get(key, 0) + 1
    return summary


def has_terminal_event(rows):
    return any(
        REPORT.field(row, "event") in ("session_termination", "proactive_termination")
        for row in rows
    )


def final_netem_counters(netem_rows):
    counters = {}
    for row in netem_rows:
        direction = REPORT.field(row, "direction")
        if direction not in counters:
            counters[direction] = {}
        for key in (
            "received", "forwarded", "delayed", "dropped", "duplicated",
            "reordered", "rate_limited", "overflow_dropped",
        ):
            value = metric_number(REPORT.field(row, key), 0.0)
            counters[direction][key] = counters[direction].get(key, 0.0) + value
    return counters


def rolling_goodput(progress_rows):
    points = []
    left = 0
    for right, (elapsed, delivered) in enumerate(progress_rows):
        while left < right and progress_rows[left][0] < elapsed - 1.0:
            left += 1
        if left < right and elapsed > progress_rows[left][0]:
            points.append(
                (
                    elapsed,
                    (delivered - progress_rows[left][1])
                    / (elapsed - progress_rows[left][0])
                    / (1024 * 1024),
                )
            )
    return points


def read_run(spec):
    if len(spec) == 2:
        label, trace_dir = spec
        rtp_filename = "rtp.csv"
    else:
        label, trace_dir, rtp_filename = spec
    trace_dir = Path(trace_dir)
    manifest = read_manifest(trace_dir)
    rtp = read_csv(trace_dir / rtp_filename)
    peer = read_csv(trace_dir / "rtp_peer.csv")
    netem = read_csv(trace_dir / "netem.csv")
    progress_raw = read_csv(trace_dir / "progress.csv")
    progress = [
        (REPORT.timeline_seconds(row, manifest), float(row["delivered_bytes"]))
        for row in progress_raw
        if REPORT.field(row, "delivered_bytes") != ""
    ]
    state = state_rows(rtp, manifest)
    rtt = raw_rtt_ms(rtp)
    peer_state = state_rows(peer, manifest)
    peer_rtt = raw_rtt_ms(peer)
    return {
        "label": label,
        "path": trace_dir,
        "rtp_filename": rtp_filename,
        "manifest": manifest,
        "rtp": rtp,
        "rtp_peer": peer,
        "netem": netem,
        "progress": progress,
        "state": state,
        "rtt": rtt,
        "peer_state": peer_state,
        "peer_rtt": peer_rtt,
        "rolling": rolling_goodput(progress),
        "health": trace_health(trace_dir, manifest, rtp, peer),
        "summary": summarize_run(manifest, state, peer_state, rtt, peer_rtt, netem, progress, rtp, peer),
    }


def trace_health(trace_dir, manifest, rtp, peer):
    checks = {}

    schema = manifest.get("trace_schema_version", "")
    row_schema = REPORT.field(rtp[0], "schema_version") if rtp else ""
    checks["schema_compatible"] = schema in ("9",) or row_schema in ("9", "1", "8")

    observer = manifest.get("rtp_observer", "")
    checks["observer_present"] = boolean(observer) if observer != "" else False

    dropped = metric_number(manifest.get("rtp_dropped_capacity"), 0) or 0
    peer_dropped = metric_number(manifest.get("rtp_peer_dropped_capacity"), 0) or 0
    checks["capture_not_dropped"] = dropped == 0 and peer_dropped == 0

    checks["terminated"] = has_terminal_event(rtp) and has_terminal_event(peer)

    probe_outcome = manifest.get("probe_outcome", "")
    checks["task_completed"] = probe_outcome in ("", "completed")

    checks["endpoint_integrity"] = True
    for key in ("sink_read_outcome", "client_mux_outcome", "server_mux_outcome"):
        outcome = manifest.get(key, "")
        if outcome and "corrupt" in outcome.lower():
            checks["endpoint_integrity"] = False

    checks["rtp_readable"] = len(rtp) > 0
    checks["peer_readable"] = len(peer) > 0

    failures = [key for key, ok in checks.items() if not ok]
    invalid_keys = {
        "rtp_readable",
        "peer_readable",
        "schema_compatible",
        "task_completed",
        "endpoint_integrity",
    }
    if any(key in invalid_keys for key in failures):
        quality = "invalid"
    elif failures:
        quality = "degraded"
    else:
        quality = "healthy"
    return {"checks": checks, "evidence_quality": quality}


def _percent(value, total):
    return 100.0 * value / total if total else math.nan


def summarize_run(manifest, state, peer_state, rtt, peer_rtt, netem, progress, rtp, peer):
    count = len(state)
    actions = {}
    for row in state:
        actions[row["action"]] = actions.get(row["action"], 0) + 1
    low_rate = sum(row["send_rate"] <= 128.0001 for row in state)
    outage = sum(row["outage"] for row in state)
    app_limited = sum(row["delivery_sample_app_limited"] for row in state)
    staged = [
        row["pending_send_bytes"] for row in state if row["pending_send_bytes"] is not None
    ]
    empty_stage = sum(value == 0.0 for value in staged)
    accepts = [row["accepts_new_packet"] for row in state if row["accepts_new_packet"] is not None]
    retransmitted = [row["retransmitted"] for row in state if row["retransmitted"] > 0]
    loss = [row["loss"] for row in state if row["loss"] is not None]
    cc_loss = [row["cc_loss"] for row in state if row["cc_loss"] is not None]
    controller_state = {
        "slow_start": _percent(
            sum(boolean(REPORT.field(row, "slow_start")) for row in rtp), count
        ),
        "gentle_mode": _percent(
            sum(boolean(REPORT.field(row, "gentle_mode")) for row in rtp), count
        ),
        "gentle_draining": _percent(
            sum(boolean(REPORT.field(row, "gentle_draining")) for row in rtp), count
        ),
        "queue_building": _percent(
            sum(boolean(REPORT.field(row, "queue_building")) for row in rtp), count
        ),
        "drain_floor_binding": _percent(
            sum(boolean(REPORT.field(row, "drain_floor_binding")) for row in rtp), count
        ),
        "outage_recovery": _percent(outage, count),
    }
    delivered = metric_number(manifest.get("delivered_bytes"))
    elapsed = metric_number(manifest.get("elapsed_seconds"))
    return {
        "count": count,
        "actions": actions,
        "low_send_rate_occupancy": _percent(low_rate, count),
        "controller_state_occupancy": controller_state,
        "app_limited_occupancy": _percent(app_limited, count),
        "empty_send_stage_occupancy": _percent(empty_stage, len(staged)) if staged else math.nan,
        "accepts_new_packet_occupancy": (
            _percent(sum(accepts), len(accepts)) if accepts else math.nan
        ),
        "retransmission_samples": len(retransmitted),
        "max_retransmitted": max(retransmitted, default=0.0),
        "loss_samples": len(loss),
        "mean_loss_ratio": statistics.fmean(loss) if loss else math.nan,
        "cc_loss_samples": len(cc_loss),
        "mean_cc_loss_ratio": statistics.fmean(cc_loss) if cc_loss else math.nan,
        "rtt_p50_ms": REPORT.quantile(rtt, 0.5) if rtt else math.nan,
        "peer_rtt_p50_ms": REPORT.quantile(peer_rtt, 0.5) if peer_rtt else math.nan,
        "terminations": termination_summary(rtp),
        "peer_terminations": termination_summary(peer),
        "final_netem_counters": final_netem_counters(netem),
        "delivered_bytes": delivered if delivered is not None else (
            progress[-1][1] if progress else 0
        ),
        "elapsed_seconds": elapsed if elapsed is not None else (
            progress[-1][0] if progress else 0
        ),
        "goodput_mib_per_second": metric_number(manifest.get("goodput_mib_per_second")),
        "sink_read_outcome": manifest.get("sink_read_outcome", ""),
        "client_mux_outcome": manifest.get("client_mux_outcome", ""),
        "server_mux_outcome": manifest.get("server_mux_outcome", ""),
        "probe_outcome": manifest.get("probe_outcome", ""),
    }


CONFIG_KEYS = (
    "window_seconds",
    "mss_bytes",
    "fec",
    "rtp_handshake",
    "link_profile",
    "scenario",
)


def pair_config_agrees(baseline, candidate):
    mismatches = []
    for key in CONFIG_KEYS:
        left = baseline.get(key, "")
        right = candidate.get(key, "")
        if left and right and left != right:
            mismatches.append(key)
    return mismatches


def pair_runs(baseline_runs, candidate_runs):
    candidates_by_seed = {}
    for run in candidate_runs:
        key = (run["manifest"].get("netem_c2s_seed"), run["manifest"].get("netem_s2c_seed"))
        candidates_by_seed.setdefault(key, []).append(run)
    pairs = []
    for baseline in baseline_runs:
        key = (baseline["manifest"].get("netem_c2s_seed"), baseline["manifest"].get("netem_s2c_seed"))
        for candidate in candidates_by_seed.get(key, []):
            pairs.append({"baseline": baseline, "candidate": candidate})
    return pairs


def delta_percent(candidate, baseline):
    if baseline in (None, 0) or candidate is None:
        return None
    return (candidate - baseline) / abs(baseline) * 100.0


METRICS = ("goodput_mib_per_second", "rtt_p50_ms", "low_send_rate_occupancy")


def pair_metrics(pair):
    baseline = pair["baseline"]
    candidate = pair["candidate"]
    out = {}
    for metric in METRICS:
        base = baseline["summary"][metric]
        cand = candidate["summary"][metric]
        if metric == "rtt_p50_ms":
            # For latency, lower is better; keep the raw sign convention and
            # report the arithmetic delta so materiality stays comparable.
            value = delta_percent(cand, base)
        else:
            value = delta_percent(cand, base)
        out[metric] = {
            "baseline": base,
            "candidate": cand,
            "delta_percent": value,
        }
    return out


def classify(valid_pairs):
    if not valid_pairs:
        return "insufficient_evidence"
    deltas = [
        pair["metrics"]["goodput_mib_per_second"]["delta_percent"]
        for pair in valid_pairs
    ]
    deltas = [value for value in deltas if value is not None]
    if not deltas:
        return "insufficient_evidence"
    if all(value <= -10.0 for value in deltas):
        return "likely_regression"
    if all(value >= 10.0 for value in deltas):
        return "likely_improvement"
    if all(-10.0 < value < 10.0 for value in deltas):
        return "no_material_change"
    return "mixed_results"


def guidance_hints(pairs, verdict):
    hints = []
    changes = []
    for index, pair in enumerate(pairs):
        for metric, values in pair["metrics"].items():
            delta = values["delta_percent"]
            if delta is None:
                continue
            changes.append(
                {
                    "pair": index,
                    "metric": metric,
                    "baseline": values["baseline"],
                    "candidate": values["candidate"],
                    "delta_percent": delta,
                }
            )
    changes.sort(key=lambda item: abs(item["delta_percent"]), reverse=True)
    for change in changes[:5]:
        if change["metric"] == "rtt_p50_ms":
            direction = "worse" if change["delta_percent"] > 0 else "better"
        else:
            direction = "worse" if change["delta_percent"] < 0 else "better"
        hints.append(
            {
                "metric": change["metric"],
                "baseline": change["baseline"],
                "candidate": change["candidate"],
                "direction": direction,
                "delta_percent": change["delta_percent"],
                "does_not_prove": (
                    "This is a consistency signal from the paired traces and "
                    "does_not_prove that any specific change produced the "
                    "difference; machine noise, scheduling, or adjacent workload "
                    "interference can move the same metric."
                ),
            }
        )
    if verdict in ("likely_regression", "likely_improvement"):
        hints.append(
            {
                "metric": "verdict",
                "direction": verdict,
                "delta_percent": None,
                "does_not_prove": (
                    "The paired verdict is a consistency label over the whole "
                    "evidence set and does_not_prove an explanation; confirm "
                    "with repeated runs before attributing the change."
                ),
            }
        )
    if not pairs:
        hints.append(
            {
                "metric": "evidence_boundary",
                "direction": "insufficient_evidence",
                "delta_percent": None,
                "does_not_prove": (
                    "No valid paired evidence remains after trace-health and "
                    "configuration filtering; re-run the affected role or fix "
                    "the broken trace before comparing. This boundary does_not_prove "
                    "that either side is faster or slower."
                ),
            }
        )
    return hints


def build_comparison(baseline_specs, candidate_specs):
    runs = []
    for spec in baseline_specs:
        run = read_run(spec)
        run["role"] = "baseline"
        runs.append(run)
    for spec in candidate_specs:
        run = read_run(spec)
        run["role"] = "candidate"
        runs.append(run)

    baseline_runs = [run for run in runs if run["role"] == "baseline"]
    candidate_runs = [run for run in runs if run["role"] == "candidate"]
    pairs = pair_runs(baseline_runs, candidate_runs)

    valid_pairs = []
    excluded = []
    for pair in pairs:
        baseline = pair["baseline"]
        candidate = pair["candidate"]
        mismatches = pair_config_agrees(baseline["manifest"], candidate["manifest"])
        reasons = []
        reasons.extend(f"{key} mismatch" for key in mismatches)
        for role in ("baseline", "candidate"):
            quality = (baseline if role == "baseline" else candidate)["health"]["evidence_quality"]
            if quality == "invalid":
                reasons.append(f"{role} trace invalid")
            elif quality == "degraded":
                reasons.append(f"{role} evidence degraded")
        pair["metrics"] = pair_metrics(pair)
        pair["excluded_reasons"] = reasons
        pair["valid"] = not reasons
        if pair["valid"]:
            valid_pairs.append(pair)
        else:
            excluded.append(pair)

    verdict = classify(valid_pairs)
    hints = guidance_hints(valid_pairs, verdict)

    run_health = [
        {
            "label": run["label"],
            "role": run["role"],
            "path": str(run["path"]),
            "evidence_quality": run["health"]["evidence_quality"],
            "checks": run["health"]["checks"],
            "summary": run["summary"],
        }
        for run in runs
    ]

    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "verdict": verdict,
        "valid_pairs": len(valid_pairs),
        "total_pairs": len(pairs),
        "evidence_quality": _overall_quality(runs),
        "runs": run_health,
        "pairs": [
            {
                "index": index,
                "baseline": pair["baseline"]["label"],
                "candidate": pair["candidate"]["label"],
                "valid": pair["valid"],
                "excluded_reasons": pair["excluded_reasons"],
                "metrics": {
                    metric: {
                        "baseline": values["baseline"],
                        "candidate": values["candidate"],
                        "delta_percent": values["delta_percent"],
                    }
                    for metric, values in pair["metrics"].items()
                },
            }
            for index, pair in enumerate(pairs)
        ],
        "largest_changes": [
            {
                "metric": change["metric"],
                "baseline": change["baseline"],
                "candidate": change["candidate"],
                "delta_percent": change["delta_percent"],
                "does_not_prove": change["does_not_prove"],
            }
            for change in hints
            if "baseline" in change
        ],
        "agent_guidance": hints,
    }


def _overall_quality(runs):
    qualities = {run["health"]["evidence_quality"] for run in runs}
    if "invalid" in qualities:
        return "invalid"
    if "degraded" in qualities:
        return "degraded"
    return "healthy"


def _fmt(value, pattern):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return pattern.format(value)
        except (ValueError, TypeError):
            return str(value)
    return str(value)


def escape(value):
    return html.escape(str(value))


def render_html(comparison, runs):
    health_rows = []
    for run in runs:
        checks = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in run["health"]["checks"].items()
        )
        manifest = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in run["manifest"].items()
        )
        summary = run["summary"]
        controller = "".join(
            f"<td>{escape(f'{value:.1f}') if isinstance(value, float) else escape(value)}</td>"
            for value in (
                summary["controller_state_occupancy"]["slow_start"],
                summary["controller_state_occupancy"]["gentle_mode"],
                summary["controller_state_occupancy"]["gentle_draining"],
                summary["controller_state_occupancy"]["queue_building"],
                summary["controller_state_occupancy"]["drain_floor_binding"],
                summary["controller_state_occupancy"]["outage_recovery"],
            )
        )
        health_rows.append(
            "<tr>"
            f"<th>{escape(run['label'])}</th>"
            f"<td>{escape(run['role'])}</td>"
            f"<td>{escape(run['health']['evidence_quality'])}</td>"
            f"<td>{escape(run['manifest'].get('netem_c2s_seed', ''))} / {escape(run['manifest'].get('netem_s2c_seed', ''))}</td>"
            f"<td>{escape(_fmt(summary['goodput_mib_per_second'], '{:.3f}'))}</td>"
            f"<td>{escape(_fmt(summary['rtt_p50_ms'], '{:.2f}'))}</td>"
            f"<td>{escape(_fmt(summary['low_send_rate_occupancy'], '{:.1f}'))}</td>"
            f"<td>{escape(str(summary['terminations']))}</td>"
            f"<td>{escape(str(summary['peer_terminations']))}</td>"
            f"<td>{escape(str(summary['final_netem_counters']))}</td>"
            f"<td>{escape(str(summary['sink_read_outcome'] or summary['probe_outcome'] or ''))}</td>"
            f"<td>{escape(str(summary['client_mux_outcome']))} / {escape(str(summary['server_mux_outcome']))}</td>"
            "</tr>"
        )

    pair_rows = []
    for index, pair in enumerate(comparison["pairs"]):
        cells = ""
        for metric in METRICS:
            values = pair["metrics"][metric]
            cells += (
                f"<td>{escape(_fmt(values['baseline'], '{:.3f}'))}"
                f" → {escape(_fmt(values['candidate'], '{:.3f}'))}"
                f" ({escape(_fmt(values['delta_percent'], '{:+.1f}%'))})</td>"
            )
        pair_rows.append(
            "<tr>"
            f"<th>{index}</th>"
            f"<td>{escape(pair['baseline'])}</td>"
            f"<td>{escape(pair['candidate'])}</td>"
            f"<td>{escape(pair['valid'])}</td>"
            f"<td>{escape(', '.join(pair['excluded_reasons']) or '-')}</td>"
            f"{cells}"
            "</tr>"
        )

    guidance = "".join(
        f"<li><b>{escape(hint['metric'])}</b> ({escape(hint['direction'])})"
        f"{f', {hint['delta_percent']:+.1f}%' if hint['delta_percent'] is not None else ''}: {escape(hint['does_not_prove'])}</li>"
        for hint in comparison["agent_guidance"]
    )

    controller_rows = "".join(
        "<tr>"
        f"<th>{escape(run['label'])}</th>{controller}"
        "</tr>"
        for run in runs
    )

    timeline_series = []
    for run in runs:
        timeline_series.append(
            (
                f"{run['label']} goodput",
                [(point[0], point[1]) for point in run["rolling"]],
            )
        )
    cdf_series = [
        (
            f"{run['label']} RTT",
            [
                (value, (index + 1) / len(run["rtt"]) * 100.0)
                for index, value in enumerate(run["rtt"])
            ],
        )
        for run in runs
        if run["rtt"]
    ]
    netem_series = []
    for run in runs:
        for direction, counters in run["summary"]["final_netem_counters"].items():
            netem_series.append((f"{run['label']} {direction} forwarded", [(0, counters.get("forwarded", 0.0))]))

    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Netem paired comparison</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 1200px; padding: 0 1rem; color: #172033; }}
h1 {{ margin-bottom: .25rem; }} section {{ margin: 2rem 0; }} svg {{ width: 100%; height: auto; border: 1px solid #d9dfeb; border-radius: 8px; }}
text {{ font-size: 11px; fill: #43506a; }} .plot-bg {{ fill: #fbfcff; }} .grid {{ stroke: #dfe5ef; stroke-width: 1; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: .4rem .55rem; vertical-align: top; }}
.note {{ background: #f5f7fb; border-left: 4px solid #64748b; padding: .75rem 1rem; }}
.verdict {{ font-size: 1.15rem; font-weight: 600; }}
</style></head><body>
<h1>Netem paired comparison</h1>
<p class="note">The verdict is a consistency label derived from valid paired seed identities only; it is not statistical confidence and does not prove causality. Every hint below carries a does_not_prove constraint.</p>
<p class="verdict">verdict: {escape(comparison['verdict'])} — {comparison['valid_pairs']} valid / {comparison['total_pairs']} total pairs</p>
<section><h2>Run health</h2><table><thead><tr><th>run</th><th>role</th><th>evidence</th><th>c2s/s2c seed</th><th>goodput MiB/s</th><th>RTT p50 ms</th><th>low rate %</th><th>terminations</th><th>peer terminations</th><th>netem counters</th><th>probe/sink</th><th>mux outcomes</th></tr></thead><tbody>{''.join(health_rows)}</tbody></table></section>
<section><h2>Paired outcomes</h2><table><thead><tr><th>#</th><th>baseline</th><th>candidate</th><th>valid</th><th>excluded</th>{''.join(f'<th>{escape(metric)}</th>' for metric in METRICS)}</tr></thead><tbody>{''.join(pair_rows)}</tbody></table></section>
<section><h2>Controller-state occupancy</h2><table><thead><tr><th>run</th><th>slow start</th><th>gentle</th><th>gentle drain</th><th>queue building</th><th>drain floor</th><th>outage recovery</th></tr></thead><tbody>{controller_rows}</tbody></table></section>
{REPORT.svg_line_chart("Rolling application goodput (shared timeline)", "trace time (s)", "MiB/s over ~1 s", timeline_series)}
{REPORT.svg_line_chart("Raw RTT empirical CDF", "raw RTT (ms)", "samples ≤ x (%)", cdf_series, (0.0, 100.0))}
<section><h2>Agent guidance</h2><ul>{guidance}</ul></section>
</body></html>"""
    return content


def render_comparison(baseline_specs, candidate_specs, output_dir):
    output_dir = Path(output_dir)
    runs = []
    for spec in list(baseline_specs) + list(candidate_specs):
        run = read_run(spec)
        run["role"] = "baseline" if spec in baseline_specs else "candidate"
        runs.append(run)
    comparison = build_comparison(baseline_specs, candidate_specs)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "comparison.html").write_text(
        render_html(comparison, runs), encoding="utf-8"
    )
    return output_dir


def parse_spec(value):
    label, separator, path = value.partition("=")
    if not separator or not label or not path:
        raise argparse.ArgumentTypeError("trace must be LABEL=TRACE_DIR[/RTP_FILE.csv]")
    source = Path(path)
    if source.suffix == ".csv":
        return label, source.parent, source.name
    return label, source, "rtp.csv"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        action="append",
        type=parse_spec,
        default=[],
        metavar="LABEL=TRACE_DIR[/RTP_FILE.csv]",
        help="baseline run (repeatable)",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        type=parse_spec,
        default=[],
        metavar="LABEL=TRACE_DIR[/RTP_FILE.csv]",
        help="candidate run (repeatable)",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not args.baseline or not args.candidate:
        parser.error("at least one --baseline and one --candidate trace are required")
    output_dir = render_comparison(args.baseline, args.candidate, args.out)
    print(output_dir)


if __name__ == "__main__":
    main()
