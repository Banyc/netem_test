//! Clean-link `mux`-over-`rtp` bulk stall: reproduction + stall watchdog.
//!
//! `hol_verify4::v4_clean_muxbulk` writes a bulk stream for a fixed window and
//! only completes when the writer drains; on a *clean* 50 ms link it sometimes
//! blocks for tens of seconds (a mux stream `write_all` that neither completes
//! nor errors).  The stall is non-deterministic, so this scenario does not just
//! time the whole run: it wraps every `write_all` in a watchdog and, when the
//! watchdog fires, dumps the rtp connection's transport-state snapshot so the
//! parked path is visible instead of a bare timeout.
//!
//! The rtp snapshot is captured through the public [`MetricsObserver`] hook:
//! `stall_reason`, congestion window / in-flight / pending-send bytes, the
//! pacer token count, the app write-waiter count, and the last send-driver
//! wake.  Together they say whether the sender is blocked by pacing, by the
//! congestion window, by a full send stage, or by an underlay that stopped
//! accepting — and whether the peer stopped acknowledging.
//!
//! Two scenarios share the write loop:
//! - [`clean_link_mux_bulk_completes_within_timeout`] — the real reproduction
//!   (a stall fails the test and prints the dump).
//! - [`induced_stall_fires_the_watchdog`] — a deterministic validation: the
//!   server sink stops reading, the mux window fills, and the watchdog must
//!   fire.  This proves the watchdog and its dump work even when the
//!   load-dependent real stall does not reproduce.
//!
//! Run with:
//!
//! ```sh
//! cargo test --release --test mux_bulk_clean_stall -- --ignored --nocapture --test-threads=1
//! ```

use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use rtp::metrics::{
    MetricsEvent, MetricsInterest, MetricsObserver, MetricsSendDriverWake, MetricsSnapshot,
};
use support::mux::{mux_client_connect_via, spawn_mux_over_rtp_server_with_mss_via};
use support::payload::cyclic_payload;
use support::rtp::rtp_connect_with_mss_and_observer_via;
use support::{TEST_TASK_QUEUE_BOUND, TestScope, submit_test_task};
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};

mod support;

const CHUNK: usize = 262_044;
const WRITE_WINDOW: Duration = Duration::from_secs(20);
const STALL_TIMEOUT: Duration = Duration::from_secs(60);
/// A single 256 KiB mux write completes in well under a second even on a slow
/// link; a multi-second block is the stall.  This is the deterministic
/// detector: it fires on the blocking write instead of waiting out the whole
/// window.
const WRITE_WATCHDOG: Duration = Duration::from_secs(5);

/// A clean (no-loss) 50 ms RTT link, matching the `hol_verify4` preset that
/// exposes the stall: the mux stream's flow-control window fills before the
/// sender drains, and the writer wedges.
fn clean_link(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(50),
        seed,
        ..NetemConfig::default()
    }
}

/// A deliberately slow link for the watchdog validation: at 200 Kbps a 256 KiB
/// frame takes ~10 s, so the write blocks while the session stays healthy
/// (ACKs still flow, just slowly).
fn slow_link(seed: u64) -> NetemConfig {
    NetemConfig {
        latency: Duration::from_millis(50),
        rate: 200_000,
        seed,
        ..NetemConfig::default()
    }
}

/// The latest transport state seen through the metrics observer.
#[derive(Clone, Copy, Default)]
struct LatestState {
    snapshot: Option<MetricsSnapshot>,
    last_wake: Option<MetricsSendDriverWake>,
    last_event: Option<(MetricsEvent, Duration)>,
    event_index: u64,
}

/// A metrics observer that keeps the newest transport snapshot (plus the last
/// send-driver wake) in a shared slot the watchdog can read when a write
/// blocks.  Per-packet attempts are the highest-rate event, so they only take
/// a fresh snapshot every 256th call; raw RTT samples are skipped.
fn diagnostic_observer() -> (MetricsObserver, Arc<Mutex<LatestState>>) {
    let latest = Arc::new(Mutex::new(LatestState::default()));
    let attempts = Arc::new(AtomicU64::new(0));
    let observer = MetricsObserver::selective(
        {
            let attempts = Arc::clone(&attempts);
            move |event, _elapsed| match event {
                MetricsEvent::SendDataPacketAttempt => {
                    if attempts.fetch_add(1, Ordering::Relaxed) % 256 == 0 {
                        MetricsInterest::Snapshot
                    } else {
                        MetricsInterest::EventOnly
                    }
                }
                MetricsEvent::RttSample => MetricsInterest::Skip,
                // Resume requests now fire once per application write: keep
                // them cheap, a full snapshot is only needed on rare events.
                MetricsEvent::SendDriverResumeRequest(_) => MetricsInterest::EventOnly,
                _ => MetricsInterest::Snapshot,
            }
        },
        {
            let latest = Arc::clone(&latest);
            move |observation| {
                let mut state = latest.lock().unwrap();
                state.event_index = observation.event_index;
                state.last_event = Some((observation.event, observation.elapsed));
                if let Some(snapshot) = observation.snapshot {
                    state.snapshot = Some(snapshot);
                }
                if let MetricsEvent::SendDriverWake(wake) = observation.event {
                    state.last_wake = Some(wake);
                }
            }
        },
    );
    (observer, latest)
}

/// Print everything the watchdog knows about the parked connection.
fn dump_stall(
    label: &str,
    latest: &Arc<Mutex<LatestState>>,
    writes: u64,
    delivered: u64,
    elapsed: Duration,
) {
    let state = *latest.lock().unwrap();
    eprintln!("===== mux bulk stall: {label} =====");
    eprintln!("  writes={writes} delivered={delivered}B elapsed={elapsed:?}");
    eprintln!("  rtp events seen: {}", state.event_index);
    eprintln!("  last rtp event: {:?}", state.last_event);
    eprintln!("  last send-driver wake: {:?}", state.last_wake);
    let Some(s) = state.snapshot else {
        eprintln!("  (no rtp snapshot captured)");
        return;
    };
    eprintln!(
        "  cwnd={}pkt in_flight={} pipe={} pending_send_bytes={} accepts_new_packet={}",
        s.congestion_window_packets,
        s.in_flight_packets,
        s.packets_in_pipe,
        s.pending_send_bytes,
        s.accepts_new_packet,
    );
    eprintln!(
        "  send_rate={:.0}pkt/s pacer_tokens={:.2} app_write_waiters={} slow_start={} outage_recovery={}",
        s.send_rate_packets_per_second,
        s.pacer_tokens_packets,
        s.application_write_waiters,
        s.slow_start,
        s.outage_recovery,
    );
    eprintln!(
        "  next_send_seq={} next_recv_seq={:?} received={} delivery_rate={:?} app_limited={:?}",
        s.next_send_sequence,
        s.next_receive_sequence,
        s.received_packets,
        s.delivery_rate_packets_per_second,
        s.delivery_sample_app_limited,
    );
    eprintln!(
        "  no_progress_for={:?} no_response_for={:?} stall_reason={:?}",
        s.no_progress_for, s.no_response_for, s.stall_reason,
    );
    eprintln!(
        "  retransmit_ready={} retransmit_active={} retransmitted={} min_rtt={:?} srtt={:?} rto={:?}",
        s.retransmission_ready_packets,
        s.retransmission_active_packets,
        s.retransmitted_packets,
        s.minimum_rtt,
        s.smoothed_rtt,
        s.retransmission_timeout,
    );
}

/// What one bounded write window did.
enum WriteOutcome {
    Completed { writes: u64, elapsed: Duration },
    Stalled { writes: u64, elapsed: Duration },
}

/// Write `CHUNK`-sized frames until [`WRITE_WINDOW`] elapses.  Each write is
/// raced against [`WRITE_WATCHDOG`]; if it blocks, the transport state is
/// dumped and the stall is returned instead of hanging.
async fn drive_writes<W: AsyncWrite + Unpin>(
    stream_write: &mut W,
    latest: &Arc<Mutex<LatestState>>,
    delivered: &AtomicU64,
) -> WriteOutcome {
    let payload = cyclic_payload(CHUNK);
    let start = Instant::now();
    let mut writes = 0u64;
    while start.elapsed() < WRITE_WINDOW {
        let write = stream_write.write_all(&payload[..CHUNK]);
        tokio::pin!(write);
        match tokio::time::timeout(WRITE_WATCHDOG, &mut write).await {
            Ok(result) => result.unwrap(),
            Err(_) => {
                let elapsed = start.elapsed();
                dump_stall(
                    "write_all watchdog",
                    latest,
                    writes,
                    delivered.load(Ordering::Relaxed),
                    elapsed,
                );
                return WriteOutcome::Stalled { writes, elapsed };
            }
        }
        writes += 1;
    }
    WriteOutcome::Completed {
        writes,
        elapsed: start.elapsed(),
    }
}

/// A clean-link mux bulk stream must make progress and complete its write
/// window.  A blocking `write_all` is caught by [`WRITE_WATCHDOG`] (and the
/// transport state is dumped), so a stalled run fails fast and diagnosably
/// instead of only timing out at the end.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "clean-link mux bulk stall watchdog; run with --ignored --nocapture --test-threads=1"]
async fn clean_link_mux_bulk_completes_within_timeout() {
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TEST_TASK_QUEUE_BOUND);
    let (observer, latest) = diagnostic_observer();
    let latest_for_run = Arc::clone(&latest);

    let delivered = Arc::new(AtomicU64::new(0));
    let delivered_for_run = Arc::clone(&delivered);

    let run = tasks.run(async move {
        let delivered_for_server = Arc::clone(&delivered_for_run);
        let server_addr = spawn_mux_over_rtp_server_with_mss_via(
            &task_tx,
            false,
            rtp::udp::NO_FEC_MSS,
            move |mut stream_read, mut stream_write| {
                let delivered = Arc::clone(&delivered_for_server);
                async move {
                    let mut buf = vec![0u8; 64 * 1024];
                    while let Ok(n) = stream_read.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                        delivered.fetch_add(n as u64, Ordering::Relaxed);
                    }
                    let _ = stream_write.shutdown();
                }
            },
        )
        .await
        .unwrap();

        let pair = NetemPair::spawn(server_addr, clean_link(11), clean_link(22)).unwrap();
        let (connected_read, connected_write) = rtp_connect_with_mss_and_observer_via(
            &task_tx,
            pair.client_addr(),
            false,
            rtp::udp::NO_FEC_MSS,
            observer,
        )
        .await;
        let opener = mux_client_connect_via(&task_tx, connected_read, connected_write);
        let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

        // Drain the read half so flow-control ACKs keep moving.
        submit_test_task(
            &task_tx,
            Box::pin(async move {
                let mut buf = vec![0u8; 8 * 1024];
                while let Ok(n) = stream_read.read(&mut buf).await {
                    if n == 0 {
                        break;
                    }
                }
            }),
        );

        let outcome = drive_writes(&mut stream_write, &latest_for_run, &delivered_for_run).await;
        let _ = stream_write.shutdown();
        drop(opener);
        pair.stop();
        outcome
    });

    match tokio::time::timeout(STALL_TIMEOUT, run).await {
        Ok(WriteOutcome::Completed { writes, elapsed }) => {
            let bytes = delivered.load(Ordering::Relaxed);
            eprintln!(
                "[mux-clean-stall] completed: writes={writes} elapsed={elapsed:?} delivered={bytes}B"
            );
            assert!(writes > 0, "the clean-link mux writer made no progress");
            assert!(bytes > 0, "the clean-link mux sink delivered nothing");
        }
        Ok(WriteOutcome::Stalled { writes, elapsed }) => panic!(
            "clean-link mux bulk stalled after {writes} writes ({elapsed:?}); transport dump printed above"
        ),
        Err(_) => {
            dump_stall(
                "outer timeout",
                &latest,
                0,
                delivered.load(Ordering::Relaxed),
                Duration::ZERO,
            );
            panic!("clean-link mux bulk did not complete within {STALL_TIMEOUT:?}");
        }
    }
}

/// Deterministic validation of the watchdog: the server sink stops reading
/// after a little data, so the mux stream's flow-control window fills and the
/// client's `write_all` blocks.  The watchdog must fire and dump the transport
/// state — this is the instrumentation working even when the load-dependent
/// real stall does not reproduce.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "watchdog validation via an induced stall; run with --ignored --nocapture --test-threads=1"]
async fn induced_stall_fires_the_watchdog() {
    let mut tasks = TestScope::new();
    let task_tx = tasks.submitter(TEST_TASK_QUEUE_BOUND);
    let (observer, latest) = diagnostic_observer();

    let run = tasks.run(async move {
        let server_addr = spawn_mux_over_rtp_server_with_mss_via(
            &task_tx,
            false,
            rtp::udp::NO_FEC_MSS,
            move |mut stream_read, mut _stream_write| {
                async move {
                    let mut buf = vec![0u8; 64 * 1024];
                    // Read far slower than the sender writes: the mux window
                    // fills and the peer's write_all blocks, while the stream
                    // stays open (the read half keeps being polled).
                    loop {
                        match stream_read.read(&mut buf).await {
                            Ok(0) | Err(_) => break,
                            Ok(_) => {}
                        }
                        tokio::time::sleep(Duration::from_millis(500)).await;
                    }
                }
            },
        )
        .await
        .unwrap();

        let pair = NetemPair::spawn(server_addr, slow_link(11), slow_link(22)).unwrap();
        let (connected_read, connected_write) = rtp_connect_with_mss_and_observer_via(
            &task_tx,
            pair.client_addr(),
            false,
            rtp::udp::NO_FEC_MSS,
            observer,
        )
        .await;
        let opener = mux_client_connect_via(&task_tx, connected_read, connected_write);
        let (mut stream_read, mut stream_write) = opener.open().await.unwrap();

        submit_test_task(
            &task_tx,
            Box::pin(async move {
                let mut buf = vec![0u8; 8 * 1024];
                while let Ok(n) = stream_read.read(&mut buf).await {
                    if n == 0 {
                        break;
                    }
                }
            }),
        );

        let no_delivery = AtomicU64::new(0);
        let outcome = drive_writes(&mut stream_write, &latest, &no_delivery).await;
        let _ = stream_write.shutdown();
        drop(opener);
        pair.stop();
        outcome
    });

    match tokio::time::timeout(STALL_TIMEOUT, run).await {
        Ok(WriteOutcome::Stalled { writes, .. }) => {
            eprintln!(
                "[induced-stall] watchdog fired after {writes} writes — instrumentation validated"
            );
        }
        Ok(WriteOutcome::Completed { writes, elapsed }) => panic!(
            "the watchdog did not fire on an induced stall (writes={writes} elapsed={elapsed:?})"
        ),
        Err(_) => panic!("the induced-stall run did not finish within {STALL_TIMEOUT:?}"),
    }
}
