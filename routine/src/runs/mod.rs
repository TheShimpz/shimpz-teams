//! A claimed run's transitions, without I/O (ADR-0086, ADR-0092, ADR-0101): spending, freezing, thawing, and ending.

use strict_json::Value;
use team_core::entropy::Entropy;
use team_protocol::identifiers::{canonical_action_id, canonical_assistant_id};
use team_protocol::payload::MAX_TURN_DURATION_MS;
use team_protocol::python::value::small_int;
use team_protocol::routine::{MAX_ROLLUP_RUNS, SHOWN_MODES, canonical_position};
use team_protocol::routine_notice::REQUEST_KINDS;

use crate::claim;
use crate::definition;
use crate::json::{as_str, at, elements, int, object, set, sha256_hex, text};
use crate::plan;
use crate::record::{
    self, FROZEN_OUTCOMES, Lease, MAX_FROZEN_RUNS, Notice, Routine, RoutineStateError, Run,
    RunStatus, TeamRoutines, Transition, changed, put_notice_into, replace_routine, replace_run,
    run_notice, without_run,
};

/// A run's summed active duration, the chat usage bound (ADR-0082).
pub const MAX_USAGE_MS: i64 = MAX_TURN_DURATION_MS;

fn duration(usage: &Value) -> i64 {
    small_int(at(usage, "duration_ms")).unwrap_or(0)
}

/// Charge one segment's active time to the run: whole seconds against its budget, milliseconds to its usage.
///
/// # Errors
///
/// `invalid-duration` for a negative duration, or a lease refusal.
pub fn spend(
    state: &TeamRoutines,
    run_id: &str,
    lease: &Lease,
    now: i64,
    elapsed: (i64, i64),
) -> Transition {
    let (seconds, milliseconds) = elapsed;
    if seconds < 0 || milliseconds < 0 {
        return Err(RoutineStateError::new("invalid-duration"));
    }
    let mut value = claim::live_lease(state, run_id, lease, now)?.clone();
    let spent = duration(&value.usage)
        .saturating_add(milliseconds)
        .min(MAX_USAGE_MS);
    set(&mut value.usage, "duration_ms", int(spent));
    value.active_seconds_left -= seconds;
    changed(state, |next| {
        replace_run(next, value);
        Ok(())
    })
}

/// Two usages summed: their active durations, within the bound.
#[must_use]
pub fn joined_usage(first: &Value, second: &Value) -> Value {
    object(vec![(
        "duration_ms",
        int(duration(first)
            .saturating_add(duration(second))
            .min(MAX_USAGE_MS)),
    )])
}

/// The run lost the protection of its secret values; every later version of its notice says so, for good.
///
/// # Errors
///
/// `run-not-found`.
pub fn lose_protection(state: &TeamRoutines, run_id: &str) -> Transition {
    let mut value = record::run(state, run_id)?.clone();
    value.protection_lost = true;
    changed(state, |next| {
        replace_run(next, value);
        Ok(())
    })
}

/// What a frozen run waits for: its request kind and the Assistant Action that asked at its call's position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    /// The request kind.
    pub kind: String,
    /// The Assistant that asked.
    pub assistant_id: String,
    /// The Action that asked.
    pub action: String,
    /// The call's 1-based step position.
    pub position: i64,
}

/// Park a run for a person; it keeps no lease, and the same Routine never fires while it is frozen.
///
/// `request` is its kind and the Assistant Action that asked at its call's position: a replay step, which must be
/// that step of the plan. A Routine being deleted never freezes a run: its deletion ends only the frozen runs it saw.
///
/// # Errors
///
/// A lease refusal, `routine-deleting`, `freeze-invalid`, `frozen-limit`, or a refusal of its notice.
pub fn freeze(
    state: &TeamRoutines,
    run_id: &str,
    lease: &Lease,
    now: i64,
    request: &Request,
    entropy: &dyn Entropy,
) -> Transition {
    let value = claim::live_lease(state, run_id, lease, now)?.clone();
    let current = record::routine(state, &value.routine_id)?;
    if current.deleting {
        return Err(RoutineStateError::new("routine-deleting"));
    }
    let steps = elements(at(&current.plan, "steps"));
    let total = int(i64::try_from(steps.len()).unwrap_or(i64::MAX));
    let placed = canonical_position(&int(request.position), &total)
        .and_then(small_int)
        .and_then(|position| usize::try_from(position - 1).ok())
        .and_then(|index| steps.get(index));
    let matches_step = placed.is_some_and(|step| {
        as_str(at(step, "assistant")) == Some(request.assistant_id.as_str())
            && as_str(at(step, "action")) == Some(request.action.as_str())
    });
    if !REQUEST_KINDS.contains(&request.kind.as_str())
        || canonical_assistant_id(&text(&request.assistant_id)).is_none()
        || canonical_action_id(&text(&request.action)).is_none()
        || !matches_step
    {
        return Err(RoutineStateError::new("freeze-invalid"));
    }
    if state
        .runs
        .iter()
        .filter(|item| item.status == RunStatus::Frozen)
        .count()
        >= MAX_FROZEN_RUNS
    {
        return Err(RoutineStateError::new("frozen-limit"));
    }
    changed(state, |next| {
        let detail = object(vec![("request_kind", text(&request.kind))]);
        let mut frozen = run_notice(next, &value, "frozen", now, detail, entropy)?;
        frozen.status = RunStatus::Frozen;
        frozen.lease_sha256 = String::new();
        frozen.lease_key = String::new();
        frozen.lease_expires_at = 0;
        frozen.request_kind.clone_from(&request.kind);
        frozen.assistant_id.clone_from(&request.assistant_id);
        frozen.action.clone_from(&request.action);
        replace_run(next, frozen);
        Ok(())
    })
}

/// A human resumes a frozen run; it runs under a fresh internal lease that no machine assertion knows.
///
/// A Routine being deleted never resumes a run, so its deletion ends each frozen run it saw without racing a replay.
/// Returns the state and the lease token.
///
/// # Errors
///
/// `run-not-found`, `run-not-frozen`, or [`record::ENTROPY_UNAVAILABLE`].
pub fn thaw(
    state: &TeamRoutines,
    run_id: &str,
    now: i64,
    requests_used: i64,
    entropy: &dyn Entropy,
) -> Transition<(TeamRoutines, String)> {
    let value = record::run(state, run_id)?;
    if value.status != RunStatus::Frozen || record::routine(state, &value.routine_id)?.deleting {
        return Err(RoutineStateError::new("run-not-frozen"));
    }
    let (mut resumed, token) = claim::human_lease(value, now, entropy)?;
    resumed.requests_used = value.requests_used.max(requests_used);
    let next = changed(state, |next| {
        replace_run(next, resumed);
        Ok(())
    })?;
    Ok((next, token))
}

/// A worker ends its leased run, which completed every step, with a durable notice; only its live lease may.
///
/// `shown` is the shown step's slot of the run's cursor: `{"step", "output", "digest"}`.
///
/// # Errors
///
/// A lease refusal, or a refusal of its notice.
pub fn finish(
    state: &TeamRoutines,
    run_id: &str,
    lease: &Lease,
    now: i64,
    shown: Option<&Value>,
    entropy: &dyn Entropy,
) -> Transition {
    let value = claim::live_lease(state, run_id, lease, now)?.clone();
    completed_run(state, &value, now, shown, entropy)
}

/// End a run that completed every step, by its Routine's output disposition with `shown`.
fn completed_run(
    state: &TeamRoutines,
    value: &Run,
    now: i64,
    shown: Option<&Value>,
    entropy: &dyn Entropy,
) -> Transition {
    changed(state, |next| {
        completion(next, value, completed(value), now, shown, entropy)?;
        without_run(next, &value.run_id, now)
    })
}

/// How a completed run tells the person, by its Routine's output disposition (ADR-0092 amendment, ADR-0101).
///
/// `show` publishes the result every run; `changes` only when its keyed digest differs from the last shown one,
/// recorded in the same write; `none` publishes nothing, a continuous Routine's healthy run rolling up into its
/// minute's notice. A result not kept is unavailable, and so is every result after the run lost its protection,
/// which never moves the `changes` baseline; a run with a notice of its own always gets its terminal version.
fn completion(
    state: &mut TeamRoutines,
    value: &Run,
    outcome: &str,
    now: i64,
    shown: Option<&Value>,
    entropy: &dyn Entropy,
) -> Transition<()> {
    let current = record::routine(state, &value.routine_id)?.clone();
    let disposition = at(&current.plan, "output");
    let mode = as_str(at(disposition, "mode")).unwrap_or_default();
    let summary = definition::summary(&current.plan, current.revision);
    if SHOWN_MODES.contains(&mode) {
        // On the wire the shown step is its position (ADR-0092 amendment, 2026-10-05, scale).
        let position = at(&definition::disposition(&current.plan), "step").clone();
        let valid = shown
            .filter(|shown| at(shown, "step") == at(disposition, "step") && !value.protection_lost);
        let mut output = valid.map_or_else(
            || plan::output_state(&position, "unavailable"),
            |shown| at(shown, "output").clone(),
        );
        let digest = valid.and_then(|shown| as_str(at(shown, "digest")));
        if mode == "changes" && as_str(at(&output, "state")) == Some("shown") {
            if digest.is_some_and(|digest| digest == current.output_digest) {
                if value.notice_version == 0 {
                    quiet(state, &current);
                    return Ok(());
                }
                output = plan::output_state(&position, "unchanged");
            } else {
                let mut baseline = current.clone();
                digest
                    .unwrap_or_default()
                    .clone_into(&mut baseline.output_digest);
                replace_routine(state, baseline);
            }
        }
        let detail = object(vec![("plan", summary), ("output", output)]);
        run_notice(state, value, outcome, now, detail, entropy)?;
        return Ok(());
    }
    if value.notice_version == 0 && !value.protection_lost {
        if outcome == "done" && healthy(state, value, now)? {
            return Ok(());
        }
        quiet(state, &current);
        return Ok(());
    }
    let detail = object(vec![("plan", summary), ("output", Value::Null)]);
    run_notice(state, value, outcome, now, detail, entropy)?;
    Ok(())
}

/// A completed run that tells the person nothing still resets its Routine's failure streak.
fn quiet(state: &mut TeamRoutines, current: &Routine) {
    let mut reset = current.clone();
    reset.failures = 0;
    replace_routine(state, reset);
}

/// Roll a continuous Routine's healthy run into the one versioned notice of the minute it ended in.
///
/// Its count doubles as the notice version Admin acknowledges, so a notice acknowledged mid-minute is replaced by the
/// next version, which carries the minute's summed usage (ADR-0092 section 9, ADR-0101). `false` when the run rolls
/// up into nothing.
fn healthy(state: &mut TeamRoutines, value: &Run, now: i64) -> Transition<bool> {
    let current = record::routine(state, &value.routine_id)?.clone();
    if !record::continuous(&current) {
        return Ok(false);
    }
    let minute = now - now.rem_euclid(60);
    let same = current.rollup_minute == minute;
    let runs = if same { current.rollup_runs + 1 } else { 1 };
    // The rollup minute only moves forward: a clock stepped back into an earlier minute, which may already be
    // delivered, or past the count gaps allow, gives the run no rollup and leaves the counters as they are.
    if minute < current.rollup_minute || runs > i64::try_from(MAX_ROLLUP_RUNS).unwrap_or(i64::MAX) {
        return Ok(false);
    }
    let usage = match (&current.rollup_usage, same) {
        (Some(rollup), true) => joined_usage(rollup, &value.usage),
        _ => value.usage.clone(),
    };
    let mut rolled = current.clone();
    rolled.rollup_minute = minute;
    rolled.rollup_runs = runs;
    rolled.rollup_usage = Some(usage.clone());
    rolled.failures = 0;
    replace_routine(state, rolled);
    let notice_id =
        sha256_hex(format!("healthy:{}:{minute}", current.routine_id).as_bytes())[..32].to_owned();
    let mut notice = Notice::new(
        &notice_id,
        &current.routine_id,
        "",
        "healthy",
        minute,
        object(vec![("runs", int(runs))]),
    );
    notice.version = runs;
    notice.usage = Some(usage);
    put_notice_into(state, notice)?;
    Ok(true)
}

/// Team itself ends a run without its lease: a human Stop or answer, an expired lease or deadline, or recovery.
///
/// A leased run ends stopped or failed; a frozen run denied, stopped, or failed; one that may have acted is held
/// instead (`fence`). With `status`, the run must still be in it, so a stale decision never lands on a changed run.
///
/// # Errors
///
/// `run-not-found`, `run-changed`, `invalid-outcome`, or a refusal of its notice.
pub fn end(
    state: &TeamRoutines,
    run_id: &str,
    now: i64,
    outcome: &str,
    detail: Value,
    status: Option<RunStatus>,
    entropy: &dyn Entropy,
) -> Transition {
    let value = record::run(state, run_id)?.clone();
    if status.is_some_and(|status| value.status != status) {
        return Err(RoutineStateError::new("run-changed"));
    }
    let allowed: &[&str] = match value.status {
        RunStatus::Leased => &["stopped", "failed"],
        RunStatus::Frozen => &FROZEN_OUTCOMES,
        _ => &[],
    };
    if !allowed.contains(&outcome) {
        return Err(RoutineStateError::new("invalid-outcome"));
    }
    changed(state, |next| {
        run_notice(next, &value, outcome, now, detail, entropy)?;
        without_run(next, run_id, now)
    })
}

/// Team's watchdog ends a leased run whose sealed cursor completed every step, as the worker would have.
///
/// # Errors
///
/// A refusal of the lease the watchdog read, or of its notice.
pub fn complete_recovered(
    state: &TeamRoutines,
    run_id: &str,
    lease_sha256: &str,
    now: i64,
    shown: Option<&Value>,
    entropy: &dyn Entropy,
) -> Transition {
    let value = claim::read_lease(state, run_id, lease_sha256)?.clone();
    completed_run(state, &value, now, shown, entropy)
}

/// How a run that completed every step ends: recovered when a continuation after a hold completed it.
#[must_use]
pub fn completed(value: &Run) -> &'static str {
    let first = claim::network_of(&value.generation, &value.run_id)
        .map(|network| claim::generation_for(&network, &value.run_id, ""));
    if first.as_deref() == Some(value.generation.as_str()) {
        "done"
    } else {
        "recovered"
    }
}

#[cfg(test)]
mod tests;
