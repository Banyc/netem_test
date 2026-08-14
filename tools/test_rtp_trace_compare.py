#!/usr/bin/env python3

import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("rtp_trace_compare.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_compare", MODULE_PATH)
COMPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPARE)


RTP_HEADER = [
    "schema_version", "event_index", "elapsed_us", "event",
    "termination_cause", "termination_error_kind", "termination_raw_os_error",
    "raw_rtt_us", "pacer_tokens_packets", "send_rate_packets_per_second",
    "loss_ratio", "in_flight_packets", "packets_in_pipe",
    "retransmitted_packets", "next_send_sequence", "minimum_rtt_us",
    "smoothed_rtt_us", "congestion_window_packets", "received_packets",
    "next_receive_sequence", "delivery_rate_packets_per_second",
    "delivery_sample_app_limited", "pending_send_bytes",
    "send_stage_capacity_bytes", "accepts_new_packet", "slow_start",
    "gentle_mode", "gentle_draining", "queue_building",
    "drain_floor_binding", "outage_recovery", "no_response_for_us",
    "no_progress_for_us", "stall_reason", "congestion_loss_ratio",
    "congestion_action", "trace_elapsed_us",
]

NETEM_HEADER = [
    "elapsed_us", "trace_elapsed_us", "direction", "delayed", "dropped",
    "duplicated", "reordered", "rate_limited", "forwarded", "received",
    "overflow_dropped", "queue_len",
]


def rtp_row(
    index,
    elapsed_us,
    event,
    raw_rtt_us,
    send_rate,
    action="none",
    slow_start=False,
    gentle_mode=False,
    gentle_draining=False,
    queue_building=False,
    drain_floor_binding=False,
    outage_recovery=False,
    cause="",
    error_kind="",
    trace_elapsed_us=None,
):
    return [
        "9", index, elapsed_us, event, cause, error_kind, "", raw_rtt_us, 0,
        send_rate, "0.0", 1, 1, 0, index + 1, raw_rtt_us, raw_rtt_us, 2, 1,
        index + 2, send_rate, "false", 0, 4096, "true",
        "true" if slow_start else "false",
        "true" if gentle_mode else "false",
        "true" if gentle_draining else "false",
        "true" if queue_building else "false",
        "true" if drain_floor_binding else "false",
        "true" if outage_recovery else "false",
        0, 0, "", "0.0", action,
        trace_elapsed_us if trace_elapsed_us is not None else elapsed_us,
    ]


class TraceCompareTest(unittest.TestCase):
    def write_trace(
        self,
        trace_dir,
        revision,
        goodput,
        c2s_seed,
        s2c_seed,
        *,
        probe_outcome="completed",
        rtp_observer="true",
        broken=False,
    ):
        trace_dir.mkdir(parents=True)
        manifest = [
            ["key", "value"],
            ["trace_schema_version", "9"],
            ["rtp_observer", rtp_observer],
            ["rtp_dropped_capacity", "0"],
            ["rtp_peer_dropped_capacity", "0"],
            ["measurement_start_trace_elapsed_us", "1000000"],
            ["revision", revision],
            ["scenario", "mux_over_rtp_hostile_goodput_30s"],
            ["window_seconds", "30"],
            ["mss_bytes", "8192"],
            ["fec", "false"],
            ["rtp_handshake", "false"],
            ["link_profile", "hostile"],
            ["netem_c2s_seed", c2s_seed],
            ["netem_s2c_seed", s2c_seed],
            ["netem_samples", "1"],
            ["progress_samples", "2"],
            ["goodput_mib_per_second", goodput],
            ["probe_outcome", probe_outcome],
            ["sink_read_outcome", "completed"],
            ["client_mux_outcome", "ok"],
            ["server_mux_outcome", "ok"],
            ["delivered_bytes", int(float(goodput) * 1024 * 1024 * 30)],
            ["elapsed_seconds", "30"],
        ]
        self.write_csv(trace_dir / "manifest.csv", manifest)
        rows = [
            RTP_HEADER,
            rtp_row(0, 0, "send_data_pkt", 100000, 128, slow_start=True),
            rtp_row(
                1,
                50000,
                "session_termination",
                200000,
                512,
                action="bandwidth_probe",
                slow_start=True,
                cause="stopped",
                error_kind="ok",
                trace_elapsed_us=1050000,
            ),
        ]
        self.write_csv(trace_dir / "rtp.csv", rows)
        self.write_csv(trace_dir / "rtp_peer.csv", rows)
        self.write_csv(
            trace_dir / "netem.csv",
            [
                NETEM_HEADER,
                [0, 1000000, "c2s", 50, 8, 0, 1, 0, 90, 100, 2, 1],
                [0, 1000000, "s2c", 60, 3, 0, 0, 0, 95, 100, 1, 1],
            ],
        )
        self.write_csv(
            trace_dir / "progress.csv",
            [
                ["elapsed_us", "trace_elapsed_us", "delivered_bytes"],
                [1000000, 1000000, 0],
                [31000000, 31000000, int(float(goodput) * 1024 * 1024 * 30)],
            ],
        )
        if broken:
            (trace_dir / "rtp_peer.csv").unlink()

    def test_render_compares_controller_occupancy_and_distributions(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(after, "new", "2.0", 11, 12)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )

            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["schema_version"], 2)
            self.assertEqual(comparison["valid_pairs"], 1)
            self.assertEqual(comparison["total_pairs"], 1)
            self.assertEqual(comparison["verdict"], "likely_improvement")

            # Deterministic verdict: re-render and compare byte-for-byte.
            output2 = root / "comparison2"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output2,
            )
            self.assertEqual(
                (output / "comparison.json").read_bytes(),
                (output2 / "comparison.json").read_bytes(),
            )

            document = (output / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("before-1", document)
            self.assertIn("after-1", document)
            self.assertIn("Controller-state occupancy", document)
            self.assertIn("slow start", document)
            self.assertIn("terminations", document)
            self.assertIn("Rolling application goodput", document)
            self.assertIn("Raw RTT empirical CDF", document)
            self.assertIn("Agent guidance", document)
            self.assertIn("does_not_prove", document)

            run_by_label = {run["label"]: run for run in comparison["runs"]}
            self.assertEqual(run_by_label["before-1"]["role"], "baseline")
            self.assertEqual(run_by_label["after-1"]["role"], "candidate")
            self.assertEqual(
                run_by_label["after-1"]["summary"]["controller_state_occupancy"]["slow_start"],
                100.0,
            )

    def test_agent_guidance_rejects_broken_trace_and_bounds_causality(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(after, "new", "0.5", 11, 12, broken=True)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["valid_pairs"], 0)
            self.assertEqual(comparison["verdict"], "insufficient_evidence")
            self.assertEqual(comparison["evidence_quality"], "invalid")
            broken_run = comparison["runs"][1]
            self.assertEqual(broken_run["evidence_quality"], "invalid")
            self.assertFalse(broken_run["checks"]["peer_readable"])
            # Source-boundary hints carry does_not_prove and no causal wording.
            guidance = json.dumps(comparison["agent_guidance"])
            self.assertIn("does_not_prove", guidance)
            for forbidden in ("caused", "causal", "due to", "because of the change"):
                self.assertNotIn(forbidden, guidance)

    def test_both_valid_pairs_at_minus_10_yields_likely_regression(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "2.0", 11, 12)
            self.write_trace(after, "new", "1.6", 11, 12)
            self.write_trace(root / "before2", "old", "3.0", 21, 22)
            self.write_trace(root / "after2", "new", "2.4", 21, 22)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before), ("before-2", root / "before2")],
                [("after-1", after), ("after-2", root / "after2")],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["valid_pairs"], 2)
            self.assertEqual(comparison["verdict"], "likely_regression")
            # Both deltas are at or below -10%.
            for pair in comparison["pairs"]:
                self.assertLessEqual(
                    pair["metrics"]["goodput_mib_per_second"]["delta_percent"], -10.0
                )

    def test_one_invalid_trace_yields_insufficient_evidence_when_no_valid_pair_remains(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "2.0", 11, 12)
            self.write_trace(after, "new", "1.6", 11, 12)
            self.write_trace(root / "before2", "old", "3.0", 21, 22)
            self.write_trace(root / "after2", "new", "2.4", 21, 22, broken=True)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before), ("before-2", root / "before2")],
                [("after-1", after), ("after-2", root / "after2")],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["valid_pairs"], 1)
            self.assertEqual(comparison["total_pairs"], 2)
            # The broken pair is excluded; the surviving valid pair drives the
            # classification (still at -20%, so likely_regression).
            self.assertEqual(comparison["verdict"], "likely_regression")
            excluded = [p for p in comparison["pairs"] if not p["valid"]]
            self.assertEqual(len(excluded), 1)
            self.assertIn("candidate trace invalid", excluded[0]["excluded_reasons"])

            # Same fixture with every candidate invalid: no valid pair remains.
            self.write_trace(root / "after-broken1", "new", "1.6", 11, 12, broken=True)
            self.write_trace(root / "after-broken2", "new", "2.4", 21, 22, broken=True)
            output2 = root / "comparison2"
            COMPARE.render_comparison(
                [("before-1", before), ("before-2", root / "before2")],
                [
                    ("after-1", root / "after-broken1"),
                    ("after-2", root / "after-broken2"),
                ],
                output2,
            )
            comparison2 = json.loads(
                (output2 / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison2["valid_pairs"], 0)
            self.assertEqual(comparison2["verdict"], "insufficient_evidence")

    @staticmethod
    def write_csv(path, rows):
        with path.open("w", newline="", encoding="utf-8") as output:
            csv.writer(output).writerows(rows)


if __name__ == "__main__":
    unittest.main()
