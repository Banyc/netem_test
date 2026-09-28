#!/usr/bin/env python3
"""Apply an anchored mutation to a file, run a check, and restore the file.

A vacuity probe mutates the property a check guards and reads the check's
verdict, and it is only evidence if the mutation *applied*. A pattern that
matched nothing -- or matched somewhere other than the probe believed -- leaves
the check green, which then reads either as "the check has no teeth" or, when
the probe was supposed to go red, as "the check works". Both readings are wrong,
and both have cost this workspace time: a probe whose edit never applied has
been mistaken for a check that cannot fail, and a count made against `rg -c`
counts matching *lines*, so a token that appears twice on one line reads
unchanged after one instance is removed and the count check passes while the
edit applied.

This is the Python side of the mutation instrument the Rust port carries. It
counts *occurrences* rather than lines, prints the mutated line(s) between the
edit and the verdict, restores the file with `touch` rather than `cp -p` (an
older mtime lets a build system that keys on mtime serve the mutated build's
verdict), and proves the file is byte-identical afterwards. An anchor that
matches nothing, or not the declared number of times, fails loudly as
`ANCHOR-MISS` *before* the check runs, so a stale probe cannot be read as a
verdict on the check.

The tool's own message is the better oracle than a count wherever the check can
name the value it rejected, so `Applied.names()` asserts on that message rather
than on the mutation's own shape.

The harness is Python because the suites that use it run their checks in-process
(`CHECK_GATE.check_doc_counts`, `RENDER.extract_svg_panels`, ...), and a
subprocess cannot reach a Python callable. A Rust port of a checker moves its
harness with it; until then this is the one file the Python suites share.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from pathlib import Path

ANCHOR_MISS = "ANCHOR-MISS"
"""The token a stale anchor prints, so a log names the failure it is."""


class AnchorMiss(AssertionError):
    """An anchor matched nothing, or not the number of times declared."""


class Applied:
    """One applied mutation: what changed, and how to read the check's verdict."""

    def __init__(self, path, old, new, declared, occurrences, before, changed):
        self.path = Path(path)
        self.old = old
        self.new = new
        self.declared = declared
        self.occurrences = occurrences
        self.before = before
        self.changed = changed

    def names(self, output, *fragments):
        """Assert the check's own message names every fragment the probe broke.

        The check's message is a better oracle than the mutation's shape: it
        names the value it rejected, so a probe that applied but broke a
        *different* occurrence, or a check that fails for another reason
        entirely, cannot pass as the demonstration it claims to be.
        """
        text = output if isinstance(output, str) else " ".join(str(part) for part in output)
        for fragment in fragments:
            if fragment not in text:
                raise AssertionError(
                    f"the check's own message does not name {fragment!r}: the anchor "
                    f"applied at {len(self.changed)} line(s) of {self.path.name} "
                    f"({self.occurrences} occurrence(s)) but the verdict does not "
                    f"carry the value the mutation broke -- {text}"
                )
        return output


def _changed_lines(before: str, after: str):
    """The `(line number, text)` pairs the mutation changed, one-based."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    changed = []
    for index, line in enumerate(after_lines):
        original = before_lines[index] if index < len(before_lines) else None
        if line != original:
            changed.append((index + 1, line))
    return changed


@contextlib.contextmanager
def mutated(path, old, new, count=1):
    """Apply one anchored mutation to `path`, yielding an `Applied`.

    `old` must occur exactly `count` times; anything else prints `ANCHOR-MISS`
    with the anchor, the occurrences found and the lines the first line of the
    anchor sits on, and raises `AnchorMiss` without touching the file. While the
    body runs the mutation is in place; on exit the file is restored from a
    byte copy and `touch`ed, and a restore that is not byte-identical raises.
    """
    path = Path(path)
    if not path.is_file():
        raise AnchorMiss(f"{ANCHOR_MISS}: {path} does not exist")
    before = path.read_bytes()
    text = before.decode("utf-8")
    occurrences = text.count(old)
    if occurrences != count:
        print(
            f"{ANCHOR_MISS} {path}: anchor {old!r} occurs {occurrences} time(s); "
            f"the probe declared {count}",
            file=sys.stderr,
        )
        for index, line in enumerate(text.splitlines(), 1):
            if old.splitlines()[0] in line:
                print(f"  line {index}: {line}", file=sys.stderr)
        raise AnchorMiss(
            f"{ANCHOR_MISS}: {path}: the anchor occurs {occurrences} time(s) and the "
            f"probe declared {count}, so the mutation would have changed "
            + ("nothing" if occurrences == 0 else "a different place than it says")
        )
    after = text.replace(old, new)
    path.write_text(after, encoding="utf-8")
    changed = _changed_lines(text, after)
    print(
        f"mutation {path}: anchor {old!r} -> {new!r}  occurrences={occurrences}  "
        f"sha256(before)={hashlib.sha256(before).hexdigest()[:16]}"
    )
    for index, line in changed:
        print(f"  line {index}: {line}")
    try:
        yield Applied(path, old, new, count, occurrences, before, changed)
    finally:
        path.write_bytes(before)
        os.utime(path, None)
        restored = path.read_bytes()
        if restored != before:
            raise AssertionError(
                f"{path} was not restored byte-identically by the mutation harness"
            )
        print(
            f"restored {path}: sha256={hashlib.sha256(restored).hexdigest()[:16]} "
            "(touch; byte-identical)"
        )
