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

    /// No file is ever selected by the runner test.
    struct NoFiles;

    impl team_action::files::FileSource for NoFiles {
        fn read(
            &self,
            _team: &str,
            _file: &str,
        ) -> Result<team_action::files::Stored, team_action::files::FileUnavailable> {
            Err(team_action::files::FileUnavailable)
        }
    }

    /// One running `search-web` Action of the Exa Assistant.
    struct Running;

    impl team_action::runner::Workloads for Running {
        fn binding<'a>(
            &'a self,
            _team: &'a str,
            _assistant: &'a str,
        ) -> team_core::future::BoxFuture<
            'a,
            Result<team_action::runner::Binding, team_action::executor::ActionFailure>,
        > {
            let action = team_assistant::spec::ActionSpec::of(
                &strict_json::parse(
                    br#"{"id":"search-web","input_schema":{"type":"object"},"output_schema":{"type":"object"}}"#,
                )
                .expect("an Action"),
            );
            let contract = team_assistant::spec::RuntimeContract {
                actions: vec![action],
                allowed_hosts: Vec::new(),
                integrations: Vec::new(),
                stored_inputs: Vec::new(),
                machine_contract: strict_json::parse(br#"{"messages":[]}"#).expect("a contract"),
                presentation: team_assistant::manifest::ManifestPresentation {
                    description: String::new(),
                    links: Value::Object(Vec::new()),
                },
            };
            Box::pin(async move {
                Ok(team_action::runner::Binding {
                    contract: Arc::new(contract),
                    assistant_id: "shimpz-exa".to_owned(),
                    name: "Exa".to_owned(),
                    version: "1.0.0".to_owned(),
                    summary: "Exa".to_owned(),
                    container: "container".to_owned(),
                    image: "sha256:image".to_owned(),
                })
            })
        }

        fn egress(
            &self,
            _team: &str,
            _assistant: &str,
        ) -> Result<
            Option<(team_core::secret::Secret, Vec<String>)>,
            team_action::runner::EgressUnavailable,
        > {
            Ok(None)
        }
    }

    /// The executor's private stores and its OAuth service, empty under `root`.
    fn stores(
        root: &std::path::Path,
        entropy: &Arc<CountingEntropy>,
    ) -> (
        Arc<team_integrations::store::IntegrationStore>,
        team_action::stored_input::StoredInputStore,
        Arc<team_integrations::service::OAuthService>,
    ) {
        use team_integrations::pkce::{PkceLimits, PkceStore};
        let clock: Arc<dyn team_core::clock::Clock> = Arc::new(FakeClock::at(
            UNIX_EPOCH + Duration::from_secs(1_700_000_000),
        ));
        let integrations = Arc::new(
            team_integrations::store::IntegrationStore::new(
                root.join("i/state/i.json"),
                root.join("i/key/k"),
                Arc::clone(&clock),
                entropy.clone(),
            )
            .expect("an Integration store"),
        );
        let stored_inputs = team_action::stored_input::StoredInputStore::new(
            root.join("s/state/s.json"),
            root.join("s/key/k"),
            clock,
            entropy.clone(),
        )
        .expect("a Stored Input store");
        let pkce = Arc::new(PkceStore::new(PkceLimits::default(), entropy.clone()).expect("PKCE"));
        let broker = team_integrations::broker::BrokerClient::new(Box::new(
            team_integrations::broker::FixedTransport::local(),
        ));
        let oauth = Arc::new(team_integrations::service::OAuthService::new(
            pkce,
            Arc::clone(&integrations),
            Arc::new(broker),
        ));
        (integrations, stored_inputs, oauth)
    }

    /// The real executor, recording through `audit` the way the Local controller composes it.
    fn runner(
        root: &std::path::Path,
        audit: Arc<dyn AuditSink>,
        engine: Arc<team_core::testing::FakeExec>,
    ) -> team_action::runner::Executor {
        let entropy = Arc::new(CountingEntropy::default());
        let (integrations, stored_inputs, oauth) = stores(root, &entropy);
        let journal = team_action::journal::Journal::open(
            &root.join("journal/journal.sqlite3"),
            team_action::journal::Limits::default(),
            entropy.clone(),
        )
        .expect("a journal");
        team_action::runner::Executor(Arc::new(team_action::runner::Runner {
            journal: Arc::new(journal),
            stored_inputs: Arc::new(stored_inputs),
            integrations,
            oauth,
            engine,
            slots: Arc::new(team_action::rpc::DockerSlots::default()),
            capacity: Capacity::default(),
            transport: Arc::new(Transport::local()),
            audit: Arc::new(super::Attributed(audit)),
            workloads: Arc::new(Running),
            entropy,
            blocked: std::sync::Mutex::default(),
            files: Arc::new(NoFiles),
            file_slot: team_action::files::FileSlot::default(),
        }))
    }

    /// Each written line's operation, result, detail, and cause (its members written sorted, read in any order).
    fn failures(
        path: &std::path::Path,
    ) -> Vec<(String, String, String, Option<team_protocol::cause::Cause>)> {
        let content = std::fs::read(path).expect("the log");
        content
            .split_inclusive(|byte| *byte == b'\n')
            .map(|line| {
                let event = strict_json::parse(line).expect("a JSON line");
                let text = |name: &str| match event.get(name) {
                    Some(Value::String(value)) => value.clone(),
                    _ => String::new(),
                };
                (
                    text("operation"),
                    text("result"),
                    text("detail"),
                    event.get("cause").map(|cause| {
                        team_protocol::cause::Cause::from_value(cause).expect("a cause")
                    }),
                )
            })
            .collect()
    }

    /// Scripts one workload that reads its request, writes `stderr` to stderr, and exits 1.
    fn failing(
        engine: &team_core::testing::FakeExec,
        stderr: &'static [u8],
    ) -> tokio::task::JoinHandle<()> {
        use team_core::testing::ScriptedExec;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let (ours, mut workload) = tokio::io::duplex(64 * 1024);
        engine.push(ScriptedExec {
            stream: ours,
            exit: team_core::exec::ExecExit {
                running: false,
                exit_code: Some(1),
            },
        });
        tokio::spawn(async move {
            let mut byte = [0_u8; 1];
            while workload.read(&mut byte).await.is_ok_and(|read| read == 1) && byte[0] != b'\n' {}
            let mut output = vec![2, 0, 0, 0];
            output.extend_from_slice(&u32::try_from(stderr.len()).expect("small").to_be_bytes());
            output.extend_from_slice(stderr);
            let _ = workload.write_all(&output).await;
            let _ = workload.shutdown().await;
        })
    }

    /// The executor's own records of a failed Action, its exact cause included, are admitted and written by the real
    /// Local log, and nothing of the workload's stderr reaches it.
    #[tokio::test]
    async fn the_local_log_records_a_failed_action_with_its_exact_cause() {
        use team_action::executor::{ActionError, ActionExecutor, ActionFailure, ActionRequest};
        use team_protocol::cause::{Cause, Cleanup};

        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("audit.jsonl");
        let log = log(&path);
        let engine = Arc::new(team_core::testing::FakeExec::default());
        let stderr = b"Traceback: sk-live-Local-4d1f";
        let worker = failing(&engine, stderr);
        let executor = runner(
            directory.path(),
            Arc::clone(&log) as Arc<dyn AuditSink>,
            Arc::clone(&engine),
        );
        let request = ActionRequest {
            interrupt_id: String::new(),
            assistant_id: "shimpz-exa".to_owned(),
            action: "search-web".to_owned(),
            input: Value::Object(Vec::new()),
            files: Vec::new(),
        };
        let outcome = executor
            .invoke("neo", &request, &team_core::stop::Stop::new())
            .await;
        drop(worker.await);
        let cause = Cause::exit_status(1)
            .with_stderr(stderr.len())
            .with_cleanup(Cleanup::WorkloadStopped);
        assert_eq!(
            outcome,
            Err(ActionError::new(ActionFailure::Uncertain, cause))
        );
        log.close().expect("closed");
        let written = failures(&path);
        let action = |result: &str, detail: &str, cause: Option<Cause>| {
            (
                "assistant-action".to_owned(),
                result.to_owned(),
                detail.to_owned(),
                cause,
            )
        };
        assert_eq!(
            written,
            [
                action("ok", "started:search-web", None),
                action("error", "failed:search-web", Some(cause)),
            ]
        );
        let content = String::from_utf8(std::fs::read(&path).expect("the log")).expect("UTF-8");
        assert!(!content.contains("sk-live") && !content.contains("Traceback"));
    }
}
