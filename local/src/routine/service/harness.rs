//! One Routine service over the Routine fakes, a real chat service, and real stores in a temporary directory.

use std::sync::Arc;

use strict_json::Value;
use team_action::stored_input::StoredInputStore;
use team_assistant::spec::ActionSpec;
use team_chat::progress::Reporter;
use team_core::clock::SystemClock;
use team_core::entropy::OsEntropy;
use team_core::testing::{FakeClock, MemoryAudit};
use team_inference::config::InferenceConfigStore;
use team_inference::testing::ScriptedBrain;
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::store::IntegrationStore;
use team_routine::record::{self, Lease, Routine, Run, TeamRoutines};
use team_storage::files::FileStore;

use super::{RoutineParts, RoutineService};
use crate::chat::continuation_store::ContinuationStore;
use crate::chat::limit::ChatLimitStore;
use crate::chat::ports::ActiveAssistant;
use crate::chat::service::{ChatParts, ChatService};
use crate::chat::tests::fakes::{
    FakeExecutor, FakePreparer, FakeTeam, FakeTranscripts, assistant, json,
};
use crate::chat::usage::UsageBook;
use crate::coordination::Coordination;
use crate::http::service::Problem;
use crate::routine::diagnostics::DiagnosticStore;
use crate::routine::executor::fakes::{FakeKey, FakeRoutineActions, FakeWorkloads};
use crate::routine::proposal::{ProposalBook, RoutineChat};
use crate::routine::protection::RunProtections;
use crate::routine::recorder::RecordingBook;
use crate::routine::state::none_lost;
use crate::routine::store::RoutineStore;

/// The Team every journey runs in.
pub const TEAM: &str = "team_1";
/// The Routine every journey runs.
pub const ROUTINE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
/// The Assistant every step calls.
pub const ASSISTANT: &str = "shimpz-cloudflare";

/// One reviewed Action of the fake Assistant: `effect` is `read_only` or `mutating`, with an optional verifier.
pub fn action(id: &str, effect: &str, verifier: Option<&str>) -> Value {
    let verifier =
        verifier.map_or_else(String::new, |verifier| format!(r#","verifier":{verifier}"#));
    json(&format!(
        r#"{{"id":"{id}","input_schema":{{"type":"object","properties":{{"zone":{{"type":"string"}}}},
        "additionalProperties":false}},"output_schema":{{"type":"object"}},"integrations":[],"stored_inputs":[],
        "human_requests":["approval"],"input_files":[],"effect":"{effect}"{verifier}}}"#
    ))
}

/// The verifier a mutating `apply` declares: its `check` Action, by operation id.
pub const VERIFIER: &str = r#"{"action":"check","input":{"zone":{"from":"operation_id"}},"outcome":"/outcome","result":"/result"}"#;

/// The fake Assistant running exactly these reviewed Actions, its pin covering them.
pub fn running(actions: &[Value]) -> ActiveAssistant {
    let specs = actions.iter().map(ActionSpec::of).collect();
    let mut active = assistant(ASSISTANT, specs);
    let mut pinned = (*active.pinned).clone();
    pinned.actions = actions.to_vec();
    active.pinned = Arc::new(pinned);
    active
}

/// Each distinct Action of `steps` as its confirmation permits it.
fn permitted(contracts: &team_routine::plan::Contracts, steps: &[&str]) -> Vec<Value> {
    let mut actions: Vec<&str> = steps.to_vec();
    actions.sort_unstable();
    actions.dedup();
    actions
        .iter()
        .map(|action| {
            let contract = &contracts[&(ASSISTANT.to_owned(), (*action).to_owned())];
            json(&format!(
                r#"{{"assistant":"{ASSISTANT}","action":"{action}","pin":"{}","read_only":{},"stored_inputs":[]}}"#,
                contract.pin, contract.read_only
            ))
        })
        .collect()
}

/// A confirmed continuous Routine of these steps (Action ids of `active`), due five seconds ago.
pub fn routine_of(active: &ActiveAssistant, steps: &[&str], now: i64) -> Routine {
    let contracts = crate::routine::contracts::contracts(std::slice::from_ref(active));
    let pin = |action: &str| {
        contracts[&(ASSISTANT.to_owned(), action.to_owned())]
            .pin
            .clone()
    };
    let listed: Vec<String> = steps
        .iter()
        .enumerate()
        .map(|(index, action)| {
            format!(
                r#"{{"id":"s{index}","assistant":"{ASSISTANT}","action":"{action}","pin":"{}","input":{{}}}}"#,
                pin(action)
            )
        })
        .collect();
    let plan = json(&format!(
        r#"{{"version":4,"timezone":"UTC","steps":[{}],"output":{{"mode":"show","step":"s{}"}}}}"#,
        listed.join(","),
        steps.len() - 1
    ));
    let schedule = json(r#"{"kind":"continuous","gap":3600}"#);
    let mut value = Routine::new(
        ROUTINE,
        "Zone check",
        schedule,
        Vec::new(),
        plan,
        now - 5,
        now - 5,
    );
    value.permitted = permitted(&contracts, steps);
    value.assistants = vec![(
        ASSISTANT.to_owned(),
        active.routine_scope().expect("a scope pin"),
    )];
    value.description = "Checks the zones.".to_owned();
    value.confirmation = Some(json(&format!(
        r#"{{"proposal_id":"{}","proposal_digest":"sha256:{}","principal":"{}","incarnation":"{}","confirmed_at":1}}"#,
        "c".repeat(32),
        "e".repeat(64),
        "f".repeat(32),
        "b".repeat(64)
    )));
    value
}

/// The service and the fakes behind it.
pub struct Harness {
    pub _directory: tempfile::TempDir,
    pub service: RoutineService,
    pub chat: ChatService,
    pub actions: Arc<FakeRoutineActions>,
    pub workloads: Arc<FakeWorkloads>,
    pub key: Arc<FakeKey>,
    pub team: Arc<FakeTeam>,
    pub store: Arc<RoutineStore>,
    pub coordination: Coordination,
}

fn sealed(root: &std::path::Path, name: &str) -> (std::path::PathBuf, std::path::PathBuf) {
    (
        root.join(name).join("state"),
        root.join(name).join("key/aes256.key"),
    )
}

fn routine_chat(store: &Arc<RoutineStore>, diagnostics: &Arc<DiagnosticStore>) -> RoutineChat {
    let clock: Arc<dyn team_core::clock::Clock> = Arc::new(SystemClock);
    RoutineChat {
        routines: Arc::clone(store),
        diagnostics: Arc::clone(diagnostics),
        recordings: Arc::new(RecordingBook::new(clock.clone(), Arc::new(OsEntropy))),
        proposals: Arc::new(ProposalBook::new(clock.clone())),
        lost: Arc::new(none_lost),
        clock,
        entropy: Arc::new(OsEntropy),
    }
}

/// The Team's files, Integrations, and Stored Inputs.
fn stores(root: &std::path::Path) -> (FileStore, IntegrationStore, StoredInputStore) {
    let clock = Arc::new(SystemClock);
    let files = FileStore::new(
        root.join("storage"),
        1 << 30,
        None,
        clock.clone(),
        Arc::new(OsEntropy),
    )
    .expect("files");
    let (state, key) = (
        root.join("integrations/state.json"),
        root.join("integrations-key/aes256.key"),
    );
    let integrations = IntegrationStore::new(state, key, clock.clone(), Arc::new(OsEntropy))
        .expect("integrations");
    let (state, key) = (
        root.join("stored/state.json"),
        root.join("stored-key/aes256.key"),
    );
    let stored = StoredInputStore::new(state, key, clock.clone(), Arc::new(OsEntropy))
        .expect("stored inputs");
    (files, integrations, stored)
}

fn chat(
    root: &std::path::Path,
    team: Arc<FakeTeam>,
    routine: RoutineChat,
    audit: Arc<MemoryAudit>,
) -> ChatService {
    let (files, integrations, stored) = stores(root);
    let limit = Arc::new(ChatLimitStore::new(&root.join("inference")));
    let fake_clock = Arc::new(FakeClock::at(std::time::SystemTime::now()));
    let continuations = ContinuationStore::new(
        &root.join("chat-continuations/state/continuations.json"),
        &root.join("chat-continuations/key/aes256.key"),
        fake_clock.clone(),
        Arc::new(OsEntropy),
        32,
    )
    .expect("continuations");
    let parts = ChatParts {
        space_id: "local-space".to_owned(),
        coordination: Coordination::new(limit.clone()),
        team,
        brain: Arc::new(ScriptedBrain::default()),
        actions: Arc::new(FakeExecutor::default()),
        transcripts: Arc::new(FakeTranscripts::default()),
        preparer: Arc::new(FakePreparer::default()),
        files: Arc::new(files),
        inference: Arc::new(InferenceConfigStore::new(root.join("inference"))),
        limit,
        continuations: Arc::new(continuations),
        integrations: Arc::new(integrations),
        stored_inputs: Arc::new(stored),
        integration_challenges: Arc::new(
            team_integrations::pending::default_store(Arc::new(OsEntropy)).expect("store"),
        ),
        pkce: Arc::new(PkceStore::new(PkceLimits::default(), Arc::new(OsEntropy)).expect("pkce")),
        browser: None,
        routine,
        usage: Arc::new(UsageBook::default()),
        audit,
        clock: fake_clock,
        entropy: Arc::new(OsEntropy),
    };
    ChatService::new(parts).expect("chat")
}

fn routine_stores(root: &std::path::Path) -> (Arc<RoutineStore>, Arc<DiagnosticStore>) {
    let (state, key) = sealed(root, "routines");
    let store = Arc::new(RoutineStore::new(&state, &key, Arc::new(OsEntropy)).expect("routines"));
    let (state, key) = sealed(root, "routine-diagnostics");
    let diagnostics =
        Arc::new(DiagnosticStore::new(&state, &key, Arc::new(OsEntropy)).expect("diagnostics"));
    (store, diagnostics)
}

/// The Team's state holding exactly `routine`.
fn hold(store: &RoutineStore, routine: &Routine) {
    let added = record::add_routine(&TeamRoutines::default(), routine).expect("a Routine");
    let written: Result<(), crate::routine::store::StoreError> =
        store.update(TEAM, |_state| Ok((added, ())));
    written.expect("written");
}

/// A harness whose Team runs `active`, holding `routine`.
pub fn harness(active: ActiveAssistant, routine: &Routine) -> Harness {
    let directory = tempfile::tempdir().expect("directory");
    let root = directory.path();
    let (store, diagnostics) = routine_stores(root);
    let team = Arc::new(FakeTeam::with(TEAM, vec![active]));
    let audit = Arc::new(MemoryAudit::default());
    let chat = chat(
        root,
        Arc::clone(&team),
        routine_chat(&store, &diagnostics),
        audit.clone(),
    );
    let (actions, workloads, key) = (
        Arc::new(FakeRoutineActions::default()),
        Arc::new(FakeWorkloads::default()),
        Arc::new(FakeKey::default()),
    );
    let coordination = chat.parts().coordination.clone();
    let parts = RoutineParts {
        space_id: "local-space".to_owned(),
        coordination: coordination.clone(),
        team: team.clone(),
        actions: Arc::new(Arc::clone(&actions)),
        journal: actions.journal.clone(),
        workloads: workloads.clone(),
        key: key.clone(),
        store: Arc::clone(&store),
        diagnostics,
        protections: Arc::new(RunProtections::of_boot("b".repeat(32))),
        chat: chat.clone(),
        audit: audit.clone(),
        clock: Arc::new(SystemClock),
        entropy: Arc::new(OsEntropy),
    };
    let service = RoutineService::new(parts).expect("service");
    hold(&store, routine);
    Harness {
        _directory: directory,
        service,
        chat,
        actions,
        workloads,
        key,
        team,
        store,
        coordination,
    }
}

/// A claimed run: its id and the lease its worker proves.
pub struct Claimed {
    pub run_id: String,
    pub lease: Lease,
    pub revision: i64,
    pub digest: String,
}

impl Harness {
    /// The Team's Routine state.
    pub fn state(&self) -> TeamRoutines {
        self.store.load(TEAM).expect("state")
    }

    /// The run `run_id`.
    pub fn run(&self, run_id: &str) -> Option<Run> {
        self.state()
            .runs
            .into_iter()
            .find(|item| item.run_id == run_id)
    }

    /// Claims the due run.
    pub async fn claim(&self) -> Option<Claimed> {
        let claimed = self
            .service
            .claim_routine_run(true)
            .await
            .expect("a claim")?;
        let text = |name: &str| match claimed.get(name) {
            Some(Value::String(text)) => text.clone(),
            _ => panic!("{name}"),
        };
        let revision = match claimed.get("revision") {
            Some(Value::Number(strict_json::Number::Int(revision))) => *revision,
            _ => panic!("revision"),
        };
        Some(Claimed {
            run_id: text("run_id"),
            lease: record::lease_of(&text("lease_token"), &"e".repeat(64)),
            revision,
            digest: text("plan_digest"),
        })
    }

    /// Runs one segment of a claimed run.
    pub async fn segment(&self, claimed: &Claimed) -> Result<String, Problem> {
        let answer = self
            .service
            .run_routine(
                TEAM,
                &claimed.run_id,
                claimed.lease.clone(),
                (claimed.revision, claimed.digest.clone()),
                &Reporter::silent(),
            )
            .await?;
        match answer.get("status") {
            Some(Value::String(status)) => Ok(status.clone()),
            _ => panic!("a status"),
        }
    }

    /// Claims the due run and runs its segment.
    pub async fn run_due(&self) -> (String, String) {
        let claimed = self.claim().await.expect("a due run");
        let status = self.segment(&claimed).await.expect("a segment");
        (claimed.run_id, status)
    }

    /// The outcomes of every notice of the Team, in order.
    pub fn notices(&self) -> Vec<String> {
        self.state()
            .notices
            .iter()
            .map(|item| item.outcome.clone())
            .collect()
    }

    /// The Routine.
    pub fn routine(&self) -> Routine {
        record::routine(&self.state(), ROUTINE)
            .expect("the Routine")
            .clone()
    }
}
