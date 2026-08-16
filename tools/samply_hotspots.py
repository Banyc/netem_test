#! /usr/bin/env python3
"""Summarize presymbolicated Samply profiles into owning-symbol hotspots. 

Reads a Samply profile ('.json' or '.json.gz') plus its presymbolicated 
sidecar and reduces the sample stacks to deterministic owning-symbol
hotspots. The default resolver follows Samply's emitted filename first
('profile.json.gz' → 'profile.json.syms.json') and accepts the historical
'profile.syms.json' naming when an older recording uses it. The
sidecar's per-symbol-table inline chain applies to a whole symbol-table
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

SCHEMA_VERSION = 7


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
    for index, library in enumerate(profile.get("libs", [])):
        names = [library.get("debugName"), library.get("name")]
        table = None
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


def _thread_inventory(profile):
    """Summarize every profile thread name before any caller filter applies."""
    grouped = {}
    cpu_active_complete = True
    for thread in profile.get("threads", []):
        name = thread.get("name")
        samples = thread.get("samples", {})
        stacks = samples.get("stack", [])
        nonempty_samples = sum(stack is not None for stack in stacks)
        cpu_deltas = samples.get("threadCPUDelta")
        cpu_deltas_valid = (
            cpu_deltas is not None
            and len(cpu_deltas) == len(stacks)
            and all(isinstance(delta, (int, float)) and delta >= 0 for delta in cpu_deltas)
        )
        cpu_active_samples = (
            sum(stack is not None and delta > 0 for stack, delta in zip(stacks, cpu_deltas))
            if cpu_deltas_valid
            else None
        )
        cpu_active_complete &= cpu_deltas_valid
        record = grouped.setdefault(
            name,
            {
                "name": name,
                "thread_count": 0,
                "sample_count": 0,
                "nonempty_samples": 0,
                "cpu_active_samples": 0,
            },
        )
        record["thread_count"] += 1
        record["sample_count"] += len(stacks)
        record["nonempty_samples"] += nonempty_samples
        if record["cpu_active_samples"] is None or cpu_active_samples is None:
            record["cpu_active_samples"] = None
        else:
            record["cpu_active_samples"] += cpu_active_samples
    total_cpu_active_samples = (
        sum(record["cpu_active_samples"] for record in grouped.values())
        if cpu_active_complete
        else None
    )
    records = list(grouped.values())
    for record in records:
        record["cpu_active_percent"] = (
            100.0 * record["cpu_active_samples"] / total_cpu_active_samples
            if total_cpu_active_samples
            else None
        )
    records.sort(
        key=lambda record: (
            -(record["cpu_active_samples"] or 0),
            -record["nonempty_samples"],
            record["name"] or "",
        )
    )
    return {
        "cpu_active_complete": cpu_active_complete,
        "total_cpu_active_samples": total_cpu_active_samples,
        "threads": records,
    }


def summarize(
    profile,
    symbols,
    contains=(),
    limit=None,
    thread_names=(),
    callers_of=(),
    cpu_active_only=False,
):
    """Reduce a profile's sidecar to owning-symbol hotspots. 

    Each nonempty sample in the selected threads walks its thread-local
    stackTable/frameTable/funcTable/resourceTable and resolves each frame's
    owning symbol through the sidecar symbol tables. The leaf (first) frame
    is counted per sample; each name is counted at most once per sample for
    inclusive total. Percentages use the total nonempty sample count, and
    'contains' is a disjunctive substring filter over hotspot names, and
    'thread_names' is an exact-name thread filter that selects every thread
    with any requested name (zero matches is an error). With
    'cpu_active_only', samples whose aligned Samply threadCPUDelta is zero
    are excluded; missing, nonnumeric, negative, or misaligned CPU deltas are
    errors rather than silently falling back to wall-clock sampling.
    """
    strings = symbols["string_table"]
    symbol_tables = _symbol_tables(profile, symbols)
    thread_inventory = _thread_inventory(profile)
    inclusive = Counter()
    leaf = Counter()
    caller_edges = Counter()
    total = 0
    examined_nonempty = 0
    excluded_zero_cpu = 0
    selected_threads = [
        thread
        for thread in profile.get("threads", [])
        if not thread_names or thread.get("name") in thread_names
    ]
    if thread_names and not selected_threads:
        raise ValueError(
            "no profile threads matched: " + ", ".join(sorted(thread_names))
        )
    for thread in selected_threads:
        samples = thread["samples"]
        sample_stacks = samples["stack"]
        cpu_deltas = samples.get("threadCPUDelta")
        if cpu_active_only:
            if cpu_deltas is None:
                raise ValueError(
                    "CPU-active sampling requested but a selected thread has no "
                    "threadCPUDelta"
                )
            if len(cpu_deltas) != len(sample_stacks):
                raise ValueError(
                    "CPU-active sampling requested but threadCPUDelta and stack "
                    "Lengths differ"
                )
        stacks = thread["stackTable"]
        frames = thread["frameTable"]
        functions = thread["funcTable"]
        resources = thread["resourceTable"]
        thread_strings = thread["stringArray"]
        for sample_index, stack_index in enumerate(sample_stacks):
            if stack_index is None:
                continue
            examined_nonempty += 1
            if cpu_active_only:
                cpu_delta = cpu_deltas[sample_index]
                if not isinstance(cpu_delta, (int, float)) or cpu_delta < 0:
                    raise ValueError(
                        "CPU-active sampling requires nonnegative numeric "
                        "threadCPUDelta values"
                    )
                if cpu_delta == 0:
                    excluded_zero_cpu += 1
                    continue
            stack_frames = []
            while stack_index is not None and stack_index >= 0:
                frame_index = stacks["frame"][stack_index]
                function_index = frames["func"][frame_index]
                name = thread_strings[functions["name"][function_index]]
                resource_index = functions["resource"][function_index]
                frame_files = functions.get("fileName")
                frame_lines = frames.get("line")
                frame_addresses = frames.get("address")
                frame_columns = frames.get("column")
                file_index = (
                    frame_files[function_index]
                    if frame_files is not None
                    else None
                )
                if resource_index is not None:
                    library_index = resources["lib"][resource_index]
                    table = symbol_tables.get(library_index)
                    if table is not None:
                        name = _resolved_name(
                            frames["address"][frame_index],
                            table[0],
                            table[1],
                            strings,
                            name,
                        )
                stack_frames.append(
                    {
                        "name": name,
                        "address": (
                            frame_addresses[frame_index]
                            if frame_addresses is not None
                            else None
                        ),
                        "line": (
                            frame_lines[frame_index]
                            if frame_lines is not None
                            else None
                        ),
                        "column": (
                            frame_columns[frame_index]
                            if frame_columns is not None
                            else None
                        ),
                        "file": (
                            thread_strings[file_index]
                            if file_index is not None
                            else None
                        ),
                    }
                )
                stack_index = stacks["prefix"][stack_index]
            stack_names = [frame["name"] for frame in stack_frames]
            leaf[stack_names[0]] += 1
            for name in set(stack_names):
                inclusive[name] += 1
            seen_edges = set()
            for index, callee in enumerate(stack_names):
                if not any(value in callee for value in callers_of):
                    continue
                caller = next(
                    (
                        frame
                        for frame in stack_frames[index + 1:]
                        if frame["name"] != callee
                    ),
                    None,
                )
                if caller is None:
                    continue
                seen_edges.add(
                    (
                        callee,
                        caller["name"],
                        caller["address"],
                        caller["file"],
                        caller["line"],
                        caller["column"],
                    )
                )
            for edge in seen_edges:
                caller_edges[edge] += 1
    total = examined_nonempty - excluded_zero_cpu
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
    callers = [
        {
            "callee": callee,
            "caller": caller,
            "caller_address": caller_address,
            "caller_file": caller_file,
            "caller_line": caller_line,
            "caller_column": caller_column,
            "samples": samples,
            "percent_of_callee_samples": 100.0 * samples / inclusive[callee],
        }
        for (callee, caller, caller_address, caller_file, caller_line, caller_column),
        samples in caller_edges.items()
    ]
    callers.sort(
        key=lambda edge: (
            -edge["samples"],
            edge["callee"],
            edge["caller"],
            edge["caller_address"] or -1,
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "sample_mode": "cpu-active-only" if cpu_active_only else "wall-samples",
        "total_samples": total,
        "examined_nonempty_samples": examined_nonempty,
        "excluded_zero_cpu_samples": excluded_zero_cpu,
        "contains": list(contains),
        "thread_names": list(thread_names),
        "callers_of": list(callers_of),
        "selected_thread_count": len(selected_threads),
        "thread_inventory": thread_inventory,
        "hotspots": hotspots,
        "callers": callers,
    }


def _load_json(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def default_symbols_path(profile_path):
    # Samply replaces only the final extension: profile.json.gz becomes
    # profile.json.syms.json, while profile.json becomes profile.syms.json.
    emitted = profile_path.with_suffix(".syms.json")
    if emitted.is_file():
        return emitted

    # Older Samply/tooling combinations stripped the whole JSON suffix;
    # keep those preserved recordings readable, but prefer today's emitted name if
    # siblings exist.
    name = str(profile_path)
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            historical = Path(name[: -len(suffix)] + ".syms.json")
            if historical.is_file():
                return historical
    return emitted


def _print_table(result):
    print(f"sample mode: {result['sample_mode']}")
    print(f"total samples: {result['total_samples']}")
    print("profile threads:")
    for thread in result["thread_inventory"]["threads"]:
        active = thread["cpu_active_samples"]
        active_percent = thread["cpu_active_percent"]
        active_text = (
            f"{active:>8} {active_percent:6.2f}%"
            if active is not None and active_percent is not None
            else "       ?       ?"
        )
        print(
            f"{active_text} active "
            f"{thread['nonempty_samples']:>8} nonempty "
            f"{thread['thread_count']:>3} threads "
            f"{thread['name'] or '<unnamed>'}"
        )
    if result["sample_mode"] == "cpu-active-only":
        print(
            "excluded zero-CPU samples: "
            f"{result['excluded_zero_cpu_samples']} / "
            f"{result['examined_nonempty_samples']}"
        )
    if result["contains"]:
        print(f"contains filters: {','.join(result['contains'])}")
    for hotspot in result["hotspots"]:
        print(
            f"{hotspot['inclusive_samples']:>8} {hotspot['inclusive_percent']:6.2f}% "
            f"{hotspot['leaf_samples']:>8} {hotspot['leaf_percent']:6.2f}% {hotspot['name']}"
        )
    if result["callers"]:
        print("callers:")
        for edge in result["callers"]:
            print(
                f"{edge['samples']:>8} {edge['percent_of_callee_samples']:6.2f}% "
                f"{edge['callee']} <- {edge['caller']} "
                f"{edge['caller_file'] or '?'}:{edge['caller_line'] or '?'} "
                f"@{edge['caller_address'] if edge['caller_address'] is not None else '?'}"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Summarize presymbolicated Samply profiles into owning-symbol "
            "hotspots (schema %d)." % SCHEMA_VERSION
        )
    )
    parser.add_argument(
        "--symbols",
        default=None,
        type=Path,
        default=None,
        help=(
            "Sidecar symbols JSON (default: "
            "Samply's emitted sibling, with "
            "historical profile.syms.json fallback)"
        ),
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument(
        "--contains",
        action="append",
        default=[],
        metavar="TEXT",
        help="Keep hotspot names containing TEXT (repeatable; any match wins)",
    )
    parser.add_argument(
        "--thread",
        action="append",
        default=[],
        metavar="NAME",
        help="Retain threads with this exact name; may be repeated",
    )
    parser.add_argument(
        "--callers-of",
        action="append",
        default=[],
        metavar="TEXT",
        help=(
            "Report immediate distinct callers of symbols containing TEXT "
            "(repeatable; any match wins)"
        ),
    )
    parser.add_argument(
        "--cpu-active-only",
        action="store_true",
        help=(
            "Exclude samples with zero threadCPUDelta; fail if selected "
            "threads lack aligned CPU-delta data"
        ),
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
        type=Path,
        help="Write the complete structured summary",
    )
    args = parser.parse_args(argv)

    symbols_path = args.symbols or default_symbols_path(args.profile)
    profile = _load_json(args.profile)
    symbols = _load_json(symbols_path)
    try:
        result = summarize(
            profile,
            symbols,
            thread_names=args.thread,
            contains=args.contains,
            limit=args.limit,
            callers_of=args.callers_of,
            cpu_active_only=args.cpu_active_only,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.json:
        args.json.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
