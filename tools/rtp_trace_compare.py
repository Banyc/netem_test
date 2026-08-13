#!/usr/bin/env python3
"""Compare multiple NETEM_PERF_TRACE_DIR captures or endpoints in one report."""

import argparse
import html
import importlib.util
import math
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("rtp_trace_report.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def read_manifest(trace_dir):
    return {
        row["key"]: row["value"]
        for row in REPORT.read_csv(trace_dir / "manifest.csv")
    }


def read_state_rows(trace_dir, rtp_filename="rtp.csv"):
    rows = []
    for row in REPORT.read_csv(trace_dir / rtp_filename):
        rate = REPORT.field(
            row,
            "send_rate_packets_per_second",
            "send_rate_bytes_per_second",
        )
        if REPORT.field(row, "smoothed_rtt_us") == "" or rate == "":
            continue
        rows.append(
            {
                "time": float(row["elapsed_us"]) / 1_000_000.0,
                "send_rate": float(rate),
                "action": REPORT.field(row, "congestion_action") or "none",
                "outage": REPORT.boolean(REPORT.field(row, "outage_recovery")),
                "delivery_sample_app_limited": REPORT.boolean(
                    REPORT.field(row, "delivery_sample_app_limited", "app_limited")
                ),
                "pending_send_bytes": REPORT.optional_float(
                    REPORT.field(row, "pending_send_bytes")
                ),
                "accepts_new_packet": (
                    None
                    if REPORT.field(row, "accepts_new_packet") == ""
                    else REPORT.boolean(REPORT.field(row, "accepts_new_packet"))
                ),
                "no_response": REPORT.optional_float(
                    REPORT.field(row, "no_response_for_us")
                ),
                "no_progress": REPORT.optional_float(
                    REPORT.field(row, "no_progress_for_us")
                ),
                "event": REPORT.field(row, "event"),
            }
        )
    return rows


def rolling_goodput(trace_dir):
    progress = [
        (
            float(row["elapsed_us"]) / 1_000_000.0,
            float(row["delivered_bytes"]),
        )
        for row in REPORT.read_csv(trace_dir / "progress.csv")
    ]
    points = []
    left = 0
    for right, (elapsed, delivered) in enumerate(progress):
        while left < right and progress[left][0] < elapsed - 1.0:
            left += 1
        if left < right and elapsed > progress[left][0]:
            points.append(
                (
                    elapsed,
                    (delivered - progress[left][1])
                    / (elapsed - progress[left][0])
                    / (1024 * 1024),
                )
            )
    return points


def raw_rtt_cdf(trace_dir, rtp_filename="rtp.csv"):
    samples = sorted(
        float(row["raw_rtt_us"]) / 1000.0
        for row in REPORT.read_csv(trace_dir / rtp_filename)
        if REPORT.field(row, "raw_rtt_us") != ""
    )
    return [
        (value, (index + 1) / len(samples) * 100.0)
        for index, value in enumerate(samples)
    ]


def event_count(trace_dir, rtp_filename, event):
    return sum(
        REPORT.field(row, "event") == event
        for row in REPORT.read_csv(trace_dir / rtp_filename)
    )


def termination_summary(trace_dir, rtp_filename):
    summary = {}
    for row in REPORT.read_csv(trace_dir / rtp_filename):
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


def active_episodes(rows, key):
    episodes = []
    start = None
    last = None
    for row in rows:
        if row[key] and start is None:
            start = row["time"]
        elif not row[key] and start is not None:
            episodes.append((start, last))
            start = None
        last = row["time"]
    if start is not None:
        episodes.append((start, last))
    return episodes


def parse_spec(value):
    label, separator, path = value.partition("=")
    if not separator or not label or not path:
        raise argparse.ArgumentTypeError("trace must be LABEL=TRACE_DIR[/RTP_FILE.csv]")
    source = Path(path)
    if source.suffix == ".csv":
        return label, source.parent, source.name
    return label, source, "rtp.csv"


def render_comparison(specs, output):
    runs = []
    for spec in specs:
        if len(spec) == 2:
            label, trace_dir = spec
            rtp_filename = "rtp.csv"
        else:
            label, trace_dir, rtp_filename = spec
        manifest = read_manifest(trace_dir)
        state = read_state_rows(trace_dir, rtp_filename)
        actions = {}
        for row in state:
            actions[row["action"]] = actions.get(row["action"], 0) + 1
        runs.append(
            {
                "label": label,
                "path": trace_dir,
                "rtp_filename": rtp_filename,
                "manifest": manifest,
                "state": state,
                "actions": actions,
                "rolling": rolling_goodput(trace_dir),
                "rtt_cdf": raw_rtt_cdf(trace_dir, rtp_filename),
                "terminations": termination_summary(trace_dir, rtp_filename),
            }
        )

    summary_rows = []
    action_names = sorted(
        {action for run in runs for action in run["actions"] if action != "none"}
    )
    for run in runs:
        manifest = run["manifest"]
        state = run["state"]
        count = len(state)
        censored = run["actions"].get("censored_outage_sample", 0)
        low_rate = sum(row["send_rate"] <= 128.0001 for row in state)
        outage = sum(row["outage"] for row in state)
        delivery_sample_app_limited = sum(
            row["delivery_sample_app_limited"] for row in state
        )
        outage_episodes = active_episodes(state, "outage")
        max_outage_span = max(
            (end - start for start, end in outage_episodes), default=0.0
        )
        staged = [
            row["pending_send_bytes"]
            for row in state
            if row["pending_send_bytes"] is not None
        ]
        empty_stage = sum(value == 0.0 for value in staged)
        accepts_new_packet = [
            row["accepts_new_packet"]
            for row in state
            if row["accepts_new_packet"] is not None
        ]
        max_no_response = max(
            (row["no_response"] or 0.0 for row in state), default=0.0
        ) / 1_000_000.0
        max_no_progress = max(
            (row["no_progress"] or 0.0 for row in state), default=0.0
        ) / 1_000_000.0
        summary_rows.append(
            "<tr>"
            f"<th>{html.escape(run['label'])}</th>"
            f"<td>{html.escape(manifest.get('revision', ''))}</td>"
            f"<td>{html.escape(manifest.get('netem_c2s_seed', ''))} / {html.escape(manifest.get('netem_s2c_seed', ''))}</td>"
            f"<td>{float(manifest.get('goodput_mib_per_second', math.nan)):.3f}</td>"
            f"<td>{100.0 * low_rate / count if count else math.nan:.1f}%</td>"
            f"<td>{100.0 * delivery_sample_app_limited / count if count else math.nan:.1f}%</td>"
            f"<td>{100.0 * empty_stage / len(staged) if staged else math.nan:.1f}%</td>"
            f"<td>{100.0 * sum(accepts_new_packet) / len(accepts_new_packet) if accepts_new_packet else math.nan:.1f}%</td>"
            f"<td>{100.0 * outage / count if count else math.nan:.1f}%</td>"
            f"<td>{len(outage_episodes)} / {max_outage_span:.3f}</td>"
            f"<td>{100.0 * censored / count if count else math.nan:.1f}%</td>"
            f"<td>{max_no_response:.3f}/{max_no_progress:.3f}</td>"
            f"<td>{html.escape(str(run['terminations']))}</td>"
            f"<td>{count:,}</td>"
            "</tr>"
        )

    action_header = "".join(f"<th>{html.escape(name)}</th>" for name in action_names)
    action_rows = []
    for run in runs:
        count = len(run["state"])
        cells = "".join(
            f"<td>{100.0 * run['actions'].get(name, 0) / count if count else math.nan:.1f}%</td>"
            for name in action_names
        )
        action_rows.append(
            f"<tr><th>{html.escape(run['label'])}</th>{cells}</tr>"
        )

    rolling_series = [(run["label"], run["rolling"]) for run in runs]
    rate_series = [
        (
            run["label"],
            [(row["time"], row["send_rate"]) for row in run["state"]],
        )
        for run in runs
    ]
    cdf_series = [(run["label"], run["rtt_cdf"]) for run in runs]
    liveness_series = []
    for run in runs:
        liveness_series.extend(
            [
                (
                    f"{run['label']} no response",
                    [
                        (row["time"], row["no_response"] / 1_000_000.0)
                        for row in run["state"]
                        if row["no_response"] is not None
                    ],
                ),
                (
                    f"{run['label']} no progress",
                    [
                        (row["time"], row["no_progress"] / 1_000_000.0)
                        for row in run["state"]
                        if row["no_progress"] is not None
                    ],
                ),
            ]
        )
    source_rows = "".join(
        f"<tr><th>{html.escape(run['label'])}</th>"
        f"<td>{html.escape(str(run['path'] / run['rtp_filename']))}</td></tr>"
        for run in runs
    )
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>RTP trace comparison</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 1180px; padding: 0 1rem; color: #172033; }}
h1 {{ margin-bottom: .25rem; }} section {{ margin: 2rem 0; }} svg {{ width: 100%; height: auto; border: 1px solid #d9dfeb; border-radius: 8px; }}
text {{ font-size: 11px; fill: #43506a; }} .plot-bg {{ fill: #fbfcff; }} .grid {{ stroke: #dfe5ef; stroke-width: 1; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: .4rem .55rem; vertical-align: top; }}
.note {{ background: #f5f7fb; border-left: 4px solid #64748b; padding: .75rem 1rem; }}
</style></head><body>
<h1>RTP trace comparison</h1>
<p class="note">Compare identical seed pairs before drawing a causal conclusion. "At/below 128 pps" identifies time at or below the outage restart rate; loss control may intentionally reduce the rate as far as 1 pps. Action percentages are state-snapshot occupancy, not event counts. "App-limited delivery samples" describes the packet behind the latest delivery-rate sample, not the sender's current staging state.</p>
<section><h2>Run summary</h2><table><thead><tr><th>run</th><th>revision</th><th>c2s/s2c seed</th><th>goodput MiB/s</th><th>at/below 128 pps</th><th>app-limited delivery samples</th><th>empty send stage</th><th>accepts new packet</th><th>outage recovery</th><th>outage episodes / max span s</th><th>censored sample</th><th>max no-response/progress s</th><th>terminations</th><th>state rows</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></section>
{REPORT.svg_line_chart("Rolling application goodput", "elapsed time (s)", "MiB/s over ~1 s", rolling_series)}
{REPORT.svg_line_chart("Congestion-control send rate", "elapsed time (s)", "packets/s", rate_series)}
{REPORT.svg_line_chart("Raw RTT empirical CDF", "raw RTT (ms)", "samples ≤ x (%)", cdf_series, (0.0, 100.0))}
{REPORT.svg_line_chart("Peer liveness waits", "elapsed time (s)", "seconds", liveness_series)}
<section><h2>Congestion-control action occupancy</h2><table><thead><tr><th>run</th>{action_header}</tr></thead><tbody>{''.join(action_rows)}</tbody></table></section>
<section><h2>Trace sources</h2><table>{source_rows}</table></section>
</body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "trace",
        nargs="+",
        type=parse_spec,
        help="LABEL=TRACE_DIR or LABEL=TRACE_DIR/RTP_FILE.csv",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    render_comparison(args.trace, args.out)
    print(args.out)


if __name__ == "__main__":
    main()
