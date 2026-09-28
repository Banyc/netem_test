#!/usr/bin/env python3

"""Exercise `tools/check-gate.py`'s documented-count check.

A count written into prose that a command already determines goes stale with
no signal: the code moves, the sentence does not, and a reader acts on the
number. `tools/PERF_INFRA.md` carried a stale one twice. The gate therefore
either *verifies* such a count against the source that determines it or
*derives* it - the sentence names the command that prints it instead.

A check that cannot fail would be the same defect one level up, so every
failure mode the check claims has a case here: a changed verified count (the
diagnostic names the written value and the derived one), a sentence that left
the prose (so the check cannot quietly lose its subject), a source declaration
that has gone (so a value that cannot be derived is a failure and not a skip),
a derived claim whose command is no longer named, and a transcribed tally
coming back into a derived sentence. The fixture is the repository's own doc
set copied into a temporary root, perturbed one count at a time.
"""

import contextlib
import hashlib
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

SPEC = importlib.util.spec_from_file_location("check_gate", TOOLS / "check-gate.py")
CHECK_GATE = importlib.util.module_from_spec(SPEC)
# `dataclasses` resolves the defining module through `sys.modules`, so the
# module must be registered before it executes.
sys.modules["check_gate"] = CHECK_GATE
SPEC.loader.exec_module(CHECK_GATE)

MUTATION_SPEC = importlib.util.spec_from_file_location("mutation", TOOLS / "mutation.py")
MUTATION = importlib.util.module_from_spec(MUTATION_SPEC)
MUTATION_SPEC.loader.exec_module(MUTATION)

# The doc set the checker reads, plus exactly the declarations its counts are
# derived from. Copied, so a case perturbs a count without touching the tree.
FIXTURE_FILES = (
    "tools/PERF_INFRA.md",
    "tools/MANDATE_SMOKE.md",
    "tools/PERF_PENDING_rtp_mux.md",
    "tools/mandate-producers.json",
    "tools/mandate-arms.json",
    "tools/mandate-baseline.json",
    "netem-test/src/tools/mandate_compare.rs",
    "tests/GATE.md",
)


class DocCountTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="doc-counts-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in FIXTURE_FILES:
            source = REPO / rel
            self.assertTrue(source.is_file(), f"fixture source is missing: {source}")
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)

    def edit(self, rel, old, new, *, count=1, names=()):
        """Mutate one fixture file, run the doc-count check, and restore it.

        The mutation goes through `tools/mutation.py`: an anchor that no longer
        matches once fails as `ANCHOR-MISS` *before* the check runs, so a stale
        probe cannot read as a verdict on the check, and the fixture is
        byte-identical afterwards.
        """
        with MUTATION.mutated(self.root / rel, old, new, count) as applied:
            problems, summary = self.run_check()
            if names:
                applied.names(" ".join(problems), *names)
            return problems, summary

    def run_check(self):
        problems, summary = CHECK_GATE.check_doc_counts(self.root)
        self.assertTrue(summary, "the check returned no summary line")
        return problems, summary[0]

    def test_the_repository_docs_pass_and_the_check_looked_at_counts(self):
        problems, summary = self.run_check()
        self.assertEqual(problems, [], f"unexpected problems: {problems}")
        self.assertIn("verified count(s)", summary)
        self.assertNotIn("0 verified count(s)", summary)
        self.assertIn("derived inventory claim(s)", summary)

    def test_a_changed_verified_count_fails_naming_both_values(self):
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "alongside the eight evidence",
            "alongside the nine evidence",
            # The check's own message is the oracle: it names the written value
            # and the one that determines it.
            names=("'nine'", "8 is what determines it"),
        )
        self.assertEqual(len(problems), 1, f"expected one problem, got {problems}")

    def test_a_sentence_that_left_the_prose_fails_rather_than_being_skipped(self):
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "**216.3 s** on a warm build (12 panels, 24 SVG+PNG plot\nfiles)",
            "**216.3 s** on a warm build",
            names=("no longer states 'panels and plot files of a mandate-check run'",),
        )

    def test_a_missing_source_is_a_failure_not_a_skip(self):
        (self.root / "tools" / "mandate-baseline.json").unlink()
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "cannot derive: tools/mandate-baseline.json does not exist" in p
                for p in problems
            ),
            f"a gone source was silently tolerated: {problems}",
        )

    def test_the_producer_count_is_derived_from_the_registry(self):
        problems, _summary = self.edit(
            "tools/MANDATE_SMOKE.md",
            "Two producers are declared today",
            "Three producers are declared today",
            names=("'producers declared'", "'Three'", "2 is what"),
        )

    def test_the_harness_relation_counts_are_derived_from_its_own_blocks(self):
        problems, _summary = self.edit(
            "tests/GATE.md",
            "**11 orthogonal** rows",
            "**12 orthogonal** rows",
            names=(
                "'declared relation counts of the harness declaration'",
                "'12'",
                "11 is what determines it",
            ),
        )

    def test_a_derived_claim_must_still_name_its_command(self):
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "printed by `crates/rtp/tools/check-ignored.py` (run",
            "printed by that crate's own checker (run",
            names=("'the rtp opt-in inventory' no longer names", "crates/rtp/tools/check-ignored.py"),
        )

    def test_the_draft_relation_counts_come_from_its_own_rows(self):
        # The draft's rows carry a `TBD` cost by design; the substitution that
        # lets the relation derivation see all of them is what makes this fail
        # when the prose moves and the blocks do not.
        problems, _summary = self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the 94 rows are **46 orthogonal**",
            "the 94 rows are **47 orthogonal**",
            names=("'relation counts of the rtp_mux draft'", "'47'", "46 is what determines it"),
        )

    def test_the_draft_row_count_is_the_parsed_row_count(self):
        problems, _summary = self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the 94 rows are **46 orthogonal**",
            "the 90 rows are **46 orthogonal**",
            names=("'90'", "94 is what determines it"),
        )

    def test_the_draft_cost_sums_come_from_its_rows(self):
        problems, _summary = self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "and 4135 s in `perf` (26 rows, 7 of them still `TBD`)",
            "and 4136 s in `perf` (26 rows, 7 of them still `TBD`)",
            names=("'cost sums of the rtp_mux draft'", "'4136'", "4135 is what determines it"),
        )

    def test_the_draft_target_count_is_the_distinct_row_targets(self):
        problems, _summary = self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the eleven measurement targets",
            "the twelve measurement targets",
            names=("'measurement targets of the rtp_mux draft'", "'twelve'", "11 is what determines it"),
        )

    def test_every_declared_reference_counts_both_declarations(self):
        problems, _summary = self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "reference (all 33)",
            "reference (all 34)",
            names=("'every declared reference, harness plus draft'", "'34'", "33 is what determines it"),
        )

    def test_a_derived_claim_rejects_a_transcribed_tally(self):
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "The command's\noutput is the tally",
            "It reports 32 `#[ignore]`d tests in all. The command's\noutput is the tally",
            names=("'the rtp opt-in inventory' has a transcription back", "32 `#[ignore]`d tests"),
        )

    def test_a_transcription_split_across_lines_is_refused(self):
        # The tally the derivation replaced was split across two source lines;
        # a prohibition that only sees one line would let it back in.
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "The command's\noutput is the tally",
            "There are 32 `#[ignore]`d\ntests in all. The command's\noutput is the tally",
            names=("'the rtp opt-in inventory' has a transcription back", "transcribed total of #[ignore]d tests"),
        )

    def test_the_probe_selfcheck_tally_is_derived_too(self):
        problems, _summary = self.edit(
            "tools/PERF_INFRA.md",
            "is what\n`crates/rtp/tools/check-ignored.py` prints",
            "is 5 of 6 probes recorded, which\n`crates/rtp/tools/check-ignored.py` prints",
            names=("'the rtp probe-selfcheck tally' has a transcription back", "5 of 6 probes recorded"),
        )


class MutationAnchorTest(unittest.TestCase):
    """The harness the doc-count probes mutate through.

    Its own failure modes need cases too, because an instrument that cannot
    fail is not coverage. The second case is the hazard `rg -c` carries: a
    *line* count reads a token that appears twice on one line as one match, so
    a probe that changed one of the two instances passes a count check and is
    read as a verdict on the check.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mutation-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "GATE.md"
        # `tests/GATE.md`'s own `perf-history-archive` row states the phrase
        # twice on one line; this is that sentence.
        self.path.write_text(
            "where a run's evidence is kept and what it is compared against, "
            "rather than a measurement, and what the run is compared against\n",
            encoding="utf-8",
        )
        self.anchor = "is compared against"

    def test_an_anchor_twice_on_one_line_fails_loudly_as_anchor_miss(self):
        before = self.path.read_bytes()
        self.assertEqual(self.path.read_text().count(self.anchor), 2)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(MUTATION.AnchorMiss) as caught:
            with MUTATION.mutated(self.path, self.anchor, "IS COMPARED"):
                self.fail("a stale anchor must not reach the check")
        message = str(caught.exception)
        self.assertIn(MUTATION.ANCHOR_MISS, message)
        self.assertIn("occurs 2 time(s)", message)
        self.assertIn("declared 1", message)
        self.assertIn(MUTATION.ANCHOR_MISS, stderr.getvalue())
        self.assertEqual(
            self.path.read_bytes(), before, "a refused anchor must not touch the file"
        )

    def test_an_anchor_matching_nothing_fails_loudly_as_anchor_miss(self):
        with self.assertRaises(MUTATION.AnchorMiss) as caught:
            with MUTATION.mutated(self.path, "not in this file", "x"):
                self.fail("a stale anchor must not reach the check")
        self.assertIn(MUTATION.ANCHOR_MISS, str(caught.exception))
        self.assertIn("occurs 0 time(s)", str(caught.exception))

    def test_a_declared_occurrence_count_is_honoured(self):
        with MUTATION.mutated(self.path, self.anchor, "IS COMPARED", count=2) as applied:
            self.assertEqual(applied.occurrences, 2)
            self.assertEqual(self.path.read_text().count("IS COMPARED"), 2)
            self.assertEqual(len(applied.changed), 1, applied.changed)
        self.assertEqual(self.path.read_text().count("IS COMPARED"), 0)

    def test_the_harness_names_the_mutated_line_and_restores_byte_identically(self):
        before = self.path.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        with MUTATION.mutated(self.path, "rather than a measurement", "rather than a guess") as applied:
            line = applied.changed[0]
            self.assertEqual(line[0], 1, applied.changed)
            self.assertIn("rather than a guess", line[1])
            self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
