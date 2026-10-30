//! One chat service over fakes and real stores in a temporary directory.

use std::fmt::Write as _;
use std::sync::Arc;

use strict_json::Value;
use team_action::stored_input::StoredInputStore;
use team_core::clock::SystemClock;
use team_core::entropy::OsEntropy;
use team_core::secret::Secret;
use team_core::testing::{FakeClock, MemoryAudit};
use team_inference::brain::{BrainClient, ModelCredential};
use team_inference::config::{InferenceConfig, InferenceConfigStore};
use team_inference::testing::ScriptedBrain;
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::store::IntegrationStore;
use team_storage::files::FileStore;

use crate::chat::continuation_store::ContinuationStore;
use crate::chat::limit::ChatLimitStore;
use crate::chat::ports::ActiveAssistant;
use crate::chat::service::{ChatParts, ChatService};
use crate::chat::tests::fakes::{FakeExecutor, FakePreparer, FakeTeam, action, assistant, json};
use crate::chat::usage::UsageBook;
use crate::coordination::Coordination;
use crate::http::route::{Method, resolve};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service, StreamRecord};
use crate::routine::diagnostics::DiagnosticStore;
use crate::routine::proposal::{ProposalBook, RoutineChat};
use crate::routine::recorder::RecordingBook;
use crate::routine::state::none_lost;
use crate::routine::store::RoutineStore;

/// The Team every journey runs in.
pub const TEAM: &str = "team_1";

pub struct Harness {
    pub directory: tempfile::TempDir,
    pub preparer: Arc<FakePreparer>,
    pub parts: ChatParts,
    pub clock: Arc<FakeClock>,
    pub integrations: Arc<IntegrationStore>,
    pub service: ChatService,
    pub brain: Arc<ScriptedBrain>,
    pub executor: Arc<FakeExecutor>,
    pub team: Arc<FakeTeam>,
    pub audit: Arc<MemoryAudit>,
    pub inference: Arc<InferenceConfigStore>,
    pub continuations: Arc<ContinuationStore>,
    pub coordination: Coordination,
}

/// The default Assistant every journey selects: `shimpz-cloudflare` with `list-zones`.
pub fn cloudflare() -> ActiveAssistant {
    assistant("shimpz-cloudflare", vec![action("list-zones")])
}

pub fn harness() -> Harness {
    harness_with(Arc::new(ScriptedBrain::default()), vec![cloudflare()])
}

fn stores(
    root: &std::path::Path,
) -> (Arc<FileStore>, Arc<IntegrationStore>, Arc<StoredInputStore>) {
    let clock = Arc::new(SystemClock);
    let files = FileStore::new(
        root.join("storage"),
        1 << 30,
        None,
        clock.clone(),
        Arc::new(OsEntropy),
    )
    .expect("files");
    let sealed = |name: &str| {
        (
            root.join(name).join("state/state.json"),
            root.join(name).join("key/aes256.key"),
        )
    };
    let (state, key) = sealed("assistant-integrations");
    let integrations = IntegrationStore::new(state, key, clock.clone(), Arc::new(OsEntropy))
        .expect("integrations");
    let (state, key) = sealed("assistant-stored-inputs");
    let stored =
        StoredInputStore::new(state, key, clock, Arc::new(OsEntropy)).expect("stored inputs");
    (Arc::new(files), Arc::new(integrations), Arc::new(stored))
}

/// The inference settings of the Team: `OpenAI`'s `gpt-6-luna`.
fn configured(root: &std::path::Path) -> Arc<InferenceConfigStore> {
    let inference = Arc::new(InferenceConfigStore::new(root.join("inference")));
    let config = InferenceConfig {
        provider: "openai".to_owned(),
        model: "gpt-6-luna".to_owned(),
    };
    inference.save(TEAM, &config).expect("configured");
    inference
}

fn continuations(root: &std::path::Path, clock: &Arc<FakeClock>) -> Arc<ContinuationStore> {
    let state = root.join("chat-continuations/state/continuations.json");
    let key = root.join("chat-continuations/key/aes256.key");
    let clock = Arc::clone(clock) as Arc<dyn team_core::clock::Clock>;
    let store = ContinuationStore::new(&state, &key, clock, Arc::new(OsEntropy), 32);
    Arc::new(store.expect("continuations"))
}

/// The Team's Routine state, its diagnostics, and empty recording and card books.
fn routine_chat(root: &std::path::Path, clock: &Arc<FakeClock>) -> RoutineChat {
    let clock = Arc::clone(clock) as Arc<dyn team_core::clock::Clock>;
    let sealed = |name: &str| {
        (
            root.join(name).join("state"),
            root.join(name).join("key/aes256.key"),
        )
    };
    let (state, key) = sealed("routines");
    let routines = RoutineStore::new(&state, &key, Arc::new(OsEntropy)).expect("routines");
    let (state, key) = sealed("routine-diagnostics");
    let diagnostics = DiagnosticStore::new(&state, &key, Arc::new(OsEntropy)).expect("diagnostics");
    RoutineChat {
        routines: Arc::new(routines),
        diagnostics: Arc::new(diagnostics),
        recordings: Arc::new(RecordingBook::new(Arc::clone(&clock), Arc::new(OsEntropy))),
        proposals: Arc::new(ProposalBook::new(Arc::clone(&clock))),
        lost: Arc::new(none_lost),
        clock,
        entropy: Arc::new(OsEntropy),
    }
}

pub fn harness_with(
    brain: Arc<dyn BrainClientScripted>,
    assistants: Vec<ActiveAssistant>,
) -> Harness {
    harness_on(brain, Arc::new(FakeTeam::with(TEAM, assistants)))
}

/// The stores and fakes a harness's service runs with.
struct Built {
    parts: ChatParts,
    executor: Arc<FakeExecutor>,
    preparer: Arc<FakePreparer>,
    audit: Arc<MemoryAudit>,
    clock: Arc<FakeClock>,
    integrations: Arc<IntegrationStore>,
}

fn built(root: &std::path::Path, brain: Arc<dyn BrainClient>, team: Arc<FakeTeam>) -> Built {
    let limit = Arc::new(ChatLimitStore::new(&root.join("inference")));
    let clock = Arc::new(FakeClock::at(std::time::SystemTime::now()));
    let (files, integrations, stored_inputs) = stores(root);
    let (executor, preparer) = (
        Arc::new(FakeExecutor::default()),
        Arc::new(FakePreparer::default()),
    );
    let audit = Arc::new(MemoryAudit::default());
    let challenges = team_integrations::pending::default_store(Arc::new(OsEntropy)).expect("store");
    let parts = ChatParts {
        space_id: "local-space".to_owned(),
        coordination: Coordination::new(limit.clone()),
        team,
        brain,
        actions: executor.clone(),
        preparer: preparer.clone(),
        files,
        inference: configured(root),
        limit,
        continuations: continuations(root, &clock),
        turns: Arc::new(crate::chat::turn::store::Turns::new(
            &root.join("chat-continuations/state/turns"),
            &root.join("chat-continuations/key/aes256.key"),
            Arc::new(OsEntropy),
        )),
        integrations: Arc::clone(&integrations),
        stored_inputs,
        integration_challenges: Arc::new(challenges),
        pkce: Arc::new(PkceStore::new(PkceLimits::default(), Arc::new(OsEntropy)).expect("pkce")),
        browser: None,
        routine: routine_chat(root, &clock),
        usage: Arc::new(UsageBook::default()),
        audit: audit.clone(),
        clock: clock.clone(),
        entropy: Arc::new(OsEntropy),
    };
    Built {
        parts,
        executor,
        preparer,
        audit,
        clock,
        integrations,
    }
}

pub fn harness_on(brain: Arc<dyn BrainClientScripted>, team: Arc<FakeTeam>) -> Harness {
    let directory = tempfile::tempdir().expect("directory");
    let built = built(
        directory.path(),
        Arc::clone(&brain).client(),
        Arc::clone(&team),
    );
    let parts = built.parts;
    Harness {
        directory,
        preparer: built.preparer,
        clock: built.clock,
        integrations: built.integrations,
        service: ChatService::new(parts.clone()).expect("service"),
        brain: brain.scripted(),
        executor: built.executor,
        team,
        audit: built.audit,
        inference: Arc::clone(&parts.inference),
        continuations: Arc::clone(&parts.continuations),
        coordination: parts.coordination.clone(),
        parts,
    }
}

/// A Brain double a harness can be built over: the scripted one, or one that also blocks.
pub trait BrainClientScripted: Send + Sync {
    /// The client the service calls.
    fn client(self: Arc<Self>) -> Arc<dyn BrainClient>;
    /// The scripted answers behind it.
    fn scripted(self: Arc<Self>) -> Arc<ScriptedBrain>;
}

impl BrainClientScripted for ScriptedBrain {
    fn client(self: Arc<Self>) -> Arc<dyn BrainClient> {
        self
    }

    fn scripted(self: Arc<Self>) -> Arc<ScriptedBrain> {
        self
    }
}

const USAGE: &str = r#""usage":{"model_calls":1,"provider_requests":1,"failed_calls":0,"unreported_calls":0,
"input_tokens":1331,"output_tokens":36,"cache_read_tokens":0,"cache_write_tokens":0}"#;

/// A turn answer with its usage and empty defaults unless `fields` states them.
pub fn turn(fields: &str) -> Value {
    let mut text = format!("{{{fields}");
    for (name, default) in [
        ("memory", "[]"),
        ("routine", "null"),
        ("browser", "null"),
        ("clarification", "null"),
    ] {
        if !fields.contains(&format!("\"{name}\"")) {
            let _ = write!(text, ",\"{name}\":{default}");
        }
    }
    let _ = write!(text, ",{USAGE}}}");
    json(&text)
}

/// A completed turn's answer.
pub fn reply(text: &str) -> Value {
    turn(&format!(
        r#""status":"completed","reply":"{text}","actions":[]"#
    ))
}

/// An Action suspension of `list-zones` under `interrupt`.
pub fn suspension(interrupt: &str) -> Value {
    turn(&format!(
        r#""status":"action-required","reply":"","actions":[{{"interrupt_id":"{interrupt}",
        "assistant_id":"shimpz-cloudflare","action":"list-zones","input":{{"zone":"example.com"}}}}]"#
    ))
}

/// Each send's own nonce: a send is never resent as itself (ADR-0092), and Team runs a repeated one only once.
pub static SENDS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

/// A fresh nonce.
pub fn nonce() -> String {
    format!(
        "{:032x}",
        SENDS.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
    )
}

/// One chat body under a fresh request identity sent a quarter of an hour ago: within its turn's grant, too old to
/// open a Routine recording.
pub fn chat_body(message: &str, assistants: &[&str], locale: Option<&str>) -> Value {
    let assistants: Vec<String> = assistants.iter().map(|id| format!("\"{id}\"")).collect();
    let locale = locale.map_or_else(|| "null".to_owned(), |locale| format!("\"{locale}\""));
    json(&format!(
        r#"{{"message":"{message}","files":[],"assistant_ids":[{}],"conversation":[],
        "request":{{"issued_at":{},"nonce":"{}"}},"timezone":null,"locale":{locale},"refine":null}}"#,
        assistants.join(","),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_secs())
            - 1_000,
        nonce()
    ))
}

/// The session every harness call is sent under.
pub const SESSION: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

/// Calls one operation on `service` as the Supervisor of the session `authority` names, with the test model credential.
pub async fn call_on(
    service: &ChatService,
    method: Method,
    path: &str,
    body: Option<Value>,
    authority: &str,
) -> Result<Answer, Problem> {
    let route = resolve(method, path).expect("route");
    let call = Call {
        route: &route,
        principal: Principal::Supervisor {
            supervisor_id: "c".repeat(32),
            assurance: None,
            authority_sha256: authority.to_owned(),
        },
        body: body.map_or(Body::None, Body::Json),
        model: Some(ModelCredential {
            provider: "openai".to_owned(),
            model: String::new(),
            api_key: Secret::new("test-key-not-real-0000".to_owned()),
        }),
        decision_key: None,
        trace_id: "a".repeat(32),
        upgrade_key: None,
    };
    service.call(call).await
}

/// One streamed answer drained to its terminal.
pub async fn drained(answer: Result<Answer, Problem>) -> Streamed {
    let Ok(Answer::Stream(mut records)) = answer else {
        panic!("not a stream: {answer:?}");
    };
    let mut progress = Vec::new();
    while let Some(record) = records.recv().await {
        match record {
            StreamRecord::Progress(event) => progress.push(event),
            StreamRecord::Terminal { status, body } => {
                return Streamed {
                    progress,
                    status,
                    body,
                };
            }
        }
    }
    panic!("the stream ended without its terminal record");
}

/// One streamed answer: its progress, then its terminal status and body.
pub struct Streamed {
    pub progress: Vec<Value>,
    pub status: u16,
    pub body: Value,
}

impl Streamed {
    pub fn code(&self) -> Option<&str> {
        match self.body.get("code") {
            Some(Value::String(code)) => Some(code),
            _ => None,
        }
    }

    pub fn text(&self, name: &str) -> Option<&str> {
        match self.body.get(name) {
            Some(Value::String(text)) => Some(text),
            _ => None,
        }
    }
}

impl Harness {
    /// The same Team after a controller restart: the same stores and Brain, fresh in-memory challenges.
    pub fn restarted(&self) -> ChatService {
        let mut parts = self.parts.clone();
        parts.integration_challenges = Arc::new(
            team_integrations::pending::default_store(Arc::new(OsEntropy)).expect("store"),
        );
        ChatService::new(parts).expect("service")
    }

    /// The same Team after its process died: the same stores and Action executor, a fresh process's slots, turns,
    /// and challenges, and the Brain `brain` answering from here on.
    pub fn after_death(&self, brain: Arc<ScriptedBrain>) -> ChatService {
        let mut parts = self.parts.clone();
        let limit = Arc::clone(&parts.limit);
        parts.coordination = Coordination::new(limit);
        parts.brain = brain;
        parts.turns = Arc::new(crate::chat::turn::store::Turns::new(
            &self.directory.path().join("chat-continuations/state/turns"),
            &self
                .directory
                .path()
                .join("chat-continuations/key/aes256.key"),
            Arc::new(OsEntropy),
        ));
        parts.integration_challenges = Arc::new(
            team_integrations::pending::default_store(Arc::new(OsEntropy)).expect("store"),
        );
        ChatService::new(parts).expect("service")
    }

    /// Calls one operation as the Supervisor with the test model credential.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Answer, Problem> {
        call_on(&self.service, method, path, body, SESSION).await
    }

    /// Calls one JSON operation.
    pub async fn json(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, Problem> {
        match self.call(method, path, body).await? {
            Answer::Json { body, .. } => Ok(body),
            other => panic!("not JSON: {other:?}"),
        }
    }

    /// Calls one streamed operation and drains it.
    pub async fn stream(&self, method: Method, path: &str, body: Value) -> Streamed {
        drained(self.call(method, path, Some(body)).await).await
    }
    /// Starts one turn and waits for its terminal.
    pub async fn chat(&self, body: Value) -> Streamed {
        self.stream(Method::Post, "/v1/teams/team_1/chat", body)
            .await
    }

    /// Answers the pending human request.
    pub async fn answer(&self, challenge: &str, decision: &str) -> Streamed {
        let body = if decision == "submit" {
            json(&format!(
                r#"{{"challenge_id":"{challenge}","decision":"submit","value":true}}"#
            ))
        } else {
            json(&format!(
                r#"{{"challenge_id":"{challenge}","decision":"deny"}}"#
            ))
        };
        self.stream(Method::Post, "/v1/teams/team_1/chat/human", body)
            .await
    }
}
