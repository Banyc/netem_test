#!/usr/bin/env python3

"""Exercise `netem-tools mandate-check` as a black box.

The runner was `tools/mandate_check.py`, which this suite imported in-process;
it is now the `mandate-check` subcommand of the `netem-tools` binary, so this
suite drives the *command*: the fake cargo below supplies a producer's evidence
and output stream from a JSON plan, and every assertion reads the command's
stdout, stderr, exit status and the `mandate-check.json` it writes. What cannot
be driven from a declaration and a plan — the parsers, the timing derivation,
the declaration validation, the panel verifier — is unit-tested where it lives,
in `netem-test/src/tools/mandate_check/`, and each such case names its Rust
test in the migration table in `tools/PERF_INFRA.md`.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
WORKSPACE = TOOLS.parent

# The runner's own constants, restated here because the suite drives the built
# binary rather than importing a module: these are the values the contract
# names, and a change to one is a change the assertions below have to follow.
REPORT_NAME = "mandate-check.json"
REPORT_SCHEMA = "mandate-check/10"
LOG_NAME = "mandate-smoke.log"
PLOTS_DIRNAME = "plots"
ARMS_DECLARATION_NAME = "mandate-arms.json"
PRODUCERS_DECLARATION_NAME = "mandate-producers.json"
ARMS_DECLARATION_SCHEMA = "mandate-arms/1"
PRODUCERS_DECLARATION_SCHEMA = "mandate-producers/1"
PRIMARY_PRODUCER = "rtp_mux"
MANDATE_IDS = ("M1", "M2", "M3", "M4")
TIMING_ARGS = ("-Z", "unstable-options", "--report-time")
TIMING_ENV = "RUSTC_BOOTSTRAP"
TIMING_ENV_VALUE = "1"
DURATION_SOURCE_STREAM_BRACKET = "stream-bracket-of-mandate-lines"
DURATION_SOURCE_HARNESS = "libtest-report-time"
EXIT_OK = 0
EXIT_EVIDENCE_FAILURE = 2
EXIT_MANDATE_FAILURE = 3


def binary():
    """The built `netem-tools`, wherever the build put it.

    The runner has no Python implementation any more, so a missing binary is a
    failure and not a skip: a suite that cannot reach the command has checked
    nothing.
    """
    candidates = (
        WORKSPACE / "target" / "release" / "netem-tools",
        WORKSPACE / "target" / "debug" / "netem-tools",
        WORKSPACE / "netem-test" / "target" / "release" / "netem-tools",
        WORKSPACE / "netem-test" / "target" / "debug" / "netem-tools",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "netem-tools is not built; build it with "
        "`cargo build --release -p netem-test --features cli --bin netem-tools` "
        "before running this suite"
    )


BINARY = binary()


def perf_history_binary():
    """The built `perf-history`, which the runner's history step invokes."""
    candidates = (
        WORKSPACE / "target" / "release" / "perf-history",
        WORKSPACE / "target" / "debug" / "perf-history",
        WORKSPACE / "netem-test" / "target" / "release" / "perf-history",
        WORKSPACE / "netem-test" / "target" / "debug" / "perf-history",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "perf-history is not built; build it with "
        "`cargo build --release -p netem-test --features cli --bin perf-history` "
        "before running this suite"
    )

# A stand-in cargo. Everything it does comes from a JSON plan, so the tool can
# be exercised end to end without a Rust build: it records the argv, cwd and
# the two contract environment variables it was handed, optionally sleeps (to
# reach the timeout path), prints the plan's stdout/stderr, writes each
# mandate's declaration and data CSV into $MANDATE_CHECK_DIR, and exits with
# the plan's status.
FAKE_CARGO = '''#!/usr/bin/env python3
"""A stand-in cargo that produces the smoke set's evidence from a JSON plan."""

import json
import os
import pathlib
import sys
import time

NEWLINE = chr(10)


def package_of(argv):
    for index, token in enumerate(argv):
        if token in ("-p", "--package") and index + 1 < len(argv):
            return argv[index + 1]
    return None


def main():
    plan_path = os.environ.get("FAKE_CARGO_PLAN")
    if not plan_path:
        print("fake cargo: FAKE_CARGO_PLAN is not set", file=sys.stderr)
        return 97
    plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    argv = sys.argv[1:]
    package = package_of(argv)
    # A plan may carry one sub-plan per package so a two-producer run can be
    # exercised end to end; without one the whole plan is the response, which
    # is what every one-producer test uses.
    plan = (plan.get("by_package") or {}).get(package) or plan
    out = os.environ.get("MANDATE_CHECK_DIR")
    if out is None:
        print("fake cargo: MANDATE_CHECK_DIR is not set", file=sys.stderr)
        return 98
    # One JSON line per invocation: a two-producer run invokes this twice, and
    # a reader that wants the first may have it.
    with open(os.environ["FAKE_CARGO_RECORD"], "a", encoding="utf-8") as record:
        record.write(
            json.dumps(
                {
                    "argv": argv,
                    "package": package,
                    "cwd": os.getcwd(),
                    "mandate_check_dir": out,
                    "quick": os.environ.get("MANDATE_SMOKE_QUICK"),
                    # The runner sets this so the stable-pinned toolchain's
                    # libtest accepts the `-Z unstable-options` it times with.
                    "rustc_bootstrap": os.environ.get("RUSTC_BOOTSTRAP"),
                }
            )
            + NEWLINE
        )
    if plan.get("sleep"):
        time.sleep(plan["sleep"])
    for line in plan.get("stdout") or []:
        # Flush per line: the real smoke set streams its output, and the
        # per-test timing is an observation of that stream.
        print(line, flush=True)
        if plan.get("line_sleep"):
            time.sleep(plan["line_sleep"])
    for line in plan.get("stderr") or []:
        print(line, file=sys.stderr, flush=True)
    for mandate, spec in (plan.get("mandates") or {}).items():
        if spec.get("json") is not None:
            pathlib.Path(out, mandate + ".json").write_text(
                json.dumps(spec["json"]), encoding="utf-8"
            )
        csv = spec.get("csv")
        if csv is not None:
            if isinstance(csv, str):
                text = csv
            else:
                text = NEWLINE.join(",".join(str(cell) for cell in row) for row in csv)
                if text:
                    text += NEWLINE
            pathlib.Path(out, mandate + ".csv").write_text(text, encoding="utf-8")
    return plan.get("exit", 0)


if __name__ == "__main__":
    raise SystemExit(main())
'''

M1_DECLARATION = {
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

M1_ROWS = [
    ["panel", "series", "x", "y"],
    ["latency", "impaired", 0.0, 12.5],
    ["latency", "impaired", 1.0, 31.5],
    ["latency", "impaired", 2.0, 88.25],
    ["cdf", "impaired", 12.5, 33.3],
    ["cdf", "impaired", 31.5, 66.7],
    ["cdf", "impaired", 88.25, 100.0],
]

M2_DECLARATION = {
    "mandate": "M2",
    "title": "M2 interactive delivery and latency under a known offer "
    "(1=clean 2=hostile 3=lone_tail)",
    "x_label": "arm (1=clean 2=hostile 3=lone_tail)",
    "y_label": "value",
    "panels": [
        {
            "id": "delivery",
            "chart": "bar",
            "series": [{"name": "delivery"}],
            "bounds": [{"y": 1.0, "label": "M2 delivery floor 1.000"}],
        },
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
        },
    ],
}

M2_ROWS = [
    ["panel", "series", "x", "y"],
    ["delivery", "delivery", 1.0, 1.0],
    ["latency", "p99_ms", 1.0, 26.251],
    ["delivery", "delivery", 2.0, 1.0],
    ["latency", "p99_ms", 2.0, 134.232],
    ["delivery", "delivery", 3.0, 1.0],
    ["latency", "p99_ms", 3.0, 185.8015],
]

M3_DECLARATION = {
    "mandate": "M3",
    "title": "M3 bulk goodput",
    "x_label": "seed",
    "y_label": "goodput (MiB/s)",
    "panels": [
        {
            "id": "goodput",
            "chart": "bar",
            "series": [{"name": "bulk"}],
            "bounds": [{"y": 0.35, "label": "M3 floor 0.35x link rate"}],
        }
    ],
}

M3_ROWS = [
    ["panel", "series", "x", "y"],
    ["goodput", "bulk", 11.0, 0.52],
    ["goodput", "bulk", 21.0, 0.61],
    ["goodput", "bulk", 31.0, 0.47],
]

# M4 is the interactive lane's split across several flows. The fixture mirrors
# the real declaration's four panels and its clean/hostile arms, at four flows.
M4_DECLARATION = {
    "mandate": "M4",
    "title": "M4 interactive lane fairness: 4 flows on one interactive lane",
    "x_label": "flow (1..4)",
    "y_label": "share of the lane's delivered bytes",
    "panels": [
        {
            "id": "shares",
            "chart": "bar",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.25, "label": "fair share 25.0%"}],
        },
        {
            "id": "imbalance",
            "chart": "bar",
            "y_label": "departure from the fair share",
            "x_label": "flow (1..4)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.01, "label": "fair-share bound 1.0%"}],
        },
        {
            "id": "delivery",
            "chart": "bar",
            "y_label": "delivery (received / offered)",
            "series": [{"name": "clean"}, {"name": "hostile"}],
            "bounds": [{"y": 0.995, "label": "M4 per-flow delivery floor 0.995"}],
        },
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
        },
    ],
}


def _m4_rows():
    rows = [["panel", "series", "x", "y"]]
    per_arm = {
        "clean": {
            "shares": [0.251, 0.249, 0.250, 0.250],
            "imbalance": [0.004, -0.004, 0.002, -0.002],
            "delivery": [1.0, 1.0, 1.0, 1.0],
        },
        "hostile": {
            "shares": [0.248, 0.252, 0.251, 0.249],
            "imbalance": [-0.008, 0.008, 0.004, -0.004],
            "delivery": [0.998, 0.999, 0.998, 0.999],
        },
    }
    latency = {
        "clean_p50": [22.0, 23.5, 21.8, 24.1],
        "clean_p99": [118.4, 121.0, 116.7, 119.9],
        "hostile_p50": [96.0, 102.5, 88.4, 110.2],
        "hostile_p99": [402.0, 388.7, 431.5, 399.2],
    }
    for arm, panel_values in per_arm.items():
        for panel, values in panel_values.items():
            for flow, value in enumerate(values, start=1):
                rows.append([panel, arm, flow, value])
    for series, values in latency.items():
        for flow, value in enumerate(values, start=1):
            rows.append(["latency", series, flow, value])
    return rows


M4_ROWS = _m4_rows()

PASS_LINES = [
    "running 4 tests",
    # The smoke set's per-arm lines, in the producer's own padded shape, before
    # that arm's mandate's MANDATE line — the ordering the runner attributes by.
    "[mandate-smoke clean    ] sent=  800 recv=  800 delivery=1.000 p50=   25.3 "
    "p90=   43.0 p99=   89.0 p999=   97.9 max=   102.5 over250=   0 wire=     42300B "
    "bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke hostile  ] sent=  800 recv=  800 delivery=1.000 p50=   39.5 "
    "p90=  150.6 p99=  245.1 p999=  283.6 max=   289.9 over250=  21 wire=     42300B "
    "bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke lone_tail] sent=  240 recv=  240 delivery=1.000 p50=    0.3 "
    "p90=   69.6 p99=  174.7 p999=  681.9 max=  2693.3 over250=   4 wire=    120000B "
    "bulk_sink=         0B bulk_wire=         0B wall=15.3s window=15s",
    # The producer's own censoring reading for the M1 series, which is what the
    # latency panel states per arm: the verdict no pixel carries.
    "[m1-censoring] arm=impaired samples=3 last=88.2 rungs_at_edge=-0.50 "
    "rise_run=1 edge_gap_ms=1000.0 screen=flat verdict=Clear max=88.2 "
    "burst=1.0 rungs=0 step=300 rtt=50 required=50.0 room=2000.0 "
    "window_holds=true lone_tail=false datagrams=4320",
    "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0",
    "test m1_interactive_tail_latency ... ok <86.163s>",
    "[mandate-smoke clean    ] sent=  800 recv=  800 delivery=1.000 p50=   25.3 "
    "p90=   43.0 p99=   89.0 p999=   97.9 max=   102.5 over250=   0 wire=     42300B "
    "bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke hostile  ] sent=  800 recv=  800 delivery=1.000 p50=   39.5 "
    "p90=  150.6 p99=  245.1 p999=  283.6 max=   289.9 over250=  21 wire=     42300B "
    "bulk_sink=   8123456B bulk_wire=   9123456B wall=12.3s window=12s",
    "[mandate-smoke lone_tail] sent=  240 recv=  240 delivery=1.000 p50=    0.3 "
    "p90=   69.6 p99=  174.7 p999=  681.9 max=  2693.3 over250=   4 wire=    120000B "
    "bulk_sink=         0B bulk_wire=         0B wall=15.3s window=15s",
    "MANDATE M2 PASS clean_offer_msgs=2398 clean_offer_floor=2352 "
    "clean_offered_bps=51200 clean_delivery=1.000 clean_p99=26.3 "
    "hostile_offer_msgs=2400 hostile_offer_floor=2352 hostile_delivery=1.000 "
    "lone_delivery=1.000 offer_bps=51200 offer_tolerance=0.02 "
    "nondergrading_p99_ms=100.0 delivery_floor=0.995",
    "test m2_offered_load_latency ... ok <86.162s>",
    "[mandate-smoke m3/rep1] delivered 0.963 MiB/s over 2.0004s, shaper forwarded "
    "0.972 MiB/s, capacity 1.000 MiB/s, fraction 0.963 (820148 / 992240 bytes)",
    "[mandate-smoke m3/rep2] delivered 0.971 MiB/s over 2.0011s, shaper forwarded "
    "0.980 MiB/s, capacity 1.000 MiB/s, fraction 0.971 (826960 / 994352 bytes)",
    "[mandate-smoke m3/rep3] delivered 0.958 MiB/s over 2.0008s, shaper forwarded "
    "0.967 MiB/s, capacity 1.000 MiB/s, fraction 0.958 (815872 / 990128 bytes)",
    "MANDATE M3 PASS goodput=0.52 floor=0.35 link_mib_s=8.0",
    "test m3_bulk_goodput_fraction ... ok <60.589s>",
    "[mandate-smoke m4/clean flow A] sent= 1200 recv= 1200 delivery=1.000 "
    "share=0.2502 offered=1269600B delivered=1269600B p50=   22.0 p90=   41.0 "
    "p99=  118.4 max=  240.0",
    "[mandate-smoke m4/clean flow B] sent= 1200 recv= 1200 delivery=1.000 "
    "share=0.2498 offered=1269600B delivered=1269600B p50=   23.5 p90=   42.0 "
    "p99=  121.0 max=  244.0",
    "[mandate-smoke m4/clean flow C] sent= 1200 recv= 1200 delivery=1.000 "
    "share=0.2501 offered=1269600B delivered=1269600B p50=   21.8 p90=   40.0 "
    "p99=  116.7 max=  238.0",
    "[mandate-smoke m4/clean flow D] sent= 1200 recv= 1200 delivery=1.000 "
    "share=0.2499 offered=1269600B delivered=1269600B p50=   24.1 p90=   43.0 "
    "p99=  119.9 max=  246.0",
    "[mandate-smoke m4/clean  ] ideal_share=0.2500 min_share=0.2498 "
    "max_share=+0.2502 imbalance=0.0016 window=12s wall=24.3s",
    # 1198 of 1200 is 0.998 to the three decimals the line prints: a delivery
    # figure its own counts cannot produce is what `check_delivery_granularity`
    # refuses, so the fixture is its own quotient rather than a ratio nothing
    # counts to.
    "[mandate-smoke m4/hostile flow A] sent= 1200 recv= 1198 delivery=0.998 "
    "share=0.2480 offered=1269600B delivered=1267000B p50=   96.0 p90=  180.0 "
    "p99=  402.0 max=  900.0",
    "[mandate-smoke m4/hostile] ideal_share=0.2500 min_share=0.2480 "
    "max_share=+0.2520 imbalance=0.0080 window=12s wall=24.1s",
    "MANDATE M4 PASS flows=4 clean_delivery_min=1.000 hostile_delivery_min=0.998 "
    "clean_imbalance=0.004 hostile_imbalance=0.008 imbalance_bound=0.010 "
    "fair_share=0.2500 delivery_floor=0.995 clean_p99_max=121.0 ceiling=250.0 "
    # The real line prints the hostile arm's own p99 guard beside the mandate
    # ceiling, and that measurement is what names the series the latency panel's
    # ceiling bound governs; the plotter refuses a crossed bound without it.
    "hostile_p99_guard=900.0 window_s=12.0",
    "test m4_interactive_lane_fairness ... ok <23.037s>",
    "test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
    "finished in 86.16s",
]


# The same stream from a libtest that did not stamp its results: the runner
# asked for the stamps and did not get them, so no test has a duration and the
# run must say so rather than bracket one from the line positions.
def unstamped(lines):
    """A libtest stream with the per-test stamps and the target total removed."""
    stripped = []
    for line in lines:
        line = re.sub(r" <[0-9.]+s>$", "", line)
        line = re.sub(r"; finished in [0-9.]+s$", "", line)
        stripped.append(line)
    return stripped


def arm_lines(lines):
    """Just the `[mandate-smoke ...]` lines of a plan's stdout, in order."""
    return [line for line in lines if line.startswith("[mandate-smoke ")]


# A second producer's stdout: this crate's own perf-tier probes. The shape is
# the real one — libtest flushes `test <name> ... ` before the test runs, the
# probe's own prose and arm line follow, and the state arrives on a line of its
# own — so this fixture also proves the runner times a producer that prints
# during its own test. There is no `MANDATE` line and no evidence file, because
# a report-only probe asserts no bound to declare — which is what `section=` is
# for.
PROBE_LINES = [
    "running 4 tests",
    "test tests::clean_forwarding_perf_probe ... clean_forwarding_perf_probe: "
    "direct=3.157 Mpps filter=513.149 Mpps queued=2.105 Mpps",
    "[mandate-smoke forwarding] section=probe recv=200000 direct_mpps=3.157 "
    "filter_mpps=513.149 queued_mpps=2.105",
    "ok <0.010s>",
    "test tests::short_deadline_latency_perf_probe ... short_deadline_latency_perf_probe: "
    "median=125ns",
    "[mandate-smoke deadline] section=probe recv=1000 median_us=0.125 "
    "idle_poll_us=5000.000",
    "ok <0.001s>",
    "test tests::std_udp_connected_peer_perf_probe ... [perf] connected UDP peer: "
    "connected=27259 roundtrips/s",
    "[mandate-smoke std-udp] section=probe recv=21 operations=2000 "
    "connected_rps=27259 unconnected_rps=23793 speedup=1.146",
    "ok <0.020s>",
    "test tests::learned_destination_cache_perf_probe ... [perf] Learned destination: "
    "cached=12.86 ns/packet",
    "[mandate-smoke dest-cache] section=probe recv=5000000 cached_ns=12.86 "
    "locked_ns=25.80 speedup=2.01",
    "ok <0.015s>",
    "test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
    "finished in 0.05s",
]


class MandateCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self._tmp.name)
        self.crate = self.root / "rtp_mux"
        (self.crate / "tests").mkdir(parents=True)
        (self.crate / "Cargo.toml").write_text(
            '[package]\nname = "rtp_mux"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (self.crate / "tests" / "mandate_smoke.rs").write_text(
            "// the mandate smoke set\n", encoding="utf-8"
        )
        self.cargo = self.root / "fake-cargo"
        self.cargo.write_text(FAKE_CARGO, encoding="utf-8")
        self.cargo.chmod(0o755)
        # A stand-in second producer's checkout: enough for the runner's source
        # check, and pointed at by --producer-path in the two-producer tests.
        self.probe_crate = self.root / "netem_test"
        (self.probe_crate / "src").mkdir(parents=True)
        (self.probe_crate / "Cargo.toml").write_text(
            '[package]\nname = "netem_test"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (self.probe_crate / "src" / "lib.rs").write_text(
            "// the perf-tier probes\n", encoding="utf-8"
        )
        self.out = self.root / "run"
        self.record = self.root / "cargo-record.json"
        self.plan_path = self.root / "plan.json"

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def healthy_plan(self, **overrides):
        plan = {
            "exit": 0,
            "stdout": list(PASS_LINES),
            "stderr": [],
            "mandates": {
                "M1": {"json": M1_DECLARATION, "csv": M1_ROWS},
                "M2": {"json": M2_DECLARATION, "csv": M2_ROWS},
                "M3": {"json": M3_DECLARATION, "csv": M3_ROWS},
                "M4": {"json": M4_DECLARATION, "csv": M4_ROWS},
            },
        }
        plan.update(overrides)
        return plan

    def two_producer_plan(self, probes=None, **overrides):
        """One plan carrying a sub-plan per package, for a two-producer run.

        The `rtp_mux` sub-plan is the healthy smoke set; the `netem_test`
        sub-plan is the probes' arm lines and no evidence.
        """
        plan = self.healthy_plan()
        probes = PROBE_LINES if probes is None else probes
        # Keyed by the cargo package name the runner invokes, which is the
        # package's own name and not the producer's registry id. Each sub-plan
        # sleeps between lines so the per-test brackets are real durations.
        plan["by_package"] = {
            "rtp_mux": {**self.healthy_plan(), "line_sleep": 0.01},
            "netem-test": {
                "exit": 0,
                "stdout": list(probes),
                "stderr": [],
                "line_sleep": 0.01,
            },
        }
        plan.update(overrides)
        return plan

    def run_two_producers(self, plan, *extra, **kwargs):
        """Run both producers against a fake cargo, with the probe checkout."""
        return self.run_tool(
            plan,
            "--producer-path",
            f"netem_test={self.probe_crate}",
            producers=("rtp_mux", "netem_test"),
            *extra,
            **kwargs,
        )

    def run_tool(
        self,
        plan,
        *extra,
        no_rasterize=True,
        browser=None,
        extra_env=None,
        producers=("rtp_mux",),
        history=False,
        cwd=None,
    ):
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        if self.record.exists():
            self.record.unlink()
        env = dict(os.environ)
        env["FAKE_CARGO_PLAN"] = str(self.plan_path)
        env["FAKE_CARGO_RECORD"] = str(self.record)
        env.update(extra_env or {})
        arguments = [
            "--cargo",
            str(self.cargo),
            "--producer-path",
            f"rtp_mux={self.crate}",
            "--dir",
            str(self.out),
        ]
        # The default producer set is every declared producer, so a test that
        # wants one producer names it; `producers=()` runs the default set.
        for name in producers:
            arguments += ["--producer", name]
        if no_rasterize:
            arguments.append("--no-rasterize")
        if browser is not None:
            arguments += ["--browser", browser]
        arguments += list(extra)
        # The history step (archive + compare) is the wrapper's, and is on by
        # default; a suite that wants the battery alone says so, which is what
        # the Python suite's in-process call did.
        command = [str(BINARY), "mandate-check"]
        if not history:
            command.append("--no-history")
        command += arguments
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def report(self):
        return json.loads((self.out / REPORT_NAME).read_text(encoding="utf-8"))

    def cargo_record(self):
        return self.cargo_records()[0]

    def cargo_records(self):
        lines = [
            line
            for line in self.record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [json.loads(line) for line in lines]

    def reject(self, plan, fragment, *extra, **kwargs):
        """Assert one run is refused non-zero, naming the problem."""
        code, stdout, stderr = self.run_tool(plan, *extra, **kwargs)
        self.assertNotEqual(code, 0, f"expected a non-zero exit; stdout={stdout}")
        self.assertIn(fragment, stdout + stderr)
        return code, stdout, stderr

    # -- the success path --------------------------------------------------

    def test_healthy_run_passes_and_writes_machine_checkable_evidence(self):
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        for mandate in ("M1", "M2", "M3", "M4"):
            self.assertIn(f"{mandate} PASS", stdout)
        self.assertIn("p99=31.5 ceiling=250.0 over250=0", stdout)
        self.assertIn("clean_delivery=1.0 clean_p99=26.3", stdout)
        self.assertIn("flows=4 clean_delivery_min=1.0", stdout)
        report = self.report()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["schema"], REPORT_SCHEMA)
        self.assertGreater(report["duration_seconds"], 0)
        self.assertEqual(report["smoke"]["exit_code"], 0)
        self.assertFalse(report["smoke"]["timed_out"])
        self.assertEqual(
            report["command"],
            [
                str(self.cargo),
                "test",
                "--release",
                "-p",
                "rtp_mux",
                "--test",
                "mandate_smoke",
                "--",
                *TIMING_ARGS,
                "--nocapture",
            ],
        )
        record = self.cargo_record()
        self.assertEqual(
            record["argv"],
            [
                "test",
                "--release",
                "-p",
                "rtp_mux",
                "--test",
                "mandate_smoke",
                "--",
                *TIMING_ARGS,
                "--nocapture",
            ],
        )
        # The stamps the run is timed from are libtest's, so the runner asks
        # for them itself and opens the gate the stable-pinned toolchain's
        # libtest puts on the flag.
        self.assertIn("--report-time", record["argv"])
        self.assertEqual(record["rustc_bootstrap"], TIMING_ENV_VALUE)
        self.assertEqual(Path(record["cwd"]).resolve(), self.crate.resolve())
        self.assertEqual(record["mandate_check_dir"], report["out_dir"])
        self.assertIsNone(record["quick"])

    def test_report_records_parsed_values_and_plot_paths(self):
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        m1 = report["mandates"]["M1"]
        self.assertEqual(m1["verdict"], "PASS")
        self.assertEqual(m1["values"], {"p99": 31.5, "ceiling": 250.0, "over250": 0})
        self.assertEqual(m1["raw_line"], "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0")
        self.assertEqual(m1["panels"], 2)
        self.assertEqual(len(m1["plots"]), 2)
        self.assertEqual(m1["series_counts"], [1, 1])
        for path in m1["plots"]:
            self.assertTrue(Path(path).is_file(), path)
            self.assertGreater(Path(path).stat().st_size, 0)
        self.assertEqual(
            Path(m1["plots"][0]).parent.resolve(), (self.out / "plots").resolve()
        )
        m3 = report["mandates"]["M3"]
        self.assertEqual(m3["values"], {"goodput": 0.52, "floor": 0.35, "link_mib_s": 8.0})
        self.assertEqual(len(m3["plots"]), 1)
        self.assertEqual(report["mandates"]["M2"]["values"]["clean_delivery"], 1.0)
        m4 = report["mandates"]["M4"]
        self.assertEqual(m4["verdict"], "PASS")
        self.assertEqual(m4["values"]["flows"], 4)
        self.assertEqual(m4["values"]["clean_p99_max"], 121.0)
        self.assertEqual(m4["panels"], 4)
        self.assertEqual(len(m4["plots"]), 4)
        self.assertEqual(m4["series_counts"], [8, 8, 8, 16])

    def test_every_panel_carries_its_mandatory_summary(self):
        # The forced summary: in the report, beside every plot, and printed by
        # the runner, so a reader never has to open an SVG to know what it shows.
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        for mandate in ("M1", "M2", "M3", "M4"):
            section = report["mandates"][mandate]
            self.assertEqual(
                len(section["panel_summaries"]), section["panels"], mandate
            )
            for document in section["panel_summaries"]:
                self.assertTrue(document["panel"], document)
                self.assertIn("panel ", document["block"])
                self.assertIn("axis=", document["block"])
                self.assertIn("reading:", document["block"])
        sidecar = self.out / "plots" / "M1-latency.summary.txt"
        self.assertTrue(sidecar.is_file())
        self.assertIn("panel latency", sidecar.read_text(encoding="utf-8"))
        self.assertIn("summary| panel latency", stdout)

    def test_verdict_block_names_every_plot_and_the_report(self):
        code, stdout, _ = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0)
        for mandate, panel in (
            ("M1", "latency"),
            ("M1", "cdf"),
            ("M2", "delivery"),
            ("M2", "latency"),
            ("M3", "goodput"),
            ("M4", "shares"),
            ("M4", "imbalance"),
            ("M4", "delivery"),
            ("M4", "latency"),
        ):
            path = (self.out / "plots" / f"{mandate}-{panel}.svg").resolve()
            self.assertIn(f"plot: {path}", stdout)
        self.assertIn(
            f"report:  {(self.out / REPORT_NAME).resolve()}", stdout
        )

    def test_report_records_per_test_and_per_mandate_timings(self):
        plan = self.healthy_plan()
        plan["line_sleep"] = 0.05
        code, stdout, stderr = self.run_tool(plan)
        self.assertEqual(code, 0, stderr)
        report = self.report()
        timings = report["timings"]
        self.assertIn("libtest-per-test-stamp", timings["method"])
        self.assertIn("never bracketed", timings["method"])
        self.assertNotIn("streamed-line-arrival", timings["method"])
        self.assertEqual(timings["origin"], "smoke-child-start")
        self.assertEqual(
            [entry["name"] for entry in timings["tests"]],
            [
                "m1_interactive_tail_latency",
                "m2_offered_load_latency",
                "m3_bulk_goodput_fraction",
                "m4_interactive_lane_fairness",
            ],
        )
        # Each duration is the fixture's own stamp, not the gap between its
        # line's arrival and the previous completion's (the line sleeps make
        # those gaps a fifth of a second, which none of these numbers is).
        self.assertEqual(
            [entry["duration_seconds"] for entry in timings["tests"]],
            [86.163, 86.162, 60.589, 23.037],
        )
        for entry in timings["tests"]:
            self.assertEqual(entry["target"], "mandate_smoke")
            self.assertEqual(entry["state"], "ok")
            self.assertEqual(entry["duration_source"], "libtest-report-time")
        # The target's own total is recorded, and the stamps fit it: the
        # fixture's 86.163 s test is the whole 86.16 s target, so the fit
        # check has to tolerate libtest's own rounding and still refuse a
        # number that could not be one test's time.
        (target,) = timings["targets"]
        self.assertEqual(target["target"], "mandate_smoke")
        self.assertEqual(target["producer"], "rtp_mux")
        self.assertEqual(target["total_seconds"], 86.16)
        self.assertEqual(target["total_source"], "libtest-finished-in")
        self.assertEqual(target["ran"], 4)
        self.assertEqual(target["stamped"], 4)
        self.assertEqual(target["max_seconds"], 86.163)
        self.assertEqual(target["sum_seconds"], 255.951)
        self.assertTrue(target["fits"])
        self.assertFalse(target["serial"])
        measured = {
            mandate: report["mandates"][mandate]["duration_seconds"]
            for mandate in ("M1", "M2", "M3", "M4")
        }
        self.assertEqual(set(measured), {"M1", "M2", "M3", "M4"})
        for duration in measured.values():
            self.assertGreater(duration, 0)
        for mandate in ("M1", "M2", "M3", "M4"):
            self.assertEqual(
                report["mandates"][mandate]["duration_source"],
                DURATION_SOURCE_STREAM_BRACKET,
            )
        self.assertIn("duration: ", stdout)
        self.assertIn(DURATION_SOURCE_STREAM_BRACKET, stdout)
        self.assertIn("4/4 test(s) stamped by libtest", stdout)
        self.assertIn("fit=yes", stdout)
        # The existing per-mandate assertion above (`assertGreater(duration, 0)`)
        # covers one half of the property a `duration:` line owes; here is the
        # half it cannot see: a duration must be a measurement or be absent with
        # its reason, so the runner's own check passes and no `0.00s` is
        # printed for a bracket nobody can trust. Every one of these four
        # brackets resolves, because this plan sleeps between lines.
        # A duration is a measurement or it is absent with its reason: a
        # `0.00s` figure is what "not measured" looks like too.
        for mandate in report["mandate_order"]:
            record = report["mandates"][mandate]
            if record["duration_seconds"] is None:
                self.assertTrue(record["duration_note"], record)
            else:
                self.assertNotEqual(
                    f"{record['duration_seconds']:.2f}", "0.00", record
                )
        self.assertNotIn("duration: 0.00s", stdout)
        self.assertNotIn("unmeasured", stdout)

    def test_a_run_whose_brackets_do_not_resolve_reports_unmeasured_not_zero(self):
        # End to end: a plan whose lines arrive together leaves every mandate
        # bracket unresolvable. The run still passes -- an unmeasurable bracket
        # is not a failure, it is a figure that must not be printed as one --
        # and every `duration:` line says unmeasured with its reason.
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        # This plan emits its lines with no pause, so the intervals between its
        # MANDATE lines are line-read gaps far below the report's resolution:
        # M2, M3 and M4 each close a bracket that is not a duration. (M1's own
        # bracket runs from the child's start, so it carries the process spawn
        # and is the one bracket here that legitimately resolves; it is checked
        # by the invariant below rather than pinned to a state.)
        for mandate in ("M2", "M3", "M4"):
            record = report["mandates"][mandate]
            self.assertIsNone(record["duration_seconds"], record)
            self.assertIn("empty bracket", record["duration_note"])
        for mandate in report["mandate_order"]:
            record = report["mandates"][mandate]
            if record["duration_seconds"] is None:
                self.assertTrue(record["duration_note"], record)
            else:
                self.assertGreater(record["duration_seconds"], 0, record)
        self.assertIn("duration: unmeasured (empty bracket:", stdout)
        self.assertNotIn("duration: 0.00s", stdout)
        # A duration is a measurement or it is absent with its reason: a
        # `0.00s` figure is what "not measured" looks like too.
        for mandate in report["mandate_order"]:
            record = report["mandates"][mandate]
            if record["duration_seconds"] is None:
                self.assertTrue(record["duration_note"], record)
            else:
                self.assertNotEqual(
                    f"{record['duration_seconds']:.2f}", "0.00", record
                )

    def test_a_stamp_less_target_makes_the_run_refuse_rather_than_invent(self):
        # End to end: the instrument is not measuring, so the report must not
        # carry numbers that look measured.
        code, stdout, _ = self.run_tool(
            self.healthy_plan(stdout=unstamped(PASS_LINES))
        )
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("not one of its results carries libtest's own per-test stamp", stdout)
        report = self.report()
        self.assertFalse(report["ok"])
        for entry in report["timings"]["tests"]:
            self.assertIsNone(entry["duration_seconds"], entry)
            self.assertIsNone(entry["duration_source"])
        self.assertEqual(len(report["problems"]), 1)
        self.assertIn("rtp_mux: the mandate_smoke target ran 4 test(s)", report["problems"][0])

    def test_a_stamp_over_the_targets_total_makes_the_run_refuse(self):
        plan = self.healthy_plan(
            stdout=[
                line.replace("ok <86.163s>", "ok <999.000s>")
                for line in PASS_LINES
            ]
        )
        code, stdout, _ = self.run_tool(plan)
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("cannot fit the mandate_smoke target's own total", stdout)
        report = self.report()
        (target,) = report["timings"]["targets"]
        self.assertFalse(target["fits"])

    def test_quick_asks_the_smoke_set_for_its_shortest_windows(self):
        code, _, stderr = self.run_tool(self.healthy_plan(), "--quick")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.cargo_record()["quick"], "1")
        self.assertTrue(self.report()["quick"])

    def test_an_inherited_quick_or_out_dir_cannot_reach_the_smoke_set(self):
        code, _, stderr = self.run_tool(
            self.healthy_plan(),
            extra_env={
                "MANDATE_SMOKE_QUICK": "1",
                "MANDATE_CHECK_DIR": str(self.root / "somewhere-else"),
            },
        )
        self.assertEqual(code, 0, stderr)
        record = self.cargo_record()
        self.assertIsNone(record["quick"])
        self.assertEqual(record["mandate_check_dir"], self.report()["out_dir"])

    def test_stale_evidence_from_an_earlier_run_is_cleared_first(self):
        (self.out / "plots").mkdir(parents=True)
        (self.out / "M1.json").write_text("{ not json", encoding="utf-8")
        (self.out / "M1.csv").write_text("panel,series,x,y\nstale,stale,0,1\n", encoding="utf-8")
        (self.out / "plots" / "M1-latency.svg").write_text("<svg/>", encoding="utf-8")
        (self.out / REPORT_NAME).write_text("{}", encoding="utf-8")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.report()["mandates"]["M1"]["series_counts"], [1, 1])
        rendered = (self.out / "plots" / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertNotEqual(rendered, "<svg/>")
        self.assertNotIn("stale", (self.out / "M1.csv").read_text(encoding="utf-8"))

    def test_a_run_that_writes_no_report_leaves_no_earlier_runs_report(self):
        # the `mandate-compare` subcommand reads `<dir>/mandate-check.json`, so a report
        # left behind by an earlier run is compared as if it were this run's
        # measurement. A run that cannot write its own report must therefore
        # leave none at all: not the report, not the log that explains what the
        # smoke set printed, and not an evidence file.
        self.out.mkdir(parents=True)
        stale_report = self.out / REPORT_NAME
        stale_report.write_text(
            json.dumps({"schema": "mandate-check/3", "ok": True, "verdict": "PASS"}),
            encoding="utf-8",
        )
        stale_log = self.out / LOG_NAME
        stale_log.write_text("an earlier run's smoke-set output\n", encoding="utf-8")
        (self.out / "M1.csv").write_text(
            "panel,series,x,y\nstale,stale,0,1\n", encoding="utf-8"
        )
        # The run cannot do its job: the smoke set source is gone, so the
        # command fails before it writes anything.
        (self.crate / "tests" / "mandate_smoke.rs").unlink()
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("does not exist", stderr)
        self.assertFalse(
            stale_report.exists(),
            "the earlier run's report survived a run that wrote none, so a "
            "later comparison would read it as this run's measurement",
        )
        self.assertFalse(
            stale_log.exists(),
            "the earlier run's log survived a run that wrote none, so it "
            "describes a different run than the one that just failed",
        )
        self.assertFalse((self.out / "M1.csv").exists())
        self.assertFalse((self.out / "plots").exists())

    def test_a_foreign_non_empty_dir_is_refused_rather_than_cleared(self):
        self.out.mkdir(parents=True)
        stranger = self.out / "notes.txt"
        stranger.write_text("someone else's file\n", encoding="utf-8")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 2)
        self.assertIn("is not this command's directory to clear", stderr)
        self.assertTrue(stranger.is_file())
        self.assertEqual(stranger.read_text(encoding="utf-8"), "someone else's file\n")

    def test_revision_is_resolved_from_git_when_jj_cannot(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        environment = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        run = subprocess.run(
            ["git", "init", "-q", str(self.crate)],
            capture_output=True,
            text=True,
            env=dict(os.environ, **environment),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        run = subprocess.run(
            ["git", "-C", str(self.crate), "commit", "-q", "--allow-empty", "-m", "smoke set"],
            capture_output=True,
            text=True,
            env=dict(os.environ, **environment),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        head = subprocess.run(
            ["git", "-C", str(self.crate), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "-C", str(self.crate), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.report()["rtp_mux"]["revision"], head)
        self.assertEqual(self.report()["rtp_mux"]["revision_source"], "git")
        self.assertEqual(self.report()["rtp_mux"]["tree_id"], head_tree)
        self.assertEqual(self.report()["rtp_mux"]["tree_id_source"], "git")
        self.assertIn(f"tree:     {head_tree} (git)", stdout)

    def test_a_malformed_censoring_row_is_refused(self):
        # The rows are the readings the M1 panel states, so a row that reads
        # nothing, and two rows for one arm, are failures rather than panels
        # drawn from a reading nothing chose.
        for label, extra in (
            ("no measurement", ["[m1-censoring] arm=impaired verdict="]),
            (
                "printed twice",
                ["[m1-censoring] arm=impaired verdict=Clear room=2000.0"],
            ),
        ):
            with self.subTest(case=label):
                plan = self.healthy_plan(stdout=list(PASS_LINES) + extra)
                self.reject(
                    plan,
                    "is printed twice"
                    if label == "printed twice"
                    else "carries no <key>=<value> measurement",
                )

    def test_report_records_each_arm_measurement_schema_seven(self):
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["schema"], "mandate-check/10")
        # The schema bump is over `/5`: a `/5` reader's keys keep their meaning
        # (a test's `duration_seconds` is still its own seconds, and the arm
        # record is untouched), and the new keys say where a duration came
        # from rather than changing what the old ones name. `/7` over `/6` adds
        # the per-arm instrument readings the plots state, in `censoring` (one
        # record per producer) and `censoring_arms` (the arms a mandate's line
        # panel actually states). `/8` over `/7` adds `duration_note` and makes
        # a mandate's `duration_seconds` null when its bracket resolves to no
        # duration at the report's own precision -- only a figure that could
        # never be trusted becomes null, so a `/7` reader keeps working. `/9`
        # over `/8` adds `delivery_granularity`: the units a declared delivery
        # floor tolerates at the run's own offered count, which is a new key and
        # leaves every `/8` field as it was.
        self.assertEqual(
            sorted(report["censoring"]),
            ["rtp_mux"],
        )
        self.assertEqual(
            report["censoring"]["rtp_mux"]["instrument"], "m1-censoring"
        )
        self.assertEqual(
            report["censoring"]["rtp_mux"]["arms"]["impaired"]["verdict"], "Clear"
        )
        self.assertEqual(report["mandates"]["M1"]["censoring_arms"], ["impaired"])
        self.assertEqual(report["mandates"]["M2"]["censoring_arms"], [])
        # The reading is on the panel, not only in the report: the verdict, the
        # arm's room, and a dot at every drawn sample (so the series' own
        # discreteness is visible rather than inferred from its steepness).
        svg = (self.out / "plots" / "M1-latency.svg").read_text(encoding="utf-8")
        self.assertIn("impaired: Clear", svg)
        self.assertIn("room 2000 ms", svg)
        self.assertIn('<circle class="sample"', svg)

        self.assertEqual(
            sorted(report["timings"]),
            ["mandates", "method", "origin", "targets", "tests"],
        )
        self.assertEqual(
            sorted(report["timings"]["tests"][0]),
            [
                "duration_seconds",
                "duration_source",
                "finished_at_seconds",
                "name",
                "producer",
                "started_at_seconds",
                "state",
                "target",
            ],
        )
        self.assertEqual(
            [arm["producer"] for arm in report["arms"]],
            ["rtp_mux"] * len(report["arms"]),
        )
        arms = {arm["id"]: arm for arm in report["arms"]}
        self.assertEqual(
            sorted(arms),
            [
                "M1/clean",
                "M1/hostile",
                "M1/lone_tail",
                "M2/clean",
                "M2/hostile",
                "M2/lone_tail",
                "M3/m3/rep1",
                "M3/m3/rep2",
                "M3/m3/rep3",
                "M4/m4/clean",
                "M4/m4/clean flow A",
                "M4/m4/clean flow B",
                "M4/m4/clean flow C",
                "M4/m4/clean flow D",
                "M4/m4/hostile",
                "M4/m4/hostile flow A",
            ],
        )
        clean = arms["M1/clean"]
        self.assertEqual(clean["mandate"], "M1")
        self.assertEqual(clean["label"], "clean")
        self.assertEqual(clean["dialect"], "kv")
        self.assertEqual(clean["sample_count"], 800)
        self.assertEqual(clean["stats"]["p99"], 89.0)
        self.assertEqual(clean["stats"]["over250"], 0)
        self.assertEqual(clean["counters"]["received"], 800)
        self.assertEqual(clean["counters"]["sent"], 800)
        self.assertEqual(clean["counters"]["wire_bytes"], 42300)
        self.assertEqual(clean["counters"]["bulk_wire_bytes"], 9123456)
        self.assertEqual(clean["windows"]["window_seconds"], 12)
        self.assertEqual(
            clean["cells"],
            [
                "M1@impairment=loss2pct-iid+latency=25ms+jitter=5ms+lane=dual"
                "+shape=cadence+flows=1+scale=256B+metric=p99",
            ],
        )
        self.assertIn("sent=", clean["raw_line"])
        lone = arms["M1/lone_tail"]
        self.assertEqual(lone["counters"]["wire_bytes"], 120000)
        self.assertEqual(lone["stats"]["over250"], 4)
        self.assertEqual(lone["cells"][0].split("@")[1].split("+")[3], "shape=request-response")
        # The bulk-rep dialect: the M3 rep line carries no `recv`, so its
        # sample count is absent rather than invented, and its shaper counter is
        # the forwarded bytes.
        rep = arms["M3/m3/rep2"]
        self.assertEqual(rep["dialect"], "bulk-rep")
        self.assertIsNone(rep["sample_count"])
        self.assertEqual(rep["stats"]["fraction"], 0.971)
        self.assertEqual(rep["counters"]["delivered_bytes"], 826960)
        self.assertEqual(rep["counters"]["forwarded_bytes"], 994352)
        self.assertEqual(rep["windows"]["elapsed_seconds"], 2.0011)
        self.assertEqual(rep["cells"][0].split("@")[0], "M3")
        # The M4 flow arms inherit the arm family's cells by longest prefix.
        flow = arms["M4/m4/clean flow C"]
        self.assertEqual(flow["sample_count"], 1200)
        self.assertEqual(flow["counters"]["offered_bytes"], 1269600)
        self.assertEqual(flow["counters"]["delivered_bytes"], 1269600)
        self.assertIn("flows=4", flow["cells"][0])
        aggregate = arms["M4/m4/clean"]
        self.assertIsNone(aggregate["sample_count"])
        self.assertEqual(aggregate["stats"]["imbalance"], 0.0016)
        self.assertEqual(aggregate["windows"]["wall_seconds"], 24.3)
        self.assertEqual(report["arm_notes"], [])
        self.assertEqual(
            report["arm_declaration"]["schema"],
            ARMS_DECLARATION_SCHEMA,
        )
        self.assertGreater(report["arm_declaration"]["declared_cells"], 0)
        self.assertIn("arms: 16 measured", stdout)
        self.assertIn("M1 arms: 3 (clean, hostile, lone_tail), 1840 sample(s)", stdout)

    # -- the delivery floor's own units ------------------------------------

    def test_delivery_granularity_states_the_units_each_floor_tolerates(self):
        # A delivery mandate asserts a ratio, but what it is a floor over is a
        # count of units the arm offers and receives, so the floor has a size:
        # `floor(offered x (1 - floor))` lost units are tolerated and the next
        # one breaches it. Three decimals of the ratio cannot state that, so the
        # report states it in units, and times the breach against the window of
        # the arm that tolerates fewest -- the arm whose breach is the cheapest
        # to reach is the one whose granularity the mandate states.
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        granularity = report["delivery_granularity"]
        self.assertEqual(sorted(granularity), ["M2", "M4"])
        m4 = granularity["M4"]
        self.assertEqual(m4["floor"], 0.995)
        self.assertEqual(m4["offered_min"], 1200)
        self.assertEqual(m4["budget_units"], 6)
        self.assertEqual(m4["min_failing_units"], 7)
        self.assertEqual(m4["units_short_max"], 2)
        self.assertEqual(m4["block_ms"], 70.0)
        hostile = [
            entry for entry in m4["arms"] if entry["id"].endswith("hostile flow A")
        ]
        self.assertEqual(len(hostile), 1)
        self.assertEqual(hostile[0]["offered"], 1200)
        self.assertEqual(hostile[0]["received"], 1198)
        self.assertEqual(hostile[0]["units_short"], 2)
        self.assertEqual(hostile[0]["budget_units"], 6)
        # M2's arms state their own windows, so the tightest budget is the
        # lone-tail arm's: 240 offered over 15 s tolerates `floor(240 x 0.005)`
        # = 1 lost message and breaches at the second, 125 ms of its own offer.
        m2 = granularity["M2"]
        self.assertEqual(m2["offered_min"], 240)
        self.assertEqual(m2["budget_units"], 1)
        self.assertEqual(m2["min_failing_units"], 2)
        self.assertEqual(m2["units_short_max"], 0)
        self.assertEqual(m2["block_ms"], 125.0)
        self.assertIn(
            "delivery: M4 floor=0.995 offered_min=1200 budget_units=6 "
            "units_short_max=2",
            stdout,
        )
        self.assertIn("a breach of the floor is 7 counted unit(s)", stdout)
        self.assertIn("delivery: M2 floor=0.995 offered_min=240", stdout)
        self.assertIn("is 2 counted unit(s)", stdout)

    def test_a_delivery_arm_without_its_counts_is_refused(self):
        # The ratio with nothing counting under it: this arm's delivery is 1198
        # of 1200 units, and with the received count removed the units the floor
        # tolerates -- and so the size of any breach of it -- cannot be stated
        # at all, which is exactly what a reader of a rare delivery failure
        # needs to know.
        plan = self.healthy_plan(
            stdout=[
                line.replace(
                    "sent= 1200 recv= 1198 delivery=0.998",
                    "sent= 1200 delivery=0.998",
                )
                for line in PASS_LINES
            ]
        )
        self.reject(
            plan,
            "M4/m4/hostile flow A reports delivery=0.998 under M4's "
            "delivery_floor 0.995 without the counts that ratio is the "
            "quotient of",
        )

    def test_a_delivery_ratio_its_counts_cannot_produce_is_refused(self):
        # A ratio that is not its own quotient is a measurement of something
        # else, and any unit budget recorded beside it would be arithmetic over
        # counts that do not belong to that ratio.
        plan = self.healthy_plan(
            stdout=[
                line.replace(
                    "sent= 1200 recv= 1200 delivery=1.000",
                    "sent= 1200 recv= 1200 delivery=0.990",
                )
                for line in PASS_LINES
            ]
        )
        self.reject(
            plan,
            "which is 1.000000: the ratio is not the quotient of the counts "
            "recorded beside it",
        )

    def test_a_delivery_reported_without_a_floor_is_refused(self):
        # The obligation is the mandate's own line: a line that reports a
        # delivery minimum must declare the floor those figures are read
        # against, or the reported delivery is read against no bound at all.
        plan = self.healthy_plan(
            stdout=[
                line.replace("delivery_floor=0.995 ", "") for line in PASS_LINES
            ]
        )
        self.reject(
            plan,
            "M4: the line reports clean_delivery_min, hostile_delivery_min but "
            "declares no delivery_floor",
        )

    def test_m1_without_a_censoring_reading_is_refused(self):
        # The panel that cannot show its own failure: a peak that returned and a
        # climb cut off by the window's end are the same pixels, so a producer
        # that declares M1 and prints no per-arm censoring reading is refused
        # rather than rendered with no machine verdict on it.
        plan = self.healthy_plan(
            stdout=[
                line for line in PASS_LINES if not line.startswith("[m1-censoring]")
            ]
        )
        self.reject(plan, "no '[m1-censoring] arm=...' reading")


    def test_a_prose_arm_line_is_kept_as_a_note_rather_than_dropped(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES)
            + ["[mandate-smoke m4/clean] noted 3 flows in flight, see the panel"]
        )
        code, _, stderr = self.run_tool(plan)
        # The note follows the last MANDATE line, so it is kept with no mandate
        # rather than attributed to one by guesswork.
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(
            report["arm_notes"],
            [
                {
                    "mandate": None,
                    "label": "m4/clean",
                    "body": "noted 3 flows in flight, see the panel",
                }
            ],
        )
        self.assertEqual(len(report["arms"]), 16)

    def test_an_arm_without_a_declared_cell_is_refused(self):
        plan = self.healthy_plan(
            stdout=[line.replace("clean    ", "brand_new", 1) for line in PASS_LINES]
        )
        code, stdout, _ = self.reject(plan, "covers no declared cell")
        self.assertEqual(code, 2)
        self.assertIn("'M1/brand_new'", stdout)

    def test_a_dangling_arm_line_is_refused_with_the_line_named(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["[mandate-smoke trailing] sent=1 recv=1 p99=1.0"]
        )
        code, stdout, _ = self.reject(plan, "cannot be attributed to a mandate")
        self.assertEqual(code, 2)
        self.assertIn("[mandate-smoke trailing]", stdout)

    def test_one_absent_arm_is_recorded_as_absent_rather_than_failing_the_run(self):
        plan = self.healthy_plan(
            stdout=[
                line
                for line in PASS_LINES
                if not line.startswith("[mandate-smoke clean    ]")
            ]
        )
        # The reader fails a run whose arm lines cannot be attributed or whose
        # mandate lost every arm; it does not invent an expected arm set, so a
        # single absent arm is reported by the comparison against the baseline
        # (an arm the baseline covered and the candidate did not) rather than
        # refusing the run here.
        code, _, stderr = self.run_tool(plan)
        self.assertEqual(code, 0, stderr)
        ids = [arm["id"] for arm in self.report()["arms"]]
        self.assertNotIn("M1/clean", ids)
        self.assertNotIn("M2/clean", ids)
        self.assertIn("M1/hostile", ids)

    def test_a_run_that_measured_no_arm_at_all_is_refused(self):
        plan = self.healthy_plan(
            stdout=[
                line for line in PASS_LINES if not line.startswith("[mandate-smoke ")
            ]
        )
        code, stdout, _ = self.reject(plan, "no '[mandate-smoke <arm>] ...' arm measurement")
        self.assertEqual(code, 2)
        for mandate in MANDATE_IDS:
            self.assertIn(f"{mandate}: no '[mandate-smoke", stdout)

    def test_an_arm_line_before_a_missing_mandate_line_is_named(self):
        plan = self.healthy_plan(
            stdout=[
                line for line in PASS_LINES if line != "MANDATE M1 PASS p99=31.5 ceiling=250.0 over250=0"
            ]
        )
        code, stdout, _ = self.reject(plan, "M1: no '[mandate-smoke")
        self.assertEqual(code, 2)

    def test_failing_mandate_exit_is_three_and_names_the_measured_values(self):
        # The per-arm lines stay in the plan: the extended contract requires a
        # measured arm for every mandate, and this case is about a measured
        # FAIL being a verdict rather than an evidence failure.
        plan = self.healthy_plan(
            stdout=[
                (
                    "MANDATE M2 FAIL clean_offer_msgs=120 clean_offer_floor=2352 "
                    "clean_offered_bps=51200 clean_delivery=0.784 clean_p99=18223.9 "
                    "hostile_offer_msgs=2400 hostile_offer_floor=2352 "
                    "hostile_delivery=1.000 lone_delivery=1.000 offer_bps=51200 "
                    "offer_tolerance=0.02 nondergrading_p99_ms=100.0 "
                    "delivery_floor=0.995"
                )
                if line.startswith("MANDATE M2 ")
                else line
                for line in PASS_LINES
            ]
        )
        code, stdout, stderr = self.run_tool(plan)
        self.assertEqual(code, 3, stderr)
        self.assertIn("M2 FAIL  clean_offer_msgs=120", stdout)
        self.assertIn("exit=3", stdout)
        report = self.report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["mandates"]["M2"]["verdict"], "FAIL")
        self.assertEqual(report["mandates"]["M2"]["values"]["clean_p99"], 18223.9)
        self.assertEqual(len(report["mandates"]["M2"]["plots"]), 2)

    # -- every rejection: non-zero, and naming the problem -----------------

    def test_missing_mandate_line_is_refused(self):
        # Extended from M1/M2/M3 to every id in MANDATE_IDS: a missing M4 line is
        # the failure mode the fourth id introduces, and it must be refused with
        # the same non-zero, evidence-incomplete treatment and name M4.
        for mandate in MANDATE_IDS:
            with self.subTest(mandate=mandate):
                plan = self.healthy_plan(
                    stdout=[
                        line
                        for line in PASS_LINES
                        if not line.startswith(f"MANDATE {mandate} ")
                    ]
                )
                code, stdout, _ = self.reject(plan, f"no 'MANDATE {mandate}")
                self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
                self.assertIn(f"{mandate}: the smoke set printed no", stdout)
                self.assertIn("never measured", stdout)
                self.assertEqual(
                    self.report()["mandates"][mandate]["declared"], False
                )

    def test_no_mandate_lines_at_all_is_refused(self):
        plan = self.healthy_plan(stdout=["running 4 tests", "test result: ok. 4 passed"])
        code, stdout, _ = self.reject(plan, "no 'MANDATE M1")
        self.assertEqual(code, 2)
        for mandate in MANDATE_IDS:
            self.assertIn(f"{mandate}: the smoke set printed no", stdout)

    def test_malformed_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["MANDATE M1 MAYBE p99=1.0", "MANDATE M9 PASS x=1"]
        )
        code, stdout, _ = self.reject(plan, "does not match the contract grammar")
        self.assertEqual(code, 2)
        self.assertIn("not one of M1, M2, M3, M4", stdout)

    def test_mandate_line_without_a_measurement_is_refused(self):
        plan = self.healthy_plan(
            stdout=[
                "MANDATE M1 PASS",
                "MANDATE M2 PASS delivery=1.000",
                "MANDATE M3 PASS goodput=0.52",
                "MANDATE M4 PASS flows=4",
            ]
        )
        self.reject(plan, "without a single key=value measurement")

    def test_duplicate_mandate_line_is_refused(self):
        plan = self.healthy_plan(
            stdout=list(PASS_LINES) + ["MANDATE M1 PASS p99=2.0"]
        )
        self.reject(plan, "M1 is declared twice")

    def test_missing_declaration_file_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M2"]["json"] = None
        code, stdout, _ = self.reject(plan, "M2.json was not written by the smoke set")
        self.assertEqual(code, 2)
        self.assertIn("MANDATE_CHECK_DIR", stdout)

    def test_missing_csv_file_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M3"]["csv"] = None
        self.reject(plan, "M3.csv was not written by the smoke set")

    def test_empty_csv_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = ""
        _, stdout, _ = self.reject(plan, "is empty")
        self.assertIn("M1: data CSV", stdout)

    def test_header_only_csv_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = [["panel", "series", "x", "y"]]
        self.reject(plan, "has no data rows")

    def test_a_declared_series_with_no_rows_is_refused(self):
        plan = self.healthy_plan()
        plan["mandates"]["M1"]["csv"] = [
            ["panel", "series", "x", "y"],
            ["latency", "impaired", 0.0, 12.5],
            ["latency", "impaired", 1.0, 31.5],
        ]
        self.reject(plan, "M1: the declaration and the data do not agree")
        self.assertEqual(self.report()["mandates"]["M1"]["plots"], [])

    def test_compile_failure_is_refused_and_the_log_is_kept(self):
        plan = self.healthy_plan(
            exit=101,
            stdout=[],
            stderr=["error[E0425]: cannot find value `nope` in this scope"],
            mandates={},
        )
        code, stdout, stderr = self.reject(plan, "the test target exited 101")
        self.assertEqual(code, 2)
        self.assertIn("cannot find value `nope`", stderr)
        self.assertIn("mandate-smoke.log", stdout)
        log = (self.out / LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("cannot find value `nope`", log)
        report = self.report()
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["smoke"]["exit_code"], 101)
        self.assertTrue(report["problems"])

    def test_timeout_is_refused(self):
        plan = self.healthy_plan(sleep=30)
        code, stdout, _ = self.reject(plan, "did not finish within 1s", "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertTrue(self.report()["smoke"]["timed_out"])

    def test_missing_crate_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        completed = subprocess.run(
            [
                str(BINARY),
                "mandate-check",
                "--no-history",
                "--producer-path",
                f"rtp_mux={empty}",
                "--dir",
                str(self.root / "run2"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("has no Cargo.toml", completed.stderr)

    def test_missing_smoke_source_is_refused(self):
        (self.crate / "tests" / "mandate_smoke.rs").unlink()
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 2)
        self.assertIn("mandate_smoke", stderr)
        self.assertIn("does not exist", stderr)

    def test_missing_cargo_is_refused(self):
        # The previous run's report is seeded first: a run refused after the
        # run directory was prepared must not leave it behind for a later
        # comparison to read as this run's measurement.
        self.out.mkdir(parents=True)
        stale_report = self.out / REPORT_NAME
        stale_report.write_text(
            json.dumps({"schema": "mandate-check/3", "ok": True}), encoding="utf-8"
        )
        completed = subprocess.run(
            [
                str(BINARY),
                "mandate-check",
                "--no-history",
                "--cargo",
                str(self.root / "no-such-cargo"),
                "--producer-path",
                f"rtp_mux={self.crate}",
                "--dir",
                str(self.out),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("was not found on PATH", completed.stderr)
        self.assertFalse(stale_report.exists())

    def test_a_plot_that_cannot_be_produced_is_refused(self):
        code, stdout, stderr = self.run_tool(
            self.healthy_plan(),
            no_rasterize=False,
            browser=str(self.root / "no-such-browser"),
        )
        self.assertEqual(code, 2, stdout)
        self.assertIn("no headless browser", stdout + stderr)
        self.assertIn("M1: ", stdout)

    # -- grammar unit coverage --------------------------------------------

    def test_a_run_records_every_declared_producers_arms(self):
        code, stdout, stderr = self.run_tool(
            self.two_producer_plan(),
            "--producer-path",
            f"netem_test={self.probe_crate}",
            producers=(),
        )
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["producers_declared"], ["rtp_mux", "netem_test"])
        self.assertEqual(report["producers_selected"], ["rtp_mux", "netem_test"])
        by_producer = {}
        for arm in report["arms"]:
            by_producer.setdefault(arm["producer"], []).append(arm["id"])
        self.assertEqual(sorted(by_producer), ["netem_test", "rtp_mux"])
        self.assertEqual(len(by_producer["rtp_mux"]), 16)
        self.assertEqual(
            sorted(by_producer["netem_test"]),
            [
                "probe/deadline",
                "probe/dest-cache",
                "probe/forwarding",
                "probe/std-udp",
            ],
        )
        self.assertEqual(report["producers"]["netem_test"]["arms"], 4)
        self.assertEqual(report["producers"]["netem_test"]["target"], "lib")
        self.assertFalse(report["producers"]["netem_test"]["verdicts"])
        # Evidence is derived from the verdict sections, so the registry entry
        # cannot declare the guard away.
        self.assertFalse(report["producers"]["netem_test"]["evidence"])
        self.assertTrue(report["producers"]["rtp_mux"]["evidence"])
        # The record a `mandate-check/4` reader reads stays the primary
        # producer's, not the second one's.
        self.assertEqual(report["smoke"]["producer"], "rtp_mux")
        self.assertEqual(
            report["command"], report["producers"]["rtp_mux"]["command"]
        )
        self.assertEqual(report["rtp_mux"]["path"], str(self.crate.resolve()))
        self.assertIn("arms: 20 measured (netem_test, rtp_mux)", stdout)
        self.assertIn("producer: netem_test  netem_test:lib", stdout)
        self.assertIn(
            "probe arms: 4 (deadline, dest-cache, forwarding, std-udp)", stdout
        )
        records = self.cargo_records()
        self.assertEqual(
            [record["package"] for record in records], ["rtp_mux", "netem-test"]
        )
        self.assertEqual(
            records[1]["argv"],
            [
                "test",
                "--release",
                "-p",
                "netem-test",
                "--lib",
                "--",
                *TIMING_ARGS,
                "--ignored",
                "--test-threads=1",
                "--nocapture",
            ],
        )
        self.assertEqual(
            records[1]["rustc_bootstrap"], TIMING_ENV_VALUE
        )
        self.assertEqual(
            [entry["producer"] for entry in report["timings"]["tests"]],
            ["rtp_mux"] * 4 + ["netem_test"] * 4,
        )
        self.assertEqual(
            [entry["target"] for entry in report["timings"]["tests"]],
            ["mandate_smoke"] * 4 + ["lib"] * 4,
        )
        # The probes print from inside their own tests, so their completions are
        # split by their own arm lines; both producers are still timed.
        probe_timings = [
            entry
            for entry in report["timings"]["tests"]
            if entry["producer"] == "netem_test"
        ]
        self.assertEqual(
            [entry["name"] for entry in probe_timings],
            [
                "tests::clean_forwarding_perf_probe",
                "tests::short_deadline_latency_perf_probe",
                "tests::std_udp_connected_peer_perf_probe",
                "tests::learned_destination_cache_perf_probe",
            ],
        )
        for entry in probe_timings:
            self.assertEqual(entry["state"], "ok")
            self.assertEqual(entry["duration_source"], "libtest-report-time")
        # The probe producer declares --test-threads=1, so its tests cannot
        # overlap and its stamped times must fit its own total; the fit check
        # is enforced there and the record says so.
        self.assertEqual(
            [entry["target"] for entry in report["timings"]["targets"]],
            ["mandate_smoke", "lib"],
        )
        probe_target = report["timings"]["targets"][1]
        self.assertTrue(probe_target["serial"])
        self.assertTrue(probe_target["fits"])
        self.assertEqual(
            [entry["duration_seconds"] for entry in probe_timings],
            [0.01, 0.001, 0.02, 0.015],
        )

    def test_the_second_producers_arms_keep_their_own_sample_counts(self):
        code, _, stderr = self.run_two_producers(self.two_producer_plan())
        self.assertEqual(code, 0, stderr)
        arms = {arm["id"]: arm for arm in self.report()["arms"]}
        forwarding = arms["probe/forwarding"]
        self.assertEqual(forwarding["mandate"], "probe")
        self.assertEqual(forwarding["label"], "forwarding")
        self.assertEqual(forwarding["dialect"], "kv")
        self.assertEqual(forwarding["sample_count"], 200000)
        self.assertEqual(forwarding["values"]["section"], "probe")
        self.assertEqual(forwarding["values"]["direct_mpps"], 3.157)
        self.assertEqual(
            forwarding["cells"],
            ["probe-forwarding@metric=throughput+layer=netem-runner"],
        )
        # A probe measures no window, so its record invents none: the measured
        # geometry the comparison reads is the sample count it actually ran.
        self.assertEqual(forwarding["windows"], {})
        self.assertEqual(arms["probe/std-udp"]["sample_count"], 21)
        self.assertEqual(arms["probe/dest-cache"]["sample_count"], 5000000)
        self.assertEqual(arms["probe/deadline"]["sample_count"], 1000)

    def test_a_producer_whose_arm_lines_are_gone_is_refused(self):
        plan = self.two_producer_plan(
            probes=["running 4 tests", "test result: ok. 4 passed; 0 failed"]
        )
        code, stdout, _ = self.run_two_producers(plan)
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("netem_test: probe: no '[mandate-smoke", stdout)
        # The other producer's arms are still recorded: one producer's failure
        # does not delete the other's evidence.
        self.assertEqual(
            len(
                [
                    arm
                    for arm in self.report()["arms"]
                    if arm["producer"] == "rtp_mux"
                ]
            ),
            16,
        )

    def test_an_arm_whose_section_the_producer_does_not_declare_is_refused(self):
        plan = self.two_producer_plan(
            probes=[
                line.replace("section=probe", "section=mandate") for line in PROBE_LINES
            ]
        )
        code, stdout, _ = self.run_two_producers(plan)
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("'mandate/forwarding'", stdout)
        self.assertIn("does not declare", stdout)

    def test_an_arm_with_no_section_token_before_any_mandate_line_is_refused(self):
        plan = self.two_producer_plan(
            probes=[line.replace(" section=probe", "") for line in PROBE_LINES]
        )
        code, stdout, _ = self.run_two_producers(plan)
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("cannot be attributed to a mandate", stdout)

    def test_a_missing_producer_source_is_refused(self):
        (self.probe_crate / "src" / "lib.rs").unlink()
        code, _, stderr = self.run_two_producers(self.two_producer_plan())
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("netem_test", stderr)
        self.assertIn("does not exist", stderr)

    def test_unknown_producer_selection_is_refused(self):
        code, _, stderr = self.run_tool(self.healthy_plan(), "--producer", "nope")
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("--producer 'nope' is not one of rtp_mux, netem_test", stderr)

    def test_a_malformed_producer_path_is_refused(self):
        code, _, stderr = self.run_tool(
            self.healthy_plan(), "--producer-path", "netem_test"
        )
        self.assertEqual(code, EXIT_EVIDENCE_FAILURE)
        self.assertIn("is not <id>=<path>", stderr)


    # -- the cases the port moved from the module to the command ------------

    def test_default_out_dir_is_beneath_tmpdir(self):
        # The runner's own default run directory: a fresh directory beneath
        # $TMPDIR, named by the run's own `output:` line.
        self.plan_path.write_text(json.dumps(self.healthy_plan()), encoding="utf-8")
        if self.record.exists():
            self.record.unlink()
        env = dict(
            os.environ,
            TMPDIR=str(self.root),
            FAKE_CARGO_PLAN=str(self.plan_path),
            FAKE_CARGO_RECORD=str(self.record),
        )
        completed = subprocess.run(
            [
                str(BINARY),
                "mandate-check",
                "--no-history",
                "--cargo",
                str(self.cargo),
                "--producer-path",
                f"rtp_mux={self.crate}",
                "--producer",
                "rtp_mux",
                "--no-rasterize",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        found = re.search(r"^  output:   (.+)$", completed.stdout, re.M)
        self.assertIsNotNone(found, completed.stdout)
        created = Path(found.group(1))
        self.assertEqual(created.parent.resolve(), self.root.resolve())
        self.assertTrue(created.is_dir())
        shutil.rmtree(created, ignore_errors=True)

    def test_the_shipped_registry_records_every_declared_producer(self):
        # The registry is the runner's own file, and the record it writes says
        # what each declared producer is: a reader can see which producers
        # exist and which the run selected, rather than reading a one-producer
        # report as the whole inventory.
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["producers_declared"], ["rtp_mux", "netem_test"])
        self.assertEqual(report["producers_selected"], ["rtp_mux"])
        self.assertEqual(report["section_order"], ["M1", "M2", "M3", "M4", "probe"])
        smoke = report["producers"][PRIMARY_PRODUCER]
        self.assertEqual(smoke["package"], "rtp_mux")
        self.assertEqual(smoke["target"], "mandate_smoke")
        self.assertEqual(smoke["source"], "tests/mandate_smoke.rs")
        self.assertEqual(smoke["default_path"], "../rtp_mux")
        self.assertEqual(smoke["sections"], ["M1", "M2", "M3", "M4"])
        self.assertEqual(smoke["verdicts"], ["M1", "M2", "M3", "M4"])
        self.assertTrue(smoke["evidence"])
        self.assertEqual(smoke["log"], str((self.out / LOG_NAME).resolve()))
        self.assertTrue(smoke["selected"])
        probe = report["producers"]["netem_test"]
        self.assertEqual(probe["package"], "netem_test")
        self.assertEqual(probe["target"], "lib")
        self.assertEqual(probe["sections"], ["probe"])
        self.assertEqual(probe["verdicts"], [])
        # Derived, not declared: a producer with no verdict line owes no
        # evidence file, so a registry entry cannot declare the guard away.
        self.assertFalse(probe["evidence"])
        self.assertFalse(probe["selected"])
        self.assertIsNone(probe["command"])

    def test_the_arm_declaration_and_its_cells_are_recorded(self):
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        declaration = report["arm_declaration"]
        self.assertEqual(declaration["schema"], ARMS_DECLARATION_SCHEMA)
        self.assertEqual(Path(declaration["path"]).name, ARMS_DECLARATION_NAME)
        self.assertTrue(declaration["source"])
        self.assertEqual(declaration["declared_cells"], 13)
        # Every arm claims a declared coverage cell, and a family prefix covers
        # the arms it names: `M4/m4/clean` covers the per-flow arms.
        cells = {arm["id"]: arm["cells"] for arm in report["arms"]}
        self.assertIn("M1/clean", cells)
        self.assertTrue(cells["M1/clean"])
        self.assertEqual(cells["M4/m4/clean flow A"], cells["M4/m4/clean"])

    def test_declared_checkouts_resolve_to_real_directories(self):
        # Every declared `default_path` resolves -- inside the workspace or
        # beside it -- to a directory that is there. One that does not is a
        # registry entry the runner can never build.
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        declared = [
            entry
            for entry in self.report()["producers"].values()
            if entry["default_path"]
        ]
        self.assertTrue(declared)
        for entry in declared:
            resolved = (WORKSPACE / entry["default_path"]).resolve()
            self.assertTrue(
                resolved.is_dir(), f"{entry['id']}: {resolved} is not a directory"
            )
            self.assertIn(
                WORKSPACE.parent,
                (resolved, *resolved.parents),
                f"{entry['id']}: {resolved} is neither the workspace nor beside it",
            )

    def test_tree_id_follows_the_content_and_not_the_commit_id(self):
        # The commit id is not the content. An empty commit on top of a tree
        # changes the commit id and leaves the tree id alone; a content change
        # moves the tree id.
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        environment = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*arguments):
            run = subprocess.run(
                ["git", "-C", str(self.crate), *arguments],
                capture_output=True,
                text=True,
                env=dict(os.environ, **environment),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout.strip()

        git("init", "-q")
        (self.crate / "tracked.txt").write_text("first\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "-m", "first")
        first = git("rev-parse", "HEAD")
        first_tree = git("rev-parse", "HEAD^{tree}")
        code, stdout, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["rtp_mux"]["revision"], first)
        self.assertEqual(report["rtp_mux"]["revision_source"], "git")
        self.assertEqual(report["rtp_mux"]["tree_id"], first_tree)
        self.assertEqual(report["rtp_mux"]["tree_id_source"], "git")
        self.assertIn(f"tree:     {first_tree} (git)", stdout)

        git("commit", "-q", "--allow-empty", "-m", "empty")
        empty = git("rev-parse", "HEAD")
        self.assertNotEqual(empty, first)
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["rtp_mux"]["revision"], empty)
        self.assertEqual(
            report["rtp_mux"]["tree_id"],
            first_tree,
            "an empty commit changed the commit id and the tree id with it: the "
            "tree id is not describing the content",
        )

        (self.crate / "tracked.txt").write_text("second\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "-m", "second")
        second_tree = git("rev-parse", "HEAD^{tree}")
        self.assertNotEqual(second_tree, first_tree)
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.report()["rtp_mux"]["tree_id"],
            second_tree,
            "the tree id did not move when the content did",
        )

    def test_tree_id_is_stable_across_a_jj_rewrite_of_the_working_copy(self):
        # jj rewrites `@` on every operation, so a commit id read from it is
        # throwaway: `jj new` produces a different commit id with the same tree
        # id, and only an edit moves the tree id.
        if shutil.which("jj") is None:
            self.skipTest("jj is not installed")
        environment = {
            "JJ_EDITOR": "true",
            "JJ_USER": "tree id test",
            "JJ_EMAIL": "tree-id@example.invalid",
        }

        def jj(*arguments):
            run = subprocess.run(
                ["jj", *arguments],
                cwd=str(self.crate),
                capture_output=True,
                text=True,
                env=dict(os.environ, **environment),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout.strip()

        jj("git", "init")
        (self.crate / "tracked.txt").write_text("first\n", encoding="utf-8")
        first = jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["rtp_mux"]["revision"], first)
        self.assertEqual(report["rtp_mux"]["revision_source"], "jj")
        first_tree = report["rtp_mux"]["tree_id"]
        self.assertEqual(len(first_tree), 40)

        jj("new")
        second = jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        self.assertNotEqual(second, first)
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        report = self.report()
        self.assertEqual(report["rtp_mux"]["revision"], second)
        self.assertEqual(
            report["rtp_mux"]["tree_id"],
            first_tree,
            "rewriting the working-copy commit moved the tree id: the tree id "
            "is not naming the content",
        )

        (self.crate / "tracked.txt").write_text("second\n", encoding="utf-8")
        jj("log", "-r", "@", "--no-graph", "-T", "commit_id")
        code, _, stderr = self.run_tool(self.healthy_plan())
        self.assertEqual(code, 0, stderr)
        self.assertNotEqual(self.report()["rtp_mux"]["tree_id"], first_tree)

    def test_the_history_step_writes_and_prints_the_summary_and_vs_prev(self):
        # A run owns three artifacts: the rendered panels, a run-level
        # `summary.md` and `vs-prev.md`. The history step produces the two text
        # ones and prints both; the boundary between the two Rust binaries is
        # where a port can silently drop them, so the assertion is on this
        # command's stdout.
        perf_history_binary()
        # The archive root is the working directory's `./.net-perf-history`, so
        # the run is given a working directory of its own rather than the
        # repository: a suite may not leave an archive in the tree it tests.
        code, stdout, stderr = self.run_tool(
            self.healthy_plan(), history=True, cwd=self.root
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.root / ".net-perf-history").is_dir())
        summary = self.out / "summary.md"
        vs_prev = self.out / "vs-prev.md"
        self.assertTrue(summary.is_file(), "summary.md was not written")
        self.assertTrue(vs_prev.is_file(), "vs-prev.md was not written")
        self.assertIn(summary.read_text(encoding="utf-8"), stdout)
        self.assertIn(vs_prev.read_text(encoding="utf-8"), stdout)
        summary = summary.resolve()
        vs_prev = vs_prev.resolve()
        self.assertTrue(list((self.out / "plots").glob("*.svg")))
        self.assertIn("verdict: PASS", stdout)
        self.assertIn(f"summary:  {summary}", stdout)
        self.assertIn(f"vs-prev:  {vs_prev}", stdout)

    def test_a_failing_history_step_fails_the_invocation(self):
        # The battery passed; the history step could not do its job. Its status
        # must reach the caller, or a run that was never archived reads as a
        # pass -- which is the whole reason the wrapper carries that status.
        perf_history_binary()
        # The archive root is `<working directory>/.net-perf-history`; a file
        # where that directory must go is a history step that cannot archive.
        (self.root / ".net-perf-history").write_text(
            "a file, not an archive root\n", encoding="utf-8"
        )
        code, stdout, stderr = self.run_tool(
            self.healthy_plan(), history=True, cwd=self.root
        )
        self.assertNotEqual(code, 0)
        self.assertIn("verdict: PASS", stdout)
        self.assertIn("perf-history: error:", stderr)
        self.assertEqual(code, 2)
        self.assertNotIn("summary.md", stdout)


if __name__ == "__main__":
    unittest.main()
