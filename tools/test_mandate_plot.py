#!/usr/bin/env python3

"""Exercise `netem-tools mandate-plot`'s render, its refusals and its summary.

The plotter was `tools/mandate_plot.py`, imported in-process by `mandate-check`;
it is now the Rust `netem-tools mandate-plot` subcommand, so this suite drives
the *binary* and checks what a run's reader sees: the panel the panel renders,
the summary beside it, the refusal on stderr and the exit status. The checks
that read an artifact the binary alone can build are unit-tested where they
live, in `netem-test/src/tools/mandate_plot/`; everything a declaration and a
CSV can drive is here, against the command line.

Every refusal case is paired with the render that is green on the same rule
where that is what makes the refusal a measurement rather than a predicate that
is true of everything.
"""

import base64
import csv
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CRATE = TOOLS.parent


def binary():
    """The built `netem-tools`, wherever the build put it.

    A missing binary is a failure and not a skip: the panels are the evidence a
    run is read from, so a suite that cannot reach the renderer has checked
    nothing.
    """
    candidates = (
        CRATE / "target" / "release" / "netem-tools",
        CRATE / "target" / "debug" / "netem-tools",
        CRATE / "netem-test" / "target" / "release" / "netem-tools",
        CRATE / "netem-test" / "target" / "debug" / "netem-tools",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "netem-tools is not built; build it with "
        "`cargo build -p netem-test --features cli --bin netem-tools` before running this suite"
    )


BINARY = binary()

# A browser that must never run: the `--no-rasterize` case sets this so a run
# that consulted a browser anyway fails loudly instead of passing by luck.
FORBIDDEN_BROWSER = """#!/usr/bin/env python3
import sys

print("a browser was consulted", file=sys.stderr)
sys.exit(3)
"""


def _latency_rows(points):
    """The one-series latency CSV a census-of-shape fixture is built from."""
    return [["panel", "series", "x", "y"]] + [
        ["latency", "lone_tail", x, y] for x, y in points
    ]


# A healthy M1 declaration: a latency line panel carrying the mandate's own
# ceiling as a bound, and a latency CDF panel with no horizontal bound.
HEALTHY_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "RTT (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "impaired", "role": "impaired"}],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        },
        {
            "id": "cdf",
            "chart": "cdf",
            "series": [{"name": "impaired"}],
            "bounds": [],
        },
    ],
}

HEALTHY_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "impaired", 0.0, 12.5],
    ["latency", "impaired", 1.0, 31.5],
    ["latency", "impaired", 2.0, 88.25],
    ["cdf", "impaired", 12.5, 33.3],
    ["cdf", "impaired", 31.5, 66.7],
    ["cdf", "impaired", 88.25, 100.0],
]

BAR_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput",
    "x_label": "seed",
    "y_label": "goodput (MiB/s)",
    "panels": [
        {
            "id": "goodput",
            "chart": "bar",
            "series": [{"name": "candidate"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

BAR_ROWS = [
    ["panel", "series", "x", "y"],
    ["goodput", "candidate", 11.0, 0.52],
    ["goodput", "candidate", 21.0, 0.61],
    ["goodput", "candidate", 31.0, 0.47],
]

# Minimal 1x1 PNG a fake browser writes to its --screenshot target.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

FAKE_BROWSER = """#!/usr/bin/env python3
import sys

PNG = {png!r}
out = None
for argument in sys.argv:
    if argument.startswith("--screenshot="):
        out = argument.split("=", 1)[1]
if out:
    open(out, "wb").write(PNG)
"""

NULL_BROWSER = """#!/usr/bin/env python3
# Deliberately writes nothing, like a browser that silently produced no PNG.
"""


# The M2 and M4 panels of the preserved battery run in which the audit found
# panels drawn so that their own bound could not be seen (`AUDIT_COVERAGE.md`,
# "Plots that cannot show their own failure"). These declarations and rows are
# that run's own, so the axis tests below are about that run's numbers.
DELIVERY_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "delivery",
            "chart": "bar",
            "y_label": "delivery (received / offered)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [
                {"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}
            ],
        }
    ],
}

DELIVERY_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "clean", 1.0, 1.0],
    ["delivery", "clean", 2.0, 1.0],
    ["delivery", "clean", 3.0, 1.0],
    ["delivery", "clean", 4.0, 1.0],
    ["delivery", "hostile", 1.0, 1.0],
    ["delivery", "hostile", 2.0, 1.0],
    ["delivery", "hostile", 3.0, 1.0],
    ["delivery", "hostile", 4.0, 1.0],
]

# The `MANDATE_SMOKE_FAULT=M4_drop` render (99 % loss): the four clean flows
# were starved to a share of 0.000, which is an imbalance of -1.000 against a
# 0.25 ideal, while the hostile arm stayed inside its band. The axis is
# therefore the *data's* scale (-1..0.0605) and the declared `±1 %` band is
# 0.009884 of it -- 2.1 px of 228 -- which the axis test refused, deleting the
# panel whose whole point is that the fault's guard fired.
DROP_IMBALANCE_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "departure from the fair share",
    "panels": [
        {
            "id": "imbalance",
            "chart": "bar",
            "y_label": "departure from the fair share",
            "x_label": "flow (1..4)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.01, "label": "fair-share bound \u00b11.0%"}],
        }
    ],
}

DROP_IMBALANCE_ROWS = [
    ["panel", "series", "x", "y"],
    ["imbalance", "clean", 1.0, -1.0],
    ["imbalance", "clean", 2.0, -1.0],
    ["imbalance", "clean", 3.0, -1.0],
    ["imbalance", "clean", 4.0, -1.0],
    ["imbalance", "hostile", 1.0, 0.000116],
    ["imbalance", "hostile", 2.0, 0.000116],
    ["imbalance", "hostile", 3.0, -0.000347],
    ["imbalance", "hostile", 4.0, 0.000116],
]

# A line panel whose lone tail crosses its ceiling: the 1600 ms sample is the
# minority beyond 250 ms, and the run states the arm's own 3200 ms p99 guard, so
# the panel owes the reader which bound governs the arm that departed.
CROSSING_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency (clean vs hostile vs lone tail)",
    "x_label": "elapsed time (s)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [
                {"name": "clean"},
                {"name": "hostile"},
                {"name": "lone_tail"},
            ],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        }
    ],
}

CROSSING_ROWS = (
    [["panel", "series", "x", "y"]]
    + [["latency", "clean", float(i), 20.0 + i] for i in range(10)]
    + [["latency", "hostile", float(i), 100.0 + i] for i in range(10)]
    + [["latency", "lone_tail", float(i), 100.0 + i] for i in range(9)]
    + [["latency", "lone_tail", 9.0, 1600.0]]
)

CROSSING_SERIES = [
    ("clean", [(float(i), 20.0 + i) for i in range(10)]),
    ("hostile", [(float(i), 100.0 + i) for i in range(10)]),
    ("lone_tail", [(float(i), 100.0 + i) for i in range(9)] + [(9.0, 1600.0)]),
]

CROSSING_GUARDS = {
    "ceiling": 250.0,
    "clean_p99": 27.0,
    "hostile_p99_guard": 900.0,
    "hostile_over250_guard": 8.0,
    "lone_p99_guard": 3200.0,
    "lone_over250_guard": 8.0,
}

# The `M4-latency` shape: four statistic series, one bar per flow, and a run
# guard that belongs to a single series (`hostile_p99_guard=900`). The ceiling
# is crossed by the one `hostile_p99` flow above it.
SERIES_GUARD_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "bar",
            "y_label": "latency (ms)",
            "series": [
                {"name": "clean_p50"},
                {"name": "clean_p99"},
                {"name": "hostile_p50"},
                {"name": "hostile_p99"},
            ],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        }
    ],
}

SERIES_GUARD_ROWS = (
    [["panel", "series", "x", "y"]]
    + [["latency", "clean_p50", float(i), 22.0] for i in (1, 2, 3, 4)]
    + [["latency", "clean_p99", float(i), 180.0] for i in (1, 2, 3, 4)]
    + [["latency", "hostile_p50", float(i), 70.0] for i in (1, 2, 3, 4)]
    + [["latency", "hostile_p99", 1.0, 1000.0]]
    + [["latency", "hostile_p99", float(i), 340.0] for i in (2, 3, 4)]
)

SERIES_GUARD_VALUES = {"ceiling": 250.0, "hostile_p99_guard": 900.0, "flows": 4}

# A line panel one lone-tail outlier would otherwise set the axis on: three
# bodies whose own ranges are tens of milliseconds against a 250 ms ceiling, and
# a single 1400 ms sample. The bodies are what the reader compares, so the axis
# is clipped at the value the panel is read against.
CLIP_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency (clean vs hostile vs lone tail)",
    "x_label": "elapsed time (s)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [
                {"name": "clean"},
                {"name": "hostile"},
                {"name": "lone_tail"},
            ],
            "bounds": [
                {
                    "y": 250.0,
                    "label": "M1 ceiling 250 ms",
                    "series": "lone_tail",
                }
            ],
        }
    ],
}

CLIP_ROWS = (
    [["panel", "series", "x", "y"]]
    + [["latency", "clean", float(i), 20.0 + (i % 20)] for i in range(200)]
    + [["latency", "hostile", float(i), 40.0 + 2 * (i % 20)] for i in range(200)]
    + [["latency", "lone_tail", float(i), 10.0 + (i % 20)] for i in range(200)]
    + [["latency", "lone_tail", 199.0, 1400.0]]
)

CLIP_SERIES = [
    ("clean", [(float(i), 20.0 + (i % 20)) for i in range(200)]),
    ("hostile", [(float(i), 40.0 + 2 * (i % 20)) for i in range(200)]),
    (
        "lone_tail",
        [(float(i), 10.0 + (i % 20)) for i in range(200)] + [(199.0, 1400.0)],
    ),
]

DROP_IMBALANCE_SERIES = [
    ("clean", [(float(x), -1.0) for x in (1, 2, 3, 4)]),
    (
        "hostile",
        [
            (1.0, 0.000116),
            (2.0, 0.000116),
            (3.0, -0.000347),
            (4.0, 0.000116),
        ],
    ),
]

# The `MANDATE_SMOKE_FAULT=M4_late` render (every flow's last stretch held past
# the cutoff, with the early body repaired late): the clean arm's p50 and p99
# are 3.9-4.3 s against a 250 ms ceiling, so the ceiling is 1.6 px of a
# 0..4565.34 axis -- the same refusal, on the latency panel, of a render whose
# verdict is PASS and whose panel is the evidence the fault showed.
LATE_LATENCY_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "bar",
            "y_label": "latency (ms)",
            "x_label": "flow (1..4)",
            "series": [
                {"name": "clean_p50"},
                {"name": "clean_p99"},
                {"name": "hostile_p50"},
                {"name": "hostile_p99"},
            ],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        }
    ],
}

LATE_LATENCY_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "clean_p50", 1.0, 3886.742],
    ["latency", "clean_p50", 2.0, 3882.2],
    ["latency", "clean_p50", 3.0, 3835.351],
    ["latency", "clean_p50", 4.0, 3467.733],
    ["latency", "clean_p99", 1.0, 4345.298],
    ["latency", "clean_p99", 2.0, 4347.945],
    ["latency", "clean_p99", 3.0, 4346.328],
    ["latency", "clean_p99", 4.0, 4312.403],
    ["latency", "hostile_p50", 1.0, 81.0],
    ["latency", "hostile_p50", 2.0, 72.365],
    ["latency", "hostile_p50", 3.0, 72.431],
    ["latency", "hostile_p50", 4.0, 85.271],
    ["latency", "hostile_p99", 1.0, 282.427],
    ["latency", "hostile_p99", 2.0, 308.469],
    ["latency", "hostile_p99", 3.0, 313.148],
    ["latency", "hostile_p99", 4.0, 294.569],
]

# The run's own `MANDATE M4` keys the panel's labels name: the ceiling is the
# declaration's, and `hostile_p99_guard` is the arm's own tolerance, so the
# panel draws both lines and the axis test measures the ceiling against it.
LATE_LATENCY_RUN_VALUES = {"ceiling": 250.0, "hostile_p99_guard": 900.0}

# The live `M2-latency` panel: the interactive arms' p99 against the
# non-degrading bound under the known offer. `M2_LATENCY_RUN_VALUES` is a run
# that states a guard of its own for the two impaired arms, which is what makes
# the crossing on the lone-tail bar attributable.
M2_LATENCY_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and latency under a known offer "
    "(1=clean 2=hostile 3=lone_tail)",
    "x_label": "arm (1=clean 2=hostile 3=lone_tail)",
    "y_label": "value",
    "panels": [
        {
            "id": "latency",
            "chart": "bar",
            "series": [{"name": "p99_ms"}],
            "bounds": [
                {
                    "y": 100.0,
                    "label": "M2 non-degrading p99 bound (ms)",
                    "x": [1],
                }
            ],
        }
    ],
}

M2_LATENCY_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "p99_ms", 1.0, 26.251],
    ["latency", "p99_ms", 2.0, 61.5],
    ["latency", "p99_ms", 3.0, 185.8015],
]

# The run's own measurements for the latency panel: the per-arm p99 the panel
# plots and the two impaired arms' guards, which are the bounds the crossing
# lone bar is read against instead of the clean arm's non-degrading bound.
M2_LATENCY_RUN_VALUES = {
    "clean_p99_ms": 26.251,
    "hostile_p99_ms": 61.5,
    "lone_p99_ms": 185.8015,
    "hostile_p99_guard": 200.0,
    "lone_p99_guard": 400.0,
}

# The run's per-arm guards for a four-arm latency panel, which is the shape that
# makes the attribution label long enough to wrap.
M2_LATENCY_FOUR_ARM_RUN_VALUES = {
    "clean_p99_guard": 300.0,
    "hostile_p99_guard": 200.0,
    "lone_p99_guard": 400.0,
    "burst_p99_guard": 600.0,
}

# Widths of the labels that run drew, measured on its own standalone panels by
# headless Chrome (`getBBox().width`, 11px text with no font-family declared,
# resolved as Times). They are the fixtures the width model is calibrated
# against: the model is a *model*, so the only thing that keeps it honest is a
# check that fails when it starts underestimating the drawn text.
RENDERED_LABEL_WIDTHS = (
    (
        "M2 non-degrading p99 bound (ms) [1 of 3 bars beyond it; run guards "
        "hostile_p99_guard=200 lone_p99_guard=400]",
        512.22,
    ),
    (
        "M1 ceiling 250 ms [4 of 16 bars beyond it; run guards "
        "hostile_p99_guard=900]",
        349.0,
    ),
    ("M4 per-flow delivery floor 0.995", 144.94),
    ("fair-share bound \u00b11.0%", 103.94),
    ("fair share 25.0%", 72.36),
    ("M3 floor 0.35x link rate", 105.37),
    ("M2 delivery floor 1.000", 105.1),
    ("M1 ceiling 250 ms", 82.77),
)

SHARES_ROWS = [
    ["panel", "series", "x", "y"],
    ["shares", "clean", 1.0, 0.250059],
    ["shares", "clean", 2.0, 0.250059],
    ["shares", "clean", 3.0, 0.249941],
    ["shares", "clean", 4.0, 0.249941],
    ["shares", "hostile", 1.0, 0.250173],
    ["shares", "hostile", 2.0, 0.249365],
    ["shares", "hostile", 3.0, 0.250289],
    ["shares", "hostile", 4.0, 0.250173],
]

SHARES_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.250000, "label": "fair share 25.0%"}],
        }
    ],
}

# The recorded `MANDATE M2` line of a real run, whose clean arm is asserted at
# `1.000` while the impaired arms are asserted at its own `delivery_floor` of
# `0.995` -- the two floors the delivery panel has to draw per arm.
M2_RUN_VALUES = {
    "clean_delivery": 1.0,
    "hostile_delivery": 1.0,
    "lone_delivery": 1.0,
    # The impaired arms' own delivery floors mark them as not the reference arm.
    "hostile_delivery_guard": 0.995,
    "lone_delivery_guard": 0.995,
    "delivery_floor": 0.995,
}

# The recorded `MANDATE M4` line, whose per-flow delivery floor is `0.995` on
# *both* arms -- the same value the declaration draws -- so there is nothing
# restated and no per-arm split is owed.
M4_DELIVERY_RUN_VALUES = {
    "flows": 4.0,
    "clean_delivery_min": 1.0,
    "hostile_delivery_min": 1.0,
    "imbalance_bound": 0.01,
    "fair_share": 0.25,
    "delivery_floor": 0.995,
    "hostile_p99_guard": 900.0,
}

# The recorded `M4-shares`/`M4-imbalance` pair, verbatim from a real run's
# `M4.csv`: the shares straddle the fair share (0.249912 and 0.250029 around
# 0.25), so no bar can fail the line the panel draws, and the imbalance panel
# carries the departure `(share - 0.25) / 0.25` to the evidence files' own
# six-decimal resolution.
SHARES_IMBALANCE_ROWS = [
    ["panel", "series", "x", "y"],
    ["shares", "clean", 1.0, 0.250029],
    ["imbalance", "clean", 1.0, 0.000118],
    ["shares", "clean", 2.0, 0.250029],
    ["imbalance", "clean", 2.0, 0.000118],
    ["shares", "clean", 3.0, 0.250029],
    ["imbalance", "clean", 3.0, 0.000118],
    ["shares", "clean", 4.0, 0.249912],
    ["imbalance", "clean", 4.0, -0.000353],
    ["shares", "hostile", 1.0, 0.250029],
    ["imbalance", "hostile", 1.0, 0.000114],
    ["shares", "hostile", 2.0, 0.249914],
    ["imbalance", "hostile", 2.0, -0.000343],
    ["shares", "hostile", 3.0, 0.250029],
    ["imbalance", "hostile", 3.0, 0.000114],
    ["shares", "hostile", 4.0, 0.250029],
    ["imbalance", "hostile", 4.0, 0.000114],
]

SHARES_IMBALANCE_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.250000, "label": "fair share 25.0%"}],
        },
        {
            "id": "imbalance",
            "chart": "bar",
            "y_label": "departure from the fair share",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.01, "label": "fair-share bound \u00b11.0%"}],
        },
    ],
}

FRACTION_ROWS = [
    ["panel", "series", "x", "y"],
    ["fraction", "fraction", 1.0, 0.958217],
    ["fraction", "fraction", 2.0, 0.958271],
    ["fraction", "fraction", 3.0, 0.963341],
]

FRACTION_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput vs the shaped clock and the configured link rate",
    "x_label": "seed",
    "y_label": "fraction of link rate",
    "panels": [
        {
            "id": "fraction",
            "chart": "bar",
            "series": [{"name": "fraction"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

# The M2 delivery panel of that run: one series at the ideal, with the floor
# exactly at it. The axis is the band view (the floor *is* the scale), so the
# bound lands a few pixels below the plot's top and the label has nowhere to go
# above it -- the placement the audit measured four pixels above the plot.
M2_DELIVERY_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and p99 ms",
    "x_label": "arm (1=clean 2=hostile 3=lone_tail)",
    "y_label": "delivery (received / offered)",
    "panels": [
        {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "delivery"}],
            "bounds": [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        }
    ],
}

M2_DELIVERY_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "delivery", 1.0, 1.0],
    ["delivery", "delivery", 2.0, 1.0],
    ["delivery", "delivery", 3.0, 1.0],
]

# The tail of the recorded `M1-latency` `lone_tail` series that caused the
# misreading this renderer's gap rule comes from: one 2.65 s period nobody
# observed (13.11 s -> 15.76 s), a peak at the far side of it, and a return to
# 29.9 ms thirty milliseconds later. The thirty samples are that run's own
# `latency,lone_tail,<x>,<y>` rows, so the fixture is the shape a real line
# panel is asked to draw and not a shape invented to make the rule fire.
REAL_LONE_TAIL_TAIL = (
    (13.103597, 17.158834),
    (13.105368, 1.772375),
    (15.757062, 2651.693959),
    (15.786949, 29.887334),
    (15.799594, 12.644959),
    (15.804529, 4.935167),
    (15.817246, 12.717500),
    (15.898248, 81.002500),
    (15.902922, 4.673834),
    (15.903107, 0.186292),
    (16.011494, 108.387459),
    (16.025917, 14.422792),
    (16.056668, 30.752000),
    (16.103649, 46.981334),
    (16.106230, 2.581125),
    (16.106516, 0.286167),
    (16.120773, 14.257167),
    (16.155288, 34.514834),
    (16.155476, 0.187584),
    (16.263466, 107.991250),
    (16.321342, 57.875667),
    (16.321571, 0.230125),
    (16.321734, 0.162500),
    (16.321849, 0.115959),
    (16.370935, 49.085959),
    (16.371099, 0.163542),
    (16.484877, 113.779084),
    (16.485060, 0.183000),
    (16.485216, 0.156125),
    (16.545019, 59.803375),
)

# A ladder the window cut off: every sample above the one before it, and the
# last sample the series maximum. On the panel this is the *same* shape as a
# peak that returned -- which is why the run's own verdict, and not the
# reader's eye, has to say which one it is.
TRUNCATED_CLIMB = tuple((index * 0.25, index * 300.0) for index in range(1, 7))

LONE_TAIL_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "lone_tail"}],
            "bounds": [],
        }
    ],
}




# A real run's M1 readings, and the shape both of its panels are about: the
# line panel draws one 250 ms ceiling across three arms whose own guards differ
# by more than the ceiling itself, and the CDF beside it draws the same three
# distributions with no horizontal bound at all. `clean` reached the ceiling's
# side of its distribution at 107.674 ms, so on a linear axis it is 6.9 % of the
# width -- the CDF reference-reach case the renderer answers with a log axis.
M1_RUN_VALUES = {
    "clean_p50": 24.4,
    "clean_p99": 93.1,
    "clean_max": 107.7,
    "hostile_p50": 45.8,
    "hostile_p99": 231.8,
    "hostile_max": 277.1,
    "lone_p50": 0.2,
    "lone_p99": 166.4,
    "lone_p999": 1465.8,
    "lone_max": 1567.1,
    "ceiling": 250.0,
    "hostile_p99_guard": 900.0,
    "hostile_over250_guard": 8.0,
    "lone_p99_guard": 3200.0,
    "lone_p999_guard": 8000.0,
    "lone_over250_guard": 8.0,
}

M1_ARMS_DECLARATION = {
    "mandate": "M1",
    "title": "M1 interactive tail latency",
    "x_label": "elapsed time (s)",
    "y_label": "latency (ms)",
    "panels": [
        {
            "id": "latency",
            "chart": "line",
            "series": [{"name": "clean"}, {"name": "hostile"}, {"name": "lone_tail"}],
            "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
        },
        {
            "id": "cdf",
            "chart": "cdf",
            "x_label": "latency (ms)",
            "y_label": "percentile (%)",
            "series": [{"name": "clean"}, {"name": "hostile"}, {"name": "lone_tail"}],
            "bounds": [],
        },
    ],
}

M1_ARMS_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "clean", 1.53, 20.137],
    ["latency", "clean", 2.06, 107.674],
    ["latency", "clean", 13.52, 20.8],
    ["latency", "hostile", 1.5, 0.05],
    ["latency", "hostile", 2.44, 277.114],
    ["latency", "hostile", 13.55, 69.55],
    ["latency", "lone_tail", 1.5, 0.092417],
    ["latency", "lone_tail", 10.87, 1567.111],
    ["latency", "lone_tail", 17.23, 1466.0],
    ["cdf", "clean", 20.137, 0.0],
    ["cdf", "clean", 85.316, 98.0],
    ["cdf", "clean", 93.088, 99.0],
    ["cdf", "clean", 107.674, 100.0],
    ["cdf", "hostile", 0.05, 0.0],
    ["cdf", "hostile", 213.408, 98.0],
    ["cdf", "hostile", 231.837, 99.0],
    ["cdf", "hostile", 277.114, 100.0],
    ["cdf", "lone_tail", 0.092417, 0.0],
    ["cdf", "lone_tail", 122.818459, 98.0],
    ["cdf", "lone_tail", 172.260084, 99.0],
    ["cdf", "lone_tail", 1567.110834, 100.0],
]


class MandatePlotTest(unittest.TestCase):
    """The command-line contract: render, refuse, and state what was drawn."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)
        self.out = self.root / "panels"

    def tearDown(self):
        self._tmp.cleanup()

    def write_mandate(self, declaration=None, rows=HEALTHY_ROWS, name="M1"):
        path = self.root / f"{name}.json"
        path.write_text(
            json.dumps(HEALTHY_DECLARATION if declaration is None else declaration),
            encoding="utf-8",
        )
        if rows is not None:
            data = self.root / f"{name}.csv"
            if isinstance(rows, str):
                data.write_text(rows, encoding="utf-8")
            else:
                with data.open("w", newline="", encoding="utf-8") as sink:
                    csv.writer(sink).writerows(rows)
        return path

    def write_browser(self, source):
        path = self.root / "fake-browser"
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def run_main(self, *arguments, env=None):
        """Run the plotter's command line, as `mandate-check` runs it."""
        environment = dict(os.environ)
        environment.pop("NETEM_RENDER_BROWSER", None)
        environment.update(env or {})
        completed = subprocess.run(
            [str(BINARY), "mandate-plot", *(str(argument) for argument in arguments)],
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def reject(self, declaration=None, rows=HEALTHY_ROWS, fragment=""):
        """Assert one declaration/data pair is refused, naming the problem."""
        declaration_path = self.write_mandate(declaration, rows)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0, f"expected a non-zero exit; stderr={stderr}")
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn(fragment, stderr)
        return stderr

    # -- the axis test: a panel must be able to show its own bound -----------

    def render_mandate(self, declaration, rows, name, *arguments):
        """Render one declaration and return ``(exit code, stderr, out dir)``."""
        out = self.root / f"out-{name}"
        declaration_path = self.write_mandate(declaration, rows, name=name)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(out), *arguments
        )
        return code, stderr, out


    def test_a_mis_scaled_axis_fails_the_whole_render(self):
        # The vacuity half: the same unchanged data on the axis the audit found
        # cannot be rendered at all, rather than silently drawn at half scale.
        declaration = {
            **DELIVERY_DECLARATION,
            "panels": [
                {
                    **DELIVERY_DECLARATION["panels"][0],
                    "y_extent": [0.0, 2.0],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, DELIVERY_ROWS, "M4bad")
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("M4 per-flow delivery floor 0.995", stderr)
        self.assertIn("sub-pixel", stderr)

    def test_the_real_delivery_panel_renders_a_band_view(self):
        code, stderr, out = self.render_mandate(
            DELIVERY_DECLARATION, DELIVERY_ROWS, "M4"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-delivery.svg").read_text(encoding="utf-8")
        self.assertIn("band view", document)
        self.assertIn("not 0-based", document)
        # A delivery floor the run meets exactly still has to resolve its band:
        # a 0.5 % loss is a large visible step, not half a pixel.
        self.assertIn("M4 per-flow delivery floor 0.995", document)
        self.assertNotIn(">2.00<", document)

    def test_a_crossed_bound_without_the_run_is_refused(self):
        # The vacuity half: the M2 latency panel with the bound's own x-window
        # removed and the run's measurements not supplied is a panel a crossing
        # cannot be attributed on. It is refused, not drawn.
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [
                        {"y": 100.0, "label": "M2 non-degrading p99 bound (ms)"}
                    ],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, M2_LATENCY_ROWS, "M2bare")
        self.assertNotEqual(code, 0)
        self.assertIn("M2 non-degrading p99 bound (ms)", stderr)
        self.assertIn("run's own measurements were not supplied", stderr)
        self.assertIn("tolerated guard", stderr)

    def test_the_run_s_own_guards_attribute_the_crossed_latency_bound(self):
        out = self.root / "out-M2"
        declaration_path = self.write_mandate(
            M2_LATENCY_DECLARATION, M2_LATENCY_ROWS, name="M2run"
        )
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-latency.svg").read_text(encoding="utf-8")
        # Every drawn bound now says who is beyond it and by which guard, so the
        # crossing lone bar can no longer be read as a bound breach.
        self.assertIn("1 of 3 bars beyond it", document)
        self.assertIn("hostile_p99_guard=200", document)
        self.assertIn("lone_p99_guard=400", document)
        # Named *and* drawn: three lines, each labelled with the arm it governs,
        # so the crossing bar is inside its own arm's guard rather than above a
        # bound no line of it was placed under.
        self.assertEqual(document.count('class="bound"'), 3)
        for label in (
            "M2 non-degrading p99 bound (ms) [governs clean; 1 of 3 bars beyond it]",
            "run hostile_p99_guard=200 [governs hostile]",
            "run lone_p99_guard=400 [governs lone]",
        ):
            self.assertIn(label, document)
        # the data itself is untouched: the same three bars, at the same heights
        rects = re.findall(r'<rect x="[-0-9.]+\w*"', document)
        self.assertTrue(rects)

    def test_the_panel_inventory_is_a_pure_function_of_the_mandate_declaration(self):
        # The tooling keeps no panel list of its own: it writes exactly the ids
        # the mandate declares, adding and dropping nothing. So a `wire` panel
        # can exist only while a *producer* declares one -- no stale name in the
        # tooling can resurrect it -- and because the live M2 declaration names
        # `delivery` and `latency` and nothing else, no M2 wire panel can be
        # produced. The synthetic case below is the proof of the first half: the
        # same renderer, handed a declaration that names a wire panel, writes
        # one, so any `M2-wire` that ever appears is a producer's declaration and
        # not a branch here.
        synthetic_declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [{**M2_LATENCY_DECLARATION["panels"][0], "id": "wire"}],
        }
        synthetic_rows = [M2_LATENCY_ROWS[0]] + [
            ["wire"] + row[1:] for row in M2_LATENCY_ROWS[1:]
        ]
        for name, declaration, rows in (
            ("M2live", M2_LATENCY_DECLARATION, M2_LATENCY_ROWS),
            ("M2declaredwire", synthetic_declaration, synthetic_rows),
        ):
            with self.subTest(case=name):
                code, stderr, out = self.render_mandate(
                    declaration,
                    rows,
                    name,
                    "--run-values",
                    json.dumps(M2_LATENCY_RUN_VALUES),
                )
                self.assertEqual(code, 0, stderr)
                self.assertEqual(
                    sorted(path.name for path in out.glob("*.svg")),
                    sorted(
                        f"{declaration['mandate']}-{panel['id']}.svg"
                        for panel in declaration["panels"]
                    ),
                )

    def test_no_panel_the_live_m2_declaration_draws_carries_a_wire_quantity(self):
        # M2 is offered throughput in, non-degrading latency asserted, goodput
        # inferred: the own-wire multiple is not a mandate quantity any more, so
        # nothing the tooling draws for M2 may name one. Reintroducing a wire
        # bound into either live panel makes the render draw its label and this
        # check goes red -- which is what its vacuity was demonstrated with.
        rendered = []
        for name, declaration, rows, run_values in (
            (
                "M2nowire-delivery",
                M2_DELIVERY_DECLARATION,
                M2_DELIVERY_ROWS,
                M2_RUN_VALUES,
            ),
            (
                "M2nowire-latency",
                M2_LATENCY_DECLARATION,
                M2_LATENCY_ROWS,
                M2_LATENCY_RUN_VALUES,
            ),
        ):
            code, stderr, out = self.render_mandate(
                declaration,
                rows,
                name,
                "--run-values",
                json.dumps(run_values),
            )
            self.assertEqual(code, 0, stderr)
            rendered += [path.read_text(encoding="utf-8") for path in out.glob("*.svg")]
        self.assertTrue(rendered)
        for document in rendered:
            for token in ("wire", "own-wire", "multiple", "6x", "14x"):
                self.assertNotIn(token, document.lower(), token)

    def test_a_declared_governance_attributes_the_bound_without_a_run(self):
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [
                        {"y": 6, "label": "M2 non-degrading p99 bound (ms)", "series": "p99_ms", "x": [1]}
                    ],
                }
            ],
        }
        code, stderr, out = self.render_mandate(declaration, M2_LATENCY_ROWS, "M2decl")
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-latency.svg").read_text(encoding="utf-8")
        self.assertIn("governs series p99_ms", document)
        self.assertIn("governs x=1", document)
        bounds = re.findall(
            r'class="bound" x1="([0-9.]+)" y1="[0-9.]+" x2="([0-9.]+)"', document
        )
        self.assertEqual(len(bounds), 1, bounds)
        left, right = (float(value) for value in bounds[0])
        # governed by x=1 alone, so the line stops over the first bar group
        # instead of running to the plot's right edge as a panel-wide one does
        self.assertGreater(right - left, 0.0)
        self.assertLess(right, 960 - 24)

    def test_a_governed_x_the_panel_does_not_draw_is_an_error(self):
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": "b", "x": [9]}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, M2_LATENCY_ROWS, "M2x")
        self.assertNotEqual(code, 0)
        self.assertIn("draws no category", stderr)

    def test_a_governed_series_the_panel_does_not_declare_is_an_error(self):
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": "b", "series": "clean"}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, M2_LATENCY_ROWS, "M2s")
        self.assertNotEqual(code, 0)
        self.assertIn("governs series 'clean'", stderr)

    def test_a_malformed_governed_x_is_an_error(self):
        for value, fragment in (
            ("all", "bound.x must be a number"),
            ([], "bound.x must be a number"),
            ({"min": 1}, "bound.x.max must be a number"),
            ({"min": 2, "max": 1}, "min <= max"),
        ):
            with self.subTest(x=value):
                declaration = {
                    **M2_LATENCY_DECLARATION,
                    "panels": [
                        {
                            **M2_LATENCY_DECLARATION["panels"][0],
                            "bounds": [{"y": 6, "label": "b", "x": value}],
                        }
                    ],
                }
                code, stderr, _ = self.render_mandate(declaration, M2_LATENCY_ROWS, "M2bad")
                self.assertNotEqual(code, 0)
                self.assertIn(fragment, stderr)

    def test_a_malformed_pinned_extent_is_an_error(self):
        for value, fragment in (
            ([1.0], "two-element [low, high] list"),
            ([2.0, 1.0], "low < high"),
            ([1.0, "high"], "must be a number"),
        ):
            with self.subTest(extent=value):
                declaration = {
                    **DELIVERY_DECLARATION,
                    "panels": [
                        {**DELIVERY_DECLARATION["panels"][0], "y_extent": value}
                    ],
                }
                code, stderr, _ = self.render_mandate(declaration, DELIVERY_ROWS, "M4e")
                self.assertNotEqual(code, 0)
                self.assertIn(fragment, stderr)

    def test_a_pinned_extent_on_a_cdf_panel_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][1], y_extent=[0.0, 100.0])
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[HEALTHY_DECLARATION["panels"][0], panel],
        )
        self.reject(declaration, fragment="cannot pin a cdf panel")

    def test_a_breached_delivery_floor_still_renders_and_shows_the_breach(self):
        # The other half of that rule: a floor the run asserts no looser guard
        # against is a breach when a bar crosses it, so the panel must keep its
        # evidence on the run that fails rather than refuse to be drawn. The
        # crossing also makes the panel a band view, so the breach is at scale.
        rows = [
            row if row[:3] != ["delivery", "hostile", 2.0] else ["delivery", "hostile", 2.0, 0.994]
            for row in DELIVERY_ROWS
        ]
        declaration_path = self.write_mandate(DELIVERY_DECLARATION, rows, name="M4breach")
        out = self.root / "out-M4breach"
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            "--run-values",
            json.dumps({"delivery_floor": 0.995, "hostile_p99_guard": 900.0}),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-delivery.svg").read_text(encoding="utf-8")
        # The breach is named, on the side it happened: the one bar below a
        # *floor* used to be announced as `1 of 8 bars beyond it`, which reads
        # as a bar over a ceiling. The clause states the side it measured, and
        # no unrelated guard is pulled in by name.
        self.assertIn("1 of 8 bars under it", document)
        self.assertNotIn("beyond it", document)
        self.assertNotIn("hostile_p99_guard", document)
        self.assertIn("band view", document)

    def test_run_values_that_are_not_an_object_are_an_error(self):
        declaration_path = self.write_mandate(M2_LATENCY_DECLARATION, M2_LATENCY_ROWS, name="M2v")
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(self.out),
            "--run-values",
            "[1, 2]",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("run values must be a JSON object", stderr)

    def test_a_label_that_cannot_be_wrapped_into_the_plot_is_refused(self):
        # The other vacuity half: a label too long to lay out inside the plot
        # is an error naming the panel and the overflow, not a panel drawn with
        # its annotation running off the edge. This is the reachable failure --
        # `LABEL_MAX_LINES` caps the wrap, so the remainder is one line that no
        # longer fits.
        label = "M2 non-degrading p99 bound (ms) [" + "; ".join(
            f"arm_{index}_guard=1000000" for index in range(40)
        ) + "]"
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [{"y": 6, "label": label}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(
            declaration,
            M2_LATENCY_ROWS,
            "M2long",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("panel 'latency'", stderr)
        self.assertIn("does not fit the plot area", stderr)
        self.assertIn("past its left edge", stderr)

    def test_a_line_panels_label_is_held_to_the_same_fit(self):
        # The check is on the drawn SVG, so it covers every chart kind, not
        # just the bar panels the attribution clause made long.
        label = "M1 ceiling 250 ms [" + "; ".join(
            f"arm_{index}_guard=1000000" for index in range(40)
        ) + "]"
        declaration = {
            **HEALTHY_DECLARATION,
            "panels": [
                {
                    **HEALTHY_DECLARATION["panels"][0],
                    "bounds": [{"y": 250.0, "label": label}],
                },
                HEALTHY_DECLARATION["panels"][1],
            ],
        }
        code, stderr, _ = self.render_mandate(declaration, HEALTHY_ROWS, "M1long")
        self.assertNotEqual(code, 0)
        self.assertIn("panel 'latency'", stderr)
        self.assertIn("does not fit the plot area", stderr)

    def test_healthy_two_panel_input_renders_and_reports_the_panel_count(self):
        declaration_path = self.write_mandate()
        code, stdout, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("panels: 2", stdout)
        self.assertIn("mandate: M1", stdout)
        svg_paths = [self.out / "M1-latency.svg", self.out / "M1-cdf.svg"]
        for svg_path in svg_paths:
            self.assertTrue(svg_path.is_file(), svg_path)
            self.assertIn(f"svg: {svg_path}", stdout)
        self.assertNotIn("png:", stdout)

    def test_every_chart_kind_renders(self):
        for chart, identifier in (("line", "latency"), ("cdf", "cdf"), ("bar", "goodput")):
            with self.subTest(chart=chart):
                declaration = {
                    "mandate": "MX",
                    "title": f"{chart} panel",
                    "x_label": "x",
                    "y_label": "y",
                    "panels": [
                        {
                            "id": identifier,
                            "chart": chart,
                            "series": [{"name": "s"}],
                            "bounds": [],
                        }
                    ],
                }
                rows = [
                    ["panel", "series", "x", "y"],
                    [identifier, "s", 1.0, 10.0],
                    [identifier, "s", 2.0, 40.0],
                    [identifier, "s", 3.0, 65.0],
                ]
                out = self.root / f"out-{chart}"
                declaration_path = self.write_mandate(declaration, rows, name="MX")
                code, stdout, stderr = self.run_main(
                    str(declaration_path), "--no-rasterize", "--out", str(out)
                )
                self.assertEqual(code, 0, stderr)
                self.assertIn("panels: 1", stdout)
                self.assertTrue((out / f"MX-{identifier}.svg").is_file())

    def test_bounds_are_emitted_into_the_svg(self):
        declaration_path = self.write_mandate()
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        # The artifact is the evidence: the bound line and its label are in it.
        self.assertIn('class="bound"', document)
        self.assertEqual(document.count('class="bound"'), 1)
        self.assertIn("M1 ceiling 250 ms", document)

    def test_bar_bounds_are_emitted_into_the_svg(self):
        declaration_path = self.write_mandate(BAR_DECLARATION, BAR_ROWS, name="M3")
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M3-goodput.svg").read_text(encoding="utf-8")
        self.assertIn('class="bound"', document)
        self.assertIn("M3 floor 0.35x link rate", document)
        # The bars are the series geometry, and the plot background is not.
        rects = [
            rect
            for rect in re.findall(r'<rect\b[^>]*>', document)
            if "plot-bg" not in rect
        ]
        self.assertTrue(rects, "the panel drew no bar geometry")

    def test_series_are_plotted_in_ascending_x_order(self):
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "impaired", 3.0, 30.0],
            ["latency", "impaired", 1.0, 10.0],
            ["cdf", "impaired", 30.0, 50.0],
            ["cdf", "impaired", 10.0, 100.0],
        ]
        declaration_path = self.write_mandate(rows=rows)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        document = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        polyline = re.search(r'<polyline\b[^>]*\bpoints="([^"]*)"', document).group(1)
        xs = [float(pair.split(",")[0]) for pair in polyline.split()]
        self.assertEqual(xs, sorted(xs))

    def test_missing_declaration_is_an_error(self):
        code, _, stderr = self.run_main(
            str(self.root / "absent.json"), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate declaration not found", stderr)

    def test_malformed_declaration_json_is_an_error(self):
        path = self.root / "M1.json"
        path.write_text("{not json", encoding="utf-8")
        code, _, stderr = self.run_main(str(path), "--no-rasterize", "--out", str(self.out))
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", stderr)

    def test_declaration_that_is_not_an_object_is_an_error(self):
        path = self.root / "M1.json"
        path.write_text("[1, 2]", encoding="utf-8")
        code, _, stderr = self.run_main(str(path), "--no-rasterize", "--out", str(self.out))
        self.assertNotEqual(code, 0)
        self.assertIn("must be a JSON object", stderr)

    def test_missing_mandate_name_is_an_error(self):
        declaration = dict(HEALTHY_DECLARATION)
        declaration.pop("mandate")
        self.reject(declaration, fragment="mandate must be a non-empty string")

    def test_empty_panel_list_is_an_error(self):
        declaration = dict(HEALTHY_DECLARATION, panels=[])
        self.reject(declaration, fragment="panels must be a non-empty list")

    def test_duplicate_panel_id_is_an_error(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[HEALTHY_DECLARATION["panels"][0], dict(HEALTHY_DECLARATION["panels"][1], id="latency")],
        )
        self.reject(declaration, fragment="is declared twice")

    def test_unusable_panel_id_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], id="lat/ency")
        self.reject(dict(HEALTHY_DECLARATION, panels=[panel]), fragment="must match")

    def test_malformed_chart_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], chart="scatter")
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment="is not a chart; expected one of line, cdf, bar")

    def test_series_without_a_name_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], series=[{"role": "impaired"}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".series[0].name must be a non-empty string")

    def test_empty_series_list_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], series=[])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".series must be a non-empty list")

    def test_bound_without_a_label_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], bounds=[{"y": 250.0}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".bounds[0].label must be a non-empty string")

    def test_bound_with_a_non_numeric_y_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], bounds=[{"y": "ceiling", "label": "c"}])
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment=".bounds[0].y must be a number")

    def test_missing_data_csv_is_an_error(self):
        declaration_path = self.write_mandate(rows=None)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate data CSV not found", stderr)

    def test_empty_csv_file_is_an_error(self):
        self.reject(rows="", fragment="mandate data CSV is empty (no header row)")

    def test_header_only_csv_is_an_error(self):
        self.reject(rows=[["panel", "series", "x", "y"]], fragment="has no data rows")

    def test_csv_header_mismatch_is_an_error(self):
        rows = [["panel", "series", "x", "y_ms"]] + HEALTHY_ROWS[1:]
        self.reject(rows=rows, fragment="header must be panel,series,x,y")

    def test_csv_row_with_the_wrong_field_count_is_an_error(self):
        rows = HEALTHY_ROWS[:1] + HEALTHY_ROWS[1:] + [["latency", "impaired", 9.0]]
        self.reject(rows=rows, fragment="has 3 field(s), expected 4")

    def test_declared_panel_with_no_rows_is_rejected(self):
        rows = [["panel", "series", "x", "y"]] + [row for row in HEALTHY_ROWS[1:] if row[0] != "cdf"]
        message = self.reject(rows=rows, fragment="has no rows for it")
        self.assertIn("panel 'cdf'", message)
        self.assertIn("an empty chart is not a graph", message)

    def test_declared_series_with_no_rows_is_rejected(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[dict(HEALTHY_DECLARATION["panels"][0], series=[{"name": "impaired"}, {"name": "clean"}]), HEALTHY_DECLARATION["panels"][1]],
        )
        message = self.reject(declaration, fragment="has no row for it")
        self.assertIn("declares series 'clean'", message)

    def test_csv_row_naming_an_undeclared_series_is_rejected(self):
        rows = HEALTHY_ROWS + [["latency", "clean", 3.0, 44.0]]
        message = self.reject(rows=rows, fragment="which the declaration does not declare")
        self.assertIn("series 'clean' in panel 'latency'", message)

    def test_csv_row_naming_an_undeclared_panel_is_rejected(self):
        rows = HEALTHY_ROWS + [["M9", "impaired", 3.0, 44.0]]
        message = self.reject(rows=rows, fragment="which the declaration does not declare")
        self.assertIn("panel 'M9'", message)

    def test_non_numeric_x_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", "abc", 1.0]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-numeric x/y")

    def test_non_numeric_y_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", 1.0, ""]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-numeric x/y")

    def test_non_finite_y_is_rejected(self):
        rows = HEALTHY_ROWS[:1] + [["latency", "impaired", 1.0, "nan"]] + HEALTHY_ROWS[2:]
        self.reject(rows=rows, fragment="has a non-finite x/y")

    def test_cdf_y_outside_the_percentile_axis_is_rejected(self):
        rows = [["panel", "series", "x", "y"]]
        rows += [row for row in HEALTHY_ROWS[1:] if row[0] == "latency"]
        rows += [
            ["cdf", "impaired", 12.5, 250.0],
            ["cdf", "impaired", 31.5, 66.7],
            ["cdf", "impaired", 88.25, 100.0],
        ]
        self.reject(rows=rows, fragment="a cdf y is a percentile")

    def test_panel_labels_override_the_mandate_labels(self):
        declaration = dict(
            HEALTHY_DECLARATION,
            panels=[
                HEALTHY_DECLARATION["panels"][0],
                dict(
                    HEALTHY_DECLARATION["panels"][1],
                    x_label="RTT (ms)",
                    y_label="samples <= x (%)",
                ),
            ],
        )
        declaration_path = self.write_mandate(declaration)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        cdf = (self.out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertIn("samples &lt;= x (%)", cdf)
        self.assertIn("RTT (ms)", cdf)
        latency = (self.out / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertIn("elapsed time (s)", latency)
        self.assertNotIn("samples", latency)

    def test_panel_label_of_the_wrong_type_is_an_error(self):
        panel = dict(HEALTHY_DECLARATION["panels"][0], y_label=7)
        declaration = dict(HEALTHY_DECLARATION, panels=[panel, HEALTHY_DECLARATION["panels"][1]])
        self.reject(declaration, fragment="panels[0].y_label must be a string")

    def test_out_path_that_is_a_file_is_an_error(self):
        declaration_path = self.write_mandate()
        blocker = self.root / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(blocker)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--out is not a directory", stderr)

    def test_no_rasterize_writes_svgs_without_consulting_a_browser(self):
        # The environment names a browser that fails loudly, so a run that
        # consulted one despite `--no-rasterize` cannot pass by luck.
        declaration_path = self.write_mandate()
        browser = self.write_browser(FORBIDDEN_BROWSER)
        code, stdout, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(self.out),
            env={"NETEM_RENDER_BROWSER": browser},
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("panels: 2", stdout)
        self.assertEqual(len(list(self.out.glob("*.svg"))), 2)

    def test_rasterize_without_a_browser_is_an_error_after_writing_svgs(self):
        declaration_path = self.write_mandate()
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--out",
            str(self.out),
            "--browser",
            "netem-no-such-browser-xyz",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("no headless browser found", stderr)
        # The SVG evidence is written and verified even though the PNG step failed.
        self.assertTrue((self.out / "M1-latency.svg").is_file())
        self.assertTrue((self.out / "M1-cdf.svg").is_file())

    def test_rasterize_records_verified_pngs(self):
        declaration_path = self.write_mandate()
        browser = self.write_browser(FAKE_BROWSER.format(png=ONE_PIXEL_PNG))
        code, stdout, stderr = self.run_main(
            str(declaration_path), "--browser", browser, "--out", str(self.out)
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(list(self.out.glob("*.png"))), 2)
        for png_path in sorted(self.out.glob("*.png")):
            data = png_path.read_bytes()
            # A real PNG, which is what the binary verified before recording the
            # path; the bytes say so here too.
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreater(len(data), 8)
            self.assertIn(f"png: {png_path}", stdout)

    def test_rasterize_rejects_a_browser_that_writes_no_png(self):
        declaration_path = self.write_mandate()
        browser = self.write_browser(NULL_BROWSER)
        code, _, stderr = self.run_main(
            str(declaration_path), "--browser", browser, "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("did not produce a valid PNG", stderr)

    def test_cli_exit_status_is_nonzero_for_a_missing_csv(self):
        declaration_path = self.write_mandate(rows=None)
        code, _, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(self.out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate data CSV not found", stderr)

    def test_find_browser_env_override_is_honoured(self):
        # The variable is the plotter's own override: a render with no
        # `--browser` that succeeds *and* records PNGs proves the name it
        # carries is the one that was consulted.
        declaration_path = self.write_mandate(rows=HEALTHY_ROWS)
        browser = self.write_browser(FAKE_BROWSER.format(png=ONE_PIXEL_PNG))
        code, stdout, stderr = self.run_main(
            str(declaration_path),
            "--out",
            str(self.out),
            env={"NETEM_RENDER_BROWSER": browser},
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(list(self.out.glob("*.png"))), 2)
        self.assertIn(f"png: {self.out / 'M1-latency.png'}", stdout)

    def test_a_band_label_whose_magnitude_is_not_its_declared_value_is_refused(self):
        # The mirror is derived from the label's own number, so a `±` label that
        # does not state the value it is declared at says the band's centre is
        # somewhere the declaration never named: refused rather than mirrored on
        # a guess.
        declaration = json.loads(json.dumps(SHARES_IMBALANCE_DECLARATION))
        declaration["panels"][1]["bounds"][0]["y"] = 0.02
        code, stderr, _ = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4odd"
        )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("does not say where the band is centred", stderr)

    def test_the_latency_panel_renders_on_a_run_whose_worst_arm_touches_the_bound(self):
        # The runs the tool refused (the bars just under and just over the
        # bound): the band the axis test measures is now the tolerance the run's
        # guards open between the bound and the arm's own limit, not the sliver
        # between the bound and the worst arm -- which is a sliver *by
        # construction* on any run whose worst arm lands near the bound. The
        # range is the bound plus a margin, not the observed maximum, so the
        # panel draws the crossing it is named for on both runs.
        for name, worst in (("below", 98.5), ("above", 100.8)):
            with self.subTest(worst_arm=worst):
                rows = [
                    ["panel", "series", "x", "y"],
                    ["latency", "p99_ms", 1.0, 35.3],
                    ["latency", "p99_ms", 2.0, 81.8],
                    ["latency", "p99_ms", 3.0, worst],
                ]
                code, stderr, out = self.render_mandate(
                    M2_LATENCY_DECLARATION,
                    rows,
                    f"M2{name}",
                    "--run-values",
                    json.dumps(M2_LATENCY_RUN_VALUES),
                )
                self.assertEqual(code, 0, stderr)
                document = (out / "M2-latency.svg").read_text(encoding="utf-8")
                ticks = [
                    float(value)
                    for value in re.findall(
                        r'text-anchor="end">([-0-9.]+)<', document
                    )
                ]
                self.assertGreater(ticks[-1], 400.0)
                self.assertIn("hostile_p99_guard=200", document)
                self.assertIn("lone_p99_guard=400", document)

    def test_a_bound_with_no_tolerance_and_a_sliver_margin_is_still_refused(self):
        # The vacuity half of the tolerance rule: the same run, with the guards
        # the label would name not supplied. The observed margin is then the only
        # band there is -- a couple of milliseconds under a 100 ms bound over a
        # ~100 ms axis -- and it is still refused. The rule decides *which*
        # region the axis owes the reader; it does not relax the threshold.
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "p99_ms", 1.0, 35.3],
            ["latency", "p99_ms", 2.0, 81.8],
            ["latency", "p99_ms", 3.0, 98.5],
        ]
        code, stderr, _ = self.render_mandate(M2_LATENCY_DECLARATION, rows, "M2noguard")
        self.assertNotEqual(code, 0)
        self.assertIn("sub-pixel", stderr)
        self.assertIn("M2 non-degrading p99 bound (ms)", stderr)

    def test_a_guard_key_named_without_its_series_is_refused(self):
        # red, at the render: a panel whose run guard is named on the ceiling as
        # a *key* (`hostile_p99_guard=900`) while the ceiling sits at that key's
        # own value, so no guard line is drawn beside it. The panel then names
        # the key and never the series it bounds -- exactly the hole that let a
        # hostile bar cross the ceiling with nothing saying which bound governs
        # it.
        declaration = {
            **SERIES_GUARD_DECLARATION,
            "panels": [
                {
                    **SERIES_GUARD_DECLARATION["panels"][0],
                    "bounds": [{"y": 900.0, "label": "M1 ceiling 900 ms"}],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(
            declaration,
            SERIES_GUARD_ROWS,
            "M4cg",
            "--run-values",
            json.dumps(SERIES_GUARD_VALUES),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("'hostile_p99'", stderr)
        self.assertIn("M1 ceiling 900 ms", stderr)

    def test_the_m3_panels_name_their_own_quantity_and_their_repetitions(self):
        declaration = {
            "mandate": "M3",
            "title": "M3 bulk goodput",
            "x_label": "seed",
            "y_label": "MiB/s",
            "panels": [
                {
                    "id": "goodput",
                    "chart": "bar",
                    "series": [{"name": "delivered"}, {"name": "shaper_forwarded"}],
                    "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
                },
                {
                    "id": "fraction",
                    "chart": "bar",
                    "series": [{"name": "fraction"}],
                    "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
                },
            ],
        }
        rows = [
            ["panel", "series", "x", "y"],
            ["goodput", "delivered", 1.0, 0.958],
            ["goodput", "shaper_forwarded", 1.0, 0.968],
            ["fraction", "fraction", 1.0, 0.958],
            ["goodput", "delivered", 2.0, 0.953],
            ["goodput", "shaper_forwarded", 2.0, 0.961],
            ["fraction", "fraction", 2.0, 0.953],
            ["goodput", "delivered", 3.0, 0.948],
            ["goodput", "shaper_forwarded", 3.0, 0.955],
            ["fraction", "fraction", 3.0, 0.948],
        ]
        code, stderr, out = self.render_mandate(
            declaration,
            rows,
            "M3labels",
            "--run-values",
            json.dumps({"reps": 3, "measured_s": 18.0, "floor": 0.35}),
        )
        self.assertEqual(code, 0, stderr)
        fraction = (out / "M3-fraction.svg").read_text(encoding="utf-8")
        self.assertIn("fraction of link rate", fraction)
        self.assertNotIn("MiB/s", fraction)
        self.assertIn("rep (1..3)", fraction)
        goodput = (out / "M3-goodput.svg").read_text(encoding="utf-8")
        self.assertIn("MiB/s", goodput)
        self.assertIn("rep (1..3)", goodput)
        self.assertNotIn("seed", goodput)

    def test_a_malformed_censoring_reading_is_an_error(self):
        # The reading is what the panel states, so a reading that is not an
        # object of measured tokens is an error rather than a panel drawn
        # without one -- the whole reason the band exists.
        declaration_path = self.write_mandate(
            LONE_TAIL_DECLARATION, _latency_rows(TRUNCATED_CLIMB), name="M1badcens"
        )
        code, _, stderr = self.run_main(
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(self.out),
            "--run-censoring",
            '{"lone_tail": "Censored"}',
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mandate_plot: error:", stderr)
        self.assertIn("must be a non-empty object", stderr)
        self.assertIn("'lone_tail'", stderr)

    def render_json(self, declaration, rows, name, *arguments):
        """Render one declaration and return ``(summary documents, stderr, out)``.

        A black-box reader's view of what the panel states: the `--json`
        summary the plotter wrote, keyed by panel id, which is the same
        document the run's own `mandate-check.json` carries.
        """
        out = self.root / f"out-{name}"
        declaration_path = self.write_mandate(declaration, rows, name=name)
        code, stdout, stderr = self.run_main(
            str(declaration_path), "--no-rasterize", "--out", str(out), "--json", *arguments
        )
        self.assertEqual(code, 0, stderr)
        document = json.loads(stdout)
        return (
            {entry["panel"]: entry for entry in document["summaries"]},
            stderr,
            out,
        )

    @staticmethod
    def bound_titles(document):
        """The declared sentence of every drawn bound label, in draw order."""
        return [
            html.unescape(title)
            for title in re.findall(
                r'<text class="bound-label"[^>]*>\s*<title>(.*?)</title>',
                document,
                re.S,
            )
        ]

    @staticmethod
    def panel_notes(document):
        """The panel's own notes, joined, as a reader sees them."""
        return html.unescape(
            " ".join(
                re.findall(r'<text class="panel-note"[^>]*>(.*?)</text>', document, re.S)
            )
        )

    # -- what the summary states, read back off the run --------------------

    def test_the_summary_states_the_x_extent_the_panel_drew(self):
        # The x extent is the other half of what a panel drew -- for a CDF it
        # is the latency range its curves are read against -- and a summary
        # that omitted it left the reader to guess the one extent only the tick
        # labels carried.
        panels, _, out = self.render_json(HEALTHY_DECLARATION, HEALTHY_ROWS, "M1xa")
        self.assertEqual(panels["latency"]["x_axis"], [0.0, 2.0])
        self.assertEqual(panels["cdf"]["x_axis"], [12.5, 88.25])
        block = (out / "M1-latency.summary.txt").read_text(encoding="utf-8")
        self.assertIn("x_axis=0..2", block)
        # and the drawn tick labels are the extent the summary states, to the
        # precision the axis draws them at
        cdf = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        ticks = re.findall(r'text-anchor="middle"[^>]*>([-0-9.]+)<', cdf)
        self.assertEqual(ticks[0], "12.5")
        self.assertEqual(ticks[-1], "88.2")

    def test_a_panel_with_no_bound_says_none_by_design(self):
        # No panel is exempt from stating what it drew: one with no bound says
        # "none by design", so "absent summary" is never a legitimate state.
        panels, _, out = self.render_json(HEALTHY_DECLARATION, HEALTHY_ROWS, "M1nb")
        block = panels["cdf"]["block"]
        self.assertIn("bound: none by design", block)
        sidecar = (out / "M1-cdf.summary.txt").read_text(encoding="utf-8")
        self.assertEqual(sidecar.strip(), block)

    def test_one_outlier_no_longer_sets_the_latency_axis(self):
        # The 1400 ms sample is clipped at the 250 ms the panel is read
        # against, and the axis is the anchor's -- so the bodies it compressed
        # are resolvable and the outlier is still drawn, at the frame's top.
        panels, _, out = self.render_json(CLIP_DECLARATION, CLIP_ROWS, "M1clip")
        summary = panels["latency"]
        self.assertEqual(summary["y_clip"], {"value": 250.0, "clipped": 1, "max": 1400.0})
        self.assertLess(summary["axis"][1], 1400.0)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertIn('class="y-clip"', svg)
        # The summary states the clip in its own words, and the drawn note
        # spells the sentence out on the panel's face.
        self.assertIn("y_clip: clipped at 250", summary["block"])
        self.assertIn("1 drawn value(s) up to 1400", summary["block"])
        note = html.unescape(
            " ".join(
                re.findall(r'<text class="panel-note"[^>]*>(.*?)</text>', svg, re.S)
            )
        )
        self.assertIn(
            "y axis clipped at 250: 1 of 601 value(s) up to 1400 drawn at the top edge",
            note,
        )

    def test_a_fault_render_says_which_mandate_and_what_it_did_to_the_scale(self):
        # A fault render must be readable as one: which mandate it names and
        # what it did to the scale, so it cannot be taken for a real panel.
        panels, _, out = self.render_json(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4fault", "--fault", "M4_drop"
        )
        block = panels["imbalance"]["block"]
        self.assertEqual(panels["imbalance"]["fault"], "M4_drop")
        self.assertIn("fault: M4_drop", block)
        self.assertIn("fault scale:", block)
        self.assertIn("fair-share bound \u00b11.0%", block)
        self.assertIn("2.1 px of this axis", block)
        sidecar = (out / "M4-imbalance.summary.txt").read_text(encoding="utf-8")
        self.assertIn("fault: M4_drop", sidecar)

    def test_the_starved_flows_sliver_bound_is_stated_not_refused(self):
        # The fault's own render: both arms are drawn -- the panel is evidence,
        # not an error -- and the panel says where the bound sits and how far
        # the bars are from it, which is what answers the axis refusal.
        code, stderr, out = self.render_mandate(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4drop"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        self.assertEqual(document.count('class="bound"'), 2)
        notes = html.unescape(
            " ".join(
                re.findall(r'<text class="panel-note"[^>]*>(.*?)</text>', document, re.S)
            )
        )
        self.assertIn(
            'bound "fair-share bound \u00b11.0%" at 0.01 on axis -1..0.0605: '
            "band 0.009884 = 2.1 px; nearest bar 0.000116, 0.009884 away; "
            "furthest -1, 217.1 px from the bound",
            notes,
        )
        # The mirrored arm is closer than a line of text, so it is drawn
        # unlabelled and its own position is stated instead.
        self.assertEqual(document.count("<title>fair-share bound"), 1)
        self.assertIn("is drawn unlabelled", notes)

    def test_the_late_arms_sliver_ceiling_is_stated_not_refused(self):
        # The other refusal-answering statement: a fault render whose verdict is
        # PASS and whose panel is the evidence the fault showed.
        code, stderr, out = self.render_mandate(
            LATE_LATENCY_DECLARATION,
            LATE_LATENCY_ROWS,
            "M4late",
            "--run-values",
            json.dumps(LATE_LATENCY_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-latency.svg").read_text(encoding="utf-8")
        notes = html.unescape(
            " ".join(
                re.findall(r'<text class="panel-note"[^>]*>(.*?)</text>', document, re.S)
            )
        )
        self.assertIn(
            'bound "M1 ceiling 250 ms" at 250 on axis 0..4565.34: band 32.427 '
            "= 1.6 px; nearest bar 282.427, 32.427 away; furthest 4347.94, "
            "204.7 px from the bound",
            notes,
        )
        self.assertEqual(document.count('class="bound"'), 2)
    def test_a_bar_panel_states_its_padded_category_domain(self):
        # A bar panel's x axis is the padded category domain, not the categories
        # themselves: the half-band at each end is what puts the outer bars
        # inside the plot, and it is what the axis' own ticks draw.
        panels, _, _ = self.render_json(DELIVERY_DECLARATION, DELIVERY_ROWS, "M4xbar")
        self.assertEqual(panels["delivery"]["x_axis"], [0.5, 4.5])

    def test_a_bound_the_run_restates_is_drawn_per_arm_and_named(self):
        # The measured defect: `M2-delivery` drew the clean arm's `1.000` line
        # across all three arms while the run guards the other two at `0.995`,
        # so a hostile bar at `0.996` crossed the drawn line while sitting
        # inside its own arm's floor. The run's per-arm guards are what it is
        # split against, drawn per arm.
        code, stderr, out = self.render_mandate(
            M2_DELIVERY_DECLARATION,
            M2_DELIVERY_ROWS,
            "M2arms",
            "--run-values",
            json.dumps(M2_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-delivery.svg").read_text(encoding="utf-8")
        self.assertEqual(document.count('class="bound"'), 2)
        self.assertEqual(
            self.bound_titles(document),
            [
                "M2 delivery floor 1.000 [governs clean]",
                "run hostile_delivery_guard=0.995 [governs hostile lone]",
            ],
        )
        # The two lines really are over different arms: the first ends where
        # arm 1's band ends and the second begins there.
        spans = re.findall(
            r'class="bound" x1="([-0-9.]+)"[^>]*x2="([-0-9.]+)"', document
        )
        self.assertEqual(len(spans), 2, spans)
        self.assertEqual(spans[0][1], spans[1][0])
        self.assertLess(float(spans[0][1]), float(spans[1][1]))

    def test_a_share_panel_names_the_panel_that_carries_its_departure(self):
        # A bound the panel's own bars straddle is a reference, not a line any
        # of them can fail, so the frame has no failure in it to draw: the
        # panel says what it is, names the panel that carries the departure,
        # and states the run's own worst departure per arm.
        code, stderr, out = self.render_mandate(
            SHARES_IMBALANCE_DECLARATION, SHARES_IMBALANCE_ROWS, "M4depart"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        self.assertEqual(
            self.panel_notes(document),
            "composition view - the departure is drawn on panel 'imbalance' "
            "(bound 1.0%): worst clean 0.04%, hostile 0.03%",
        )

    def test_a_crossing_arm_guarded_by_the_run_is_named_on_the_ceiling(self):
        # The lone tail's 1600 ms sample crosses the 250 ms ceiling, and the run
        # states that arm's own guard, so the drawn label names it.
        code, stderr, out = self.render_mandate(
            CROSSING_DECLARATION,
            CROSSING_ROWS,
            "M1cg",
            "--run-values",
            json.dumps(CROSSING_GUARDS),
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        labels = self.bound_titles(svg)
        self.assertTrue(any("lone_tail" in label for label in labels), labels)

    def test_a_series_guard_is_drawn_and_named_beside_the_ceiling(self):
        # The run's guard is drawn as its own line and labelled with the series
        # it governs, so the departure is attributed and the render is kept.
        code, stderr, out = self.render_mandate(
            SERIES_GUARD_DECLARATION,
            SERIES_GUARD_ROWS,
            "M4cg2",
            "--run-values",
            json.dumps(SERIES_GUARD_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M4-latency.svg").read_text(encoding="utf-8")
        labels = self.bound_titles(svg)
        self.assertTrue(
            any("governs series hostile_p99" in label for label in labels), labels
        )

    def test_every_bar_beyond_the_bound_is_not_labelled_as_a_crossing(self):
        # When *every* bar is past the bound the crossing is the verdict's, not
        # one arm's tolerated guard, so the panel draws no `N of M` clause.
        # Uniform failure is read from the bars; a single bar past it is the
        # case the clause exists for. The guards are still named and drawn.
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "p99_ms", 1.0, 120.0],
            ["latency", "p99_ms", 2.0, 158.3],
            ["latency", "p99_ms", 3.0, 135.0],
        ]
        run_values = {
            "clean_p99_ms": 120.0,
            "hostile_p99_ms": 158.3,
            "lone_p99_ms": 135.0,
            "clean_p99_guard": 50.0,
            "hostile_p99_guard": 266.7,
            "lone_p99_guard": 333.3,
        }
        code, stderr, out = self.render_mandate(
            M2_LATENCY_DECLARATION,
            rows,
            "M2all",
            "--run-values",
            json.dumps(run_values),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-latency.svg").read_text(encoding="utf-8")
        self.assertNotIn("beyond it", document)
        self.assertNotIn("under it", document)
        self.assertEqual(document.count('class="bound"'), 4)
        labels = self.bound_titles(document)
        for key in (
            "clean_p99_guard=50",
            "hostile_p99_guard=266.7",
            "lone_p99_guard=333.3",
        ):
            self.assertTrue(
                any(label.startswith(f"run {key}") for label in labels), labels
            )
        self.assertTrue(any("governs no arm of this run" in label for label in labels))

    # -- the render-integration halves the port left without a case ----------

    def test_a_cdf_carries_the_ceiling_and_the_value_it_is_read_at(self):
        # The CDF draws no bound of its own; the mandate's ceiling is a value on
        # the axis its *x* carries, so the panel can draw the failure its
        # mandate is read for and has to state, at that x, what each curve
        # reads. The readings are the run's own drawn `M1.csv` points.
        code, stderr, out = self.render_mandate(
            M1_ARMS_DECLARATION,
            M1_ARMS_ROWS,
            "M1ceil",
            "--run-values",
            json.dumps(M1_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        cdf = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertEqual(cdf.count('class="x-bound"'), 1)
        self.assertEqual(
            self.bound_titles(cdf),
            [
                "M1 ceiling 250 ms [at 250 ms: clean 100%, hostile 99.4%, "
                "lone_tail 99.06%]"
            ],
        )
        # The frame now carries the failure, so the bare pointer to the sibling
        # panel is gone.
        self.assertEqual(self.panel_notes(cdf), "")
        # The reference arm is legible on the drawn axis: the clean curve's own
        # left-most and right-most points are both past the axis' halfway mark.
        plot = re.search(
            r'<rect x="([-0-9.]+)" y="[-0-9.]+" width="([-0-9.]+)" height="[-0-9.]+" class="plot-bg"',
            cdf,
        )
        left, width = (float(value) for value in plot.groups())
        clean = re.search(
            r'<polyline points="([^"]*)" fill="none" stroke="#2563eb"', cdf
        ).group(1)
        xs = [float(pair.split(",")[0]) for pair in clean.split()]
        self.assertGreaterEqual((min(xs) - left) / width, 0.5)
        self.assertGreaterEqual((max(xs) - left) / width, 0.5)

    def test_a_ceiling_beyond_the_drawn_x_range_is_stated_not_drawn(self):
        # A run whose samples all sit under the ceiling has no pixel for the
        # mark. It still owes the reading, and it owes saying that the value is
        # outside the drawn range, so the clamp cannot pass for a measurement.
        declaration = {
            "mandate": "M1",
            "title": "M1 interactive tail latency",
            "x_label": "elapsed time (s)",
            "y_label": "latency (ms)",
            "panels": [
                {
                    "id": "latency",
                    "chart": "line",
                    "series": [{"name": "clean"}, {"name": "hostile"}],
                    "bounds": [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
                },
                {
                    "id": "cdf",
                    "chart": "cdf",
                    "x_label": "latency (ms)",
                    "y_label": "percentile (%)",
                    "series": [{"name": "clean"}, {"name": "hostile"}],
                    "bounds": [],
                },
            ],
        }
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "clean", 1.0, 20.0],
            ["latency", "clean", 2.0, 60.0],
            ["latency", "clean", 3.0, 24.0],
            ["latency", "hostile", 1.0, 30.0],
            ["latency", "hostile", 2.0, 70.0],
            ["latency", "hostile", 3.0, 34.0],
            ["cdf", "clean", 20.0, 0.0],
            ["cdf", "clean", 60.0, 50.0],
            ["cdf", "clean", 100.0, 100.0],
            ["cdf", "hostile", 30.0, 0.0],
            ["cdf", "hostile", 70.0, 50.0],
            ["cdf", "hostile", 100.0, 100.0],
        ]
        code, stderr, out = self.render_mandate(
            declaration, rows, "M1under", "--run-values", json.dumps({"ceiling": 250.0})
        )
        self.assertEqual(code, 0, stderr)
        cdf = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertEqual(cdf.count('class="x-bound"'), 0)
        self.assertEqual(
            self.bound_titles(cdf),
            [
                "M1 ceiling 250 ms [at 250 ms: clean 100%, hostile 100%] "
                "(x beyond this panel's drawn range)"
            ],
        )

    def test_a_two_sided_bound_is_drawn_on_both_of_its_sides(self):
        # The measured defect: a panel read `fair-share bound \u00b11.0%` beside a
        # single `+0.01` line, so the `-1 %` arm a starved flow crosses was not
        # drawn at all. Both arms are in the artifact, each at its own value.
        code, stderr, out = self.render_mandate(
            SHARES_IMBALANCE_DECLARATION, SHARES_IMBALANCE_ROWS, "M4band"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        self.assertEqual(document.count('class="bound"'), 2)
        self.assertEqual(
            self.bound_titles(document),
            ["fair-share bound \u00b11.0%", "fair-share bound -1.0%"],
        )

    def test_a_bound_at_the_top_of_its_axis_is_labelled_below_its_line(self):
        # `M2-delivery`'s floor is the band view's own top, so the label has to
        # drop below the line rather than escape into the legend.
        code, stderr, out = self.render_mandate(
            M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS, "M2top"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-delivery.svg").read_text(encoding="utf-8")
        bound_y = float(
            re.search(r'class="bound" x1="[^"]*" y1="([-0-9.]+)"', document).group(1)
        )
        for label_y in re.findall(
            r'<text class="bound-label"[^>]*\by="([-0-9.]+)"', document
        ):
            self.assertGreater(
                float(label_y),
                bound_y,
                f"a label at y={label_y} is drawn above its own bound at y={bound_y}",
            )

    def test_panels_whose_axis_can_already_show_their_bound_are_unchanged(self):
        # The coarse view whose fine counterpart is M4-imbalance, and a floor
        # with half the axis between it and the data: both keep their zero
        # baseline. What they no longer keep is an axis that tops out on the
        # bound itself, so an over-share bar would be clipped by the frame.
        for name, declaration, rows, bound in (
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS, 0.25),
            ("M3frac", FRACTION_DECLARATION, FRACTION_ROWS, 0.35),
        ):
            with self.subTest(panel=name):
                code, stderr, out = self.render_mandate(declaration, rows, name)
                self.assertEqual(code, 0, stderr)
                panel_id = declaration["panels"][0]["id"]
                document = (out / f"{declaration['mandate']}-{panel_id}.svg").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("band view", document)
                ticks = [
                    float(value)
                    for value in re.findall(r'text-anchor="end">([-0-9.]+)<', document)
                ]
                self.assertEqual(ticks[0], 0.0)
                self.assertGreater(ticks[-1], bound, "no room above the bound")


if __name__ == "__main__":
    unittest.main()
