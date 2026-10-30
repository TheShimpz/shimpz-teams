//! The Stop of one turn, Routine segment, Browser task, or preview: set once, observed by everything it ends.
//!
//! A chat Stop, a Team's destruction, and the Space reset end work by setting its `Stop`; the work itself observes it
//! (it aborts its Brain request, fail-stops its executing Action, removes its Browser) and then releases what it
//! holds. Setting it is idempotent and never waits.

use std::sync::Arc;

use tokio::sync::watch;

/// One cancellable unit of work's stop signal. Clones observe the same signal.
#[derive(Clone, Debug)]
pub struct Stop {
    sender: Arc<watch::Sender<bool>>,
}

impl Stop {
    /// A signal not yet set.
    #[must_use]
    pub fn new() -> Self {
        let (sender, _receiver) = watch::channel(false);
        Self {
            sender: Arc::new(sender),
        }
    }

    /// Sets the signal; every waiter of [`Stop::stopped`] wakes.
    pub fn stop(&self) {
        self.sender.send_replace(true);
    }

    /// Whether the signal is set.
    #[must_use]
    pub fn is_stopped(&self) -> bool {
        *self.sender.borrow()
    }

    /// Resolves once the signal is set (at once when it already is).
    pub async fn stopped(&self) {
        let mut receiver = self.sender.subscribe();
        // The sender lives as long as `self`, so the wait ends only by the value turning true.
        let _ = receiver.wait_for(|stopped| *stopped).await;
    }
}

impl Default for Stop {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::Stop;

    #[tokio::test]
    async fn a_waiter_wakes_when_any_clone_stops() {
        let stop = Stop::new();
        let observer = stop.clone();
        let waiter = tokio::spawn(async move { observer.stopped().await });
        assert!(!stop.is_stopped());
        stop.stop();
        stop.stop();
        waiter.await.expect("the waiter ends");
        assert!(stop.is_stopped());
        // Already set: resolves at once.
        stop.stopped().await;
    }
}
