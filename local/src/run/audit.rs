//! The audit the Action executor records through: its events name no principal of their own, so each carries the
//! principal and trace of the request it runs for, or Team's own when it runs on no request (a Routine run, or a chat
//! turn that outlives its request).

use std::sync::Arc;

use strict_json::Value;
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};
use team_core::future::BoxFuture;

use crate::audit::Principal;
use crate::http::server::current_request;

/// An audit sink that attributes each event without a principal.
pub struct Attributed(pub Arc<dyn AuditSink>);

impl std::fmt::Debug for Attributed {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Attributed")
    }
}

/// `event` under the current request's principal and trace, or under Team's own principal.
fn attributed(event: AuditEvent) -> AuditEvent {
    if event
        .fields()
        .iter()
        .any(|(name, _)| name == "principal_class")
    {
        return event;
    }
    let text = |value: &str| Value::String(value.to_owned());
    let Some(context) = current_request() else {
        let team = Principal::team();
        return event
            .with("principal_id", team.id.as_deref().map_or(Value::Null, text))
            .with("principal_class", text(team.class.as_str()));
    };
    event
        .with("trace_id", text(&context.trace_id))
        .with(
            "principal_id",
            context.principal_id.as_deref().map_or(Value::Null, text),
        )
        .with("principal_class", text(context.principal_class))
        .with("credential_state", text(context.credential_state))
}

impl AuditSink for Attributed {
    fn record(&self, event: AuditEvent) -> BoxFuture<'_, Result<(), AuditUnavailable>> {
        self.0.record(attributed(event))
    }
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_core::audit::AuditEvent;

    use super::attributed;

    fn field<'a>(event: &'a AuditEvent, name: &str) -> Option<&'a Value> {
        event
            .fields()
            .iter()
            .find(|(field, _)| field == name)
            .map(|(_, value)| value)
    }

    #[test]
    fn an_event_on_no_request_is_attributed_to_team() {
        let event = attributed(
            AuditEvent::new().with("operation", Value::String("assistant-action".to_owned())),
        );
        assert_eq!(
            field(&event, "principal_id"),
            Some(&Value::String("team-local".to_owned()))
        );
        assert_eq!(
            field(&event, "principal_class"),
            Some(&Value::String("machine".to_owned()))
        );
    }

    #[test]
    fn an_event_that_names_its_principal_is_kept() {
        let event = AuditEvent::new()
            .with("principal_id", Value::Null)
            .with("principal_class", Value::String("absent".to_owned()));
        let kept = attributed(event.clone());
        assert_eq!(kept.fields(), event.fields());
    }
}
