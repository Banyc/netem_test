//! An actively-polled root [`TestScope`] for the netem scenario tests.

use std::{
    future::Future,
    ops::{Deref, DerefMut},
};

/// An actively-polled scope of test-owned background tasks. The test body
/// runs through [`TestScope::run`], which races it against `join_next()` on
/// the scope, so a background task that panics (in particular one that
/// unwraps a panicked child join) fails the test immediately instead of
/// being observed only when the scope is dropped. [`Self::run`] owns the
/// final epilog: after the body completes, admission closes, already-queued
/// futures are adopted, live children are aborted, non-cancelled joins are
/// unwrapped, and the reapers are joined before returning.
///
/// The wrapped `JoinSet` is exposed (via the `Deref` impls) so existing test
/// code can keep calling `&mut tasks` / `tasks.spawn(..)` /
/// `tasks.join_next()` unchanged.
pub(crate) struct TestScope {
    pub(crate) tasks: tokio::task::JoinSet<()>,
    reapers: tokio::task::JoinSet<()>,
    reaper_shutdown: tokio::sync::watch::Sender<bool>,
}

impl Deref for TestScope {
    type Target = tokio::task::JoinSet<()>;
    fn deref(&self) -> &Self::Target {
        &self.tasks
    }
}

// Deref-mut lets existing test code keep calling `&mut tasks` /
// `tasks.spawn(..)` / `tasks.join_next()` unchanged while the scope adds the
// actively-polled `run` wrapper around the test body.
impl DerefMut for TestScope {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.tasks
    }
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
    /// immediately rather than waiting for the measurement body. Keep a
    /// sender clone alive for the channel to stay open.
    pub(crate) fn submitter(&mut self, bound: usize) -> tokio::sync::mpsc::Sender<super::TestTask> {
        super::spawn_test_task_reaper_with_shutdown(
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
                    // A background task exited before the body. Re-raise any
                    // panic it surfaced immediately; a normal completion is a
                    // legitimate shutdown (e.g. a keepalive reader ending when
                    // its session closes) and is drained silently.
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
        super::abort_and_reap_test_tasks(&mut self.tasks).await;
        self.reaper_shutdown.send_replace(true);
        while let Some(joined) = self.reapers.join_next().await {
            joined.unwrap();
        }
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn run_reaps_root_and_submitted_tasks_before_returning() {
        let mut scope = TestScope::new();
        let root_started = std::sync::Arc::new(tokio::sync::Notify::new());
        let root_started_clone = std::sync::Arc::clone(&root_started);
        scope.spawn_required("parked root task", async move {
            root_started_clone.notify_one();
            std::future::pending::<()>().await;
        });
        let tx = scope.submitter(4);
        let submitted_started = std::sync::Arc::new(tokio::sync::Notify::new());
        let submitted_started_clone = std::sync::Arc::clone(&submitted_started);
        super::super::submit_test_task(
            &tx,
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
        let tx = scope.submitter(4);
        let panicked = std::sync::Arc::new(tokio::sync::Notify::new());
        let panicked_clone = std::sync::Arc::clone(&panicked);
        super::super::submit_test_task(
            &tx,
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
