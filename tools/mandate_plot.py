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

**A panel that cannot show the failure it is drawn for is refused, not
rendered.** `AGENTS.md` ("Read every panel") makes that a defect of the same
family as an assertion that cannot fail, and two panels of the run that became
`rtp_mux v0.0.22` were drawn that way (`AUDIT_COVERAGE.md`, "Plots that cannot
show their own failure"). Three checks enforce it here, and none can be
silenced by softening a declaration:

- **the axis test** — a bar panel's axis is chosen by `bar_axis_extent`, and
  then measured: a bound the panel draws at its own scale needs a band of at
  least `MIN_BOUND_PIXELS` of the axis height, because a departure of the size
  the bound exists to catch must not be sub-pixel. The delivery floors failed
  it: over `0..2` for a `0..1` quantity, the M4 floor's 0.5 % band was 1.1
  pixels. A panel that fails is an error naming the axis and the bound.
- **the governance test** — a bound drawn across a bar panel whose bars split
  around it (an outlier split: at most a third of them beyond it, the rest not)
  is a departure whose meaning lives in *the run*: an arm's own guard may
  tolerate what the mandate bound forbids. The run's own per-arm guard
  measurements (`*_guard` keys of the `MANDATE` line, passed in as `run_values`
  by `tools/mandate-check`) are therefore named on the line, and a render that
  is *not* given the run's measurements, for a bound a minority of the bars
  crosses, is refused: without them the label would read as a breach the verdict
  tolerates (the M2 wire budget line across the hostile and lone-tail arms). A
  declaration may state its own governance instead with `bounds[i].series`
  and/or `bounds[i].x` (which also clips the drawn line to the x-window it
  governs). A crossing the run asserts nothing loosely against — a per-flow
  delivery floor with no guard — stands as the breach it draws and is not
  refused: the evidence survives a failing run.
- **the label-fit test** — the bound label is the part of the panel that says
  what its line governs, and `check_label_fit` reads every drawn label back out
  of the SVG and refuses the render when its box leaves the plot area. The
  measured run had three panels whose label began 4.6-14.7 px *above* the plot
  (`M2-delivery`, `M4-imbalance`, `M4-shares`, whose bounds sit at the top of a
  band view), and a bound governing a narrow x-window drew its label off the
  plot's left edge. Labels are therefore laid out in pixels by
  `rtp_trace_report.layout_bound_label`: wrapped to the plot's width, placed
  below the line when there is no room above it, and anchored at the line's own
  end only as far as the text allows. The fit is determined by a *model* of the
  text width (`rtp_trace_report.label_text_width`, an upper bound over the
  fonts a browser resolves for the panel's 11px style, pinned in the tests to
  widths real Chrome measured), so a font wider than that bound is outside what
  it can catch; the vertical extent needs no width and is caught whatever font
  draws it.

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

# -- the bound-label geometry of a drawn panel ----------------------------
#
# `check_label_fit` reads the layout back out of the markup rather than from
# the plotter's own state, so it measures the artifact that is written: the
# anchor each label is drawn at and the plot rectangle it is drawn in.
PLOT_BG_RE = re.compile(
    r'<rect x="([-0-9.]+)" y="([-0-9.]+)" width="([-0-9.]+)" height="([-0-9.]+)" '
    r'class="plot-bg"'
)
BOUND_LABEL_RE = re.compile(r'<text class="bound-label"([^>]*)>(.*?)</text>', re.S)
BOUND_LABEL_TITLE_RE = re.compile(r"<title>.*?</title>", re.S)
TEXT_ATTRIBUTE_RE = re.compile(r'([A-Za-z][\w-]*)="([^"]*)"')

# -- the bar-panel axis policy --------------------------------------------
#
# A bar panel's y axis is *chosen*, not inherited from the data's min/max,
# because the choice decides whether the panel can show its own bound.
MIN_UNIT_SPAN = 0.01
"""The smallest span a fraction panel (`[0, 1]` quantity) may be drawn over.

Without it, a quantity pinned at its ideal has no span to draw, and the zoom
would amplify sampling noise into an apparent departure.
"""

MIN_BOUND_PIXELS = 6.0
"""The least height, in pixels, a one-sided bound's own region must be drawn in.

`AGENTS.md`'s test is "would a regression be visible at this scale?": the
region around a one-sided bound is the only place its failure can show, and
`AUDIT_COVERAGE.md` records the delivery panels failing it at half a pixel.
Six pixels is three line widths — a step, not a smudge — and it is measured in
pixels rather than as a share of the axis because the defect it catches is
sub-*pixel*: a floor 19 % of the way up an axis is not the same defect as one
0.5 % of the way up.
"""

FRAME_HEADROOM = 0.05
"""The share of the span kept between the frame and the nearest datum/bound."""

CROSSING_BULK_SHARE = 1.0 / 3.0
"""Above this share on the far side, a split is a target, not a crossing.

A bound the bars split around evenly (the fair share of `M4-shares`) is a
value the bars are expected to sit at, and no attribution of it is owed; a
bound with at most a third of the bars beyond it (`M2-wire`'s lone 6.6x bar
against the 6x budget) is read as a departure, and the panel must say what the
departure is asserted against.
"""

GUARD_KEY_SUFFIX = "_guard"
"""The `MANDATE` line's per-arm guard measurements: `hostile_wire_guard=10`."""

BAR_BOUND_LABEL_STYLE = (
    REPORT.BOUND_LABEL_STYLE + ";stroke:#ffffff;stroke-width:3;paint-order:stroke"
)
"""A bar panel's bound label, haloed white.

The label names what the line governs now, so it is longer than the bound's own
name and usually lands across the bars it describes; the halo keeps it legible
there instead of asking the reader to decode dark text on a saturated bar.
"""


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


def _require_extent(value, where):
    """A pinned `[low, high]` axis, or ``None`` when not declared."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail(f"{where} must be a two-element [low, high] list when present")
    low = _require_number(value[0], f"{where}[0]")
    high = _require_number(value[1], f"{where}[1]")
    if not low < high:
        _fail(f"{where} must be [low, high] with low < high, got {value!r}")
    return (low, high)


def bound_governed_x(bound):
    """The x-window a bound declares it governs, or ``None`` for the panel's own.

    ``x`` is one category, a list of categories, or ``{"min", "max"}``; the
    categories themselves are checked against the CSV's own x values when the
    panel is rendered, because the declaration does not carry them.
    """
    governed = bound.get("x")
    if governed is None:
        return None
    if isinstance(governed, dict):
        low = _require_number(governed.get("min"), "bound.x.min")
        high = _require_number(governed.get("max"), "bound.x.max")
        if not low <= high:
            _fail(f"bound.x must be min <= max, got {governed!r}")
        return (low, high)
    if isinstance(governed, (int, float)) and not isinstance(governed, bool):
        value = float(governed)
        return (value, value)
    if isinstance(governed, (list, tuple)) and governed:
        values = [_require_number(item, "bound.x[]") for item in governed]
        return (min(values), max(values))
    _fail(
        "bound.x must be a number, a non-empty list of numbers, or "
        f"{{'min': .., 'max': ..}}, got {governed!r}"
    )


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
            if bound.get("series") is not None:
                _require_text(bound["series"], f"{bound_where}.series")
            bound_governed_x(bound)
        _require_extent(panel.get("y_extent"), f"{where}.y_extent")
        if panel.get("chart") == "cdf" and panel.get("y_extent") is not None:
            _fail(
                f"{where}.y_extent cannot pin a cdf panel: its axis is the fixed "
                "0-100 percentile axis the CSV's y is already carried on"
            )
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


def _bound_specs(panel):
    """The panel's declared bounds as dicts with a numeric ``y``."""
    specs = []
    for bound in panel.get("bounds") or []:
        spec = dict(bound)
        spec["y"] = float(bound["y"])
        specs.append(spec)
    return specs


def _bound_values(series):
    return [value for _, points in series for _, value in points]


def unit_span(values, bounds):
    """The quantity's unit (1.0) when every value and bound lies in ``[0, 1]``.

    A panel of fractions of a whole — a delivery ratio, a share, a fraction of
    the shaped clock — has a natural top. Detecting it is what lets the axis
    policy below tell a floor at the top of that unit from a floor in the
    middle of a range, which is the difference between `M4-delivery` (whose
    bound *is* the top) and `M3-fraction` (whose bound has half the axis between
    it and the data).
    """
    if not values:
        return None
    if all(0.0 <= value <= 1.0 for value in values) and all(
        0.0 <= y <= 1.0 for y in bounds
    ):
        return 1.0
    return None


def bound_side(values, y):
    """``floor``/``cap`` when every value is on one side of ``y``, else ``None``.

    A bound the values straddle is a *target* the panel is read against (a fair
    share, a symmetric departure band), not a line a bar can fail, so it is
    drawn as it stands and owes the axis test nothing.
    """
    if not values:
        return None
    if all(value >= y for value in values):
        return "floor"
    if all(value <= y for value in values):
        return "cap"
    return None


def bound_band(values, y, unit):
    """The width of the region the axis must resolve for a one-sided bound.

    The nearest value on the bound's own side is how close a failing departure
    begins; a bound sitting at its ideal has no such distance at all, and there
    the unit's own resolution floor (`MIN_UNIT_SPAN`) is what the axis has to
    show in its place — `M2`'s delivery floor is `1.000`, so nothing below it is
    tolerated and the axis still has to make a small loss visible.
    """
    nearest = min((abs(value - y) for value in values), default=0.0)
    return max(nearest, MIN_UNIT_SPAN * unit if unit else 0.0)


def crossing_values(values, y):
    """The values beyond ``y`` that the panel reads as a departure.

    Empty unless at most `CROSSING_BULK_SHARE` of the bars are beyond the
    bound: a bound the bars split around evenly is the value they are expected
    to sit at (`M4-shares`' fair share), while a lone bar past the line is a
    crossing whose attribution the panel owes its reader (`M2-wire`).
    """
    below = [value for value in values if value < y]
    above = [value for value in values if value > y]
    total = len(below) + len(above)
    if not total:
        return []
    if len(above) <= CROSSING_BULK_SHARE * total:
        return above
    if len(below) <= CROSSING_BULK_SHARE * total:
        return below
    return []


def run_guards(run_values, series=None, text=""):
    """The run's own per-arm guards, from the `MANDATE` line's `*_guard` keys.

    Only a guard about *this* panel's quantity is named: the guard's last
    underscore token has to appear in one of the panel's series names or in the
    text the bound carries, so `hostile_wire_guard` is named on the wire panel
    while a `hostile_p99_guard` is not named on a delivery panel whose series
    are `clean`/`hostile`. A guard named against the wrong quantity would be
    worse than no attribution: it would look like evidence.
    """
    if not isinstance(run_values, dict):
        return []
    haystack = [name for name, _ in series or []] + [text]
    guards = []
    for key in sorted(run_values):
        value = run_values[key]
        if not isinstance(key, str) or not key.endswith(GUARD_KEY_SUFFIX):
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if series is not None:
            token = key[: -len(GUARD_KEY_SUFFIX)].rsplit("_", 1)[-1]
            if token and not any(token in entry for entry in haystack):
                continue
        guards.append((key, float(value)))
    return guards


def bar_plot_height(series_count):
    """The pixel height of a bar panel's plot area, as `svg_bar_chart` lays it out."""
    legend_columns = min(max(series_count, 1), LEGEND_COLUMNS)
    legend_rows = math.ceil(max(series_count, 1) / legend_columns)
    return REPORT.HEIGHT - (REPORT.PAD_TOP + (legend_rows - 1) * 18) - REPORT.PAD_BOTTOM


def bound_is_the_scale(values, y, unit):
    """Whether this bound is what the panel's axis has to resolve.

    Two shapes: a **floor at the top of the unit**, where the quantity's ideal
    is the bound itself or a hair below it (`M2`/`M4` delivery), and a
    **crossing**, where a minority of the bars has passed the bound and the
    band around it is the whole reading (`M2-wire`'s lone bar past the budget).
    A bound the bars split around evenly is a target (`M4-shares`), and an
    untouched bound far from every bar is not the panel's scale either.
    """
    if bound_side(values, y) == "floor" and unit is not None:
        if unit - y <= FRAME_HEADROOM * unit:
            return True
    return bool(crossing_values(values, y))


def bar_axis_extent(series, bounds):
    """The y extent for one bar panel, as ``(low, high)``.

    Two shapes, and the choice between them is the whole point:

    - **the band view**, for a fraction of a unit whose floor sits at the top of
      that unit (within `FRAME_HEADROOM`). The zero baseline is meaningless
      there — a delivery ratio cannot fail towards zero — and it is exactly what
      left `M4-delivery`'s 0.5 % floor band at half a pixel of a 0..2 axis. The
      axis is drawn around the floor instead: as far below it as its own band is
      wide (never less than `MIN_UNIT_SPAN` of the unit), so the floor splits the
      plot into the region it tolerates and the region it fails.
    - **the zero baseline**, every other bar panel's shape, because there the
      bar's length from zero is the reading. Unchanged from the data-driven
      extent of `rtp_trace_report`.
    """
    values = _bound_values(series)
    ys = [bound["y"] for bound in bounds]
    unit = unit_span(values, ys)
    if unit is not None:
        for bound in bounds:
            if bound_is_the_scale(values, bound["y"], unit):
                band = bound_band(values, bound["y"], unit)
                span = 2.0 * band
                headroom = FRAME_HEADROOM * span
                return (unit - span - headroom, unit + headroom)
    low, high = REPORT.extent_including_bounds(
        REPORT.finite_extent(series), [(y, "") for y in ys]
    )
    return (min(low, 0.0), max(high, 0.0))


def check_panel_axis(panel_id, series, bounds, extent, plot_height=None):
    """Problems that make an axis unable to show a bound drawn on it.

    This is `AGENTS.md`'s first panel test — "would a regression be visible at
    this scale?" — as a measurement: the band around a bound the panel draws as
    its scale must be at least `MIN_BOUND_PIXELS` tall, or the failure the bound
    exists to catch is sub-pixel. The delivery panels failed it at 0.5 %, which
    is why `bar_axis_extent` draws them as a band view now; a pinned `y_extent`
    that reintroduces the failure is refused by name.
    """
    problems = []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    values = _bound_values(series)
    unit = unit_span(values, [bound["y"] for bound in bounds])
    for bound in bounds:
        y = bound["y"]
        if bound_side(values, y) is None and not crossing_values(values, y):
            continue
        band = bound_band(values, y, unit)
        pixels = band / span * plot_height
        if pixels < MIN_BOUND_PIXELS:
            problems.append(
                f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} leaves the "
                f"bound {bound['label']!r} (y={y:g}) a band of {band:.4g} "
                f"({band / span:.1%} of its height, {pixels:.1f} px of "
                f"{plot_height:.0f}), under the {MIN_BOUND_PIXELS:.0f} px a bound "
                "needs to show the departure it exists to catch, so that "
                "departure would be sub-pixel"
            )
    return problems


def check_bound_governance(panel_id, series, bounds, run_values):
    """Problems that make a crossed bound unreadable: nothing says what governs it.

    `AGENTS.md`'s second panel test — "does each drawn bound apply to every
    series it crosses?" — as a measurement. A bound a minority of the bars sit
    beyond is read as a departure, and whether that departure is a breach or an
    arm's tolerated tripwire is a property of *the run*, held in the run's own
    per-arm guards. The panel therefore needs the run's measurements: without
    them it must not guess, and the render is refused rather than drawn so a
    reader cannot conclude a breach the verdict tolerates (the `M2-wire`
    defect).

    A crossing is *not* refused when the run's measurements are supplied but
    hold no guard for this panel's quantity: a mandate bound nothing is asserted
    loosely against (a per-flow delivery floor) then stands as the breach it
    draws, and the panel keeps its evidence.
    """
    problems = []
    values = _bound_values(series)
    for bound in bounds:
        crossing = crossing_values(values, bound["y"])
        if not crossing:
            continue
        if bound.get("series") or bound.get("x"):
            continue
        if run_values is not None:
            continue
        problems.append(
            f"panel {panel_id!r} draws the bound {bound['label']!r} "
            f"(y={bound['y']:g}) with {len(crossing)} of {len(values)} bar(s) "
            "beyond it, and the run's own measurements were not supplied, so the "
            "panel cannot say whether that crossing is a breach or an arm's "
            "tolerated guard; pass the run's MANDATE values (tools/mandate-check "
            "does) or declare which series the bound governs with "
            "bounds[].series / bounds[].x"
        )
    return problems


def check_bound_x_categories(panel_id, series, bounds):
    """Every x a bound declares it governs must be a category the panel draws."""
    problems = []
    categories = sorted({x for _, points in series for x, _ in points})
    names = {name for name, _ in series}
    for bound in bounds:
        window = bound_governed_x(bound)
        if window is not None:
            low, high = window
            governed = [x for x in categories if low <= x <= high]
            if not governed:
                problems.append(
                    f"panel {panel_id!r}: the bound {bound['label']!r} declares it "
                    f"governs x={low:g}..{high:g}, and the panel draws no category "
                    f"there (its categories are {categories})"
                )
            elif len(governed) < len(categories) and (
                low != min(governed) or high != max(governed)
            ):
                problems.append(
                    f"panel {panel_id!r}: the bound {bound['label']!r} declares it "
                    f"governs x={low:g}..{high:g}, which is not a boundary between "
                    f"the panel's categories {categories}"
                )
        if bound.get("series") and bound["series"] not in names:
            problems.append(
                f"panel {panel_id!r}: the bound {bound['label']!r} declares it "
                f"governs series {bound['series']!r}, which the panel does not "
                f"declare (its series are {sorted(names)})"
            )
    return problems


def axis_label(y_label, extent):
    """The y label, plus a note when the axis is a truncated band view.

    A bar's length is a claim about its value, so an axis that does not start
    at zero has to say so on its own face; the ticks alone leave the reader to
    notice, and the figure is read long after the tick range is.
    """
    low, high = extent
    if low > 0.0:
        decimals = max(2, math.ceil(-math.log10(high - low)) + 2)
        return (
            f"{y_label} [band view {low:.{decimals}g}..{high:.{decimals}g}, "
            "not 0-based]"
        )
    return y_label


def panel_plot_rect(panel_id, markup):
    """The ``(left, top, right, bottom)`` rectangle a panel's data is drawn in."""
    match = PLOT_BG_RE.search(markup)
    if match is None:
        _fail(
            f"panel {panel_id!r} was drawn without a plot area, so there is no "
            "rectangle its bound labels could be checked against"
        )
    left, top, width, height = (float(value) for value in match.groups())
    return (left, top, left + width, top + height)


def label_boxes(markup):
    """Each drawn bound label as ``(declared, line, (x0, y0, x1, y1))``.

    The box is the anchor the text element carries, extended left by
    `rtp_trace_report.label_text_width` and up/down by that module's font
    ascent and descent. A wrapped label is several elements, and each is
    checked: the block is only inside the plot if every line is. `declared` is
    the label the element carries in its ``<title>`` (the undivided sentence)
    and `line` is the one line this element draws.
    """
    boxes = []
    for attributes, content in BOUND_LABEL_RE.findall(markup):
        values = dict(TEXT_ATTRIBUTE_RE.findall(attributes))
        titles = BOUND_LABEL_TITLE_RE.findall(content)
        declared = html.unescape(
            re.sub(r"</?title>", "", titles[0]) if titles else content
        )
        line = html.unescape(BOUND_LABEL_TITLE_RE.sub("", content))
        anchor = float(values["x"])
        baseline = float(values["y"])
        width = REPORT.label_text_width(line)
        if values.get("text-anchor") == "end":
            left = anchor - width
        elif values.get("text-anchor") == "middle":
            left = anchor - width / 2
        else:
            left = anchor
        boxes.append(
            (
                declared,
                line,
                (
                    left,
                    baseline - REPORT.LABEL_ASCENT_PX,
                    left + width,
                    baseline + REPORT.LABEL_DESCENT_PX,
                ),
            )
        )
    return boxes


def check_label_fit(panel_id, markup):
    """Problems that make a drawn bound label leave the panel's plot area.

    This is `AGENTS.md`'s panel rule as a refusal, the way `check_panel_axis`
    is: a bound label that is present but not readably placed is evidence a
    reader can miss, and the reading that found it must not depend on a reader
    noticing. The measured run had three panels doing exactly that -- the
    labels of `M2-delivery`, `M4-imbalance` and `M4-shares` each began several
    pixels above the top of the plot, across the legend.

    The fit is a *model*, not a measurement: `label_text_width` is an upper
    bound over the fonts a browser resolves for the panel's text style, so a
    label the model says fits can in principle be drawn by a font wider than
    that bound and still leave the plot. What this catches is every placement
    and wrapping defect the tool can produce -- an anchor moved off the plot, a
    label too long to wrap into `LABEL_MAX_LINES` lines, a block with no room
    above or below its line -- and what it cannot catch is a font outside the
    model's table. The vertical extent needs no width at all, so a label drawn
    above or below the plot is caught whatever font draws it.
    """
    left, top, right, bottom = panel_plot_rect(panel_id, markup)
    problems = []
    for declared, line, (x0, y0, x1, y1) in label_boxes(markup):
        outside = []
        if x0 < left:
            outside.append(f"{left - x0:.1f} px past its left edge")
        if x1 > right:
            outside.append(f"{x1 - right:.1f} px past its right edge")
        if y0 < top:
            outside.append(f"{top - y0:.1f} px above it")
        if y1 > bottom:
            outside.append(f"{y1 - bottom:.1f} px below it")
        if not outside:
            continue
        detail = "" if line == declared else f" (on the drawn line {line!r})"
        problems.append(
            f"panel {panel_id!r}: the bound label {declared!r} does not fit the "
            f"plot area{detail} -- its drawn box {x0:.1f},{y0:.1f}..{x1:.1f},{y1:.1f} "
            f"is {', '.join(outside)}, and the plot area is "
            f"{left:.1f},{top:.1f}..{right:.1f},{bottom:.1f}. The label is the "
            "part of the panel that says what its bound governs, so it has to "
            "be inside the panel; shorten the label or its guard list, or give "
            "the panel an axis with room for it"
        )
    return problems


def governed_label(bound, series, run_values, crossing=True):
    """A bound's label plus the clause naming what the line governs.

    The crossing clause is for bar panels, where one line is drawn across many
    bars and the bars' own guards are the only thing that distinguishes a
    breach from a tolerated tripwire. A line panel's series carry their own
    legend entries, so its bounds are labelled with their declared governance
    and left at that.
    """
    clauses = []
    if bound.get("series"):
        clauses.append(f"governs series {bound['series']}")
    window = bound_governed_x(bound)
    if window is not None:
        low, high = window
        clauses.append(
            f"governs x={low:g}" if low == high else f"governs x={low:g}..{high:g}"
        )
    values = _bound_values(series)
    crossed = crossing_values(values, bound["y"]) if crossing else []
    if crossed:
        clause = f"{len(crossed)} of {len(values)} bars beyond it"
        guards = run_guards(run_values, series, bound["label"])
        if guards:
            clause += "; run guards " + " ".join(
                f"{key}={value:g}" for key, value in guards
            )
        clauses.append(clause)
    if not clauses:
        return bound["label"]
    return f"{bound['label']} [{'; '.join(clauses)}]"


def svg_bar_chart(title, x_label, y_label, series, bounds=None, extent=None, run_values=None):
    """Grouped bars for ``[(name, [(x, y)])]`` on the axis ``bar_axis_extent`` picks.

    ``rtp_trace_report.svg_histogram`` buckets raw samples, which is a
    different contract from declared x/y points, so the bars are drawn here
    with the report's own axis constants, palette and extent helper. ``bounds``
    are declaration dicts: each is drawn across the x-window it governs (the
    whole plot unless it declares ``x``), and labelled with that governance.
    """
    series = [(name, points) for name, points in series if points]
    bounds = bounds or []
    if not series:
        return f"<section><h2>{html.escape(title)}</h2><p>No samples.</p></section>"
    xs = [x for _, points in series for x, _ in points]
    x_min, x_max = min(xs), max(xs)
    if x_min == x_max:
        x_min, x_max = x_min - 0.5, x_max + 0.5
    if extent is None:
        extent = bar_axis_extent(series, bounds)
    y_min, y_max = extent
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
    # Bars rise from the axis' own bottom. On a zero-baseline axis that is zero,
    # as before; on the band view the axis starts at the floor's band, so the
    # bar's length is the margin over that band — which is the reading.
    baseline = sy(max(y_min, 0.0) if y_min <= 0.0 else y_min)
    # A band view's ticks span a few percent of the unit; the report's two
    # decimals would print its six ticks as three values.
    y_decimals = 2
    if y_min > 0.0:
        y_decimals = min(6, max(2, math.ceil(-math.log10(y_max - y_min)) + 2))
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
        parts.append(f"<text x=\"{REPORT.PAD_LEFT - 9}\" y=\"{y + 4:.1f}\" text-anchor=\"end\">{y_value:.{y_decimals}f}</text>")
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
    for bound in bounds or []:
        y = sy(bound["y"])
        window = bound_governed_x(bound)
        if window is None:
            left, right = REPORT.PAD_LEFT, REPORT.WIDTH - REPORT.PAD_RIGHT
        else:
            left = max(
                min(sx(window[0]) - group_width / 2, sx(window[1]) + group_width / 2),
                REPORT.PAD_LEFT,
            )
            right = min(
                max(sx(window[0]) - group_width / 2, sx(window[1]) + group_width / 2),
                REPORT.WIDTH - REPORT.PAD_RIGHT,
            )
            left, right = min(left, right), max(left, right)
        label = governed_label(bound, series, run_values)
        parts.append(f"<line class=\"bound\" x1=\"{left:.1f}\" y1=\"{y:.1f}\" x2=\"{right:.1f}\" y2=\"{y:.1f}\" stroke=\"{REPORT.BOUND_STROKE}\" stroke-width=\"1.4\" stroke-dasharray=\"6 4\"/>")
        markup, _ = REPORT.bound_label_markup(
            label,
            right,
            y,
            (
                REPORT.PAD_LEFT,
                plot_top,
                REPORT.WIDTH - REPORT.PAD_RIGHT,
                REPORT.HEIGHT - REPORT.PAD_BOTTOM,
            ),
            BAR_BOUND_LABEL_STYLE,
        )
        parts.append(markup)
    parts.append(f"<text x=\"{REPORT.WIDTH / 2}\" y=\"{REPORT.HEIGHT - 5}\" text-anchor=\"middle\">{html.escape(x_label)}</text>")
    parts.append(f"<text x=\"18\" y=\"{REPORT.HEIGHT / 2}\" text-anchor=\"middle\" transform=\"rotate(-90 18 {REPORT.HEIGHT / 2})\">{html.escape(axis_label(y_label, extent))}</text>")
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


def panel_markup(title, x_label, y_label, panel, points, run_values=None):
    """Markup for one declared panel, as exactly one ``<svg>`` document span."""
    chart = panel["chart"]
    chart_title = f"{title} [{panel['id']}]"
    panel_x_label = panel.get("x_label", x_label)
    panel_y_label = panel.get("y_label", y_label)
    series = [
        (entry["name"], sorted(points[(panel["id"], entry["name"])]))
        for entry in panel["series"]
    ]
    bounds = _bound_specs(panel)
    extent = _require_extent(panel.get("y_extent"), f"panels.{panel['id']}.y_extent")
    if chart == "bar":
        axis = extent if extent is not None else bar_axis_extent(series, bounds)
        problems = (
            check_panel_axis(
                panel["id"],
                series,
                bounds,
                axis,
                bar_plot_height(len(series)),
            )
            + check_bound_governance(panel["id"], series, bounds, run_values)
            + check_bound_x_categories(panel["id"], series, bounds)
        )
        if problems:
            _fail("\n  ".join(problems))
        markup = svg_bar_chart(
            chart_title,
            panel_x_label,
            panel_y_label,
            series,
            bounds,
            axis,
            run_values,
        )
    elif chart == "line":
        labelled = [
            (bound["y"], governed_label(bound, series, run_values, crossing=False))
            for bound in bounds
        ]
        markup = REPORT.svg_line_chart(
            chart_title, panel_x_label, panel_y_label, series, extent, labelled
        )
    else:
        labelled = [
            (bound["y"], governed_label(bound, series, run_values, crossing=False))
            for bound in bounds
        ]
        markup = REPORT.svg_cdf_chart(
            chart_title, panel_x_label, panel_y_label, series, labelled
        )
    problems = check_label_fit(panel["id"], markup)
    if problems:
        _fail("\n  ".join(problems))
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


def render_mandate(declaration_path, out_dir, *, rasterize=True, browser=None, run_values=None):
    """Validate one mandate, write and verify its panels, and rasterize them.

    ``run_values`` are the ``MANDATE`` line's own measurements for this
    mandate, as parsed by ``tools/mandate-check``. They are used for exactly
    one thing: naming the run's per-arm guards (`*_guard`) on a bound whose
    bars cross it, so the panel cannot read as a breach the verdict tolerates.
    They change no plotted point, series or bound value.

    Raises MandatePlotError naming the problem for any invalid declaration or
    data file, any empty panel or undeclared series, any axis that cannot show
    a bound it carries, any crossed bound that states nothing about what it
    governs, any written SVG without series geometry, or any declared bound
    missing from the SVG; raises ``render_graph.RenderGraphError`` (naming the
    browser, or the offending PNG) when rasterization was requested and could
    not be verified.
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
        markup = panel_markup(title, x_label, y_label, panel, points, run_values)
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


def load_run_values(path_or_json):
    """The run's `MANDATE` measurements, from a JSON object or a file of one.

    The label it feeds is prose about the run, so an unreadable or non-object
    value is an error rather than a silently unattributed panel.
    """
    if path_or_json is None:
        return None
    text = path_or_json
    candidate = Path(path_or_json)
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as error:
            _fail(f"run values unreadable: {candidate}: {error}")
    try:
        values = json.loads(text)
    except json.JSONDecodeError as error:
        _fail(f"run values are neither a JSON object nor a file of one: {error}")
    if not isinstance(values, dict):
        _fail(f"run values must be a JSON object, got {type(values).__name__}")
    return values


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
        "--run-values",
        default=None,
        metavar="JSON|PATH",
        help=(
            "this run's MANDATE measurements (a JSON object or a file of one), "
            "used only to name the run's own per-arm * _guard values on a bound "
            "whose bars cross it"
        ),
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
            run_values=load_run_values(args.run_values),
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
