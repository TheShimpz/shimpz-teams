//! The bounded shape of a step record's and a run record's sealed document.

use super::{
    BINDING_FIELDS, MAX_ROUTINE_STEPS, PLAN_DIGEST_RE, ROUTINE_ID_RE, RUN_STEP_STATUSES, Value,
    canonical_run_step, small_int, valid_revision, whole,
};

fn bound(document: &Value) -> bool {
    let member = |name: &str| document.get(name).unwrap_or(&Value::Null);
    ROUTINE_ID_RE.is_full_value(member("routine_id"))
        && valid_revision(member("revision"))
        && PLAN_DIGEST_RE.is_full_value(member("plan_digest"))
        && whole(member("total"), 1, MAX_ROUTINE_STEPS)
}

/// One sealed step record: its run binding and exactly one run entry's wire view at its own position.
pub(super) fn step_document(document: &Value, run: &str) -> bool {
    let Value::Object(members) = document else {
        return false;
    };
    let view = Value::Object(
        members
            .iter()
            .filter(|(name, _)| !BINDING_FIELDS.contains(&name.as_str()))
            .cloned()
            .collect(),
    );
    let status = view.get("status").and_then(|status| match status {
        Value::String(status) => Some(status.as_str()),
        _ => None,
    });
    let position = view.get("position").and_then(small_int);
    ROUTINE_ID_RE.is_full(run)
        && bound(document)
        && status.is_some_and(|status| RUN_STEP_STATUSES.contains(&status))
        && position.is_some_and(|position| {
            canonical_run_step(
                &view,
                position,
                document.get("total").unwrap_or(&Value::Null),
            )
            .is_some()
        })
}

pub(super) fn run_document(document: &Value, run: &str) -> bool {
    let fields = [
        "routine_id",
        "revision",
        "plan_digest",
        "total",
        "reached",
        "dispatched",
    ];
    let Value::Object(members) = document else {
        return false;
    };
    let total = document.get("total").and_then(small_int).unwrap_or(0);
    ROUTINE_ID_RE.is_full(run)
        && bound(document)
        && members.len() == fields.len()
        && members
            .iter()
            .all(|(name, _)| fields.contains(&name.as_str()))
        && document
            .get("reached")
            .and_then(small_int)
            .is_some_and(|reached| (0..=total).contains(&reached))
        && matches!(document.get("dispatched"), Some(Value::Bool(_)))
}
