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

import importlib.util
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

# The doc set the checker reads, plus exactly the declarations its counts are
# derived from. Copied, so a case perturbs a count without touching the tree.
FIXTURE_FILES = (
    "tools/PERF_INFRA.md",
    "tools/MANDATE_SMOKE.md",
    "tools/PERF_PENDING_rtp_mux.md",
    "tools/mandate-producers.json",
    "tools/mandate-arms.json",
    "tools/mandate-baseline.json",
    "tools/mandate_compare.py",
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

    def edit(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"{rel} no longer contains the fixture text {old!r}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

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
        self.edit(
            "tools/PERF_INFRA.md",
            "alongside the eight evidence",
            "alongside the nine evidence",
        )
        problems, _summary = self.run_check()
        self.assertEqual(len(problems), 1, f"expected one problem, got {problems}")
        self.assertIn("'nine'", problems[0])
        self.assertIn("8 is what determines it", problems[0])

    def test_a_sentence_that_left_the_prose_fails_rather_than_being_skipped(self):
        self.edit(
            "tools/PERF_INFRA.md",
            "**216.3 s** on a warm build (10 panels, 20 SVG+PNG plot\nfiles)",
            "**216.3 s** on a warm build",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "no longer states 'panels and plot files of a mandate-check run'" in p
                for p in problems
            ),
            f"the vanished count was not reported: {problems}",
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
        self.edit(
            "tools/MANDATE_SMOKE.md",
            "Two producers are declared today",
            "Three producers are declared today",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'producers declared'" in p and "'Three'" in p and "2 is what" in p
                for p in problems
            ),
            f"the registry did not determine the producer count: {problems}",
        )

    def test_the_harness_relation_counts_are_derived_from_its_own_blocks(self):
        self.edit("tests/GATE.md", "**11 orthogonal** rows", "**12 orthogonal** rows")
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'declared relation counts of the harness declaration'" in p
                and "'12'" in p
                and "11 is what determines it" in p
                for p in problems
            ),
            f"the blocks did not determine the relation counts: {problems}",
        )

    def test_a_derived_claim_must_still_name_its_command(self):
        self.edit(
            "tools/PERF_INFRA.md",
            "printed by `crates/rtp/tools/check-ignored.py` (run",
            "printed by that crate's own checker (run",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'the rtp opt-in inventory' no longer names" in p
                and "crates/rtp/tools/check-ignored.py" in p
                for p in problems
            ),
            f"the derived claim lost its pointer unnoticed: {problems}",
        )

    def test_the_draft_relation_counts_come_from_its_own_rows(self):
        # The draft's rows carry a `TBD` cost by design; the substitution that
        # lets the relation derivation see all of them is what makes this fail
        # when the prose moves and the blocks do not.
        self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the 94 rows are **46 orthogonal**",
            "the 94 rows are **47 orthogonal**",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'relation counts of the rtp_mux draft'" in p
                and "'47'" in p
                and "46 is what determines it" in p
                for p in problems
            ),
            f"the draft's own rows did not determine the relation counts: {problems}",
        )

    def test_the_draft_row_count_is_the_parsed_row_count(self):
        self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the 94 rows are **46 orthogonal**",
            "the 90 rows are **46 orthogonal**",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any("'90'" in p and "94 is what determines it" in p for p in problems),
            f"the draft's row count was not derived from its blocks: {problems}",
        )

    def test_the_draft_cost_sums_come_from_its_rows(self):
        self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "and 4135 s in `perf` (26 rows, 7 of them still `TBD`)",
            "and 4136 s in `perf` (26 rows, 7 of them still `TBD`)",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'cost sums of the rtp_mux draft'" in p
                and "'4136'" in p
                and "4135 is what determines it" in p
                for p in problems
            ),
            f"the draft's rows did not determine its cost sums: {problems}",
        )

    def test_the_draft_target_count_is_the_distinct_row_targets(self):
        self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "the eleven measurement targets",
            "the twelve measurement targets",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'measurement targets of the rtp_mux draft'" in p
                and "'twelve'" in p
                and "11 is what determines it" in p
                for p in problems
            ),
            f"the draft's distinct targets did not determine the count: {problems}",
        )

    def test_every_declared_reference_counts_both_declarations(self):
        self.edit(
            "tools/PERF_PENDING_rtp_mux.md",
            "reference (all 33)",
            "reference (all 34)",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'every declared reference, harness plus draft'" in p
                and "'34'" in p
                and "33 is what determines it" in p
                for p in problems
            ),
            f"the reference total was not derived: {problems}",
        )

    def test_a_derived_claim_rejects_a_transcribed_tally(self):
        self.edit(
            "tools/PERF_INFRA.md",
            "The command's\noutput is the tally",
            "It reports 32 `#[ignore]`d tests in all. The command's\noutput is the tally",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'the rtp opt-in inventory' has a transcription back" in p
                and "32 `#[ignore]`d tests" in p
                for p in problems
            ),
            f"the transcribed tally was not refused: {problems}",
        )

    def test_a_transcription_split_across_lines_is_refused(self):
        # The tally the derivation replaced was split across two source lines;
        # a prohibition that only sees one line would let it back in.
        self.edit(
            "tools/PERF_INFRA.md",
            "The command's\noutput is the tally",
            "There are 32 `#[ignore]`d\ntests in all. The command's\noutput is the tally",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'the rtp opt-in inventory' has a transcription back" in p
                and "transcribed total of #[ignore]d tests" in p
                for p in problems
            ),
            f"the line-split transcription was not refused: {problems}",
        )

    def test_the_probe_selfcheck_tally_is_derived_too(self):
        self.edit(
            "tools/PERF_INFRA.md",
            "is what\n`crates/rtp/tools/check-ignored.py` prints",
            "is 5 of 6 probes recorded, which\n`crates/rtp/tools/check-ignored.py` prints",
        )
        problems, _summary = self.run_check()
        self.assertTrue(
            any(
                "'the rtp probe-selfcheck tally' has a transcription back" in p
                and "5 of 6 probes recorded" in p
                for p in problems
            ),
            f"the transcribed probe tally was not refused: {problems}",
        )


if __name__ == "__main__":
    unittest.main()
