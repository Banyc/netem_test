//! An actively-polled root [`TestScope`] for the netem scenario tests.

use std::future::Future;

/// An actively-polled scope of test-owned background tasks. The test body
/// runs through [`TestScope::run`], which races it against `join_next()` on
/// the scope, so a background task that panics (in particular one that
/// unwraps a panicked child join) fails the test immediately instead of
/// being observed only when the scope is dropped. Background tasks that end
/// normally are drained silently (legitimate shutdowns); dropping the scope
/// remains the abort backstop for tasks still running when the body
/// completes. [`Self::run`] reaps the scope, but the [`Self::submitter`]
/// reaper covers tasks submitted through the handle even before `run`.
///
/// The wrapped `JoinSet` is exposed (via the `Deref` impls) so existing test
/// code can keep calling `&mut tasks` / `tasks.spawn(..)` /
/// `tasks.join_next()` unchanged.
pub(crate) struct TestScope {
    pub(crate) tasks: tokio::task::JoinSet<()>,
}

impl std::ops::Deref for TestScope {
    type Target = tokio::task::JoinSet<()>;
    fn deref(&self) -> &Self::Target {
        &self.tasks
    }
}

// Deref-mut lets existing test code keep calling `&mut tasks` /
// `tasks.spawn(..)` / `tasks.join_next()` unchanged while the scope adds the
// actively-polled `run` wrapper around the test body.
impl std::ops::DerefMut for TestScope {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.tasks
    }
}

impl TestScope {
    pub(crate) fn new() -> Self {
        Self {
            tasks: tokio::task::JoinSet::new(),
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
    /// submitted child from the moment it is created — including during
    /// setup, before [`Self::run`] begins — so a panicked child surfaces
    /// via the reaper instead of being hidden until the scope is reaped.
    /// Keep a sender clone alive for the channel to stay open.
    pub(crate) fn submitter(&mut self, bound: usize) -> tokio::sync::mpsc::Sender<super::TestTask> {
        super::spawn_test_task_reaper(&mut self.tasks, bound)
    }

    pub(crate) async fn run<F: Future>(mut self, body: F) -> F::Output {
        tokio::pin!(body);
        loop {
            tokio::select! {
                biased;
                joined = self.tasks.join_next(), if !self.tasks.is_empty() => {
                    // A background task exited before the body. Re-raise any
                    // panic it surfaced immediately; a normal completion is a
                    // legitimate shutdown (e.g. a keepalive reader ending when
                    // its session closes) and is drained silently.
                    let joined = joined.expect("background task exists");
                    joined.unwrap();
                }
                value = &mut body => {
                    // The body completed. Drain tasks that exited in the same
                    // poll cycle so a required task that ended right as the
                    // body finished still fails the test.
                    while let Some(joined) = self.tasks.try_join_next() {
                        joined.unwrap();
                    }
                    return value;
                }
            }
        }
    }
}
