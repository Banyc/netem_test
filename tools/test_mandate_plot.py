#!/usr/bin/env python3

import base64
import csv
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("mandate_plot.py")
SPEC = importlib.util.spec_from_file_location("mandate_plot", MODULE_PATH)
MANDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANDATE)

WORKSPACE = Path(__file__).resolve().parents[1]

# A healthy M1 declaration: a latency line panel carrying the mandate's own
# ceiling as a bound, and a latency CDF panel with no horizontal bound.
HEALTHY_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "RTT (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "impaired", "role": "impaired"}],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        },
        {
            "id": "cdf",
            "chart": "cdf",
            "series": [{"name": "impaired"}],
            "bounds": [],
        },
    ],
}

HEALTHY_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "impaired", 0.0, 12.5],
    ["latency", "impaired", 1.0, 31.5],
    ["latency", "impaired", 2.0, 88.25],
    ["cdf", "impaired", 12.5, 33.3],
    ["cdf", "impaired", 31.5, 66.7],
    ["cdf", "impaired", 88.25, 100.0],
]

BAR_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput",
    "x_label": "seed",
    "y_label": "goodput (MiB/s)",
    "panels": [
        {
            "id": "goodput",
            "chart": "bar",
            "series": [{"name": "candidate"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

BAR_ROWS = [
    ["panel", "series", "x", "y"],
    ["goodput", "candidate", 11.0, 0.52],
    ["goodput", "candidate", 21.0, 0.61],
    ["goodput", "candidate", 31.0, 0.47],
]

# Minimal 1x1 PNG a fake browser writes to its --screenshot target.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

FAKE_BROWSER = """#!/usr/bin/env python3
import sys

PNG = {png!r}
out = None
for argument in sys.argv:
    if argument.startswith("--screenshot="):
        out = argument.split("=", 1)[1]
if out:
    open(out, "wb").write(PNG)
"""

NULL_BROWSER = """#!/usr/bin/env python3
# Deliberately writes nothing, like a browser that silently produced no PNG.
"""


# The M2 and M4 panels of the preserved battery run in which the audit found
# panels drawn so that their own bound could not be seen (`AUDIT_COVERAGE.md`,
# "Plots that cannot show their own failure"). These declarations and rows are
# that run's own, so the axis tests below are about that run's numbers.
DELIVERY_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "delivery",
            "chart": "bar",
            "y_label": "delivery (received / offered)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [
                {"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}
            ],
        }
    ],
}

DELIVERY_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "clean", 1.0, 1.0],
    ["delivery", "clean", 2.0, 1.0],
    ["delivery", "clean", 3.0, 1.0],
    ["delivery", "clean", 4.0, 1.0],
    ["delivery", "hostile", 1.0, 1.0],
    ["delivery", "hostile", 2.0, 1.0],
    ["delivery", "hostile", 3.0, 1.0],
    ["delivery", "hostile", 4.0, 1.0],
]

WIRE_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and own-wire multiple",
    "x_label": "arm (1=clean 2=hostile 3=lone_tail)",
    "y_label": "value",
    "panels": [
        {
            "id": "wire",
            "chart": "bar",
            "series": [{"name": "wire_x"}],
            "bounds": [{"y": 6, "label": "M2 wire budget 6x"}],
        }
    ],
}

WIRE_ROWS = [
    ["panel", "series", "x", "y"],
    ["wire", "wire_x", 1.0, 2.151595],
    ["wire", "wire_x", 2.0, 5.000262],
    ["wire", "wire_x", 3.0, 6.633134],
]

# The run's own verdict measurements for M2, whose per-arm guards are what the
# wire panel's label names instead of reading as a breach the verdict tolerates.
WIRE_RUN_VALUES = {
    "budget": 6.0,
    "clean_wire_x": 2.15,
    "hostile_wire_guard": 10.0,
    "hostile_wire_x": 5.0,
    "lone_wire_guard": 14.0,
    "lone_wire_x": 6.63,
}

# The run's per-arm guards for a four-arm wire panel, which is the shape that
# makes the attribution label long enough to wrap.
WIRE_FOUR_ARM_RUN_VALUES = {
    "clean_wire_guard": 3.0,
    "hostile_wire_guard": 10.0,
    "lone_wire_guard": 14.0,
    "burst_wire_guard": 21.0,
}

# Widths of the labels that run drew, measured on its own standalone panels by
# headless Chrome (`getBBox().width`, 11px text with no font-family declared,
# resolved as Times). They are the fixtures the width model is calibrated
# against: the model is a *model*, so the only thing that keeps it honest is a
# check that fails when it starts underestimating the drawn text.
RENDERED_LABEL_WIDTHS = (
    (
        "M2 wire budget 6x [1 of 3 bars beyond it; run guards "
        "hostile_wire_guard=10 lone_wire_guard=14]",
        436.73,
    ),
    (
        "M1 ceiling 250 ms [4 of 16 bars beyond it; run guards "
        "hostile_p99_guard=900]",
        349.0,
    ),
    ("M4 per-flow delivery floor 0.995", 144.94),
    ("fair-share bound \u00b11.0%", 103.94),
    ("fair share 25.0%", 72.36),
    ("M3 floor 0.35x link rate", 105.37),
    ("M2 delivery floor 1.000", 105.1),
    ("M1 ceiling 250 ms", 82.77),
)

SHARES_ROWS = [
    ["panel", "series", "x", "y"],
    ["shares", "clean", 1.0, 0.250059],
    ["shares", "clean", 2.0, 0.250059],
    ["shares", "clean", 3.0, 0.249941],
    ["shares", "clean", 4.0, 0.249941],
    ["shares", "hostile", 1.0, 0.250173],
    ["shares", "hostile", 2.0, 0.249365],
    ["shares", "hostile", 3.0, 0.250289],
    ["shares", "hostile", 4.0, 0.250173],
]

SHARES_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.250000, "label": "fair share 25.0%"}],
        }
    ],
}

# The recorded `M4-shares`/`M4-imbalance` pair, verbatim from a real run's
# `M4.csv`: the shares straddle the fair share (0.249912 and 0.250029 around
# 0.25), so no bar can fail the line the panel draws, and the imbalance panel
# carries the departure `(share - 0.25) / 0.25` to the evidence files' own
# six-decimal resolution.
SHARES_IMBALANCE_ROWS = [
    ["panel", "series", "x", "y"],
    ["shares", "clean", 1.0, 0.250029],
    ["imbalance", "clean", 1.0, 0.000118],
    ["shares", "clean", 2.0, 0.250029],
    ["imbalance", "clean", 2.0, 0.000118],
    ["shares", "clean", 3.0, 0.250029],
    ["imbalance", "clean", 3.0, 0.000118],
    ["shares", "clean", 4.0, 0.249912],
    ["imbalance", "clean", 4.0, -0.000353],
    ["shares", "hostile", 1.0, 0.250029],
    ["imbalance", "hostile", 1.0, 0.000114],
    ["shares", "hostile", 2.0, 0.249914],
    ["imbalance", "hostile", 2.0, -0.000343],
    ["shares", "hostile", 3.0, 0.250029],
    ["imbalance", "hostile", 3.0, 0.000114],
    ["shares", "hostile", 4.0, 0.250029],
    ["imbalance", "hostile", 4.0, 0.000114],
]

SHARES_IMBALANCE_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.250000, "label": "fair share 25.0%"}],
        },
        {
            "id": "imbalance",
            "chart": "bar",
            "y_label": "departure from the fair share",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.01, "label": "fair-share bound \u00b11.0%"}],
        },
    ],
}

FRACTION_ROWS = [
    ["panel", "series", "x", "y"],
    ["fraction", "fraction", 1.0, 0.958217],
    ["fraction", "fraction", 2.0, 0.958271],
    ["fraction", "fraction", 3.0, 0.963341],
]

FRACTION_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput vs the shaped clock and the configured link rate",
    "x_label": "seed",
    "y_label": "fraction of link rate",
    "panels": [
        {
            "id": "fraction",
            "chart": "bar",
            "series": [{"name": "fraction"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

# The M2 delivery panel of that run: one series at the ideal, with the floor
# exactly at it. The axis is the band view (the floor *is* the scale), so the
# bound lands a few pixels below the plot's top and the label has nowhere to go
# above it -- the placement the audit measured four pixels above the plot.
M2_DELIVERY_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and own-wire multiple",
    "x_label": "arm (1=clean 2=hostile 3=lone_tail)",
    "y_label": "delivery (received / offered)",
    "panels": [
        {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "delivery"}],
            "bounds": [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        }
    ],
}

M2_DELIVERY_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "delivery", 1.0, 1.0],
    ["delivery", "delivery", 2.0, 1.0],
    ["delivery", "delivery", 3.0, 1.0],
]

# The tail of the recorded `M1-latency` `lone_tail` series that caused the
# misreading this renderer's gap rule comes from: one 2.65 s period nobody
# observed (13.11 s -> 15.76 s), a peak at the far side of it, and a return to
# 29.9 ms thirty milliseconds later. The thirty samples are that run's own
# `latency,lone_tail,<x>,<y>` rows, so the fixture is the shape a real line
# panel is asked to draw and not a shape invented to make the rule fire.
REAL_LONE_TAIL_TAIL = (
    (13.103597, 17.158834),
    (13.105368, 1.772375),
    (15.757062, 2651.693959),
    (15.786949, 29.887334),
    (15.799594, 12.644959),
    (15.804529, 4.935167),
    (15.817246, 12.717500),
    (15.898248, 81.002500),
    (15.902922, 4.673834),
    (15.903107, 0.186292),
    (16.011494, 108.387459),
    (16.025917, 14.422792),
    (16.056668, 30.752000),
    (16.103649, 46.981334),
    (16.106230, 2.581125),
    (16.106516, 0.286167),
    (16.120773, 14.257167),
    (16.155288, 34.514834),
    (16.155476, 0.187584),
    (16.263466, 107.991250),
    (16.321342, 57.875667),
    (16.321571, 0.230125),
    (16.321734, 0.162500),
    (16.321849, 0.115959),
    (16.370935, 49.085959),
    (16.371099, 0.163542),
    (16.484877, 113.779084),
    (16.485060, 0.183000),
    (16.485216, 0.156125),
    (16.545019, 59.803375),
)

# A ladder the window cut off: every sample above the one before it, and the
# last sample the series maximum. On the panel this is the *same* shape as a
# peak that returned -- which is why the run's own verdict, and not the
# reader's eye, has to say which one it is.
TRUNCATED_CLIMB = tuple((index * 0.25, index * 300.0) for index in range(1, 7))

LONE_TAIL_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "lone_tail"}],
            "bounds": [],
        }
    ],
}


def _latency_rows(points):
    return [["panel", "series", "x", "y"]] + [
        ["latency", "lone_tail", x, y] for x, y in points
    ]


def _points(rows):
    """CSV rows from a fixture, in the shape `mandate_plot.parse_points` takes."""
    return MANDATE.parse_points(
        [(line, row[0], row[1], row[2], row[3]) for line, row in enumerate(rows[1:], start=2)]
    )


def _reading_markup(sentences):
    """A line panel's reading band, wrapped and drawn, as `svg_line_chart` does."""
    rows = []
    for sentence in sentences:
        rows.extend(MANDATE.REPORT.wrap_label(sentence, MANDATE.REPORT.READING_PLOT_WIDTH))
    body = "".join(
        f'<text class="arm-reading" x="76" y="{24 + index * 13}">{row}</text>'
        for index, row in enumerate(rows)
    )
    return f'<svg><g class="arm-readings">{body}</g></svg>'


class MandatePlotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)
        self.out = self.root / "panels"

    def tearDown(self):
        self._tmp.cleanup()

    def write_mandate(self, declaration=None, rows=HEALTHY_ROWS, name="M1"):
        path = self.root / f"{name}.json"
        path.write_text(
            json.dumps(HEALTHY_DECLARATION if declaration is None else declaration),
            encoding="utf-8",
        )
        if rows is not None:
            data = self.root / f"{name}.csv"
            if isinstance(rows, str):
                data.write_text(rows, encoding="utf-8")
            else:
                with data.open("w", newline="", encoding="utf-8") as sink:
                    csv.writer(sink).writerows(rows)
        return path

    def write_browser(self, source):
        path = self.root / "fake-browser"
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def run_main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def reject(self, declaration=None, rows=HEALTHY_ROWS, fragment=""):
        """Assert one declaration/data pair is refused, naming the problem."""
        declaration_path = self.write_mandate(declaration, rows)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0, f"expected a non-zero exit; stderr={stderr}")
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn(fragment, stderr)
        return stderr

    # -- the axis test: a panel must be able to show its own bound -----------

    def render_mandate(self, declaration, rows, name, *arguments):
        """Render one declaration and return ``(exit code, stderr, out dir)``."""
        out = self.root / f"out-{name}"
        declaration_path = self.write_mandate(declaration, rows, name=name)
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            *arguments,
        )
        return code, stderr, out

    def test_the_delivery_floor_that_was_sub_pixel_is_now_the_axis_feature(self):
        # The audit's measurement: over the old 0..2 axis, M4's 0.5 % floor band
        # was 0.25 % of the height. The band view has to spend a real share of
        # the axis on it, or the panel cannot show the failure it is drawn for.
        series = [
            ("clean", [(x, 1.0) for x in (1.0, 2.0, 3.0, 4.0)]),
            ("hostile", [(x, 1.0) for x in (1.0, 2.0, 3.0, 4.0)]),
        ]
        bounds = [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}]
        extent = MANDATE.bar_axis_extent(series, bounds)
        low, high = extent
        self.assertGreater(low, 0.0, "the delivery axis cannot be 0-based")
        self.assertLessEqual(high - low, 4 * (high - 0.995) + 1e-12)
        self.assertEqual(MANDATE.check_panel_axis("delivery", series, bounds, extent), [])
        # A 0.5 % loss has to be a visible step, not half a pixel.
        band = max(1.0 - 0.995, MANDATE.MIN_UNIT_SPAN)
        pixels = band / (high - low) * MANDATE.bar_plot_height(2)
        self.assertGreaterEqual(pixels, MANDATE.MIN_BOUND_PIXELS)
        # and the old axis, the one the audit measured, is refused by name
        old = MANDATE.check_panel_axis("delivery", series, bounds, (0.0, 2.0))
        self.assertEqual(len(old), 1, old)
        self.assertIn("0.5%", old[0])
        self.assertIn("sub-pixel", old[0])
        self.assertIn("M4 per-flow delivery floor 0.995", old[0])

    def test_a_mis_scaled_axis_fails_the_whole_render(self):
        # The vacuity half: the same unchanged data on the axis the audit found
        # cannot be rendered at all, rather than silently drawn at half scale.
        declaration = {
            **DELIVERY_DECLARATION,
            "panels": [
                {
                    **DELIVERY_DECLARATION["panels"][0],
                    "y_extent": [0.0, 2.0],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, DELIVERY_ROWS, "M4bad")
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("M4 per-flow delivery floor 0.995", stderr)
        self.assertIn("sub-pixel", stderr)

    def test_the_real_delivery_panel_renders_a_band_view(self):
        code, stderr, out = self.render_mandate(
            DELIVERY_DECLARATION, DELIVERY_ROWS, "M4"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-delivery.svg").read_text(encoding="utf-8")
        self.assertIn("band view", document)
        self.assertIn("not 0-based", document)
        # A delivery floor the run meets exactly still has to resolve its band:
        # a 0.5 % loss is a large visible step, not half a pixel.
        self.assertIn("M4 per-flow delivery floor 0.995", document)
        self.assertNotIn(">2.00<", document)

    def test_panels_whose_axis_can_already_show_their_bound_are_unchanged(self):
        # The deliberate coarse view, whose fine counterpart is M4-imbalance,
        # and a floor with half the axis between it and the data: both keep the
        # zero baseline they had. What they no longer keep is the axis that
        # topped out on the bound itself (M4-shares drew 0..0.2503, so a flow
        # *over* the fair share was clipped by the frame): the axis now carries
        # `MIN_HEADROOM_PIXELS` above every value it names.
        for name, declaration, rows, bound in (
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS, 0.25),
            ("M3frac", FRACTION_DECLARATION, FRACTION_ROWS, 0.35),
        ):
            with self.subTest(panel=name):
                code, stderr, out = self.render_mandate(declaration, rows, name)
                self.assertEqual(code, 0, stderr)
                panel_id = declaration["panels"][0]["id"]
                document = (out / f"{declaration['mandate']}-{panel_id}.svg").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("band view", document)
                ticks = [
                    float(value)
                    for value in MANDATE.re.findall(
                        r'text-anchor="end">([-0-9.]+)<', document
                    )
                ]
                self.assertEqual(ticks[0], 0.0)
                self.assertGreater(ticks[-1], bound, "no room above the bound")
                headroom = (
                    (ticks[-1] - bound) / (ticks[-1] - ticks[0])
                    * MANDATE.bar_plot_height(1)
                )
                self.assertGreaterEqual(headroom, MANDATE.MIN_HEADROOM_PIXELS)

    def test_the_headroom_policy_spends_the_pixel_floor_not_the_span_share(self):
        # A fair share pinned at 25 % has a data spread of a ten-thousandth, so
        # the span's own 5 % of headroom is a third of a pixel: the pixel floor
        # is the only thing that keeps an over-share bar drawable.
        values = [0.250029, 0.249914]
        low, high = MANDATE.axis_with_headroom(0.0, max(values), 0.25, 228)
        self.assertGreater((high - 0.25) / (high - low) * 228, MANDATE.MIN_HEADROOM_PIXELS)
        span_share_only = 0.25 + MANDATE.FRAME_HEADROOM * 0.25
        self.assertGreater(high, span_share_only)

    def test_a_floor_far_below_the_data_keeps_the_zero_baseline(self):
        series = [("fraction", [(1.0, 0.958217), (2.0, 0.958271)])]
        bounds = [{"y": 0.35, "label": "M3 floor 0.35x link rate"}]
        self.assertEqual(MANDATE.bar_axis_extent(series, bounds)[0], 0.0)

    def test_a_bound_the_bars_split_around_is_a_target_not_a_crossing(self):
        # M4-shares' fair share: the bars straddle it, so it is the value they are
        # read against and owes no attribution. M2-wire's lone bar past the
        # budget is the crossing the panel has to explain.
        shares = [0.250059, 0.250059, 0.249941, 0.249941, 0.250173, 0.249365,
                  0.250289, 0.250173]
        self.assertEqual(MANDATE.crossing_values(shares, 0.25), [])
        wire = [2.151595, 5.000262, 6.633134]
        self.assertEqual(MANDATE.crossing_values(wire, 6.0), [6.633134])

    # -- the governance test: a crossed bound must say what it governs -------

    def test_a_crossed_bound_without_the_run_is_refused(self):
        # The vacuity half: the M2 wire panel's own numbers, with the run's
        # measurements not supplied, is the panel the audit found misleading — a
        # crossing the panel cannot attribute. It is refused, not drawn.
        code, stderr, _ = self.render_mandate(WIRE_DECLARATION, WIRE_ROWS, "M2bare")
        self.assertNotEqual(code, 0)
        self.assertIn("M2 wire budget 6x", stderr)
        self.assertIn("run's own measurements were not supplied", stderr)
        self.assertIn("tolerated guard", stderr)

    def test_the_run_s_own_guards_attribute_the_crossed_wire_bound(self):
        out = self.root / "out-M2"
        declaration_path = self.write_mandate(
            WIRE_DECLARATION, WIRE_ROWS, name="M2run"
        )
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-wire.svg").read_text(encoding="utf-8")
        # Every drawn bound now says who is beyond it and by which guard, so the
        # crossing lone bar can no longer be read as a budget breach.
        self.assertIn("1 of 3 bars beyond it", document)
        self.assertIn("hostile_wire_guard=10", document)
        self.assertIn("lone_wire_guard=14", document)
        # the data itself is untouched: the same three bars, at the same heights
        rects = MANDATE.re.findall(r'<rect x="[-0-9.]+\w*"', document)
        self.assertTrue(rects)

    def test_a_declared_governance_attributes_the_bound_without_a_run(self):
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [
                        {"y": 6, "label": "M2 wire budget 6x", "series": "wire_x", "x": [1]}
                    ],
                }
            ],
        }
        code, stderr, out = self.render_mandate(declaration, WIRE_ROWS, "M2decl")
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-wire.svg").read_text(encoding="utf-8")
        self.assertIn("governs series wire_x", document)
        self.assertIn("governs x=1", document)
        bounds = MANDATE.re.findall(
            r'class="bound" x1="([0-9.]+)" y1="[0-9.]+" x2="([0-9.]+)"', document
        )
        self.assertEqual(len(bounds), 1, bounds)
        left, right = (float(value) for value in bounds[0])
        # governed by x=1 alone, so the line stops over the first bar group
        # instead of running to the plot's right edge as a panel-wide one does
        self.assertGreater(right - left, 0.0)
        self.assertLess(right, MANDATE.REPORT.WIDTH - MANDATE.REPORT.PAD_RIGHT)

    def test_a_governed_x_the_panel_does_not_draw_is_an_error(self):
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": "b", "x": [9]}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, WIRE_ROWS, "M2x")
        self.assertNotEqual(code, 0)
        self.assertIn("draws no category", stderr)

    def test_a_governed_series_the_panel_does_not_declare_is_an_error(self):
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": "b", "series": "clean"}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, WIRE_ROWS, "M2s")
        self.assertNotEqual(code, 0)
        self.assertIn("governs series 'clean'", stderr)

    def test_a_malformed_governed_x_is_an_error(self):
        for value, fragment in (
            ("all", "bound.x must be a number"),
            ([], "bound.x must be a number"),
            ({"min": 1}, "bound.x.max must be a number"),
            ({"min": 2, "max": 1}, "min <= max"),
        ):
            with self.subTest(x=value):
                declaration = {
                    **WIRE_DECLARATION,
                    "panels": [
                        {
                            **WIRE_DECLARATION["panels"][0],
                            "bounds": [{"y": 6, "label": "b", "x": value}],
                        }
                    ],
                }
                code, stderr, _ = self.render_mandate(declaration, WIRE_ROWS, "M2bad")
                self.assertNotEqual(code, 0)
                self.assertIn(fragment, stderr)

    def test_a_malformed_pinned_extent_is_an_error(self):
        for value, fragment in (
            ([1.0], "two-element [low, high] list"),
            ([2.0, 1.0], "low < high"),
            ([1.0, "high"], "must be a number"),
        ):
            with self.subTest(extent=value):
                declaration = {
                    **DELIVERY_DECLARATION,
                    "panels": [
                        {**DELIVERY_DECLARATION["panels"][0], "y_extent": value}
                    ],
                }
                code, stderr, _ = self.render_mandate(declaration, DELIVERY_ROWS, "M4e")
                self.assertNotEqual(code, 0)
                self.assertIn(fragment, stderr)

    def test_a_pinned_extent_on_a_cdf_panel_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][1], y_extent=[0.0, 100.0])
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[HEALTHY_DECLARATION["panels"][0], panel],
        )
        self.reject(declaration, fragment="cannot pin a cdf panel")

    def test_a_breached_delivery_floor_still_renders_and_shows_the_breach(self):
        # The other half of that rule: a floor the run asserts no looser guard
        # against is a breach when a bar crosses it, so the panel must keep its
        # evidence on the run that fails rather than refuse to be drawn. The
        # crossing also makes the panel a band view, so the breach is at scale.
        rows = [
            row if row[:3] != ["delivery", "hostile", 2.0] else ["delivery", "hostile", 2.0, 0.994]
            for row in DELIVERY_ROWS
        ]
        declaration_path = self.write_mandate(DELIVERY_DECLARATION, rows, name="M4breach")
        out = self.root / "out-M4breach"
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            "--run-values",
            json.dumps({"delivery_floor": 0.995, "hostile_p99_guard": 900.0}),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-delivery.svg").read_text(encoding="utf-8")
        # the breach is named, and no unrelated guard is pulled in by name
        self.assertIn("1 of 8 bars beyond it", document)
        self.assertNotIn("hostile_p99_guard", document)
        self.assertIn("band view", document)

    def test_run_values_that_are_not_an_object_are_an_error(self):
        declaration_path = self.write_mandate(WIRE_DECLARATION, WIRE_ROWS, name="M2v")
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(self.out),
            "--run-values",
            "[1, 2]",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("run values must be a JSON object", stderr)

    # -- the label-fit test: an annotation must lie inside the plot --------
    #
    # The measurement these come from: on the preserved battery run the three
    # panels whose bound sat at the top of a band-view axis (`M2-delivery`,
    # `M4-imbalance`, `M4-shares`) drew their label 4.6-14.7 px *above* the plot
    # area, across the legend, and a bound governing a narrow x-window drew its
    # label off the plot's left edge. The vertical part of the fit needs no
    # font at all (it is the anchor plus the ascent/descent); the horizontal
    # part is `rtp_trace_report.label_text_width`, an upper bound over the fonts
    # a browser resolves for the panel's 11px text style, pinned by
    # `RENDERED_LABEL_WIDTHS` to the widths the real renderer measured.

    def rendered_labels(self, declaration, rows, name, *arguments):
        """Render one panel and return ``(document, plot rect, [(declared, line, box)])``."""
        code, stderr, out = self.render_mandate(declaration, rows, name, *arguments)
        self.assertEqual(code, 0, stderr)
        panel_id = declaration["panels"][0]["id"]
        document = (out / f"{declaration['mandate']}-{panel_id}.svg").read_text(
            encoding="utf-8"
        )
        return (
            document,
            MANDATE.panel_plot_rect(panel_id, document),
            MANDATE.label_boxes(document),
        )

    def bound_line_y(self, document):
        match = MANDATE.re.search(r'class="bound" x1="[-0-9.]+" y1="([-0-9.]+)"', document)
        self.assertIsNotNone(match, "the panel draws no bound line")
        return float(match.group(1))

    def test_every_real_panel_draws_its_bound_labels_inside_its_plot_area(self):
        cases = (
            ("M1line", HEALTHY_DECLARATION, HEALTHY_ROWS, ()),
            (
                "M2wire",
                WIRE_DECLARATION,
                WIRE_ROWS,
                ("--run-values", json.dumps(WIRE_RUN_VALUES)),
            ),
            ("M2delivery", M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS, ()),
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS, ()),
            ("M4delivery", DELIVERY_DECLARATION, DELIVERY_ROWS, ()),
        )
        for name, declaration, rows, arguments in cases:
            with self.subTest(panel=name):
                document, plot, boxes = self.rendered_labels(
                    declaration, rows, name, *arguments
                )
                self.assertTrue(boxes, "the panel drew no bound label at all")
                self.assertEqual(
                    MANDATE.check_label_fit(declaration["panels"][0]["id"], document),
                    [],
                )
                left, top, right, bottom = plot
                for declared, line, (x0, y0, x1, y1) in boxes:
                    # The vertical extent is width-free, so it is asserted
                    # against the plot rectangle directly rather than through
                    # the model the check uses.
                    self.assertGreaterEqual(y0, top, declared)
                    self.assertLessEqual(y1, bottom, declared)
                    self.assertGreaterEqual(x0, left, declared)
                    self.assertLessEqual(x1, right, declared)
                    self.assertAlmostEqual(
                        y1 - y0,
                        MANDATE.REPORT.LABEL_ASCENT_PX + MANDATE.REPORT.LABEL_DESCENT_PX,
                    )

    def test_a_bound_at_the_top_of_its_axis_is_labelled_below_its_line(self):
        # `M2-delivery`'s floor is the band view's own top, so there is no room
        # for the label above the line; it has to drop below it rather than
        # escape into the legend, which is where the audit measured it.
        for name, declaration, rows in (
            ("M2delivery", M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS),
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS),
        ):
            with self.subTest(panel=name):
                document, plot, boxes = self.rendered_labels(declaration, rows, name)
                bound_y = self.bound_line_y(document)
                for _, line, (_, y0, _, _) in boxes:
                    self.assertGreater(
                        y0,
                        bound_y,
                        f"{line!r} is drawn above its own bound and out of the plot",
                    )
                self.assertGreaterEqual(boxes[0][2][1], plot[1])

    def test_a_badly_placed_label_fails_the_fit_check(self):
        # The vacuity pair for the check itself: the same markup, with its own
        # label moved to the canvas origin, must go red while the untouched
        # markup passes. Without this the check could be a predicate that is
        # true of everything.
        markup = MANDATE.svg_bar_chart(
            "t",
            "x",
            "arm",
            [("s", [(1.0, 1.0), (2.0, 2.5)])],
            [{"y": 2.0, "label": "a bound"}],
        )
        self.assertEqual(MANDATE.check_label_fit("p", markup), [])
        misplaced = MANDATE.re.sub(
            r'(<text class="bound-label" x=")[-0-9.]+(" y=")[-0-9.]+(")',
            r"\g<1>0.0\g<2>0.0\g<3>",
            markup,
        )
        self.assertNotEqual(misplaced, markup)
        problems = MANDATE.check_label_fit("p", misplaced)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not fit the plot area", problems[0])
        self.assertIn("past its left edge", problems[0])
        self.assertIn("above it", problems[0])

    def test_a_label_that_cannot_be_wrapped_into_the_plot_is_refused(self):
        # The other vacuity half: a label too long to lay out inside the plot
        # is an error naming the panel and the overflow, not a panel drawn with
        # its annotation running off the edge. This is the reachable failure --
        # `LABEL_MAX_LINES` caps the wrap, so the remainder is one line that no
        # longer fits.
        label = "M2 wire budget 6x [" + "; ".join(
            f"arm_{index}_guard=1000000" for index in range(40)
        ) + "]"
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": label}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(
            declaration,
            WIRE_ROWS,
            "M2long",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("panel 'wire'", stderr)
        self.assertIn("does not fit the plot area", stderr)
        self.assertIn("past its left edge", stderr)

    def test_a_line_panels_label_is_held_to_the_same_fit(self):
        # The check is on the drawn SVG, so it covers every chart kind, not
        # just the bar panels the attribution clause made long.
        label = "M1 ceiling 250 ms [" + "; ".join(
            f"arm_{index}_guard=1000000" for index in range(40)
        ) + "]"
        declaration = {
            **HEALTHY_DECLARATION,
            "panels": [
                {
                    **HEALTHY_DECLARATION["panels"][0],
                    "bounds": [{"y": 250.0, "label": label}],
                },
                HEALTHY_DECLARATION["panels"][1],
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, HEALTHY_ROWS, "M1long")
        self.assertNotEqual(code, 0)
        self.assertIn("panel 'latency'", stderr)
        self.assertIn("does not fit the plot area", stderr)

    def test_a_long_guard_list_wraps_into_the_plot(self):
        rows = [
            ["panel", "series", "x", "y"],
            ["wire", "wire_x", 1.0, 2.1],
            ["wire", "wire_x", 2.0, 5.0],
            ["wire", "wire_x", 3.0, 4.4],
            ["wire", "wire_x", 4.0, 6.63],
        ]
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": "M2 wire budget 6x"}],
                }
            ],
        }
        document, plot, boxes = self.rendered_labels(
            declaration,
            rows,
            "M2wrap",
            "--run-values",
            json.dumps(WIRE_FOUR_ARM_RUN_VALUES),
        )
        self.assertGreater(len(boxes), 1, "a four-arm guard list has to wrap")
        self.assertEqual(MANDATE.check_label_fit("wire", document), [])
        for declared, _, (x0, y0, x1, y1) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])
            self.assertGreaterEqual(y0, plot[1])
            self.assertLessEqual(y1, plot[3])
        # the whole sentence survives in the first line's title and the lines
        # re-join to it, so a wrapped label is still one annotation
        self.assertEqual(" ".join(line for _, line, _ in boxes), boxes[0][0])
        self.assertIn("clean_wire_guard=3", boxes[0][0])
        self.assertIn("burst_wire_guard=21", boxes[0][0])

    def test_a_narrow_governed_window_moves_the_label_inside_the_plot(self):
        # A bound governing the first of three categories has a line only as
        # long as that category's slot; the label's anchor has to move right
        # rather than run the text off the plot's left edge, which is what the
        # old fixed anchor did.
        declaration = {
            **WIRE_DECLARATION,
            "panels": [
                {
                    **WIRE_DECLARATION["panels"][0],
                    "bounds": [
                        {
                            "y": 6,
                            "label": "M2 wire budget 6x",
                            "series": "wire_x",
                            "x": [1],
                        }
                    ],
                }
            ],
        }
        document, plot, boxes = self.rendered_labels(
            declaration,
            WIRE_ROWS,
            "M2narrow",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        line = MANDATE.re.search(
            r'class="bound" x1="[-0-9.]+" y1="[-0-9.]+" x2="([-0-9.]+)"',
            document,
        )
        line_right = float(line.group(1))
        for declared, _, (x0, _, x1, _) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])
            # the label left its governed window rather than leave the plot
            self.assertGreater(x1, line_right)
        self.assertGreater(line_right - plot[0], 0.0)

    def test_the_label_width_model_does_not_underestimate_the_rendered_text(self):
        for label, measured in RENDERED_LABEL_WIDTHS:
            with self.subTest(label=label):
                self.assertGreaterEqual(
                    MANDATE.REPORT.label_text_width(label),
                    measured,
                    "the width model must not be narrower than the text the "
                    "browser draws, or a label it calls fitting can overflow",
                )

    def test_the_width_model_check_fails_when_the_model_is_narrowed(self):
        # The vacuity half of the calibration: the same fixture test on a model
        # narrowed below the measured widths must go red, so the assertion above
        # is about the model and not a tautology.
        label, measured = RENDERED_LABEL_WIDTHS[0]
        with mock.patch.object(MANDATE.REPORT, "LABEL_ADVANCE_SAFETY", 0.2):
            self.assertLess(MANDATE.REPORT.label_text_width(label), measured)

    def test_every_bar_beyond_the_bound_is_not_labelled_as_a_crossing(self):
        # A boundary case of the attribution rule: when *every* bar is past the
        # bound the crossing is the verdict's, not one arm's tolerated guard, so
        # the panel draws no `N of M` clause. Uniform failure is read from the
        # bars; a single bar past it is the case the clause exists for. The
        # guards are still named -- they are the arms' own bounds, and the axis
        # has to carry them whether or not a bar has been past the budget yet --
        # but no crossing is attributed.
        rows = [
            ["panel", "series", "x", "y"],
            ["wire", "wire_x", 1.0, 7.2],
            ["wire", "wire_x", 2.0, 9.5],
            ["wire", "wire_x", 3.0, 8.1],
        ]
        document, plot, boxes = self.rendered_labels(
            WIRE_DECLARATION,
            rows,
            "M2all",
            "--run-values",
            json.dumps(WIRE_FOUR_ARM_RUN_VALUES),
        )
        self.assertEqual(len(boxes), 1)
        self.assertIn("M2 wire budget 6x", boxes[0][1])
        self.assertNotIn("beyond it", document)
        self.assertIn("run guards", boxes[0][1])
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(
            ticks[-1],
            max(value for _, value in MANDATE.run_guards(
                WIRE_FOUR_ARM_RUN_VALUES,
                [("wire_x", [])],
                "M2 wire budget 6x",
            )),
            "the axis must carry every guard the label names",
        )
        for _, _, (x0, y0, x1, y1) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])

    def test_a_lone_bar_beyond_the_bound_names_the_crossing_and_the_guards(self):
        # The other boundary case, and the one the audit's label was for: one of
        # three bars past the budget, with the run's own guards named, all of it
        # inside the plot area.
        document, plot, boxes = self.rendered_labels(
            WIRE_DECLARATION,
            WIRE_ROWS,
            "M2one",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][1], boxes[0][0])
        self.assertIn("1 of 3 bars beyond it", boxes[0][1])
        self.assertIn("hostile_wire_guard=10", boxes[0][1])
        self.assertIn("lone_wire_guard=14", boxes[0][1])
        for _, _, (x0, _, x1, _) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])

    # -- healthy renders ---------------------------------------------------

    def test_healthy_two_panel_input_renders_and_reports_the_panel_count(self):
        declaration_path = self.write_mandate()
        code, stdout, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("panels: 2", stdout)
        self.assertIn("mandate: M1", stdout)
        svg_paths = [self.out / "M1-latency.svg", self.out / "M1-cdf.svg"]
        for svg_path in svg_paths:
            self.assertTrue(svg_path.is_file(), svg_path)
            self.assertIn(f"svg: {svg_path}", stdout)
        self.assertNotIn("png:", stdout)

    def test_every_chart_kind_renders(self):
        for chart, identifier in (("line", "latency"), ("cdf", "cdf"), ("bar", "goodput")):
            with self.subTest(chart=chart):
                declaration = {
                    "mandate": "MX",
                    "title": f"{chart} panel",
                    "x_label": "x",
                    "y_label": "y",
                    "panels": [
                        {
                            "id": identifier,
                            "chart": chart,
                            "series": [{"name": "s"}],
                            "bounds": [],
                        }
                    ],
                }
                rows = [
                    ["panel", "series", "x", "y"],
                    [identifier, "s", 1.0, 10.0],
                    [identifier, "s", 2.0, 40.0],
                    [identifier, "s", 3.0, 65.0],
                ]
                out = self.root / f"out-{chart}"
                declaration_path = self.write_mandate(declaration, rows, name="MX")
                code, stdout, stderr = self.run_main(
                    str(declaration_path), "--no-rasterize", "--out", str(out)
                )
                self.assertEqual(code, 0, stderr)
                self.assertIn("panels: 1", stdout)
                self.assertTrue((out / f"MX-{identifier}.svg").is_file())

    def test_bounds_are_emitted_into_the_svg(self):
        declaration_path = self.write_mandate()
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        # The artifact is the evidence: the bound line and its label are in it.
        self.assertIn('class="bound"', document)
        self.assertEqual(document.count('class="bound"'), 1)
        self.assertIn("M1 ceiling 250 ms", document)

    def test_bar_bounds_are_emitted_into_the_svg(self):
        declaration_path = self.write_mandate(BAR_DECLARATION, BAR_ROWS, name="M3")
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M3-goodput.svg").read_text(encoding="utf-8")
        self.assertIn('class="bound"', document)
        self.assertIn("M3 floor 0.35x link rate", document)
        # The bars are the series geometry, and the plot background is not.
        self.assertGreater(MANDATE.RENDER.panel_series_count(document), 0)

    def test_series_are_plotted_in_ascending_x_order(self):
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "impaired", 3.0, 30.0],
            ["latency", "impaired", 1.0, 10.0],
            ["cdf", "impaired", 30.0, 50.0],
            ["cdf", "impaired", 10.0, 100.0],
        ]
        declaration_path = self.write_mandate(rows=rows)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        polyline = MANDATE.RENDER.POLYLINE_RE.search(document).group(1)
        xs = [float(pair.split(",")[0]) for pair in polyline.split()]
        self.assertEqual(xs, sorted(xs))

    # -- declaration failures ---------------------------------------------

    def test_missing_declaration_is_an_error(self):
        code, _, stderr = self.run_main(
            str(self.root / "absent.json"), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate declaration not found", stderr)

    def test_malformed_declaration_json_is_an_error(self):
        path = self.root / "M1.json"
        path.write_text("{not json", encoding="utf-8")
        code, _, stderr = self.run_main(str(path), "--no-rasterize", "--out", str(self.out))
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", stderr)

    def test_declaration_that_is_not_an_object_is_an_error(self):
        path = self.root / "M1.json"
        path.write_text("[1, 2]", encoding="utf-8")
        code, _, stderr = self.run_main(str(path), "--no-rasterize", "--out", str(self.out))
        self.assertNotEqual(code, 0)
        self.assertIn("must be a JSON object", stderr)

    def test_missing_mandate_name_is_an_error(self):
        declaration = dict(HEALTHY_DECLARATION)
        declaration.pop("mandate")
        self.reject(declaration, fragment="mandate must be a non-empty string")

    def test_empty_panel_list_is_an_error(self):
        declaration = dict(HEALTHY_DECLARATION, panels=[])
        self.reject(declaration, fragment="panels must be a non-empty list")

    def test_duplicate_panel_id_is_an_error(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[HEALTHY_DECLARATION["panels"][0], dict(HEALTHY_DECLARATION["panels"][1], id="latency")],
        )
        self.reject(declaration, fragment="is declared twice")

    def test_unusable_panel_id_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], id="lat/ency")
        self.reject(dict(HEALTHY_DECLARATION, panels=[panel]), fragment="must match")

    def test_malformed_chart_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], chart="scatter")
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment="is not a chart; expected one of line, cdf, bar")

    def test_series_without_a_name_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], series=[{"role": "impaired"}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".series[0].name must be a non-empty string")

    def test_empty_series_list_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], series=[])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".series must be a non-empty list")

    def test_bound_without_a_label_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], bounds=[{"y": 250.0}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".bounds[0].label must be a non-empty string")

    def test_bound_with_a_non_numeric_y_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], bounds=[{"y": "ceiling", "label": "c"}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".bounds[0].y must be a number")

    # -- data failures -----------------------------------------------------

    def test_missing_data_csv_is_an_error(self):
        declaration_path = self.write_mandate(rows=None)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate data CSV not found", stderr)

    def test_empty_csv_file_is_an_error(self):
        self.reject(rows="", fragment="mandate data CSV is empty (no header row)")

    def test_header_only_csv_is_an_error(self):
        self.reject(rows=[["panel", "series", "x", "y"]], fragment="has no data rows")

    def test_csv_header_mismatch_is_an_error(self):
        rows = [["panel", "series", "x", "y_ms"]] + HEALTHY_ROWS[1:]
        self.reject(rows=rows, fragment="header must be panel,series,x,y")

    def test_csv_row_with_the_wrong_field_count_is_an_error(self):
        rows = HEALTHY_ROWS[:1] + HEALTHY_ROWS[1:] + [["latency", "impaired", 9.0]]
        self.reject(rows=rows, fragment="has 3 field(s), expected 4")

    def test_declared_panel_with_no_rows_is_rejected(self):
        rows = [["panel", "series", "x", "y"]] + [row for row in HEALTHY_ROWS[1:] if row[0] != "cdf"]
        message = self.reject(rows=rows, fragment="has no rows for it")
        self.assertIn("panel 'cdf'", message)
        self.assertIn("an empty chart is not a graph", message)

    def test_declared_series_with_no_rows_is_rejected(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[dict(HEALTHY_DECLARATION["panels"][0], series=[{"name": "impaired"}, {"name": "clean"}]), HEALTHY_DECLARATION["panels"][1]],
        )
        message = self.reject(declaration, fragment="has no row for it")
        self.assertIn("declares series 'clean'", message)

    def test_csv_row_naming_an_undeclared_series_is_rejected(self):
        rows = HEALTHY_ROWS + [["latency", "clean", 3.0, 44.0]]
        message = self.reject(rows=rows, fragment="which the declaration does not declare")
        self.assertIn("series 'clean' in panel 'latency'", message)

    def test_csv_row_naming_an_undeclared_panel_is_rejected(self):
        rows = HEALTHY_ROWS + [["M9", "impaired", 3.0, 44.0]]
        message = self.reject(rows=rows, fragment="which the declaration does not declare")
        self.assertIn("panel 'M9'", message)

    def test_non_numeric_x_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", "abc", 1.0]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-numeric x/y")

    def test_non_numeric_y_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", 1.0, ""]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-numeric x/y")

    def test_non_finite_y_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", 1.0, "nan"]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-finite x/y")

    def test_cdf_y_outside_the_percentile_axis_is_rejected(self):
        rows = [["panel", "series", "x", "y"]]
        rows += [row for row in HEALTHY_ROWS[1:] if row[0] == "latency"]
        rows += [
            ["cdf", "impaired", 12.5, 250.0],
            ["cdf", "impaired", 31.5, 66.7],
            ["cdf", "impaired", 88.25, 100.0],
        ]
        self.reject(rows=rows, fragment="a cdf y is a percentile")

    def test_panel_labels_override_the_mandate_labels(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[
                HEALTHY_DECLARATION["panels"][0],
                dict(
                    HEALTHY_DECLARATION["panels"][1],
                    x_label="RTT (ms)",
                    y_label="samples <= x (%)",
                ),
            ],
        )
        declaration_path = self.write_mandate(declaration)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        cdf = (self.out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertIn("samples &lt;= x (%)", cdf)
        self.assertIn("RTT (ms)", cdf)
        latency = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertIn("elapsed time (s)", latency)
        self.assertNotIn("samples", latency)

    def test_panel_label_of_the_wrong_type_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], y_label=7)
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment="panels[0].y_label must be a string")

    def test_out_path_that_is_a_file_is_an_error(self):
        declaration_path = self.write_mandate()
        blocker = self.root / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(blocker)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--out is not a directory", stderr)

    # -- rasterization reasons honestly ------------------------------------

    def test_no_rasterize_writes_svgs_without_consulting_a_browser(self):
        declaration_path = self.write_mandate()
        with mock.patch.object(
            MANDATE.RENDER, "find_browser", side_effect=AssertionError("no browser may be consulted")
        ):
            code, stdout, stderr = self.run_main(
                str(declaration_path), "--no-rasterize", "--out", str(self.out)
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn("panels: 2", stdout)
        self.assertEqual(len(list(self.out.glob("*.svg"))), 2)

    def test_rasterize_without_a_browser_is_an_error_after_writing_svgs(self):
        declaration_path = self.write_mandate()
        with mock.patch.object(MANDATE.RENDER, "find_browser", return_value=None):
            code, _, stderr = self.run_main(
                str(declaration_path), "--out", str(self.out)
            )
        self.assertNotEqual(code, 0)
        self.assertIn("no headless browser found", stderr)
        # The SVG evidence is written and verified even though the PNG step failed.
        self.assertTrue((self.out / "M1-latency.svg").is_file())
        self.assertTrue((self.out / "M1-cdf.svg").is_file())

    def test_rasterize_records_verified_pngs(self):
        declaration_path = self.write_mandate()
        browser = self.write_browser(FAKE_BROWSER.format(png=ONE_PIXEL_PNG))
        code, stdout, stderr = self.run_main(
            str(declaration_path), "--browser", browser, "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(list(self.out.glob("*.png"))), 2)
        for png_path in sorted(self.out.glob("*.png")):
            self.assertEqual(
                MANDATE.RENDER.png_dimensions(png_path.read_bytes()), (1, 1)
            )
            self.assertIn(f"png: {png_path}", stdout)

    def test_rasterize_rejects_a_browser_that_writes_no_png(self):
        declaration_path = self.write_mandate()
        browser = self.write_browser(NULL_BROWSER)
        code, _, stderr = self.run_main(
            str(declaration_path), "--browser", browser, "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("did not produce a valid PNG", stderr)

    def test_cli_exit_status_is_nonzero_for_a_missing_csv(self):
        declaration_path = self.write_mandate(rows=None)
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                str(declaration_path),
                "--no-rasterize",
                "--out",
                str(self.out),
            ],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("mandate data CSV not found", completed.stderr)

    def test_find_browser_env_override_is_honoured(self):
        override = Path(sys.executable)
        with mock.patch.dict(os.environ, {MANDATE.RENDER.BROWSER_ENV: str(override)}):
            self.assertEqual(MANDATE.RENDER.find_browser(), str(override))

    # -- the readings that only the eye made, now measurements ----------------
    #
    # Every defect below was found by *looking at* a run whose four mandate
    # lines passed: the `M2-wire` panel announced guards at 10x and 14x on an
    # axis that topped out at 6.6; `M4-shares` drew its fair share at the very
    # top of its own axis, so a flow over the share could not be drawn at all;
    # one series' three bars were drawn flush and read as a staircase; the
    # legend said `wire_x`; and a label long enough to run off the canvas was
    # drawn anyway. Each test renders the broken input and requires the refusal
    # (red), then renders the real input and requires the check to pass (green).

    def test_a_guard_the_panel_names_outside_its_axis_is_refused(self):
        broken = {
            **WIRE_DECLARATION,
            "panels": [{**WIRE_DECLARATION["panels"][0], "y_extent": [0.0, 7.0]}],
        }
        code, stderr, _ = self.render_mandate(
            broken,
            WIRE_ROWS,
            "M2pin",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("named guard 14", stderr)
        self.assertIn("does not resolve", stderr)
        # green: the automatic axis carries both guards, inside the frame
        document, _, _ = self.rendered_labels(
            WIRE_DECLARATION,
            WIRE_ROWS,
            "M2carry",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(ticks[-1], 14.0, "the axis tops out below the guard named")
        self.assertEqual(
            MANDATE.check_named_values_in_axis(
                "wire",
                [{"y": 6.0, "label": "M2 wire budget 6x"}],
                [10.0, 14.0],
                (ticks[0], ticks[-1]),
            ),
            [],
        )

    def test_a_bound_with_no_room_above_it_is_refused(self):
        # The axis the audit found on M4-shares: 0..0.25, the fair share itself,
        # so every bar is clipped at the line the panel exists to watch.
        broken = {
            **SHARES_DECLARATION,
            "panels": [
                {**SHARES_DECLARATION["panels"][0], "y_extent": [0.0, 0.25]}
            ],
        }
        code, stderr, _ = self.render_mandate(broken, SHARES_ROWS, "M4flat")
        self.assertNotEqual(code, 0)
        self.assertIn("over-bound bar", stderr)
        self.assertIn("same picture", stderr)
        # green: the automatic extent keeps MIN_HEADROOM_PIXELS over the bound
        document, _, _ = self.rendered_labels(
            SHARES_DECLARATION, SHARES_ROWS, "M4room"
        )
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(ticks[-1], 0.25)
        self.assertEqual(
            MANDATE.check_bound_headroom(
                "shares",
                [{"y": 0.25, "label": "fair share 25.0%"}],
                [],
                (ticks[0], ticks[-1]),
            ),
            [],
        )

    def test_a_bound_label_drawn_twice_at_one_anchor_is_refused(self):
        code, stderr, out = self.render_mandate(SHARES_DECLARATION, SHARES_ROWS, "M4dup")
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        self.assertEqual(MANDATE.check_label_overlap("shares", document), [])
        element = MANDATE.re.search(
            r'<text class="bound-label".*?</text>', document, MANDATE.re.S
        ).group(0)
        # red: the artifact the run drew, with its label element duplicated on
        # the same anchor -- the `fair share 25.0%fair share 25.0%` reading
        problems = MANDATE.check_label_overlap("shares", document.replace(element, element + element, 1))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("drawn twice on the same anchor", problems[0])
        # and two *different* labels over one another are refused as an overlap
        other = element.replace("fair share 25.0%", "fair-share bound")
        overlap = MANDATE.check_label_overlap("shares", document.replace(element, element + other, 1))
        self.assertEqual(len(overlap), 1, overlap)
        self.assertIn("overlap", overlap[0])

    def test_bars_drawn_flush_are_refused(self):
        # red: the geometry the preserved run drew -- three 311 px bars whose
        # rectangles overlapped by 52 px, which the eye read as one staircase.
        staircase = (
            '<svg viewBox="0 0 960 300">'
            '<rect x="72.0" y="178.2" width="311.0" height="73.8" fill="#2563eb"/>'
            '<rect x="331.2" y="90.1" width="311.0" height="161.9" fill="#2563eb"/>'
            '<rect x="590.4" y="31.3" width="311.0" height="220.7" fill="#2563eb"/>'
            "</svg>"
        )
        problems = MANDATE.check_bar_separation("wire", staircase)
        self.assertTrue(problems)
        self.assertIn("overlap by 51.8 px", problems[0])
        # green: the rendered panel's three bars are separate
        code, stderr, out = self.render_mandate(
            WIRE_DECLARATION,
            WIRE_ROWS,
            "M2gap",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-wire.svg").read_text(encoding="utf-8")
        self.assertEqual(len(MANDATE.bar_boxes(document)), 3)
        self.assertEqual(MANDATE.check_bar_separation("wire", document), [])

    def test_a_legend_that_draws_a_column_name_is_refused(self):
        code, stderr, out = self.render_mandate(
            WIRE_DECLARATION,
            WIRE_ROWS,
            "M2legend",
            "--run-values",
            json.dumps(WIRE_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-wire.svg").read_text(encoding="utf-8")
        self.assertEqual(MANDATE.legend_text(document), ["own-wire multiple"])
        series = [("wire_x", [(1.0, 2.0)])]
        self.assertEqual(MANDATE.check_series_labels("wire", document, series), [])
        # red: the same panel with the producer's column name in the legend --
        # the label the preserved run drew
        raw = document.replace("own-wire multiple", "wire_x")
        problems = MANDATE.check_series_labels("wire", raw, series)
        self.assertTrue(problems)
        self.assertIn("wire_x", problems[0])

    def test_a_clipped_label_and_a_placeholder_are_refused(self):
        # red: the y label with the band-view note the old renderer appended,
        # which is 66 characters long and runs off the top and bottom of the
        # 300 px canvas when it is rotated down the 18 px left margin
        long_label = {
            **M2_DELIVERY_DECLARATION,
            "panels": [
                {
                    **M2_DELIVERY_DECLARATION["panels"][0],
                    "y_label": "delivery (received / offered) [band view 0.979..1.001, "
                    "not 0-based]",
                }
            ],
        }
        code, stderr, _ = self.render_mandate(long_label, M2_DELIVERY_ROWS, "M2clip")
        self.assertNotEqual(code, 0)
        self.assertIn("draws it clipped", stderr)
        self.assertIn("px above it", stderr)
        # red: a label carrying the empty template its absent evidence left
        broken = {
            **M2_DELIVERY_DECLARATION,
            "panels": [
                {
                    **M2_DELIVERY_DECLARATION["panels"][0],
                    "bounds": [
                        {
                            "y": 1.0,
                            "label": "M2 delivery floor 1.000 []",
                        }
                    ],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(broken, M2_DELIVERY_ROWS, "M2empty")
        self.assertNotEqual(code, 0)
        self.assertIn("empty placeholder", stderr)
        # green: the real panel's every text is inside the canvas
        document, _, _ = self.rendered_labels(
            M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS, "M2txt"
        )
        self.assertEqual(MANDATE.check_canvas_text_fit("delivery", document), [])
        self.assertEqual(MANDATE.band_view_note((0.979, 1.001)),
                         "band view 0.979..1.001, not 0-based")
        self.assertEqual(MANDATE.band_view_note((0.0, 0.26)), "")


    def test_the_wire_panel_renders_on_a_run_whose_worst_arm_touches_the_budget(self):
        # The runs the tool refused (2.12 / 4.91 / 5.91 and / 6.05): the band the
        # axis test measures is now the tolerance the run's guards open between
        # the budget and the arm's own limit, not the sliver between the budget
        # and the worst arm -- which is a sliver *by construction* on any run
        # whose worst arm lands near the budget. The range is the bound plus a
        # margin, not the observed maximum, so the panel draws the crossing it
        # is named for on both runs.
        for name, worst in (("below", 5.91), ("above", 6.05)):
            with self.subTest(worst_arm=worst):
                rows = [
                    ["panel", "series", "x", "y"],
                    ["wire", "wire_x", 1.0, 2.12],
                    ["wire", "wire_x", 2.0, 4.91],
                    ["wire", "wire_x", 3.0, worst],
                ]
                code, stderr, out = self.render_mandate(
                    WIRE_DECLARATION,
                    rows,
                    f"M2{name}",
                    "--run-values",
                    json.dumps(WIRE_RUN_VALUES),
                )
                self.assertEqual(code, 0, stderr)
                document = (out / "M2-wire.svg").read_text(encoding="utf-8")
                ticks = [
                    float(value)
                    for value in MANDATE.re.findall(
                        r'text-anchor="end">([-0-9.]+)<', document
                    )
                ]
                self.assertGreater(ticks[-1], 14.0)
                self.assertIn("hostile_wire_guard=10", document)
                self.assertIn("lone_wire_guard=14", document)

    def test_a_bound_with_no_tolerance_and_a_sliver_margin_is_still_refused(self):
        # The vacuity half of the tolerance rule: the same run, with the guards
        # the label would name not supplied. The observed margin is then the only
        # band there is -- 0.09 of a 6x budget over a 6.2 axis, 3.3 px -- and it
        # is still refused. The rule decides *which* region the axis owes the
        # reader; it does not relax the threshold.
        rows = [
            ["panel", "series", "x", "y"],
            ["wire", "wire_x", 1.0, 2.12],
            ["wire", "wire_x", 2.0, 4.91],
            ["wire", "wire_x", 3.0, 5.91],
        ]
        code, stderr, _ = self.render_mandate(WIRE_DECLARATION, rows, "M2noguard")
        self.assertNotEqual(code, 0)
        self.assertIn("sub-pixel", stderr)
        self.assertIn("M2 wire budget 6x", stderr)

    def test_a_panel_labelled_with_a_sibling_s_unit_is_refused(self):
        series = [("fraction", [(1.0, 0.958217), (2.0, 0.958271)])]
        # red: the label the preserved run drew on the fraction panel -- the
        # goodput panel's unit, carried over the mandate's shared y_label
        problems = MANDATE.check_axis_label(
            "fraction", "MiB/s", series, declared=None, carried="MiB/s"
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("MiB/s", problems[0])
        # green: the label a single-series panel draws for itself
        self.assertEqual(
            MANDATE.check_axis_label(
                "fraction",
                "fraction of link rate",
                series,
                declared=None,
                carried="MiB/s",
            ),
            [],
        )
        # a panel that states its own label keeps its word, and a panel with
        # several series has no single quantity to name
        self.assertEqual(
            MANDATE.check_axis_label(
                "fraction", "MiB/s", series, declared="MiB/s", carried="MiB/s"
            ),
            [],
        )
        self.assertEqual(
            MANDATE.check_axis_label(
                "goodput",
                "MiB/s",
                [("delivered", []), ("shaper_forwarded", [])],
                declared=None,
                carried="MiB/s",
            ),
            [],
        )

    def test_an_x_axis_that_contradicts_the_run_s_categories_is_refused(self):
        run = {"reps": 3, "measured_s": 18.0}
        # red: M3 draws one bar per repetition at x=1..3 and labelled both its
        # panels `seed`
        problems = MANDATE.check_x_axis_label(
            "fraction", "seed", [1.0, 2.0, 3.0], run
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("seed", problems[0])
        self.assertIn("reps=3", problems[0])
        self.assertEqual(
            MANDATE.check_x_axis_label(
                "fraction", "rep (1..3)", [1.0, 2.0, 3.0], run
            ),
            [],
        )
        # green: a panel whose categories are not the run's repetitions, or a
        # run with no repetition count, keeps the declaration's label
        self.assertEqual(
            MANDATE.check_x_axis_label("goodput", "seed", [11.0, 21.0, 31.0], run),
            [],
        )
        self.assertEqual(
            MANDATE.check_x_axis_label("fraction", "seed", [1.0, 2.0], {"reps": 3}),
            [],
        )

    def test_the_m3_panels_name_their_own_quantity_and_their_repetitions(self):
        declaration = {
            "mandate": "M3",
            "title": "M3 bulk goodput",
            "x_label": "seed",
            "y_label": "MiB/s",
            "panels": [
                {
                    "id": "goodput",
                    "chart": "bar",
                    "series": [{"name": "delivered"}, {"name": "shaper_forwarded"}],
                    "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
                },
                {
                    "id": "fraction",
                    "chart": "bar",
                    "series": [{"name": "fraction"}],
                    "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
                },
            ],
        }
        rows = [
            ["panel", "series", "x", "y"],
            ["goodput", "delivered", 1.0, 0.958],
            ["goodput", "shaper_forwarded", 1.0, 0.968],
            ["fraction", "fraction", 1.0, 0.958],
            ["goodput", "delivered", 2.0, 0.953],
            ["goodput", "shaper_forwarded", 2.0, 0.961],
            ["fraction", "fraction", 2.0, 0.953],
            ["goodput", "delivered", 3.0, 0.948],
            ["goodput", "shaper_forwarded", 3.0, 0.955],
            ["fraction", "fraction", 3.0, 0.948],
        ]
        code, stderr, out = self.render_mandate(
            declaration,
            rows,
            "M3labels",
            "--run-values",
            json.dumps({"reps": 3, "measured_s": 18.0, "floor": 0.35}),
        )
        self.assertEqual(code, 0, stderr)
        fraction = (out / "M3-fraction.svg").read_text(encoding="utf-8")
        self.assertIn("fraction of link rate", fraction)
        self.assertNotIn("MiB/s", fraction)
        self.assertIn("rep (1..3)", fraction)
        goodput = (out / "M3-goodput.svg").read_text(encoding="utf-8")
        self.assertIn("MiB/s", goodput)
        self.assertIn("rep (1..3)", goodput)
        self.assertNotIn("seed", goodput)
    def test_a_tick_that_rounds_to_zero_is_not_drawn_negative(self):
        # M4-imbalance's band view starts a few ten-thousandths below zero, and
        # the tick there used to read `-0.00` under an all-positive panel.
        self.assertEqual(MANDATE.tick_label(-0.000344, 2), "0.00")
        self.assertEqual(MANDATE.tick_label(-1.5, 2), "-1.50")
        self.assertEqual(MANDATE.tick_label(0.25, 2), "0.25")
        declaration = {
            **FRACTION_DECLARATION,
            "panels": [
                {
                    **FRACTION_DECLARATION["panels"][0],
                    "series": [{"name": "delta"}],
                    "bounds": [{"y": 0.01, "label": "bound"}],
                }
            ],
        }
        rows = [
            ["panel", "series", "x", "y"],
            ["fraction", "delta", 1.0, -0.000344],
            ["fraction", "delta", 2.0, 0.000115],
            ["fraction", "delta", 3.0, 0.000115],
        ]
        code, stderr, out = self.render_mandate(declaration, rows, "MXzero")
        self.assertEqual(code, 0, stderr)
        document = (out / "M3-fraction.svg").read_text(encoding="utf-8")
        ticks = MANDATE.axis_tick_labels(document)
        self.assertEqual(len(ticks), 6, ticks)
        # No tick carries a sign it does not mean: a value that rounds to zero
        # is drawn without one (the negative tick here is a real -0.0003, so it
        # keeps its sign).
        self.assertEqual(
            [tick for tick in ticks if tick.startswith("-") and float(tick) == 0.0],
            [],
        )
        # ...and the six ticks are six values: the resolution comes from the step
        # between them, not from the sign of the axis' lower edge.
        self.assertEqual(len(set(ticks)), 6, ticks)
        self.assertEqual(MANDATE.check_tick_labels_distinct("fraction", document), [])

    def test_a_malformed_censoring_reading_is_an_error(self):
        # The reading is what the panel states, so a reading that is not an
        # object of measured tokens is an error rather than a panel drawn
        # without one -- the whole reason the band exists.
        declaration_path = self.write_mandate(
            LONE_TAIL_DECLARATION, _latency_rows(TRUNCATED_CLIMB), name="M1badcens"
        )
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(self.out),
            "--run-censoring",
            '{"lone_tail": "Censored"}',
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("must be a non-empty object", stderr)
        self.assertIn("'lone_tail'", stderr)

    def test_a_band_view_whose_ticks_repeat_is_refused(self):
        # The measured defect: `M4-imbalance` draws a 1 % departure bound over an
        # axis spanning 1.3 % of the share around zero, and two decimals printed
        # its six ticks as `0.01 0.01 0.01 0.00 0.00 0.00` -- an axis too coarse
        # for the departure the panel exists to show.
        series = [
            ("clean", [(1.0, 0.0001), (2.0, 0.0001), (3.0, 0.0001), (4.0, -0.0002)]),
            ("hostile", [(1.0, 0.0005), (2.0, 0.0015), (3.0, 0.0015), (4.0, -0.0027)]),
        ]
        bounds = [{"y": 0.01, "label": "fair-share bound 1.0%"}]
        low, high = MANDATE.bar_axis_extent(
            series, bounds, None, MANDATE.bar_plot_height(2)
        )
        span = high - low
        # The rule the fix replaced, stated as a vacuity: it keyed the decimals
        # off the sign of the lower edge, so a panel spanning 1.3 % of the unit
        # around zero got two of them and collided.
        old = [MANDATE.tick_label(low + span * tick / 5, 2) for tick in range(6)]
        self.assertLess(len(set(old)), 6, old)
        repeated = "".join(
            f'<text x="63" y="0" text-anchor="end">{tick}</text>' for tick in old
        )
        problems = MANDATE.check_tick_labels_distinct("imbalance", repeated)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("repeat", problems[0])
        self.assertIn("cannot carry the quantity", problems[0])
        # The fixed rule, and the panel it renders, are both readable.
        declaration = {
            "mandate": "M4",
            "title": "M4 interactive lane fairness",
            "x_label": "flow (1..4)",
            "y_label": "departure from the fair share",
            "panels": [
                {
                    "id": "imbalance",
                    "chart": "bar",
                    "series": [{"name": "clean"}, {"name": "hostile"}],
                    "bounds": bounds,
                }
            ],
        }
        rows = [["panel", "series", "x", "y"]] + [
            ["imbalance", name, x, y] for name, points in series for x, y in points
        ]
        code, stderr, out = self.render_mandate(declaration, rows, "M4imb")
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        ticks = MANDATE.axis_tick_labels(document)
        self.assertEqual(len(set(ticks)), len(ticks), ticks)
        self.assertEqual(MANDATE.check_tick_labels_distinct("imbalance", document), [])

    # -- the honesty of a line series' geometry, and its stated readings -----

    def test_a_hole_in_the_sampling_is_drawn_as_a_gap_not_a_wall(self):
        # The series that caused the misreading: the recorded `lone_tail` tail,
        # which steps from 13.11 s (1.8 ms) to 15.76 s (2651.7 ms) -- a 2.65 s
        # period nobody observed. Drawn as one polyline it is a near-vertical
        # wall, and a reader took it for a climb the window's end truncated.
        declaration = {**LONE_TAIL_DECLARATION, "panels": [dict(LONE_TAIL_DECLARATION["panels"][0], bounds=[{"y": 250.0, "label": "M1 ceiling 250 ms", "series": "lone_tail"}])]}
        code, stderr, out = self.render_mandate(
            declaration, _latency_rows(REAL_LONE_TAIL_TAIL), "M1hole"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-latency.svg").read_text(encoding="utf-8")
        series = [("lone_tail", list(REAL_LONE_TAIL_TAIL))]
        self.assertEqual(MANDATE.check_gap_honesty("latency", series, document), [])
        # Every drawn sample is a dot, so where the samples *are* (and are not)
        # is on the panel rather than inferred from the line's steepness...
        self.assertEqual(
            document.count('<circle class="sample"'), len(REAL_LONE_TAIL_TAIL)
        )
        # ...and the line is two segments, not one drawn across the hole.
        colours = MANDATE.drawn_polylines(document)
        self.assertEqual(colours[MANDATE.REPORT.COLORS[0]], 2)
        # The pre-change drawing is refused, naming the hole it paints as a
        # climb: this is the red half of the vacuity pair.
        continuous = MANDATE.REPORT.svg_line_chart(
            "M1 [latency]", "elapsed time (s)", "latency (ms)", series
        )
        problems = MANDATE.check_gap_honesty("latency", series, continuous)
        self.assertEqual(len(problems), 2, problems)
        joined = "\n".join(problems)
        self.assertIn("2.65 s hole", joined)
        self.assertIn("near-vertical climb", joined)
        self.assertIn("sample marker(s)", joined)

    def test_a_censored_reading_cannot_be_left_off_the_panel(self):
        # A climb the window cut off -- the shape the eye cannot tell from a
        # peak that returned, and the shape the run's own detector classified.
        reading = {
            "lone_tail": {
                "verdict": "Censored",
                "rungs_at_edge": 1.0,
                "room": 1200.0,
            }
        }
        rows = _latency_rows(TRUNCATED_CLIMB)
        code, stderr, out = self.render_mandate(
            LONE_TAIL_DECLARATION,
            rows,
            "M1cens",
            "--run-censoring",
            json.dumps(reading),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-latency.svg").read_text(encoding="utf-8")
        series = [("lone_tail", list(TRUNCATED_CLIMB))]
        readings = MANDATE.panel_readings(series, reading)
        self.assertEqual(
            MANDATE.check_readings_stated("latency", series, readings, document), []
        )
        # The panel states the verdict, the room, and the fact that makes the
        # difference: nothing follows the maximum.
        self.assertIn("lone_tail: Censored", document)
        self.assertIn("room 1200 ms", document)
        self.assertIn("nothing after it", document)
        # The red half: the same panel drawn without the run's reading is
        # refused by name, verdict and all.
        silent = MANDATE.REPORT.svg_line_chart(
            "M1 [latency]",
            "elapsed time (s)",
            "latency (ms)",
            series,
        )
        problems = MANDATE.check_readings_stated("latency", series, readings, silent)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Censored", problems[0])
        self.assertIn("the opposite conclusion", problems[0])
        # And end to end: a reading about an arm no line panel draws cannot be
        # carried by any panel, so the whole render is refused rather than
        # silently dropping a machine verdict.
        stray = {"clean": {"verdict": "Clear", "rungs_at_edge": -1.0, "room": 2000.0}}
        code, stderr, _ = self.render_mandate(
            LONE_TAIL_DECLARATION,
            rows,
            "M1stray",
            "--run-censoring",
            json.dumps(stray),
        )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("about no series any line panel", stderr)

    def test_a_caption_whose_numbers_come_from_another_source_is_refused(self):
        # The defect this replaces: `check_readings_stated` reads the drawn
        # sentence back and requires it to be the one the formatter produced,
        # which is green on a caption whose *magnitude* was taken from
        # somewhere else -- the producer's own `[m1-censoring] max=` token is
        # the candidate this loop checked first -- because the formatter is the
        # only thing either side of that comparison ever consulted. The caption
        # is what the reader trusts instead of the pixels, so a caption that is
        # authoritative and wrong is worse than no caption.
        series = [("lone_tail", list(REAL_LONE_TAIL_TAIL))]
        detector = {"verdict": "Clear", "rungs_at_edge": -2.43, "room": 105000.0}
        drawn = MANDATE.arm_reading(
            "lone_tail", MANDATE.REPORT.decimate(series[0][1]), detector
        )
        markup = _reading_markup([drawn])
        self.assertEqual(
            MANDATE.check_readings_stated(
                "latency", series, [("lone_tail", drawn)], markup
            ),
            [],
        )
        self.assertEqual(MANDATE.check_reading_numbers("latency", series, markup), [])
        # The red half: the same sentence with the magnitude the instrument's
        # own row states (the detector's `max=`, which on the recorded run was
        # a *different* series' maximum). `check_readings_stated` is still
        # green on it -- that is the vacuity the new check closes -- and the
        # numbers check refuses by name, arm and both values.
        wrong = drawn.replace("peak 2652 ms", "peak 1074.1 ms")
        self.assertNotEqual(wrong, drawn)
        broken = _reading_markup([wrong])
        self.assertEqual(
            MANDATE.check_readings_stated(
                "latency", series, [("lone_tail", wrong)], broken
            ),
            [],
        )
        problems = MANDATE.check_reading_numbers("latency", series, broken)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'lone_tail'", problems[0])
        self.assertIn("1074.1", problems[0])
        self.assertIn("2651.69", problems[0])
        self.assertIn("worse than no caption", problems[0])
        # The other numbers are pinned too: a peak time, a last sample and a
        # hole count from a different series are each caught, and a caption
        # that states no maximum at all cannot be read back and is refused
        # rather than skipped.
        for broken_text, fragment in (
            (drawn.replace("at 15.76 s", "at 9.56 s"), "where its maximum is"),
            (drawn.replace("last 59.8 ms", "last 424.3 ms"), "its last sample"),
            (drawn.replace("1 sample gap(s)", "2 sample gap(s)"), "sample gap(s)"),
            (drawn.replace("peak 2652 ms at 15.76 s", "the series is quiet"), "states no maximum"),
        ):
            with self.subTest(caption=broken_text):
                rows = MANDATE.check_reading_numbers(
                    "latency", series, _reading_markup([broken_text])
                )
                self.assertTrue(rows, broken_text)
                self.assertIn(fragment, "\n".join(rows))

    def test_a_caption_taken_from_another_arm_is_refused(self):
        # The other shape the finding named: the arm may be selected
        # differently in the two paths, so one arm's sentence can be drawn
        # beside another arm's series. The band is split per arm by the arm's
        # own `<arm>: ` marker rather than by position, so a reading is matched
        # to the series it names -- and a sentence that names the wrong arm is
        # measured against the wrong points and refused.
        series = [
            ("first", [(float(index), 10.0 * index) for index in range(1, 8)]),
            ("second", [(float(index), 100.0 * index) for index in range(1, 8)]),
        ]
        first = MANDATE.arm_reading("first", series[0][1])
        second = MANDATE.arm_reading("second", series[1][1])
        # The run read one arm and not the other: both sentence shapes --
        # `<arm>: <verdict> - ...` and `<arm> - ...` -- are in the band, and
        # the split is by the arm's own marker rather than by draw position.
        verdicts = {"first": {"verdict": "Censored", "room": 40.0}}
        read = [
            ("first", MANDATE.arm_reading("first", series[0][1], verdicts["first"])),
            ("second", second),
        ]
        for sentences in ([first, second], [second, first], [text for _, text in read]):
            with self.subTest(band=[text.split(" ")[0] for text in sentences]):
                self.assertEqual(
                    MANDATE.check_reading_numbers(
                        "latency", series, _reading_markup(sentences)
                    ),
                    [],
                )
        mislabelled = _reading_markup([second.replace("second - ", "first - ", 1)])
        problems = MANDATE.check_reading_numbers("latency", series, mislabelled)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'first'", problems[0])
        self.assertIn("states its maximum as 700", problems[0])
        self.assertIn("is 70", problems[0])

    def test_a_render_whose_caption_states_another_series_maximum_is_refused(self):
        # End to end: a formatter that takes its magnitude from the detector's
        # own `max=` token instead of from the drawn points is refused by the
        # render, not merely by the predicate -- the artifact never reaches
        # disk carrying a number its own series contradicts.
        detector = {"verdict": "Clear", "rungs_at_edge": -2.43, "room": 105000.0}
        rows = _latency_rows(REAL_LONE_TAIL_TAIL)
        censoring = json.dumps({"lone_tail": {**detector, "max": 1074.1}})
        code, stderr, _ = self.render_mandate(
            LONE_TAIL_DECLARATION, rows, "M1lie", "--run-censoring", censoring
        )
        self.assertEqual(code, 0, stderr)
        honest = MANDATE.arm_reading

        def lying_formatter(arm, points, reading=None):
            text = honest(arm, points, reading)
            return MANDATE.re.sub(
                r"peak [-+0-9.eE]+ ms",
                f"peak {(reading or detector)['max']:.1f} ms",
                text,
            )

        with mock.patch.object(MANDATE, "arm_reading", lying_formatter):
            code, stderr, _ = self.render_mandate(
                LONE_TAIL_DECLARATION, rows, "M1lie", "--run-censoring", censoring
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("states its maximum as 1074.1", stderr)
        self.assertIn("does not measure", stderr)

    def test_a_share_panel_names_the_panel_that_carries_its_departure(self):
        # `M4-shares` draws a share against the fair share, and the mandate's
        # failure is the *departure* from it: a bound its own bars straddle is
        # a reference, not a line any of them can fail, so the frame itself has
        # no failure in it to draw. The panel therefore says what it is, names
        # the panel that carries the departure, and states the run's own worst
        # departure per arm and the bound it is read against -- all of it
        # derived from the companion panel's own drawn points.
        declaration = SHARES_IMBALANCE_DECLARATION
        panels = declaration["panels"]
        points = _points(SHARES_IMBALANCE_ROWS)
        code, stderr, out = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4depart"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        self.assertEqual(
            " ".join(MANDATE.drawn_notes(document)),
            "composition view - the departure is drawn on panel 'imbalance' "
            "(bound 1.0%): worst clean 0.04%, hostile 0.03%",
        )
        self.assertEqual(
            MANDATE.check_departure_view_stated(
                "shares", panels[0], panels, points, document
            ),
            [],
        )
        # The note is inside the plot and clear of the bound label, which is
        # where a note about the frame has to live to be read.
        self.assertEqual(MANDATE.check_note_fit("shares", document), [])
        self.assertEqual(MANDATE.check_label_fit("shares", document), [])
        self.assertEqual(MANDATE.check_label_overlap("shares", document), [])

    def test_a_share_panel_without_its_departure_statement_is_refused(self):
        # The vacuity of the departure-view check: the same declaration and the
        # same data, drawn without the note, must go red -- and end to end, a
        # render that suppresses the note must not write the panel at all.
        declaration = SHARES_IMBALANCE_DECLARATION
        panels = declaration["panels"]
        points = _points(SHARES_IMBALANCE_ROWS)
        bare = MANDATE.svg_bar_chart(
            "M4 [shares]",
            "flow (1..4)",
            "share of the lane's delivered bytes",
            [("clean", [(1.0, 0.250029)]), ("hostile", [(1.0, 0.250029)])],
            [{"y": 0.25, "label": "fair share 25.0%"}],
        )
        # The pre-change drawing: no note at all, and the check names the panel
        # whose departure it is silent about.
        self.assertEqual(MANDATE.drawn_notes(bare), [])
        problems = MANDATE.check_departure_view_stated(
            "shares", panels[0], panels, points, bare
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot show the departure", problems[0])
        self.assertIn("'imbalance'", problems[0])
        self.assertIn("no departure to draw", problems[0])
        # The other red half: a note whose numbers came from somewhere else is
        # not the note this panel's own data derives, so it is refused too.
        code, stderr, out = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4depart2"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        lying = document.replace("clean 0.04%", "clean 4.00%")
        self.assertNotEqual(lying, document)
        problems = MANDATE.check_departure_view_stated(
            "shares", panels[0], panels, points, lying
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Expected on the panel", problems[0])
        # End to end: a drawing that drops the note does not write the panel,
        # rather than writing one that reads as "no departure".
        drawn_chart = MANDATE.svg_bar_chart

        def chart_without_its_note(*arguments, **keywords):
            return drawn_chart(*arguments, **{**keywords, "note": ""})

        with mock.patch.object(MANDATE, "svg_bar_chart", chart_without_its_note):
            code, stderr, _ = self.render_mandate(
                declaration, SHARES_IMBALANCE_ROWS, "M4depart3"
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("cannot show the departure", stderr)

    def test_a_panel_whose_bound_a_bar_can_fail_owes_no_note(self):
        # The check is about a bound the bars *straddle*, not about every bar
        # panel: a floor is a line a bar can fail, so that panel can already
        # show its own failure and is left alone -- as is a share panel whose
        # mandate declares no panel carrying the departure for it to name.
        delivery = {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "clean"}],
            "bounds": [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}],
        }
        shares = {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}],
            "bounds": [{"y": 0.25, "label": "fair share 25.0%"}],
        }
        points = MANDATE.parse_points(
            [
                (2, "delivery", "clean", "1.0", "1.0"),
                (3, "shares", "clean", "1.0", "0.250029"),
                (4, "shares", "clean", "2.0", "0.249912"),
            ]
        )
        self.assertEqual(MANDATE.target_bounds(delivery, [("clean", [(1.0, 1.0)])]), [])
        self.assertEqual(
            len(
                MANDATE.target_bounds(
                    shares, [("clean", [(1.0, 0.250029), (2.0, 0.249912)])]
                )
            ),
            1,
        )
        for panel in (delivery, shares):
            with self.subTest(panel=panel["id"]):
                self.assertEqual(
                    MANDATE.departure_view_note(panel, [delivery, shares], points), ""
                )

    def test_a_reading_band_that_eats_the_plot_is_refused(self):
        # The band and the shape it explains share one canvas, so the band may
        # not eat the shape: three arms of readings leave most of the plot,
        # and a band that leaves too little is refused rather than drawn.
        self.assertEqual(
            MANDATE.check_reading_band("latency", MANDATE.REPORT.line_plot_height(3, 6)),
            [],
        )
        problems = MANDATE.check_reading_band(
            "latency", MANDATE.REPORT.line_plot_height(3, 20)
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("shape its readings are about", problems[0])


if __name__ == "__main__":
    unittest.main()
