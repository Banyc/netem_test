//! The gate checker's own conformance cases, ported from the (now deleted)
//! `tools/test_check_gate.py` suite.
//!
//! Each case drives the ported checker in-process against the fixture crate
//! root in `check_gate_common`; a case that cannot fail is not coverage, so
//! every refusal the checker claims has a case here that triggers it.

mod check_gate_common;

use check_gate_common::{Fixture, GateOptions, Plan};

#[test]
fn a_well_formed_declaration_passes_and_prints_its_panels() {
    let fixture = Fixture::new();
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}{}", outcome.stdout, outcome.stderr);
    for expected in [
        "gate-perf-design: 4 perf test row(s)",
        "4 coverage cell(s)",
        "gate-budgets: default 0.50/30.00s",
        "gate-budgets: perf 0.50/30.00s",
        "gate-perf-relations: 1 orthogonal, 1 composite, 1 re-measurement, 1 baseline of 4 row(s), stated against beta::t_beta",
        "gate-perf-composite: alpha::t_ig varies metric, scale",
    ] {
        assert!(
            outcome.stdout.contains(expected),
            "missing {expected:?} in:\n{}",
            outcome.stdout
        );
    }
}

#[test]
fn an_unknown_test_is_refused() {
    let fixture = Fixture::new();
    fixture.write_gate(GateOptions {
        design: Some(
            check_gate_common::BASE_DESIGN
                .replace("beta::t_beta = default", "beta::t_missing = default"),
        ),
        ..GateOptions::default()
    });
    fixture.rejects("does not report this test");
}

#[test]
fn an_unknown_target_is_refused() {
    let fixture = Fixture::new();
    fixture.write_gate(GateOptions {
        design: Some(
            check_gate_common::BASE_DESIGN
                .replace("beta::t_beta = default", "gamma::t_beta = default"),
        ),
        ..GateOptions::default()
    });
    fixture.rejects("does not report this test");
}

#[test]
fn a_directory_holding_no_target_is_resolved_rather_than_blamed() {
    let fixture = Fixture::new();
    let outcome = fixture.check_dir("tests", None);
    assert_eq!(outcome.exit, 0, "{}{}", outcome.stdout, outcome.stderr);
    assert!(!outcome.stdout.contains("STALE manifest entry"));
    assert!(outcome.stdout.contains("holds no `*.rs` test target"));
    assert!(
        outcome
            .stdout
            .contains("resolved from cargo's own target list")
    );
    assert!(
        outcome
            .stdout
            .contains("gate manifest OK: 1 ignored scenarios classified")
    );
}

#[test]
fn a_directory_holding_some_of_the_targets_fails() {
    let mut fixture = Fixture::new();
    let mut plan = Plan::base();
    plan.metadata = Some(vec![(
        "tests".to_string(),
        vec![
            (
                "alpha".to_string(),
                "{root}/tests/tests/alpha.rs".to_string(),
            ),
            ("beta".to_string(), "{root}/tests/tests/beta.rs".to_string()),
            (
                "gamma".to_string(),
                "{root}/tests/tests/gamma.rs".to_string(),
            ),
        ],
    )]);
    fixture.write_plan(plan);
    let outcome = fixture.check_dir("tests/tests", None);
    assert_ne!(outcome.exit, 0);
    let output = format!("{}{}", outcome.stdout, outcome.stderr);
    assert!(output.contains("SCENARIO DIRECTORY"), "{output}");
    assert!(output.contains("also compiles gamma"), "{output}");
}

#[test]
fn targets_under_more_than_one_directory_are_an_error() {
    let mut fixture = Fixture::new();
    let mut plan = Plan::base();
    plan.metadata = Some(vec![(
        "tests".to_string(),
        vec![
            (
                "alpha".to_string(),
                "{root}/tests/tests/alpha.rs".to_string(),
            ),
            ("beta".to_string(), "{root}/tests/tests/beta.rs".to_string()),
            (
                "delta".to_string(),
                "{root}/tests/elsewhere/delta.rs".to_string(),
            ),
        ],
    )]);
    fixture.write_plan(plan);
    let outcome = fixture.check_dir("tests", None);
    assert_ne!(outcome.exit, 0);
    let output = format!("{}{}", outcome.stdout, outcome.stderr);
    assert!(
        output.contains("compiles its targets under 2 directories"),
        "{output}"
    );
}
