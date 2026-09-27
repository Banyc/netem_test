#!/usr/bin/env python3

"""Capture every (value, spec) pair the plot tool actually formats.

`pyformat_diff.py` can cross all recorded CSV values with all the specs the
plot's f-strings mention, and that covers the realized pairs by construction.
It does *not* prove that the values *inside* the plot -- axis bounds, tick
steps, ratios computed from the CSV -- are among the CSV values, and some of
them are not. So this tool records the plot's own `format()` calls instead of
inferring them:

* parse `mandate_plot.py` and rewrite every `JoinedStr`/`FormattedValue` into a
  call recording `(type, repr, spec)` and returning what `format()` would;
* execute the transformed module in a private namespace (the file on disk is
  never written to);
* run it over every recorded run under `--runs`, exactly as
  `tools/mandate_plot.py` would be run by `tools/mandate-check`.

The output is a `spec TAB value-repr` pair list, the fixture
`tools/pyformat-realized-pairs.txt`, which `pyformat_diff.py --pairs` then
checks against `netem-tools py-format` with CPython as the oracle. Regenerate:

```sh
python3 tools/pyformat_instrument.py \
    --runs /Users/charliesmith/code/tmp /private/tmp \
    --out tools/pyformat-realized-pairs.txt
```

Runs that the current plot tool refuses still contribute the calls made before
the refusal, which is where the refusal strings' formatted numbers come from.
"""

import argparse
import ast
import glob
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PLOT = TOOLS / "mandate_plot.py"

RECORDS = []

# The wire protocol's kind tags; the records carry Python type names.
_KIND_TAG = {"float": "f", "int": "i"}


def _record(value, spec, conversion):
    """`FORMAT_VALUE` with a recording side effect; returns `format()`'s text."""
    if conversion == 114:  # 'r'
        obj = repr(value)
    elif conversion == 115:  # 's'
        obj = str(value)
    elif conversion == 97:  # 'a'
        obj = ascii(value)
    else:
        obj = value
    text = format(obj, spec if spec is not None else "")
    RECORDS.append((type(obj).__name__, repr(obj), spec if spec is not None else ""))
    return text


def _spec_expr(node):
    """AST for the spec string of a `FormattedValue` (a `JoinedStr` or None)."""
    if node is None:
        return ast.Constant("")
    if not isinstance(node, ast.JoinedStr):
        raise TypeError(f"unexpected format_spec: {ast.dump(node)}")
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(ast.Constant(value.value))
        else:
            parts.append(_record_call(value))
    if not parts:
        return ast.Constant("")
    out = parts[0]
    for extra in parts[1:]:
        out = ast.BinOp(left=out, op=ast.Add(), right=extra)
    return out


def _record_call(formatted):
    return ast.Call(
        func=ast.Name(id="_record", ctx=ast.Load()),
        args=[
            formatted.value,
            _spec_expr(formatted.format_spec),
            ast.Constant(formatted.conversion),
        ],
        keywords=[],
    )


class _Rewrite(ast.NodeTransformer):
    """Replace every f-string with a concatenation of recording calls."""

    def visit_JoinedStr(self, node):
        # Not `generic_visit`: `_record_call` handles a nested spec itself, and
        # visiting it twice would record the same format call twice.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(ast.Constant(value.value))
            else:
                parts.append(_record_call(value))
        out = parts[0]
        for extra in parts[1:]:
            out = ast.BinOp(left=out, op=ast.Add(), right=extra)
        return ast.copy_location(out, node)


def load_plot():
    """The transformed `mandate_plot` module, without mutating the file."""
    source = PLOT.read_text(encoding="utf-8")
    tree = _Rewrite().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    code = compile(tree, str(PLOT), "exec")
    module = {"__name__": "mandate_plot_instrumented", "__file__": str(PLOT), "__doc__": None}
    module["_record"] = _record
    exec(code, module)
    return module


def recorded_runs(dirs):
    """Every run directory holding both an `M1.json` and a stored `M1-latency`."""
    out = []
    for base in dirs:
        for run in glob.glob(os.path.join(base, "*/")):
            if os.path.isfile(run + "M1.json") and os.path.isfile(
                run + "plots/M1-latency.svg"
            ):
                out.append(run)
    return sorted(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", nargs="+", required=True, metavar="DIR")
    parser.add_argument("--out", type=Path, default=TOOLS / "pyformat-realized-pairs.txt")
    parser.add_argument(
        "--force-refusals",
        action="store_true",
        help=(
            "pass every censoring arm rather than the mandate's own, which makes "
            "the plot refuse; the specs inside its refusal strings are only "
            "reached on that path"
        ),
    )
    parser.add_argument(
        "--all-out",
        type=Path,
        default=None,
        metavar="FILE",
        help="also write every call as `kind TAB repr TAB spec`",
    )
    args = parser.parse_args(argv)

    module = load_plot()
    plot_main = module["main"]
    rendered = refused = 0
    for run in recorded_runs(args.runs):
        report_path = os.path.join(run, "mandate-check.json")
        report = json.load(open(report_path)) if os.path.isfile(report_path) else None
        for mandate_path in sorted(glob.glob(run + "M*.json")):
            mandate_id = os.path.basename(mandate_path)[:-5]
            with tempfile.TemporaryDirectory() as out_dir:
                plot_argv = [mandate_path, "--out", out_dir, "--no-rasterize"]
                if report:
                    mandate = report.get("mandates", {}).get(mandate_id, {})
                    if mandate.get("values"):
                        plot_argv += ["--run-values", json.dumps(mandate["values"])]
                    arms = (report.get("censoring", {}).get("rtp_mux", {}) or {}).get("arms")
                    keep = mandate.get("censoring_arms") or []
                    if args.force_refusals and arms:
                        plot_argv += ["--run-censoring", json.dumps(arms)]
                    elif keep and arms:
                        plot_argv += [
                            "--run-censoring",
                            json.dumps({arm: arms[arm] for arm in keep if arm in arms}),
                        ]
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        status = plot_main(plot_argv)
                    # The plot catches its own refusals and returns 1 rather than
                    # raising, so a raise is not the only refusal signal.
                    if status:
                        refused += 1
                    else:
                        rendered += 1
                except SystemExit:
                    refused += 1
                except Exception:  # noqa: BLE001 - a refused render is expected
                    refused += 1

    kinds = {}
    pairs = {}
    for kind, value, spec in RECORDS:
        kinds[kind] = kinds.get(kind, 0) + 1
        # Strings carry exception messages and paths, may contain newlines, and
        # are only ever formatted with the empty spec (a pass-through). Floats
        # and ints carry the numbers -- including the ones embedded in refusal
        # strings -- so those are what the fixture records.
        if kind in ("float", "int"):
            pairs[(kind, value, spec)] = True

    print(
        f"renders rendered={rendered} refused/other={refused}; "
        f"format calls={len(RECORDS)} kinds={kinds}; "
        f"distinct float/int pairs={len(pairs)}",
        file=sys.stderr,
    )
    if not pairs:
        print("ERROR: no float or int format call was recorded", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(
            "# `kind TAB value-repr TAB spec` for every pair mandate_plot.py\n"
            "# actually formats, captured by tools/pyformat_instrument.py from the\n"
            f"# recorded runs. {rendered} renders, {kinds.get('float', 0)} float and\n"
            f"# {kinds.get('int', 0)} int format calls, {len(pairs)} distinct pairs.\n"
        )
        for kind, value, spec in sorted(pairs):
            handle.write(f"{_KIND_TAG[kind]}\t{value}\t{spec}\n")
    if args.all_out is not None:
        with open(args.all_out, "w", encoding="utf-8") as handle:
            for kind, value, spec in RECORDS:
                handle.write(f"{kind}\t{value}\t{spec}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
