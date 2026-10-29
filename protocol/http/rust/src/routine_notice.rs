//! Canonical Team Routine notices and the views Admin lists (`routine_notice.py`, ADR-0086, ADR-0092, ADR-0101).

use std::collections::HashSet;
use std::sync::LazyLock;

use strict_json::Value;

use crate::progress;
use crate::python::datetime::fromisoformat;
use crate::python::regex::{Pattern, declared};
use crate::python::value::{
    array, exact_object, field, int, natural_digits, object_of, py_eq, small_int, string, whole,
};
use crate::routine::{
    self, ERROR_CODE_RE, MAX_FAILURE_STREAK, MAX_ROLLUP_RUNS, MAX_ROUTINES, OUTCOMES,
    PAUSE_REASONS, ROUTINE_ID_RE, ROUTINE_OUTCOMES,
};

/// What a frozen run waits for: a person's answer to a declared human request, or an Integration.
pub const REQUEST_KINDS: [&str; 2] = ["human", "integrations"];
/// The Team steps a creation notice lists of the recording turns that made its Routine.
pub const MAX_CREATION_STEPS: usize = 64;
/// The most notices of one batch.
pub const MAX_NOTICE_BATCH: usize = 1024;
/// The encoded notice list of one batch, under the Local API's 128 KiB response cap.
pub const MAX_NOTICE_BATCH_BYTES: usize = 112 * 1024;
/// A live run's statuses, sorted.
pub const RUN_STATUSES: [&str; 3] = ["frozen", "held", "leased"];
/// A Routine's state: it runs, or a person paused it.
pub const ROUTINE_STATES: [&str; 2] = ["active", "paused"];
/// A run lease token.
pub static LEASE_TOKEN_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[A-Za-z0-9_-]{43}\Z"));
static INSTANT_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"));
/// The unresolved incidents a Team holds at most, which its Routine list carries.
pub const MAX_UNRESOLVED_INCIDENTS: usize = 32;
/// A plan summary encoded at most.
pub const MAX_SUMMARY_BYTES: usize = 4 * 1024;
/// A Team's whole Routine list, encoded.
pub const MAX_ROUTINE_LIST_BYTES: usize = MAX_ROUTINES * (MAX_SUMMARY_BYTES + 8 * 1024 + 1024)
    + MAX_UNRESOLVED_INCIDENTS * 4 * 1024
    + 4 * 1024;

/// `x in options` of a tuple of strings: equality, so only an equal string is in it.
fn among(value: &Value, options: &[&str]) -> bool {
    string(value).is_some_and(|text| options.contains(&text))
}

/// `type(x) is int and x >= 1`, at any size.
fn positive(value: &Value) -> bool {
    natural_digits(value).is_some_and(|digits| digits != "0")
}

/// `instant`: a real UTC instant in whole seconds, written `YYYY-MM-DDTHH:MM:SSZ`.
#[must_use]
pub fn instant(value: &Value) -> bool {
    string(value).is_some_and(|text| INSTANT_RE.is_full(text) && fromisoformat(text).is_ok())
}

/// `optional`: null, or a string that fully matches `pattern`.
#[must_use]
pub fn optional(value: &Value, pattern: &Pattern) -> bool {
    *value == Value::Null || routine::matches(value, pattern)
}

fn completed(detail: &[(String, Value)]) -> bool {
    let output = field(detail, "output");
    routine::canonical_summary(field(detail, "plan")).is_some_and(|summary| {
        *output == Value::Null
            || (routine::canonical_output(output).is_some()
                && py_eq(field_of(output, "step"), field_of(summary, "steps")))
    })
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// `_creation`: each step a finished chat progress event, numbered from 1 in order. Python also requires the canonical
/// event to equal the step; `canonical_event` rebuilds exactly the step's own members, so an admitted step always does.
fn creation(detail: &[(String, Value)]) -> bool {
    let Some(steps) = array(field(detail, "steps")) else {
        return false;
    };
    steps.len() <= MAX_CREATION_STEPS
        && steps.iter().zip(1_i64..).all(|(step, position)| {
            progress::canonical_event(step).is_ok_and(|event| {
                string(field_of(&event, "state")) == Some("finished")
                    && py_eq(field_of(&event, "seq"), &int(position))
            })
        })
}

/// An outcome's exact detail fields and the check of its members.
type DetailRule = (&'static [&'static str], fn(&[(String, Value)]) -> bool);

/// Each outcome's exact detail fields and check.
fn detail_rule(outcome: &str) -> DetailRule {
    match outcome {
        "done" | "recovered" => (&["plan", "output"], completed),
        "paused" => (&["reason"], |detail| {
            among(field(detail, "reason"), &PAUSE_REASONS)
        }),
        "skipped" => (&["missed"], |detail| positive(field(detail, "missed"))),
        "healthy" => (&["runs"], |detail| {
            small_int(field(detail, "runs"))
                .and_then(|runs| usize::try_from(runs).ok())
                .is_some_and(|runs| (1..=MAX_ROLLUP_RUNS).contains(&runs))
        }),
        "frozen" => (&["request_kind"], |detail| {
            among(field(detail, "request_kind"), &REQUEST_KINDS)
        }),
        "failed" => (&["code"], |detail| {
            routine::matches(field(detail, "code"), &ERROR_CODE_RE)
        }),
        "created" => (&["steps"], creation),
        "streak-paused" => (&["failures"], |detail| {
            whole(field(detail, "failures"), 1, MAX_FAILURE_STREAK)
        }),
        _ => (&[], |_| true),
    }
}

/// `notice_usage`: whether `usage` is exactly what a notice of `outcome` carries.
#[must_use]
pub fn notice_usage(outcome: &Value, usage: &Value) -> bool {
    if string(outcome) == Some("created") {
        return *usage == Value::Null
            || routine::canonical_turn_usage(usage)
                .is_some_and(|admitted| py_eq(&admitted, usage));
    }
    if among(outcome, &ROUTINE_OUTCOMES) && string(outcome) != Some("healthy") {
        return *usage == Value::Null;
    }
    routine::canonical_run_usage(usage).is_some()
}

/// `canonical_notice_detail`: the exact closed detail for a run outcome; never an Action's raw input or result.
#[must_use]
pub fn canonical_notice_detail<'a>(outcome: &Value, detail: &'a Value) -> Option<&'a Value> {
    let outcome = string(outcome).filter(|outcome| OUTCOMES.contains(outcome))?;
    let (fields, valid) = detail_rule(outcome);
    exact_object(detail, fields)
        .filter(|members| valid(members))
        .map(|_| detail)
}

/// `canonical_routine_view`: one Routine as a Supervisor sees it: its plan summary, disposition, and state.
#[must_use]
pub fn canonical_routine_view(value: &Value) -> Option<&Value> {
    let fields = [
        "routine_id",
        "name",
        "schedule",
        "timezone",
        "timezone_source",
        "next_run_at",
        "needs_reconfirm",
        "state",
        "deleting",
        "plan",
        "output",
    ];
    let members = exact_object(value, &fields)?;
    let summary = routine::canonical_summary(field(members, "plan"))?;
    let valid = routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::exact_name(field(members, "name"))
        && routine::canonical_disposition(field(members, "output"), field_of(summary, "steps"))
            .is_some()
        && routine::exact_schedule(field(members, "schedule"))
        && routine::zoned(
            field(members, "timezone"),
            field(members, "timezone_source"),
        )
        && instant(field(members, "next_run_at"))
        && matches!(field(members, "needs_reconfirm"), Value::Bool(_))
        && matches!(field(members, "deleting"), Value::Bool(_))
        && among(field(members, "state"), &ROUTINE_STATES);
    valid.then_some(value)
}

/// `canonical_run_view`: one live run: a frozen run names the request it waits for, and its call.
#[must_use]
pub fn canonical_run_view(value: &Value) -> Option<&Value> {
    let fields = [
        "run_id",
        "routine_id",
        "status",
        "request_kind",
        "assistant_id",
        "action",
    ];
    let members = exact_object(value, &fields)?;
    let (kind, assistant, action) = (
        field(members, "request_kind"),
        field(members, "assistant_id"),
        field(members, "action"),
    );
    let status = string(field(members, "status")).filter(|status| RUN_STATUSES.contains(status));
    let request = if status == Some("frozen") {
        among(kind, &REQUEST_KINDS)
            && routine::valid_assistant(assistant)
            && routine::valid_action(action)
    } else {
        [kind, assistant, action]
            .iter()
            .all(|item| **item == Value::Null)
    };
    let valid = routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && status.is_some()
        && request;
    valid.then_some(value)
}

/// `_held_step`: the call a held run stopped at and its position, or all null when it sealed no plan.
fn held_step(members: &[(String, Value)]) -> bool {
    if *field(members, "assistant_id") == Value::Null {
        return ["action", "position", "steps"]
            .iter()
            .all(|key| *field(members, key) == Value::Null);
    }
    routine::valid_assistant(field(members, "assistant_id"))
        && routine::valid_action(field(members, "action"))
        && routine::canonical_position(field(members, "position"), field(members, "steps"))
            .is_some()
}

/// `canonical_incident_view`: one unresolved incident of a held run, its held call by position.
#[must_use]
pub fn canonical_incident_view(value: &Value) -> Option<&Value> {
    let fields = [
        "run_id",
        "routine_id",
        "assistant_id",
        "action",
        "position",
        "steps",
    ];
    let members = exact_object(value, &fields)?;
    let valid = routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && held_step(members);
    valid.then_some(value)
}

/// `canonical_notice`: one undelivered run or Routine outcome for Admin to write to its Team's transcript.
#[must_use]
pub fn canonical_notice(value: &Value) -> Option<&Value> {
    let fields = [
        "team_id",
        "notice_id",
        "version",
        "routine_id",
        "name",
        "run_id",
        "outcome",
        "created_at",
        "detail",
        "usage",
        "protection_lost",
    ];
    let members = exact_object(value, &fields)?;
    let (outcome, run_id) = (field(members, "outcome"), field(members, "run_id"));
    let routine_outcome = among(outcome, &ROUTINE_OUTCOMES);
    let protection = field(members, "protection_lost");
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "notice_id"), &ROUTINE_ID_RE)
        && positive(field(members, "version"))
        && routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::exact_name(field(members, "name"))
        && optional(run_id, &ROUTINE_ID_RE)
        && (*run_id == Value::Null) == routine_outcome
        && (*run_id == Value::Null || py_eq(run_id, field(members, "notice_id")))
        && instant(field(members, "created_at"))
        && canonical_notice_detail(outcome, field(members, "detail")).is_some()
        && notice_usage(outcome, field(members, "usage"))
        && matches!(protection, Value::Bool(_))
        && (!routine_outcome || *protection == Value::Bool(false));
    valid.then_some(value)
}

/// `canonical_notice_batch`: a bounded batch of notices; while `more` is true Admin acknowledges it and asks again.
#[must_use]
pub fn canonical_notice_batch(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["notices", "more"])?;
    let Value::Bool(more) = field(members, "more") else {
        return None;
    };
    let notices = array(field(members, "notices"))?;
    if notices.len() > MAX_NOTICE_BATCH || (*more && notices.is_empty()) {
        return None;
    }
    let admitted: Vec<Value> = notices
        .iter()
        .map(|notice| canonical_notice(notice).cloned())
        .collect::<Option<_>>()?;
    let admitted = Value::Array(admitted);
    if routine::encoded_bytes(&admitted) > MAX_NOTICE_BATCH_BYTES {
        return None;
    }
    let keys: HashSet<(Option<&str>, Option<&str>)> = notices
        .iter()
        .map(|notice| {
            (
                string(field_of(notice, "team_id")),
                string(field_of(notice, "notice_id")),
            )
        })
        .collect();
    (keys.len() == notices.len())
        .then(|| object_of(vec![("notices", admitted), ("more", Value::Bool(*more))]))
}
