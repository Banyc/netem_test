#!/usr/bin/env python3

import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
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
    "retransmitted_packets", "retransmission_attempts",
    "retransmission_first_attempts", "retransmission_repeat_attempts",
    "retransmission_rto_reason", "retransmission_reorder_reason",
    "retransmission_fast_loss_reason", "retransmission_pre_outage_reason",
    "tail_probe_attempts", "next_send_sequence", "minimum_rtt_us",
    "smoothed_rtt_us", "congestion_window_packets", "received_packets",
    "next_receive_sequence", "delivery_rate_packets_per_second",
    "delivery_sample_app_limited", "application_write_waiters",
    "application_limited_detections",
    "application_limited_detections_suppressed_by_waiting_writer",
    "pending_send_bytes", "send_stage_capacity_bytes", "accepts_new_packet", "slow_start",
    "gentle_mode", "gentle_draining", "queue_building",
    "drain_floor_binding", "outage_recovery", "no_response_for_us",
    "no_progress_for_us", "stall_reason", "congestion_persistent_queue_for_us",
    "congestion_persistent_queue_resets", "congestion_loss_ratio",
    "congestion_action", "trace_elapsed_us",
]

NETEM_HEADER = [
    "elapsed_us", "trace_elapsed_us", "direction", "delayed", "dropped",
    "duplicated", "reordered", "rate_limited", "forwarded", "received",
    "overflow_dropped", "queue_len",
]


def rtp_row(
    event,
    raw_rtt_us,
    elapsed_us=0,
    send_rate=128,
    action="none",
    slow_start=False,
    gentle_mode=False,
    gentle_draining=False,
    queue_building=False,
    outage_recovery=False,
    drain_floor_binding=False,
    cause="",
    error_kind="",
    persistent_queue_for_us="",
    persistent_queue_resets=0,
    trace_elapsed_us=None,
    index=0,
):
    return [
        "9", index, elapsed_us, event, cause, error_kind, "", raw_rtt_us, 0,
        send_rate, "0.0", 1, 1, 0,
        index + 1, index + 1, 0, index + 1, 0, 0, 0, 2,
        index + 1, raw_rtt_us, raw_rtt_us, 2, 1,
        index + 2, send_rate, "false", 1, index + 5, index + 2, 0, 4096, "true",
        "true" if slow_start else "false",
        "true" if gentle_mode else "false",
        "true" if gentle_draining else "false",
        "true" if queue_building else "false",
        "true" if drain_floor_binding else "false",
        "true" if outage_recovery else "false",
        0, 0, "", persistent_queue_for_us, persistent_queue_resets, "0.0", action,
        trace_elapsed_us if trace_elapsed_us is not None else elapsed_us,
    ]


class TraceCompareTest(unittest.TestCase):
    def test_split_window_goodput_uses_the_exact_midpoint(self):
        mib = 1024 * 1024
        first, second = COMPARE.split_window_goodput(
            [(0.0, 0.0), (9.8, 9.8 * mib), (10.2, 10.6 * mib), (20.0, 40 * mib)],
            20.0,
            40 * mib,
        )
        self.assertAlmostEqual(first, 1.02)
        self.assertAlmostEqual(second, 2.98)


    def test_split_window_goodput_rejects_a_sparse_midpoint(self):
        self.assertEqual(
            COMPARE.split_window_goodput(
                [(0.0, 0.0), (20.0, 40 * 1024 * 1024)],
                20.0,
                1024 * 1024,
            ),
            (None, None),
        )

    def write_trace(
        self,
        trace_dir,
        label,
        goodput,
        c2s_seed,
        s2c_seed,
        probe_outcome="completed",
        rtp_observer="true",
        broken=False,
        timeboxed_open=False,
        warmup_seconds=None,
        gentle_mode=False,
        gentle_draining=False,
        queue_building=False,
        drain_floor_binding=False,
        outage_recovery=False,
        gentle_mode_exits=None,
        peer_gentle_mode_exits=None,
        trace_schema_version="9",
        counter_baseline_present=None,
        persistent_queue_for_us="",
        persistent_queue_resets=0,
        first_action="none",
        second_action="bandwidth_probe",
        revision="1",
    ):
        trace_dir.mkdir(parents=True)
        manifest = [
            ["key", "value"],
            ["trace_schema_version", trace_schema_version],
            ["rtp_observer", rtp_observer],
            ["rtp_dropped_capacity", "0"],
            ["rtp_peer_dropped_capacity", "0"],
            ["rtp_send_driver_resume_signal_wakes", "11"],
            ["rtp_send_driver_ack_schedule_signal_wakes", "14"],
            ["rtp_send_driver_pacing_timer_wakes", "12"],
            ["rtp_send_driver_protocol_timer_wakes", "13"],
            ["rtp_send_driver_kill_requested_wakes", "1"],
            ["rtp_peer_send_driver_resume_signal_wakes", "21"],
            ["rtp_peer_send_driver_ack_schedule_signal_wakes", "24"],
            ["rtp_peer_send_driver_pacing_timer_wakes", "22"],
            ["rtp_peer_send_driver_protocol_timer_wakes", "23"],
            ["rtp_peer_send_driver_kill_requested_wakes", "2"],
            ["rtp_send_driver_resume_application_data_requests", "31"],
            ["rtp_send_driver_resume_peer_ack_requests", "32"],
            ["rtp_peer_send_driver_resume_ack_flush_requests", "41"],
            ["rtp_retransmission_armor_duplicates", "17"],
            ["rtp_peer_retransmission_armor_duplicates", "27"],
            ["rtp_data_send_would_blocks", "5"],
            ["rtp_peer_data_send_would_blocks", "6"],
            ["measurement_start_trace_elapsed_us", "1000000"],
            ["scenario_revision", revision],
            ["scenario", "mux_over_rtp_hostile_goodput_30s"],
            ["window_seconds", "30"],
            ["mss_bytes", "8192"],
            ["fec", "false"],
            ["retransmission_armor", "false"],
            ["rtp_handshake", "false"],
            ["link_profile", "hostile"],
            ["netem_c2s_seed", c2s_seed],
            ["netem_s2c_seed", s2c_seed],
            ["netem_samples", "1"],
            ["progress_samples", "3"],
            ["goodput_mib_per_second", goodput],
            ["probe_outcome", probe_outcome],
            ["measurement_end_reason", "timebox_elapsed" if timeboxed_open else ""],
            ["sink_read_outcome", "running" if timeboxed_open else "completed"],
            ["client_mux_outcome", "running" if timeboxed_open else "ok"],
            ["server_mux_outcome", "running" if timeboxed_open else "ok"],
            ["delivered_bytes", int(float(goodput) * 1024 * 1024 * 30)],
            ["elapsed_seconds", "30"],
        ]
        if warmup_seconds is not None:
            manifest.append(["warmup_seconds", str(warmup_seconds)])
        if counter_baseline_present is not None:
            present = str(counter_baseline_present).lower()
            manifest.extend(
                [
                    ["rtp_counter_baseline_present", present],
                    ["rtp_peer_counter_baseline_present", present],
                ]
            )
        for cause, count in (gentle_mode_exits or {}).items():
            manifest.append([f"rtp_gentle_mode_exit_{cause}", str(count)])
        for cause, count in (peer_gentle_mode_exits or {}).items():
            manifest.append([f"rtp_peer_gentle_mode_exit_{cause}", str(count)])
        self.write_csv(trace_dir / "manifest.csv", manifest)
        rows = [
            RTP_HEADER,
            rtp_row(
                "send_data_pkt",
                100000,
                action=first_action,
                slow_start=True,
                gentle_mode=gentle_mode,
                gentle_draining=gentle_draining,
                queue_building=queue_building,
                drain_floor_binding=drain_floor_binding,
                outage_recovery=outage_recovery,
                persistent_queue_for_us=persistent_queue_for_us,
                persistent_queue_resets=persistent_queue_resets,
            ),
        ]
        if not timeboxed_open:
            rows.append(rtp_row(
                "session_termination",
                50000,
                200000,
                action=second_action,
                slow_start=True,
                gentle_mode=gentle_mode,
                gentle_draining=gentle_draining,
                queue_building=queue_building,
                drain_floor_binding=drain_floor_binding,
                outage_recovery=outage_recovery,
                cause="stopped",
                error_kind="",
                trace_elapsed_us=1050000,
                persistent_queue_for_us=persistent_queue_for_us,
                persistent_queue_resets=persistent_queue_resets,
                index=1,
            ))
        self.write_csv(trace_dir / "rtp.csv", rows)
        self.write_csv(trace_dir / "rtp_peer.csv", rows)
        self.write_csv(
            trace_dir / "netem.csv",
            NETEM_HEADER,
            [0, 1000000, "c2s", 50, 8, 0, 1, 0, 90, 100, 2, 1],
            [0, 1000000, "s2c", 60, 3, 0, 0, 0, 95, 100, 1, 1],
        )
        self.write_csv(
            trace_dir / "progress.csv",
            ["elapsed_us", "trace_elapsed_us", "delivered_bytes"],
            [0, 0, 0],
            [15000000, 15000000, int(float(goodput) * 1024 * 1024 * 15)],
            [30000000, 30000000, int(float(goodput) * 1024 * 1024 * 30)],
        )
        if broken:
            (trace_dir / "rtp_peer.csv").unlink()

    def test_render_compares_controller_occupancy_and_distributions(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(
                before,
                "old",
                "1.0",
                11,
                12,
                gentle_mode=True,
                gentle_draining=True,
                queue_building=True,
                drain_floor_binding=True,
                outage_recovery=True,
                first_action="queue_hold",
                gentle_mode_exits={
                    "loss": 2,
                    "gate_open": 1,
                    "drain_guard": 0,
                    "outage_reset": 1,
                },
                peer_gentle_mode_exits={
                    "loss": 3,
                    "gate_open": 0,
                    "drain_guard": 1,
                    "outage_reset": 0,
                },
                persistent_queue_for_us=400000,
                persistent_queue_resets=7,
            )
            self.write_trace(
                after,
                "new",
                "2.0",
                11,
                12,
                first_action="delay_drain",
                gentle_mode_exits={
                    "loss": 0,
                    "gate_open": 2,
                    "drain_guard": 0,
                    "outage_reset": 0,
                },
                peer_gentle_mode_exits={
                    "loss": 1,
                    "gate_open": 1,
                    "drain_guard": 0,
                    "outage_reset": 2,
                },
                persistent_queue_resets=2,
            )
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["schema_version"], 39)
            self.assertEqual(comparison["valid_pairs"], 1)
            self.assertEqual(comparison["total_pairs"], 1)
            self.assertEqual(comparison["verdict"], "likely_improvement")
            for run in comparison["runs"]:
                if run["label"] == "before-1":
                    mib_delivered = 30.0
                else:
                    mib_delivered = 60.0
                self.assertEqual(
                    run["summary"]["goodput_first_half_mib_per_second"],
                    run["summary"]["goodput_mib_per_second"],
                )
                self.assertEqual(
                    run["summary"]["goodput_second_half_mib_per_second"],
                    run["summary"]["goodput_mib_per_second"],
                )
                self.assertEqual(run["summary"]["application_write_waiter_occupancy"], 100.0)
                self.assertEqual(run["summary"]["application_limited_detections"], 6.0)
                self.assertEqual(
                    run["summary"]["gentle_gate_open_streak_max_ms"],
                    0.0,
                )
                self.assertEqual(
                    run["summary"]["application_limited_detections_suppressed_by_waiting_writer"],
                    3.0,
                )
                self.assertAlmostEqual(
                    run["summary"]["application_limited_suppression_percent"],
                    100.0 / 3.0,
                )
                # The action-row occupancy metrics are exercised by the
                # fixture: one queue_hold/delay_drain row out of two state rows.
                self.assertEqual(
                    run["summary"]["queue_hold_occupancy"],
                    50.0 if run["label"] == "before-1" else 0.0,
                )
                self.assertEqual(
                    run["summary"]["delay_drain_occupancy"],
                    0.0 if run["label"] == "before-1" else 50.0,
                )
                # The legacy fixtures carry no loss-backoff columns, so the
                # counters stay absent: binding percent stays unknown and the
                # per-GiB values are zero (never normalized without bytes).
                self.assertIsNone(
                    run["summary"]["congestion_loss_backoff_floor_binding_percent"]
                )
                self.assertEqual(
                    run["summary"]["congestion_loss_backoffs_per_gib_delivered"],
                    0.0,
                )
                self.assertEqual(
                    run["summary"]["congestion_loss_backoff_floor_bindings_per_gib_delivered"],
                    0.0,
                )
                self.assertIsNone(run["summary"]["congestion_loss_backoff_floor_lift_p50"])
                self.assertIsNone(run["summary"]["congestion_loss_backoff_floor_lift_max"])
                self.assertEqual(
                    run["summary"]["final_retransmission_counters"],
                    {
                        "attempts": 2.0,
                        "first_attempts": 2.0,
                        "repeat_attempts": 0.0,
                        "rto_reason": 2.0,
                        "reorder_reason": 0.0,
                        "fast_loss_reason": 0.0,
                        "pre_outage_reason": 0.0,
                        "tail_probes": 2.0,
                    },
                )
                self.assertEqual(
                    run["summary"]["send_driver_wakes"],
                    {
                        "resume_signal": 11.0,
                        "ack_schedule_signal": 14.0,
                        "pacing_timer": 12.0,
                        "protocol_timer": 13.0,
                        "kill_requested": 1.0,
                    },
                )
                self.assertEqual(
                    run["summary"]["peer_send_driver_wakes"],
                    {
                        "resume_signal": 21.0,
                        "ack_schedule_signal": 24.0,
                        "pacing_timer": 22.0,
                        "protocol_timer": 23.0,
                        "kill_requested": 2.0,
                    },
                )
                self.assertEqual(
                    run["summary"]["send_driver_resume_requests"]["application_data"],
                    31.0,
                )
                self.assertEqual(
                    run["summary"]["send_driver_resume_requests"]["peer_ack"],
                    32.0,
                )
                self.assertEqual(
                    run["summary"]["peer_send_driver_resume_requests"]["ack_flush"],
                    41.0,
                )
                # Normalized scheduler/recovery metrics: protocol-timer wakes
                # per GiB, application-data resume requests per GiB, armor
                # duplicates raw + per GiB, and WouldBlocks raw + per GiB.
                self.assertAlmostEqual(
                    run["summary"]["sender_protocol_timer_wakes_per_gib_delivered"],
                    13.0 * 1024 / mib_delivered,
                )
                self.assertAlmostEqual(
                    run["summary"]["peer_protocol_timer_wakes_per_gib_delivered"],
                    23.0 * 1024 / mib_delivered,
                )
                self.assertAlmostEqual(
                    run["summary"]["sender_application_data_resume_requests_per_gib_delivered"],
                    31.0 * 1024 / mib_delivered,
                )
                self.assertEqual(
                    run["summary"]["sender_retransmission_armor_duplicates"], 17.0
                )
                self.assertAlmostEqual(
                    run["summary"]["sender_retransmission_armor_duplicates_per_gib_delivered"],
                    17.0 * 1024 / mib_delivered,
                )
                self.assertEqual(
                    run["summary"]["peer_retransmission_armor_duplicates"], 27.0
                )
                self.assertAlmostEqual(
                    run["summary"]["peer_retransmission_armor_duplicates_per_gib_delivered"],
                    27.0 * 1024 / mib_delivered,
                )
                self.assertEqual(run["summary"]["sender_data_send_would_blocks"], 5.0)
                self.assertAlmostEqual(
                    run["summary"]["sender_data_send_would_blocks_per_gib_delivered"],
                    5.0 * 1024 / mib_delivered,
                )
                self.assertEqual(run["summary"]["peer_data_send_would_blocks"], 6.0)
                self.assertAlmostEqual(
                    run["summary"]["peer_data_send_would_blocks_per_gib_delivered"],
                    6.0 * 1024 / mib_delivered,
                )
            pair_metrics = comparison["pairs"][0]["metrics"]
            self.assertAlmostEqual(
                pair_metrics["goodput_second_half_mib_per_second"]["delta_percent"],
                100.0,
            )
            self.assertAlmostEqual(
                pair_metrics["retransmission_attempts_per_gib_delivered"]["baseline"],
                2 * 1024 / 30,
            )
            self.assertAlmostEqual(
                pair_metrics["retransmission_attempts_per_gib_delivered"]["candidate"],
                2 * 1024 / 60,
            )
            self.assertAlmostEqual(
                pair_metrics["retransmission_attempts_per_gib_delivered"]["delta_percent"],
                -50.0,
            )
            self.assertAlmostEqual(
                pair_metrics["retransmission_attempts_per_gib_delivered"]["difference"],
                -(2 * 1024 / 60),
            )
            self.assertEqual(
                pair_metrics["retransmission_repeat_attempts_per_gib_delivered"]["baseline"],
                0.0,
            )
            self.assertEqual(pair_metrics["gentle_mode_occupancy"]["baseline"], 100.0)
            self.assertEqual(pair_metrics["gentle_mode_occupancy"]["candidate"], 0.0)
            self.assertEqual(pair_metrics["gentle_mode_occupancy"]["delta_percent"], -100.0)
            self.assertEqual(pair_metrics["gentle_draining_occupancy"]["delta_percent"], -100.0)
            self.assertEqual(pair_metrics["queue_building_occupancy"]["baseline"], 100.0)
            self.assertEqual(pair_metrics["queue_building_occupancy"]["candidate"], 0.0)
            self.assertEqual(pair_metrics["drain_floor_binding_occupancy"]["delta_percent"], -100.0)
            self.assertEqual(pair_metrics["outage_recovery_occupancy"]["delta_percent"], -100.0)
            self.assertEqual(pair_metrics["queue_hold_occupancy"]["baseline"], 50.0)
            self.assertEqual(pair_metrics["queue_hold_occupancy"]["candidate"], 0.0)
            self.assertEqual(pair_metrics["queue_hold_occupancy"]["delta_percent"], -100.0)
            self.assertEqual(pair_metrics["delay_drain_occupancy"]["baseline"], 0.0)
            self.assertEqual(pair_metrics["delay_drain_occupancy"]["candidate"], 50.0)
            self.assertEqual(pair_metrics["delay_drain_occupancy"]["difference"], 50.0)
            self.assertEqual(pair_metrics["persistent_queue_occupancy"]["baseline"], 100.0)
            self.assertEqual(pair_metrics["persistent_queue_occupancy"]["candidate"], 0.0)
            self.assertEqual(pair_metrics["persistent_queue_max_ms"]["baseline"], 400.0)
            self.assertEqual(pair_metrics["persistent_queue_max_ms"]["candidate"], 0.0)
            self.assertEqual(pair_metrics["persistent_queue_resets"]["baseline"], 7.0)
            self.assertEqual(pair_metrics["persistent_queue_resets"]["candidate"], 2.0)
            for cause, baseline, candidate, difference in (
                ("loss", 5.0, 1.0, -4.0),
                ("gate_open", 1.0, 3.0, 2.0),
                ("drain_guard", 1.0, 0.0, -1.0),
                ("outage_reset", 1.0, 2.0, 1.0),
            ):
                metric = pair_metrics[f"gentle_mode_exit_{cause}"]
                self.assertEqual(metric["baseline"], baseline)
                self.assertEqual(metric["candidate"], candidate)
                self.assertEqual(metric["difference"], difference)
            self.assertEqual(pair_metrics["app_limited_occupancy"]["baseline"], 0.0)
            self.assertEqual(
                pair_metrics["application_write_waiter_occupancy"]["candidate"],
                100.0,
            )
            self.assertAlmostEqual(
                pair_metrics["application_limited_suppression_percent"]["baseline"],
                100.0 / 3.0,
            )
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
            self.assertIn("first / second half MiB/s", document)
            self.assertIn("Raw RTT empirical CDF", document)
            self.assertIn("Agent guidance", document)
            self.assertIn("does_not_prove", document)
            self.assertIn("application_limited_suppression_percent", document)
            self.assertIn("gentle_mode_occupancy", document)
            self.assertIn("gentle_draining_occupancy", document)
            self.assertIn("queue_building_occupancy", document)
            self.assertIn("drain_floor_binding_occupancy", document)
            self.assertIn("outage_recovery_occupancy", document)
            self.assertIn("persistent_queue_occupancy", document)
            self.assertIn("persistent_queue_max_ms", document)
            self.assertIn("persistent_queue_resets", document)
            self.assertIn("gentle_mode_exit_drain_guard", document)
            self.assertIn("retransmission_attempts_per_gib_delivered", document)
            self.assertIn(
                "retransmission_repeat_attempts_per_gib_delivered", document
            )
            run_by_label = {run["label"]: run for run in comparison["runs"]}
            self.assertEqual(run_by_label["before-1"]["role"], "baseline")
            self.assertEqual(run_by_label["after-1"]["role"], "candidate")
            self.assertEqual(
                run_by_label["after-1"]["summary"]["controller_state_occupancy"]["slow_start"],
                100.0,
            )
            self.assertEqual(
                run_by_label["after-1"]["summary"]["rtt_p50_ms"],
                75.0,
            )


    def test_successful_timebox_accounts_for_intentionally_live_endpoints(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12, timeboxed_open=True)
            self.write_trace(after, "new", "1.1", 11, 12, timeboxed_open=True)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["schema_version"], 39)
            self.assertEqual(comparison["evidence_quality"], "healthy")
            for run in comparison["runs"]:
                self.assertEqual(run["evidence_quality"], "healthy")
                self.assertTrue(run["checks"]["endpoint_lifecycle_accounted"])
                self.assertEqual(run["summary"]["terminations"], {})
                self.assertEqual(
                    run["summary"]["measurement_end_reason"],
                    "timebox_elapsed",
                )


    def test_measurement_state_window_includes_long_warmup_offset(self):
        manifest = {
            "rtp_observer": "true",
            "window_seconds": "30",
            "measurement_start_trace_elapsed_us": "60000000",
        }
        rtp = [{"smoothed_rtt_us": "1000", "trace_elapsed_us": "75000000"}]
        checks = COMPARE.trace_health(Path("."), manifest, rtp, [], [], [])
        self.assertTrue(checks["checks"]["measurement_state_present"])

    def test_sparse_message_timebox_accepts_its_missing_sink_outcome_only(self):
        manifest = {
            "scenario": "mux_over_rtp_hostile-periodic-bottleneck-300ms_message_latency_window",
            "measurement_end_reason": "timebox_elapsed",
            "probe_outcome": "completed",
            "client_mux_outcome": "running",
            "server_mux_outcome": "running",
        }
        checks = COMPARE.trace_health(Path("."), manifest, [], [], [], [])
        self.assertTrue(checks["checks"]["endpoint_lifecycle_accounted"])

        manifest["scenario"] = "mux_over_rtp_hostile_goodput_30s"
        checks = COMPARE.trace_health(Path("."), manifest, [], [], [], [])
        self.assertFalse(checks["checks"]["endpoint_lifecycle_accounted"])

        manifest["scenario"] = (
            "mux_over_rtp_hostile-periodic-bottleneck-300ms_message_latency_window"
        )
        manifest["client_mux_outcome"] = "error: closed early"
        checks = COMPARE.trace_health(Path("."), manifest, [], [], [], [])
        self.assertFalse(checks["checks"]["endpoint_lifecycle_accounted"])

    def test_schema_22_and_23_require_counter_baselines_after_warmup(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            for schema in ("22", "23", "24", "25", "26"):
                with self.subTest(schema=schema):
                    before = root / f"before-{schema}"
                    after = root / f"after-{schema}"
                    self.write_trace(
                        before,
                        "old",
                        "1.0",
                        11,
                        12,
                        timeboxed_open=True,
                        warmup_seconds=5,
                        trace_schema_version=schema,
                        counter_baseline_present=False,
                    )
                    self.write_trace(
                        after,
                        "new",
                        "1.1",
                        11,
                        12,
                        timeboxed_open=True,
                        warmup_seconds=5,
                        trace_schema_version=schema,
                        counter_baseline_present=True,
                    )
                    output = root / f"comparison-{schema}"
                    COMPARE.render_comparison(
                        [("before-1", before)],
                        [("after-1", after)],
                        output,
                    )
                    comparison = json.loads(
                        (output / "comparison.json").read_text(encoding="utf-8")
                    )
                    run_by_label = {
                        run["label"]: run for run in comparison["runs"]
                    }
                    self.assertEqual(
                        run_by_label["before-1"]["evidence_quality"],
                        "degraded",
                    )
                    self.assertFalse(
                        run_by_label["before-1"]["checks"]["counter_baseline_present"]
                    )
                    self.assertEqual(
                        run_by_label["after-1"]["evidence_quality"],
                        "healthy",
                    )
                    self.assertTrue(
                        run_by_label["after-1"]["checks"]["counter_baseline_present"]
                    )


    def test_final_netem_counters_do_not_sum_cumulative_snapshots(self):
        rows = [
            {"direction": "c2s", "received": "3", "forwarded": "2", "dropped": "1"},
            {"direction": "s2c", "received": "4", "forwarded": "4", "dropped": "0"},
            {"direction": "c2s", "received": "13", "forwarded": "10", "dropped": "3"},
            {"direction": "s2c", "received": "9", "forwarded": "8", "dropped": "1"},
        ]
        counters = COMPARE.final_netem_counters(rows)

        self.assertEqual(counters["c2s"]["received"], 13.0)
        self.assertEqual(counters["c2s"]["forwarded"], 10.0)
        self.assertEqual(counters["c2s"]["dropped"], 3.0)
        self.assertEqual(counters["s2c"]["received"], 9.0)
        self.assertEqual(counters["s2c"]["forwarded"], 8.0)
        self.assertEqual(counters["s2c"]["dropped"], 1.0)


    def test_raw_rtt_converts_trace_microseconds_to_report_milliseconds(self):
        rows = [
            {"raw_rtt_us": "250000"},
            {"raw_rtt_us": ""},
            {"raw_rtt_us": "125500"},
        ]
        self.assertEqual(COMPARE.raw_rtt_ms(rows), [125.5, 250.0])

    def test_run_health_reports_peer_upper_tail_quantiles(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12, timeboxed_open=True)
            self.write_trace(after, "new", "1.1", 11, 12, timeboxed_open=True)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )
            document = (output / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("RTT p50 / p90 / p99 ms", document)
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            summary = {
                run["label"]: run["summary"] for run in comparison["runs"]
            }["after-1"]
            self.assertEqual(summary["peer_rtt_p90_ms"], 100.0)
            self.assertEqual(summary["peer_rtt_p99_ms"], 100.0)

    def test_retransmission_rate_requires_delivered_application_bytes(self):
        self.assertEqual(COMPARE._per_gib(3, 1024 ** 3), 3.0)
        self.assertIsNone(COMPARE._per_gib(3, 0))


    def test_action_streak_reports_a_sampled_lower_bound(self):
        state = [
            {"time": 0.00, "action": "gentle_probe"},
            {"time": 0.05, "action": "gentle_probe"},
            {"time": 0.10, "action": "delay_drain"},
            {"time": 0.20, "action": "gentle_probe"},
            {"time": 0.50, "action": "gentle_probe"},
        ]
        self.assertAlmostEqual(
            COMPARE.action_streak_max_ms(state, "gentle_probe"),
            300.0,
        )


    def test_guidance_uses_metric_specific_direction(self):
        for metric in (
            "rtt_p50_ms",
            "low_send_rate_occupancy",
            "retransmission_attempts_per_gib_delivered",
            "retransmission_repeat_attempts_per_gib_delivered",
        ):
            self.assertEqual(COMPARE.metric_direction(metric, -1.0), "better")
            self.assertEqual(COMPARE.metric_direction(metric, 1.0), "worse")
        self.assertEqual(
            COMPARE.metric_direction("queue_building_occupancy", -1.0),
            "changed",
        )
        self.assertEqual(
            COMPARE.metric_direction("persistent_queue_resets", 1.0),
            "changed",
        )
        self.assertEqual(
            COMPARE.metric_direction("gentle_gate_open_streak_max_ms", -1.0),
            "changed",
        )

    def test_upper_tail_rtt_guidance_uses_lower_is_better_direction(self):
        for metric in ("rtt_p90_ms", "rtt_p99_ms"):
            self.assertEqual(COMPARE.metric_direction(metric, -1.0), "better")
            self.assertEqual(COMPARE.metric_direction(metric, 1.0), "worse")

    def test_guidance_preserves_a_change_from_a_zero_baseline(self):
        metric = "low_send_rate_occupancy"
        pair = {
            "metrics": {
                metric: {
                    "baseline": 0.0,
                    "candidate": 4.75,
                    "difference": 4.75,
                    "delta_percent": None,
                }
            }
        }
        hint = COMPARE.guidance_hints([pair], "no_material_change")[0]

        self.assertEqual(hint["metric"], metric)
        self.assertEqual(hint["direction"], "worse")
        self.assertEqual(hint["difference"], 4.75)
        self.assertIsNone(hint["delta_percent"])
        self.assertEqual(hint["ranking_delta_percent"], 100.0)
        self.assertEqual(
            COMPARE._guidance_change(hint),
            ", Δ +4.750; median rank +100.0%; changed 1/1; "
            "100% directional consistency",
        )


    def test_guidance_ranking_bounds_a_near_zero_baseline(self):
        pair = {
            "metrics": {
                "retransmission_repeat_attempts_per_gib_delivered": {
                    "baseline": 3.5,
                    "candidate": 50.0,
                    "difference": 46.5,
                    "delta_percent": 1328.5714285714287,
                },
                "goodput_mib_per_second": {
                    "baseline": 10.0,
                    "candidate": 0.0,
                    "difference": -10.0,
                    "delta_percent": -100.0,
                },
            }
        }
        hints = COMPARE.guidance_hints([pair], "mixed_results")

        self.assertEqual(hints[0]["metric"], "goodput_mib_per_second")
        self.assertEqual(hints[0]["ranking_delta_percent"], -100.0)
        repeat = hints[1]
        self.assertEqual(
            repeat["metric"],
            "retransmission_repeat_attempts_per_gib_delivered",
        )
        self.assertGreater(repeat["delta_percent"], 1000.0)
        self.assertAlmostEqual(repeat["ranking_delta_percent"], 93.0)


    def test_event_normalization_requires_a_counter_and_delivered_bytes(self):
        # Both a concrete counter and positive delivered bytes are required;
        # an absent counter or missing/zero bytes reject the normalization.
        self.assertIsNone(COMPARE._per_gib(None, 1024 ** 3))
        self.assertIsNone(COMPARE._per_gib(3.0, 0))
        self.assertIsNone(COMPARE._per_gib(3.0, None))
        self.assertEqual(COMPARE._per_gib(3.0, 1024 ** 3), 3.0)
        # A present zero counter with bytes is a real zero, while an absent
        # schema field stays unknown rather than being misread as zero.
        manifest = {
            "rtp_retransmission_armor_duplicates": "0",
            "delivered_bytes": str(1024 ** 3),
        }
        summary = COMPARE.summarize_run(manifest, [], [], [], [], [], [], [], [])
        self.assertEqual(summary["sender_retransmission_armor_duplicates"], 0.0)
        self.assertEqual(
            summary["sender_retransmission_armor_duplicates_per_gib_delivered"],
            0.0,
        )
        summary_absent = COMPARE.summarize_run({}, [], [], [], [], [], [], [], [])
        self.assertIsNone(summary_absent["sender_retransmission_armor_duplicates"])
        self.assertIsNone(
            summary_absent["sender_retransmission_armor_duplicates_per_gib_delivered"]
        )

    def test_guidance_ranks_median_pair_effect_not_one_pair_outlier(self):
        def pair(goodput_candidate, rare_candidate):
            return {
                "metrics": {
                    "goodput_mib_per_second": {
                        "baseline": 10.0,
                        "candidate": goodput_candidate,
                        "difference": goodput_candidate - 10.0,
                        "delta_percent": (goodput_candidate - 10.0) * 10.0,
                    },
                    "gentle_mode_exit_loss": {
                        "baseline": 0.0,
                        "candidate": rare_candidate,
                        "difference": rare_candidate,
                        "delta_percent": None,
                    },
                }
            }

        pairs = [pair(11.0, 1.0), pair(11.0, 0.0), pair(11.0, 0.0), pair(11.0, 0.0)]

        hints = COMPARE.guidance_hints(pairs, "mixed_results")

        self.assertEqual([hint["metric"] for hint in hints], ["goodput_mib_per_second"])
        self.assertEqual(hints[0]["pair_count"], 4)
        self.assertEqual(hints[0]["changed_pairs"], 4)
        self.assertEqual(hints[0]["directional_consistency_percent"], 100.0)


    def test_behavior_conditioning_separates_added_and_removed_events(self):
        def pair(gate_difference, goodput_delta_percent):
            metrics = {}
            for metric in COMPARE.CONDITIONING_METRICS:
                difference = gate_difference if metric.endswith("gate_open") else 0.0
                metrics[metric] = {
                    "baseline": 1.0,
                    "candidate": 1.0 + difference,
                    "difference": difference,
                    "delta_percent": difference * 100.0,
                }
            for metric in COMPARE.CONDITIONED_OUTCOME_METRICS:
                if metric in (
                    "goodput_mib_per_second",
                    "goodput_second_half_mib_per_second",
                ):
                    baseline = 10.0
                    candidate = baseline * (1.0 + goodput_delta_percent / 100.0)
                elif metric == "rtt_p50_ms":
                    baseline = 100.0
                    candidate = 99.0
                else:
                    baseline = 0.0
                    candidate = 0.0
                metrics[metric] = {
                    "baseline": baseline,
                    "candidate": candidate,
                    "difference": candidate - baseline,
                    "delta_percent": COMPARE.delta_percent(candidate, baseline),
                }
            return {"metrics": metrics}

        observations = COMPARE.behavior_conditioned_observations(
            [pair(1.0, -12.0), pair(1.0, -9.0), pair(-1.0, 20.0)]
        )
        groups = {
            (observation["metric"], observation["condition"]): observation
            for observation in observations
        }
        added = groups["gentle_mode_exit_gate_open", "candidate_higher"]
        self.assertEqual(added["pair_count"], 2)
        self.assertEqual(added["support"], "repeated")
        self.assertEqual(added["attention"], "consistent_material_goodput_shift")
        goodput = next(
            outcome
            for outcome in added["outcomes"]
            if outcome["metric"] == "goodput_mib_per_second"
        )
        self.assertAlmostEqual(goodput["delta_percent"], -10.5)
        self.assertEqual(goodput["direction"], "worse")
        self.assertEqual(goodput["directional_consistency_percent"], 100.0)

        removed = groups["gentle_mode_exit_gate_open", "candidate_lower"]
        self.assertEqual(removed["pair_count"], 1)
        self.assertEqual(removed["support"], "isolated")
        self.assertEqual(removed["attention"], "context_only")
        self.assertIn("does_not_prove", removed["does_not_prove"])

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


    def test_warmup_boundary_mismatch_excludes_pair(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(
                after,
                "new",
                "1.1",
                11,
                12,
                warmup_seconds=5,
            )
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
            self.assertIn(
                "warmup_seconds mismatch",
                comparison["pairs"][0]["excluded_reasons"],
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


    def test_missing_gentle_exit_counters_remain_unknown(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(after, "old", "1.0", 11, 12)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            metrics = comparison["pairs"][0]["metrics"]
            for cause in COMPARE.GENTLE_EXIT_CAUSES:
                metric = metrics[f"gentle_mode_exit_{cause}"]
                self.assertIsNone(metric["baseline"])
                self.assertIsNone(metric["candidate"])
                self.assertIsNone(metric["difference"])
            self.assertIn("n/a", (output / "comparison.html").read_text(encoding="utf-8"))

    @staticmethod
    def write_csv(path, *rows):
        if len(rows) == 1 and rows[0] and isinstance(rows[0][0], (list, tuple)):
            rows = rows[0]
        with path.open("w", newline="", encoding="utf-8") as output:
            csv.writer(output).writerows(rows)

    @staticmethod
    def state_row(**overrides):
        """Minimal summarize_run state row with every read key present."""
        row = {
            "time": 0.0,
            "send_rate": 128.0,
            "raw_rtt": None,
            "action": "none",
            "outage": False,
            "delivery_sample_app_limited": False,
            "application_write_waiters": None,
            "application_limited_detections": None,
            "application_limited_detections_suppressed_by_waiting_writer": None,
            "pending_send_bytes": None,
            "accepts_new_packet": None,
            "no_response": None,
            "no_progress": None,
            "retransmitted": 0.0,
            "retransmission_active": 0.0,
            "retransmission_ready": 0.0,
            "retransmission_attempts": 0.0,
            "retransmission_first_attempts": 0.0,
            "retransmission_repeat_attempts": 0.0,
            "retransmission_rto_reason": 0.0,
            "retransmission_reorder_reason": 0.0,
            "retransmission_fast_loss_reason": 0.0,
            "retransmission_pre_outage_reason": 0.0,
            "tail_probe_attempts": 0.0,
            "rto_postponements": None,
            "cc_rate_samples": 0.0,
            "cc_probe_decisions": 0.0,
            "cc_probe_increases": 0.0,
            "cc_probe_before_feedback": 0.0,
            "cc_persistent_queue_available": False,
            "cc_persistent_queue_for": None,
            "cc_persistent_queue_resets": None,
            "cc_delay_drains": 0.0,
            "cc_loss_backoff_floor": None,
            "cc_loss_backoff_raw": None,
            "cc_loss_backoff_target": None,
            "cc_loss_backoffs": 0.0,
            "cc_loss_backoff_floor_bindings": 0.0,
            "loss": None,
            "cc_loss": None,
            "event": "",
            "context": "unknown",
            **{field: None for field in COMPARE.FEC_COUNTER_FIELDS},
        }
        row.update(overrides)
        return row

    def test_fec_counters_are_normalized_without_inventing_legacy_zeroes(self):
        manifest = {"delivered_bytes": str(1024 ** 3)}
        summary = COMPARE.summarize_run(manifest, [], [], [], [], [], [], [], [])
        self.assertFalse(summary["fec_counters_present"])
        self.assertIsNone(summary["fec_counters"]["parity_sent"])
        self.assertIsNone(summary["fec_counters"]["recovered_symbols"])
        self.assertIsNone(summary["fec_parity_sent_per_gib_delivered"])
        self.assertIsNone(summary["fec_recovered_symbols_per_gib_delivered"])
        # Present typed counters with bytes normalize to real values; a
        # present zero stays a real zero, never a fabricated None.
        state = [
            self.state_row(
                fec_parity_sent=10.0,
                fec_groups_flushed=4.0,
                fec_recovered_symbols=2.0,
                fec_dropped_malformed_packets=0.0,
                fec_dropped_decoder_panics=0.0,
            )
        ]
        summary_fec = COMPARE.summarize_run(manifest, state, [], [], [], [], [], [], [])
        self.assertTrue(summary_fec["fec_counters_present"])
        self.assertEqual(summary_fec["fec_counters"]["parity_sent"], 10.0)
        self.assertEqual(summary_fec["fec_counters"]["recovered_symbols"], 2.0)
        self.assertEqual(summary_fec["fec_parity_sent_per_gib_delivered"], 10.0)
        self.assertEqual(summary_fec["fec_recovered_symbols_per_gib_delivered"], 2.0)
        self.assertEqual(summary_fec["fec_dropped_malformed_packets_per_gib_delivered"], 0.0)
        self.assertIsNone(
            summary_fec["fec_counters"]["groups_skipped_no_spare_capacity"]
        )
        # Wire cost stays None when the capture lacks forwarded_bytes, and
        # computes from the final per-direction totals when present.
        no_wire = COMPARE.summarize_run(
            manifest, [], [], [], [],
            [{"direction": "c2s", "received": "10", "forwarded": "9"}],
            [], [], [],
        )
        self.assertIsNone(no_wire["udp_payload_bytes_per_delivered_byte"])
        with_wire = COMPARE.summarize_run(
            manifest, [], [], [], [],
            [
                {
                    "direction": "c2s",
                    "received": "10",
                    "forwarded": "9",
                    "forwarded_bytes": "5000",
                    "scheduled_drain_packets": "4",
                },
                {
                    "direction": "s2c",
                    "received": "10",
                    "forwarded": "9",
                    "forwarded_bytes": "3000",
                    "scheduled_drain_packets": "6",
                },
            ],
            [], [], [],
        )
        self.assertEqual(
            with_wire["udp_payload_bytes_per_delivered_byte"],
            8000.0 / (1024 ** 3),
        )
        self.assertEqual(with_wire["scheduled_drain_packets"], 10.0)

    def test_wire_price_is_direction_honest_and_names_its_denominator(self):
        """The window price must be readable per direction, with the reverse
        direction's share visible instead of folded into one unlabelled sum
        over a denominator that counts a single direction's application
        bytes."""
        gib = 1024 ** 3
        summary = COMPARE.summarize_run(
            {"delivered_bytes": str(gib)},
            [], [], [], [],
            [
                {
                    "direction": "c2s",
                    "forwarded": "100",
                    "forwarded_bytes": "500000",
                },
                {
                    "direction": "s2c",
                    "forwarded": "20",
                    "forwarded_bytes": "1000",
                },
            ],
            [], [], [],
        )
        # The single legacy key summed both directions while the denominator
        # counted one direction's application bytes; it must not survive under
        # a name that a stale consumer could read as a different quantity.
        self.assertNotIn("wire_bytes_per_delivered_byte", summary)
        self.assertEqual(summary["wire_payload_direction"], "c2s")
        self.assertAlmostEqual(
            summary["forward_udp_payload_bytes_per_delivered_byte"],
            500000.0 / gib,
        )
        self.assertAlmostEqual(
            summary["reverse_udp_payload_bytes_per_delivered_byte"],
            1000.0 / gib,
        )
        self.assertAlmostEqual(
            summary["udp_payload_bytes_per_delivered_byte"],
            501000.0 / gib,
        )
        self.assertAlmostEqual(
            summary["reverse_udp_payload_share_percent"],
            100.0 * 1000.0 / 501000.0,
        )
        # The measured quantity is the UDP payload the proxy was handed. The
        # IP+UDP header cost is a separate estimate and says so: it names its
        # per-datagram constant and never borrows a measured name.
        self.assertEqual(
            summary["derived_ipv4_udp_header_bytes_per_datagram"], 28
        )
        self.assertAlmostEqual(
            summary["derived_ipv4_udp_header_bytes_per_delivered_byte"],
            28.0 * 120 / gib,
        )
        self.assertAlmostEqual(
            summary["derived_ipv4_udp_bytes_per_delivered_byte"],
            (501000.0 + 28.0 * 120) / gib,
        )
        # One direction only: the reverse side stays unavailable rather than
        # being invented as zero.
        single = COMPARE.summarize_run(
            {"delivered_bytes": "1000"},
            [], [], [], [],
            [{"direction": "c2s", "forwarded": "1", "forwarded_bytes": "1000"}],
            [], [], [],
        )
        self.assertEqual(single["wire_payload_direction"], "c2s")
        self.assertIsNone(single["reverse_udp_payload_bytes_per_delivered_byte"])
        self.assertIsNone(single["reverse_udp_payload_share_percent"])
        self.assertEqual(single["udp_payload_bytes_per_delivered_byte"], 1.0)

    def test_wire_window_price_denominator_matches_the_tick_span(self):
        """netem.csv and progress.csv are written from one observation list,
        so the wire numerator's span is the tick span and its denominator must
        be the delivered bytes over that same span, not the manifest's whole
        window."""
        netem = [
            {"elapsed_us": "0", "direction": "c2s", "forwarded": "1",
             "forwarded_bytes": "0"},
            {"elapsed_us": "0", "direction": "s2c", "forwarded": "1",
             "forwarded_bytes": "0"},
            {"elapsed_us": "20000000", "direction": "c2s", "forwarded": "10",
             "forwarded_bytes": "81920"},
            {"elapsed_us": "20000000", "direction": "s2c", "forwarded": "10",
             "forwarded_bytes": "640"},
        ]
        summary = COMPARE.summarize_run(
            {"delivered_bytes": "90000"},
            [], [], [], [],
            netem,
            [(0.0, 4096.0), (20.0, 80000.0)],
            [], [],
        )
        self.assertEqual(summary["wire_window_source"], "netem_tick_span")
        self.assertEqual(
            summary["wire_window_delivered_bytes"], 80000.0 - 4096.0
        )
        self.assertNotEqual(
            summary["wire_window_delivered_bytes"], summary["delivered_bytes"]
        )
        self.assertAlmostEqual(
            summary["udp_payload_bytes_per_delivered_byte"],
            82560.0 / (80000.0 - 4096.0),
        )

    def test_wire_window_denominator_falls_back_and_reports_its_source(self):
        """Without a confirmable netem/progress pairing the manifest window is
        the denominator, the source field says so, and a capture whose counters
        still carry the unmeasured warmup is flagged instead of trusted."""
        unpaired = COMPARE.summarize_run(
            {"delivered_bytes": "90000"},
            [], [], [], [],
            [
                {"elapsed_us": "0", "direction": "c2s", "forwarded": "1",
                 "forwarded_bytes": "0"},
                {"elapsed_us": "20000000", "direction": "c2s",
                 "forwarded": "10", "forwarded_bytes": "81920"},
            ],
            [(0.0, 4096.0)],
            [], [],
        )
        self.assertEqual(unpaired["wire_window_source"], "manifest_window")
        self.assertEqual(unpaired["wire_window_delivered_bytes"], 90000.0)
        self.assertTrue(unpaired["netem_forwarded_bytes_window_relative"])
        warmup_charged = COMPARE.summarize_run(
            {"delivered_bytes": "90000"},
            [], [], [], [],
            [
                {"elapsed_us": "0", "direction": "c2s", "forwarded": "999",
                 "forwarded_bytes": "8000000"},
                {"elapsed_us": "20000000", "direction": "c2s",
                 "forwarded": "1000", "forwarded_bytes": "8010000"},
            ],
            [(0.0, 0.0), (20.0, 90000.0)],
            [], [],
        )
        self.assertFalse(
            warmup_charged["netem_forwarded_bytes_window_relative"]
        )
        # A capture that never recorded the column reports nothing rather
        # than a fabricated zero or a fabricated marker.
        absent = COMPARE.summarize_run(
            {"delivered_bytes": "1000"},
            [], [], [], [],
            [{"direction": "c2s", "forwarded": "1"}],
            [], [], [],
        )
        self.assertIsNone(absent["netem_forwarded_bytes_window_relative"])
        self.assertIsNone(absent["udp_payload_bytes_per_delivered_byte"])

    def test_wire_metrics_are_compared_and_lower_is_better(self):
        """The direction-honest components are first-class comparison
        metrics: a reader sees their paired change, and a rise in any of them
        reads as worse rather than as an unclassified change."""
        for metric in (
            "udp_payload_bytes_per_delivered_byte",
            "reverse_udp_payload_bytes_per_delivered_byte",
            "derived_ipv4_udp_header_bytes_per_delivered_byte",
        ):
            self.assertIn(metric, COMPARE.METRICS)
            self.assertIn(metric, COMPARE.LOWER_IS_BETTER_METRICS)
            self.assertEqual(COMPARE.metric_direction(metric, 1.0), "worse")
            self.assertEqual(COMPARE.metric_direction(metric, -1.0), "better")
        self.assertNotIn("wire_bytes_per_delivered_byte", COMPARE.METRICS)

    def add_fec_snapshot(self, trace_dir, parity, recovered):
        """Add trace-schema-32 FEC snapshot columns to both endpoint RTP CSVs.

        The FEC counters are state-row snapshot columns, not manifest keys, so
        a comparison only sees them when the RTP CSV carries the columns.
        """
        for name in ("rtp.csv", "rtp_peer.csv"):
            path = Path(trace_dir) / name
            with path.open(newline="") as source:
                rows = list(csv.DictReader(source))
            fieldnames = list(rows[0])
            for field in COMPARE.FEC_COUNTER_FIELDS:
                if field not in fieldnames:
                    fieldnames.append(field)
            values = {field: "0" for field in COMPARE.FEC_COUNTER_FIELDS}
            values["fec_parity_sent"] = str(parity)
            values["fec_groups_flushed"] = "12"
            values["fec_recovered_symbols"] = str(recovered)
            for row in rows:
                row.update(values)
            with path.open("w", newline="") as sink:
                writer = csv.DictWriter(sink, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

    def test_fec_enabled_arm_flows_through_metrics_and_guidance(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(after, "new", "1.1", 11, 12)
            self.add_fec_snapshot(before, parity=200, recovered=2)
            self.add_fec_snapshot(after, parity=100, recovered=8)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)], [("after-1", after)], output
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            # A real FEC capture must surface present counters, never None.
            for run in comparison["runs"]:
                self.assertTrue(run["summary"]["fec_counters_present"])
            pair = comparison["pairs"][0]["metrics"]
            self.assertIsNotNone(
                pair["fec_parity_sent_per_gib_delivered"]["baseline"]
            )
            self.assertIsNotNone(
                pair["fec_parity_sent_per_gib_delivered"]["candidate"]
            )
            hints = {hint["metric"]: hint for hint in comparison["agent_guidance"]}
            # Parity cost is lower-is-better; the candidate sent half.
            self.assertEqual(
                hints["fec_parity_sent_per_gib_delivered"]["direction"], "better"
            )
            # Recovered symbols are direction-neutral diagnostics.
            self.assertEqual(
                hints["fec_recovered_symbols_per_gib_delivered"]["direction"],
                "changed",
            )

    def test_comparison_cli_exit_code_reflects_the_evidence(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            good_before = root / "gb"
            good_after = root / "ga"
            self.write_trace(good_before, "b", "1.0", 11, 12)
            self.write_trace(good_after, "a", "1.1", 11, 12)
            broken = root / "broken"
            self.write_trace(broken, "x", "1.0", 11, 12, broken=True)

            def run(baseline, candidate, out, *extra):
                stderr = io.StringIO()
                argv = [
                    "--baseline", f"b-1={baseline}",
                    "--candidate", f"c-1={candidate}",
                    "--out", str(root / out),
                ] + list(extra)
                with redirect_stderr(stderr):
                    code = COMPARE.main(argv)
                return code, stderr.getvalue()

            healthy_code, healthy_err = run(good_before, good_after, "o1")
            self.assertEqual(healthy_code, 0)
            self.assertEqual(healthy_err, "")

            invalid_code, invalid_err = run(broken, broken, "o2")
            self.assertEqual(invalid_code, 2)
            self.assertIn("refusing to report success", invalid_err)
            self.assertIn("insufficient_evidence", invalid_err)
            # The artifacts are still written so a caller can inspect them.
            self.assertTrue((root / "o2" / "comparison.json").is_file())

            # --report-only is the explicit opt-out for a caller that wants
            # the JSON on invalid input.
            report_code, report_err = run(broken, broken, "o3", "--report-only")
            self.assertEqual(report_code, 0)
            self.assertEqual(report_err, "")

    def test_explicit_fec_treatment_allows_only_its_declared_difference(self):
        baseline = {
            "warmup_seconds": "0",
            "window_seconds": "30",
            "mss_bytes": "8192",
            "retransmission_armor": "false",
            "rtp_handshake": "false",
            "fec": "false",
            "instream_group_fec": "false",
            "netem_c2s": "cfg-a",
            "netem_s2c": "cfg-b",
            "link_d": "link",
            "perf_loop_profile": "release",
        }
        candidate = {**baseline, "fec": "true", "instream_group_fec": "true"}
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, candidate),
            ["fec", "instream_group_fec"],
        )
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, candidate, ("fec",)),
            ["instream_group_fec"],
        )
        self.assertEqual(
            COMPARE.pair_config_agrees(
                baseline, candidate, ("fec", "instream_group_fec")
            ),
            [],
        )
        # An unrelated difference stays a mismatch even when FEC is allowed.
        other = {**candidate, "mss_bytes": "4096"}
        self.assertEqual(
            COMPARE.pair_config_agrees(
                baseline, other, ("fec", "instream_group_fec")
            ),
            ["mss_bytes"],
        )

    def test_declared_netem_field_admits_only_its_exact_delta(self):
        raw = (
            "NetemConfig { latency: 300ms, jitter: 0ns, delay_corr: 0, "
            "loss: 214748364, loss_corr: 0, duplicate: 0, dup_corr: 0, "
            "reorder: 0, reorder_corr: 0, reorder_gap_pkts: 0, "
            "loss_model: PacketKeyed { key_offset: 1 }, rate: 50000000, "
            "seed: 11, queue_limit_pkts: 1024, max_datagram_size: 0 }"
        )
        enveloped = raw.replace("key_offset: 1 }", "key_offset: 11 }")
        baseline = {
            "warmup_seconds": "0",
            "window_seconds": "30",
            "mss_bytes": "8192",
            "retransmission_armor": "false",
            "rtp_handshake": "false",
            "fec": "false",
            "instream_group_fec": "false",
            "netem_c2s": raw,
            "netem_s2c": raw,
            "link_d": "link",
            "perf_loop_profile": "release",
        }
        candidate = {
            **baseline,
            "fec": "true",
            "netem_c2s": enveloped,
            "netem_s2c": enveloped,
        }
        declared = {
            "netem_c2s.loss_model.key_offset": 10,
            "netem_s2c.loss_model.key_offset": 10,
        }
        # The deliberate FEC-envelope key shift is an undeclared mismatch until
        # the treatment names exactly that leaf.
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, candidate, ("fec",)),
            [
                "netem_c2s.loss_model.key_offset",
                "netem_s2c.loss_model.key_offset",
            ],
        )
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, candidate, ("fec",), declared),
            [],
        )
        # The declared leaf is pinned to an exact signed delta.
        wrong = {
            "netem_c2s.loss_model.key_offset": 9,
            "netem_s2c.loss_model.key_offset": 9,
        }
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, candidate, ("fec",), wrong),
            [
                "netem_c2s.loss_model.key_offset",
                "netem_s2c.loss_model.key_offset",
            ],
        )
        # An undeclared netem difference on the same lane is still rejected and
        # named by its dotted field.
        shifted = enveloped.replace("latency: 300ms", "latency: 250ms")
        undeclared = {**candidate, "netem_c2s": shifted, "netem_s2c": shifted}
        self.assertEqual(
            COMPARE.pair_config_agrees(baseline, undeclared, ("fec",), declared),
            ["netem_c2s.latency", "netem_s2c.latency"],
        )

    def test_netem_difference_that_cannot_be_parsed_stays_a_whole_key_mismatch(self):
        base = {
            "retransmission_armor": "false",
            "netem_c2s": "NetemConfig { latency: 1ms }",
            "netem_s2c": "x",
        }
        self.assertEqual(
            COMPARE.pair_config_agrees(base, {**base, "netem_c2s": "not-a-config"}),
            ["netem_c2s"],
        )

    def test_parse_netem_fields_keeps_the_variant_and_leaf_paths(self):
        fields = COMPARE.parse_netem_fields(
            "NetemConfig { rate: 5, loss_model: PacketKeyed { key_offset: 11 } }"
        )
        self.assertEqual(fields["rate"], "5")
        self.assertEqual(fields["loss_model"], "PacketKeyed")
        self.assertEqual(fields["loss_model.key_offset"], "11")
        self.assertIsNone(COMPARE.parse_netem_fields(""))
        self.assertIsNone(COMPARE.parse_netem_fields("NetemConfig { broken }"))

    def test_allow_config_field_parser_requires_a_signed_integer(self):
        self.assertEqual(
            COMPARE.parse_config_field("netem_c2s.loss_model.key_offset=10"),
            ("netem_c2s.loss_model.key_offset", 10),
        )
        self.assertEqual(
            COMPARE.parse_config_field("netem_s2c.loss_model.key_offset=-10"),
            ("netem_s2c.loss_model.key_offset", -10),
        )
        with self.assertRaises(Exception):
            COMPARE.parse_config_field("netem_c2s.latency")
        with self.assertRaises(Exception):
            COMPARE.parse_config_field("netem_c2s.latency=fast")

    def test_retransmission_armor_mismatch_is_configuration_mismatch(self):
        base = {"retransmission_armor": "false"}
        self.assertEqual(
            COMPARE.pair_config_agrees(base, {"retransmission_armor": "true"}),
            ["retransmission_armor"],
        )
        # Absent armor against an explicit value is a mismatch, and a
        # non-boolean value is a mismatch too.
        self.assertEqual(
            COMPARE.pair_config_agrees(base, {}),
            ["retransmission_armor"],
        )
        self.assertEqual(
            COMPARE.pair_config_agrees(base, {"retransmission_armor": "maybe"}),
            ["retransmission_armor"],
        )
        # Only the explicit allowlist can suppress the armor key.
        self.assertEqual(
            COMPARE.pair_config_agrees(base, {}, ("retransmission_armor",)),
            [],
        )

    def test_unknown_rows_stay_out_of_clean(self):
        state = [
            self.state_row(loss=0.0, context="clean"),
            self.state_row(loss=0.0, context="clean"),
            self.state_row(loss=None, context="unknown"),
            self.state_row(loss=0.02, context="positive_loss"),
        ]
        summary = COMPARE.summarize_run(
            {"delivered_bytes": str(1024 ** 3)}, state, [], [], [], [], [], [], []
        )
        context = summary["context"]
        self.assertEqual(context["known_rows"], 3)
        self.assertEqual(context["unknown_rows"], 1)
        self.assertEqual(context["coverage_percent"], 75.0)
        self.assertAlmostEqual(context["clean_occupancy"], 100.0 * 2 / 3)
        self.assertAlmostEqual(context["positive_loss_occupancy"], 100.0 * 1 / 3)

    def test_sparse_message_tail_and_wire_tradeoff_is_not_neutral(self):
        def pair(p95, p99, wire, goodput_delta):
            return {
                "baseline": {
                    "manifest": {"scenario": "mux_over_rtp_hostile_message_latency"}
                },
                "metrics": {
                    "message_latency_p95_ms": {"delta_percent": p95},
                    "message_latency_p99_ms": {"delta_percent": p99},
                    "udp_payload_bytes_per_delivered_byte": {
                        "delta_percent": wire
                    },
                    "goodput_mib_per_second": {"delta_percent": goodput_delta},
                },
            }

        # Tail improves materially while wire cost regresses: a latency/
        # overhead tradeoff is mixed, never neutral, even though goodput
        # barely moved.
        pairs = [
            pair(-25.0, -30.0, 15.0, 0.5),
            pair(-20.0, -22.0, 18.0, -0.3),
        ]
        self.assertEqual(COMPARE.classify(pairs), "mixed_results")
        self.assertNotEqual(COMPARE.classify(pairs), "no_material_change")
        # Clean improvement on both axes.
        self.assertEqual(
            COMPARE.classify([pair(-25.0, -30.0, -15.0, 0.5)]),
            "likely_improvement",
        )
        # Neither tail nor wire moves materially.
        self.assertEqual(
            COMPARE.classify([pair(-1.0, 2.0, 3.0, 0.0)]),
            "no_material_change",
        )

    def test_controller_activation_coverage_separates_pair_states(self):
        def run(label, gentle, hold):
            return {
                "label": label,
                "summary": {
                    "controller_state_occupancy": {
                        "slow_start": 0.0,
                        "gentle_mode": gentle,
                        "gentle_draining": 0.0,
                        "queue_building": 0.0,
                        "drain_floor_binding": 0.0,
                        "outage_recovery": 0.0,
                    },
                    "queue_hold_occupancy": hold,
                    "delay_drain_occupancy": 0.0,
                },
            }

        pairs = [
            {"baseline": run("b1", 100.0, 0.0), "candidate": run("c1", 80.0, 0.0)},
            {"baseline": run("b2", 100.0, 0.0), "candidate": run("c2", 0.0, 0.0)},
            {"baseline": run("b3", 0.0, 0.0), "candidate": run("c3", 50.0, 0.0)},
            {"baseline": run("b4", 0.0, 0.0), "candidate": run("c4", 0.0, 0.0)},
            {
                "baseline": run("b5", float("nan"), 0.0),
                "candidate": run("c5", 0.0, 0.0),
            },
        ]
        coverage = COMPARE.controller_activation_coverage(pairs)
        gentle = coverage["gentle_mode"]
        self.assertEqual(
            gentle["counts"],
            {
                "both_active": 1,
                "baseline_only": 1,
                "candidate_only": 1,
                "neither_active": 1,
                "unknown": 1,
            },
        )
        self.assertEqual(len(gentle["pairs"]), 5)
        self.assertEqual(
            [item["state"] for item in gentle["pairs"]],
            [
                "both_active",
                "baseline_only",
                "candidate_only",
                "neither_active",
                "unknown",
            ],
        )
        # queue_hold_occupancy is read from the summary directly and stays
        # inactive for every pair.
        hold = coverage["queue_hold_occupancy"]
        self.assertEqual(hold["counts"]["neither_active"], 5)

    def test_pairwise_rtt_cdf_skips_invalid_pairs(self):
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
            document = (output / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("No valid paired RTT samples.", document)
            self.assertIn("does_not_prove", document)

            # A valid pair renders one baseline/candidate CDF panel.
            self.write_trace(root / "after2", "new", "1.2", 11, 12)
            output2 = root / "comparison2"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", root / "after2")],
                output2,
            )
            document2 = (output2 / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("Paired raw RTT CDF: before-1 vs after-1", document2)
            self.assertNotIn("No valid paired RTT samples.", document2)

    def test_render_emits_activation_coverage_and_allowlist(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "old", "1.0", 11, 12)
            self.write_trace(after, "new", "1.2", 11, 12)
            output = root / "comparison"
            COMPARE.render_comparison(
                [("before-1", before)],
                [("after-1", after)],
                output,
                ("fec",),
            )
            comparison = json.loads(
                (output / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(comparison["allowed_config_mismatches"], ["fec"])
            self.assertIn("controller_activation_coverage", comparison)
            self.assertEqual(
                comparison["controller_activation_coverage"]["gentle_mode"]["counts"]["unknown"],
                0,
            )
            document = (output / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("Comparison readiness", document)
            self.assertIn("Controller activation coverage", document)


    def test_ack_flush_claim_reasons_are_normalized_and_direction_neutral(self):
        manifest = {
            "rtp_ack_flush_initial_claims": "10",
            "rtp_ack_flush_age_claims": "30",
            "rtp_ack_flush_count_claims": "40",
            "rtp_ack_flush_fin_claims": "15",
            "rtp_ack_flush_explicit_claims": "5",
            "rtp_peer_ack_flush_initial_claims": "2",
            "rtp_peer_ack_flush_age_claims": "8",
            "rtp_peer_ack_flush_count_claims": "6",
            "rtp_peer_ack_flush_fin_claims": "2",
            "rtp_peer_ack_flush_explicit_claims": "2",
            "rtp_send_driver_resume_ack_flush_requests": "500",
            "rtp_peer_send_driver_resume_ack_flush_requests": "300",
            "delivered_bytes": str(1024 ** 3),
        }
        summary = COMPARE.summarize_run(manifest, [], [], [], [], [], [], [], [])
        self.assertEqual(summary["ack_flush_claims_total"], 100.0)
        self.assertEqual(summary["peer_ack_flush_claims_total"], 20.0)
        self.assertEqual(summary["ack_flush_claims_total_per_gib_delivered"], 100.0)
        self.assertEqual(summary["peer_ack_flush_claims_total_per_gib_delivered"], 20.0)
        for reason, count in (
            ("initial", 10.0),
            ("age", 30.0),
            ("count", 40.0),
            ("fin", 15.0),
            ("explicit", 5.0),
        ):
            self.assertEqual(summary[f"ack_flush_{reason}_claims"], count)
            self.assertEqual(
                summary[f"ack_flush_{reason}_claims_per_gib_delivered"], count
            )
        for reason, share in (
            ("initial", 0.10),
            ("age", 0.30),
            ("count", 0.40),
            ("fin", 0.15),
            ("explicit", 0.05),
        ):
            self.assertAlmostEqual(summary[f"ack_flush_{reason}_claim_share"], share)
        for reason, count in (
            ("initial", 2.0),
            ("age", 8.0),
            ("count", 6.0),
            ("fin", 2.0),
            ("explicit", 2.0),
        ):
            self.assertEqual(summary[f"peer_ack_flush_{reason}_claims"], count)
            self.assertEqual(
                summary[f"peer_ack_flush_{reason}_claims_per_gib_delivered"], count
            )
        for reason, share in (
            ("initial", 0.10),
            ("age", 0.40),
            ("count", 0.30),
            ("fin", 0.10),
            ("explicit", 0.10),
        ):
            self.assertAlmostEqual(
                summary[f"peer_ack_flush_{reason}_claim_share"], share
            )
        self.assertEqual(summary["send_driver_resume_requests"]["ack_flush"], 500.0)
        self.assertEqual(
            summary["peer_send_driver_resume_requests"]["ack_flush"], 300.0
        )
        for metric in COMPARE.ACK_FLUSH_METRICS:
            self.assertIn(metric, COMPARE.METRICS)
            self.assertIn(metric, COMPARE.NEUTRAL_DIRECTION_METRICS)
            self.assertEqual(COMPARE.metric_direction(metric, 1.0), "changed")
            self.assertEqual(COMPARE.metric_direction(metric, -1.0), "changed")
        zero = {
            f"rtp_ack_flush_{reason}_claims": "0"
            for reason in COMPARE.ACK_FLUSH_REASONS
        }
        summary_zero = COMPARE.summarize_run(
            {**zero, "delivered_bytes": str(1024 ** 3)},
            [], [], [], [], [], [], [], [],
        )
        self.assertEqual(summary_zero["ack_flush_claims_total"], 0.0)
        self.assertEqual(summary_zero["ack_flush_initial_claim_share"], 0.0)
        summary_absent = COMPARE.summarize_run({}, [], [], [], [], [], [], [], [])
        self.assertEqual(summary_absent["ack_flush_initial_claim_share"], 0.0)
        self.assertIsNone(summary_absent["ack_flush_initial_claims"])

    def test_split_window_goodput_uses_the_exact_midpoint(self):
        mib = 1024 * 1024
        first, second = COMPARE.split_window_goodput(
            [(0.0, 0.0), (9.8, 9.8 * mib), (10.2, 10.6 * mib), (20.0, 40 * mib)],
            20.0,
            40 * mib,
        )
        self.assertAlmostEqual(first, 1.02)
        self.assertAlmostEqual(second, 2.98)
        first, second = COMPARE.split_window_goodput(
            [(0.0, 0.0), (10.25, 10.25 * mib), (20.5, 20.5 * mib)],
            20.5,
            20.5 * mib,
        )
        self.assertAlmostEqual(first, 1.0)
        self.assertAlmostEqual(second, 1.0)
        self.assertEqual(
            COMPARE.split_window_goodput(
                [(12.0, 12 * mib), (20.0, 40 * mib)], 20.0, 40 * mib
            ),
            (None, None),
        )
        self.assertEqual(
            COMPARE.split_window_goodput(
                [(0.0, 0.0), (10.8, 10.8 * mib), (20.0, 40 * mib)],
                20.0,
                40 * mib,
            ),
            (None, None),
        )

    def test_event_normalization_requires_a_counter_and_delivered_bytes(self):
        self.assertIsNone(COMPARE._per_gib(None, 1024 ** 3))
        self.assertIsNone(COMPARE._per_gib(3.0, 0))
        self.assertIsNone(COMPARE._per_gib(3.0, None))
        self.assertEqual(COMPARE._per_gib(3.0, 1024 ** 3), 3.0)
        manifest = {
            "rtp_retransmission_armor_duplicates": "0",
            "delivered_bytes": str(1024 ** 3),
        }
        summary = COMPARE.summarize_run(manifest, [], [], [], [], [], [], [], [])
        self.assertEqual(summary["sender_retransmission_armor_duplicates"], 0.0)
        self.assertEqual(
            summary["sender_retransmission_armor_duplicates_per_gib_delivered"],
            0.0,
        )
        summary_absent = COMPARE.summarize_run({}, [], [], [], [], [], [], [], [])
        self.assertIsNone(summary_absent["sender_retransmission_armor_duplicates"])
        self.assertIsNone(
            summary_absent["sender_retransmission_armor_duplicates_per_gib_delivered"]
        )
        claims = {
            f"rtp_ack_flush_{reason}_claims": "4"
            for reason in COMPARE.ACK_FLUSH_REASONS
        }
        summary_with_bytes = COMPARE.summarize_run(
            {**claims, "delivered_bytes": str(1024 ** 3)},
            [], [], [], [], [], [], [], [],
        )
        self.assertEqual(summary_with_bytes["ack_flush_claims_total"], 20.0)
        self.assertEqual(
            summary_with_bytes["ack_flush_claims_total_per_gib_delivered"], 20.0
        )
        summary_no_bytes = COMPARE.summarize_run(
            claims, [], [], [], [], [], [], [], []
        )
        self.assertEqual(summary_no_bytes["ack_flush_claims_total"], 20.0)
        self.assertIsNone(summary_no_bytes["ack_flush_claims_total_per_gib_delivered"])

    def test_csv_rows_well_formed_rejects_short_control_and_non_finite_fields(self):
        self.assertTrue(
            COMPARE.csv_rows_well_formed(
                [{"a": "1", "b": "", "c": "true", "d": "1.5e3", "e": "x"}]
            )
        )
        # A short row (missing trailing field) is None under DictReader.
        self.assertFalse(COMPARE.csv_rows_well_formed([{"a": "1", "b": None}]))
        self.assertFalse(COMPARE.csv_rows_well_formed([{"a": "nan"}]))
        self.assertFalse(COMPARE.csv_rows_well_formed([{"a": "inf"}]))
        self.assertFalse(COMPARE.csv_rows_well_formed([{"a": "-infinity"}]))
        self.assertFalse(COMPARE.csv_rows_well_formed([{"a": "x\x00y"}]))

    def _corrupt_healthy_trace(self, trace, filename, mutate):
        self.write_trace(trace, "cand-1", "1.0", 11, 12)
        path = trace / filename
        rows = list(csv.reader(path.open(newline="")))
        mutate(rows)
        self.write_csv(path, rows)
        return COMPARE.read_run(("cand-1", trace))

    def test_non_finite_numeric_field_invalidates_the_trace(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            trace = Path(directory) / "trace"

            def mutate(rows):
                rows[1][rows[0].index("delivered_bytes")] = "nan"

            run = self._corrupt_healthy_trace(trace, "progress.csv", mutate)
            self.assertFalse(run["health"]["checks"]["csv_fields_well_formed"])
            self.assertEqual(run["health"]["evidence_quality"], "invalid")

    def test_short_csv_row_invalidates_the_trace(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            trace = Path(directory) / "trace"
            run = self._corrupt_healthy_trace(
                trace, "rtp.csv", lambda rows: rows.__setitem__(1, rows[1][:-1])
            )
            self.assertFalse(run["health"]["checks"]["csv_fields_well_formed"])
            self.assertEqual(run["health"]["evidence_quality"], "invalid")

    def test_control_character_in_csv_field_invalidates_the_trace(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            trace = Path(directory) / "trace"
            run = self._corrupt_healthy_trace(
                trace,
                "rtp.csv",
                lambda rows: rows[1].__setitem__(0, rows[1][0] + "\x00"),
            )
            self.assertFalse(run["health"]["checks"]["csv_fields_well_formed"])
            self.assertEqual(run["health"]["evidence_quality"], "invalid")

    def test_render_refuses_a_graphless_comparison_with_valid_pairs(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            output = Path(directory) / "out"
            with mock.patch.object(
                COMPARE,
                "build_comparison",
                lambda *a, **k: {"valid_pairs": 1, "pairs": [], "runs": []},
            ), mock.patch.object(
                COMPARE, "render_html", lambda *a, **k: "<html></html>"
            ):
                with self.assertRaises(ValueError) as context:
                    COMPARE.render_comparison([], [], output)
            self.assertIn("no graph panel", str(context.exception))

    def test_render_writes_graph_panels_for_a_healthy_pair(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            self.write_trace(before, "base-1", "1.0", 11, 12)
            self.write_trace(after, "cand-1", "1.1", 11, 12)
            output = root / "out"
            COMPARE.render_comparison(
                [("base-1", before)], [("cand-1", after)], output
            )
            content = (output / "comparison.html").read_text(encoding="utf-8")
            self.assertIn("<svg", content)

    def test_bulk_mixed_material_deltas_are_mixed_results(self):
        """A pair set split between material improvement and regression is
        mixed, never no_material_change, however large the two deltas are."""

        def pair(delta):
            return {
                "baseline": {"manifest": {"scenario": "bulk_clean"}},
                "metrics": {
                    "goodput_mib_per_second": {"delta_percent": delta},
                },
            }

        pairs = [pair(50.0), pair(-50.0)]
        self.assertEqual(COMPARE.classify_bulk_scenario(pairs), "mixed_results")
        self.assertEqual(COMPARE.classify(pairs), "mixed_results")

    def bulk_pair(
        self,
        goodput_percent,
        rtt_p50=None,
        rtt_p90=None,
        rtt_p99=None,
    ):
        """One valid bulk pair. Goodput is given as a delta, RTT percentiles
        as raw (baseline_ms, candidate_ms) endpoints -- endpoints rather than
        deltas so the absolute-millisecond floor is exercised exactly as the
        classifier reads it. A None percentile is absent from the pair."""

        def endpoint(baseline, candidate):
            return {
                "baseline": baseline,
                "candidate": candidate,
                "difference": candidate - baseline,
                "delta_percent": (candidate - baseline) / abs(baseline) * 100.0,
            }

        metrics = {
            "goodput_mib_per_second": endpoint(
                100.0, 100.0 * (1.0 + goodput_percent / 100.0)
            ),
        }
        metrics["goodput_mib_per_second"]["delta_percent"] = goodput_percent
        for name, points in (
            ("rtt_p50_ms", rtt_p50),
            ("rtt_p90_ms", rtt_p90),
            ("rtt_p99_ms", rtt_p99),
        ):
            if points is not None:
                metrics[name] = endpoint(*points)
        return {
            "baseline": {"manifest": {"scenario": "bulk_clean"}},
            "metrics": metrics,
        }

    def test_bulk_shifted_rtt_with_flat_goodput_is_not_neutral(self):
        """A CDF that separates into two clusters while goodput stays flat is
        a material latency improvement, never no_material_change. The numbers
        are the preserved baseline-redesign pair: p50 343->324 ms, p90
        383->347 ms, p99 392->354 ms, goodput -0.8%."""
        pairs = [
            self.bulk_pair(-0.84, (343.2, 324.2), (382.5, 346.5), (392.5, 353.5)),
            self.bulk_pair(-0.77, (344.5, 324.3), (382.6, 348.0), (392.4, 353.7)),
        ]
        verdict = COMPARE.classify_bulk_scenario(pairs)
        self.assertEqual(verdict, "latency_improvement")
        self.assertNotEqual(verdict, "no_material_change")
        self.assertNotEqual(verdict, "likely_regression")

    def test_bulk_goodput_only_change_keeps_its_verdict(self):
        """A flat RTT CDF must leave the throughput axis alone: the goodput
        verdicts and the mixed split survive unchanged."""
        flat = ((350.0, 351.0), (383.0, 384.0), (393.0, 394.0))
        moved = [
            self.bulk_pair(15.0, *flat),
            self.bulk_pair(22.0, *flat),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(moved), "likely_improvement"
        )
        regressed = [
            self.bulk_pair(-15.0, *flat),
            self.bulk_pair(-22.0, *flat),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(regressed), "likely_regression"
        )
        split = [self.bulk_pair(50.0, *flat), self.bulk_pair(-50.0, *flat)]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(split), "mixed_results"
        )
        unchanged = [self.bulk_pair(1.0, *flat), self.bulk_pair(-1.0, *flat)]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(unchanged), "no_material_change"
        )

    def test_bulk_rtt_regression_is_flagged_not_ignored(self):
        """The same materiality rule fires in the other direction with a flat
        goodput, and a cross-axis trade is mixed rather than either
        likely_improvement or likely_regression."""
        regressed = [
            self.bulk_pair(-0.5, (343.0, 372.0), (383.0, 415.0), (393.0, 425.0)),
            self.bulk_pair(0.4, (344.0, 371.0), (382.0, 414.0), (392.0, 424.0)),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(regressed), "latency_regression"
        )
        # Goodput improves materially while the CDF moves the wrong way.
        trade = [
            self.bulk_pair(15.0, (343.0, 372.0), (383.0, 415.0), (393.0, 425.0)),
            self.bulk_pair(18.0, (344.0, 371.0), (382.0, 414.0), (392.0, 424.0)),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(trade), "mixed_results"
        )

    def test_bulk_movement_inside_the_measured_spread_stays_neutral(self):
        """Neither a sub-threshold relative movement nor a percentage that
        crosses the threshold on a sub-millisecond `clean`-lane RTT is
        material: the relative rule alone would fire on scheduler jitter."""
        below_percent = [
            self.bulk_pair(0.2, (343.0, 356.0), (383.0, 397.0), (393.0, 407.0)),
            self.bulk_pair(-0.3, (344.0, 357.0), (382.0, 396.0), (392.0, 406.0)),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(below_percent),
            "no_material_change",
        )
        clean_jitter = [
            self.bulk_pair(0.1, (0.0760, 0.0800), (0.1160, 0.1330), (0.1897, 0.3778)),
            self.bulk_pair(-0.2, (0.0770, 0.0770), (0.1160, 0.1123), (0.2310, 0.2065)),
        ]
        self.assertEqual(
            COMPARE.classify_bulk_scenario(clean_jitter), "no_material_change"
        )
        self.assertEqual(COMPARE.latency_direction(clean_jitter), "unchanged")

    def test_message_mixed_wire_with_unchanged_tail_is_mixed_results(self):
        """A wire-byte series that moved both ways is a material tradeoff, not
        a likely regression, when the latency tail did not move."""

        def pair(wire):
            return {
                "baseline": {
                    "manifest": {"scenario": "mux_over_rtp_hostile_message_latency"}
                },
                "metrics": {
                    "message_latency_p95_ms": {"delta_percent": 1.0},
                    "message_latency_p99_ms": {"delta_percent": -1.0},
                    "udp_payload_bytes_per_delivered_byte": {
                        "delta_percent": wire
                    },
                    "goodput_mib_per_second": {"delta_percent": 0.0},
                },
            }

        pairs = [pair(15.0), pair(-15.0)]
        self.assertEqual(COMPARE.classify(pairs), "mixed_results")

    def test_message_regressed_tail_with_improved_wire_is_mixed_results(self):
        """A latency-regressed / overhead-improved tradeoff is mixed_results,
        never a likely regression."""

        def pair(p95, p99, wire):
            return {
                "baseline": {
                    "manifest": {"scenario": "mux_over_rtp_hostile_message_latency"}
                },
                "metrics": {
                    "message_latency_p95_ms": {"delta_percent": p95},
                    "message_latency_p99_ms": {"delta_percent": p99},
                    "udp_payload_bytes_per_delivered_byte": {
                        "delta_percent": wire
                    },
                    "goodput_mib_per_second": {"delta_percent": 0.0},
                },
            }

        pairs = [pair(25.0, 30.0, -15.0), pair(20.0, 22.0, -18.0)]
        self.assertEqual(COMPARE.classify(pairs), "mixed_results")


if __name__ == "__main__":
    unittest.main()
