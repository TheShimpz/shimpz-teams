//! Every file preview and download under way, each bound to its Team for as long as it runs (ADR-0093).
//!
//! A Team's destruction and the Space reset each close the book for their whole duration: no preview or download of
//! it opens, every running one is ended, a running preview's helper is removed, and they wait until every ended one
//! left, so none can start a helper or send a file after they finished. A download whose last check passed before the
//! close finishes sending first; its next check fails.

use std::collections::HashMap;
use std::pin::pin;
use std::sync::{Arc, Mutex as StdMutex, MutexGuard, PoisonError};
use std::time::Duration;

use team_core::future::BoxFuture;
use tokio::sync::Notify;
use tokio::time::{Instant, timeout_at};

/// How long a close waits for the ended previews to leave.
pub const PREVIEW_DRAIN: Duration = Duration::from_secs(30);

/// The preview was ended by its Team's teardown or the Space reset, or its Team's previews are closed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PreviewEnded;

/// A close could not finish.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreviewDrainError {
    /// A running preview's helper could not be removed.
    HelperNotRemoved,
    /// An ended preview did not leave in time.
    Timeout,
}

/// A helper's removal failed (an already absent helper is a success).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RemoveFailed;

/// Removes a preparation helper container.
pub trait HelperRemover: Send + Sync {
    /// Force-removes `container`; succeeds when it is already absent.
    ///
    /// # Errors
    /// [`RemoveFailed`] when it may still exist.
    fn remove<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), RemoveFailed>>;
}

/// The book of previews.
#[derive(Clone, Debug, Default)]
pub struct PreviewBook {
    inner: Arc<Inner>,
}

#[derive(Debug, Default)]
struct Inner {
    state: StdMutex<State>,
    left: Notify,
}

#[derive(Debug, Default)]
struct State {
    next: u64,
    previews: HashMap<u64, Entry>,
    // A Team's id, or `None` for the whole Space, by how many closes hold it.
    closed: HashMap<Option<String>, usize>,
}

#[derive(Debug)]
struct Entry {
    team_id: String,
    ended: bool,
    container: Option<String>,
}

impl PreviewBook {
    /// An empty book.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// Opens one preview or download of `team_id`.
    ///
    /// # Errors
    /// [`PreviewEnded`] while the Team's or the Space's previews are closed.
    pub fn open(&self, team_id: &str) -> Result<Preview, PreviewEnded> {
        let mut state = self.state();
        if state.closed.contains_key(&Some(team_id.to_owned())) || state.closed.contains_key(&None)
        {
            return Err(PreviewEnded);
        }
        let id = state.next;
        state.next += 1;
        state.previews.insert(
            id,
            Entry {
                team_id: team_id.to_owned(),
                ended: false,
                container: None,
            },
        );
        Ok(Preview {
            book: self.clone(),
            id,
        })
    }

    /// Closes `team_id`'s previews, or the whole Space's with `None`: ends every one under way, removes their running
    /// helpers, and waits up to `wait` for them to leave. The previews stay closed until the guard drops.
    ///
    /// # Errors
    /// [`PreviewDrainError`] when a helper could not be removed or a preview did not leave in time; the close then
    /// ends at once.
    pub async fn close(
        &self,
        team_id: Option<&str>,
        remover: &dyn HelperRemover,
        wait: Duration,
    ) -> Result<PreviewsClosed, PreviewDrainError> {
        let key = team_id.map(str::to_owned);
        let (ending, containers) = {
            let mut state = self.state();
            *state.closed.entry(key.clone()).or_default() += 1;
            let mut ending = Vec::new();
            let mut containers = Vec::new();
            for (id, entry) in &mut state.previews {
                if team_id.is_none_or(|team| entry.team_id == team) {
                    entry.ended = true;
                    ending.push(*id);
                    containers.extend(entry.container.clone());
                }
            }
            (ending, containers)
        };
        // Built first, so every failure below reopens what this close closed.
        let closed = PreviewsClosed {
            book: self.clone(),
            key,
        };
        for container in &containers {
            remover
                .remove(container)
                .await
                .map_err(|RemoveFailed| PreviewDrainError::HelperNotRemoved)?;
        }
        let deadline = Instant::now() + wait;
        loop {
            let mut notified = pin!(self.inner.left.notified());
            notified.as_mut().enable();
            if !ending
                .iter()
                .any(|id| self.state().previews.contains_key(id))
            {
                return Ok(closed);
            }
            timeout_at(deadline, notified)
                .await
                .map_err(|_| PreviewDrainError::Timeout)?;
        }
    }
}

/// One preview or download under way; it leaves the book when dropped.
#[derive(Debug)]
pub struct Preview {
    book: PreviewBook,
    id: u64,
}

impl Preview {
    /// Records the helper this preview started.
    ///
    /// # Errors
    /// [`PreviewEnded`] when the preview was ended first; the caller removes the helper it started.
    pub fn started(&self, container: &str) -> Result<(), PreviewEnded> {
        let mut state = self.book.state();
        let entry = state.previews.get_mut(&self.id).ok_or(PreviewEnded)?;
        if entry.ended {
            return Err(PreviewEnded);
        }
        entry.container = Some(container.to_owned());
        Ok(())
    }

    /// Records that this preview's helper is gone.
    pub fn stopped(&self, container: &str) {
        let mut state = self.book.state();
        if let Some(entry) = state.previews.get_mut(&self.id)
            && entry.container.as_deref() == Some(container)
        {
            entry.container = None;
        }
    }

    /// Fails once the preview was ended; checked before each step that starts a helper or sends bytes.
    ///
    /// # Errors
    /// [`PreviewEnded`] when it was ended.
    pub fn check(&self) -> Result<(), PreviewEnded> {
        let state = self.book.state();
        match state.previews.get(&self.id) {
            Some(entry) if !entry.ended => Ok(()),
            _ => Err(PreviewEnded),
        }
    }
}

impl Drop for Preview {
    fn drop(&mut self) {
        self.book.state().previews.remove(&self.id);
        self.book.inner.left.notify_waiters();
    }
}

/// A Team's or the Space's previews, held closed until dropped.
#[derive(Debug)]
pub struct PreviewsClosed {
    book: PreviewBook,
    key: Option<String>,
}

impl Drop for PreviewsClosed {
    fn drop(&mut self) {
        let mut state = self.book.state();
        if let Some(count) = state.closed.get_mut(&self.key) {
            *count -= 1;
            if *count == 0 {
                state.closed.remove(&self.key);
            }
        }
    }
}

#[cfg(test)]
mod tests;
