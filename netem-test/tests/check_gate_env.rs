//! The env-scaled opt-in surface's conformance cases, ported from the
//! `CheckGateEnvTier*` classes of the (now deleted) `tools/test_check_gate.py` suite.
//!
//! Every other block keys on `#[ignore]`, so a tier scaled by an environment
//! variable and run by a script appears in none of them. These cases pin both
//! halves of the detection (a script names it, a source reads it) and both
//! directions of the enforcement.

mod check_gate_common;

use check_gate_common::{EnvFixture, Plan, WAKE_KNOB_RS};

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

const CHURN: &str = "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py \
                     | per-dial loss rate under a sized load | fixture-liveness@shape=churn";

// -- the closing diagnostic names the failure that fired ---------------------

#[test]
fn an_env_tier_only_failure_names_the_env_tier_and_not_the_manifest() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_GHOST | local/run_env.py \
         | per-dial loss rate | fixture-liveness@shape=churn",
    );
    let outcome = fixture.check_split();
    assert_ne!(outcome.exit, 0, "{}", output_of(&outcome));
    assert!(
        outcome.stdout.contains(
            "ENV TIER: gate-env-tier surface fixture-churn: FIXTURE_GHOST is passed to no \
             env-reading function"
        ),
        "{}",
        outcome.stdout
    );
    assert!(
        !outcome.stderr.contains("manifest has"),
        "the manifest did not fail, so its count line must not fire:\n{}",
        outcome.stderr
    );
    assert!(outcome.stderr.contains("ENV TIER"), "{}", outcome.stderr);
    assert!(
        outcome.stderr.contains(
            &fixture
                .root
                .join("tests")
                .join("GATE.md")
                .display()
                .to_string()
        ),
        "{}",
        outcome.stderr
    );
}

#[test]
fn a_manifest_failure_gives_the_manifest_diagnostic() {
    let mut fixture = EnvFixture::new();
    fixture.declare(CHURN);
    let mut plan = Plan::base();
    plan.set("tests", "alpha", &["t_ok"], &["t_ig", "t_ig2"]);
    fixture.replace_plan(plan);
    let outcome = fixture.check_split();
    assert_ne!(outcome.exit, 0, "{}", output_of(&outcome));
    assert!(
        outcome
            .stdout
            .contains("UNCLASSIFIED ignored scenario: alpha::t_ig2"),
        "{}",
        outcome.stdout
    );
    assert!(
        outcome
            .stderr
            .contains("manifest has 1 entries, binaries report 2"),
        "{}",
        outcome.stderr
    );
    assert!(
        !outcome.stderr.contains("ENV TIER"),
        "the env surface is declared here, so its symptom must not fire:\n{}",
        outcome.stderr
    );
}

// -- detection and enforcement ----------------------------------------------

#[test]
fn an_undeclared_surface_is_named_rather_than_invisible() {
    let fixture = EnvFixture::new();
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "env-scaled opt-in surface undeclared",
            "FIXTURE_ITERATIONS",
            "local/run_env.py",
            "src/env_knob.rs",
        ],
    );
}

#[test]
fn a_well_formed_surface_passes_and_is_summarised() {
    let fixture = EnvFixture::new();
    fixture.declare(CHURN);
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier: 1 env-scaled surface(s)",
            "gate-env-tier-surface: fixture-churn",
        ],
    );
    assert_lacks(&outcome, "env-scaled opt-in surface undeclared");
}

#[test]
fn a_surface_the_declaration_omits_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS | local/run_env.py | per-dial loss rate under a \
         sized load | fixture-liveness@shape=churn",
    );
    fixture.rejects(
        "FIXTURE_ROUNDS is set by local/run_env.py and read by this crate's sources, so it \
         scales an opt-in tier",
    );
}

#[test]
fn a_declared_variable_no_source_reads_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_GHOST | local/run_env.py \
         | per-dial loss rate | fixture-liveness@shape=churn",
    );
    fixture.rejects("fixture-churn: FIXTURE_GHOST is passed to no env-reading function");
}

#[test]
fn a_runner_naming_no_declared_variable_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | tools/other.py | per-dial loss \
         rate | fixture-liveness@shape=churn",
    );
    fixture.write_source("tools/other.py", "print('nothing')\n");
    fixture.rejects("runner 'tools/other.py' names none of the declared variables");
}

#[test]
fn a_row_with_no_runner_field_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | | per-dial loss rate under a \
         sized load | fixture-liveness@shape=churn",
    );
    fixture.rejects(
        "no runner field; state the script that runs the surface, or '-' for a surface no \
         script runs",
    );
}

#[test]
fn a_row_with_three_fields_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS | local/run_env.py | per-dial loss rate under a \
         sized load",
    );
    fixture.rejects("got 3 field(s)");
}

#[test]
fn a_runner_that_is_not_a_file_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/absent.py | per-dial loss \
         rate | fixture-liveness@shape=churn",
    );
    fixture.rejects("runner 'local/absent.py' is not a file under");
}

#[test]
fn a_repeated_variable_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ITERATIONS | local/run_env.py | per-dial \
         loss rate | fixture-liveness@shape=churn",
    );
    fixture.rejects("FIXTURE_ITERATIONS named more than once");
}

#[test]
fn a_malformed_variable_name_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = fixture_iterations | local/run_env.py | per-dial loss rate | \
         fixture-liveness@shape=churn",
    );
    fixture.rejects("is not an environment variable name");
}

#[test]
fn a_surface_with_no_coverage_cell_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py | per-dial loss \
         rate | ",
    );
    fixture.rejects("covers no cell");
}

#[test]
fn a_surface_that_measures_nothing_fails() {
    let fixture = EnvFixture::new();
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py | | \
         fixture-liveness@shape=churn",
    );
    fixture.rejects("says nothing about what it measures");
}

// -- the half of a surface no script can show --------------------------------

#[test]
fn a_variable_only_the_sources_read_must_be_declared() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_WAKE_BOUND is read by src/wake_knob.rs and set by no script");
}

#[test]
fn a_declared_scriptless_variable_passes() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_WAKE_BOUND | \
         local/run_env.py | per-dial loss rate and the handover wake bound | \
         fixture-liveness@shape=churn, fixture-wake@shape=handover",
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier: 1 env-scaled surface(s), 3 variable(s)",
            "FIXTURE_WAKE_BOUND",
        ],
    );
}

#[test]
fn the_note_names_a_surface_no_script_sets() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "FIXTURE_WAKE_BOUND",
            "read by src/env_knob.rs, src/wake_knob.rs",
        ],
    );
}

#[test]
fn the_note_names_a_surface_no_script_sets_at_all() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    std::fs::remove_file(fixture.root.join("local").join("run_env.py")).expect("unlink");
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "FIXTURE_ITERATIONS, FIXTURE_ROUNDS, FIXTURE_WAKE_BOUND (named by no script and read \
             by src/env_knob.rs, src/wake_knob.rs)",
        ],
    );
}

#[test]
fn a_surface_no_script_runs_is_declared_with_the_marker() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(
        "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py | per-dial loss \
         rate under a sized load | fixture-liveness@shape=churn\n\
         fixture-wake = FIXTURE_WAKE_BOUND | - | the handover wake bound the sources read \
         in-process | fixture-wake@shape=handover",
    );
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier: 2 env-scaled surface(s), 3 variable(s)",
            "fixture-wake (no script runner) FIXTURE_WAKE_BOUND",
        ],
    );
}

#[test]
fn a_marked_surface_whose_variables_a_script_sets_fails() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(
        "fixture-wake = FIXTURE_ITERATIONS,FIXTURE_WAKE_BOUND | - | the handover wake bound \
         the sources read in-process | fixture-wake@shape=handover",
    );
    fixture
        .rejects("'-' states that no script runs it, but local/run_env.py sets FIXTURE_ITERATIONS");
}

// -- the load shape ----------------------------------------------------------

const LOAD_CHURN: &str = "fixture-churn = FIXTURE_ITERATIONS,FIXTURE_ROUNDS,FIXTURE_WAKE_BOUND \
                          | local/run_env.py | per-dial loss rate under a sized load \
                          | fixture-liveness@shape=churn";

#[test]
fn a_load_shape_is_recorded_and_evaluated() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_ITERATIONS,FIXTURE_ROUNDS | local/run_env.py | \
         per-dial loss rate at a stated cost | fixture-liveness@shape=churn | \
         FIXTURE_ITERATIONS=100,FIXTURE_ROUNDS=8,\
         total=FIXTURE_ITERATIONS*FIXTURE_ROUNDS,wall=2.5s,bound=3e-4/dial"
    ));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier-load: fixture-load FIXTURE_ITERATIONS*FIXTURE_ROUNDS = 800, 2.5s, \
             bound 3e-4/dial",
        ],
    );
}

#[test]
fn a_load_whose_total_is_not_arithmetic_fails() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py | per-dial loss \
         rate at a stated cost | fixture-liveness@shape=churn | \
         FIXTURE_ITERATIONS=100,total=2*,wall=2.5s"
    ));
    fixture.rejects("load total=2* is not arithmetic over the named variables");
}

#[test]
fn a_load_total_no_variable_produces_fails() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py | per-dial loss \
         rate at a stated cost | fixture-liveness@shape=churn | \
         FIXTURE_ITERATIONS=100,total=800,wall=2.5s"
    ));
    fixture.rejects("load total=800 yields a count from no variable");
}

#[test]
fn a_load_with_no_wall_clock_fails() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py | per-dial loss \
         rate at a stated cost | fixture-liveness@shape=churn | \
         FIXTURE_ITERATIONS=100,total=FIXTURE_ITERATIONS*8"
    ));
    fixture.rejects("load wall=None is not a positive duration");
}

#[test]
fn a_load_sizing_a_variable_the_runner_does_not_set_fails() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_WAKE_BOUND | local/run_env.py | the wake bound \
         at a stated cost | fixture-wake@shape=handover | \
         FIXTURE_WAKE_BOUND=50,total=FIXTURE_WAKE_BOUND*2,wall=0.5s"
    ));
    fixture.rejects(
        "its load sizes FIXTURE_WAKE_BOUND, which the runner 'local/run_env.py' does not set",
    );
}

#[test]
fn a_row_with_a_sixth_field_is_refused() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/wake_knob.rs", WAKE_KNOB_RS);
    fixture.declare(&format!(
        "{LOAD_CHURN}\nfixture-load = FIXTURE_ITERATIONS | local/run_env.py | the load at a \
         stated cost | fixture-liveness@shape=churn | \
         FIXTURE_ITERATIONS=100,total=FIXTURE_ITERATIONS*8,wall=0.5s | extra"
    ));
    fixture.rejects("got 6 field(s)");
}

// -- the direct literal, the mention and the toolchain name ------------------

const DIRECT_KNOB_RS: &str = r##"//! `env::var("FIXTURE_COMMENTED")` is prose about a read, not a read.
/* and `env::var("FIXTURE_BLOCK")` is prose in a block comment. */

fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

fn hops(name: &str, default: u64) -> u64 {
    env_parse(name, default)
}

pub fn forwarded() -> u64 {
    hops("FIXTURE_FORWARDED", 16)
}

pub fn direct_cycles() -> u64 {
    std::env::var("FIXTURE_DIRECT_CYCLES")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1500)
}

pub fn direct_dist_dir() -> Option<std::ffi::OsString> {
    std::env::var_os("FIXTURE_DIST_DIR")
}

static MODULE_SCOPE: std::sync::LazyLock<Option<String>> =
    std::sync::LazyLock::new(|| std::env::var("FIXTURE_MODULE_SCOPE").ok());

static TARGET: std::sync::LazyLock<Option<String>> =
    std::sync::LazyLock::new(|| std::env::var("CARGO_TARGET_DIR").ok());

const DOC: &str = "env::var(\"FIXTURE_ESCAPED\")";
const RAW: &str = r#"env::var("FIXTURE_RAW")"#;
"##;

const DIRECT_REAL: &str = "fixture-direct = FIXTURE_DIRECT_CYCLES,FIXTURE_DIST_DIR,\
                           FIXTURE_FORWARDED,FIXTURE_MODULE_SCOPE | - | the load the sources \
                           read in-process | fixture-liveness@shape=direct";
const DIRECT_MENTIONS: &str = "fixture-mentions = FIXTURE_BLOCK,FIXTURE_COMMENTED,\
                               FIXTURE_ESCAPED,FIXTURE_RAW | - | the names only a comment or a \
                               string constant mentions | fixture-liveness@shape=mention";

#[test]
fn the_direct_literal_and_the_var_os_literal_are_reads() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(&format!("{CHURN}\n{DIRECT_REAL}"));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier: 2 env-scaled surface(s), 6 variable(s)",
            "FIXTURE_DIRECT_CYCLES, FIXTURE_DIST_DIR",
        ],
    );
}

#[test]
fn a_direct_literal_the_declaration_omits_is_refused() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_DIRECT_CYCLES is read by src/direct_knob.rs and set by no script");
}

#[test]
fn the_reader_closure_still_reaches_a_two_hop_wrapper() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_FORWARDED is read by src/direct_knob.rs and set by no script");
}

#[test]
fn a_name_only_a_mention_names_is_still_stale() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(&format!("{CHURN}\n{DIRECT_REAL}\n{DIRECT_MENTIONS}"));
    fixture.rejects(
        "surface fixture-mentions: FIXTURE_BLOCK, FIXTURE_COMMENTED, FIXTURE_ESCAPED, \
         FIXTURE_RAW is passed to no env-reading function of this crate; a declared variable the \
         crate never reads is a stale declaration",
    );
}

#[test]
fn a_toolchain_name_is_not_demanded_of_a_surface() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(&format!("{CHURN}\n{DIRECT_REAL}"));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_lacks(&outcome, "CARGO_TARGET_DIR");
}

#[test]
fn a_toolchain_name_is_not_a_declarable_variable() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/direct_knob.rs", DIRECT_KNOB_RS);
    fixture.declare(&format!(
        "{CHURN}\n{DIRECT_REAL}\nfixture-runner-env = CARGO_TARGET_DIR | - | a name the \
         toolchain sets | fixture-liveness@shape=toolchain"
    ));
    fixture.rejects("surface fixture-runner-env: CARGO_TARGET_DIR is passed to");
}

// -- the closure forwarder and the const alias -------------------------------

const FORWARDER_KNOB_RS: &str = r##"fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn iterations() -> u64 {
    env_parse("FIXTURE_ITERATIONS", 10)
}

/// The closure forwarder: `env_usize` is a reader, so `closure_forwarded` is one
/// too, and the literal at its call site is the name it forwards.
pub fn closure_forwarded(key: &str) -> u64 {
    let env_usize = |name: &str, default: u64| {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    };
    env_usize(key, 7)
}

pub fn closure_read() -> u64 {
    closure_forwarded("FIXTURE_CLOSURE")
}

/// A `const` string alias handed to `env::var`: no literal stands at the call,
/// so there is nothing for the direct half to match on.
const ALIAS_NAME: &str = "FIXTURE_ALIAS";

pub fn alias_read() -> Option<String> {
    std::env::var(ALIAS_NAME).ok()
}

static OS_ALIAS_NAME: &str = "FIXTURE_OS_ALIAS";

pub fn alias_read_os() -> Option<std::ffi::OsString> {
    std::env::var_os(OS_ALIAS_NAME)
}

/// `ALIAS_NAME` declared *again* inside a raw string, after the real one.
pub const RAW_ALIAS_DECL_MENTION: &str =
    r#"const ALIAS_NAME: &str = "FIXTURE_ALIAS_IN_STRING";"#;

/// The new forms written in prose rather than in code.
pub const RAW_FORM_MENTIONS: &str = r#"let env_usize = |k: &str| std::env::var(k);
env_usize("FIXTURE_CLOSURE_MENTION");
std::env::var(ALIAS_NAME);
const FIXTURE_ALIAS_DECL_MENTION: &str = "FIXTURE_ALIAS_DECL_MENTION";"#;

// `env_usize("FIXTURE_CLOSURE_LINE_MENTION", 0)` on a doc line is prose too.
"##;

const FORWARDED: &str = "fixture-forwarded = FIXTURE_ALIAS,FIXTURE_CLOSURE,FIXTURE_OS_ALIAS | \
                         - | the names read through a closure forwarder and a const string \
                         alias | fixture-liveness@shape=forwarder";
const FORM_MENTIONS: &str = "fixture-form-mentions = FIXTURE_ALIAS_DECL_MENTION,\
                             FIXTURE_ALIAS_IN_STRING,FIXTURE_CLOSURE_LINE_MENTION,\
                             FIXTURE_CLOSURE_MENTION | - | the new forms only prose mentions | \
                             fixture-liveness@shape=form-mention";

#[test]
fn a_closure_forwarded_name_and_a_const_alias_are_reads() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/forwarder_knob.rs", FORWARDER_KNOB_RS);
    fixture.declare(&format!("{CHURN}\n{FORWARDED}"));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &[
            "gate-env-tier: 2 env-scaled surface(s), 5 variable(s)",
            "FIXTURE_ALIAS, FIXTURE_CLOSURE, FIXTURE_OS_ALIAS",
        ],
    );
}

#[test]
fn a_closure_forwarded_name_the_declaration_omits_is_refused() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/forwarder_knob.rs", FORWARDER_KNOB_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_CLOSURE is read by src/forwarder_knob.rs and set by no script");
}

#[test]
fn a_const_alias_name_the_declaration_omits_is_refused() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/forwarder_knob.rs", FORWARDER_KNOB_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_ALIAS is read by src/forwarder_knob.rs and set by no script");
}

#[test]
fn a_quoted_alias_declaration_cannot_shadow_the_real_one() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/forwarder_knob.rs", FORWARDER_KNOB_RS);
    fixture.declare(&format!(
        "{CHURN}\n{FORWARDED}\nfixture-shadow = FIXTURE_ALIAS_IN_STRING | - | a name only the \
         quoted declaration names | fixture-liveness@shape=shadow"
    ));
    fixture.rejects(
        "surface fixture-shadow: FIXTURE_ALIAS_IN_STRING is passed to no env-reading function \
         of this crate",
    );
}

#[test]
fn the_new_forms_only_a_mention_names_are_still_stale() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/forwarder_knob.rs", FORWARDER_KNOB_RS);
    fixture.declare(&format!("{CHURN}\n{FORWARDED}\n{FORM_MENTIONS}"));
    fixture.rejects(
        "surface fixture-form-mentions: FIXTURE_ALIAS_DECL_MENTION, FIXTURE_ALIAS_IN_STRING, \
         FIXTURE_CLOSURE_LINE_MENTION, FIXTURE_CLOSURE_MENTION is passed to no env-reading \
         function of this crate",
    );
}

// -- the key position --------------------------------------------------------

const KEYED_ARG_RS: &str = r#"fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

fn hops(name: &str, default: u64) -> u64 {
    env_parse(name, default)
}

pub fn two_hop() -> u64 {
    hops("FIXTURE_TWO_HOP", 8)
}

/// The body reads a fixed literal; `mandate` is a suffix of the value, not a key.
pub fn fault(mandate: &str) -> Option<String> {
    let value = std::env::var("FIXTURE_FAULT_KEY").ok()?;
    value.contains(mandate).then_some(mandate.to_owned())
}

pub fn faulted() -> Option<String> {
    fault("FIXTURE_NOT_A_KEY")
}

/// Two parameters, one key: `note` is printed, never handed to `env::var`.
fn flagged(key: &str, note: &str) -> u64 {
    eprintln!("{note}");
    env_parse(key, 3)
}

pub fn flagged_call() -> u64 {
    flagged("FIXTURE_KEYED", "FIXTURE_NOTE_ONLY")
}
"#;

const KEYED_READ: &str = "fixture-keyed = FIXTURE_FAULT_KEY,FIXTURE_KEYED,FIXTURE_TWO_HOP | - \
                          | the names the read-forwarders hand to env::var as their key | \
                          fixture-liveness@shape=keyed";

#[test]
fn the_key_positions_of_the_fixture_are_the_reads() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(&format!("{CHURN}\n{KEYED_READ}"));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_has(
        &outcome,
        &["gate-env-tier: 2 env-scaled surface(s), 5 variable(s)"],
    );
    assert_lacks(&outcome, "FIXTURE_NOT_A_KEY");
    assert_lacks(&outcome, "FIXTURE_NOTE_ONLY");
}

#[test]
fn a_name_shaped_argument_to_a_suffix_reader_is_not_demanded() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(&format!("{CHURN}\n{KEYED_READ}"));
    let outcome = fixture.check();
    assert_eq!(outcome.exit, 0, "{}", output_of(&outcome));
    assert_lacks(&outcome, "no declared surface names it");
}

#[test]
fn a_name_shaped_argument_to_a_suffix_reader_is_not_a_read() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(CHURN);
    fixture.rejects("FIXTURE_FAULT_KEY is read by src/keyed_arg.rs and set by no script");
}

#[test]
fn a_non_key_argument_declared_is_refused_as_stale() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(&format!(
        "{CHURN}\n{KEYED_READ}\nfixture-suffix = FIXTURE_NOTE_ONLY,FIXTURE_NOT_A_KEY | - | a \
         suffix and a printed label | fixture-liveness@shape=suffix"
    ));
    fixture.rejects(
        "surface fixture-suffix: FIXTURE_NOTE_ONLY, FIXTURE_NOT_A_KEY is passed to no \
         env-reading function of this crate",
    );
}

#[test]
fn a_two_hop_key_position_is_still_a_read() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(&format!(
        "{CHURN}\nfixture-partial = FIXTURE_FAULT_KEY,FIXTURE_KEYED | - | the keyed fixture \
         without the two-hop wrapper | fixture-liveness@shape=partial"
    ));
    fixture.rejects("FIXTURE_TWO_HOP is read by src/keyed_arg.rs and set by no script");
}

#[test]
fn the_first_of_two_parameters_is_the_key() {
    let fixture = EnvFixture::new();
    fixture.write_source("src/keyed_arg.rs", KEYED_ARG_RS);
    fixture.declare(&format!(
        "{CHURN}\nfixture-partial = FIXTURE_FAULT_KEY,FIXTURE_TWO_HOP | - | the keyed fixture \
         without the first-parameter key | fixture-liveness@shape=partial"
    ));
    fixture.rejects("FIXTURE_KEYED is read by src/keyed_arg.rs and set by no script");
}
