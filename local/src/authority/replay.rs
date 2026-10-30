//! The one-use guard every Supervisor and Routine assertion is consumed by.
//!
//! It remembers each consumed assertion id until its expiry. A restart forgets them, which the 15-second assertion
//! lifetime bounds. When it holds its capacity of live ids, a new assertion is answered unavailable rather than
//! admitted unguarded.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};

use super::Refusal;

/// The most live assertion ids the guard holds.
pub const MAX_REPLAY_ENTRIES: usize = 16 * 1024;

/// The bounded one-use guard.
#[derive(Debug)]
pub struct ReplayGuard {
    capacity: usize,
    seen: Mutex<HashMap<String, i64>>,
}

impl Default for ReplayGuard {
    fn default() -> Self {
        Self::with_capacity(MAX_REPLAY_ENTRIES)
    }
}

impl ReplayGuard {
    /// A guard of `capacity` live ids, clamped to 1 through [`MAX_REPLAY_ENTRIES`].
    #[must_use]
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            capacity: capacity.clamp(1, MAX_REPLAY_ENTRIES),
            seen: Mutex::new(HashMap::new()),
        }
    }

    /// Consumes `assertion_id`, live until `expires_at`, at `now` (both in Unix seconds).
    ///
    /// # Errors
    /// [`Refusal::Denied`] for an id consumed before and not yet expired; [`Refusal::Unavailable`] when the guard holds
    /// its capacity of live ids.
    pub fn consume(&self, assertion_id: &str, expires_at: i64, now: i64) -> Result<(), Refusal> {
        let mut seen = self.seen.lock().unwrap_or_else(PoisonError::into_inner);
        seen.retain(|_, expiry| *expiry >= now);
        if seen.contains_key(assertion_id) {
            return Err(Refusal::Denied);
        }
        if seen.len() >= self.capacity {
            return Err(Refusal::Unavailable);
        }
        seen.insert(assertion_id.to_owned(), expires_at);
        Ok(())
    }
}
