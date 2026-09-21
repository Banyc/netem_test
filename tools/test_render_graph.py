#!/usr/bin/env python3

import base64
import importlib.util
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("render_graph.py")
SPEC = importlib.util.spec_from_file_location("render_graph", MODULE_PATH)
RENDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDER)


# A healthy panel: plot background plus a two-point series polyline.
HEALTHY_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="850" height="230" class="plot-bg"/>'
    '<polyline points="60.0,200.0 900.0,120.0" fill="none" stroke="#1f77b4" stroke-width="1.7"/>'
    "</svg>"
)
# An empty chart: a real <svg> with axes/background but no series.
EMPTY_CHART_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="850" height="230" class="plot-bg"/>'
    '<line x1="60" y1="30" x2="60" y2="260" class="grid"/>'
    '<text x="480" y="295" text-anchor="middle">trace time (s)</text>'
    "</svg>"
)
# A polyline that draws nothing (one point).
DEGENERATE_POLYLINE_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<polyline points="60.0,200.0" fill="none" stroke="#1f77b4"/>'
    "</svg>"
)
# A geometry-less <rect/> paints nothing: this panel has an axis but no data.
GEOMETRYLESS_RECT_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    "<rect/>"
    '<line x1="60" y1="30" x2="60" y2="260" class="grid"/>'
    "</svg>"
)
# Zero-sized rects paint nothing either, whatever fill they carry.
ZERO_SIZED_RECT_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="0" height="230" fill="#2563eb"/>'
    '<rect x="60" y="30" width="850" height="0.0" fill="#2563eb"/>'
    "</svg>"
)
# A size a browser cannot parse is not drawable, and must not crash the tool.
MALFORMED_SIZE_RECT_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="wide" height="230" fill="#2563eb"/>'
    "</svg>"
)
# A rect without its own width paints nothing; a stroke-width attribute is not
# a geometry width and must not be mistaken for one.
STROKE_WIDTH_ONLY_RECT_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" height="230" stroke-width="3" fill="#2563eb"/>'
    "</svg>"
)
# A real bar, with the attribute form rtp_trace_report.svg_histogram emits.
REAL_BAR_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="850" height="230" class="plot-bg"/>'
    '<rect x="60.0" y="100.0" width="18.7" height="130.0" fill="#2563eb"/>'
    "</svg>"
)
# A histogram as the report generates it: a background, one maximum-height
# bar, and zero-height bars for empty buckets. The maximum-height bar makes
# the panel data-bearing even though the empty buckets draw nothing.
REAL_HISTOGRAM_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<rect x="60" y="30" width="850" height="230" class="plot-bg"/>'
    '<rect x="60.0" y="30.0" width="16.7" height="230.0" fill="#2563eb"/>'
    '<rect x="76.7" y="260.0" width="16.7" height="0.0" fill="#2563eb"/>'
    '<rect x="93.4" y="180.0" width="16.7" height="80.0" fill="#2563eb"/>'
    '<text x="480" y="295" text-anchor="middle">raw RTT (ms); 48 equal-width bins</text>'
    "</svg>"
)


# A panel truncated before its close tag (no </svg>).
UNTERMINATED_PANEL = (
    '<svg viewBox="0 0 960 300" role="img">'
    '<polyline points="60.0,200.0 900.0,120.0" fill="none" stroke="#1f77b4"/>'
)


def healthy_html(count=2):
    panels = "".join(f"<section><h2>panel {i}</h2>{HEALTHY_PANEL}</section>" for i in range(count))
    return f"<!doctype html><html><body>{panels}</body></html>"


# Minimal 1x1 PNG the fake browser writes to its --screenshot target.
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


class RenderGraphTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_html(self, text, name="comparison.html"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_browser(self, source):
        path = self.root / "fake-browser"
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def assertRaisesMessage(self, exception, fragment, callable_, *args, **kwargs):
        with self.assertRaises(exception) as context:
            callable_(*args, **kwargs)
        self.assertIn(fragment, str(context.exception))
        return str(context.exception)

    # -- extraction -------------------------------------------------------

    def test_extracts_every_panel_in_document_order(self):
        panels = RENDER.extract_svg_panels(healthy_html(3))
        self.assertEqual(len(panels), 3)

    def test_extracts_no_panel_from_graphless_html(self):
        self.assertEqual(RENDER.extract_svg_panels("<html><body>no charts</body></html>"), [])

    def test_extracts_an_unterminated_panel_for_validation(self):
        panels = RENDER.extract_svg_panels(f"<html><body>{UNTERMINATED_PANEL}</body></html>")
        self.assertEqual(len(panels), 1)

    def test_a_removed_middle_close_is_not_terminated_by_the_next_panel(self):
        # The middle panel loses its own close tag. The next panel's close
        # must not be allowed to terminate it, which would emit a panel that
        # is really two charts concatenated.
        broken = "<html><body>" + HEALTHY_PANEL + HEALTHY_PANEL[:-6] + HEALTHY_PANEL + "</body></html>"
        panels = RENDER.extract_svg_panels(broken)
        self.assertEqual(len(panels), 3)
        self.assertEqual([panel.count("<svg") for panel in panels], [1, 1, 1])
        # The damaged panel is reported, not silently concatenated.
        problems = RENDER.validate_panel(1, panels[1])
        self.assertTrue(any("missing </svg>" in problem for problem in problems))

    def test_a_removed_middle_close_fails_the_whole_render(self):
        html = self.write_html(
            "<html><body>" + HEALTHY_PANEL + HEALTHY_PANEL[:-6] + HEALTHY_PANEL + "</body></html>"
        )
        message = self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "missing </svg> close tag",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )
        self.assertIn("panel 1", message)

    def test_a_panel_with_nested_openings_is_an_error(self):
        # Defense in depth: even if an extractor handed back a concatenation,
        # validate_panel refuses a panel that is not exactly one SVG document.
        concatenated = HEALTHY_PANEL + HEALTHY_PANEL
        problems = RENDER.validate_panel(4, concatenated)
        self.assertTrue(any("panel 4" in problem for problem in problems))
        self.assertTrue(any("opening tags" in problem for problem in problems))

    # -- series-data check -------------------------------------------------

    def test_series_count_is_positive_for_a_real_panel(self):
        self.assertGreater(RENDER.panel_series_count(HEALTHY_PANEL), 0)

    def test_series_count_is_zero_for_an_empty_chart(self):
        self.assertEqual(RENDER.panel_series_count(EMPTY_CHART_PANEL), 0)

    def test_series_count_is_zero_for_a_one_point_polyline(self):
        self.assertEqual(RENDER.panel_series_count(DEGENERATE_POLYLINE_PANEL), 0)

    def test_series_count_is_zero_for_a_geometryless_rect(self):
        self.assertEqual(RENDER.panel_series_count(GEOMETRYLESS_RECT_PANEL), 0)

    def test_series_count_is_zero_for_a_zero_sized_rect(self):
        self.assertEqual(RENDER.panel_series_count(ZERO_SIZED_RECT_PANEL), 0)

    def test_series_count_is_zero_for_an_unparseable_rect_size(self):
        self.assertEqual(RENDER.panel_series_count(MALFORMED_SIZE_RECT_PANEL), 0)

    def test_series_count_ignores_a_stroke_width_as_a_geometry_width(self):
        self.assertEqual(RENDER.panel_series_count(STROKE_WIDTH_ONLY_RECT_PANEL), 0)

    def test_series_count_is_positive_for_a_drawable_bar(self):
        self.assertEqual(RENDER.panel_series_count(REAL_BAR_PANEL), 1)

    def test_series_count_is_positive_for_a_generated_histogram(self):
        self.assertGreater(RENDER.panel_series_count(REAL_HISTOGRAM_PANEL), 0)

    def test_validate_panel_names_a_geometryless_rect_panel(self):
        problems = RENDER.validate_panel(1, GEOMETRYLESS_RECT_PANEL)
        self.assertTrue(any("panel 1" in problem for problem in problems))
        self.assertTrue(any("empty chart" in problem for problem in problems))

    def test_geometryless_rect_panel_fails_the_whole_render(self):
        html = self.write_html(
            healthy_html(1) + f"<section>{GEOMETRYLESS_RECT_PANEL}</section>"
        )
        message = self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "empty chart",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )
        self.assertIn("panel 1", message)

    def test_main_returns_one_and_names_a_geometryless_rect_panel(self):
        html = self.write_html(f"<html><body>{GEOMETRYLESS_RECT_PANEL}</body></html>")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = RENDER.main([str(html), "--no-rasterize", "--out", str(self.root / "out")])
        self.assertEqual(code, 1)
        self.assertIn("panel 0", stderr.getvalue())
        self.assertIn("empty chart", stderr.getvalue())

    def test_validate_panel_passes_a_real_panel(self):
        self.assertEqual(RENDER.validate_panel(0, HEALTHY_PANEL), [])

    def test_validate_panel_names_a_truncated_panel(self):
        problems = RENDER.validate_panel(2, UNTERMINATED_PANEL)
        self.assertTrue(any("panel 2" in problem for problem in problems))
        self.assertTrue(any("</svg>" in problem for problem in problems))

    def test_validate_panel_names_an_empty_chart(self):
        problems = RENDER.validate_panel(3, EMPTY_CHART_PANEL)
        self.assertTrue(any("panel 3" in problem for problem in problems))
        self.assertTrue(any("empty chart" in problem for problem in problems))

    # -- missing / graphless input ----------------------------------------

    def test_missing_file_is_an_error(self):
        self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "comparison HTML not found",
            RENDER.render_panels,
            self.root / "absent.html",
            self.root / "out",
            rasterize=False,
        )

    def test_graphless_html_is_an_error(self):
        html = self.write_html("<html><body>no charts</body></html>")
        message = self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "no <svg> panel found",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )
        self.assertIn("error", message)

    def test_empty_file_is_an_error(self):
        html = self.write_html("")
        self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "no <svg> panel found",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )

    def test_empty_chart_is_an_error(self):
        html = self.write_html(f"<html><body>{EMPTY_CHART_PANEL}</body></html>")
        self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "empty chart",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )

    def test_truncated_panel_is_an_error(self):
        html = self.write_html(f"<html><body>{UNTERMINATED_PANEL}</body></html>")
        self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "truncated panel",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=False,
        )

    # -- SVG production ----------------------------------------------------

    def test_healthy_html_produces_and_writes_panels(self):
        html = self.write_html(healthy_html(2))
        summary = RENDER.render_panels(html, self.root / "out", rasterize=False)
        self.assertEqual(summary["panels"], 2)
        self.assertEqual(len(summary["svg"]), 2)
        for path in summary["svg"]:
            document = Path(path).read_text(encoding="utf-8")
            self.assertIn("<svg", document)
            self.assertIn("xmlns", document)
            self.assertIn("polyline", document)
            document.encode("ascii")

    # -- rasterization honesty --------------------------------------------

    def test_rasterize_without_a_browser_is_an_error(self):
        html = self.write_html(healthy_html(1))
        with mock.patch.object(RENDER, "find_browser", return_value=None):
            self.assertRaisesMessage(
                RENDER.RenderGraphError,
                "no headless browser found",
                RENDER.render_panels,
                html,
                self.root / "out",
                rasterize=True,
                browser=None,
            )
        # The SVGs must already exist even though the PNG step failed.
        self.assertTrue((self.root / "out" / "g0.svg").is_file())

    def test_rasterize_records_verified_pngs(self):
        html = self.write_html(healthy_html(2))
        browser = self.write_browser(FAKE_BROWSER.format(png=ONE_PIXEL_PNG))
        summary = RENDER.render_panels(
            html, self.root / "out", rasterize=True, browser=browser
        )
        self.assertTrue(summary["rasterized"])
        self.assertEqual(len(summary["png"]), 2)
        for path in summary["png"]:
            self.assertEqual(
                RENDER.png_dimensions(Path(path).read_bytes()), (1, 1)
            )

    def test_rasterize_fails_when_browser_writes_no_png(self):
        html = self.write_html(healthy_html(1))
        browser = self.write_browser(NULL_BROWSER)
        message = self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "did not produce a valid PNG",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=True,
            browser=browser,
        )
        self.assertIn("g0.png", message)

    def test_rasterize_fails_when_the_browser_cannot_be_executed(self):
        html = self.write_html(healthy_html(1))
        browser = self.root / "not-executable"
        browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        message = self.assertRaisesMessage(
            RENDER.RenderGraphError,
            "could not be executed",
            RENDER.render_panels,
            html,
            self.root / "out",
            rasterize=True,
            browser=str(browser),
        )
        self.assertIn("g0.png", message)

    def test_png_dimensions_rejects_garbage(self):
        self.assertIsNone(RENDER.png_dimensions(b"not a png"))

    def test_png_dimensions_reads_ihdr(self):
        self.assertEqual(RENDER.png_dimensions(ONE_PIXEL_PNG), (1, 1))

    # -- CLI ---------------------------------------------------------------

    def test_main_returns_zero_on_healthy_svg_only(self):
        html = self.write_html(healthy_html(1))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = RENDER.main([str(html), "--no-rasterize", "--out", str(self.root / "out")])
        self.assertEqual(code, 0)
        self.assertIn("panels: 1", stdout.getvalue())

    def test_main_returns_one_and_names_the_problem(self):
        html = self.write_html("<html><body>no charts</body></html>")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = RENDER.main([str(html), "--no-rasterize", "--out", str(self.root / "out")])
        self.assertEqual(code, 1)
        self.assertIn("no <svg> panel found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
