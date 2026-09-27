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
//! Run `netem-tools mandate-compare --help` for the flags.

use std::env;

use netem_test::tools::mandate_compare;

fn usage() -> String {
    [
        "netem-tools <subcommand> [options]",
        "",
        "Subcommands:",
        "  mandate-compare   diff a mandate-check run against the committed baseline",
        "",
        "Run `netem-tools <subcommand> --help` for a subcommand's flags.",
    ]
    .join("\n")
}

fn main() {
    let argv: Vec<String> = env::args().skip(1).collect();
    let Some(subcommand) = argv.first() else {
        eprintln!("netem-tools: error: no subcommand given");
        eprintln!("{}", usage());
        std::process::exit(2);
    };
    let rest = &argv[1..];
    let status = match subcommand.as_str() {
        "mandate-compare" => mandate_compare::main(rest),
        other => {
            eprintln!("netem-tools: error: unknown subcommand {other:?}");
            eprintln!("{}", usage());
            2
        }
    };
    std::process::exit(status);
}
