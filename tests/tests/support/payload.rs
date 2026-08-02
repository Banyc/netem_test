// ─────────────────────────── deterministic payload ───────────────────────

use std::time::Duration;

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
