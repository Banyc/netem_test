#!/usr/bin/env python3
"""Tests for tools/shape_report.py."""

import csv
import importlib.util
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("shape_report.py")

# The tool names its ridgeline inputs explicitly. The on-disk convention is
# "<scenario>__<arm>_.csv" (double underscore before the arm), so these names
# are pinned here to catch one drifting away from the files the tool reads.
RIDGELINE_NAMES = [
    "dyn_single_mux__A_.csv",
    "dyn_dual_auto_small_first__B_.csv",
    "dyn_dual_auto_small_first_migrating__B_mig_.csv",
    "dyn_dual_auto_big_first__C_.csv",
    "dyn_dual_auto_big_first_migrating__C_mig_.csv",
    "dyn_dual_hint_static__E_.csv",
]

PRIMARY_CSV = "rtp_mux_response_migration.csv"


def load_tool(dist_dir, out_path):
    """Execute shape_report.py with a controlled argv and return the module.

    The tool runs argparse, loads data and writes its page at import time, so
    every scenario gets a fresh module object rather than a cached import.
    """
    spec = importlib.util.spec_from_file_location("shape_report_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    argv = ["shape_report.py", "--dist-dir", str(dist_dir), "--out", str(out_path)]
    with mock.patch.object(sys, "argv", argv):
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)
    return module


_EMPTY_TMP = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
_EMPTY_TOOL = None


def empty_tool():
    """A tool instance loaded against a directory with no CSVs."""
    global _EMPTY_TOOL
    if _EMPTY_TOOL is None:
        root = Path(_EMPTY_TMP.name)
        _EMPTY_TOOL = load_tool(root / "dist", root / "report.html")
    return _EMPTY_TOOL


class ShapeReportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.out = self.root / "report.html"

    def write_csv(self, name, rows, header=("series", "value")):
        path = self.dist / name
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def write_text(self, name, text):
        path = self.dist / name
        path.write_text(text)
        return path

    def tool(self, out=None):
        return load_tool(self.dist, out or self.out)

    def html(self, out=None):
        return (out or self.out).read_text()


# ── CSV loading ───────────────────────────────────────────────────────────────


class LoadTest(ShapeReportTestCase):
    def test_parses_sorts_and_preserves_series_order(self):
        self.write_csv(
            "data.csv",
            [("b", "3"), ("a", "9"), ("b", "1"), ("a", "2"), ("b", "2.5")],
        )
        loaded = self.tool().load("data.csv")
        self.assertEqual(list(loaded.keys()), ["b", "a"])
        self.assertEqual(loaded["b"], [1.0, 2.5, 3.0])
        self.assertEqual(loaded["a"], [2.0, 9.0])

    def test_missing_file_returns_empty_mapping(self):
        self.assertEqual(self.tool().load("absent.csv"), {})

    def test_empty_header_only_and_blank_line_files_return_empty(self):
        self.write_text("empty.csv", "")
        self.write_text("header.csv", "series,value\n")
        self.write_text("blank.csv", "series,value\n\n")
        tool = self.tool()
        self.assertEqual(tool.load("empty.csv"), {})
        self.assertEqual(tool.load("header.csv"), {})
        self.assertEqual(tool.load("blank.csv"), {})

    def test_extra_columns_are_ignored(self):
        self.write_csv(
            "extra.csv",
            [("s", "1", "ignored"), ("s", "2", "ignored")],
            header=("series", "value", "note"),
        )
        self.assertEqual(self.tool().load("extra.csv")["s"], [1.0, 2.0])

    def test_non_numeric_value_raises_value_error(self):
        # The loader performs no row validation: a malformed value fails loudly.
        self.write_text("bad.csv", "series,value\ns,abc\n")
        with self.assertRaises(ValueError):
            self.tool().load("bad.csv")

    def test_missing_value_field_raises_type_error(self):
        self.write_text("short.csv", "series,value\ns\n")
        with self.assertRaises(TypeError):
            self.tool().load("short.csv")

    def test_unknown_header_raises_key_error(self):
        self.write_text("hdr.csv", "foo,bar\ns,1\n")
        with self.assertRaises(KeyError):
            self.tool().load("hdr.csv")


# ── Aggregation helpers ───────────────────────────────────────────────────────


class QuantileTest(unittest.TestCase):
    def test_empty_samples_return_zero(self):
        self.assertEqual(empty_tool().quantile([], 0.5), 0.0)

    def test_single_sample_returns_that_sample_for_any_p(self):
        for p in (0.0, 0.25, 0.5, 0.99, 1.0):
            self.assertEqual(empty_tool().quantile([7.5], p), 7.5)

    def test_linear_interpolation_between_two_samples(self):
        tool = empty_tool()
        self.assertEqual(tool.quantile([0.0, 10.0], 0.0), 0.0)
        self.assertEqual(tool.quantile([0.0, 10.0], 0.25), 2.5)
        self.assertEqual(tool.quantile([0.0, 10.0], 0.5), 5.0)
        self.assertEqual(tool.quantile([0.0, 10.0], 1.0), 10.0)

    def test_p_spans_the_n_minus_one_index_range(self):
        tool = empty_tool()
        samples = [0.0, 10.0, 20.0]
        self.assertEqual(tool.quantile(samples, 0.5), 10.0)
        self.assertAlmostEqual(tool.quantile(samples, 0.75), 15.0)

    def test_quartiles_of_four_samples(self):
        tool = empty_tool()
        samples = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(tool.quantile(samples, 0.0), 1.0)
        self.assertAlmostEqual(tool.quantile(samples, 0.5), 2.5)
        self.assertAlmostEqual(tool.quantile(samples, 0.9), 3.7)
        self.assertAlmostEqual(tool.quantile(samples, 0.99), 3.97)


class KdeLogTest(unittest.TestCase):
    def test_grid_endpoints_and_size(self):
        grid, density = empty_tool().kde_log([1.0, 10.0, 100.0])
        self.assertEqual(len(grid), 200)
        self.assertEqual(len(density), 200)
        self.assertEqual(grid[0], -1.0)
        self.assertEqual(grid[-1], 2.5)

    def test_density_integrates_to_one(self):
        samples = [10.0 ** (1.0 + 0.4 * ((i % 9) - 4) / 4) for i in range(200)]
        grid, density = empty_tool().kde_log(samples)
        step = grid[1] - grid[0]
        self.assertAlmostEqual(sum(density) * step, 1.0, places=6)

    def test_density_is_nonnegative(self):
        _, density = empty_tool().kde_log([1.0, 5.0, 50.0])
        self.assertTrue(all(d >= 0.0 for d in density))

    def test_equal_samples_use_the_bandwidth_floor(self):
        # Equal log-samples make stdev 0, so h falls back to the 0.01 floor;
        # the peak density is then near 1/(sqrt(2*pi)*0.01), not the ~4 that a
        # larger floor would give.
        grid, density = empty_tool().kde_log([10.0, 10.0, 10.0])
        floor_peak = 1.0 / (math.sqrt(2 * math.pi) * 0.01)
        peak = max(density)
        self.assertAlmostEqual(peak, floor_peak, delta=0.15 * floor_peak)
        self.assertAlmostEqual(grid[density.index(peak)], 1.0, delta=0.02)

    def test_samples_below_the_clamp_are_logged_without_error(self):
        grid, density = empty_tool().kde_log([0.0, 1.0])
        self.assertEqual(len(grid), 200)
        self.assertTrue(any(d > 0.0 for d in density))

    def test_zero_sample_is_clamped_to_one_microsecond(self):
        # A 0 ms sample is clamped to 0.001 ms before the log, i.e. log10 = -3.
        # At the leftmost grid point (-1) with the n=1 bandwidth 1.06 the
        # density is exp(-0.5*(2/1.06)**2)/(sqrt(2*pi)*1.06); a 0.01 clamp
        # would place the sample at -2 and roughly quadruple this value.
        _, density = empty_tool().kde_log([0.0])
        expected = math.exp(-0.5 * (2.0 / 1.06) ** 2) / (math.sqrt(2 * math.pi) * 1.06)
        self.assertAlmostEqual(density[0], expected, places=6)


class EcdfTest(unittest.TestCase):
    def test_pairs_x_with_i_over_n(self):
        xs, ys = empty_tool().ecdf([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(xs, [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(ys, [0.0, 0.25, 0.5, 0.75])

    def test_empty_input_returns_empty_pairs(self):
        self.assertEqual(empty_tool().ecdf([]), ([], []))

    def test_repeated_samples_keep_their_own_step(self):
        xs, ys = empty_tool().ecdf([5.0, 5.0, 9.0])
        self.assertEqual(xs, [5.0, 5.0, 9.0])
        self.assertAlmostEqual(ys[0], 0.0)
        self.assertAlmostEqual(ys[1], 1.0 / 3.0)
        self.assertAlmostEqual(ys[2], 2.0 / 3.0)


class ShiftFunctionTest(unittest.TestCase):
    def test_percentile_grid_is_one_to_ninety_nine(self):
        ps, _, _, _ = empty_tool().shift_function([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
        self.assertEqual(len(ps), 99)
        self.assertEqual(ps[0], 0.01)
        self.assertEqual(ps[-1], 0.99)

    def test_shift_is_quantile_b_minus_quantile_a(self):
        _, shifts, _, _ = empty_tool().shift_function(
            [1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 6.0, 8.0, 10.0]
        )
        self.assertAlmostEqual(shifts[0], 1.04)
        self.assertAlmostEqual(shifts[-1], 4.96)

    def test_shift_sign_follows_b_minus_a(self):
        _, shifts, _, _ = empty_tool().shift_function([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
        self.assertTrue(all(shift > 0.0 for shift in shifts))

    def test_bootstrap_band_endpoints_are_the_seeded_percentiles(self):
        ps, _, ci_low, ci_high = empty_tool().shift_function(
            [1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 6.0, 8.0, 10.0]
        )
        self.assertEqual(len(ci_low), len(ps))
        self.assertEqual(len(ci_high), len(ps))
        self.assertTrue(all(low <= high for low, high in zip(ci_low, ci_high)))
        self.assertAlmostEqual(ci_low[0], -1.0)
        self.assertAlmostEqual(ci_high[0], 5.0)
        self.assertAlmostEqual(ci_low[-1], 1.0)
        self.assertAlmostEqual(ci_high[-1], 6.92)

    def test_is_deterministic_for_a_fixed_seed(self):
        first = empty_tool().shift_function([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0])
        second = empty_tool().shift_function([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0])
        self.assertEqual(first, second)

    def test_bootstrap_uses_four_hundred_resamples(self):
        a = [
            10.85, 23.999, 24.029, 42.039, 52.836, 58.231,
            71.229, 73.922, 80.905, 82.659, 87.446, 88.668,
        ]
        b = [
            5.112, 15.877, 17.25, 19.397, 22.422, 52.306,
            55.953, 59.272, 80.388, 83.388, 95.908, 98.346,
        ]
        _, _, _, ci_high = empty_tool().shift_function(a, b)
        # Percentile 88's upper band edge changes if the bootstrap does one
        # fewer resample (399 gives 18.48284), so this pins n_resample=400.
        self.assertAlmostEqual(ci_high[87], 18.84136000000001)


class SummaryTableTest(unittest.TestCase):
    def test_headers_are_fixed(self):
        headers, _ = empty_tool().summary_table({"s": [1.0]})
        self.assertEqual(headers, ["Series", "n", "Mean", "p50", "p90", "p99", "Max"])

    def test_row_values_are_n_mean_p50_p90_p99_max(self):
        _, rows = empty_tool().summary_table({"s": [1.0, 2.0, 3.0, 4.0]})
        self.assertEqual(rows, [("s", 4, 2.5, 2.5, 3.7, 3.9699999999999998, 4.0)])

    def test_empty_map_has_headers_and_no_rows(self):
        headers, rows = empty_tool().summary_table({})
        self.assertEqual(rows, [])
        self.assertEqual(len(headers), 7)

    def test_row_order_follows_mapping_order(self):
        _, rows = empty_tool().summary_table({"b": [1.0], "a": [2.0]})
        self.assertEqual([row[0] for row in rows], ["b", "a"])

    def test_max_is_the_largest_sample(self):
        _, rows = empty_tool().summary_table({"s": [1.0, 3.0, 5.0]})
        self.assertEqual(rows[0][-1], 5.0)


# ── SVG primitives ────────────────────────────────────────────────────────────


class SvgAxisTest(unittest.TestCase):
    def test_linear_scale_maps_bounds_to_plot_corners(self):
        _, sx, sy = empty_tool().svg_axis(0.0, 10.0, 0.0, 1.0, 600, 350, 55)
        self.assertEqual(sx(0.0), 55.0)
        self.assertEqual(sx(5.0), 300.0)
        self.assertEqual(sx(10.0), 545.0)
        self.assertEqual(sy(0.0), 295.0)
        self.assertEqual(sy(1.0), 55.0)

    def test_linear_axis_emits_six_x_ticks_and_six_y_ticks(self):
        axis, _, _ = empty_tool().svg_axis(0.0, 10.0, 0.0, 1.0, 600, 350, 55)
        self.assertEqual(axis.count('y2="299"'), 6)
        self.assertEqual(axis.count('x1="51"'), 6)
        for label in ("0.0", "2", "4", "6", "8", "10"):
            self.assertIn(">{}<".format(label), axis)

    def test_log_scale_maps_decades_proportionally(self):
        _, sx, _ = empty_tool().svg_axis(1.0, 100.0, 0.0, 1.0, 600, 350, 55, log_x=True)
        self.assertEqual(sx(1.0), 55.0)
        self.assertEqual(sx(10.0), 300.0)
        self.assertEqual(sx(100.0), 545.0)

    def test_log_axis_ticks_are_restricted_to_the_range(self):
        axis, _, _ = empty_tool().svg_axis(1.0, 100.0, 0.0, 1.0, 600, 350, 55, log_x=True)
        for label in ("1", "2", "5", "10", "20", "50", "100"):
            self.assertIn(">{}<".format(label), axis)
        self.assertNotIn(">0.5<", axis)
        self.assertNotIn(">200<", axis)

    def test_sub_unit_log_ticks_use_one_decimal_place(self):
        axis, _, _ = empty_tool().svg_axis(0.1, 1.0, 0.0, 1.0, 600, 350, 55, log_x=True)
        for label in ("0.1", "0.2", "0.5", "1"):
            self.assertIn(">{}<".format(label), axis)

    def test_y_axis_labels_use_two_decimals(self):
        axis, _, _ = empty_tool().svg_axis(0.0, 1.0, 0.0, 1.0, 600, 350, 55)
        for label in ("0.00", "0.20", "0.40", "0.60", "0.80", "1.00"):
            self.assertIn(">{}<".format(label), axis)

    def test_axis_labels_are_emitted_when_given(self):
        axis, _, _ = empty_tool().svg_axis(
            0.0, 1.0, 0.0, 1.0, 600, 350, 55, x_label="RTT (ms)", y_label="Density"
        )
        self.assertIn(">RTT (ms)<", axis)
        self.assertIn(">Density<", axis)

    def test_degenerate_linear_range_maps_to_the_centre(self):
        _, sx, _ = empty_tool().svg_axis(5.0, 5.0, 0.0, 1.0, 600, 350, 55)
        self.assertEqual(sx(5.0), 300.0)

    def test_degenerate_log_range_maps_to_the_centre(self):
        _, sx, _ = empty_tool().svg_axis(5.0, 5.0, 0.0, 1.0, 600, 350, 55, log_x=True)
        self.assertEqual(sx(5.0), 300.0)


class PolylineFillTest(unittest.TestCase):
    def test_polyline_scales_points_and_sets_default_width(self):
        got = empty_tool().polyline(
            [(0.0, 1.0), (10.0, 2.0)], lambda x: x * 2, lambda y: y * 3, "#abc"
        )
        self.assertEqual(
            got,
            '<polyline points="0.0,3.0 20.0,6.0" fill="none" stroke="#abc" stroke-width="1.5"/>',
        )

    def test_polyline_adds_dash_array_only_when_requested(self):
        solid = empty_tool().polyline([(0.0, 0.0)], lambda x: x, lambda y: y, "#abc")
        dashed = empty_tool().polyline(
            [(0.0, 0.0)], lambda x: x, lambda y: y, "#abc", dash="4,4"
        )
        self.assertNotIn("stroke-dasharray", solid)
        self.assertIn('stroke-dasharray="4,4"', dashed)

    def test_fill_between_closes_low_forward_and_high_backward(self):
        got = empty_tool().fill_between(
            [0.0, 1.0, 2.0], [0.0, 0.0, 0.0], [1.0, 2.0, 3.0], lambda x: x, lambda y: y, "#888", 0.2
        )
        self.assertEqual(
            got,
            '<polygon points="0.0,0.0 1.0,0.0 2.0,0.0 2.0,3.0 1.0,2.0 0.0,1.0" '
            'fill="#888" opacity="0.2"/>',
        )

    def test_fill_between_pairs_each_high_value_with_its_own_x(self):
        # The high edge is traversed in reverse x order *with* its own y, so
        # the polygon closes (2, 30), (1, 20), (0, 10) rather than the mirrored
        # (2, 10), (1, 20), (0, 30).
        got = empty_tool().fill_between(
            [0.0, 1.0, 2.0], [0.0, 0.0, 0.0], [10.0, 20.0, 30.0], lambda x: x, lambda y: y, "#888"
        )
        self.assertEqual(
            got,
            '<polygon points="0.0,0.0 1.0,0.0 2.0,0.0 2.0,30.0 1.0,20.0 0.0,10.0" '
            'fill="#888" opacity="0.15"/>',
        )


# ── Assembled report ──────────────────────────────────────────────────────────


class ReportContentTest(ShapeReportTestCase):
    def write_two_series(self):
        rows = [("pinned", str(v)) for v in range(1, 41)]
        rows += [("migrating", str(100 + v)) for v in range(1, 41)]
        self.write_csv(PRIMARY_CSV, rows)

    def test_two_series_produce_three_figures_and_a_summary(self):
        self.write_two_series()
        self.tool()
        html = self.html()
        self.assertIn("<h3>Figure 1: Density (log scale)</h3>", html)
        self.assertIn("<h3>Figure 2: Empirical CDF</h3>", html)
        self.assertIn("<h3>Figure 3: Shift function (migrating \u2212 pinned)</h3>", html)
        self.assertIn(">pinned<", html)
        self.assertIn(">migrating<", html)

    def test_shift_figure_is_drawn_inside_the_canvas(self):
        self.write_two_series()
        self.tool()
        html = self.html()
        match = re.search(r'<polyline points="([^"]+)" fill="none" stroke="#e67e22"', html)
        self.assertIsNotNone(match)
        xs = [float(pair.split(",")[0]) for pair in match.group(1).split()]
        self.assertGreaterEqual(min(xs), 55.0)
        self.assertLessEqual(max(xs), 545.0)
        band = re.search(r'<polygon points="([^"]+)" fill="#888"', html)
        self.assertIsNotNone(band)
        band_xs = [float(pair.split(",")[0]) for pair in band.group(1).split()]
        self.assertGreaterEqual(min(band_xs), 55.0)
        self.assertLessEqual(max(band_xs), 545.0)

    def test_summary_table_formats_mean_and_counts(self):
        self.write_two_series()
        self.tool()
        html = self.html()
        self.assertIn("<td>pinned</td><td>40</td><td>20.50</td>", html)
        self.assertIn("<td>migrating</td><td>40</td><td>120.50</td>", html)

    def test_single_series_omits_the_shift_figure(self):
        self.write_csv(PRIMARY_CSV, [("only", "1"), ("only", "2")])
        self.tool()
        html = self.html()
        self.assertIn("Figure 1", html)
        self.assertNotIn("Figure 3", html)

    def test_three_series_omit_the_shift_figure(self):
        rows = [("a", "1"), ("a", "2"), ("b", "3"), ("b", "4"), ("c", "5"), ("c", "6")]
        self.write_csv(PRIMARY_CSV, rows)
        self.tool()
        self.assertNotIn("Figure 3", self.html())

    def test_no_data_still_writes_headers_and_footer(self):
        self.tool()
        html = self.html()
        self.assertNotIn("<h3>Figure", html)
        self.assertIn("Summary table", html)
        self.assertIn("<th>Series</th>", html)
        self.assertIn("CSV directory:", html)

    def test_footer_names_the_dist_dir(self):
        self.tool()
        self.assertIn(str(self.dist), self.html())

    def test_known_series_use_their_palette_colours(self):
        rows = [("pinned", "1"), ("pinned", "2"), ("migrating", "3"), ("migrating", "4")]
        self.write_csv(PRIMARY_CSV, rows)
        self.tool()
        html = self.html()
        self.assertIn('stroke="#e74c3c"', html)
        self.assertIn('stroke="#2980b9"', html)

    def test_unknown_series_use_the_fallback_colour(self):
        self.write_csv(PRIMARY_CSV, [("mystery", "1"), ("mystery", "2")])
        self.tool()
        self.assertIn('stroke="#888"', self.html())

    def test_loaded_arguments_are_recorded_on_the_module(self):
        tool = self.tool()
        self.assertEqual(tool.args.dist_dir, str(self.dist))

    def test_series_minimum_below_the_axis_floor_is_clamped(self):
        # Figure 1 clamps its x floor to 0.1 ms, so a series containing 0.0
        # still places the 0.1 tick on the left edge of the plot.
        self.write_csv(PRIMARY_CSV, [("pinned", "0.0"), ("pinned", "1.0")])
        self.tool()
        self.assertIn(
            '<text x="55.0" y="309" text-anchor="middle" font-size="10" fill="currentColor">0.1</text>',
            self.html(),
        )

    def test_density_peak_label_sits_at_the_kde_peak(self):
        samples = [10.0, 10.0, 10.0, 100.0, 100.0, 100.0]
        self.write_csv(PRIMARY_CSV, [("pinned", str(v)) for v in samples])
        tool = self.tool()
        grid, density = tool.kde_log(samples)
        peak = density.index(max(density))
        _, sx, sy = tool.svg_axis(10.0, 100.0, 0, 1, 600, 350, 55, log_x=True)
        expected = (
            '<text x="{}" y="{}" text-anchor="middle" font-size="11" '
            'fill="#e74c3c">pinned</text>'
        ).format(sx(10 ** grid[peak]), sy(density[peak]) - 4)
        self.assertIn(expected, self.html())

    def test_ecdf_label_sits_at_the_last_sample(self):
        samples = [10.0, 20.0, 30.0]
        self.write_csv(PRIMARY_CSV, [("pinned", str(v)) for v in samples])
        tool = self.tool()
        xs, ys = tool.ecdf(samples)
        _, sx, sy = tool.svg_axis(10.0, 30.0, 0, 1, 600, 350, 55, log_x=True)
        expected = (
            '<text x="{}" y="{}" text-anchor="middle" font-size="11" '
            'fill="#e74c3c">pinned</text>'
        ).format(sx(xs[-1]), sy(ys[-1]) - 4)
        self.assertIn(expected, self.html())


class RidgelineTest(ShapeReportTestCase):
    def test_only_series_named_small_enter_the_ridgeline(self):
        self.write_csv(
            "dyn_single_mux__A_.csv",
            [("small", "10"), ("small", "20"), ("burst", "99"), ("burst", "100")],
        )
        self.tool()
        html = self.html()
        self.assertIn("Figure 4: Ridgeline", html)
        self.assertIn(">dyn_single_mux__A_<", html)
        self.assertIn("p50=15.0", html)
        self.assertNotIn(">burst<", html)

    def test_csv_without_a_small_series_is_skipped(self):
        self.write_csv("dyn_single_mux__A_.csv", [("burst", "10")])
        self.tool()
        self.assertNotIn("Figure 4", self.html())

    def test_small_match_is_case_insensitive(self):
        self.write_csv("dyn_single_mux__A_.csv", [("SMALL", "10"), ("SMALL", "20")])
        self.tool()
        self.assertIn("p50=15.0", self.html())

    def test_first_small_series_wins(self):
        self.write_csv(
            "dyn_single_mux__A_.csv",
            [("small_a", "10"), ("small_a", "10"), ("small_b", "100"), ("small_b", "100")],
        )
        self.tool()
        html = self.html()
        self.assertIn("p50=10.0", html)
        self.assertNotIn("p50=100.0", html)

    def test_ridgeline_peak_scales_to_sixty_percent_of_the_row(self):
        self.write_csv("dyn_single_mux__A_.csv", [("small", "10"), ("small", "20")])
        self.tool()
        match = re.search(r'<polygon points="([^"]+)" fill="#3498db"', self.html())
        self.assertIsNotNone(match)
        ys = [float(pair.split(",")[1]) for pair in match.group(1).split()]
        # One row: total_h = 80 + 55 and y_center = total_h - 55 = 80.
        self.assertAlmostEqual(max(ys) - 80.0, 80 * 0.6)

    def test_every_named_ridgeline_csv_is_loaded_when_present(self):
        for name in RIDGELINE_NAMES:
            self.write_csv(name, [("small", "10"), ("small", "20")])
        self.tool()
        html = self.html()
        for name in RIDGELINE_NAMES:
            self.assertIn(">{}<".format(name[:-4]), html)
        self.assertEqual(html.count("p50="), len(RIDGELINE_NAMES))

    def test_ridgeline_names_use_the_scenario_double_underscore_arm_convention(self):
        self.assertEqual(
            empty_tool().ridgeline_csvs,
            RIDGELINE_NAMES,
        )

    def test_ridgeline_rows_are_appended_after_the_primary_series(self):
        self.write_csv(PRIMARY_CSV, [("pinned", "1"), ("pinned", "2")])
        self.write_csv("dyn_single_mux__A_.csv", [("small", "10"), ("small", "20")])
        self.tool()
        html = self.html()
        self.assertLess(html.index("<td>pinned</td>"), html.index("<td>dyn_single_mux__A_</td>"))


# ── CLI ───────────────────────────────────────────────────────────────────────


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_cli(self, args, cwd=None):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_cli_writes_report_and_reports_absolute_path_and_size(self):
        dist = self.root / "dist"
        dist.mkdir()
        out = self.root / "report.html"
        proc = self.run_cli(["--dist-dir", str(dist), "--out", str(out)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())
        self.assertEqual(
            proc.stdout.strip(),
            "wrote {} {} bytes".format(os.path.abspath(out), out.stat().st_size),
        )

    def test_cli_defaults_write_shape_report_html_in_the_working_directory(self):
        proc = self.run_cli([])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        default = self.root / "shape_report.html"
        self.assertTrue(default.exists())
        self.assertIn("CSV directory: tests/target/netem-report", default.read_text())

    def test_cli_exits_zero_when_the_dist_dir_is_missing(self):
        proc = self.run_cli(
            ["--dist-dir", str(self.root / "nope"), "--out", str(self.root / "out.html")]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.root / "out.html").exists())


if __name__ == "__main__":
    unittest.main()
