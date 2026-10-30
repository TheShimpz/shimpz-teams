//! The seam through which a chat turn, a Routine segment, and the direct invoke route run Actions (frozen in W0).
//!
//! A Brain suspension's Action requests form one durable batch bound to the Team's generation and the Brain thread:
//! the batch is prepared once (each operation journaled as `prepared`), each request is invoked under the turn's
//! [`Stop`] (journaled `executing` before dispatch; an `executing` operation found again is uncertain and never
//! re-executed; a `completed` one replays its result), the results' delivery to the model is acknowledged, and a batch
//! that ends before delivery is terminated (its settled receipts kept for that exact batch only, an uncertain batch
//! never ended this way).

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;

use crate::human::ActionTranscript;

/// One Action request of a Brain suspension.
#[derive(Clone, Debug, PartialEq)]
pub struct ActionRequest {
    /// The Brain's interrupt id, unique within the batch.
    pub interrupt_id: String,
    /// The installed Assistant's id.
    pub assistant_id: String,
    /// The declared Action's id.
    pub action: String,
    /// The schema-validated input.
    pub input: Value,
    /// The files the logical turn selected, as Team bound them; a file-taking Action names one by id (ADR-0093).
    pub files: Vec<ActionFile>,
}

/// One file a turn selected, exactly as the turn bound it; its bytes stay in Team storage.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionFile {
    /// Its opaque id.
    pub id: String,
    /// Its name.
    pub name: String,
    /// The media type Team determined while preparing it.
    pub media_type: String,
    /// Its size in bytes.
    pub size: i64,
    /// The SHA-256 of its bytes, in hex.
    pub sha256: String,
}

/// What one batch is bound to.
#[derive(Clone, Debug, PartialEq)]
pub struct BatchScope {
    /// The Team's id.
    pub team_id: String,
    /// The Team's generation: its network's id, which ends the batch's journal state with the Team.
    pub generation: String,
    /// The Brain thread the suspension belongs to.
    pub thread_id: String,
    /// Whether the batch is a Routine run's, which reserves its incident's archive marker first.
    pub routine: bool,
    /// The responses a person gave the paused Actions of exactly the continuation this batch resumes, by interrupt.
    /// They live only with this batch, so no other turn or run can ever replay them.
    pub transcripts: Vec<ActionTranscript>,
}

/// One invoked request's outcome.
#[derive(Clone, Debug, PartialEq)]
pub enum InvokeOutcome {
    /// The Action completed (or its completed receipt replayed) with this projected, credential-free result.
    Completed(Value),
    /// The Action asked a person; the operation is journaled back to `prepared` and the turn pauses with this request.
    HumanRequest(Value),
}

/// A closed Action failure; its public message and code are fixed by the HTTP layer, never upstream text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionFailure {
    /// The Action is not declared by the Assistant.
    NotDeclared,
    /// The input does not satisfy the Action's schema.
    InvalidInput,
    /// The Assistant is not installed, not running, or its workload changed since the batch was bound.
    ContextChanged,
    /// The workload could not be proven stopped after a failure; nothing more runs in it.
    Blocked,
    /// The Action needs a person, which the direct invoke route cannot ask.
    HumanRequestRequired,
    /// Stop arrived before dispatch; nothing was sent.
    Stopped,
    /// The exchange failed after dispatch, so whether the Action took effect is unknown.
    Uncertain,
    /// The Action answered a closed failure that proved it had no effect.
    Failed,
    /// The journal, a credential store, or the audit log is unavailable; nothing was dispatched.
    StateUnavailable,
    /// Docker is unavailable; nothing was dispatched.
    DockerUnavailable,
    /// An Integration the Action declares is not connected or could not be resolved (428); nothing was dispatched.
    IntegrationUnavailable,
    /// The file the Action names is not one the turn selected, or no longer has its turn's bytes; nothing was
    /// dispatched.
    FileUnavailable,
    /// Another file delivery held the one file-RPC slot past this delivery's deadline; nothing was dispatched.
    FileBusy,
}

/// One durable batch of a Brain suspension.
pub trait ActionBatch: Send {
    /// Journals every request as `prepared`, once.
    ///
    /// # Errors
    /// [`ActionFailure`] when the batch cannot be prepared; nothing ran.
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>>;

    /// Runs one prepared request under `stop`.
    ///
    /// # Errors
    /// [`ActionFailure`] for a refusal before dispatch or a failed or uncertain execution.
    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionFailure>>;

    /// Acknowledges that the model accepted these requests' results, retiring their journal entries.
    ///
    /// # Errors
    /// [`ActionFailure::StateUnavailable`] when the journal cannot record it.
    fn delivered<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>>;

    /// Ends the scope's open batch whose every result exists but whose delivery a Team restart interrupted, so the next
    /// batch may open (ADR-0125); a batch delivered already, or one an operation of which may still have acted, stays as
    /// it is.
    ///
    /// # Errors
    /// [`ActionFailure::StateUnavailable`] when the journal cannot record it.
    fn settle_undelivered(&mut self) -> BoxFuture<'_, Result<(), ActionFailure>> {
        Box::pin(async { Ok(()) })
    }

    /// Ends a batch whose results were never delivered; `false` when it holds an uncertain outcome and stays.
    ///
    /// # Errors
    /// [`ActionFailure::StateUnavailable`] when the journal cannot record it.
    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionFailure>>;

    /// Every value Team injected into the most recent [`ActionBatch::invoke`]'s attempt: its Integration tokens and its
    /// Stored Input values. A caller that keeps anything of the call (a Routine recording) protects them first; it is
    /// empty when that invoke dispatched nothing or replayed a receipt.
    fn injected(&self) -> Vec<Secret>;
}

/// Team's own classification of one failed dispatched attempt (ADR-0092), never a model's or a message's.
#[derive(Clone, Debug, PartialEq)]
pub enum Fault {
    /// The Action answered this Team-admitted handled failure.
    Handled(Box<crate::failure::ActionFailure>),
    /// The exchange failed with this closed condition after the workload was proven fail-stopped.
    Transport(String),
    /// Team could not prove the workload stopped after an ambiguous outcome.
    Unquiesced,
    /// A Team-detected policy fault: an invalid frame (its condition) or result, or a secret echo.
    Policy(Option<String>),
    /// Anything else, such as a workload that never received the call or a rejected Stored Input.
    Other,
}

/// The observer refused to seal a dispatch: nothing is sent, and the invoke fails with
/// [`ActionFailure::StateUnavailable`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DispatchRefused;

/// What a Routine run's batch tells the run around each dispatch.
pub trait DispatchObserver: Send + Sync {
    /// The logical operation a permitted retry repeats; `None` lets the journal mint the request's own.
    fn logical_operation(&self, request: &ActionRequest) -> Option<String>;

    /// Called after the operation is journaled `executing` and before its RPC, with the workload it is sent to and
    /// every value Team injects into it.
    ///
    /// # Errors
    /// [`DispatchRefused`] when the dispatch cannot be sealed; the RPC is then not sent.
    fn dispatching(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        workload: &str,
        injected: &[Secret],
    ) -> Result<(), DispatchRefused>;

    /// The dispatched Action returned a result or a human request, before Team projects anything of it.
    fn returned(&self, request: &ActionRequest);

    /// The dispatched Action failed; called once, before the invoke returns its failure, with the values the
    /// attempt was given.
    fn failed(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        fault: Fault,
        injected: &[Secret],
    );
}

/// Runs Actions for every caller.
pub trait ActionExecutor: Send + Sync {
    /// A new batch bound to `scope`.
    fn batch(&self, scope: BatchScope) -> Box<dyn ActionBatch>;

    /// Runs one Action directly (the `assistant-invoke` route), outside any batch, returning its projected result.
    ///
    /// # Errors
    /// [`ActionFailure`] for any refusal or failure; [`ActionFailure::HumanRequestRequired`] when it asks a person.
    fn invoke<'a>(
        &'a self,
        team_id: &'a str,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Value, ActionFailure>>;
}
