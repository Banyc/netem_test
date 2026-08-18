//! An actively-polled root [`TestScope`] for the netem scenario tests.

use std::future::Future;

/// A boxed test-owned task future: session supervisors, per-stream sinks,
/// and rtp_mux drivers all submit through [`spawn_test_task_reaper_with_shutdown`].
pub(crate) type TestTask =
    std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'static>>;

/// Submission handle for the bounded test-task reaper.  Keep one clone alive
/// for the channel to stay open; [`submit_test_task`] /
/// [`submit_test_task_required`] are the only admission paths, so a full or
/// closed channel always fails the test instead of silently dropping work.
#[derive(Clone)]
pub(crate) struct TestTaskSubmitter {
    tx: tokio::sync::mpsc::Sender<TestTask>,
}

/// An actively-polled scope of test-owned background tasks. The test body
/// runs through [`TestScope::run`], which races it against `join_next()` on
/// the scope, so a background task that panics (in particular one that
/// unwraps a panicked child join) fails the test immediately instead of
/// being observed only when the scope is dropped. [`Self::run`] owns the
/// final epilog: after the body completes, admission closes, already-queued
/// futures are adopted, live children are aborted, non-cancelled joins are
/// unwrapped, and the reapers are joined before returning.
pub(crate) struct TestScope {
    tasks: tokio::task::JoinSet<()>,
    reapers: tokio::task::JoinSet<()>,
    reaper_shutdown: tokio::sync::watch::Sender<bool>,
}

impl TestScope {
    pub(crate) fn new() -> Self {
        let (reaper_shutdown, _) = tokio::sync::watch::channel(false);
        Self {
            tasks: tokio::task::JoinSet::new(),
            reapers: tokio::task::JoinSet::new(),
            reaper_shutdown,
        }
    }

    /// Spawn an ordinary root task into this scope.  The task's panic (or a
    /// panicked child join) fails the test when [`Self::run`] polls it.
    pub(crate) fn spawn(&mut self, task: impl Future<Output = ()> + Send + 'static) {
        self.tasks.spawn(task);
    }

    /// Spawn a task that must stay alive for the whole [`TestScope::run`] body.
    /// A normal completion while the body is still running panics the test
    /// with a message naming the task (the wrapper turns the completion into
    /// a panic); a panic inside the future propagates unchanged.
    pub(crate) fn spawn_required(
        &mut self,
        name: &'static str,
        future: impl Future<Output = ()> + Send + 'static,
    ) {
        self.tasks.spawn(async move {
            future.await;
            panic!("required task '{name}' exited before the test body completed");
        });
    }

    /// Spawn the bounded inner reaper into this scope and return its
    /// submission handle. The reaper actively drives (and unwraps) every
    /// submitted child from the moment it is created, so a panicked child
    /// aborts the reaper immediately; the reaper's own `JoinError` is then
    /// observed by [`Self::run`] as soon as it begins polling the scope.
    /// Setup should therefore happen inside `run`'s body (submitting dynamic
    /// children through this handle) so a setup-time failure surfaces
    /// immediately rather than waiting for the measurement body.
    pub(crate) fn submitter(&mut self, bound: usize) -> TestTaskSubmitter {
        spawn_test_task_reaper_with_shutdown(
            &mut self.reapers,
            bound,
            self.reaper_shutdown.subscribe(),
        )
    }

    pub(crate) async fn run<F: Future>(mut self, body: F) -> F::Output {
        tokio::pin!(body);
        let value = loop {
            let selected = tokio::select! {
                biased;
                joined = self.tasks.join_next(), if !self.tasks.is_empty() => {
                    let joined = joined.expect("background task exists");
                    joined.unwrap();
                    None
                }
                joined = self.reapers.join_next(), if !self.reapers.is_empty() => {
                    joined.expect("task reaper exists").unwrap();
                    panic!("test task reaper stopped before the test body completed");
                }
                value = &mut body => Some(value),
            };
            if let Some(value) = selected {
                break value;
            }
        };
        // Final epilog: close admission and reap every child before
        // returning so no completed value, error, or panic is hidden.
        abort_and_reap_test_tasks(&mut self.tasks).await;
        self.reaper_shutdown.send_replace(true);
        while let Some(joined) = self.reapers.join_next().await {
            joined.unwrap();
        }
        value
    }
}

/// Add the bounded reaper to `reapers` and return its submission handle.  The
/// reaper selects between new submissions and `join_next()` completions,
/// unwrapping every completion so panics surface immediately.  On shutdown it
/// closes admission, adopts any future still queued (a submitted future is
/// never silently dropped), and reaps the owned set.
pub(crate) fn spawn_test_task_reaper_with_shutdown(
    reapers: &mut tokio::task::JoinSet<()>,
    bound: usize,
    mut shutdown_rx: tokio::sync::watch::Receiver<bool>,
) -> TestTaskSubmitter {
    let (tx, mut rx) = tokio::sync::mpsc::channel::<TestTask>(bound);
    reapers.spawn(async move {
        let mut owned = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                biased;
                Some(fut) = rx.recv() => {
                    owned.spawn(fut);
                }
                Some(joined) = owned.join_next() => {
                    joined.unwrap();
                }
                _ = shutdown_rx.changed() => break,
                else => break,
            }
        }
        // The scope is shutting down: close admission, adopt every future
        // still queued (a submitted future is never silently dropped), and
        // reap the owned set so a completed panic still surfaces.
        rx.close();
        while let Ok(fut) = rx.try_recv() {
            owned.spawn(fut);
        }
        abort_and_reap_test_tasks(&mut owned).await;
    });
    TestTaskSubmitter { tx }
}

/// Abort and reap every child of `tasks`: owner-requested cancellation is
/// expected (cancelled joins are skipped), but a child that already
/// completed — including a panic — is unwrapped and re-raised.
pub(crate) async fn abort_and_reap_test_tasks(tasks: &mut tokio::task::JoinSet<()>) {
    tasks.abort_all();
    while let Some(joined) = tasks.join_next().await {
        if joined
            .as_ref()
            .is_err_and(tokio::task::JoinError::is_cancelled)
        {
            continue;
        }
        joined.unwrap();
    }
}

/// Submit a test-owned task future; panics if the bounded submission
/// channel is full (the reaper is not draining) or closed (the reaper
/// stopped unexpectedly), mirroring [`try_send_observation`]. A closed
/// channel must fail the test too: silently dropping the submitted future
/// would hide a task the test is relying on.
pub(crate) fn submit_test_task(tx: &TestTaskSubmitter, fut: TestTask) {
    match tx.tx.try_send(fut) {
        Ok(()) => {}
        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
            panic!("test task submission channel is full; the reaper is not draining")
        }
        Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
            panic!("test task reaper stopped unexpectedly")
        }
    }
}

/// Submit a REQUIRED test-owned task: like [`submit_test_task`] but wraps
/// the future so that completing before the test body does panics the
/// reaper (mirroring [`TestScope::spawn_required`] for the bounded
/// submission handle, for helpers called inside `run` bodies).
pub(crate) fn submit_test_task_required(
    tx: &TestTaskSubmitter,
    name: &'static str,
    fut: impl std::future::Future<Output = ()> + Send + 'static,
) {
    submit_test_task(
        tx,
        Box::pin(async move {
            fut.await;
            panic!("required task '{name}' exited before the test body completed");
        }),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[should_panic(expected = "test task submission channel is full; the reaper is not draining")]
    fn submit_test_task_panics_when_channel_is_full() {
        let (tx, _rx) = tokio::sync::mpsc::channel::<TestTask>(1);
        let parked = Box::pin(std::future::pending::<()>());
        submit_test_task(&TestTaskSubmitter { tx: tx.clone() }, parked);
        let second = Box::pin(std::future::pending::<()>());
        // The receiver is never polled, so the single slot stays occupied and
        // the second submission must panic instead of being dropped silently.
        submit_test_task(&TestTaskSubmitter { tx }, second);
    }

    #[test]
    #[should_panic(expected = "test task reaper stopped unexpectedly")]
    fn submit_test_task_panics_when_channel_is_closed() {
        let (tx, rx) = tokio::sync::mpsc::channel::<TestTask>(1);
        drop(rx);
        let fut = Box::pin(std::future::pending::<()>());
        // The reaper stopped (the receiver is gone); submitting must panic
        // rather than silently dropping the future.
        submit_test_task(&TestTaskSubmitter { tx }, fut);
    }

    #[tokio::test]
    async fn run_reaps_root_and_submitted_tasks_before_returning() {
        let mut scope = TestScope::new();
        let root_started = std::sync::Arc::new(tokio::sync::Notify::new());
        let root_started_clone = std::sync::Arc::clone(&root_started);
        scope.spawn_required("parked root task", async move {
            root_started_clone.notify_one();
            std::future::pending::<()>().await;
        });
        let submitter = scope.submitter(4);
        let submitted_started = std::sync::Arc::new(tokio::sync::Notify::new());
        let submitted_started_clone = std::sync::Arc::clone(&submitted_started);
        submit_test_task(
            &submitter,
            Box::pin(async move {
                submitted_started_clone.notify_one();
                std::future::pending::<()>().await;
            }),
        );
        let value = scope
            .run(async {
                // Let the background tasks actually start so the reaping
                // epilog has live children to abort.
                tokio::time::timeout(std::time::Duration::from_secs(5), async {
                    root_started.notified().await;
                    submitted_started.notified().await;
                })
                .await
                .expect("background tasks never started");
                42
            })
            .await;
        assert_eq!(value, 42);
    }

    #[tokio::test]
    #[should_panic(expected = "submitted panic")]
    async fn run_cascades_a_submitted_panic_that_beat_shutdown() {
        let mut scope = TestScope::new();
        let submitter = scope.submitter(4);
        let panicked = std::sync::Arc::new(tokio::sync::Notify::new());
        let panicked_clone = std::sync::Arc::clone(&panicked);
        submit_test_task(
            &submitter,
            Box::pin(async move {
                panicked_clone.notify_waiters();
                panic!("submitted panic");
            }),
        );
        scope
            .run(async {
                // Wait for the submitted child to panic (beating the final
                // shutdown) so the reaper unwraps it and the panic cascades.
                tokio::time::timeout(std::time::Duration::from_secs(5), panicked.notified())
                    .await
                    .expect("submitted child never ran");
                tokio::task::yield_now().await;
            })
            .await;
    }
}
