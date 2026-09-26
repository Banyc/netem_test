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
show their own failure"). Every check below enforces it, and none can be
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
- **the axis-range test** — `check_named_values_in_axis` measures every value a
  panel *names* against the axis it draws: every bound, and every per-arm guard
  its own label names. A `M2-wire` panel that announced
  `hostile_wire_guard=10` and `lone_wire_guard=14` on an axis topping out at
  6.6 was naming two values the reader could not see, and the region between
  the budget and the guard — the region the crossing it explains lives in — was
  off the frame with them. An axis that does not resolve a value the panel
  names is an error, and so is a named value drawn on the frame's own edge.
- **the headroom test** — `check_bound_headroom` requires `MIN_HEADROOM_PIXELS`
  of axis above the highest value a panel names, so the bar that crosses that
  value has somewhere to go. An axis topping out *at* the bound draws a breach
  and a value exactly at the bound as the same picture: `M4-shares` was drawn
  over `0..0.25` — the fair share itself — so a flow over the share could not
  be drawn at all. `axis_with_headroom` spends the span's own 5 % and then the
  pixel floor, because a fair share pinned at 25 % has a data spread of a
  ten-thousandth and the span's 5 % of it is a third of a pixel.
- **the label-overlap test** — `check_label_overlap` refuses a label drawn
  twice on one anchor or any two labels sharing ink. The preserved run drew
  `fair share 25.0%fair share 25.0%` at a single anchor, leaving the text of
  neither readable; wrapped lines of one label are exempt, since they are
  stacked a line height apart.
- **the bar-separation test** — `check_bar_separation` refuses two bars that
  touch, because flush bars read as one continuously growing quantity rather
  than as separate values. It measures the geometry that produced the
  staircase: the old placement clamped each category's group towards the middle
  so the outermost groups overlapped their neighbours by 52 px, and one
  series then painted a rising ribbon. Bars are laid out in category bands with
  the domain padded by half a band at each end, so no placement is clamped and
  every pair keeps `MIN_BAR_GAP_PIXELS`.
- **the canvas-text test** — `check_canvas_text_fit` refuses a drawn text that
  leaves the canvas (a label the reader cannot finish, including the *rotated*
  y label, whose length runs along the panel's height) and a text carrying an
  empty template (`[]`, `()`, `None`), which draws the absence of the evidence
  its own label claims. The band-view note is drawn inside the plot for this
  reason: appended to the y label it was 66 characters rotated down a 300 px
  margin.
- **the legend test** — `check_series_labels` refuses a legend that draws a
  producer's column name (`wire_x`, `shaper_forwarded`) instead of the
  quantity's name. `series_label` maps the names whose prettified form is still
  cryptic and prettifies the rest; a legend that shows the CSV's spelling is a
  claim about the producer's code, not about the run.

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
TEXT_ELEMENT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)
ROTATE_RE = re.compile(r"rotate\(\s*[-0-9.]+\s+([-0-9.]+)\s+([-0-9.]+)\s*\)")
BAR_RECT_RE = re.compile(
    r'<rect x="([-0-9.]+)" y="([-0-9.]+)" width="([-0-9.]+)" '
    r'height="([-0-9.]+)" fill="(#[0-9A-Fa-f]{6})"'
)
LEGEND_GROUP_RE = re.compile(r'<g class="legend">(.*?)</g>', re.S)
LABEL_OVERLAP_PX2 = 1.0
"""The area two drawn labels may share before the panel is refused.

One square pixel of shared ink is a rendering artefact at this scale; more than
that is a label the reader cannot read, and the run that drew
`fair share 25.0%fair share 25.0%` at one anchor shared all of it.
"""

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

MIN_HEADROOM_PIXELS = 6.0
"""The least height, in pixels, an axis keeps above the highest bound it names.

The standing rule is that the failure a panel is drawn for must be drawable
inside its frame. A bound drawn flush with the top of its axis leaves the bar
that crosses it nowhere to go: a breach and a value exactly at the bound paint
the same picture, and the reader cannot tell which one the run measured. Six
pixels is `MIN_BOUND_PIXELS`, for the same reason — below it an over-bound bar
is a smudge against the frame rather than a bar.
"""

MIN_AXIS_INSET_PIXELS = 2.0
"""The least distance, in pixels, a named bound keeps from the frame's own edge.

A bound drawn *on* the frame is not inside the axis range: it reads as the
frame's border, and the region it governs is off the panel.
"""

MIN_BAR_GAP_PIXELS = 1.0
"""The least gap between two drawn bars.

Bars drawn flush read as one continuously growing quantity — the staircase a
reader sees as a ribbon — rather than as three values, so the gap is a property
of the panel rather than a matter of taste.
"""

BAR_BAND_INSET_SHARE = 0.12
"""The share of a category's band kept clear at each end of it."""

BAR_GAP_SHARE = 0.18
"""The share of a bar's slot left as the gap to its neighbour."""

SERIES_LABEL_VOCABULARY = {
    "wire_x": "own-wire multiple",
    "fraction": "fraction of link rate",
}
"""Human names for the producer columns whose prettified form is still cryptic.

`wire_x` is this battery's own-wire multiple: the `x` is the multiple, not a
run of the series, and no mechanical rule recovers that. `fraction` is a
*dimensionless* fraction of the configured link rate, and it is the panel's own
`y_label` too (a panel with one series names that series' quantity), so a reader
is never shown `MiB/s` — the sibling goodput panel's unit — over it. Every other
column name the producers emit is prettified by `series_label`
(`shaper_forwarded` -> `shaper forwarded`), and a name that is already a word
(`delivery`, `hostile`) is left alone.
"""

RAW_COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
"""The shape of a column name: snake_case, which a legend must not show raw."""

PLACEHOLDER_TEXT_RE = re.compile(r"\[\s*\]|\(\s*\)|\bNone\b|\bnull\b|\bnan\b")
"""What a template renders where the collection it names came out empty.

A panel drawing `[]` where a list of guards belongs is drawing the absence of
the evidence its label claims, so it is refused the way a missing bound is.
"""


def series_label(name):
    """The human label a series is drawn with, never its raw column name.

    The legend is how a reader tells one series from another, and a column name
    is not a name for a quantity: `shaper_forwarded` is the shaper's forwarded
    rate, and the reader should not have to decode the producer's spelling to
    learn that. The vocabulary is for the names a mechanical prettification
    still leaves cryptic; everything else snake_case is prettified.
    """
    if name in SERIES_LABEL_VOCABULARY:
        return SERIES_LABEL_VOCABULARY[name]
    if RAW_COLUMN_NAME_RE.match(name):
        return name.replace("_", " ")
    return name


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


def failure_side(values, y):
    """The side of ``y`` on which a departure begins, or ``0.0`` when neither is.

    A **crossing** says so itself: the minority beyond the bound is the side a
    bar failed towards. A one-sided bound says so too — a cap fails upwards, a
    floor downwards. A bound the bars split around evenly has no failing side at
    all: it is the value they are read at.
    """
    crossing = crossing_values(values, y)
    if crossing:
        return 1.0 if max(crossing) > y else -1.0
    side = bound_side(values, y)
    if side == "cap":
        return 1.0
    if side == "floor":
        return -1.0
    return 0.0


def bound_band(values, y, unit, tolerances=()):
    """The width of the region the axis must resolve for a one-sided bound.

    The nearest value on the bound's own side is how close a failing departure
    begins; a bound sitting at its ideal has no such distance at all, and there
    the unit's own resolution floor (`MIN_UNIT_SPAN`) is what the axis has to
    show in its place — `M2`'s delivery floor is `1.000`, so nothing below it is
    tolerated and the axis still has to make a small loss visible.

    That margin is a proxy for the departure that matters, and it is the wrong
    proxy when the run asserts a looser guard for the arms: a value inside its
    guard is not a departure, so where the run names one the region the axis
    owes the reader is the tolerance the guards open between the reference and
    the point where a departure begins. `M2-wire` is exactly that panel, and it
    is the reason the axis range cannot be derived from the data: an arm sitting
    0.09 under a 6x budget makes the observed margin a sliver by construction,
    and the tool refused the panel for a departure that is *tolerated* — three
    times over, in three runs whose worst arm was 6.63, 6.05 and 5.91. The
    margin rule is unchanged for every bound with no such guard (the delivery
    floors, the fair-share bounds), which is where a small departure really
    is the failure.
    """
    side = failure_side(values, y)
    if side:
        tolerated = sorted(
            abs(tolerance - y)
            for tolerance in tolerances
            if (tolerance - y) * side > 0.0
        )
        if tolerated:
            return max(tolerated[0], MIN_UNIT_SPAN * unit if unit else 0.0)
    nearest = min((abs(value - y) for value in values), default=0.0)
    return max(nearest, MIN_UNIT_SPAN * unit if unit else 0.0)


def clustered_around(values, y):
    """Whether the values sit as a cluster around a bound in the middle of a unit.

    The shape that makes a bound a *target* — `M4-shares`' fair share, whose
    flows land on either side of 25 % by a ten-thousandth, and which the old
    rule therefore labelled `1 of 8 bars beyond it` and drew as a crossing.
    A fraction panel has a sampling resolution of its own (`MIN_UNIT_SPAN` of the
    unit), and a bound every value sits within that resolution of is the value
    the bars are read *at*.

    None of this applies at the top of the unit, where a cluster is what
    delivery looks like and the one bar below it is the breach the floor exists
    to catch: `M4-delivery`'s seven bars at 1.000 and one at 0.994 against a
    0.995 floor is clustered on the same scale, and reading it as a target would
    lose the only failure that panel has.
    """
    unit = unit_span(values, [y])
    if unit is None or unit - y <= FRAME_HEADROOM * unit:
        return False
    return all(abs(value - y) <= MIN_UNIT_SPAN * unit for value in values)


def crossing_values(values, y):
    """The values beyond ``y`` that the panel reads as a departure.

    Empty unless at most `CROSSING_BULK_SHARE` of the bars are beyond the
    bound: a bound the bars split around evenly is the value they are expected
    to sit at (`M4-shares`' fair share), while a lone bar past the line is a
    crossing whose attribution the panel owes its reader (`M2-wire`). A bound
    the values are *clustered* around (`clustered_around`) has no crossing at
    all: it is the target they are read at, not a line any of them failed.
    """
    if clustered_around(values, y):
        return []
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

    One shape: a **floor at the top of the unit** — the quantity's ideal is the
    unit, the bound sits within `FRAME_HEADROOM` of it, and the bars *reach* for
    it, so the axis is drawn around the top rather than from zero. The reaching
    is tested on a majority rather than on every bar, because a delivery floor
    that has just been breached is still the panel's scale (`M4-delivery`'s one
    bar at 0.994 against a 0.995 floor) and drawing it from zero would put the
    breach the panel exists for under a fifth of a pixel.

    A **crossing** (`M2-wire`'s lone bar past the budget) is deliberately *not*
    this shape: the band view draws the axis around the top of the unit, so a
    crossing bound that is not at that top would be drawn off the axis entirely
    — which is how `M4-shares`' fair share at 0.25 came to be labelled 7553 px
    below its own plot. A bound the bars split around evenly is a target
    (`M4-shares`), and an untouched bound far from every bar is not the panel's
    scale either.
    """
    if unit is None or unit - y > FRAME_HEADROOM * unit:
        return False
    reaching = [value for value in values if value >= y]
    return len(reaching) * 2 > len(values)


def named_guard_values(series, bounds, run_values, *, crossing):
    """The run's own guards the panel's drawn labels will name.

    Only what a label actually says counts as named. A bar panel's bound carries
    the guards for the quantity it governs, so the axis has to carry them too —
    whether or not a bar has crossed yet, because the range is the reason the
    crossing is drawable at all when it comes. A line panel's label carries no
    such clause, so the M1 latency panel does not silently inherit
    `lone_p999_guard=8000` and blow its axis up to eight thousand milliseconds.
    `governed_label` builds both labels the same way.
    """
    if not crossing or not isinstance(run_values, dict):
        return []
    guards = set()
    for bound in bounds:
        guards.update(
            value for _, value in run_guards(run_values, series, bound["label"])
        )
    return sorted(guards)


def axis_with_headroom(low, high, top, plot_height):
    """Widen an axis so a value over its highest named bound is still drawable.

    Two margins, because they answer different questions. `FRAME_HEADROOM` of
    the span is the frame's own breathing room, so a line or tick at the top of
    an axis does not merge with its border. `MIN_HEADROOM_PIXELS` is the
    reader's: the highest bound the panel names needs enough axis above it that
    a bar which crosses it is a bar, not a smudge — and on a panel whose data
    spread is tiny against the bound (a fair share pinned at 25 %) the span's
    own 5 % is under a pixel, so the pixel floor is what does the work.
    """
    span = high - low
    if span > 0:
        high = high + FRAME_HEADROOM * span
    minimum = MIN_HEADROOM_PIXELS / plot_height
    if top > high:
        return (low, high)
    if minimum < 1.0 and high - top < minimum * (high - low):
        high = max(high, (top - minimum * low) / (1.0 - minimum))
    return (low, high)


def bar_axis_extent(series, bounds, run_values=None, plot_height=None):
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
      bar's length from zero is the reading. The extent carries every bound the
      panel draws *and* every guard its own label names, because a panel that
      announces `lone_wire_guard=14` on an axis topping out at 6.7 is naming a
      value it does not draw — the guard is then off the chart, and so is the
      region between the budget and it, which is where the crossing the panel
      exists to explain actually lies.
    """
    values = _bound_values(series)
    ys = [bound["y"] for bound in bounds]
    guards = named_guard_values(series, bounds, run_values, crossing=True)
    if plot_height is None:
        plot_height = bar_plot_height(len(series))
    unit = unit_span(values, ys)
    if unit is not None:
        for bound in bounds:
            if bound_is_the_scale(values, bound["y"], unit):
                band = bound_band(values, bound["y"], unit)
                span = 2.0 * band
                headroom = FRAME_HEADROOM * span
                return (unit - span - headroom, unit + headroom)
    named = [*ys, *guards]
    low = min([0.0, *values, *named])
    high = max([0.0, *values, *named])
    return axis_with_headroom(low, high, max(named) if named else high, plot_height)


def line_axis_extent(series, bounds, pinned=None):
    """The y extent of a line or CDF panel, as the report's own chart lays it out."""
    if pinned is not None:
        return pinned
    return REPORT.extent_including_bounds(
        REPORT.finite_extent(series), [(bound["y"], "") for bound in bounds]
    )


def panel_axis_extent(panel, series, bounds, run_values, plot_height):
    """The axis a panel is drawn on, so the checks measure the drawn axis.

    One function for both the drawing and the checks: a check reading a
    different extent from the one the chart used would be measuring nothing.
    """
    chart = panel["chart"]
    if chart == "cdf":
        return (0.0, 100.0)
    pinned = _require_extent(
        panel.get("y_extent"), f"panels.{panel['id']}.y_extent"
    )
    if chart == "bar":
        if pinned is not None:
            return pinned
        return bar_axis_extent(series, bounds, run_values, plot_height)
    return line_axis_extent(series, bounds, pinned)


def check_panel_axis(panel_id, series, bounds, extent, plot_height=None, run_values=None):
    """Problems that make an axis unable to show a bound drawn on it.

    This is `AGENTS.md`'s first panel test — "would a regression be visible at
    this scale?" — as a measurement: the band around a bound the panel draws as
    its scale must be at least `MIN_BOUND_PIXELS` tall, or the failure the bound
    exists to catch is sub-pixel. The delivery panels failed it at 0.5 %, which
    is why `bar_axis_extent` draws them as a band view now; a pinned `y_extent`
    that reintroduces the failure is refused by name.

    The band is measured with the run's own guards, because they are part of the
    question: a crossing a named guard tolerates is not a departure, and what the
    reader then has to be able to see is the tolerance (`bound_band`).
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
        tolerances = [
            value for _, value in run_guards(run_values, series, bound["label"])
        ]
        band = bound_band(values, y, unit, tolerances)
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


def check_named_values_in_axis(panel_id, bounds, guards, extent, plot_height=None):
    """Problems that make a named value unreadable: the axis does not resolve it.

    A panel whose label announces `lone_wire_guard=14` while its axis tops out
    at 6.7 is announcing a value the reader cannot see — the guard, and with it
    the whole region between the bound and the guard, are outside the frame, and
    that region is where the crossing the panel exists to explain actually lies.
    A verdict line cannot show this and neither can a summary, so it is measured
    on the drawn axis instead of left to the eye: *every* bound the panel draws
    and *every* guard its own labels name has to sit inside the range, with the
    frame's own edge kept clear.
    """
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    problems = []
    named = [(bound["y"], f"bound {bound['label']!r}") for bound in bounds]
    named += [(value, f"named guard {value:g}") for value in guards]
    for value, what in named:
        if low <= value <= high:
            clear = min(value - low, high - value) * plot_height / span
            if clear >= MIN_AXIS_INSET_PIXELS:
                continue
            where = f"only {clear:.1f} px from the nearest edge of it"
        elif value > high:
            where = f"{(value - high) * plot_height / span:.1f} px above it"
        else:
            where = f"{(low - value) * plot_height / span:.1f} px below it"
        problems.append(
            f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} does not resolve "
            f"the {what} (y={value:g}) that the panel names: it is {where}, and a "
            f"named value the axis does not show is a claim the reader has no way "
            "to check"
        )
    return problems


def check_bound_headroom(panel_id, bounds, guards, extent, plot_height=None):
    """Problems that make an over-bound bar undrawable: the axis is too short.

    An axis that tops out at the highest bound it names leaves the bar that
    crosses that bound nowhere to go — a breach and a value exactly at the bound
    paint the same picture — so the frame has to keep `MIN_HEADROOM_PIXELS`
    above every value it names. A pinned `y_extent` is where this bites, and on
    a panel whose data spread is a ten-thousandth of its bound (a fair share
    pinned at 25 %) it bites on the automatic extent too, which is why the
    policy spends a pixel floor on it as well as the span's own share.
    """
    if not bounds and not guards:
        return []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    top = max([bound["y"] for bound in bounds] + list(guards))
    headroom = (high - top) / span * plot_height
    if headroom >= MIN_HEADROOM_PIXELS:
        return []
    return [
        f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} keeps {headroom:.1f} "
        f"px above the highest value it names (y={top:g}), under the "
        f"{MIN_HEADROOM_PIXELS:.0f} px an over-bound bar needs: a breach and a "
        "value exactly at that bound would be drawn as the same picture, so the "
        "panel could not show the failure it exists for"
    ]


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


def band_view_note(extent):
    """The note an axis that is not 0-based has to carry, or ``""``.

    A bar's length is a claim about its value, so an axis that does not start at
    zero has to say so on its own face; the ticks alone leave the reader to
    notice, and the figure is read long after the tick range is. The note is
    drawn *inside* the plot rather than appended to the y label: a rotated label
    runs down the 300 px left margin, where a note long enough to state both ends
    of the band (a delivery band view's is ~66 characters) is drawn off the top
    of the canvas. Inside the plot, the note has the plot's own 864 px and
    `check_canvas_text_fit` measures it there.
    """
    low, high = extent
    if low <= 0.0:
        return ""
    decimals = max(2, math.ceil(-math.log10(high - low)) + 2)
    return f"band view {low:.{decimals}g}..{high:.{decimals}g}, not 0-based"


def box_overlap(first, second):
    """The area, in square pixels, two ``(x0, y0, x1, y1)`` boxes share."""
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return max(width, 0.0) * max(height, 0.0)


def check_label_overlap(panel_id, markup):
    """Problems that make a bound label unreadable: it shares pixels with another.

    A label is the part of the panel that says what its line governs, so two
    labels over one another are the text of neither — the run that drew
    `fair share 25.0%fair share 25.0%` at a single anchor left the reader unable
    to read either copy. Wrapped lines of *one* label are exempt: they are
    stacked a line height apart, so they touch at most and never overlap.
    """
    problems = []
    boxes = label_boxes(markup)
    for index, (declared, line, box) in enumerate(boxes):
        for other_declared, other_line, other in boxes[index + 1 :]:
            area = box_overlap(box, other)
            if area <= LABEL_OVERLAP_PX2:
                continue
            same_anchor = (
                declared == other_declared
                and abs(box[1] - other[1]) < REPORT.LABEL_LINE_HEIGHT_PX - 0.5
            )
            what = (
                f"the bound label {declared!r} is drawn twice on the same anchor"
                if same_anchor
                else f"the bound labels {declared!r} and {other_declared!r} overlap"
            )
            problems.append(
                f"panel {panel_id!r}: {what} -- their boxes share {area:.0f} of "
                f"{min((box[2] - box[0]) * (box[3] - box[1]), (other[2] - other[0]) * (other[3] - other[1])):.0f} "
                "px, so both names are drawn where neither can be read"
                + (
                    f" (on the drawn lines {line!r} and {other_line!r})"
                    if line != declared or other_line != other_declared
                    else ""
                )
            )
    return problems


def bar_boxes(markup):
    """Each drawn bar as ``(x0, y0, x1, y1)``, in document order."""
    return [
        (float(x), float(y), float(x) + float(width), float(y) + float(height))
        for x, y, width, height, _ in BAR_RECT_RE.findall(markup)
    ]


def check_bar_separation(panel_id, markup):
    """Problems that let bars read as one ribbon instead of as values.

    Bars drawn flush against one another are a *continuously growing* quantity —
    a staircase the eye completes into a ribbon — and no reader can recover the
    values from it. The panel must keep `MIN_BAR_GAP_PIXELS` between any two
    bars, which is also the check on the layout that produced the staircase: a
    bar placement that clamps the first and last groups towards the middle
    overlaps its neighbours, and an overlap is exactly what this measures.
    """
    bars = sorted(bar_boxes(markup))
    problems = []
    for index, first in enumerate(bars):
        for second in bars[index + 1 :]:
            gap = max(second[0] - first[2], first[0] - second[2])
            if gap >= MIN_BAR_GAP_PIXELS:
                continue
            stated = (
                f"overlap by {-gap:.1f} px"
                if gap < 0.0
                else f"are {gap:.2f} px apart"
            )
            problems.append(
                f"panel {panel_id!r}: two drawn bars {stated}, under the "
                f"{MIN_BAR_GAP_PIXELS:.0f} px a reader needs to tell one value from "
                "the next; drawn flush they read as one constantly growing "
                "quantity rather than as separate values"
            )
    return problems


def drawn_text_boxes(markup):
    """Each drawn ``<text>`` as ``(text, (x0, y0, x1, y1))``.

    The box is modelled the way `label_boxes` models a bound label (the anchor
    the element carries, extended by the text style's ascent and descent), and a
    text rotated about its own anchor — the panel's y label — is modelled on the
    axis it runs along, so a label longer than the canvas is caught instead of
    being exempted from the fit for being sideways.
    """
    boxes = []
    for attributes, content in TEXT_ELEMENT_RE.findall(markup):
        values = dict(TEXT_ATTRIBUTE_RE.findall(attributes))
        text = html.unescape(BOUND_LABEL_TITLE_RE.sub("", content)).strip()
        width = REPORT.label_text_width(text)
        x = float(values.get("x", 0.0))
        y = float(values.get("y", 0.0))
        rotation = ROTATE_RE.search(values.get("transform", ""))
        if rotation is not None:
            centre_x, centre_y = (float(value) for value in rotation.groups())
            boxes.append(
                (
                    text,
                    (
                        centre_x - REPORT.LABEL_ASCENT_PX,
                        centre_y - width / 2,
                        centre_x + REPORT.LABEL_DESCENT_PX,
                        centre_y + width / 2,
                    ),
                )
            )
            continue
        anchor = values.get("text-anchor", "start")
        left = x
        if anchor == "middle":
            left = x - width / 2
        elif anchor == "end":
            left = x - width
        boxes.append(
            (
                text,
                (
                    left,
                    y - REPORT.LABEL_ASCENT_PX,
                    left + width,
                    y + REPORT.LABEL_DESCENT_PX,
                ),
            )
        )
    return boxes


def legend_text(markup):
    """The label each legend entry draws, in document order."""
    group = LEGEND_GROUP_RE.search(markup)
    if group is None:
        return []
    return [
        html.unescape(BOUND_LABEL_TITLE_RE.sub("", content)).strip()
        for _, content in TEXT_ELEMENT_RE.findall(group.group(1))
    ]


def check_series_labels(panel_id, markup, series):
    """Problems that make a series unnamed to a human reader.

    The legend is how a reader tells one series from another, and a column name
    is not the name of a quantity: `wire_x` is the own-wire multiple and
    `shaper_forwarded` is the shaper's forwarded rate. `series_label` is what
    turns those into labels, and this is what keeps it applied -- a legend that
    shows the CSV's own spelling is the producer's *schema* leaking into the
    panel, which is a claim about the code rather than about the run.
    """
    drawn = legend_text(markup)
    expected = [series_label(name) for name, _ in series]
    problems = []
    if drawn != expected:
        problems.append(
            f"panel {panel_id!r}: the legend draws {drawn}, not the labels its "
            f"series have {expected}; every series needs a human name"
        )
    for text in drawn:
        if RAW_COLUMN_NAME_RE.match(text):
            problems.append(
                f"panel {panel_id!r}: the legend draws the raw column name "
                f"{text!r}; a reader is told the producer's spelling instead of "
                "the quantity's name"
            )
    return problems


def check_canvas_text_fit(panel_id, markup):
    """Problems that make a drawn text unreadable or uninformative.

    Two defects of the same family as a clipped bound label. A text that leaves
    the canvas is truncated — the reader sees a sentence that ends mid-word, and
    the value or unit it lost is not recoverable — and a text carrying an empty
    template (``[]``, ``()``, ``None``) is *drawing the absence of the evidence
    its own label claims*, which is worse than drawing nothing because it reads
    as a measurement. Both are measured on the artifact, at the font the panel
    declares, the way `check_label_fit` measures bound labels.
    """
    problems = []
    for text, (x0, y0, x1, y1) in drawn_text_boxes(markup):
        placeholder = PLACEHOLDER_TEXT_RE.search(text)
        if placeholder is not None:
            problems.append(
                f"panel {panel_id!r}: the drawn text {text!r} carries the empty "
                f"placeholder {placeholder.group(0)!r}; a panel must draw the "
                "measurement, not the template its absent evidence left behind"
            )
        if x0 < 0.0 or y0 < 0.0 or x1 > REPORT.WIDTH or y1 > REPORT.HEIGHT:
            outside = []
            if x0 < 0.0:
                outside.append(f"{-x0:.1f} px past the left edge")
            if x1 > REPORT.WIDTH:
                outside.append(f"{x1 - REPORT.WIDTH:.1f} px past the right edge")
            if y0 < 0.0:
                outside.append(f"{-y0:.1f} px above it")
            if y1 > REPORT.HEIGHT:
                outside.append(f"{y1 - REPORT.HEIGHT:.1f} px below it")
            problems.append(
                f"panel {panel_id!r}: the drawn text {text!r} is {', '.join(outside)}, "
                f"so the {REPORT.WIDTH}x{REPORT.HEIGHT} canvas draws it clipped; a "
                "label the reader cannot finish is not a label"
            )
    return problems


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

    The guard clause is *not* conditional on a crossing: the guards are the
    arms' own bounds, and the axis has to carry them whether or not one of them
    has been reached yet (a run whose worst arm sits 0.09 under the budget is
    the same panel as one whose worst arm sits 0.05 over it). Naming them is
    also what makes the range honest — a panel may draw what it names.
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
    if crossing:
        values = _bound_values(series)
        crossing_clauses = []
        crossed = crossing_values(values, bound["y"])
        if crossed:
            crossing_clauses.append(f"{len(crossed)} of {len(values)} bars beyond it")
        guards = run_guards(run_values, series, bound["label"])
        if guards:
            crossing_clauses.append(
                "run guards "
                + " ".join(f"{key}={value:g}" for key, value in guards)
            )
        if crossing_clauses:
            clauses.append("; ".join(crossing_clauses))
    if not clauses:
        return bound["label"]
    return f"{bound['label']} [{'; '.join(clauses)}]"


def repeated_category_index(categories, run_values):
    """The run's repetition count when a panel draws exactly its ``1..reps``.

    The ``MANDATE`` line's own vocabulary is the check on a declaration's prose:
    `M3` measures `reps=3` and draws one bar per repetition at x=1..3, so the
    categories *are* the run's repetitions — and a reader told they are `seed`
    is told something the run does not say. A declaration that labels the panel
    itself keeps its word; only the mandate's shared default is overridden.
    """
    if not isinstance(run_values, dict):
        return None
    reps = run_values.get("reps")
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 2:
        return None
    if sorted(set(categories)) != list(range(1, reps + 1)):
        return None
    return reps


def panel_x_label_for(panel, mandate_x_label, categories, run_values):
    """The x label a panel draws: its own, or the run's own vocabulary."""
    if "x_label" in panel:
        return panel["x_label"]
    reps = repeated_category_index(categories, run_values)
    if reps is not None:
        return f"rep (1..{reps})"
    return mandate_x_label


def panel_y_label_for(panel, mandate_y_label, series):
    """The y label a panel draws: its own, or its single series' own quantity.

    The mandate's `y_label` is a default shared by every panel of the mandate,
    and a panel whose axes differ from its siblings must not inherit one of
    theirs: `M3-fraction` was drawn on the goodput panel's `MiB/s`, telling the
    reader a dimensionless fraction of the link rate was a rate. A panel with
    one series has one quantity and says so; a panel with several has no single
    quantity to name and keeps the mandate's default.
    """
    if "y_label" in panel:
        return panel["y_label"]
    if len(series) == 1:
        return series_label(series[0][0])
    return mandate_y_label


def tick_label(value, decimals):
    """A y tick, with a value that rounds to zero printed without its sign.

    A band view whose lower edge is a few ten-thousandths below zero printed it
    as `-0.00`: a negative axis label under a panel that is entirely positive,
    which a reader has to stop and decode. The tick is still *at* the negative
    value -- only its rounded text loses a sign it does not mean.
    """
    label = f"{value:.{decimals}f}"
    return label.lstrip("-") if float(label) == 0.0 else label


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
    # -- the x axis: one band per distinct x --------------------------------
    #
    # Each category owns a band, and the domain is padded by half a band at both
    # ends so every band lies inside the plot. The padding is what lets the bars
    # be placed without clamping them: the placement that clamped the first and
    # last groups towards the middle is what made a single-series panel read as
    # a staircase -- the clamp moved the outer bars until they overlapped their
    # neighbours, and the overlap painted a rising ribbon instead of three
    # values (`check_bar_separation` now measures the overlap itself).
    categories = sorted(set(xs))
    slot = min(
        (
            after - before
            for before, after in zip(categories, categories[1:])
            if after > before
        ),
        default=1.0,
    )
    x_min, x_max = categories[0] - slot / 2, categories[-1] + slot / 2
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

    band = plot_width / len(categories)
    inset = min(band * BAR_BAND_INSET_SHARE, band / 2)
    bar_slot = max((band - 2 * inset) / len(series), 0.0)
    bar_gap = min(bar_slot * BAR_GAP_SHARE, bar_slot / 2)
    bar_width = max(bar_slot - bar_gap, 0.0)
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
        parts.append(f"<text x=\"{REPORT.PAD_LEFT - 9}\" y=\"{y + 4:.1f}\" text-anchor=\"end\">{tick_label(y_value, y_decimals)}</text>")
    for category_index, category in enumerate(categories):
        for index, (name, points) in enumerate(series):
            color = REPORT.COLORS[index % len(REPORT.COLORS)]
            for x_value, y_value in points:
                if x_value != category:
                    continue
                left = (
                    REPORT.PAD_LEFT
                    + category_index * band
                    + inset
                    + index * bar_slot
                )
                top = sy(y_value)
                parts.append(f"<rect x=\"{left:.1f}\" y=\"{min(top, baseline):.1f}\" width=\"{bar_width:.1f}\" height=\"{abs(top - baseline):.1f}\" fill=\"{color}\"/>")
    for bound in bounds or []:
        y = sy(bound["y"])
        window = bound_governed_x(bound)
        if window is None:
            left, right = REPORT.PAD_LEFT, REPORT.WIDTH - REPORT.PAD_RIGHT
        else:
            left = max(
                min(sx(window[0]) - band / 2, sx(window[1]) + band / 2),
                REPORT.PAD_LEFT,
            )
            right = min(
                max(sx(window[0]) - band / 2, sx(window[1]) + band / 2),
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
    note = band_view_note(extent)
    if note:
        parts.append(
            f"<text class=\"axis-note\" x=\"{REPORT.PAD_LEFT + 5:.1f}\" "
            f"y=\"{plot_top + 13:.1f}\" style=\"{BAR_BOUND_LABEL_STYLE}\">"
            f"{html.escape(note)}</text>"
        )
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
        parts.append(f"<text x=\"{x + 25:.1f}\" y=\"{y + 4}\">{html.escape(series_label(name))}</text>")
    parts.append("</g></svg></section>")
    return "".join(parts)


def check_x_axis_label(panel_id, x_label, categories, run_values):
    """Problems that let an axis label contradict the run's own vocabulary.

    The run's `MANDATE` line is the one place that says what its categories are,
    and `M3` measures `reps=3` while drawing one bar per repetition at x=1..3.
    Both of its panels were labelled `seed`. The renderer labels them with what
    the run measured; this is what keeps that applied, because a panel whose
    axis names a quantity the run did not measure is a claim about the
    producer's wording rather than about the run.
    """
    reps = repeated_category_index(categories, run_values)
    if reps is None:
        return []
    expected = f"rep (1..{reps})"
    if x_label == expected:
        return []
    return [
        f"panel {panel_id!r}: the run measured reps={reps} and this panel draws a "
        f"bar at each of 1..{reps}, but its x axis is labelled {x_label!r}: those "
        "categories are the run's repetitions, and a reader told they are "
        f"{x_label!r} is told something the run never measured"
    ]


def check_axis_label(panel_id, y_label, series, *, declared, carried):
    """Problems that label a panel's axis with a sibling panel's quantity.

    The mandate's `y_label` is a default shared by its panels, so a panel whose
    axes differ from its siblings must not inherit one of theirs: `M3-fraction`
    was drawn on the goodput panel's `MiB/s`, telling a reader that a
    dimensionless fraction of the link rate was a rate. A single-series panel
    has exactly one quantity, and `panel_y_label` names it; this refuses a drawn
    label that is the carried default instead.
    """
    if declared is not None or len(series) != 1:
        return []
    expected = series_label(series[0][0])
    if y_label == expected:
        return []
    return [
        f"panel {panel_id!r}: its y axis is labelled {y_label!r} — the mandate's "
        f"shared y_label, carried over from a sibling panel — while its only "
        f"series is {expected!r}: a single-series panel names its own quantity, "
        f"and a reader told the axis is {y_label!r} is told a unit the run never "
        "measured"
    ]


def panel_markup(title, x_label, y_label, panel, points, run_values=None):
    """Markup for one declared panel, as exactly one ``<svg>`` document span."""
    chart = panel["chart"]
    chart_title = f"{title} [{panel['id']}]"
    series = [
        (entry["name"], sorted(points[(panel["id"], entry["name"])]))
        for entry in panel["series"]
    ]
    panel_x_label = panel_x_label_for(
        panel, x_label, [x for _, points in series for x, _ in points], run_values
    )
    panel_y_label = panel_y_label_for(panel, y_label, series)
    bounds = _bound_specs(panel)
    pinned = _require_extent(panel.get("y_extent"), f"panels.{panel['id']}.y_extent")
    plot_height = bar_plot_height(len(series))
    axis = panel_axis_extent(panel, series, bounds, run_values, plot_height)
    guards = named_guard_values(
        series, bounds, run_values, crossing=chart == "bar"
    )
    problems = (
        check_bound_governance(panel["id"], series, bounds, run_values)
        + check_bound_x_categories(panel["id"], series, bounds)
        + check_axis_label(
            panel["id"],
            panel_y_label,
            series,
            declared=panel.get("y_label"),
            carried=y_label,
        )
        + check_x_axis_label(
            panel["id"],
            panel_x_label,
            [x for _, points in series for x, _ in points],
            run_values,
        )
    )
    if chart == "bar":
        # The axis test is the bar panel's own: a *bar* panel's axis is chosen
        # by `bar_axis_extent`, and the band a bound needs is the reading of the
        # bar's length against it. A line panel draws a trajectory whose
        # crossing of a ceiling is the line poking above it, so it owes the
        # axis-range and headroom checks (which it gets) rather than a band.
        problems += check_panel_axis(
            panel["id"], series, bounds, axis, plot_height, run_values
        )
    problems += check_named_values_in_axis(
        panel["id"], bounds, guards, axis, plot_height
    ) + check_bound_headroom(panel["id"], bounds, guards, axis, plot_height)
    if problems:
        _fail("\n  ".join(problems))
    drawn = [(series_label(name), points) for name, points in series]
    if chart == "bar":
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
            chart_title, panel_x_label, panel_y_label, drawn, axis, labelled
        )
    else:
        labelled = [
            (bound["y"], governed_label(bound, series, run_values, crossing=False))
            for bound in bounds
        ]
        markup = REPORT.svg_cdf_chart(
            chart_title, panel_x_label, panel_y_label, drawn, labelled
        )
    problems = (
        check_label_fit(panel["id"], markup)
        + check_label_overlap(panel["id"], markup)
        + check_series_labels(panel["id"], markup, series)
        + check_canvas_text_fit(panel["id"], markup)
        + (check_bar_separation(panel["id"], markup) if chart == "bar" else [])
    )
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
    mandate, as parsed by ``tools/mandate-check``. They are used for three
    things, all of them the panel's own honesty: naming the run's per-arm guards
    (`*_guard`) on a bound whose bars cross it, extending the axis to cover
    every guard the panel's labels name, and measuring a guarded bound's band
    against its tolerance instead of against a crossing that tolerance absorbs.
    They change no plotted point, series or bound value.

    Raises MandatePlotError naming the problem for any invalid declaration or
    data file, any empty panel or undeclared series, any axis that cannot show a
    bound it carries — a sub-pixel band, a named guard or bound outside the
    range, no headroom above the highest value named — any crossed bound that
    states nothing about what it governs, any written SVG without series
    geometry, any declared bound missing from the SVG, any bound label drawn
    outside its plot or over another label, any two bars that touch, any drawn
    text that leaves the canvas or carries an empty placeholder, and any legend
    drawing a producer's column name; raises ``render_graph.RenderGraphError``
    (naming the browser, or the offending PNG) when rasterization was requested
    and could not be verified.
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
