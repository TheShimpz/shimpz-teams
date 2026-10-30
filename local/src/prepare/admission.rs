//! The controller's one preparation slot (ADR-0093). Local's 256 MiB controller holds at most one preparation's
//! originals, text, and helper at a time, from before the first original is read until the helper is gone.
//!
//! A chat turn or Routine segment waits for the slot until its Stop. A file preview must answer promptly: at most
//! four wait at once, each at most 10 s, and one ended by its Team's teardown or the Space reset leaves at once.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use team_core::stop::Stop;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{Instant, timeout};

use crate::coordination::previews::Preview;

/// The most previews that wait for the slot at once.
pub const PREVIEW_QUEUE: usize = 4;
/// How long one preview waits for the slot.
pub const PREVIEW_DEADLINE: Duration = Duration::from_secs(10);
/// How often a waiting preview notices that it was ended.
pub const PREVIEW_POLL: Duration = Duration::from_millis(100);

/// Why a preview did not get the slot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreviewWait {
    /// The queue is full, or the deadline passed.
    Busy,
    /// The preview was ended while it waited.
    Ended,
}

/// The slot was not taken because the turn was stopped.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stopped;

/// The controller's preparation slot.
#[derive(Debug)]
pub struct Admission {
    slot: Arc<Semaphore>,
    waiting: AtomicUsize,
}

impl Default for Admission {
    fn default() -> Self {
        Self::new()
    }
}

impl Admission {
    /// A free slot.
    #[must_use]
    pub fn new() -> Self {
        Self {
            slot: Arc::new(Semaphore::new(1)),
            waiting: AtomicUsize::new(0),
        }
    }

    /// Holds the slot for a turn's segment, leaving the wait as soon as `stop` is set.
    ///
    /// # Errors
    /// [`Stopped`] when the turn was stopped first.
    pub async fn turn(&self, stop: &Stop) -> Result<OwnedSemaphorePermit, Stopped> {
        if stop.is_stopped() {
            return Err(Stopped);
        }
        tokio::select! {
            biased;
            () = stop.stopped() => Err(Stopped),
            permit = Arc::clone(&self.slot).acquire_owned() => permit.map_err(|_| Stopped),
        }
    }

    /// Holds the slot for a preview within the queue and deadline, leaving once the preview is ended.
    ///
    /// # Errors
    /// [`PreviewWait`] when it is busy or ended.
    pub async fn preview(&self, preview: &Preview) -> Result<OwnedSemaphorePermit, PreviewWait> {
        let admitted = self
            .waiting
            .try_update(Ordering::SeqCst, Ordering::SeqCst, |waiting| {
                (waiting < PREVIEW_QUEUE).then_some(waiting + 1)
            });
        if admitted.is_err() {
            return Err(PreviewWait::Busy);
        }
        let waited = self.wait(preview).await;
        self.waiting.fetch_sub(1, Ordering::SeqCst);
        waited
    }

    async fn wait(&self, preview: &Preview) -> Result<OwnedSemaphorePermit, PreviewWait> {
        let ends = Instant::now() + PREVIEW_DEADLINE;
        loop {
            if let Ok(permit) = timeout(PREVIEW_POLL, Arc::clone(&self.slot).acquire_owned()).await
            {
                return permit.map_err(|_| PreviewWait::Busy);
            }
            preview.check().map_err(|_| PreviewWait::Ended)?;
            if Instant::now() >= ends {
                return Err(PreviewWait::Busy);
            }
        }
    }
}

#[cfg(test)]
mod tests;
