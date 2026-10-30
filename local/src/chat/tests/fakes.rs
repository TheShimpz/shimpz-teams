//! In-memory fakes of what the chat and Routine services read of the rest of the controller, for their tests.

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_action::executor::{
    ActionBatch, ActionExecutor, ActionFailure, ActionRequest, BatchScope, InvokeOutcome,
};
use team_action::runner::Binding;
use team_assistant::language::LanguagePack;
use team_assistant::manifest::ManifestPresentation;
use team_assistant::spec::{ActionSpec, RuntimeContract};
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_routine::pin::PinnedAssistant;

use crate::chat::ports::{ActiveAssistant, TeamScope, TeamView};
use crate::http::service::Problem;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Parses a JSON literal.
#[must_use]
pub fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

/// The fixture image every fake Assistant runs.
pub const IMAGE: &str = "ghcr.io/theshimpz/shimpz-assistant@sha256:1111111111111111111111111111111111111111111111111111111111111111";

/// One read-only Action taking a closed object with an optional `zone` string.
#[must_use]
pub fn action(id: &str) -> ActionSpec {
    ActionSpec::of(&json(&format!(
        r#"{{"id":"{id}","input_schema":{{"type":"object","properties":{{"zone":{{"type":"string"}}}},
        "additionalProperties":false}},"output_schema":{{"type":"object"}},"integrations":[],"stored_inputs":[],
        "human_requests":["approval"],"input_files":[],"effect":"read_only"}}"#
    )))
}

/// The title and description every fake request's copy references, a password request's label, and the help text
/// of the fake Stored Input.
pub const COPY: [&str; 4] = [
    "List zones",
    "Allow listing the zones.",
    "API key",
    "Create a test key in the provider dashboard and copy it.",
];

fn message(text: &str) -> Value {
    team_protocol::python::value::object_of(vec![
        (
            "id",
            Value::String(assistant_protocol::catalog::message_id(text)),
        ),
        ("msgid", Value::String(text.to_owned())),
        ("max_length", team_protocol::python::value::int(80)),
        ("params", Value::Array(Vec::new())),
    ])
}

/// The fake catalog, sorted by id.
#[must_use]
pub fn messages() -> Value {
    let mut entries: Vec<Value> = COPY.iter().map(|text| message(text)).collect();
    entries.sort_by_key(|entry| match entry.get("id") {
        Some(Value::String(id)) => id.clone(),
        _ => String::new(),
    });
    Value::Array(entries)
}

/// One fingerprinted request of `kind` whose copy references the fake catalog.
#[must_use]
pub fn human_request(kind: &str, ordinal: i64) -> Value {
    use team_protocol::python::value::{int, object_of};
    let reference = |text: &str| {
        object_of(vec![
            (
                "message",
                Value::String(assistant_protocol::catalog::message_id(text)),
            ),
            ("params", Value::Object(Vec::new())),
        ])
    };
    let request = object_of(vec![
        ("kind", Value::String(kind.to_owned())),
        ("ordinal", int(ordinal)),
        ("title", reference(COPY[0])),
        ("description", reference(COPY[1])),
    ]);
    let fingerprint = assistant_protocol::human_request::fingerprint(&request);
    let Value::Object(mut framed) = request else {
        unreachable!()
    };
    framed.push(("fingerprint".to_owned(), Value::String(fingerprint)));
    Value::Object(framed)
}

/// The fake binding's pack: its catalog and a `pt` translation of every message.
fn pack(machine_contract: &Value) -> LanguagePack {
    let translations: Vec<(String, Value)> = COPY
        .iter()
        .map(|text| {
            (
                assistant_protocol::catalog::message_id(text),
                Value::String(format!("PT {text}")),
            )
        })
        .collect();
    LanguagePack {
        catalog_digest: team_assistant::language::catalog_digest(machine_contract),
        pack_digest: format!("sha256:{}", "a".repeat(64)),
        messages: messages(),
        translations: Value::Object(vec![("pt".to_owned(), Value::Object(translations))]),
        size: 0,
    }
}

/// The machine-contract document of one fake Action, as a pin covers it.
fn pinned_action(action: &ActionSpec) -> Value {
    let names = |items: &[String]| {
        Value::Array(
            items
                .iter()
                .map(|item| Value::String(item.clone()))
                .collect(),
        )
    };
    team_protocol::python::value::object_of(vec![
        ("id", Value::String(action.id.clone())),
        ("input_schema", action.input_schema.clone()),
        ("output_schema", action.output_schema.clone()),
        ("integrations", names(&action.integrations)),
        ("stored_inputs", names(&action.stored_inputs)),
        ("human_requests", names(&action.human_requests)),
        ("input_files", names(&action.input_files)),
        ("effect", Value::String(action.effect.clone())),
    ])
}

fn pinned(id: &str, actions: &[ActionSpec]) -> PinnedAssistant {
    PinnedAssistant {
        assistant_id: id.to_owned(),
        version: "1.0.0".to_owned(),
        image: IMAGE.to_owned(),
        image_labels: Vec::new(),
        allowed_hosts: Vec::new(),
        actions: actions.iter().map(pinned_action).collect(),
        integrations: BTreeMap::new(),
        stored_inputs: BTreeMap::new(),
        catalog: format!("sha256:{}", "0".repeat(64)),
        pack_digest: format!("sha256:{}", "a".repeat(64)),
    }
}

/// A running fake Assistant with `actions`.
#[must_use]
pub fn assistant(id: &str, actions: Vec<ActionSpec>) -> ActiveAssistant {
    let machine_contract = team_protocol::python::value::object_of(vec![("messages", messages())]);
    let pack = pack(&machine_contract);
    let pinned = pinned(id, &actions);
    let contract = RuntimeContract {
        actions,
        allowed_hosts: Vec::new(),
        integrations: Vec::new(),
        stored_inputs: Vec::new(),
        machine_contract,
        presentation: ManifestPresentation {
            description: String::new(),
            links: Value::Object(Vec::new()),
        },
    };
    ActiveAssistant {
        binding: Binding {
            contract: Arc::new(contract),
            assistant_id: id.to_owned(),
            name: "Shimpz Cloudflare".to_owned(),
            version: "1.0.0".to_owned(),
            summary: "Manage zones.".to_owned(),
            container: "c".repeat(64),
            image: IMAGE.to_owned(),
        },
        genesis: "Use the declared Actions.".to_owned(),
        pack: Arc::new(pack),
        pinned: Arc::new(pinned),
    }
}

/// A Team the fakes hold.
#[derive(Clone, Debug)]
pub struct FakeTeamState {
    /// Its network.
    pub network_id: String,
    /// Its creation name.
    pub team_name: String,
    /// Its display name, when renamed.
    pub display_name: Option<String>,
    /// Its running Assistants.
    pub assistants: Vec<ActiveAssistant>,
}

/// Teams by id; an absent one answers `404`.
#[derive(Debug, Default)]
pub struct FakeTeam {
    /// The Teams.
    pub teams: Mutex<BTreeMap<String, FakeTeamState>>,
}

impl FakeTeam {
    /// One Team `team_id` named `Marketing` running `assistants`.
    #[must_use]
    pub fn with(team_id: &str, assistants: Vec<ActiveAssistant>) -> Self {
        let fake = Self::default();
        lock(&fake.teams).insert(
            team_id.to_owned(),
            FakeTeamState {
                network_id: "f".repeat(64),
                team_name: "Marketing".to_owned(),
                display_name: None,
                assistants,
            },
        );
        fake
    }

    /// Changes one Team.
    pub fn change(&self, team_id: &str, change: impl FnOnce(&mut FakeTeamState)) {
        if let Some(team) = lock(&self.teams).get_mut(team_id) {
            change(team);
        }
    }

    fn state(&self, team_id: &str) -> Result<FakeTeamState, Problem> {
        lock(&self.teams).get(team_id).cloned().ok_or(Problem::new(
            404,
            "Team not found",
            "team-not-found",
        ))
    }
}

impl TeamView for FakeTeam {
    fn network<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<String, Problem>> {
        Box::pin(async move { Ok(self.state(team_id)?.network_id) })
    }

    fn scope<'a>(
        &'a self,
        team_id: &'a str,
        scan: bool,
    ) -> BoxFuture<'a, Result<TeamScope, Problem>> {
        Box::pin(async move {
            let state = self.state(team_id)?;
            Ok(TeamScope {
                team_name: state.team_name,
                network_id: state.network_id,
                assistants: if scan { state.assistants } else { Vec::new() },
            })
        })
    }

    fn teams(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, Problem>> {
        Box::pin(async move {
            Ok(lock(&self.teams)
                .iter()
                .map(|(team, state)| (team.clone(), state.network_id.clone()))
                .collect())
        })
    }

    fn display_name(&self, team_id: &str, _network_id: &str) -> Result<Option<String>, Problem> {
        Ok(self.state(team_id)?.display_name)
    }
}

/// What one invoke of the fake executor answers.
pub type Answer =
    Box<dyn Fn(&ActionRequest, &Stop) -> Result<InvokeOutcome, ActionFailure> + Send + Sync>;

/// An Action executor whose batches log every hook into one shared journal of events.
pub struct FakeExecutor {
    /// Every hook every batch ran, in order (`prepare`, `invoke interrupt`, `delivered`, `terminate`).
    pub events: Arc<Mutex<Vec<String>>>,
    /// Each invoke's answer, in order; an exhausted queue completes with `{}`.
    pub answers: Arc<Mutex<VecDeque<Answer>>>,
    /// Every scope a batch was opened for.
    pub scopes: Arc<Mutex<Vec<BatchScope>>>,
    /// The values every invoke reports it injected (an Integration token, a Stored Input value).
    pub injected: Arc<Mutex<Vec<String>>>,
}

impl Default for FakeExecutor {
    fn default() -> Self {
        Self {
            events: Arc::new(Mutex::new(Vec::new())),
            answers: Arc::new(Mutex::new(VecDeque::new())),
            scopes: Arc::new(Mutex::new(Vec::new())),
            injected: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

impl std::fmt::Debug for FakeExecutor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("FakeExecutor")
    }
}

impl FakeExecutor {
    /// Queues the next invoke's answer.
    pub fn answer(&self, answer: Answer) {
        lock(&self.answers).push_back(answer);
    }

    /// Queues a completed result.
    pub fn complete(&self, result: Value) {
        self.answer(Box::new(move |_, _| {
            Ok(InvokeOutcome::Completed(result.clone()))
        }));
    }

    /// The events so far.
    #[must_use]
    pub fn events(&self) -> Vec<String> {
        lock(&self.events).clone()
    }

    /// The interrupts invoked so far.
    #[must_use]
    pub fn invoked(&self) -> Vec<String> {
        self.events()
            .iter()
            .filter_map(|event| event.strip_prefix("invoke ").map(str::to_owned))
            .collect()
    }
}

struct FakeBatch {
    events: Arc<Mutex<Vec<String>>>,
    answers: Arc<Mutex<VecDeque<Answer>>>,
    injected: Arc<Mutex<Vec<String>>>,
}

impl ActionBatch for FakeBatch {
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        let ids: Vec<&str> = requests
            .iter()
            .map(|request| request.interrupt_id.as_str())
            .collect();
        lock(&self.events).push(format!("prepare {}", ids.join(",")));
        Box::pin(async { Ok(()) })
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionFailure>> {
        Box::pin(async move {
            if stop.is_stopped() {
                return Err(ActionFailure::Stopped);
            }
            lock(&self.events).push(format!("invoke {}", request.interrupt_id));
            let answer = lock(&self.answers).pop_front();
            match answer {
                Some(answer) => answer(request, stop),
                None => Ok(InvokeOutcome::Completed(Value::Object(Vec::new()))),
            }
        })
    }

    fn delivered<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        let ids: Vec<&str> = requests
            .iter()
            .map(|request| request.interrupt_id.as_str())
            .collect();
        lock(&self.events).push(format!("delivered {}", ids.join(",")));
        Box::pin(async { Ok(()) })
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionFailure>> {
        lock(&self.events).push("terminate".to_owned());
        Box::pin(async { Ok(true) })
    }

    fn injected(&self) -> Vec<Secret> {
        lock(&self.injected)
            .iter()
            .map(|value| Secret::new(value.clone()))
            .collect()
    }
}

impl ActionExecutor for FakeExecutor {
    fn batch(&self, scope: BatchScope) -> Box<dyn ActionBatch> {
        lock(&self.scopes).push(scope);
        Box::new(FakeBatch {
            events: Arc::clone(&self.events),
            answers: Arc::clone(&self.answers),
            injected: Arc::clone(&self.injected),
        })
    }

    fn invoke<'a>(
        &'a self,
        _team_id: &'a str,
        _request: &'a ActionRequest,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Value, ActionFailure>> {
        Box::pin(async { Err(ActionFailure::HumanRequestRequired) })
    }
}

/// Prepares every selected file as one short text, or refuses with the failure a test set.
#[derive(Debug, Default)]
pub struct FakePreparer {
    /// The failure every preparation answers, when set.
    pub failure: Mutex<Option<crate::prepare::PrepareError>>,
}

impl crate::prepare::Preparer for FakePreparer {
    fn prepare_segment<'a>(
        &'a self,
        _team_id: &'a str,
        file_ids: &'a [String],
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Vec<crate::prepare::Prepared>, crate::prepare::PrepareError>> {
        Box::pin(async move {
            if stop.is_stopped() {
                return Err(crate::prepare::PrepareError::Stopped);
            }
            if let Some(failure) = *lock(&self.failure) {
                return Err(failure);
            }
            Ok(file_ids
                .iter()
                .map(|id| crate::prepare::Prepared {
                    file_id: id.clone(),
                    derivative: json(&format!(
                        r#"{{"id":"{id}","name":"notes.md","media_type":"text/markdown","size":5,
                        "sha256":"{}","content":{{"type":"text","text":"notes","pdf":false}}}}"#,
                        "f".repeat(64)
                    )),
                })
                .collect())
        })
    }

    fn rendition<'a>(
        &'a self,
        _team_id: &'a str,
        _file_id: &'a str,
        _page: Option<u32>,
        _preview: &'a crate::coordination::previews::Preview,
    ) -> BoxFuture<'a, Result<crate::prepare::Rendition, crate::prepare::PrepareError>> {
        Box::pin(async { Err(crate::prepare::PrepareError::Unavailable) })
    }
}

/// A password request for the Stored Input `exa-key`, its copy referencing the fake catalog.
#[must_use]
pub fn password_request() -> Value {
    use team_protocol::python::value::{int, object_of};
    let reference = |text: &str| {
        object_of(vec![
            (
                "message",
                Value::String(assistant_protocol::catalog::message_id(text)),
            ),
            ("params", Value::Object(Vec::new())),
        ])
    };
    let request = object_of(vec![
        ("kind", Value::String("input:password".to_owned())),
        ("ordinal", int(0)),
        ("title", reference(COPY[0])),
        ("description", reference(COPY[1])),
        ("label", reference(COPY[2])),
        ("required", Value::Bool(true)),
        ("placeholder", Value::Null),
        ("min_length", int(1)),
        ("max_length", int(1024)),
        ("stored_input", Value::String("exa-key".to_owned())),
    ]);
    let fingerprint = assistant_protocol::human_request::fingerprint(&request);
    let Value::Object(mut framed) = request else {
        unreachable!()
    };
    framed.push(("fingerprint".to_owned(), Value::String(fingerprint)));
    Value::Object(framed)
}

/// A fake Assistant whose `search` Action asks for the Stored Input `exa-key`.
#[must_use]
pub fn searching() -> ActiveAssistant {
    let search = ActionSpec::of(&json(
        r#"{"id":"search","input_schema":{"type":"object"},"output_schema":{"type":"object"},"integrations":[],
        "stored_inputs":["exa-key"],"human_requests":["input:password"],"input_files":[],"effect":"read_only"}"#,
    ));
    let mut active = assistant("shimpz-exa", vec![search]);
    let mut contract = (*active.binding.contract).clone();
    contract.stored_inputs = vec![team_assistant::manifest::StoredInputDeclaration {
        id: "exa-key".to_owned(),
        kind: "password".to_owned(),
        label: COPY[2].to_owned(),
        description: COPY[3].to_owned(),
        help_url: "https://dashboard.exa.ai/api-keys".to_owned(),
        host: "api.exa.ai".to_owned(),
        header: Some("x-api-key".to_owned()),
        query: None,
        scheme: None,
        hmac: None,
        routes: Value::Array(Vec::new()),
    }];
    active.binding.contract = Arc::new(contract);
    active
}

/// A fake Assistant whose `list-zones` Action needs the Cloudflare Integration.
#[must_use]
pub fn connected() -> ActiveAssistant {
    let list = ActionSpec::of(&json(
        r#"{"id":"list-zones","input_schema":{"type":"object"},"output_schema":{"type":"object"},
        "integrations":["cloudflare"],"stored_inputs":[],"human_requests":[],"input_files":[],"effect":"read_only"}"#,
    ));
    let mut active = assistant("shimpz-cloudflare", vec![list]);
    let mut contract = (*active.binding.contract).clone();
    contract.integrations = vec![team_assistant::manifest::IntegrationDeclaration {
        id: "cloudflare".to_owned(),
        provider: "cloudflare".to_owned(),
        scopes: vec!["dns.read".to_owned(), "zone.read".to_owned()],
    }];
    active.binding.contract = Arc::new(contract);
    active
}
