use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use netem_test::CountersSnapshot;
use rtp::metrics::{MetricsEvent, MetricsInterest, MetricsObservation, MetricsObserver};

const TRACE_SCHEMA_VERSION: u16 = 7;
const DEFAULT_CAPACITY: usize = 100_000;
const STATE_SAMPLE_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, Copy)]
struct NetemObservation {
    elapsed: Duration,
    c2s: CountersSnapshot,
    s2c: CountersSnapshot,
    delivered_bytes: u64,
}

#[derive(Debug)]
struct RtpCapture {
    observations: Mutex<Vec<MetricsObservation>>,
    last_state_sample_micros: AtomicU64,
    dropped_capacity: AtomicU64,
    capacity: usize,
}

impl RtpCapture {
    fn record(&self, observation: MetricsObservation) {
        let mut observations = self.observations.lock().unwrap();
        if observations.len() >= self.capacity {
            self.dropped_capacity.fetch_add(1, Ordering::Relaxed);
        } else {
            observations.push(observation);
        }
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
        if event == MetricsEvent::ProactiveTermination || self.claim_state_sample_at(elapsed) {
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
    rtp: Arc<RtpCapture>,
    rtp_peer: Arc<RtpCapture>,
    netem: Vec<NetemObservation>,
}

impl PerfTrace {
    fn new_rtp_capture() -> Arc<RtpCapture> {
        Arc::new(RtpCapture {
            observations: Mutex::new(Vec::with_capacity(DEFAULT_CAPACITY)),
            last_state_sample_micros: AtomicU64::new(u64::MAX),
            dropped_capacity: AtomicU64::new(0),
            capacity: DEFAULT_CAPACITY,
        })
    }

    pub(crate) fn from_env() -> Option<Self> {
        let output_dir = std::env::var_os("NETEM_PERF_TRACE_DIR").map(PathBuf::from)?;
        let capture_rtp = std::env::var_os("NETEM_PERF_TRACE_RTP").is_none_or(|value| value != "0");
        Some(Self {
            output_dir,
            capture_rtp,
            rtp: Self::new_rtp_capture(),
            rtp_peer: Self::new_rtp_capture(),
            netem: Vec::new(),
        })
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
        self.netem.push(NetemObservation {
            elapsed,
            c2s,
            s2c,
            delivered_bytes,
        });
    }

    pub(crate) fn finish(self, metadata: &[(&str, String)]) -> io::Result<PathBuf> {
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
        write_csv_row(&mut out, &["rtp_observer", &self.capture_rtp.to_string()])?;
        write_csv_row(
            &mut out,
            &[
                "rtp_state_sample_interval_micros",
                &STATE_SAMPLE_INTERVAL.as_micros().to_string(),
            ],
        )?;
        write_csv_row(&mut out, &["rtp_capacity", &self.rtp.capacity.to_string()])?;
        write_csv_row(
            &mut out,
            &[
                "rtp_dropped_capacity",
                &self
                    .rtp
                    .dropped_capacity
                    .load(Ordering::Relaxed)
                    .to_string(),
            ],
        )?;
        write_csv_row(
            &mut out,
            &[
                "rtp_peer_dropped_capacity",
                &self
                    .rtp_peer
                    .dropped_capacity
                    .load(Ordering::Relaxed)
                    .to_string(),
            ],
        )?;
        for (key, value) in metadata {
            write_csv_row(&mut out, &[key, value])?;
        }
        Ok(())
    }

    fn write_rtp(&self, capture: &RtpCapture, filename: &str) -> io::Result<()> {
        let mut observations = capture.observations.lock().unwrap().clone();
        observations.sort_unstable_by_key(|observation| observation.event_index);
        let mut out = csv_writer(self.output_dir.join(filename))?;
        writeln!(
            out,
            "schema_version,event_index,elapsed_us,event,raw_rtt_us,pacer_tokens_packets,send_rate_packets_per_second,loss_ratio,in_flight_packets,packets_in_pipe,retransmitted_packets,next_send_sequence,minimum_rtt_us,smoothed_rtt_us,congestion_window_packets,received_packets,next_receive_sequence,delivery_rate_packets_per_second,delivery_sample_app_limited,pending_send_bytes,send_stage_capacity_bytes,accepts_new_packet,slow_start,gentle_mode,gentle_draining,queue_building,drain_floor_binding,outage_recovery,no_response_for_us,no_progress_for_us,stall_reason,congestion_loss_ratio,congestion_action"
        )?;
        for observation in observations {
            let mut fields = vec![
                observation.schema_version.to_string(),
                observation.event_index.to_string(),
                observation.elapsed.as_micros().to_string(),
                observation.event.as_str().to_owned(),
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
                fields.resize(30, String::new());
            }
            writeln!(out, "{}", fields.join(","))?;
        }
        Ok(())
    }

    fn write_netem(&self) -> io::Result<()> {
        let mut out = csv_writer(self.output_dir.join("netem.csv"))?;
        writeln!(
            out,
            "elapsed_us,direction,delayed,dropped,duplicated,reordered,rate_limited,forwarded,received,overflow_dropped,queue_len"
        )?;
        for observation in &self.netem {
            write_netem_row(&mut out, observation.elapsed, "c2s", observation.c2s)?;
            write_netem_row(&mut out, observation.elapsed, "s2c", observation.s2c)?;
        }
        Ok(())
    }

    fn write_progress(&self) -> io::Result<()> {
        let mut out = csv_writer(self.output_dir.join("progress.csv"))?;
        writeln!(out, "elapsed_us,delivered_bytes")?;
        for observation in &self.netem {
            writeln!(
                out,
                "{},{}",
                observation.elapsed.as_micros(),
                observation.delivered_bytes,
            )?;
        }
        Ok(())
    }
}

fn csv_writer(path: impl AsRef<Path>) -> io::Result<BufWriter<File>> {
    Ok(BufWriter::new(File::create(path)?))
}

fn write_netem_row(
    out: &mut impl Write,
    elapsed: Duration,
    direction: &str,
    snapshot: CountersSnapshot,
) -> io::Result<()> {
    let stats = snapshot.stats;
    writeln!(
        out,
        "{},{},{},{},{},{},{},{},{},{},{}",
        elapsed.as_micros(),
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
    use rtp::metrics::{MetricsSnapshot, SCHEMA_VERSION};

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
                retransmitted_packets: 0,
                next_send_sequence: 0,
                minimum_rtt: None,
                smoothed_rtt: Duration::from_millis(20),
                congestion_window_packets: 1,
                received_packets: 0,
                next_receive_sequence: None,
                delivery_rate_packets_per_second: None,
                delivery_sample_app_limited: None,
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

    #[test]
    fn state_is_throttled_but_all_raw_rtt_samples_are_kept() {
        let capture = RtpCapture {
            observations: Mutex::new(Vec::new()),
            last_state_sample_micros: AtomicU64::new(u64::MAX),
            dropped_capacity: AtomicU64::new(0),
            capacity: 8,
        };
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
                MetricsEvent::ProactiveTermination,
                Duration::from_millis(51)
            ),
            MetricsInterest::Snapshot,
            "termination must bypass the periodic state throttle"
        );

        let observations = capture.observations.lock().unwrap();
        assert_eq!(observations.len(), 4);
        assert_eq!(observations[0].event_index, 0);
        assert_eq!(observations[1].event_index, 2);
        assert_eq!(observations[2].event_index, 3);
        assert_eq!(observations[3].event_index, 4);
    }

    #[test]
    fn observer_free_capture_has_no_rtp_callback() {
        let trace = PerfTrace {
            output_dir: PathBuf::from("unused"),
            capture_rtp: false,
            rtp: Arc::new(RtpCapture {
                observations: Mutex::new(Vec::new()),
                last_state_sample_micros: AtomicU64::new(u64::MAX),
                dropped_capacity: AtomicU64::new(0),
                capacity: 1,
            }),
            rtp_peer: Arc::new(RtpCapture {
                observations: Mutex::new(Vec::new()),
                last_state_sample_micros: AtomicU64::new(u64::MAX),
                dropped_capacity: AtomicU64::new(0),
                capacity: 1,
            }),
            netem: Vec::new(),
        };
        assert!(trace.rtp_observer().is_none());
        assert!(trace.rtp_peer_observer().is_none());
    }
}
