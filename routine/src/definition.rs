//! What a Routine definition is on the wire and in its Team's budgets, without I/O (ADR-0092, ADR-0101).
//!
//! A definition is a recorded plan and its standing scope: the Actions it may call at their pins (every replay step's),
//! whether each is read-only, and the Stored Inputs each uses by name only. A Supervisor inspects a Routine through its
//! plan's safe projection, its summary, and its pages. On the wire a step is named by its 1-based position in its
//! revision's plan, never by its internal id: a projected step shows its Action, whether it only reads, each input's
//! source (a literal as a bounded preview, a reference by the earlier step's position, its pointer, and through an
//! array item its selector and the item's pointer), and the Stored Inputs its Action uses. Every list view and notice
//! carries a compact summary of the revision instead of its steps.
//!
//! A definition's share of its Team's budgets is one function of its units: its replay steps ([`run_units`]), which
//! every start reserves, every rolling 24-hour cap multiplies, and the run's active time grows with. Paused Routines
//! count, so resuming one never needs room it lacks; a deleting one keeps its share until it is gone.

use strict_json::Value;
use team_protocol::routine::{
    MAX_PAGE_BYTES, MAX_PAGE_STEPS, MAX_SUMMARY_RUNS, SHOWN_MODES, canonical_step, daily_cap,
    encoded_bytes, literal_preview, preview, where_text,
};

use crate::dates;
use crate::json::{as_str, at, canonical, elements, int, members, object, text};
use crate::plan::{self, MAX_DAILY_STEPS, MAX_DEFINITION_BYTES, TEAM_DEFINITION_BYTES};
use crate::record::Routine;

/// A run's units: its replay steps, which every budget derives from (ADR-0101 section 9).
#[must_use]
pub fn run_units(value: &Routine) -> i64 {
    i64::try_from(elements(at(&value.plan, "steps")).len()).unwrap_or(i64::MAX)
}

/// The Routine's state: paused by a person, or active.
#[must_use]
pub const fn state(value: &Routine) -> &'static str {
    if value.paused { "paused" } else { "active" }
}

/// Each step id's 1-based position.
fn position_of(plan: &Value, step_id: &str) -> Option<i64> {
    elements(at(plan, "steps"))
        .iter()
        .position(|step| as_str(at(step, "id")) == Some(step_id))
        .and_then(|index| i64::try_from(index + 1).ok())
}

fn permitted_entry<'p>(
    permitted: &'p [Value],
    assistant: &Value,
    action: &Value,
) -> Option<&'p Value> {
    permitted
        .iter()
        .find(|item| at(item, "assistant") == assistant && at(item, "action") == action)
}

/// A step-output source's item selector as a person reads it, or `None` when it selects no item.
#[must_use]
pub fn shown_where(source: &Value) -> Option<Value> {
    let (key, constant) = members(source.get("where")?).first()?;
    Some(object(vec![
        ("member", text(key)),
        (
            "value_json",
            where_text(constant).map_or(Value::Null, Value::String),
        ),
    ]))
}

fn input(member: &str, source: &Value, plan: &Value) -> Value {
    match as_str(at(source, "kind")) {
        Some("literal") => object(vec![
            ("member", text(member)),
            ("source", text("literal")),
            ("value", text(&literal_preview(at(source, "value")))),
        ]),
        Some("run_clock") => {
            let shown = match as_str(at(source, "format")) {
                Some("date") => "date".to_owned(),
                _ => dates::shown(elements(at(source, "parts"))),
            };
            object(vec![
                ("member", text(member)),
                ("source", text("run_clock")),
                ("value", text(&preview(&shown))),
            ])
        }
        _ => object(vec![
            ("member", text(member)),
            ("source", text("step_output")),
            (
                "step",
                as_str(at(source, "step"))
                    .and_then(|step| position_of(plan, step))
                    .map_or(Value::Null, int),
            ),
            ("pointer", at(source, "pointer").clone()),
            ("where", shown_where(source).unwrap_or(Value::Null)),
            ("item", source.get("item").cloned().unwrap_or(Value::Null)),
        ]),
    }
}

/// The projection of the step at `position`: its Action, its effect, every input's source, its Stored Inputs.
///
/// `None` when the position is outside the plan or its Action is not permitted.
#[must_use]
pub fn step(plan: &Value, permitted: &[Value], position: i64) -> Option<Value> {
    let index = usize::try_from(position.checked_sub(1)?).ok()?;
    let value = elements(at(plan, "steps")).get(index)?;
    let entry = permitted_entry(permitted, at(value, "assistant"), at(value, "action"))?;
    let mut names: Vec<&(String, Value)> = members(at(value, "input")).iter().collect();
    names.sort_by(|left, right| left.0.cmp(&right.0));
    Some(object(vec![
        ("position", int(position)),
        ("assistant", at(value, "assistant").clone()),
        ("action", at(value, "action").clone()),
        ("read_only", at(entry, "read_only").clone()),
        (
            "inputs",
            Value::Array(
                names
                    .into_iter()
                    .map(|(member, source)| input(member, source, plan))
                    .collect(),
            ),
        ),
        ("stored_inputs", at(entry, "stored_inputs").clone()),
    ]))
}

/// A Routine as a recording send's Brain sees it once the send selects it (ADR-0122).
///
/// Its description and revision, each step's id beside its plan projection, its output by step id, and the latest
/// run's outcome per step, or `null` when that evidence is unavailable. `None` when a step is not permitted.
#[must_use]
pub fn selected(value: &Routine, last_run: Option<Value>) -> Option<Value> {
    let plan = &value.plan;
    let mut steps = Vec::new();
    for (index, item) in elements(at(plan, "steps")).iter().enumerate() {
        let Value::Object(projected) =
            step(plan, &value.permitted, i64::try_from(index + 1).ok()?)?
        else {
            return None;
        };
        let mut entry = vec![("id".to_owned(), at(item, "id").clone())];
        entry.extend(projected);
        steps.push(Value::Object(entry));
    }
    Some(object(vec![
        ("routine_id", text(&value.routine_id)),
        ("name", text(&value.name)),
        ("description", text(&value.description)),
        ("revision", int(value.revision)),
        ("output", at(plan, "output").clone()),
        ("steps", Value::Array(steps)),
        ("last_run", last_run.unwrap_or(Value::Null)),
    ]))
}

/// Whether every step's projection stays within its own wire bound, so every page of the plan is deliverable.
#[must_use]
pub fn steps_fit(plan: &Value, permitted: &[Value]) -> bool {
    let total = i64::try_from(elements(at(plan, "steps")).len()).unwrap_or(i64::MAX);
    (1..=total).all(|position| {
        step(plan, permitted, position)
            .is_some_and(|projected| canonical_step(&projected, position).is_some())
    })
}

/// The revision's compact summary: its step count and its Actions as runs, at most 16, with the steps after them.
#[must_use]
pub fn summary(plan: &Value, revision: i64) -> Value {
    let mut runs: Vec<(Value, Value, i64)> = Vec::new();
    for value in elements(at(plan, "steps")) {
        let (assistant, action) = (at(value, "assistant"), at(value, "action"));
        match runs.last_mut() {
            Some(last) if last.0 == *assistant && last.1 == *action => last.2 += 1,
            _ => runs.push((assistant.clone(), action.clone(), 1)),
        }
    }
    runs.truncate(MAX_SUMMARY_RUNS);
    let total = i64::try_from(elements(at(plan, "steps")).len()).unwrap_or(i64::MAX);
    let more = total - runs.iter().map(|run| run.2).sum::<i64>();
    let actions = runs
        .into_iter()
        .map(|(assistant, action, count)| Value::Array(vec![assistant, action, int(count)]))
        .collect();
    object(vec![
        ("revision", int(revision)),
        ("plan_digest", text(&plan::digest(plan))),
        ("steps", int(total)),
        ("actions", Value::Array(actions)),
        ("more", int(more)),
    ])
}

/// The plan's output disposition on the wire: its shown step by position, or none.
#[must_use]
pub fn disposition(plan: &Value) -> Value {
    let output = at(plan, "output");
    let shown = matches!(at(output, "step"), Value::Null);
    let total = i64::try_from(elements(at(plan, "steps")).len()).unwrap_or(i64::MAX);
    object(vec![
        ("mode", at(output, "mode").clone()),
        ("step", if shown { Value::Null } else { int(total) }),
    ])
}

/// Whether a plan shows a result.
#[must_use]
pub fn shows(plan: &Value) -> bool {
    as_str(at(at(plan, "output"), "mode")).is_some_and(|mode| SHOWN_MODES.contains(&mode))
}

/// Whole consecutive projected steps from `offset`, as many as fit one page, and where the next page starts.
///
/// `None` when a step is not permitted.
#[must_use]
pub fn page(
    routine_id: &str,
    revision: i64,
    plan: &Value,
    permitted: &[Value],
    offset: i64,
) -> Option<Value> {
    let total = i64::try_from(elements(at(plan, "steps")).len()).unwrap_or(i64::MAX);
    let chosen = paged(|position| step(plan, permitted, position), offset, total)?;
    let following = offset + i64::try_from(chosen.len()).unwrap_or(i64::MAX);
    Some(object(vec![
        ("routine_id", text(routine_id)),
        ("revision", int(revision)),
        ("plan_digest", text(&plan::digest(plan))),
        ("total", int(total)),
        ("offset", int(offset)),
        ("steps", Value::Array(chosen)),
        (
            "next",
            if following == total {
                Value::Null
            } else {
                int(following)
            },
        ),
    ]))
}

/// Whole consecutive entries from `offset`, by 1-based position, as many as fit one page's count and bytes.
pub fn paged(entry: impl Fn(i64) -> Option<Value>, offset: i64, total: i64) -> Option<Vec<Value>> {
    let mut chosen: Vec<Value> = Vec::new();
    let mut used = 2;
    for position in offset + 1..=total {
        let item = entry(position)?;
        let cost = encoded_bytes(&item) + 1;
        if !chosen.is_empty() && (chosen.len() == MAX_PAGE_STEPS || used + cost > MAX_PAGE_BYTES) {
            break;
        }
        chosen.push(item);
        used += cost;
    }
    Some(chosen)
}

/// Whether every projected step of a Routine is within its wire bound and its definition within its own budget.
#[must_use]
pub fn fits(value: &Routine) -> bool {
    fits_within(value, MAX_DEFINITION_BYTES)
}

/// [`fits`] under its own definition bound.
pub(crate) fn fits_within(value: &Routine, maximum: usize) -> bool {
    steps_fit(&value.plan, &value.permitted) && definition_bytes(value) <= maximum
}

/// The canonical bytes of a Routine's definition: its plan and its standing scope, which its budget bounds.
#[must_use]
pub fn definition_bytes(value: &Routine) -> usize {
    let standing = object(vec![
        ("permitted", Value::Array(value.permitted.clone())),
        (
            "confirmation",
            value.confirmation.clone().unwrap_or(Value::Null),
        ),
    ]);
    canonical(&value.plan).len() + canonical(&standing).len()
}

/// The Action units a Routine's rolling 24-hour cap allocates: every start may use every unit.
#[must_use]
pub fn daily_steps(value: &Routine) -> i64 {
    daily_cap(&value.schedule).saturating_mul(run_units(value))
}

/// The daily Action units left for a new or changed Routine after these Routines' allocations.
#[must_use]
pub fn capacity(routines: &[&Routine]) -> i64 {
    MAX_DAILY_STEPS - routines.iter().map(|item| daily_steps(item)).sum::<i64>()
}

/// Which Team budget a definition outgrows beside the Team's other Routines, or `None`: daily steps or bytes.
#[must_use]
pub fn over_budget(others: &[&Routine], admitted: &Routine) -> Option<&'static str> {
    if daily_steps(admitted) > capacity(others) {
        return Some("routine-step-budget");
    }
    let bytes: usize = others
        .iter()
        .map(|item| definition_bytes(item))
        .sum::<usize>()
        + definition_bytes(admitted);
    (bytes > TEAM_DEFINITION_BYTES).then_some("routine-team-budget")
}
