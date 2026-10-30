use std::sync::{Arc, Mutex};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_assistant::manifest::IntegrationDeclaration;
use team_assistant::manifest::ManifestPresentation;
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
use crate::executor::{
    ActionExecutor, ActionFailure, ActionRequest, BatchScope, DispatchObserver, DispatchRefused,
    Fault, InvokeOutcome,
};
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
            crate::files::tests::upload(),
            ActionSpec::of(&json(
                r#"{"id":"sync","input_schema":{"type":"object"},"output_schema":{"type":"object"},"integrations":["cloudflare-write"]}"#,
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
    audit: Arc<MemoryAudit>,
    file: String,
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
    let audit = Arc::new(MemoryAudit::default());
    let (source, file) = crate::files::tests::store(root.path());
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
        audit: audit.clone(),
        workloads: Arc::new(Fixed(binding())),
        entropy,
        blocked: Mutex::default(),
        files: Arc::new(source),
        file_slot: crate::files::FileSlot::default(),
    };
    World {
        _root: root,
        engine,
        runner: Arc::new(runner),
        audit,
        file,
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
        files: Vec::new(),
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
        transcripts: Vec::new(),
    }
}

fn answered(interrupt_id: &str) -> BatchScope {
    BatchScope {
        transcripts: vec![crate::files::tests::approved(interrupt_id)],
        ..scope(false)
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

fn upload_request(world: &World, interrupt: &str) -> ActionRequest {
    ActionRequest {
        input: json(&format!(r#"{{"document":"{}"}}"#, world.file)),
        files: crate::files::selected(&[crate::files::tests::wire(&world.file)]),
        ..request(interrupt, "upload")
    }
}

async fn sent_text(sent: tokio::task::JoinHandle<Vec<u8>>) -> String {
    String::from_utf8(sent.await.expect("joined")).expect("utf-8")
}

#[tokio::test]
async fn file_bytes_reach_only_the_approved_replay_and_are_audited_by_id_and_size() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let first = upload_request(&world, "i-1");
    let sent = scripted(&world.engine, r#"{"type":"result","result":{}}"#, 0);
    // The first invocation carries metadata only, so it cannot report success with the file.
    let withheld = executor.invoke("team_1", &first, &Stop::new()).await;
    assert_eq!(withheld, Err(ActionFailure::Uncertain));
    let sent = sent_text(sent).await;
    assert!(sent.contains(r#""content":{"type":"withheld"}"#) && !sent.contains("base64"));
    let replay = [upload_request(&world, "i-2")];
    let sent = scripted(
        &world.engine,
        r#"{"type":"result","result":{"ok":true}}"#,
        0,
    );
    let mut batch = executor.batch(answered("i-2"));
    batch.prepare(&replay).await.expect("prepared");
    let completed = batch.invoke(&replay[0], &Stop::new()).await;
    assert_eq!(
        completed,
        Ok(InvokeOutcome::Completed(json(r#"{"ok":true}"#)))
    );
    let encoded = STANDARD.encode(b"%PDF-1.4 quarterly report");
    assert!(sent_text(sent).await.contains(&encoded));
    let details: Vec<String> = world
        .audit
        .events()
        .iter()
        .flat_map(|event| event.fields().to_vec())
        .filter_map(|(_, value)| match value {
            Value::String(text) => Some(text),
            _ => None,
        })
        .collect();
    let delivered = format!("file-delivered:upload:{}:25", world.file);
    assert!(details.iter().any(|detail| detail.contains(&delivered)));
    assert!(details.iter().all(|detail| !detail.contains("Relat")));
    assert!(world.runner.file_slot.free());
}

#[tokio::test(start_paused = true)]
async fn a_refused_admission_never_begins_the_journaled_attempt() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [upload_request(&world, "i-1")];
    let mut batch = executor.batch(answered("i-1"));
    batch.prepare(&requests).await.expect("prepared");
    let held = world.runner.file_slot.admit(true, &Stop::new()).await;
    let busy = batch.invoke(&requests[0], &Stop::new()).await;
    assert_eq!(busy, Err(ActionFailure::FileBusy));
    let stop = Stop::new();
    stop.stop();
    let stopped = batch.invoke(&requests[0], &stop).await;
    assert_eq!(stopped, Err(ActionFailure::Stopped));
    drop(held);
    assert_eq!(world.engine.started().len(), 0);
    // Nothing was journaled `executing`, so the operation still runs once the slot is free.
    let _sent = scripted(&world.engine, r#"{"type":"result","result":{}}"#, 0);
    let ran = batch.invoke(&requests[0], &Stop::new()).await;
    assert_eq!(ran, Ok(InvokeOutcome::Completed(json("{}"))));
}

#[tokio::test]
async fn delivered_bytes_outside_an_admitted_slot_never_reach_the_workload() {
    let world = world();
    let request = upload_request(&world, "i-1");
    let binding = binding();
    let action = binding.contract.action("upload").expect("declared").clone();
    let target = super::Target {
        team: "team_1",
        binding: &binding,
        action: &action,
        operation_id: "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
    };
    let delivery = super::Delivery {
        file: request.files.first(),
        admitted: None,
    };
    let transcript = crate::files::tests::approved("i-1");
    let mut watch = super::Watch {
        observer: None,
        injected: Vec::new(),
    };
    let stop = Stop::new();
    let attempt = world
        .runner
        .attempt(
            &target,
            &request.input,
            &transcript,
            delivery,
            (&stop, &mut watch),
        )
        .await;
    let refused = super::Failed::NotDispatched(ActionFailure::FileUnavailable);
    assert_eq!(attempt, Err(refused));
    assert_eq!(world.engine.started().len(), 0);
}

#[tokio::test]
async fn a_file_the_turn_did_not_select_or_a_changed_file_is_refused_before_journaling() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let unselected = ActionRequest {
        files: Vec::new(),
        ..upload_request(&world, "i-1")
    };
    let mut batch = executor.batch(scope(false));
    let prepared = batch.prepare(std::slice::from_ref(&unselected)).await;
    assert_eq!(prepared, Err(ActionFailure::FileUnavailable));
    let direct = executor.invoke("team_1", &unselected, &Stop::new()).await;
    assert_eq!(direct, Err(ActionFailure::FileUnavailable));
    // The file's commitment is in the fingerprint: a different file under the same id is a changed context.
    let requests = [upload_request(&world, "i-2")];
    let mut batch = executor.batch(scope(false));
    batch.prepare(&requests).await.expect("prepared");
    let mut changed = requests[0].clone();
    changed.files[0].name = "other.pdf".to_owned();
    let invoked = batch.invoke(&changed, &Stop::new()).await;
    assert_eq!(invoked, Err(ActionFailure::ContextChanged));
    assert_eq!(world.engine.started().len(), 0);
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

/// What a Routine run's batch told its observer; it refuses every dispatch when `refuse` is set.
#[derive(Default)]
struct Observer {
    refuse: bool,
    events: Mutex<Vec<String>>,
    injected: Mutex<Vec<String>>,
    faults: Mutex<Vec<Fault>>,
}

fn exposed(values: &[Secret]) -> Vec<String> {
    values
        .iter()
        .map(|value| value.expose().to_owned())
        .collect()
}

impl DispatchObserver for Observer {
    fn logical_operation(&self, _request: &ActionRequest) -> Option<String> {
        None
    }

    fn dispatching(
        &self,
        request: &ActionRequest,
        _operation_id: &str,
        workload: &str,
        injected: &[Secret],
    ) -> Result<(), DispatchRefused> {
        let event = format!("dispatching {} {workload}", request.interrupt_id);
        self.events.lock().expect("events").push(event);
        *self.injected.lock().expect("injected") = exposed(injected);
        if self.refuse {
            Err(DispatchRefused)
        } else {
            Ok(())
        }
    }

    fn returned(&self, request: &ActionRequest) {
        let event = format!("returned {}", request.interrupt_id);
        self.events.lock().expect("events").push(event);
    }

    fn failed(
        &self,
        request: &ActionRequest,
        _operation_id: &str,
        fault: Fault,
        injected: &[Secret],
    ) {
        let event = format!("failed {}", request.interrupt_id);
        self.events.lock().expect("events").push(event);
        assert_eq!(exposed(injected), *self.injected.lock().expect("injected"));
        self.faults.lock().expect("faults").push(fault);
    }
}

const TOKEN: &str = "integration-token-value-1";
const STORED: &str = "stored-secret-value-1234";

/// Connects the `sync` Action's Integration and seals the `api-key` Stored Input the other Actions declare.
fn connected(world: &World) {
    let tokens = team_integrations::token::TokenSet {
        access_token: Secret::new(TOKEN.to_owned()),
        refresh_token: None,
        scopes: vec!["zone.read".to_owned()],
        expires_in: 3600,
        broker_lease: None,
    };
    let scopes = ["zone.read".to_owned()];
    world
        .runner
        .integrations
        .put(
            ("team_1", "example", "cloudflare-write"),
            ("cloudflare", &scopes),
            &tokens,
            None,
        )
        .expect("connected");
    world
        .runner
        .stored_inputs
        .seal(
            ("team_1", "example", "api-key"),
            "password",
            &Secret::new(STORED.to_owned()),
            &"a".repeat(64),
        )
        .expect("sealed");
}

#[tokio::test]
async fn a_batch_reports_every_value_it_injected_into_its_last_call() {
    let world = world();
    connected(&world);
    let ok = r#"{"type":"result","result":{"ok":true}}"#;
    let _first = scripted(&world.engine, ok, 0);
    let _second = scripted(&world.engine, ok, 0);
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "sync"), request("i-2", "lookup")];
    let mut batch = executor.batch(scope(false));
    batch.prepare(&requests).await.expect("prepared");
    let completed = InvokeOutcome::Completed(json(r#"{"ok":true}"#));
    assert_eq!(
        batch.invoke(&requests[0], &Stop::new()).await,
        Ok(completed.clone())
    );
    assert_eq!(exposed(&batch.injected()), [TOKEN]);
    assert_eq!(
        batch.invoke(&requests[1], &Stop::new()).await,
        Ok(completed)
    );
    assert_eq!(exposed(&batch.injected()), [STORED]);
}

#[tokio::test]
async fn a_routine_batch_seals_each_dispatch_with_its_workload_and_values_before_the_rpc() {
    let world = world();
    connected(&world);
    let _sent = scripted(
        &world.engine,
        r#"{"type":"result","result":{"ok":true}}"#,
        0,
    );
    let observer = Arc::new(Observer::default());
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "lookup")];
    let mut batch = executor.observed(scope(true), observer.clone());
    batch.prepare(&requests).await.expect("prepared");
    assert!(batch.invoke(&requests[0], &Stop::new()).await.is_ok());
    let events = observer.events.lock().expect("events").clone();
    assert_eq!(events, ["dispatching i-1 container", "returned i-1"]);
    assert_eq!(*observer.injected.lock().expect("injected"), [STORED]);
}

#[tokio::test]
async fn a_dispatch_the_run_cannot_seal_is_never_sent() {
    let world = world();
    connected(&world);
    let observer = Arc::new(Observer {
        refuse: true,
        ..Observer::default()
    });
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "lookup")];
    let mut batch = executor.observed(scope(true), observer.clone());
    batch.prepare(&requests).await.expect("prepared");
    assert_eq!(
        batch.invoke(&requests[0], &Stop::new()).await,
        Err(ActionFailure::StateUnavailable)
    );
    assert_eq!(world.engine.started().len(), 0);
    assert!(observer.faults.lock().expect("faults").is_empty());
}

#[tokio::test]
async fn a_failed_dispatch_is_classified_by_team_and_told_once() {
    let world = world();
    connected(&world);
    let failure = r#"{"type":"failure","failure":{"error_type":"E","message":"m","provider":null,"http_status":null,"response_excerpt":null,"redacted":false,"truncated":false}}"#;
    let _handled = scripted(&world.engine, failure, 0);
    let _invalid = scripted(&world.engine, r#"{"type":"result","result":[]}"#, 0);
    let observer = Arc::new(Observer::default());
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [request("i-1", "lookup"), request("i-2", "lookup")];
    let mut batch = executor.observed(scope(true), observer.clone());
    batch.prepare(&requests).await.expect("prepared");
    assert_eq!(
        batch.invoke(&requests[0], &Stop::new()).await,
        Err(ActionFailure::Failed)
    );
    assert!(batch.invoke(&requests[1], &Stop::new()).await.is_err());
    let faults = observer.faults.lock().expect("faults").clone();
    assert!(matches!(
        faults.as_slice(),
        [Fault::Handled(_), Fault::Policy(_)]
    ));
    let events = observer.events.lock().expect("events").clone();
    let failed = events
        .iter()
        .filter(|event| event.starts_with("failed"))
        .count();
    assert_eq!(failed, 2);
}

#[tokio::test]
async fn an_approval_reaches_only_the_batch_whose_continuation_carries_it() {
    let world = world();
    let executor = Executor(Arc::clone(&world.runner));
    let requests = [upload_request(&world, "i-1")];
    let sent = scripted(
        &world.engine,
        r#"{"type":"result","result":{"ok":true}}"#,
        0,
    );
    let mut approved = executor.batch(answered("i-1"));
    approved.prepare(&requests).await.expect("prepared");
    let completed = approved.invoke(&requests[0], &Stop::new()).await;
    assert!(matches!(completed, Ok(InvokeOutcome::Completed(_))));
    assert!(sent_text(sent).await.contains("base64"));
    approved.terminate().await.expect("ended");
    // A later batch reusing the same interrupt id, as every Routine run does, carries no approval of its own.
    let sent = scripted(&world.engine, r#"{"type":"result","result":{}}"#, 0);
    let mut later = executor.batch(BatchScope {
        generation: "network-2".to_owned(),
        thread_id: "thread-2".to_owned(),
        ..scope(false)
    });
    later.prepare(&requests).await.expect("prepared");
    let withheld = later.invoke(&requests[0], &Stop::new()).await;
    assert_eq!(withheld, Err(ActionFailure::Uncertain));
    let sent = sent_text(sent).await;
    assert!(sent.contains(r#""content":{"type":"withheld"}"#) && !sent.contains("base64"));
}
