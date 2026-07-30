#!/usr/bin/env python3
"""Generate a self-contained HTML report from netem distribution CSVs."""

import argparse
import csv
import math
import os
import random
import statistics
import textwrap
from collections import OrderedDict

# ── CLI ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--dist-dir", default="tests/target/netem-dist")
parser.add_argument("--out", default="shape_report.html")
args = parser.parse_args()

# ── Helpers ───────────────────────────────────────────────────────────────────

def load(name):
    """Parse a CSV with columns 'series,value', preserving series order."""
    path = os.path.join(args.dist_dir, name)
    series_map = OrderedDict()
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                s = row["series"]
                series_map.setdefault(s, []).append(float(row["value"]))
    except FileNotFoundError:
        return {}
    for s in series_map:
        series_map[s].sort()
    return series_map

def quantile(sorted_samples, p):
    """Linear interpolation quantile (p in [0,1])."""
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac

def kde_log(samples, grid_min=-1, grid_max=2.5, n_grid=200):
    """Gaussian KDE in log10 space with Silverman bandwidth."""
    log_samples = [math.log10(max(v, 0.001)) for v in samples]
    n = len(log_samples)
    sd = statistics.stdev(log_samples) if n > 1 else 1.0
    h = max(1.06 * sd * (n ** -0.2), 0.01)
    grid = [grid_min + (grid_max - grid_min) * i / (n_grid - 1) for i in range(n_grid)]
    density = [0.0] * n_grid
    const = 1.0 / (math.sqrt(2 * math.pi) * h)
    for i, g in enumerate(grid):
        total = 0.0
        for v in log_samples:
            z = (g - v) / h
            total += math.exp(-0.5 * z * z)
        density[i] = const * total / n
    return grid, density

def ecdf(sorted_samples):
    """Return (x, y) pairs for the empirical CDF."""
    n = len(sorted_samples)
    xs = []
    ys = []
    for i, v in enumerate(sorted_samples):
        xs.append(v)
        ys.append(i / n)
    return xs, ys

def shift_function(a, b, n_resample=400, seed=11):
    """Shift function: quantile(b) - quantile(a) at p=1..99, with 95% bootstrap band."""
    rng = random.Random(seed)
    ps = [i / 100 for i in range(1, 100)]
    qa = [quantile(a, p) for p in ps]
    qb = [quantile(b, p) for p in ps]
    shifts = [qb[i] - qa[i] for i in range(len(ps))]

    # Bootstrap
    boot_shifts = []
    for _ in range(n_resample):
        ra = [rng.choice(a) for _ in a]
        rb = [rng.choice(b) for _ in b]
        ra.sort()
        rb.sort()
        bs = [quantile(rb, p) - quantile(ra, p) for p in ps]
        boot_shifts.append(bs)

    ci_low = []
    ci_high = []
    for i in range(len(ps)):
        vals = sorted(bs[i] for bs in boot_shifts)
        ci_low.append(vals[10])   # 2.5%
        ci_high.append(vals[-11]) # 97.5%

    return ps, shifts, ci_low, ci_high

def summary_table(series_map):
    """Return (headers, rows) for summary table."""
    headers = ["Series", "n", "Mean", "p50", "p90", "p99", "Max"]
    rows = []
    for name, samples in series_map.items():
        n = len(samples)
        mean = statistics.mean(samples)
        p50 = quantile(samples, 0.50)
        p90 = quantile(samples, 0.90)
        p99 = quantile(samples, 0.99)
        mx = samples[-1]
        rows.append((name, n, mean, p50, p90, p99, mx))
    return headers, rows

# ── Load data ─────────────────────────────────────────────────────────────────

csv_name = "rtp_mux_response_migration.csv"
series_map = load(csv_name)

# Ridgeline CSVs
ridgeline_csvs = [
    "dyn_single_mux__A_.csv",
    "dyn_dual_auto_small_first_B_.csv",
    "dyn_dual_auto_small_first_migrating_B_mig_.csv",
    "dyn_dual_auto_big_first_C_.csv",
    "dyn_dual_auto_big_first_migrating_C_mig_.csv",
    "dyn_dual_hint_static_E_.csv",
]
ridgeline_groups = OrderedDict()
for rc in ridgeline_csvs:
    data = load(rc)
    small_key = [k for k in data if "small" in k.lower()]
    if small_key:
        ridgeline_groups[rc.replace(".csv", "")] = data[small_key[0]]

# ── SVG generation helpers ────────────────────────────────────────────────────

def svg_axis(x_min, x_max, y_min, y_max, width, height, pad, x_label="", y_label="", log_x=False):
    """Return SVG elements for axes."""
    x0, y0 = pad, height - pad
    x1, y1 = width - pad, pad
    plot_w = x1 - x0
    plot_h = y0 - y1

    def scale_x(v):
        if log_x:
            return x0 + (math.log10(max(v, 0.001)) - math.log10(max(x_min, 0.001))) / (math.log10(max(x_max, 0.001)) - math.log10(max(x_min, 0.001))) * plot_w
        return x0 + (v - x_min) / (x_max - x_min) * plot_w

    def scale_y(v):
        return y0 - (v - y_min) / (y_max - y_min) * plot_h

    lines = []
    lines.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="currentColor"/>')
    lines.append(f'<line x1="{x0}" y1="{y1}" x2="{x0}" y2="{y0}" stroke="currentColor"/>')

    # X ticks (log or linear)
    if log_x:
        ticks = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
        ticks = [t for t in ticks if x_min <= t <= x_max]
    else:
        n_ticks = 5
        step = (x_max - x_min) / n_ticks
        ticks = [x_min + step * i for i in range(n_ticks + 1)]

    for t in ticks:
        sx = scale_x(t)
        lines.append(f'<line x1="{sx}" y1="{y0}" x2="{sx}" y2="{y0 + 4}" stroke="currentColor"/>')
        label = f"{t:.0f}" if t >= 1 else f"{t:.1f}"
        lines.append(f'<text x="{sx}" y="{y0 + 14}" text-anchor="middle" font-size="10" fill="currentColor">{label}</text>')

    # Y ticks
    n_yticks = 5
    y_step = (y_max - y_min) / n_yticks
    for i in range(n_yticks + 1):
        ty = y_min + y_step * i
        sy = scale_y(ty)
        lines.append(f'<line x1="{x0 - 4}" y1="{sy}" x2="{x0}" y2="{sy}" stroke="currentColor"/>')
        lines.append(f'<text x="{x0 - 6}" y="{sy + 3}" text-anchor="end" font-size="10" fill="currentColor">{ty:.2f}</text>')

    # Labels
    if x_label:
        lines.append(f'<text x="{(x0 + x1) / 2}" y="{height - 4}" text-anchor="middle" font-size="12" fill="currentColor">{x_label}</text>')
    if y_label:
        lines.append(f'<text x="{pad - 4}" y="{(y0 + y1) / 2}" text-anchor="middle" font-size="12" fill="currentColor" transform="rotate(-90,{pad - 4},{(y0 + y1) / 2})">{y_label}</text>')

    return "\n".join(lines), scale_x, scale_y

def polyline(points, scale_x, scale_y, color, stroke_width=1.5, dash=""):
    pts = " ".join(f"{scale_x(x)},{scale_y(y)}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{stroke_width}"{dash_attr}/>'

def fill_between(xs, y_low, y_high, scale_x, scale_y, color, opacity=0.15):
    pts = " ".join(f"{scale_x(x)},{scale_y(y_low[i])}" for i, x in enumerate(xs))
    pts += " "
    pts += " ".join(f"{scale_x(x)},{scale_y(y_high[i])}" for i, x in enumerate(reversed(xs)))
    return f'<polygon points="{pts}" fill="{color}" opacity="{opacity}"/>'

# ── Build figures ─────────────────────────────────────────────────────────────

figures = []

COLORS = {"pinned": "#e74c3c", "migrating": "#2980b9", "small": "#27ae60"}
W = 600
H = 350
PAD = 55

if series_map:
    # Figure 1: Density overlay (log-ms)
    all_vals = []
    for k, v in series_map.items():
        all_vals.extend(v)
    all_min = max(min(all_vals), 0.1)
    all_max = max(all_vals)

    fig1_parts = [f'<div class="figure"><h3>Figure 1: Density (log scale)</h3><svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">']
    ax, sx, sy = svg_axis(all_min, all_max, 0, 1, W, H, PAD, x_label="RTT (ms)", y_label="Density", log_x=True)
    fig1_parts.append(ax)

    for k in series_map:
        grid, dens = kde_log(series_map[k])
        grid_vals = [10 ** g for g in grid]
        fig1_parts.append(polyline(list(zip(grid_vals, dens)), sx, sy, COLORS.get(k, "#888")))
        # Label at peak
        peak_idx = dens.index(max(dens))
        fig1_parts.append(f'<text x="{sx(grid_vals[peak_idx])}" y="{sy(dens[peak_idx]) - 4}" text-anchor="middle" font-size="11" fill="{COLORS.get(k, "#888")}">{k}</text>')

    fig1_parts.append('</svg></div>')
    figures.append("\n".join(fig1_parts))

    # Figure 2: Empirical CDF
    fig2_parts = [f'<div class="figure"><h3>Figure 2: Empirical CDF</h3><svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">']
    ax, sx, sy = svg_axis(all_min, all_max, 0, 1, W, H, PAD, x_label="RTT (ms)", y_label="Cumulative probability", log_x=True)
    fig2_parts.append(ax)
    for k in series_map:
        xs, ys = ecdf(series_map[k])
        fig2_parts.append(polyline(list(zip(xs, ys)), sx, sy, COLORS.get(k, "#888")))
        # Label at right
        fig2_parts.append(f'<text x="{sx(xs[-1])}" y="{sy(ys[-1]) - 4}" text-anchor="middle" font-size="11" fill="{COLORS.get(k, "#888")}">{k}</text>')
    fig2_parts.append('</svg></div>')
    figures.append("\n".join(fig2_parts))

    # Figure 3: Shift function
    if len(series_map) == 2:
        keys = list(series_map.keys())
        a, b = series_map[keys[0]], series_map[keys[1]]
        ps, shifts, ci_low, ci_high = shift_function(a, b)

        shift_min = min(min(ci_low), min(shifts), 0)
        shift_max = max(max(ci_high), max(shifts), 0)
        margin = (shift_max - shift_min) * 0.1 or 1
        shift_min -= margin
        shift_max += margin

        fig3_parts = [f'<div class="figure"><h3>Figure 3: Shift function ({keys[1]} − {keys[0]})</h3><svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">']
        ax, sx, sy = svg_axis(0, 1, shift_min, shift_max, W, H, PAD, x_label="Percentile", y_label="Difference (ms)")
        fig3_parts.append(ax)

        p_vals = [p * 100 for p in ps]
        # Bootstrap band
        fig3_parts.append(fill_between(p_vals, ci_low, ci_high, sx, sy, "#888", 0.2))
        # Zero line
        fig3_parts.append(f'<line x1="{sx(0)}" y1="{sy(0)}" x2="{sx(100)}" y2="{sy(0)}" stroke="#999" stroke-dasharray="4,4"/>')
        # Shift line
        fig3_parts.append(polyline(list(zip(p_vals, shifts)), sx, sy, "#e67e22", 2))
        fig3_parts.append('</svg></div>')
        figures.append("\n".join(fig3_parts))

# Figure 4: Ridgeline
if ridgeline_groups:
    n_rows = len(ridgeline_groups)
    row_h = 80
    total_h = n_rows * row_h + PAD
    ridgeline_parts = [f'<div class="figure"><h3>Figure 4: Ridgeline (small-series distributions)</h3><svg width="{W}" height="{total_h}" xmlns="http://www.w3.org/2000/svg">']

    all_r_vals = []
    for v in ridgeline_groups.values():
        all_r_vals.extend(v)
    r_min = max(min(all_r_vals), 0.1)
    r_max = max(all_r_vals)

    ax_y_min = 0
    ax_y_max = 1
    _, sx, _ = svg_axis(r_min, r_max, ax_y_min, ax_y_max, W, total_h, PAD, x_label="RTT (ms)", log_x=True)

    for idx, (name, samples) in enumerate(ridgeline_groups.items()):
        y_center = total_h - PAD - idx * row_h
        grid, dens = kde_log(samples)
        grid_vals = [10 ** g for g in grid]
        max_d = max(dens) or 1
        norm_dens = [d / max_d * row_h * 0.6 for d in dens]

        pts = []
        for i, gv in enumerate(grid_vals):
            pts.append((gv, y_center + norm_dens[i]))
        for i in range(len(grid_vals) - 1, -1, -1):
            pts.append((grid_vals[i], y_center))
        pts_str = " ".join(f"{sx(p[0])},{p[1]}" for p in pts)
        ridgeline_parts.append(f'<polygon points="{pts_str}" fill="#3498db" opacity="0.5"/>')
        # p50 label
        p50 = quantile(samples, 0.50)
        ridgeline_parts.append(f'<text x="{sx(p50)}" y="{y_center - 4}" text-anchor="middle" font-size="10" fill="currentColor">p50={p50:.1f}</text>')
        ridgeline_parts.append(f'<text x="{PAD - 4}" y="{y_center + 3}" text-anchor="end" font-size="10" fill="currentColor">{name}</text>')

    ridgeline_parts.append('</svg></div>')
    figures.append("\n".join(ridgeline_parts))

# ── Summary table ─────────────────────────────────────────────────────────────

all_series = OrderedDict()
if series_map:
    for k, v in series_map.items():
        all_series[k] = v
for k, v in ridgeline_groups.items():
    all_series[k] = v

headers, rows = summary_table(all_series)
table_rows = []
for r in rows:
    vals = [r[0], str(r[1])]
    for v in r[2:]:
        vals.append(f"{v:.2f}")
    table_rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")

table_html = f"""<details>
<summary>Summary table</summary>
<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;width:100%">
<thead><tr>{"".join(f"<th>{h}</th>" for h in headers)}</tr></thead>
<tbody>{"".join(table_rows)}</tbody>
</table>
</details>"""

# ── Assemble page ────────────────────────────────────────────────────────────

page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Netem shape report</title>
<style>
:root {{ color-scheme: light; --bg: #fff; --fg: #222; }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg: #1e1e1e; --fg: #ddd; }}
@media (prefers-color-scheme: dark) {{
:root {{ --bg: #1e1e1e; --fg: #ddd; }}
}}
body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--fg); max-width: 720px; margin: 2em auto; padding: 0 1em; }}
h2 {{ margin-top: 0; }}
.figure {{ margin: 1.5em 0; }}
svg {{ display: block; margin: 0 auto; background: var(--bg); }}
details {{ margin: 1em 0; }}
summary {{ cursor: pointer; font-weight: bold; }}
table {{ font-size: 0.85em; }}
th, td {{ text-align: right; padding: 2px 8px; }}
th:first-child, td:first-child {{ text-align: left; }}
.footer {{ margin-top: 2em; font-size: 0.85em; color: #888; }}
</style>
</head>
<body>
<h2>Netem shape report</h2>
{"".join(figures)}
{table_html}
<div class="footer">CSV directory: {args.dist_dir}</div>
<script>
if (window.matchMedia('(prefers-color-scheme: dark)').matches) {{
document.documentElement.setAttribute('data-theme', 'dark');
}}
</script>
</body>
</html>"""

with open(args.out, "w") as f:
    f.write(page)

path = os.path.abspath(args.out)
size = os.path.getsize(args.out)
print(f"wrote {path} {size} bytes")
