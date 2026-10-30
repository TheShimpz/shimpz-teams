//! A Team's undelivered notices: each run's one versioned notice, the Routine's own outcomes, and their delivery.

use strict_json::Value;
use team_core::entropy::Entropy;
use team_protocol::routine_notice::canonical_notice_detail;

use super::{
    MAX_FAILURE_STREAK, MAX_ROUTINE_NOTICES, MAX_UNDELIVERED_NOTICES, Notice, RoutineStateError,
    Run, TeamRoutines, Transition, changed, new_id, replace_routine, routine,
};
use crate::json::{int, object, text};

/// Why one more Routine change, which adds its one notice, cannot be admitted now; `None` when it fits.
#[must_use]
pub fn change_room(state: &TeamRoutines) -> Option<&'static str> {
    (state.notices.len() + 1 > MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES)
        .then_some("notices-full")
}

/// Publish the next version of the run's one notice, keyed by its run id, with the run's usage and protection.
///
/// Returns the run carrying that version. A completed or recovered run resets its Routine's failure streak; a failed
/// one extends it, the third pausing it with a `streak-paused` notice that says so.
///
/// # Errors
///
/// `notice-invalid`, `notices-full`, `routine-not-found`, or [`ENTROPY_UNAVAILABLE`](super::ENTROPY_UNAVAILABLE).
pub fn run_notice(
    state: &mut TeamRoutines,
    value: &Run,
    outcome: &str,
    now: i64,
    detail: Value,
    entropy: &dyn Entropy,
) -> Transition<Run> {
    let version = value.notice_version + 1;
    let mut notice = Notice::new(
        &value.run_id,
        &value.routine_id,
        &value.run_id,
        outcome,
        now,
        detail,
    );
    notice.version = version;
    notice.usage = Some(value.usage.clone());
    notice.protection_lost = value.protection_lost;
    put_notice_into(state, notice)?;
    if ["done", "recovered", "failed"].contains(&outcome) {
        let mut current = routine(state, &value.routine_id)?.clone();
        let failures = if outcome == "failed" {
            current.failures + 1
        } else {
            0
        };
        let streak = !current.paused && failures >= MAX_FAILURE_STREAK;
        current.failures = failures;
        current.paused = current.paused || streak;
        let routine_id = current.routine_id.clone();
        replace_routine(state, current);
        if streak {
            let paused = Notice::new(
                &new_id(entropy)?,
                &routine_id,
                "",
                "streak-paused",
                now,
                object(vec![("failures", int(failures))]),
            );
            put_notice_into(state, paused)?;
        }
    }
    let mut versioned = value.clone();
    versioned.notice_version = version;
    Ok(versioned)
}

/// The undelivered notices.
#[must_use]
pub fn undelivered(state: &TeamRoutines) -> usize {
    state.notices.len()
}

/// Add or update an undelivered notice, which is never evicted.
///
/// # Errors
///
/// `notice-invalid`, `notices-full`, or `routine-not-found` for a notice that names no Routine.
pub fn put_notice(state: &TeamRoutines, notice: Notice) -> Transition {
    changed(state, |next| put_notice_into(next, notice))
}

pub(crate) fn put_notice_into(state: &mut TeamRoutines, notice: Notice) -> Transition<()> {
    let mut notice = notice;
    let detail = canonical_notice_detail(&text(&notice.outcome), &notice.detail)
        .cloned()
        .ok_or(RoutineStateError::new("notice-invalid"))?;
    notice.detail = detail;
    // Each version names the Routine as it is now, so a later rename never retitles an earlier version.
    if notice.name.is_empty() {
        notice
            .name
            .clone_from(&routine(state, &notice.routine_id)?.name);
    }
    if notice.version < 1 {
        return Err(RoutineStateError::new("notice-invalid"));
    }
    state
        .notices
        .retain(|item| item.notice_id != notice.notice_id);
    if state.notices.len() >= MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES {
        return Err(RoutineStateError::new("notices-full"));
    }
    state.notices.push(notice);
    Ok(())
}

/// Admin delivered these exact notice versions; a notice updated since stays. Delivery never changes a run.
#[must_use]
pub fn acknowledge(state: &TeamRoutines, delivered: &[(String, i64)]) -> TeamRoutines {
    let mut next = state.clone();
    next.notices.retain(|item| {
        !delivered
            .iter()
            .any(|(id, version)| *id == item.notice_id && *version == item.version)
    });
    next
}
