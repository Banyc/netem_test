//! `netem-tools` — the harness's tool subcommands, one implementation each.
//!
//! The perf tooling is migrating from Python to Rust one subcommand at a time,
//! and this binary is where the ported subcommands live. Its first subcommand
//! is `mandate-compare`, the multi-axis coverage/claim comparison the Python
//! tooling provided before the migration to Rust.
//!
//! ```text
//! netem-tools mandate-compare [report] [--baseline <path>] [--json-out <path>]
//! netem-tools mandate-plot <declaration.json> --out <dir> [--no-rasterize]
//! ```
//!
//! Run `netem-tools <subcommand> --help` for the flags. The command line is
//! parsed with clap's `derive` API: a subcommand dispatcher, and each
//! subcommand's flags a struct. The crate's `cli` feature carries clap, and both
//! binaries require that feature, so a plain library build — and every sibling
//! crate that consumes this one as a dev-dependency — never fetches or builds a
//! parser the library does not call.

use std::path::PathBuf;

use clap::{Parser, Subcommand};

use netem_test::tools::mandate_compare::{self, Args};
use netem_test::tools::mandate_plot::{self, Args as PlotArgs};
use netem_test::tools::pyformat;

#[derive(Debug, Parser)]
#[command(
    name = "netem-tools",
    about = "the harness's tool subcommands, one implementation each",
    disable_help_subcommand = true
)]
struct Cli {
    #[command(subcommand)]
    command: Tool,
}

#[derive(Debug, Subcommand)]
enum Tool {
    /// diff a mandate-check run against the committed baseline
    MandateCompare(MandateCompare),
    /// apply a Python `format()` spec to a value (the port's prerequisite)
    PyFormat(PyFormat),
    /// render and verify a mandate's declared performance panels
    MandatePlot(MandatePlot),
}

// `py-format` has two faces: a single call (`--value`/`--spec`) for a human,
// and a streaming `kind TAB value TAB spec` stdin face for the corpus
// differential, so one process formats hundreds of thousands of pairs.
#[derive(Debug, clap::Args)]
struct PyFormat {
    /// format this value instead of reading stdin (a Python `repr()`)
    #[arg(long, value_name = "repr", allow_hyphen_values = true)]
    value: Option<String>,
    /// the value's kind: f (float), i (int), s (str)
    #[arg(long, value_name = "kind", default_value = "f")]
    kind: String,
    /// the format spec (the part after the `:`), e.g. `.4g`
    #[arg(
        long,
        value_name = "spec",
        default_value = "",
        allow_hyphen_values = true
    )]
    spec: String,
}

// The flags of `mandate-compare`, converted into the comparison's own `Args`.
// The comparison's defaults are named here rather than restated, so an omitted
// flag and the library's default cannot drift apart. A doc comment would become
// the subcommand's long help, and the flag docs below already say what a caller
// needs, so the rationale stays a plain comment.
#[derive(Debug, clap::Args)]
struct MandateCompare {
    /// the report to compare (default: mandate-check.json)
    #[arg(value_name = "report")]
    report: Option<PathBuf>,
    /// the committed baseline report
    #[arg(long, value_name = "path")]
    baseline: Option<PathBuf>,
    /// relative fall in a count that is coverage loss
    #[arg(
        long,
        value_name = "f",
        default_value_t = mandate_compare::DEFAULT_COUNT_TOLERANCE,
        allow_negative_numbers = true
    )]
    count_tolerance: f64,
    /// relative fall in a measured window
    #[arg(
        long,
        value_name = "f",
        default_value_t = mandate_compare::DEFAULT_WINDOW_TOLERANCE,
        allow_negative_numbers = true
    )]
    window_tolerance: f64,
    /// absolute fall in the delivery ratio
    #[arg(
        long,
        value_name = "f",
        default_value_t = mandate_compare::DEFAULT_DELIVERY_TOLERANCE,
        allow_negative_numbers = true
    )]
    delivery_tolerance: f64,
    /// relative statistic movement reported as drift
    #[arg(
        long,
        value_name = "f",
        default_value_t = mandate_compare::DEFAULT_VALUE_TOLERANCE,
        allow_negative_numbers = true
    )]
    value_tolerance: f64,
    /// exit 5 when a statistic moved past the tolerance
    #[arg(long)]
    fail_on_value_drift: bool,
    /// write the diff as JSON as well as printing it
    #[arg(long, value_name = "path")]
    json_out: Option<PathBuf>,
}

impl From<MandateCompare> for Args {
    fn from(cli: MandateCompare) -> Args {
        Args {
            report: cli.report,
            baseline: cli.baseline,
            count_tolerance: cli.count_tolerance,
            window_tolerance: cli.window_tolerance,
            delivery_tolerance: cli.delivery_tolerance,
            value_tolerance: cli.value_tolerance,
            fail_on_value_drift: cli.fail_on_value_drift,
            json_out: cli.json_out,
        }
    }
}

// The flags of `mandate-plot`, converted into the plotter's own `Args`. The
// defaults are named here rather than restated: `--rasterize` is on unless
// `--no-rasterize` is given, which is the Python tool's own pair of flags and
// its own default, because a run that silently skipped the PNG step would be an
// evidence failure rather than a saving.
#[derive(Debug, clap::Args)]
struct MandatePlot {
    /// the mandate's .json panel declaration; its sibling .csv carries the data
    #[arg(value_name = "declaration")]
    declaration: PathBuf,
    /// directory the panel SVGs (and PNGs) are written into
    #[arg(long, value_name = "dir")]
    out: PathBuf,
    /// verify and write SVGs only; do not attempt the external PNG step
    #[arg(long)]
    no_rasterize: bool,
    /// headless browser executable or name (default: $NETEM_RENDER_BROWSER)
    #[arg(long, value_name = "path")]
    browser: Option<String>,
    /// this run's MANDATE measurements (a JSON object or a file of one)
    #[arg(long, value_name = "JSON|PATH", allow_hyphen_values = true)]
    run_values: Option<String>,
    /// this run's per-arm censoring readings (a JSON object or a file of one)
    #[arg(long, value_name = "JSON|PATH", allow_hyphen_values = true)]
    run_censoring: Option<String>,
    /// this run's MANDATE_SMOKE_FAULT selector, when the run took one
    #[arg(long, value_name = "NAME")]
    fault: Option<String>,
    /// print a JSON summary of the produced panels instead of the text one
    #[arg(long)]
    json: bool,
}

impl From<MandatePlot> for PlotArgs {
    fn from(cli: MandatePlot) -> PlotArgs {
        PlotArgs {
            declaration: cli.declaration,
            out: cli.out,
            rasterize: !cli.no_rasterize,
            browser: cli.browser,
            run_values: cli.run_values,
            run_censoring: cli.run_censoring,
            fault: cli.fault,
            json: cli.json,
        }
    }
}

fn main() {
    let cli = Cli::parse();
    let status = match cli.command {
        Tool::MandateCompare(args) => mandate_compare::main(args.into()),
        Tool::PyFormat(args) => pyformat::main(pyformat::Args {
            value: args.value,
            kind: args.kind,
            spec: Some(args.spec),
        }),
        Tool::MandatePlot(args) => {
            let json = args.json;
            match mandate_plot::render_mandate(&args.into()) {
                Ok(summary) => {
                    mandate_plot::print_summary(&summary, json);
                    0
                }
                Err(error) => {
                    eprintln!("mandate_plot: error: {error}");
                    1
                }
            }
        }
    };
    std::process::exit(status);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(argv: &[&str]) -> Cli {
        let mut full = vec!["netem-tools"];
        full.extend_from_slice(argv);
        Cli::try_parse_from(full).expect("the arguments parse")
    }

    /// `netem-tools mandate-compare ...`'s flags, for the tests below. The
    /// `Tool` enum gained a second variant when `py-format` landed, so the
    /// binding is a `match` rather than a single-variant `let`.
    fn mandate_compare(argv: &[&str]) -> MandateCompare {
        match parse(argv).command {
            Tool::MandateCompare(args) => args,
            other => panic!("expected mandate-compare, got {other:?}"),
        }
    }

    #[test]
    fn mandate_compare_binds_every_flag_to_the_comparison_options() {
        let args = mandate_compare(&[
            "mandate-compare",
            "candidate.json",
            "--baseline",
            "baseline.json",
            "--count-tolerance",
            "0.25",
            "--window-tolerance",
            "0.02",
            "--delivery-tolerance",
            "0.01",
            "--value-tolerance",
            "0.6",
            "--fail-on-value-drift",
            "--json-out",
            "diff.json",
        ]);
        let args: Args = args.into();
        assert_eq!(args.report, Some(PathBuf::from("candidate.json")));
        assert_eq!(args.baseline, Some(PathBuf::from("baseline.json")));
        assert_eq!(args.count_tolerance, 0.25);
        assert_eq!(args.window_tolerance, 0.02);
        assert_eq!(args.delivery_tolerance, 0.01);
        assert_eq!(args.value_tolerance, 0.6);
        assert!(args.fail_on_value_drift);
        assert_eq!(args.json_out, Some(PathBuf::from("diff.json")));
    }

    #[test]
    fn an_omitted_flag_takes_the_comparison_default() {
        let args = mandate_compare(&["mandate-compare"]);
        let args: Args = args.into();
        let defaults = Args::default();
        assert_eq!(args.report, None);
        assert_eq!(args.baseline, None);
        assert_eq!(args.count_tolerance, defaults.count_tolerance);
        assert_eq!(args.window_tolerance, defaults.window_tolerance);
        assert_eq!(args.delivery_tolerance, defaults.delivery_tolerance);
        assert_eq!(args.value_tolerance, defaults.value_tolerance);
        assert!(!args.fail_on_value_drift);
        assert_eq!(args.json_out, None);
    }

    #[test]
    fn a_negative_tolerance_reaches_the_comparison_which_refuses_it() {
        // The comparison validates tolerances (a negative one is an error), so
        // the parser must bind `-1` as a value rather than reject it as an
        // unknown flag: the hand-rolled parser passed it through, and the
        // black-box suite asserts the refusal.
        let parsed = mandate_compare(&["mandate-compare", "--count-tolerance", "-1"]);
        let args: Args = parsed.into();
        assert_eq!(args.count_tolerance, -1.0);
    }

    #[test]
    fn py_format_binds_the_value_kind_and_spec() {
        let cli = parse(&["py-format", "--value", "12345.6789", "--spec", ".4g"]);
        let Tool::PyFormat(args) = cli.command else {
            panic!("expected the py-format subcommand");
        };
        let args = netem_test::tools::pyformat::Args {
            value: args.value,
            kind: args.kind,
            spec: Some(args.spec),
        };
        assert_eq!(args.value.as_deref(), Some("12345.6789"));
        assert_eq!(args.kind, "f");
        assert_eq!(args.spec.as_deref(), Some(".4g"));
    }

    #[test]
    fn an_unknown_subcommand_or_flag_is_a_usage_error() {
        assert!(Cli::try_parse_from(["netem-tools", "bogus"]).is_err());
        assert!(Cli::try_parse_from(["netem-tools", "mandate-compare", "--bogus"]).is_err());
    }
}
