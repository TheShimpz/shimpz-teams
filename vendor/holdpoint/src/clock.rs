//! Wall-clock time, injectable so tests control it.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The worker's source of wall-clock time and of short sleeps.
pub trait Clock: Send + Sync {
    /// Milliseconds since the Unix epoch.
    fn now_ms(&self) -> i64;
    /// Waits about `duration`.
    fn sleep(&self, duration: Duration);
}

/// The system clock.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        let since = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
        i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
    }

    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

/// A clock that only moves when told to; `sleep` advances it instantly.
#[derive(Debug, Clone, Default)]
pub struct ManualClock {
    now: Arc<AtomicI64>,
}

impl ManualClock {
    /// A clock that reads `now_ms`.
    #[must_use]
    pub fn at(now_ms: i64) -> Self {
        Self { now: Arc::new(AtomicI64::new(now_ms)) }
    }

    /// Moves the clock forward by `ms`.
    pub fn advance(&self, ms: i64) {
        self.now.fetch_add(ms, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> i64 {
        self.now.load(Ordering::SeqCst)
    }

    fn sleep(&self, duration: Duration) {
        self.advance(i64::try_from(duration.as_millis()).unwrap_or(i64::MAX));
        std::thread::yield_now();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_system_clock_moves_forward() {
        let clock = SystemClock;
        let before = clock.now_ms();
        clock.sleep(Duration::from_millis(2));
        assert!(clock.now_ms() >= before + 2);
    }

    #[test]
    fn the_manual_clock_moves_only_when_told() {
        let clock = ManualClock::at(100);
        assert_eq!(clock.now_ms(), 100);
        clock.advance(5);
        clock.sleep(Duration::from_millis(10));
        assert_eq!(clock.now_ms(), 115);
    }
}
