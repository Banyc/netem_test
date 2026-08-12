#!/usr/bin/env python3

import csv
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("rtp_trace_report.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class TraceReportTest(unittest.TestCase):
    def test_render_reads_trace_and_emits_all_figures(self):
        safe_tmp = os.environ["TMPDIR"]
        with tempfile.TemporaryDirectory(dir=safe_tmp) as directory:
            trace_dir = Path(directory)
            self.write_csv(trace_dir / "manifest.csv", [["key", "value"], ["scenario", "fixture"]])
            self.write_csv(
                trace_dir / "rtp.csv",
                [
                    [
                        "schema_version", "event_index", "elapsed_us", "event", "raw_rtt_us",
                        "pacer_tokens_packets", "send_rate_packets_per_second", "loss_ratio",
                        "in_flight_packets", "packets_in_pipe", "retransmitted_packets",
                        "next_send_sequence", "minimum_rtt_us", "smoothed_rtt_us",
                        "congestion_window_packets", "received_packets", "next_receive_sequence",
                        "delivery_rate_packets_per_second", "delivery_sample_app_limited",
                        "slow_start", "gentle_mode", "gentle_draining", "queue_building",
                        "drain_floor_binding", "outage_recovery", "no_response_for_us",
                        "no_progress_for_us", "stall_reason"
                    ],
                    [3, 0, 0, "rtt_sample", 100000, 0, 128, 0.01, 2, 2, 0, 3, 100000, 100000, 10, 1, 2, 64, False, True, False, False, False, False, False, 1000, 2000, ""],
                    [3, 2, 100000, "rtt_sample", 200000, 0, 256, 0.02, 3, 3, 1, 4, 100000, 112500, 11, 2, 3, 128, False, False, True, True, True, False, True, 2000, 3000, "no_progress"],
                    [4, 1, 50000, "rtt_sample", 300000, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
                ],
            )
            self.write_csv(
                trace_dir / "netem.csv",
                [
                    ["elapsed_us", "direction", "delayed", "dropped", "duplicated", "reordered", "rate_limited", "forwarded", "received", "overflow_dropped", "queue_len"],
                    [0, "c2s", 1, 0, 0, 0, 1, 1, 1, 0, 2],
                    [1, "c2s", 1, 1, 0, 1, 1, 1, 1, 0, 1],
                    [0, "s2c", 1, 0, 0, 0, 1, 1, 1, 0, 1],
                ],
            )
            self.write_csv(
                trace_dir / "progress.csv",
                [["elapsed_us", "delivered_bytes"], [0, 0], [100000, 1048576]],
            )
            output = trace_dir / "report.html"
            REPORT.render_report(trace_dir, output)
            document = output.read_text(encoding="utf-8")
            self.assertIn("RTT over time", document)
            self.assertIn("Raw RTT histogram", document)
            self.assertIn("Raw RTT empirical CDF", document)
            self.assertIn("Netem queue depth", document)
            self.assertIn("Rolling application goodput", document)
            self.assertIn("Controller modes", document)
            self.assertIn("Congestion-control action", document)
            self.assertIn("Peer liveness waits", document)
            self.assertIn("RTP send staging", document)
            self.assertIn("p50=200.00 ms", document)

            (trace_dir / "rtp_peer.csv").write_text(
                (trace_dir / "rtp.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            peer_output = trace_dir / "peer.html"
            REPORT.render_report(trace_dir, peer_output, "rtp_peer.csv")
            self.assertIn("rtp_peer.csv", peer_output.read_text(encoding="utf-8"))

    def test_render_accepts_legacy_rate_headers(self):
        safe_tmp = os.environ["TMPDIR"]
        with tempfile.TemporaryDirectory(dir=safe_tmp) as directory:
            trace_dir = Path(directory)
            self.write_csv(trace_dir / "manifest.csv", [["key", "value"], ["trace_schema_version", 1]])
            self.write_csv(
                trace_dir / "rtp.csv",
                [
                    [
                        "schema_version", "event_index", "elapsed_us", "event", "raw_rtt_us",
                        "pacer_tokens_packets", "send_rate_bytes_per_second", "loss_ratio",
                        "in_flight_packets", "packets_in_pipe", "retransmitted_packets",
                        "next_send_sequence", "minimum_rtt_us", "smoothed_rtt_us",
                        "congestion_window_packets", "received_packets", "next_receive_sequence",
                        "delivery_rate_bytes_per_second", "app_limited"
                    ],
                    [1, 0, 0, "rtt_sample", 100000, 0, 128, 0.01, 2, 2, 0, 3, 100000, 100000, 10, 1, 2, 64, False],
                ],
            )
            self.write_csv(trace_dir / "netem.csv", [["elapsed_us", "direction", "delayed", "dropped", "duplicated", "reordered", "rate_limited", "forwarded", "received", "overflow_dropped", "queue_len"]])
            self.write_csv(trace_dir / "progress.csv", [["elapsed_us", "delivered_bytes"], [0, 0]])
            output = trace_dir / "report.html"
            REPORT.render_report(trace_dir, output)
            self.assertIn("packets/s", output.read_text(encoding="utf-8"))

    @staticmethod
    def write_csv(path, rows):
        with path.open("w", newline="", encoding="utf-8") as output:
            csv.writer(output).writerows(rows)


if __name__ == "__main__":
    unittest.main()
