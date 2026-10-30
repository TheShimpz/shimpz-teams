//! One chat service over fakes and real stores in a temporary directory.

use std::fmt::Write as _;
use std::sync::Arc;

use strict_json::Value;
use team_action::stored_input::StoredInputStore;
use team_core::clock::SystemClock;
use team_core::entropy::OsEntropy;
use team_core::secret::Secret;
use team_core::testing::MemoryAudit;
use team_inference::brain::{BrainClient, ModelCredential};
use team_inference::config::{InferenceConfig, InferenceConfigStore};
use team_inference::testing::ScriptedBrain;
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::store::IntegrationStore;
use team_storage::files::FileStore;

use crate::chat::continuation_store::ContinuationStore;
use crate::chat::fakes::{
    FakeExecutor, FakePreparer, FakeTeam, FakeTranscripts, action, assistant, json,
};
use crate::chat::limit::ChatLimitStore;
use crate::chat::ports::ActiveAssistant;
use crate::chat::service::{ChatParts, ChatService};
use crate::chat::usage::UsageBook;
use crate::coordination::Coordination;
use crate::http::route::{Method, resolve};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service, StreamRecord};

/// The Team every journey runs in.
pub const TEAM: &str = "team_1";

pub struct Harness {
    pub _directory: tempfile::TempDir,
    pub service: ChatService,
    pub brain: Arc<ScriptedBrain>,
    pub executor: Arc<FakeExecutor>,
    pub team: Arc<FakeTeam>,
    pub transcripts: Arc<FakeTranscripts>,
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

fn continuations(root: &std::path::Path) -> Arc<ContinuationStore> {
    let state = root.join("chat-continuations/state/continuations.json");
    let key = root.join("chat-continuations/key/aes256.key");
    let store =
        ContinuationStore::new(&state, &key, Arc::new(SystemClock), Arc::new(OsEntropy), 32);
    Arc::new(store.expect("continuations"))
}

pub fn harness_with(
    brain: Arc<dyn BrainClientScripted>,
    assistants: Vec<ActiveAssistant>,
) -> Harness {
    harness_on(brain, Arc::new(FakeTeam::with(TEAM, assistants)))
}

pub fn harness_on(brain: Arc<dyn BrainClientScripted>, team: Arc<FakeTeam>) -> Harness {
    let directory = tempfile::tempdir().expect("directory");
    let root = directory.path().to_path_buf();
    let limit = Arc::new(ChatLimitStore::new(&root.join("inference")));
    let coordination = Coordination::new(limit.clone());
    let inference = configured(&root);
    let continuations = continuations(&root);
    let (files, integrations, stored_inputs) = stores(&root);
    let executor = Arc::new(FakeExecutor::default());
    let transcripts = Arc::new(FakeTranscripts::default());
    let audit = Arc::new(MemoryAudit::default());
    let parts = ChatParts {
        space_id: "local-space".to_owned(),
        coordination: coordination.clone(),
        team: team.clone(),
        brain: Arc::clone(&brain).client(),
        actions: executor.clone(),
        transcripts: transcripts.clone(),
        preparer: Arc::new(FakePreparer),
        files,
        inference: inference.clone(),
        limit,
        continuations: continuations.clone(),
        integrations,
        stored_inputs,
        integration_challenges: Arc::new(
            team_integrations::pending::default_store(Arc::new(OsEntropy)).expect("store"),
        ),
        pkce: Arc::new(PkceStore::new(PkceLimits::default(), Arc::new(OsEntropy)).expect("pkce")),
        browser: None,
        usage: Arc::new(UsageBook::default()),
        audit: audit.clone(),
        clock: Arc::new(SystemClock),
        entropy: Arc::new(OsEntropy),
    };
    Harness {
        _directory: directory,
        service: ChatService::new(parts).expect("service"),
        brain: brain.scripted(),
        executor,
        team,
        transcripts,
        audit,
        inference,
        continuations,
        coordination,
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

/// One chat body under the fixed test request identity.
pub fn chat_body(message: &str, assistants: &[&str], locale: Option<&str>) -> Value {
    let assistants: Vec<String> = assistants.iter().map(|id| format!("\"{id}\"")).collect();
    let locale = locale.map_or_else(|| "null".to_owned(), |locale| format!("\"{locale}\""));
    json(&format!(
        r#"{{"message":"{message}","files":[],"assistant_ids":[{}],"conversation":[],
        "request":{{"issued_at":1700000000,"nonce":"{}"}},"timezone":null,"locale":{locale},"refine":null}}"#,
        assistants.join(","),
        "0".repeat(32)
    ))
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
    /// Calls one operation as the Supervisor with the test model credential.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Answer, Problem> {
        let route = resolve(method, path).expect("route");
        let call = Call {
            route: &route,
            principal: Principal::Supervisor {
                supervisor_id: "c".repeat(32),
                assurance: None,
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
        self.service.call(call).await
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
        let answer = self.call(method, path, Some(body)).await;
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
