//! What one audit line may say: its principal, operation, result, and a closed set of metadata fields, never a body,
//! a secret, or provider data.
//!
//! The Local writer admits each event with [`admit`]: `operation`, `result`, `principal_id`, and `principal_class`
//! always; `trace_id` and `credential_state` from the principal; `team_id`, `assistant`, `detail`, `model_usage`, and a
//! failed Action's closed `cause` (`team_protocol::cause`) for an ordinary event; `team_id` and `activity` for an
//! activity event (ADR-0120). Anything else refuses the event, so no caller can put a body or a credential into the
//! log. The in-memory audit of the tests admits through this same function, so a record the Local log would refuse
//! fails there too.

use strict_json::{Number, Value};
use team_protocol::cause::canonical_cause;
use team_protocol::websocket::canonical_origin;

use super::AuditEvent;

/// The shape of the credential a request presented, never the credential.
pub const CREDENTIAL_STATES: [&str; 5] = [
    "assertion_absent_or_malformed",
    "assertion_present",
    "assertion_rejected",
    "machine_bearer_present",
    "machine_bearer_rejected",
];

/// The closed activity operations: a Team's Sign-in (ADR-0117) and its learned memory (ADR-0121).
pub const ACTIVITY_OPERATIONS: [&str; 8] = [
    "browser-sign-in-start",
    "browser-sign-in-save",
    "browser-sign-in-cancel",
    "browser-sign-in-delete",
    "browser-sign-in-expire",
    "memory-remember",
    "memory-forget",
    "memory-evicted",
];

const ACTIVITY_KEYS: [&str; 9] = [
    "event_id",
    "phase",
    "subject",
    "generation",
    "incarnation",
    "factor",
    "initiator_id",
    "origin",
    "outcome",
];
const ACTIVITY_PHASES: [&str; 3] = ["requested", "completed", "failed"];
/// The longest activity origin.
pub const MAX_ACTIVITY_ORIGIN_CHARS: usize = 255;

/// The event is not admissible; it is never written.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Inadmissible;

/// The admitted fields of one event, before the writer adds `ts`, `service`, `trace_id` (when drawn), and
/// `prev_sha256`. A field whose value is `null` is left out, except `principal_id`.
///
/// # Errors
/// [`Inadmissible`] for an event outside the closed shape.
pub fn admit(event: &AuditEvent) -> Result<Vec<(String, Value)>, Inadmissible> {
    let get = |name: &str| {
        event
            .fields()
            .iter()
            .find(|(field, _)| field == name)
            .map(|(_, value)| value)
    };
    let Some(Value::String(operation)) = get("operation") else {
        return Err(Inadmissible);
    };
    let activity = get("activity").filter(|value| **value != Value::Null);
    let allowed: &[&str] = if activity.is_some() {
        &["team_id", "activity"]
    } else {
        &["team_id", "assistant", "detail", "model_usage", "cause"]
    };
    let principal = [
        "operation",
        "result",
        "principal_id",
        "principal_class",
        "credential_state",
        "trace_id",
    ];
    if event
        .fields()
        .iter()
        .any(|(name, _)| !principal.contains(&name.as_str()) && !allowed.contains(&name.as_str()))
    {
        return Err(Inadmissible);
    }
    if !principal_holds(get("principal_class"), get("principal_id"))
        || !matches!(get("result"), Some(Value::String(result)) if ["denied", "error", "ok"].contains(&result.as_str()))
        || !get("credential_state").is_none_or(|state| matches!(state, Value::String(state) if CREDENTIAL_STATES.contains(&state.as_str())))
        || !get("trace_id").is_none_or(|trace| matches!(trace, Value::String(trace) if lower_hex(trace, 32)))
        || !["team_id", "assistant", "detail"]
            .iter()
            .all(|name| matches!(get(name), None | Some(Value::Null | Value::String(_))))
        || !matches!(get("model_usage"), None | Some(Value::Null | Value::Array(_)))
        || !get("cause").is_none_or(cause_holds)
        || activity.is_some_and(|activity| !ACTIVITY_OPERATIONS.contains(&operation.as_str()) || !activity_holds(activity))
    {
        return Err(Inadmissible);
    }
    Ok(event
        .fields()
        .iter()
        .filter(|(name, value)| name == "principal_id" || *value != Value::Null)
        .cloned()
        .collect())
}

/// A cause is exactly one canonical closed cause, member order included, so the log never holds anything else.
fn cause_holds(cause: &Value) -> bool {
    *cause == Value::Null || canonical_cause(cause).as_ref() == Some(cause)
}

fn principal_holds(class: Option<&Value>, id: Option<&Value>) -> bool {
    match (class, id) {
        (Some(Value::String(class)), Some(Value::Null)) => class == "absent",
        (Some(Value::String(class)), Some(Value::String(id))) => match class.as_str() {
            "human" => lower_hex(id, 32),
            "machine" => machine_id(id),
            _ => false,
        },
        _ => false,
    }
}

fn lower_hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// `^[a-z][a-z0-9-]{0,63}$`.
fn machine_id(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=64).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

/// `^[0-9a-z][0-9a-z-]{0,127}$`.
fn activity_text(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=128).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

/// An activity event's fields: a closed set with `event_id` and a phase, each a bounded lowercase identifier or a
/// non-negative integer below 2^31; its origin is `team` or one exact, bounded HTTP(S) Origin.
fn activity_holds(activity: &Value) -> bool {
    let Value::Object(members) = activity else {
        return false;
    };
    members.iter().any(|(name, _)| name == "event_id")
        && matches!(activity.get("phase"), Some(Value::String(phase)) if ACTIVITY_PHASES.contains(&phase.as_str()))
        && members.iter().all(|(name, value)| {
            ACTIVITY_KEYS.contains(&name.as_str())
                && if name == "origin" {
                    origin_holds(value)
                } else {
                    match value {
                        Value::String(text) => activity_text(text),
                        Value::Number(Number::Int(number)) => (0..1_i64 << 31).contains(number),
                        _ => false,
                    }
                }
        })
}

fn origin_holds(value: &Value) -> bool {
    match value {
        Value::String(origin) => {
            origin == "team"
                || (origin.chars().count() <= MAX_ACTIVITY_ORIGIN_CHARS
                    && canonical_origin(Some(origin)).as_deref() == Some(origin.as_str()))
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{AuditEvent, Inadmissible, admit};

    fn json(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("json")
    }

    fn failed_action(cause: Value) -> AuditEvent {
        let text = |value: &str| Value::String(value.to_owned());
        AuditEvent::new()
            .with("operation", text("assistant-action"))
            .with("result", text("error"))
            .with("principal_id", text("team-local"))
            .with("principal_class", text("machine"))
            .with("team_id", text("neo"))
            .with("detail", text("failed:search-web"))
            .with("cause", cause)
    }

    #[test]
    fn a_failed_action_record_carries_exactly_one_canonical_closed_cause() {
        let cause = json(
            r#"{"kind":"assistant-exit","condition":"exit-status","phase":"exit-inspection","exit_code":1,"stderr_bytes":40,"cleanup":"workload-stopped"}"#,
        );
        assert!(admit(&failed_action(cause)).is_ok());
        assert!(admit(&failed_action(Value::Null)).is_ok());
        for refused in [
            // Out of canonical order: the log holds only the one canonical spelling.
            r#"{"condition":"deadline-passed","kind":"assistant-timeout","phase":"exchange"}"#,
            // Child output, a member name an Assistant chose, and an unknown word never reach the log.
            r#"{"kind":"assistant-exit","condition":"stderr-output","phase":"exit-inspection","stderr_bytes":5,"stderr":"sk-live-123"}"#,
            r#"{"kind":"assistant-output-invalid","condition":"result-schema-mismatch","phase":"result-validation","member":"password"}"#,
            r#"{"kind":"assistant-timeout","condition":"Bearer sk-live-123","phase":"exchange"}"#,
            r#""assistant-timeout""#,
        ] {
            assert_eq!(
                admit(&failed_action(json(refused))),
                Err(Inadmissible),
                "{refused}"
            );
        }
    }

    #[test]
    fn an_activity_event_never_carries_a_cause() {
        let text = |value: &str| Value::String(value.to_owned());
        let activity = AuditEvent::new()
            .with("operation", text("memory-forget"))
            .with("result", text("ok"))
            .with("principal_id", text("team-local"))
            .with("principal_class", text("machine"))
            .with("activity", json(r#"{"event_id":"e1","phase":"completed"}"#))
            .with(
                "cause",
                json(r#"{"kind":"stop","condition":"stopped","phase":"exchange"}"#),
            );
        assert_eq!(admit(&activity), Err(Inadmissible));
    }
}
