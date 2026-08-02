// ───────────────────────────── queued packet ───────────────────────────

use std::net::SocketAddr;
use std::time::Instant;

#[derive(Clone)]
pub(crate) struct Queued {
    pub(crate) time_to_send: Instant,
    /// Monotonic insertion sequence used as a tiebreaker so the min-heap
    /// preserves FIFO order among packets with equal `time_to_send`. Without
    /// this, `BinaryHeap` returns equal-timestamp packets in arbitrary order,
    /// which reorders the byte stream and trips the reliable layer.
    pub(crate) seq: u64,
    pub(crate) data: Vec<u8>,
    pub(crate) dst: SocketAddr,
}

impl PartialEq for Queued {
    fn eq(&self, other: &Self) -> bool {
        (self.time_to_send, self.seq) == (other.time_to_send, other.seq)
    }
}

impl Eq for Queued {}

impl PartialOrd for Queued {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Queued {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.time_to_send
            .cmp(&other.time_to_send)
            .then(self.seq.cmp(&other.seq))
    }
}
