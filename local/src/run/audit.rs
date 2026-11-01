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
    use std::sync::Arc;
    use std::time::{Duration, UNIX_EPOCH};

    use strict_json::Value;
    use team_action::frames::CallBroker;
    use team_action::provider::{Attempt, Broker, CallScope, Capacity, Transport};
    use team_core::audit::{AuditEvent, AuditSink};
    use team_core::testing::{CountingEntropy, FakeClock};
    use team_integrations::https::{Tunnel, native_roots};

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

    /// The Assistant egress grant of the broker test: one reviewed host, behind an egress nobody listens on.
    struct Declared;

    impl team_action::provider::EgressRoute for Declared {
        fn route(
            &self,
        ) -> Result<Option<(team_core::secret::Secret, Vec<String>)>, team_action::provider::Refusal>
        {
            Ok(Some((
                team_core::secret::Secret::new("policy-token".to_owned()),
                vec!["api.example.com".to_owned()],
            )))
        }
    }

    const OPERATION: &str = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6";

    fn provider_call(url: &str) -> Value {
        let frame = format!(
            r#"{{"type":"fetch","method":"POST","url":"{url}","headers":[],"body":"e30="}}"#
        );
        strict_json::parse(frame.as_bytes()).expect("a call frame")
    }

    /// The Local log at `path`, under a fixed clock.
    fn log(path: &std::path::Path) -> Arc<crate::audit::AuditLog> {
        Arc::new(crate::audit::AuditLog::new(
            path.to_owned(),
            crate::audit::Settings {
                max_bytes: crate::audit::MAX_BYTES,
                group_commit: Duration::from_secs(60),
            },
            Arc::new(FakeClock::at(
                UNIX_EPOCH + Duration::from_secs(1_700_000_000),
            )),
            Arc::new(CountingEntropy::default()),
        ))
    }

    /// A broker of one `search-web` attempt that records through `audit` the way the executor does.
    fn broker(audit: Arc<dyn AuditSink>) -> Broker {
        Broker::new(CallScope {
            attempt: Attempt {
                team_id: "neo".to_owned(),
                assistant_id: "shimpz-exa".to_owned(),
                action_id: "search-web".to_owned(),
                operation_id: OPERATION.to_owned(),
            },
            credentials: Vec::new(),
            missing: std::collections::BTreeSet::new(),
            authorized: true,
            route: Box::new(Declared),
            audit: Arc::new(super::Attributed(audit)),
            stop: team_core::stop::Stop::new(),
            capacity: Capacity::default(),
            transport: Arc::new(Transport::new(Tunnel::new(
                "127.0.0.1".to_owned(),
                1,
                Duration::from_secs(1),
                native_roots(),
            ))),
        })
    }

    /// Each written line's operation, Team, Assistant, result, and detail.
    fn recorded(path: &std::path::Path) -> Vec<[String; 5]> {
        let content = std::fs::read(path).expect("the log");
        content
            .split_inclusive(|byte| *byte == b'\n')
            .map(|line| {
                let event = strict_json::parse(line).expect("a JSON line");
                ["operation", "team_id", "assistant", "result", "detail"].map(|name| {
                    match event.get(name) {
                        Some(Value::String(value)) => value.clone(),
                        _ => String::new(),
                    }
                })
            })
            .collect()
    }

    fn line(result: &str, metadata: &str) -> [String; 5] {
        [
            "assistant-provider-call",
            "neo",
            "shimpz-exa",
            result,
            &format!("search-web:{OPERATION}:{metadata}"),
        ]
        .map(str::to_owned)
    }

    /// The provider broker's dispatch, outcome, and refusal records are admitted by the Local log the executor
    /// records through, so a Team-made provider call is answered instead of failing its attempt as a transport fault.
    #[tokio::test]
    async fn the_local_log_records_every_provider_call_phase() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("audit.jsonl");
        let log = log(&path);
        let mut broker = broker(Arc::clone(&log) as Arc<dyn AuditSink>);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        let sent = broker
            .answer(provider_call("https://api.example.com/search"), deadline)
            .await;
        assert_eq!(sent, Ok(br#"{"error":"unavailable"}"#.to_vec()));
        broker.release();
        let refused = broker
            .answer(
                provider_call("https://elsewhere.example.com/search"),
                deadline,
            )
            .await;
        assert_eq!(refused, Ok(br#"{"error":"refused"}"#.to_vec()));
        log.close().expect("closed");
        assert_eq!(
            recorded(&path),
            [
                line(
                    "ok",
                    r#"{"call":1,"credentials":[],"host":"api.example.com","method":"POST","phase":"dispatch"}"#
                ),
                line(
                    "error",
                    r#"{"call":1,"error":"unavailable","phase":"outcome","reason":"connect"}"#
                ),
                line(
                    "denied",
                    r#"{"call":2,"error":"refused","phase":"refused","reason":"host"}"#
                ),
            ]
        );
    }
}
