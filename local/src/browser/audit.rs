//! The Browser's audit lines: metadata only, never a URL path, page content, input, or credential.
//!
//! Each event names its operation, result, actor, Team, and either a closed `detail` or an ADR-0120 `activity`; the
//! audit writer adds the time, the service, and the chain. A request's events carry the verified principal that made
//! it; what Team does on its own (a background cleanup, an expiry) carries Team itself.

use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::audit::{AuditSink, AuditUnavailable};

use crate::audit::{Outcome, PrincipalClass};
use crate::http::service::Principal;

/// Who an audit event names as its actor: the verified principal of a request, or Team itself.
pub type Actor = crate::audit::Principal;

/// The verified principal of one request, as its audit lines name it.
#[must_use]
pub fn request_actor(principal: &Principal, trace_id: &str) -> Actor {
    let (id, class, credential_state) = match principal {
        Principal::Machine => (
            Some("admin".to_owned()),
            PrincipalClass::Machine,
            "machine_bearer_present",
        ),
        Principal::Routine { .. } => (
            Some("admin-routine".to_owned()),
            PrincipalClass::Machine,
            "assertion_present",
        ),
        Principal::Supervisor { supervisor_id, .. } => (
            Some(supervisor_id.clone()),
            PrincipalClass::Human,
            "assertion_present",
        ),
        Principal::HostReset => (None, PrincipalClass::Absent, "assertion_present"),
    };
    Actor {
        id,
        class,
        credential_state: Some(credential_state),
        trace_id: Some(trace_id.to_owned()),
    }
}

/// The verified person an actor is, as an activity event's initiator; empty when no person is.
#[must_use]
pub fn human(actor: &Actor) -> String {
    if actor.class == PrincipalClass::Human {
        actor.id.clone().unwrap_or_default()
    } else {
        String::new()
    }
}

fn outcome(result: &str) -> Outcome {
    match result {
        "ok" => Outcome::Ok,
        "denied" => Outcome::Denied,
        _ => Outcome::Error,
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
            .event(operation, outcome(result))
            .with("team_id", Value::String(team_id.to_owned()))
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
            .event(operation, outcome(result))
            .with("team_id", Value::String(team_id.to_owned()))
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
