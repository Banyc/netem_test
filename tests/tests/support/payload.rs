// ─────────────────────────── deterministic payload ───────────────────────

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

/// Upper bound on the implicit runtime teardown in [`run_bounded`].
const RUNTIME_SHUTDOWN: Duration = Duration::from_secs(15);

/// Generate a deterministic payload of `n` bytes. The modulus is a prime so
/// the byte pattern is non-trivial and reproducible across runs.
pub fn payload(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i % 251) as u8).collect()
}

/// Deterministic payload whose length is a multiple of the 251-byte pattern
/// period. When the caller loops over this buffer with partial writes, the
/// wrapped stream still matches the `(global_offset % 251)` pattern expected by
/// the byte-counting sinks.
pub fn cyclic_payload(n: usize) -> Vec<u8> {
    let n = n - (n % 251);
    payload(n)
}

// ─────────────────────────── timeout wrapper ─────────────────────────────

/// Run `fut` to completion, panicking with `label` if it does not finish
/// within `dur`. This is the timeout guard every ignored async test uses so
/// failures surface instead of hanging the harness.
pub async fn with_timeout<T, F>(dur: Duration, label: &str, fut: F) -> T
where
    F: std::future::Future<Output = T>,
{
    tokio::time::timeout(dur, fut)
        .await
        .unwrap_or_else(|_| panic!("timeout ({dur:?}) waiting for {label}"))
}

/// Disarms a detached wall-clock deadline when dropped.  Declared before the
/// runtime in [`run_bounded`] so, on unwind, the runtime drops while the
/// deadline is still armed and it keeps covering [`Runtime::drop`].
struct DeadlineGuard {
    expired: Arc<AtomicBool>,
}

impl Drop for DeadlineGuard {
    fn drop(&mut self) {
        self.expired.store(true, Ordering::Relaxed);
    }
}

/// Run an async test body on an explicit multi-thread runtime whose teardown
/// is bounded, under a detached wall-clock deadline.
///
/// `#[tokio::test]` builds and drops its runtime *inside* the generated
/// function, after the async body returns, so a guard living in the body is
/// already disarmed when `Runtime::drop` waits on an `rtp` driver that never
/// yields (a single synchronous `promote_due` pass over a large in-flight
/// set, which `Runtime::drop` cannot preempt) — the residual multi-minute
/// hang.  Building the runtime here keeps the deadline in the caller's sync
/// frame, so it also covers that teardown, and `shutdown_timeout` bounds the
/// drop instead of parking forever.
pub fn run_bounded<T>(
    label: &'static str,
    limit: Duration,
    body: impl std::future::Future<Output = T>,
) -> T {
    let _deadline = {
        let expired = Arc::new(AtomicBool::new(false));
        let thread_expired = Arc::clone(&expired);
        std::thread::spawn(move || {
            let start = Instant::now();
            while !thread_expired.load(Ordering::Relaxed) {
                if start.elapsed() >= limit {
                    eprintln!("===== test deadline {limit:?} exceeded: {label} =====");
                    eprintln!(
                        "  tokio teardown cannot stop a driver that never yields; aborting the \
                         test binary so a stalled run cannot hang the suite"
                    );
                    std::process::abort();
                }
                std::thread::sleep(Duration::from_millis(200));
            }
        });
        DeadlineGuard { expired }
    };
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("multi-thread runtime");
    let outcome = runtime.block_on(body);
    runtime.shutdown_timeout(RUNTIME_SHUTDOWN);
    outcome
}
