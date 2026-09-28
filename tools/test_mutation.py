#!/usr/bin/env python3

"""Exercise `tools/mutation.py`, the anchor-checked mutation harness.

The gate checkers' vacuity probes mutate a file through this harness, so its own
failure modes need cases too: an instrument that cannot fail is not coverage.
The second case is the hazard `rg -c` carries -- a *line* count reads a token
that appears twice on one line as one match, so a probe that changed one of the
two instances passes a count check and is read as a verdict on the check.
"""

import contextlib
import hashlib
import importlib.util
import io
import shutil
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

MUTATION_SPEC = importlib.util.spec_from_file_location("mutation", TOOLS / "mutation.py")
MUTATION = importlib.util.module_from_spec(MUTATION_SPEC)
MUTATION_SPEC.loader.exec_module(MUTATION)


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
