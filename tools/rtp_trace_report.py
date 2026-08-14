#!/usr/bin/env python3
"""Render a self-contained HTML report from a NETEM_PERF_TRACE_DIR capture."""

import argparse
import csv
import html
import math
import statistics
from pathlib import Path


COLORS = (
    "#2563eb",
    "#dc2626",
    "#059669",
    "#7c3aed",
    "#d97706",
    "#0891b2",
    "#be185d",
    "#4d7c0f",
)
WIDTH = 960
HEIGHT = 300
PAD_LEFT = 72
PAD_RIGHT = 24
PAD_TOP = 24
PAD_BOTTOM = 48


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def optional_float(value):
    return None if value in (None, "") else float(value)


def field(row, name, legacy_name=None, default=""):
    if name in row:
        return row[name]
    if legacy_name is not None and legacy_name in row:
        return row[legacy_name]
    return default


def boolean(value):
    return str(value).lower() in ("1", "true", "yes")


def timeline_seconds(row, manifest):
    """Shared timeline for a row: prefer trace_elapsed_us, otherwise align
    legacy elapsed_us with the manifest measurement-start offset."""
    trace_elapsed = row.get("trace_elapsed_us", "")
    if trace_elapsed not in (None, ""):
        return float(trace_elapsed) / 1_000_000.0
    elapsed = float(row.get("elapsed_us", 0))
    start = manifest.get("measurement_start_trace_elapsed_us")
    if start not in (None, ""):
        return (float(start) + elapsed) / 1_000_000.0
    return elapsed / 1_000_000.0


def quantile(sorted_values, fraction):
    if not sorted_values:
        return math.nan
    index = fraction * (len(sorted_values) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[lower]
    weight = index - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def decimate(points, limit=3000):
    if len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    return points[::step]


def finite_extent(series):
    values = [value for _, points in series for _, value in points if math.isfinite(value)]
    if not values:
        return (0.0, 1.0)
    low, high = min(values), max(values)
    if low == high:
        margin = max(abs(low) * 0.05, 1.0)
        return (low - margin, high + margin)
    margin = (high - low) * 0.05
    return (low - margin, high + margin)


def svg_line_chart(title, x_label, y_label, series, y_extent=None):
    series = [(name, decimate(points)) for name, points in series if points]
    if not series:
        return f"<section><h2>{html.escape(title)}</h2><p>No samples.</p></section>"
    xs = [x for _, points in series for x, _ in points]
    x_min, x_max = min(xs), max(xs)
    if x_min == x_max:
        x_max = x_min + 1.0
    y_min, y_max = y_extent or finite_extent(series)
    legend_columns = min(len(series), 4)
    legend_rows = math.ceil(len(series) / legend_columns)
    plot_top = PAD_TOP + (legend_rows - 1) * 18
    plot_width = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_height = HEIGHT - plot_top - PAD_BOTTOM

    def sx(value):
        return PAD_LEFT + (value - x_min) / (x_max - x_min) * plot_width

    def sy(value):
        return plot_top + (y_max - value) / (y_max - y_min) * plot_height

    parts = [
        f"<section><h2>{html.escape(title)}</h2><svg viewBox=\"0 0 {WIDTH} {HEIGHT}\" role=\"img\">",
        f"<rect x=\"{PAD_LEFT}\" y=\"{plot_top}\" width=\"{plot_width}\" height=\"{plot_height}\" class=\"plot-bg\"/>",
    ]
    for tick in range(6):
        fraction = tick / 5
        x_value = x_min + (x_max - x_min) * fraction
        x = sx(x_value)
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{plot_top}\" x2=\"{x:.1f}\" y2=\"{HEIGHT - PAD_BOTTOM}\" class=\"grid\"/>")
        parts.append(f"<text x=\"{x:.1f}\" y=\"{HEIGHT - 24}\" text-anchor=\"middle\">{x_value:.1f}</text>")
        y_value = y_min + (y_max - y_min) * fraction
        y = sy(y_value)
        parts.append(f"<line x1=\"{PAD_LEFT}\" y1=\"{y:.1f}\" x2=\"{WIDTH - PAD_RIGHT}\" y2=\"{y:.1f}\" class=\"grid\"/>")
        parts.append(f"<text x=\"{PAD_LEFT - 9}\" y=\"{y + 4:.1f}\" text-anchor=\"end\">{y_value:.2f}</text>")
    for index, (name, points) in enumerate(series):
        color = COLORS[index % len(COLORS)]
        path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
        parts.append(f"<polyline points=\"{path}\" fill=\"none\" stroke=\"{color}\" stroke-width=\"1.7\"/>")
    parts.append(f"<text x=\"{WIDTH / 2}\" y=\"{HEIGHT - 5}\" text-anchor=\"middle\">{html.escape(x_label)}</text>")
    parts.append(f"<text x=\"18\" y=\"{HEIGHT / 2}\" text-anchor=\"middle\" transform=\"rotate(-90 18 {HEIGHT / 2})\">{html.escape(y_label)}</text>")
    parts.append("<g class=\"legend\">")
    for index, (name, _) in enumerate(series):
        column = index % legend_columns
        row = index // legend_columns
        x = PAD_LEFT + column * (plot_width / legend_columns)
        y = 14 + row * 18
        color = COLORS[index % len(COLORS)]
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{y}\" x2=\"{x + 20:.1f}\" y2=\"{y}\" stroke=\"{color}\" stroke-width=\"3\"/>")
        parts.append(f"<text x=\"{x + 25:.1f}\" y=\"{y + 4}\">{html.escape(name)}</text>")
    parts.append("</g></svg></section>")
    return "".join(parts)


def svg_histogram(samples, buckets=48):
    if not samples:
        return "<section><h2>Raw RTT histogram</h2><p>No raw RTT samples.</p></section>"
    low, high = min(samples), max(samples)
    if low == high:
        high = low + 1.0
    counts = [0] * buckets
    for value in samples:
        index = min(int((value - low) / (high - low) * buckets), buckets - 1)
        counts[index] += 1
    maximum = max(counts)
    plot_width = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_height = HEIGHT - PAD_TOP - PAD_BOTTOM
    bar_width = plot_width / buckets
    parts = [
        "<section><h2>Raw RTT histogram</h2>",
        f"<svg viewBox=\"0 0 {WIDTH} {HEIGHT}\" role=\"img\">",
        f"<rect x=\"{PAD_LEFT}\" y=\"{PAD_TOP}\" width=\"{plot_width}\" height=\"{plot_height}\" class=\"plot-bg\"/>",
    ]
    for index, count in enumerate(counts):
        height = count / maximum * plot_height
        x = PAD_LEFT + index * bar_width
        y = PAD_TOP + plot_height - height
        parts.append(f"<rect x=\"{x:.1f}\" y=\"{y:.1f}\" width=\"{max(bar_width - 1, 0.5):.1f}\" height=\"{height:.1f}\" fill=\"#2563eb\"/>")
    for tick in range(6):
        value = low + (high - low) * tick / 5
        x = PAD_LEFT + plot_width * tick / 5
        parts.append(f"<text x=\"{x:.1f}\" y=\"{HEIGHT - 24}\" text-anchor=\"middle\">{value:.1f}</text>")
    parts.append(f"<text x=\"{WIDTH / 2}\" y=\"{HEIGHT - 5}\" text-anchor=\"middle\">raw RTT (ms); {buckets} equal-width bins</text>")
    parts.append("<text x=\"18\" y=\"150\" text-anchor=\"middle\" transform=\"rotate(-90 18 150)\">count</text>")
    parts.append("</svg></section>")
    return "".join(parts)


def svg_cdf(samples):
    sorted_samples = sorted(samples)
    points = [(value, (index + 1) / len(sorted_samples) * 100.0) for index, value in enumerate(sorted_samples)]
    return svg_line_chart("Raw RTT empirical CDF", "raw RTT (ms)", "samples ≤ x (%)", [("raw RTT", points)], (0.0, 100.0))


def load_trace(trace_dir, rtp_filename="rtp.csv"):
    manifest = {row["key"]: row["value"] for row in read_csv(trace_dir / "manifest.csv")}
    rtp = read_csv(trace_dir / rtp_filename)
    netem = read_csv(trace_dir / "netem.csv")
    progress = read_csv(trace_dir / "progress.csv")
    return manifest, rtp, netem, progress


def render_report(trace_dir, output, rtp_filename="rtp.csv"):
    manifest, rtp_rows, netem_rows, progress_rows = load_trace(trace_dir, rtp_filename)
    rtp = []
    for row in rtp_rows:
        if field(row, "smoothed_rtt_us") == "":
            continue
        rtp.append(
            {
                "time": timeline_seconds(row, manifest),
                "raw_rtt": optional_float(row["raw_rtt_us"]),
                "min_rtt": optional_float(row["minimum_rtt_us"]),
                "srtt": float(row["smoothed_rtt_us"]),
                "cwnd": float(row["congestion_window_packets"]),
                "send_rate": float(field(row, "send_rate_packets_per_second", "send_rate_bytes_per_second")),
                "delivery_rate": optional_float(field(row, "delivery_rate_packets_per_second", "delivery_rate_bytes_per_second")),
                "delivery_sample_app_limited": boolean(
                    field(row, "delivery_sample_app_limited", "app_limited")
                ),
                "pending_send_bytes": optional_float(field(row, "pending_send_bytes")),
                "send_stage_capacity_bytes": optional_float(field(row, "send_stage_capacity_bytes")),
                "accepts_new_packet": boolean(field(row, "accepts_new_packet")),
                "loss": optional_float(row["loss_ratio"]),
                "cc_loss": optional_float(field(row, "congestion_loss_ratio")),
                "cc_action": {
                    "": 0.0,
                    "outage_reset": 1.0,
                    "censored_outage_sample": 2.0,
                    "slow_start_ack": 3.0,
                    "bandwidth_probe": 4.0,
                    "gentle_probe": 5.0,
                    "delay_drain": 6.0,
                    "loss_backoff": 7.0,
                    "huge_loss_backoff": 8.0,
                }.get(field(row, "congestion_action"), 0.0),
                "rtx": float(row["retransmitted_packets"]),
                "pipe": float(row["packets_in_pipe"]),
                "send_seq": int(row["next_send_sequence"]),
                "recv_seq": None if row["next_receive_sequence"] == "" else int(row["next_receive_sequence"]),
                "slow_start": boolean(field(row, "slow_start")),
                "gentle_mode": boolean(field(row, "gentle_mode")),
                "gentle_draining": boolean(field(row, "gentle_draining")),
                "queue_building": boolean(field(row, "queue_building")),
                "drain_floor_binding": boolean(field(row, "drain_floor_binding")),
                "outage_recovery": boolean(field(row, "outage_recovery")),
                "no_response": optional_float(field(row, "no_response_for_us")),
                "no_progress": optional_float(field(row, "no_progress_for_us")),
                "stall": {"": 0.0, "no_response": 1.0, "no_progress": 2.0}.get(field(row, "stall_reason"), 0.0),
                "rto": optional_float(field(row, "retransmission_timeout_us")),
                "oldest_pipe_age": optional_float(field(row, "oldest_pipe_packet_age_us")),
                "max_rto_overdue": optional_float(field(row, "maximum_packet_rto_overdue_us")),
                "rto_postponements": optional_float(field(row, "rto_deadline_postponements")),
                "rtx_active": optional_float(field(row, "retransmission_active_packets")),
                "rtx_ready": optional_float(field(row, "retransmission_ready_packets")),
                "cc_control_rtt": optional_float(field(row, "congestion_control_rtt_us")),
                "cc_rtt_floor": optional_float(field(row, "congestion_rtt_floor_us")),
                "cc_queue_tolerance": optional_float(field(row, "congestion_queue_tolerance_us")),
                "cc_delivery_peak": optional_float(field(row, "congestion_delivery_peak_packets_per_second")),
                "cc_drain_floor": optional_float(field(row, "congestion_drain_floor_packets_per_second")),
                "cc_drain_target": optional_float(field(row, "congestion_drain_target_packets_per_second")),
                "cc_rate_samples": optional_float(field(row, "congestion_rate_samples")),
                "cc_probe_decisions": optional_float(field(row, "congestion_bandwidth_probe_decisions")),
                "cc_probe_increases": optional_float(field(row, "congestion_bandwidth_probe_increases")),
                "cc_probe_before_feedback": optional_float(field(row, "congestion_bandwidth_probe_before_feedback")),
                "cc_last_probe_interval": optional_float(field(row, "congestion_last_bandwidth_probe_interval_us")),
                "cc_delay_drains": optional_float(field(row, "congestion_delay_drains")),
            }
        )
    raw_rtt_points = [
        (timeline_seconds(row, manifest), float(row["raw_rtt_us"]) / 1000.0)
        for row in rtp_rows if field(row, "raw_rtt_us") != ""
    ]
    raw_rtt_ms = sorted(value for _, value in raw_rtt_points)
    rtt_series = [
        ("raw RTT", raw_rtt_points),
        ("smoothed RTT", [(row["time"], row["srtt"] / 1000.0) for row in rtp]),
        ("minimum RTT", [(row["time"], row["min_rtt"] / 1000.0) for row in rtp if row["min_rtt"] is not None]),
        ("current RTO", [(row["time"], row["rto"] / 1000.0) for row in rtp if row["rto"] is not None]),
        ("oldest pipe age", [(row["time"], row["oldest_pipe_age"] / 1000.0) for row in rtp if row["oldest_pipe_age"] is not None]),
        ("maximum stored-RTO overdue", [(row["time"], row["max_rto_overdue"] / 1000.0) for row in rtp if row["max_rto_overdue"] is not None]),
        ("controller RTT floor", [(row["time"], row["cc_rtt_floor"] / 1000.0) for row in rtp if row["cc_rtt_floor"] is not None]),
        ("controller queue gate", [(row["time"], row["cc_queue_tolerance"] / 1000.0) for row in rtp if row["cc_queue_tolerance"] is not None]),
    ]
    rate_series = [
        ("send rate", [(row["time"], row["send_rate"]) for row in rtp]),
        ("delivery rate", [(row["time"], row["delivery_rate"]) for row in rtp if row["delivery_rate"] is not None]),
        ("controller delivery peak", [(row["time"], row["cc_delivery_peak"]) for row in rtp if row["cc_delivery_peak"] is not None]),
        ("controller drain floor", [(row["time"], row["cc_drain_floor"]) for row in rtp if row["cc_drain_floor"] is not None]),
        ("controller drain target", [(row["time"], row["cc_drain_target"]) for row in rtp if row["cc_drain_target"] is not None]),
    ]
    congestion_series = [
        ("cwnd", [(row["time"], row["cwnd"]) for row in rtp]),
        ("pipe", [(row["time"], row["pipe"]) for row in rtp]),
        ("retransmitted", [(row["time"], row["rtx"]) for row in rtp]),
        ("retransmission active", [(row["time"], row["rtx_active"]) for row in rtp if row["rtx_active"] is not None]),
        ("retransmission ready", [(row["time"], row["rtx_ready"]) for row in rtp if row["rtx_ready"] is not None]),
        ("RTO deadline postponements", [(row["time"], row["rto_postponements"]) for row in rtp if row["rto_postponements"] is not None]),
    ]
    mode_series = [
        ("slow start", [(row["time"], float(row["slow_start"])) for row in rtp]),
        ("gentle", [(row["time"], float(row["gentle_mode"]) * 2.0) for row in rtp]),
        ("gentle drain", [(row["time"], float(row["gentle_draining"]) * 3.0) for row in rtp]),
        ("queue building", [(row["time"], float(row["queue_building"]) * 4.0) for row in rtp]),
        ("drain floor", [(row["time"], float(row["drain_floor_binding"]) * 5.0) for row in rtp]),
        ("outage recovery", [(row["time"], float(row["outage_recovery"]) * 6.0) for row in rtp]),
        ("watchdog expired", [(row["time"], row["stall"] * 3.5) for row in rtp]),
    ]
    liveness_series = [
        ("no response", [(row["time"], row["no_response"] / 1_000_000.0) for row in rtp if row["no_response"] is not None]),
        ("no progress", [(row["time"], row["no_progress"] / 1_000_000.0) for row in rtp if row["no_progress"] is not None]),
    ]
    send_stage_series = [
        (
            "pending send bytes",
            [
                (row["time"], row["pending_send_bytes"])
                for row in rtp
                if row["pending_send_bytes"] is not None
            ],
        ),
        (
            "send stage capacity",
            [
                (row["time"], row["send_stage_capacity_bytes"])
                for row in rtp
                if row["send_stage_capacity_bytes"] is not None
            ],
        ),
    ]
    loss_series = [
        ("estimated loss", [(row["time"], row["loss"] * 100.0) for row in rtp if row["loss"] is not None]),
        ("CC loss events", [(row["time"], row["cc_loss"] * 100.0) for row in rtp if row["cc_loss"] is not None]),
    ]
    action_series = [
        ("Last CC action", [(row["time"], row["cc_action"]) for row in rtp]),
    ]
    controller_rtt_series = [
        ("controller RTT", [(row["time"], row["cc_control_rtt"] / 1000.0) for row in rtp if row["cc_control_rtt"] is not None]),
        ("controller RTT floor", [(row["time"], row["cc_rtt_floor"] / 1000.0) for row in rtp if row["cc_rtt_floor"] is not None]),
        ("controller queue gate", [(row["time"], row["cc_queue_tolerance"] / 1000.0) for row in rtp if row["cc_queue_tolerance"] is not None]),
        ("probe interval", [(row["time"], row["cc_last_probe_interval"] / 1000.0) for row in rtp if row["cc_last_probe_interval"] is not None]),
    ]
    decision_count_series = [
        ("rate samples", [(row["time"], row["cc_rate_samples"]) for row in rtp if row["cc_rate_samples"] is not None]),
        ("probe decisions", [(row["time"], row["cc_probe_decisions"]) for row in rtp if row["cc_probe_decisions"] is not None]),
        ("probe increases", [(row["time"], row["cc_probe_increases"]) for row in rtp if row["cc_probe_increases"] is not None]),
        ("probe before feedback", [(row["time"], row["cc_probe_before_feedback"]) for row in rtp if row["cc_probe_before_feedback"] is not None]),
        ("delay drains", [(row["time"], row["cc_delay_drains"]) for row in rtp if row["cc_delay_drains"] is not None]),
    ]
    first_send_seq = rtp[0]["send_seq"] if rtp else 0
    first_recv_seq = next((row["recv_seq"] for row in rtp if row["recv_seq"] is not None), None)
    sequence_series = [
        ("sent", [(row["time"], (row["send_seq"] - first_send_seq) % (1 << 64)) for row in rtp]),
        ("received", [(row["time"], (row["recv_seq"] - first_recv_seq) % (1 << 64)) for row in rtp if row["recv_seq"] is not None and first_recv_seq is not None]),
    ]
    queue_series = []
    for direction in ("c2s", "s2c"):
        queue_series.append(
            (direction, [(timeline_seconds(row, manifest), float(row["queue_len"])) for row in netem_rows if row["direction"] == direction])
        )
    decisions_series = []
    for direction in ("c2s", "s2c"):
        rows = [row for row in netem_rows if row["direction"] == direction]
        decisions_series.extend(
            [
                (f"{direction} dropped", [(timeline_seconds(row, manifest), float(row["dropped"])) for row in rows]),
                (f"{direction} forwarded", [(timeline_seconds(row, manifest), float(row["forwarded"])) for row in rows]),
            ]
        )
    progress = [
        (timeline_seconds(row, manifest), float(row["delivered_bytes"]))
        for row in progress_rows
    ]
    delivered_series = [
        ("delivered", [(elapsed, delivered / (1024 * 1024)) for elapsed, delivered in progress])
    ]
    rolling_goodput = []
    left = 0
    for right, (elapsed, delivered) in enumerate(progress):
        while left < right and progress[left][0] < elapsed - 1.0:
            left += 1
        if left < right and elapsed > progress[left][0]:
            rolling_goodput.append(
                (elapsed, (delivered - progress[left][1]) / (elapsed - progress[left][0]) / (1024 * 1024))
            )

    stats = "No raw RTT samples."
    if raw_rtt_ms:
        stats = (
            f"n={len(raw_rtt_ms):,}; min={raw_rtt_ms[0]:.2f} ms; "
            f"p50={quantile(raw_rtt_ms, 0.5):.2f} ms; "
            f"p90={quantile(raw_rtt_ms, 0.9):.2f} ms; "
            f"p99={quantile(raw_rtt_ms, 0.99):.2f} ms; max={raw_rtt_ms[-1]:.2f} ms; "
            f"mean={statistics.fmean(raw_rtt_ms):.2f} ms."
        )
    manifest_rows = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
        for key, value in manifest.items()
    )
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>RTP performance trace</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; padding: 0 1rem; color: #172033 }}
h1 {{ margin-bottom: .25rem; }} section {{ margin: 2rem 0; }} svg {{ width: 100%; height: auto; border: 1px solid #d9dfeb; border-radius: 8px; }}
text {{ font-size: 11px; fill: #43506a; }} .plot-bg {{ fill: #fbfcff; }} .grid {{ stroke: #dfe5ef; stroke-width: 1; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: .35rem .5rem; vertical-align: top; }} th {{ width: 18rem; }}
.note {{ background: #f5f7fb; border-left: 4px solid #64748b; padding: .75rem 1rem; }}
</style></head><body>
<h1>RTP performance trace</h1><p>{html.escape(str(trace_dir / rtp_filename))}</p>
<section><h2>Capture metadata</h2><table>{manifest_rows}</table></section>
<section><h2>Raw RTT summary</h2><p>{html.escape(stats)}</p><p class="note">The plots expose distribution shape; the report deliberately does not label the data unimodal or multimodal automatically.</p></section>
{svg_line_chart("RTT over time", "elapsed time (s)", "RTT (ms)", rtt_series)}
{svg_histogram(raw_rtt_ms)}
{svg_cdf(raw_rtt_ms)}
{svg_line_chart("Controller RTT and probe timing", "elapsed time (s)", "RTT (ms)", controller_rtt_series)}
{svg_line_chart("Controller decision counts", "elapsed time (s)", "cumulative decisions", decision_count_series)}
{svg_line_chart("Congestion-control rates", "elapsed time (s)", "packets/s", rate_series)}
{svg_line_chart("Application delivery", "elapsed time (s)", "delivered MiB", delivered_series)}
{svg_line_chart("Rolling application goodput", "elapsed time (s)", "MiB/s over ~1 s", [("goodput", rolling_goodput)])}
{svg_line_chart("Congestion state", "elapsed time (s)", "packets", congestion_series)}
{svg_line_chart("Controller modes", "elapsed time (s)", "active lane (0 = inactive)", mode_series, (0.0, 7.0))}
{svg_line_chart("Congestion-control action", "elapsed time (s)", "action lane (1 reset, 2 censored, 3 slow start, 4 probe, 5 gentle, 6 drain, 7 loss, 8 huge loss)", action_series, (0.0, 8.0))}
{svg_line_chart("Peer liveness waits", "elapsed time (s)", "seconds", liveness_series)}
{svg_line_chart("RTP send staging", "elapsed time (s)", "bytes", send_stage_series)}
{svg_line_chart("Estimated packet loss", "elapsed time (s)", "loss (%)", loss_series)}
{svg_line_chart("RTP sequence progress", "elapsed time (s)", "packets since first sample", sequence_series)}
{svg_line_chart("Netem queue depth", "elapsed time (s)", "queued packets", queue_series)}
{svg_line_chart("Netem decisions", "elapsed time (s)", "cumulative packets", decisions_series)}
</body></html>"""
    output.write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--rtp-file",
        default="rtp.csv",
        help="endpoint trace within TRACE_DIR (for example rtp_peer.csv)",
    )
    args = parser.parse_args()
    output = args.out or args.trace_dir / "report.html"
    render_report(args.trace_dir, output, args.rtp_file)
    print(output)


if __name__ == "__main__":
    main()
