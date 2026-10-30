//! The deterministic Team-owned loop between Brain suspensions and Assistant Actions.
//!
//! A turn starts (or continues from a pause), and each Action suspension is admitted whole before its first side
//! effect: every request must name a declared Action, carry a distinct interrupt, and pass its input contract, and the
//! batch's worst-case results must fit the one resume that returns them. The batch then runs in order through the
//! durable [`ActionBatch`], the Brain resumes with its results, and only then is the batch acknowledged as delivered.
//! The Team context is revalidated around every side effect and resume, and Stop is observed between every two
//! bounded operations.

use std::collections::{BTreeMap, BTreeSet};

use strict_json::Value;
use team_action::executor::{self, ActionBatch, ActionFailure, ActionFile, InvokeOutcome};
use team_action::result::MAX_RESULT_BYTES;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_inference::brain::BrainError;
use team_inference::runtime::{
    ActionRequest, BrowserRequest, ConversationEntry, Runtime, RuntimeAction, RuntimeContext,
    RuntimeTurn, TurnStatus, resume_capacity,
};

use crate::attachments::{admitted_actions, selected_ids};
use crate::progress::{Phase, Position, Reporter};

pub use team_inference::runtime::BODY;

/// A chat turn's Action rounds.
pub const MAX_ACTION_ROUNDS: usize = 8;
/// A replayed Routine run has one round per step of its plan (ADR-0092 amendment, 2026-10-05, scale), so a resumed
/// continuation admits the larger of the two and each turn enforces its own.
pub const MAX_RESUMABLE_ROUNDS: usize = 256;
/// The distinct file-taking logical Actions one turn may run (ADR-0093, `action/files.py`).
pub const MAX_FILE_ACTIONS_PER_TURN: usize = 2;

/// A refusal a controller adapter raises, projected by the HTTP layer exactly as given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refusal {
    /// The HTTP status.
    pub status: u16,
    /// The fixed public message.
    pub message: &'static str,
    /// The stable code.
    pub code: &'static str,
}

/// Why a turn did not reach an outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChatError {
    /// The Controller cancelled the turn between two bounded operations (`chat-stopped`).
    Stopped,
    /// The Brain violated the turn contract or the turn could not finish safely (`brain-turn-failed`).
    Orchestration(&'static str),
    /// The Brain was unavailable or answered outside its contract (`brain-runtime-failed`).
    Brain(BrainError),
    /// An Action could not run, or failed.
    Action(ActionFailure),
    /// The Team changed since the turn bound it (`team-context-changed`).
    ContextChanged,
    /// A controller adapter refused.
    Refused(Refusal),
}

/// One Action a turn ran.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvokedAction {
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action: String,
}

/// A completed turn.
#[derive(Clone, Debug, PartialEq)]
pub struct ChatOutcome {
    /// The reply.
    pub reply: String,
    /// The Actions the turn ran, in order.
    pub actions: Vec<InvokedAction>,
    /// The closed question that ended the turn (ADR-0081).
    pub clarification: Option<Value>,
    /// The canonical memory changes the turn proposed (ADR-0084).
    pub memory: Vec<Value>,
    /// The chat agent's Routine record (ADR-0101).
    pub routine: Option<Value>,
    /// The terminal's `browser` field when the turn ran the Browser (ADR-0117).
    pub browser: Option<Value>,
}

/// The secret-free state needed to continue one Brain suspension.
#[derive(Clone, Debug, PartialEq)]
pub struct ChatContinuation {
    /// The suspended turn.
    pub turn: RuntimeTurn,
    /// The interrupts of every delivered round, sorted.
    pub seen_interrupts: Vec<String>,
    /// The Actions run before this round.
    pub invoked: Vec<InvokedAction>,
    /// The round this continuation resumes at.
    pub round_index: usize,
    /// The distinct file-taking logical Actions this turn admitted before this round (ADR-0093).
    pub file_actions: usize,
}

/// A whole batch paused before its first side effect (an Integration must be connected first).
#[derive(Clone, Debug, PartialEq)]
pub struct ChatSuspension {
    /// Where the turn resumes.
    pub continuation: ChatContinuation,
    /// The admitted batch.
    pub requests: Vec<ActionRequest>,
}

/// A validated Action request paused for a person, without returning anything to the Brain.
#[derive(Clone, Debug, PartialEq)]
pub struct ChatHumanSuspension {
    /// Where the turn resumes: the round of the paused batch.
    pub continuation: ChatContinuation,
    /// The Action that asked.
    pub action: ActionRequest,
    /// The admitted human request.
    pub request: Value,
    /// The interrupts of the batch that completed before it.
    pub completed_interrupts: Vec<String>,
}

/// How a segment of a turn ended.
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// The turn completed.
    Completed(ChatOutcome),
    /// The batch paused for an Integration.
    Suspended(ChatSuspension),
    /// An Action paused for a person.
    Human(ChatHumanSuspension),
}

/// The controller adapters a turn runs with.
pub trait Strategy: Send + Sync {
    /// The turn's Stop.
    fn stop(&self) -> &Stop;

    /// Whether the turn was cancelled.
    fn cancelled(&self) -> bool {
        self.stop().is_stopped()
    }

    /// Requires the Team to be exactly as the segment prepared it.
    ///
    /// # Errors
    /// [`ChatError`] when it changed or cannot be read.
    fn validate_context(&self) -> BoxFuture<'_, Result<(), ChatError>>;

    /// The schema-validated input object of one request of a declared Action.
    ///
    /// # Errors
    /// [`ChatError`] when the input does not satisfy the Action's contract.
    fn validate_action(
        &self,
        assistant_id: &str,
        action: &RuntimeAction,
        input: &Value,
    ) -> Result<Value, ChatError>;

    /// Whether the admitted batch must pause before it runs (an Integration it needs is not connected).
    ///
    /// # Errors
    /// [`ChatError`] when the requirements cannot be read.
    fn pause_before_batch<'a>(
        &'a self,
        batch: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<bool, ChatError>>;

    /// Runs the turn's one Browser task: the result the Brain resumes with and the terminal's `browser` field; `None`
    /// when this turn has no Browser.
    fn run_browser<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _request: &'a BrowserRequest,
    ) -> Option<BoxFuture<'a, (Value, Value)>> {
        None
    }

    /// The Brain resumed with the Browser task's result and the turn's checkpoint holds it: the task is delivered and
    /// is never run again (ADR-0125).
    fn browser_delivered(&self, _request: &BrowserRequest) {}

    /// Makes the turn's state after a Brain answer durable before anything it supersedes is deleted (ADR-0125): a
    /// restarted Team resumes the turn from here, first acknowledging the `undelivered` batch whose results the answer
    /// holds.
    ///
    /// # Errors
    /// [`ChatError`] when it could not be made durable; the turn then ends.
    fn checkpoint<'a>(
        &'a self,
        _continuation: &'a ChatContinuation,
        _undelivered: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ChatError>> {
        Box::pin(async { Ok(()) })
    }

    /// What the Brain receives of a batch's results (ADR-0122).
    fn shown_results(&self, results: Value) -> Value {
        results
    }

    /// The turn's progress.
    fn progress(&self) -> &Reporter;

    /// The turn's own Action round limit.
    fn max_rounds(&self) -> usize {
        MAX_ACTION_ROUNDS
    }

    /// Whether the turn keeps each Action it ran; a replayed run keeps none.
    fn record_invoked(&self) -> bool {
        true
    }
}

/// The executor's request for one admitted Brain request, with the files its turn selected (ADR-0093).
#[must_use]
pub fn executor_request(request: &ActionRequest, files: &[ActionFile]) -> executor::ActionRequest {
    executor::ActionRequest {
        interrupt_id: request.interrupt_id.clone(),
        assistant_id: request.assistant_id.clone(),
        action: request.action.clone(),
        input: request.input.clone(),
        files: files.to_vec(),
    }
}

fn stopped_unless<T>(
    strategy: &dyn Strategy,
    result: Result<T, ChatError>,
) -> Result<T, ChatError> {
    match result {
        Err(_) if strategy.cancelled() => Err(ChatError::Stopped),
        other => other,
    }
}

/// A Brain failure after Stop won is the stopped turn itself: Stop aborts the in-flight request (ADR-0079).
async fn brain_call(
    strategy: &dyn Strategy,
    call: BoxFuture<'_, Result<RuntimeTurn, BrainError>>,
) -> Result<RuntimeTurn, ChatError> {
    let _span = strategy.progress().span(Phase::Model, None);
    stopped_unless(strategy, call.await.map_err(ChatError::Brain))
}

fn check_stop(strategy: &dyn Strategy) -> Result<(), ChatError> {
    if strategy.cancelled() {
        Err(ChatError::Stopped)
    } else {
        Ok(())
    }
}

/// The declared Action of each request of a suspension, refusing an empty, oversized, undeclared, or repeated one.
fn declared_batch<'d>(
    requests: &'d [ActionRequest],
    declared: &'d BTreeMap<(String, String), RuntimeAction>,
    capacity: usize,
) -> Result<Vec<(&'d ActionRequest, &'d RuntimeAction)>, ChatError> {
    if requests.is_empty() {
        return Err(ChatError::Orchestration(
            "Brain suspended without an Action request",
        ));
    }
    if requests.len() > capacity {
        return Err(ChatError::Orchestration(
            "Action batch results could exceed the Brain request",
        ));
    }
    let mut seen = BTreeSet::new();
    let mut contracts = Vec::with_capacity(requests.len());
    for request in requests {
        let action = declared
            .get(&(request.assistant_id.clone(), request.action.clone()))
            .ok_or(ChatError::Orchestration(
                "Brain requested an undeclared Action contract",
            ))?;
        if !seen.insert(request.interrupt_id.as_str()) {
            return Err(ChatError::Orchestration(
                "Brain repeated an Action interrupt id",
            ));
        }
        contracts.push((request, action));
    }
    Ok(contracts)
}

/// Validates a complete suspension before allowing its first side effect: a file-taking Action may name only a file
/// the turn selected.
fn validate_batch(
    requests: &[ActionRequest],
    declared: &BTreeMap<(String, String), RuntimeAction>,
    strategy: &dyn Strategy,
    capacity: usize,
    selected: &BTreeSet<String>,
) -> Result<Vec<ActionRequest>, ChatError> {
    let contracts = declared_batch(requests, declared, capacity)?;
    let mut validated = Vec::with_capacity(requests.len());
    for (request, action) in contracts {
        let input = strategy.validate_action(&request.assistant_id, action, &request.input)?;
        if !matches!(input, Value::Object(_)) {
            return Err(ChatError::Orchestration(
                "Action validator returned an invalid input contract",
            ));
        }
        if let Some(name) = action.input_files.first() {
            let named = match input.get(name) {
                Some(Value::String(file_id)) => selected.contains(file_id),
                _ => false,
            };
            if !named {
                return Err(ChatError::Orchestration(
                    "Brain named a file this turn did not select",
                ));
            }
        }
        validated.push(ActionRequest {
            interrupt_id: request.interrupt_id.clone(),
            assistant_id: request.assistant_id.clone(),
            action: action.id.clone(),
            input,
        });
    }
    Ok(validated)
}

/// Admits a round's whole batch, and the turn's file budget it reserves, before any of its side effects.
fn admit_batch(
    turn: &RuntimeTurn,
    context: &RuntimeContext,
    declared: &BTreeMap<(String, String), RuntimeAction>,
    strategy: &dyn Strategy,
    admitted: (&BTreeSet<String>, usize),
) -> Result<(Vec<ActionRequest>, usize), ChatError> {
    let (seen, file_actions) = admitted;
    let capacity = resume_capacity(context, MAX_RESULT_BYTES);
    let selected: BTreeSet<String> = selected_ids(context).into_iter().collect();
    let batch = validate_batch(&turn.actions, declared, strategy, capacity, &selected)?;
    if batch
        .iter()
        .any(|request| seen.contains(&request.interrupt_id))
    {
        return Err(ChatError::Orchestration(
            "Brain repeated an Action interrupt across rounds",
        ));
    }
    let taking = batch
        .iter()
        .filter(|request| {
            declared
                .get(&(request.assistant_id.clone(), request.action.clone()))
                .is_some_and(|action| !action.input_files.is_empty())
        })
        .count();
    let total = file_actions + taking;
    if total > MAX_FILE_ACTIONS_PER_TURN {
        return Err(ChatError::Orchestration(
            "Action batch exceeds the turn's file deliveries",
        ));
    }
    Ok((batch, total))
}

/// What one batch ran: its results by interrupt and the Actions it invoked, unless a person must answer.
enum Ran {
    Done(Vec<(String, Value)>, Vec<InvokedAction>),
    Human(Box<ChatHumanSuspension>),
}

/// Runs one admitted batch in order.
async fn run_batch(
    strategy: &dyn Strategy,
    batch: &mut dyn ActionBatch,
    requests: &[ActionRequest],
    files: &[ActionFile],
    checkpoint: &ChatContinuation,
) -> Result<Ran, ChatError> {
    let mut results: Vec<(String, Value)> = Vec::with_capacity(requests.len());
    let mut invoked = Vec::new();
    let total = i64::try_from(requests.len()).unwrap_or(i64::MAX);
    for (index, request) in requests.iter().enumerate() {
        check_stop(strategy)?;
        strategy.validate_context().await?;
        let outcome = {
            let _span = strategy.progress().span(
                Phase::Action,
                Some(Position {
                    index: i64::try_from(index + 1).unwrap_or(i64::MAX),
                    total,
                    assistant_id: request.assistant_id.clone(),
                    action: request.action.clone(),
                }),
            );
            let invoked = batch
                .invoke(&executor_request(request, files), strategy.stop())
                .await;
            stopped_unless(strategy, invoked.map_err(ChatError::Action))?
        };
        check_stop(strategy)?;
        let result = match outcome {
            InvokeOutcome::Completed(result) => result,
            InvokeOutcome::HumanRequest(human) => {
                return Ok(Ran::Human(Box::new(ChatHumanSuspension {
                    continuation: checkpoint.clone(),
                    action: request.clone(),
                    request: human,
                    completed_interrupts: results
                        .iter()
                        .map(|(interrupt, _)| interrupt.clone())
                        .collect(),
                })));
            }
        };
        results.push((request.interrupt_id.clone(), result));
        if strategy.record_invoked() {
            invoked.push(InvokedAction {
                assistant_id: request.assistant_id.clone(),
                action: request.action.clone(),
            });
        }
    }
    Ok(Ran::Done(results, invoked))
}

/// Runs the turn's one Browser task, then requires that the Brain only replies (ADR-0117 §6).
async fn run_browser(
    runtime: &dyn Runtime,
    context: &RuntimeContext,
    turn: &RuntimeTurn,
    strategy: &dyn Strategy,
) -> Result<(RuntimeTurn, Value), ChatError> {
    let not_offered = ChatError::Orchestration("Brain called the Browser where it was not offered");
    let request = turn.browser.as_ref().ok_or(not_offered)?;
    if !context.browser || !context.attachments.is_empty() {
        return Err(not_offered);
    }
    let task = strategy.run_browser(context, request).ok_or(not_offered)?;
    strategy.validate_context().await?;
    let (result, terminal) = {
        let _span = strategy.progress().span(Phase::Browser, None);
        task.await
    };
    check_stop(strategy)?;
    strategy.validate_context().await?;
    let results = Value::Object(vec![(request.interrupt_id.clone(), result)]);
    let resumed = brain_call(strategy, runtime.resume(context, results, strategy.stop())).await?;
    if resumed.status != TurnStatus::Completed
        || !resumed.memory.is_empty()
        || resumed.routine.is_some()
        || resumed.clarification.is_some()
    {
        return Err(ChatError::Orchestration(
            "Brain did more than reply after a Browser result",
        ));
    }
    Ok((resumed, terminal))
}

/// The turn's state between rounds.
struct Drive<'a> {
    runtime: &'a dyn Runtime,
    context: &'a RuntimeContext,
    strategy: &'a dyn Strategy,
    declared: BTreeMap<(String, String), RuntimeAction>,
}

impl Drive<'_> {
    /// The files the turn selected.
    fn files(&self) -> Vec<ActionFile> {
        team_action::files::selected(&self.context.attachments)
    }

    fn executor_requests(&self, requests: &[ActionRequest]) -> Vec<executor::ActionRequest> {
        let files = self.files();
        requests
            .iter()
            .map(|request| executor_request(request, &files))
            .collect()
    }

    async fn complete(
        &self,
        turn: RuntimeTurn,
        invoked: Vec<InvokedAction>,
        round: usize,
        browser: Option<Value>,
    ) -> Result<Outcome, ChatError> {
        if turn.clarification.is_some() && (round > 0 || !invoked.is_empty()) {
            // A question is only admissible before any Action of the logical turn ran (ADR-0081).
            return Err(ChatError::Orchestration(
                "Brain asked a clarification after Actions ran",
            ));
        }
        {
            let _span = self.strategy.progress().span(Phase::TeamContext, None);
            self.strategy.validate_context().await?;
        }
        Ok(Outcome::Completed(ChatOutcome {
            reply: turn.reply,
            actions: invoked,
            clarification: turn.clarification,
            memory: turn.memory,
            routine: turn.routine,
            browser,
        }))
    }

    /// Admits a round's batch and prepares it, unless it must pause first.
    async fn admit(
        &self,
        batch: &mut dyn ActionBatch,
        checkpoint: &ChatContinuation,
        seen: &BTreeSet<String>,
    ) -> Result<(Vec<ActionRequest>, usize, bool), ChatError> {
        let strategy = self.strategy;
        let _span = strategy.progress().span(Phase::ActionPreparation, None);
        strategy.validate_context().await?;
        let admitted = (seen, checkpoint.file_actions);
        let (requests, files) = admit_batch(
            &checkpoint.turn,
            self.context,
            &self.declared,
            strategy,
            admitted,
        )?;
        if strategy.pause_before_batch(&requests).await? {
            return Ok((requests, files, true));
        }
        let prepared = self.executor_requests(&requests);
        batch.prepare(&prepared).await.map_err(ChatError::Action)?;
        Ok((requests, files, false))
    }

    /// Resumes the Brain with a ran batch's results; the batch is acknowledged as delivered once the turn's next
    /// checkpoint holds them.
    async fn deliver(
        &self,
        requests: &[ActionRequest],
        results: Vec<(String, Value)>,
        seen: &mut BTreeSet<String>,
    ) -> Result<RuntimeTurn, ChatError> {
        let strategy = self.strategy;
        strategy.validate_context().await?;
        seen.extend(requests.iter().map(|request| request.interrupt_id.clone()));
        let shown = strategy.shown_results(Value::Object(results));
        let resume = self.runtime.resume(self.context, shown, strategy.stop());
        let resumed = brain_call(strategy, resume).await?;
        let repeated = resumed
            .actions
            .iter()
            .any(|request| seen.contains(&request.interrupt_id));
        if resumed.status == TurnStatus::ActionRequired && repeated {
            return Err(ChatError::Orchestration(
                "Brain repeated an Action interrupt across rounds",
            ));
        }
        Ok(resumed)
    }

    /// The batch's receipts go once the turn's checkpoint after the Brain's answer holds their results.
    async fn delivered(
        &self,
        batch: &mut dyn ActionBatch,
        requests: &[ActionRequest],
        next: &ChatContinuation,
    ) -> Result<(), ChatError> {
        self.strategy.checkpoint(next, requests).await?;
        let _span = self.strategy.progress().span(Phase::ActionDelivery, None);
        let delivered = self.executor_requests(requests);
        batch.delivered(&delivered).await.map_err(ChatError::Action)
    }

    /// Admits and runs one round's batch, then resumes the Brain with its results: the next turn and the batch's
    /// effects, or the pause it ended in.
    async fn round(
        &self,
        batch: &mut dyn ActionBatch,
        checkpoint: ChatContinuation,
        seen: &mut BTreeSet<String>,
    ) -> Result<
        Result<(RuntimeTurn, Vec<InvokedAction>, usize, Vec<ActionRequest>), Outcome>,
        ChatError,
    > {
        let (requests, file_actions, pause) = self.admit(batch, &checkpoint, seen).await?;
        if pause {
            return Ok(Err(Outcome::Suspended(ChatSuspension {
                continuation: checkpoint,
                requests,
            })));
        }
        let (results, invoked) =
            match run_batch(self.strategy, batch, &requests, &self.files(), &checkpoint).await? {
                Ran::Human(suspension) => return Ok(Err(Outcome::Human(*suspension))),
                Ran::Done(results, invoked) => (results, invoked),
            };
        let resumed = self.deliver(&requests, results, seen).await?;
        Ok(Ok((resumed, invoked, file_actions, requests)))
    }
}

/// Where a turn stands between rounds.
struct Standing {
    turn: RuntimeTurn,
    seen: BTreeSet<String>,
    invoked: Vec<InvokedAction>,
    file_actions: usize,
}

impl Standing {
    /// The continuation a checkpoint at `round` keeps.
    fn at(&self, round: usize) -> ChatContinuation {
        ChatContinuation {
            turn: self.turn.clone(),
            seen_interrupts: self.seen.iter().cloned().collect(),
            invoked: self.invoked.clone(),
            round_index: round,
            file_actions: self.file_actions,
        }
    }
}

impl Drive<'_> {
    /// Runs the turn's Browser task and resumes the Brain with it; the result is delivered once the checkpoint
    /// holds the resumed turn. The terminal's `browser` field.
    async fn browser(&self, standing: &mut Standing, round: usize) -> Result<Value, ChatError> {
        let request = standing.turn.browser.clone();
        let (resumed, terminal) =
            run_browser(self.runtime, self.context, &standing.turn, self.strategy).await?;
        standing.turn = resumed;
        self.strategy.checkpoint(&standing.at(round), &[]).await?;
        if let Some(request) = &request {
            self.strategy.browser_delivered(request);
        }
        Ok(terminal)
    }
}

async fn drive(
    runtime: &dyn Runtime,
    context: &RuntimeContext,
    continuation: ChatContinuation,
    strategy: &dyn Strategy,
    batch: &mut dyn ActionBatch,
) -> Result<Outcome, ChatError> {
    // While attachment content is in the turn, only authorizing Actions are declared to it (ADR-0093).
    let declared = admitted_actions(context);
    let state = Drive {
        runtime,
        context,
        strategy,
        declared,
    };
    let mut standing = Standing {
        seen: continuation.seen_interrupts.iter().cloned().collect(),
        turn: continuation.turn,
        invoked: continuation.invoked,
        file_actions: continuation.file_actions,
    };
    let mut browser = None;
    for round in continuation.round_index..=strategy.max_rounds() {
        check_stop(strategy)?;
        if standing.turn.status == TurnStatus::BrowserRequired {
            // A Browser result admits only the reply, so the completed turn ends in this same round (ADR-0117 §6).
            browser = Some(state.browser(&mut standing, round).await?);
        }
        if standing.turn.status == TurnStatus::Completed {
            return state
                .complete(standing.turn, standing.invoked, round, browser)
                .await;
        }
        if round == strategy.max_rounds() {
            return Err(ChatError::Orchestration(
                "Brain exceeded the Action round limit",
            ));
        }
        let checkpoint = standing.at(round);
        match state.round(batch, checkpoint, &mut standing.seen).await? {
            Err(paused) => return Ok(paused),
            Ok((resumed, ran, files, requests)) => {
                standing.invoked.extend(ran);
                (standing.file_actions, standing.turn) = (files, resumed);
                state
                    .delivered(batch, &requests, &standing.at(round + 1))
                    .await?;
            }
        }
    }
    Err(ChatError::Orchestration(
        "Brain did not complete the chat turn",
    ))
}

/// Starts a turn and runs it until it completes or pauses before an all-or-nothing Action batch.
///
/// `conversation` is the committed presentation history before this turn; the Brain uses it only when it retains no
/// completed exchange of its own.
///
/// # Errors
/// [`ChatError`] for every turn that does not complete or pause.
pub async fn run_until_pause(
    runtime: &dyn Runtime,
    context: &RuntimeContext,
    message: &str,
    conversation: &[ConversationEntry],
    strategy: &dyn Strategy,
    batch: &mut dyn ActionBatch,
) -> Result<Outcome, ChatError> {
    check_stop(strategy)?;
    strategy.validate_context().await?;
    let turn = brain_call(
        strategy,
        runtime.start(context, message, conversation, strategy.stop()),
    )
    .await?;
    let continuation = ChatContinuation {
        turn,
        seen_interrupts: Vec::new(),
        invoked: Vec::new(),
        round_index: 0,
        file_actions: 0,
    };
    strategy.checkpoint(&continuation, &[]).await?;
    drive(runtime, context, continuation, strategy, batch).await
}

/// Continues a turn a previous Team process left at its checkpoint: the batch whose results the checkpoint holds is
/// acknowledged first (its receipts replay from the Action journal), then the turn goes on (ADR-0125).
///
/// # Errors
/// [`ChatError`] for every turn that does not complete or pause.
pub async fn continue_after_restart(
    runtime: &dyn Runtime,
    context: &RuntimeContext,
    (continuation, undelivered): (ChatContinuation, &[ActionRequest]),
    strategy: &dyn Strategy,
    batch: &mut dyn ActionBatch,
) -> Result<Outcome, ChatError> {
    if !undelivered.is_empty() {
        let files = team_action::files::selected(&context.attachments);
        let requests: Vec<_> = undelivered
            .iter()
            .map(|request| executor_request(request, &files))
            .collect();
        batch.prepare(&requests).await.map_err(ChatError::Action)?;
        batch
            .delivered(&requests)
            .await
            .map_err(ChatError::Action)?;
    }
    drive(runtime, context, continuation, strategy, batch).await
}

/// Continues an admitted suspension without re-running the person's message.
///
/// # Errors
/// [`ChatError`] for every turn that does not complete or pause.
pub async fn continue_after_pause(
    runtime: &dyn Runtime,
    context: &RuntimeContext,
    continuation: ChatContinuation,
    strategy: &dyn Strategy,
    batch: &mut dyn ActionBatch,
) -> Result<Outcome, ChatError> {
    drive(runtime, context, continuation, strategy, batch).await
}

#[cfg(test)]
mod tests;
