#!/usr/bin/env python3
"""Deterministic shared-timeline comparison JSON/HTML from netem traces.

Reads manifest/rtp/rtp_peer/netem/progress evidence for a set of baseline
and candidate runs, computes per-run trace health, pairs runs by c2s/s2c
seed identity, calculates candidate-versus-baseline metric deltas, and
classifies the result.  The verdict is a consistency label, not statistical
confidence or causality; every source hint carries a does_not_prove
constraint and no hint identifies a specific branch as causal.
"""

import argparse
import csv
import html
import importlib.util
import json
import math
import statistics
from pathlib import Path

COMPARISON_SCHEMA_VERSION = 28

GENTLE_EXIT_CAUSES = ("loss", "gate_open", "drain_guard", "outage_reset")

MODULE_PATH = Path(__file__).with_name("rtp_trace_report.py")
SPEC = importlib.util.spec_from_file_location("rtp_trace_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def read_manifest(trace_dir):
    return {row["key"]: row["value"] for row in read_csv(trace_dir / "manifest.csv")}


def optional_float(value):
    return None if value in (None, "") else float(value)


def boolean(value):
    return str(value).lower() in ("1", "true", "yes")


def metric_number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def state_rows(rows, manifest):
    """Rows carrying a controller-state snapshot, on the shared timeline."""
    state = []
    for row in rows:
        smoothed = REPORT.field(row, "smoothed_rtt_us")
        if smoothed == "":
            continue
        rate = REPORT.field(row, "send_rate_packets_per_second", "send_rate_bytes_per_second")
        state.append(
            {
                "time": REPORT.timeline_seconds(row, manifest),
                "send_rate": metric_number(rate, 0.0),
                "raw_rtt": metric_number(REPORT.field(row, "raw_rtt_us")),
                "action": REPORT.field(row, "congestion_action") or "none",
                "outage": boolean(REPORT.field(row, "outage_recovery")),
                "delivery_sample_app_limited": boolean(
                    REPORT.field(row, "delivery_sample_app_limited", "app_limited")
                ),
                "application_write_waiters": optional_float(
                    REPORT.field(row, "application_write_waiters")
                ),
                "application_limited_detections": optional_float(
                    REPORT.field(row, "application_limited_detections")
                ),
                "application_limited_detections_suppressed_by_waiting_writer": optional_float(
                    REPORT.field(row, "application_limited_detections_suppressed_by_waiting_writer")
                ),
                "pending_send_bytes": optional_float(REPORT.field(row, "pending_send_bytes")),
                "accepts_new_packet": (
                    None if REPORT.field(row, "accepts_new_packet") == ""
                    else boolean(REPORT.field(row, "accepts_new_packet"))
                ),
                "no_response": optional_float(REPORT.field(row, "no_response_for_us")),
                "no_progress": optional_float(REPORT.field(row, "no_progress_for_us")),
                "retransmitted": metric_number(REPORT.field(row, "retransmitted_packets"), 0.0),
                "retransmission_active": metric_number(REPORT.field(row, "retransmission_active_packets"), 0.0),
                "retransmission_ready": metric_number(REPORT.field(row, "retransmission_ready_packets"), 0.0),
                "retransmission_attempts": metric_number(REPORT.field(row, "retransmission_attempts"), 0.0),
                "retransmission_first_attempts": metric_number(REPORT.field(row, "retransmission_first_attempts"), 0.0),
                "retransmission_repeat_attempts": metric_number(REPORT.field(row, "retransmission_repeat_attempts"), 0.0),
                "retransmission_rto_reason": metric_number(REPORT.field(row, "retransmission_rto_reason"), 0.0),
                "retransmission_reorder_reason": metric_number(REPORT.field(row, "retransmission_reorder_reason"), 0.0),
                "retransmission_fast_loss_reason": metric_number(REPORT.field(row, "retransmission_fast_loss_reason"), 0.0),
                "retransmission_pre_outage_reason": metric_number(REPORT.field(row, "retransmission_pre_outage_reason"), 0.0),
                "tail_probe_attempts": metric_number(REPORT.field(row, "tail_probe_attempts"), 0.0),
                "rto_postponements": metric_number(REPORT.field(row, "rto_deadline_postponements"), 0.0),
                "cc_rate_samples": metric_number(REPORT.field(row, "congestion_rate_samples"), 0.0),
                "cc_probe_decisions": metric_number(REPORT.field(row, "congestion_bandwidth_probe_decisions"), 0.0),
                "cc_probe_increases": metric_number(REPORT.field(row, "congestion_bandwidth_probe_increases"), 0.0),
                "cc_probe_before_feedback": metric_number(REPORT.field(row, "congestion_bandwidth_probe_before_feedback"), 0.0),
                "cc_persistent_queue_available": "congestion_persistent_queue_for_us" in row,
                "cc_persistent_queue_for": optional_float(
                    REPORT.field(row, "congestion_persistent_queue_for_us")
                ),
                "cc_persistent_queue_resets": optional_float(
                    REPORT.field(row, "congestion_persistent_queue_resets")
                ),
                "cc_delay_drains": metric_number(REPORT.field(row, "congestion_delay_drains"), 0.0),
                "cc_loss_backoff_floor": optional_float(
                    REPORT.field(row, "congestion_loss_backoff_floor_packets_per_second")
                ),
                "cc_loss_backoff_raw": optional_float(
                    REPORT.field(row, "congestion_loss_backoff_raw_target_packets_per_second")
                ),
                "cc_loss_backoff_target": optional_float(
                    REPORT.field(row, "congestion_loss_backoff_target_packets_per_second")
                ),
                "cc_loss_backoffs": metric_number(
                    REPORT.field(row, "congestion_loss_backoffs"), 0.0
                ),
                "cc_loss_backoff_floor_bindings": metric_number(
                    REPORT.field(row, "congestion_loss_backoff_floor_bindings"), 0.0
                ),
                "loss": metric_number(REPORT.field(row, "loss_ratio")),
                "cc_loss": metric_number(REPORT.field(row, "congestion_loss_ratio")),
                "event": REPORT.field(row, "event"),
            }
        )
    return state


def raw_rtt_ms(rows):
    return sorted(
        value / 1000.0
        for value in (optional_float(REPORT.field(row, "raw_rtt_us")) for row in rows)
        if value is not None
    )


def termination_summary(rows):
    summary = {}
    for row in rows:
        event = REPORT.field(row, "event")
        if event == "proactive_termination":
            key = "proactive_stall/broken_pipe"
        elif event == "session_termination":
            cause = REPORT.field(row, "termination_cause") or "unknown"
            error = REPORT.field(row, "termination_error_kind") or "unknown"
            key = f"{cause}/{error}"
            errno = REPORT.field(row, "termination_raw_os_error")
            if errno:
                key += f"(errno={errno})"
        else:
            continue
        summary[key] = summary.get(key, 0) + 1
    return summary


def has_terminal_event(rows):
    return any(
        REPORT.field(row, "event") in ("session_termination", "proactive_termination")
        for row in rows
    )


def final_netem_counters(netem_rows):
    counters = {}
    for row in reversed(netem_rows):
        direction = REPORT.field(row, "direction")
        if not direction or direction in counters:
            continue
        counters[direction] = {
            key: metric_number(REPORT.field(row, key), 0.0)
            for key in (
                "received", "forwarded", "delayed", "dropped",
                "duplicated", "reordered", "rate_limited", "overflow_dropped",
            )
        }
    return counters


def rolling_goodput(progress_rows):
    points = []
    left = 0
    for right, (elapsed, delivered) in enumerate(progress_rows):
        while left < right and progress_rows[left][0] < elapsed - 1.0:
            left += 1
        if left < right and elapsed > progress_rows[left][0]:
            points.append(
                (
                    elapsed,
                    (delivered - progress_rows[left][1])
                    / (elapsed - progress_rows[left][0])
                    / (1024 * 1024),
                )
            )
    return points


def split_window_goodput(progress_rows, elapsed_seconds, delivered_bytes):
    """Return first-half and second-half application goodput in MiB/s.

    The cumulative delivery counter is linearly interpolated at the exact
    measurement midpoint. A trace must bracket that midpoint with samples no
    more than one second apart; sparse historical traces return unavailable
    values rather than pretending their whole-window average was stationary.
    """
    if (
        elapsed_seconds is None
        or delivered_bytes is None
        or elapsed_seconds <= 0
        or delivered_bytes < 0
    ):
        return None, None
    midpoint = elapsed_seconds / 2.0
    points = [
        (elapsed, delivered)
        for elapsed, delivered in progress_rows
        if 0.0 <= elapsed <= elapsed_seconds
    ]
    if len(points) < 2:
        return None, None
    for previous, current in zip(points, points[1:]):
        if current[0] < previous[0] or current[1] < previous[1]:
            return None, None
    left = next(
        (point for point in reversed(points) if point[0] <= midpoint), None
    )
    right = next((point for point in points if point[0] >= midpoint), None)
    if left is None or right is None or right[0] - left[0] > 1.0:
        return None, None
    if right[0] == left[0]:
        midpoint_delivered = left[1]
    else:
        fraction = (midpoint - left[0]) / (right[0] - left[0])
        midpoint_delivered = left[1] + fraction * (right[1] - left[1])
    if not 0.0 <= midpoint_delivered <= delivered_bytes:
        return None, None
    mib = 1024 * 1024
    first = midpoint_delivered / midpoint / mib
    second = (delivered_bytes - midpoint_delivered) / midpoint / mib
    return first, second


def read_run(spec):
    if len(spec) == 2:
        label, trace_dir = spec
        rtp_filename = "rtp.csv"
    else:
        label, trace_dir, rtp_filename = spec
    trace_dir = Path(trace_dir)
    manifest = read_manifest(trace_dir)
    rtp = read_csv(trace_dir / rtp_filename)
    peer = read_csv(trace_dir / "rtp_peer.csv")
    netem = read_csv(trace_dir / "netem.csv")
    progress_raw = read_csv(trace_dir / "progress.csv")
    progress = [
        (REPORT.timeline_seconds(row, manifest), float(row["delivered_bytes"]))
        for row in progress_raw
        if REPORT.field(row, "delivered_bytes") != ""
    ]
    measurement_progress = [
        (float(row.get("elapsed_us", 0)) / 1_000_000.0, float(row["delivered_bytes"]))
        for row in progress_raw
        if REPORT.field(row, "delivered_bytes") != ""
    ]
    state = state_rows(rtp, manifest)
    rtt = raw_rtt_ms(rtp)
    peer_state = state_rows(peer, manifest)
    peer_rtt = raw_rtt_ms(peer)
    return {
        "label": label,
        "path": trace_dir,
        "rtp_filename": rtp_filename,
        "manifest": manifest,
        "rtp": rtp,
        "rtp_peer": peer,
        "netem": netem,
        "progress": progress,
        "state": state,
        "rtt": rtt,
        "peer_state": peer_state,
        "rolling": rolling_goodput(progress),
        "peer_rtt": peer_rtt,
        "health": trace_health(
            trace_dir,
            manifest,
            rtp,
            peer,
            netem,
            progress_raw,
        ),
        "summary": summarize_run(
            manifest,
            state,
            peer_state,
            rtt,
            peer_rtt,
            netem,
            measurement_progress,
            rtp,
            peer,
        ),
    }


def trace_health(trace_dir, manifest, rtp, peer, netem, progress):
    checks = {}
    schema = manifest.get("trace_schema_version", "")
    row_schema = REPORT.field(rtp[0], "schema_version") if rtp else ""
    supported_schemas = (
        "27",
        "26",
        "25",
        "24",
        "23",
        "22",
        "21",
        "20",
        "19",
        "18",
        "17",
        "16",
        "15",
        "14",
        "10",
        "9",
        "8",
        "1",
    )
    checks["schema_compatible"] = (
        schema in supported_schemas or row_schema in supported_schemas
    )
    observer = manifest.get("rtp_observer", "")
    checks["observer_present"] = boolean(observer) if observer != "" else False
    warmup_seconds = metric_number(manifest.get("warmup_seconds"), 0.0) or 0.0
    checks["counter_baseline_present"] = (
        schema not in ("22", "23", "24", "25", "26")
        or not checks["observer_present"]
        or warmup_seconds == 0.0
        or (
            boolean(manifest.get("rtp_counter_baseline_present"))
            and boolean(manifest.get("rtp_peer_counter_baseline_present"))
        )
    )
    dropped = metric_number(manifest.get("rtp_dropped_capacity"), 0) or 0
    peer_dropped = metric_number(manifest.get("rtp_peer_dropped_capacity"), 0) or 0
    checks["capture_not_dropped"] = dropped == 0 and peer_dropped == 0
    probe_outcome = manifest.get("probe_outcome", "")
    checks["task_completed"] = probe_outcome in ("", "completed")
    endpoint_outcomes = [
        manifest.get(key, "")
        for key in ("sink_read_outcome", "client_mux_outcome", "server_mux_outcome")
    ]
    expected_live_timebox = (
        manifest.get("measurement_end_reason", "") == "timebox_elapsed"
        and probe_outcome == "completed"
        and all(outcome == "running" for outcome in endpoint_outcomes)
    )
    checks["endpoint_lifecycle_accounted"] = (
        has_terminal_event(rtp) and has_terminal_event(peer)
    ) or expected_live_timebox
    checks["endpoint_integrity"] = True
    for key in ("sink_read_outcome", "client_mux_outcome", "server_mux_outcome"):
        outcome = manifest.get(key, "")
        if outcome and "corrupt" in outcome.lower():
            checks["endpoint_integrity"] = False
    runner_exit = metric_number(manifest.get("perf_loop_runner_exit_code"))
    checks["runner_succeeded"] = runner_exit in (None, 0)
    checks["rtp_readable"] = len(rtp) > 0
    checks["peer_readable"] = len(peer) > 0
    expected_rtp = metric_number(manifest.get("rtp_captured"))
    expected_peer = metric_number(manifest.get("rtp_peer_captured"))
    checks["capture_counts_match"] = (
        expected_rtp in (None, len(rtp))
        and expected_peer in (None, len(peer))
    )
    expected_netem = metric_number(manifest.get("netem_samples"))
    expected_progress = metric_number(manifest.get("progress_samples"))
    checks["netem_complete"] = (
        len(netem) > 0
        and len(netem) % 2 == 0
        and expected_netem in (None, len(netem) // 2)
    )
    checks["progress_complete"] = (
        len(progress) > 0
        and expected_progress in (None, len(progress))
    )
    window_seconds = metric_number(manifest.get("window_seconds"))
    state_times = [
        REPORT.timeline_seconds(row, manifest)
        for row in rtp
        if REPORT.field(row, "smoothed_rtt_us") != ""
    ]
    checks["measurement_state_present"] = (
        not boolean(manifest.get("rtp_observer", ""))
        or any(
            time is not None
            and (window_seconds is None or time <= window_seconds)
            for time in state_times
        )
    )
    sources = (rtp, peer, netem, progress)
    checks["shared_clock"] = (
        manifest.get("measurement_start_trace_elapsed_us", "")
        and all(
            REPORT.field(row, "trace_elapsed_us") != ""
            for rows in sources
            for row in rows
        )
    )
    failures = [key for key, ok in checks.items() if not ok]
    invalid_keys = {
        "rtp_readable",
        "peer_readable",
        "schema_compatible",
        "task_completed",
        "endpoint_integrity",
        "capture_not_dropped",
        "runner_succeeded",
        "capture_counts_match",
        "netem_complete",
        "progress_complete",
        "measurement_state_present",
    }
    if any(key in invalid_keys for key in failures):
        quality = "invalid"
    elif failures:
        quality = "degraded"
    else:
        quality = "healthy"
    return {"checks": checks, "evidence_quality": quality}


def _percent(value, total):
    return 100.0 * value / total if total else math.nan


def _per_gib(value, delivered_bytes):
    """Normalize an event count by application bytes delivered.

    Rejects the normalization unless BOTH a concrete counter and positive
    delivered bytes exist: an absent schema field stays None rather than
    being misread as zero, while a present zero count with bytes is a real
    zero."""
    if value is None or delivered_bytes is None or delivered_bytes <= 0:
        return None
    return value * 1024 ** 3 / delivered_bytes


def action_streak_max_ms(state, action):
    """Longest observed continuous run of one controller action.

    State snapshots are sampled, so the run length is bounded below by the
    longest interval between consecutive retained samples of the action
    rather than a reconstructed timer value.
    """
    longest = 0.0
    start = None
    last = None
    for row in state:
        if row["action"] == action:
            if start is None:
                start = row["time"]
                last = row["time"]
            else:
                last = row["time"]
        elif start is not None:
            longest = max(longest, last - start)
            start = None
            last = None
    if start is not None:
        longest = max(longest, last - start)
    return longest * 1000.0


def summarize_run(manifest, state, peer_state, rtt, peer_rtt, netem, progress, rtp, peer):
    def send_driver_wakes(prefix):
        return {
            wake: metric_number(
                manifest.get(f"{prefix}_send_driver_{wake}_wakes"), 0.0
            )
            for wake in (
                "resume_signal",
                "ack_schedule_signal",
                "pacing_timer",
                "protocol_timer",
                "kill_requested",
            )
        }

    def send_driver_resume_requests(prefix):
        return {
            source: metric_number(
                manifest.get(f"{prefix}_send_driver_resume_{source}_requests"), 0.0
            )
            for source in (
                "application_data",
                "application_frame",
                "application_finish",
                "peer_ack",
                "ack_flush",
                "post_open_handshake",
                "receive_opportunity",
            )
        }

    count = len(state)
    actions = {}
    for row in state:
        actions[row["action"]] = actions.get(row["action"], 0) + 1
    low_rate = sum(row["send_rate"] <= 128.0001 for row in state)
    outage = sum(row["outage"] for row in state)
    app_limited = sum(row["delivery_sample_app_limited"] for row in state)
    waiting_writers = [
        row["application_write_waiters"]
        for row in state
        if row["application_write_waiters"] is not None
    ]
    staged = [
        row["pending_send_bytes"]
        for row in state
        if row["pending_send_bytes"] is not None
    ]
    empty_stage = sum(value == 0.0 for value in staged)
    accepts = [
        row["accepts_new_packet"]
        for row in state
        if row["accepts_new_packet"] is not None
    ]
    retransmitted = [
        row["retransmitted"]
        for row in state
        if row["retransmitted"] > 0
    ]
    active_depths = [row["retransmission_active"] for row in state]
    ready_depths = [row["retransmission_ready"] for row in state]
    postponements = [
        row["rto_postponements"]
        for row in state
        if row["rto_postponements"] is not None
    ]
    retransmission_attempts = state[-1]["retransmission_attempts"] if state else 0.0
    retransmission_repeat_attempts = state[-1]["retransmission_repeat_attempts"] if state else 0.0
    loss = [row["loss"] for row in state if row["loss"] is not None]
    cc_loss = [row["cc_loss"] for row in state if row["cc_loss"] is not None]
    # gentle_mode_exits = actions.get("gentle_probe", 0)
    # delivered_bytes = float(manifest.get("delivered_bytes", 0) or 0)
    persistent_queue_rows = [
        row for row in state if row["cc_persistent_queue_available"]
    ]
    persistent_queue_durations = [
        row["cc_persistent_queue_for"]
        for row in persistent_queue_rows
        if row["cc_persistent_queue_for"] is not None
    ]
    final_counter = lambda field: next(
        (row[field] for row in reversed(state) if row[field] is not None), None
    )
    application_limited_detections = final_counter("application_limited_detections")
    application_limited_suppressions = final_counter(
        "application_limited_detections_suppressed_by_waiting_writer"
    )
    application_limited_classifications = (
        application_limited_detections + application_limited_suppressions 
        if application_limited_detections is not None
        and application_limited_suppressions is not None
        else None
    )
    queue_hold_occupancy = _percent(actions.get("queue_hold", 0), count)
    delay_drain_occupancy = _percent(actions.get("delay_drain", 0), count)
    final_loss_backoffs = state[-1]["cc_loss_backoffs"] if state else 0.0
    final_loss_backoff_bindings = (
        state[-1]["cc_loss_backoff_floor_bindings"] if state else 0.0
    )
    loss_floor_lifts = [
        row["cc_loss_backoff_floor"] - row["cc_loss_backoff_raw"]
        for row in state
        if row["cc_loss_backoff_floor"] is not None
        and row["cc_loss_backoff_raw"] is not None
        and row["cc_loss_backoff_floor"] > row["cc_loss_backoff_raw"]
    ]
    controller_state = {
        "slow_start": _percent(
            sum(boolean(REPORT.field(row, "slow_start")) for row in rtp), count
        ),
        "gentle_mode": _percent(
            sum(boolean(REPORT.field(row, "gentle_mode")) for row in rtp), count
        ),
        "gentle_draining": _percent(
            sum(boolean(REPORT.field(row, "gentle_draining")) for row in rtp), count
        ),
        "queue_building": _percent(
            sum(boolean(REPORT.field(row, "queue_building")) for row in rtp), count
        ),
        "drain_floor_binding": _percent(
            sum(boolean(REPORT.field(row, "drain_floor_binding")) for row in rtp), count
        ),
        "outage_recovery": _percent(outage, count),
    }
    delivered = metric_number(manifest.get("delivered_bytes"))
    delivered_bytes = delivered if delivered is not None else (
        progress[-1][1] if progress else 0
    )

    elapsed = metric_number(manifest.get("elapsed_seconds"))
    first_half_goodput, second_half_goodput = split_window_goodput(
        progress, elapsed, delivered_bytes
    )

    gentle_mode_exits = {
        cause: metric_number(manifest.get(f"rtp_gentle_mode_exit_{cause}"))
        for cause in GENTLE_EXIT_CAUSES
    }
    peer_gentle_mode_exits = {
        cause: metric_number(manifest.get(f"rtp_peer_gentle_mode_exit_{cause}"))
        for cause in GENTLE_EXIT_CAUSES
    }
    total_gentle_mode_exits = {
        cause: (
            gentle_mode_exits[cause] + peer_gentle_mode_exits[cause] if gentle_mode_exits[cause] is not None
            and peer_gentle_mode_exits[cause] is not None
            else None
        )
        for cause in GENTLE_EXIT_CAUSES
    }
    sender_wakes = send_driver_wakes("rtp")
    peer_wakes = send_driver_wakes("rtp_peer")
    resume_requests = send_driver_resume_requests("rtp")
    peer_resume_requests = send_driver_resume_requests("rtp_peer")
    sender_armor_duplicates = metric_number(
        manifest.get("rtp_retransmission_armor_duplicates")
    )
    peer_armor_duplicates = metric_number(
        manifest.get("rtp_peer_retransmission_armor_duplicates")
    )
    sender_would_blocks = metric_number(manifest.get("rtp_data_send_would_blocks"))
    peer_would_blocks = metric_number(manifest.get("rtp_peer_data_send_would_blocks"))

    return {
        "count": count,
        "actions": actions,
        "queue_hold_occupancy": queue_hold_occupancy,
        "delay_drain_occupancy": delay_drain_occupancy,
        "congestion_loss_backoff_floor_binding_percent": (
            100.0 * final_loss_backoff_bindings / final_loss_backoffs
            if final_loss_backoffs > 0 and final_loss_backoff_bindings is not None
            else None
        ),
        "congestion_loss_backoffs_per_gib_delivered": _per_gib(
            final_loss_backoffs, delivered_bytes
        ),
        "congestion_loss_backoff_floor_bindings_per_gib_delivered": _per_gib(
            final_loss_backoff_bindings, delivered_bytes
        ),
        "congestion_loss_backoff_floor_lift_p50": (
            REPORT.quantile(loss_floor_lifts, 0.5) if loss_floor_lifts else None
        ),
        "congestion_loss_backoff_floor_lift_max": (
            max(loss_floor_lifts) if loss_floor_lifts else None
        ),
        "controller_state_occupancy": controller_state,
        "low_send_rate_occupancy": _percent(low_rate, count),
        "gentle_mode_occupancy": controller_state["gentle_mode"],
        "gentle_draining_occupancy": controller_state["gentle_draining"],
        "queue_building_occupancy": controller_state["queue_building"],
        "drain_floor_binding_occupancy": controller_state["drain_floor_binding"],
        "outage_recovery_occupancy": controller_state["outage_recovery"],
        "persistent_queue_occupancy": (
            _percent(len(persistent_queue_durations), len(persistent_queue_rows))
            if persistent_queue_rows
            else None
        ),
        "persistent_queue_max_ms": (
            max(persistent_queue_durations, default=0.0) / 1000.0
            if persistent_queue_rows
            else None
        ),
        "persistent_queue_resets": final_counter("cc_persistent_queue_resets"),
        "gentle_gate_open_streak_max_ms": action_streak_max_ms(
            state, "gentle_probe"
        ),
        **{
            f"gentle_mode_exit_{cause}": total_gentle_mode_exits[cause]
            for cause in GENTLE_EXIT_CAUSES
        },
        "gentle_mode_exits": gentle_mode_exits,
        "peer_gentle_mode_exits": peer_gentle_mode_exits,
        "app_limited_occupancy": _percent(app_limited, count),
        "application_write_waiter_occupancy": (
            _percent(sum(value > 0 for value in waiting_writers), len(waiting_writers))
            if waiting_writers
            else math.nan
        ),
        "application_limited_detections": application_limited_detections,
        "application_limited_detections_suppressed_by_waiting_writer": application_limited_suppressions,
        "application_limited_suppression_percent": (
            _percent(application_limited_suppressions, application_limited_classifications)
            if application_limited_classifications
            else None
        ),
        "empty_send_stage_occupancy": _percent(empty_stage, len(staged)) if staged else math.nan,
        "accepts_new_packet_occupancy": (
            _percent(sum(accepts), len(accepts)) if accepts else math.nan
        ),
        "retransmission_samples": len(retransmitted),
        "max_retransmitted": max(retransmitted, default=0.0),
        "max_retransmission_active": max(active_depths, default=0.0),
        "max_retransmission_ready": max(ready_depths, default=0.0),
        "final_retransmission_counters": {
            "attempts": retransmission_attempts,
            "first_attempts": state[-1]["retransmission_first_attempts"] if state else 0.0,
            "repeat_attempts": retransmission_repeat_attempts,
            "rto_reason": state[-1]["retransmission_rto_reason"] if state else 0.0,
            "reorder_reason": state[-1]["retransmission_reorder_reason"] if state else 0.0,
            "fast_loss_reason": state[-1]["retransmission_fast_loss_reason"] if state else 0.0,
            "pre_outage_reason": state[-1]["retransmission_pre_outage_reason"] if state else 0.0,
            "tail_probes": state[-1]["tail_probe_attempts"] if state else 0.0,
        },
        "retransmission_attempts_per_gib_delivered": _per_gib(retransmission_attempts, delivered_bytes),
        "retransmission_repeat_attempts_per_gib_delivered": _per_gib(retransmission_repeat_attempts, delivered_bytes),
        "final_rto_deadline_postponements": postponements[-1] if postponements else 0.0,
        "final_congestion_counters": {
            "rate_samples": state[-1]["cc_rate_samples"] if state else 0.0,
            "probe_decisions": state[-1]["cc_probe_decisions"] if state else 0.0,
            "probe_increases": state[-1]["cc_probe_increases"] if state else 0.0,
            "probe_before_feedback": state[-1]["cc_probe_before_feedback"] if state else 0.0,
            "delay_drains": state[-1]["cc_delay_drains"] if state else 0.0,
            "gentle_mode_exits": gentle_mode_exits,
        },
        "loss_samples": len(loss),
        "mean_loss_ratio": statistics.fmean(loss) if loss else math.nan,
        "cc_loss_samples": len(cc_loss),
        "mean_cc_loss_ratio": statistics.fmean(cc_loss) if cc_loss else math.nan,
        "rtt_p50_ms": REPORT.quantile(rtt, 0.5) if rtt else math.nan,
        "peer_rtt_p50_ms": REPORT.quantile(peer_rtt, 0.5) if peer_rtt else math.nan,
        "terminations": termination_summary(rtp),
        "peer_terminations": termination_summary(peer),
        "send_driver_wakes": sender_wakes,
        "peer_send_driver_wakes": peer_wakes,
        "send_driver_resume_requests": resume_requests,
        "peer_send_driver_resume_requests": peer_resume_requests,
        "sender_application_data_resume_requests_per_gib_delivered": _per_gib(
            resume_requests["application_data"], delivered_bytes
        ),
        "sender_protocol_timer_wakes_per_gib_delivered": _per_gib(
            sender_wakes["protocol_timer"], delivered_bytes
        ),
        "peer_protocol_timer_wakes_per_gib_delivered": _per_gib(
            peer_wakes["protocol_timer"], delivered_bytes
        ),
        "sender_retransmission_armor_duplicates": sender_armor_duplicates,
        "sender_retransmission_armor_duplicates_per_gib_delivered": _per_gib(
            sender_armor_duplicates, delivered_bytes
        ),
        "peer_retransmission_armor_duplicates": peer_armor_duplicates,
        "peer_retransmission_armor_duplicates_per_gib_delivered": _per_gib(
            peer_armor_duplicates, delivered_bytes
        ),
        "sender_data_send_would_blocks": sender_would_blocks,
        "sender_data_send_would_blocks_per_gib_delivered": _per_gib(
            sender_would_blocks, delivered_bytes
        ),
        "peer_data_send_would_blocks": peer_would_blocks,
        "peer_data_send_would_blocks_per_gib_delivered": _per_gib(
            peer_would_blocks, delivered_bytes
        ),
        "final_netem_counters": final_netem_counters(netem),
        "delivered_bytes": delivered_bytes,
        "elapsed_seconds": elapsed if elapsed is not None else (progress[-1][0] if progress else 0),
        "goodput_mib_per_second": metric_number(manifest.get("goodput_mib_per_second")),
        "goodput_first_half_mib_per_second": first_half_goodput,
        "goodput_second_half_mib_per_second": second_half_goodput,
        "sink_read_outcome": manifest.get("sink_read_outcome", ""),
        "client_mux_outcome": manifest.get("client_mux_outcome", ""),
        "server_mux_outcome": manifest.get("server_mux_outcome", ""),
        "probe_outcome": manifest.get("probe_outcome", ""),
        "measurement_end_reason": manifest.get("measurement_end_reason", ""),
    }


CONFIG_KEYS = (
    "warmup_seconds",
    "window_seconds",
    "mss_bytes",
    "fec",
    "rtp_handshake",
    "perf_loop_profile",
    "netem_s2c",
    "link_profile",
    "netem_c2s",
    "scenario",
)


def pair_config_agrees(baseline, candidate):
    mismatches = []
    for key in CONFIG_KEYS:
        default = "0" if key == "warmup_seconds" else ""
        left = baseline.get(key, default)
        right = candidate.get(key, default)
        if left and right and left != right:
            mismatches.append(key)
    return mismatches


def pair_runs(baseline_runs, candidate_runs):
    candidates_by_seed = {}
    for run in candidate_runs:
        key = (run["manifest"].get("netem_c2s_seed"), run["manifest"].get("netem_s2c_seed"))
        candidates_by_seed.setdefault(key, []).append(run)
    pairs = []
    for baseline in baseline_runs:
        key = (baseline["manifest"].get("netem_c2s_seed"), baseline["manifest"].get("netem_s2c_seed"))
        for candidate in candidates_by_seed.get(key, []):
            pairs.append({"baseline": baseline, "candidate": candidate})
    return pairs


def metric_difference(candidate, baseline):
    if baseline is None or candidate is None:
        return None
    difference = candidate - baseline
    return difference if math.isfinite(difference) else None


def delta_percent(candidate, baseline):
    difference = metric_difference(candidate, baseline)
    if difference is None or baseline == 0:
        return None
    return difference / abs(baseline) * 100.0


def ranking_delta_percent(candidate, baseline):
    """Signed, bounded two-sided delta used only to rank guidance.

    Baseline-relative percentages are useful to display, but become
    arbitrarily large when the baseline is near zero and are undefined
    when it is exactly zero. Scaling by the larger endpoint keeps the ranking in
    [-100, 100] while preserving direction and treating a true
    appearance/disappearance as a full-scale change.
    """
    difference = metric_difference(candidate, baseline)
    if difference is None:
        return None
    scale = max(abs(candidate), abs(baseline))
    if scale == 0:
        return 0.0
    return difference / scale * 100.0


METRICS = (
    "goodput_mib_per_second",
    "goodput_first_half_mib_per_second",
    "goodput_second_half_mib_per_second",
    "rtt_p50_ms",
    "low_send_rate_occupancy",
    "gentle_mode_occupancy",
    "gentle_draining_occupancy",
    "queue_building_occupancy",
    "drain_floor_binding_occupancy",
    "outage_recovery_occupancy",
    "persistent_queue_occupancy",
    "persistent_queue_max_ms",
    "persistent_queue_resets",
    "gentle_gate_open_streak_max_ms",
    "gentle_mode_exit_loss",
    "gentle_mode_exit_gate_open",
    "gentle_mode_exit_outage_reset",
    "gentle_mode_exit_drain_guard",
    "queue_hold_occupancy",
    "delay_drain_occupancy",
    "congestion_loss_backoff_floor_binding_percent",
    "congestion_loss_backoffs_per_gib_delivered",
    "congestion_loss_backoff_floor_bindings_per_gib_delivered",
    "congestion_loss_backoff_floor_lift_p50",
    "congestion_loss_backoff_floor_lift_max",
    "sender_application_data_resume_requests_per_gib_delivered",
    "sender_protocol_timer_wakes_per_gib_delivered",
    "peer_protocol_timer_wakes_per_gib_delivered",
    "sender_retransmission_armor_duplicates",
    "sender_retransmission_armor_duplicates_per_gib_delivered",
    "peer_retransmission_armor_duplicates",
    "peer_retransmission_armor_duplicates_per_gib_delivered",
    "sender_data_send_would_blocks",
    "sender_data_send_would_blocks_per_gib_delivered",
    "peer_data_send_would_blocks",
    "peer_data_send_would_blocks_per_gib_delivered",
    "app_limited_occupancy",
    "application_write_waiter_occupancy",
    "application_limited_suppression_percent",
    "retransmission_attempts_per_gib_delivered",
    "retransmission_repeat_attempts_per_gib_delivered",
)

NEUTRAL_DIRECTION_METRICS = {
    "app_limited_occupancy",
    "application_write_waiter_occupancy",
    "application_limited_suppression_percent",
    "gentle_mode_occupancy",
    "gentle_draining_occupancy",
    "queue_building_occupancy",
    "drain_floor_binding_occupancy",
    "gentle_mode_exit_loss",
    "gentle_mode_exit_gate_open",
    "gentle_mode_exit_drain_guard",
    "gentle_mode_exit_outage_reset",
    "persistent_queue_occupancy",
    "persistent_queue_max_ms",
    "persistent_queue_resets",
    "gentle_gate_open_streak_max_ms",
    "queue_hold_occupancy",
    "delay_drain_occupancy",
    "congestion_loss_backoff_floor_binding_percent",
    "congestion_loss_backoff_floor_lift_p50",
    "congestion_loss_backoff_floor_lift_max",
    "sender_retransmission_armor_duplicates",
    "peer_retransmission_armor_duplicates",
    "sender_data_send_would_blocks",
    "peer_data_send_would_blocks",
}

LOWER_IS_BETTER_METRICS = {
    "rtt_p50_ms",
    "congestion_bandwidth_probe_before_feedback_percent",
    "low_send_rate_occupancy",
    "outage_recovery_occupancy",
    "retransmission_attempts_per_gib_delivered",
    "retransmission_repeat_attempts_per_gib_delivered",
    "congestion_loss_backoffs_per_gib_delivered",
    "congestion_loss_backoff_floor_bindings_per_gib_delivered",
    "sender_application_data_resume_requests_per_gib_delivered",
    "sender_protocol_timer_wakes_per_gib_delivered",
    "peer_protocol_timer_wakes_per_gib_delivered",
    "sender_retransmission_armor_duplicates_per_gib_delivered",
    "peer_retransmission_armor_duplicates_per_gib_delivered",
    "sender_data_send_would_blocks_per_gib_delivered",
    "peer_data_send_would_blocks_per_gib_delivered",
}

CONDITIONING_METRICS = tuple(
    f"gentle_mode_exit_{cause}" for cause in GENTLE_EXIT_CAUSES
)

CONDITIONED_OUTCOME_METRICS = (
    "goodput_mib_per_second",
    "goodput_second_half_mib_per_second",
    "rtt_p50_ms",
    "retransmission_attempts_per_gib_delivered",
    "retransmission_repeat_attempts_per_gib_delivered",
)


def probe_before_feedback_percent(summary):
    """Percent of applied probe increases that ran before the previous one
    got feedback. This is a timing classification, not proof that a probe
    caused queue growth; it is None when no increase was ever applied so the
    denominator is zero."""
    counters = summary["final_congestion_counters"]
    increases = counters["probe_increases"]
    if not increases:
        return None
    return 100.0 * counters["probe_before_feedback"] / increases


def metric_direction(metric, signed_change):
    if metric in NEUTRAL_DIRECTION_METRICS:
        return "changed"
    if metric in LOWER_IS_BETTER_METRICS:
        return "worse" if signed_change > 0 else "better"
    return "worse" if signed_change < 0 else "better"


def _conditioned_outcome(pairs, metric):
    samples = []
    for pair in pairs:
        values = pair["metrics"][metric]
        difference = values["difference"]
        ranking_delta = ranking_delta_percent(
            values["candidate"], values["baseline"]
        )
        if difference is None or ranking_delta is None:
            continue
        samples.append(
            {
                "baseline": values["baseline"],
                "candidate": values["candidate"],
                "difference": difference,
                "delta_percent": values["delta_percent"],
                "ranking_delta_percent": ranking_delta,
            }
        )
    if not samples:
        return None

    changed = [sample for sample in samples if sample["difference"] != 0]
    positive = sum(sample["ranking_delta_percent"] > 0 for sample in changed)
    negative = sum(sample["ranking_delta_percent"] < 0 for sample in changed)
    raw_deltas = [
        sample["delta_percent"]
        for sample in samples
        if sample["delta_percent"] is not None
    ]
    ranking_delta = statistics.median(
        sample["ranking_delta_percent"] for sample in samples
    )
    return {
        "metric": metric,
        "baseline": statistics.median(sample["baseline"] for sample in samples),
        "candidate": statistics.median(sample["candidate"] for sample in samples),
        "difference": statistics.median(sample["difference"] for sample in samples),
        "delta_percent": statistics.median(raw_deltas) if raw_deltas else None,
        "ranking_delta_percent": ranking_delta,
        "direction": (
            metric_direction(metric, ranking_delta)
            if ranking_delta != 0
            else "unchanged"
        ),
        "pair_count": len(samples),
        "changed_pairs": len(changed),
        "directional_consistency_percent": (
            100.0 * max(positive, negative) / len(changed) if changed else 0.0
        ),
    }


def behavior_conditioned_observations(pairs):
    """Summarize outcomes where a candidate behavior counter differs."""
    observations = []
    for conditioning_metric in CONDITIONING_METRICS:
        for condition, predicate in (
            ("candidate_higher", lambda difference: difference > 0),
            ("candidate_lower", lambda difference: difference < 0),
        ):
            selected = [
                pair
                for pair in pairs
                if pair["metrics"][conditioning_metric]["difference"] is not None
                and predicate(pair["metrics"][conditioning_metric]["difference"])
            ]
            if not selected:
                continue
            outcomes = [
                outcome
                for metric in CONDITIONED_OUTCOME_METRICS
                if (outcome := _conditioned_outcome(selected, metric)) is not None
            ]
            goodput = next(
                (
                    outcome
                    for outcome in outcomes
                    if outcome["metric"] == "goodput_mib_per_second"
                ),
                None,
            )
            material_consistent_goodput = (
                len(selected) >= 2
                and goodput is not None
                and abs(goodput["ranking_delta_percent"]) >= 10.0
                and goodput["directional_consistency_percent"] == 100.0
            )
            observations.append(
                {
                    "metric": conditioning_metric,
                    "condition": condition,
                    "pair_count": len(selected),
                    "total_valid_pairs": len(pairs),
                    "support": "repeated" if len(selected) >= 2 else "isolated",
                    "attention": (
                        "consistent_material_goodput_shift"
                        if material_consistent_goodput
                        else "context_only"
                    ),
                    "outcomes": outcomes,
                    "does_not_prove": (
                        "This groups pairs after observing a behavior-counter "
                        "difference. Selection can amplify noise and does_not_prove "
                        "that the event produced any outcome; use it to find the "
                        "behavior-changing pairs, then inspect their timelines."
                    ),
                }
            )
    return observations


def pair_metrics(pair):
    baseline = pair["baseline"]
    candidate = pair["candidate"]
    out = {}
    for metric in METRICS:
        base = baseline["summary"][metric]
        cand = candidate["summary"][metric]
        if metric == "rtt_p50_ms":
            # For latency, lower is better; keep the raw sign convention and
            # report the arithmetic delta so materiality stays comparable.
            value = delta_percent(cand, base)
        else:
            value = delta_percent(cand, base)
        out[metric] = {
            "baseline": base,
            "candidate": cand,
            "difference": metric_difference(cand, base),
            "delta_percent": value,
        }
    baseline_pbf = probe_before_feedback_percent(baseline["summary"])
    candidate_pbf = probe_before_feedback_percent(candidate["summary"])
    out["congestion_bandwidth_probe_before_feedback_percent"] = {
        "baseline": baseline_pbf,
        "candidate": candidate_pbf,
        "difference": metric_difference(candidate_pbf, baseline_pbf),
        "delta_percent": delta_percent(candidate_pbf, baseline_pbf),
    }
    return out


def classify(valid_pairs):
    if not valid_pairs:
        return "insufficient_evidence"
    deltas = [
        pair["metrics"]["goodput_mib_per_second"]["delta_percent"]
        for pair in valid_pairs
    ]
    deltas = [value for value in deltas if value is not None]
    if not deltas:
        return "insufficient_evidence"
    if all(value <= -10.0 for value in deltas):
        return "likely_regression"
    if all(value >= 10.0 for value in deltas):
        return "likely_improvement"
    if all(-10.0 < value < 10.0 for value in deltas):
        return "no_material_change"
    return "mixed_results"


def guidance_hints(pairs, verdict):
    hints = []
    by_metric = {}
    for pair in pairs:
        for metric, values in pair["metrics"].items():
            difference = values["difference"]
            if difference is None:
                continue
            ranking_delta = ranking_delta_percent(
                values["candidate"], values["baseline"]
            )
            if ranking_delta is None:
                continue
            by_metric.setdefault(metric, []).append(
                {
                    "baseline": values["baseline"],
                    "candidate": values["candidate"],
                    "difference": difference,
                    "delta_percent": values["delta_percent"],
                    "ranking_delta_percent": ranking_delta,
                }
            )
    changes = []
    for metric, samples in by_metric.items():
        changed = [sample for sample in samples if sample["difference"] != 0]
        if not changed:
            continue
        ranking_delta = statistics.median(
            sample["ranking_delta_percent"] for sample in samples
        )
        # A one-pair outlier in a larger paired set remains visible in the
        # pair table, but does not become aggregate guidance by itself.
        if ranking_delta == 0:
            continue
        raw_deltas = [
            sample["delta_percent"]
            for sample in samples
            if sample["delta_percent"] is not None
        ]
        positive = sum(sample["ranking_delta_percent"] > 0 for sample in changed)
        negative = sum(sample["ranking_delta_percent"] < 0 for sample in changed)
        directional_consistency = 100.0 * max(positive, negative) / len(changed)
        changes.append(
            {
                "metric": metric,
                "baseline": statistics.median(
                    sample["baseline"] for sample in samples
                ),
                "candidate": statistics.median(
                    sample["candidate"] for sample in samples
                ),
                "difference": statistics.median(
                    sample["difference"] for sample in samples
                ),
                "delta_percent": (
                    statistics.median(raw_deltas) if raw_deltas else None
                ),
                "ranking_delta_percent": ranking_delta,
                "pair_count": len(samples),
                "changed_pairs": len(changed),
                "directional_consistency_percent": directional_consistency,
                "sort_magnitude": abs(ranking_delta),
            }
        )
    changes.sort(key=lambda item: item["sort_magnitude"], reverse=True)
    for change in changes[:5]:
        direction = metric_direction(
            change["metric"], change["ranking_delta_percent"]
        )
        hints.append(
            {
                "metric": change["metric"],
                "baseline": change["baseline"],
                "candidate": change["candidate"],
                "direction": direction,
                "difference": change["difference"],
                "delta_percent": change["delta_percent"],
                "ranking_delta_percent": change["ranking_delta_percent"],
                "pair_count": change["pair_count"],
                "changed_pairs": change["changed_pairs"],
                "directional_consistency_percent": change["directional_consistency_percent"],
                "does_not_prove": (
                    "This is a median consistency signal from the paired traces "
                    "does_not_prove that any specific change produced the "
                    "difference; machine noise, scheduling, or adjacent workload "
                    "interference can move the same metric."
                ),
            }
        )
    if verdict in ("likely_regression", "likely_improvement"):
        hints.append(
            {
                "metric": "verdict",
                "direction": verdict,
                "delta_percent": None,
                "does_not_prove": (
                    "The paired verdict is a consistency label over the whole "
                    "evidence set and does_not_prove an explanation; confirm "
                    "with repeated runs before attributing the change."
                ),
            }
        )
    if not pairs:
        hints.append(
            {
                "metric": "evidence_boundary",
                "direction": "insufficient_evidence",
                "delta_percent": None,
                "does_not_prove": (
                    "No valid paired evidence remains after trace-health and "
                    "configuration filtering; re-run the affected role or fix "
                    "the broken trace before comparing. This boundary does_not_prove "
                    "that either side is faster or slower."
                ),
            }
        )
    return hints


def build_comparison(baseline_specs, candidate_specs):
    runs = []
    for spec in baseline_specs:
        run = read_run(spec)
        run["role"] = "baseline"
        runs.append(run)
    for spec in candidate_specs:
        run = read_run(spec)
        run["role"] = "candidate"
        runs.append(run)

    baseline_runs = [run for run in runs if run["role"] == "baseline"]
    candidate_runs = [run for run in runs if run["role"] == "candidate"]
    pairs = pair_runs(baseline_runs, candidate_runs)

    valid_pairs = []
    excluded = []
    for pair in pairs:
        baseline = pair["baseline"]
        candidate = pair["candidate"]
        mismatches = pair_config_agrees(baseline["manifest"], candidate["manifest"])
        reasons = []
        reasons.extend(f"{key} mismatch" for key in mismatches)
        for role in ("baseline", "candidate"):
            quality = (baseline if role == "baseline" else candidate)["health"]["evidence_quality"]
            if quality == "invalid":
                reasons.append(f"{role} trace invalid")
        pair["metrics"] = pair_metrics(pair)
        pair["excluded_reasons"] = reasons
        pair["valid"] = not reasons
        if pair["valid"]:
            valid_pairs.append(pair)
        else:
            excluded.append(pair)

    verdict = classify(valid_pairs)
    hints = guidance_hints(valid_pairs, verdict)
    conditioned = behavior_conditioned_observations(valid_pairs)

    run_health = [
        {
            "label": run["label"],
            "role": run["role"],
            "path": str(run["path"]),
            "evidence_quality": run["health"]["evidence_quality"],
            "checks": run["health"]["checks"],
            "summary": run["summary"],
        }
        for run in runs
    ]
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "verdict": verdict,
        "valid_pairs": len(valid_pairs),
        "total_pairs": len(pairs),
        "evidence_quality": _overall_quality(runs),
        "runs": run_health,
        "pairs": [
            {
                "index": index,
                "baseline": pair["baseline"]["label"],
                "candidate": pair["candidate"]["label"],
                "valid": pair["valid"],
                "excluded_reasons": pair["excluded_reasons"],
                "metrics": {
                    metric: {
                        "baseline": values["baseline"],
                        "candidate": values["candidate"],
                        "difference": values["difference"],
                        "delta_percent": values["delta_percent"],
                    }
                    for metric, values in pair["metrics"].items()
                },
            }
            for index, pair in enumerate(pairs)
        ],
        "largest_changes": [
            {
                "metric": change["metric"],
                "baseline": change["baseline"],
                "candidate": change["candidate"],
                "difference": change["difference"],
                "delta_percent": change["delta_percent"],
                "ranking_delta_percent": change["ranking_delta_percent"],
                "pair_count": change["pair_count"],
                "changed_pairs": change["changed_pairs"],
                "directional_consistency_percent": change["directional_consistency_percent"],
                "does_not_prove": change["does_not_prove"],
            }
            for change in hints
            if "baseline" in change
        ],
        "behavior_conditioned_observations": conditioned,
        "agent_guidance": hints,
    }


def _overall_quality(runs):
    qualities = {run["health"]["evidence_quality"] for run in runs}
    if "invalid" in qualities:
        return "invalid"
    if "degraded" in qualities:
        return "degraded"
    return "healthy"


def _fmt(value, pattern):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return pattern.format(value)
        except (ValueError, TypeError):
            return str(value)
    return str(value)


def _guidance_change(hint):
    parts = []
    if hint["delta_percent"] is not None:
        parts.append(f"{hint['delta_percent']:+.1f}%")
    elif hint.get("difference") is not None:
        parts.append(f"Δ {hint['difference']:+.3f}")
    if hint.get("ranking_delta_percent") is not None:
        parts.append(f"median rank {hint['ranking_delta_percent']:+.1f}%")
    if hint.get("pair_count"):
        parts.append(f"changed {hint['changed_pairs']}/{hint['pair_count']}")
        parts.append(
            f"{hint['directional_consistency_percent']:.0f}% directional consistency"
        )
    return f", {'; '.join(parts)}" if parts else ""


def escape(value):
    return html.escape(str(value))


def render_html(comparison, runs):
    health_rows = []
    for run in runs:
        checks = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in run["health"]["checks"].items()
        )
        manifest = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in run["manifest"].items()
        )
        summary = run["summary"]
        controller = "".join(
            f"<td>{escape(f'{value:.1f}') if isinstance(value, float) else escape(value)}</td>"
            for value in (
                summary["controller_state_occupancy"]["slow_start"],
                summary["controller_state_occupancy"]["gentle_mode"],
                summary["controller_state_occupancy"]["gentle_draining"],
                summary["controller_state_occupancy"]["queue_building"],
                summary["controller_state_occupancy"]["drain_floor_binding"],
                summary["controller_state_occupancy"]["outage_recovery"],
            )
        )
        health_rows.append(
            "<tr>"
            f"<th>{escape(run['label'])}</th>"
            f"<td>{escape(run['role'])}</td>"
            f"<td>{escape(run['health']['evidence_quality'])}</td>"
            f"<td>{escape(run['manifest'].get('netem_c2s_seed', ''))} / {escape(run['manifest'].get('netem_s2c_seed', ''))}</td>"
            f"<td>{escape(_fmt(summary['goodput_mib_per_second'], '{:.3f}'))}</td>"
            f"<td>{escape(_fmt(summary['goodput_first_half_mib_per_second'], '{:.3f}'))} / "
            f"{escape(_fmt(summary['goodput_second_half_mib_per_second'], '{:.3f}'))}</td>"
            f"<td>{escape(_fmt(summary['rtt_p50_ms'], '{:.2f}'))}</td>"
            f"<td>{escape(_fmt(summary['low_send_rate_occupancy'], '{:.1f}'))}</td>"
            f"<td>{escape(str(summary['terminations']))}</td>"
            f"<td>{escape(str(summary['peer_terminations']))}</td>"
            f"<td>{escape(str(summary['send_driver_wakes']))}</td>"
            f"<td>{escape(str(summary['peer_send_driver_wakes']))}</td>"
            f"<td>{escape(str(summary['send_driver_resume_requests']))}</td>"
            f"<td>{escape(str(summary['peer_send_driver_resume_requests']))}</td>"
            f"<td>{escape(str(summary['final_netem_counters']))}</td>"
            f"<td>{escape(str(summary['sink_read_outcome'] or summary['probe_outcome'] or ''))}</td>"
            f"<td>{escape(str(summary['client_mux_outcome']))} / {escape(str(summary['server_mux_outcome']))}</td>"
            "</tr>"
        )
    pair_rows = []
    for index, pair in enumerate(comparison["pairs"]):
        cells = ""
        for metric in METRICS:
            values = pair["metrics"][metric]
            change = (
                _fmt(values['delta_percent'], '{:+.1f}%')
                if values["delta_percent"] is not None
                else (
                    f"Δ {_fmt(values['difference'], '{:+.3f}')}"
                    if values["difference"] is not None
                    else "n/a"
                )
            )
            cells += (
                f"<td>{escape(_fmt(values['baseline'], '{:.3f}'))}"
                f" → {escape(_fmt(values['candidate'], '{:.3f}'))}"
                f" ({escape(change)})</td>"
            )
        pair_rows.append(
            "<tr>"
            f"<th>{index}</th>"
            f"<td>{escape(pair['baseline'])}</td>"
            f"<td>{escape(pair['candidate'])}</td>"
            f"<td>{escape(pair['valid'])}</td>"
            f"<td>{escape(', '.join(pair['excluded_reasons']) or '-')}</td>"
            f"{cells}"
            "</tr>"
        )
    guidance = "".join(
        f"<li><b>{escape(hint['metric'])}</b> ({escape(hint['direction'])})"
        f"{escape(_guidance_change(hint))}: {escape(hint['does_not_prove'])}</li>"
        for hint in comparison["agent_guidance"]
    )
    conditioned_rows = []
    for observation in comparison["behavior_conditioned_observations"]:
        outcomes = "<br>".join(
            f"{escape(outcome['metric'])} ({escape(outcome['direction'])})"
            f"{escape(_guidance_change(outcome))}"
            for outcome in observation["outcomes"]
        )
        conditioned_rows.append(
            "<tr>"
            f"<th>{escape(observation['metric'])}</th>"
            f"<td>{escape(observation['condition'])}</td>"
            f"<td>{escape(observation['pair_count'])} / {escape(observation['total_valid_pairs'])}</td>"
            f"<td>{escape(observation['support'])}</td>"
            f"<td>{escape(observation['attention'])}</td>"
            f"<td>{outcomes}</td>"
            f"<td>{escape(observation['does_not_prove'])}</td>"
            "</tr>"
        )
    controller_rows = "".join(
        "<tr>"
        f"<th>{escape(run['label'])}</th>{controller}"
        "</tr>"
        for run in runs
    )
    timeline_series = []
    for run in runs:
        timeline_series.append(
            (
                f"{run['label']} goodput",
                [(point[0], point[1]) for point in run["rolling"]],
            )
        )
    cdf_series = [
        (
            f"{run['label']} RTT",
            [
                (value, (index + 1) / len(run["rtt"]) * 100.0)
                for index, value in enumerate(run["rtt"])
            ],
        )
        for run in runs
        if run["rtt"]
    ]
    netem_series = []
    for run in runs:
        for direction, counters in run["summary"]["final_netem_counters"].items():
            netem_series.append(
                (
                    f"{run['label']} {direction} forwarded",
                    [(0, counters.get("forwarded", 0))],
                )
            )

    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Netem paired comparison</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 1200px; padding: 0 1rem; color: #172033; }}
h1 {{ margin-bottom: .25rem; }}
section {{ margin: 2rem 0; }}
svg {{ width: 100%; height: auto; border: 1px solid #d9dfeb; border-radius: 8px; }}
text {{ font-size: 11px; fill: #43506a; }}
.plot-bg {{ fill: #fbfcff; }}
.grid {{ stroke: #dfe5ef; stroke-width: 1; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: .4rem .55rem; vertical-align: top; }}
.note {{ background: #f5f7fb; border-left: 4px solid #64748b; padding: .75rem 1rem; }}
.verdict {{ font-size: 1.15rem; font-weight: 600; }}
</style></head><body>
<h1>Netem paired comparison</h1>
<p class="note">The verdict is a consistency label derived from valid paired seed identities only; it is not statistical confidence and does not prove causality. Every hint below carries a does_not_prove constraint.</p>
<p class="verdict">verdict: {escape(comparison['verdict'])} - {comparison['valid_pairs']} valid / {comparison['total_pairs']} total pairs</p>
<section><h2>Run health</h2><table><thead><tr><th>run</th><th>role</th><th>evidence</th><th>c2s/s2c seed</th><th>goodput MiB/s</th><th>first / second half MiB/s</th><th>RTT p50 ms</th><th>low rate %</th><th>terminations</th><th>peer terminations</th><th>send-driver wakes</th><th>peer send-driver wakes</th><th>resume requests</th><th>peer resume requests</th><th>netem counters</th><th>probe/sink</th><th>mux outcomes</th></tr></thead><tbody>{''.join(health_rows)}</tbody></table></section>
<section><h2>Paired outcomes</h2><table><thead><tr><th>#</th><th>baseline</th><th>candidate</th><th>valid</th><th>excluded</th>{''.join(f'<th>{escape(metric)}</th>' for metric in METRICS)}</tr></thead><tbody>{''.join(pair_rows)}</tbody></table></section>
<section><h2>Behavior-conditioned observations</h2><table><thead><tr><th>counter</th><th>condition</th><th>pairs</th><th>support</th><th>attention</th><th>outcomes</th><th>boundary</th></tr></thead><tbody>{''.join(conditioned_rows)}</tbody></table></section>
<section><h2>Controller-state occupancy</h2><table><thead><tr><th>run</th><th>slow start</th><th>gentle</th><th>gentle drain</th><th>queue building</th><th>drain floor</th><th>outage recovery</th></tr></thead><tbody>{controller_rows}</tbody></table></section>
{REPORT.svg_line_chart("Rolling application goodput (shared timeline)", "trace time (s)", "MiB/s over ~1 s", timeline_series)}
{REPORT.svg_line_chart("Raw RTT empirical CDF", "raw RTT (ms)", "samples ≤ x (%)", cdf_series, (0.0, 100.0))}
<section><h2>Agent guidance</h2><ul>{guidance}</ul></section>
</body></html>"""
    return content


def render_comparison(baseline_specs, candidate_specs, output_dir):
    output_dir = Path(output_dir)
    runs = []
    for spec in list(baseline_specs) + list(candidate_specs):
        run = read_run(spec)
        run["role"] = "baseline" if spec in baseline_specs else "candidate"
        runs.append(run)
    comparison = build_comparison(baseline_specs, candidate_specs)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "comparison.html").write_text(
        render_html(comparison, runs), encoding="utf-8"
    )
    return output_dir


def parse_spec(value):
    label, separator, path = value.partition("=")
    if not separator or not label or not path:
        raise argparse.ArgumentTypeError("trace must be LABEL=TRACE_DIR[/RTP_FILE.csv]")
    source = Path(path)
    if source.suffix == ".csv":
        return label, source.parent, source.name
    return label, source, "rtp.csv"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        action="append",
        type=parse_spec,
        default=[],
        metavar="LABEL=TRACE_DIR[/RTP_FILE.csv]",
        help="baseline run (repeatable)",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        type=parse_spec,
        default=[],
        metavar="LABEL=TRACE_DIR[/RTP_FILE.csv]",
        help="candidate run (repeatable)",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not args.baseline or not args.candidate:
        parser.error("at least one --baseline and one --candidate trace are required")
    output_dir = render_comparison(args.baseline, args.candidate, args.out)
    print(output_dir)


if __name__ == "__main__":
    main()
