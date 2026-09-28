#!/usr/bin/env python3

"""Differential-check `netem-tools py-format` against CPython's `format()`.

The plot tool's port dies on number formatting: Rust has no `{:g}`, and
`{:.4}` means four decimal places where Python's `:.4g` means four significant
digits. `netem-test/src/tools/pyformat.rs` reimplements CPython's rules; this
driver is the proof that it does, by running CPython itself over the recorded
corpus rather than re-deriving the answer a second time in Python.

Two corpora, one oracle:

* **The recorded runs.** Every numeric cell of every `M*.csv` under the run
  directories, deduplicated by exact bit pattern, crossed with the specs the
  plot tool actually applies on the value path. This is the measurement the
  port depends on, and it is a one-off (`--runs <dir>`).
* **The committed corpus.** A bounded, in-tree set of values
  (`pyformat-corpus.txt`) crossed with the whole spec surface — presentation
  types `e E f F g G %` and the empty type, fill/align/sign/`#`/`0`/width/
  grouping and precision. This is what `tools/test_netem_tools.py` runs on
  every suite invocation, so the check is reproducible after the scratch runs
  are gone.
* **The realized pairs.** `pyformat-realized-pairs.txt` holds the exact
  `kind TAB value TAB spec` triples the plot tool formats, ints included,
  captured by `tools/pyformat_instrument.py`. This is the tightest form: the
  plot's own values — axis bounds and tick steps the CSVs do not contain —
  rather than a cross product that merely contains them.

The comparison is exact: an agreement is an identical output string (or, for a
pair both sides reject, an identical rejection). The process is one
`netem-tools py-format` fed `kind TAB value TAB spec` lines and read back
`ok|err TAB text`, so hundreds of thousands of pairs cost one spawn.

Exit status is 0 only when every attempted pair agreed **and** at least one
pair was attempted: an instrument that compared nothing has not checked
anything.
"""

import argparse
import csv
import glob
import os
import subprocess
import sys
import threading
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CRATE = TOOLS.parent
CORPUS = TOOLS / "pyformat-corpus.txt"

# The specs the mandated plot path applies, read out of the Python plot tool's
# f-strings by its own AST before the port to `netem-tools mandate-plot`: `g` x44, `:.1f` x67, `:.4g` x30, `:.0f` x13,
# `:.2f` x14, `:+.4g` x2, `:.6g` x2, `:.1%` x3, `:.0%` x3, `:.2%` x1, plus the
# two dynamic families `:.{decimals}f` (decimals 2..6) and `:.{decimals}g`
# (decimals 2..8). The empty spec is the one `{path}`, `{error}` and the bare
# `{reps}` use, and it is a distinct float behaviour (shortest repr).
# `tools/pyformat-realized-pairs.txt` records which of these the plot actually
# reaches, and a test asserts that set is a subset of this one.
PLOT_SPECS = (
    "",
    "g",
    ".4g",
    "+.4g",
    ".6g",
    ".0f",
    ".1f",
    ".2f",
    ".0%",
    ".1%",
    ".2%",
) + tuple(f".{d}f" for d in range(2, 7)) + tuple(f".{d}g" for d in range(2, 9))

# The whole surface this shim claims, so the check covers the grammar and not
# only the specs today's plot happens to use. `_,.2f` is deliberately invalid:
# it is the pair both sides must reject, which is what keeps the both-rejected
# branch of the comparison exercised.
SURFACE_SPECS = PLOT_SPECS + (
    "e",
    "E",
    "f",
    "F",
    "G",
    "%",
    "n",
    ".0",
    ".1",
    ".4",
    ".0e",
    ".2e",
    ".6e",
    ".2E",
    "#g",
    "#.4g",
    "#.0f",
    "#.0e",
    "#.0%",
    "+.1f",
    " .1f",
    "-.1f",
    "z",
    "z.1f",
    "z.0f",
    "08.1f",
    "010.2f",
    "+010.2f",
    " 010.2f",
    "<10.2f",
    ">10.2f",
    "^10.2f",
    "=10.2f",
    "*^12.3g",
    ",.2f",
    "_,.2f",
    ",.10g",
    ",e",
    "_g",
    ",.0f",
    ",.4g",
)

# Values the recorded corpus does not reach but the grammar has edges at:
# exact ties, the `g` fixed/exponent boundary, the repr boundary at 1e16, and
# the powers of ten spanning 1e-6..1e7 the boundary probe walks.
EDGE_VALUES = (
    2.675,
    1.005,
    0.5,
    1.5,
    2.5,
    0.125,
    0.0625,
    1e23,
    1e22,
    1e17,
    1e16,
    1e15,
    9.999999999999999e15,
    1e7,
    9999999.0,
    9999999.4,
    9999999.5,
    9999999.6,
    1e6,
    999999.0,
    999999.5,
    999999.4,
    1e5,
    99999.5,
    1e4,
    9999.5,
    1234.5,
    1e3,
    999.5,
    100.0,
    99.95,
    9.999,
    1.0,
    0.9999999,
    0.9999995,
    0.999999,
    0.1,
    0.01,
    0.001,
    0.0001,
    9.999999e-5,
    0.00001,
    1e-5,
    1e-6,
    9.9999995e-7,
    1e-7,
    5e-324,
    1.7976931348623157e308,
    0.0,
    -0.0,
    1.0 / 3.0,
    2.0 / 3.0,
    1e-323,
)


def pairs(values, specs):
    """The (value, spec) stream, in the order both the writer and reader see it."""
    for value in values:
        for spec in specs:
            yield value, spec


def binary():
    candidates = (
        CRATE / "target" / "release" / "netem-tools",
        CRATE / "target" / "debug" / "netem-tools",
        CRATE / "netem-test" / "target" / "release" / "netem-tools",
        CRATE / "netem-test" / "target" / "debug" / "netem-tools",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "netem-tools is not built; run "
        "`cargo build -p netem-test --features cli --bin netem-tools`"
    )


def oracle(kind, value, spec):
    """CPython's answer for one tagged request: `ok text` or `err message`."""
    try:
        obj = {"f": float, "i": int, "s": str}[kind](value)
        return "ok", format(obj, spec)
    except Exception as exc:  # noqa: BLE001 - the rejection *is* the answer
        return "err", f"{type(exc).__name__}: {exc}"


def compare(values, specs, tool, max_disagreements=20):
    """Run the differential over `values x specs`.

    `disagreements` is a list of `(value_repr, spec, rust_output, python_output)`
    capped at `max_disagreements`; `messages` counts pairs where both sides
    rejected the pair but with different text (informational: the port owns its
    own error wording, and the recorded specs are all valid).
    """
    return compare_pairs([("f", repr(value), spec) for value, spec in pairs(values, specs)], tool, max_disagreements)


def read_pairs(path):
    """An exact `kind TAB value-repr TAB spec` pair list, comments allowed."""
    out = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            kind, _, rest = line.partition("\t")
            value, _, spec = rest.partition("\t")
            out.append((kind, value, spec))
    return out


def compare_pairs(pair_list, tool, max_disagreements=20):
    """Run the differential over an explicit `(kind, value, spec)` list.

    The same list is walked twice -- once by the writer thread feeding
    `netem-tools py-format`, once by the reader pairing its answers with the
    oracle -- so peer units running latency measurements in the sibling crates
    do not perturb the pairing.
    """
    proc = subprocess.Popen(
        [str(tool), "py-format"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1 << 20,
    )
    failure = []

    def write():
        try:
            for kind, value, spec in pair_list:
                proc.stdin.write(f"{kind}\t{value}\t{spec}\n")
        except Exception as exc:  # noqa: BLE001 - re-raised below
            failure.append(exc)
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001 - already closed
                pass

    writer = threading.Thread(target=write)
    writer.start()

    attempted = agreed = ok_agreed = 0
    disagreements = []
    messages = 0
    for (kind, value, spec), line in zip(pair_list, proc.stdout):
        attempted += 1
        line = line.rstrip("\n")
        status, _, text = line.partition("\t")
        pystatus, pytext = oracle(kind, value, spec)
        if status == "ok" and pystatus == "ok" and text == pytext:
            agreed += 1
            ok_agreed += 1
        elif status == "err" and pystatus == "err":
            # Both sides rejecting is an agreement about the spec, but it is
            # NOT evidence about the formatter: a broken oracle or an unknown
            # kind makes every pair land here. `ok_agreed` is what the caller
            # must guard on.
            agreed += 1
            if text != pytext:
                messages += 1
        elif len(disagreements) < max_disagreements:
            disagreements.append(
                (f"{kind}:{value}", spec, line, f"{pystatus}\t{pytext}")
            )
    writer.join()
    if failure:
        raise failure[0]
    if proc.wait() != 0:
        raise SystemExit(f"{tool} py-format exited {proc.returncode}")
    return attempted, agreed, ok_agreed, disagreements, messages


def read_corpus(path):
    """The committed corpus: one `repr` per line, comments allowed."""
    values = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            values.append(float(text))
    return values


def run_values(dirs):
    """Every numeric CSV cell under `<dir>/*/M*.csv`, deduplicated by bits."""
    seen = {}
    files = []
    for base in dirs:
        for run in sorted(glob.glob(os.path.join(base, "*/"))):
            files.extend(sorted(glob.glob(os.path.join(run, "M*.csv"))))
    for path in files:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for row in reader:
                for cell in row:
                    try:
                        value = float(cell)
                    except ValueError:
                        continue
                    # `float.hex()` distinguishes -0.0 from 0.0, which a
                    # plain set would collapse.
                    seen.setdefault(value.hex(), value)
    return list(seen.values()), len(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--runs",
        nargs="*",
        default=None,
        metavar="DIR",
        help="scan DIR/*/M*.csv for the recorded corpus (one-off measurement)",
    )
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument(
        "--pairs",
        type=Path,
        default=None,
        metavar="FILE",
        help="an exact `kind TAB value TAB spec` pair list (the plot's own calls)",
    )
    parser.add_argument(
        "--specs",
        choices=("plot", "surface"),
        default="surface",
        help="the spec set to cross the values with",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    parser.add_argument("--max-disagreements", type=int, default=20)
    args = parser.parse_args(argv)

    specs = PLOT_SPECS if args.specs == "plot" else SURFACE_SPECS
    tool = binary()
    if args.pairs is not None:
        pair_list = read_pairs(args.pairs)
        source = f"{len(pair_list)} realized (kind, value, spec) pairs from {args.pairs.name}"
        attempted, agreed, ok_agreed, disagreements, messages = compare_pairs(
            pair_list, tool, args.max_disagreements
        )
    else:
        if args.runs is not None:
            values, files = run_values(args.runs)
            source = f"{len(values)} unique values from {files} recorded CSVs x {len(specs)} specs"
        else:
            values = read_corpus(args.corpus) + list(EDGE_VALUES)
            source = f"{len(values)} values from {args.corpus.name} + edge values x {len(specs)} specs"
        if args.limit is not None:
            values = values[: args.limit]
        attempted, agreed, ok_agreed, disagreements, messages = compare(
            values, specs, tool, args.max_disagreements
        )

    print(f"py-format differential: {source}")
    print(f"  pairs attempted = {attempted}")
    print(f"  agreements      = {agreed}")
    print(
        "  formatted pairs = "
        f"{ok_agreed}  (pairs both sides formatted; the rest both rejected)"
    )
    print(f"  disagreements   = {attempted - agreed}")
    print(f"  both-rejected pairs whose message text differs = {messages} (informational)")
    for value, spec, rust, python in disagreements:
        print(f"  DISAGREE value={value} spec={spec!r} rust={rust!r} python={python!r}")
    if attempted == 0:
        print("  ERROR: no pair was attempted", file=sys.stderr)
        return 1
    if ok_agreed == 0:
        print(
            "  ERROR: every pair was rejected by both sides, so nothing was "
            "formatted; this is not evidence about the formatter",
            file=sys.stderr,
        )
        return 1
    return 0 if agreed == attempted else 1


if __name__ == "__main__":
    sys.exit(main())
