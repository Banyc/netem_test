//! An actively-polled root [`TestScope`] for the netem scenario tests.

use std::future::Future;

/// An actively-polled scope of test-owned background tasks. The test body
/// runs through [`TestScope::run`], which races it against `join_next()` on
/// the scope, so a background task that panics (in particular one that
/// unwraps a panicked child join) fails the test immediately instead of
/// being observed only when the scope is dropped. Background tasks that end
/// normally are drained silently (legitimate shutdowns); dropping the scope
/// remains the abort backstop for tasks still running when the body
/// completes.
///
/// The wrapped `JoinSet` is exposed so the support helpers that still take
/// `&mut tokio::task::JoinSet<()>` can be driven with `&mut scope.tasks`.
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

    pub(crate) async fn run<F: Future>(&mut self, body: F) -> F::Output {
        tokio::pin!(body);
        loop {
            tokio::select! {
                value = &mut body => return value,
                joined = self.tasks.join_next(), if !self.tasks.is_empty() => {
                    // A background task exited before the body. Re-raise any
                    // panic it surfaced immediately; a normal completion is a
                    // legitimate shutdown (e.g. a keepalive reader ending when
                    // its session closes) and is drained silently.
                    let joined = joined.expect("background task exists");
                    joined.expect("a background task panicked");
                }
            }
        }
    }
}
