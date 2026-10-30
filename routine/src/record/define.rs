//! A Routine definition's closed contract and the transitions that admit one: creation, update, and refinement.

use strict_json::Value;
use team_core::entropy::Entropy;
use team_protocol::identifiers::{
    canonical_action_id, canonical_assistant_id, canonical_identifier,
};
use team_protocol::payload::{SHA256_RE, SOURCE_DIGEST_RE};
use team_protocol::routine::{
    MAX_PERMITTED, MAX_ROUTINE_ASSISTANTS, MAX_STEP_STORED_INPUTS, PLAN_DIGEST_RE, ROUTINE_ID_RE,
    canonical_name, canonical_schedule, exact_description, valid_revision, zoned,
};

use super::{
    HEX32_RE, MAX_ROUTINES, Notice, Routine, RoutineStateError, TeamRoutines, Transition, changed,
    continuous, live_runs, new_id, next_after, put_notice_into, replace_routine, routine,
};
use crate::definition;
use crate::json::{as_str, at, elements, has_fields, int, is_integer, object, text};
use crate::plan;

/// The authenticated confirmation of the card that defined a revision, with the Team incarnation it was in.
fn confirmed(value: Option<&Value>) -> bool {
    let Some(value) = value else {
        return false;
    };
    has_fields(
        value,
        &[
            "proposal_id",
            "proposal_digest",
            "principal",
            "incarnation",
            "confirmed_at",
        ],
    ) && ROUTINE_ID_RE.is_full_value(at(value, "proposal_id"))
        && PLAN_DIGEST_RE.is_full_value(at(value, "proposal_digest"))
        && HEX32_RE.is_full_value(at(value, "principal"))
        && SHA256_RE.is_full_value(at(value, "incarnation"))
        && positive_integer(at(value, "confirmed_at"))
}

fn positive_integer(value: &Value) -> bool {
    is_integer(value)
        && !matches!(value, Value::Number(strict_json::Number::Int(integer)) if *integer <= 0)
        && !matches!(value, Value::Number(strict_json::Number::BigInt(digits)) if digits.starts_with('-'))
}

/// One permitted entry in its closed shape.
fn permitted_entry(item: &Value) -> bool {
    let stored = elements(at(item, "stored_inputs"));
    let names: Vec<&str> = stored.iter().filter_map(as_str).collect();
    has_fields(
        item,
        &["assistant", "action", "pin", "read_only", "stored_inputs"],
    ) && canonical_assistant_id(at(item, "assistant")).is_some()
        && canonical_action_id(at(item, "action")).is_some()
        && PLAN_DIGEST_RE.is_full_value(at(item, "pin"))
        && matches!(at(item, "read_only"), Value::Bool(_))
        && matches!(at(item, "stored_inputs"), Value::Array(_))
        && stored.len() <= MAX_STEP_STORED_INPUTS
        && stored
            .iter()
            .all(|name| canonical_identifier(name).is_some())
        && names.len() == stored.len()
        && names.windows(2).all(|pair| pair[0] < pair[1])
}

/// A step's Assistant and Action.
fn step_key(step: &Value) -> (&str, &str) {
    (
        as_str(at(step, "assistant")).unwrap_or_default(),
        as_str(at(step, "action")).unwrap_or_default(),
    )
}

/// Exactly the plan's steps' Actions, each once, sorted, at a complete pin: the step's own pin.
fn permitted_valid(value: &Routine) -> bool {
    let entries = &value.permitted;
    if i64::try_from(entries.len()).unwrap_or(i64::MAX) > MAX_PERMITTED
        || !entries.iter().all(permitted_entry)
    {
        return false;
    }
    let pins: Vec<((&str, &str), &str)> = entries
        .iter()
        .map(|item| {
            (
                (
                    as_str(at(item, "assistant")).unwrap_or_default(),
                    as_str(at(item, "action")).unwrap_or_default(),
                ),
                as_str(at(item, "pin")).unwrap_or_default(),
            )
        })
        .collect();
    let steps = elements(at(&value.plan, "steps"));
    pins.windows(2).all(|pair| pair[0].0 < pair[1].0)
        && pins
            .iter()
            .all(|(key, _)| steps.iter().any(|step| step_key(step) == *key))
        && steps.iter().all(|step| {
            pins.iter()
                .find(|(key, _)| *key == step_key(step))
                .is_some_and(|(_, pin)| Some(*pin) == as_str(at(step, "pin")))
        })
}

/// Exactly the Assistants of the plan's steps and permitted Actions, each with its scope pin, sorted.
fn assistants_valid(value: &Routine) -> bool {
    let mut used: Vec<&str> = value
        .permitted
        .iter()
        .filter_map(|item| as_str(at(item, "assistant")))
        .collect();
    used.sort_unstable();
    used.dedup();
    let named: Vec<&str> = value
        .assistants
        .iter()
        .map(|(assistant, _)| assistant.as_str())
        .collect();
    named == used
        && value.assistants.len() <= MAX_ROUTINE_ASSISTANTS
        && value.assistants.windows(2).all(|pair| pair[0] < pair[1])
        && value.assistants.iter().all(|(assistant, digest)| {
            canonical_assistant_id(&text(assistant)).is_some() && SOURCE_DIGEST_RE.is_full(digest)
        })
}

/// Whether a Routine's definition is in its closed contract: identity, plan, scope, confirmation, and schedule.
#[must_use]
pub fn definition_valid(value: &Routine) -> bool {
    // A well-formed plan's zone loads.
    if !plan::well_formed(&value.plan) {
        return false;
    }
    ROUTINE_ID_RE.is_full(&value.routine_id)
        && canonical_name(&text(&value.name)).is_some()
        && exact_description(&text(&value.description))
        && zoned(at(&value.plan, "timezone"), &text(&value.timezone_source))
        && canonical_schedule(&value.schedule).is_some()
        && permitted_valid(value)
        && assistants_valid(value)
        && confirmed(value.confirmation.as_ref())
        && valid_revision(&int(value.revision))
}

/// A copy of a Routine revision's definition in the closed contract, within its budget; anything else is refused.
///
/// A definition that changed clears a scope hold and starts a fresh failure streak and output comparison.
fn defined(value: &Routine, revision: i64) -> Transition<Routine> {
    let mut defined = value.clone();
    defined.revision = revision;
    if !definition_valid(&defined) {
        return Err(RoutineStateError::new("routine-invalid"));
    }
    // Every page of what a Supervisor inspects is deliverable, and the definition fits its own budget (scale).
    if !definition::fits(value) {
        return Err(RoutineStateError::new("routine-too-large"));
    }
    defined.needs_reconfirm = false;
    defined.deleting = false;
    defined.failures = 0;
    defined.output_digest = String::new();
    Ok(defined)
}

/// A copy of a new Routine revision in the closed contract, scheduled from its anchor.
fn admitted(value: &Routine, revision: i64) -> Transition<Routine> {
    let mut admitted = defined(value, revision)?;
    if value.next_run_at != first_run(&admitted)? {
        return Err(RoutineStateError::new("routine-invalid"));
    }
    admitted.gap_started_at = 0;
    admitted.missed = 0;
    admitted.reported_missed = 0;
    admitted.rollup_minute = 0;
    admitted.rollup_runs = 0;
    admitted.rollup_usage = None;
    admitted.run_requested = 0;
    Ok(admitted)
}

/// A new revision's first firing: a continuous Routine at once, any other its schedule's first after its anchor.
///
/// # Errors
///
/// `routine-invalid` for a schedule or zone outside the contract.
pub fn first_run(value: &Routine) -> Transition<i64> {
    if continuous(value) {
        Ok(value.anchor)
    } else {
        next_after(value, value.anchor)
    }
}

/// A defined Routine scheduled from `now`, the instant it is durable.
///
/// # Errors
///
/// `routine-invalid` for a schedule or zone outside the contract.
pub fn scheduled(value: &Routine, now: i64) -> Transition<Routine> {
    let mut anchored = value.clone();
    anchored.anchor = now;
    anchored.next_run_at = first_run(&anchored)?;
    Ok(anchored)
}

fn budgets(others: &[&Routine], admitted: &Routine) -> Transition<()> {
    definition::over_budget(others, admitted)
        .map_or(Ok(()), |refused| Err(RoutineStateError::new(refused)))
}

/// Add a confirmed Routine with its created notice in one transition; its minted id never exists twice.
///
/// The notice shows what the recording that made it spent: its model `usage` and its Team `steps` (ADR-0101).
///
/// # Errors
///
/// The [`RoutineStateError`] of the first refusal.
pub fn create(
    state: &TeamRoutines,
    value: &Routine,
    now: i64,
    usage: Option<Value>,
    steps: Vec<Value>,
    entropy: &dyn Entropy,
) -> Transition {
    changed(state, |next| {
        add_routine_into(next, value)?;
        let mut notice = Notice::new(
            &new_id(entropy)?,
            &value.routine_id,
            "",
            "created",
            now,
            object(vec![("steps", Value::Array(steps))]),
        );
        notice.usage = usage;
        put_notice_into(next, notice)
    })
}

/// The Routine of this id, unless it is being deleted, at exactly the revision a card saw, with no live run.
fn changeable<'s>(
    state: &'s TeamRoutines,
    routine_id: &str,
    expected_revision: i64,
) -> Transition<&'s Routine> {
    let current = routine(state, routine_id)?;
    if current.deleting {
        return Err(RoutineStateError::new("routine-not-found"));
    }
    if current.revision != expected_revision {
        return Err(RoutineStateError::new("routine-revision-changed"));
    }
    if live_runs(state)
        .iter()
        .any(|item| item.routine_id == current.routine_id)
    {
        return Err(RoutineStateError::new("routine-busy"));
    }
    Ok(current)
}

fn others<'s>(state: &'s TeamRoutines, routine_id: &str) -> Vec<&'s Routine> {
    state
        .routines
        .iter()
        .filter(|item| item.routine_id != routine_id)
        .collect()
}

/// Replace a Routine's definition as its next revision, with its changed notice.
///
/// Only the revision the card saw changes, never while a run is live or it is being deleted; it clears a scope hold,
/// keeps a pause and the minute rollup, and restarts its schedule. An incident of an earlier revision stays, and can
/// no longer resume.
///
/// # Errors
///
/// The [`RoutineStateError`] of the first refusal.
pub fn update(
    state: &TeamRoutines,
    value: &Routine,
    expected_revision: i64,
    now: i64,
    entropy: &dyn Entropy,
) -> Transition {
    let current = changeable(state, &value.routine_id, expected_revision)?;
    let mut changed_routine = admitted(value, current.revision + 1)?;
    budgets(&others(state, &current.routine_id), &changed_routine)?;
    // The minute rollup outlives a change: its notice id is the Routine's and the minute's, so a count restarted at 1
    // would reuse a delivered version of the same notice.
    changed_routine.paused = current.paused;
    changed_routine.rollup_minute = current.rollup_minute;
    changed_routine.rollup_runs = current.rollup_runs;
    changed_routine
        .rollup_usage
        .clone_from(&current.rollup_usage);
    changed(state, |next| {
        replace_routine(next, changed_routine.clone());
        let notice = Notice::new(
            &new_id(entropy)?,
            &changed_routine.routine_id,
            "",
            "changed",
            now,
            object(vec![]),
        );
        put_notice_into(next, notice)
    })
}

/// Replace a Routine's definition by its confirmed refinement as its next revision, with its changed notice.
///
/// Only the revision the card was built from changes, never while a run is live or it is being deleted (ADR-0122). A
/// refinement changes what each run does and nothing about when it runs: the Routine keeps its name, schedule, zone,
/// and its scheduling state as it is at this write, so a run that ended while the card was open is neither repeated
/// nor skipped. Its budgets are checked again.
///
/// # Errors
///
/// The [`RoutineStateError`] of the first refusal.
pub fn refine(
    state: &TeamRoutines,
    value: &Routine,
    expected_revision: i64,
    now: i64,
    entropy: &dyn Entropy,
) -> Transition {
    let current = changeable(state, &value.routine_id, expected_revision)?;
    if value.timezone() != current.timezone() {
        return Err(RoutineStateError::new("routine-invalid"));
    }
    let mut definition = current.clone();
    definition.description.clone_from(&value.description);
    definition.assistants.clone_from(&value.assistants);
    definition.plan.clone_from(&value.plan);
    definition.permitted.clone_from(&value.permitted);
    definition.confirmation.clone_from(&value.confirmation);
    let refined = defined(&definition, current.revision + 1)?;
    budgets(&others(state, &current.routine_id), &refined)?;
    changed(state, |next| {
        replace_routine(next, refined.clone());
        let notice = Notice::new(
            &new_id(entropy)?,
            &refined.routine_id,
            "",
            "changed",
            now,
            object(vec![]),
        );
        put_notice_into(next, notice)
    })
}

/// Add a new Routine at its first revision.
///
/// # Errors
///
/// `routine-invalid`, `routine-too-large`, `routine-exists`, `routine-limit`, or a budget refusal.
pub fn add_routine(state: &TeamRoutines, value: &Routine) -> Transition {
    changed(state, |next| add_routine_into(next, value))
}

fn add_routine_into(state: &mut TeamRoutines, value: &Routine) -> Transition<()> {
    let admitted = admitted(value, 1)?;
    if state
        .routines
        .iter()
        .any(|item| item.routine_id == admitted.routine_id)
    {
        return Err(RoutineStateError::new("routine-exists"));
    }
    if state.routines.len() >= MAX_ROUTINES {
        return Err(RoutineStateError::new("routine-limit"));
    }
    budgets(&state.routines.iter().collect::<Vec<_>>(), &admitted)?;
    state.routines.push(admitted);
    Ok(())
}
