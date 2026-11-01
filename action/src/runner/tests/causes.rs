//! Each failure's exact cause, injected at the runner: Team's own refused audit records, an unconfirmed fail-stop, a
//! secret echo, a refusal before dispatch, and a journal that cannot record an attempt's end.

use std::sync::{Arc, Mutex};

use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, FakeExec, MemoryAudit, ScriptedExec};
use team_protocol::payload::cause::{Cause, Cleanup, Condition, Phase};
use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

use super::super::{Executor, Runner};
use super::{Fixed, STORED, World, at, because, binding, connected, request, scripted, world};
use crate::executor::{ActionError, ActionExecutor, ActionFailure};
use crate::frames::tests::frame;
use crate::provider::Capacity;
use crate::rpc::DockerSlots;

/// An audit that refuses, as inadmissible, every record of one operation (or of one detail prefix) and keeps the rest
/// in the real admission's in-memory audit: the way a record the Local log refuses stops what it was for.
struct Refusing {
    inner: Arc<MemoryAudit>,
    operation: &'static str,
    detail: Option<&'static str>,
}

impl team_core::audit::AuditSink for Refusing {
    fn record(
        &self,
        event: team_core::audit::AuditEvent,
    ) -> BoxFuture<'_, Result<(), team_core::audit::AuditUnavailable>> {
        let text = |name: &str| {
            event
                .fields()
                .iter()
                .find(|(field, _)| field == name)
                .and_then(|(_, value)| match value {
                    Value::String(text) => Some(text.clone()),
                    _ => None,
                })
                .unwrap_or_default()
        };
        let refused = text("operation") == self.operation
            && self
                .detail
                .is_none_or(|prefix| text("detail").starts_with(prefix));
        if refused {
            return Box::pin(async { Err(team_core::audit::AuditUnavailable::Refused) });
        }
        self.inner.record(event)
    }
}

/// `world`'s runner recording through `audit`.
fn recording_through(world: &World, audit: Arc<dyn team_core::audit::AuditSink>) -> Arc<Runner> {
    let runner = &world.runner;
    Arc::new(Runner {
        journal: Arc::clone(&runner.journal),
        stored_inputs: Arc::clone(&runner.stored_inputs),
        integrations: Arc::clone(&runner.integrations),
        oauth: Arc::clone(&runner.oauth),
        engine: world.engine.clone(),
        slots: Arc::new(DockerSlots::default()),
        capacity: Capacity::default(),
        transport: Arc::clone(&runner.transport),
        audit,
        workloads: Arc::new(Fixed(binding())),
        entropy: Arc::new(CountingEntropy::default()),
        blocked: Mutex::default(),
        files: Arc::clone(&runner.files),
        file_slot: crate::files::FileSlot::default(),
    })
}

/// Each `assistant-action` record's detail and cause, as the audit received them.
fn action_records(audit: &MemoryAudit) -> Vec<(String, Option<Value>)> {
    audit
        .events()
        .iter()
        .filter(|event| {
            event.fields().iter().any(|(name, value)| {
                name == "operation" && *value == Value::String("assistant-action".to_owned())
            })
        })
        .map(|event| {
            let field = |name: &str| {
                event
                    .fields()
                    .iter()
                    .find(|(field, _)| field == name)
                    .map(|(_, value)| value.clone())
            };
            let detail = match field("detail") {
                Some(Value::String(detail)) => detail,
                _ => String::new(),
            };
            (detail, field("cause"))
        })
        .collect()
}

/// When Team's own dispatch record is refused, nothing is sent and the failure names the refused record, never a
/// transport or Docker failure; the refusal is not recorded again.
#[tokio::test]
async fn a_refused_dispatch_record_is_named_and_nothing_is_sent() {
    let world = world();
    let refusing = Arc::new(Refusing {
        inner: world.audit.clone(),
        operation: "assistant-action",
        detail: Some("started:"),
    });
    let runner = recording_through(&world, refusing);
    let outcome = Executor(runner)
        .invoke("team_1", &request("i-1", "lookup"), &Stop::new())
        .await;
    assert_eq!(
        outcome,
        Err(because(
            ActionFailure::StateUnavailable,
            Condition::RecordRefused,
            Phase::DispatchRecord
        ))
    );
    assert_eq!(world.engine.started().len(), 0);
    assert_eq!(action_records(&world.audit), []);
}

/// Scripts one exec that reads its request line, makes one provider call, and then waits for its reply.
fn calling(engine: &FakeExec) -> tokio::task::JoinHandle<()> {
    let (ours, mut workload) = duplex(64 * 1024);
    engine.push(ScriptedExec {
        stream: ours,
        exit: ExecExit {
            running: false,
            exit_code: Some(0),
        },
    });
    tokio::spawn(async move {
        let mut byte = [0_u8; 1];
        while workload.read(&mut byte).await.is_ok_and(|read| read == 1) && byte[0] != b'\n' {}
        let call =
            br#"{"type":"fetch","method":"GET","url":"https://api.example.com/v1","headers":[]}"#;
        let _ = workload
            .write_all(&frame(1, &[call.as_slice(), b"\n"].concat()))
            .await;
        let mut rest = Vec::new();
        let _ = workload.read_to_end(&mut rest).await;
    })
}

/// The provider-call failure the Local log once caused: every provider-call record refused. The attempt ends naming
/// the refused record in the provider-call phase and the workload's confirmed stop, and that cause reaches the audit.
#[tokio::test]
async fn a_refused_provider_call_record_reaches_the_failure_and_the_audit_without_collapsing() {
    let world = world();
    let refusing = Arc::new(Refusing {
        inner: world.audit.clone(),
        operation: "assistant-provider-call",
        detail: None,
    });
    let runner = recording_through(&world, refusing);
    let workload = calling(&world.engine);
    let outcome = Executor(runner)
        .invoke("team_1", &request("i-1", "lookup"), &Stop::new())
        .await;
    drop(workload.await);
    let cause = Cause::new(Condition::RecordRefused, Phase::ProviderCall)
        .with_cleanup(Cleanup::WorkloadStopped);
    assert_eq!(
        outcome,
        Err(ActionError::new(ActionFailure::Uncertain, cause))
    );
    assert_eq!(world.engine.stopped(), ["container"]);
    assert_eq!(
        action_records(&world.audit),
        [
            ("started:lookup".to_owned(), None),
            ("failed:lookup".to_owned(), Some(cause.to_value())),
        ]
    );
}

/// A failure whose workload cannot be proven stopped keeps its initiating cause and says the stop is unconfirmed.
#[tokio::test]
async fn an_unconfirmed_fail_stop_keeps_the_initiating_cause() {
    let world = world();
    world.engine.block_fail_stop();
    let _sent = scripted(&world.engine, r#"{"type":"result","result":{}}"#, 2);
    let outcome = Executor(Arc::clone(&world.runner))
        .invoke("team_1", &request("i-1", "lookup"), &Stop::new())
        .await;
    let cause = Cause::exit_status(2).with_cleanup(Cleanup::WorkloadStopUnconfirmed);
    assert_eq!(
        outcome,
        Err(ActionError::new(ActionFailure::Blocked, cause))
    );
    let blocked = Executor(Arc::clone(&world.runner))
        .invoke("team_1", &request("i-2", "lookup"), &Stop::new())
        .await;
    assert_eq!(blocked, Err(at(ActionFailure::Blocked, Phase::Admission)));
}

/// A result echoing an injected value is refused as an echo; neither the value nor the result reaches the audit,
/// which records only the closed cause.
#[tokio::test]
async fn a_secret_echo_is_named_without_the_secret_reaching_the_audit() {
    let world = world();
    connected(&world);
    let echo = format!(r#"{{"type":"result","result":{{"leak":"{STORED}"}}}}"#);
    let _sent = scripted(&world.engine, &echo, 0);
    let outcome = Executor(Arc::clone(&world.runner))
        .invoke("team_1", &request("i-1", "lookup"), &Stop::new())
        .await;
    let cause = Cause::new(Condition::InjectedValueEchoed, Phase::ResultValidation);
    assert_eq!(
        outcome,
        Err(ActionError::new(ActionFailure::Uncertain, cause))
    );
    let recorded = format!("{:?}", world.audit.events());
    assert!(!recorded.contains(STORED) && !recorded.contains("leak"));
    assert_eq!(
        action_records(&world.audit).last(),
        Some(&("invalid-output:lookup".to_owned(), Some(cause.to_value())))
    );
}

/// A refusal before dispatch is recorded with its cause, so Team's record states it too.
#[tokio::test]
async fn a_refusal_before_dispatch_is_recorded_with_its_cause() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let sync = request("i-1", "sync");
    let refused = executor.invoke("team_1", &sync, &Stop::new()).await;
    let unavailable = at(ActionFailure::IntegrationUnavailable, Phase::Preparation);
    assert_eq!(refused, Err(unavailable));
    assert_eq!(
        action_records(&world.audit),
        [(
            "not-dispatched:sync".to_owned(),
            unavailable.cause.map(|cause| cause.to_value())
        )]
    );
}

/// A journal that cannot record an attempt's end becomes the cause's cleanup, except over an unconfirmed stop.
#[test]
fn a_journal_cleanup_never_hides_an_unconfirmed_stop() {
    let unconfirmed = Cause::exit_status(1).with_cleanup(Cleanup::WorkloadStopUnconfirmed);
    assert_eq!(crate::executor::journal_cleanup(unconfirmed), unconfirmed);
    assert_eq!(
        crate::executor::journal_cleanup(
            Cause::exit_status(1).with_cleanup(Cleanup::WorkloadStopped)
        ),
        Cause::exit_status(1).with_cleanup(Cleanup::JournalUnavailable)
    );
}
