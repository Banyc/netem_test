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
        # zero baseline and the data-driven extent they had.
        for name, declaration, rows, expected in (
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS, (0.0, 0.2503)),
            ("M3frac", FRACTION_DECLARATION, FRACTION_ROWS, (0.0, 0.9942)),
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
                self.assertAlmostEqual(ticks[0], expected[0], places=2)
                self.assertAlmostEqual(ticks[-1], expected[1], places=2)

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


if __name__ == "__main__":
    unittest.main()
