//! The perf-test dual mandate's conformance cases, ported from
//! `CheckGatePerfTest` in the (now deleted) `tools/test_check_gate.py` suite.
//!
//! Every case writes a fixture `GATE.md` (and, where the case needs one, a
//! report) and asserts the checker's exit code and its diagnostic. A case that
//! cannot fail is not coverage, so each refusal the checker claims has a row
//! here that triggers it.

mod check_gate_common;

use std::time::{Duration, SystemTime};

use check_gate_common::{BASE_BUDGETS, BASE_DESIGN, Fixture, GateOptions, Plan, Timing};

/// One named family whose two rows share the `conformance-alpha` cell name.
const MEMBER_DESIGN: &str =
    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none
alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms
alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt
lib::tests::probe = perf | 0.3 | orthogonal@delay | conformance-alpha@impairment=none";
const MEMBER_BUDGETS: &str = "default = 30
perf = 30
baseline = beta::t_beta
drift = 0.5
drift_floor_s = 2.0
baseline.delay = alpha::t_ok
members.delay = conformance-alpha";

fn write(fixture: &Fixture, options: GateOptions) {
    fixture.write_gate(options);
}

fn output_of(outcome: &check_gate_common::Outcome) -> String {
    format!("{}{}", outcome.stdout, outcome.stderr)
}

fn assert_has(outcome: &check_gate_common::Outcome, fragments: &[&str]) {
    let output = output_of(outcome);
    for fragment in fragments {
        assert!(
            output.contains(fragment),
            "missing {fragment:?} in:\n{output}"
        );
    }
}

fn assert_lacks(outcome: &check_gate_common::Outcome, fragment: &str) {
    let output = output_of(outcome);
    assert!(
        !output.contains(fragment),
        "unexpected {fragment:?} in:\n{output}"
    );
}

// -- the reserved `lib` target ----------------------------------------------

#[test]
fn a_lib_entry_in_the_manifest_resolves() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            manifest: "alpha::t_ig = perf\nlib::tests::probe = perf".to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &["lib target: 1 ignored scenario(s), 1 named in gate-manifest"],
    );
    assert_lacks(&outcome, "STALE manifest entry");
}

#[test]
fn a_lib_opt_in_a_perf_design_row_names_is_not_called_unclassified() {
    let fixture = Fixture::new();
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_lacks(&outcome, "unclassified ignored lib scenario");
    assert_has(
        &outcome,
        &["lib target: 1 ignored scenario(s), 1 named in gate-manifest or a gate-perf-design row"],
    );
}

#[test]
fn an_undeclared_lib_opt_in_is_reported_by_name() {
    let fixture = Fixture::new();
    let design = BASE_DESIGN
        .lines()
        .filter(|line| !line.contains("lib::"))
        .collect::<Vec<_>>()
        .join("\n");
    write(
        &fixture,
        GateOptions {
            design: Some(design),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "note: unclassified ignored lib scenario lib::tests::probe",
            "lib target: 1 ignored scenario(s), 0 named",
        ],
    );
}

#[test]
fn a_lib_entry_in_gate_default_required_resolves() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            required: "alpha::t_ok\nlib::tests::t_lib_default".to_string(),
            asserting: "alpha::t_ok\nlib::tests::t_lib_default".to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &["default-required: 2 asserting scenario(s) present"],
    );
}

// -- the zero-row (gap-only) declaration ------------------------------------

#[test]
fn a_zero_row_gap_only_declaration_passes() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(String::new()),
            budgets: String::new(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &["gate-perf-design: 0 perf test row(s)", "baseline unset"],
    );
}

#[test]
fn a_zero_row_declaration_with_no_gap_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(String::new()),
            budgets: String::new(),
            gaps: String::new(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("declares no row and gate-coverage-gaps records no gap");
}

#[test]
fn a_zero_row_declaration_with_a_baseline_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(String::new()),
            budgets: "baseline = beta::t_beta".to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("declares baseline 'beta::t_beta' while gate-perf-design declares no row");
}

#[test]
fn a_zero_row_declaration_still_refuses_a_reason_less_gap() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(String::new()),
            budgets: String::new(),
            gaps: "M1@lane=dual-lane = ".to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("records no reason");
}

#[test]
fn a_lib_entry_that_is_not_an_ignored_test_is_still_stale() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            manifest: "alpha::t_ig = perf\nlib::tests::probe = perf\nlib::tests::t_nope = perf"
                .to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("STALE manifest entry (no longer ignored): lib::tests::t_nope");
}

#[test]
fn a_declared_lib_package_resolves_the_reserved_target() {
    let mut fixture = Fixture::new();
    std::fs::create_dir_all(fixture.root.join("other").join("src")).expect("mkdir");
    std::fs::write(
        fixture.root.join("other").join("src").join("lib.rs"),
        check_gate_common::LIB_RS,
    )
    .expect("write");
    let mut plan = Plan::base();
    plan.remove("tests", "lib");
    plan.set("other", "lib", &["tests::t_lib_default"], &["tests::probe"]);
    fixture.write_plan(plan);
    write(
        &fixture,
        GateOptions {
            lib_package: Some("other".to_string()),
            required: "alpha::t_ok\nlib::tests::t_lib_default".to_string(),
            asserting: "alpha::t_ok\nlib::tests::t_lib_default".to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_lacks(&outcome, "does not report this test");
    assert_has(
        &outcome,
        &[
            "default-required: 2 asserting scenario(s) present",
            "gate-perf-design: 4 perf test row(s)",
        ],
    );
}

#[test]
fn a_declared_lib_package_that_lacks_the_test_fails() {
    let fixture = Fixture::new();
    std::fs::create_dir_all(fixture.root.join("other").join("src")).expect("mkdir");
    std::fs::write(
        fixture.root.join("other").join("src").join("lib.rs"),
        check_gate_common::LIB_RS,
    )
    .expect("write");
    write(
        &fixture,
        GateOptions {
            lib_package: Some("other".to_string()),
            ..GateOptions::default()
        },
    );
    fixture.rejects(
        "PERF DECLARATION: gate-perf-design row lib::tests::probe: the 'lib' target does not \
         report this test",
    );
}

#[test]
fn a_gate_lib_package_block_naming_no_package_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            lib_package: Some(String::new()),
            ..GateOptions::default()
        },
    );
    fixture.rejects("must name exactly one package, found 0");
}

#[test]
fn a_gate_lib_package_block_naming_two_packages_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            lib_package: Some("other\nnetem-test".to_string()),
            ..GateOptions::default()
        },
    );
    fixture.rejects("must name exactly one package, found 2");
}

#[test]
fn a_gate_lib_package_block_naming_an_invalid_package_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            lib_package: Some("not a package".to_string()),
            ..GateOptions::default()
        },
    );
    fixture.rejects("which is not a cargo package name");
}

#[test]
fn declaration_without_perf_blocks_still_passes() {
    let fixture = Fixture::new();
    std::fs::write(
        fixture.root.join("tests").join("GATE.md"),
        "# no perf blocks\n\n```gate-manifest\nalpha::t_ig = perf\n```\n\n\
         ```gate-default-required\nalpha::t_ok\n```\n\n\
         ```gate-asserting\nalpha::t_ok\n```\n\n\
         ```gate-perf-guard-helpers\n```\n",
    )
    .expect("write");
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["PENDING (advisory, not a failure)"]);
}

// -- the six failure modes ---------------------------------------------------

#[test]
fn unknown_test_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                BASE_DESIGN.replace("beta::t_beta = default", "beta::t_missing = default"),
            ),
            ..GateOptions::default()
        },
    );
    fixture.rejects("does not report this test");
}

#[test]
fn unknown_target_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("beta::t_beta = default", "gamma::t_beta = default")),
            ..GateOptions::default()
        },
    );
    fixture.rejects("does not report this test");
}

#[test]
fn test_in_the_wrong_tier_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("alpha::t_ok = default |", "alpha::t_ok = perf |")),
            ..GateOptions::default()
        },
    );
    fixture.rejects("declares tier 'perf' but the test set puts it in 'default'");
}

#[test]
fn wrong_tier_for_an_ignored_scenario_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("alpha::t_ig = perf |", "alpha::t_ig = standard |")),
            ..GateOptions::default()
        },
    );
    fixture.rejects("declares tier 'standard' but the test set puts it in 'perf'");
}

#[test]
fn over_budget_tier_sum_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            budgets: BASE_BUDGETS.replace("default = 30", "default = 0.2"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("over its 0.20s budget");
    assert_has(&outcome, &["declares 0.50s in the default tier"]);
}

#[test]
fn missing_tier_budget_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            budgets: "default = 30\nbaseline = beta::t_beta".to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("gate-budgets declares no budget for it");
}

#[test]
fn empty_coverage_cell_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("conformance-beta@impairment=none", "")),
            ..GateOptions::default()
        },
    );
    fixture.rejects("no coverage cell");
}

#[test]
fn malformed_coverage_cell_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "conformance-beta@impairment=none",
                "conformance-beta@impairment=",
            )),
            ..GateOptions::default()
        },
    );
    fixture.rejects("is malformed");
}

#[test]
fn gap_without_a_reason_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            gaps: "M1@lane=dual-lane =".to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("records no reason");
}

#[test]
fn malformed_gap_cell_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            gaps: "just-a-word = covered nowhere".to_string(),
            ..GateOptions::default()
        },
    );
    fixture.rejects("is malformed");
}

#[test]
fn measured_declared_drift_fails() {
    let fixture = Fixture::new();
    fixture.write_report(&[Timing::new("beta", "t_beta", 30.0)], "mandate-check/2");
    let outcome = fixture.rejects_with(
        "measured/declared drift for beta::t_beta",
        Some(fixture.report_path.clone()),
    );
    assert_has(&outcome, &["declared 0.40s, measured 30.00s"]);
}

// -- the report-side behaviour ----------------------------------------------

#[test]
fn a_row_measured_over_its_tier_budget_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "beta::t_beta = default | 0.4",
                "beta::t_beta = default | 25",
            )),
            ..GateOptions::default()
        },
    );
    fixture.write_report(&[Timing::new("beta", "t_beta", 31.0)], "mandate-check/2");
    let outcome = fixture.rejects_with(
        "over its default tier budget",
        Some(fixture.report_path.clone()),
    );
    assert_has(&outcome, &["measured 31.00s"]);
}

#[test]
fn drift_inside_the_tolerance_passes() {
    let fixture = Fixture::new();
    fixture.write_report(&[Timing::new("beta", "t_beta", 0.5)], "mandate-check/2");
    let outcome = fixture.check_with(Some(fixture.report_path.clone()));
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["drift compared 1 of 4 declared row(s)"]);
}

#[test]
fn a_stale_report_is_noted_not_failed() {
    let fixture = Fixture::new();
    fixture.write_report(&[Timing::new("beta", "t_beta", 30.0)], "mandate-check/2");
    let stale = SystemTime::now() - Duration::from_secs(3600);
    std::fs::OpenOptions::new()
        .write(true)
        .open(&fixture.report_path)
        .expect("open the report")
        .set_modified(stale)
        .expect("set the mtime");
    let outcome = fixture.check_with(Some(fixture.report_path.clone()));
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["predates"]);
}

#[test]
fn a_report_without_per_test_timings_is_noted_not_failed() {
    let fixture = Fixture::new();
    fixture.write_report_raw("{\"schema\": \"mandate-check/1\"}");
    let outcome = fixture.check_with(Some(fixture.report_path.clone()));
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["carries no per-test timings"]);
}

#[test]
fn a_missing_report_named_explicitly_fails() {
    let fixture = Fixture::new();
    fixture.rejects_with("does not exist", Some(fixture.report_path.clone()));
}

#[test]
fn a_report_with_an_unknown_schema_fails() {
    let fixture = Fixture::new();
    fixture.write_report(&[], "something-else/1");
    fixture.rejects_with(
        "not a mandate-check report",
        Some(fixture.report_path.clone()),
    );
}

#[test]
fn a_report_that_is_not_an_object_fails() {
    let fixture = Fixture::new();
    fixture.write_report_raw("[1, 2, 3]");
    fixture.rejects_with("is not a JSON object", Some(fixture.report_path.clone()));
}

// -- the relation to the baseline -------------------------------------------

#[test]
fn an_unlabelled_multi_dimension_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
                "alpha::t_ok = default | 0.1 | conformance-alpha@lane=loopback+layer=link",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("alpha::t_ok: it declares no relation to the baseline");
    assert_has(
        &outcome,
        &["its cells vary 2 dimension(s), so write `composite(lane,layer)`"],
    );
}

#[test]
fn a_row_differing_in_no_dimension_without_a_re_measurement_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat)",
                "lib::tests::probe = perf | 0.3 | orthogonal",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture
        .rejects("lib::tests::probe: its cells name no dimension that differs from the baseline");
    assert_has(&outcome, &["write `re-measurement(<reason>)`"]);
}

#[test]
fn an_unlabelled_row_differing_in_no_dimension_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | \
                 probe-runner@impairment=none",
                "lib::tests::probe = perf | 0.3 | probe-runner@impairment=none",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("lib::tests::probe: it declares no relation to the baseline");
    assert_has(
        &outcome,
        &["its cells vary 0 dimension(s), so write `re-measurement(<reason>)`"],
    );
}

#[test]
fn an_ambiguous_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "probe-alpha@metric=throughput+scale=200-pkt",
                "probe-alpha@metric=throughput+metric=latency",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("alpha::t_ig: its relation to the baseline cannot be determined");
    assert_has(
        &outcome,
        &[
            "states 'metric' as both 'throughput' and 'latency'",
            "the dimensions it varies are ambiguous",
        ],
    );
}

#[test]
fn a_baseline_that_is_not_one_point_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "conformance-beta@impairment=none",
                "conformance-beta@impairment=none+impairment=delay20ms",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("baseline row beta::t_beta: its cells are not one point");
    assert_has(&outcome, &["no row's relation to it can be determined"]);
}

#[test]
fn a_composite_label_that_misnames_the_dimensions_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("composite(metric,scale)", "composite(metric,lane)")),
            ..GateOptions::default()
        },
    );
    fixture.rejects(
        "alpha::t_ig: it is labelled composite(metric,lane), but its cells vary metric, scale",
    );
}

#[test]
fn a_composite_label_on_a_one_dimension_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | composite(lane,layer)",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture
        .rejects("alpha::t_ok: it varies exactly one dimension from the baseline (impairment)");
    assert_has(&outcome, &["write `orthogonal`, not `composite`"]);
}

#[test]
fn a_re_measurement_label_on_a_multi_dimension_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ig = perf | 0.2 | composite(metric,scale)",
                "alpha::t_ig = perf | 0.2 | re-measurement(second-tier-repeat)",
            )),
            ..GateOptions::default()
        },
    );
    fixture.rejects(
        "alpha::t_ig: it is labelled a re-measurement, but its cells vary 2 dimension(s) from \
         the baseline (metric, scale)",
    );
}

#[test]
fn the_baseline_row_must_be_labelled_baseline() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "beta::t_beta = default | 0.4 | baseline |",
                "beta::t_beta = default | 0.4 | orthogonal |",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture
        .rejects("beta::t_beta: it is the gate-budgets baseline, so its relation is the reference");
    assert_has(&outcome, &["write `baseline`"]);
}

#[test]
fn a_non_baseline_row_may_not_be_labelled_baseline() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | baseline",
            )),
            ..GateOptions::default()
        },
    );
    let outcome =
        fixture.rejects("alpha::t_ok: it is labelled `baseline`, but the baseline is beta::t_beta");
    assert_has(&outcome, &["so write `orthogonal`"]);
}

#[test]
fn a_re_measurement_without_a_reason_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                BASE_DESIGN.replace("re-measurement(second-tier-repeat)", "re-measurement()"),
            ),
            ..GateOptions::default()
        },
    );
    fixture.rejects("re-measurement(<reason>)` names no reason");
}

#[test]
fn a_composite_relation_naming_one_dimension_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace("composite(metric,scale)", "composite(metric)")),
            ..GateOptions::default()
        },
    );
    fixture.rejects("`composite(...)` must name at least two dimensions");
}

#[test]
fn an_unknown_relation_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | mostly-orthogonal",
            )),
            ..GateOptions::default()
        },
    );
    fixture.rejects("is not a relation this grammar knows");
}

#[test]
fn a_row_without_a_relation_field_reports_it_as_a_field_count() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none",
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none | extra",
            )),
            ..GateOptions::default()
        },
    );
    fixture.rejects("found 5 field(s)");
}

#[test]
fn a_row_covering_two_cells_of_one_dimension_is_orthogonal() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "orthogonal | conformance-alpha@impairment=delay20ms",
                "orthogonal | conformance-alpha@impairment=delay20ms,conformance-alpha@impairment=delay25ms",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["1 orthogonal"]);
}

#[test]
fn a_row_whose_cells_vary_different_dimensions_is_composite() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "orthogonal | conformance-alpha@impairment=delay20ms",
                "composite(impairment,scale) | conformance-alpha@impairment=delay20ms,conformance-alpha@scale=128-pkt",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(&outcome, &["alpha::t_ok varies impairment, scale"]);
}

// -- the rest of the declaration --------------------------------------------

#[test]
fn budgets_without_a_design_block_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: None,
            ..GateOptions::default()
        },
    );
    fixture.rejects("gate-perf-design is missing while");
}

#[test]
fn baseline_naming_no_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            budgets: BASE_BUDGETS.replace("baseline = beta::t_beta", "baseline = beta::t_nope"),
            ..GateOptions::default()
        },
    );
    fixture.rejects("baseline 'beta::t_nope' is not a gate-perf-design row");
}

#[test]
fn a_budget_block_without_a_baseline_fails() {
    let fixture = Fixture::new();
    let budgets = BASE_BUDGETS
        .lines()
        .filter(|line| !line.contains("baseline"))
        .collect::<Vec<_>>()
        .join("\n");
    write(
        &fixture,
        GateOptions {
            budgets,
            ..GateOptions::default()
        },
    );
    fixture.rejects("declares no 'baseline = <row>' line");
}

#[test]
fn a_malformed_budget_line_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            budgets: format!("{BASE_BUDGETS}\nnonsense = fast"),
            ..GateOptions::default()
        },
    );
    fixture.rejects("is neither a tier nor one of baseline/drift/drift_floor_s");
}

// -- several named baselines ------------------------------------------------

#[test]
fn a_row_naming_an_undeclared_baseline_family_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | orthogonal@nope",
            )),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "alpha::t_ok: it names the baseline family 'nope', which gate-budgets does not declare",
    );
    assert_has(&outcome, &["declare `baseline.nope = <row>`"]);
}

#[test]
fn a_named_baseline_naming_no_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            budgets: format!("{BASE_BUDGETS}\nbaseline.ghost = alpha::t_missing"),
            ..GateOptions::default()
        },
    );
    fixture.rejects("baseline.ghost names 'alpha::t_missing', which is not a gate-perf-design row");
}

#[test]
fn a_declared_baseline_no_row_uses_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | baseline@widow | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | probe-runner@impairment=none"
                    .to_string(),
            ),
            budgets: format!("{BASE_BUDGETS}\nbaseline.widow = alpha::t_ig"),
            ..GateOptions::default()
        },
    );
    fixture.rejects(
        "baseline.widow = alpha::t_ig is declared but no row states a relation against it",
    );
}

#[test]
fn a_row_labelled_the_wrong_familys_baseline_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@fam-a | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | baseline@fam-b | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | orthogonal@fam-a | probe-runner@metric=throughput"
                    .to_string(),
            ),
            budgets: format!(
                "{BASE_BUDGETS}\nbaseline.fam-a = alpha::t_ig\nbaseline.fam-b = alpha::t_ok"
            ),
            ..GateOptions::default()
        },
    );
    let outcome = fixture
        .rejects("alpha::t_ok: it is labelled `baseline@fam-a`, but baseline.fam-a is alpha::t_ig");
    assert_has(&outcome, &["a baseline label names only the family"]);
}

#[test]
fn a_named_familys_reference_must_carry_its_family_label() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms",
                "alpha::t_ok = default | 0.1 | baseline | conformance-alpha@impairment=delay20ms",
            )),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    let outcome =
        fixture.rejects("alpha::t_ok: it is the reference row of baseline.delay (alpha::t_ok)");
    assert_has(&outcome, &["write `baseline@delay`"]);
}

#[test]
fn a_row_that_is_two_families_reference_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(BASE_DESIGN.replace(
                "alpha::t_ok = default | 0.1 | orthogonal",
                "alpha::t_ok = default | 0.1 | baseline@fam-a",
            )),
            budgets: format!(
                "{BASE_BUDGETS}\nbaseline.fam-a = alpha::t_ok\nbaseline.fam-b = alpha::t_ok"
            ),
            ..GateOptions::default()
        },
    );
    fixture.rejects("alpha::t_ok' is the reference row of baseline.fam-a, baseline.fam-b");
}

#[test]
fn a_named_family_is_derived_against_its_own_baseline() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | orthogonal@delay | conformance-alpha@impairment=none"
                    .to_string(),
            ),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance-alpha"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-perf-relations: 1 orthogonal, 1 composite, 0 re-measurement, 2 baseline of 4 \
             row(s) across 2 baseline(s)",
            "gate-perf-family: delay(alpha::t_ok) 1 orthogonal, 0 composite, 0 re-measurement, 1 baseline",
            "gate-perf-composite: alpha::t_ig varies metric, scale against beta::t_beta",
            "gate-perf-namespace: delay(conformance-alpha) 2 row(s), cells: conformance-alpha",
            "gate-perf-membership: 1 cell-name namespace(s) declared, 1 cell name(s) claimed, \
             0 row(s) stated outside the namespace of the family they name",
        ],
    );
}

#[test]
fn a_member_of_a_named_family_is_refused_the_wrong_label() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | oriented@delay | probe-runner@impairment=none"
                    .to_string(),
            ),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    fixture.rejects("lib::tests::probe: relation 'oriented@delay' is not a relation");
}

#[test]
fn a_composite_label_mismatching_its_own_family_baseline_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | composite(metric,lane)@delay | probe-runner@impairment=other+transport=std-udp"
                    .to_string(),
            ),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("lib::tests::probe: it is labelled composite(metric,lane)@delay");
    assert_has(&outcome, &["its cells vary impairment, transport"]);
}

#[test]
fn a_one_dimension_member_of_a_named_family_must_be_orthogonal() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | composite(metric,lane)@delay | probe-runner@impairment=none"
                    .to_string(),
            ),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "lib::tests::probe: it varies exactly one dimension from baseline 'delay' (impairment)",
    );
    assert_has(&outcome, &["so write `orthogonal@delay`, not `composite`"]);
}

// -- family membership -------------------------------------------------------

#[test]
fn a_named_family_without_a_namespace_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-budgets: baseline.delay declares a family whose cell-name namespace is not declared",
    );
    assert_has(&outcome, &["write `members.delay = conformance-alpha`"]);
}

#[test]
fn a_family_whose_cells_share_no_prefix_says_so() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none",
                "probe-runner@impairment=none",
            )),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture
        .rejects("baseline.delay declares a family whose cell-name namespace is not declared");
    assert_has(
        &outcome,
        &["its rows' cells are named conformance-alpha, probe-runner and share no prefix"],
    );
}

#[test]
fn a_namespace_for_an_undeclared_family_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{MEMBER_BUDGETS}\nmembers.ghost = probe-*"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-budgets: members.ghost = probe-* declares the cell-name namespace of a family \
         gate-budgets does not declare",
    );
    assert_has(
        &outcome,
        &["declare `baseline.ghost = <row>` or remove the line"],
    );
}

#[test]
fn a_malformed_namespace_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!(
                "{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance alpha"
            ),
            ..GateOptions::default()
        },
    );
    let outcome =
        fixture.rejects("members.delay = 'conformance alpha' does not name a cell-name namespace");
    assert_has(
        &outcome,
        &["optionally followed by '*' to make it the prefix"],
    );
}

#[test]
fn a_bare_members_line_declares_no_family() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{MEMBER_BUDGETS}\nmembers = conformance-*"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects("'members' names no family");
    assert_has(
        &outcome,
        &[
            "the default family's namespace is the residual",
            "write `members.<family> = <prefix>`",
        ],
    );
}

#[test]
fn a_duplicate_namespace_for_a_family_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{MEMBER_BUDGETS}\nmembers.delay = conformance-alpha*"),
            ..GateOptions::default()
        },
    );
    fixture.rejects("duplicate members line for family 'delay'");
}

#[test]
fn a_namespace_matching_no_row_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok\nmembers.delay = zzz-*"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "members.delay = zzz-* matches no row's cell name, so it declares a namespace nothing \
         occupies",
    );
    assert_has(
        &outcome,
        &["write the prefix the family's cells carry (conformance-alpha)"],
    );
}

#[test]
fn a_namespace_not_covering_its_own_family_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none",
                "probe-runner@impairment=none",
            )),
            budgets: MEMBER_BUDGETS.to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "members.delay = conformance-alpha does not cover the family's own cells (probe-runner)",
    );
    assert_has(&outcome, &["write `members.delay = <prefix>`"]);
}

#[test]
fn a_reference_row_outside_its_familys_namespace_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none",
                "probe-runner@impairment=none",
            )),
            budgets: format!(
                "{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok\nmembers.delay = probe-runner"
            ),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-perf-design row alpha::t_ok is the reference of baseline.delay, but its cells are \
         named conformance-alpha, which members.delay = probe-runner does not claim",
    );
    assert_has(
        &outcome,
        &["the family's namespace must contain its own reference"],
    );
}

#[test]
fn a_row_outside_its_familys_namespace_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.replace(
                "conformance-alpha@impairment=none",
                "probe-runner@impairment=none",
            )),
            budgets: MEMBER_BUDGETS.to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-perf-design row lib::tests::probe: its cells are named probe-runner, which \
         members.delay = conformance-alpha does not claim",
    );
    assert_has(
        &outcome,
        &["declare it as this family's own cell name: `members.delay = <prefix>`"],
    );
}

#[test]
fn a_row_whose_cell_name_belongs_to_another_family_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(
                "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none\n\
                 alpha::t_ok = default | 0.1 | baseline@delay | conformance-alpha@impairment=delay20ms\n\
                 alpha::t_ig = perf | 0.2 | baseline@other | probe-alpha@metric=throughput+scale=200-pkt\n\
                 lib::tests::probe = perf | 0.3 | orthogonal@delay | probe-alpha@impairment=none"
                    .to_string(),
            ),
            budgets: format!(
                "{BASE_BUDGETS}\nbaseline.delay = alpha::t_ok\nmembers.delay = conformance-alpha\n\
                 baseline.other = alpha::t_ig\nmembers.other = probe-alpha"
            ),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-perf-design row lib::tests::probe: its cells are named probe-alpha, which \
         members.delay = conformance-alpha does not claim",
    );
    assert_has(
        &outcome,
        &[
            "those cells belong to family 'other' (members.other = probe-alpha), so state the row \
             against `@other` or move the name out",
        ],
    );
}

#[test]
fn a_cell_name_claimed_by_two_families_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: format!("{MEMBER_BUDGETS}\nmembers.other = conformance-*"),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-budgets: the cell name 'conformance-alpha' is claimed by 2 families \
         (members.delay = conformance-alpha, members.other = conformance-*)",
    );
    assert_has(
        &outcome,
        &[
            "a cell name belongs to exactly one family",
            "gate-perf-design row alpha::t_ok: its cells are named conformance-alpha, which 2 \
             families claim",
        ],
    );
}

#[test]
fn a_default_row_inside_a_named_namespace_fails() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: MEMBER_BUDGETS.replace(
                "members.delay = conformance-alpha",
                "members.delay = conformance-*",
            ),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.rejects(
        "gate-perf-design row beta::t_beta names no family, but its cells are named \
         conformance-beta, which belong to family 'delay'",
    );
    assert_has(&outcome, &["write `@delay`"]);
}

#[test]
fn the_default_familys_residual_namespace_is_a_derivation() {
    let fixture = Fixture::new();
    write(
        &fixture,
        GateOptions {
            design: Some(MEMBER_DESIGN.to_string()),
            budgets: MEMBER_BUDGETS.to_string(),
            ..GateOptions::default()
        },
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-perf-namespace: default(residual) 2 row(s), cells: conformance-beta, probe-alpha",
            "gate-perf-membership: 1 cell-name namespace(s) declared, 1 cell name(s) claimed, \
             0 row(s) stated outside the namespace of the family they name",
        ],
    );
}
