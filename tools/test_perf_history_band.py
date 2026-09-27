#!/usr/bin/env python3

"""Exercise `perf-history`'s M1 rejection as a black box.

The M1 band used to be a fixed percentage, and the archive shows the cost: over
the eleven runs on record `lone_tail p90` spans 37.1-57.8 ms, so the pair
`37.1 -> 55.0` (+48 %) is inside the arm's own spread while a settled 40 % band
rejects it -- the battery came back PASS 4/4 while the wrapper exited 6 on a good
build. The band is now drawn from the archive
(`max(mean + 4sd over the archived runs, settled band)`), trusted from eight
archived readings of that arm and metric and falling back to the settled band,
*named*, below that.

Every case here runs the compiled `perf-history` over a synthetic archive whose
readings are the archive's own, so what is asserted is the command's status, its
`vs-prev.md` and its stderr -- not a Python restatement of the rule. The vacuity
is behavioural and in both directions: the same false-positive pair is rejected
when the archive is too thin to supply a measured band (so the check that says
"exit 0" can fail), and a genuine degradation is rejected with the arm, the
metric, both values and the bound named. A rule that cannot fail is not coverage,
and a rule that always fails is not a rule.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CRATE = TOOLS.parent


def binary():
    """The built `perf-history`, wherever the build put it.

    A missing binary is a failure and not a skip: the rejection has no Python
    implementation any more, so an instrument that cannot run has not checked
    anything.
    """
    candidates = (
        CRATE / "target" / "release" / "perf-history",
        CRATE / "target" / "debug" / "perf-history",
        CRATE / "netem-test" / "target" / "release" / "perf-history",
        CRATE / "netem-test" / "target" / "debug" / "perf-history",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "perf-history is not built; build it with "
        "`cargo build -p netem-test --features cli --bin perf-history` before running this suite"
    )


BINARY = binary()

# The eleven archived runs' own readings, in archive order
# (`crates/netem_test/.net-perf-history/mandate-check`, 2026-09-27). They are the
# population the measured band is drawn from, and the reason a fixed percentage
# cannot bound these quantities.
ARCHIVED_LONE_P90 = (45.7, 45.7, 39.3, 38.6, 49.3, 57.8, 44.8, 52.3, 44.3, 37.1, 55.0)
ARCHIVED_LONE_P99 = (
    161.4, 161.4, 174.8, 155.1, 189.3, 165.5, 155.0, 165.8, 169.3, 178.7, 165.0,
)
ARCHIVED_HOSTILE_P99 = (
    138.1, 138.1, 123.9, 221.8, 150.8, 221.1, 203.2, 124.1, 122.5, 136.3, 125.1,
)

# The fewest archived readings a measured band is trusted from, mirroring
# `MIN_SPREAD_SAMPLES` in `netem-test/src/bin/perf-history.rs`. Stated here so
# the fallback case can name the shortfall it expects.
MIN_SPREAD_SAMPLES = 8

# The index of the archived run whose `lone_tail p90` is 37.1: the baseline the
# recorded false positive was compared against.
LONE_P90_37_1 = 9
# The index of the archived run whose `hostile p99` is 125.1.
HOSTILE_P99_125_1 = 10

REPORT_NAME = "mandate-check.json"
LOG_NAME = "mandate-smoke.log"


def run_files(directory, lone_p90, lone_p99, hostile_p99):
    """Write the report and the arm log a run directory is read through."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    (Path(directory) / REPORT_NAME).write_text(
        '{"revision":"aaaa","rtp_mux":{"revision":"aaaa"}}', encoding="utf-8"
    )
    (Path(directory) / LOG_NAME).write_text(
        f"[mandate-smoke lone_tail] p50= 0.2 p90= {lone_p90} p99= {lone_p99} "
        "delivery= 1.000\n"
        f"[mandate-smoke hostile  ] p50= 1.4 p90= 83.7 p99= {hostile_p99} "
        "delivery= 1.000\n",
        encoding="utf-8",
    )


class M1BandTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="perf-history-band-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.archive = self.root / "archive"
        self.series = self.archive / "mandate-check"

    # ── fixtures ────────────────────────────────────────────────────────────
    def write_archive(self, indices):
        """One archived entry per index, named so the order is the name order."""
        for index in indices:
            run_files(
                self.series / f"2026{index:02}",
                ARCHIVED_LONE_P90[index],
                ARCHIVED_LONE_P99[index],
                ARCHIVED_HOSTILE_P99[index],
            )

    def candidate(self, name, lone_p90, lone_p99, hostile_p99):
        """A run directory outside the archive, with the panel a run owes."""
        directory = self.root / name
        run_files(directory, lone_p90, lone_p99, hostile_p99)
        (directory / "plots").mkdir(exist_ok=True)
        (directory / "plots" / "M1-latency.svg").write_text("<svg/>", encoding="utf-8")
        return directory

    def history(self, candidate, baseline_index):
        """Run the binary with the archive and the baseline under test."""
        environment = dict(
            os.environ,
            PERF_ARCHIVE_DIR=str(self.archive),
            PERF_BASELINE_DIR=str(self.series / f"2026{baseline_index:02}"),
        )
        return subprocess.run(
            [
                str(BINARY),
                str(candidate),
                "--label",
                "mandate-check",
                "--no-archive",
                "--no-ab",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    # ── the false positive, and the vacuity that gives its green teeth ──────
    def test_the_archived_false_positive_passes(self):
        """`lone_tail p90 37.1 -> 55.0` is inside the arm's own spread."""
        self.write_archive(range(len(ARCHIVED_LONE_P90)))
        candidate = self.candidate("false-positive", 55.0, 165.0, 125.1)
        result = self.history(candidate, LONE_P90_37_1)
        self.assertEqual(result.returncode, 0, result.stderr)
        vs_prev = (candidate / "vs-prev.md").read_text(encoding="utf-8")
        self.assertIn("## M1: no degradation", vs_prev)
        self.assertIn("37.1000 -> 55.0000", vs_prev)
        self.assertIn("(measured)", vs_prev)

    def test_the_measured_half_is_what_saves_the_false_positive(self):
        """The same pair on a thin archive is rejected: the measured band is
        what changes the verdict, so the case above is not vacuous."""
        thin = (LONE_P90_37_1, 8, 0)
        self.write_archive(thin)
        candidate = self.candidate("thin-false-positive", 55.0, 165.0, 125.1)
        result = self.history(candidate, LONE_P90_37_1)
        self.assertEqual(
            result.returncode,
            6,
            "with no measured band available the settled 40 % band rejects, "
            "which is the defect; the full-archive case must be the one that passes"
            f"\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        vs_prev = (candidate / "vs-prev.md").read_text(encoding="utf-8")
        self.assertIn(
            f"insufficient: {len(thin)} of {MIN_SPREAD_SAMPLES} archived runs",
            vs_prev,
            "the fallback must be named, not silently substituted",
        )
        self.assertIn("settled (insufficient spread)", vs_prev)

    # ── the hard rejection: a genuine degradation still exits 6 ─────────────
    def test_a_genuine_degradation_is_rejected_and_names_its_bound(self):
        """The documented +300 ms one-way fault on the hostile arm."""
        self.write_archive(range(len(ARCHIVED_LONE_P90)))
        candidate = self.candidate("degraded", 55.0, 165.0, 1891.4)
        result = self.history(candidate, HOSTILE_P99_125_1)
        self.assertEqual(
            result.returncode, 6, "a genuinely degraded run must still be rejected"
        )
        self.assertIn("M1 DEGRADATION", result.stderr)
        for expected in (
            "mandate-smoke/hostile",
            "p99",
            "125.1000 -> 1891.4000",
            "bound",
            "314.83",
        ):
            self.assertIn(expected, result.stderr + result.stdout)

    def test_the_rejection_is_reported_not_softened(self):
        """`--only-compare` reports the same verdict with status 0, so a caller
        can tell "worse than last time" from "could not tell"."""
        self.write_archive(range(len(ARCHIVED_LONE_P90)))
        candidate = self.candidate("degraded-report-only", 55.0, 165.0, 1891.4)
        environment = dict(
            os.environ,
            PERF_ARCHIVE_DIR=str(self.archive),
            PERF_BASELINE_DIR=str(self.series / f"2026{HOSTILE_P99_125_1:02}"),
        )
        result = subprocess.run(
            [
                str(BINARY),
                str(candidate),
                "--label",
                "mandate-check",
                "--no-archive",
                "--no-ab",
                "--only-compare",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("M1 DEGRADATION", result.stderr)
        vs_prev = (candidate / "vs-prev.md").read_text(encoding="utf-8")
        self.assertIn("## M1 DEGRADATION - this run is rejected", vs_prev)


if __name__ == "__main__":
    unittest.main()
