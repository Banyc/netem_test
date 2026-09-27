#!/usr/bin/env python3

import base64
import csv
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("mandate_plot.py")
SPEC = importlib.util.spec_from_file_location("mandate_plot", MODULE_PATH)
MANDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANDATE)

WORKSPACE = Path(__file__).resolve().parents[1]

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


def _latency_rows(points):
    return [["panel", "series", "x", "y"]] + [
        ["latency", "lone_tail", x, y] for x, y in points
    ]


# A real run's M1 readings, and the shape both of the panels below are about.
# The line panel draws one 250 ms ceiling across three arms whose own guards
# differ by more than the ceiling itself (`lone_tail`'s is 3200 ms), and the CDF
# beside it draws the same three distributions with no horizontal bound at all.
# These are the `MANDATE M1` line's own numbers and the run's own drawn
# `M1.csv` points: `clean` reached the ceiling's side of its distribution at
# 107.7 ms, `hostile` reads 99.4 % at 250 ms and `lone_tail` 99.06 %.
M1_RUN_VALUES = {
    "clean_p50": 24.4,
    "clean_p90": 30.0,
    "clean_p99": 93.1,
    "clean_p999": 103.4,
    "clean_max": 107.7,
    "clean_over250": 0,
    "hostile_p50": 45.8,
    "hostile_p90": 141.0,
    "hostile_p99": 231.8,
    "hostile_p999": 267.7,
    "hostile_max": 277.1,
    "hostile_over250": 12,
    "lone_p50": 0.2,
    "lone_p90": 59.4,
    "lone_p99": 166.4,
    "lone_p999": 1465.8,
    "lone_max": 1567.1,
    "lone_over250": 2,
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

def _points(rows):
    """CSV rows from a fixture, in the shape `mandate_plot.parse_points` takes."""
    return MANDATE.parse_points(
        [(line, row[0], row[1], row[2], row[3]) for line, row in enumerate(rows[1:], start=2)]
    )


def _reading_markup(sentences):
    """A line panel's reading band, wrapped and drawn, as `svg_line_chart` does."""
    rows = []
    for sentence in sentences:
        rows.extend(MANDATE.REPORT.wrap_label(sentence, MANDATE.REPORT.READING_PLOT_WIDTH))
    body = "".join(
        f'<text class="arm-reading" x="76" y="{24 + index * 13}">{row}</text>'
        for index, row in enumerate(rows)
    )
    return f'<svg><g class="arm-readings">{body}</g></svg>'


class MandatePlotTest(unittest.TestCase):
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

    def run_main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MANDATE.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

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
            str(declaration_path),
            "--no-rasterize",
            "--out",
            str(out),
            *arguments,
        )
        return code, stderr, out

    def test_the_delivery_floor_that_was_sub_pixel_is_now_the_axis_feature(self):
        # The audit's measurement: over the old 0..2 axis, M4's 0.5 % floor band
        # was 0.25 % of the height. The band view has to spend a real share of
        # the axis on it, or the panel cannot show the failure it is drawn for.
        series = [
            ("clean", [(x, 1.0) for x in (1.0, 2.0, 3.0, 4.0)]),
            ("hostile", [(x, 1.0) for x in (1.0, 2.0, 3.0, 4.0)]),
        ]
        bounds = [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}]
        extent = MANDATE.bar_axis_extent(series, bounds)
        low, high = extent
        self.assertGreater(low, 0.0, "the delivery axis cannot be 0-based")
        self.assertLessEqual(high - low, 4 * (high - 0.995) + 1e-12)
        self.assertEqual(MANDATE.check_panel_axis("delivery", series, bounds, extent), [])
        # A 0.5 % loss has to be a visible step, not half a pixel.
        band = max(1.0 - 0.995, MANDATE.MIN_UNIT_SPAN)
        pixels = band / (high - low) * MANDATE.bar_plot_height(2)
        self.assertGreaterEqual(pixels, MANDATE.MIN_BOUND_PIXELS)
        # and the old axis, the one the audit measured, is refused by name
        old = MANDATE.check_panel_axis("delivery", series, bounds, (0.0, 2.0))
        self.assertEqual(len(old), 1, old)
        self.assertIn("0.5%", old[0])
        self.assertIn("sub-pixel", old[0])
        self.assertIn("M4 per-flow delivery floor 0.995", old[0])

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

    def test_panels_whose_axis_can_already_show_their_bound_are_unchanged(self):
        # The deliberate coarse view, whose fine counterpart is M4-imbalance,
        # and a floor with half the axis between it and the data: both keep the
        # zero baseline they had. What they no longer keep is the axis that
        # topped out on the bound itself (M4-shares drew 0..0.2503, so a flow
        # *over* the fair share was clipped by the frame): the axis now carries
        # `MIN_HEADROOM_PIXELS` above every value it names.
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
                    for value in MANDATE.re.findall(
                        r'text-anchor="end">([-0-9.]+)<', document
                    )
                ]
                self.assertEqual(ticks[0], 0.0)
                self.assertGreater(ticks[-1], bound, "no room above the bound")
                headroom = (
                    (ticks[-1] - bound) / (ticks[-1] - ticks[0])
                    * MANDATE.bar_plot_height(1)
                )
                self.assertGreaterEqual(headroom, MANDATE.MIN_HEADROOM_PIXELS)

    def test_the_headroom_policy_spends_the_pixel_floor_not_the_span_share(self):
        # A fair share pinned at 25 % has a data spread of a ten-thousandth, so
        # the span's own 5 % of headroom is a third of a pixel: the pixel floor
        # is the only thing that keeps an over-share bar drawable.
        values = [0.250029, 0.249914]
        low, high = MANDATE.axis_with_headroom(0.0, max(values), 0.25, 228)
        self.assertGreater((high - 0.25) / (high - low) * 228, MANDATE.MIN_HEADROOM_PIXELS)
        span_share_only = 0.25 + MANDATE.FRAME_HEADROOM * 0.25
        self.assertGreater(high, span_share_only)

    def test_a_floor_far_below_the_data_keeps_the_zero_baseline(self):
        series = [("fraction", [(1.0, 0.958217), (2.0, 0.958271)])]
        bounds = [{"y": 0.35, "label": "M3 floor 0.35x link rate"}]
        self.assertEqual(MANDATE.bar_axis_extent(series, bounds)[0], 0.0)

    def test_a_bound_the_bars_split_around_is_a_target_not_a_crossing(self):
        # M4-shares' fair share: the bars straddle it, so it is the value they are
        # read against and owes no attribution. A lone-tail bar past M2's
        # non-degrading bound is the crossing the panel has to explain.
        shares = [0.250059, 0.250059, 0.249941, 0.249941, 0.250173, 0.249365,
                  0.250289, 0.250173]
        self.assertEqual(MANDATE.crossing_values(shares, 0.25), [])
        latency = [26.251, 61.5, 185.8015]
        self.assertEqual(MANDATE.crossing_values(latency, 100.0), [185.8015])

    # -- the governance test: a crossed bound must say what it governs -------

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
        rects = MANDATE.re.findall(r'<rect x="[-0-9.]+\w*"', document)
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
        bounds = MANDATE.re.findall(
            r'class="bound" x1="([0-9.]+)" y1="[0-9.]+" x2="([0-9.]+)"', document
        )
        self.assertEqual(len(bounds), 1, bounds)
        left, right = (float(value) for value in bounds[0])
        # governed by x=1 alone, so the line stops over the first bar group
        # instead of running to the plot's right edge as a panel-wide one does
        self.assertGreater(right - left, 0.0)
        self.assertLess(right, MANDATE.REPORT.WIDTH - MANDATE.REPORT.PAD_RIGHT)

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

    # -- the label-fit test: an annotation must lie inside the plot --------
    #
    # The measurement these come from: on the preserved battery run the three
    # panels whose bound sat at the top of a band-view axis (`M2-delivery`,
    # `M4-imbalance`, `M4-shares`) drew their label 4.6-14.7 px *above* the plot
    # area, across the legend, and a bound governing a narrow x-window drew its
    # label off the plot's left edge. The vertical part of the fit needs no
    # font at all (it is the anchor plus the ascent/descent); the horizontal
    # part is `rtp_trace_report.label_text_width`, an upper bound over the fonts
    # a browser resolves for the panel's 11px text style, pinned by
    # `RENDERED_LABEL_WIDTHS` to the widths the real renderer measured.

    def rendered_labels(self, declaration, rows, name, *arguments):
        """Render one panel and return ``(document, plot rect, [(declared, line, box)])``."""
        code, stderr, out = self.render_mandate(declaration, rows, name, *arguments)
        self.assertEqual(code, 0, stderr)
        panel_id = declaration["panels"][0]["id"]
        document = (out / f"{declaration['mandate']}-{panel_id}.svg").read_text(
            encoding="utf-8"
        )
        return (
            document,
            MANDATE.panel_plot_rect(panel_id, document),
            MANDATE.label_boxes(document),
        )

    def bound_line_y(self, document):
        match = MANDATE.re.search(r'class="bound" x1="[-0-9.]+" y1="([-0-9.]+)"', document)
        self.assertIsNotNone(match, "the panel draws no bound line")
        return float(match.group(1))

    def test_every_real_panel_draws_its_bound_labels_inside_its_plot_area(self):
        cases = (
            ("M1line", HEALTHY_DECLARATION, HEALTHY_ROWS, ()),
            (
                "M2latency",
                M2_LATENCY_DECLARATION,
                M2_LATENCY_ROWS,
                ("--run-values", json.dumps(M2_LATENCY_RUN_VALUES)),
            ),
            ("M2delivery", M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS, ()),
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS, ()),
            ("M4delivery", DELIVERY_DECLARATION, DELIVERY_ROWS, ()),
        )
        for name, declaration, rows, arguments in cases:
            with self.subTest(panel=name):
                document, plot, boxes = self.rendered_labels(
                    declaration, rows, name, *arguments
                )
                self.assertTrue(boxes, "the panel drew no bound label at all")
                self.assertEqual(
                    MANDATE.check_label_fit(declaration["panels"][0]["id"], document),
                    [],
                )
                left, top, right, bottom = plot
                for declared, line, (x0, y0, x1, y1) in boxes:
                    # The vertical extent is width-free, so it is asserted
                    # against the plot rectangle directly rather than through
                    # the model the check uses.
                    self.assertGreaterEqual(y0, top, declared)
                    self.assertLessEqual(y1, bottom, declared)
                    self.assertGreaterEqual(x0, left, declared)
                    self.assertLessEqual(x1, right, declared)
                    self.assertAlmostEqual(
                        y1 - y0,
                        MANDATE.REPORT.LABEL_ASCENT_PX + MANDATE.REPORT.LABEL_DESCENT_PX,
                    )

    def test_a_bound_at_the_top_of_its_axis_is_labelled_below_its_line(self):
        # `M2-delivery`'s floor is the band view's own top, so there is no room
        # for the label above the line; it has to drop below it rather than
        # escape into the legend, which is where the audit measured it.
        for name, declaration, rows in (
            ("M2delivery", M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS),
            ("M4shares", SHARES_DECLARATION, SHARES_ROWS),
        ):
            with self.subTest(panel=name):
                document, plot, boxes = self.rendered_labels(declaration, rows, name)
                bound_y = self.bound_line_y(document)
                for _, line, (_, y0, _, _) in boxes:
                    self.assertGreater(
                        y0,
                        bound_y,
                        f"{line!r} is drawn above its own bound and out of the plot",
                    )
                self.assertGreaterEqual(boxes[0][2][1], plot[1])

    def test_a_badly_placed_label_fails_the_fit_check(self):
        # The vacuity pair for the check itself: the same markup, with its own
        # label moved to the canvas origin, must go red while the untouched
        # markup passes. Without this the check could be a predicate that is
        # true of everything.
        markup = MANDATE.svg_bar_chart(
            "t",
            "x",
            "arm",
            [("s", [(1.0, 1.0), (2.0, 2.5)])],
            [{"y": 2.0, "label": "a bound"}],
        )
        self.assertEqual(MANDATE.check_label_fit("p", markup), [])
        misplaced = MANDATE.re.sub(
            r'(<text class="bound-label" x=")[-0-9.]+(" y=")[-0-9.]+(")',
            r"\g<1>0.0\g<2>0.0\g<3>",
            markup,
        )
        self.assertNotEqual(misplaced, markup)
        problems = MANDATE.check_label_fit("p", misplaced)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not fit the plot area", problems[0])
        self.assertIn("past its left edge", problems[0])
        self.assertIn("above it", problems[0])

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

    def test_a_guard_for_every_arm_is_drawn_on_its_own_band_and_inside_the_plot(self):
        # The four-arm latency panel: the run states a guard for *every* arm, so
        # no arm is left for the declaration's bound to govern. The panel draws
        # each arm's guard over that arm's own band and the declared bound
        # across the whole plot saying exactly that it governs no arm -- rather
        # than one bound line a reader has to read as four different arms'
        # floors. Every label it draws, including the long declared one, has to
        # lie inside the plot.
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "p99_ms", 1.0, 26.251],
            ["latency", "p99_ms", 2.0, 61.5],
            ["latency", "p99_ms", 3.0, 90.0],
            ["latency", "p99_ms", 4.0, 185.8015],
        ]
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [{"y": 100.0, "label": "M2 non-degrading p99 bound (ms)"}],
                }
            ],
        }
        document, plot, boxes = self.rendered_labels(
            declaration,
            rows,
            "M2wrap",
            "--run-values",
            json.dumps(M2_LATENCY_FOUR_ARM_RUN_VALUES),
        )
        self.assertGreater(len(boxes), 1, "the labels have to wrap or be several")
        self.assertEqual(MANDATE.check_label_fit("latency", document), [])
        for declared, _, (x0, y0, x1, y1) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])
            self.assertGreaterEqual(y0, plot[1])
            self.assertLessEqual(y1, plot[3])
        # Every guard the run states is on the panel, on its own band, and the
        # declared bound is there too saying it governs none of them.
        titles = [declared for declared, _, _ in boxes]
        self.assertEqual(document.count('class="bound"'), 5)
        for key in (
            "clean_p99_guard=300",
            "hostile_p99_guard=200",
            "lone_p99_guard=400",
            "burst_p99_guard=600",
        ):
            self.assertTrue(
                any(title.startswith(f"run {key}") for title in titles), titles
            )
        self.assertTrue(
            any(title.startswith("M2 non-degrading p99 bound (ms)") for title in titles), titles
        )
        self.assertTrue(
            any("governs no arm of this run" in title for title in titles), titles
        )
        # The wrapped lines of one label re-join to the sentence in its title,
        # so a wrapped label is still one annotation. Only the *first* drawn
        # line of a wrap carries the whole sentence in its `<title>`; the rest
        # are continuation elements with no title of their own.
        groups: list[list] = []
        for element in MANDATE.re.findall(
            r'<text class="bound-label"[^>]*>(.*?)</text>', document, MANDATE.re.S
        ):
            title = MANDATE.re.search(r"<title>(.*?)</title>", element, MANDATE.re.S)
            line = MANDATE.re.sub(r"<title>.*?</title>", "", element).strip()
            if title is not None:
                groups.append([MANDATE.html.unescape(title.group(1)), [line]])
            else:
                self.assertTrue(groups, f"a continuation line with no label: {line!r}")
                groups[-1][1].append(line)
        for declared, lines in groups:
            self.assertEqual(" ".join(lines), declared, lines)
        self.assertEqual(
            sum(len(lines) for _, lines in groups),
            len(boxes),
            "every drawn line belongs to exactly one label",
        )
        wrapped = [declared for declared, lines in groups if len(lines) > 1]
        self.assertEqual(len(wrapped), 1, "the declared label is the one that wraps")
        self.assertIn(
            "governs no arm of this run",
            wrapped[0],
            "the wrapped label is the declared bound, not a guard's",
        )

    def test_a_narrow_governed_window_moves_the_label_inside_the_plot(self):
        # A bound governing the first of three categories has a line only as
        # long as that category's slot; the label's anchor has to move right
        # rather than run the text off the plot's left edge, which is what the
        # old fixed anchor did.
        declaration = {
            **M2_LATENCY_DECLARATION,
            "panels": [
                {
                    **M2_LATENCY_DECLARATION["panels"][0],
                    "bounds": [
                        {
                            "y": 6,
                            "label": "M2 non-degrading p99 bound (ms)",
                            "series": "p99_ms",
                            "x": [1],
                        }
                    ],
                }
            ],
        }
        document, plot, boxes = self.rendered_labels(
            declaration,
            M2_LATENCY_ROWS,
            "M2narrow",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        line = MANDATE.re.search(
            r'class="bound" x1="[-0-9.]+" y1="[-0-9.]+" x2="([-0-9.]+)"',
            document,
        )
        line_right = float(line.group(1))
        for declared, _, (x0, _, x1, _) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])
            # the label left its governed window rather than leave the plot
            self.assertGreater(x1, line_right)
        self.assertGreater(line_right - plot[0], 0.0)

    def test_the_label_width_model_does_not_underestimate_the_rendered_text(self):
        for label, measured in RENDERED_LABEL_WIDTHS:
            with self.subTest(label=label):
                self.assertGreaterEqual(
                    MANDATE.REPORT.label_text_width(label),
                    measured,
                    "the width model must not be narrower than the text the "
                    "browser draws, or a label it calls fitting can overflow",
                )

    def test_the_width_model_check_fails_when_the_model_is_narrowed(self):
        # The vacuity half of the calibration: the same fixture test on a model
        # narrowed below the measured widths must go red, so the assertion above
        # is about the model and not a tautology.
        label, measured = RENDERED_LABEL_WIDTHS[0]
        with mock.patch.object(MANDATE.REPORT, "LABEL_ADVANCE_SAFETY", 0.2):
            self.assertLess(MANDATE.REPORT.label_text_width(label), measured)

    def test_every_bar_beyond_the_bound_is_not_labelled_as_a_crossing(self):
        # A boundary case of the attribution rule: when *every* bar is past the
        # bound the crossing is the verdict's, not one arm's tolerated guard, so
        # the panel draws no `N of M` clause. Uniform failure is read from the
        # bars; a single bar past it is the case the clause exists for. The
        # guards are still named -- they are the arms' own bounds, and the axis
        # has to carry them whether or not a bar has been past the bound yet --
        # but no crossing is attributed.
        rows = [
            ["panel", "series", "x", "y"],
            ["latency", "p99_ms", 1.0, 120.0],
            ["latency", "p99_ms", 2.0, 158.3],
            ["latency", "p99_ms", 3.0, 135.0],
        ]
        # Three arms, the number of bars the panel draws: a run enumerating four
        # arms over a three-bar panel attributes nothing, and the guards it
        # names would then have no band to be drawn on.
        run_values = {
            "clean_p99_ms": 120.0,
            "hostile_p99_ms": 158.3,
            "lone_p99_ms": 135.0,
            "clean_p99_guard": 50.0,
            "hostile_p99_guard": 266.7,
            "lone_p99_guard": 333.3,
        }
        document, plot, boxes = self.rendered_labels(
            M2_LATENCY_DECLARATION,
            rows,
            "M2all",
            "--run-values",
            json.dumps(run_values),
        )
        self.assertNotIn("beyond it", document)
        self.assertNotIn("under it", document)
        # Every guard the run states is drawn, and the declared bound says it
        # governs no arm.
        titles = [declared for declared, _, _ in boxes]
        self.assertEqual(document.count('class="bound"'), 4)
        for key in ("clean_p99_guard=50", "hostile_p99_guard=266.7", "lone_p99_guard=333.3"):
            self.assertTrue(
                any(title.startswith(f"run {key}") for title in titles), titles
            )
        self.assertTrue(
            any("governs no arm of this run" in title for title in titles), titles
        )
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(
            ticks[-1],
            max(value for _, value in MANDATE.run_guards(
                run_values,
                [("p99_ms", [])],
                "M2 non-degrading p99 bound (ms)",
            )),
            "the axis must carry every guard the label names",
        )
        for _, _, (x0, y0, x1, y1) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])

    def test_a_lone_bar_beyond_the_bound_is_drawn_under_its_own_arm_s_guard(self):
        # The other boundary case, and the one this drawing exists for: one of
        # three bars past the *declared bound*, with the run's guards drawn on
        # the arms they govern. Before this the panel drew one line, the bound
        # across every arm, on an axis reaching the guards -- so the `lone`
        # bar sat above the bound on a PASS with no line to cross, and
        # the tolerance was a sentence the reader had to trust. Now the line
        # over the `lone` band is its own guard, and the bar is under it.
        document, plot, boxes = self.rendered_labels(
            M2_LATENCY_DECLARATION,
            M2_LATENCY_ROWS,
            "M2one",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        titles = [declared for declared, _, _ in boxes]
        self.assertEqual(
            titles,
            [
                "M2 non-degrading p99 bound (ms) [governs clean; 1 of 3 bars beyond it]",
                "run hostile_p99_guard=200 [governs hostile]",
                "run lone_p99_guard=400 [governs lone]",
            ],
        )
        for _, _, (x0, _, x1, _) in boxes:
            self.assertGreaterEqual(x0, plot[0])
            self.assertLessEqual(x1, plot[2])
        # The drawn geometry, not the label: the line over the `lone` band is
        # its guard, and the lone bar's top is below it and above the bound.
        panel = M2_LATENCY_DECLARATION["panels"][0]
        series = [("p99_ms", [(1.0, 26.251), (2.0, 61.5), (3.0, 185.8015)])]
        bounds = MANDATE._bound_specs(panel)
        drawn_bounds = MANDATE.drawable_bounds(panel, series, bounds, M2_LATENCY_RUN_VALUES)
        axis = MANDATE.bar_axis_extent(
            series, drawn_bounds, M2_LATENCY_RUN_VALUES, MANDATE.bar_plot_height(len(series))
        )
        values = MANDATE.drawn_bound_values(document, axis)
        self.assertEqual(len(values), 3, values)
        spans = MANDATE.re.findall(
            r'class="bound" x1="([-0-9.]+)" y1="([-0-9.]+)" x2="([-0-9.]+)"', document
        )
        rightmost = max(spans, key=lambda span: float(span[2]))
        guard = MANDATE.drawn_bound_values(
            f'<rect x="72.0" y="24.0" width="864.0" height="228.0" class="plot-bg"/>'
            f'<line class="bound" x1="{rightmost[0]}" y1="{rightmost[1]}" '
            f'x2="{rightmost[2]}" y2="{rightmost[1]}" />',
            axis,
        )[0]
        # The value is read back through the drawn pixel grid, so the round trip
        # is a fraction of a pixel wide rather than exact.
        self.assertAlmostEqual(guard, 400.0, delta=1.0)
        lone_bar = 185.8015
        self.assertGreater(lone_bar, 100.0, "the lone bar is past the bound")
        self.assertLess(lone_bar, guard, "and inside its own arm's guard")

    def test_a_guard_that_is_named_and_not_drawn_is_refused(self):
        # The vacuity of the guard-drawing rule, as a predicate and end to end.
        # Both halves are the *pre-fix* artifact: one line, the bound across
        # every arm, with the run's guards named on its caption and no line at
        # either of them. The predicate reads the drawn lines back out of the
        # SVG, so a renderer that stopped drawing a guard it names goes red.
        panel = M2_LATENCY_DECLARATION["panels"][0]
        series = MANDATE.panel_series(panel, _points(M2_LATENCY_ROWS))
        bounds = MANDATE._bound_specs(panel)
        plan = MANDATE.drawable_bounds(panel, series, bounds, M2_LATENCY_RUN_VALUES)
        axis = MANDATE.bar_axis_extent(
            series, plan, M2_LATENCY_RUN_VALUES, MANDATE.bar_plot_height(len(series))
        )
        guards = MANDATE.named_guard_values(
            series, plan, M2_LATENCY_RUN_VALUES, crossing=True
        )
        self.assertEqual(guards, [200.0, 400.0])
        drawn = MANDATE.svg_bar_chart(
            "t", "x", "value", series, plan, axis, M2_LATENCY_RUN_VALUES
        )
        self.assertEqual(MANDATE.check_named_guards_drawn("latency", guards, axis, drawn), [])
        stripped = MANDATE.svg_bar_chart(
            "t", "x", "value", series, [plan[0]], axis, M2_LATENCY_RUN_VALUES
        )
        problems = MANDATE.check_named_guards_drawn("latency", guards, axis, stripped)
        self.assertEqual(len(problems), 2, problems)
        for value in ("200", "400"):
            self.assertTrue(
                any(f"names the guard {value}" in problem for problem in problems),
                problems,
            )
        # End to end: the same stripping through the render path refuses the
        # panel rather than writing one that names guards it does not draw.
        plan_of = MANDATE.drawable_bounds
        with mock.patch.object(
            MANDATE, "drawable_bounds", lambda *a, **k: [plan_of(*a, **k)[0]]
        ):
            code, stderr, _ = self.render_mandate(
                M2_LATENCY_DECLARATION,
                M2_LATENCY_ROWS,
                "M2nodraw",
                "--run-values",
                json.dumps(M2_LATENCY_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("names the guard 200", stderr)
        self.assertIn("names the guard 400", stderr)
        self.assertIn("take on trust", stderr)

    # -- healthy renders ---------------------------------------------------

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
        self.assertGreater(MANDATE.RENDER.panel_series_count(document), 0)

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
        polyline = MANDATE.RENDER.POLYLINE_RE.search(document).group(1)
        xs = [float(pair.split(",")[0]) for pair in polyline.split()]
        self.assertEqual(xs, sorted(xs))

    # -- declaration failures ---------------------------------------------

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

    # -- data failures -----------------------------------------------------

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

    # -- rasterization reasons honestly ------------------------------------

    def test_no_rasterize_writes_svgs_without_consulting_a_browser(self):
        declaration_path = self.write_mandate()
        with mock.patch.object(
            MANDATE.RENDER, "find_browser", side_effect=AssertionError("no browser may be consulted")
        ):
            code, stdout, stderr = self.run_main(
                str(declaration_path), "--no-rasterize", "--out", str(self.out)
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn("panels: 2", stdout)
        self.assertEqual(len(list(self.out.glob("*.svg"))), 2)

    def test_rasterize_without_a_browser_is_an_error_after_writing_svgs(self):
        declaration_path = self.write_mandate()
        with mock.patch.object(MANDATE.RENDER, "find_browser", return_value=None):
            code, _, stderr = self.run_main(
                str(declaration_path), "--out", str(self.out)
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
            self.assertEqual(
                MANDATE.RENDER.png_dimensions(png_path.read_bytes()), (1, 1)
            )
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
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                str(declaration_path),
                "--no-rasterize",
                "--out",
                str(self.out),
            ],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("mandate data CSV not found", completed.stderr)

    def test_find_browser_env_override_is_honoured(self):
        override = Path(sys.executable)
        with mock.patch.dict(os.environ, {MANDATE.RENDER.BROWSER_ENV: str(override)}):
            self.assertEqual(MANDATE.RENDER.find_browser(), str(override))

    # -- the readings that only the eye made, now measurements ----------------
    #
    # Every defect below was found by *looking at* a run whose four mandate
    # lines passed: a panel announced guards on an axis that topped out below
    # them; `M4-shares` drew its fair share at the very
    # top of its own axis, so a flow over the share could not be drawn at all;
    # one series' three bars were drawn flush and read as a staircase; the
    # legend said `shaper_forwarded`; and a label long enough to run off the canvas was
    # drawn anyway. Each test renders the broken input and requires the refusal
    # (red), then renders the real input and requires the check to pass (green).

    def test_a_guard_the_panel_names_outside_its_axis_is_refused(self):
        broken = {
            **M2_LATENCY_DECLARATION,
            "panels": [{**M2_LATENCY_DECLARATION["panels"][0], "y_extent": [0.0, 190.0]}],
        }
        code, stderr, _ = self.render_mandate(
            broken,
            M2_LATENCY_ROWS,
            "M2pin",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("named guard 200", stderr)
        self.assertIn("does not resolve", stderr)
        # green: the automatic axis carries both guards, inside the frame
        document, _, _ = self.rendered_labels(
            M2_LATENCY_DECLARATION,
            M2_LATENCY_ROWS,
            "M2carry",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(ticks[-1], 400.0, "the axis tops out below the guard named")
        self.assertEqual(
            MANDATE.check_named_values_in_axis(
                "latency",
                [{"y": 100.0, "label": "M2 non-degrading p99 bound (ms)"}],
                [200.0, 400.0],
                (ticks[0], ticks[-1]),
            ),
            [],
        )

    def test_a_bound_with_no_room_above_it_is_refused(self):
        # The axis the audit found on M4-shares: 0..0.25, the fair share itself,
        # so every bar is clipped at the line the panel exists to watch.
        broken = {
            **SHARES_DECLARATION,
            "panels": [
                {**SHARES_DECLARATION["panels"][0], "y_extent": [0.0, 0.25]}
            ],
        }
        code, stderr, _ = self.render_mandate(broken, SHARES_ROWS, "M4flat")
        self.assertNotEqual(code, 0)
        self.assertIn("over-bound bar", stderr)
        self.assertIn("same picture", stderr)
        # green: the automatic extent keeps MIN_HEADROOM_PIXELS over the bound
        document, _, _ = self.rendered_labels(
            SHARES_DECLARATION, SHARES_ROWS, "M4room"
        )
        ticks = [
            float(value)
            for value in MANDATE.re.findall(r'text-anchor="end">([-0-9.]+)<', document)
        ]
        self.assertGreater(ticks[-1], 0.25)
        self.assertEqual(
            MANDATE.check_bound_headroom(
                "shares",
                [{"y": 0.25, "label": "fair share 25.0%"}],
                [],
                (ticks[0], ticks[-1]),
            ),
            [],
        )

    def test_a_two_sided_bound_is_drawn_on_both_of_its_sides(self):
        # The defect, measured on the run whose panel read `fair-share bound
        # ±1.0%` beside a single `+0.01` line: the axis' *low* came from the
        # data (-0.000353) and not from the bound, so the `-1 %` arm sat 202 px
        # below the frame and a starved flow's departure was a bar flush with
        # the frame bottom, crossing nothing. The panel has to draw the band
        # where its own label says it applies.
        declaration = SHARES_IMBALANCE_DECLARATION
        panel = declaration["panels"][1]
        points = _points(SHARES_IMBALANCE_ROWS)
        series = MANDATE.panel_series(panel, points)
        bounds = MANDATE._bound_specs(panel)
        drawn_bounds = MANDATE.drawable_bounds(panel, series, bounds, None)
        plot_height = MANDATE.bar_plot_height(len(series))
        extent = MANDATE.panel_axis_extent(
            panel, series, drawn_bounds, None, plot_height
        )
        code, stderr, out = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4band"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        # green: both arms are in the artifact, each at its own value.
        arms = sorted(MANDATE.drawn_bound_values(document, extent))
        self.assertEqual(len(arms), 2, arms)
        self.assertAlmostEqual(arms[0], -0.01, places=4)
        self.assertAlmostEqual(arms[1], 0.01, places=4)
        self.assertEqual(
            MANDATE.check_two_sided_bound_drawn(
                "imbalance", drawn_bounds, extent, document, plot_height
            ),
            [],
        )
        # and the labels say which arm each line is: the declared sentence on
        # the declared value, the mirrored value on the mirrored arm.
        self.assertEqual(
            [declared for declared, _, _ in MANDATE.label_boxes(document)],
            [
                "fair-share bound ±1.0%",
                "fair-share bound -1.0%",
            ],
        )
        # A one-sided bound is left exactly as declared: the mirror is for the
        # declaration that names a band, not for every bound.
        floor = SHARES_DECLARATION["panels"][0]["bounds"]
        self.assertEqual(MANDATE.mirrored_bounds(floor), floor)
        self.assertEqual(
            MANDATE.check_two_sided_bound_drawn("shares", floor, extent, document),
            [],
        )
        # red: the same panel with its lower arm removed -- the one-sided
        # artifact the run actually drew. The check has to name the missing arm,
        # not merely fail to find fault.
        element = max(
            MANDATE.re.findall(r'<line class="bound"[^>]*/>', document),
            key=lambda line: float(
                MANDATE.re.search(r'y1="([-0-9.]+)"', line).group(1)
            ),
        )
        one_sided = document.replace(element, "", 1)
        self.assertNotEqual(one_sided, document)
        problems = MANDATE.check_two_sided_bound_drawn(
            "imbalance", drawn_bounds, extent, one_sided, plot_height
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("draws no line at -0.01", problems[0])
        self.assertIn("crossing nothing", problems[0])
        # The pre-fix axis, from the declared bound alone: this is the existing
        # axis-range check, which covers the other half of the property as soon
        # as the arm is a value the panel *names*.
        old_extent = MANDATE.bar_axis_extent(series, bounds, None, plot_height)
        self.assertLess(old_extent[0], 0.0)
        self.assertGreater(old_extent[0], -0.01)
        stale = MANDATE.check_named_values_in_axis(
            "imbalance", drawn_bounds, [], old_extent, plot_height
        )
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("-0.01", stale[0])
        self.assertIn("below it", stale[0])

    def test_the_lower_arm_of_a_band_keeps_room_below_it(self):
        # The second half of the same defect: an axis whose low *is* the lower
        # arm gives a bar that crosses it nowhere to go, so the breach and the
        # arm are drawn as the same picture. `check_bound_headroom` measured
        # only the side above the highest bound; the downward-failing side is
        # the same rule.
        declaration = SHARES_IMBALANCE_DECLARATION
        panel = declaration["panels"][1]
        series = MANDATE.panel_series(panel, _points(SHARES_IMBALANCE_ROWS))
        bounds = MANDATE._bound_specs(panel)
        drawn_bounds = MANDATE.drawable_bounds(panel, series, bounds, None)
        plot_height = MANDATE.bar_plot_height(len(series))
        extent = MANDATE.panel_axis_extent(
            panel, series, drawn_bounds, None, plot_height
        )
        # green: the automatic extent keeps MIN_HEADROOM_PIXELS below the arm...
        self.assertEqual(
            MANDATE.check_bound_headroom(
                "imbalance", drawn_bounds, [], extent, plot_height, series
            ),
            [],
        )
        below = (-0.01 - extent[0]) / (extent[1] - extent[0]) * plot_height
        self.assertGreaterEqual(below, MANDATE.MIN_HEADROOM_PIXELS)
        # ...and above it.
        above = (extent[1] - 0.01) / (extent[1] - extent[0]) * plot_height
        self.assertGreaterEqual(above, MANDATE.MIN_HEADROOM_PIXELS)
        # red: the axis flush with the lower arm is refused, naming the side.
        flush = (extent[0], extent[1])
        problems = MANDATE.check_bound_headroom(
            "imbalance", drawn_bounds, [], (-0.01, flush[1]), plot_height, series
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("below the bound", problems[0])
        self.assertIn("flush with the frame", problems[0])

    def test_a_two_sided_bound_cannot_be_written_one_sided(self):
        # End to end: with the mirror disabled the pipeline must not write the
        # panel at all, rather than writing the half-bound the run drew.
        declaration = SHARES_IMBALANCE_DECLARATION
        with mock.patch.object(MANDATE, "mirrored_bounds", lambda bounds: bounds):
            code, stderr, _ = self.render_mandate(
                declaration, SHARES_IMBALANCE_ROWS, "M4one"
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("draws no line at -0.01", stderr)
        # and the arm's missing headroom is stated as what it is, not as a
        # negative distance: on a one-sided axis the -1 % arm is *outside* it.
        self.assertIn("outside the axis below it", stderr)

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

    def test_a_bound_label_drawn_twice_at_one_anchor_is_refused(self):
        code, stderr, out = self.render_mandate(SHARES_DECLARATION, SHARES_ROWS, "M4dup")
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        self.assertEqual(MANDATE.check_label_overlap("shares", document), [])
        element = MANDATE.re.search(
            r'<text class="bound-label".*?</text>', document, MANDATE.re.S
        ).group(0)
        # red: the artifact the run drew, with its label element duplicated on
        # the same anchor -- the `fair share 25.0%fair share 25.0%` reading
        problems = MANDATE.check_label_overlap("shares", document.replace(element, element + element, 1))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("drawn twice on the same anchor", problems[0])
        # and two *different* labels over one another are refused as an overlap
        other = element.replace("fair share 25.0%", "fair-share bound")
        overlap = MANDATE.check_label_overlap("shares", document.replace(element, element + other, 1))
        self.assertEqual(len(overlap), 1, overlap)
        self.assertIn("overlap", overlap[0])

    def test_bars_drawn_flush_are_refused(self):
        # red: the geometry the preserved run drew -- three 311 px bars whose
        # rectangles overlapped by 52 px, which the eye read as one staircase.
        staircase = (
            '<svg viewBox="0 0 960 300">'
            '<rect x="72.0" y="178.2" width="311.0" height="73.8" fill="#2563eb"/>'
            '<rect x="331.2" y="90.1" width="311.0" height="161.9" fill="#2563eb"/>'
            '<rect x="590.4" y="31.3" width="311.0" height="220.7" fill="#2563eb"/>'
            "</svg>"
        )
        problems = MANDATE.check_bar_separation("latency", staircase)
        self.assertTrue(problems)
        self.assertIn("overlap by 51.8 px", problems[0])
        # green: the rendered panel's three bars are separate
        code, stderr, out = self.render_mandate(
            M2_LATENCY_DECLARATION,
            M2_LATENCY_ROWS,
            "M2gap",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-latency.svg").read_text(encoding="utf-8")
        self.assertEqual(len(MANDATE.bar_boxes(document)), 3)
        self.assertEqual(MANDATE.check_bar_separation("latency", document), [])

    def test_a_legend_that_draws_a_column_name_is_refused(self):
        code, stderr, out = self.render_mandate(
            M2_LATENCY_DECLARATION,
            M2_LATENCY_ROWS,
            "M2legend",
            "--run-values",
            json.dumps(M2_LATENCY_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-latency.svg").read_text(encoding="utf-8")
        self.assertEqual(MANDATE.legend_text(document), ["p99 ms"])
        series = [("p99_ms", [(1.0, 2.0)])]
        self.assertEqual(MANDATE.check_series_labels("latency", document, series), [])
        # red: the same panel with the producer's column name in the legend --
        # the label the preserved run drew
        raw = document.replace("p99 ms", "p99_ms")
        problems = MANDATE.check_series_labels("latency", raw, series)
        self.assertTrue(problems)
        self.assertIn("p99_ms", problems[0])

    def test_a_clipped_label_and_a_placeholder_are_refused(self):
        # red: the y label with the band-view note the old renderer appended,
        # which is 66 characters long and runs off the top and bottom of the
        # 300 px canvas when it is rotated down the 18 px left margin
        long_label = {
            **M2_DELIVERY_DECLARATION,
            "panels": [
                {
                    **M2_DELIVERY_DECLARATION["panels"][0],
                    "y_label": "delivery (received / offered) [band view 0.979..1.001, "
                    "not 0-based]",
                }
            ],
        }
        code, stderr, _ = self.render_mandate(long_label, M2_DELIVERY_ROWS, "M2clip")
        self.assertNotEqual(code, 0)
        self.assertIn("draws it clipped", stderr)
        self.assertIn("px above it", stderr)
        # red: a label carrying the empty template its absent evidence left
        broken = {
            **M2_DELIVERY_DECLARATION,
            "panels": [
                {
                    **M2_DELIVERY_DECLARATION["panels"][0],
                    "bounds": [
                        {
                            "y": 1.0,
                            "label": "M2 delivery floor 1.000 []",
                        }
                    ],
                }
            ],
        }
        code, stderr, _ = self.render_mandate(broken, M2_DELIVERY_ROWS, "M2empty")
        self.assertNotEqual(code, 0)
        self.assertIn("empty placeholder", stderr)
        # green: the real panel's every text is inside the canvas
        document, _, _ = self.rendered_labels(
            M2_DELIVERY_DECLARATION, M2_DELIVERY_ROWS, "M2txt"
        )
        self.assertEqual(MANDATE.check_canvas_text_fit("delivery", document), [])
        self.assertEqual(MANDATE.band_view_note((0.979, 1.001)),
                         "band view 0.979..1.001, not 0-based")
        self.assertEqual(MANDATE.band_view_note((0.0, 0.26)), "")


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
                    for value in MANDATE.re.findall(
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

    # -- the sliver-bound test: a fault panel states where its bound sits -----

    def render_sliver(self, declaration, rows, name, run_values=None):
        """Render one fault-geometry declaration and return its imbalance SVG."""
        arguments = [] if run_values is None else [
            "--run-values",
            json.dumps(run_values),
        ]
        code, stderr, out = self.render_mandate(declaration, rows, name, *arguments)
        self.assertEqual(code, 0, stderr)
        panel = declaration["panels"][0]["id"]
        return (out / f"{declaration['mandate']}-{panel}.svg").read_text(
            encoding="utf-8"
        )

    def test_the_starved_flows_sliver_bound_is_stated_not_refused(self):
        document = self.render_sliver(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4drop"
        )
        # Both arms are drawn -- the fault panel is evidence, not an error --
        # and the panel says where the bound sits and how far the bars are.
        self.assertEqual(document.count('class="bound"'), 2)
        notes = " ".join(MANDATE.drawn_notes(document))
        self.assertIn(
            'bound "fair-share bound \u00b11.0%" at 0.01 on axis -1..0.0605: '
            "band 0.009884 = 2.1 px; nearest bar 0.000116, 0.009884 away; "
            "furthest -1, 217.1 px from the bound",
            notes,
        )
        # The mirrored arm is 4.3 px from the stated one -- closer than a line
        # of text -- so it is drawn unlabelled and its own position is stated.
        self.assertEqual(document.count("<title>fair-share bound"), 1)
        self.assertIn("its arm at -0.01 (4.3 px away) is drawn unlabelled", notes)

    def test_the_starved_flows_sliver_is_still_refused_without_the_statement(self):
        # The pre-fix behaviour, and the check's own vacuity: the same geometry
        # with no statement is refused by the axis test naming the band, and the
        # statement check refuses a panel that draws the sliver silently.
        series = DROP_IMBALANCE_SERIES
        bounds = [{"y": 0.01, "label": "fair-share bound \u00b11.0%"}]
        drawn = MANDATE.mirrored_bounds(bounds)
        axis = MANDATE.bar_axis_extent(series, drawn, None)
        problems = MANDATE.check_panel_axis("imbalance", series, drawn, axis)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("sub-pixel", problems[0])
        self.assertIn("fair-share bound \u00b11.0%", problems[0])
        # red: the panel that draws the sliver and states nothing
        markup = MANDATE.svg_bar_chart(
            "M4 [imbalance]",
            "flow (1..4)",
            "departure from the fair share",
            series,
            drawn,
            axis,
            None,
        )
        silent = MANDATE.check_sliver_bound_stated(
            "imbalance", series, drawn, axis, markup
        )
        self.assertEqual(len(silent), 1, silent)
        self.assertIn("fair-share bound \u00b11.0%", silent[0])
        self.assertIn("states nothing", silent[0])
        self.assertIn("2.1 px", silent[0])
        # green: the same artifact with the statement's own numbers on it
        document = self.render_sliver(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4drop2"
        )
        self.assertEqual(
            MANDATE.check_sliver_bound_stated(
                "imbalance", series, drawn, axis, document
            ),
            [],
        )

    def test_the_late_arms_sliver_ceiling_is_stated_not_refused(self):
        document = self.render_sliver(
            LATE_LATENCY_DECLARATION,
            LATE_LATENCY_ROWS,
            "M4late",
            LATE_LATENCY_RUN_VALUES,
        )
        # The ceiling is 1.6 px of a 4565.34 ms axis and the fault's own body is
        # 204.7 px past it, so the panel states both rather than refusing.
        notes = " ".join(MANDATE.drawn_notes(document))
        self.assertIn(
            'bound "M1 ceiling 250 ms" at 250 on axis 0..4565.34: band 32.427 '
            "= 1.6 px; nearest bar 282.427, 32.427 away; furthest 4347.94, "
            "204.7 px from the bound",
            notes,
        )
        # The axis test still measures the band it always did, and the run's own
        # guard is still drawn beside the ceiling.
        self.assertEqual(document.count('class="bound"'), 2)

    def test_a_stated_sliver_whose_numbers_are_not_the_runs_is_refused(self):
        # The statement is what the reader is told instead of the pixels, so a
        # sentence carrying a number nothing measured is worse than silence.
        document = self.render_sliver(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4drop3"
        )
        series = DROP_IMBALANCE_SERIES
        bounds = [{"y": 0.01, "label": "fair-share bound \u00b11.0%"}]
        drawn = MANDATE.mirrored_bounds(bounds)
        axis = MANDATE.bar_axis_extent(series, drawn, None)
        tampered = document.replace("217.1 px from the bound", "1.0 px from the bound")
        self.assertNotEqual(tampered, document)
        problems = MANDATE.check_sliver_bound_stated(
            "imbalance", series, drawn, axis, tampered
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("far_px=1", problems[0])

    def test_the_sliver_statement_is_not_owed_where_no_departure_is_drawn(self):
        # The other half of the rule: the statement is only the honest reading
        # where the panel *shows* a departure. A bound's furthest bar can be
        # the lowest one and a pass, so that panel keeps the refusal.
        series = [("p99_ms", [(1.0, 35.3), (2.0, 81.8), (3.0, 98.5)])]
        bounds = [{"y": 100.0, "label": "M2 non-degrading p99 bound (ms)"}]
        axis = MANDATE.bar_axis_extent(series, bounds, None)
        self.assertEqual(
            MANDATE.sliver_bound_statements(series, bounds, axis, MANDATE.bar_plot_height(1)),
            [],
        )

    def test_a_departure_smaller_than_the_band_is_not_a_statement(self):
        # The threshold the statement stands on: a departure has to be at least
        # as legible as the six pixels the band is measured against, or the
        # panel has nothing to state but the sliver itself and the refusal is
        # the right answer. On a `0..5` axis, `0.02` is 0.9 px and `0.2` is
        # 9.1 px.
        bound = {"y": 0.0, "label": "synthetic bound"}
        self.assertEqual(
            MANDATE.bound_sliver_statement(
                bound,
                [-0.02, 0.02],
                0.005,
                (0.0, 5.0),
                MANDATE.bar_plot_height(1),
            ),
            "",
        )
        statement = MANDATE.bound_sliver_statement(
            bound, [-0.2, 0.2], 0.005, (0.0, 5.0), MANDATE.bar_plot_height(1)
        )
        self.assertIn("furthest -0.2, 9.1 px from the bound", statement)

    # -- the forced panel summary: every panel states what it drew -----------

    def summary_check(self, document, declaration=None, fault=None):
        """Measure one rendered SVG back against its own drawn geometry."""
        declaration = declaration or DROP_IMBALANCE_DECLARATION
        panel = declaration["panels"][0]
        series = DROP_IMBALANCE_SERIES
        bounds = MANDATE.mirrored_bounds(panel["bounds"])
        axis = MANDATE.bar_axis_extent(series, bounds, None)
        plot_height = MANDATE.bar_plot_height(len(series))
        stated = [
            str(bound["label"])
            for bound, _ in MANDATE.sliver_bound_statements(
                series, bounds, axis, plot_height
            )
        ]
        return MANDATE.check_panel_summary_stated(
            panel["id"],
            panel["chart"],
            panel.get("x_label") or declaration["x_label"],
            panel.get("y_label") or declaration["y_label"],
            series,
            bounds,
            axis,
            document,
            plot_height,
            stated,
            None,
            fault,
        )

    def test_the_summary_states_axis_series_bounds_pixels_and_reading(self):
        # The forced summary, and the check that reads it back: the block a
        # reader takes in at a glance, with the bound's own pixel position
        # measured from the SVG's drawn `y1` rather than asserted.
        code, stderr, out = self.render_mandate(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4blk"
        )
        self.assertEqual(code, 0, stderr)
        text = (out / "M4-imbalance.summary.txt").read_text(encoding="utf-8")
        self.assertIn("panel imbalance  chart=bar  axis=-1..0.0605", text)
        self.assertIn("x=flow (1..4)", text)
        self.assertIn("y=departure from the fair share", text)
        self.assertIn("series: clean 4 pts -1..-1; hostile 4 pts", text)
        self.assertIn("bound: 'fair-share bound \u00b11.0%' y=0.01 px=", text)
        self.assertIn("reason=declaration drawn=stated-as-sliver", text)
        self.assertIn(
            "bound: 'fair-share bound -1.0%' y=-0.01 px=", text
        )
        self.assertIn("reason=declaration-band drawn=unlabelled", text)
        self.assertIn("reading: clean -1..-1 (4 pts); hostile", text)
        svg = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        drawn = [float(y) for y in MANDATE.DRAWN_BOUND_RE.findall(svg)]
        parsed = MANDATE.read_panel_summary(svg)
        self.assertEqual([entry["px"] for entry in parsed["bounds"]], drawn)

    def test_a_panel_without_a_summary_is_refused(self):
        # red: the same panel with its `<desc>` removed. The plot step refuses
        # it, so a summary-less panel cannot reach a run's evidence.
        document = self.render_sliver(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4sum0"
        )
        self.assertEqual(self.summary_check(document), [])
        silent = MANDATE.PANEL_SUMMARY_RE.sub("", document)
        self.assertNotEqual(silent, document)
        problems = self.summary_check(silent)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("carries no panel summary", problems[0])

    def test_a_summary_that_is_not_the_drawn_panel_is_refused(self):
        # red: a summary carrying a number the run did not measure is worse than
        # silence, because the reader trusts it instead of the pixels.
        document = self.render_sliver(
            DROP_IMBALANCE_DECLARATION, DROP_IMBALANCE_ROWS, "M4sum1"
        )
        parsed = MANDATE.read_panel_summary(document)
        parsed["reading"] = "clean 0..0 (4 pts)"
        tampered = MANDATE.introduce_panel_summary(
            MANDATE.PANEL_SUMMARY_RE.sub("", document), parsed
        )
        problems = self.summary_check(tampered)
        self.assertTrue(problems)
        self.assertIn("'reading'", problems[0])

    def test_a_fault_render_says_which_mandate_and_what_it_did_to_the_scale(self):
        code, stderr, out = self.render_mandate(
            DROP_IMBALANCE_DECLARATION,
            DROP_IMBALANCE_ROWS,
            "M4fault",
            "--fault",
            "M4_drop",
        )
        self.assertEqual(code, 0, stderr)
        text = (out / "M4-imbalance.summary.txt").read_text(encoding="utf-8")
        self.assertIn("fault: M4_drop", text)
        self.assertIn("fault scale:", text)
        self.assertIn("fair-share bound \u00b11.0%", text)
        self.assertIn("2.1 px of this axis", text)
        svg = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        self.assertEqual(MANDATE.read_panel_summary(svg)["fault"], "M4_drop")
        # a fault belongs to the mandate it names, not to every panel
        self.assertIsNone(MANDATE.mandate_fault("M4", "M1_latency"))
        self.assertEqual(MANDATE.mandate_fault("M4", "M4_drop"), "M4_drop")

    def test_a_fault_is_refused_where_the_summary_does_not_state_it(self):
        # red: the panel that took the fault but whose summary says nothing.
        code, stderr, out = self.render_mandate(
            DROP_IMBALANCE_DECLARATION,
            DROP_IMBALANCE_ROWS,
            "M4fault2",
            "--fault",
            "M4_drop",
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        parsed = MANDATE.read_panel_summary(svg)
        parsed["fault"] = None
        silent = MANDATE.introduce_panel_summary(
            MANDATE.PANEL_SUMMARY_RE.sub("", svg), parsed
        )
        problems = self.summary_check(silent, fault="M4_drop")
        self.assertTrue(problems)
        self.assertIn("'fault'", problems[0])

    def test_a_panel_with_no_bound_says_none_by_design(self):
        block = MANDATE.panel_summary_block(
            {
                "panel": "goodput",
                "chart": "bar",
                "axis": [0.0, 1.0],
                "x_label": "rep (1..3)",
                "y_label": "MiB/s",
                "series": [
                    {"name": "goodput", "points": 3, "min": 0.948, "max": 0.953}
                ],
                "bounds": [],
                "reading": "goodput 0.948..0.953 (3 pts)",
                "fault": None,
            }
        )
        self.assertIn("bound: none by design", block)
        self.assertNotIn("fault:", block)

    # -- the summary states both axis extents it drew ------------------------

    def test_the_summary_states_the_x_extent_the_panel_drew(self):
        # The x extent is the other half of what a panel drew -- for a CDF it is
        # the latency range its curves are read against -- and a summary that
        # omitted it left the reader to guess the one extent only the tick
        # labels carried.
        code, stderr, out = self.render_mandate(
            HEALTHY_DECLARATION, HEALTHY_ROWS, "M1xa"
        )
        self.assertEqual(code, 0, stderr)
        blocks = (out / "M1-latency.summary.txt").read_text(encoding="utf-8")
        self.assertIn("x_axis=0..2", blocks)
        latency = MANDATE.read_panel_summary(
            (out / "M1-latency.svg").read_text(encoding="utf-8")
        )
        self.assertEqual(latency["x_axis"], [0.0, 2.0])
        cdf = MANDATE.read_panel_summary(
            (out / "M1-cdf.svg").read_text(encoding="utf-8")
        )
        self.assertEqual(cdf["x_axis"], [12.5, 88.25])
        # and the drawn tick labels are the extent the summary states, to the
        # precision the axis draws them at
        ticks = MANDATE.X_AXIS_TICK_RE.findall(
            (out / "M1-cdf.svg").read_text(encoding="utf-8")
        )
        self.assertEqual(ticks[0], "12.5")
        self.assertEqual(ticks[-1], "88.2")

    def test_a_stated_x_extent_that_is_not_the_drawn_axis_is_refused(self):
        # red, at the check itself: a stated extent its own axis cannot draw.
        code, stderr, out = self.render_mandate(
            HEALTHY_DECLARATION, HEALTHY_ROWS, "M1xb"
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertEqual(
            MANDATE.check_x_axis_extent_stated(
                "cdf", "cdf", [12.5, 88.25], svg
            ),
            [],
        )
        problems = MANDATE.check_x_axis_extent_stated("cdf", "cdf", [0.0, 100.0], svg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("states the x axis 0..100", problems[0])
        self.assertIn("12.5", problems[0])
        self.assertIn("88.2", problems[0])

    def test_a_summary_without_the_x_extent_is_refused(self):
        # red: the same panel with the field dropped. An absent extent and a
        # false one are refused alike, the way an absent reading is.
        code, stderr, out = self.render_mandate(
            HEALTHY_DECLARATION, HEALTHY_ROWS, "M1xc"
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        parsed = MANDATE.read_panel_summary(svg)
        del parsed["x_axis"]
        silent = MANDATE.introduce_panel_summary(
            MANDATE.PANEL_SUMMARY_RE.sub("", svg), parsed
        )
        problems = MANDATE.check_panel_summary_stated(
            "cdf",
            "cdf",
            "latency (ms)",
            "percentile (%)",
            [("impaired", [(12.5, 33.3), (31.5, 66.7), (88.25, 100.0)])],
            [],
            (0.0, 100.0),
            silent,
            MANDATE.bar_plot_height(1),
            [],
        )
        self.assertTrue(any("'x_axis'" in problem for problem in problems), problems)

    def test_a_bar_panel_states_its_padded_category_domain(self):
        # A bar panel's x axis is the padded category domain, not the categories
        # themselves: the half-band at each end is what puts the outer bars
        # inside the plot, and it is what the axis' own ticks draw.
        code, stderr, out = self.render_mandate(
            DELIVERY_DECLARATION, DELIVERY_ROWS, "M4xbar"
        )
        self.assertEqual(code, 0, stderr)
        parsed = MANDATE.read_panel_summary(
            (out / "M4-delivery.svg").read_text(encoding="utf-8")
        )
        self.assertEqual(parsed["x_axis"], [0.5, 4.5])

    # -- a crossing must name the bound that governs the series it crossed --

    def test_a_crossing_arm_guarded_by_the_run_is_named_on_the_ceiling(self):
        # green: the lone tail's 1600 ms sample crosses the 250 ms ceiling, and
        # the run states that arm's own guard, so the drawn label names it.
        code, stderr, out = self.render_mandate(
            CROSSING_DECLARATION,
            CROSSING_ROWS,
            "M1cg",
            "--run-values",
            json.dumps(CROSSING_GUARDS),
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        labels = [declared for declared, _, _ in MANDATE.label_boxes(svg)]
        self.assertTrue(any("lone_tail" in label for label in labels), labels)
        self.assertEqual(
            MANDATE.check_crossing_series_governed(
                "latency",
                "line",
                CROSSING_SERIES,
                [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
                CROSSING_GUARDS,
                svg,
            ),
            [],
        )

    def test_a_crossing_whose_arm_is_named_nowhere_is_refused(self):
        # red, at the check: the same drawn panel with the governance clause
        # stripped from every bound label. The run still states `lone_tail`'s own
        # guard, so the departure has to be attributed and is not.
        code, stderr, out = self.render_mandate(
            CROSSING_DECLARATION,
            CROSSING_ROWS,
            "M1cg2",
            "--run-values",
            json.dumps(CROSSING_GUARDS),
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        stripped = MANDATE.GOVERNANCE_CLAUSE_RE.sub("", svg)
        self.assertNotEqual(stripped, svg)
        problems = MANDATE.check_crossing_series_governed(
            "latency",
            "line",
            CROSSING_SERIES,
            [{"y": 250.0, "label": "M1 ceiling 250 ms"}],
            CROSSING_GUARDS,
            stripped,
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'lone_tail'", problems[0])
        self.assertIn("M1 ceiling 250 ms", problems[0])

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

    def test_a_series_guard_is_drawn_and_named_beside_the_ceiling(self):
        # green: the same panel with the ceiling at the mandate's own value. The
        # run's guard is drawn as its own line and labelled with the series it
        # governs, so the departure is attributed and the render is kept.
        code, stderr, out = self.render_mandate(
            SERIES_GUARD_DECLARATION,
            SERIES_GUARD_ROWS,
            "M4cg2",
            "--run-values",
            json.dumps(SERIES_GUARD_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M4-latency.svg").read_text(encoding="utf-8")
        labels = [declared for declared, _, _ in MANDATE.label_boxes(svg)]
        self.assertTrue(
            any("governs series hostile_p99" in label for label in labels), labels
        )

    # -- a line panel's axis is not set by one outlier -----------------------

    def test_one_outlier_no_longer_sets_the_latency_axis(self):
        # green: the 1400 ms sample is clipped at the 250 ms the panel is read
        # against, and the axis is the anchor's -- so the bodies it compresses
        # are resolvable and the outlier is still drawn, at the frame's top.
        code, stderr, out = self.render_mandate(
            CLIP_DECLARATION, CLIP_ROWS, "M1clip"
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        parsed = MANDATE.read_panel_summary(svg)
        self.assertEqual(
            parsed["y_clip"], {"value": 250.0, "clipped": 1, "max": 1400.0}
        )
        self.assertIn('class="y-clip"', svg)
        self.assertIn(
            "y axis clipped at 250: 1 of 601 value(s) up to 1400 drawn at the "
            "top edge",
            svg,
        )
        self.assertLess(parsed["axis"][1], 1400.0)
        # the body the outlier used to compress is now drawn at a real scale:
        # the clean arm's own range is at least twice the height it had on the
        # axis the lone-tail peak set.
        bounds = [{"y": 250.0, "label": "M1 ceiling 250 ms"}]
        plot_height = MANDATE.REPORT.line_plot_height(3, 0)
        unclipped = MANDATE.REPORT.extent_including_bounds(
            MANDATE.REPORT.finite_extent(CLIP_SERIES), [(250.0, "")]
        )
        clipped = MANDATE.line_axis_extent(
            CLIP_SERIES, bounds, None, plot_height
        )
        clean = [value for _, value in CLIP_SERIES[0][1]]
        spread = max(clean) - min(clean)
        before = spread / (unclipped[1] - unclipped[0]) * plot_height
        after = spread / (clipped[1] - clipped[0]) * plot_height
        self.assertGreater(after, 2 * before, (before, after))

    def test_an_axis_that_still_reaches_the_outlier_is_refused(self):
        # red, at the check: the same panel measured against the axis the old
        # policy drew -- the data's own extent, outlier and all.
        code, stderr, out = self.render_mandate(
            CLIP_DECLARATION, CLIP_ROWS, "M1clip2"
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        bounds = [{"y": 250.0, "label": "M1 ceiling 250 ms"}]
        unclipped = MANDATE.REPORT.extent_including_bounds(
            MANDATE.REPORT.finite_extent(CLIP_SERIES), [(250.0, "")]
        )
        problems = MANDATE.check_line_axis_clip_stated(
            "latency",
            "line",
            CLIP_SERIES,
            bounds,
            unclipped,
            svg,
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("one outlier set the axis", problems[0])
        self.assertIn("1400", problems[0])

    def test_a_clip_the_panel_does_not_state_is_refused(self):
        # red: the clipped axis drawn without the sentence that says so.
        code, stderr, out = self.render_mandate(
            CLIP_DECLARATION, CLIP_ROWS, "M1clip3"
        )
        self.assertEqual(code, 0, stderr)
        svg = (out / "M1-latency.svg").read_text(encoding="utf-8")
        bounds = [{"y": 250.0, "label": "M1 ceiling 250 ms"}]
        clipped = MANDATE.line_axis_extent(
            CLIP_SERIES, bounds, None, MANDATE.REPORT.line_plot_height(3, 0)
        )
        self.assertEqual(
            MANDATE.check_line_axis_clip_stated(
                "latency", "line", CLIP_SERIES, bounds, clipped, svg
            ),
            [],
        )
        silent = MANDATE.PANEL_NOTE_RE.sub("", svg)
        self.assertNotEqual(silent, svg)
        problems = MANDATE.check_line_axis_clip_stated(
            "latency", "line", CLIP_SERIES, bounds, clipped, silent
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not state it", problems[0])

    def test_a_tail_inside_the_axis_owes_no_clip(self):
        # The predicate's edge: a peak a multiple of the read-at value but a
        # *minority* of the samples is an outlier and clips; a peak the axis can
        # hold is the data's own extent and clips nothing.
        inside = [("arm", [(float(i), 200.0) for i in range(20)])]
        self.assertEqual(
            MANDATE.line_clip_owed(inside, [{"y": 250.0, "label": "ceil"}]),
            None,
        )
        # a peak the axis must hold because it is not a minority
        majority = [
            (
                "arm",
                [(float(i), 400.0 + i) for i in range(100)]
                + [(float(i), 100.0) for i in range(100)],
            )
        ]
        self.assertEqual(
            MANDATE.line_clip_owed(
                majority, [{"y": 250.0, "label": "ceil"}]
            ),
            None,
        )
        self.assertEqual(
            MANDATE.line_clip_owed(
                CLIP_SERIES, [{"y": 250.0, "label": "ceil"}]
            ),
            250.0,
        )

    def test_a_panel_labelled_with_a_sibling_s_unit_is_refused(self):
        series = [("fraction", [(1.0, 0.958217), (2.0, 0.958271)])]
        # red: the label the preserved run drew on the fraction panel -- the
        # goodput panel's unit, carried over the mandate's shared y_label
        problems = MANDATE.check_axis_label(
            "fraction", "MiB/s", series, declared=None, carried="MiB/s"
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("MiB/s", problems[0])
        # green: the label a single-series panel draws for itself
        self.assertEqual(
            MANDATE.check_axis_label(
                "fraction",
                "fraction of link rate",
                series,
                declared=None,
                carried="MiB/s",
            ),
            [],
        )
        # a panel that states its own label keeps its word, and a panel with
        # several series has no single quantity to name
        self.assertEqual(
            MANDATE.check_axis_label(
                "fraction", "MiB/s", series, declared="MiB/s", carried="MiB/s"
            ),
            [],
        )
        self.assertEqual(
            MANDATE.check_axis_label(
                "goodput",
                "MiB/s",
                [("delivered", []), ("shaper_forwarded", [])],
                declared=None,
                carried="MiB/s",
            ),
            [],
        )

    def test_an_x_axis_that_contradicts_the_run_s_categories_is_refused(self):
        run = {"reps": 3, "measured_s": 18.0}
        # red: M3 draws one bar per repetition at x=1..3 and labelled both its
        # panels `seed`
        problems = MANDATE.check_x_axis_label(
            "fraction", "seed", [1.0, 2.0, 3.0], run
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("seed", problems[0])
        self.assertIn("reps=3", problems[0])
        self.assertEqual(
            MANDATE.check_x_axis_label(
                "fraction", "rep (1..3)", [1.0, 2.0, 3.0], run
            ),
            [],
        )
        # green: a panel whose categories are not the run's repetitions, or a
        # run with no repetition count, keeps the declaration's label
        self.assertEqual(
            MANDATE.check_x_axis_label("goodput", "seed", [11.0, 21.0, 31.0], run),
            [],
        )
        self.assertEqual(
            MANDATE.check_x_axis_label("fraction", "seed", [1.0, 2.0], {"reps": 3}),
            [],
        )

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
    def test_a_tick_that_rounds_to_zero_is_not_drawn_negative(self):
        # M4-imbalance's band view starts a few ten-thousandths below zero, and
        # the tick there used to read `-0.00` under an all-positive panel.
        self.assertEqual(MANDATE.tick_label(-0.000344, 2), "0.00")
        self.assertEqual(MANDATE.tick_label(-1.5, 2), "-1.50")
        self.assertEqual(MANDATE.tick_label(0.25, 2), "0.25")
        declaration = {
            **FRACTION_DECLARATION,
            "panels": [
                {
                    **FRACTION_DECLARATION["panels"][0],
                    "series": [{"name": "delta"}],
                    "bounds": [{"y": 0.01, "label": "bound"}],
                }
            ],
        }
        rows = [
            ["panel", "series", "x", "y"],
            ["fraction", "delta", 1.0, -0.000344],
            ["fraction", "delta", 2.0, 0.000115],
            ["fraction", "delta", 3.0, 0.000115],
        ]
        code, stderr, out = self.render_mandate(declaration, rows, "MXzero")
        self.assertEqual(code, 0, stderr)
        document = (out / "M3-fraction.svg").read_text(encoding="utf-8")
        ticks = MANDATE.axis_tick_labels(document)
        self.assertEqual(len(ticks), 6, ticks)
        # No tick carries a sign it does not mean: a value that rounds to zero
        # is drawn without one (the negative tick here is a real -0.0003, so it
        # keeps its sign).
        self.assertEqual(
            [tick for tick in ticks if tick.startswith("-") and float(tick) == 0.0],
            [],
        )
        # ...and the six ticks are six values: the resolution comes from the step
        # between them, not from the sign of the axis' lower edge.
        self.assertEqual(len(set(ticks)), 6, ticks)
        self.assertEqual(MANDATE.check_tick_labels_distinct("fraction", document), [])

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

    def test_a_band_view_whose_ticks_repeat_is_refused(self):
        # The measured defect: `M4-imbalance` draws a 1 % departure bound over an
        # axis spanning 1.3 % of the share around zero, and two decimals printed
        # its six ticks as `0.01 0.01 0.01 0.00 0.00 0.00` -- an axis too coarse
        # for the departure the panel exists to show.
        series = [
            ("clean", [(1.0, 0.0001), (2.0, 0.0001), (3.0, 0.0001), (4.0, -0.0002)]),
            ("hostile", [(1.0, 0.0005), (2.0, 0.0015), (3.0, 0.0015), (4.0, -0.0027)]),
        ]
        bounds = [{"y": 0.01, "label": "fair-share bound 1.0%"}]
        low, high = MANDATE.bar_axis_extent(
            series, bounds, None, MANDATE.bar_plot_height(2)
        )
        span = high - low
        # The rule the fix replaced, stated as a vacuity: it keyed the decimals
        # off the sign of the lower edge, so a panel spanning 1.3 % of the unit
        # around zero got two of them and collided.
        old = [MANDATE.tick_label(low + span * tick / 5, 2) for tick in range(6)]
        self.assertLess(len(set(old)), 6, old)
        repeated = "".join(
            f'<text x="63" y="0" text-anchor="end">{tick}</text>' for tick in old
        )
        problems = MANDATE.check_tick_labels_distinct("imbalance", repeated)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("repeat", problems[0])
        self.assertIn("cannot carry the quantity", problems[0])
        # The fixed rule, and the panel it renders, are both readable.
        declaration = {
            "mandate": "M4",
            "title": "M4 interactive lane fairness",
            "x_label": "flow (1..4)",
            "y_label": "departure from the fair share",
            "panels": [
                {
                    "id": "imbalance",
                    "chart": "bar",
                    "series": [{"name": "clean"}, {"name": "hostile"}],
                    "bounds": bounds,
                }
            ],
        }
        rows = [["panel", "series", "x", "y"]] + [
            ["imbalance", name, x, y] for name, points in series for x, y in points
        ]
        code, stderr, out = self.render_mandate(declaration, rows, "M4imb")
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-imbalance.svg").read_text(encoding="utf-8")
        ticks = MANDATE.axis_tick_labels(document)
        self.assertEqual(len(set(ticks)), len(ticks), ticks)
        self.assertEqual(MANDATE.check_tick_labels_distinct("imbalance", document), [])

    # -- the honesty of a line series' geometry, and its stated readings -----

    def test_a_hole_in_the_sampling_is_drawn_as_a_gap_not_a_wall(self):
        # The series that caused the misreading: the recorded `lone_tail` tail,
        # which steps from 13.11 s (1.8 ms) to 15.76 s (2651.7 ms) -- a 2.65 s
        # period nobody observed. Drawn as one polyline it is a near-vertical
        # wall, and a reader took it for a climb the window's end truncated.
        declaration = {**LONE_TAIL_DECLARATION, "panels": [dict(LONE_TAIL_DECLARATION["panels"][0], bounds=[{"y": 250.0, "label": "M1 ceiling 250 ms", "series": "lone_tail"}])]}
        code, stderr, out = self.render_mandate(
            declaration, _latency_rows(REAL_LONE_TAIL_TAIL), "M1hole"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-latency.svg").read_text(encoding="utf-8")
        series = [("lone_tail", list(REAL_LONE_TAIL_TAIL))]
        self.assertEqual(MANDATE.check_gap_honesty("latency", series, document), [])
        # Every drawn sample is a dot, so where the samples *are* (and are not)
        # is on the panel rather than inferred from the line's steepness...
        self.assertEqual(
            document.count('<circle class="sample"'), len(REAL_LONE_TAIL_TAIL)
        )
        # ...and the line is two segments, not one drawn across the hole.
        colours = MANDATE.drawn_polylines(document)
        self.assertEqual(colours[MANDATE.REPORT.COLORS[0]], 2)
        # The pre-change drawing is refused, naming the hole it paints as a
        # climb: this is the red half of the vacuity pair.
        continuous = MANDATE.REPORT.svg_line_chart(
            "M1 [latency]", "elapsed time (s)", "latency (ms)", series
        )
        problems = MANDATE.check_gap_honesty("latency", series, continuous)
        self.assertEqual(len(problems), 2, problems)
        joined = "\n".join(problems)
        self.assertIn("2.65 s hole", joined)
        self.assertIn("near-vertical climb", joined)
        self.assertIn("sample marker(s)", joined)

    def test_a_censored_reading_cannot_be_left_off_the_panel(self):
        # A climb the window cut off -- the shape the eye cannot tell from a
        # peak that returned, and the shape the run's own detector classified.
        reading = {
            "lone_tail": {
                "verdict": "Censored",
                "rungs_at_edge": 1.0,
                "room": 1200.0,
            }
        }
        rows = _latency_rows(TRUNCATED_CLIMB)
        code, stderr, out = self.render_mandate(
            LONE_TAIL_DECLARATION,
            rows,
            "M1cens",
            "--run-censoring",
            json.dumps(reading),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-latency.svg").read_text(encoding="utf-8")
        series = [("lone_tail", list(TRUNCATED_CLIMB))]
        readings = MANDATE.panel_readings(series, reading)
        self.assertEqual(
            MANDATE.check_readings_stated("latency", series, readings, document), []
        )
        # The panel states the verdict, the room, and the fact that makes the
        # difference: nothing follows the maximum.
        self.assertIn("lone_tail: Censored", document)
        self.assertIn("room 1200 ms", document)
        self.assertIn("nothing after it", document)
        # The red half: the same panel drawn without the run's reading is
        # refused by name, verdict and all.
        silent = MANDATE.REPORT.svg_line_chart(
            "M1 [latency]",
            "elapsed time (s)",
            "latency (ms)",
            series,
        )
        problems = MANDATE.check_readings_stated("latency", series, readings, silent)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Censored", problems[0])
        self.assertIn("the opposite conclusion", problems[0])
        # And end to end: a reading about an arm no line panel draws cannot be
        # carried by any panel, so the whole render is refused rather than
        # silently dropping a machine verdict.
        stray = {"clean": {"verdict": "Clear", "rungs_at_edge": -1.0, "room": 2000.0}}
        code, stderr, _ = self.render_mandate(
            LONE_TAIL_DECLARATION,
            rows,
            "M1stray",
            "--run-censoring",
            json.dumps(stray),
        )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("about no series any line panel", stderr)

    def test_a_caption_whose_numbers_come_from_another_source_is_refused(self):
        # The defect this replaces: `check_readings_stated` reads the drawn
        # sentence back and requires it to be the one the formatter produced,
        # which is green on a caption whose *magnitude* was taken from
        # somewhere else -- the producer's own `[m1-censoring] max=` token is
        # the candidate this loop checked first -- because the formatter is the
        # only thing either side of that comparison ever consulted. The caption
        # is what the reader trusts instead of the pixels, so a caption that is
        # authoritative and wrong is worse than no caption.
        series = [("lone_tail", list(REAL_LONE_TAIL_TAIL))]
        detector = {"verdict": "Clear", "rungs_at_edge": -2.43, "room": 105000.0}
        drawn = MANDATE.arm_reading(
            "lone_tail", MANDATE.REPORT.decimate(series[0][1]), detector
        )
        markup = _reading_markup([drawn])
        self.assertEqual(
            MANDATE.check_readings_stated(
                "latency", series, [("lone_tail", drawn)], markup
            ),
            [],
        )
        self.assertEqual(MANDATE.check_reading_numbers("latency", series, markup), [])
        # The red half: the same sentence with the magnitude the instrument's
        # own row states (the detector's `max=`, which on the recorded run was
        # a *different* series' maximum). `check_readings_stated` is still
        # green on it -- that is the vacuity the new check closes -- and the
        # numbers check refuses by name, arm and both values.
        wrong = drawn.replace("peak 2652 ms", "peak 1074.1 ms")
        self.assertNotEqual(wrong, drawn)
        broken = _reading_markup([wrong])
        self.assertEqual(
            MANDATE.check_readings_stated(
                "latency", series, [("lone_tail", wrong)], broken
            ),
            [],
        )
        problems = MANDATE.check_reading_numbers("latency", series, broken)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'lone_tail'", problems[0])
        self.assertIn("1074.1", problems[0])
        self.assertIn("2651.69", problems[0])
        self.assertIn("worse than no caption", problems[0])
        # The other numbers are pinned too: a peak time, a last sample and a
        # hole count from a different series are each caught, and a caption
        # that states no maximum at all cannot be read back and is refused
        # rather than skipped.
        for broken_text, fragment in (
            (drawn.replace("at 15.76 s", "at 9.56 s"), "where its maximum is"),
            (drawn.replace("last 59.8 ms", "last 424.3 ms"), "its last sample"),
            (drawn.replace("1 sample gap(s)", "2 sample gap(s)"), "sample gap(s)"),
            (drawn.replace("peak 2652 ms at 15.76 s", "the series is quiet"), "states no maximum"),
        ):
            with self.subTest(caption=broken_text):
                rows = MANDATE.check_reading_numbers(
                    "latency", series, _reading_markup([broken_text])
                )
                self.assertTrue(rows, broken_text)
                self.assertIn(fragment, "\n".join(rows))

    def test_a_caption_taken_from_another_arm_is_refused(self):
        # The other shape the finding named: the arm may be selected
        # differently in the two paths, so one arm's sentence can be drawn
        # beside another arm's series. The band is split per arm by the arm's
        # own `<arm>: ` marker rather than by position, so a reading is matched
        # to the series it names -- and a sentence that names the wrong arm is
        # measured against the wrong points and refused.
        series = [
            ("first", [(float(index), 10.0 * index) for index in range(1, 8)]),
            ("second", [(float(index), 100.0 * index) for index in range(1, 8)]),
        ]
        first = MANDATE.arm_reading("first", series[0][1])
        second = MANDATE.arm_reading("second", series[1][1])
        # The run read one arm and not the other: both sentence shapes --
        # `<arm>: <verdict> - ...` and `<arm> - ...` -- are in the band, and
        # the split is by the arm's own marker rather than by draw position.
        verdicts = {"first": {"verdict": "Censored", "room": 40.0}}
        read = [
            ("first", MANDATE.arm_reading("first", series[0][1], verdicts["first"])),
            ("second", second),
        ]
        for sentences in ([first, second], [second, first], [text for _, text in read]):
            with self.subTest(band=[text.split(" ")[0] for text in sentences]):
                self.assertEqual(
                    MANDATE.check_reading_numbers(
                        "latency", series, _reading_markup(sentences)
                    ),
                    [],
                )
        mislabelled = _reading_markup([second.replace("second - ", "first - ", 1)])
        problems = MANDATE.check_reading_numbers("latency", series, mislabelled)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'first'", problems[0])
        self.assertIn("states its maximum as 700", problems[0])
        self.assertIn("is 70", problems[0])

    def test_a_render_whose_caption_states_another_series_maximum_is_refused(self):
        # End to end: a formatter that takes its magnitude from the detector's
        # own `max=` token instead of from the drawn points is refused by the
        # render, not merely by the predicate -- the artifact never reaches
        # disk carrying a number its own series contradicts.
        detector = {"verdict": "Clear", "rungs_at_edge": -2.43, "room": 105000.0}
        rows = _latency_rows(REAL_LONE_TAIL_TAIL)
        censoring = json.dumps({"lone_tail": {**detector, "max": 1074.1}})
        code, stderr, _ = self.render_mandate(
            LONE_TAIL_DECLARATION, rows, "M1lie", "--run-censoring", censoring
        )
        self.assertEqual(code, 0, stderr)
        honest = MANDATE.arm_reading

        def lying_formatter(arm, points, reading=None):
            text = honest(arm, points, reading)
            return MANDATE.re.sub(
                r"peak [-+0-9.eE]+ ms",
                f"peak {(reading or detector)['max']:.1f} ms",
                text,
            )

        with mock.patch.object(MANDATE, "arm_reading", lying_formatter):
            code, stderr, _ = self.render_mandate(
                LONE_TAIL_DECLARATION, rows, "M1lie", "--run-censoring", censoring
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("states its maximum as 1074.1", stderr)
        self.assertIn("does not measure", stderr)

    def test_a_share_panel_names_the_panel_that_carries_its_departure(self):
        # `M4-shares` draws a share against the fair share, and the mandate's
        # failure is the *departure* from it: a bound its own bars straddle is
        # a reference, not a line any of them can fail, so the frame itself has
        # no failure in it to draw. The panel therefore says what it is, names
        # the panel that carries the departure, and states the run's own worst
        # departure per arm and the bound it is read against -- all of it
        # derived from the companion panel's own drawn points.
        declaration = SHARES_IMBALANCE_DECLARATION
        panels = declaration["panels"]
        points = _points(SHARES_IMBALANCE_ROWS)
        code, stderr, out = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4depart"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        self.assertEqual(
            " ".join(MANDATE.drawn_notes(document)),
            "composition view - the departure is drawn on panel 'imbalance' "
            "(bound 1.0%): worst clean 0.04%, hostile 0.03%",
        )
        self.assertEqual(
            MANDATE.check_departure_view_stated(
                "shares", panels[0], panels, points, document
            ),
            [],
        )
        # The note is inside the plot and clear of the bound label, which is
        # where a note about the frame has to live to be read.
        self.assertEqual(MANDATE.check_note_fit("shares", document), [])
        self.assertEqual(MANDATE.check_label_fit("shares", document), [])
        self.assertEqual(MANDATE.check_label_overlap("shares", document), [])

    def test_a_share_panel_without_its_departure_statement_is_refused(self):
        # The vacuity of the departure-view check: the same declaration and the
        # same data, drawn without the note, must go red -- and end to end, a
        # render that suppresses the note must not write the panel at all.
        declaration = SHARES_IMBALANCE_DECLARATION
        panels = declaration["panels"]
        points = _points(SHARES_IMBALANCE_ROWS)
        bare = MANDATE.svg_bar_chart(
            "M4 [shares]",
            "flow (1..4)",
            "share of the lane's delivered bytes",
            [("clean", [(1.0, 0.250029)]), ("hostile", [(1.0, 0.250029)])],
            [{"y": 0.25, "label": "fair share 25.0%"}],
        )
        # The pre-change drawing: no note at all, and the check names the panel
        # whose departure it is silent about.
        self.assertEqual(MANDATE.drawn_notes(bare), [])
        problems = MANDATE.check_departure_view_stated(
            "shares", panels[0], panels, points, bare
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot carry the failure its mandate is read for", problems[0])
        self.assertIn("'imbalance'", problems[0])
        self.assertIn("no failure to draw", problems[0])
        # The other red half: a note whose numbers came from somewhere else is
        # not the note this panel's own data derives, so it is refused too.
        code, stderr, out = self.render_mandate(
            declaration, SHARES_IMBALANCE_ROWS, "M4depart2"
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M4-shares.svg").read_text(encoding="utf-8")
        lying = document.replace("clean 0.04%", "clean 4.00%")
        self.assertNotEqual(lying, document)
        problems = MANDATE.check_departure_view_stated(
            "shares", panels[0], panels, points, lying
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Expected on the panel", problems[0])
        # End to end: a drawing that drops the note does not write the panel,
        # rather than writing one that reads as "no departure".
        drawn_chart = MANDATE.svg_bar_chart

        def chart_without_its_note(*arguments, **keywords):
            return drawn_chart(*arguments, **{**keywords, "note": ""})

        with mock.patch.object(MANDATE, "svg_bar_chart", chart_without_its_note):
            code, stderr, _ = self.render_mandate(
                declaration, SHARES_IMBALANCE_ROWS, "M4depart3"
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("cannot carry the failure", stderr)

    def test_a_panel_whose_bound_a_bar_can_fail_owes_no_note(self):
        # The check is about a bound the bars *straddle*, not about every bar
        # panel: a floor is a line a bar can fail, so that panel can already
        # show its own failure and is left alone -- as is a share panel whose
        # mandate declares no panel carrying the departure for it to name.
        delivery = {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "clean"}],
            "bounds": [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}],
        }
        shares = {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}],
            "bounds": [{"y": 0.25, "label": "fair share 25.0%"}],
        }
        points = MANDATE.parse_points(
            [
                (2, "delivery", "clean", "1.0", "1.0"),
                (3, "shares", "clean", "1.0", "0.250029"),
                (4, "shares", "clean", "2.0", "0.249912"),
            ]
        )
        self.assertEqual(MANDATE.target_bounds(delivery, [("clean", [(1.0, 1.0)])]), [])
        self.assertEqual(
            len(
                MANDATE.target_bounds(
                    shares, [("clean", [(1.0, 0.250029), (2.0, 0.249912)])]
                )
            ),
            1,
        )
        for panel in (delivery, shares):
            with self.subTest(panel=panel["id"]):
                self.assertEqual(
                    MANDATE.departure_view_note(panel, [delivery, shares], points), ""
                )

    def test_a_bound_the_run_restates_is_drawn_per_arm_and_named(self):
        # The measured defect: `M2-delivery` drew the clean arm's `1.000` line
        # across all three arms, and the run guards the other two at `0.995`,
        # so a hostile bar at `0.996` crossed the drawn line while sitting
        # inside its own arm's floor -- a breach the verdict tolerates, and the
        # one difference between the arms the panel exists to compare. The run's
        # per-arm guards are what it is split against, drawn per arm.
        declaration = M2_DELIVERY_DECLARATION
        panels = declaration["panels"]
        points = _points(M2_DELIVERY_ROWS)
        series = MANDATE.panel_series(panels[0], points)
        bounds = MANDATE._bound_specs(panels[0])
        self.assertEqual(
            MANDATE.arm_bound_values(panels[0], series, bounds, M2_RUN_VALUES),
            {"clean": 1.0, "hostile": 0.995, "lone": 0.995},
        )
        plans = MANDATE.effective_bounds(panels[0], series, bounds, M2_RUN_VALUES)
        self.assertEqual(
            [(plan["y"], plan["arms"], plan["window"]) for plan in plans],
            [(1.0, ["clean"], [1.0]), (0.995, ["hostile", "lone"], [2.0, 3.0])],
        )
        code, stderr, out = self.render_mandate(
            declaration,
            M2_DELIVERY_ROWS,
            "M2arms",
            "--run-values",
            json.dumps(M2_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M2-delivery.svg").read_text(encoding="utf-8")
        self.assertEqual(document.count('class="bound"'), 2)
        self.assertEqual(
            [declared for declared, _, _ in MANDATE.label_boxes(document)],
            [
                "M2 delivery floor 1.000 [governs clean]",
                "run hostile_delivery_guard=0.995 [governs hostile lone]",
            ],
        )
        self.assertEqual(
            MANDATE.check_bound_arm_governance(
                "delivery", panels[0], series, bounds, M2_RUN_VALUES, document
            ),
            [],
        )
        # The two lines really are over different arms, not one line drawn
        # twice: the first ends where arm 1's band ends.
        spans = MANDATE.re.findall(
            r'class="bound" x1="([-0-9.]+)"[^>]*x2="([-0-9.]+)"', document
        )
        self.assertEqual(len(spans), 2, spans)
        # Two lines over two runs of arms, meeting at the boundary between
        # them, rather than one line drawn twice over the whole plot.
        self.assertEqual(spans[0][1], spans[1][0])
        self.assertLess(float(spans[0][1]), float(spans[1][1]))

    def test_a_bound_drawn_across_arms_with_different_floors_is_refused(self):
        # The vacuity of the per-arm check: the pre-change drawing -- one line
        # at the declared bound, its label naming no arm -- must go red, both
        # as a predicate and end to end.
        declaration = M2_DELIVERY_DECLARATION
        panels = declaration["panels"]
        points = _points(M2_DELIVERY_ROWS)
        series = MANDATE.panel_series(panels[0], points)
        bounds = MANDATE._bound_specs(panels[0])
        one_line = MANDATE.svg_bar_chart(
            "M2 [delivery]",
            "arm (1=clean 2=hostile 3=lone_tail)",
            "delivery (received / offered)",
            series,
            bounds,
            (0.979, 1.001),
            M2_RUN_VALUES,
        )
        problems = MANDATE.check_bound_arm_governance(
            "delivery", panels[0], series, bounds, M2_RUN_VALUES, one_line
        )
        self.assertEqual(len(problems), 2, problems)
        joined = "\n".join(problems)
        self.assertIn("would be the floor of neither", joined)
        self.assertIn("governs clean", joined)
        self.assertIn("governs hostile lone", joined)
        # End to end: a drawing that keeps the single declared line does not
        # write the panel at all.
        drawn_chart = MANDATE.svg_bar_chart

        def chart_with_one_bound(*arguments, **keywords):
            plan = dict(arguments[4][0])
            plan.pop("window", None)
            plan.pop("arms", None)
            return drawn_chart(*arguments[:4], [plan], *arguments[5:], **keywords)

        with mock.patch.object(MANDATE, "svg_bar_chart", chart_with_one_bound):
            code, stderr, _ = self.render_mandate(
                declaration,
                M2_DELIVERY_ROWS,
                "M2arms2",
                "--run-values",
                json.dumps(M2_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("would be the floor of neither", stderr)

    def test_a_run_that_states_a_per_arm_bound_owes_a_line_over_that_arm(self):
        # The other half of the vacuity: the split is offered only where the
        # run states a bound of its own for the panel's quantity. M4's own
        # per-flow delivery floor is the value the M2 declaration already draws,
        # and M4's run values bear on a different quantity, so the delivery
        # panel is not split -- a split offered anyway would be an invention
        # rather than a reading.
        delivery = {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "delivery"}],
            "bounds": [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        }
        series = [("delivery", [(1.0, 1.0), (2.0, 1.0), (3.0, 1.0)])]
        bounds = MANDATE._bound_specs(delivery)
        self.assertIsNone(MANDATE.arm_bound_values(delivery, series, bounds, None))
        self.assertIsNone(
            MANDATE.arm_bound_values(delivery, series, bounds, M4_DELIVERY_RUN_VALUES)
        )
        self.assertEqual(
            MANDATE.effective_bounds(delivery, series, bounds, M4_DELIVERY_RUN_VALUES),
            [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        )
        # The latency panel is the opposite case: it restates no bound, but the
        # run states a guard of its own for two of its three arms, and those
        # guards *are* those arms' bounds. Each is drawn over the arm it
        # governs, and the declaration's bound keeps the one arm no guard
        # claims -- which is what makes the lone-tail bar's 185.8 a value under
        # its own 400 ms guard rather than a breach of a 100 ms line drawn
        # across it.
        latency = {
            "id": "latency",
            "chart": "bar",
            "series": [{"name": "p99_ms"}],
            "bounds": [{"y": 100.0, "label": "M2 non-degrading p99 bound (ms)"}],
        }
        latency_series = [("p99_ms", [(1.0, 26.251), (2.0, 61.5), (3.0, 185.8015)])]
        latency_bounds = MANDATE._bound_specs(latency)
        self.assertEqual(
            MANDATE.arm_bound_values(
                latency, latency_series, latency_bounds, M2_LATENCY_RUN_VALUES
            ),
            {"clean": 100.0, "hostile": 200.0, "lone": 400.0},
        )
        self.assertEqual(
            [
                (plan["y"], plan["arms"], plan["window"], plan["label"])
                for plan in MANDATE.effective_bounds(
                    latency, latency_series, latency_bounds, M2_LATENCY_RUN_VALUES
                )
            ],
            [
                (100.0, ["clean"], [1.0], "M2 non-degrading p99 bound (ms)"),
                (200.0, ["hostile"], [2.0], "run hostile_p99_guard=200"),
                (400.0, ["lone"], [3.0], "run lone_p99_guard=400"),
            ],
        )

    def test_a_line_bound_names_the_guard_of_every_arm_it_crosses(self):
        # `M1-latency` draws one 250 ms ceiling across three arms and the run
        # asserts a different bound for each of them, so the line has to say
        # which arm it governs and what the others are read against. As drawn
        # before this, the `lone_tail` series crosses the ceiling at 1567.1 ms
        # and the panel states nothing that says 1567.1 ms is inside that arm's
        # own 3200 ms p99 guard: the reader's only mark is the mandate's, and
        # the mandate's bound is not what that arm is asserted against.
        panels = M1_ARMS_DECLARATION["panels"]
        points = _points(M1_ARMS_ROWS)
        series = MANDATE.panel_series(panels[0], points)
        self.assertEqual(
            MANDATE.arm_guard_tokens(series, M1_RUN_VALUES),
            [
                (
                    "hostile",
                    [
                        ("hostile_p99_guard", "p99", 900.0),
                        ("hostile_over250_guard", "over250", 8.0),
                    ],
                ),
                (
                    "lone_tail",
                    [
                        ("lone_p99_guard", "p99", 3200.0),
                        ("lone_p999_guard", "p999", 8000.0),
                        ("lone_over250_guard", "over250", 8.0),
                    ],
                ),
            ],
        )
        # The arm is read off the run's own key and the panel's own legend
        # (`lone` -> `lone_tail`), and a guard for an arm this panel does not
        # draw is left out rather than named against the wrong series.
        self.assertEqual(
            MANDATE.arm_guard_tokens([("clean", [])], M1_RUN_VALUES), []
        )
        code, stderr, out = self.render_mandate(
            M1_ARMS_DECLARATION,
            M1_ARMS_ROWS,
            "M1arms",
            "--run-values",
            json.dumps(M1_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-latency.svg").read_text(encoding="utf-8")
        # The label is long enough to wrap; `label_boxes` reports one box per
        # drawn line, with the undivided sentence in the first line's
        # `<title>`. The drawn ink has to spell that sentence out, not clip it.
        boxes = MANDATE.label_boxes(document)
        self.assertEqual(
            boxes[0][0],
            "M1 ceiling 250 ms [governs clean; hostile guards "
            "hostile_p99_guard=900, hostile_over250_guard=8%; lone_tail "
            "guards lone_p99_guard=3200, lone_p999_guard=8000, "
            "lone_over250_guard=8%]",
        )
        self.assertGreater(len(boxes), 1, "the long label is wrapped, not clipped")
        self.assertEqual(" ".join(line for _, line, _ in boxes), boxes[0][0])
        self.assertEqual(
            MANDATE.check_bound_arm_governance(
                "latency",
                panels[0],
                series,
                MANDATE._bound_specs(panels[0]),
                M1_RUN_VALUES,
                document,
            ),
            [],
        )

    def test_a_line_bound_that_names_no_arm_is_refused(self):
        # The vacuity of the arm-governance clause: the pre-change drawing --
        # one line at the declared ceiling, its label naming no arm and no
        # guard -- has to go red, both as a predicate and end to end.
        panels = M1_ARMS_DECLARATION["panels"]
        points = _points(M1_ARMS_ROWS)
        series = MANDATE.panel_series(panels[0], points)
        bounds = MANDATE._bound_specs(panels[0])
        one_line = MANDATE.REPORT.svg_line_chart(
            "M1 [latency]",
            "elapsed time (s)",
            "latency (ms)",
            series,
            REPORT_EXTENT := MANDATE.REPORT.extent_including_bounds(
                MANDATE.REPORT.finite_extent(series), [(250.0, "")]
            ),
            [(250.0, "M1 ceiling 250 ms")],
            walls=True,
            markers=True,
        )
        problems = MANDATE.check_bound_arm_governance(
            "latency", panels[0], series, bounds, M1_RUN_VALUES, one_line
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("would be the bound of none of them", problems[0])
        self.assertIn("governs clean", problems[0])
        self.assertIn("lone_p99_guard=3200", problems[0])
        # End to end: a drawing that keeps the bare declared label does not
        # write the panel at all. The clause is stripped from the artifact the
        # chart is handed rather than from `governed_label`, because the check's
        # own plan is built by that formatter: patching it would weaken both
        # sides at once and prove nothing about the drawing.
        drawn_line = MANDATE.REPORT.svg_line_chart

        def line_without_the_arm_guards(
            title, x_label, y_label, chart_series, extent, line_bounds, **keywords
        ):
            return drawn_line(
                title,
                x_label,
                y_label,
                chart_series,
                extent,
                [
                    (y, label.split(" [")[0] if label else label)
                    for y, label in line_bounds or []
                ],
                **keywords,
            )

        with mock.patch.object(MANDATE.REPORT, "svg_line_chart", line_without_the_arm_guards):
            code, stderr, _ = self.render_mandate(
                M1_ARMS_DECLARATION,
                M1_ARMS_ROWS,
                "M1arms2",
                "--run-values",
                json.dumps(M1_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("the bound of none of them", stderr)

    def test_the_value_at_a_bound_is_read_off_the_drawn_segment(self):
        # The value a panel states at its x bound has to be the one the drawn
        # polyline shows there: interpolated between the bracketing samples,
        # and clamped to the series' own end when the bound is past it.
        self.assertEqual(
            MANDATE.value_at([(0.0, 0.0), (100.0, 50.0), (200.0, 100.0)], 150.0),
            75.0,
        )
        self.assertEqual(MANDATE.value_at([(0.0, 0.0), (100.0, 100.0)], 250.0), 100.0)
        self.assertEqual(MANDATE.value_at([], 250.0), None)
        self.assertEqual(MANDATE.panel_unit("percentile (%)"), "%")
        self.assertEqual(MANDATE.panel_unit("latency (ms)"), "ms")
        self.assertEqual(MANDATE.panel_unit("share"), "")

    def test_a_cdf_carries_the_ceiling_and_the_value_it_is_read_at(self):
        # `M1-cdf` draws the latency distribution and no bound of its own; the
        # mandate's ceiling is a value on the axis its *x* carries, so the
        # panel can draw the failure its mandate is read for -- the curve not
        # reaching the top by the ceiling -- and has to state, at that x, what
        # each curve reads. The four readings below are the run's own drawn
        # `M1.csv` points: at 250 ms the clean curve has already reached 100 %,
        # `hostile` reads 99.4 % and `lone_tail` 99.06 %.
        panels = M1_ARMS_DECLARATION["panels"]
        points = _points(M1_ARMS_ROWS)
        code, stderr, out = self.render_mandate(
            M1_ARMS_DECLARATION,
            M1_ARMS_ROWS,
            "M1ceil",
            "--run-values",
            json.dumps(M1_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        cdf = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertEqual(len(MANDATE.re.findall(r'class="x-bound"', cdf)), 1)
        self.assertEqual(
            [declared for declared, _, _ in MANDATE.label_boxes(cdf)],
            [
                "M1 ceiling 250 ms [at 250 ms: clean 100%, hostile 99.4%, "
                "lone_tail 99.06%]"
            ],
        )
        # The frame now carries the failure, so the bare pointer to the sibling
        # panel is gone: the panel that can draw the failure does not name the
        # one that used to.
        self.assertEqual(MANDATE.drawn_notes(cdf), [])
        self.assertEqual(
            MANDATE.check_x_bound_drawn(
                "cdf",
                panels[1],
                panels,
                points,
                "elapsed time (s)",
                "latency (ms)",
                M1_RUN_VALUES,
                cdf,
            ),
            [],
        )
        # End to end: a chart that drops the mark and the sentence does not
        # write the panel at all.
        drawn_cdf = MANDATE.REPORT.svg_cdf_chart

        def cdf_without_its_ceiling(*arguments, **keywords):
            return drawn_cdf(*arguments, **{**keywords, "x_bounds": None})

        with mock.patch.object(MANDATE.REPORT, "svg_cdf_chart", cdf_without_its_ceiling):
            code, stderr, _ = self.render_mandate(
                M1_ARMS_DECLARATION,
                M1_ARMS_ROWS,
                "M1ceil2",
                "--run-values",
                json.dumps(M1_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("has to say what the bound reads", stderr)

    def test_a_cdf_ceiling_value_from_another_series_is_refused(self):
        # The other half of the vacuity, and the one `check_reading_numbers`
        # exists for: a sentence that reached the panel is not a sentence that
        # is true of it. This drawer states the value it reads one unit high,
        # so the numbers on the artifact are no longer the numbers the panel's
        # own drawn points measure, and the render must be refused.
        real = MANDATE.x_bound_label

        def one_unit_high(bound, series, x_unit, y_unit, drawn_range=None):
            return real(
                bound,
                [(name, [(x, value + 1.0) for x, value in points]) for name, points in series],
                x_unit,
                y_unit,
                drawn_range,
            )

        with mock.patch.object(MANDATE, "x_bound_label", one_unit_high):
            code, stderr, _ = self.render_mandate(
                M1_ARMS_DECLARATION,
                M1_ARMS_ROWS,
                "M1ceil3",
                "--run-values",
                json.dumps(M1_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("which the series it is drawn from does not measure", stderr)

    def test_a_cdf_axis_keeps_its_reference_arm_legible(self):
        # The measured defect: a latency CDF is read for where its *reference*
        # arm's body and tail sit, and a linear axis out to the worst arm's
        # tail paints that arm as a sliver at the left edge. On this fixture's
        # numbers the `clean` curve ends at 107.674 ms on an axis running to
        # 1567.11 ms: 6.9 % of the width. The axis goes logarithmic, and the
        # share is measured back off the drawn artifact -- the reference arm's
        # own largest sample against the axis the panel drew.
        declaration = M1_ARMS_DECLARATION
        panel = declaration["panels"][1]
        series = MANDATE.panel_series(panel, _points(M1_ARMS_ROWS))
        reference = MANDATE.reference_arm_names(series, M1_RUN_VALUES)
        self.assertEqual(reference, ["clean"])
        self.assertEqual(MANDATE.cdf_x_scale(series, reference), "log")
        linear = MANDATE.reference_reach_share(series, reference, "linear")
        self.assertAlmostEqual(linear[0], 0.0687, places=4)
        code, stderr, out = self.render_mandate(
            declaration,
            M1_ARMS_ROWS,
            "M1reach",
            "--run-values",
            json.dumps(M1_RUN_VALUES),
        )
        self.assertEqual(code, 0, stderr)
        document = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        scale = MANDATE.drawn_x_scale(document)
        self.assertEqual(scale, "log", "the axis the ticks put the panel on")
        drawn = MANDATE.reference_reach_share(series, reference, scale)
        self.assertGreaterEqual(drawn[0], MANDATE.MIN_REFERENCE_REACH_SHARE)
        self.assertEqual(
            MANDATE.check_cdf_reference_reach("cdf", panel, series, reference, document),
            [],
        )
        # And the ink agrees with the measurement: the `clean` curve's own
        # left-most and right-most points are both past the axis' halfway mark.
        plot = MANDATE.panel_plot_rect("cdf", document)
        clean = MANDATE.re.findall(
            r'<polyline points="([^"]*)" fill="none" stroke="#2563eb"', document
        )
        self.assertEqual(len(clean), 1, clean)
        xs = [float(point.split(",")[0]) for point in clean[0].split()]
        span = plot[2] - plot[0]
        self.assertGreaterEqual((min(xs) - plot[0]) / span, MANDATE.MIN_REFERENCE_REACH_SHARE)
        self.assertGreaterEqual((max(xs) - plot[0]) / span, MANDATE.MIN_REFERENCE_REACH_SHARE)

    def test_a_squeezed_cdf_reference_arm_is_refused_unless_the_panel_says_so(self):
        # The vacuity, both halves, on the same panel and the same data: drawn
        # on a linear axis the panel is refused with the measured share, and the
        # same squeezed axis *stating* the share is accepted -- the escape
        # `AGENTS.md` allows a frame that cannot show what it owes. The log
        # axis is the third reading, and is the one the real panel takes.
        panel = M1_ARMS_DECLARATION["panels"][1]
        series = MANDATE.panel_series(panel, _points(M1_ARMS_ROWS))
        reference = MANDATE.reference_arm_names(series, M1_RUN_VALUES)
        drawn = [
            (name, MANDATE.REPORT.decimate(points)) for name, points in series
        ]
        for scale, expected in (("linear", 1), ("log", 0)):
            with self.subTest(scale=scale):
                markup = MANDATE.REPORT.svg_cdf_chart(
                    "M1 [cdf]",
                    "latency (ms)",
                    "percentile (%)",
                    drawn,
                    x_scale=scale,
                )
                self.assertEqual(MANDATE.drawn_x_scale(markup), scale)
                problems = MANDATE.check_cdf_reference_reach(
                    "cdf", panel, series, reference, markup
                )
                self.assertEqual(len(problems), expected, problems)
                if problems:
                    self.assertIn("6.9% of the width", problems[0])
                    self.assertIn("clean", problems[0])
        # The statement the renderer owes when even the drawn scale squeezes.
        note = MANDATE.cdf_scale_note(series, reference, "linear")
        self.assertIn("7% of the width", note)
        self.assertIn("clean", note)
        stated = MANDATE.REPORT.svg_cdf_chart(
            "M1 [cdf]",
            "latency (ms)",
            "percentile (%)",
            drawn,
            x_scale="linear",
            note=note,
        )
        # The note is drawn wrapped, and the wrap only ever breaks at spaces,
        # so its lines re-join to the sentence the renderer owes.
        self.assertEqual(" ".join(MANDATE.drawn_notes(stated)), note)
        self.assertEqual(
            MANDATE.check_cdf_reference_reach("cdf", panel, series, reference, stated),
            [],
        )

    def test_a_cdf_panel_that_drops_the_scale_and_the_sentence_is_refused(self):
        # End to end: a renderer that neither puts the axis on the scale that
        # keeps the reference arm legible nor says that it cannot is refused,
        # rather than writing a panel whose own subject is a sliver.
        with mock.patch.object(
            MANDATE, "cdf_x_scale", lambda *a, **k: "linear"
        ), mock.patch.object(MANDATE, "cdf_scale_note", lambda *a, **k: ""):
            code, stderr, _ = self.render_mandate(
                M1_ARMS_DECLARATION,
                M1_ARMS_ROWS,
                "M1flat",
                "--run-values",
                json.dumps(M1_RUN_VALUES),
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("panel 'cdf'", stderr)
        self.assertIn("6.9% of the width", stderr)
        self.assertIn("sliver", stderr)

    def test_a_ceiling_beyond_the_drawn_x_range_is_stated_not_drawn(self):
        # A run whose samples all sit under the ceiling cannot draw the mark:
        # there is no pixel for a value past the axis. It still owes the
        # reading, and it owes saying that the value is outside the drawn
        # range, so the clamp the value is read with cannot pass for a
        # measurement.
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
        self.assertEqual(len(MANDATE.re.findall(r'class="x-bound"', cdf)), 0)
        self.assertEqual(
            [declared for declared, _, _ in MANDATE.label_boxes(cdf)],
            [
                "M1 ceiling 250 ms [at 250 ms: clean 100%, hostile 100%] "
                "(x beyond this panel's drawn range)"
            ],
        )
        # The vacuity: drop the clause that says the value is outside the
        # drawn range and the render is refused -- the clamp then reads as a
        # measurement.
        real = MANDATE.x_bound_label

        def without_the_caveat(bound, series, x_unit, y_unit, drawn_range=None):
            return real(bound, series, x_unit, y_unit, None).replace(
                " (x beyond this panel's drawn range)", ""
            )

        with mock.patch.object(MANDATE, "x_bound_label", without_the_caveat):
            code, stderr, _ = self.render_mandate(
                declaration, rows, "M1under2", "--run-values", json.dumps({"ceiling": 250.0})
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("outside this panel's drawn x range", stderr)

    def test_a_panel_with_no_bound_says_where_the_bound_is_drawn(self):
        # `M1-cdf` draws the latency distribution and no line at all: the
        # mandate's ceiling is on the sibling latency panel, so the CDF's
        # reader has no mark to read the distribution against. A panel whose
        # frame cannot carry the failure its mandate is read for says where
        # that failure is drawn -- the same debt a share panel owes for a
        # departure it cannot contain.
        panels = HEALTHY_DECLARATION["panels"]
        points = _points(HEALTHY_ROWS)
        self.assertEqual(
            MANDATE.bound_reference_note(panels[1], panels, points),
            "composition view - this panel draws the quantity; the mandate's "
            "bound 'M1 ceiling 250 ms' is drawn on panel 'latency'",
        )
        code, stderr, out = self.render_mandate(HEALTHY_DECLARATION, HEALTHY_ROWS, "M1cdf")
        self.assertEqual(code, 0, stderr)
        for name in ("M1-latency", "M1-cdf"):
            with self.subTest(panel=name):
                document = (out / f"{name}.svg").read_text(encoding="utf-8")
                self.assertEqual(MANDATE.check_note_fit(name.split("-")[1], document), [])
        cdf = (out / "M1-cdf.svg").read_text(encoding="utf-8")
        self.assertEqual(
            MANDATE.drawn_notes(cdf),
            [
                "composition view - this panel draws the quantity; the "
                "mandate's bound 'M1 ceiling 250 ms' is drawn on panel 'latency'"
            ],
        )
        # The latency panel draws its own bound, so it owes no note.
        self.assertEqual(
            MANDATE.drawn_notes((out / "M1-latency.svg").read_text(encoding="utf-8")),
            [],
        )
        # The red half: the pre-change drawing, with no note, is refused.
        bare = MANDATE.REPORT.svg_cdf_chart(
            "M1 [cdf]", "latency (ms)", "percentile (%)", [("cdf", [(12.5, 50.0)])]
        )
        self.assertEqual(MANDATE.drawn_notes(bare), [])
        problems = MANDATE.check_departure_view_stated(
            "cdf", panels[1], panels, points, bare
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("draws no bound at all", problems[0])
        self.assertIn("'latency'", problems[0])
        # End to end: a chart that drops the note does not write the panel.
        drawn_cdf = MANDATE.REPORT.svg_cdf_chart

        def cdf_without_its_note(*arguments, **keywords):
            return drawn_cdf(*arguments, **{**keywords, "note": ""})

        with mock.patch.object(MANDATE.REPORT, "svg_cdf_chart", cdf_without_its_note):
            code, stderr, _ = self.render_mandate(
                HEALTHY_DECLARATION, HEALTHY_ROWS, "M1cdf2"
            )
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("cannot carry the failure", stderr)

    def test_a_reading_band_that_eats_the_plot_is_refused(self):
        # The band and the shape it explains share one canvas, so the band may
        # not eat the shape: three arms of readings leave most of the plot,
        # and a band that leaves too little is refused rather than drawn.
        self.assertEqual(
            MANDATE.check_reading_band("latency", MANDATE.REPORT.line_plot_height(3, 6)),
            [],
        )
        problems = MANDATE.check_reading_band(
            "latency", MANDATE.REPORT.line_plot_height(3, 20)
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("shape its readings are about", problems[0])


if __name__ == "__main__":
    unittest.main()
