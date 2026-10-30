//! What a Supervisor and a recording send's Brain read of a run's diagnostics and records, in the Team's current
//! incarnation (ADR-0092 section 8, ADR-0122).

use strict_json::{Number, Value};
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::value::{object_of, small_int};
use team_protocol::routine_run::{
    RUN_STEP_FIELDS, RUN_STEP_GAPS, canonical_diagnostics, canonical_run_steps,
};
use team_routine::definition::paged;

use super::{BINDING_FIELDS, DiagnosticError, DiagnosticStore, RunSteps};

/// Why a page of a run's entries could not be read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepsError {
    /// The snapshot the reader holds no longer holds (`routine-run-changed`, 409).
    Changed,
    /// No retained record, or an offset outside the run's steps (`routine-run-steps-not-found`, 404).
    NotFound,
    /// The records could not be read or admitted (`routine-diagnostics-unavailable`, 503).
    Unavailable,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

/// A Supervisor's view of one run's execution details in the Team's current incarnation.
///
/// # Errors
/// [`DiagnosticError`] when the diagnostics cannot be read or their view is not admitted.
pub fn run_diagnostics(
    store: &DiagnosticStore,
    team: &str,
    incarnation: &str,
    run: &str,
    now: i64,
) -> Result<Value, DiagnosticError> {
    let found = store.read(team, incarnation, run, now)?;
    let view = object_of(vec![
        ("team_id", text(team)),
        ("run_id", text(run)),
        (
            "diagnostics",
            Value::Array(found.iter().map(super::Diagnostic::view).collect()),
        ),
    ]);
    canonical_diagnostics(&view)
        .cloned()
        .ok_or(DiagnosticError::Malformed)
}

/// The run binding every retained record agrees on, or `None` when none is retained.
fn binding_of(records: &RunSteps) -> Result<Option<Value>, DiagnosticError> {
    let mut found: Option<Value> = None;
    for item in records.steps.values().chain(records.run.iter()) {
        let binding = object_of(
            BINDING_FIELDS
                .iter()
                .map(|name| (*name, item.get(name).cloned().unwrap_or(Value::Null)))
                .collect(),
        );
        let sorted = |value: &Value| {
            dumps(
                value,
                Options {
                    ensure_ascii: true,
                    sort_keys: true,
                    compact: false,
                },
            )
        };
        match &found {
            Some(known) if sorted(known) != sorted(&binding) => {
                return Err(DiagnosticError::Disagree);
            }
            Some(_) => {}
            None => found = Some(binding),
        }
    }
    Ok(found)
}

fn gap(position: i64, status: &str) -> Value {
    object_of(vec![
        ("position", int(position)),
        ("status", text(status)),
        ("assistant_id", Value::Null),
        ("action", Value::Null),
        ("attempt", Value::Null),
        ("duration_ms", Value::Null),
        ("inputs", Value::Null),
    ])
}

/// How far the run's terminal record proves it went: steps completed, and whether the next was dispatched.
fn reached(records: &RunSteps) -> Option<(i64, bool)> {
    let run = records.run.as_ref()?;
    let reached = run.get("reached").and_then(small_int)?;
    let dispatched = matches!(run.get("dispatched"), Some(Value::Bool(true)));
    Some((reached, dispatched))
}

/// One step's latest record, or the gap the run's own records prove: not run, or unavailable.
fn entry(records: &RunSteps, position: i64) -> Value {
    if let Some(record) = records.steps.get(&position) {
        return object_of(
            RUN_STEP_FIELDS
                .iter()
                .map(|name| (*name, record.get(name).cloned().unwrap_or(Value::Null)))
                .collect(),
        );
    }
    match reached(records) {
        Some((reached, dispatched))
            if position > reached + 1 || (position == reached + 1 && !dispatched) =>
        {
            gap(position, "not_run")
        }
        _ => gap(position, "unavailable"),
    }
}

fn total_of(binding: &Value) -> i64 {
    binding.get("total").and_then(small_int).unwrap_or(0)
}

/// A Routine's latest run as a recording send's Brain sees it: the revision it executed and each step's outcome.
///
/// Only the run's own retained records in the Team's current incarnation count, never a reconstruction: a step whose
/// record kept no inputs is `unavailable`, and evidence that expired, is missing, belongs to another incarnation, or
/// cannot be read is unavailable as a whole, `None` (ADR-0122).
#[must_use]
pub fn last_run(
    store: &DiagnosticStore,
    team: &str,
    incarnation: &str,
    routine: &str,
    now: i64,
) -> Option<Value> {
    let run = store.latest_run(team, routine, now).ok()??;
    let records = store
        .run_steps(team, incarnation, &run, now, "latest")
        .ok()?;
    let binding = binding_of(&records).ok()??;
    let steps = (1..=total_of(&binding))
        .map(|position| {
            let found = entry(&records, position);
            let status = found.get("status").cloned().unwrap_or(Value::Null);
            let gapped = matches!(&status, Value::String(status) if RUN_STEP_GAPS.contains(&status.as_str()));
            let kept = gapped || !matches!(found.get("inputs"), Some(Value::Null) | None);
            object_of(vec![
                ("position", int(position)),
                ("status", if kept { status } else { text("unavailable") }),
            ])
        })
        .collect();
    Some(object_of(vec![
        (
            "revision",
            binding.get("revision").cloned().unwrap_or(Value::Null),
        ),
        ("steps", Value::Array(steps)),
    ]))
}

/// A Supervisor's page of one run's entries from `offset`, bound to its own revision and one snapshot of its records.
/// The page needs only the run's own records, never its revision's plan, so it renders after any later change.
///
/// # Errors
/// [`StepsError`].
pub fn run_steps(
    store: &DiagnosticStore,
    team: &str,
    incarnation: &str,
    run: &str,
    snapshot: &str,
    offset: i64,
    now: i64,
) -> Result<Value, StepsError> {
    let records = store
        .run_steps(team, incarnation, run, now, snapshot)
        .map_err(|error| match error {
            DiagnosticError::RunChanged => StepsError::Changed,
            _ => StepsError::Unavailable,
        })?;
    let binding = binding_of(&records).map_err(|_| StepsError::Unavailable)?;
    let Some(binding) = binding else {
        return Err(StepsError::NotFound);
    };
    let total = total_of(&binding);
    if !(0..total).contains(&offset) {
        return Err(StepsError::NotFound);
    }
    let steps = paged(|position| Some(entry(&records, position)), offset, total)
        .ok_or(StepsError::Unavailable)?;
    let following = offset + i64::try_from(steps.len()).unwrap_or(i64::MAX);
    let member = |name: &str| binding.get(name).cloned().unwrap_or(Value::Null);
    let view = object_of(vec![
        ("team_id", text(team)),
        ("run_id", text(run)),
        ("routine_id", member("routine_id")),
        ("revision", member("revision")),
        ("plan_digest", member("plan_digest")),
        ("total", int(total)),
        ("snapshot", text(&records.snapshot)),
        ("offset", int(offset)),
        ("steps", Value::Array(steps)),
        (
            "next",
            if following == total {
                Value::Null
            } else {
                int(following)
            },
        ),
    ]);
    canonical_run_steps(&view)
        .cloned()
        .ok_or(StepsError::Unavailable)
}
