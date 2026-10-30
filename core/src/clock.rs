//! The wall clock every expiry, record time, and audit time is read from.
//!
//! Deadlines and elapsed times use `tokio::time::Instant`, which a test pauses and advances; only wall time, which is
//! persisted or compared with persisted values, goes through this seam.

use std::time::SystemTime;

/// The current wall time.
pub trait Clock: Send + Sync {
    /// The time now.
    fn now(&self) -> SystemTime;
}

/// The host's wall clock.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }
}
