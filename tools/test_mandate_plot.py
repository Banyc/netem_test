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
