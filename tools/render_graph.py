#!/usr/bin/env python3
"""Render and verify the graph panels of a perf-loop ``comparison.html``.

The paired-capture loop's mandatory evidence includes a *rendered graph*: the
comparison HTML embeds one ``<svg>`` panel per chart (rolling goodput, RTT CDFs,
and one paired RTT CDF per valid seed pair), and that graph must actually exist
and carry data before a verdict is read.

This tool replaces the unversioned manual script that used to live outside the
repository (a scratch-directory ``render-graph.sh``). That script extracted
``<svg>`` blocks with a glob, rasterized them with headless Chrome, and then
``ls``-ed the result: with no ``<svg>`` in the HTML the glob stayed literal,
Chrome rasterized the literal name into a blank screenshot, and the script
still exited zero. A graph that cannot be produced was therefore indistinguishable
from a graph that was produced and read.

The in-repo contract is the opposite:

* extracting no panel is an error (non-zero exit, message naming the file);
* a panel that contains no series data (an empty chart) is an error naming the
  offending panel index;
* every produced SVG is written and verified in-repo before any PNG step runs;
* rasterization is required by default, and if no headless browser is available
  the PNG step fails loudly (non-zero exit) instead of silently emitting
  nothing;
* an explicitly constructed file list is rasterized and every produced PNG is
  verified to be a real, non-degenerate PNG, so the old literal-glob/blank-PNG
  failure mode cannot recur.

Rasterization is delegated to a headless browser (Chrome/Chromium) because SVG
rasterization needs one; the verification does not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>")
SVG_CLOSE = "</svg>"
POLYLINE_RE = re.compile(r"<polyline\b[^>]*\bpoints=\"([^\"]*)\"")
PATH_RE = re.compile(r"<path\b[^>]*\bd=\"([^\"]*)\"")
RECT_RE = re.compile(r"<rect\b[^>]*>")
WIDTH_ATTR_RE = re.compile(r'\s+width="[^"]*"')
HEIGHT_ATTR_RE = re.compile(r'\s+height="[^"]*"')

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A polyline with fewer than two points draws nothing.
MIN_POLYLINE_POINTS = 2

# Class rules the comparison HTML supplies in its own <style> block; a
# standalone SVG file must carry them itself or its plot background renders
# as an opaque default-black rectangle.
STANDALONE_STYLE = (
    "<style>"
    "text{font-size:11px;fill:#43506a}"
    ".plot-bg{fill:#fbfcff}"
    ".grid{stroke:#dfe5ef;stroke-width:1}"
    "</style>"
)

BROWSER_ENV = "NETEM_RENDER_BROWSER"
BROWSER_APP_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser", "chrome")


class RenderGraphError(Exception):
    """A graph-production failure that must surface as a non-zero exit."""


def extract_svg_panels(html: str) -> list[str]:
    """Return the text of every ``<svg ...>`` panel opening, in order.

    Each panel spans from an ``<svg ...>`` opening tag to its ``</svg>`` close;
    an opening with no close returns the remainder of the document so that
    ``validate_panel`` can flag the missing close instead of silently dropping
    the panel (which would look like a graph that was never generated).
    """
    panels = []
    for match in SVG_OPEN_RE.finditer(html):
        end = html.find(SVG_CLOSE, match.start())
        if end < 0:
            panels.append(html[match.start():])
        else:
            panels.append(html[match.start():end + len(SVG_CLOSE)])
    return panels


def _polyline_point_count(points_attr: str) -> int:
    return len([point for point in points_attr.split() if point.strip()])


def panel_series_count(panel: str) -> int:
    """Count drawable data series in one SVG panel.

    A polyline needs at least two points to draw a segment, a ``<path>`` needs
    a non-empty ``d``, and a bar is a ``<rect>`` that is not the plot
    background. A panel with only axes, gridlines, and a background therefore
    counts zero series and is treated as an empty chart.
    """
    series = 0
    for points in POLYLINE_RE.findall(panel):
        if _polyline_point_count(points) >= MIN_POLYLINE_POINTS:
            series += 1
    for d in PATH_RE.findall(panel):
        if d.strip():
            series += 1
    for rect in RECT_RE.findall(panel):
        if "plot-bg" not in rect:
            series += 1
    return series


def validate_panel(index: int, panel: str) -> list[str]:
    """Return the problems that make this panel unusable as evidence."""
    problems = []
    if SVG_CLOSE not in panel:
        problems.append(f"panel {index}: missing </svg> close tag (truncated panel)")
    if panel_series_count(panel) <= 0:
        problems.append(
            f"panel {index}: no series data (an empty chart is not a graph)"
        )
    return problems


def standalone_panel(panel: str, width: int, height: int) -> str:
    """Make one embedded panel a self-contained SVG document.

    Adds the SVG namespace and the class styling the surrounding HTML provided,
    and pins an explicit pixel size so rasterization has a definite canvas.
    """
    match = SVG_OPEN_RE.match(panel)
    if match is None:
        return panel
    open_tag = match.group(0)
    if "xmlns" not in open_tag:
        open_tag = open_tag.replace(
            "<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1
        )
    open_tag = WIDTH_ATTR_RE.sub("", open_tag)
    open_tag = HEIGHT_ATTR_RE.sub("", open_tag)
    open_tag = open_tag.replace(
        "<svg ", f'<svg width="{width}" height="{height}" ', 1
    )
    return open_tag + STANDALONE_STYLE + panel[match.end():]


def find_browser(explicit: str | None = None) -> str | None:
    """Resolve a headless browser executable, or return None."""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        return found
    env = os.environ.get(BROWSER_ENV)
    if env:
        return find_browser(env)
    for app_path in BROWSER_APP_PATHS:
        if Path(app_path).is_file():
            return app_path
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) for a PNG byte string, else None."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height)


def rasterize_svg(
    browser: str,
    svg_path: Path,
    png_path: Path,
    width: int,
    height: int,
    timeout: float = 120.0,
) -> str | None:
    """Best-effort headless screenshot of one standalone SVG file.

    Returns None on success, or a message describing why the browser could not
    be run to completion.
    """
    command = [
        browser,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--screenshot={png_path}",
        f"--window-size={width},{height}",
        svg_path.resolve().as_uri(),
    ]
    try:
        subprocess.run(
            command,
            check=False,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"the browser did not finish within {timeout:.0f}s"
    except OSError as error:
        return f"the browser could not be executed: {error}"
    return None


def render_panels(
    comparison_html: Path,
    out_dir: Path,
    *,
    rasterize: bool = True,
    browser: str | None = None,
    width: int = 960,
    height: int = 300,
) -> dict:
    """Extract, verify, write, and optionally rasterize the comparison panels.

    Raises RenderGraphError, naming the problem, when the HTML is missing, when
    it contains no panel, when any panel is empty, or when rasterization was
    requested but could not be performed or verified.
    """
    comparison_html = Path(comparison_html)
    if not comparison_html.is_file():
        raise RenderGraphError(f"comparison HTML not found: {comparison_html}")
    html = comparison_html.read_text(encoding="utf-8", errors="replace")
    panels = extract_svg_panels(html)
    if not panels:
        raise RenderGraphError(
            f"no <svg> panel found in {comparison_html}: the comparison rendered "
            "no graph. A graph that cannot be produced is an error, not an empty "
            "file to skim past."
        )

    problems = []
    for index, panel in enumerate(panels):
        problems.extend(validate_panel(index, panel))
    if problems:
        raise RenderGraphError(
            f"{comparison_html} produced {len(panels)} panel(s) but at least one "
            "cannot be used as evidence:\n  " + "\n  ".join(problems)
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {
        "html": str(comparison_html),
        "panels": len(panels),
        "series_counts": [panel_series_count(panel) for panel in panels],
        "svg": [],
        "png": [],
        "browser": None,
        "rasterized": False,
    }
    for index, panel in enumerate(panels):
        svg_path = out_dir / f"g{index}.svg"
        svg_path.write_text(
            standalone_panel(panel, width, height), encoding="utf-8"
        )
        summary["svg"].append(str(svg_path))

    if not rasterize:
        return summary

    resolved = find_browser(browser)
    if resolved is None:
        raise RenderGraphError(
            "cannot rasterize: no headless browser found, so the PNG step cannot "
            f"run. The {len(panels)} SVG panel(s) were written and verified under "
            f"{out_dir}. Set {BROWSER_ENV} or pass --browser, or pass "
            "--no-rasterize to accept SVG-only evidence explicitly."
        )
    summary["browser"] = resolved

    failures = []
    for svg_path in summary["svg"]:
        png_path = Path(svg_path).with_suffix(".png")
        problem = rasterize_svg(resolved, Path(svg_path), png_path, width, height)
        if problem is not None:
            failures.append(f"{png_path}: {problem}")
            continue
        data = png_path.read_bytes() if png_path.is_file() else b""
        dimensions = png_dimensions(data)
        if dimensions is None:
            failures.append(
                f"{png_path}: the browser did not produce a valid PNG "
                f"({len(data)} bytes)"
            )
        elif dimensions[0] <= 0 or dimensions[1] <= 0:
            failures.append(
                f"{png_path}: the browser produced a degenerate PNG "
                f"{dimensions[0]}x{dimensions[1]}"
            )
        else:
            summary["png"].append(str(png_path))
    if failures:
        raise RenderGraphError(
            "rasterization failed; the SVGs were verified but the PNG step "
            "cannot be trusted:\n  " + "\n  ".join(failures)
        )
    summary["rasterized"] = True
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and verify the <svg> graph panels of a perf-loop "
            "comparison.html, then rasterize them to PNG."
        )
    )
    parser.add_argument("comparison_html", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: <comparison_html parent>/graphs)",
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
        help=f"headless browser executable or name (default: ${BROWSER_ENV} or a "
        "known Chrome/Chromium path)",
    )
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON summary of the produced panels",
    )
    args = parser.parse_args(argv)

    out_dir = args.out if args.out is not None else args.comparison_html.parent / "graphs"
    try:
        summary = render_panels(
            args.comparison_html,
            out_dir,
            rasterize=args.rasterize,
            browser=args.browser,
            width=args.width,
            height=args.height,
        )
    except RenderGraphError as error:
        print(f"render_graph: error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"panels: {summary['panels']}")
        for path in summary["svg"]:
            print(f"svg: {path}")
        if summary["rasterized"]:
            for path in summary["png"]:
                print(f"png: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
