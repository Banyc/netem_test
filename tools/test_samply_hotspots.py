"""Tests for tools/samply_hotspots.py."""

import importlib.util
import json
import os
import tempfile
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
    frame_to_func: frame index → func index.
    funcs: list of (name_index, address, resource_index).
    libs: list of {"debugName", "codeId", "symbol_table"}.
    Strings live in the sidecar's string_table, keyed by the symbol index.
    """
    stacks, stack_table = _build_stack_table(frame_lists)
    return {
        "libs": libs,
        "threads": [
            {
                "samples": {"stack": stacks},
                "stackTable": stack_table,
                "frameTable": {
                    "address": [funcs[index][1] for index in frame_to_func],
                    "func": frame_to_func,
                },
                "funcTable": {
                    "name": [f[0] for f in funcs],
                    "address": [f[1] for f in funcs],
                    "resource": [f[2] for f in funcs],
                },
                "resourceTable": {"lib": [f[2] for f in funcs]},
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

    def test_thread_filter_selects_all_threads_with_an_exact_name(self):
        # Threads sharing the exact requested name all contribute samples;
        # every other thread is excluded from the summary.
        def thread_with(name, frame_lists):
            profile = sample_profile(
                frame_lists,
                frame_to_func=[0],
                funcs=[(0, 0x1000, 0)],
                libs=[
                    {
                        "debugName": "Lib.dylib",
                        "codeId": "1",
                        "symbol_table": [{"rva": 0x1000, "size": 0x100, "symbol": 0}],
                    }
                ],
                strings=["worker"],
            )
            thread = profile["threads"][0]
            thread["name"] = name
            return thread, profile["libs"]

        worker_a, libs = thread_with("tokio-runtime-worker", [[0], [0]])
        worker_b, _ = thread_with("tokio-runtime-worker", [[0]])
        other, _ = thread_with("main-thread", [[0]])
        profile = {
            "libs": libs,
            "threads": [worker_a, worker_b, other],
        }
        result = HOTSPOTS.summarize(
            profile,
            sidecar([], ["worker"]),
            thread_names=["tokio-runtime-worker"],
        )
        self.assertEqual(result["thread_names"], ["tokio-runtime-worker"])
        self.assertEqual(result["selected_thread_count"], 2)
        self.assertEqual(
            {entry["name"] for entry in result["thread_inventory"]["threads"]},
            {"tokio-runtime-worker", "main-thread"},
        )
        # Three worker samples, none from the excluded thread.
        self.assertEqual(result["total_samples"], 3)
        by_name = {h["name"]: h for h in result["hotspots"]}
        self.assertEqual(by_name["worker"]["inclusive_samples"], 3)
        self.assertEqual(by_name["worker"]["inclusive_percent"], 100.0)

    def test_thread_inventory_reports_all_cpu_owners_before_filtering(self):
        def thread_with(name, cpu_deltas):
            profile = sample_profile(
                [[0] for _ in cpu_deltas],
                frame_to_func=[0],
                funcs=[(0, 0x1000, 0)],
                libs=[],
                strings=["work"],
            )
            thread = profile["threads"][0]
            thread["name"] = name
            thread["samples"]["threadCPUDelta"] = cpu_deltas
            return thread

        profile = {
            "threads": [
                thread_with("tokio-rt-worker", [0, 4]),
                thread_with("tokio-rt-worker", [2]),
                thread_with("netem-c2s", [3, 0, 1]),
            ]
        }
        result = HOTSPOTS.summarize(
            profile,
            sidecar([], []),
            thread_names=["tokio-rt-worker"],
            cpu_active_only=True,
        )
        inventory = result["thread_inventory"]
        self.assertTrue(inventory["cpu_active_complete"])
        self.assertEqual(inventory["total_cpu_active_samples"], 4)
        by_name = {entry["name"]: entry for entry in inventory["threads"]}
        self.assertEqual(by_name["tokio-rt-worker"]["thread_count"], 2)
        self.assertEqual(by_name["tokio-rt-worker"]["cpu_active_samples"], 2)
        self.assertEqual(by_name["tokio-rt-worker"]["cpu_active_percent"], 50.0)
        self.assertEqual(by_name["netem-c2s"]["cpu_active_samples"], 2)
        self.assertEqual(by_name["netem-c2s"]["cpu_active_percent"], 50.0)

    def test_thread_filter_rejects_a_name_absent_from_the_profile(self):
        profile = sample_profile(
            [[0]],
            frame_to_func=[0],
            funcs=[(0, 0x1000, 0)],
            libs=[
                {
                    "debugName": "lib.dylib",
                    "codeId": "1",
                    "symbol_table": [
                        {"rva": 0x1000, "size": 0x100, "symbol": 0},
                    ],
                }
            ],
            strings=["worker"],
        )
        profile["threads"][0]["name"] = "main-thread"
        with self.assertRaisesRegex(
            ValueError, "no profile threads matched: tokio-runtime-worker"
        ):
            HOTSPOTS.summarize(
                profile,
                sidecar([], ["worker"]),
                thread_names=["tokio-runtime-worker"],
            )

    def test_cpu_active_only_excludes_zero_delta_samples(self):
        profile = sample_profile(
            [[0], [1], [1]],
            frame_to_func=[0, 1],
            funcs=[(0, 0x1000, 0), (1, 0x2000, 0)],
            libs=[],
            strings=["idle", "active"],
        )
        profile["threads"][0]["samples"]["threadCPUDelta"] = [0, 4, 2]

        result = HOTSPOTS.summarize(
            profile,
            sidecar([], []),
            cpu_active_only=True,
        )
        self.assertEqual(result["sample_mode"], "cpu-active-only")
        self.assertEqual(result["examined_nonempty_samples"], 3)
        self.assertEqual(result["excluded_zero_cpu_samples"], 1)
        self.assertEqual(result["total_samples"], 2)
        self.assertEqual([h["name"] for h in result["hotspots"]], ["active"])
        self.assertEqual(result["hotspots"][0]["inclusive_percent"], 100.0)

    def test_cpu_active_only_rejects_missing_or_misaligned_deltas(self):
        profile = sample_profile(
            [[0]],
            frame_to_func=[0],
            funcs=[(0, 0x1000, 0)],
            libs=[],
            strings=["work"],
        )
        with self.assertRaisesRegex(ValueError, "has no threadCPUDelta"):
            HOTSPOTS.summarize(
                profile,
                sidecar([], []),
                cpu_active_only=True,
            )
        profile["threads"][0]["samples"]["threadCPUDelta"] = []
        with self.assertRaisesRegex(ValueError, "Lengths differ"):
            HOTSPOTS.summarize(
                profile,
                sidecar([], []),
                cpu_active_only=True,
            )

    def test_reports_immediate_distinct_callers_of_matching_symbols(self):
        profile = sample_profile(
            [[0, 1, 2], [0, 0, 1], [0, 3, 2]],
            frame_to_func=[0, 1, 2, 3],
            funcs=[
                (0, 0x1000, 0),
                (1, 0x2000, 0),
                (2, 0x3000, 0),
                (3, 0x4000, 0),
            ],
            libs=[],
            strings=["mutex:: lock", "rtp:: send", "runtime", "rtp:: recv"],
        )
        result = HOTSPOTS.summarize(
            profile,
            sidecar([], []),
            callers_of=["mutex:: lock"],
        )
        self.assertEqual(result["callers_of"], ["mutex:: lock"])
        self.assertEqual(
            result["callers"],
            [
                {
                    "callee": "mutex:: lock",
                    "caller": "rtp:: send",
                    "caller_address": 0x2000,
                    "caller_file": None,
                    "caller_line": None,
                    "caller_column": None,
                    "samples": 2,
                    "percent_of_callee_samples": 100.0 * 2 / 3,
                },
                {
                    "callee": "mutex:: lock",
                    "caller": "rtp:: recv",
                    "caller_address": 0x4000,
                    "caller_file": None,
                    "caller_line": None,
                    "caller_column": None,
                    "samples": 1,
                    "percent_of_callee_samples": 100.0 / 3,
                },
            ],
        )

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

    def test_default_sidecar_matches_samply_output_name(self):
        self.assertEqual(
            HOTSPOTS.default_symbols_path(Path("profile.json")),
            Path("profile.syms.json"),
        )
        self.assertEqual(
            HOTSPOTS.default_symbols_path(Path("profile.json.gz")),
            Path("profile.json.syms.json"),
        )

    def test_default_sidecar_accepts_historical_and_prefers_emitted_name(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            directory = Path(directory)
            profile_path = directory / "profile.json.gz"
            historical = directory / "profile.syms.json"
            emitted = directory / "profile.json.syms.json"

            historical.touch()
            self.assertEqual(HOTSPOTS.default_symbols_path(profile_path), historical)

            emitted.touch()
            self.assertEqual(HOTSPOTS.default_symbols_path(profile_path), emitted)

    def test_json_output_is_utf8_and_machine_readable(self):
        profile = sample_profile(
            [[0]],
            frame_to_func=[0],
            funcs=[(0, 0x1000, 0)],
            libs=[
                {
                    "debugName": "Libtest.dylib",
                    "codeId": "aaaa",
                    "symbol_table": [{"rva": 0x1000, "size": 0x100, "symbol": 0}],
                }
            ],
            strings=["work"],
        )
        symbols = sidecar([], ["work"])
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            directory = Path(directory)
            profile_path = directory / "profile.json"
            symbols_path = directory / "profile.syms.json"
            output_path = directory / "hotspots.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            symbols_path.write_text(json.dumps(symbols), encoding="utf-8")

            self.assertEqual(
                HOTSPOTS.main([str(profile_path), "--json", str(output_path)]),
                0,
            )
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["total_samples"], 1)
            self.assertEqual(result["hotspots"][0]["name"], "work")
    def test_cpu_delta_ranking_uses_weight_not_sample_count(self):
        profile = sample_profile(
            [[0], [1], [1], [1]],
            frame_to_func=[0, 1],
            funcs=[(0, 0x1000, 0), (1, 0x2000, 0)],
            libs=[],
            strings=["hot", "steady"],
        )
        profile["threads"][0]["samples"]["threadCPUDelta"] = [100, 10, 10, 10]

        result = HOTSPOTS.summarize(
            profile,
            sidecar([], ["hot", "steady"]),
            cpu_active_only=True,
        )
        self.assertEqual(result["sample_mode"], "cpu-active-only")
        self.assertEqual(result["total_samples"], 4)
        self.assertEqual(result["total_cpu_delta"], 130.0)
        # Count-ranked hotspots favor the symbol seen in the most samples...
        self.assertEqual([h["name"] for h in result["hotspots"]], ["steady", "hot"])
        self.assertEqual(result["hotspots"][0]["inclusive_samples"], 3)
        # ... while CPU-ranked hotspots weight ownership by threadCPUDelta.
        self.assertEqual([h["name"] for h in result["cpu_hotspots"]], ["hot", "steady"])
        by_cpu = {h["name"]: h for h in result["cpu_hotspots"]}
        self.assertEqual(by_cpu["hot"]["inclusive_cpu"], 100.0)
        self.assertEqual(by_cpu["steady"]["inclusive_cpu"], 30.0)
        self.assertEqual(by_cpu["hot"]["leaf_cpu"], 100.0)
        self.assertEqual(by_cpu["steady"]["leaf_cpu"], 30.0)
        self.assertAlmostEqual(
            by_cpu["hot"]["inclusive_cpu_percent"], 100.0 * 100.0 / 130.0
        )
        self.assertEqual(result["schema_version"], 9)


    def test_leaf_ranking_surfaces_actual_work_beneath_shared_parents(self):
        # Two samples share an inclusive parent (frame 0); the leaf (frame 1)
        # holds the actual work. Rank-by-leaf must surface frame 1 first.
        # Frame lists are leaf-first: samples 1-2 leaf on leaf_a, sample 3
        # on leaf_b, all sharing caller frame 0 (the parent).
        profile = sample_profile(
            [[1, 0], [1, 0], [2, 0]],
            frame_to_func=[0, 1, 2],
            funcs=[(0, 0x1000, 0), (1, 0x2000, 0), (2, 0x3000, 0)],
            libs=[],
            strings=["parent", "leaf_a", "leaf_b"],
        )
        result = HOTSPOTS.summarize(
            profile,
            sidecar([], ["parent", "leaf_a", "leaf_b"]),
            rank_by="leaf",
        )
        self.assertEqual(
            [h["name"] for h in result["hotspots"]],
            ["leaf_a", "leaf_b", "parent"],
        )
        self.assertEqual(result["hotspots"][0]["leaf_samples"], 2)
        self.assertEqual(result["rank_by"], "leaf")
        self.assertEqual(
            [h["name"] for h in result["cpu_hotspots"]],
            ["leaf_a", "leaf_b", "parent"],
        )

    def test_rejects_unknown_hotspot_ranking(self):
        profile = sample_profile(
            [[0]],
            frame_to_func=[0],
            funcs=[(0, 0x1000, 0)],
            libs=[],
            strings=["only"],
        )
        with self.assertRaisesRegex(ValueError, "unknown hotspot ranking"):
            HOTSPOTS.summarize(
                profile,
                sidecar([], ["only"]),
                rank_by="self",
            )

    def test_cpu_delta_schema_records_weighted_totals(self):
        profile = sample_profile(
            [[0], [1], [1]],
            frame_to_func=[0, 1],
            funcs=[(0, 0x1000, 0), (1, 0x2000, 0)],
            libs=[],
            strings=["a", "b"],
        )
        profile["threads"][0]["samples"]["threadCPUDelta"] = [40, 20, 20]
        result = HOTSPOTS.summarize(
            profile,
            sidecar([], ["a", "b"]),
            cpu_active_only=True,
        )
        self.assertEqual(result["total_cpu_delta"], 80.0)
        self.assertEqual(result["total_samples"], 3)
        by_cpu = {h["name"]: h for h in result["cpu_hotspots"]}
        self.assertEqual(by_cpu["a"]["inclusive_cpu"], 40.0)
        self.assertEqual(by_cpu["b"]["inclusive_cpu"], 40.0)
        self.assertEqual(
            by_cpu["a"]["inclusive_cpu_percent"], 100.0 * 40.0 / 80.0
        )
        self.assertEqual(by_cpu["a"]["leaf_cpu"], 40.0)
        self.assertEqual(by_cpu["b"]["leaf_cpu"], 40.0)
        # Count ranking (wall samples) is unchanged by the CPU weights.
        self.assertEqual(
            [h["name"] for h in result["hotspots"]], ["b", "a"]
        )



if __name__ == "__main__":
    unittest.main()
