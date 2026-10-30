//! The confirmation card a recording or refinement shows, what its confirmation binds, and whether its description
//! agrees with the card's own settings (ADR-0101, ADR-0122).

use std::collections::BTreeSet;

use strict_json::Value;
use team_protocol::phrase;
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::value::{int, object_of, py_eq};
use team_protocol::routine::escaped;
use team_protocol::routine_proposal::OUTPUT_CHOICES;
use team_routine::dates;
use team_routine::definition::shown_where;
use team_routine::json::{as_str, at, elements, members};
use team_routine::plan::digest;
use team_routine::record::Routine;
use team_routine::recording::Recorded;
use team_routine::trace::exposes;
use team_storage::private::instant_text;

use super::{Stopped, within_bound};

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn optional(value: Option<&str>) -> Value {
    value.map_or(Value::Null, text)
}

/// Each step's 1-based position in the plan, by its id.
#[must_use]
pub fn step_positions(document: &Value) -> Vec<(String, i64)> {
    elements(at(document, "steps"))
        .iter()
        .zip(1..)
        .filter_map(|(step, position)| Some((as_str(at(step, "id"))?.to_owned(), position)))
        .collect()
}

fn position(positions: &[(String, i64)], step: &str) -> Value {
    positions
        .iter()
        .find(|(id, _)| id == step)
        .map_or(Value::Null, |(_, position)| int(*position))
}

/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` with every invisible character
/// escaped.
fn escaped_json(value: &Value) -> String {
    escaped(&dumps(
        value,
        Options {
            ensure_ascii: false,
            sort_keys: true,
            compact: true,
        },
    ))
}

fn card_input(member: &str, source: &Value, origin: &str, positions: &[(String, i64)]) -> Value {
    let shown =
        |origin: &str, value: Value, step: Value, pointer: Value, place: Value, item: Value| {
            object_of(vec![
                ("member", text(member)),
                ("origin", text(origin)),
                ("value", value),
                ("step", step),
                ("pointer", pointer),
                ("where", place),
                ("item", item),
            ])
        };
    let none = || Value::Null;
    match as_str(at(source, "kind")) {
        Some("literal") => shown(
            origin,
            text(&escaped_json(at(source, "value"))),
            none(),
            none(),
            none(),
            none(),
        ),
        Some("run_clock") => {
            // The run date alone, or a text template complete, each of its dates in braces as a person reads it.
            let value = match as_str(at(source, "format")) {
                Some("date") => Value::Null,
                _ => text(&escaped(&dates::shown(elements(at(source, "parts"))))),
            };
            shown("clock", value, none(), none(), none(), none())
        }
        _ => shown(
            origin,
            none(),
            as_str(at(source, "step")).map_or(Value::Null, |step| position(positions, step)),
            at(source, "pointer").clone(),
            shown_where(source).unwrap_or(Value::Null),
            source.get("item").cloned().unwrap_or(Value::Null),
        ),
    }
}

fn card_step(
    step: &Value,
    candidate: &Routine,
    recorded: &Recorded,
    positions: &[(String, i64)],
) -> Value {
    let (assistant, action) = (at(step, "assistant"), at(step, "action"));
    let id = as_str(at(step, "id")).unwrap_or_default();
    let read_only = candidate
        .permitted
        .iter()
        .find(|item| at(item, "assistant") == assistant && at(item, "action") == action)
        .map_or(Value::Null, |item| at(item, "read_only").clone());
    let mut inputs: Vec<&(String, Value)> = members(at(step, "input")).iter().collect();
    inputs.sort_by(|left, right| left.0.cmp(&right.0));
    let inputs = inputs
        .into_iter()
        .map(|(member, source)| {
            card_input(
                member,
                source,
                recorded.origin(id, member).unwrap_or_default(),
                positions,
            )
        })
        .collect();
    object_of(vec![
        ("position", position(positions, id)),
        ("assistant", assistant.clone()),
        ("action", action.clone()),
        ("read_only", read_only),
        ("inputs", Value::Array(inputs)),
    ])
}

/// The confirmation card: the description, the schedule, zone, and output, and each step with its effect and inputs.
///
/// Its description is the recording agent's account of the work alone; the schedule, zone, output, and steps are the
/// candidate's own, which Admin shows in its fixed words, so model text never stands in for any of them.
#[must_use]
pub fn card(
    proposal_id: &str,
    candidate: &Routine,
    recorded: &Recorded,
    framing: (Option<&str>, i64),
) -> Value {
    let (replaces, expires_at) = framing;
    let document = &candidate.plan;
    let positions = step_positions(document);
    let steps = elements(at(document, "steps"))
        .iter()
        .map(|step| card_step(step, candidate, recorded, &positions))
        .collect();
    object_of(vec![
        ("proposal_id", text(proposal_id)),
        ("expires_at", text(&instant_text(expires_at))),
        ("replaces", optional(replaces)),
        ("name", text(&candidate.name)),
        ("description", text(&candidate.description)),
        ("schedule", candidate.schedule.clone()),
        ("timezone", at(document, "timezone").clone()),
        ("timezone_source", text(&candidate.timezone_source)),
        ("output", at(at(document, "output"), "mode").clone()),
        ("steps", Value::Array(steps)),
    ])
}

/// What a confirmation binds: the exact definition, the card shown, and the revision a replacement saw.
#[must_use]
pub fn bound_digest(candidate: &Routine, view: &Value, expected_revision: Option<i64>) -> String {
    let assistants = candidate
        .assistants
        .iter()
        .map(|(assistant, scope)| Value::Array(vec![text(assistant), text(scope)]))
        .collect();
    digest(&object_of(vec![
        ("routine_id", text(&candidate.routine_id)),
        ("name", text(&candidate.name)),
        ("schedule", candidate.schedule.clone()),
        ("timezone_source", text(&candidate.timezone_source)),
        ("assistants", Value::Array(assistants)),
        ("plan", candidate.plan.clone()),
        ("permitted", Value::Array(candidate.permitted.clone())),
        ("view", view.clone()),
        (
            "expected_revision",
            expected_revision.map_or(Value::Null, int),
        ),
    ]))
}

/// Whether a card's description states no schedule, output, or timezone but the card's own (ADR-0101).
///
/// The output is read as Edit reads it, beside the card's own label in each interface language; a timezone counts only
/// where the card has one of its own. A description the phrase reader cannot read within its budget disagrees.
#[must_use]
pub fn description_agrees(view: &Value) -> bool {
    let description = as_str(at(view, "description")).unwrap_or_default();
    let output = as_str(at(view, "output")).unwrap_or_default();
    let mut zones = BTreeSet::new();
    if as_str(at(view, "timezone_source")) != Some("none")
        && let Some(zone) = as_str(at(view, "timezone"))
    {
        zones.insert(zone.to_owned());
    }
    let Ok(schedules) = phrase::stated(description, None) else {
        return false;
    };
    let labels = OUTPUT_CHOICES.iter().filter_map(|(_, choices)| {
        choices
            .iter()
            .find(|(mode, _)| *mode == output)
            .map(|(_, label)| *label)
    });
    let outputs_agree = labels.into_iter().all(|label| {
        phrase::outputs(&format!("{description}\n{label}")).is_ok_and(|found| found == [output])
    });
    let zones_agree =
        phrase::zones(description).is_ok_and(|found| found.iter().all(|zone| zones.contains(zone)));
    schedules
        .iter()
        .all(|schedule| py_eq(schedule, at(view, "schedule")))
        && outputs_agree
        && zones_agree
}

/// A card a person may be shown: within its bound, exposing nothing the span protects, and with a description that
/// agrees with its settings.
pub(crate) fn admissible(view: &Value, protected: &BTreeSet<String>) -> Result<(), Stopped> {
    if !within_bound(view) {
        return Err(Stopped::refused("routine-proposal-too-large"));
    }
    if exposes(view, protected) {
        return Err(Stopped::refused("routine-secret-literal"));
    }
    if !description_agrees(view) {
        return Err(Stopped::refused("routine-description-conflict"));
    }
    Ok(())
}
