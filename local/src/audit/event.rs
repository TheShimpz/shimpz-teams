//! Who an audit line attributes its operation to, and its result; what a line may say is the closed admission of
//! [`team_core::audit::admit`], which the writer applies to every event.

use strict_json::Value;
use team_core::audit::AuditEvent;
#[cfg(doc)]
use team_core::audit::CREDENTIAL_STATES;

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
