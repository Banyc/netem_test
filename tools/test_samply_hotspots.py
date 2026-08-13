"""Tests for tools/samply_hotspots.py."""

import importlib.util
import unittest
from pathlib import Path

TOOLS = Path(__file__).with_name("samply_hotspots.py")
SPEC = importlib.util.spec_from_file_location("samply_hotspots", TOOLS)
HOTSPOTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOTSPOTS)


def _build_stack_table(frame_lists):
    """Encode frame lists (leaf-first) into samply-style stackTable rows."""
    prefix = []
    frames = []
    memo = {}

    def intern(remaining):
        key = tuple(remaining)
        if key in memo:
            return memo[key]
        if not remaining:
            index = -1
        else:
            parent = intern(remaining[1:])
            index = len(prefix)
            prefix.append(parent)
            frames.append(remaining[0])
        memo[key] = index
        return index

    stacks = [intern(frames) for frames in frame_lists]
    return stacks, {"prefix": prefix, "frame": frames}


def sample_profile(frame_lists, frame_to_func, funcs, libs, strings):
    """Build a minimal per-thread samply profile.

    frame_lists: list of frame-index lists (leaf-first) per sample.
    frame_to_func: frame index -> func index.
    funcs: list of (name_index, address, resource_index).
    libs: list of {"debugName", "codeId", "symbol_table"}.
    Strings live in the sidecar's string_table, keyed by the symbol index.
    """
    stacks, stack_table = _build_stack_table(frame_lists)
    return {
        "Libs": libs,
        "threads": [
            {
                "samples": {"stack": stacks},
                "stackTable": stack_table,
                "frameTable": {"func": frame_to_func},
                "funcTable": {
                    "name": [f[0] for f in funcs],
                    "address": [f[1] for f in funcs],
                    "resource": [f[2] for f in funcs],
                },
                "resourceTable": {"Lib": [f[2] for f in funcs]},
                "stringArray": strings,
            }
        ],
    }


def sidecar(libs, strings):
    """Build the presymbolicated sidecar for `libs`; strings are the shared
    string table the symbol entries index into."""
    return {"data": libs, "string_table": strings}


class SamplyHotspotsTest(unittest.TestCase):
    def test_resolves_owning_symbol_and_counts_recursive_inclusive_once(self):
        # One stack: work -> work -> main (leaf-first), where work appears
        # twice (recursion) and frames resolve to owning symbols.
        profile = sample_profile(
            [[1, 2, 0]],  # leaf-first: work, work (recursive), main
            frame_to_func=[0, 1, 1],  # frame0 -> main, frame1/2 -> work
            funcs=[
                (0, 0x1000, 0),  # main
                (1, 0x2000, 0),  # work
            ],
            libs=[
                {
                    "debugName": "libwork.dylib",
                    "codeId": "CAFEBABE",
                    "symbol_table": [
                        {"rva": 0x1000, "size": 0x100, "symbol": 0},
                        {"rva": 0x2000, "size": 0x100, "symbol": 1},
                    ],
                }
            ],
            strings=["main", "work"],
        )
        result = HOTSPOTS.summarize(profile, sidecar([], ["main", "work"]))
        self.assertEqual(result["total_samples"], 1)
        by_name = {h["name"]: h for h in result["hotspots"]}
        self.assertIn("main", by_name)
        self.assertIn("work", by_name)
        # The leaf (first) frame is work; the recursive copy appears twice in
        # the stack but counts at most once for the inclusive total.
        self.assertEqual(by_name["work"]["leaf_samples"], 1)
        self.assertEqual(by_name["work"]["inclusive_samples"], 1)
        self.assertEqual(by_name["main"]["leaf_samples"], 0)
        self.assertEqual(by_name["main"]["inclusive_samples"], 1)
        # Both percentages use the total nonempty sample count.
        self.assertEqual(by_name["work"]["leaf_percent"], 100.0)
        self.assertEqual(by_name["main"]["leaf_percent"], 0.0)

    def test_contains_filter_is_disjunctive(self):
        profile = sample_profile(
            [[0], [1], [2]],
            frame_to_func=[0, 1, 2],
            funcs=[(0, 0x1000, 0), (1, 0x2000, 0), (2, 0x3000, 0)],
            libs=[
                {
                    "debugName": "lib.dylib",
                    "codeId": "1",
                    "symbol_table": [
                        {"rva": 0x1000, "size": 0x100, "symbol": 0},
                        {"rva": 0x2000, "size": 0x100, "symbol": 1},
                        {"rva": 0x3000, "size": 0x100, "symbol": 2},
                    ],
                }
            ],
            strings=["alpha::run", "beta::run", "gamma::run"],
        )
        filtered = HOTSPOTS.summarize(profile, sidecar([], ["alpha::run", "beta::run", "gamma::run"]), contains=["pha", "bet"])
        names = {h["name"] for h in filtered["hotspots"]}
        self.assertEqual(names, {"alpha::run", "beta::run"})
        self.assertNotIn("gamma::run", names)
        # The unfiltered denominator is the total nonempty sample count.
        self.assertEqual(filtered["total_samples"], 3)

    def test_duplicate_library_names_match_code_id(self):
        # Two libraries share debugName but differ by codeId; the owning
        # table must be chosen by (name, code_id) identity.
        profile = sample_profile(
            [[0], [1]],
            frame_to_func=[0, 1],
            funcs=[(0, 0x1000, 0), (1, 0x1000, 1)],
            libs=[
                {
                    "debugName": "libdup.dylib",
                    "codeId": "aaaa",
                    "symbol_table": [{"rva": 0x1000, "size": 0x100, "symbol": 0}],
                },
                {
                    "debugName": "libdup.dylib",
                    "codeId": "BBBB",
                    "symbol_table": [{"rva": 0x1000, "size": 0x100, "symbol": 1}],
                },
            ],
            strings=["first", "second"],
        )
        result = HOTSPOTS.summarize(profile, sidecar([], ["first", "second"]))
        by_name = {h["name"]: h for h in result["hotspots"]}
        self.assertIn("first", by_name)
        self.assertIn("second", by_name)
        self.assertEqual(by_name["first"]["leaf_samples"], 1)
        self.assertEqual(by_name["second"]["leaf_samples"], 1)

    def test_default_sidecar_keeps_json_in_the_name(self):
        path = HOTSPOTS._default_sidecar_path(Path("profile.json"))
        self.assertEqual(path, "profile.json.syms.json")


if __name__ == "__main__":
    unittest.main()
