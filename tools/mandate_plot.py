#!/usr/bin/env python3
"""Render the per-mandate performance panels of the tri-mandate constitution.

The perf tests that measure **M1** (interactive tail latency), **M2**
(interactive delivery and wire amplification) and **M3** (bulk goodput) each
write two sibling files into one directory:

- ``<mandate>.json`` — the panel declaration: ``mandate``, ``title``,
  ``x_label``, ``y_label`` and a non-empty ``panels`` list. Each panel carries
  an ``id`` (which names its output file), a ``chart`` (``line``, ``cdf`` or
  ``bar``), a non-empty ``series`` list (each ``name``, optional ``role``), and
  a ``bounds`` list of horizontal reference lines (``{"y": …, "label": …}``).
  A panel whose axes differ from its siblings (a latency CDF beside a
  latency-over-time line) may override the mandate's ``x_label``/``y_label``.
  A series' optional ``role`` is producer metadata and changes no geometry;
  the ``chart`` and the series order decide how the panel is drawn.
- ``<mandate>.csv`` — the series data, header ``panel,series,x,y``, one row per
  plotted point.

The command::

    python3 tools/mandate_plot.py <mandate>.json --out <dir> [--browser <path>] [--no-rasterize]

It writes one verified SVG per declared panel into ``<out>``, prints
``panels: N`` and the written paths, and rasterizes every panel to a verified
PNG by default. The panel verification and the browser/PNG step are
``tools/render_graph.py``'s, imported rather than copied, so the mandate
panels cannot be held to a weaker standard than the paired loop's graphs.

**A panel that cannot be produced is an error, not an empty file to skim
past.** The tool exits non-zero, naming the problem, for a missing or
unreadable JSON/CSV, a malformed declaration field, a CSV with no data rows, a
declared panel or series with no rows, a CSV row naming an undeclared
panel/series (and the reverse), a malformed ``chart``, a non-numeric or
non-finite ``x``/``y``, a written SVG that carries no series geometry, and a
declared bound that did not make it into the SVG.

Two reading choices the input contract leaves open, decided here and made
loud instead of silent:

- The CSV always carries the *plotted* points, for every chart kind. A ``cdf``
  panel is drawn on a fixed 0-100% percentile axis and its ``y`` is therefore
  required to lie in ``[0, 100]``; the renderer does not derive a percentile
  from the ``x`` samples, so a producer that dumps raw samples fails loudly
  instead of plotting them. ``rtp_trace_report.svg_cdf``/``cdf_points`` remain
  available for deriving one.
- ``bounds`` are horizontal only (``y``). A mandate ceiling expressed on the
  plot's *x* axis (an M1 latency ceiling against a latency CDF) has no declared
  representation yet; the M1 declaration in the contract solves this by leaving
  the CDF panel's ``bounds`` empty and carrying the ceiling on the latency
  panel.
"""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import math
import re
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent


def _load_sibling(name):
    """Load a sibling tool by path, the way the other tools load each other.

    Loading by path rather than by import keeps the tool runnable and testable
    regardless of the process's ``sys.path``.
    """
    path = MODULE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDER = _load_sibling("render_graph")
REPORT = _load_sibling("rtp_trace_report")

CHARTS = ("line", "cdf", "bar")
CSV_COLUMNS = ("panel", "series", "x", "y")
PANEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LEGEND_COLUMNS = 4


class MandatePlotError(Exception):
    """A mandate-panel production failure that must surface as a non-zero exit."""


def _fail(message):
    raise MandatePlotError(message)


def _require_text(value, where):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a non-empty string")
    return value


def _require_number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{where} must be finite, got {value!r}")
    return number


def load_declaration(path):
    """Read and parse the panel declaration, naming an unreadable file."""
    path = Path(path)
    if not path.is_file():
        _fail(f"mandate declaration not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _fail(f"mandate declaration unreadable: {path}: {error}")
    except UnicodeDecodeError as error:
        _fail(f"mandate declaration is not UTF-8 text: {path}: {error}")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        _fail(f"mandate declaration is not valid JSON: {path}: {error}")
    if not isinstance(document, dict):
        _fail(f"mandate declaration must be a JSON object, got {type(document).__name__}: {path}")
    return document


def validate_declaration(document, path):
    """Check the declaration and return ``(mandate, title, x_label, y_label, panels)``."""
    mandate = _require_text(document.get("mandate"), f"{path}: mandate")
    title = _require_text(document.get("title"), f"{path}: title")
    for field in ("x_label", "y_label"):
        if not isinstance(document.get(field, ""), str):
            _fail(f"{path}: {field} must be a string when present")
    x_label = document.get("x_label", "")
    y_label = document.get("y_label", "")
    panels = document.get("panels")
    if not isinstance(panels, list) or not panels:
        _fail(
            f"{path}: panels must be a non-empty list; a mandate with no panel "
            "has no graph to read"
        )
    seen_ids = set()
    for index, panel in enumerate(panels):
        where = f"{path}: panels[{index}]"
        if not isinstance(panel, dict):
            _fail(f"{where} must be an object, got {type(panel).__name__}")
        identifier = _require_text(panel.get("id"), f"{where}.id")
        if not PANEL_ID_RE.match(identifier):
            _fail(
                f"{where}.id {identifier!r} must match {PANEL_ID_RE.pattern} so it "
                "can name this panel's output file"
            )
        if identifier in seen_ids:
            _fail(
                f"{where}.id {identifier!r} is declared twice; a CSV row could not "
                "say which of the two panels it belongs to"
            )
        seen_ids.add(identifier)
        if panel.get("chart") not in CHARTS:
            _fail(
                f"{where}.chart {panel.get('chart')!r} is not a chart; expected one "
                f"of {', '.join(CHARTS)}"
            )
        series = panel.get("series")
        if not isinstance(series, list) or not series:
            _fail(f"{where}.series must be a non-empty list")
        names = set()
        for series_index, entry in enumerate(series):
            entry_where = f"{where}.series[{series_index}]"
            if not isinstance(entry, dict):
                _fail(f"{entry_where} must be an object, got {type(entry).__name__}")
            name = _require_text(entry.get("name"), f"{entry_where}.name")
            if name in names:
                _fail(
                    f"{entry_where}.name {name!r} is declared twice; a CSV row "
                    "names a series, not one occurrence of it"
                )
            names.add(name)
            if entry.get("role") is not None and not isinstance(entry["role"], str):
                _fail(f"{entry_where}.role must be a string when present")
        # A mandate's own labels are the default; a panel whose axes differ
        # from its siblings (a latency CDF beside a latency-over-time line)
        # overrides them rather than mislabelling one of the two.
        for field in ("x_label", "y_label"):
            if field in panel and not isinstance(panel[field], str):
                _fail(f"{where}.{field} must be a string when present")
        bounds = panel.get("bounds") or []
        if not isinstance(bounds, list):
            _fail(f"{where}.bounds must be a list when present")
        for bound_index, bound in enumerate(bounds):
            bound_where = f"{where}.bounds[{bound_index}]"
            if not isinstance(bound, dict):
                _fail(f"{bound_where} must be an object, got {type(bound).__name__}")
            _require_number(bound.get("y"), f"{bound_where}.y")
            _require_text(bound.get("label"), f"{bound_where}.label")
    return mandate, title, x_label, y_label, panels


def load_rows(path):
    """Read the ``panel,series,x,y`` rows, refusing an empty or malformed file."""
    path = Path(path)
    if not path.is_file():
        _fail(f"mandate data CSV not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        _fail(f"mandate data CSV unreadable: {path}: {error}")
    except UnicodeDecodeError as error:
        _fail(f"mandate data CSV is not UTF-8 text: {path}: {error}")
    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        _fail(f"mandate data CSV is empty (no header row): {path}")
    header = [cell.strip() for cell in header]
    if tuple(header) != CSV_COLUMNS:
        _fail(
            f"mandate data CSV header must be {','.join(CSV_COLUMNS)}, got "
            f"{','.join(header) or '(nothing)'}: {path}"
        )
    rows = []
    for line_number, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(CSV_COLUMNS):
            _fail(
                f"mandate data CSV line {line_number} has {len(row)} field(s), "
                f"expected {len(CSV_COLUMNS)}: {path}"
            )
        rows.append((line_number, row[0].strip(), row[1].strip(), row[2], row[3]))
    if not rows:
        _fail(
            f"mandate data CSV has no data rows: {path} (an empty chart is not a "
            "graph)"
        )
    return rows


def parse_points(rows):
    """Group ``(panel, series) -> [(x, y)]``, refusing a non-numeric or non-finite cell."""
    points = {}
    for line_number, panel, series, x_cell, y_cell in rows:
        where = f"line {line_number} (panel {panel!r}, series {series!r})"
        try:
            x = float(x_cell)
            y = float(y_cell)
        except ValueError:
            _fail(
                f"mandate data CSV {where} has a non-numeric x/y: "
                f"x={x_cell!r} y={y_cell!r}"
            )
        if not (math.isfinite(x) and math.isfinite(y)):
            _fail(
                f"mandate data CSV {where} has a non-finite x/y: "
                f"x={x_cell!r} y={y_cell!r}"
            )
        points.setdefault((panel, series), []).append((x, y))
    return points


def reconcile(panels, points, csv_path):
    """Refuse any disagreement between the declared panels and the CSV rows."""
    declared = {
        (panel["id"], entry["name"])
        for panel in panels
        for entry in panel["series"]
    }
    problems = []
    for panel in panels:
        rows_for_panel = [pair for pair in points if pair[0] == panel["id"]]
        if not rows_for_panel:
            problems.append(
                f"panel {panel['id']!r} is declared but {csv_path} has no rows for "
                "it (an empty chart is not a graph)"
            )
            continue
        for entry in panel["series"]:
            if (panel["id"], entry["name"]) not in points:
                problems.append(
                    f"panel {panel['id']!r} declares series {entry['name']!r} but "
                    f"{csv_path} has no row for it"
                )
    declared_panel_ids = {panel["id"] for panel in panels}
    for pair in sorted(points):
        panel_id, series_name = pair
        if panel_id not in declared_panel_ids:
            problems.append(
                f"{csv_path} has a row for panel {panel_id!r}, which the "
                "declaration does not declare"
            )
        elif pair not in declared:
            problems.append(
                f"{csv_path} has a row for series {series_name!r} in panel "
                f"{panel_id!r}, which the declaration does not declare"
            )
    if problems:
        _fail(
            "the declaration and the data do not agree:\n  "
            + "\n  ".join(problems)
        )


def check_chart_domain(panel, points):
    """Refuse a point that cannot mean what its chart kind says it means."""
    if panel["chart"] != "cdf":
        return
    for entry in panel["series"]:
        for _, y in points[(panel["id"], entry["name"])]:
            if not 0.0 <= y <= 100.0:
                _fail(
                    f"panel {panel['id']!r} is a cdf: series {entry['name']!r} has "
                    f"y={y!r}, but a cdf y is a percentile on a 0-100 axis. Emit the "
                    "plotted percentile in y; the renderer does not derive it from "
                    "the x samples."
                )


def _bounds(panel):
    return [(float(bound["y"]), bound["label"]) for bound in panel.get("bounds") or []]


def svg_bar_chart(title, x_label, y_label, series, bounds=None):
    """Grouped bars for ``[(name, [(x, y)])]`` on a zero baseline.

    ``rtp_trace_report.svg_histogram`` buckets raw samples, which is a
    different contract from declared x/y points, so the bars are drawn here
    with the report's own axis constants, palette and extent helper.
    """
    series = [(name, points) for name, points in series if points]
    if not series:
        return f"<section><h2>{html.escape(title)}</h2><p>No samples.</p></section>"
    xs = [x for _, points in series for x, _ in points]
    x_min, x_max = min(xs), max(xs)
    if x_min == x_max:
        x_min, x_max = x_min - 0.5, x_max + 0.5
    y_min, y_max = REPORT.extent_including_bounds(REPORT.finite_extent(series), bounds)
    y_min = min(y_min, 0.0)
    y_max = max(y_max, 0.0)
    legend_columns = min(len(series), LEGEND_COLUMNS)
    legend_rows = math.ceil(len(series) / legend_columns)
    plot_top = REPORT.PAD_TOP + (legend_rows - 1) * 18
    plot_width = REPORT.WIDTH - REPORT.PAD_LEFT - REPORT.PAD_RIGHT
    plot_height = REPORT.HEIGHT - plot_top - REPORT.PAD_BOTTOM

    def sx(value):
        return REPORT.PAD_LEFT + (value - x_min) / (x_max - x_min) * plot_width

    def sy(value):
        return plot_top + (y_max - value) / (y_max - y_min) * plot_height

    distinct = sorted(set(xs))
    gaps = [after - before for before, after in zip(distinct, distinct[1:]) if after > before]
    slot = min(gaps) if gaps else (x_max - x_min)
    # One slot per distinct x holds that x's bars side by side. The slot is a
    # data-space measure, so it is converted to pixels before any placement;
    # using it directly would draw sub-pixel bars whenever the x values are
    # larger than the plot's pixel width.
    group_width = min((slot / (x_max - x_min)) * plot_width * 0.8, plot_width / 2)
    bar_width = group_width / len(series)
    baseline = sy(0.0)
    parts = [
        f"<section><h2>{html.escape(title)}</h2><svg viewBox=\"0 0 {REPORT.WIDTH} {REPORT.HEIGHT}\" role=\"img\">",
        f"<rect x=\"{REPORT.PAD_LEFT}\" y=\"{plot_top}\" width=\"{plot_width}\" height=\"{plot_height}\" class=\"plot-bg\"/>",
    ]
    for tick in range(6):
        fraction = tick / 5
        x_value = x_min + (x_max - x_min) * fraction
        x = sx(x_value)
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{plot_top}\" x2=\"{x:.1f}\" y2=\"{REPORT.HEIGHT - REPORT.PAD_BOTTOM}\" class=\"grid\"/>")
        parts.append(f"<text x=\"{x:.1f}\" y=\"{REPORT.HEIGHT - 24}\" text-anchor=\"middle\">{x_value:.2f}</text>")
        y_value = y_min + (y_max - y_min) * fraction
        y = sy(y_value)
        parts.append(f"<line x1=\"{REPORT.PAD_LEFT}\" y1=\"{y:.1f}\" x2=\"{REPORT.WIDTH - REPORT.PAD_RIGHT}\" y2=\"{y:.1f}\" class=\"grid\"/>")
        parts.append(f"<text x=\"{REPORT.PAD_LEFT - 9}\" y=\"{y + 4:.1f}\" text-anchor=\"end\">{y_value:.2f}</text>")
    for index, (name, points) in enumerate(series):
        color = REPORT.COLORS[index % len(REPORT.COLORS)]
        for x_value, y_value in points:
            # The group is centred on its x and then clamped to the plot, so
            # the first and last categories stay inside the canvas.
            left = sx(x_value) - group_width / 2
            left = min(
                max(left, REPORT.PAD_LEFT),
                REPORT.PAD_LEFT + plot_width - group_width,
            )
            left += index * bar_width
            top = sy(y_value)
            parts.append(f"<rect x=\"{left:.1f}\" y=\"{min(top, baseline):.1f}\" width=\"{bar_width * 0.9:.1f}\" height=\"{abs(top - baseline):.1f}\" fill=\"{color}\"/>")
    for y_value, label in bounds or []:
        y = sy(y_value)
        parts.append(f"<line class=\"bound\" x1=\"{REPORT.PAD_LEFT}\" y1=\"{y:.1f}\" x2=\"{REPORT.WIDTH - REPORT.PAD_RIGHT}\" y2=\"{y:.1f}\" stroke=\"{REPORT.BOUND_STROKE}\" stroke-width=\"1.4\" stroke-dasharray=\"6 4\"/>")
        parts.append(f"<text class=\"bound-label\" x=\"{REPORT.WIDTH - REPORT.PAD_RIGHT - 4}\" y=\"{y - 5:.1f}\" text-anchor=\"end\" style=\"{REPORT.BOUND_LABEL_STYLE}\">{html.escape(label)}</text>")
    parts.append(f"<text x=\"{REPORT.WIDTH / 2}\" y=\"{REPORT.HEIGHT - 5}\" text-anchor=\"middle\">{html.escape(x_label)}</text>")
    parts.append(f"<text x=\"18\" y=\"{REPORT.HEIGHT / 2}\" text-anchor=\"middle\" transform=\"rotate(-90 18 {REPORT.HEIGHT / 2})\">{html.escape(y_label)}</text>")
    parts.append("<g class=\"legend\">")
    for index, (name, _) in enumerate(series):
        column = index % legend_columns
        row = index // legend_columns
        x = REPORT.PAD_LEFT + column * (plot_width / legend_columns)
        y = 14 + row * 18
        color = REPORT.COLORS[index % len(REPORT.COLORS)]
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{y}\" x2=\"{x + 20:.1f}\" y2=\"{y}\" stroke=\"{color}\" stroke-width=\"4\"/>")
        parts.append(f"<text x=\"{x + 25:.1f}\" y=\"{y + 4}\">{html.escape(name)}</text>")
    parts.append("</g></svg></section>")
    return "".join(parts)


def panel_markup(title, x_label, y_label, panel, points):
    """Markup for one declared panel, as exactly one ``<svg>`` document span."""
    chart = panel["chart"]
    chart_title = f"{title} [{panel['id']}]"
    panel_x_label = panel.get("x_label", x_label)
    panel_y_label = panel.get("y_label", y_label)
    series = [
        (entry["name"], sorted(points[(panel["id"], entry["name"])]))
        for entry in panel["series"]
    ]
    bounds = _bounds(panel)
    if chart == "line":
        markup = REPORT.svg_line_chart(chart_title, panel_x_label, panel_y_label, series, None, bounds)
    elif chart == "cdf":
        markup = REPORT.svg_cdf_chart(chart_title, panel_x_label, panel_y_label, series, bounds)
    else:
        markup = svg_bar_chart(chart_title, panel_x_label, panel_y_label, series, bounds)
    panels = RENDER.extract_svg_panels(markup)
    if len(panels) != 1:
        _fail(
            f"panel {panel['id']!r} rendered {len(panels)} SVG documents; a "
            f"{chart!r} panel must render exactly one"
        )
    return panels[0]


def standalone(markup, title):
    """A self-contained SVG document carrying its own title."""
    document = RENDER.standalone_panel(markup, REPORT.WIDTH, REPORT.HEIGHT)
    marker = "</style>"
    index = document.find(marker)
    if index < 0:
        return document
    cut = index + len(marker)
    return document[:cut] + f"<title>{html.escape(title)}</title>" + document[cut:]


def render_mandate(declaration_path, out_dir, *, rasterize=True, browser=None):
    """Validate one mandate, write and verify its panels, and rasterize them.

    Raises MandatePlotError naming the problem for any invalid declaration or
    data file, any empty panel or undeclared series, any written SVG without
    series geometry, or any declared bound missing from the SVG; raises
    ``render_graph.RenderGraphError`` (naming the browser, or the offending
    PNG) when rasterization was requested and could not be verified.
    """
    declaration_path = Path(declaration_path)
    document = load_declaration(declaration_path)
    mandate, title, x_label, y_label, panels = validate_declaration(
        document, declaration_path
    )
    data_path = declaration_path.with_suffix(".csv")
    points = parse_points(load_rows(data_path))
    reconcile(panels, points, data_path)
    for panel in panels:
        check_chart_domain(panel, points)

    out_dir = Path(out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        _fail(f"--out is not a directory: {out_dir}")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _fail(f"--out cannot be created: {out_dir}: {error}")
    summary = {
        "declaration": str(declaration_path),
        "data": str(data_path),
        "mandate": mandate,
        "panels": len(panels),
        "series_counts": [],
        "svg": [],
        "png": [],
        "browser": None,
        "rasterized": False,
    }
    for panel in panels:
        panel_id = panel["id"]
        markup = panel_markup(title, x_label, y_label, panel, points)
        problems = RENDER.validate_panel(0, markup)
        if problems:
            _fail(
                f"panel {panel_id!r} cannot be used as evidence: "
                + "; ".join(problems)
            )
        svg_path = out_dir / f"{mandate}-{panel_id}.svg"
        try:
            svg_path.write_text(
                standalone(markup, f"{title} [{panel_id}]"), encoding="utf-8"
            )
            written = svg_path.read_text(encoding="utf-8")
        except OSError as error:
            _fail(f"panel {panel_id!r} could not be written to {svg_path}: {error}")
        count = RENDER.panel_series_count(written)
        if count <= 0:
            _fail(
                f"{svg_path} was written without series geometry (an empty chart "
                "is not a graph)"
            )
        bounds = panel.get("bounds") or []
        drawn = written.count('class="bound"')
        if drawn != len(bounds):
            _fail(
                f"{svg_path} draws {drawn} bound line(s) for {len(bounds)} declared "
                "bound(s); a bound that is not in the panel is not a bound"
            )
        missing = [
            bound["label"]
            for bound in bounds
            if html.escape(bound["label"]) not in written
        ]
        if missing:
            _fail(f"{svg_path} does not label every declared bound; missing {missing}")
        summary["series_counts"].append(count)
        summary["svg"].append(str(svg_path))

    if not rasterize:
        return summary
    rasterization = RENDER.rasterize_panels(
        summary["svg"], browser=browser, width=REPORT.WIDTH, height=REPORT.HEIGHT
    )
    summary["browser"] = rasterization["browser"]
    summary["png"] = rasterization["png"]
    summary["rasterized"] = True
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Render and verify the per-mandate performance panels declared by "
            "a mandate .json, then rasterize them to PNG."
        )
    )
    parser.add_argument(
        "declaration",
        type=Path,
        help="the mandate's .json panel declaration; its sibling .csv carries the data",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory the panel SVGs (and PNGs) are written into",
    )
    parser.add_argument(
        "--rasterize",
        dest="rasterize",
        action="store_true",
        default=True,
        help="rasterize the panels to PNG (default; fails if no browser is found)",
    )
    parser.add_argument(
        "--no-rasterize",
        dest="rasterize",
        action="store_false",
        help="verify and write SVGs only; do not attempt the external PNG step",
    )
    parser.add_argument(
        "--browser",
        default=None,
        help=f"headless browser executable or name (default: ${RENDER.BROWSER_ENV} or "
        "a known Chrome/Chromium path)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON summary of the produced panels",
    )
    args = parser.parse_args(argv)

    try:
        summary = render_mandate(
            args.declaration,
            args.out,
            rasterize=args.rasterize,
            browser=args.browser,
        )
    except (MandatePlotError, RENDER.RenderGraphError) as error:
        print(f"mandate_plot: error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"panels: {summary['panels']}")
        print(f"mandate: {summary['mandate']}")
        for path in summary["svg"]:
            print(f"svg: {path}")
        if summary["rasterized"]:
            for path in summary["png"]:
                print(f"png: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
