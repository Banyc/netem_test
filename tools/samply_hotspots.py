#!/usr/bin/env python3
"""Summarize presymbolicated Samply profiles into owning-symbol hotspots.

Reads a Samply profile (`.json` or `.json.gz`) plus its presymbolicated
sidecar (default: `<profile>.syms.json`) and reduces the sample stacks to
deterministic owning-symbol hotspots.

The sidecar's per-symbol-table inline chain applies to a whole symbol-table
entry and cannot locate an inline call; using it can label an entire Tokio
worker as an unrelated inline function, so counts resolve the owning symbol
range (rva .. rva + size) instead.
"""

import argparse
import bisect
from collections import Counter
import gzip
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 2


def _symbol_tables(profile, symbols):
    by_identity = {}
    by_name = {}
    for table in symbols["data"]:
        name = table["debug_name"]
        code_id = table.get("code_id")
        if code_id:
            by_identity[(name, code_id.upper())] = table
            by_name.setdefault(name, []).append(table)
    resolved = {}
    for index, library in enumerate(profile.get("Libs", [])):
        names = [library.get("debugName"), library.get("name")]
        code_id = library.get("codeId")
        if code_id:
            for name in names:
                table = by_identity.get((name, code_id.upper()))
                if table is not None:
                    break
        if table is None:
            for name in names:
                candidates = by_name.get(name, ())
                if len(candidates) == 1:
                    table = candidates[0]
                    break
        if table is None:
            continue
        entries = sorted(table.get("symbol_table", []), key=lambda entry: entry["rva"])
        resolved[index] = (entries, [entry["rva"] for entry in entries])
    return resolved


def _resolved_name(address, table, starts, strings, fallback):
    index = bisect.bisect_right(starts, address) - 1
    if index < 0:
        return fallback
    entry = table[index]
    if address >= entry["rva"] + entry["size"]:
        return fallback
    return strings[entry["symbol"]]


def summarize(profile, symbols, contains=(), limit=None):
    """Reduce a profile+sidecar to owning-symbol hotspots.

    Each nonempty sample in every thread walks its thread-local
    stackTable/frameTable/funcTable/resourceTable and resolves each frame's
    owning symbol through the sidecar symbol tables.  The leaf (first) frame
    is counted per sample; each name is counted at most once per sample for
    the inclusive total.  Percentages use the total nonempty sample count,
    and `contains` is a disjunctive substring filter over hotspot names.
    """
    strings = symbols["string_table"]
    symbol_tables = _symbol_tables(profile, symbols)
    inclusive = Counter()
    leaf = Counter()
    total = 0
    for thread in profile.get("threads", []):
        samples = thread["samples"]
        stacks = thread["stackTable"]
        frames = thread["frameTable"]
        functions = thread["funcTable"]
        resources = thread["resourceTable"]
        thread_strings = thread["stringArray"]
        for stack_index in samples["stack"]:
            if stack_index is None:
                continue
            total += 1
            first = True
            seen = set()
            while stack_index is not None and stack_index >= 0:
                frame_index = stacks["frame"][stack_index]
                function_index = frames["func"][frame_index]
                name = thread_strings[functions["name"][function_index]]
                resource_index = functions["resource"][function_index]
                if resource_index is not None:
                    library_index = resources["Lib"][resource_index]
                    table = symbol_tables.get(library_index)
                    if table is not None:
                        name = _resolved_name(
                            frames["address"][frame_index],
                            table[0],
                            table[1],
                            strings,
                            name,
                        )
                if first:
                    leaf[name] += 1
                    first = False
                if name not in seen:
                    inclusive[name] += 1
                    seen.add(name)
                stack_index = stacks["prefix"][stack_index]
    names = set(inclusive) | set(leaf)
    if contains:
        names = {name for name in names if any(value in name for value in contains)}
    hotspots = [
        {
            "name": name,
            "inclusive_samples": inclusive[name],
            "inclusive_percent": 100.0 * inclusive[name] / total,
            "leaf_samples": leaf[name],
            "leaf_percent": 100.0 * leaf[name] / total,
        }
        for name in names
    ]
    hotspots.sort(key=lambda h: (-h["inclusive_samples"], -h["leaf_samples"], h["name"]))
    if limit is not None:
        hotspots = hotspots[:limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "total_samples": total,
        "contains": list(contains),
        "hotspots": hotspots,
    }


def _load_json(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _default_sidecar_path(profile_path):
    return str(profile_path) + ".syms.json"


def _print_table(result):
    print(f"total samples: {result['total_samples']}")
    if result["contains"]:
        print(f"contains filters: {', '.join(result['contains'])}")
    for hotspot in result["hotspots"]:
        print(
            f"{hotspot['inclusive_samples']:>8}  {hotspot['inclusive_percent']:6.2f}%  "
            f"{hotspot['leaf_samples']:>8}  {hotspot['leaf_percent']:6.2f}%  {hotspot['name']}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Summarize presymbolicated Samply profiles into owning-symbol "
            "hotspots (schema %d)." % SCHEMA_VERSION
        )
    )
    parser.add_argument("profile", help="Samply profile JSON (.json or .json.gz)")
    parser.add_argument(
        "--symbols",
        default=None,
        help="Sidecar symbols JSON (default: <profile>.syms.json)",
    )
    parser.add_argument(
        "--contains",
        action="append",
        default=[],
        metavar="TEXT",
        help="Keep hotspot names containing TEXT (repeatable; any match wins)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Report at most N hotspots",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text table",
    )
    args = parser.parse_args(argv)

    profile_path = Path(args.profile)
    symbols_path = Path(args.symbols) if args.symbols else Path(
        _default_sidecar_path(profile_path)
    )
    profile = _load_json(profile_path)
    symbols = _load_json(symbols_path)
    result = summarize(
        profile, symbols, contains=args.contains, limit=args.limit
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
