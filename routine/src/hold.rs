//! Holding a Routine run and settling its incident, without I/O (ADR-0092 sections 5 and 7, ADR-0101).
//!
//! A held run becomes its own incident in the write that indexes it: the same record, `unresolved`, keeping the run's
//! notice, held call, remaining active time, usage, and lost protection, while its generation's removal is queued like
//! an ended run's. It is resumed in place as the run's continuation, under a fresh internal lease, after Team-admitted
//! evidence, or set aside (`skipped`) by a person: Rodar, which requests one fresh run, or the Routine's deletion. A
//! set-aside record goes once what it kept is released, and a Routine's deletion completes only after that, so no run
//! record outlives its Routine. A person's card checks the exact state it was opened on in the same write.

use strict_json::Value;
use team_core::entropy::Entropy;

use crate::claim;
use crate::json::{object, text};
use crate::record::{
    self, Lease, RoutineStateError, Run, RunStatus, TeamRoutines, Transition, changed, ended,
    replace_routine, replace_run, run_notice, set_paused_into,
};

/// The call a held run stopped at: its Assistant, Action, position, and its plan's step count; all empty when the run
/// sealed no cursor, or when it is a run of a Routine being deleted whose evidence could not be read.
pub type HeldStep = (String, String, Option<i64>, i64);

/// The held step of a run that sealed no cursor.
#[must_use]
pub const fn unknown_step() -> HeldStep {
    (String::new(), String::new(), None, 0)
}

/// Why recovery paused a held run's Routine; each is published on the run's notice.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PauseReason {
    /// The run's recovery budget is spent.
    Exhausted,
    /// A policy fault holds it.
    Policy,
    /// Its evidence could not settle it.
    Evidence,
}

impl PauseReason {
    /// Its name on the notice.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Exhausted => "exhausted",
            Self::Policy => "policy",
            Self::Evidence => "evidence",
        }
    }
}

/// A held run's incident is durable and its batch archived: index the run as its incident in one write.
///
/// The run's live state is queued for removal like an ended run's; its archived journal marker stays with the
/// incident. `step` is the Assistant Action its sealed cursor stopped at, with its position, which its incident names.
///
/// # Errors
///
/// `run-not-found`, `run-not-held`, or a refusal of its notice.
pub fn settle_hold(
    state: &TeamRoutines,
    run_id: &str,
    now: i64,
    step: HeldStep,
    entropy: &dyn Entropy,
) -> Transition {
    let value = record::run(state, run_id)?.clone();
    if value.status != RunStatus::Held {
        return Err(RoutineStateError::new("run-not-held"));
    }
    let deleting = record::routine(state, &value.routine_id)?.deleting;
    let mut indexed_state = changed(state, |next| {
        let mut indexed = run_notice(next, &value, "held", now, object(vec![]), entropy)?;
        let (assistant_id, action, position, steps) = step;
        indexed.status = RunStatus::Unresolved;
        indexed.assistant_id = assistant_id;
        indexed.action = action;
        indexed.position = position;
        indexed.steps = steps;
        replace_run(next, indexed.clone());
        ended(next, &indexed, now);
        Ok(())
    })?;
    // A Routine being deleted keeps no incident for a person to settle: its run is set aside as it is indexed.
    if deleting {
        indexed_state = skip_incident(&indexed_state, run_id, now, None, entropy)?;
    }
    Ok(indexed_state)
}

/// The held run's incident, unresolved or set aside.
///
/// # Errors
///
/// `incident-not-found`.
pub fn incident<'s>(state: &'s TeamRoutines, incident_id: &str) -> Transition<&'s Run> {
    record::incidents(state)
        .into_iter()
        .find(|item| item.run_id == incident_id)
        .ok_or(RoutineStateError::new("incident-not-found"))
}

/// Charge time a held run's recovery spent outside any segment, such as verification, to its active time left.
///
/// # Errors
///
/// `incident-not-found`.
pub fn spend_incident(state: &TeamRoutines, incident_id: &str, seconds: i64) -> Transition {
    let mut value = incident(state, incident_id)?.clone();
    value.active_seconds_left = (value.active_seconds_left - seconds).max(0);
    changed(state, |next| {
        replace_run(next, value);
        Ok(())
    })
}

/// Resume a held run in place after Team-admitted evidence, as its continuation in `generation` (ADR-0092).
///
/// Only the revision the run executed, of a Routine still listed, not paused, and with no live run, may continue. Its
/// archived generation is queued for removal in the same write; the continuation runs under a fresh internal lease that
/// no machine assertion knows, and goes on with the run's notice. Returns the state and the lease token.
///
/// # Errors
///
/// `incident-not-found`, `incident-not-unresolved`, `routine-not-resumable`, `run-time-exhausted`, or `routine-busy`.
pub fn reopen_incident(
    state: &TeamRoutines,
    incident_id: &str,
    now: i64,
    generation: &str,
    entropy: &dyn Entropy,
) -> Transition<(TeamRoutines, String)> {
    let value = incident(state, incident_id)?;
    if value.status != RunStatus::Unresolved {
        return Err(RoutineStateError::new("incident-not-unresolved"));
    }
    let current = record::routine(state, &value.routine_id)?;
    if current.deleting || current.paused || current.revision != value.revision {
        return Err(RoutineStateError::new("routine-not-resumable"));
    }
    if value.active_seconds_left <= 0 {
        return Err(RoutineStateError::new("run-time-exhausted"));
    }
    if record::live_runs(state)
        .iter()
        .any(|item| item.routine_id == value.routine_id)
        || claim::network_of(generation, incident_id).is_none()
    {
        return Err(RoutineStateError::new("routine-busy"));
    }
    // The continuation runs at once under its own generation.
    let (mut resumed, token) = claim::human_lease(value, now, entropy)?;
    generation.clone_into(&mut resumed.generation);
    let archived = (incident_id.to_owned(), value.generation.clone());
    let next = changed(state, |next| {
        replace_run(next, resumed);
        // The hold queued its generation already unless that removal has run since.
        if !next.discards.contains(&archived) {
            next.discards.push(archived);
        }
        Ok(())
    })?;
    Ok((next, token))
}

/// The detail an incident view names its call by.
#[must_use]
pub fn step_detail(step: &HeldStep) -> Value {
    let (assistant_id, action, position, steps) = step;
    if assistant_id.is_empty() {
        return object(vec![
            ("assistant_id", Value::Null),
            ("action", Value::Null),
            ("position", Value::Null),
            ("steps", Value::Null),
        ]);
    }
    object(vec![
        ("assistant_id", text(assistant_id)),
        ("action", text(action)),
        ("position", position.map_or(Value::Null, crate::json::int)),
        ("steps", crate::json::int(*steps)),
    ])
}

/// The call an incident names.
#[must_use]
pub fn held_step(value: &Run) -> HeldStep {
    (
        value.assistant_id.clone(),
        value.action.clone(),
        value.position,
        value.steps,
    )
}

/// What a person's recovery card was opened on: the run's revision and generation, and the Routine's revision.
///
/// `current` is 0 when the Routine is deleted or being deleted.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Expected {
    /// The run's revision.
    pub revision: i64,
    /// The run's generation.
    pub generation: String,
    /// The Routine's current revision, or 0.
    pub current: i64,
}

/// The Routine's current revision, 0 once it is deleted or being deleted.
#[must_use]
pub fn current_revision(state: &TeamRoutines, routine_id: &str) -> i64 {
    state
        .routines
        .iter()
        .find(|item| item.routine_id == routine_id && !item.deleting)
        .map_or(0, |item| item.revision)
}

/// Refuse a card's transition when the incident or its Routine changed since the card opened.
fn expect(state: &TeamRoutines, value: &Run, expected: Option<&Expected>) -> Transition<()> {
    let Some(expected) = expected else {
        return Ok(());
    };
    let current = current_revision(state, &value.routine_id);
    if (value.revision, value.generation.as_str(), current)
        != (
            expected.revision,
            expected.generation.as_str(),
            expected.current,
        )
    {
        return Err(RoutineStateError::new("incident-changed"));
    }
    Ok(())
}

/// A person sets the held run aside, never verified, replayed, or fabricated; its possible effects stay unresolved.
///
/// Rodar or deleting the Routine does it; the run's `user-skipped` notice is distinct from the Routine's own
/// missed-schedule skip. A card's `expected` state is checked in the same write.
///
/// # Errors
///
/// `incident-not-found`, `incident-not-unresolved`, `incident-changed`, or a refusal of its notice.
pub fn skip_incident(
    state: &TeamRoutines,
    incident_id: &str,
    now: i64,
    expected: Option<&Expected>,
    entropy: &dyn Entropy,
) -> Transition {
    let value = incident(state, incident_id)?.clone();
    if value.status != RunStatus::Unresolved {
        return Err(RoutineStateError::new("incident-not-unresolved"));
    }
    expect(state, &value, expected)?;
    changed(state, |next| {
        let mut skipped = run_notice(next, &value, "user-skipped", now, object(vec![]), entropy)?;
        skipped.status = RunStatus::Skipped;
        replace_run(next, skipped);
        Ok(())
    })
}

/// Rodar: set the held run aside and request one fresh run of the Routine's current revision, in one write.
///
/// The Routine leaves its pause with a fresh failure streak. Whatever its schedule, the request stands until a claim
/// starts it now, under every start cap and without moving its cadence.
///
/// # Errors
///
/// A refusal of [`skip_incident`], or `routine-not-found`.
pub fn run_incident(
    state: &TeamRoutines,
    incident_id: &str,
    now: i64,
    expected: &Expected,
    entropy: &dyn Entropy,
) -> Transition {
    let routine_id = incident(state, incident_id)?.routine_id.clone();
    let skipped = skip_incident(state, incident_id, now, Some(expected), entropy)?;
    changed(&skipped, |next| {
        set_paused_into(next, &routine_id, false)?;
        let mut current = record::routine(next, &routine_id)?.clone();
        current.run_requested = now;
        replace_routine(next, current);
        Ok(())
    })
}

/// Recovery paused the Routine an unresolved incident holds, and the run's notice says why.
///
/// # Errors
///
/// `incident-not-found`, `incident-not-unresolved`, or a refusal of its pause or notice.
pub fn pause_incident(
    state: &TeamRoutines,
    incident_id: &str,
    now: i64,
    reason: PauseReason,
    entropy: &dyn Entropy,
) -> Transition {
    let value = incident(state, incident_id)?.clone();
    if value.status != RunStatus::Unresolved {
        return Err(RoutineStateError::new("incident-not-unresolved"));
    }
    changed(state, |next| {
        set_paused_into(next, &value.routine_id, true)?;
        let noticed = run_notice(
            next,
            &value,
            "paused",
            now,
            object(vec![("reason", text(reason.as_str()))]),
            entropy,
        )?;
        replace_run(next, noticed);
        Ok(())
    })
}

/// A skipped incident's archive marker, cursor, and evidence are gone: its record goes; one gone is released.
///
/// # Errors
///
/// `incident-not-skipped`.
pub fn release_incident(state: &TeamRoutines, incident_id: &str) -> Transition {
    let Some(value) = state.runs.iter().find(|item| item.run_id == incident_id) else {
        return Ok(state.clone());
    };
    if value.status != RunStatus::Skipped {
        return Err(RoutineStateError::new("incident-not-skipped"));
    }
    changed(state, |next| {
        next.runs.retain(|item| item.run_id != incident_id);
        Ok(())
    })
}

/// Stop a run's lease so no worker may advance it; nothing ends it yet.
fn held(state: &TeamRoutines, value: &Run) -> Transition {
    if value.generation.is_empty() {
        return Err(RoutineStateError::new("generation-invalid"));
    }
    let mut fenced = value.clone();
    fenced.status = RunStatus::Held;
    fenced.lease_sha256 = String::new();
    fenced.lease_key = String::new();
    fenced.lease_expires_at = 0;
    changed(state, |next| {
        replace_run(next, fenced);
        Ok(())
    })
}

/// Stop the live lease of a run that must be held (ADR-0092).
///
/// # Errors
///
/// A lease refusal, or `generation-invalid` for a run whose segment never started.
pub fn fence(state: &TeamRoutines, run_id: &str, lease: &Lease, now: i64) -> Transition {
    let value = claim::live_lease(state, run_id, lease, now)?.clone();
    held(state, &value)
}

/// Team's watchdog holds a leased run nothing runs any more whose durable state shows it may have acted.
///
/// # Errors
///
/// A refusal of the lease the watchdog read, or `generation-invalid`.
pub fn hold_recovered(state: &TeamRoutines, run_id: &str, lease_sha256: &str) -> Transition {
    let value = claim::read_lease(state, run_id, lease_sha256)?.clone();
    held(state, &value)
}

/// Leased runs whose lease or active time ran out; the caller stops each and decides its outcome.
#[must_use]
pub fn expired(state: &TeamRoutines, now: i64) -> Vec<&Run> {
    state
        .runs
        .iter()
        .filter(|item| {
            item.status == RunStatus::Leased
                && (item.lease_expires_at <= now || item.active_seconds_left <= 0)
        })
        .collect()
}

/// Machine-leased runs claimed under a routine key that is no longer current.
#[must_use]
pub fn rekeyed<'s>(state: &'s TeamRoutines, key_fingerprint: &str) -> Vec<&'s Run> {
    state
        .runs
        .iter()
        .filter(|item| {
            item.status == RunStatus::Leased
                && item.lease_key != key_fingerprint
                && item.lease_key != record::HUMAN_LEASE
        })
        .collect()
}

#[cfg(test)]
mod tests;
