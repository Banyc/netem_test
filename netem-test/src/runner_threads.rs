//! Drop-scoped owner for the per-link runner threads.
//!
//! The link owner (`NetemLink` / `NetemPair`) holds exactly one
//! [`RunnerThreads`]: spawning a direction thread immediately adopts it, and
//! the owner's `stop` / `Drop` path signals the shared stop flag, collects
//! *every* join result, and only then unwraps any one of them — so one panicked
//! runner cannot detach a sibling before it is joined.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Debug, Default)]
pub(crate) struct RunnerThreads {
    stop: Arc<AtomicBool>,
    threads: Mutex<Vec<std::thread::JoinHandle<()>>>,
}

impl RunnerThreads {
    pub(crate) fn new() -> Self {
        Self {
            stop: Arc::new(AtomicBool::new(false)),
            threads: Mutex::new(Vec::new()),
        }
    }

    /// The shared stop flag handed to every spawned runner: the runner's
    /// receive loops exit when it flips, and no runner ever owns it alone.
    pub(crate) fn stop_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.stop)
    }

    /// Adopt a successfully spawned direction thread into this owner so its
    /// join result is collected on stop / drop.
    pub(crate) fn adopt(&self, thread: std::thread::JoinHandle<()>) {
        self.threads.lock().unwrap().push(thread);
    }

    /// Signal every runner to stop, then collect all join results before
    /// unwrapping any one of them (one panic cannot detach a sibling).
    /// Idempotent: subsequent calls drain an empty thread list.
    pub(crate) fn stop_and_join(&self) {
        self.stop.store(true, Ordering::Relaxed);
        let results = {
            let mut threads = self.threads.lock().unwrap();
            threads
                .drain(..)
                .map(std::thread::JoinHandle::join)
                .collect::<Vec<_>>()
        };
        for result in results {
            result.unwrap();
        }
    }
}

impl Drop for RunnerThreads {
    fn drop(&mut self) {
        self.stop_and_join();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    fn adopted_waiting_thread(runners: &RunnerThreads) -> mpsc::Receiver<()> {
        let (started_tx, started_rx) = mpsc::channel();
        let stop = runners.stop_flag();
        let thread = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            while !stop.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(1));
            }
        });
        runners.adopt(thread);
        started_rx
    }

    #[test]
    fn drop_signals_and_joins_every_adopted_thread() {
        let runners = RunnerThreads::new();
        adopted_waiting_thread(&runners)
            .recv_timeout(Duration::from_secs(5))
            .expect("adopted thread never started");
        adopted_waiting_thread(&runners)
            .recv_timeout(Duration::from_secs(5))
            .expect("adopted thread never started");
        // Dropping must signal the stop flag and join both adopted threads
        // (each blocks on the stop flag, so a join that never signals would
        // hang this test).
        drop(runners);
    }

    #[test]
    fn explicit_stop_is_idempotent() {
        let runners = RunnerThreads::new();
        adopted_waiting_thread(&runners)
            .recv_timeout(Duration::from_secs(5))
            .expect("adopted thread never started");
        runners.stop_and_join();
        // A second stop must not panic or double-join anything.
        runners.stop_and_join();
        drop(runners);
    }
}
