#!/usr/bin/env python3

"""Keep the drafted `rtp_mux` perf declaration pasteable.

`tools/PERF_PENDING_rtp_mux.md` is the `gate-perf-design` / `gate-budgets` /
`gate-coverage-gaps` block set another crate applies to its own `GATE.md`. It
cannot be checked by `check-gate.py` here — the rows name tests in a sibling
checkout, and a checked-in draft must not resolve them — so this test pins the
properties the checker would reject on the day it is applied, using only the
draft's own text: every row parses as `<target>::<test> = <tier> | <cost> |
<cell>[,...]` under the checker's grammar, no row is declared twice, every
tier a row names has a budget, the baseline names a row, every gap carries a
well-formed cell and a reason, and the rows whose cost is a number fit their
tier's budget. A `TBD` cost is allowed by design (the draft is where the
measurement is still owed) and is what the checker refuses when the block lands.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DRAFT = TOOLS / "PERF_PENDING_rtp_mux.md"

SPEC = importlib.util.spec_from_file_location("check_gate", TOOLS / "check-gate.py")
CHECK_GATE = importlib.util.module_from_spec(SPEC)
# `dataclasses` resolves the defining module through `sys.modules`, so the
# module must be registered before it executes.
sys.modules["check_gate"] = CHECK_GATE
SPEC.loader.exec_module(CHECK_GATE)


def block(name):
    match = re.search(rf"```{re.escape(name)}\n(.*?)```", DRAFT.read_text(encoding="utf-8"), re.S)
    return match.group(1) if match else None


class PendingDeclarationTest(unittest.TestCase):
    def setUp(self):
        self.design = block("gate-perf-design")
        self.budgets = block("gate-budgets")
        self.gaps = block("gate-coverage-gaps")
        self.rows = []
        for number, line in enumerate(self.design.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, rest = line.partition(" = ")
            self.assertTrue(separator, f"line {number} is not '<name> = <tier> | ...': {line!r}")
            fields = [field.strip() for field in rest.split("|")]
            self.assertEqual(len(fields), 3, f"{name}: expected three fields, got {fields}")
            self.rows.append((name, fields[0], fields[1], fields[2]))

    def test_the_three_blocks_exist(self):
        for name, body in (
            ("gate-perf-design", self.design),
            ("gate-budgets", self.budgets),
            ("gate-coverage-gaps", self.gaps),
        ):
            self.assertIsNotNone(body, f"the draft has no ```{name} block")

    def test_every_row_is_a_unique_well_formed_declaration(self):
        seen = set()
        for name, tier, cost, coverage in self.rows:
            self.assertIn("::", name, f"{name!r} is not '<target>::<test>'")
            self.assertNotIn(" ", name, f"{name!r} contains whitespace")
            self.assertNotIn(name, seen, f"{name} is declared twice")
            seen.add(name)
            self.assertIn(tier, CHECK_GATE.PERF_TIERS, f"{name}: unknown tier {tier!r}")
            if cost != "TBD":
                self.assertGreaterEqual(
                    float(cost), 0.0, f"{name}: cost {cost!r} is not a non-negative number"
                )
            cells = [cell.strip() for cell in coverage.split(",") if cell.strip()]
            self.assertTrue(cells, f"{name}: no coverage cell")
            for cell in cells:
                self.assertIsNone(
                    CHECK_GATE.cell_problem(cell), f"{name}: malformed cell {cell!r}"
                )
        self.assertGreater(len(self.rows), 0, "the draft declares no row")

    def test_every_used_tier_has_a_budget_and_a_measured_row_fits_it(self):
        budgets = {}
        baseline = None
        for line in self.budgets.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition(" = ")
            self.assertTrue(separator, f"budget line {line!r} is not '<key> = <value>'")
            key, value = key.strip(), value.strip()
            if key == "baseline":
                baseline = value
            elif key in CHECK_GATE.PERF_TIERS:
                budgets[key] = float(value)
        names = {name for name, _, _, _ in self.rows}
        self.assertIsNotNone(baseline, "gate-budgets names no baseline row")
        self.assertIn(baseline, names, f"baseline {baseline!r} is not a declared row")
        sums = {}
        for name, tier, cost, _ in self.rows:
            self.assertIn(tier, budgets, f"no budget declared for the {tier} tier")
            if cost != "TBD":
                sums[tier] = sums.get(tier, 0.0) + float(cost)
        for tier, total in sums.items():
            self.assertLessEqual(
                total, budgets[tier], f"the {tier} rows sum to {total} over {budgets[tier]}"
            )

    def test_every_gap_names_a_cell_and_a_reason(self):
        gaps = [line.strip() for line in self.gaps.splitlines() if line.strip() and not line.strip().startswith("#")]
        self.assertGreater(len(gaps), 0, "the draft records no coverage gap")
        for line in gaps:
            cell, separator, reason = line.partition(" = ")
            self.assertTrue(separator, f"gap line {line!r} is not '<cell> = <reason>'")
            self.assertIsNone(CHECK_GATE.cell_problem(cell.strip()), f"gap cell {cell!r} is malformed")
            self.assertTrue(reason.strip(), f"gap {cell!r} records no reason")


if __name__ == "__main__":
    unittest.main()
