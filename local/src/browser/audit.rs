//! The Browser's audit lines: metadata only, never a URL path, page content, input, or credential.
//!
//! Each event names its operation, result, actor, Team, and either a closed `detail` or an ADR-0120 `activity`; the
//! audit writer adds the time, the service, and the chain. A request's events carry the verified principal that made
//! it; what Team does on its own (a background cleanup, an expiry) carries Team itself.

use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};

use crate::http::service::Principal;

/// Who an audit event names as its actor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Actor {
    /// The principal's id (`admin`, `admin-routine`, `team-local`, or a Supervisor's opaque id).
    pub principal_id: Option<String>,
    /// `human`, `machine`, or `absent`.
    pub principal_class: &'static str,
    /// How the request proved it, for a request's principal.
    pub credential_state: Option<&'static str>,
    /// The request's trace id, when the event belongs to one.
    pub trace_id: Option<String>,
}

impl Actor {
    /// Team itself, for what it records on its own behalf.
    #[must_use]
    pub fn team() -> Self {
        Self {
            principal_id: Some("team-local".to_owned()),
            principal_class: "machine",
            credential_state: None,
            trace_id: None,
        }
    }

    /// The verified principal of one request.
    #[must_use]
    pub fn request(principal: &Principal, trace_id: &str) -> Self {
        let (principal_id, principal_class, credential_state) = match principal {
            Principal::Machine => (Some("admin"), "machine", "machine_bearer_present"),
            Principal::Routine { .. } => (Some("admin-routine"), "machine", "assertion_present"),
            // The Supervisor's opaque id is not part of the W0 principal yet (reported as an interface change).
            Principal::Supervisor { .. } | Principal::HostReset => {
                (None, "human", "assertion_present")
            }
        };
        Self {
            principal_id: principal_id.map(str::to_owned),
            principal_class,
            credential_state: Some(credential_state),
            trace_id: Some(trace_id.to_owned()),
        }
    }

    /// The verified human who made the request, as an activity event's initiator; empty when no human did.
    #[must_use]
    pub fn human(&self) -> String {
        if self.principal_class == "human" {
            self.principal_id.clone().unwrap_or_default()
        } else {
            String::new()
        }
    }

    fn event(&self, operation: &str, result: &str, team_id: &str) -> AuditEvent {
        let text = |value: &str| Value::String(value.to_owned());
        let mut event = AuditEvent::new()
            .with("operation", text(operation))
            .with("result", text(result))
            .with(
                "principal_id",
                self.principal_id.as_deref().map_or(Value::Null, text),
            )
            .with("principal_class", text(self.principal_class))
            .with("team_id", text(team_id));
        if let Some(state) = self.credential_state {
            event = event.with("credential_state", text(state));
        }
        if let Some(trace_id) = &self.trace_id {
            event = event.with("trace_id", text(trace_id));
        }
        event
    }
}

/// One activity field's value: a closed word or a count.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Field {
    /// A bounded lowercase identifier, or an Origin.
    Text(String),
    /// A non-negative count.
    Count(i64),
}

/// Records one Browser event with a closed `detail`.
///
/// # Errors
/// [`AuditUnavailable`] when it was not recorded.
pub async fn record(
    sink: &dyn AuditSink,
    actor: &Actor,
    operation: &str,
    result: &str,
    team_id: &str,
    detail: &str,
) -> Result<(), AuditUnavailable> {
    sink.record(
        actor
            .event(operation, result, team_id)
            .with("detail", Value::String(detail.to_owned())),
    )
    .await
}

/// Records one activity event (ADR-0120) of a Team's Sign-in.
///
/// # Errors
/// [`AuditUnavailable`] when it was not recorded.
pub async fn activity(
    sink: &dyn AuditSink,
    actor: &Actor,
    operation: &str,
    result: &str,
    team_id: &str,
    fields: &[(&str, Field)],
) -> Result<(), AuditUnavailable> {
    let members = fields
        .iter()
        .map(|(name, field)| {
            let value = match field {
                Field::Text(text) => Value::String(text.clone()),
                Field::Count(count) => Value::Number(Number::Int(*count)),
            };
            ((*name).to_owned(), value)
        })
        .collect();
    sink.record(
        actor
            .event(operation, result, team_id)
            .with("activity", Value::Object(members)),
    )
    .await
}

/// A view's or a session's line writer: one operation, actor, and Team, a closed state between a fixed prefix and
/// suffix (`view:` … `:container=<12 hex>`).
#[derive(Clone)]
pub struct Lines {
    sink: Arc<dyn AuditSink>,
    actor: Actor,
    operation: &'static str,
    team_id: String,
    prefix: &'static str,
    suffix: String,
}

impl std::fmt::Debug for Lines {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Lines")
            .field("operation", &self.operation)
            .finish_non_exhaustive()
    }
}

impl Lines {
    /// Lines of `operation` for `team_id`, each `prefix` + state + `suffix`.
    #[must_use]
    pub fn new(
        sink: Arc<dyn AuditSink>,
        actor: Actor,
        operation: &'static str,
        team_id: &str,
        prefix: &'static str,
        suffix: String,
    ) -> Self {
        Self {
            sink,
            actor,
            operation,
            team_id: team_id.to_owned(),
            prefix,
            suffix,
        }
    }

    /// Records one state.
    ///
    /// # Errors
    /// [`AuditUnavailable`] when it was not recorded.
    pub async fn record(&self, state: &str) -> Result<(), AuditUnavailable> {
        let detail = format!("{}{state}{}", self.prefix, self.suffix);
        record(
            self.sink.as_ref(),
            &self.actor,
            self.operation,
            "ok",
            &self.team_id,
            &detail,
        )
        .await
    }
}

/// The first 12 characters of a Docker id, as audit details name containers.
#[must_use]
pub fn short(id: &str) -> &str {
    id.get(..12).unwrap_or(id)
}
