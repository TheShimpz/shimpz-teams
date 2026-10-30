//! What one audit line may say: its principal, operation, result, and a closed set of metadata fields, never a body,
//! a secret, or provider data.
//!
//! An event reaches the writer as an [`AuditEvent`] whose fields this module admits: `operation`, `result`,
//! `principal_id`, and `principal_class` always; `trace_id` and `credential_state` from the principal; `team_id`,
//! `assistant`, `detail`, and `model_usage` for an ordinary event; `team_id` and `activity` for an activity event
//! (ADR-0120). Anything else refuses the event, so no caller can put a body or a credential into the log.

use strict_json::{Number, Value};
use team_core::audit::AuditEvent;
use team_protocol::websocket::canonical_origin;

/// An audit line's result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// The operation succeeded.
    Ok,
    /// It was refused.
    Denied,
    /// It failed.
    Error,
}

impl Outcome {
    /// Its wire text.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Denied => "denied",
            Self::Error => "error",
        }
    }
}

/// Who an audit line attributes the operation to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrincipalClass {
    /// Nobody was verified.
    Absent,
    /// A verified Supervisor, by their opaque id.
    Human,
    /// A machine principal, such as Team itself.
    Machine,
}

impl PrincipalClass {
    /// Its wire text.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Absent => "absent",
            Self::Human => "human",
            Self::Machine => "machine",
        }
    }
}

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

/// The verified principal of one request or of Team's own work.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Principal {
    /// Its id: `None` exactly when absent; 32 hex digits for a human; a lowercase name for a machine.
    pub id: Option<String>,
    /// Its class.
    pub class: PrincipalClass,
    /// The shape of the credential it presented, one of [`CREDENTIAL_STATES`].
    pub credential_state: Option<&'static str>,
    /// The request's trace id (32 hex digits); the writer draws one when absent.
    pub trace_id: Option<String>,
}

impl Principal {
    /// Team itself, for the events it records on its own behalf: startup, background work, and its own failures.
    #[must_use]
    pub fn team() -> Self {
        Self {
            id: Some("team-local".to_owned()),
            class: PrincipalClass::Machine,
            credential_state: None,
            trace_id: None,
        }
    }

    /// An event of `operation` with `result` attributed to this principal; callers add its metadata fields.
    #[must_use]
    pub fn event(&self, operation: &str, result: Outcome) -> AuditEvent {
        let mut event = AuditEvent::new()
            .with("operation", text(operation))
            .with("result", text(result.as_str()))
            .with("principal_id", self.id.as_deref().map_or(Value::Null, text))
            .with("principal_class", text(self.class.as_str()));
        if let Some(state) = self.credential_state {
            event = event.with("credential_state", text(state));
        }
        if let Some(trace_id) = &self.trace_id {
            event = event.with("trace_id", text(trace_id));
        }
        event
    }
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// The event is not admissible; it is never written.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Inadmissible;

/// The admitted fields of one event, before the writer adds `ts`, `service`, `trace_id` (when drawn), and
/// `prev_sha256`. A field whose value is `null` is left out, except `principal_id`.
pub(super) fn admit(event: &AuditEvent) -> Result<Vec<(String, Value)>, Inadmissible> {
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
        &["team_id", "assistant", "detail", "model_usage"]
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
