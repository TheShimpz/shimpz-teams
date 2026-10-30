//! A Team's Routine starts in any rolling 24 hours, without I/O (ADR-0092 section 9, ADR-0101).
//!
//! Each start is kept as its Routine, its instant, and the business steps its revision may run, for exactly one
//! window, so the Team's daily budget survives a Routine's pause, change, deletion, or recreation, while each Routine's
//! own cap counts only its own starts. A start reserves every step of its revision: at most
//! [`crate::plan::MAX_DAILY_STEPS`] business steps start in any window, whatever Routine, revision, or person's Rodar
//! started them, so the window never holds more than that many starts. At a cap, nothing starts until enough starts
//! leave the window; no Brain is asked.

use crate::plan::MAX_DAILY_STEPS;

/// One rolling window.
pub const WINDOW_SECONDS: i64 = 86_400;
/// Every start reserves at least one step, so the window holds at most this many.
pub const MAX_STARTS: i64 = MAX_DAILY_STEPS;

/// One start: its Routine, its instant, and the steps it reserved.
pub type Start = (String, i64, i64);

/// The starts still inside the 24 hours that end at `now`, oldest first.
#[must_use]
pub fn window(starts: &[Start], now: i64) -> Vec<Start> {
    // Chronological by instant alone: the oldest start leaves the window first, whichever Routine made it.
    let mut current: Vec<Start> = starts
        .iter()
        .filter(|item| item.1 > now - WINDOW_SECONDS)
        .cloned()
        .collect();
    current.sort_by_key(|item| item.1);
    current
}

/// When one more start fits under `limit`: now, or as soon as enough of these leave the window.
fn free_at_limit(instants: &[i64], limit: i64, now: i64) -> i64 {
    let count = i64::try_from(instants.len()).unwrap_or(i64::MAX);
    if count < limit {
        return now;
    }
    usize::try_from(count - limit)
        .ok()
        .and_then(|index| instants.get(index))
        .map_or(now, |instant| instant + WINDOW_SECONDS)
}

/// When a start of `steps` business steps fits under the Team's daily step budget beside the window's starts.
fn steps_free_at(current: &[Start], steps: i64, now: i64) -> i64 {
    let mut used: i64 = current.iter().map(|item| item.2).sum();
    let mut now = now;
    for (_routine, at, reserved) in current {
        if used + steps <= MAX_DAILY_STEPS {
            break;
        }
        used -= reserved;
        now = at + WINDOW_SECONDS;
    }
    now
}

/// The earliest instant the Routine may start again under its own cap, when it has one, and the Team's budget.
#[must_use]
pub fn free_at(starts: &[Start], routine_id: &str, cap: Option<i64>, now: i64, steps: i64) -> i64 {
    let current = window(starts, now);
    let own = cap.map_or(now, |cap| {
        let instants: Vec<i64> = current
            .iter()
            .filter(|item| item.0 == routine_id)
            .map(|item| item.1)
            .collect();
        free_at_limit(&instants, cap, now)
    });
    own.max(steps_free_at(&current, steps, now))
}

/// The window after one more start of the Routine at `now`, reserving its revision's `steps`.
#[must_use]
pub fn started(starts: &[Start], routine_id: &str, now: i64, steps: i64) -> Vec<Start> {
    let mut next = window(starts, now);
    next.push((routine_id.to_owned(), now, steps));
    next
}
