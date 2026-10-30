use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_assistant::manifest::{IntegrationDeclaration, ManifestPresentation};
use team_assistant::spec::{ActionSpec, RuntimeContract};
use team_core::clock::Clock;
use team_core::exec::ExecExit;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, FakeClock, FakeExec, MemoryAudit, ScriptedExec};
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::service::OAuthService;
use team_integrations::store::IntegrationStore;
use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

use super::{Binding, Executor, Runner, Workloads};
use crate::executor::{ActionExecutor, ActionFailure, ActionRequest, BatchScope, InvokeOutcome};
use crate::frames::tests::frame;
use crate::journal::{Journal, Limits};
use crate::provider::{Capacity, Transport};
use crate::rpc::DockerSlots;
use crate::stored_input::StoredInputStore;

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

struct Fixed(Binding);

impl Workloads for Fixed {
    fn binding<'a>(
        &'a self,
        _team: &'a str,
        assistant: &'a str,
    ) -> BoxFuture<'a, Result<Binding, ActionFailure>> {
        let found = (assistant == self.0.assistant_id)
            .then(|| self.0.clone())
            .ok_or(ActionFailure::ContextChanged);
        Box::pin(async move { found })
    }

    fn egress(
        &self,
        _team: &str,
        _assistant: &str,
    ) -> Result<Option<(Secret, Vec<String>)>, super::EgressUnavailable> {
        Ok(None)
    }
}

fn action(id: &str, effect: &str) -> ActionSpec {
    ActionSpec::of(&json(&format!(
        r#"{{"id":"{id}","input_schema":{{"type":"object"}},"output_schema":{{"type":"object"}},"stored_inputs":["api-key"],"effect":"{effect}"}}"#
    )))
}

fn binding() -> Binding {
    let contract = RuntimeContract {
        actions: vec![
            action("lookup", "read_only"),
            action("publish", "mutating"),
            ActionSpec::of(&json(
                r#"{"id":"sync","input_schema":{"type":"object"},"integrations":["cloudflare-write"]}"#,
            )),
        ],
        allowed_hosts: vec!["api.example.com".to_owned()],
        integrations: vec![IntegrationDeclaration {
            id: "cloudflare-write".to_owned(),
            provider: "cloudflare".to_owned(),
            scopes: vec!["zone.read".to_owned()],
        }],
        stored_inputs: Vec::new(),
        machine_contract: json(r#"{"messages":[]}"#),
        presentation: ManifestPresentation {
            description: String::new(),
            links: Value::Object(Vec::new()),
        },
    };
    Binding {
        contract: Arc::new(contract),
        assistant_id: "example".to_owned(),
        name: "Example".to_owned(),
        version: "1.0.0".to_owned(),
        summary: "Example".to_owned(),
        container: "container".to_owned(),
        image: "sha256:image".to_owned(),
    }
}

struct World {
    _root: tempfile::TempDir,
    engine: Arc<FakeExec>,
    runner: Arc<Runner>,
}

fn oauth(integrations: &Arc<IntegrationStore>, entropy: Arc<CountingEntropy>) -> Arc<OAuthService> {
    let pkce = Arc::new(PkceStore::new(PkceLimits::default(), entropy).expect("pkce"));
    let transport = Box::new(team_integrations::broker::FixedTransport::local());
    let broker = team_integrations::broker::BrokerClient::new(transport);
    Arc::new(OAuthService::new(
        pkce,
        Arc::clone(integrations),
        Arc::new(broker),
    ))
}

fn stores(
    root: &std::path::Path,
    entropy: &Arc<CountingEntropy>,
) -> (Arc<IntegrationStore>, StoredInputStore) {
    let clock: Arc<dyn Clock> = Arc::new(FakeClock::at(
        UNIX_EPOCH + Duration::from_secs(1_000_000_000),
    ));
    let path = |name: &str| root.join(name);
    let integrations = IntegrationStore::new(
        path("i/state/i.json"),
        path("i/key/k"),
        Arc::clone(&clock),
        entropy.clone(),
    );
    let stored = StoredInputStore::new(
        path("s/state/s.json"),
        path("s/key/k"),
        clock,
        entropy.clone(),
    );
    (
        Arc::new(integrations.expect("store")),
        stored.expect("store"),
    )
}

fn world() -> World {
    let root = tempfile::tempdir().expect("temporary directory");
    let entropy = Arc::new(CountingEntropy::default());
    let path = |name: &str| root.path().join(name);
    let (integrations, stored) = stores(root.path(), &entropy);
    let oauth = oauth(&integrations, entropy.clone());
    let engine = Arc::new(FakeExec::default());
    let tunnel = team_integrations::https::Tunnel::new(
        "127.0.0.1".to_owned(),
        1,
        Duration::from_secs(1),
        rustls::RootCertStore::empty(),
    );
    let runner = Runner {
        journal: Arc::new(
            Journal::open(
                &path("journal/journal.sqlite3"),
                Limits::default(),
                entropy.clone(),
            )
            .expect("journal"),
        ),
        stored_inputs: Arc::new(stored),
        oauth,
        integrations,
        engine: engine.clone(),
        slots: Arc::new(DockerSlots::default()),
        capacity: Capacity::default(),
        transport: Arc::new(Transport::new(tunnel)),
        audit: Arc::new(MemoryAudit::default()),
        workloads: Arc::new(Fixed(binding())),
        entropy,
        blocked: Mutex::default(),
        transcripts: Mutex::default(),
    };
    World {
        _root: root,
        engine,
        runner: Arc::new(runner),
    }
}

/// Scripts one exec that reads its request line, answers `terminal`, and exits `code`; the request it read.
fn scripted(engine: &FakeExec, terminal: &str, code: i64) -> tokio::task::JoinHandle<Vec<u8>> {
    let (ours, mut workload) = duplex(64 * 1024);
    let output = frame(1, format!("{terminal}\n").as_bytes());
    engine.push(ScriptedExec {
        stream: ours,
        exit: ExecExit {
            running: false,
            exit_code: Some(code),
        },
    });
    tokio::spawn(async move {
        let mut request = Vec::new();
        let mut byte = [0_u8; 1];
        while workload.read(&mut byte).await.is_ok_and(|read| read == 1) && byte[0] != b'\n' {
            request.push(byte[0]);
        }
        let _ = workload.write_all(&output).await;
        let _ = workload.shutdown().await;
        request
    })
}

fn request(interrupt: &str, action: &str) -> ActionRequest {
    ActionRequest {
        interrupt_id: interrupt.to_owned(),
        assistant_id: "example".to_owned(),
        action: action.to_owned(),
        input: json(r#"{"q":"a"}"#),
    }
}

#[tokio::test]
async fn stored_input_ids_only_in_exec_request() {
    let world = world();
    let value = Secret::new("stored-secret-value-1234".to_owned());
    world
        .runner
        .stored_inputs
        .seal(
            ("team_1", "example", "api-key"),
            "password",
            &value,
            &"a".repeat(64),
        )
        .expect("sealed");
    let sent = scripted(
        &world.engine,
        r#"{"type":"result","result":{"ok":true}}"#,
        0,
    );
    let result = Executor(Arc::clone(&world.runner))
        .invoke("team_1", &request("i-1", "lookup"), &Stop::new())
        .await;
    assert_eq!(result, Ok(json(r#"{"ok":true}"#)));
    let sent = String::from_utf8(sent.await.expect("joined")).expect("utf-8");
    assert!(
        sent.contains(r#""stored_inputs":["api-key"]"#) && !sent.contains("stored-secret-value")
    );
}

#[tokio::test]
async fn an_undeclared_action_or_invalid_input_never_dispatches() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    assert_eq!(
        executor
            .invoke("team_1", &request("i-1", "missing"), &Stop::new())
            .await,
        Err(ActionFailure::NotDeclared)
    );
    let invalid = ActionRequest {
        input: json("[]"),
        ..request("i-1", "lookup")
    };
    assert_eq!(
        executor.invoke("team_1", &invalid, &Stop::new()).await,
        Err(ActionFailure::InvalidInput)
    );
    let foreign = ActionRequest {
        assistant_id: "other".to_owned(),
        ..request("i-1", "lookup")
    };
    assert_eq!(
        executor.invoke("team_1", &foreign, &Stop::new()).await,
        Err(ActionFailure::ContextChanged)
    );
    assert_eq!(world.engine.started().len(), 0);
}

fn scope(routine: bool) -> BatchScope {
    BatchScope {
        team_id: "team_1".to_owned(),
        generation: "network-1".to_owned(),
        thread_id: "thread-1".to_owned(),
        routine,
    }
}

#[tokio::test]
async fn completed_replays_without_a_second_dispatch() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "publish")];
    let _sent = scripted(&world.engine, r#"{"type":"result","result":{"n":1}}"#, 0);
    let mut first = executor.batch(scope(false));
    first.prepare(&requests).await.expect("prepared");
    assert_eq!(
        first.invoke(&requests[0], &Stop::new()).await,
        Ok(InvokeOutcome::Completed(json(r#"{"n":1}"#)))
    );
    assert_eq!(first.terminate().await, Ok(true));
    let mut replay = executor.batch(scope(false));
    replay.prepare(&requests).await.expect("reopened");
    assert_eq!(
        replay.invoke(&requests[0], &Stop::new()).await,
        Ok(InvokeOutcome::Completed(json(r#"{"n":1}"#)))
    );
    assert_eq!(world.engine.started().len(), 1);
    replay.delivered(&requests).await.expect("delivered");
}

#[tokio::test]
async fn journal_executing_is_uncertain_never_reexecuted() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "publish")];
    let _sent = scripted(&world.engine, r#"{"type":"result","result":{}}"#, 1);
    let mut first = executor.batch(scope(false));
    first.prepare(&requests).await.expect("prepared");
    assert_eq!(
        first.invoke(&requests[0], &Stop::new()).await,
        Err(ActionFailure::Uncertain)
    );
    assert_eq!(world.engine.stopped(), ["container"]);
    // The interrupted operation stays uncertain: the same batch is refused again without a dispatch.
    let mut again = executor.batch(scope(false));
    again.prepare(&requests).await.expect("reopened");
    assert_eq!(
        again.invoke(&requests[0], &Stop::new()).await,
        Err(ActionFailure::Uncertain)
    );
    assert_eq!(world.engine.started().len(), 1);
}

#[tokio::test]
async fn settled_batch_ends_on_provider_failure_or_stop() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "lookup")];
    let failure = r#"{"type":"failure","failure":{"error_type":"E","message":"m","provider":null,"http_status":null,"response_excerpt":null,"redacted":false,"truncated":false}}"#;
    let _sent = scripted(&world.engine, failure, 0);
    let mut batch = executor.batch(scope(false));
    batch.prepare(&requests).await.expect("prepared");
    // A reviewed read-only Action's handled failure had no business effect, so its batch ends cleanly.
    assert_eq!(
        batch.invoke(&requests[0], &Stop::new()).await,
        Err(ActionFailure::Failed)
    );
    assert_eq!(batch.terminate().await, Ok(true));
    let stop = Stop::new();
    stop.stop();
    let mut stopped = executor.batch(scope(true));
    let fresh = [request("i-2", "publish")];
    stopped.prepare(&fresh).await.expect("prepared");
    assert_eq!(
        stopped.invoke(&fresh[0], &stop).await,
        Err(ActionFailure::Stopped)
    );
    assert_eq!(stopped.terminate().await, Ok(true));
}

#[tokio::test]
async fn an_unavailable_integration_is_refused_before_dispatch() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let sync = request("i-1", "sync");
    let direct = executor.invoke("team_1", &sync, &Stop::new()).await;
    assert_eq!(direct, Err(ActionFailure::IntegrationUnavailable));
    let mut batch = executor.batch(scope(false));
    let prepared = batch.prepare(std::slice::from_ref(&sync)).await;
    assert_eq!(prepared, Err(ActionFailure::IntegrationUnavailable));
    assert_eq!(world.engine.started().len(), 0);
}
