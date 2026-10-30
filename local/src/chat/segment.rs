//! One segment of a chat turn (`local/chat/segment.py`): the Team as it is now, what the Brain sees of it, and the turn
//! engine run against it until the turn completes or pauses.

use std::sync::{Arc, Mutex, PoisonError};

use strict_json::Value;
use team_action::challenges::{HumanRequirement, input_projection, render_copy};
use team_action::executor::{ActionBatch, ActionFailure, ActionRequest, BatchScope, InvokeOutcome};
use team_action::human::validate_request;
use team_assistant::spec::Direction;
use team_chat::orchestrator::{
    ChatContinuation, ChatError, Outcome, Refusal, Strategy, continue_after_pause, run_until_pause,
};
use team_chat::progress::{Phase, Reporter};
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_inference::brain::ModelCredential;
use team_inference::config::{ConfigError, InferenceConfig};
use team_inference::runtime::{
    ActionRequest as BrainRequest, Brain, BrowserRequest, ConversationEntry, Runtime,
    RuntimeAction, RuntimeContext,
};
use team_integrations::flow::{Requested, requirements_for_batch};
use team_integrations::pending::IntegrationRequirement;
use team_storage::files::StorageErrorKind;

use super::pending::Identity;
use super::ports::ActiveAssistant;
use super::problem;
use super::service::{ChatService, chat_problem, refused};
use super::usage::RequestUsage;
use crate::http::service::Problem;
use crate::prepare::PrepareError;

/// The model a chat turn about a Routine uses on each provider, with the Team's own key.
pub const ROUTINE_MODELS: [(&str, &str); 2] = [
    ("openai", "gpt-6.1-sol"),
    ("anthropic", "claude-sonnet-5-5"),
];

/// One segment's request.
pub struct SegmentRequest {
    /// The Team.
    pub team_id: String,
    /// The execution's token.
    pub token: String,
    /// The execution's Stop.
    pub stop: Stop,
    /// The files the turn selected.
    pub file_ids: Vec<String>,
    /// The Assistants the turn selected.
    pub assistant_ids: Vec<String>,
    /// The request's provider and key.
    pub credential: (String, Secret),
    /// A new turn's message; `None` for a continuation.
    pub message: Option<String>,
    /// A new turn's committed history window.
    pub conversation: Vec<ConversationEntry>,
    /// The interface language the turn's start pinned.
    pub locale: Option<String>,
    /// The paused turn this segment continues.
    pub continuation: Option<ChatContinuation>,
    /// The responses a person gave exactly that paused turn's Actions; only this segment's batch replays them.
    pub transcripts: Vec<team_action::human::ActionTranscript>,
    /// The identity the paused turn bound.
    pub expected: Option<Identity>,
    /// The turn's progress.
    pub progress: Reporter,
    /// The model the turn's start pinned.
    pub model: Option<String>,
    /// The request's Brain usage.
    pub usage: Arc<RequestUsage>,
    /// The verified principal of the request, for the Browser's audit.
    pub actor: crate::audit::Principal,
    /// The turn's send in the Team's recording span (ADR-0101).
    pub recording: Option<String>,
    /// The Routine a new turn's person asked to refine from its panel, by id (ADR-0122).
    pub refine: Option<String>,
}

/// One segment's outcome and what the turn's next request binds.
pub struct SegmentResult {
    /// The Team's current display name.
    pub team_name: String,
    /// The Team the segment ran against.
    pub identity: Identity,
    /// How the segment ended.
    pub outcome: Outcome,
    /// The Integrations a paused batch waits for.
    pub integrations: Vec<IntegrationRequirement>,
    /// The person's request an Action paused for, its copy rendered and its purpose asked.
    pub human: Option<HumanRequirement>,
    /// The Routine scope pin of each Assistant the Brain saw.
    pub contracts: Vec<(String, String)>,
    /// The id of the Action batch a human request paused.
    pub paused_batch: Option<String>,
    /// The model the segment ran on.
    pub model: String,
    /// The stored intent Team recorded again from a bound answer, with no model.
    pub answered: Option<crate::routine::recorder::Intent>,
}

/// The Team as one segment runs against it.
#[derive(Clone)]
pub struct Setup {
    /// The Team's creation name.
    pub team_name: String,
    /// Its network incarnation.
    pub network_id: String,
    /// The selected running Assistants.
    pub assistants: Vec<ActiveAssistant>,
    /// The selected files' metadata.
    pub files: Vec<Value>,
    /// The Team's model configuration.
    pub config: InferenceConfig,
}

impl Setup {
    /// The identity a paused turn binds.
    #[must_use]
    pub fn identity(&self) -> Identity {
        Identity::Chat {
            team_name: self.team_name.clone(),
            network_id: self.network_id.clone(),
            assistants: self
                .assistants
                .iter()
                .map(|active| {
                    (
                        active.id().to_owned(),
                        active.binding.image.clone(),
                        active.binding.container.clone(),
                    )
                })
                .collect(),
            files: self.files.clone(),
            inference: (self.config.provider.clone(), self.config.model.clone()),
        }
    }
}

/// `local:{space}:{team}:{network}:default`, the Brain thread of one Team incarnation.
///
/// # Errors
/// `409 ownership-conflict` for an identity outside its persisted contract.
pub fn brain_thread_id(space_id: &str, team_id: &str, network_id: &str) -> Result<String, Problem> {
    let space = !space_id.is_empty()
        && space_id.len() <= 48
        && space_id.split('-').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        });
    let network = (12..=64).contains(&network_id.len())
        && network_id
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
    let team = team_protocol::identifiers::TEAM_ID_RE.is_full(team_id);
    if space && network && team {
        Ok(format!("local:{space_id}:{team_id}:{network_id}:default"))
    } else {
        Err(Problem::new(
            409,
            "Team identity failed its persisted contract",
            "ownership-conflict",
        ))
    }
}

/// One Routine run's journal thread label, beside the Team's conversation in the same network generation.
///
/// # Errors
/// `409 ownership-conflict` for an identity outside its persisted contract.
pub fn routine_thread_id(
    space_id: &str,
    team_id: &str,
    network_id: &str,
    run_id: &str,
) -> Result<String, Problem> {
    let conversation = brain_thread_id(space_id, team_id, network_id)?;
    let run = run_id.len() == 32
        && run_id
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    if !run {
        return Err(Problem::new(
            409,
            "Team identity failed its persisted contract",
            "ownership-conflict",
        ));
    }
    Ok(format!(
        "{}:routine-{run_id}",
        conversation.trim_end_matches(":default")
    ))
}

fn prepare_problem(error: PrepareError) -> Problem {
    match error {
        PrepareError::Stopped => problem::chat_stopped(),
        PrepareError::Limit(limit) => Problem::new(
            422,
            "the selected files exceed this message's limits",
            limit.code(),
        ),
        PrepareError::Changed => problem::team_context_changed(),
        PrepareError::NotFound => problem::selected_file_not_found(),
        PrepareError::Unreadable | PrepareError::Unavailable | PrepareError::Busy => Problem::new(
            503,
            "attached files could not be prepared",
            "attachments-unavailable",
        ),
    }
}

impl ChatService {
    /// The Team's model configuration, refused when it is absent or bound to another provider than the turn's.
    pub(super) async fn turn_inference(
        &self,
        team_id: &str,
        provider: &str,
    ) -> Result<InferenceConfig, Problem> {
        let store = Arc::clone(&self.parts.inference);
        let team = team_id.to_owned();
        let config = tokio::task::spawn_blocking(move || store.load(&team))
            .await
            .map_err(|_| problem::internal_error())?
            .map_err(|_: ConfigError| problem::inference_not_configured())?;
        if config.provider != provider {
            return Err(problem::inference_provider_mismatch());
        }
        Ok(config)
    }

    async fn file_metadata(
        &self,
        team_id: &str,
        file_ids: &[String],
    ) -> Result<Vec<Value>, Problem> {
        if file_ids.len() > team_protocol::payload::MAX_CHAT_FILES {
            return Err(Problem::new(
                422,
                "files must contain at most 8 opaque ids",
                "invalid-files",
            ));
        }
        if file_ids.is_empty() {
            return Ok(Vec::new());
        }
        let files = Arc::clone(&self.parts.files);
        let (team, ids) = (team_id.to_owned(), file_ids.to_vec());
        let read = tokio::task::spawn_blocking(move || {
            let ids: Vec<&str> = ids.iter().map(String::as_str).collect();
            files.metadata(&team, &ids, None)
        })
        .await
        .map_err(|_| problem::internal_error())?;
        match read {
            Ok(rows) => Ok(rows
                .iter()
                .map(team_storage::files::FileMetadata::to_value)
                .collect()),
            Err(error) if error.kind == StorageErrorKind::NotFound => {
                Err(problem::selected_file_not_found())
            }
            Err(error) if error.kind == StorageErrorKind::Input => {
                Err(Problem::new(422, error.message, "invalid-files"))
            }
            Err(error) => Err(crate::prepare::files::storage_problem(&error)),
        }
    }

    /// The Team as a segment runs against it: its name, network, the selected Assistants, files, and model.
    pub(super) async fn setup(
        &self,
        team_id: &str,
        file_ids: &[String],
        provider: &str,
        assistant_ids: &[String],
    ) -> Result<Setup, Problem> {
        let _team = self.parts.coordination.locks.team(team_id).await;
        self.setup_unlocked(team_id, file_ids, provider, assistant_ids)
            .await
    }

    /// [`ChatService::setup`] for a caller that already holds the Team's stripe.
    pub(super) async fn setup_unlocked(
        &self,
        team_id: &str,
        file_ids: &[String],
        provider: &str,
        assistant_ids: &[String],
    ) -> Result<Setup, Problem> {
        let scope = self.parts.team.scope(team_id, true).await?;
        let assistants = assistant_ids
            .iter()
            .map(|id| scope.assistant(id).cloned())
            .collect::<Option<Vec<_>>>()
            .ok_or(problem::assistant_unavailable())?;
        let files = self.file_metadata(team_id, file_ids).await?;
        let config = self.turn_inference(team_id, provider).await?;
        Ok(Setup {
            team_name: scope.team_name,
            network_id: scope.network_id,
            assistants,
            files,
            config,
        })
    }

    async fn knowledge(&self, team_id: &str) -> Result<Vec<Value>, Problem> {
        let store = Arc::clone(&self.parts.inference);
        let team = team_id.to_owned();
        tokio::task::spawn_blocking(move || store.load_knowledge(&team))
            .await
            .map_err(|_| problem::internal_error())?
            .map_err(|_| super::inference::store_failed())
    }

    async fn attachments(&self, request: &SegmentRequest) -> Result<Vec<Value>, Problem> {
        if request.file_ids.is_empty() {
            return Ok(Vec::new());
        }
        let prepared = self
            .parts
            .preparer
            .prepare_segment(&request.team_id, &request.file_ids, &request.stop)
            .await
            .map_err(prepare_problem)?;
        if request.stop.is_stopped() {
            return Err(problem::chat_stopped());
        }
        Ok(prepared.into_iter().map(|item| item.derivative).collect())
    }

    /// What the Brain sees in this segment: Assistants, knowledge, and the message's prepared files.
    async fn context(
        &self,
        request: &SegmentRequest,
        setup: &Setup,
    ) -> Result<RuntimeContext, Problem> {
        let display = self
            .parts
            .team
            .display_name(&request.team_id, &setup.network_id)?;
        let thread_id = brain_thread_id(&self.parts.space_id, &request.team_id, &setup.network_id)?;
        let memories = self.knowledge(&request.team_id).await?;
        let attachments = self.attachments(request).await?;
        let routine = self.routine_view(request).await?;
        let model = Self::turn_model(request, &setup.config.model, routine.span_mode);
        let browser = setup.files.is_empty()
            && !routine.mode
            && self
                .parts
                .browser
                .as_ref()
                .is_some_and(|browser| browser.available());
        Ok(RuntimeContext {
            thread_id,
            team_name: display.unwrap_or_else(|| setup.team_name.clone()),
            assistants: setup
                .assistants
                .iter()
                .map(ActiveAssistant::runtime)
                .collect(),
            credential: ModelCredential {
                provider: request.credential.0.clone(),
                model,
                api_key: request.credential.1.clone(),
            },
            memories: Some(memories),
            routines: routine.routines,
            routine_question: routine.question,
            routine_mode: routine.mode,
            routine_rerun: routine.rerun,
            routine_schedule: routine.schedule,
            routine_output: routine.output,
            routine_settings_answer: routine.settings_answer,
            routine_selected: routine.selected,
            locale: request.locale.clone(),
            turn_clock: routine.turn_clock,
            attachments,
            browser,
        })
    }
}

/// A durable batch that records which Team's turn has an Action executing, so Stop reports it.
struct Tracked {
    inner: Box<dyn ActionBatch>,
    service: ChatService,
    team_id: String,
    token: String,
    /// The send a recording turn keeps each successful call in, with the Assistants it runs against.
    recording: Option<(String, Vec<ActiveAssistant>)>,
}

impl Tracked {
    /// Protects every value the executor injected into a recording send's call (its Integration tokens and Stored
    /// Input values) before anything of the call is kept.
    fn protect_injected(&self) {
        let Some((recording, _)) = &self.recording else {
            return;
        };
        let values: Vec<String> = self
            .inner
            .injected()
            .iter()
            .map(|value| value.expose().to_owned())
            .collect();
        if values.is_empty() {
            return;
        }
        self.service
            .parts
            .routine
            .recordings
            .protect(&self.team_id, recording, &values);
    }

    /// Keeps a recording send's successful call as an occurrence.
    fn record(&self, request: &ActionRequest, outcome: &Result<InvokeOutcome, ActionFailure>) {
        let (Some((recording, assistants)), Ok(InvokeOutcome::Completed(result))) =
            (&self.recording, outcome)
        else {
            return;
        };
        if let Some(active) = assistants
            .iter()
            .find(|active| active.id() == request.assistant_id)
        {
            let book = &self.service.parts.routine.recordings;
            crate::routine::recorder::recorded(
                book,
                (&self.team_id, recording),
                active,
                request,
                result,
            );
        }
    }
}

impl ActionBatch for Tracked {
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        self.inner.prepare(requests)
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionFailure>> {
        Box::pin(async move {
            self.service
                .executing()
                .insert(self.team_id.clone(), self.token.clone());
            let outcome = self.inner.invoke(request, stop).await;
            self.protect_injected();
            self.record(request, &outcome);
            let mut executing = self.service.executing();
            if executing.get(&self.team_id) == Some(&self.token) {
                executing.remove(&self.team_id);
            }
            outcome
        })
    }

    fn delivered<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        self.inner.delivered(requests)
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionFailure>> {
        self.inner.terminate()
    }

    fn injected(&self) -> Vec<Secret> {
        self.inner.injected()
    }
}

/// The controller adapters one chat segment runs the turn engine with.
struct TurnStrategy<'a> {
    service: &'a ChatService,
    request: &'a SegmentRequest,
    setup: Setup,
    integrations: Mutex<Vec<IntegrationRequirement>>,
}

fn invalid_input() -> ChatError {
    ChatError::Refused(Refusal {
        status: 422,
        message: "Action payload does not match its reviewed schema",
        code: "invalid-action-input",
    })
}

impl Strategy for TurnStrategy<'_> {
    fn stop(&self) -> &Stop {
        &self.request.stop
    }

    fn validate_context(&self) -> BoxFuture<'_, Result<(), ChatError>> {
        Box::pin(async move {
            let request = self.request;
            let current = self
                .service
                .setup(
                    &request.team_id,
                    &request.file_ids,
                    &request.credential.0,
                    &request.assistant_ids,
                )
                .await
                .map_err(|problem| refused(&problem))?;
            if current.identity() == self.setup.identity() {
                Ok(())
            } else {
                Err(ChatError::ContextChanged)
            }
        })
    }

    fn validate_action(
        &self,
        assistant_id: &str,
        action: &RuntimeAction,
        input: &Value,
    ) -> Result<Value, ChatError> {
        let active = self
            .setup
            .assistants
            .iter()
            .find(|active| active.id() == assistant_id)
            .ok_or_else(|| refused(&problem::assistant_unavailable()))?;
        let spec = active.action(&action.id).ok_or_else(invalid_input)?;
        spec.validate(Direction::Input, input)
            .map_err(|_| invalid_input())?;
        Ok(input.clone())
    }

    fn pause_before_batch<'a>(
        &'a self,
        batch: &'a [BrainRequest],
    ) -> BoxFuture<'a, Result<bool, ChatError>> {
        Box::pin(async move {
            let views: Vec<_> = self
                .setup
                .assistants
                .iter()
                .map(ActiveAssistant::view)
                .collect();
            let requested: Vec<Requested<'_>> = batch
                .iter()
                .map(|request| Requested {
                    assistant_id: &request.assistant_id,
                    action: &request.action,
                })
                .collect();
            let missing = requirements_for_batch(
                &self.request.team_id,
                &views,
                &requested,
                &self.service.parts.integrations,
            )
            .map_err(|_| refused(&problem::integration_contract_unavailable()))?;
            let pause = !missing.is_empty();
            *self
                .integrations
                .lock()
                .unwrap_or_else(PoisonError::into_inner) = missing;
            Ok(pause)
        })
    }

    fn run_browser<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        request: &'a BrowserRequest,
    ) -> Option<BoxFuture<'a, (Value, Value)>> {
        let browser = self.service.parts.browser.clone()?;
        Some(Box::pin(async move {
            let turn = self.service.browser_turn(self.request, &self.setup);
            browser.run(&turn, &request.objective).await
        }))
    }

    fn shown_results(&self, results: Value) -> Value {
        let Some(recording) = self.request.recording.as_deref() else {
            return results;
        };
        let span = self
            .service
            .parts
            .routine
            .recordings
            .get(&self.request.team_id, Some(recording));
        crate::routine::recorder::labeled(span.as_ref(), results)
    }

    fn progress(&self) -> &Reporter {
        &self.request.progress
    }
}

/// The paused request admitted again against its Action's reviewed declarations and its binding's catalog.
pub(crate) fn admitted_request<'s>(
    setup: &'s Setup,
    paused: &team_chat::orchestrator::ChatHumanSuspension,
) -> Result<
    (
        &'s ActiveAssistant,
        &'s team_assistant::spec::ActionSpec,
        team_action::human::HumanRequest,
    ),
    ChatError,
> {
    let changed = ChatError::Orchestration("Action human request contract changed");
    let action = &paused.action;
    let active = setup
        .assistants
        .iter()
        .find(|active| active.id() == action.assistant_id)
        .ok_or_else(|| refused(&problem::assistant_unavailable()))?;
    let spec = active.action(&action.action).ok_or(changed)?;
    let contract = &active.binding.contract.machine_contract;
    let messages = contract.get("messages").cloned().unwrap_or(Value::Null);
    let request = validate_request(
        &paused.request,
        &spec.human_requests,
        &spec.stored_inputs,
        &messages,
    )
    .map_err(|_| changed)?;
    Ok((active, spec, request))
}

/// The selected file an authorization discloses, and the projection of the input its card shows.
pub(crate) fn presentation(
    spec: &team_assistant::spec::ActionSpec,
    action: &BrainRequest,
    context: &RuntimeContext,
    kind: &str,
) -> Result<(Option<Value>, Option<Value>), ChatError> {
    let selected = team_action::files::selected(&context.attachments);
    let file = team_action::files::disclosure(&spec.input_files, &action.input, &selected, kind)
        .map_err(|_| ChatError::Orchestration("Action file is unavailable"))?;
    if !team_action::human::authorizes(kind) {
        return Ok((file, None));
    }
    let shown = input_projection(&action.input)
        .map_err(|_| ChatError::Orchestration("Action input cannot be shown"))?;
    Ok((file, Some(shown)))
}

impl ChatService {
    /// Renders the paused request in the turn's language and asks the Brain why the task needs it (ADR-0090).
    async fn requirement(
        &self,
        strategy: &TurnStrategy<'_>,
        context: &RuntimeContext,
        paused: &team_chat::orchestrator::ChatHumanSuspension,
    ) -> Result<HumanRequirement, ChatError> {
        let action = &paused.action;
        let (active, spec, request) = admitted_request(&strategy.setup, paused)?;
        let help = super::answer::declared_help(&request, &active.binding.contract.stored_inputs);
        let locale = context.locale.clone().unwrap_or_else(|| "en".to_owned());
        let help_text = help.as_ref().map(|(text, _)| text.as_str());
        let copy = render_copy(&request, &active.pack, &locale, help_text)
            .map_err(|_| ChatError::Orchestration("Action human request copy is unavailable"))?;
        let (file, input) = presentation(spec, action, context, &request.kind)?;
        let requirement = HumanRequirement {
            assistant_id: active.id().to_owned(),
            assistant_name: active.binding.name.clone(),
            action_id: action.action.clone(),
            action_summary: spec.summary.clone(),
            interrupt_id: action.interrupt_id.clone(),
            request,
            assistant_version: active.binding.version.clone(),
            copy,
            help_url: help.as_ref().map(|(_, url)| url.clone()),
            help_text: help.map(|(text, _)| text),
            purpose: None,
            purpose_locale: None,
            file,
            input,
        };
        self.with_purpose(strategy, context, requirement, action)
            .await
    }

    /// A turn without a concrete interface language asks for no purpose; Stop and a changed Team still end the turn
    /// after the call, before the challenge exists.
    async fn with_purpose(
        &self,
        strategy: &TurnStrategy<'_>,
        context: &RuntimeContext,
        mut requirement: HumanRequirement,
        action: &BrainRequest,
    ) -> Result<HumanRequirement, ChatError> {
        let Some(locale) = context.locale.clone() else {
            return Ok(requirement);
        };
        let brain = Brain {
            client: self.parts.brain.as_ref(),
            usage: strategy.request.usage.as_ref(),
        };
        let purpose = brain
            .purpose(
                context,
                action,
                &requirement.assistant_name,
                &requirement.action_summary,
                &strategy.request.stop,
            )
            .await;
        if strategy.cancelled() {
            return Err(ChatError::Stopped);
        }
        strategy.validate_context().await?;
        requirement.purpose_locale = purpose.as_ref().map(|_| locale);
        requirement.purpose = purpose;
        Ok(requirement)
    }

    /// The Team as the segment runs against it, refused when it is not the one a paused turn bound, and what the
    /// Brain sees of it.
    async fn prepared(&self, request: &SegmentRequest) -> Result<(Setup, RuntimeContext), Problem> {
        let setup = {
            let _span = request.progress.span(Phase::TeamContext, None);
            let (team, provider) = (&request.team_id, &request.credential.0);
            self.setup(team, &request.file_ids, provider, &request.assistant_ids)
                .await?
        };
        if request
            .expected
            .as_ref()
            .is_some_and(|expected| *expected != setup.identity())
        {
            return Err(problem::team_context_changed());
        }
        let context = self.context(request, &setup).await?;
        Ok((setup, context))
    }

    /// The segment's durable batch, recording each successful call of a recording send.
    fn tracked(&self, request: &SegmentRequest, setup: &Setup, scope: BatchScope) -> Tracked {
        Tracked {
            inner: self.parts.actions.batch(scope),
            service: self.clone(),
            team_id: request.team_id.clone(),
            token: request.token.clone(),
            recording: request
                .recording
                .clone()
                .map(|recording| (recording, setup.assistants.clone())),
        }
    }

    /// Runs or resumes one segment of a chat turn with its durable Action hooks.
    ///
    /// # Errors
    /// The public problem of a turn that ended without an outcome; the batch is ended first.
    pub(super) async fn run_segment(
        &self,
        request: &SegmentRequest,
    ) -> Result<SegmentResult, Problem> {
        let (setup, context) = self.prepared(request).await?;
        let scope = BatchScope {
            team_id: request.team_id.clone(),
            generation: setup.network_id.clone(),
            thread_id: context.thread_id.clone(),
            routine: false,
            transcripts: request.transcripts.clone(),
        };
        let mut batch = self.tracked(request, &setup, scope);
        let strategy = TurnStrategy {
            service: self,
            request,
            setup: setup.clone(),
            integrations: Mutex::new(Vec::new()),
        };
        let answered = self.answered_intent(request);
        let outcome = match self
            .drive(&strategy, &context, &mut batch, answered.is_some())
            .await
        {
            Ok(outcome) => outcome,
            Err(error) => {
                let ended = batch.terminate().await;
                return Err(if ended.is_err() {
                    problem::action_state_unavailable()
                } else {
                    chat_problem(error)
                });
            }
        };
        let human = match &outcome {
            Outcome::Human(paused) => Some(
                self.requirement(&strategy, &context, paused)
                    .await
                    .map_err(chat_problem)?,
            ),
            _ => None,
        };
        let integrations = std::mem::take(
            &mut *strategy
                .integrations
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        let mut result = result(&setup, &context, outcome, integrations, human);
        result.answered = answered;
        Ok(result)
    }

    async fn drive(
        &self,
        strategy: &TurnStrategy<'_>,
        context: &RuntimeContext,
        batch: &mut Tracked,
        answered: bool,
    ) -> Result<Outcome, ChatError> {
        let request = strategy.request;
        let brain = Brain {
            client: self.parts.brain.as_ref(),
            usage: request.usage.as_ref(),
        };
        // Team answers a send itself only when it is Admin's composed answer that binds the pending Routine question.
        let brain: &dyn Runtime = if answered {
            &super::routine::AnsweredRuntime
        } else {
            &brain
        };
        match (&request.continuation, &request.message) {
            (None, Some(message)) => {
                let prompt = team_chat::contract::build_prompt(message, &strategy.setup.files);
                run_until_pause(
                    brain,
                    context,
                    &prompt,
                    &request.conversation,
                    strategy,
                    batch,
                )
                .await
            }
            (Some(continuation), None) => {
                continue_after_pause(brain, context, continuation.clone(), strategy, batch).await
            }
            _ => Err(ChatError::Orchestration("invalid chat continuation")),
        }
    }
}

impl ChatService {
    /// The turn's Browser task: its Brain calls under the turn's key and Stop, its usage metered as the request's,
    /// priced from the catalog of the turn's provider.
    pub(super) fn browser_turn(
        &self,
        request: &SegmentRequest,
        setup: &Setup,
    ) -> crate::browser::service::BrowserTurn {
        use team_browser::task::{BrainCalls, BrowserCall, Usage};
        use team_inference::usage::{Counts, UsageRecorder};
        let provider = request.credential.0.clone();
        let usage = Arc::clone(&request.usage);
        let metered = provider.clone();
        let meter: team_browser::task::Meter =
            Arc::new(move |call: BrowserCall, model: &str, counts: &Usage| {
                let counts = Counts {
                    model_calls: counts.model_calls,
                    provider_requests: counts.provider_requests,
                    failed_calls: counts.failed_calls,
                    unreported_calls: counts.unreported_calls,
                    input_tokens: counts.input_tokens,
                    output_tokens: counts.output_tokens,
                    cache_read_tokens: counts.cache_read_tokens,
                    cache_write_tokens: counts.cache_write_tokens,
                };
                usage.record(call.operation(), &metered, model, &counts);
            });
        let credential = ModelCredential {
            provider: provider.clone(),
            model: request
                .model
                .clone()
                .unwrap_or_else(|| setup.config.model.clone()),
            api_key: request.credential.1.clone(),
        };
        let calls = BrainCalls::new(
            Arc::clone(&self.parts.brain),
            credential,
            request.stop.clone(),
            meter,
        );
        crate::browser::service::BrowserTurn {
            team_id: request.team_id.clone(),
            incarnation: setup.network_id.clone(),
            locale: request.locale.clone(),
            stop: request.stop.clone(),
            brain: Arc::new(calls),
            price: Arc::new(move |model| super::inference::model_price(&provider, model)),
            actor: request.actor.clone(),
        }
    }
}

/// One segment's result: what it ran against, how it ended, and what the turn's next request binds.
fn result(
    setup: &Setup,
    context: &RuntimeContext,
    outcome: Outcome,
    integrations: Vec<IntegrationRequirement>,
    human: Option<HumanRequirement>,
) -> SegmentResult {
    let contracts = setup
        .assistants
        .iter()
        .filter_map(|active| Some((active.id().to_owned(), active.routine_scope()?)))
        .collect();
    let paused_batch = match &outcome {
        Outcome::Human(paused) => {
            let scope = (setup.network_id.clone(), context.thread_id.clone());
            Some(paused_batch_id(&scope, &paused.continuation.turn.actions))
        }
        _ => None,
    };
    SegmentResult {
        team_name: context.team_name.clone(),
        identity: setup.identity(),
        outcome,
        integrations,
        human,
        contracts,
        paused_batch,
        model: context.credential.model.clone(),
        answered: None,
    }
}

/// The id of the Action batch a human request paused: the SHA-256 of its generation, thread, and interrupts, so ending
/// the turn removes exactly that batch.
#[must_use]
pub fn paused_batch_id(scope: &(String, String), actions: &[BrainRequest]) -> String {
    use sha2::{Digest, Sha256};
    let mut identity = vec![
        Value::String(scope.0.clone()),
        Value::String(scope.1.clone()),
    ];
    identity.extend(
        actions
            .iter()
            .map(|request| Value::String(request.interrupt_id.clone())),
    );
    let canonical = team_protocol::python::json::canonical(&Value::Array(identity));
    super::hex(&Sha256::digest(canonical))
}
