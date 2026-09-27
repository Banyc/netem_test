//! `netem-tools` — the harness's tool subcommands, one implementation each.
//!
//! The perf tooling is migrating from Python to Rust one subcommand at a time,
//! and this binary is where the ported subcommands live. Its first subcommand
//! is `mandate-compare`, the multi-axis coverage/claim comparison the Python
//! tooling provided before the migration to Rust.
//!
//! ```text
//! netem-tools mandate-compare [report] [--baseline <path>] [--json-out <path>]
//! ```
//!
//! Run `netem-tools mandate-compare --help` for the flags. The command line is
//! parsed with clap's `derive` API: a subcommand dispatcher, and each
//! subcommand's flags a struct. The crate's `cli` feature carries clap, and both
//! binaries require that feature, so a plain library build — and every sibling
//! crate that consumes this one as a dev-dependency — never fetches or builds a
//! parser the library does not call.

use std::path::PathBuf;

use clap::{Parser, Subcommand};

use netem_test::tools::mandate_compare::{self, Args};

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

fn main() {
    let cli = Cli::parse();
    let status = match cli.command {
        Tool::MandateCompare(args) => mandate_compare::main(args.into()),
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

    #[test]
    fn mandate_compare_binds_every_flag_to_the_comparison_options() {
        let cli = parse(&[
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
        let Tool::MandateCompare(args) = cli.command;
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
        let cli = parse(&["mandate-compare"]);
        let Tool::MandateCompare(args) = cli.command;
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
        let cli = parse(&["mandate-compare", "--count-tolerance", "-1"]);
        let Tool::MandateCompare(args) = cli.command;
        let args: Args = args.into();
        assert_eq!(args.count_tolerance, -1.0);
    }

    #[test]
    fn an_unknown_subcommand_or_flag_is_a_usage_error() {
        assert!(Cli::try_parse_from(["netem-tools", "bogus"]).is_err());
        assert!(Cli::try_parse_from(["netem-tools", "mandate-compare", "--bogus"]).is_err());
    }
}
