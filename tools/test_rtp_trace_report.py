#!/usr/bin/env python3

import csv
import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("rtp_trace_report.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class TraceReportTest(unittest.TestCase):
    def test_queue_hold_has_a_distinct_lane_before_delay_drain(self):
        # queue_hold must occupy lane 6 and delay_drain lane 7 so a hold is
        # visually distinct from (and plotted before) the drain that follows.
        self.assertEqual(REPORT.CONGESTION_ACTION_LANES["queue_hold"], 6.0)
        self.assertEqual(REPORT.CONGESTION_ACTION_LANES["delay_drain"], 7.0)
        safe_tmp = os.environ["TMPDIR"]
        with tempfile.TemporaryDirectory(dir=safe_tmp) as directory:
            trace_dir = Path(directory)
            self.write_csv(
                trace_dir / "manifest.csv",
                [["key", "value"], ["scenario", "lane-fixture"]],
            )
            self.write_csv(
                trace_dir / "rtp.csv",
                [
                    "elapsed_us", "raw_rtt_us", "minimum_rtt_us", "smoothed_rtt_us",
                    "send_rate_packets_per_second", "retransmission_timeout_us",
                    "congestion_window_packets", "retransmitted_packets",
                    "packets_in_pipe", "next_send_sequence",
                    "next_receive_sequence", "loss_ratio", "congestion_action",
                ],
                [0, 100000, 100000, 100000, 128, 500000, 10, 0, 2, 3, 2, 0.0, "queue_hold"],
                [100000, 200000, 100000, 200000, 300, 600000, 31, 4, 20, 4, 2, 0.2, "delay_drain"],
            )
            self.write_csv(
                trace_dir / "netem.csv",
                ["elapsed_us", "direction", "delayed", "dropped", "duplicated", "reordered", "rate_limited", "forwarded", "received", "overflow_dropped", "queue_len"],
                [0, "c2s", 1, 0, 0, 0, 1, 1, 1, 0, 2],
            )
            self.write_csv(
                trace_dir / "progress.csv",
                ["elapsed_us", "delivered_bytes"],
                [0, 0],
            )
            output = trace_dir / "report.html"
            REPORT.render_report(trace_dir, output)
            document = output.read_text(encoding="utf-8")
            section = document.split(
                "<h2>Congestion-control action</h2>", 1
            )[1].split("</section>", 1)[0]
            polyline = re.search(r'<polyline points="([^"]+)"', section)
            self.assertIsNotNone(polyline)
            points = [
                tuple(float(value) for value in pair.split(","))
                for pair in polyline.group(1).split()
            ]
            self.assertEqual(len(points), 2)
            # The first row (queue_hold) renders before the second
            # (delay_drain); the two lanes map to distinct y positions with
            # lane 6 plotted before lane 7 on the shared action axis.
            first_lane = 10.0 - (
                points[0][1] - REPORT.PAD_TOP
            ) / (REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM) * 10.0
            second_lane = 10.0 - (
                points[1][1] - REPORT.PAD_TOP
            ) / (REPORT.HEIGHT - REPORT.PAD_TOP - REPORT.PAD_BOTTOM) * 10.0
            self.assertAlmostEqual(first_lane, 6.0)
            self.assertAlmostEqual(second_lane, 7.0)
            self.assertLess(points[0][0], points[1][0])

    def test_render_reads_trace_and_emits_all_figures(self):
        safe_tmp = os.environ["TMPDIR"]
        with tempfile.TemporaryDirectory(dir=safe_tmp) as directory:
            trace_dir = Path(directory)
            self.write_csv(trace_dir / "manifest.csv", [["key", "value"], ["scenario", "fixture"]])
            self.write_csv(
                trace_dir / "rtp.csv",
                [
                    "schema_version", "event_index", "elapsed_us", "event", "raw_rtt_us",
                    "pacer_tokens_packets", "send_rate_packets_per_second", "loss_ratio",
                    "in_flight_packets", "packets_in_pipe", "retransmitted_packets",
                    "next_send_sequence", "minimum_rtt_us", "smoothed_rtt_us",
                    "congestion_window_packets", "received_packets", "next_receive_sequence",
                    "delivery_rate_packets_per_second", "delivery_sample_app_limited",
                    "slow_start", "gentle_mode", "gentle_draining", "queue_building",
                    "drain_floor_binding", "outage_recovery", "no_response_for_us",
                    "no_progress_for_us", "stall_reason", "retransmission_timeout_us",
                    "oldest_pipe_packet_age_us", "maximum_packet_rto_overdue_us",
                    "rto_deadline_postponements", "retransmission_active_packets",
                    "retransmission_ready_packets", "congestion_control_rtt_us",
                    "congestion_rtt_floor_us", "congestion_queue_tolerance_us",
                    "congestion_persistent_queue_for_us",
                    "congestion_persistent_queue_resets",
                    "congestion_delivery_peak_packets_per_second",
                    "congestion_drain_floor_packets_per_second",
                    "congestion_drain_target_packets_per_second",
                    "congestion_bandwidth_probe_decisions", "congestion_rate_samples",
                    "congestion_bandwidth_probe_increases",
                    "congestion_bandwidth_probe_before_feedback",
                    "congestion_last_bandwidth_probe_interval_us", "congestion_delay_drains",
                ],
                [3, 0, 0, "rtt_sample", 100000, 0, 128, 0.01, 2, 2, 0, 3, 100000, 100000, 10, 1, 2, 64, False, True, False, False, False, False, False, 1000, 2000, "", 500000, 300000, 120000, 1, 2, 1, 100000, 90000, 20000, 400000, 1],
                [3, 2, 100000, "rtt_sample", 200000, 0, 300, 200, 220, 20, 4, 2, 1, 250000, 31, 1, 2, 3, 128, False, False, True, True, True, False, 2000, 3000, "no_progress", 600000, 400000, 240000, 2, 3, 2, 110000, 95000, 25000, "", 2, 320, 210, 230, 12, 5, 3, 2, 300000, 4],
                [4, 1, 50000, "rtt_sample", 300000, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", 1],
            )
            self.write_csv(
                trace_dir / "netem.csv",
                ["elapsed_us", "direction", "delayed", "dropped", "duplicated", "reordered", "rate_limited", "forwarded", "received", "overflow_dropped", "queue_len"],
                [0, "c2s", 1, 0, 0, 0, 1, 1, 1, 0, 2],
                [1, "c2s", 1, 1, 1, 1, 1, 0, 1, 0, 1],
                [0, "s2c", 0, 0, 1, 1, 1, 0, 1, 0, 1],
            )
            self.write_csv(
                trace_dir / "progress.csv",
                ["elapsed_us", "delivered_bytes"],
                [0, 0],
                [100000, 1048576],
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
            self.assertIn("current RTO", document)
            self.assertIn("maximum stored-RTO overdue", document)
            self.assertIn("controller RTT floor", document)
            self.assertIn("controller queue gate", document)
            self.assertIn("persistent queue duration", document)
            self.assertIn("controller drain target", document)
            self.assertIn("retransmission active", document)
            self.assertIn("retransmission ready", document)
            self.assertIn("Retransmission causes", document)
            self.assertIn("RTO deadline postponements", document)
            self.assertIn("Controller RTT and probe timing", document)
            self.assertIn("Controller decision counts", document)
            self.assertIn("persistent queue resets", document)

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
    def write_csv(path, *rows):
        if len(rows) == 1 and rows[0] and isinstance(rows[0][0], (list, tuple)):
            rows = rows[0]
        with path.open("w", newline="", encoding="utf-8") as output:
            csv.writer(output).writerows(rows)


if __name__ == "__main__":
    unittest.main()
