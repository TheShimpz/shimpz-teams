//! The Local chat-turn service (`Group::Chat`, `local/chat/service.py`): turns on each Team's one execution slot, their
//! pauses and answers, Stop, the Space chat limit, and capability planning and intent routing.
//!
//! A turn and every answer to it stream metadata-only progress and then one terminal record. The work runs on its own
//! task once admitted, so a person who leaves the page never changes it (ADR-0119).

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_action::executor::{ActionExecutor, ActionFailure};
use team_action::stored_input::StoredInputStore;
use team_chat::orchestrator::ChatError;
use team_chat::progress::{Reporter, Sink};
use team_core::audit::AuditSink;
use team_core::clock::Clock;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_inference::brain::BrainClient;
use team_inference::config::InferenceConfigStore;
use team_integrations::challenge::ChallengeError;
use team_integrations::pending::IntegrationChallengeStore;
use team_integrations::pkce::PkceStore;
use team_integrations::store::IntegrationStore;
use team_protocol::python::value::object_of;
use team_storage::files::FileStore;
use tokio::sync::mpsc;

use super::continuation_store::ContinuationStore;
use super::limit::ChatLimitStore;
use super::pending::PendingLocalChat;
use super::ports::TeamView;
use super::problem;
use crate::browser::service::BrowserService;
use crate::coordination::Coordination;
use crate::coordination::executions::{ExecutionKind, Refusal};
use crate::http::operation::Operation;
use crate::http::server::usage::ModelUsageSink;
use crate::http::service::{Answer, Call, Problem, Service, StreamRecord};
use crate::install::action_problem;
use crate::prepare::Preparer;

/// One human challenge per Team, with the paused turn it resumes.
pub type HumanChallenges = team_action::challenges::HumanChallengeStore<PendingLocalChat>;
/// One Integration challenge per Team, with the paused turn it resumes.
pub type IntegrationChallenges = IntegrationChallengeStore<PendingLocalChat>;

/// Everything the chat service runs with.
#[derive(Clone)]
pub struct ChatParts {
    /// The Space's id, which every Brain thread names.
    pub space_id: String,
    /// The one coordination authority.
    pub coordination: Coordination,
    /// The Team as the controller holds it.
    pub team: Arc<dyn TeamView>,
    /// The Brain runtime.
    pub brain: Arc<dyn BrainClient>,
    /// The Action executor.
    pub actions: Arc<dyn ActionExecutor>,
    /// Attachment preparation.
    pub preparer: Arc<dyn Preparer>,
    /// The Team files.
    pub files: Arc<FileStore>,
    /// Inference settings and learned memory.
    pub inference: Arc<InferenceConfigStore>,
    /// The Space chat limit.
    pub limit: Arc<ChatLimitStore>,
    /// Encrypted paused turns.
    pub continuations: Arc<ContinuationStore>,
    /// Integration credentials.
    pub integrations: Arc<IntegrationStore>,
    /// Stored Inputs.
    pub stored_inputs: Arc<StoredInputStore>,
    /// The Integration challenges, shared with the Integrations group.
    pub integration_challenges: Arc<IntegrationChallenges>,
    /// OAuth PKCE state, shared with the Integrations group.
    pub pkce: Arc<PkceStore>,
    /// The Browser, where this image runs one.
    pub browser: Option<Arc<BrowserService>>,
    /// The Routine state, recording spans, and cards a chat turn about a Routine runs with (ADR-0101).
    pub routine: crate::routine::proposal::RoutineChat,
    /// Each request's Brain usage.
    pub usage: Arc<dyn ModelUsageSink>,
    /// The audit journal.
    pub audit: Arc<dyn AuditSink>,
    /// The wall clock.
    pub clock: Arc<dyn Clock>,
    /// The random source.
    pub entropy: Arc<dyn Entropy>,
}

/// The chat service's state, shared by every task it runs.
pub struct Inner {
    pub(super) parts: ChatParts,
    pub(super) human: HumanChallenges,
    /// The token of each Team whose turn has an Action executing now, so Stop reports it.
    pub(super) executing: Mutex<HashMap<String, String>>,
}

/// The chat service; clones share one state.
#[derive(Clone)]
pub struct ChatService(Arc<Inner>);

impl std::ops::Deref for ChatService {
    type Target = Inner;

    fn deref(&self) -> &Inner {
        &self.0
    }
}

impl std::fmt::Debug for ChatService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ChatService")
    }
}

/// A turn admitted to its Team's execution slot.
pub(super) struct Admitted {
    /// The slot.
    pub execution: crate::coordination::executions::Execution,
}

/// The public problem of a turn that ended without an outcome.
#[must_use]
pub fn chat_problem(error: ChatError) -> Problem {
    match error {
        ChatError::Stopped | ChatError::Action(ActionFailure::Stopped) => problem::chat_stopped(),
        ChatError::Orchestration(_) => problem::brain_turn_failed(),
        ChatError::Brain(_) => problem::brain_runtime_failed(),
        ChatError::ContextChanged => problem::team_context_changed(),
        ChatError::Action(failure) => action_problem(failure),
        ChatError::Refused(refusal) => Problem::new(refusal.status, refusal.message, refusal.code),
    }
}

/// A chat error carrying `problem` exactly.
#[must_use]
pub fn refused(problem: &Problem) -> ChatError {
    let message: &'static str = match &problem.message {
        std::borrow::Cow::Borrowed(message) => message,
        std::borrow::Cow::Owned(_) => "internal error",
    };
    ChatError::Refused(team_chat::orchestrator::Refusal {
        status: problem.status,
        message,
        code: problem.code.unwrap_or("internal-error"),
    })
}

/// The terminal answer of a failed streamed operation.
fn failure_body(problem: &Problem) -> Value {
    let mut members = vec![("error", Value::String(problem.message.clone().into_owned()))];
    if let Some(code) = problem.code {
        members.push(("code", Value::String(code.to_owned())));
    }
    object_of(members)
}

/// The status a streamed answer terminates with: `428` for a turn that paused for a person or an Integration.
fn answered_status(body: &Value) -> u16 {
    match body.get("status") {
        Some(Value::String(status))
            if status == "human-required" || status == "integrations-required" =>
        {
            428
        }
        _ => 200,
    }
}

/// Streams one admitted operation's progress and then its terminal record; the work runs on its own task.
pub fn stream<F, W>(work: W) -> Answer
where
    W: FnOnce(Reporter) -> F,
    F: std::future::Future<Output = Result<Value, Problem>> + Send + 'static,
{
    let capacity = usize::try_from(team_protocol::progress::MAX_EVENTS).unwrap_or(2048) + 1;
    let (sender, receiver) = mpsc::channel(capacity);
    let progress = sender.clone();
    let sink: Sink = Arc::new(move |event| {
        let _ = progress.try_send(StreamRecord::Progress(event));
    });
    let future = work(Reporter::new(sink));
    tokio::spawn(async move {
        let record = match future.await {
            Ok(body) => StreamRecord::Terminal {
                status: answered_status(&body),
                body,
            },
            Err(problem) => StreamRecord::Terminal {
                status: problem.status,
                body: failure_body(&problem),
            },
        };
        // A consumer that left never changes the admitted operation.
        let _ = sender.send(record).await;
    });
    Answer::Stream(receiver)
}

impl ChatService {
    /// The service over `parts`.
    ///
    /// # Errors
    /// [`ChallengeError`] when the human challenge store cannot be built.
    pub fn new(parts: ChatParts) -> Result<Self, ChallengeError> {
        let human = team_action::challenges::store(Arc::clone(&parts.entropy))?;
        Ok(Self(Arc::new(Inner {
            parts,
            human,
            executing: Mutex::new(HashMap::new()),
        })))
    }

    /// The parts it runs with.
    #[must_use]
    pub fn parts(&self) -> &ChatParts {
        &self.parts
    }

    /// The human challenges.
    #[must_use]
    pub fn human_challenges(&self) -> &HumanChallenges {
        &self.human
    }

    pub(super) fn executing(&self) -> MutexGuard<'_, HashMap<String, String>> {
        self.executing
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// Admits one chat turn to the Team's slot, refusing while the chat limit is unreadable.
    ///
    /// # Errors
    /// The slot's refusal as its public problem.
    pub(super) fn admit(&self, team_id: &str) -> Result<Admitted, Problem> {
        self.parts
            .limit
            .state()
            .map_err(|_| problem::chat_limit_unavailable())?;
        let token =
            token_hex(self.parts.entropy.as_ref(), 16).map_err(|_| problem::internal_error())?;
        let execution = self
            .parts
            .coordination
            .executions
            .admit(team_id, ExecutionKind::Chat, token)
            .map_err(|refusal| match refusal {
                Refusal::ChatActive => problem::chat_active(),
                Refusal::RoutineActive => problem::routine_active(),
                Refusal::SpaceResetting => problem::space_resetting(),
                Refusal::SpaceBusy => problem::space_busy(),
            })?;
        Ok(Admitted { execution })
    }

    fn limit(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let int = |limit: i64| object_of(vec![("limit", team_protocol::python::value::int(limit))]);
        if call.operation() == Operation::ChatLimitRead {
            let limit = self
                .parts
                .limit
                .state()
                .map_err(|_| problem::chat_limit_unavailable())?;
            return Ok(int(i64::try_from(limit).unwrap_or(0)));
        }
        let body = match &call.body {
            crate::http::service::Body::Json(body) => {
                team_protocol::payload::canonical_chat_limit(body)
            }
            _ => None,
        };
        let limit = body
            .and_then(|body| match body.get("limit") {
                Some(Value::Number(strict_json::Number::Int(limit))) => u8::try_from(*limit).ok(),
                _ => None,
            })
            .ok_or(Problem::new(
                422,
                "chat limit must be exactly {limit} from 1 to 2",
                "invalid-body",
            ))?;
        let executions = &self.parts.coordination.executions;
        let saved = executions
            .save_limit(limit)
            .map_err(|_| problem::chat_limit_unavailable())?;
        Ok(int(i64::from(saved)))
    }

    /// The JSON operations.
    async fn json(&self, call: &Call<'_>) -> Result<Value, Problem> {
        match call.operation() {
            Operation::ChatLimitRead | Operation::ChatLimitSave => self.limit(call),
            Operation::ChatHumanPending => self.pending_human(call).await,
            Operation::ChatHumanOpen => self.open_human(call).await,
            Operation::ChatIntegrationPending => self.pending_integrations(call).await,
            Operation::ChatStop => self.stop(call).await,
            Operation::ChatCapabilityPlan => self.capability_plan(call).await,
            Operation::ChatIntentRoute => self.intent_route(call).await,
            _ => Err(problem::internal_error()),
        }
    }

    async fn answer(&self, call: Call<'_>) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::Chat => self.start(&call),
            Operation::ChatHumanSubmit => self.submit_human(&call),
            Operation::ChatIntegrationSubmit => self.submit_integrations(&call),
            _ => self
                .json(&call)
                .await
                .map(|body| Answer::Json { status: 200, body }),
        }
    }
}

impl Service for ChatService {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move { self.answer(call).await })
    }
}
