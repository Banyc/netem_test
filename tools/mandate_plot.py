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
        [--run-values JSON|PATH] [--run-censoring JSON|PATH]

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
- **the sliver-bound test** — `check_sliver_bound_stated` refuses a panel that
  draws a bound its own axis cannot resolve *and* says nothing about where the
  bound sits. The axis test above measures the band a bound needs; when a run's
  data lies far enough beyond the bound that the axis is the data's scale rather
  than the bound's, that band is a sliver. Measured on the fault renders the
  battery never takes, `MANDATE_SMOKE_FAULT=M4_drop` left `M4-imbalance`'s
  `±1 %` band `2.1 px` of a `-1..0.0605` axis whose span the starved flows'
  `-1.000` departures had set, and `MANDATE_SMOKE_FAULT=M4_late` left
  `M4-latency`'s 250 ms ceiling `1.6 px` of a `0..4565` axis the fault's
  3886-4348 ms body had set -- and both panels were refused rather than drawn,
  so the fault's visual evidence that its guard fires was missing. The failure
  those panels exist for was *visible* in both cases (a `217 px` departure on
  the first, `205 px` on the second), so a panel may draw such a bound -- and
  `check_panel_axis` accepts it -- once the panel states on its own face where
  the bound sits, how wide its band is in pixels, and how far the nearest and
  furthest bars lie from it. The statement is owed only when a departure at
  least `MIN_BOUND_PIXELS` legible is drawn, so the ordinary case (data at the
  bound, no visible departure) keeps the refusal. Both halves are refused by
  name: a sliver-bound panel that states nothing, and a stated one whose
  numbers are not the drawn points' and the drawn axis' own.
- **the panel-summary test** — `check_panel_summary_stated` requires every panel
  to carry a summary of what it drew, and measures it back out of the artifact.
  `AGENTS.md`'s report rule is that the tool's own message is the oracle, and a
  panel a reader can only read from its pixels is how the *producer's* answer
  gets inferred from a render: an `exit=2` read as a plot refusal, a panel count
  read as files, a filtered-out test's silence read as a pass. So a panel states,
  in the machine-readable `<desc class="panel-summary">` it carries, its chart,
  axis and axis labels, its series with the range and count of the points drawn,
  every bound line with its value, pixel position, band pixels and why it is
  drawn, its reading in the quantity's own units, and -- for a run that took a
  `MANDATE_SMOKE_FAULT` selector -- that it is a fault render rather than a
  refusal. Absent and false are refused alike: the summary's numbers are
  recomputed from the drawn points, the drawn axis and the drawn
  `class="bound"` lines, and a panel with no bound states `none by design`, so
  "no summary" is never a legitimate state. `tools/mandate-check` writes each
  block to `plots/<panel>.summary.txt`, carries it in `mandate-check.json` under
  `panel_summaries`, and prints it with the run.
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
  of axis beyond every value a panel names, on the side a bar can fail towards:
  above the highest, so the bar that crosses that value has somewhere to go, and
  below the lowest *downward-failing* one, so a floor or a band's lower arm is
  not drawn flush with the frame. An axis topping out *at* the bound draws a
  breach and a value exactly at the bound as the same picture: `M4-shares` was
  drawn over `0..0.25` — the fair share itself — so a flow over the share could
  not be drawn at all. `axis_with_headroom` spends the span's own 5 % and then
  the pixel floor, because a fair share pinned at 25 % has a data spread of a
  ten-thousandth and the span's 5 % of it is a third of a pixel.
- **the two-sided-bound test** — `check_two_sided_bound_drawn` refuses a panel
  that draws one arm of a bound its own label declares as a symmetric band
  (`±`). `M4-imbalance` names `fair-share bound ±1.0%` and drew a single line at
  `+0.01`, over an axis whose *low* was the data's own minimum (`-0.000234`)
  rather than the bound: `-1 %` — the departure a starved flow makes, and the
  failure the M4 mandate exists for — was drawn as a 4.9 px bar flush with the
  frame bottom, crossing nothing. The arms are read back out of the SVG and
  measured against `+y` and `-y`, the axis is required to keep
  `MIN_HEADROOM_PIXELS` beyond each, and a `±` label whose magnitude is not the
  value it declares (so the band's centre is unknowable) is refused rather than
  mirrored on a guess.
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
- **the axis-resolution test** — `check_tick_labels_distinct` reads the y tick
  labels back out of the artifact and refuses a panel whose ticks repeat a
  value. The measured panel is `M4-imbalance`, whose axis spans 1.3 % of the
  share around zero: with the decimals keyed off the sign of the lower edge,
  the report's two printed its six ticks as `0.01 0.01 0.01 0.00 0.00 0.00` — a
  coarse axis under a panel drawn for a 1 % departure, which is the axis test's
  own defect one level down. The resolution now comes from the step between the
  ticks, whatever the axis starts at.
- **the gap test** — `check_gap_honesty` measures the *drawn* geometry of a line
  panel against the holes `rtp_trace_report.series_walls` finds in the same
  points: a dot at every drawn sample, and no segment across a hole. A single
  polyline drawn across a 2.65 s hole paints an absence as a near-vertical
  climb, which is exactly how a reader took the `M1-latency` panel's
  `lone_tail` wall for a climb the window's end truncated -- and the two
  readings are the same pixels.
- **the stated-reading test** — `check_readings_stated` requires every arm the
  run read (`--run-censoring`, from the producer's own `[m1-censoring] arm=...`
  rows, which `tools/mandate-check` parses and hands over) to be stated on the
  line panel that draws it. The verdict (`Clear` / `Censored` /
  `EdgeRecordContained`), the arm's own `room` and `rungs_at_edge`, and the
  pixel facts that separate a peak which returned from a climb that did not
  (where the maximum is, how many samples follow it, where the largest hole
  is) are drawn in a band above the plot, so a reader cannot draw the opposite
  conclusion from the shape. `check_censoring_drawn` refuses the render
  outright when a reading names an arm no line panel draws, and
  `check_reading_band` refuses a band that would leave the plot too short to
  show the shape it explains.
- **the per-arm bound test** — `check_bound_arm_governance` refuses a panel
  that draws one bound across arms whose own bounds differ, which is
  `AGENTS.md`'s second panel test. A **bar** panel whose bound the run
  *restates* for some of the arms (`delivery_floor=0.995` under a declared
  `1.000`) is not one floor drawn across one group of arms: a bar between the
  two values crosses the declaration's line while sitting inside its own arm's
  floor, which is a breach the verdict tolerates and the one difference between
  the arms the panel exists to compare. The run's own keys enumerate the arms
  (`<arm>_<quantity>`) and name the arms that are not its reference (`*_guard`),
  so the declared bound is drawn over the reference arms and the run's own over
  the rest, each segment naming the arms it governs. The run's per-arm *guard*
  is the same fact (`arm_bound_source`): a guard the panel names is a bound the
  panel owes the reader, so `M2-wire`'s `hostile_wire_guard=10` and
  `lone_wire_guard=14` are drawn each over the arm it governs, and a guard that
  names a whole drawn series (`hostile_p99_guard` on a panel drawing that
  statistic) spans the plot labelled with its series. Where *every* arm states a
  guard of its own, the declared bound is drawn across the panel saying it
  governs no arm rather than passing as one arm's floor. `check_named_guards_drawn`
  measures both against the artifact: every guard a drawn label names has to
  have a line at its value. A run that states no bound of its own, a panel whose
  arms the run does not enumerate, and a quantity the run never restates are
  left with their single declared line.
  A **line** panel cannot split its one line per arm — its arms share the time
  axis — so it owes the same division in words: the bound names the arm it
  governs and, for every other arm it crosses, the run's own key and value for
  it. Measured on `M1-latency`, the `lone_tail` series peaked at 1567.1 ms
  against a panel whose only mark was the 250 ms mandate ceiling, which is not
  what that arm is asserted against (its own p99 guard is 3200 ms): the reader
  saw a 6.3×-over-ceiling peak and no way to learn it was a pass.
- **the reference-reach test** — `check_cdf_reference_reach` refuses a cdf
  panel whose own subject is a sliver. A latency CDF is read for where its
  reference arm's body and tail sit, and a linear axis out to the worst arm's
  tail decides the whole picture: measured on the `M1-cdf` artifact of a
  recorded run, axis `0.045..1017.77` ms linear, the `clean` curve ended at
  `112.55` ms — 11.1 % of the 864 px plot, its p99 at 9.2 % — while the lone
  tail was drawn in full. The axis is therefore put on base-10 logarithms when
  a linear one would leave that arm below `MIN_REFERENCE_REACH_SHARE` of the
  width, and the share is measured back off the panel's own tick labels
  (`drawn_x_scale`), so a quiet return to a linear axis is refused rather than
  believed. A panel that cannot reach the share even on a log axis owes a
  sentence saying so, and the sentence is accepted in the scale's place.
- **the x-bound test** — `check_x_bound_drawn` refuses a panel that leaves a
  mandate bound off a frame that can carry it. `bounds` are horizontal, so a
  ceiling expressed on the plot's *x* axis — an M1 latency ceiling read against
  a latency CDF — has no declared representation: it is carried on the sibling
  panel whose *y* axis is that quantity, and read back there rather than stated
  twice. Where the two panels' own drawn axis labels name the same quantity
  (`latency (ms)` on both sides), the panel draws a vertical mark at the
  ceiling and states, at that x, what each curve reads — `M1 ceiling 250 ms [at
  250 ms: clean 100%, hostile 99.4%, lone_tail 99.06%]` — which is the failure a
  CDF is read for (the curve not reaching its top by the ceiling) made visible
  in-frame. The values are parsed back out of the artifact and measured against
  the same drawn points the panel plots, so a sentence whose numbers came from
  anywhere else is refused. A ceiling outside the drawn x range owes no mark
  (there is no pixel for it) but still owes the reading, and owes saying it is
  outside, so the clamp the value is read with cannot pass for a measurement.
- **the departure-view test** — `check_departure_view_stated` refuses a panel
  whose own frame cannot carry the failure its mandate is read for. Two
  shapes, one rule. A bar panel whose bound is a value its own bars *straddle*
  draws a reference rather than a line a bar can cross, and the mandate behind
  the panel fails on a *departure* from it — a departure of the bound's size is
  invisible on an axis whose whole span is the share, so the panel either draws
  that departure or says on its own face that it is a composition view, names
  the panel that carries it, and states the run's own worst departure and the
  bound it is read against. The companion is derived from the two panels' drawn
  points — a panel is this one's departure view when every point it draws is
  `(mine - y) / y` — so the note names a panel the relation actually holds for.
  A panel that draws **no bound at all** while a sibling of the same mandate
  draws one (`M1-cdf` under `M1-latency`) has no mark to read its own quantity
  against either, and names the sibling and the bound it carries — unless the
  bound is expressible on its own x axis, which is the case the x-bound test
  above takes over: naming a sibling is what a frame does when it *cannot*
  carry the failure, and a bare pointer is not enough once the value is
  knowable. The check refuses a silent panel in both shapes, because a panel
  drawn silently reads as evidence that there is no failure to draw.
- **the stated-number test** — `check_reading_numbers` reads the numbers back
  out of that drawn band and measures them against the drawn points. A band is
  the part of a latency panel a reader trusts *instead of* the pixels, so a
  band whose numbers came from anywhere else (the producer's own
  `[m1-censoring] max=` token, another arm's series, a differently filtered
  one) is authoritative and wrong. `check_readings_stated` cannot see that: it
  compares the drawn sentence with the sentence the formatter produced, so the
  formatter is the only source either side of it consults. Each stated number
  is therefore compared at the precision it is *written* to -- where the
  maximum is, what follows it, where the holes are -- and a clause the check
  cannot read back at all is refused rather than skipped.

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
  representation: the M1 declaration in the contract carries it on the latency
  panel, and the CDF panel reads it back off the sibling whose *y* axis names
  the CDF's *x* quantity, drawing it as a vertical mark and stating the value
  each curve reads there (`derived_x_bounds`, `check_x_bound_drawn`). A
  declaration that renamed either axis stops the transfer instead of
  annotating a panel the bound is not about.
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
DRAWN_BOUND_RE = re.compile(r'<line class="bound"[^>]*\by1="([-0-9.]+)"')
BOUND_LABEL_TITLE_RE = re.compile(r"<title>.*?</title>", re.S)
TEXT_ATTRIBUTE_RE = re.compile(r'([A-Za-z][\w-]*)="([^"]*)"')
TEXT_ELEMENT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)
ROTATE_RE = re.compile(r"rotate\(\s*[-0-9.]+\s+([-0-9.]+)\s+([-0-9.]+)\s*\)")
BAR_RECT_RE = re.compile(
    r'<rect x="([-0-9.]+)" y="([-0-9.]+)" width="([-0-9.]+)" '
    r'height="([-0-9.]+)" fill="(#[0-9A-Fa-f]{6})"'
)
SLIVER_STATEMENT_RE = re.compile(
    r'bound "(?P<label>[^"]*)" at (?P<value>[-+0-9.eE]+) on axis '
    r'(?P<low>[-+0-9.eE]+)\.\.(?P<high>[-+0-9.eE]+): band (?P<band>[-+0-9.eE]+) = '
    r'(?P<band_px>[0-9.]+) px; nearest bar (?P<near>[-+0-9.eE]+), '
    r'(?P<near_dist>[-+0-9.eE]+) away; furthest (?P<far>[-+0-9.eE]+), '
    r'(?P<far_px>[0-9.]+) px from the bound'
    r'(?:; its arm at (?P<arm>[-+0-9.eE]+) \((?P<arm_px>[0-9.]+) px away\) '
    r'is drawn unlabelled)?'
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

BAR_BOUND_LABEL_STYLE = REPORT.NOTE_LABEL_STYLE
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

READINGS_GROUP_RE = re.compile(r'<g class="arm-readings">(.*?)</g>', re.S)
"""The band a line panel reserves for its per-arm readings, in the artifact."""

POLYLINE_ELEMENT_RE = re.compile(r"<polyline\s([^>]*?)/>")
"""Each drawn series segment, so a panel's geometry is read from the SVG."""

SAMPLE_MARKER_RE = re.compile(r'<circle class="sample"')
"""The dot a line panel draws at every sample it plots."""

AXIS_TICK_RE = re.compile(
    rf'<text x="{REPORT.PAD_LEFT - 9}" y="[-0-9.]+" text-anchor="end">([^<]*)</text>'
)
"""The y tick labels a panel draws, in the report's own encoding.

The tick text is how a reader reads a bar's magnitude, so it is read back out
of the artifact rather than from the loop that printed it.
"""

STATED_PEAK_RE = re.compile(
    r"(?:peak|max(?:imum)?) ([-+0-9.eE]+) ms at ([-+0-9.eE]+) s"
)
"""The magnitude a drawn reading states, and where it says it happened.

The clause's own word is matched loosely on purpose: a caption that spells the
maximum `max` rather than `peak` is still a caption claiming a magnitude, and a
check that skipped it would be blind to exactly the change it exists to catch.
"""

STATED_AFTER_RE = re.compile(
    r"(-?\d+) sample\(s\) after it \(next ([-+0-9.eE]+) ms at "
    r"([-+0-9.eE]+) s, last ([-+0-9.eE]+) ms at ([-+0-9.eE]+) s\)"
)
"""What the drawn reading says follows its maximum, when something does."""

STATED_END_RE = re.compile(
    r"nothing after it, so the series ends on its own maximum"
)
"""The drawn reading's other form: the maximum is the last sample."""

STATED_HOLES_RE = re.compile(
    r"(\d+) sample gap\(s\) over ([-+0-9.eE]+) s, largest ([-+0-9.eE]+) s "
    r"\(([-+0-9.eE]+)-([-+0-9.eE]+) s\), drawn as breaks, not climbs"
)
"""What the drawn reading says about the holes in its own sampling."""

STATED_GAP_WALL_RE = re.compile(r"no sample gap over ([-+0-9.eE]+) s")
"""The drawn reading's form for a series with a cadence and no hole in it."""

STATED_NO_GAP_RE = re.compile(r"no sample gap(?![ \w])")
"""The drawn reading's form for a series too short to have a cadence."""

ARM_READING_MIN_PLOT_PIXELS = 100.0
"""The least plot a line panel keeps once its reading band is reserved.

A line panel is read for the *shape* of a series over time, and the readings
that explain that shape are drawn above the plot rather than over it. The two
compete for the same 300 px canvas, so the band may not eat the shape it is
there to explain: below this many pixels of plot the panel is refused, the same
way an axis that cannot show its bound is.
"""

PANEL_NOTE_RE = re.compile(
    r'<text class="panel-note" x="([-0-9.]+)" y="([-0-9.]+)"[^>]*>(.*?)</text>',
    re.S,
)
"""The note a panel draws to say what its own frame cannot show."""

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


BOUND_BAND_MARK = "\u00b1"
"""The mark a bound's label uses to declare a symmetric two-sided band.

`M4`'s imbalance bound names itself `fair-share bound ±1.0%`: the mandate fails
on a departure from the fair share in *either* direction, so the bound the
panel has to draw and the reader has to compare against is the band, not one of
its arms. Drawing only the `+1 %` line leaves a flow starved on the other side
with no line to cross -- and, because the axis' low was the data's own minimum,
the breach expanded the frame instead of crossing anything.
"""

BOUND_BAND_RE = re.compile(
    re.escape(BOUND_BAND_MARK) + r"\s*([0-9]+(?:\.[0-9]+)?)\s*(%)?"
)


def bound_band_half_width(bound):
    """The magnitude a bound's label declares as a symmetric band, or ``None``.

    ``None`` is a one-sided bound's ordinary label. A label carrying `±` states
    that the bound applies on both sides of the quantity's ideal, and the number
    it states is the half-width; a percentage is taken in the axis' own units.
    """
    match = BOUND_BAND_RE.search(str(bound.get("label", "")))
    if match is None:
        return None
    half = float(match.group(1))
    return half / 100.0 if match.group(2) else half


def two_sided_bound(bound):
    """Whether a bound's own label declares it as a band around zero.

    The declared ``y`` is the band's half-width: the label already states that
    magnitude, and the declaration's ``y`` is the only value in the artifact
    that can be it, so the other arm is ``-y``. The equality is what makes that
    derivable rather than assumed -- a `±` label whose number is *not* the
    declared ``y`` is saying the half-width is something else, and a mirror
    computed from ``y`` would then be a bound the declaration never made. Such a
    declaration is refused by `check_two_sided_bound_drawn` rather than drawn.
    """
    half = bound_band_half_width(bound)
    if half is None:
        return False
    return math.isclose(abs(float(bound["y"])), half, rel_tol=1e-9, abs_tol=1e-12)


def mirrored_bounds(bounds):
    """The bound lines a panel draws: every declared bound, plus a band's arms.

    A two-sided bound is one declaration standing for two lines, and the panel
    draws both: the declared value keeps the declared label -- the sentence that
    says the bound *is* a band -- and the mirrored arm is labelled with its own
    value, which is the number a bar falling to that side is read against, and
    marked `band_arm` so the axis policy knows that arm fails downwards whatever
    this run's bars happen to have done. A one-sided bound is returned
    unchanged, so this is a no-op for every panel that declares an ordinary
    floor or ceiling.
    """
    drawn = []
    for bound in bounds:
        drawn.append(dict(bound))
        if not two_sided_bound(bound):
            continue
        mirror = dict(bound)
        mirror["y"] = -float(bound["y"])
        mirror["label"] = str(bound["label"]).replace(BOUND_BAND_MARK, "-")
        mirror["band_arm"] = "lower"
        drawn.append(mirror)
    return drawn


def drawable_bounds(panel, series, bounds, run_values):
    """The bound lines one bar panel draws: its effective bounds, mirrored.

    The single place both the drawing and the artifact's own verification read
    the plan from, so a panel and the check on it cannot disagree about how many
    lines a declaration calls for. The run's per-arm bounds (and its guards) are
    drawn where they are drawn per arm, and the guards that name a whole series
    are drawn beside them.
    """
    return mirrored_bounds(bar_bound_plan(panel, series, bounds, run_values))


def bar_bound_plan(panel, series, bounds, run_values):
    """The un-mirrored bound lines a bar panel draws, as the run's own plan.

    The declaration's bounds, the run's per-arm bounds (`effective_bounds`) and
    the run's per-series guards (`series_guard_bounds`) in one list, so the
    drawing and every check that measures the drawn lines read the same plan --
    a check measuring a different plan from the one the chart drew would be
    measuring nothing.
    """
    plan = effective_bounds(panel, series, bounds, run_values)
    already = {float(bound["y"]) for bound in plan if bound.get("guard_key") is not None}
    plan = plan + [
        bound
        for bound in series_guard_bounds(panel, series, bounds, run_values)
        if bound["y"] not in already
    ]
    return with_drawn_guards(plan)


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


# -- a bound drawn across arms whose guards differ -------------------------
#
# `M1-latency` draws one 250 ms ceiling across three arms, and the run asserts a
# different bound for each of them: the ceiling itself on the clean arm, and
# `hostile_p99_guard` / `lone_p99_guard` / `lone_over250_guard` on the impaired
# ones. The reader who sees the `lone_tail` series peak at 1567.1 ms (a real
# run's number) crossing that ceiling has, as drawn, no way to learn that 1567.1
# ms is a *pass* for that arm -- the panel's own reading band gives the arm's
# peak and its detector verdict and never what the arm is asserted against.
#
# The bar-panel case of the same rule is `check_bound_arm_governance`'s:
# `M2-delivery` draws each arm's own floor and names the arms it governs. A line
# panel cannot split the line per arm -- its arms share the time axis -- so it
# names, on the bound, what each arm it draws is read against.
def guard_statistic_unit(statistic):
    """The unit a guard key's own statistic name declares, or ``""``.

    `over250` is a *share of samples*, and on a panel whose axis is in
    milliseconds the key's own name is the only thing that says so: read bare,
    `lone_over250_guard=8` would be read as 8 ms. Every other statistic here
    (a percentile, a maximum) is stated in the panel's own unit, which its y
    label already carries, so nothing is appended and no unit is guessed.
    """
    return "%" if statistic.startswith("over") else ""


def arm_guard_tokens(series, run_values):
    """The run's own guards, attributed to the drawn arm each one bounds.

    A `MANDATE` line names each arm's guard as `<arm>_<statistic>_guard`
    (`hostile_p99_guard`, `lone_over250_guard`), and a *line* panel's series are
    the arms themselves -- which is why the quantity-keyed clause
    `run_guards` builds cannot name them: there the statistic token has to
    appear in a series name (`hostile_wire_guard` on the wire panel, whose
    series is `wire_x`), while here it is the arm that has to.

    The arm a key belongs to is the longest underscore-delimited prefix of the
    key's own name that a drawn series' name starts with (`lone` ->
    `lone_tail`), so the attribution is read off the run's own keys and the
    panel's own legend rather than from a name the renderer would have to
    invent. A key no drawn series claims is left out: a guard named against an
    arm this panel does not draw would look like evidence for it.
    """
    if not isinstance(run_values, dict):
        return []
    names = [name for name, _ in series]
    found = {name: [] for name in names}
    # The run's own key order is the order its `MANDATE` line prints them in, and
    # reading the guards back in it keeps the label's sentence in the order the
    # producer asserted them rather than an alphabetisation of its own keys.
    for key in run_values:
        value = run_values[key]
        if not isinstance(key, str) or not key.endswith(GUARD_KEY_SUFFIX):
            continue
        if not numeric(value):
            continue
        parts = key[: -len(GUARD_KEY_SUFFIX)].split("_")
        for take in range(len(parts) - 1, 0, -1):
            candidate = "_".join(parts[:take])
            arm = next(
                (
                    name
                    for name in names
                    if candidate == name or name.startswith(candidate + "_")
                ),
                None,
            )
            if arm is None:
                continue
            statistic = "_".join(parts[take:]) or candidate
            found[arm].append((key, statistic, float(value)))
            break
    return [(name, found[name]) for name in names if found[name]]


def arm_guard_clause(series, run_values):
    """The clause a bound drawn across arms with different guards owes.

    "Does each drawn bound apply to every series it crosses?" is `AGENTS.md`'s
    second panel test, and on a line panel it has to be answered in words: the
    one line cannot be drawn per arm, so it names the arms it governs and
    states, for every other arm it crosses, the key and value the run asserts
    for it. Returns the empty string when the run states no guard for any of
    the panel's arms, which is the case where the declaration's own bound is
    the only bound there is.
    """
    tokens = arm_guard_tokens(series, run_values)
    if not tokens:
        return ""
    guarded = {name for name, _ in tokens}
    reference = [name for name, _ in series if name not in guarded]
    parts = []
    if reference:
        parts.append("governs " + " ".join(reference))
    for name, guards in tokens:
        parts.append(
            f"{name} guards "
            + ", ".join(
                f"{key}={value:g}{guard_statistic_unit(statistic)}"
                for key, statistic, value in guards
            )
        )
    return "; ".join(parts)


# -- the readings a line panel states, and the honesty of its geometry -----
#
# Two things a latency panel must be able to say about itself, both of them the
# `AGENTS.md` panel rule applied to a line series rather than to a bound:
#
# 1. **where its samples are not.** A polyline drawn straight across a hole in
#    the sampling paints an absence as a near-vertical climb. Measured on the
#    `M1-latency` panel of a real run, the `lone_tail` series steps from 13.11 s
#    (1.8 ms) to 15.76 s (2651.7 ms) -- a 2.65 s hole -- and the wall it drew
#    was read as a climb truncated by the window's end. `check_gap_honesty`
#    measures the artifact for it: a dot at every drawn sample, and no segment
#    across a hole.
# 2. **what the instrument read.** The producer's own per-arm censoring
#    reading (`Clear` / `Censored` / `EdgeRecordContained`, with
#    `rungs_at_edge` and the arm's own `room`) is a statement about the series
#    that no pixel carries, so it is drawn on the panel next to the arm it is
#    about, and `check_readings_stated` refuses a render whose artifact does
#    not say it.
#
# The sentence itself is derived from the drawn series in `arm_reading`, not
# transcribed from the log: the pixel facts (where the peak is, whether the
# series continues after it, where the largest hole is) are computed from the
# same points the panel plots, and only the detector's own verdict is carried
# in from the run. That is what makes a peak-and-return distinguishable from a
# truncation by construction -- a truncation is a series with *nothing after
# its maximum*, and the panel then says exactly that.
def numeric(value):
    """Whether a parsed token is a real number rather than a bool or a string."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def arm_reading(arm, points, detector=None):
    """The sentence one line panel states for one arm, from the arm's series.

    ``points`` are the *drawn* points in plotting order. ``detector`` is the
    run's own per-arm reading for this arm (the parsed tokens of the producer's
    censoring row), or ``None`` when the run supplied none: the shape clauses
    are always stated, because they are measurements of what was plotted, and
    the detector's verdict is stated only when the run measured one.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    peak = max(ys)
    peak_index = len(ys) - 1 - ys[::-1].index(peak)
    after = len(points) - 1 - peak_index
    verdict = detector.get("verdict") if isinstance(detector, dict) else None
    parts = [
        f"{arm}: {verdict}" if isinstance(verdict, str) and verdict.strip() else arm
    ]
    parts.append(f"peak {peak:.4g} ms at {xs[peak_index]:.2f} s")
    if after:
        parts.append(
            f"{after} sample(s) after it (next {ys[peak_index + 1]:.4g} ms at "
            f"{xs[peak_index + 1]:.2f} s, last {ys[-1]:.4g} ms at {xs[-1]:.2f} s)"
        )
    else:
        parts.append("nothing after it, so the series ends on its own maximum")
    wall = REPORT.gap_wall_seconds(points)
    # The *count* is stated as well as the largest gap: a series can have more
    # than one hole, and a reader who is told about only one of them cannot tell
    # the other from the end of the line. A hole at the very end leaves its last
    # sample as a lone dot, which is the shape most easily read as a truncation.
    holes = REPORT.series_walls(points)
    if holes:
        _, before_x, after_x, gap = max(holes, key=lambda hole: hole[3])
        parts.append(
            f"{len(holes)} sample gap(s) over {wall:.2f} s, largest {gap:.2f} s "
            f"({before_x:.2f}-{after_x:.2f} s), drawn as breaks, not climbs"
        )
    else:
        parts.append(
            f"no sample gap over {wall:.2f} s" if wall else "no sample gap"
        )
    if isinstance(detector, dict):
        measured = []
        if numeric(detector.get("rungs_at_edge")):
            measured.append(f"rungs_at_edge {detector['rungs_at_edge']:g}")
        if numeric(detector.get("room")):
            measured.append(f"room {detector['room']:g} ms")
        if measured:
            parts.append(f"detector {' '.join(measured)}")
    return " - ".join(parts)


def panel_readings(series, censoring):
    """The ``(arm, sentence)`` readings a line panel states, in series order."""
    if not censoring:
        return []
    return [
        (name, arm_reading(name, REPORT.decimate(points), censoring.get(name)))
        for name, points in series
    ]


def drawn_readings(markup):
    """The reading lines a panel actually draws, joined into one sentence each.

    Read back out of the artifact rather than from the plotter's own state, the
    way `check_label_fit` reads the drawn label boxes: a check that trusted the
    code that drew the text would pass on a panel whose text never reached the
    SVG.
    """
    group = READINGS_GROUP_RE.search(markup)
    if group is None:
        return []
    return [
        html.unescape(BOUND_LABEL_TITLE_RE.sub("", content))
        for _, content in TEXT_ELEMENT_RE.findall(group.group(1))
    ]


def drawn_polylines(markup):
    """How many series segments a panel draws per stroke colour."""
    counts = {}
    for attributes in POLYLINE_ELEMENT_RE.findall(markup):
        values = dict(TEXT_ATTRIBUTE_RE.findall(attributes))
        if values.get("fill") != "none":
            continue
        counts[values.get("stroke")] = counts.get(values.get("stroke"), 0) + 1
    return counts


def check_gap_honesty(panel_id, series, markup):
    """Problems that let a hole in the sampling read as a climb.

    A line drawn across a hole asserts that the quantity moved from one sample
    to the next, which the run did not measure: the segment's steepness is a
    property of *when* the samples were taken. The measurements are taken on the
    drawn artifact -- the dots and the segments it actually draws -- against the
    holes `rtp_trace_report.series_walls` finds in the same drawn points, so a
    render that skips either one is refused by name.
    """
    drawn = [(name, REPORT.decimate(points)) for name, points in series if points]
    problems = []
    expected_markers = sum(len(points) for _, points in drawn)
    markers = len(SAMPLE_MARKER_RE.findall(markup))
    if markers != expected_markers:
        problems.append(
            f"panel {panel_id!r}: it draws {markers} sample marker(s) for "
            f"{expected_markers} drawn sample(s); without a dot at every sample "
            "the series' own discreteness is not on the panel, so a hole in the "
            "sampling is drawn as the line's own steepness"
        )
    counts = drawn_polylines(markup)
    strokes = {}
    for index, (name, points) in enumerate(drawn):
        strokes.setdefault(REPORT.COLORS[index % len(REPORT.COLORS)], []).append(
            (name, points)
        )
    for colour, entries in sorted(strokes.items()):
        if len(entries) > 1:
            problems.append(
                f"panel {panel_id!r}: series "
                f"{sorted(name for name, _ in entries)} are drawn in the same "
                f"stroke {colour}, so this panel's segments cannot be attributed "
                "to the series they belong to and its holes cannot be checked"
            )
            continue
        name, points = entries[0]
        holes = REPORT.series_walls(points)
        runs = [
            run for run in REPORT.split_at_walls(points, holes) if len(run) >= 2
        ]
        drawn_count = counts.get(colour, 0)
        if drawn_count == len(runs):
            continue
        where = (
            f"the {max(hole[3] for hole in holes):.2f} s hole between "
            f"{max(holes, key=lambda hole: hole[3])[1]:.2f} s and "
            f"{max(holes, key=lambda hole: hole[3])[2]:.2f} s"
            if holes
            else "no hole"
        )
        problems.append(
            f"panel {panel_id!r}: series {name!r} is drawn as {drawn_count} "
            f"polyline segment(s) where its {len(holes)} hole(s) require "
            f"{len(runs)} ({where}); a segment is drawn across a hole in the "
            "sampling, so a period nobody observed is painted as a near-vertical "
            "climb the run never measured"
        )
    return problems


def check_readings_stated(panel_id, series, readings, markup):
    """Problems that leave a run's own reading off the panel it is about.

    The detector's verdict is the one thing about a latency series that its
    pixels cannot carry, and the two readings it arbitrates -- a peak that came
    back down and a climb cut off by the window's end -- are the same shape on
    the panel. So a reading the run supplied has to be on the panel, per arm,
    and this measures the drawn text rather than trusting the call that drew it.
    """
    names = [name for name, _ in series]
    stated = " ".join(drawn_readings(markup))
    problems = []
    for arm, text in readings:
        if arm not in names:
            problems.append(
                f"panel {panel_id!r}: the run's reading for arm {arm!r} is about "
                f"no series this panel draws (its series are {names}); a verdict "
                "no panel carries is evidence no reader sees"
            )
        elif text not in stated:
            problems.append(
                f"panel {panel_id!r}: the run read arm {arm!r} and the panel does "
                "not state it. As drawn, a hole in the sampling, a peak that "
                "returned and a climb cut off by the window's end are the same "
                f"shape, so the reader cannot draw the opposite conclusion from "
                f"the pixels. Missing: {text!r}"
            )
    return problems


def stated_spans(text, arms):
    """The slice of a joined reading band each arm's own sentence occupies.

    The band is read back out of the artifact as its wrapped lines and rejoined
    with single spaces (`drawn_readings`), which is how `check_readings_stated`
    already reconstructs a sentence. Each reading opens with its own arm name --
    `<arm>: <verdict> - ...` when the run read one, `<arm> - ...` when it did
    not -- so the split is by that marker rather than by position, and a clause
    cannot be attributed to a series merely because it was drawn next to it.
    """
    found = {}
    for arm in arms:
        for marker in (f"{arm}: ", f"{arm} - "):
            index = text.find(marker)
            if index >= 0:
                found[arm] = index
                break
    ordered = sorted(found.items(), key=lambda pair: pair[1])
    spans = {}
    for position, (arm, start) in enumerate(ordered):
        end = ordered[position + 1][1] if position + 1 < len(ordered) else len(text)
        spans[arm] = text[start:end]
    return spans


def stated_tolerance(text):
    """Half a unit in the last place a written number carries, plus rounding slack.

    A stated number is compared at the precision it is *written* to rather than
    against a tolerance chosen to make a run pass: `853.9` allows half of
    `0.1`, and nothing else. That is what makes the check catch a caption whose
    magnitude came from a different source whatever that source's own
    formatting was -- a `1074.1` against a series that measures `853.89` fails
    by 2200 last places rather than by a hair. The `1.001` (and the epsilon)
    absorbs the last-digit rounding of a binary value printed to that place,
    which can sit exactly half a unit from the decimal it prints.
    """
    match = re.fullmatch(
        r"([-+]?)(\d+)(?:\.(\d+))?(?:[eE]([-+]?\d+))?", text.strip()
    )
    if match is None:
        return None
    decimals = len(match.group(3) or "")
    exponent = int(match.group(4) or 0)
    return 10.0 ** (exponent - decimals) * 0.5 * 1.001 + 1e-9


def stated_problem(panel_id, arm, what, written, measured):
    """A problem when a written number is not the value the series measures."""
    tolerance = stated_tolerance(written)
    if tolerance is None:
        return (
            f"panel {panel_id!r}: the reading drawn for arm {arm!r} states {what} "
            f"as {written!r}, which is not a number; the band exists so the "
            "reader does not have to interpret the pixels, so a value the "
            "reader cannot read is not a reading"
        )
    value = float(written)
    if abs(value - measured) <= tolerance:
        return None
    return (
        f"panel {panel_id!r}: the reading drawn for arm {arm!r} states {what} "
        f"{written}, which the series it is drawn from does not measure: that "
        f"point is {measured:.6g}. A caption whose numbers come from anywhere "
        "but its own series is worse than no caption, because the band is what "
        "the reader trusts instead of the pixels"
    )


def stated_reading_problems(panel_id, arm, points, text):
    """Every number a drawn reading states, measured against the drawn series.

    The facts are recomputed here from the same points the panel plots -- where
    the maximum is, how many samples follow it, where the holes are -- so this
    is a check on the *reader's* copy of the series and not a second call to the
    formatter that wrote it. A clause the sentence does not carry at all is not
    this check's business (`check_readings_stated` owns what is stated); what it
    owns is that what *is* stated is what was measured.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    peak = max(ys)
    peak_index = len(ys) - 1 - ys[::-1].index(peak)
    after = len(points) - 1 - peak_index
    wall = REPORT.gap_wall_seconds(points)
    holes = REPORT.series_walls(points)
    problems = []

    def note(what, written, measured):
        problem = stated_problem(panel_id, arm, what, written, measured)
        if problem is not None:
            problems.append(problem)

    match = STATED_PEAK_RE.search(text)
    if match is None:
        problems.append(
            f"panel {panel_id!r}: the reading drawn for arm {arm!r} states no "
            "maximum, so the band's own claim cannot be read back against the "
            "series it is drawn from; a caption whose numbers cannot be checked "
            "is the same defect as no caption at all"
        )
    else:
        note("its maximum as", match.group(1), peak)
        note("where its maximum is as", match.group(2), xs[peak_index])
    match = STATED_AFTER_RE.search(text)
    if match is None:
        if STATED_END_RE.search(text) is None:
            problems.append(
                f"panel {panel_id!r}: the reading drawn for arm {arm!r} states "
                "neither what follows its maximum nor that nothing does, so the "
                "one clause that tells a peak which returned from a climb the "
                "window cut off cannot be read back against the series"
            )
        elif after:
            problems.append(
                f"panel {panel_id!r}: the reading drawn for arm {arm!r} states that "
                "nothing follows its maximum, and the series it is drawn from has "
                f"{after} sample(s) after it; the whole point of the clause is to "
                "tell a peak that returned from a climb the window cut off"
            )
    else:
        if int(match.group(1)) != after:
            problems.append(
                f"panel {panel_id!r}: the reading drawn for arm {arm!r} states "
                f"{match.group(1)} sample(s) after its maximum, and the series it "
                f"is drawn from has {after}: a reader told how long a peak "
                "lasted is told a number about a different series"
            )
        if after:
            note("the sample after its maximum as", match.group(2), ys[peak_index + 1])
            note("that sample's time as", match.group(3), xs[peak_index + 1])
        note("its last sample as", match.group(4), ys[-1])
        note("its last sample's time as", match.group(5), xs[-1])
    match = STATED_HOLES_RE.search(text)
    if match is None and (
        STATED_GAP_WALL_RE.search(text) is None
        and STATED_NO_GAP_RE.search(text) is None
    ):
        problems.append(
            f"panel {panel_id!r}: the reading drawn for arm {arm!r} states nothing "
            "about the holes in its own sampling, so whether the line is drawn "
            "across a period nobody observed cannot be checked from the caption; "
            "the holes are the one thing about the shape the pixels cannot carry"
        )
    if match is not None:
        if int(match.group(1)) != len(holes):
            problems.append(
                f"panel {panel_id!r}: the reading drawn for arm {arm!r} states "
                f"{match.group(1)} sample gap(s), and the series it is drawn from "
                f"has {len(holes)}: a hole the reader is not told about is a hole "
                "read as the end of the line"
            )
        if wall is not None:
            note("the least step it would call a gap as", match.group(2), wall)
        if holes:
            largest = max(holes, key=lambda hole: hole[3])
            note("its largest gap as", match.group(3), largest[3])
            note("where that gap starts as", match.group(4), largest[1])
            note("where that gap ends as", match.group(5), largest[2])
    else:
        match = STATED_GAP_WALL_RE.search(text)
        if match is not None and wall is not None:
            note("the least step it would call a gap as", match.group(1), wall)
    return problems


def check_reading_numbers(panel_id, series, markup):
    """Problems that let a drawn caption state numbers its own series did not measure.

    `check_readings_stated` reads the drawn sentence back out and requires it to
    equal the one the formatter produced, which proves the sentence reached the
    panel and proves nothing at all about the sentence being *true* of the
    series: the formatter is the only thing that was ever consulted, so a
    magnitude taken from somewhere else -- the producer's own `[m1-censoring]
    max=` token, another arm's series, a differently filtered one -- is green
    there and authoritative-and-wrong to the reader. This is the same reading
    taken one step further: the numbers are parsed out of the drawn text and
    measured against the drawn points, so the caption and the drawing cannot
    disagree without the render being refused.
    """
    joined = " ".join(drawn_readings(markup))
    if not joined:
        return []
    arms = [name for name, _ in series]
    spans = stated_spans(joined, arms)
    problems = []
    for name, points in series:
        text = spans.get(name)
        if text is None:
            continue
        drawn = REPORT.decimate(points)
        if not drawn:
            continue
        problems += stated_reading_problems(panel_id, name, drawn, text)
    return problems


def axis_tick_labels(markup):
    """The y tick labels a panel draws, in draw order."""
    return AXIS_TICK_RE.findall(markup)


def check_tick_labels_distinct(panel_id, markup):
    """Problems that make an axis unreadable: its ticks repeat a value.

    The ticks are how a reader converts a bar or a line into a number, and a
    band view spans a few percent of its unit: measured on the `M4-imbalance`
    panel, whose axis spans 1.3 % of the share around zero, the six ticks read
    `0.01 0.01 0.01 0.00 0.00 0.00` -- a coarse axis under a panel drawn for a
    1 % departure, i.e. the same defect as an axis whose bound cannot be seen,
    one level down. The resolution is a property of the step between the ticks,
    so the sign of the axis' lower edge must not decide it; this is what keeps
    that applied, measured on the artifact.
    """
    ticks = axis_tick_labels(markup)
    repeated = sorted({tick for tick in ticks if ticks.count(tick) > 1})
    if not repeated:
        return []
    return [
        f"panel {panel_id!r}: its y axis draws {len(ticks)} tick(s) as {ticks}, so "
        f"{len(repeated)} of them repeat ({repeated}); a tick the reader cannot "
        "tell from its neighbour cannot carry the quantity the panel is drawn "
        "to be read against"
    ]


def check_reading_band(panel_id, plot_height):
    """Problems that make the reading band eat the shape it explains."""
    if plot_height >= ARM_READING_MIN_PLOT_PIXELS:
        return []
    return [
        f"panel {panel_id!r}: the per-arm reading band leaves the plot "
        f"{plot_height:.0f} px of the {REPORT.HEIGHT} px canvas, under the "
        f"{ARM_READING_MIN_PLOT_PIXELS:.0f} px a latency panel needs to show the "
        "shape its readings are about; state fewer or shorter readings"
    ]


def check_censoring_drawn(panels, points, censoring):
    """Every arm the run read must be a series some line panel actually draws."""
    line_series = set()
    for panel in panels:
        if panel["chart"] != "line":
            continue
        for entry in panel["series"]:
            if (panel["id"], entry["name"]) in points:
                line_series.add(entry["name"])
    return [
        f"the run's per-arm reading for {arm!r} is about no series any line panel "
        f"of this mandate draws ({sorted(line_series)}), so no panel can state "
        "it: a machine verdict no panel carries is evidence a reader never sees"
        for arm in sorted(censoring or {})
        if arm not in line_series
    ]


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


# -- a bound the run restates per arm -------------------------------------
#
# `M2`'s delivery panel draws one bar per arm against one line. The clean arm
# is asserted at `1.000` and the impaired arms at the run's own floor
# (`0.995`), so a bar at `0.996` crosses the drawn line while being inside its
# own arm's floor: the panel shows a breach the verdict tolerates, and the
# difference between the arms it is comparing -- which floor applies to which
# -- is exactly what it cannot show. The wire panel beside it already names
# the run's per-arm guards (`hostile_wire_guard=10`, `lone_wire_guard=14`); a
# bound the run restates is the same thing, drawn per arm instead of only
# named on one line.
PER_ARM_BOUND_SUFFIXES = ("_guard", "_floor")
"""What a run's key ends in to be that arm's own bound for a quantity."""

QUANTITY_BOUND_SUFFIXES = ("_floor", "_guard", "_budget")
"""What a run's key ends in to be an unprefixed bound for a quantity."""


def run_arm_names(series, run_values):
    """The run's own arm names for a single-series panel, in the run's order.

    A `MANDATE` line names each arm's measurement of a quantity as
    `<arm>_<quantity>`, so the keys ending in the panel's own series name
    enumerate the arms in the order the producer measured them -- which is the
    order the producer writes the panel's bars in. That is what lets a per-arm
    bound be drawn over the arm it belongs to rather than over all of them.

    Where the run states no per-arm measurement of the quantity but does state
    a guard for every arm, the guards' own keys enumerate them
    (`clean_wire_guard` -> arm `clean`), in the run's order. Without that
    fallback a panel drawing exactly the arms the run guards would have no arms
    to place them on, and would name guards it could not draw -- the defect the
    wire panel's sentence had.
    """
    if not isinstance(run_values, dict) or len(series) != 1:
        return []
    quantity = series[0][0]
    names = []
    for key in run_values:
        if not isinstance(key, str) or not key.endswith("_" + quantity):
            continue
        name = key[: -len(quantity) - 1]
        if name and name not in names:
            names.append(name)
    if len(names) >= 2:
        return names
    guarded = []
    for key in run_values:
        if not isinstance(key, str) or not key.endswith(GUARD_KEY_SUFFIX):
            continue
        prefix, _, statistic = key[: -len(GUARD_KEY_SUFFIX)].rpartition("_")
        if statistic and statistic in quantity and prefix not in guarded:
            guarded.append(prefix)
    return guarded if len(guarded) >= 2 else []


def per_arm_guard(arm, quantity, run_values):
    """The run's own guard for one arm's quantity, as ``(key, value)``, or ``None``.

    A `MANDATE` line names a per-arm guard as `<arm>_<statistic>_guard`
    (`hostile_wire_guard`), and the statistic is the *quantity* the guard is
    about: `wire` in the wire panel's series `wire_x`. That is the same filter
    the caption's own clause applies (`run_guards`), so a guard about another
    quantity (`hostile_p99_guard`) is never read as a bound on this panel's
    axis -- a bound on something the panel does not measure would look like
    evidence.
    """
    if not isinstance(run_values, dict):
        return None
    for key in run_values:
        value = run_values[key]
        if not isinstance(key, str) or not key.endswith(GUARD_KEY_SUFFIX):
            continue
        if not numeric(value):
            continue
        prefix, _, statistic = key[: -len(GUARD_KEY_SUFFIX)].rpartition("_")
        if statistic and statistic in quantity and prefix == arm:
            return key, float(value)
    return None


def arm_bound_source(arm, quantity, run_values):
    """The run's own key and value for one arm's bound on a quantity.

    Two shapes, in the run's own order of authority: the run's restated bound
    for the arm (`<arm>_<quantity>_floor`), and then its per-arm *guard* for the
    quantity (`hostile_wire_guard` on a panel whose series is `wire_x`). The
    guard is the shape the caption named and no line carried: a guard a panel
    names is a bound the panel owes the reader, and a named bound the panel does
    not draw is the crossing the reader has to take on trust.
    """
    if not isinstance(run_values, dict):
        return None
    for suffix in PER_ARM_BOUND_SUFFIXES:
        key = f"{arm}_{quantity}{suffix}"
        value = run_values.get(key)
        if numeric(value):
            return key, float(value)
    return per_arm_guard(arm, quantity, run_values)


def per_arm_bound(arm, quantity, run_values):
    """The run's own bound for one arm's quantity, or ``None`` when it states none."""
    source = arm_bound_source(arm, quantity, run_values)
    return None if source is None else source[1]


def quantity_bound(quantity, run_values):
    """The run's own unprefixed bound for a quantity, as ``(key, value)``."""
    if not isinstance(run_values, dict):
        return None
    for suffix in QUANTITY_BOUND_SUFFIXES:
        key = f"{quantity}{suffix}"
        value = run_values.get(key)
        if numeric(value):
            return key, float(value)
    return None


def guarded_arms(arms, run_values):
    """The arms the run states a guard of its own for, whatever the quantity.

    The `MANDATE` line's per-arm guards are how the run says which arms are not
    its reference: `hostile_wire_guard` and `lone_wire_guard` leave the clean
    arm as the one the declaration's own bound governs. That is the same
    division the run's restated floor follows, so it is what decides which
    arms a restated bound is drawn over.
    """
    if not isinstance(run_values, dict):
        return []
    guarded = []
    for arm in arms:
        for key in run_values:
            if not isinstance(key, str) or not key.endswith(GUARD_KEY_SUFFIX):
                continue
            if key[: -len(GUARD_KEY_SUFFIX)].startswith(f"{arm}_"):
                guarded.append(arm)
                break
    return guarded


def arm_bound_values(panel, series, bounds, run_values):
    """Each drawn arm's own bound, or ``None`` when one bound serves them all.

    The split is offered only where it is a fact about the run: one bar series,
    one declared bound, the panel's categories being exactly the arms the run
    enumerates, and the run stating a bound of its own for at least one of them.
    That bound is either a restatement of the quantity's bound at a *different*
    value or the arm's own **guard**. Where it applies, the declared bound
    governs the reference arms and the run's own is drawn over the rest, so the
    panel shows which floor belongs to which arm.

    The guard half is the measured `M2-wire` defect: the panel's caption read
    `run guards hostile_wire_guard=10 lone_wire_guard=14` and the artifact drew
    one line, at the 6x budget, on an axis reaching 14.70 -- so a `lone_tail`
    bar at 6.21x sat *above* the budget on a PASS with no line to cross, and the
    reader had to take the tolerance on trust from a sentence.
    """
    if panel["chart"] != "bar" or len(bounds) != 1 or len(series) != 1:
        return None
    declared = float(bounds[0]["y"])
    arms = run_arm_names(series, run_values)
    categories = sorted({x for _, points in series for x, _ in points})
    if len(arms) < 2 or categories != [float(index + 1) for index in range(len(arms))]:
        return None
    quantity = series[0][0]
    restated = quantity_bound(quantity, run_values)
    own = {arm: arm_bound_source(arm, quantity, run_values) for arm in arms}
    if restated is None and not any(source is not None for source in own.values()):
        return None
    guarded = guarded_arms(arms, run_values)
    if not guarded:
        return None
    values = {}
    for arm in arms:
        if own[arm] is not None:
            values[arm] = own[arm][1]
        elif restated is not None:
            values[arm] = restated[1] if arm in guarded else declared
        else:
            values[arm] = declared
    return values


def effective_bounds(panel, series, bounds, run_values):
    """The bound lines a bar panel draws: one per contiguous run of equal values.

    With no restatement this is the declared bounds, unchanged. With one, each
    segment carries the arms it governs and the x-window it is drawn over, and
    names the run's own key when its value is the run's rather than the
    declaration's -- so a run whose arms have different floors or guards is
    drawn as different lines rather than as one line a reader has to guess at.
    """
    values = arm_bound_values(panel, series, bounds, run_values)
    if values is None:
        return [dict(bound) for bound in bounds]
    arms = list(values)
    quantity = series[0][0]
    restated = quantity_bound(quantity, run_values)
    segments = []
    start = 0
    while start < len(arms):
        value = values[arms[start]]
        end = start + 1
        while end < len(arms) and values[arms[end]] == value:
            end += 1
        run = arms[start:end]
        source = arm_bound_source(run[0], quantity, run_values)
        if source is None and restated is not None:
            source = restated
        segment = {
            "y": value,
            "label": (
                bounds[0]["label"]
                if value == float(bounds[0]["y"])
                else f"run {source[0]}={source[1]:g}"
            ),
            "arms": run,
            "window": [float(index + 1) for index in range(start, end)],
        }
        if source is not None and source[0].endswith(GUARD_KEY_SUFFIX):
            # The key this line *is*, so the label states it once rather than
            # listing it again among the guards the bound is read against.
            segment["guard_key"] = source[0]
        segments.append(segment)
        start = end
    declared_value = float(bounds[0]["y"])
    if all(segment["y"] != declared_value for segment in segments):
        # Every arm of this run states a bound of its own, so the declaration's
        # own is no arm's assertion -- and `render_mandate` requires a declared
        # bound to be labelled, so it is drawn across the panel saying exactly
        # that rather than being dropped or left reading as one arm's floor.
        segments.append(
            {
                "y": declared_value,
                "label": bounds[0]["label"],
                "arms": [],
                "window": [1.0, float(len(arms))],
                "governs_none": True,
            }
        )
    return segments


def with_drawn_guards(plan):
    """Tag a bar panel's bound plan where the run's guards are drawn as lines.

    A guard the panel draws gets a line of its own labelling the arms it
    governs, so the sentence that used to name it on the bound it is read
    against would only repeat what the line beside it already shows. Both the
    drawing (`drawable_bounds`) and the check that demands the drawn labels
    (`check_bound_arm_governance`) read the plan through here, so they cannot
    disagree about whether the guards are drawn.
    """
    if any(bound.get("guard_key") is not None for bound in plan):
        for bound in plan:
            bound.setdefault("guards_drawn", True)
    return plan


def series_guard_bounds(panel, series, bounds, run_values):
    """The bound lines a bar panel owes the run's per-*series* guards.

    `M2-wire` names its guards per arm (`hostile_wire_guard`) and
    `effective_bounds` draws them over the arm's own band. A panel whose series
    are statistics rather than arms -- `M4-latency` draws `clean_p50`,
    `clean_p99`, `hostile_p50`, `hostile_p99` -- names them as the series plus
    the guard suffix, and that guard's line spans the plot, labelled with the
    series it governs. Measured on a recorded `M4-latency` panel, its caption
    read `run guards hostile_p99_guard=900` over an axis reaching 945 and the
    artifact drew one line (the 250 ms ceiling), so its four bars past the
    ceiling on a PASS had no line showing they were inside their own arm's
    guard.

    Nothing is guessed: the guard's key has to be a drawn series' name plus the
    suffix exactly, so a guard whose statistic merely shares a token with a
    drawn series (`hostile_p99_guard` against a panel drawing `clean`/
    `hostile`) stays a clause on the bound rather than becoming a line on a
    quantity it does not bound.
    """
    if panel["chart"] != "bar" or len(bounds) != 1:
        return []
    names = [name for name, _ in series]
    declared = float(bounds[0]["y"])
    extra = []
    for key, value in run_guards(run_values, series, bounds[0]["label"]):
        owner = key[: -len(GUARD_KEY_SUFFIX)]
        if owner not in names or value == declared:
            continue
        extra.append(
            {
                "y": value,
                "label": f"run {key}={value:g}",
                "series": owner,
                "guard_key": key,
            }
        )
    return extra


def check_bound_arm_governance(panel_id, panel, series, bounds, run_values, markup):
    """Problems that leave a bound drawn across arms with different floors unnamed.

    "Does each drawn bound apply to every series it crosses?" is `AGENTS.md`'s
    second panel test, and a bound the run restates per arm is the bar panel's
    case of it: one line drawn across arms whose own floors differ reads as the
    floor of whichever arm crossed it, which is a breach for one arm and a pass
    for its neighbour. So every segment the run's own bounds imply has to be on
    the panel, each naming the arms it governs.

    A **line** panel is the same rule with one line it cannot split: its arms
    share the time axis, so the run's per-arm guards cannot be drawn as
    segments. There the bound names, per arm, the key and value the run asserts
    for it (`arm_guard_clause`), and this refuses a panel whose drawn label
    does not carry that clause -- which is the shape that let a `lone_tail`
    peak of 1567.1 ms cross the 250 ms ceiling on a panel saying nothing about
    the arm's own 3200 ms p99 guard.
    """
    planned = bar_bound_plan(panel, series, bounds, run_values)
    # The *run's* per-arm plan, not the declaration's bands: this check is about
    # one line drawn across arms whose own floors differ, while a band's two arms
    # are the declaration's own and are measured by `check_two_sided_bound_drawn`.
    drawn = [declared for declared, _, _ in label_boxes(markup)]
    problems = []
    if panel["chart"] != "bar":
        clause = arm_guard_clause(series, run_values)
        if not clause:
            return []
        for bound in bounds:
            label = governed_label(bound, series, run_values, crossing=False)
            if label in drawn:
                continue
            problems.append(
                f"panel {panel_id!r}: the run states the guards {clause!r} for "
                f"the arms this bound crosses, so one line across every series "
                "would be the bound of none of them; the panel has to name which "
                f"arm it governs and what the other arms are read against. "
                f"Missing: {label!r} (drawn: {drawn!r})"
            )
        return problems
    if len(planned) <= len(bounds):
        return []
    for bound in planned:
        label = governed_label(bound, series, run_values)
        if label in drawn:
            continue
        problems.append(
            f"panel {panel_id!r}: the run states the bound {planned[0]['label']!r} "
            f"for some arms and {planned[-1]['label']!r} for others, so one line "
            "across every bar would be the floor of neither; the panel has to "
            "draw each arm's own bound and name the arms it governs. Missing: "
            f"{label!r} (drawn: {drawn!r})"
        )
    return problems


def named_guard_values(series, bounds, run_values, *, crossing):
    """The run's own guards the panel's drawn labels will name on its own axis.

    Only what a label actually says counts as named. A *bar* panel's bound
    carries the guards for the quantity its bars plot, so the axis has to carry
    them too — whether or not a bar has crossed yet, because the range is the
    reason the crossing is drawable at all when it comes.

    A *line* panel's bound names the run's per-arm guards instead
    (`arm_guard_clause`), and those are not values on the axis the panel plots:
    `lone_p99_guard=3200` is a bound on one *statistic* of that series and
    `lone_over250_guard=8%` is not in the y unit at all. Pulling them onto the
    axis would put the 250 ms ceiling at 3 % of its height and delete the
    crossing the panel exists to show — the defect this docstring used to
    describe as `lone_p999_guard=8000` blowing the axis up to eight thousand
    milliseconds. What the reader compares a guard against is the arm's own
    stated peak, which the reading band carries; the legend and the clause name
    the statistic, so the value is read against the right quantity.
    """
    if not crossing or not isinstance(run_values, dict):
        return []
    guards = set()
    for bound in bounds:
        guards.update(
            value for _, value in run_guards(run_values, series, bound["label"])
        )
    return sorted(guards)


def axis_with_headroom(low, high, top, plot_height, bottom=None):
    """Widen an axis so a value past its highest (or lowest) bound is drawable.

    Two margins, because they answer different questions. `FRAME_HEADROOM` of
    the span is the frame's own breathing room, so a line or tick at the top of
    an axis does not merge with its border. `MIN_HEADROOM_PIXELS` is the
    reader's: the highest bound the panel names needs enough axis above it that
    a bar which crosses it is a bar, not a smudge — and on a panel whose data
    spread is tiny against the bound (a fair share pinned at 25 %) the span's
    own 5 % is under a pixel, so the pixel floor is what does the work.

    `bottom` is the same requirement on the other side, for a bound a bar can
    fail *downwards*: a `±` band's lower arm, or a floor the data can fall
    through. Without it the axis' low is the data's own minimum — measured on
    `M4-imbalance`, `-0.0002` against a `-1 %` arm — so the arm is drawn off the
    frame and a starved flow's departure is a small bar that crosses nothing.
    The two adjustments are applied in this order and each keeps the other's
    margin: lowering `low` only widens the span the upper margin is measured in,
    so a margin already met stays met.
    """
    span = high - low
    if span > 0:
        high = high + FRAME_HEADROOM * span
    # The margin the *policy* targets is a hair over the one the checks demand:
    # the adjustment lands on the constraint's boundary, and a boundary met only
    # to the last bit of a double reads as `5.999... px` to `check_bound_headroom`
    # and would refuse the panel the policy just widened. A millionth of a pixel
    # is far below anything drawable and far above the rounding it answers.
    minimum = MIN_HEADROOM_PIXELS * (1.0 + 1e-6) / plot_height
    if top > high:
        return (low, high)
    if minimum < 1.0 and high - top < minimum * (high - low):
        high = max(high, (top - minimum * low) / (1.0 - minimum))
    if bottom is not None and minimum < 1.0:
        if bottom < low:
            return (low, high)
        if bottom - low < minimum * (high - low):
            low = min(low, (bottom - minimum * high) / (1.0 - minimum))
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
      exists to explain actually lies. The *failing* side of the lowest such
      bound is carried too: a two-sided band's lower arm, or a floor the data
      can fall through, needs `MIN_HEADROOM_PIXELS` below it for the same
      reason a ceiling needs them above it.
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
    # Room is owed below a bound a bar can fail *through*, and only there: a
    # ceiling (`failure_side` +1) needs it above, a floor (-1) below, and a
    # bound the bars are read *at* (0, a fair share) is no line any of them
    # crosses and owes neither. A band's lower arm is downward-failing by
    # construction, whatever this run's bars happen to do: a flow below `-y`
    # has starved, and the axis must keep the room that bar needs to be drawn.
    floors = [
        float(bound["y"])
        for bound in bounds
        if bound.get("band_arm") == "lower"
        or failure_side(values, float(bound["y"])) < 0.0
    ]
    return axis_with_headroom(
        low,
        high,
        max(named) if named else high,
        plot_height,
        bottom=min(floors) if floors else None,
    )


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


def check_panel_axis(
    panel_id, series, bounds, extent, plot_height=None, run_values=None, *, stated=None
):
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

    ``stated`` names the bounds whose sliver the panel states instead
    (`sliver_bound_statements`): a bound whose band the axis cannot resolve is
    refused *unless* the panel draws it and says, with the measured distance,
    where it sits. That alternative exists because a refusal deletes the panel,
    and for a fault render the panel is the evidence that the guard fired. It is
    owed only where the panel draws a departure at least as legible as the band
    it cannot resolve, so a run whose data sits at the bound still gets the
    refusal — there the alternative would state nothing but the sliver itself.
    """
    problems = []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    values = _bound_values(series)
    for bound in bounds:
        y = float(bound["y"])
        if bound_side(values, y) is None and not crossing_values(values, y):
            continue
        band, pixels = bound_band_pixels(
            series, bounds, bound, extent, plot_height, run_values
        )
        if pixels >= MIN_BOUND_PIXELS:
            continue
        if stated and str(bound["label"]) in stated:
            continue
        nearest = min((abs(value - y) for value in values), default=0.0)
        problems.append(
            f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} leaves the "
            f"bound {bound['label']!r} (y={y:g}) a band of {band:.4g} "
            f"({band / span:.1%} of its height, {pixels:.1f} px of "
            f"{plot_height:.0f}), under the {MIN_BOUND_PIXELS:.0f} px a bound "
            "needs to show the departure it exists to catch, so that "
            "departure would be sub-pixel. The panel may draw the bound and "
            "state its position with the measured distance instead (this run's "
            f"nearest bar is {nearest:.4g} from it, "
            f"{nearest / span * plot_height:.1f} px of this axis); it states "
            "nothing, so the panel is refused rather than drawn"
        )
    return problems


def bound_band_pixels(series, bounds, bound, extent, plot_height, run_values=None):
    """The band the axis test measures for one bound, and the pixels it has.

    One function for the refusal and for the statement a panel makes in its
    place, so the band a panel is refused for and the band it states are the same
    measurement: `check_panel_axis`, `sliver_bound_statements` and
    `check_sliver_bound_stated` all read it here. The band is the proxy
    `bound_band` derives — the nearest value on the bound's own side, the
    tolerance the run's own guards open, or a fraction panel's resolution floor —
    measured against the run's guards where the bound's label names one.
    """
    values = _bound_values(series)
    unit = unit_span(values, [item["y"] for item in bounds])
    tolerances = [
        value for _, value in run_guards(run_values, series, bound["label"])
    ]
    band = bound_band(values, float(bound["y"]), unit, tolerances)
    span = extent[1] - extent[0]
    return band, band / span * plot_height


def sliver_number(value):
    """One number as the sliver statement writes it and the check reads it back."""
    return f"{value:g}"


def sliver_departure(values, bound, extent, plot_height):
    """``(pixels, value)``: the most legible departure the bars draw from a bound.

    The side is the one the bound can fail towards, and a bar on the other side
    is a pass rather than a failure: a **cap**'s bars sit under it, a **floor**'s
    over it, and a bound a run's bars straddle — or one arm of a declared `±`
    band, whose mirror carries `band_arm` — fails on either side. The side
    matters and the *distance* alone cannot stand in for it: measured on the
    `M2 wire budget 6x` panel with no guard supplied, the furthest bar is the
    *lowest* one (`2.12`, 140 px below a 6x budget it is well inside), which is
    the bar's length and not a breach. The refusal there is right, so the
    statement is reserved for a panel that draws the departure.
    """
    y = float(bound["y"])
    band_arm = bound.get("band_arm") is not None or two_sided_bound(bound)
    side = None if band_arm else bound_side(values, y)
    if side == "cap":
        candidates = [value for value in values if value > y]
    elif side == "floor":
        candidates = [value for value in values if value < y]
    else:
        candidates = list(values)
    if not candidates:
        return 0.0, None
    furthest = max(candidates, key=lambda value: abs(value - y))
    span = extent[1] - extent[0]
    return abs(furthest - y) / span * plot_height, furthest


def bound_sliver_statement(bound, values, band, extent, plot_height):
    """The sentence that states a sub-pixel bound's position, or ``""``.

    `check_panel_axis` refuses a bound whose band the axis cannot resolve,
    because a departure of the bound's own size would be sub-pixel. That refusal
    is right while the panel's data sits *at* the bound: the band is then the
    only place the failure could show, and the delivery floor drawn over `0..2`
    is the measured case. It is the wrong answer once the run's own data lies far
    enough beyond the bound that the axis is the data's scale and not the
    bound's: the fault renders are exactly that, and refusing them deleted the
    visual evidence that the guard fires.

    What makes the panel honest there is the statement, not a widened axis — the
    bound is drawn, its position is stated, and the distance from it to the bars
    the run drew is stated with it, so the reader has the number the sliver took
    away. A statement is owed only when the panel *shows* a departure at least as
    legible as the band it cannot resolve (`MIN_BOUND_PIXELS`, on the side the
    bound can fail towards — `sliver_departure`), which is why the ordinary case
    keeps the refusal: there the only thing to state is the sliver itself. The
    numbers are the drawn points' and the drawn axis' own, so
    `check_sliver_bound_stated` measures them back out of the artifact.
    """
    if not values:
        return ""
    low, high = extent
    span = high - low
    y = float(bound["y"])
    if band / span * plot_height >= MIN_BOUND_PIXELS:
        return ""
    departure_px, departure = sliver_departure(values, bound, extent, plot_height)
    if departure is None or departure_px < MIN_BOUND_PIXELS:
        return ""
    nearest = min(values, key=lambda value: abs(value - y))
    return (
        f'bound "{bound["label"]}" at {sliver_number(y)} on axis '
        f"{sliver_number(low)}..{sliver_number(high)}: band "
        f"{sliver_number(band)} = {band / span * plot_height:.1f} px; nearest "
        f"bar {sliver_number(nearest)}, {sliver_number(abs(nearest - y))} away; "
        f"furthest {sliver_number(departure)}, "
        f"{departure_px:.1f} px from the bound"
    )


def sliver_bound_statements(series, bounds, extent, plot_height, run_values=None):
    """``[(bound, sentence)]`` for every bound whose sliver the panel owes stated.

    The bounds are the ones `check_panel_axis` measures — a bound a bar can
    fail, or one a minority of them cross — whose band is sub-pixel and whose run
    draws a departure legible enough to make the statement the honest reading.
    A bound the axis test does not measure owes no statement: it is a reference
    the bars straddle, not a line a bar crosses.

    A mirrored arm whose line is within a label height of the one being stated
    cannot carry a label of its own — two labels that close are the text of
    neither, and `check_label_overlap` refuses them — so it is drawn unlabelled
    and its own position goes into this sentence: the declaration's label
    already says the bound is a band, and the statement says where each arm is.
    Measured on the `M4_drop` fault render, the `±1 %` arms sat `4.3 px` apart on
    a `-1..0.0605` axis, which is closer than one line of label text.
    """
    values = _bound_values(series)
    span = extent[1] - extent[0]
    statements = []
    for bound in bounds:
        y = float(bound["y"])
        if bound_side(values, y) is None and not crossing_values(values, y):
            continue
        band, _ = bound_band_pixels(
            series, bounds, bound, extent, plot_height, run_values
        )
        sentence = bound_sliver_statement(bound, values, band, extent, plot_height)
        if not sentence:
            continue
        for other in bounds:
            if other is bound or other.get("band_arm") is None:
                continue
            gap = abs(float(other["y"]) - y) / span * plot_height
            if gap >= REPORT.LABEL_LINE_HEIGHT_PX:
                continue
            other["unlabelled"] = True
            sentence += (
                f'; its arm at {sliver_number(float(other["y"]))} '
                f"({gap:.1f} px away) is drawn unlabelled"
            )
        statements.append((bound, sentence))
    return statements


def sliver_statement_problems(panel_id, bound, matches, values, band, extent, plot_height):
    """Problems that make a drawn sliver statement's numbers not the run's.

    Every number the statement carries is measured against the drawn points and
    the drawn axis: the bound's own position, the axis' ends, the band and its
    pixels, and the nearest and furthest bars with their distances. A statement
    is the part of a sliver-bound panel a reader trusts *instead of* the pixels,
    so a number that came from anywhere but the artifact would be authoritative
    and wrong — the same defect `check_reading_numbers` exists for on the
    latency band.
    """
    low, high = extent
    span = high - low
    y = float(bound["y"])
    nearest = min(values, key=lambda value: abs(value - y))
    departure_px, departure = sliver_departure(values, bound, extent, plot_height)
    measured = {
        "value": y,
        "low": low,
        "high": high,
        "band": band,
        "band_px": band / span * plot_height,
        "near": nearest,
        "near_dist": abs(nearest - y),
        "far": departure,
        "far_px": departure_px,
    }
    problems = []
    for match in matches:
        for field, expected in measured.items():
            printed = float(match.group(field))
            if field.endswith("_px"):
                slack = 0.05
            else:
                slack = max(1e-4 * abs(expected), 1e-9)
            if abs(printed - expected) <= slack:
                continue
            problems.append(
                f"panel {panel_id!r}: the statement for the bound "
                f"{bound['label']!r} prints {field}={printed:g}, where the drawn "
                f"points and the drawn axis {low:.4g}..{high:.4g} measure "
                f"{expected:g}; a stated distance that is not the run's is a "
                "claim the reader has no way to check"
            )
    return problems


def check_sliver_bound_stated(
    panel_id, series, bounds, extent, markup, plot_height=None, run_values=None
):
    """Problems that leave a sub-pixel bound's own position unstated.

    The axis test's refusal (`check_panel_axis`) is answered by a statement, and
    this is the check on that answer: a panel that draws a bound whose band its
    axis cannot resolve owes, on its own face, where the bound sits, how wide its
    band is in pixels, and how far the nearest and furthest bars lie from it.
    Two ways to fail it. A panel that draws the sliver and says nothing leaves the
    reader able to see a departure and unable to read it against the bound, which
    is the same panel as one drawn silently; and a panel whose stated numbers are
    not the drawn points' own is worse than silent, because the reader is told a
    distance the run did not measure.

    This reads both sides off the artifact: the bounds and bands are the ones
    `bound_band_pixels` measures from the CSV, the drawn points and the axis the
    renderer used, and the statements are parsed back out of the SVG's own note
    elements (`drawn_notes`), the way every other check here measures the panel
    that was written rather than the plan it was written from.
    """
    values = _bound_values(series)
    if not values:
        return []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    stated = {}
    for match in SLIVER_STATEMENT_RE.finditer(" ".join(drawn_notes(markup))):
        stated.setdefault(match.group("label"), []).append(match)
    drawn_labels = {declared for declared, _, _ in label_boxes(markup)}
    problems = []
    for bound in bounds:
        y = float(bound["y"])
        if bound_side(values, y) is None and not crossing_values(values, y):
            continue
        band, pixels = bound_band_pixels(
            series, bounds, bound, extent, plot_height, run_values
        )
        if pixels >= MIN_BOUND_PIXELS:
            continue
        matches = stated.get(str(bound["label"]))
        if not matches:
            problems.append(
                f"panel {panel_id!r}: the bound {bound['label']!r} (y={y:g}) has a "
                f"band of {band:.4g} ({pixels:.1f} px of "
                f"{plot_height:.0f} on the axis {low:.4g}..{high:.4g}), under the "
                f"{MIN_BOUND_PIXELS:.0f} px it needs to show the departure it "
                "exists to catch, and the panel states nothing about where the "
                "bound sits: a reader can see a departure and cannot read it "
                "against the bound, which is the same panel as one drawn silently"
            )
            continue
        problems.extend(
            sliver_statement_problems(
                panel_id, bound, matches, values, band, extent, plot_height
            )
        )
        # A mirrored arm drawn unlabelled owes its own position in the sentence:
        # the arm's label is the number a bar falling to that side is read
        # against, so a panel that drops the label and states nothing about the
        # arm is the panel drawn silently one level down.
        for other in bounds:
            if other is bound or other.get("band_arm") is None:
                continue
            gap = abs(float(other["y"]) - y) / (high - low) * plot_height
            if gap >= REPORT.LABEL_LINE_HEIGHT_PX:
                continue
            if str(other["label"]) in drawn_labels:
                continue
            if any(
                match.group("arm") is not None
                and abs(float(match.group("arm")) - float(other["y"])) <= 1e-9
                and abs(float(match.group("arm_px")) - gap) <= 0.05
                for match in matches
            ):
                continue
            problems.append(
                f"panel {panel_id!r}: the band arm at {float(other['y']):g} is "
                f"drawn {gap:.1f} px from the stated arm, too close for a label of "
                "its own, and the panel draws no label for it and states nothing "
                "about where it is: an arm neither labelled nor stated is a line "
                f"the reader cannot read a bar against"
            )
    return problems


# -- the panel summary: what a panel drew, stated with the panel -------------
#
# "The tool's own message is the oracle": a panel the master can only read from
# its pixels makes the reader infer the producer's answer from a render, which is
# how four artifacts were mis-read in one day. So a panel carries, and a refused
# plot step enforces, a summary of what it drew -- its axis and units, its
# series, every bound line with the pixel it sits at, the band it has and why it
# is drawn, and the reading its data gives in the quantity's own units. The
# summary is written beside the SVG/PNG, returned in the runner's JSON, and
# printed by the runner, so the master never has to open an SVG to know what it
# shows.
#
# `check_panel_summary_stated` is `check_readings_stated`'s family -- a claim
# measured back out of the artifact -- extended from one caption to the whole
# panel: the numbers a summary prints are recomputed from the drawn points, the
# drawn axis and the drawn bound lines, and a summary that is absent or false
# refuses the panel exactly as a sub-pixel band does. This is the summary of
# *every* panel, including one with no bound (`none by design`), so "absent
# summary" is never a legitimate state.
PANEL_SUMMARY_RE = re.compile(
    r'<desc class="panel-summary">(?P<body>.*?)</desc>', re.S
)
SVG_OPENING_RE = re.compile(r"<svg\b[^>]*>")
LEGEND_TEXT_RE = re.compile(r"<text[^>]*>(?P<label>[^<]*)</text>")
BOUND_DRAWN_LABELLED = "labelled"
BOUND_DRAWN_UNLABELLED = "unlabelled"
BOUND_DRAWN_STATED_SLIVER = "stated-as-sliver"
SUMMARY_PIXEL_SLACK = 0.05


def bound_reason(bound):
    """Why a drawn bound line is on the panel, as one token.

    The plan decides it and the summary states it, so a reader of a panel with
    several lines knows which is the declaration's and which is the run's own
    arm's, guard's or series' bound without reconstructing `bar_bound_plan`.
    """
    if bound.get("band_arm") is not None:
        return "declaration-band"
    if bound.get("guard_key") is not None:
        return "run-guard"
    if bound.get("series"):
        return "series-guard"
    if bound.get("arms"):
        return "run-arm"
    if bound.get("governs_none"):
        return "declaration-unclaimed"
    return "declaration"


def mandate_fault(mandate, fault):
    """The deliberate-fault selector that belongs to this mandate, or ``None``.

    The producer's selector names the mandate it perturbs (`M4_drop` perturbs
    M4), and a run that took a fault owes every panel it drew the statement that
    it is a fault render -- nobody may read a fault render as a refusal, and
    nobody should have to compare two directories to learn that a panel was
    drawn from perturbed input.
    """
    if not fault:
        return None
    value = str(fault).strip()
    if value == mandate or value.startswith(mandate + "_"):
        return value
    return None


def drawn_series_name(chart, name):
    """The series name the panel's own legend draws for this chart.

    Both chart families label their legend through `series_label` -- the bar
    chart applies it as it draws, and a line/CDF chart is handed the prettified
    names -- so the summary names the legend's own text rather than the
    producer's column name.
    """
    return series_label(name)


def legend_series_labels(markup):
    """The series labels a panel's legend draws, in draw order."""
    match = LEGEND_GROUP_RE.search(markup)
    if match is None:
        return []
    return [html.unescape(label) for label in LEGEND_TEXT_RE.findall(match.group(1))]


def panel_reading(chart, series, bounds, extent, plot_height):
    """What a panel's data says, in the quantity's own units, as one sentence.

    Per series it is the drawn range and the number of drawn points, and per
    bound the furthest departure the bars draw with its pixel distance -- the two
    things a reader otherwise has to measure off the render. Both are recomputed
    from the drawn points and the drawn axis by `check_panel_summary_stated`, so
    a sentence that is not the run's is refused.
    """
    values = _bound_values(series)
    span = extent[1] - extent[0]
    parts = []
    for name, points in series:
        scores = [value for _, value in points]
        if not scores:
            continue
        parts.append(
            f"{drawn_series_name(chart, name)} {sliver_number(min(scores))}.."
            f"{sliver_number(max(scores))} ({len(scores)} pts)"
        )
    for bound in bounds:
        # The panel's own definition of a departure is `crossing_values` (the
        # minority beyond the line, which `governed_label` counts), so the
        # reading uses it rather than a bare furthest value: on `M4-latency`
        # under `M4_drop` the furthest value from the 250 ms ceiling is the
        # fault's *starved* `0`, 60 px below a cap it passes, and reporting it
        # as the bound's departure would name the wrong direction.
        crossed = crossing_values(values, bound["y"])
        if not crossed:
            continue
        y = float(bound["y"])
        furthest = max(crossed, key=lambda value: abs(value - y))
        pixels = abs(furthest - y) / span * plot_height
        if pixels < MIN_BOUND_PIXELS:
            continue
        # `beyond`/`under` are the words `governed_label` already uses for a
        # crossing, so the reading states the panel's own attribution rather
        # than a second one: on a cap whose bars straddle it, the minority below
        # is what the label calls "under it", not a breach the reading invents.
        side = "beyond" if furthest > y else "under"
        parts.append(
            f"{len(crossed)} of {len(values)} values {side} {bound['label']!r} "
            f"by up to {sliver_number(abs(furthest - y))} ({pixels:.1f} px)"
        )
    return "; ".join(parts)


def panel_summary_document(
    panel_id,
    chart,
    x_label,
    y_label,
    series,
    bounds,
    extent,
    markup,
    stated_labels,
    plot_height,
    run_values=None,
    fault=None,
):
    """The summary a panel carries: what it drew, with the drawn coordinates.

    The pixel position and the band pixels of each bound are measured the way the
    other checks measure them (`DRAWN_BOUND_RE` for the drawn `y1`, and
    `bound_band_pixels` for the band), and the series' range and point count come
    from the drawn points, so the document is the drawn panel's own arithmetic
    rather than a second, possibly disagreeing, computation.
    """
    drawn_pixels = [float(y) for y in DRAWN_BOUND_RE.findall(markup)]
    drawn_labels = [declared for declared, _, _ in label_boxes(markup)]
    entries = []
    for index, bound in enumerate(bounds):
        label = str(bound["label"])
        # A drawn label carries the bound's own label *plus* the clauses naming
        # what it governs (and a wrap splits it), so the label's presence is a
        # prefix, the same test `render_mandate` uses for "every declared bound
        # is labelled".
        has_label = any(declared.startswith(label) for declared in drawn_labels)
        if label in stated_labels:
            state = BOUND_DRAWN_STATED_SLIVER
        elif bound.get("unlabelled") or not has_label:
            state = BOUND_DRAWN_UNLABELLED
        else:
            state = BOUND_DRAWN_LABELLED
        # The band is the bar panel's measure -- a line panel's crossing of a
        # ceiling is the line above it, and `check_panel_axis` does not apply its
        # band test there -- so a line/CDF panel states `n/a` rather than a
        # coincidental sliver at the nearest sample, which is a number a reader
        # could mistake for a defect.
        band_pixels = None
        if chart == "bar":
            _, band_pixels = bound_band_pixels(
                series, bounds, bound, extent, plot_height, run_values
            )
        entries.append(
            {
                "label": label,
                "value": float(bound["y"]),
                "px": drawn_pixels[index] if index < len(drawn_pixels) else None,
                "band_px": band_pixels,
                "reason": bound_reason(bound),
                "drawn": state,
            }
        )
    return {
        "panel": panel_id,
        "chart": chart,
        "axis": [float(extent[0]), float(extent[1])],
        "x_label": x_label,
        "y_label": y_label,
        "series": [
            {
                "name": drawn_series_name(chart, name),
                "points": len(points),
                "min": min((value for _, value in points), default=None),
                "max": max((value for _, value in points), default=None),
            }
            for name, points in series
        ],
        "bounds": entries,
        "reading": panel_reading(chart, series, bounds, extent, plot_height),
        "fault": fault,
    }


def panel_summary_block(document):
    """The summary as the compact human-readable block every reader sees."""
    axis = document["axis"]
    lines = [
        f"panel {document['panel']}  chart={document['chart']}  "
        f"axis={sliver_number(axis[0])}..{sliver_number(axis[1])}  "
        f"x={document['x_label']}  y={document['y_label']}"
    ]
    if document["series"]:
        lines.append(
            "  series: "
            + "; ".join(
                f"{entry['name']} {entry['points']} pts "
                f"{sliver_number(entry['min'])}..{sliver_number(entry['max'])}"
                for entry in document["series"]
            )
        )
    if document["bounds"]:
        for entry in document["bounds"]:
            pixel = "unplaced" if entry["px"] is None else f"{entry['px']:.1f}"
            band = (
                "n/a"
                if entry["band_px"] is None
                else f"{entry['band_px']:.1f}px"
            )
            lines.append(
                f"  bound: {entry['label']!r} y={sliver_number(entry['value'])} "
                f"px={pixel} band={band} "
                f"reason={entry['reason']} drawn={entry['drawn']}"
            )
    else:
        lines.append("  bound: none by design")
    lines.append(f"  reading: {document['reading']}")
    if document.get("fault"):
        lines.append(
            f"  fault: {document['fault']} — a deliberate input fault on this "
            "mandate's arm; the arm it perturbs failed as intended, so this is a "
            "fault render and not a refused panel"
        )
        slivers = [
            entry
            for entry in document["bounds"]
            if entry["drawn"] == BOUND_DRAWN_STATED_SLIVER
            and entry["band_px"] is not None
        ]
        if slivers:
            lines.append(
                "  fault scale: "
                + "; ".join(
                    f"{entry['label']!r} is {entry['band_px']:.1f} px of this "
                    f"axis and stated at px={entry['px']:.1f}"
                    for entry in slivers
                )
            )
    return "\n".join(lines)


def introduce_panel_summary(markup, document):
    """Insert a panel's summary as a machine-readable ``<desc>`` in its SVG."""
    body = html.escape(json.dumps(document, sort_keys=True))
    desc = f'<desc class="panel-summary">{body}</desc>'
    match = SVG_OPENING_RE.search(markup)
    if match is None:
        return markup
    return markup[: match.end()] + desc + markup[match.end() :]


def read_panel_summary(markup):
    """The summary a written panel carries, or ``None`` when it carries none."""
    match = PANEL_SUMMARY_RE.search(markup)
    if match is None:
        return None
    try:
        document = json.loads(html.unescape(match.group("body")))
    except (ValueError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def check_panel_summary_stated(
    panel_id,
    chart,
    x_label,
    y_label,
    series,
    bounds,
    extent,
    markup,
    plot_height,
    stated_labels,
    run_values=None,
    fault=None,
):
    """Problems that leave a panel without a true statement of what it drew.

    `AGENTS.md` makes a panel that cannot show its failure a defect; a panel that
    cannot *say* what it shows is the same defect read by a person, and it is how
    four artifacts were mis-read in one day. So the summary is mandatory and is
    checked against the drawn coordinates: the parsed `<desc>` must equal the
    document recomputed from the drawn points, the drawn axis and the drawn bound
    lines, the number of summary bounds must equal the number of ``class="bound"``
    lines the SVG draws, each stated pixel must be a drawn line's own ``y1``, and
    the series names must be the legend's own labels. An absent summary and a
    false one are refused alike, the way an absent reading and a false reading
    are.
    """
    expected = panel_summary_document(
        panel_id,
        chart,
        x_label,
        y_label,
        series,
        bounds,
        extent,
        markup,
        stated_labels,
        plot_height,
        run_values,
        fault,
    )
    match = PANEL_SUMMARY_RE.search(markup)
    if match is None:
        return [
            f"panel {panel_id!r}: it carries no panel summary, so the only thing "
            "that says what it drew is the render and the reader has to infer the "
            "producer's answer from it. Every panel owes a <desc "
            'class="panel-summary"> stating its axis, its series, every bound '
            "line with its pixel position and why it is drawn, and its reading; "
            "a plot step that cannot produce one is refused"
        ]
    try:
        stated = json.loads(html.unescape(match.group("body")))
    except (ValueError, TypeError) as error:
        return [
            f"panel {panel_id!r}: its panel summary is not readable JSON "
            f"({error}), so nothing can be measured against it"
        ]
    problems = []
    if not isinstance(stated, dict):
        return [
            f"panel {panel_id!r}: its panel summary is a {type(stated).__name__}, "
            "not an object of the panel's own fields"
        ]
    for key in expected:
        if key in stated and stated[key] == expected[key]:
            continue
        problems.append(
            f"panel {panel_id!r}: its summary's {key!r} is {stated.get(key)!r} "
            f"where the drawn panel measures {expected[key]!r}; a summary that is "
            "not the drawn panel's is worse than none, because the reader trusts "
            "it instead of the pixels"
        )
    extra = sorted(set(stated) - set(expected))
    if extra:
        problems.append(
            f"panel {panel_id!r}: its summary carries field(s) {extra} the drawn "
            "panel does not define, so at least one number is about something "
            "other than this render"
        )
    drawn_lines = [float(y) for y in DRAWN_BOUND_RE.findall(markup)]
    if len(drawn_lines) != len(bounds):
        problems.append(
            f"panel {panel_id!r}: it draws {len(drawn_lines)} bound line(s) and "
            f"its summary states {len(bounds)}, so a drawn bound is unaccounted "
            "for or a stated one is not drawn"
        )
    else:
        for entry, drawn_y in zip(expected["bounds"], drawn_lines):
            if entry["px"] is None or abs(entry["px"] - drawn_y) <= SUMMARY_PIXEL_SLACK:
                continue
            problems.append(
                f"panel {panel_id!r}: its summary states the bound "
                f"{entry['label']!r} at px={entry['px']}, where the SVG draws that "
                f"line at y1={drawn_y}; the summary has to be the drawn geometry"
            )
    legend = legend_series_labels(markup)
    expected_names = [entry["name"] for entry in expected["series"]]
    if legend and legend != expected_names:
        problems.append(
            f"panel {panel_id!r}: its summary names the series {expected_names}, "
            f"where the drawn legend names {legend}; a summary of series the panel "
            "does not draw is a claim about another panel"
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


def check_named_guards_drawn(panel_id, guards, extent, markup, plot_height=None):
    """Problems that make a guard the panel *names* unreadable: no line at it.

    `AGENTS.md`'s second panel test asks whether each drawn bound applies to
    every series it crosses; the mirror of it is whether every bound the panel
    names is drawn at all. A caption reading `run guards hostile_wire_guard=10
    lone_wire_guard=14` on a panel whose only line is the 6x budget tells the
    reader a tolerance the artifact cannot show: a bar above the budget and
    below its own guard is *between two lines* on the evidence and one line
    plus a sentence on the panel. Measured on the recorded `M2-wire` artifact,
    the axis reached 14.70 -- so both guards fit -- and the SVG carried one
    `class="bound"` line, at 6.0.

    Both sides are read off the artifact: the guards are the values the panel's
    own drawn labels name (`named_guard_values`), and the lines are the y each
    drawn bound sits at (`drawn_bound_values`, back through the drawn axis).
    """
    if not guards:
        return []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    drawn = drawn_bound_values(markup, extent)
    # A drawn y is printed to a tenth of a pixel, so half a pixel of the axis is
    # the widest a rounding artefact can be; anything further is another line.
    slack = 0.5 / plot_height * span
    problems = []
    for value in guards:
        if any(abs(value - line) <= slack for line in drawn):
            continue
        problems.append(
            f"panel {panel_id!r}: its label names the guard {value:g}, but the "
            f"artifact draws {len(drawn)} bound line(s) at {[round(line, 4) for line in drawn]} "
            "and none of them is that guard: a tolerance the panel names and "
            "does not draw is a claim the reader has to take on trust, and a "
            "bar between that guard and the bound it is read against has no "
            "second line to sit inside"
        )
    return problems


def check_bound_headroom(
    panel_id, bounds, guards, extent, plot_height=None, series=None
):
    """Problems that make an over-bound bar undrawable: the axis is too short.

    An axis that tops out at the highest bound it names leaves the bar that
    crosses that bound nowhere to go — a breach and a value exactly at the bound
    paint the same picture — so the frame has to keep `MIN_HEADROOM_PIXELS`
    above every value it names. A pinned `y_extent` is where this bites, and on
    a panel whose data spread is a ten-thousandth of its bound (a fair share
    pinned at 25 %) it bites on the automatic extent too, which is why the
    policy spends a pixel floor on it as well as the span's own share.

    The same rule holds on the other side, and it was the half nobody checked: a
    bound a bar can fail *downwards* — a floor, or a `±` band's lower arm —
    needs `MIN_HEADROOM_PIXELS` **below** it, or a bar under it is drawn flush
    with the frame and reads as the frame's border rather than as a crossing.
    `series` tells the two sides apart (`failure_side`): without it the bottom
    rule cannot be evaluated and only the upper margin is measured, which is why
    it is optional rather than required.
    """
    if not bounds and not guards:
        return []
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    problems = []
    top = max([bound["y"] for bound in bounds] + list(guards))
    headroom = (high - top) / span * plot_height
    if headroom < MIN_HEADROOM_PIXELS:
        problems.append(
            f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} keeps {headroom:.1f} "
            f"px above the highest value it names (y={top:g}), under the "
            f"{MIN_HEADROOM_PIXELS:.0f} px an over-bound bar needs: a breach and a "
            "value exactly at that bound would be drawn as the same picture, so the "
            "panel could not show the failure it exists for"
        )
    if series is None:
        return problems
    values = _bound_values(series)
    for bound in bounds:
        if (
            bound.get("band_arm") != "lower"
            and failure_side(values, bound["y"]) >= 0.0
        ):
            continue
        y = float(bound["y"])
        below = (y - low) / span * plot_height
        if below >= MIN_HEADROOM_PIXELS:
            continue
        problems.append(
            f"panel {panel_id!r}: the axis {low:.4g}..{high:.4g} keeps {below:.1f} "
            f"px below the bound {bound['label']!r} (y={y:g}), under the "
            f"{MIN_HEADROOM_PIXELS:.0f} px an under-bound bar needs: a bar that "
            "crosses that bound downwards would be drawn flush with the frame, "
            "where it reads as the frame's border and not as a crossing"
        )
    return problems


def drawn_bound_lines(markup):
    """The y each drawn bound line sits at, in document order."""
    return [float(y) for y in DRAWN_BOUND_RE.findall(markup)]


def drawn_bound_values(markup, extent):
    """The data value each drawn bound line sits at, on the panel's own frame.

    Read out of the artifact rather than from the plan the renderer was handed:
    a check that measured the plan could not tell a line that was drawn from one
    the renderer forgot, and the forgotten half is the whole defect.
    """
    rect = PLOT_BG_RE.search(markup)
    if rect is None:
        return []
    _, top, _, height = (float(group) for group in rect.groups())
    if height <= 0.0:
        return []
    low, high = extent
    return [high - (y - top) / height * (high - low) for y in drawn_bound_lines(markup)]


def check_two_sided_bound_drawn(panel_id, bounds, extent, markup, plot_height=None):
    """Problems that make half of a declared two-sided bound missing.

    A bound whose label declares a symmetric band (`±`) is one declaration
    standing for two lines, one either side of the quantity's ideal, and a
    panel that draws only one of them cannot show a breach on the other side.
    Measured on the `M4-imbalance` panel of a `tools/mandate-check` run, it
    drew a single line at `+0.01` for a label reading `fair-share bound ±1.0%`,
    over an axis whose low was the data's own minimum (`-0.000234`) rather than
    the bound: a starved flow's `-1 %` departure was therefore drawn as a
    `4.9 px` bar flush with the frame bottom and no line to cross. The eye
    caught it; nothing refused it.

    Two things are measured, both on the artifact: the panel draws both arms
    (the drawn lines are read back out of the SVG and compared with `+y` and
    `-y` in the axis' own units), and the axis keeps `MIN_HEADROOM_PIXELS`
    beyond each arm, so a bar past either side has somewhere to be drawn. The
    `y` a `±` label is mirrored from has to be the magnitude the label itself
    states, or the band's centre is unknowable and the render is refused rather
    than drawn with a mirror the declaration never made.
    """
    low, high = extent
    span = high - low
    if not span > 0:
        return [f"panel {panel_id!r}: the axis {low!r}..{high!r} has no span"]
    if not any(bound_band_half_width(bound) is not None for bound in bounds):
        return []
    if plot_height is None:
        plot_height = REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM
    drawn = drawn_bound_values(markup, extent)
    # A drawn y is printed to a tenth of a pixel, so half a pixel of the axis is
    # the widest a rounding artefact can be; anything further is another arm.
    slack = 0.5 / plot_height * span
    problems = []
    for bound in bounds:
        half = bound_band_half_width(bound)
        if half is None:
            continue
        y = float(bound["y"])
        if not two_sided_bound(bound):
            problems.append(
                f"panel {panel_id!r}: the bound {bound['label']!r} declares a "
                f"symmetric band of {half:g}, while the value it declares is "
                f"{y:g}; the declaration does not say where the band is centred, "
                "so the arm on the other side is unknowable and a departure "
                "there would be drawn with no line to cross"
            )
            continue
        missing = [
            arm
            for arm in (y, -y)
            if not any(abs(sample - arm) <= slack for sample in drawn)
        ]
        if missing:
            arms = ", ".join(f"{arm:+.4g}" for arm in missing)
            drawn_arms = ", ".join(f"{value:.4g}" for value in drawn)
            problems.append(
                f"panel {panel_id!r}: the bound {bound['label']!r} names a "
                f"two-sided band (+/-{half:g}), but the panel draws no line at "
                f"{arms}; its drawn arms are [{drawn_arms}]: a breach on the "
                "side with no line is a bar crossing nothing, which is the "
                "failure the panel is drawn for"
            )
        for arm, side, room in ((y, "above", high - y), (-y, "below", -y - low)):
            pixels = room / span * plot_height
            if pixels >= MIN_HEADROOM_PIXELS:
                continue
            where = (
                f"{-pixels:.1f} px outside the axis {side} it"
                if pixels < 0.0
                else f"only {pixels:.1f} px inside the axis {side} it"
            )
            problems.append(
                f"panel {panel_id!r}: the band arm at {arm:+.4g} is {where} "
                f"({low:.4g}..{high:.4g}), so a bar crossing it has less than the "
                f"{MIN_HEADROOM_PIXELS:.0f} px it needs: the breach and the arm "
                "would be drawn as the same picture"
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


# -- the panel a share cannot fail, and where its failure is drawn --------
#
# `AGENTS.md`'s fourth panel test is "does the panel show the quantity its
# mandate fails on". A share panel draws the share; a share *mandate* fails on
# the departure from it, and a departure of a bound's size is invisible on an
# axis whose whole span is the share. So the share panel is a composition view
# of a failure it cannot contain, and the honest resolutions are to draw the
# departure against its bound or to say on the panel that it is one and name
# the panel that carries the failure. Silence is the one thing that is not
# allowed: a share panel that draws no departure reads as evidence that there
# is no departure to draw.
def panel_series(panel, points):
    """A panel's series as ``[(name, [(x, y)])]``, in declaration order."""
    return [
        (entry["name"], sorted(points[(panel["id"], entry["name"])]))
        for entry in panel["series"]
    ]


def target_bounds(panel, series):
    """The bounds a bar panel's own bars straddle, which none of them can fail.

    A bound the values sit on both sides of is the value they are read *at*
    rather than a line a bar crosses (`bound_side`'s own distinction), and the
    standing case is a fair share. A bound a minority of the bars is beyond is
    a different thing -- a crossing, whose governance `check_bound_governance`
    owns -- and is excluded here.
    """
    values = _bound_values(series)
    return [
        bound
        for bound in _bound_specs(panel)
        if bound_side(values, bound["y"]) is None
        and not crossing_values(values, bound["y"])
    ]


def relative_departure_panel(panel, panels, points, y):
    """The mandate panel that draws ``(value - y) / y`` for this panel's points.

    The relation is *derived* from the two panels' own drawn points rather than
    declared, because a declaration is a claim and the pixels are the artifact:
    a panel is this one's departure view at `y` when every point it draws is
    `(mine - y) / y` for the matching series and category. That is the shape a
    share and its imbalance have, and it is what keeps the note from naming a
    panel the relation does not actually hold for. The tolerance is the
    evidence files' own resolution -- the two series are rounded to six
    decimals before they are written, which is `1e-6` on a share of `0.25` and
    therefore `4e-6` on the departure derived from it.
    """
    mine = {
        (name, x): value
        for name, series in panel_series(panel, points)
        for x, value in series
    }
    if not mine or y == 0.0:
        return None
    for other in panels:
        if other["id"] == panel["id"] or other["chart"] != "bar":
            continue
        theirs = {
            (name, x): value
            for name, series in panel_series(other, points)
            for x, value in series
        }
        if set(theirs) != set(mine):
            continue
        if all(
            abs(theirs[key] - (value - y) / y) <= 1e-5 for key, value in mine.items()
        ):
            return other, theirs
    return None


def panel_unit(label):
    """The unit a panel's own axis label declares, or ``""``.

    `percentile (%)` declares `%` and `latency (ms)` declares `ms`, so a value a
    panel states in its own y quantity is written with the unit its axis
    already carries instead of a unit this tool would have to choose.
    """
    match = re.search(r"\(([^()]*)\)\s*$", label or "")
    return match.group(1).strip() if match else ""


def value_at(points, x):
    """The value a drawn series reads at ``x``, off the segment it draws there.

    A panel asserts the value *at* a bound it draws on its x axis, so the value
    has to be the one the reader can see: linearly interpolated between the two
    drawn samples that bracket ``x``, exactly as the polyline between them is
    drawn. A series that has already reached the top by ``x`` reads its last
    value, and one that starts after ``x`` reads its first -- a clamp, which is
    why `derived_x_bounds`' caller states when the bound is outside the drawn
    range rather than letting the clamp read as a measurement.
    """
    if not points:
        return None
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


# -- a cdf panel's x axis, and the region it has to keep legible ----------
#
# A latency CDF reaches into whatever tail its worst arm has. `M1-cdf`, drawn
# from a real run, put the `lone_tail` maximum at 1017.77 ms on a linear axis,
# so the whole `clean` curve -- the arm M1 is a standing priority for -- ended at
# 112.55 ms, 11.1 % of the 864 px plot, with its p99 at 9.2 %. The panel was not
# lying; it was answering a question about the tail with a picture in which the
# reference arm was a sliver. So the axis is put on base-10 logarithms whenever
# a linear one would leave that arm below `MIN_REFERENCE_REACH_SHARE`, and the
# check below measures the *drawn* axis -- read back out of the panel's own tick
# labels -- so a renderer that quietly went back to a linear axis is refused.

MIN_REFERENCE_REACH_SHARE = 0.5
"""The least share of a cdf panel's x axis its reference arm must reach.

The share is measured from the axis' low edge to the reference arm's own
largest drawn sample. On the recorded `M1-cdf` that is 11.1 % on a linear axis
and 78.0 % on the log axis the policy picks, against a floor of half the width:
the number says what "the curve the reader most needs to see is a sliver" means
in pixels, and it is the same number the policy is driven by.
"""

X_AXIS_TICK_RE = re.compile(
    rf'<text x="[-0-9.]+" y="{REPORT.HEIGHT - 24}" text-anchor="middle">([^<]*)</text>'
)
"""The x tick labels a report chart draws, in the report's own encoding."""


def x_axis_share(x_min, x_max, scale, value):
    """Where ``value`` sits on an axis, as a share of that axis' span."""
    if scale == "log":
        low, high = math.log10(x_min), math.log10(x_max)
        return (math.log10(value) - low) / (high - low)
    return (value - x_min) / (x_max - x_min)


def _straight_line_residual(values, fractions):
    """The largest relative departure of ``values`` from a straight line."""
    low, high = min(values), max(values)
    span = high - low
    if span <= 0.0:
        return float("inf")
    count = len(values)
    mean_x = sum(fractions) / count
    mean_y = sum(values) / count
    denominator = sum((x - mean_x) ** 2 for x in fractions)
    if denominator <= 0.0:
        return float("inf")
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(fractions, values))
        / denominator
    )
    intercept = mean_y - slope * mean_x
    return max(
        abs(y - (intercept + slope * x)) for x, y in zip(fractions, values)
    ) / span


def drawn_x_scale(markup):
    """``log`` or ``linear``: the scale the panel's own ticks put its axis on.

    Read out of the artifact rather than taken from the renderer's own argument:
    the ticks are drawn at equal fractions of the plot width, each with a value,
    so the model that reproduces them is the model the panel drew. A linear axis
    puts those values on an arithmetic sequence and a logarithmic one puts their
    logarithms there, and the model with the smaller relative residual wins. An
    axis whose ticks cannot be read -- too few, or a value no logarithm takes --
    is taken as linear, which is what every panel that does not ask for a log
    axis draws.
    """
    values = []
    for text in X_AXIS_TICK_RE.findall(markup):
        try:
            values.append(float(text))
        except ValueError:
            return "linear"
    if len(values) < 4 or any(value <= 0.0 for value in values):
        return "linear"
    fractions = [index / (len(values) - 1) for index in range(len(values))]
    logarithmic = _straight_line_residual([math.log10(value) for value in values], fractions)
    arithmetic = _straight_line_residual(values, fractions)
    return "log" if logarithmic < arithmetic else "linear"


def reference_arm_names(series, run_values):
    """The panel's arm names the run asserts no guard of its own for.

    `arm_guard_tokens` reads the run's `*_guard` keys against the panel's own
    legend, so the arms it leaves over are the ones the run makes no separate
    claim about: `M1-cdf` draws `clean`, `hostile` and `lone_tail`, the run
    guards the last two, and `clean` is therefore the arm the panel's comparison
    is read for. A run that guards every arm or none leaves no reference arm,
    and a panel with none owes this nothing.
    """
    guarded = {name for name, _ in arm_guard_tokens(series, run_values)}
    if not guarded or len(guarded) == len(series):
        return []
    return [name for name, _ in series if name not in guarded]


def reference_reach(series, reference):
    """The samples the reference arms contribute, and the axis they sit on."""
    values = [x for _, points in series for x, _ in points]
    reach = [x for name, points in series if name in reference for x, _ in points]
    if not values or not reach:
        return [], (0.0, 0.0)
    return reach, (min(values), max(values))


def reference_reach_share(series, reference, scale):
    """``(share, low, high, largest)`` for the reference arms, or ``None``."""
    reach, (low, high) = reference_reach(series, reference)
    if not reach or not high > low or (scale == "log" and low <= 0.0):
        return None
    largest = max(reach)
    if scale == "log" and largest <= 0.0:
        return None
    return (x_axis_share(low, high, scale, largest), low, high, largest)


def cdf_x_scale(series, reference):
    """The x scale a cdf panel is drawn on, from its own dynamic range.

    Logarithmic when a linear axis would leave the reference arm below
    `MIN_REFERENCE_REACH_SHARE`; linear otherwise, and linear whatever the
    spread when the axis cannot carry a logarithm (a sample at or below zero).
    """
    reach, (low, high) = reference_reach(series, reference)
    if not reach or not high > low or low <= 0.0:
        return "linear"
    if reference_reach_share(series, reference, "linear")[0] >= MIN_REFERENCE_REACH_SHARE:
        return "linear"
    return "log"


def cdf_scale_note(series, reference, scale):
    """The sentence a cdf panel owes when its drawn axis still squeezes.

    The log axis is the fix for the measured defect, and this is the case it
    cannot fix: a reference arm narrower than half the width *even* on a log
    axis (`20.1..25 ms` against a tail reaching a second), or an axis carrying a
    zero that no logarithm takes. `AGENTS.md` allows a panel to say what it
    cannot show, so it says this, and `check_cdf_reference_reach` accepts the
    sentence in the place of the scale.
    """
    measured = reference_reach_share(series, reference, scale)
    if measured is None:
        return ""
    share, low, high, largest = measured
    if share >= MIN_REFERENCE_REACH_SHARE:
        return ""
    kind = "logarithmic (base 10)" if scale == "log" else "linear"
    return (
        f"x axis {kind}: the reference arm {' '.join(reference)} reaches "
        f"{largest:.4g} on {low:.4g}..{high:.4g}, {share:.0%} of the width, so "
        "the region that carries the failure is compressed at the left edge"
    )


def reference_reach_stated(markup, reference, share):
    """Whether the panel's own text states that its reference arm is squeezed."""
    text = " ".join(drawn_notes(markup))
    return bool(text) and f"{share:.0%}" in text and any(
        name in text for name in reference
    )


def check_cdf_reference_reach(panel_id, panel, series, reference, markup):
    """Problems that squeeze a cdf panel's reference arm to a sliver.

    `AGENTS.md`'s first panel test -- would a regression be visible at this
    scale? -- for a distribution rather than a trajectory. The panel is read for
    where its reference arm's body and tail sit, and an axis whose far tail is
    orders of magnitude away leaves that body in the first tenth of the width:
    measured on the `M1-cdf` artifact of a real run, the axis was
    0.045..1017.77 ms linear, the `clean` curve ended at 112.55 ms -- 11.1 % of
    the 864 px plot, its p99 at 9.2 %. The share is measured against the axis
    the panel *drew*, read back from its own tick labels (`drawn_x_scale`), so a
    renderer that quietly went back to a linear axis is refused rather than
    believed. A panel that cannot reach the share even on a log axis owes the
    reader a sentence saying so, and the sentence is accepted in its place.
    """
    if panel["chart"] != "cdf" or not reference:
        return []
    measured = reference_reach_share(series, reference, drawn_x_scale(markup))
    if measured is None:
        return []
    share, low, high, largest = measured
    if share >= MIN_REFERENCE_REACH_SHARE:
        return []
    if reference_reach_stated(markup, reference, share):
        return []
    return [
        f"panel {panel_id!r}: the reference arm(s) {' '.join(reference)} reach "
        f"{largest:.4g} on an x axis {low:.4g}..{high:.4g}, {share:.1%} of the "
        f"width -- under the {MIN_REFERENCE_REACH_SHARE:.0%} a distribution "
        "panel needs to show the shape of the arm it is read for, so the region "
        "that carries the failure is a sliver; draw the axis on the scale that "
        "keeps it legible, or state the squeeze on the panel"
    ]


def derived_x_bounds(panel, panels, points, mandate_x_label, mandate_y_label, run_values):
    """The bounds a sibling panel draws on the quantity this panel's x axis carries.

    ``bounds`` are horizontal, so a mandate ceiling expressed on the plot's *x*
    axis -- an M1 latency ceiling read against a latency CDF -- has no declared
    representation: the declaration carries it on the panel whose *y* axis is
    that quantity, and this reads it back rather than duplicating a bound the
    mandate already states once (one authority per bound). The relation is
    derived from the two panels' own drawn axis labels, so a declaration that
    renamed either axis stops the transfer instead of annotating a panel the
    bound is not about.
    """
    if panel["chart"] not in ("line", "cdf"):
        return []
    mine = panel_series(panel, points)
    our_x = panel_x_label_for(
        panel,
        mandate_x_label,
        [x for _, series in mine for x, _ in series],
        run_values,
    )
    derived = []
    for other in panels:
        if other["id"] == panel["id"]:
            continue
        their_y = panel_y_label_for(
            other, mandate_y_label, panel_series(other, points)
        )
        if their_y != our_x:
            continue
        for bound in other.get("bounds") or []:
            derived.append(
                {
                    "x": float(bound["y"]),
                    "label": bound["label"],
                    "source": other["id"],
                }
            )
    return derived


def x_bound_label(bound, series, x_unit, y_unit, drawn_range=None):
    """A bound carried to the x axis, with the value every series reads there.

    The failure a CDF is read for is the curve not reaching its top by the
    ceiling, and the ceiling is a value on the axis the CDF's *x* carries: so
    the panel states, at that x, what each curve reads. Without it the reader
    has the shape and no mark to read it against -- whether any sample exceeds
    the ceiling is answerable only from the numbers, which is the debt a bare
    pointer to the sibling panel leaves unpaid once the value is knowable.
    """
    x = bound["x"]
    readings = []
    for name, points in series:
        value = value_at(points, x)
        if value is None:
            continue
        readings.append(f"{name} {value:.4g}{y_unit}")
    outside = (
        drawn_range is not None
        and not (drawn_range[0] <= x <= drawn_range[1])
    )
    text = (
        f"{bound['label']} [at {x:g}{' ' + x_unit if x_unit else ''}: "
        + ", ".join(readings)
        + "]"
    )
    if outside:
        text += " (x beyond this panel's drawn range)"
    return text


def check_x_bound_drawn(
    panel_id, panel, panels, points, mandate_x_label, mandate_y_label, run_values, markup
):
    """Problems that leave a mandate bound off a panel that can carry it in-frame.

    A panel whose mandate can fail on its own x axis must be able to draw that
    failure: the mark has to be on the artifact, and the sentence has to state
    the value the bound is read against. The sentence is not checked against
    the formatter that wrote it -- that would prove only that it reached the
    panel -- but read back out of the artifact and measured, per series, against
    the same drawn points the panel plots, so a magnitude taken from anywhere
    else is refused rather than drawn authoritatively. A bound outside the drawn
    x range owes no line (there is no pixel for it) but still owes the
    sentence, and the sentence says it is outside, so the clamp `value_at`
    returns cannot read as a measurement.
    """
    derived = derived_x_bounds(
        panel, panels, points, mandate_x_label, mandate_y_label, run_values
    )
    if not derived:
        return []
    series = panel_series(panel, points)
    drawn = [(name, REPORT.decimate(points)) for name, points in series]
    x_unit = panel_unit(
        panel_x_label_for(
            panel,
            mandate_x_label,
            [x for _, points in series for x, _ in points],
            run_values,
        )
    )
    y_unit = panel_unit(panel_y_label_for(panel, mandate_y_label, series))
    xs = [x for _, points in drawn for x, _ in points]
    drawn_range = (min(xs), max(xs)) if xs else None
    drawn_xs = [
        float(value)
        for value in re.findall(r'class="x-bound" x1="([-0-9.]+)"', markup)
    ]
    labels = [declared for declared, _, _ in label_boxes(markup)]
    left, _, right, _ = panel_plot_rect(panel_id, markup)
    problems = []
    for bound in derived:
        # The sentence this bound owes is the one that reads *this* x: two bounds
        # whose labels share a prefix would otherwise be able to answer for each
        # other, and the value check below would compare one bound's numbers
        # against the other's point.
        reading = f"at {bound['x']:g}"
        stated = next(
            (
                label
                for label in labels
                if label.startswith(bound["label"])
                and label != bound["label"]
                and reading in label
            ),
            None,
        )
        if stated is None:
            problems.append(
                f"panel {panel_id!r}: the mandate's bound {bound['label']!r} is "
                f"stated on panel {bound['source']!r} on this panel's own x "
                f"quantity, so this panel can draw the failure and has to say "
                f"what the bound reads there. Expected on the panel: "
                f"{x_bound_label(bound, drawn, x_unit, y_unit, drawn_range)!r}; "
                f"drawn: {labels!r}"
            )
            continue
        for name, points in drawn:
            value = value_at(points, bound["x"])
            if value is None:
                continue
            written = re.search(
                rf"{re.escape(name)}\s+([-+0-9.eE]+){re.escape(y_unit)}", stated
            )
            if written is None:
                problems.append(
                    f"panel {panel_id!r}: the bound {bound['label']!r} is drawn "
                    f"without a value for series {name!r}, which the panel plots; "
                    f"the series reads {value:.6g}{y_unit} at {bound['x']:g} and "
                    "a reader told nothing is told the pointer the value "
                    f"replaced (drawn: {stated!r})"
                )
                continue
            problem = stated_problem(
                panel_id,
                name,
                f"its value at {bound['x']:g} as",
                written.group(1),
                value,
            )
            if problem is not None:
                problems.append(problem)
        outside = drawn_range is None or not (
            drawn_range[0] <= bound["x"] <= drawn_range[1]
        )
        if outside:
            if "beyond this panel's drawn range" not in stated:
                problems.append(
                    f"panel {panel_id!r}: the bound {bound['label']!r} at "
                    f"{bound['x']:g} is outside this panel's drawn x range and "
                    "the panel's sentence does not say so, so the value it "
                    f"states reads as a measurement (drawn: {stated!r})"
                )
            continue
        if not drawn_xs:
            problems.append(
                f"panel {panel_id!r}: the bound {bound['label']!r} at "
                f"{bound['x']:g} lies inside this panel's drawn x range "
                f"{drawn_range[0]:g}..{drawn_range[1]:g}, so the artifact needs "
                "a vertical mark at it -- a bound the panel states but does not "
                "draw is a claim with no mark to read it against"
            )
            continue
        # The mark's pixel has to be computed on the axis the panel actually
        # drew, which is read back out of its own ticks: a log axis measured
        # with a linear formula would report the ceiling as being somewhere
        # else on the panel.
        want = left + x_axis_share(
            drawn_range[0],
            drawn_range[1],
            drawn_x_scale(markup)
            if drawn_range[0] > 0.0 and bound["x"] > 0.0
            else "linear",
            bound["x"],
        ) * (right - left)
        if min(abs(value - want) for value in drawn_xs) <= 1.5:
            continue
        problems.append(
            f"panel {panel_id!r}: its drawn x marks are at {drawn_xs} px and the "
            f"bound {bound['label']!r} at {bound['x']:g} is {want:.1f} px; a mark "
            "somewhere else on the axis is not the bound the sentence states"
        )
    return problems


def departure_view_note(panel, panels, points):
    """The note a share panel owes: what it is, and where its failure is drawn.

    Returns the empty string when the panel is not one of these -- a line
    panel, a panel with no bound its bars straddle, or a share whose departure
    no other panel of the mandate draws. The numbers come from the companion's
    own drawn points and its own bound, so the note is a reading of the
    artifact rather than a sentence about the producer.
    """
    if panel["chart"] != "bar":
        return ""
    series = panel_series(panel, points)
    for bound in target_bounds(panel, series):
        found = relative_departure_panel(panel, panels, points, bound["y"])
        if found is None:
            continue
        companion, values = found
        bounds = _bound_specs(companion)
        if not bounds:
            continue
        departure_bound = min(value["y"] for value in bounds)
        worst = []
        for entry in companion["series"]:
            points_of = [
                value for (name, _), value in values.items() if name == entry["name"]
            ]
            if points_of:
                worst.append((entry["name"], max(abs(value) for value in points_of)))
        return (
            f"composition view - the departure is drawn on panel "
            f"'{companion['id']}' (bound {departure_bound:.1%}): worst "
            + ", ".join(f"{name} {value:.2%}" for name, value in worst)
        )
    return ""


def bound_reference_note(panel, panels, points, x_label="", y_label="", run_values=None):
    """The note a panel with no bound of its own owes: where the bound is drawn.

    A panel that draws no line at all cannot show a breach of the mandate's
    bound however faithfully it draws its quantity: the reader has no mark to
    read the quantity against. So it says which panel of the mandate carries
    the bound and which bound that is -- the same debt a share panel owes for a
    departure it cannot contain.

    It owes nothing once the bound is expressible on its *own* x axis and
    `check_x_bound_drawn` draws it there with the value it is read at: naming a
    sibling panel is what a frame does when it cannot carry the failure, and a
    bare pointer is not enough once the value is knowable.
    """
    if panel.get("bounds"):
        return ""
    if derived_x_bounds(panel, panels, points, x_label, y_label, run_values):
        return ""
    for other in panels:
        if other["id"] == panel["id"]:
            continue
        bounds = other.get("bounds") or []
        if not bounds:
            continue
        return (
            f"composition view - this panel draws the quantity; the mandate's "
            f"bound '{bounds[0]['label']}' is drawn on panel '{other['id']}'"
        )
    return ""


def composition_note(panel, panels, points, x_label="", y_label="", run_values=None):
    """The note a panel owes for the failure its own frame cannot carry."""
    return departure_view_note(panel, panels, points) or bound_reference_note(
        panel, panels, points, x_label, y_label, run_values
    )


def drawn_notes(markup):
    """The lines of the notes a panel draws on its own face, in draw order."""
    return [
        html.unescape(BOUND_LABEL_TITLE_RE.sub("", content)).strip()
        for _, _, content in PANEL_NOTE_RE.findall(markup)
    ]


def note_boxes(markup):
    """Each drawn note line as ``(text, (x0, y0, x1, y1))``."""
    boxes = []
    for x, y, content in PANEL_NOTE_RE.findall(markup):
        text = html.unescape(BOUND_LABEL_TITLE_RE.sub("", content)).strip()
        left = float(x)
        baseline = float(y)
        boxes.append(
            (
                text,
                (
                    left,
                    baseline - REPORT.LABEL_ASCENT_PX,
                    left + REPORT.label_text_width(text),
                    baseline + REPORT.LABEL_DESCENT_PX,
                ),
            )
        )
    return boxes


def check_note_fit(panel_id, markup):
    """Problems that make a panel's own note unreadable where it is drawn.

    A note is text on the panel's face, so it lives under the same rule as a
    bound label: it has to be inside the plot it explains, and it may not share
    ink with another drawn text. A note that leaves the plot is an annotation
    about the legend, and one that overlaps a bound label leaves neither
    readable.
    """
    boxes = note_boxes(markup)
    if not boxes:
        return []
    left, top, right, bottom = panel_plot_rect(panel_id, markup)
    problems = []
    for text, (x0, y0, x1, y1) in boxes:
        if x0 < left or y0 < top or x1 > right or y1 > bottom:
            problems.append(
                f"panel {panel_id!r}: its note {text!r} is drawn at "
                f"{x0:.1f},{y0:.1f}..{x1:.1f},{y1:.1f}, outside the plot area "
                f"{left:.1f},{top:.1f}..{right:.1f},{bottom:.1f}; a note about "
                "what this frame cannot show has to be inside the frame"
            )
    others = [(declared, box) for declared, _, box in label_boxes(markup)] + [
        (text, box) for text, box in boxes
    ]
    for index, (declared, box) in enumerate(others):
        for other_text, other in others[index + 1 :]:
            area = box_overlap(box, other)
            if area <= LABEL_OVERLAP_PX2:
                continue
            problems.append(
                f"panel {panel_id!r}: the drawn text {declared!r} shares {area:.0f} "
                f"px with {other_text!r}, so neither is readable; a note is what "
                "the reader is told instead of the pixels, so it may not be drawn "
                "under another label"
            )
    return problems


def check_departure_view_stated(
    panel_id, panel, panels, points, markup, x_label="", y_label="", run_values=None
):
    """Problems that leave a composition panel silent about the failure it cannot show.

    Two shapes, one rule. A panel whose bound is a value its own bars straddle
    cannot fail it, and the mandate behind the panel fails on a *departure*
    from it; a panel that draws no bound at all has no mark to read its own
    quantity against. Either way the panel's frame does not contain the failure
    it exists under, so it either draws that failure or says on its own face
    where the failure is drawn and what it measures. What is refused is
    neither, because a panel drawn silently reads as evidence that there is no
    failure to draw.

    A panel that *can* carry the failure -- a bound on its own x quantity, which
    `check_x_bound_drawn` draws with the value it is read at -- owes no note:
    the frame carries it, and a pointer to a sibling panel would be the weaker
    answer.
    """
    expected = composition_note(panel, panels, points, x_label, y_label, run_values)
    if not expected:
        return []
    drawn = " ".join(drawn_notes(markup))
    if expected in drawn:
        return []
    return [
        f"panel {panel_id!r}: its own frame cannot carry the failure its mandate "
        "is read for -- either its bound is a value its bars straddle, or it "
        "draws no bound at all -- and the panel says neither where that failure "
        "is drawn nor what it measures. Expected on the panel: "
        f"{expected!r}; drawn: {drawn!r}. A panel drawn silently is read as "
        "evidence that there is no failure to draw"
    ]


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
    breach from a tolerated tripwire. A line panel cannot split its line, so it
    gets the other half of the same rule instead: the arm-guard clause, naming
    the arm the declared bound governs and the arms the run asserts its own
    guards for.

    A bar's guard clause is *not* conditional on a crossing: the guards are the
    arms' own bounds, and the axis has to carry them whether or not one of them
    has been reached yet (a run whose worst arm sits 0.09 under the budget is
    the same panel as one whose worst arm sits 0.05 over it). Naming them is
    also what makes the range honest — a panel may draw what it names. A line
    panel's clause names values on statistics of its series rather than on the
    axis it plots, which is why `named_guard_values` leaves the line axis alone.
    """
    clauses = []
    if bound.get("arms"):
        clauses.append(f"governs {' '.join(bound['arms'])}")
    if bound.get("governs_none"):
        clauses.append(
            "governs no arm of this run: every arm states a guard of its own, "
            "drawn on its own band"
        )
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
            # `crossing_values` returns whichever side of the bound is the
            # minority -- and a guard line can sit *below* the bars (`M2`'s
            # wire panel draws each arm's own guard, and a run is free to guard
            # an arm tighter than the declaration's budget). A clause that said
            # `beyond it` for bars under the line would name a departure that
            # did not happen, in the direction it did not happen in.
            side = "beyond" if crossed[0] > bound["y"] else "under"
            crossing_clauses.append(
                f"{len(crossed)} of {len(values)} bars {side} it"
            )
        # A guard the panel draws as its own line is read off that line's own
        # label (`run hostile_wire_guard=10 [governs hostile]`), so the sentence
        # that used to carry it is dropped rather than repeated -- and a bound
        # that *is* a guard does not list itself among the guards it is read
        # against. Where nothing is drawn, the clause stays: naming a guard the
        # panel cannot place on the axis is the reading the clause owes.
        guards = (
            []
            if bound.get("guard_key") is not None or bound.get("guards_drawn")
            else run_guards(run_values, series, bound["label"])
        )
        if guards:
            crossing_clauses.append(
                "run guards "
                + " ".join(f"{key}={value:g}" for key, value in guards)
            )
        if crossing_clauses:
            clauses.append("; ".join(crossing_clauses))
    else:
        # A line panel's series are the arms themselves, so its bound names
        # which arm it governs and what the other arms are read against -- the
        # division a bar panel draws instead (`check_bound_arm_governance`).
        clause = arm_guard_clause(series, run_values)
        if clause:
            clauses.append(clause)
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


def note_baselines(text, plot_top, plot_bottom, budget, note_row, label_area):
    """The lines a panel's note draws as, and the baselines they are placed at.

    Candidate blocks are stepped down the plot a line height at a time, and the
    first whose drawn boxes share no ink with the labels the frame already drew
    is the one used; the block is kept inside the plot and below whatever
    already occupies its top (the band-view note). A frame with room for none
    keeps the top candidate, so `check_note_fit` refuses it by name rather than
    the note silently landing under a label.
    """
    if not text:
        return []
    lines = REPORT.wrap_label(text, budget)
    width = max(REPORT.label_text_width(line) for line in lines)
    left = REPORT.PAD_LEFT + 5

    def block(row):
        return [
            plot_top + 13 + (row + index) * REPORT.LABEL_LINE_HEIGHT_PX
            for index in range(len(lines))
        ]

    def boxes(baselines):
        return [
            (
                left,
                y - REPORT.LABEL_ASCENT_PX,
                left + width,
                y + REPORT.LABEL_DESCENT_PX,
            )
            for y in baselines
        ]

    top = block(note_row)
    last_row = note_row
    while block(last_row)[-1] + REPORT.LABEL_DESCENT_PX <= plot_bottom - 4:
        last_row += 1
    for row in range(note_row, last_row):
        baselines = block(row)
        if all(
            box_overlap(box, other) <= LABEL_OVERLAP_PX2
            for box in boxes(baselines)
            for other in label_area
        ):
            return list(zip(lines, baselines))
    return list(zip(lines, top))


def drawn_bound_window(bound):
    """The x-window a bound is drawn over: its declared governance, or its arms.

    A bound the run restates per arm carries the categories it governs under
    `window` rather than as a declared `x`, so that the label's own governance
    clause names the *arms* instead of repeating the ordinals it already
    implies.
    """
    window = bound_governed_x(bound)
    if window is not None:
        return window
    arms = bound.get("window")
    if arms:
        return (min(arms), max(arms))
    return None


def svg_bar_chart(
    title,
    x_label,
    y_label,
    series,
    bounds=None,
    extent=None,
    run_values=None,
    note="",
):
    """Grouped bars for ``[(name, [(x, y)])]`` on the axis ``bar_axis_extent`` picks.

    ``rtp_trace_report.svg_histogram`` buckets raw samples, which is a
    different contract from declared x/y points, so the bars are drawn here
    with the report's own axis constants, palette and extent helper. ``bounds``
    are declaration dicts: each is drawn across the x-window it governs (the
    whole plot unless it declares ``x``), and labelled with that governance.
    ``note`` is what the frame cannot show -- the departure view of a share
    panel, say -- drawn wrapped inside the plot, above the bars' tops.
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
    # The tick labels are how the reader converts a bar into a number, and two
    # decimals print a band view's six ticks as three values: on
    # `M4-imbalance`, whose axis spans 1.3 % of the unit around zero, they read
    # `0.01 0.01 0.01 0.00 0.00 0.00`. The resolution the reader needs is set by
    # the *step* between the ticks, so it is derived from the step whatever the
    # axis starts at -- the sign of the lower edge no longer decides it.
    step = (y_max - y_min) / 5.0
    y_decimals = 2
    if step > 0.0:
        y_decimals = min(6, max(2, math.ceil(-math.log10(step)) + 1))
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
    # The boxes the frame's own bound labels draw, so the note below is placed
    # into room that is actually free rather than over a label.
    label_area = []
    panel_note_lines = []
    for bound in bounds or []:
        y = sy(bound["y"])
        window = drawn_bound_window(bound)
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
        # An arm whose line is within a label height of the one being stated is
        # drawn without a label of its own: two labels that close are the text of
        # neither, and `check_label_overlap` refuses both. The band's declaration
        # keeps its label -- the sentence that says the bound is a band -- and the
        # arm's own position goes into the sliver statement
        # (`sliver_bound_statements`), which `check_sliver_bound_stated` reads
        # back out of this markup.
        if bound.get("unlabelled"):
            continue
        markup, layout = REPORT.bound_label_markup(
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
        label_area.extend(layout["boxes"])
    band_note = band_view_note(extent)
    note_row = 0
    if band_note:
        parts.append(
            f"<text class=\"axis-note\" x=\"{REPORT.PAD_LEFT + 5:.1f}\" "
            f"y=\"{plot_top + 13:.1f}\" style=\"{BAR_BOUND_LABEL_STYLE}\">"
            f"{html.escape(band_note)}</text>"
        )
        note_row += 1
    # The note is what the frame cannot show, so it is drawn in whatever room
    # the frame's own bound labels left: below them at the top of the plot, or
    # failing that along the bottom. Either way it is placed against the boxes
    # the labels actually drew, and a frame with room for neither is refused by
    # `check_note_fit` rather than annotated illegibly.
    for baseline in note_baselines(
        note,
        plot_top,
        REPORT.HEIGHT - REPORT.PAD_BOTTOM,
        plot_width - 2 * REPORT.LABEL_INSET_PX,
        note_row,
        label_area,
    ):
        panel_note_lines.append(baseline)
    for line, y in panel_note_lines:
        parts.append(
            f"<text class=\"panel-note\" x=\"{REPORT.PAD_LEFT + 5:.1f}\" "
            f"y=\"{y:.1f}\" "
            f"style=\"{BAR_BOUND_LABEL_STYLE}\">{html.escape(line)}</text>"
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


def panel_markup(
    title,
    x_label,
    y_label,
    panel,
    points,
    run_values=None,
    run_censoring=None,
    panels=None,
    fault=None,
):
    """Markup for one declared panel, as exactly one ``<svg>`` document span.

    ``run_censoring`` are the run's own per-arm readings for the lines this
    mandate draws; a line panel states them above its plot (`arm_reading`).
    ``panels`` is the whole mandate's panel list, which a composition panel
    needs in order to name the panel that carries the failure it cannot show.
    """
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
    # A bound the run restates for some of the panel's arms is drawn as each
    # arm's own bound, so every check below -- the axis, the headroom, the
    # drawn lines -- measures the bounds the panel actually draws.
    drawn_bounds = drawable_bounds(panel, series, bounds, run_values)
    pinned = _require_extent(panel.get("y_extent"), f"panels.{panel['id']}.y_extent")
    # A line panel reserves a band above the plot for its per-arm readings, so
    # the plot it draws -- and therefore the axis every check below measures --
    # is the one that band leaves.
    readings = panel_readings(series, run_censoring) if chart == "line" else []
    reading_rows = sum(len(lines) for lines in REPORT.reading_lines(readings))
    # A bound the mandate states on this panel's own *x* quantity -- the latency
    # ceiling the sibling latency panel draws, read against a latency CDF -- is
    # carried here as a vertical mark with the value each curve reads at it.
    x_bounds = derived_x_bounds(panel, panels or [panel], points, x_label, y_label, run_values)
    note = composition_note(
        panel, panels or [panel], points, x_label, y_label, run_values
    )
    # A distribution panel's x axis is drawn on the scale that keeps its
    # reference arm legible, and owes a sentence when no scale can.
    reference_arms = reference_arm_names(series, run_values) if chart == "cdf" else []
    x_scale = cdf_x_scale(series, reference_arms) if reference_arms else "linear"
    if reference_arms:
        scale_note = cdf_scale_note(series, reference_arms, x_scale)
        if scale_note:
            note = f"{note}; {scale_note}" if note else scale_note
    note_rows = len(REPORT.wrap_label(note, REPORT.READING_PLOT_WIDTH)) if note else 0
    plot_height = (
        REPORT.line_plot_height(len(series), reading_rows + note_rows)
        if chart != "bar"
        else bar_plot_height(len(series))
    )
    axis = panel_axis_extent(panel, series, drawn_bounds, run_values, plot_height)
    # A bound whose band this axis cannot resolve owes a statement on the panel's
    # own face, and the statement is what `check_panel_axis` accepts in the
    # refusal's place: the two are read from `sliver_bound_statements` here, so
    # the panel cannot be allowed by one reading and refused by the other.
    sliver_statements = (
        sliver_bound_statements(series, drawn_bounds, axis, plot_height, run_values)
        if chart == "bar"
        else []
    )
    stated_slivers = [str(bound["label"]) for bound, _ in sliver_statements]
    if sliver_statements:
        stated_slivers = "; ".join(sentence for _, sentence in sliver_statements)
        note = f"{note}; {stated_slivers}" if note else stated_slivers
    guards = named_guard_values(
        series, drawn_bounds, run_values, crossing=chart == "bar"
    )
    problems = (
        check_bound_governance(panel["id"], series, bounds, run_values)
        + check_bound_x_categories(panel["id"], series, bounds)
        + check_reading_band(panel["id"], plot_height)
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
            panel["id"],
            series,
            drawn_bounds,
            axis,
            plot_height,
            run_values,
            stated=[str(bound["label"]) for bound, _ in sliver_statements],
        )
    problems += check_named_values_in_axis(
        panel["id"], drawn_bounds, guards, axis, plot_height
    ) + check_bound_headroom(
        panel["id"], drawn_bounds, guards, axis, plot_height, series
    )
    if problems:
        _fail("\n  ".join(problems))
    drawn = [(series_label(name), points) for name, points in series]
    # The mark's sentence states what the *drawn* polyline reads, so it is built
    # from the decimated points the chart itself plots.
    plotted = [(name, REPORT.decimate(points)) for name, points in series]
    plotted_range = (
        (
            min(x for _, points in plotted for x, _ in points),
            max(x for _, points in plotted for x, _ in points),
        )
        if plotted
        else None
    )
    drawn_x_bounds = [
        (
            bound["x"],
            x_bound_label(
                bound,
                plotted,
                panel_unit(panel_x_label),
                panel_unit(panel_y_label),
                plotted_range,
            ),
        )
        for bound in x_bounds
    ]
    if chart == "bar":
        markup = svg_bar_chart(
            chart_title,
            panel_x_label,
            panel_y_label,
            series,
            drawn_bounds,
            axis,
            run_values,
            note=note,
        )
    elif chart == "line":
        labelled = [
            (bound["y"], governed_label(bound, series, run_values, crossing=False))
            for bound in drawn_bounds
        ]
        markup = REPORT.svg_line_chart(
            chart_title,
            panel_x_label,
            panel_y_label,
            drawn,
            axis,
            labelled,
            walls=True,
            markers=True,
            readings=readings,
            note=note,
            x_bounds=drawn_x_bounds,
        )
    else:
        labelled = [
            (bound["y"], governed_label(bound, series, run_values, crossing=False))
            for bound in drawn_bounds
        ]
        markup = REPORT.svg_cdf_chart(
            chart_title,
            panel_x_label,
            panel_y_label,
            drawn,
            labelled,
            note=note,
            x_bounds=drawn_x_bounds,
            x_scale=x_scale,
        )
    # The panel's own statement of what it drew, injected into the SVG and then
    # measured back out of it: a panel produced without a true summary is refused
    # (`check_panel_summary_stated`), the way a panel without a legible bound is.
    summary_document = panel_summary_document(
        panel["id"],
        chart,
        panel_x_label,
        panel_y_label,
        series,
        drawn_bounds,
        axis,
        markup,
        stated_slivers,
        plot_height,
        run_values,
        fault,
    )
    markup = introduce_panel_summary(markup, summary_document)
    problems = (
        check_panel_summary_stated(
            panel["id"],
            chart,
            panel_x_label,
            panel_y_label,
            series,
            drawn_bounds,
            axis,
            markup,
            plot_height,
            stated_slivers,
            run_values,
            fault,
        )
        + check_label_fit(panel["id"], markup)
        + check_label_overlap(panel["id"], markup)
        + check_series_labels(panel["id"], markup, series)
        + check_canvas_text_fit(panel["id"], markup)
        + check_tick_labels_distinct(panel["id"], markup)
        + check_note_fit(panel["id"], markup)
        + check_two_sided_bound_drawn(panel["id"], drawn_bounds, axis, markup)
        + check_named_guards_drawn(
            panel["id"], guards, axis, markup, plot_height
        )
        + check_cdf_reference_reach(
            panel["id"], panel, series, reference_arms, markup
        )
        + (
            check_departure_view_stated(
                panel["id"],
                panel,
                panels or [panel],
                points,
                markup,
                x_label,
                y_label,
                run_values,
            )
            + check_bound_arm_governance(
                panel["id"], panel, series, bounds, run_values, markup
            )
            + check_bar_separation(panel["id"], markup)
            + check_sliver_bound_stated(
                panel["id"], series, drawn_bounds, axis, markup, plot_height, run_values
            )
            if chart == "bar"
            else check_departure_view_stated(
                panel["id"],
                panel,
                panels or [panel],
                points,
                markup,
                x_label,
                y_label,
                run_values,
            )
            + check_bound_arm_governance(
                panel["id"], panel, series, bounds, run_values, markup
            )
            + check_x_bound_drawn(
                panel["id"],
                panel,
                panels or [panel],
                points,
                x_label,
                y_label,
                run_values,
                markup,
            )
        )
        + (
            check_gap_honesty(panel["id"], series, markup)
            + check_readings_stated(panel["id"], series, readings, markup)
            + check_reading_numbers(panel["id"], series, markup)
            if chart == "line"
            else []
        )
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


def render_mandate(
    declaration_path,
    out_dir,
    *,
    rasterize=True,
    browser=None,
    run_values=None,
    run_censoring=None,
    fault=None,
):
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
    text that leaves the canvas or carries an empty placeholder, any y axis whose
    ticks repeat a value, and any legend
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
    censoring_problems = check_censoring_drawn(panels, points, run_censoring)
    if censoring_problems:
        _fail("\n  ".join(censoring_problems))

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
        "summaries": [],
        "svg": [],
        "png": [],
        "browser": None,
        "rasterized": False,
        "censoring": sorted(run_censoring or {}),
    }
    for panel in panels:
        panel_id = panel["id"]
        panel_fault = mandate_fault(mandate, fault)
        markup = panel_markup(
            title,
            x_label,
            y_label,
            panel,
            points,
            run_values,
            run_censoring,
            panels,
            panel_fault,
        )
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
        # A bound the run restates per arm is drawn as each arm's own line, so
        # the count is the plan's -- one line per contiguous run of equal
        # values -- and a bound declared as a two-sided band is drawn as both
        # its arms. Each *declared* bound has to appear among the labels the
        # panel actually drew. The labels are read from their `<title>`
        # elements, which carry the undivided sentence, because a wrapped label
        # splits its own text across elements and a raw substring test would
        # fail on a line break rather than on a missing bound.
        planned = drawable_bounds(
            panel, panel_series(panel, points), _bound_specs(panel), run_values
        )
        drawn = written.count('class="bound"')
        if drawn != len(planned):
            _fail(
                f"{svg_path} draws {drawn} bound line(s) for the {len(planned)} "
                "bound(s) the declaration and the run's own per-arm bounds call "
                "for; a bound that is not in the panel is not a bound"
            )
        titles = [
            html.unescape(re.sub(r"</?title>", "", title))
            for title in BOUND_LABEL_TITLE_RE.findall(written)
        ]
        missing = [
            bound["label"]
            for bound in bounds
            if not any(title.startswith(bound["label"]) for title in titles)
        ]
        if missing:
            _fail(f"{svg_path} does not label every declared bound; missing {missing}")
        # A panel's own statement of what it drew is mandatory, written beside
        # the SVG/PNG it describes and read back out of the artifact the runner
        # ships, so a reader (or the master) never has to open an SVG to know
        # what it shows. A panel that reached here without one cannot: the check
        # in `panel_markup` refuses it first.
        document = read_panel_summary(written)
        if document is None:
            _fail(
                f"{svg_path} carries no panel summary, so the run cannot state "
                "what it drew; a panel is written with its summary or it is not "
                "written"
            )
        document["block"] = panel_summary_block(document)
        summary_path = out_dir / f"{mandate}-{panel_id}.summary.txt"
        try:
            summary_path.write_text(document["block"] + "\n", encoding="utf-8")
        except OSError as error:
            _fail(
                f"panel {panel_id!r} wrote its SVG but not its summary to "
                f"{summary_path}: {error}"
            )
        summary["summaries"].append(document)
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


def load_censoring(path_or_json):
    """The run's per-arm censoring readings, from a JSON object or a file of one.

    The shape is ``{arm: {token: value}}`` -- one arm's parsed reading per line
    panel series, as ``tools/mandate-check`` parses the producer's own
    ``[m1-censoring] arm=...`` rows. A reading that is not an object, or an arm
    with no name, is an error rather than a panel drawn without it: the whole
    point of the band is that the reader cannot get the verdict from the pixels.
    """
    readings = load_run_values(path_or_json)
    if readings is None:
        return None
    for arm, reading in readings.items():
        if not isinstance(arm, str) or not arm.strip():
            _fail(f"run censoring keys must be arm names, got {arm!r}")
        if not isinstance(reading, dict) or not reading:
            _fail(
                f"the run's reading for arm {arm!r} must be a non-empty object of "
                f"its measured tokens, got {reading!r}"
            )
    return readings


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
        "--run-censoring",
        default=None,
        metavar="JSON|PATH",
        help=(
            "this run's per-arm censoring readings (a JSON object of {arm: {token: "
            "value}} or a file of one), stated on every line panel that draws the "
            "arm; a reading for an arm no line panel draws is refused"
        ),
    )
    parser.add_argument(
        "--fault",
        default=None,
        metavar="NAME",
        help=(
            "this run's MANDATE_SMOKE_FAULT selector, when the run took one: "
            "the mandate it names has every panel state that it is a fault "
            "render, which arm the fault perturbs and what it did to the scale"
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
            run_censoring=load_censoring(args.run_censoring),
            fault=args.fault,
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
        for document in summary["summaries"]:
            print(document["block"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
