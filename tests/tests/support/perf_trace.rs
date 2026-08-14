use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use netem_test::CountersSnapshot;
use rtp::metrics::{MetricsEvent, MetricsInterest, MetricsObservation, MetricsObserver};

/// Trace schema 9: RTP rows carry `trace_elapsed_us` so endpoint, netem, and
/// progress samples share one clock (`PerfTrace::trace_start`).
const TRACE_SCHEMA_VERSION: u16 = 9;
const DEFAULT_CAPACITY: usize = 100_000;
const STATE_SAMPLE_INTERVAL: Duration = Duration::from_millis(50);
const RTP_TRACE_COLUMNS: usize = 37;
const RTP_TRACE_HEADER: &str = "schema_version,event_index,elapsed_us,event,termination_cause,termination_error_kind,termination_raw_os_error,raw_rtt_us,pacer_tokens_packets,send_rate_packets_per_second,loss_ratio,in_flight_packets,packets_in_pipe,retransmitted_packets,next_send_sequence,minimum_rtt_us,smoothed_rtt_us,congestion_window_packets,received_packets,next_receive_sequence,delivery_rate_packets_per_second,delivery_sample_app_limited,pending_send_bytes,send_stage_capacity_bytes,accepts_new_packet,slow_start,gentle_mode,gentle_draining,queue_building,drain_floor_binding,outage_recovery,no_response_for_us,no_progress_for_us,stall_reason,congestion_loss_ratio,congestion_action,trace_elapsed_us";

/// One captured RTP observation plus its position on the shared trace clock.
#[derive(Debug, Clone, Copy)]
struct CapturedRtpObservation {
    observation: MetricsObservation,
    trace_elapsed: Duration,
}

/// One netem/progress sample: the scenario-relative `elapsed` and its shared
/// trace-clock position.
#[derive(Debug, Clone, Copy)]
struct NetemObservation {
    elapsed: Duration,
    trace_elapsed: Duration,
    c2s: CountersSnapshot,
    s2c: CountersSnapshot,
    delivered_bytes: u64,
}

/// A bounded, sealable capture of RTP observations for one endpoint. State
/// samples are throttled to [`STATE_SAMPLE_INTERVAL`] while every raw RTT
/// sample and termination row is retained; callbacks are synchronous and the
/// storage is bounded at `capacity`.
#[derive(Debug)]
struct RtpCapture {
    trace_start: Instant,
    observations: Mutex<Vec<CapturedRtpObservation>>,
    last_state_sample_micros: AtomicU64,
    dropped_capacity: AtomicU64,
    sealed: AtomicBool,
    capacity: usize,
}

impl RtpCapture {
    fn new(trace_start: Instant, capacity: usize) -> Self {
        Self {
            trace_start,
            observations: Mutex::new(Vec::with_capacity(capacity)),
            last_state_sample_micros: AtomicU64::new(u64::MAX),
            dropped_capacity: AtomicU64::new(0),
            sealed: AtomicBool::new(false),
            capacity,
        }
    }

    fn record(&self, observation: MetricsObservation) {
        if self.sealed.load(Ordering::Acquire) {
            return;
        }
        let captured = CapturedRtpObservation {
            observation,
            trace_elapsed: self.trace_start.elapsed(),
        };
        let mut observations = self.observations.lock().unwrap();
        if self.sealed.load(Ordering::Acquire) {
            return;
        }
        if observations.len() >= self.capacity {
            self.dropped_capacity.fetch_add(1, Ordering::Relaxed);
        } else {
            observations.push(captured);
        }
    }

    /// Stop accepting callbacks. Runs before any output file is written so a
    /// callback that raced past the finish boundary cannot corrupt the rows.
    fn seal(&self) {
        self.sealed.store(true, Ordering::Release);
        drop(self.observations.lock().unwrap());
    }

    fn claim_state_sample_at(&self, elapsed: Duration) -> bool {
        let now = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
        let interval = STATE_SAMPLE_INTERVAL.as_micros() as u64;
        let mut previous = self.last_state_sample_micros.load(Ordering::Relaxed);
        loop {
            if previous != u64::MAX && now.saturating_sub(previous) < interval {
                return false;
            }
            match self.last_state_sample_micros.compare_exchange_weak(
                previous,
                now,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return true,
                Err(actual) => previous = actual,
            }
        }
    }

    fn interest(&self, event: MetricsEvent, elapsed: Duration) -> MetricsInterest {
        if matches!(event, MetricsEvent::SessionTermination(_))
            || self.claim_state_sample_at(elapsed)
        {
            MetricsInterest::Snapshot
        } else if event == MetricsEvent::RttSample {
            MetricsInterest::EventOnly
        } else {
            MetricsInterest::Skip
        }
    }
}

/// Opt-in capture for performance probes. Set 'NETEM_PERF_TRACE_DIR' to an
/// empty output directory to enable it. Client and accepted-peer RTP state are
/// captured independently at 50 ms while every accepted raw RTT sample is
/// retained. Storage is bounded and callback execution is synchronous; no
/// async channel or detached task is involved.
///
/// Set 'NETEM_PERF_TRACE_RTP=0' to retain only netem and application-progress
/// samples for an observer-free control run with the same output artifacts.
#[derive(Debug)]
pub(crate) struct PerfTrace {
    output_dir: PathBuf,
    capture_rtp: bool,
    trace_start: Instant,
    measurement_start_trace_elapsed: Option<Duration>,
    rtp: Arc<RtpCapture>,
    rtp_peer: Arc<RtpCapture>,
    netem: Vec<NetemObservation>,
}

impl PerfTrace {
    fn new(output_dir: PathBuf, capture_rtp: bool) -> Self {
        let trace_start = Instant::now();
        Self {
            output_dir,
            capture_rtp,
            trace_start,
            measurement_start_trace_elapsed: None,
            rtp: Self::new_rtp_capture(trace_start),
            rtp_peer: Self::new_rtp_capture(trace_start),
            netem: Vec::new(),
        }
    }

    fn new_rtp_capture(trace_start: Instant) -> Arc<RtpCapture> {
        Arc::new(RtpCapture::new(trace_start, DEFAULT_CAPACITY))
    }

    pub(crate) fn from_env() -> Option<Self> {
        let output_dir = std::env::var_os("NETEM_PERF_TRACE_DIR").map(PathBuf::from)?;
        let capture_rtp = std::env::var_os("NETEM_PERF_TRACE_RTP").is_none_or(|value| value != "0");
        Some(Self::new(output_dir, capture_rtp))
    }

    /// Anchor the measurement boundary on the shared trace clock. Netem and
    /// progress samples recorded after this call carry
    /// `measurement_start_trace_elapsed + scenario_elapsed`.
    pub(crate) fn mark_measurement_start(&mut self, start: Instant) {
        self.measurement_start_trace_elapsed =
            Some(start.saturating_duration_since(self.trace_start));
    }

    pub(crate) fn rtp_observer(&self) -> Option<MetricsObserver> {
        self.observer_for(&self.rtp)
    }

    pub(crate) fn rtp_peer_observer(&self) -> Option<MetricsObserver> {
        self.observer_for(&self.rtp_peer)
    }

    fn observer_for(&self, rtp: &Arc<RtpCapture>) -> Option<MetricsObserver> {
        if !self.capture_rtp {
            return None;
        }
        let filter_capture = Arc::clone(rtp);
        let capture = Arc::clone(rtp);
        Some(MetricsObserver::selective(
            move |event, elapsed| filter_capture.interest(event, elapsed),
            move |observation| capture.record(observation),
        ))
    }

    pub(crate) fn record_netem(
        &mut self,
        elapsed: Duration,
        c2s: CountersSnapshot,
        s2c: CountersSnapshot,
        delivered_bytes: u64,
    ) {
        let trace_elapsed = self
            .measurement_start_trace_elapsed
            .map(|start| start + elapsed)
            .unwrap_or_else(|| self.trace_start.elapsed());
        self.netem.push(NetemObservation {
            elapsed,
            trace_elapsed,
            c2s,
            s2c,
            delivered_bytes,
        });
    }

    pub(crate) fn finish(self, metadata: &[(&str, String)]) -> io::Result<PathBuf> {
        self.rtp.seal();
        self.rtp_peer.seal();
        std::fs::create_dir_all(&self.output_dir)?;
        self.write_manifest(metadata)?;
        self.write_rtp(&self.rtp, "rtp.csv")?;
        self.write_rtp(&self.rtp_peer, "rtp_peer.csv")?;
        self.write_netem()?;
        self.write_progress()?;
        Ok(self.output_dir)
    }

    fn write_manifest(&self, metadata: &[(&str, String)]) -> io::Result<()> {
        let mut out = csv_writer(self.output_dir.join("manifest.csv"))?;
        writeln!(out, "key,value")?;
        write_csv_row(
            &mut out,
            &["trace_schema_version", &TRACE_SCHEMA_VERSION.to_string()],
        )?;
        write_csv_row(
            &mut out,
            &[
                "rtp_metrics_schema_version",
                &rtp::metrics::SCHEMA_VERSION.to_string(),
            ],
        )?;
        write_csv_row(
            &mut out,
            &[
                "trace_finish_elapsed_us",
                &self.trace_start.elapsed().as_micros().to_string(),
            ],
        )?;
        write_csv_row(
            &mut out,
            &[
                "measurement_start_trace_elapsed_us",
                &self
                    .measurement_start_trace_elapsed
                    .map(|elapsed| elapsed.as_micros().to_string())
                    .unwrap_or_default(),
            ],
        )?;
        write_csv_row(&mut out, &["rtp_observer", &self.capture_rtp.to_string()])?;
        write_csv_row(
            &mut out,
            &[
                "rtp_state_sample_interval_micros",
                &STATE_SAMPLE_INTERVAL.as_micros().to_string(),
            ],
        )?;
        write_csv_row(&mut out, &["rtp_capacity", &self.rtp.capacity.to_string()])?;
        write_capture_health(&mut out, "rtp", &self.rtp)?;
        write_capture_health(&mut out, "rtp_peer", &self.rtp_peer)?;
        write_csv_row(&mut out, &["netem_samples", &self.netem.len().to_string()])?;
        write_csv_row(
            &mut out,
            &["progress_samples", &self.netem.len().to_string()],
        )?;
        for (key, value) in metadata {
            write_csv_row(&mut out, &[key, value])?;
        }
        Ok(())
    }

    fn write_rtp(&self, capture: &RtpCapture, filename: &str) -> io::Result<()> {
        let mut observations = capture.observations.lock().unwrap().clone();
        observations.sort_unstable_by_key(|captured| captured.observation.event_index);
        let mut out = csv_writer(self.output_dir.join(filename))?;
        writeln!(out, "{RTP_TRACE_HEADER}")?;
        for captured in observations {
            writeln!(
                out,
                "{}",
                rtp_fields(captured.observation, captured.trace_elapsed).join(",")
            )?;
        }
        Ok(())
    }

    fn write_netem(&self) -> io::Result<()> {
        let mut out = csv_writer(self.output_dir.join("netem.csv"))?;
        writeln!(
            out,
            "elapsed_us,trace_elapsed_us,direction,delayed,dropped,duplicated,reordered,rate_limited,forwarded,received,overflow_dropped,queue_len"
        )?;
        for observation in &self.netem {
            write_netem_row(
                &mut out,
                observation.elapsed,
                observation.trace_elapsed,
                "c2s",
                observation.c2s,
            )?;
            write_netem_row(
                &mut out,
                observation.elapsed,
                observation.trace_elapsed,
                "s2c",
                observation.s2c,
            )?;
        }
        Ok(())
    }

    fn write_progress(&self) -> io::Result<()> {
        let mut out = csv_writer(self.output_dir.join("progress.csv"))?;
        writeln!(out, "elapsed_us,trace_elapsed_us,delivered_bytes")?;
        for observation in &self.netem {
            writeln!(
                out,
                "{},{},{}",
                observation.elapsed.as_micros(),
                observation.trace_elapsed.as_micros(),
                observation.delivered_bytes,
            )?;
        }
        Ok(())
    }
}

fn write_capture_health(
    out: &mut impl Write,
    prefix: &str,
    capture: &RtpCapture,
) -> io::Result<()> {
    write_csv_row(
        out,
        &[
            &format!("{prefix}_captured"),
            &capture.observations.lock().unwrap().len().to_string(),
        ],
    )?;
    write_csv_row(
        out,
        &[
            &format!("{prefix}_dropped_capacity"),
            &capture.dropped_capacity.load(Ordering::Relaxed).to_string(),
        ],
    )
}

fn csv_writer(path: impl AsRef<Path>) -> io::Result<BufWriter<File>> {
    Ok(BufWriter::new(File::create(path)?))
}

fn rtp_fields(observation: MetricsObservation, trace_elapsed: Duration) -> Vec<String> {
    let termination = match observation.event {
        MetricsEvent::SessionTermination(termination) => Some(termination),
        _ => None,
    };
    let mut fields = vec![
        observation.schema_version.to_string(),
        observation.event_index.to_string(),
        observation.elapsed.as_micros().to_string(),
        observation.event.as_str().to_owned(),
        termination
            .map(|termination| termination.cause.as_str().to_owned())
            .unwrap_or_default(),
        termination
            .map(|termination| termination.error_kind_str().to_owned())
            .unwrap_or_default(),
        termination
            .and_then(|termination| termination.raw_os_error)
            .map(|error| error.to_string())
            .unwrap_or_default(),
        optional_u128(observation.raw_rtt_sample.map(|value| value.as_micros())),
    ];
    if let Some(snapshot) = observation.snapshot {
        fields.extend([
            snapshot.pacer_tokens_packets.to_string(),
            snapshot.send_rate_packets_per_second.to_string(),
            optional_f64(snapshot.loss_ratio),
            snapshot.in_flight_packets.to_string(),
            snapshot.packets_in_pipe.to_string(),
            snapshot.retransmitted_packets.to_string(),
            snapshot.next_send_sequence.to_string(),
            optional_u128(snapshot.minimum_rtt.map(|value| value.as_micros())),
            snapshot.smoothed_rtt.as_micros().to_string(),
            snapshot.congestion_window_packets.to_string(),
            snapshot.received_packets.to_string(),
            optional_u64(snapshot.next_receive_sequence),
            optional_f64(snapshot.delivery_rate_packets_per_second),
            optional_bool(snapshot.delivery_sample_app_limited),
            snapshot.pending_send_bytes.to_string(),
            snapshot.send_stage_capacity_bytes.to_string(),
            snapshot.accepts_new_packet.to_string(),
            snapshot.slow_start.to_string(),
            snapshot.gentle_mode.to_string(),
            snapshot.gentle_draining.to_string(),
            snapshot.queue_building.to_string(),
            snapshot.drain_floor_binding.to_string(),
            snapshot.outage_recovery.to_string(),
            optional_u128(snapshot.no_response_for.map(|value| value.as_micros())),
            optional_u128(snapshot.no_progress_for.map(|value| value.as_micros())),
            snapshot
                .stall_reason
                .map(|reason| reason.as_str())
                .unwrap_or_default()
                .to_owned(),
            optional_f64(snapshot.congestion_loss_ratio),
            snapshot
                .congestion_action
                .map(|action| action.as_str())
                .unwrap_or_default()
                .to_owned(),
        ]);
    } else {
        fields.resize(RTP_TRACE_COLUMNS - 1, String::new());
    }
    fields.push(trace_elapsed.as_micros().to_string());
    debug_assert_eq!(fields.len(), RTP_TRACE_COLUMNS);
    fields
}

fn write_netem_row(
    out: &mut impl Write,
    elapsed: Duration,
    trace_elapsed: Duration,
    direction: &str,
    snapshot: CountersSnapshot,
) -> io::Result<()> {
    let stats = snapshot.stats;
    writeln!(
        out,
        "{},{},{},{},{},{},{},{},{},{},{},{}",
        elapsed.as_micros(),
        trace_elapsed.as_micros(),
        direction,
        stats.delayed,
        stats.dropped,
        stats.duplicated,
        stats.reordered,
        stats.rate_limited,
        stats.forwarded,
        stats.received,
        stats.overflow_dropped,
        snapshot.queue_len,
    )
}

fn write_csv_row(out: &mut impl Write, values: &[&str]) -> io::Result<()> {
    for (index, value) in values.iter().enumerate() {
        if index != 0 {
            write!(out, ",")?;
        }
        write!(out, "\"{}\"", value.replace('"', "\"\""))?;
    }
    writeln!(out)
}

fn optional_u128(value: Option<u128>) -> String {
    value.map(|value| value.to_string()).unwrap_or_default()
}

fn optional_u64(value: Option<u64>) -> String {
    value.map(|value| value.to_string()).unwrap_or_default()
}

fn optional_f64(value: Option<f64>) -> String {
    value.map(|value| value.to_string()).unwrap_or_default()
}

fn optional_bool(value: Option<bool>) -> String {
    value.map(|value| value.to_string()).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rtp::metrics::{
        MetricsSnapshot, MetricsTermination, MetricsTerminationCause, SCHEMA_VERSION,
    };

    fn observation(event_index: u64, elapsed_ms: u64, event: MetricsEvent) -> MetricsObservation {
        MetricsObservation {
            schema_version: SCHEMA_VERSION,
            event_index,
            elapsed: Duration::from_millis(elapsed_ms),
            event,
            raw_rtt_sample: (event == MetricsEvent::RttSample).then(|| Duration::from_millis(20)),
            snapshot: Some(MetricsSnapshot {
                pacer_tokens_packets: 0.0,
                send_rate_packets_per_second: 1.0,
                loss_ratio: None,
                congestion_loss_ratio: None,
                congestion_action: None,
                in_flight_packets: 0,
                packets_in_pipe: 0,
                retransmission_active_packets: 0,
                retransmission_ready_packets: 0,
                retransmitted_packets: 0,
                next_send_sequence: 0,
                minimum_rtt: None,
                smoothed_rtt: Duration::from_millis(20),
                retransmission_timeout: Duration::from_millis(1000),
                oldest_pipe_packet_age: None,
                maximum_packet_rto_overdue: None,
                rto_deadline_postponements: 0,
                congestion_window_packets: 1,
                received_packets: 0,
                next_receive_sequence: None,
                delivery_rate_packets_per_second: None,
                delivery_sample_app_limited: None,
                congestion_control_rtt: None,
                congestion_rtt_floor: None,
                congestion_queue_tolerance: None,
                congestion_delivery_peak_packets_per_second: None,
                congestion_drain_floor_packets_per_second: None,
                congestion_drain_target_packets_per_second: None,
                congestion_rate_samples: 0,
                congestion_bandwidth_probe_decisions: 0,
                congestion_bandwidth_probe_increases: 0,
                congestion_bandwidth_probe_before_feedback: 0,
                congestion_last_bandwidth_probe_interval: None,
                congestion_delay_drains: 0,
                pending_send_bytes: 0,
                send_stage_capacity_bytes: 8192,
                accepts_new_packet: true,
                slow_start: true,
                gentle_mode: false,
                gentle_draining: false,
                queue_building: false,
                drain_floor_binding: false,
                outage_recovery: false,
                no_response_for: None,
                no_progress_for: None,
                stall_reason: None,
            }),
        }
    }

    fn capture(trace_start: Instant, capacity: usize) -> RtpCapture {
        RtpCapture::new(trace_start, capacity)
    }

    #[test]
    fn state_is_throttled_but_all_raw_rtt_samples_are_kept() {
        let capture = capture(Instant::now(), 8);
        assert_eq!(
            capture.interest(MetricsEvent::SendDataPacketAttempt, Duration::ZERO),
            MetricsInterest::Snapshot
        );
        capture.record(observation(0, 0, MetricsEvent::SendDataPacketAttempt));
        assert_eq!(
            capture.interest(
                MetricsEvent::SendDataPacketAttempt,
                Duration::from_millis(1)
            ),
            MetricsInterest::Skip
        );
        assert_eq!(
            capture.interest(MetricsEvent::RttSample, Duration::from_millis(2)),
            MetricsInterest::EventOnly
        );
        capture.record(observation(2, 2, MetricsEvent::RttSample));
        assert_eq!(
            capture.interest(MetricsEvent::RttSample, Duration::from_millis(3)),
            MetricsInterest::EventOnly
        );
        capture.record(observation(3, 3, MetricsEvent::RttSample));
        assert_eq!(
            capture.interest(MetricsEvent::ReceiveAckPacket, Duration::from_millis(50)),
            MetricsInterest::Snapshot
        );
        capture.record(observation(4, 50, MetricsEvent::ReceiveAckPacket));
        assert_eq!(
            capture.interest(
                MetricsEvent::SessionTermination(MetricsTermination {
                    cause: MetricsTerminationCause::ProactiveStall,
                    error_kind: std::io::ErrorKind::BrokenPipe,
                    raw_os_error: None,
                }),
                Duration::from_millis(51)
            ),
            MetricsInterest::Snapshot,
            "termination must bypass the periodic state throttle"
        );

        let observations = capture.observations.lock().unwrap();
        assert_eq!(observations.len(), 4);
        assert_eq!(observations[0].observation.event_index, 0);
        assert_eq!(observations[1].observation.event_index, 2);
        assert_eq!(observations[2].observation.event_index, 3);
        assert_eq!(observations[3].observation.event_index, 4);
        // Every captured row carries its shared trace-clock position.
        for captured in observations.iter() {
            assert!(!captured.trace_elapsed.is_zero());
        }
    }

    #[test]
    fn event_only_and_snapshot_rows_match_the_schema_width() {
        assert_eq!(RTP_TRACE_HEADER.split(',').count(), RTP_TRACE_COLUMNS);
        let snapshot = observation(0, 0, MetricsEvent::SendDataPacketAttempt);
        let mut event_only = observation(1, 1, MetricsEvent::RttSample);
        event_only.snapshot = None;
        let trace_elapsed = Duration::from_micros(123);
        assert_eq!(rtp_fields(snapshot, trace_elapsed).len(), RTP_TRACE_COLUMNS);
        let event_only_fields = rtp_fields(event_only, trace_elapsed);
        assert_eq!(event_only_fields.len(), RTP_TRACE_COLUMNS);
        assert_eq!(event_only_fields[7], "20000");
        assert!(
            event_only_fields[8..RTP_TRACE_COLUMNS - 1]
                .iter()
                .all(String::is_empty)
        );
        assert_eq!(event_only_fields[RTP_TRACE_COLUMNS - 1], "123");
    }

    #[test]
    fn sealed_capture_rejects_callbacks_after_finish_starts() {
        let trace_start = Instant::now();
        let capture = capture(trace_start, 2);
        capture.record(observation(0, 0, MetricsEvent::SendDataPacketAttempt));
        capture.seal();
        // A callback that raced past the finish boundary must be rejected:
        // the captured set is frozen and the count does not grow.
        capture.record(observation(1, 1, MetricsEvent::SendDataPacketAttempt));
        capture.record(observation(2, 2, MetricsEvent::RttSample));
        let observations = capture.observations.lock().unwrap();
        assert_eq!(observations.len(), 1);
        assert_eq!(observations[0].observation.event_index, 0);
        assert_eq!(capture.dropped_capacity.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn observer_free_capture_has_no_rtp_callback() {
        let trace = PerfTrace::new(PathBuf::from("unused"), false);
        assert!(trace.rtp_observer().is_none());
        assert!(trace.rtp_peer_observer().is_none());
    }
}
