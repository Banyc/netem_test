#!/usr/bin/env python3

import csv
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("rtp_trace_compare.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_compare", MODULE_PATH)
COMPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPARE)


class TraceCompareTest(unittest.TestCase):
    def test_render_compares_controller_occupancy_and_distributions(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", 1.0, "censored_outage_sample", 128)
            self.write_trace(after, "new", 2.0, "bandwidth_probe", 512)
            (after / "rtp_peer.csv").write_text(
                (after / "rtp.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            output = root / "comparison.html"

            COMPARE.render_comparison(
                [
                    ("before-1", before),
                    ("after-peer-1", after, "rtp_peer.csv"),
                ],
                output,
            )

            document = output.read_text(encoding="utf-8")
            self.assertIn("before-1", document)
            self.assertIn("after-peer-1", document)
            self.assertIn("1.000", document)
            self.assertIn("2.000", document)
            self.assertIn("Rolling application goodput", document)
            self.assertIn("Raw RTT empirical CDF", document)
            self.assertIn("Peer liveness waits", document)
            self.assertIn("rtp_peer.csv", document)
            self.assertIn("terminations", document)
            self.assertIn("<td>1</td>", document)
            self.assertIn("1 / 0.000", document)
            self.assertIn("censored_outage_sample", document)
            self.assertIn("bandwidth_probe", document)
            self.assertEqual(
                COMPARE.parse_spec(f"peer={after / 'rtp_peer.csv'}"),
                ("peer", after, "rtp_peer.csv"),
            )

    @staticmethod
    def write_trace(trace_dir, revision, goodput, action, send_rate):
        trace_dir.mkdir()
        TraceCompareTest.write_csv(
            trace_dir / "manifest.csv",
            [
                ["key", "value"],
                ["revision", revision],
                ["netem_c2s_seed", 11],
                ["netem_s2c_seed", 12],
                ["goodput_mib_per_second", goodput],
            ],
        )
        TraceCompareTest.write_csv(
            trace_dir / "rtp.csv",
            [
                [
                    "elapsed_us", "raw_rtt_us", "smoothed_rtt_us",
                    "send_rate_packets_per_second", "congestion_action", "outage_recovery",
                    "delivery_sample_app_limited", "pending_send_bytes", "accepts_new_packet",
                    "no_response_for_us", "no_progress_for_us", "stall_reason", "event",
                ],
                [0, 100000, 100000, send_rate, action, False, False, 0, True, 0, 0, "", "send_data_pkt"],
                [1000000, 200000, 150000, send_rate, action, True, True, 8192, False, 1000000, 500000, "no_response", "proactive_termination"],
            ],
        )
        TraceCompareTest.write_csv(
            trace_dir / "progress.csv",
            [
                ["elapsed_us", "delivered_bytes"],
                [0, 0],
                [1000000, 1048576 * goodput],
            ],
        )

    @staticmethod
    def write_csv(path, rows):
        with path.open("w", newline="", encoding="utf-8") as output:
            csv.writer(output).writerows(rows)


if __name__ == "__main__":
    unittest.main()
