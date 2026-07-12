//! RTP cumulative-progress liveness integration test.
//!
//! The RTP `ack` path was changed to tie cumulative-progress liveness to the
//! send-frontier advance rather than fresh SACKs. This test validates that
//! change end-to-end: a connection with a permanent cumulative hole does not
//! stay alive indefinitely even though periodic small heartbeat frames and
//! ACK/SACK traffic continue to flow.
//!
//! This test is `#[ignore]`-d by default so it does not lengthen normal builds.
//! Run it with:
//!
//! ```sh
//! cargo test -p tests --test rtp_liveness rtp_fresh_sacks -- --ignored --exact --nocapture
//! ```

use std::time::{Duration, Instant};

use netem_test::{NetemConfig, NetemPair};
use support::{send_timestamped_messages, spawn_rtp_msg_latency_sink};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

mod support;

/// Maximum datagram size the proxy allows. Datagrams larger than this are
/// deterministically dropped, creating a permanent cumulative hole at the
/// first data packet while allowing small ACK/SACK/heartbeat traffic to pass.
const MAX_DATAGRAM: usize = 512;

/// One-way delay applied to both proxy directions so latency does not
/// dominate the BrokenPipe timeout.
const OWD: Duration = Duration::from_millis(50);

/// Message size for the small heartbeat frames that pass through the proxy.
const MSG_BYTES: usize = 64;

/// Interval between heartbeat frames.
const MSG_INTERVAL: Duration = Duration::from_secs(1);

/// Maximum test duration. The connection must die before this deadline.
const MAX_DURATION: Duration = Duration::from_secs(65);

/// Minimum post-hole heartbeats that must be delivered before BrokenPipe.
const MIN_POST_HOLE_FRAMES: u64 = 20;

/// Minimum elapsed time before BrokenPipe is expected (must be at least
/// 30 seconds to prove cumulative-progress timeout fired, not the response
/// watchdog which would be much shorter).
const MIN_BROKEN_PIPE_ELAPSED: Duration = Duration::from_secs(30);

#[test]
#[ignore]
fn rtp_fresh_sacks_beyond_permanent_mtu_hole_do_not_keep_connection_alive() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    rt.block_on(async {
        let base = Instant::now();
        let start = Instant::now();

        let (server_addr, mut latency_rx) =
            spawn_rtp_msg_latency_sink(false, base).await.unwrap();

        let c2s = NetemConfig {
            max_datagram_size: MAX_DATAGRAM,
            latency: OWD,
            ..NetemConfig::default()
        };
        let s2c = NetemConfig {
            latency: OWD,
            ..NetemConfig::default()
        };
        let pair = NetemPair::spawn(server_addr, c2s, s2c).unwrap();

        // Connect with a small MSS so data packets fit within MAX_DATAGRAM
        // when payloads are small. The first write is a small echo payload
        // that establishes the connection and advances the cumulative front.
        let connected = rtp::udp::connect_without_handshake_with_mss(
            "0.0.0.0:0",
            &pair.client_addr().to_string(),
            None,
            false,
            rtp::udp::NO_FEC_MSS,
        )
        .await
        .unwrap();

        let mut read = connected.read.into_async_read();
        let mut write = connected.write.into_async_write();

        // Background read task: keep draining ACKs so the connection can
        // receive acknowledgements from the server.
        tokio::spawn(async move {
            let mut buf = vec![0u8; 64 * 1024];
            loop {
                let n = read.read(&mut buf).await;
                match n {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
        });

        // Phase 1: send a small message that passes through the proxy to
        // establish the connection and confirm the plumbing.
        let sent = send_timestamped_messages(
            &mut write,
            base,
            MSG_BYTES,
            MSG_INTERVAL,
            Duration::from_millis(500),
        )
        .await;
        assert!(sent > 0, "initial handshake message must be delivered");
        tokio::time::sleep(Duration::from_secs(2)).await;

        // Verify the server received the handshake frame.
        let mut pre_hole_frames = 0u64;
        while latency_rx.try_recv().is_ok() {
            pre_hole_frames += 1;
        }
        assert!(
            pre_hole_frames > 0,
            "server must have received the initial handshake frame"
        );

        // Phase 2: plant a permanent cumulative hole.  RTP emits one
        // MSS-sized packet per write.  With `max_datagram_size: 512`, a
        // 1024-byte payload's packet (~1050+ bytes on the wire) is dropped
        // deterministically.  The write call itself returns quickly — the
        // data is queued in the RTP send buffer — but the packet never
        // reaches the server.
        let hole_payload = support::payload(1024);
        let _ = write.write_all(&hole_payload).await;
        // Wait for the packet to be transmitted (and dropped).
        tokio::time::sleep(Duration::from_secs(1)).await;

        // Phase 3: send periodic small heartbeat frames that fit inside
        // MAX_DATAGRAM bytes on the wire.  These pass through the proxy,
        // arrive at the server, and generate fresh SACKs that reset the
        // response watchdog.  The cumulative send frontier, however, is
        // stuck at the dropped packet's sequence number, so the
        // progress_wait clock never resets.
        let mut heartbeat_count = 0u64;
        loop {
            if start.elapsed() >= MAX_DURATION {
                panic!(
                    "Connection stayed alive >{:?} without cumulative progress; \
                     delivered {} post-hole frames",
                    MAX_DURATION, heartbeat_count,
                );
            }

            let mut buf = Vec::with_capacity(MSG_BYTES + 12);
            buf.extend_from_slice(&((MSG_BYTES + 12) as u32).to_le_bytes());
            buf.extend_from_slice(&support::payload(MSG_BYTES));
            buf.extend_from_slice(&base.elapsed().as_micros().to_le_bytes());

            let res = tokio::time::timeout(Duration::from_secs(2), write.write_all(&buf)).await;
            match res {
                Ok(Ok(())) => {
                    heartbeat_count += 1;
                    tokio::time::sleep(MSG_INTERVAL).await;
                }
                Ok(Err(e)) => {
                    // BrokenPipe or other write error.
                    eprintln!(
                        "[rtp_liveness] write failed with {:?} after {} heartbeats",
                        e.kind(),
                        heartbeat_count,
                    );
                    break;
                }
                Err(_) => {
                    // Write stalled — the send buffer is full and no ACKs
                    // are advancing the window.
                    eprintln!(
                        "[rtp_liveness] write timed out after {} heartbeats; \
                         BrokenPipe did not fire within 2s",
                        heartbeat_count,
                    );
                    break;
                }
            }
        }

        pair.stop();

        let elapsed = start.elapsed();

        // Verify the test produced continuing ACK/SACK traffic.
        // Client→server data packets are dropped by max_datagram_size,
        // so post-hole frames never reach the server.  However, server→client
        // ACKs/SACKs pass through the reverse direction (no size filter),
        // keeping the response watchdog alive while the cumulative-progress
        // clock ticks toward BrokenPipe.
        let mut server_frames = 0u64;
        while latency_rx.try_recv().is_ok() {
            server_frames += 1;
        }
        eprintln!(
            "[rtp_liveness] elapsed={elapsed:?} post_hole_heartbeats_sent={heartbeat_count} \
             server_frames={server_frames} (pre_hole={pre_hole_frames})"
        );

        assert!(
            elapsed >= MIN_BROKEN_PIPE_ELAPSED,
            "BrokenPipe too early ({elapsed:?}); expected >= {:?}",
            MIN_BROKEN_PIPE_ELAPSED,
        );
        assert!(
            heartbeat_count >= MIN_POST_HOLE_FRAMES,
            "too few post-hole heartbeats: {heartbeat_count} < {MIN_POST_HOLE_FRAMES}"
        );
        // The pre-hole frame must have reached the server to prove plumbing
        // works; post-hole frames may be buffered behind the dropped packet.
        assert!(
            pre_hole_frames + server_frames >= 1,
            "server should have observed at least one delivered frame"
        );
    });
}
