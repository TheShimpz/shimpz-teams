//! Content-free Team work activity, for the CLI's scheduled release reconciliation.
//!
//! The server counts in-flight mutating requests (and automatic Assistant updates count themselves), and keeps a quiet
//! window after the last Supervisor-authorized mutation or chat request. The window is process memory on the monotonic
//! clock, so a restart forgets it.

use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use tokio::time::Instant;

/// How long Team stays busy after a Supervisor's last mutation.
pub const QUIET: Duration = Duration::from_secs(180);

/// The activity counters.
#[derive(Debug, Default)]
pub struct Activity {
    state: Mutex<State>,
}

#[derive(Debug, Default)]
struct State {
    active: usize,
    supervised_at: Option<Instant>,
}

/// One unit of in-flight work; Team is busy until it drops.
#[derive(Debug)]
pub struct Working<'a>(&'a Activity);

impl Drop for Working<'_> {
    fn drop(&mut self) {
        let mut state = self.0.lock();
        state.active = state.active.saturating_sub(1);
    }
}

/// One unit of in-flight work that owns its counter, for work that outlives the request handler (a stream).
#[derive(Debug)]
pub struct OwnedWorking(std::sync::Arc<Activity>);

impl Drop for OwnedWorking {
    fn drop(&mut self) {
        let mut state = self.0.lock();
        state.active = state.active.saturating_sub(1);
    }
}

impl Activity {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Starts one unit of work.
    #[must_use]
    pub fn working(&self) -> Working<'_> {
        self.lock().active += 1;
        Working(self)
    }

    /// Starts one unit of work held by `activity` itself.
    #[must_use]
    pub fn owned_working(activity: &std::sync::Arc<Self>) -> OwnedWorking {
        activity.lock().active += 1;
        OwnedWorking(std::sync::Arc::clone(activity))
    }

    /// Records a Supervisor-authorized mutation or chat request: Team stays busy for [`QUIET`] after it.
    pub fn supervised(&self) {
        self.lock().supervised_at = Some(Instant::now());
    }

    /// `busy` while work is in flight or within the quiet window, otherwise `idle`.
    #[must_use]
    pub fn state(&self) -> &'static str {
        let state = self.lock();
        let quiet = state.supervised_at.is_some_and(|at| at.elapsed() < QUIET);
        if state.active > 0 || quiet {
            "busy"
        } else {
            "idle"
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::Duration;

    use super::{Activity, QUIET};

    #[tokio::test(start_paused = true)]
    async fn in_flight_work_and_the_quiet_window_are_busy() {
        let activity = Arc::new(Activity::default());
        assert_eq!(activity.state(), "idle");
        let working = activity.working();
        assert_eq!(activity.state(), "busy");
        drop(working);
        let owned = Activity::owned_working(&activity);
        assert_eq!(activity.state(), "busy");
        drop(owned);
        assert_eq!(activity.state(), "idle");
        assert_eq!(QUIET, Duration::from_secs(180));
        activity.supervised();
        tokio::time::advance(Duration::from_secs(179)).await;
        assert_eq!(activity.state(), "busy");
        tokio::time::advance(Duration::from_secs(1)).await;
        assert_eq!(activity.state(), "idle");
    }
}
