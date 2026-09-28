//! The documented-count check's conformance cases, ported from
//! `DocCountTest` in the (now deleted) `tools/test_doc_counts.py` suite.
//!
//! The fixture is the repository's own doc set copied into a temporary root,
//! perturbed one count at a time, so a case never touches the tree it reads.
//! Every failure mode the check claims has a case: a changed verified count
//! (the diagnostic names both values), a sentence that left the prose, a source
//! that has gone, a derived claim whose command is no longer named, and a
//! transcribed tally coming back.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use netem_test::tools::check_gate::doc_counts::check_doc_counts;

/// The doc set the checker reads, plus the declarations its counts derive from.
const FIXTURE_FILES: &[&str] = &[
    "tools/PERF_INFRA.md",
    "tools/MANDATE_SMOKE.md",
    "tools/PERF_PENDING_rtp_mux.md",
    "tools/mandate-producers.json",
    "tools/mandate-arms.json",
    "tools/mandate-baseline.json",
    "netem-test/src/tools/mandate_compare.rs",
    "tests/GATE.md",
];

/// The repository root: `CARGO_MANIFEST_DIR` is `<repo>/netem-test`.
fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("the crate sits in the repository")
        .to_path_buf()
}

struct DocCountFixture {
    tmp: PathBuf,
    root: PathBuf,
}

impl DocCountFixture {
    fn new() -> DocCountFixture {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0);
        let tmp = std::env::temp_dir().join(format!(
            "netem-doc-counts-{}-{nanos}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let root = tmp.join("root");
        for rel in FIXTURE_FILES {
            let source = repo().join(rel);
            assert!(
                source.is_file(),
                "fixture source is missing: {}",
                source.display()
            );
            let dest = root.join(rel);
            std::fs::create_dir_all(dest.parent().expect("a parent")).expect("mkdir");
            std::fs::copy(&source, &dest).expect("copy");
        }
        DocCountFixture { tmp, root }
    }

    /// Mutate one fixture file, asserting the anchor's occurrence count first.
    fn edit(&self, rel: &str, old: &str, new: &str, count: usize) {
        let path = self.root.join(rel);
        let text = std::fs::read_to_string(&path).expect("read the fixture");
        let occurrences = text.matches(old).count();
        assert_eq!(
            occurrences, count,
            "ANCHOR-MISS: {old:?} occurs {occurrences} time(s) in {rel}, declared {count}"
        );
        std::fs::write(&path, text.replacen(old, new, count)).expect("write");
    }

    fn run_check(&self) -> (Vec<String>, String) {
        let (problems, summary) = check_doc_counts(&self.root);
        assert!(!summary.is_empty(), "the check returned no summary line");
        (problems, summary[0].clone())
    }
}

impl Drop for DocCountFixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.tmp);
    }
}

fn assert_named(problems: &[String], names: &[&str]) {
    let joined = problems.join(" ");
    for name in names {
        assert!(
            joined.contains(name),
            "expected {name:?} in the problems:\n{joined}"
        );
    }
}

#[test]
fn the_repository_docs_pass_and_the_check_looked_at_counts() {
    let fixture = DocCountFixture::new();
    let (problems, summary) = fixture.run_check();
    assert!(problems.is_empty(), "unexpected problems: {problems:?}");
    assert!(summary.contains("verified count(s)"));
    assert!(!summary.contains("0 verified count(s)"));
    assert!(summary.contains("derived inventory claim(s)"));
}

#[test]
fn a_changed_verified_count_fails_naming_both_values() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "alongside the eight evidence",
        "alongside the nine evidence",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_eq!(problems.len(), 1, "expected one problem, got {problems:?}");
    assert_named(&problems, &["'nine'", "8 is what determines it"]);
}

#[test]
fn a_sentence_that_left_the_prose_fails_rather_than_being_skipped() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "**216.3 s** on a warm build (12 panels, 24 SVG+PNG plot\nfiles)",
        "**216.3 s** on a warm build",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &["no longer states 'panels and plot files of a mandate-check run'"],
    );
}

#[test]
fn a_missing_source_is_a_failure_not_a_skip() {
    let fixture = DocCountFixture::new();
    std::fs::remove_file(fixture.root.join("tools").join("mandate-baseline.json")).expect("unlink");
    let (problems, _) = fixture.run_check();
    assert!(
        problems
            .iter()
            .any(|problem| problem
                .contains("cannot derive: tools/mandate-baseline.json does not exist")),
        "a gone source was silently tolerated: {problems:?}"
    );
}

#[test]
fn the_producer_count_is_derived_from_the_registry() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/MANDATE_SMOKE.md",
        "Two producers are declared today",
        "Three producers are declared today",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(&problems, &["'producers declared'", "'Three'", "2 is what"]);
}

#[test]
fn the_harness_relation_counts_are_derived_from_its_own_blocks() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tests/GATE.md",
        "**11 orthogonal** rows",
        "**12 orthogonal** rows",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'declared relation counts of the harness declaration'",
            "'12'",
            "11 is what determines it",
        ],
    );
}

#[test]
fn a_derived_claim_must_still_name_its_command() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "printed by `crates/rtp/tools/check-ignored.py` (run",
        "printed by that crate's own checker (run",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'the rtp opt-in inventory' no longer names",
            "crates/rtp/tools/check-ignored.py",
        ],
    );
}

#[test]
fn the_draft_relation_counts_come_from_its_own_rows() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_PENDING_rtp_mux.md",
        "the 94 rows are **46 orthogonal**",
        "the 94 rows are **47 orthogonal**",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'relation counts of the rtp_mux draft'",
            "'47'",
            "46 is what determines it",
        ],
    );
}

#[test]
fn the_draft_row_count_is_the_parsed_row_count() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_PENDING_rtp_mux.md",
        "the 94 rows are **46 orthogonal**",
        "the 90 rows are **46 orthogonal**",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(&problems, &["'90'", "94 is what determines it"]);
}

#[test]
fn the_draft_cost_sums_come_from_its_rows() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_PENDING_rtp_mux.md",
        "and 4135 s in `perf` (26 rows, 7 of them still `TBD`)",
        "and 4136 s in `perf` (26 rows, 7 of them still `TBD`)",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'cost sums of the rtp_mux draft'",
            "'4136'",
            "4135 is what determines it",
        ],
    );
}

#[test]
fn the_draft_target_count_is_the_distinct_row_targets() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_PENDING_rtp_mux.md",
        "the eleven measurement targets",
        "the twelve measurement targets",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'measurement targets of the rtp_mux draft'",
            "'twelve'",
            "11 is what determines it",
        ],
    );
}

#[test]
fn every_declared_reference_counts_both_declarations() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_PENDING_rtp_mux.md",
        "reference (all 33)",
        "reference (all 34)",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'every declared reference, harness plus draft'",
            "'34'",
            "33 is what determines it",
        ],
    );
}

#[test]
fn a_derived_claim_rejects_a_transcribed_tally() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "The command's\noutput is the tally",
        "It reports 32 `#[ignore]`d tests in all. The command's\noutput is the tally",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'the rtp opt-in inventory' has a transcription back",
            "32 `#[ignore]`d tests",
        ],
    );
}

#[test]
fn a_transcription_split_across_lines_is_refused() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "The command's\noutput is the tally",
        "There are 32 `#[ignore]`d\ntests in all. The command's\noutput is the tally",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'the rtp opt-in inventory' has a transcription back",
            "transcribed total of #[ignore]d tests",
        ],
    );
}

#[test]
fn the_probe_selfcheck_tally_is_derived_too() {
    let fixture = DocCountFixture::new();
    fixture.edit(
        "tools/PERF_INFRA.md",
        "is what\n`crates/rtp/tools/check-ignored.py` prints",
        "is 5 of 6 probes recorded, which\n`crates/rtp/tools/check-ignored.py` prints",
        1,
    );
    let (problems, _) = fixture.run_check();
    assert_named(
        &problems,
        &[
            "'the rtp probe-selfcheck tally' has a transcription back",
            "5 of 6 probes recorded",
        ],
    );
}
