//! The per-mandate performance panels, ported from `tools/mandate_plot.py`.
//!
//! The perf tests that measure **M1** (interactive tail latency), **M2** (the
//! interactive lane delivering under a known offer) and **M3** (bulk goodput)
//! each write two sibling files into one directory: `<mandate>.json`, the panel
//! declaration, and `<mandate>.csv`, the series data (`panel,series,x,y`, one
//! row per plotted point). This module renders one verified SVG per declared
//! panel, states each panel's own summary, and *refuses* a panel that cannot
//! show the failure it is drawn for.
//!
//! **A panel that cannot be produced is an error, not an empty file to skim
//! past.** A missing or unreadable JSON/CSV, a malformed declaration field, a
//! CSV with no data rows, a declared panel or series with no rows, a CSV row
//! naming an undeclared panel or series (and the reverse), a malformed `chart`,
//! a non-numeric or non-finite `x`/`y`, a written SVG that carries no series
//! geometry, a declared bound that did not reach the SVG, and a panel summary
//! that is absent or false all raise [`MandatePlotError`], which the command
//! line turns into a non-zero exit naming the problem.
//!
//! **A panel that cannot show the failure it is drawn for is refused, not
//! rendered.** `AGENTS.md` ("Read every panel") makes that a defect of the same
//! family as an assertion that cannot fail. Every refusal in this module
//! enforces it, and none can be silenced by softening a declaration:
//!
//! - **the axis test** -- a bar panel's axis is chosen by `bar_axis_extent` and
//!   then measured: a bound the panel draws at its own scale needs a band of at
//!   least `MIN_BOUND_PIXELS` of the axis height, because a departure of the
//!   size the bound exists to catch must not be sub-pixel.
//! - **the sliver-bound test** -- the axis test's refusal is answered by a
//!   *statement* on the panel's own face: where the bound sits, how wide its
//!   band is in pixels, and how far the nearest and furthest bars lie from it.
//!   The statement is owed only when the panel draws a departure at least
//!   `MIN_BOUND_PIXELS` legible, so the ordinary case keeps the refusal.
//! - **the axis-clip test** -- a line panel's y axis is the data's own extent,
//!   so one outlier sets it and compresses every body. Past `Y_CLIP_FACTOR`
//!   beyond the panel's read-at value, with the excess under `Y_CLIP_SHARE` of
//!   the samples, the axis is drawn to that value and the excess is drawn at the
//!   frame's top edge, stated on the panel's face.
//! - **the panel-summary test** -- every panel carries a machine-readable
//!   `<desc class="panel-summary">` whose numbers are recomputed from the drawn
//!   points, the drawn axis and the drawn `class="bound"` lines. Absent and
//!   false are refused alike, so "absent summary" is never a legitimate state.
//! - **the governance test** -- a bound drawn across a *line* panel whose arms
//!   the run guards differently names, per arm, the key and value the run
//!   asserts; a bound drawn across a *bar* panel the run restates per arm is
//!   drawn per arm instead, each segment naming the arms it governs.
//! - **the label-fit, label-overlap, bar-separation, canvas-text, legend,
//!   axis-resolution, gap-honesty, stated-reading and stated-number tests** --
//!   each measures the artifact that was written rather than trusting the code
//!   that wrote it.
//!
//! Two reading choices the input contract leaves open, decided here and made
//! loud instead of silent: a `cdf` panel is drawn on a fixed 0-100 percentile
//! axis, so its `y` is required to lie in `[0, 100]` (the renderer does not
//! derive a percentile from the `x` samples); and `bounds` are horizontal only,
//! so a mandate ceiling expressed on a plot's *x* axis is carried on the panel
//! whose *y* axis is that quantity and read back off it (`derived_x_bounds`)
//! rather than declared twice.
//!
//! # What is ported here and what is not
//!
//! This is the whole of `tools/mandate_plot.py`, and the parts of
//! `tools/render_graph.py` and `tools/rtp_trace_report.py` it imports: the SVG
//! panel extraction and validation, the bound-label metrics and placement, the
//! line/CDF chart, and the CDF reference-reach policy. What is **not** ported is
//! the rest of those two modules -- `render_graph`'s headless-browser
//! rasterization step, its `main`, and `rtp_trace_report`'s trace-report
//! rendering (`render_report`, the histogram, the congestion/FEC panels) --
//! because the mandate plotter does not call them. `rtp_trace_report.py` remains
//! the authority for the trace report; the chart primitives duplicated here are
//! recorded as a live duplication in `crates/AUDIT_COVERAGE.md`, not as a
//! second authority nobody knows about.

// `!(x > y)` is deliberate throughout this module: it is Python's own
// `not (x > y)`, and unlike `x <= y` it is true for a NaN.
#![allow(clippy::neg_cmp_op_on_partial_ord)]

mod checks;
mod draw;
mod render;

use std::collections::BTreeMap;
use std::path::Path;

use crate::tools::pyformat;
use crate::tools::pyjson::{self, J};
use crate::tools::pyre::Regex;

pub use render::{Args, print_summary, render_mandate};

// -- the refusal ------------------------------------------------------------
//
// Every production failure surfaces as this one error type, because the
// command line's contract is a single non-zero exit naming the problem: a
// panel that is refused and a declaration that is malformed are the same
// outcome for a reader of the run.

/// A mandate-panel production failure that must surface as a non-zero exit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MandatePlotError(pub String);

impl std::fmt::Display for MandatePlotError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for MandatePlotError {}

/// The module's result type.
pub type PlotResult<T> = std::result::Result<T, MandatePlotError>;

fn fail<T>(message: impl Into<String>) -> PlotResult<T> {
    Err(MandatePlotError(message.into()))
}

// -- the declared vocabulary ------------------------------------------------

/// The chart kinds a panel may declare.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Chart {
    Line,
    Cdf,
    Bar,
}

impl Chart {
    fn parse(text: &str) -> Option<Chart> {
        match text {
            "line" => Some(Chart::Line),
            "cdf" => Some(Chart::Cdf),
            "bar" => Some(Chart::Bar),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Chart::Line => "line",
            Chart::Cdf => "cdf",
            Chart::Bar => "bar",
        }
    }
}

/// The CSV's columns, in order.
pub const CSV_COLUMNS: (&str, &str, &str, &str) = ("panel", "series", "x", "y");

/// How many legend columns a panel lays its series out in.
pub const LEGEND_COLUMNS: usize = 4;

/// The largest share of a panel's samples a y-axis clip may hide.
pub const Y_CLIP_SHARE: f64 = 0.01;

/// How far beyond the read-at value a drawn value is an outlier.
pub const Y_CLIP_FACTOR: f64 = 1.5;

/// The least height, in pixels, a one-sided bound's own region must be drawn in.
pub const MIN_BOUND_PIXELS: f64 = 6.0;

/// The share of the span kept between the frame and the nearest datum/bound.
pub const FRAME_HEADROOM: f64 = 0.05;

/// Above this share on the far side, a bound's split is a target, not a crossing.
pub const CROSSING_BULK_SHARE: f64 = 1.0 / 3.0;

/// The fewest pixels of plot a line panel keeps once its reading band is
/// reserved.
pub const ARM_READING_MIN_PLOT_PIXELS: f64 = 100.0;

/// The least height, in pixels, an axis keeps beyond every value it names.
pub const MIN_HEADROOM_PIXELS: f64 = 6.0;

/// The least distance, in pixels, a named bound keeps from the frame's own edge.
pub const MIN_AXIS_INSET_PIXELS: f64 = 2.0;

/// The least gap between two drawn bars.
pub const MIN_BAR_GAP_PIXELS: f64 = 1.0;

/// The smallest span a fraction panel may be drawn over.
pub const MIN_UNIT_SPAN: f64 = 0.01;

/// The share of a category's band kept clear at each end of it.
pub const BAR_BAND_INSET_SHARE: f64 = 0.12;

/// The share of a bar's slot left as the gap to its neighbour.
pub const BAR_GAP_SHARE: f64 = 0.18;

/// How much of the area two drawn labels may share before the panel is refused.
pub const LABEL_OVERLAP_PX2: f64 = 1.0;

/// The least share of a CDF panel's x axis its reference arm must reach.
pub const MIN_REFERENCE_REACH_SHARE: f64 = 0.5;

/// The mark a bound's label uses to declare a symmetric two-sided band.
pub const BOUND_BAND_MARK: char = '\u{b1}';

/// What a run's key ends in to be that arm's own bound for a quantity.
pub const PER_ARM_BOUND_SUFFIXES: (&str, &str) = ("_guard", "_floor");

/// What a run's key ends in to be an unprefixed bound for a quantity.
pub const QUANTITY_BOUND_SUFFIXES: (&str, &str, &str) = ("_floor", "_guard", "_budget");

/// The `MANDATE` line's per-arm guard measurements: `hostile_p99_guard=900`.
pub const GUARD_KEY_SUFFIX: &str = "_guard";

/// Human names for the producer columns whose prettified form is still cryptic.
pub const SERIES_LABEL_VOCABULARY: &[(&str, &str)] = &[("fraction", "fraction of link rate")];

/// The class styling a standalone SVG file must carry itself, or its plot
/// background renders as an opaque default-black rectangle.
pub const STANDALONE_STYLE: &str = "<style>\
text{font-size:11px;fill:#43506a}\
.plot-bg{fill:#fbfcff}\
.grid{stroke:#dfe5ef;stroke-width:1}\
</style>";

/// A polyline with fewer than two points draws nothing.
pub const MIN_POLYLINE_POINTS: usize = 2;

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern, false).unwrap_or_else(|error| panic!("{pattern}: {error}"))
}

fn regex_dotall(pattern: &str) -> Regex {
    Regex::new(pattern, true).unwrap_or_else(|error| panic!("{pattern}: {error}"))
}

// -- patterns over the written artifact -------------------------------------
//
// These are the Python patterns, spelled the same way: a check defined by a
// pattern is only the same check as its twin if it is the same pattern, and the
// port's own engine takes the same subset of the syntax.

fn panel_id_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"^[A-Za-z0-9_.-]+$"))
}

fn plot_bg_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(
            r#"<rect x="([-0-9.]+)" y="([-0-9.]+)" width="([-0-9.]+)" height="([-0-9.]+)" class="plot-bg""#,
        )
    })
}

fn bound_label_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r#"<text class="bound-label"([^>]*)>(.*?)</text>"#))
}

fn drawn_bound_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r#"<line class="bound"[^>]*\by1="([-0-9.]+)""#))
}

fn bound_label_title_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r"<title>.*?</title>"))
}

fn text_attribute_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r#"([A-Za-z][\w-]*)="([^"]*)""#))
}

fn text_element_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r"<text\b([^>]*)>(.*?)</text>"))
}

fn rotate_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"rotate\(\s*[-0-9.]+\s+([-0-9.]+)\s+([-0-9.]+)\s*\)"))
}

fn bar_rect_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(
            r#"<rect x="([-0-9.]+)" y="([-0-9.]+)" width="([-0-9.]+)" height="([-0-9.]+)" fill="(#[0-9A-Fa-f]{6})""#,
        )
    })
}

fn legend_group_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r#"<g class="legend">(.*?)</g>"#))
}

fn readings_group_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r#"<g class="arm-readings">(.*?)</g>"#))
}

fn polyline_element_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"<polyline\s([^>]*?)/>"))
}

fn sample_marker_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r#"<circle class="sample""#))
}

fn axis_tick_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(&format!(
            r#"<text x="{}" y="[-0-9.]+" text-anchor="end">([^<]*)</text>"#,
            draw::PAD_LEFT - 9
        ))
    })
}

fn x_axis_tick_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(&format!(
            r#"<text x="[-0-9.]+" y="{}" text-anchor="middle">([^<]*)</text>"#,
            draw::HEIGHT - 24
        ))
    })
}

fn stated_peak_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"(?:peak|max(?:imum)?) ([-+0-9.eE]+) ms at ([-+0-9.eE]+) s"))
}

fn stated_after_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(
            r"(-?\d+) sample\(s\) after it \(next ([-+0-9.eE]+) ms at ([-+0-9.eE]+) s, last ([-+0-9.eE]+) ms at ([-+0-9.eE]+) s\)",
        )
    })
}

fn stated_end_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"nothing after it, so the series ends on its own maximum"))
}

fn stated_holes_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(
            r"(\d+) sample gap\(s\) over ([-+0-9.eE]+) s, largest ([-+0-9.eE]+) s \(([-+0-9.eE]+)-([-+0-9.eE]+) s\), drawn as breaks, not climbs",
        )
    })
}

fn stated_gap_wall_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"no sample gap over ([-+0-9.eE]+) s"))
}

fn stated_no_gap_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"no sample gap(?![ \w])"))
}

fn panel_note_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex_dotall(r#"<text class="panel-note" x="([-0-9.]+)" y="([-0-9.]+)"[^>]*>(.*?)</text>"#)
    })
}

fn placeholder_text_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"\[\s*\]|\(\s*\)|\bNone\b|\bnull\b|\bnan\b"))
}

fn bound_band_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    // A *regular* string, not a raw one: `\u{b1}` is not an escape inside
    // `r"..."`, and a pattern that looked for the six literal characters would
    // never match a band label -- which is how a `±1.0%` bound came to be drawn
    // as a one-sided line.
    ONCE.get_or_init(|| regex("\u{b1}\\s*([0-9]+(?:\\.[0-9]+)?)\\s*(%)?"))
}

fn governance_clause_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"\[(?P<body>[^\]]*)\]"))
}

fn governance_name_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"[A-Za-z_][A-Za-z0-9_]*"))
}

fn panel_summary_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex_dotall(r#"<desc class="panel-summary">(?P<body>.*?)</desc>"#))
}

fn svg_opening_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"<svg\b[^>]*>"))
}

fn legend_text_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"<text[^>]*>(?P<label>[^<]*)</text>"))
}

fn x_bound_mark_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r#"class="x-bound" x1="([-0-9.]+)""#))
}

fn raw_column_name_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$"))
}

fn y_clip_line_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r#"class="y-clip""#))
}

fn title_tag_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"</?title>"))
}

fn sliver_statement_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        regex(
            r#"bound "(?P<label>[^"]*)" at (?P<value>[-+0-9.eE]+) on axis (?P<low>[-+0-9.eE]+)\.\.(?P<high>[-+0-9.eE]+): band (?P<band>[-+0-9.eE]+) = (?P<band_px>[0-9.]+) px; nearest bar (?P<near>[-+0-9.eE]+), (?P<near_dist>[-+0-9.eE]+) away; furthest (?P<far>[-+0-9.eE]+), (?P<far_px>[0-9.]+) px from the bound(?:; its arm at (?P<arm>[-+0-9.eE]+) \((?P<arm_px>[0-9.]+) px away\) is drawn unlabelled)?"#,
        )
    })
}

fn panel_unit_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"\(([^()]*)\)\s*$"))
}

fn stated_number_re() -> &'static Regex {
    static ONCE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| regex(r"([-+]?)(\d+)(?:\.(\d+))?(?:[eE]([-+]?\d+))?"))
}

// -- formatting, in Python's own spelling -----------------------------------
//
// Every float the ported tool prints comes from the measured formatter rather
// than from a Rust format string, because the specs include Python's `g` and
// `%` presentations and Rust has neither: `{:.4}` is four decimal places to
// Rust and four *significant digits* to Python.

fn fmt_float(value: f64, spec: &str) -> String {
    pyformat::format_float(value, spec)
        .unwrap_or_else(|error| panic!("format_float({value:?}, {spec:?}): {error}"))
}

/// `f"{value:g}"`.
fn fg(value: f64) -> String {
    fmt_float(value, "g")
}

/// `f"{value:.4g}"`.
fn f4g(value: f64) -> String {
    fmt_float(value, ".4g")
}

/// `f"{value:.6g}"`.
fn f6g(value: f64) -> String {
    fmt_float(value, ".6g")
}

/// `f"{value:.1f}"`.
fn f1(value: f64) -> String {
    fmt_float(value, ".1f")
}

/// `f"{value:.2f}"`.
fn f2(value: f64) -> String {
    fmt_float(value, ".2f")
}

/// `f"{value:.0f}"`.
fn f0(value: f64) -> String {
    fmt_float(value, ".0f")
}

/// `f"{value:.1%}"`.
fn pct1(value: f64) -> String {
    fmt_float(value, ".1%")
}

/// `f"{value:.2%}"`.
fn pct2(value: f64) -> String {
    fmt_float(value, ".2%")
}

/// `f"{value:.0%}"`.
fn pct0(value: f64) -> String {
    fmt_float(value, ".0%")
}

/// `f"{value:.{decimals}f}"`.
fn fdec(value: f64, decimals: usize) -> String {
    fmt_float(value, &format!(".{decimals}f"))
}

/// `f"{value:.{decimals}g}"`.
fn fdec_g(value: f64, decimals: usize) -> String {
    fmt_float(value, &format!(".{decimals}g"))
}

/// Python's `repr()` of a float.
fn frepr(value: f64) -> String {
    pyjson::repr_float(value)
}

// -- small conveniences the ported code uses constantly ---------------------

/// `math.isclose(a, b, rel_tol, abs_tol)` for the two defaults used here.
fn is_close(a: f64, b: f64, rel_tol: f64, abs_tol: f64) -> bool {
    if a == b {
        return true;
    }
    if a.is_infinite() || b.is_infinite() {
        return false;
    }
    (a - b).abs() <= (rel_tol * b.abs()).max(rel_tol * a.abs()).max(abs_tol)
}

/// `math.ceil` for a non-negative value, as a `usize`.
fn ceil_usize(value: f64) -> usize {
    value.ceil().max(0.0) as usize
}

// -- the declaration --------------------------------------------------------

/// One declared series of one panel.
#[derive(Debug, Clone, PartialEq)]
pub struct SeriesEntry {
    pub name: String,
    #[allow(dead_code)]
    pub role: Option<String>,
}

/// One declared bound line.
///
/// The Python declaration is a dict whose keys are read by name and copied
/// around as the plan is built; the keys the *plan* adds (`band_arm`, `arms`,
/// `window`, `guard_key`, `governs_none`, `unlabelled`, `guards_drawn`) are
/// carried here as fields so a bound and the lines drawn for it cannot disagree
/// about how many lines a declaration calls for.
#[derive(Debug, Clone, PartialEq)]
pub struct Bound {
    pub y: f64,
    pub label: String,
    pub series: Option<String>,
    pub x: Option<(f64, f64)>,
    pub arms: Option<Vec<String>>,
    pub window: Option<Vec<f64>>,
    pub guard_key: Option<String>,
    pub band_arm: Option<String>,
    pub governs_none: bool,
    pub unlabelled: bool,
    pub guards_drawn: bool,
}

impl Bound {
    fn new(y: f64, label: String) -> Bound {
        Bound {
            y,
            label,
            series: None,
            x: None,
            arms: None,
            window: None,
            guard_key: None,
            band_arm: None,
            governs_none: false,
            unlabelled: false,
            guards_drawn: false,
        }
    }
}

/// One declared panel.
#[derive(Debug, Clone, PartialEq)]
pub struct Panel {
    pub id: String,
    pub chart: Chart,
    pub series: Vec<SeriesEntry>,
    pub x_label: Option<String>,
    pub y_label: Option<String>,
    pub bounds: Vec<Bound>,
    pub y_extent: Option<(f64, f64)>,
}

impl Panel {}

/// The parsed declaration, plus the mandate's own labels.
#[derive(Debug, Clone, PartialEq)]
pub struct Declaration {
    pub mandate: String,
    pub title: String,
    pub x_label: String,
    pub y_label: String,
    pub panels: Vec<Panel>,
}

/// The plotted points, keyed by `(panel, series)`.
pub type Points = BTreeMap<(String, String), Vec<(f64, f64)>>;

/// One panel's series as `[(name, points)]`, in declaration order.
pub type Series = Vec<(String, Vec<(f64, f64)>)>;

fn require_text(value: Option<&J>, where_: &str) -> PlotResult<String> {
    match value.and_then(J::as_str) {
        Some(text) if !text.trim().is_empty() => Ok(text.to_string()),
        _ => fail(format!("{where_} must be a non-empty string")),
    }
}

fn require_number(value: Option<&J>, where_: &str) -> PlotResult<f64> {
    let number = match value {
        Some(J::Int(text)) => *text as f64,
        Some(J::Float(text)) => *text,
        other => {
            return fail(format!(
                "{where_} must be a number, got {}",
                other.map(J::repr).unwrap_or_else(|| "None".to_string())
            ));
        }
    };
    if !number.is_finite() {
        return fail(format!(
            "{where_} must be finite, got {}",
            value.map(J::repr).unwrap_or_else(|| "None".to_string())
        ));
    }
    Ok(number)
}

/// A pinned `[low, high]` axis, or `None` when not declared.
fn require_extent(value: Option<&J>, where_: &str) -> PlotResult<Option<(f64, f64)>> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value == &J::Null {
        return Ok(None);
    }
    let items = match value.as_arr() {
        Some(items) if items.len() == 2 => items,
        _ => {
            return fail(format!(
                "{where_} must be a two-element [low, high] list when present"
            ));
        }
    };
    let low = require_number(items.first(), &format!("{where_}[0]"))?;
    let high = require_number(items.get(1), &format!("{where_}[1]"))?;
    if !(low < high) {
        return fail(format!(
            "{where_} must be [low, high] with low < high, got {}",
            value.repr()
        ));
    }
    Ok(Some((low, high)))
}

/// The x-window a bound declares it governs, or `None` for the panel's own.
fn bound_governed_x(bound: &Bound) -> Option<(f64, f64)> {
    bound.x
}

fn governed_x_from_json(value: Option<&J>) -> PlotResult<Option<(f64, f64)>> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value == &J::Null {
        return Ok(None);
    }
    if let Some(members) = value.as_obj() {
        let lookup = |key: &str| members.iter().find(|(name, _)| name == key).map(|(_, v)| v);
        let low = require_number(lookup("min"), "bound.x.min")?;
        let high = require_number(lookup("max"), "bound.x.max")?;
        if !(low <= high) {
            return fail(format!("bound.x must be min <= max, got {}", value.repr()));
        }
        return Ok(Some((low, high)));
    }
    if let Some(number) = value.as_f64() {
        return Ok(Some((number, number)));
    }
    if let Some(items) = value.as_arr() {
        if items.is_empty() {
            return fail(format!(
                "bound.x must be a number, a non-empty list of numbers, or \
                 {{'min': .., 'max': ..}}, got {}",
                value.repr()
            ));
        }
        let mut numbers = Vec::new();
        for item in items {
            numbers.push(require_number(Some(item), "bound.x[]")?);
        }
        let low = numbers.iter().cloned().fold(f64::INFINITY, f64::min);
        let high = numbers.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        return Ok(Some((low, high)));
    }
    fail(format!(
        "bound.x must be a number, a non-empty list of numbers, or \
         {{'min': .., 'max': ..}}, got {}",
        value.repr()
    ))
}

/// Read and parse the panel declaration, naming an unreadable file.
pub fn load_declaration(path: &Path) -> PlotResult<J> {
    if !path.is_file() {
        return fail(format!("mandate declaration not found: {}", path.display()));
    }
    let bytes = std::fs::read(path).map_err(|error| {
        MandatePlotError(format!(
            "mandate declaration unreadable: {}: {error}",
            path.display()
        ))
    })?;
    let text = String::from_utf8(bytes).map_err(|error| {
        MandatePlotError(format!(
            "mandate declaration is not UTF-8 text: {}: {error}",
            path.display()
        ))
    })?;
    let document = pyjson::parse(&text).map_err(|error| {
        MandatePlotError(format!(
            "mandate declaration is not valid JSON: {}: {error}",
            path.display()
        ))
    })?;
    if document.as_obj().is_none() {
        return fail(format!(
            "mandate declaration must be a JSON object, got {}: {}",
            document.type_name(),
            path.display()
        ));
    }
    Ok(document)
}

/// Check the declaration and return the mandate, its labels and its panels.
pub fn validate_declaration(document: &J, path: &Path) -> PlotResult<Declaration> {
    let mandate = require_text(
        document.get("mandate"),
        &format!("{}: mandate", path.display()),
    )?;
    let title = require_text(document.get("title"), &format!("{}: title", path.display()))?;
    for field in ["x_label", "y_label"] {
        if let Some(value) = document.get(field)
            && value.as_str().is_none()
        {
            return fail(format!(
                "{}: {field} must be a string when present",
                path.display()
            ));
        }
    }
    let x_label = document
        .get("x_label")
        .and_then(J::as_str)
        .unwrap_or("")
        .to_string();
    let y_label = document
        .get("y_label")
        .and_then(J::as_str)
        .unwrap_or("")
        .to_string();
    let panels_value = document.get("panels");
    let panels_items = match panels_value.and_then(J::as_arr) {
        Some(items) if !items.is_empty() => items,
        _ => {
            return fail(format!(
                "{}: panels must be a non-empty list; a mandate with no panel \
                 has no graph to read",
                path.display()
            ));
        }
    };
    let mut seen_ids: Vec<String> = Vec::new();
    let mut panels = Vec::new();
    for (index, panel) in panels_items.iter().enumerate() {
        let where_ = format!("{}: panels[{index}]", path.display());
        if panel.as_obj().is_none() {
            return fail(format!(
                "{where_} must be an object, got {}",
                panel.type_name()
            ));
        }
        let identifier = require_text(panel.get("id"), &format!("{where_}.id"))?;
        if !panel_id_re().match_at(&identifier).is_some() {
            return fail(format!(
                "{where_}.id {} must match {} so it can name this panel's output file",
                pyjson::repr_str(&identifier),
                r"^[A-Za-z0-9_.-]+$"
            ));
        }
        if seen_ids.contains(&identifier) {
            return fail(format!(
                "{where_}.id {} is declared twice; a CSV row could not say which of \
                 the two panels it belongs to",
                pyjson::repr_str(&identifier)
            ));
        }
        seen_ids.push(identifier.clone());
        let chart_text = panel.get("chart").and_then(J::as_str).unwrap_or("");
        let Some(chart) = Chart::parse(chart_text) else {
            return fail(format!(
                "{where_}.chart {} is not a chart; expected one of {}",
                panel
                    .get("chart")
                    .map(J::repr)
                    .unwrap_or_else(|| "None".to_string()),
                "line, cdf, bar"
            ));
        };
        let series_value = panel.get("series");
        let series_items = match series_value.and_then(J::as_arr) {
            Some(items) if !items.is_empty() => items,
            _ => {
                return fail(format!("{where_}.series must be a non-empty list"));
            }
        };
        let mut names: Vec<String> = Vec::new();
        let mut series = Vec::new();
        for (series_index, entry) in series_items.iter().enumerate() {
            let entry_where = format!("{where_}.series[{series_index}]");
            if entry.as_obj().is_none() {
                return fail(format!(
                    "{entry_where} must be an object, got {}",
                    entry.type_name()
                ));
            }
            let name = require_text(entry.get("name"), &format!("{entry_where}.name"))?;
            if names.contains(&name) {
                return fail(format!(
                    "{entry_where}.name {} is declared twice; a CSV row names a \
                     series, not one occurrence of it",
                    pyjson::repr_str(&name)
                ));
            }
            names.push(name.clone());
            let role = match entry.get("role") {
                None | Some(J::Null) => None,
                Some(J::Str(text)) => Some(text.clone()),
                Some(_) => {
                    return fail(format!("{entry_where}.role must be a string when present"));
                }
            };
            series.push(SeriesEntry { name, role });
        }
        for field in ["x_label", "y_label"] {
            if let Some(value) = panel.get(field)
                && value.as_str().is_none()
            {
                return fail(format!("{where_}.{field} must be a string when present"));
            }
        }
        let x_label = panel.get("x_label").and_then(J::as_str).map(str::to_string);
        let y_label = panel.get("y_label").and_then(J::as_str).map(str::to_string);
        let bounds_value = match panel.get("bounds") {
            None | Some(J::Null) => &[][..],
            Some(value) => match value.as_arr() {
                Some(items) => items,
                None => {
                    return fail(format!("{where_}.bounds must be a list when present"));
                }
            },
        };
        let mut bounds = Vec::new();
        for (bound_index, value) in bounds_value.iter().enumerate() {
            let bound_where = format!("{where_}.bounds[{bound_index}]");
            if value.as_obj().is_none() {
                return fail(format!(
                    "{bound_where} must be an object, got {}",
                    value.type_name()
                ));
            }
            let y = require_number(value.get("y"), &format!("{bound_where}.y"))?;
            let label = require_text(value.get("label"), &format!("{bound_where}.label"))?;
            let series_name = match value.get("series") {
                None | Some(J::Null) => None,
                Some(_) => Some(require_text(
                    value.get("series"),
                    &format!("{bound_where}.series"),
                )?),
            };
            let x = governed_x_from_json(value.get("x"))?;
            let mut bound = Bound::new(y, label);
            bound.series = series_name;
            bound.x = x;
            bounds.push(bound);
        }
        let y_extent = require_extent(panel.get("y_extent"), &format!("{where_}.y_extent"))?;
        if chart == Chart::Cdf && y_extent.is_some() {
            return fail(format!(
                "{where_}.y_extent cannot pin a cdf panel: its axis is the fixed \
                 0-100 percentile axis the CSV's y is already carried on"
            ));
        }
        panels.push(Panel {
            id: identifier,
            chart,
            series,
            x_label,
            y_label,
            bounds,
            y_extent,
        });
    }
    Ok(Declaration {
        mandate,
        title,
        x_label,
        y_label,
        panels,
    })
}

/// Read the `panel,series,x,y` rows, refusing an empty or malformed file.
/// One CSV row as read: its 1-based line number and its four raw cells.
pub type CsvRow = (usize, String, String, String, String);

/// One arm's guards, as `(key, statistic, value)`.
pub type ArmGuards = (String, Vec<(String, String, f64)>);

pub fn load_rows(path: &Path) -> PlotResult<Vec<CsvRow>> {
    if !path.is_file() {
        return fail(format!("mandate data CSV not found: {}", path.display()));
    }
    let bytes = std::fs::read(path).map_err(|error| {
        MandatePlotError(format!(
            "mandate data CSV unreadable: {}: {error}",
            path.display()
        ))
    })?;
    // Python reads the CSV as UTF-8 with a BOM tolerance; a leading BOM is
    // dropped rather than becoming part of the first header cell.
    let text = String::from_utf8(bytes).map_err(|error| {
        MandatePlotError(format!(
            "mandate data CSV is not UTF-8 text: {}: {error}",
            path.display()
        ))
    })?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    if text.is_empty() {
        return fail(format!(
            "mandate data CSV is empty (no header row): {}",
            path.display()
        ));
    }
    let mut lines = text.split('\n').collect::<Vec<_>>();
    // `splitlines()` does not produce a final empty line for a trailing
    // newline, which changes the line numbering the refusals print.
    if lines.last() == Some(&"") && text.ends_with('\n') {
        lines.pop();
    }
    if lines.is_empty() {
        return fail(format!(
            "mandate data CSV is empty (no header row): {}",
            path.display()
        ));
    }
    let header: Vec<String> = parse_csv_line(lines[0])
        .iter()
        .map(|c| c.trim().to_string())
        .collect();
    let expected = [CSV_COLUMNS.0, CSV_COLUMNS.1, CSV_COLUMNS.2, CSV_COLUMNS.3];
    if header.len() != 4 || header.iter().zip(expected).any(|(a, b)| a != b) {
        return fail(format!(
            "mandate data CSV header must be {}, got {}: {}",
            expected.join(","),
            if header.iter().all(|cell| cell.is_empty()) {
                "(nothing)".to_string()
            } else {
                header.join(",")
            },
            path.display()
        ));
    }
    let mut rows = Vec::new();
    for (index, line) in lines.iter().enumerate().skip(1) {
        let line_number = index + 1;
        let row = parse_csv_line(line);
        if row.is_empty() {
            continue;
        }
        if row.len() != 4 {
            return fail(format!(
                "mandate data CSV line {line_number} has {} field(s), expected 4: {}",
                row.len(),
                path.display()
            ));
        }
        rows.push((
            line_number,
            row[0].trim().to_string(),
            row[1].trim().to_string(),
            row[2].clone(),
            row[3].clone(),
        ));
    }
    if rows.is_empty() {
        return fail(format!(
            "mandate data CSV has no data rows: {} (an empty chart is not a graph)",
            path.display()
        ));
    }
    Ok(rows)
}

/// One CSV line's fields, the way `csv.reader` reads a line.
fn parse_csv_line(line: &str) -> Vec<String> {
    if line.trim().is_empty() {
        return Vec::new();
    }
    let chars: Vec<char> = line.chars().collect();
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut index = 0;
    let mut quoted = false;
    while index < chars.len() {
        let character = chars[index];
        if quoted {
            if character == '"' {
                if chars.get(index + 1) == Some(&'"') {
                    current.push('"');
                    index += 2;
                    continue;
                }
                quoted = false;
                index += 1;
                continue;
            }
            current.push(character);
            index += 1;
            continue;
        }
        match character {
            '"' if current.is_empty() => {
                quoted = true;
                index += 1;
            }
            ',' => {
                fields.push(std::mem::take(&mut current));
                index += 1;
            }
            other => {
                current.push(other);
                index += 1;
            }
        }
    }
    fields.push(current);
    fields
}

/// Python's `float()` over the CSV's own cells.
fn py_float(text: &str) -> Option<f64> {
    let text = text.trim();
    let lower = text.to_ascii_lowercase();
    match lower.as_str() {
        "nan" | "+nan" | "-nan" => return Some(f64::NAN),
        "inf" | "+inf" | "infinity" | "+infinity" => return Some(f64::INFINITY),
        "-inf" | "-infinity" => return Some(f64::NEG_INFINITY),
        _ => {}
    }
    text.parse::<f64>().ok()
}

/// Group `(panel, series) -> [(x, y)]`, refusing a non-numeric or non-finite cell.
pub fn parse_points(rows: &[(usize, String, String, String, String)]) -> PlotResult<Points> {
    let mut points: Points = BTreeMap::new();
    for (line_number, panel, series, x_cell, y_cell) in rows {
        let where_ = format!(
            "line {line_number} (panel {}, series {})",
            pyjson::repr_str(panel),
            pyjson::repr_str(series)
        );
        let (Some(x), Some(y)) = (py_float(x_cell), py_float(y_cell)) else {
            return fail(format!(
                "mandate data CSV {where_} has a non-numeric x/y: x={} y={}",
                pyjson::repr_str(x_cell),
                pyjson::repr_str(y_cell)
            ));
        };
        if !(x.is_finite() && y.is_finite()) {
            return fail(format!(
                "mandate data CSV {where_} has a non-finite x/y: x={} y={}",
                pyjson::repr_str(x_cell),
                pyjson::repr_str(y_cell)
            ));
        }
        points
            .entry((panel.clone(), series.clone()))
            .or_default()
            .push((x, y));
    }
    Ok(points)
}

/// Refuse any disagreement between the declared panels and the CSV rows.
pub fn reconcile(panels: &[Panel], points: &Points, csv_path: &Path) -> PlotResult<()> {
    let mut declared: Vec<(String, String)> = Vec::new();
    for panel in panels {
        for entry in &panel.series {
            declared.push((panel.id.clone(), entry.name.clone()));
        }
    }
    let mut problems: Vec<String> = Vec::new();
    for panel in panels {
        let rows_for_panel = points.keys().filter(|(id, _)| *id == panel.id).count();
        if rows_for_panel == 0 {
            problems.push(format!(
                "panel {} is declared but {} has no rows for it (an empty chart is \
                 not a graph)",
                pyjson::repr_str(&panel.id),
                csv_path.display()
            ));
            continue;
        }
        for entry in &panel.series {
            if !points.contains_key(&(panel.id.clone(), entry.name.clone())) {
                problems.push(format!(
                    "panel {} declares series {} but {} has no row for it",
                    pyjson::repr_str(&panel.id),
                    pyjson::repr_str(&entry.name),
                    csv_path.display()
                ));
            }
        }
    }
    let declared_ids: Vec<&String> = panels.iter().map(|panel| &panel.id).collect();
    for (panel_id, series_name) in points.keys() {
        if !declared_ids.iter().any(|id| **id == *panel_id) {
            problems.push(format!(
                "{} has a row for panel {}, which the declaration does not declare",
                csv_path.display(),
                pyjson::repr_str(panel_id)
            ));
        } else if !declared
            .iter()
            .any(|(id, name)| id == panel_id && name == series_name)
        {
            problems.push(format!(
                "{} has a row for series {} in panel {}, which the declaration \
                 does not declare",
                csv_path.display(),
                pyjson::repr_str(series_name),
                pyjson::repr_str(panel_id)
            ));
        }
    }
    if problems.is_empty() {
        return Ok(());
    }
    fail(format!(
        "the declaration and the data do not agree:\n  {}",
        problems.join("\n  ")
    ))
}

/// Refuse a point that cannot mean what its chart kind says it means.
pub fn check_chart_domain(panel: &Panel, points: &Points) -> PlotResult<()> {
    if panel.chart != Chart::Cdf {
        return Ok(());
    }
    for entry in &panel.series {
        let Some(rows) = points.get(&(panel.id.clone(), entry.name.clone())) else {
            continue;
        };
        for (_, y) in rows {
            if !(0.0..=100.0).contains(y) {
                return fail(format!(
                    "panel {} is a cdf: series {} has y={}, but a cdf y is a \
                     percentile on a 0-100 axis. Emit the plotted percentile in y; \
                     the renderer does not derive it from the x samples.",
                    pyjson::repr_str(&panel.id),
                    pyjson::repr_str(&entry.name),
                    frepr(*y)
                ));
            }
        }
    }
    Ok(())
}

/// The panel's declared bounds, as the plan's own copies.
fn bound_specs(panel: &Panel) -> Vec<Bound> {
    panel.bounds.clone()
}

/// A panel's series as `[(name, [(x, y)])]`, in declaration order.
pub fn panel_series(panel: &Panel, points: &Points) -> Series {
    panel
        .series
        .iter()
        .map(|entry| {
            let mut rows = points
                .get(&(panel.id.clone(), entry.name.clone()))
                .cloned()
                .unwrap_or_default();
            rows.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            (entry.name.clone(), rows)
        })
        .collect()
}

// -- the bounds' own algebra -------------------------------------------------

/// The magnitude a bound's label declares as a symmetric band, or `None`.
fn bound_band_half_width(bound: &Bound) -> Option<f64> {
    let found = bound_band_re().search(&bound.label)?;
    let half: f64 = found.group(1)?.parse().ok()?;
    Some(if found.group(2).is_some() {
        half / 100.0
    } else {
        half
    })
}

/// Whether a bound's own label declares it as a band around zero.
fn two_sided_bound(bound: &Bound) -> bool {
    let Some(half) = bound_band_half_width(bound) else {
        return false;
    };
    is_close(bound.y.abs(), half, 1e-9, 1e-12)
}

/// The bound lines a panel draws: every declared bound, plus a band's arms.
fn mirrored_bounds(bounds: &[Bound]) -> Vec<Bound> {
    let mut drawn = Vec::new();
    for bound in bounds {
        drawn.push(bound.clone());
        if !two_sided_bound(bound) {
            continue;
        }
        let mut mirror = bound.clone();
        mirror.y = -bound.y;
        mirror.label = bound.label.replace(BOUND_BAND_MARK, "-");
        mirror.band_arm = Some("lower".to_string());
        drawn.push(mirror);
    }
    drawn
}

fn bound_values(series: &Series) -> Vec<f64> {
    series
        .iter()
        .flat_map(|(_, points)| points.iter().map(|(_, value)| *value))
        .collect()
}

/// The quantity's unit (1.0) when every value and bound lies in `[0, 1]`.
fn unit_span(values: &[f64], bounds: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    if values.iter().all(|value| (0.0..=1.0).contains(value))
        && bounds.iter().all(|y| (0.0..=1.0).contains(y))
    {
        return Some(1.0);
    }
    None
}

/// `floor`/`cap` when every value is on one side of `y`, else `None`.
fn bound_side(values: &[f64], y: f64) -> Option<&'static str> {
    if values.is_empty() {
        return None;
    }
    if values.iter().all(|value| *value >= y) {
        return Some("floor");
    }
    if values.iter().all(|value| *value <= y) {
        return Some("cap");
    }
    None
}

/// The values beyond `y` that the panel reads as a departure.
fn crossing_values(values: &[f64], y: f64) -> Vec<f64> {
    if clustered_around(values, y) {
        return Vec::new();
    }
    let below: Vec<f64> = values.iter().copied().filter(|value| *value < y).collect();
    let above: Vec<f64> = values.iter().copied().filter(|value| *value > y).collect();
    let total = below.len() + above.len();
    if total == 0 {
        return Vec::new();
    }
    if (above.len() as f64) <= CROSSING_BULK_SHARE * total as f64 {
        return above;
    }
    if (below.len() as f64) <= CROSSING_BULK_SHARE * total as f64 {
        return below;
    }
    Vec::new()
}

/// Whether the values sit as a cluster around a bound in the middle of a unit.
fn clustered_around(values: &[f64], y: f64) -> bool {
    let Some(unit) = unit_span(values, &[y]) else {
        return false;
    };
    if unit - y <= FRAME_HEADROOM * unit {
        return false;
    }
    values
        .iter()
        .all(|value| (value - y).abs() <= MIN_UNIT_SPAN * unit)
}

/// The side of `y` on which a departure begins, or `0.0` when neither is.
fn failure_side(values: &[f64], y: f64) -> f64 {
    let crossing = crossing_values(values, y);
    if !crossing.is_empty() {
        return if crossing.iter().cloned().fold(f64::NEG_INFINITY, f64::max) > y {
            1.0
        } else {
            -1.0
        };
    }
    match bound_side(values, y) {
        Some("cap") => 1.0,
        Some("floor") => -1.0,
        _ => 0.0,
    }
}

/// The width of the region the axis must resolve for a one-sided bound.
fn bound_band(values: &[f64], y: f64, unit: Option<f64>, tolerances: &[f64]) -> f64 {
    let side = failure_side(values, y);
    if side != 0.0 {
        let mut tolerated: Vec<f64> = tolerances
            .iter()
            .filter(|tolerance| (*tolerance - y) * side > 0.0)
            .map(|tolerance| (tolerance - y).abs())
            .collect();
        tolerated.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        if let Some(first) = tolerated.first() {
            let floor = unit.map(|unit| MIN_UNIT_SPAN * unit).unwrap_or(0.0);
            return first.max(floor);
        }
    }
    let nearest = values
        .iter()
        .map(|value| (value - y).abs())
        .fold(f64::INFINITY, f64::min);
    let nearest = if values.is_empty() { 0.0 } else { nearest };
    nearest.max(unit.map(|unit| MIN_UNIT_SPAN * unit).unwrap_or(0.0))
}

/// Whether this bound is what the panel's axis has to resolve.
fn bound_is_the_scale(values: &[f64], y: f64, unit: Option<f64>) -> bool {
    let Some(unit) = unit else {
        return false;
    };
    if unit - y > FRAME_HEADROOM * unit {
        return false;
    }
    let reaching = values.iter().filter(|value| **value >= y).count();
    reaching * 2 > values.len()
}

// -- the run's own guards ----------------------------------------------------

/// Whether a parsed token is a real number rather than a bool or a string.
fn numeric(value: &J) -> bool {
    matches!(value, J::Int(_) | J::Float(_))
}

fn number_of(value: &J) -> Option<f64> {
    if numeric(value) { value.as_f64() } else { None }
}

/// `f"{value:g}"` over a JSON number, which converts an integer to a float.
fn json_g(value: &J) -> String {
    match value {
        J::Int(number) => fg(*number as f64),
        other => fg(other.as_f64().unwrap_or(0.0)),
    }
}

/// The run's own per-arm guards, from the `MANDATE` line's `*_guard` keys.
fn run_guards(run_values: Option<&J>, series: Option<&Series>, text: &str) -> Vec<(String, f64)> {
    let Some(values) = run_values.and_then(J::as_obj) else {
        return Vec::new();
    };
    let mut haystack: Vec<String> = series
        .map(|entries| entries.iter().map(|(name, _)| name.clone()).collect())
        .unwrap_or_default();
    haystack.push(text.to_string());
    let mut keys: Vec<&(String, J)> = values.iter().collect();
    keys.sort_by(|a, b| a.0.cmp(&b.0));
    let mut guards = Vec::new();
    for (key, value) in keys {
        if !key.ends_with(GUARD_KEY_SUFFIX) {
            continue;
        }
        let Some(number) = number_of(value) else {
            continue;
        };
        if series.is_some() {
            let stem = &key[..key.len() - GUARD_KEY_SUFFIX.len()];
            let token = stem.rsplit('_').next().unwrap_or("");
            if !token.is_empty() && !haystack.iter().any(|entry| entry.contains(token)) {
                continue;
            }
        }
        guards.push((key.clone(), number));
    }
    guards
}

/// The unit a guard key's own statistic name declares, or `""`.
fn guard_statistic_unit(statistic: &str) -> &'static str {
    if statistic.starts_with("over") {
        "%"
    } else {
        ""
    }
}

/// The run's own guards, attributed to the drawn arm each one bounds.
fn arm_guard_tokens(series: &Series, run_values: Option<&J>) -> Vec<ArmGuards> {
    let Some(values) = run_values.and_then(J::as_obj) else {
        return Vec::new();
    };
    let names: Vec<String> = series.iter().map(|(name, _)| name.clone()).collect();
    let mut found: Vec<ArmGuards> = names
        .iter()
        .map(|name| (name.clone(), Vec::new()))
        .collect();
    for (key, value) in values {
        if !key.ends_with(GUARD_KEY_SUFFIX) {
            continue;
        }
        let Some(number) = number_of(value) else {
            continue;
        };
        let stem = &key[..key.len() - GUARD_KEY_SUFFIX.len()];
        let parts: Vec<&str> = stem.split('_').collect();
        for take in (1..parts.len()).rev() {
            let candidate = parts[..take].join("_");
            let arm = names
                .iter()
                .find(|name| **name == candidate || name.starts_with(&format!("{candidate}_")));
            let Some(arm) = arm else {
                continue;
            };
            let statistic = if take < parts.len() {
                parts[take..].join("_")
            } else {
                candidate.clone()
            };
            if let Some(entry) = found.iter_mut().find(|(name, _)| name == arm) {
                entry.1.push((key.clone(), statistic, number));
            }
            break;
        }
    }
    found
        .into_iter()
        .filter(|(_, guards)| !guards.is_empty())
        .collect()
}

/// The clause a bound drawn across arms with different guards owes.
fn arm_guard_clause(series: &Series, run_values: Option<&J>) -> String {
    let tokens = arm_guard_tokens(series, run_values);
    if tokens.is_empty() {
        return String::new();
    }
    let guarded: Vec<String> = tokens.iter().map(|(name, _)| name.clone()).collect();
    let reference: Vec<String> = series
        .iter()
        .map(|(name, _)| name.clone())
        .filter(|name| !guarded.contains(name))
        .collect();
    let mut parts = Vec::new();
    if !reference.is_empty() {
        parts.push(format!("governs {}", reference.join(" ")));
    }
    for (name, guards) in &tokens {
        let listed: Vec<String> = guards
            .iter()
            .map(|(key, statistic, value)| {
                format!("{key}={}{}", fg(*value), guard_statistic_unit(statistic))
            })
            .collect();
        parts.push(format!("{name} guards {}", listed.join(", ")));
    }
    parts.join("; ")
}

/// The run's own arm names for a single-series panel, in the run's order.
fn run_arm_names(series: &Series, run_values: Option<&J>) -> Vec<String> {
    let Some(values) = run_values.and_then(J::as_obj) else {
        return Vec::new();
    };
    if series.len() != 1 {
        return Vec::new();
    }
    let quantity = series[0].0.as_str();
    let mut names: Vec<String> = Vec::new();
    for (key, _) in values {
        if !key.ends_with(&format!("_{quantity}")) {
            continue;
        }
        let name = &key[..key.len() - quantity.len() - 1];
        if !name.is_empty() && !names.iter().any(|existing| existing == name) {
            names.push(name.to_string());
        }
    }
    if names.len() >= 2 {
        return names;
    }
    let mut guarded: Vec<String> = Vec::new();
    for (key, _) in values {
        if !key.ends_with(GUARD_KEY_SUFFIX) {
            continue;
        }
        let stem = &key[..key.len() - GUARD_KEY_SUFFIX.len()];
        let (prefix, statistic) = match stem.rfind('_') {
            Some(index) => (&stem[..index], &stem[index + 1..]),
            None => ("", stem),
        };
        if !statistic.is_empty()
            && quantity.contains(statistic)
            && !guarded.iter().any(|existing| existing == prefix)
        {
            guarded.push(prefix.to_string());
        }
    }
    if guarded.len() >= 2 {
        guarded
    } else {
        Vec::new()
    }
}

/// The run's own guard for one arm's quantity, as `(key, value)`, or `None`.
fn per_arm_guard(arm: &str, quantity: &str, run_values: Option<&J>) -> Option<(String, f64)> {
    let values = run_values.and_then(J::as_obj)?;
    for (key, value) in values {
        if !key.ends_with(GUARD_KEY_SUFFIX) {
            continue;
        }
        let number = number_of(value)?;
        let stem = &key[..key.len() - GUARD_KEY_SUFFIX.len()];
        let Some(index) = stem.rfind('_') else {
            continue;
        };
        let (prefix, statistic) = (&stem[..index], &stem[index + 1..]);
        if !statistic.is_empty() && quantity.contains(statistic) && prefix == arm {
            return Some((key.clone(), number));
        }
    }
    None
}

/// The run's own key and value for one arm's bound on a quantity.
fn arm_bound_source(arm: &str, quantity: &str, run_values: Option<&J>) -> Option<(String, f64)> {
    let values = run_values.and_then(J::as_obj)?;
    for suffix in [PER_ARM_BOUND_SUFFIXES.0, PER_ARM_BOUND_SUFFIXES.1] {
        let key = format!("{arm}_{quantity}{suffix}");
        if let Some(value) = values
            .iter()
            .find(|(name, _)| *name == key)
            .and_then(|(_, value)| number_of(value))
        {
            return Some((key, value));
        }
    }
    per_arm_guard(arm, quantity, run_values)
}

/// The run's own unprefixed bound for a quantity, as `(key, value)`.
fn quantity_bound(quantity: &str, run_values: Option<&J>) -> Option<(String, f64)> {
    let values = run_values.and_then(J::as_obj)?;
    for suffix in [
        QUANTITY_BOUND_SUFFIXES.0,
        QUANTITY_BOUND_SUFFIXES.1,
        QUANTITY_BOUND_SUFFIXES.2,
    ] {
        let key = format!("{quantity}{suffix}");
        if let Some(value) = values
            .iter()
            .find(|(name, _)| *name == key)
            .and_then(|(_, value)| number_of(value))
        {
            return Some((key, value));
        }
    }
    None
}

/// The arms the run states a guard of its own for, whatever the quantity.
fn guarded_arms(arms: &[String], run_values: Option<&J>) -> Vec<String> {
    let Some(values) = run_values.and_then(J::as_obj) else {
        return Vec::new();
    };
    let mut guarded = Vec::new();
    for arm in arms {
        for (key, _) in values {
            if !key.ends_with(GUARD_KEY_SUFFIX) {
                continue;
            }
            if key[..key.len() - GUARD_KEY_SUFFIX.len()].starts_with(&format!("{arm}_")) {
                guarded.push(arm.clone());
                break;
            }
        }
    }
    guarded
}

/// Each drawn arm's own bound, or `None` when one bound serves them all.
fn arm_bound_values(
    panel: &Panel,
    series: &Series,
    bounds: &[Bound],
    run_values: Option<&J>,
) -> Option<Vec<(String, f64)>> {
    if panel.chart != Chart::Bar || bounds.len() != 1 || series.len() != 1 {
        return None;
    }
    let declared = bounds[0].y;
    let arms = run_arm_names(series, run_values);
    let mut categories: Vec<f64> = series[0].1.iter().map(|(x, _)| *x).collect();
    categories.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    categories.dedup();
    let expected: Vec<f64> = (0..arms.len()).map(|index| (index + 1) as f64).collect();
    if arms.len() < 2 || categories != expected {
        return None;
    }
    let quantity = series[0].0.as_str();
    let restated = quantity_bound(quantity, run_values);
    let own: Vec<(String, Option<(String, f64)>)> = arms
        .iter()
        .map(|arm| (arm.clone(), arm_bound_source(arm, quantity, run_values)))
        .collect();
    if restated.is_none() && own.iter().all(|(_, source)| source.is_none()) {
        return None;
    }
    let guarded = guarded_arms(&arms, run_values);
    if guarded.is_empty() {
        return None;
    }
    // The arms keep the run's own order, which is the order its bars are drawn
    // in: a sorted map here would reorder the segments a panel draws.
    let mut values = Vec::new();
    for (arm, source) in &own {
        let value = if let Some((_, value)) = source {
            *value
        } else if let Some((_, restated_value)) = &restated {
            if guarded.contains(arm) {
                *restated_value
            } else {
                declared
            }
        } else {
            declared
        };
        values.push((arm.clone(), value));
    }
    Some(values)
}

/// The bound lines a bar panel draws: one per contiguous run of equal values.
fn effective_bounds(
    panel: &Panel,
    series: &Series,
    bounds: &[Bound],
    run_values: Option<&J>,
) -> Vec<Bound> {
    let Some(values) = arm_bound_values(panel, series, bounds, run_values) else {
        return bounds.to_vec();
    };
    let arms: Vec<String> = values.iter().map(|(arm, _)| arm.clone()).collect();
    let value_of = |arm: &str| {
        values
            .iter()
            .find(|(name, _)| name == arm)
            .map(|(_, value)| *value)
            .expect("the arm was enumerated from this table")
    };
    let quantity = series[0].0.as_str();
    let restated = quantity_bound(quantity, run_values);
    let mut segments: Vec<Bound> = Vec::new();
    let mut start = 0;
    while start < arms.len() {
        let value = value_of(&arms[start]);
        let mut end = start + 1;
        while end < arms.len() && value_of(&arms[end]) == value {
            end += 1;
        }
        let run: Vec<String> = arms[start..end].to_vec();
        let mut source = arm_bound_source(&run[0], quantity, run_values);
        if source.is_none() && restated.is_some() {
            source = restated.clone();
        }
        let label = if value == bounds[0].y {
            bounds[0].label.clone()
        } else {
            match &source {
                Some((key, source_value)) => format!("run {key}={}", fg(*source_value)),
                None => bounds[0].label.clone(),
            }
        };
        let mut segment = Bound::new(value, label);
        segment.arms = Some(run);
        segment.window = Some((start..end).map(|index| (index + 1) as f64).collect());
        if let Some((key, _)) = &source
            && key.ends_with(GUARD_KEY_SUFFIX)
        {
            segment.guard_key = Some(key.clone());
        }
        segments.push(segment);
        start = end;
    }
    let declared_value = bounds[0].y;
    if segments.iter().all(|segment| segment.y != declared_value) {
        let mut segment = Bound::new(declared_value, bounds[0].label.clone());
        segment.arms = Some(Vec::new());
        segment.window = Some(vec![1.0, arms.len() as f64]);
        segment.governs_none = true;
        segments.push(segment);
    }
    segments
}

/// Tag a bar panel's bound plan where the run's guards are drawn as lines.
fn with_drawn_guards(mut plan: Vec<Bound>) -> Vec<Bound> {
    if plan.iter().any(|bound| bound.guard_key.is_some()) {
        for bound in plan.iter_mut() {
            bound.guards_drawn = true;
        }
    }
    plan
}

/// The bound lines a bar panel owes the run's per-*series* guards.
fn series_guard_bounds(
    panel: &Panel,
    series: &Series,
    bounds: &[Bound],
    run_values: Option<&J>,
) -> Vec<Bound> {
    if panel.chart != Chart::Bar || bounds.len() != 1 {
        return Vec::new();
    }
    let names: Vec<String> = series.iter().map(|(name, _)| name.clone()).collect();
    let declared = bounds[0].y;
    let mut extra = Vec::new();
    for (key, value) in run_guards(run_values, Some(series), &bounds[0].label) {
        let owner = key[..key.len() - GUARD_KEY_SUFFIX.len()].to_string();
        if !names.contains(&owner) || value == declared {
            continue;
        }
        let mut bound = Bound::new(value, format!("run {key}={}", fg(value)));
        bound.series = Some(owner);
        bound.guard_key = Some(key);
        extra.push(bound);
    }
    extra
}

/// The bound lines a bar panel draws: its effective bounds, mirrored.
fn drawable_bounds(
    panel: &Panel,
    series: &Series,
    bounds: &[Bound],
    run_values: Option<&J>,
) -> Vec<Bound> {
    mirrored_bounds(&bar_bound_plan(panel, series, bounds, run_values))
}

/// The un-mirrored bound lines a bar panel draws, as the run's own plan.
fn bar_bound_plan(
    panel: &Panel,
    series: &Series,
    bounds: &[Bound],
    run_values: Option<&J>,
) -> Vec<Bound> {
    let plan = effective_bounds(panel, series, bounds, run_values);
    let already: Vec<f64> = plan
        .iter()
        .filter(|bound| bound.guard_key.is_some())
        .map(|bound| bound.y)
        .collect();
    let mut plan = plan;
    for bound in series_guard_bounds(panel, series, bounds, run_values) {
        if !already.contains(&bound.y) {
            plan.push(bound);
        }
    }
    with_drawn_guards(plan)
}

/// Why a drawn bound line is on the panel, as one token.
fn bound_reason(bound: &Bound) -> &'static str {
    if bound.band_arm.is_some() {
        return "declaration-band";
    }
    if bound.guard_key.is_some() {
        return "run-guard";
    }
    if bound.series.is_some() {
        return "series-guard";
    }
    // An empty `arms` list is falsy in Python, which is how the unclaimed
    // declaration's segment is told from an arm's own.
    if bound.arms.as_ref().is_some_and(|arms| !arms.is_empty()) {
        return "run-arm";
    }
    if bound.governs_none {
        return "declaration-unclaimed";
    }
    "declaration"
}

// -- the run's own vocabulary for a panel's axes -----------------------------

/// The run's repetition count when a panel draws exactly its `1..reps`.
fn repeated_category_index(categories: &[f64], run_values: Option<&J>) -> Option<i64> {
    let values = run_values.and_then(J::as_obj)?;
    let reps = values
        .iter()
        .find(|(name, _)| name == "reps")
        .map(|(_, value)| value)?;
    let reps = match reps {
        J::Int(number) => *number,
        _ => return None,
    };
    if reps < 2 {
        return None;
    }
    let mut unique: Vec<f64> = categories.to_vec();
    unique.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    unique.dedup();
    let expected: Vec<f64> = (1..=reps).map(|index| index as f64).collect();
    if unique != expected {
        return None;
    }
    Some(reps)
}

/// The x label a panel draws: its own, or the run's own vocabulary.
fn panel_x_label_for(
    panel: &Panel,
    mandate_x_label: &str,
    categories: &[f64],
    run_values: Option<&J>,
) -> String {
    if let Some(label) = &panel.x_label {
        return label.clone();
    }
    if let Some(reps) = repeated_category_index(categories, run_values) {
        return format!("rep (1..{reps})");
    }
    mandate_x_label.to_string()
}

/// The y label a panel draws: its own, or its single series' own quantity.
fn panel_y_label_for(panel: &Panel, mandate_y_label: &str, series: &Series) -> String {
    if let Some(label) = &panel.y_label {
        return label.clone();
    }
    if series.len() == 1 {
        return series_label(&series[0].0);
    }
    mandate_y_label.to_string()
}

/// A y tick, with a value that rounds to zero printed without its sign.
fn tick_label(value: f64, decimals: usize) -> String {
    let label = fdec(value, decimals);
    if label.parse::<f64>() == Ok(0.0) {
        label.trim_start_matches('-').to_string()
    } else {
        label
    }
}

/// The human label a series is drawn with, never its raw column name.
pub fn series_label(name: &str) -> String {
    for (raw, human) in SERIES_LABEL_VOCABULARY {
        if name == *raw {
            return (*human).to_string();
        }
    }
    if raw_column_name_re().match_at(name).is_some() {
        return name.replace('_', " ");
    }
    name.to_string()
}

/// The unit a panel's own axis label declares, or `""`.
fn panel_unit(label: &str) -> String {
    match panel_unit_re().search(label) {
        Some(found) => found.group(1).unwrap_or_default().trim().to_string(),
        None => String::new(),
    }
}

/// The value a drawn series reads at `x`, off the segment it draws there.
fn value_at(points: &[(f64, f64)], x: f64) -> Option<f64> {
    let first = points.first()?;
    if x <= first.0 {
        return Some(first.1);
    }
    let last = points.last()?;
    if x >= last.0 {
        return Some(last.1);
    }
    for pair in points.windows(2) {
        let (x0, y0) = pair[0];
        let (x1, y1) = pair[1];
        if x0 <= x && x <= x1 {
            if x1 == x0 {
                return Some(y1);
            }
            return Some(y0 + (y1 - y0) * (x - x0) / (x1 - x0));
        }
    }
    Some(last.1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_band_mark_is_read_from_a_label_and_measured_against_its_value() {
        let mut bound = Bound::new(0.01, "fair-share bound ±1.0%".to_string());
        assert_eq!(bound_band_half_width(&bound), Some(0.01));
        assert!(two_sided_bound(&bound));
        // Vacuity: a label whose declared half-width is *not* its value is not
        // a two-sided bound, so the equality is what makes the mirror derivable
        // rather than assumed.
        bound.y = 0.02;
        assert!(!two_sided_bound(&bound));
        assert!(bound_band_half_width(&bound).is_some());
    }

    #[test]
    fn a_mirrored_band_draws_both_arms_and_marks_the_lower_one() {
        let bounds = vec![Bound::new(0.01, "fair-share bound ±1.0%".to_string())];
        let drawn = mirrored_bounds(&bounds);
        assert_eq!(drawn.len(), 2);
        assert_eq!(drawn[1].y, -0.01);
        assert_eq!(drawn[1].label, "fair-share bound -1.0%");
        assert_eq!(drawn[1].band_arm.as_deref(), Some("lower"));
        // A one-sided bound is a no-op.
        let plain = mirrored_bounds(&[Bound::new(250.0, "M1 ceiling 250 ms".to_string())]);
        assert_eq!(plain.len(), 1);
    }

    #[test]
    fn a_bound_a_minority_crosses_is_a_departure_and_a_split_is_a_target() {
        // One bar past the ceiling is a crossing; a fair share the bars split
        // around is not, so the numbers decide and not the declaration.
        let lopsided = vec![20.0, 24.0, 26.0, 900.0];
        assert_eq!(crossing_values(&lopsided, 250.0), vec![900.0]);
        let split = vec![0.249, 0.2501, 0.2502, 0.2499];
        assert!(crossing_values(&split, 0.25).is_empty());
        // The clustered case is the two-sided band: every value within the
        // unit's own resolution of the bound.
        let clustered = vec![0.24999, 0.250001, 0.250002];
        assert!(crossing_values(&clustered, 0.25).is_empty());
    }

    #[test]
    fn a_guard_is_named_on_a_bound_and_a_normalised_rung_guard_is_tagged() {
        let run = pyjson::parse(
            r#"{"hostile_p99": 117.9, "hostile_p99_guard": 900, "lone_p99_guard": 3200,
                "lone_over250_guard": 8, "reps": 3}"#,
        )
        .expect("parses");
        let series: Series = vec![("hostile p99".to_string(), vec![(1.0, 117.9)])];
        let guards = run_guards(Some(&run), Some(&series), "M1 ceiling 250 ms");
        // The guard's own *statistic* token (`p99`) has to appear in a drawn
        // series name, so the p99 guard is named. `lone_over250_guard`'s token
        // is `over250`, which no series here carries, so it is not.
        let keys: Vec<String> = guards.iter().map(|(key, _)| key.clone()).collect();
        // `lone_p99_guard`'s token is the same `p99`, so it is named too: the
        // label lists the run's guards *for the quantity this panel draws*,
        // whichever arm they belong to.
        assert_eq!(
            keys,
            vec![
                "hostile_p99_guard".to_string(),
                "lone_p99_guard".to_string()
            ]
        );
        // Vacuity: on a panel whose series are the arms rather than the
        // statistics, the same key is not named -- the quantity a guard binds
        // is not on that panel's axis.
        let arms: Series = vec![("hostile".to_string(), vec![(1.0, 117.9)])];
        assert!(run_guards(Some(&run), Some(&arms), "M1 ceiling 250 ms").is_empty());
        assert_eq!(guard_statistic_unit("over250"), "%");
        assert_eq!(guard_statistic_unit("p99"), "");
    }

    #[test]
    fn the_runs_reps_are_the_panels_categories_only_when_they_are_the_same() {
        let run = pyjson::parse(r#"{"reps": 3}"#).expect("parses");
        assert_eq!(
            repeated_category_index(&[1.0, 2.0, 3.0], Some(&run)),
            Some(3)
        );
        assert_eq!(repeated_category_index(&[1.0, 2.0], Some(&run)), None);
    }
}
