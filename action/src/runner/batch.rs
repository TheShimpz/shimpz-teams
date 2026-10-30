//! One durable batch of a Brain suspension (`action/execution.py` `ActionBatch`, ADR-0092).
//!
//! Each request's operation is fingerprinted over its exact binding (container and image), its input, and the
//! generations of the private values it will use, then journaled `prepared` once. A value this batch's own Actions
//! sealed (bound to their interrupt origins) never changes the fingerprint; any other change refuses the invocation
//! as a changed context. An operation journaled `executing` is begun again only as an uncertain outcome; a completed
//! one replays its result; a human request returns it to `prepared`; Team's own refusal before dispatch returns it to
//! `prepared`; a reviewed read-only Action's handled failure ends it without effect; anything else stays uncertain.

use std::sync::Arc;

use strict_json::Value;
use team_assistant::spec::ActionSpec;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;

use super::{Binding, Delivery, Failed, Outcome, Runner, Watch};
use crate::executor::{
    ActionBatch, ActionFailure, ActionRequest, BatchScope, DispatchObserver, InvokeOutcome,
};
use crate::files;
use crate::human::ActionTranscript;
use crate::journal::{self, JournalError, Operation, canonical, sha256_hex};

/// The Integration and Stored Input generations an operation binds.
type Generations = (Vec<(String, u64)>, Vec<(String, u64)>);

/// One request's prepared identity.
struct Prepared {
    operation: Operation,
    /// The generations of every Stored Input present at prepare, whatever its origin.
    present: Vec<(String, u64)>,
}

/// A batch over the journal.
pub(super) struct Batch {
    runner: Arc<Runner>,
    scope: BatchScope,
    handle: Option<journal::Batch>,
    prepared: Vec<(String, Prepared)>,
    origins: Vec<String>,
    executing_here: Vec<String>,
    /// A Routine run's observer, told around every dispatch.
    observer: Option<Arc<dyn DispatchObserver>>,
    /// What the most recent invoke's attempt was given.
    injected: Vec<Secret>,
}

/// The origin binding a newly sealed Stored Input to one exact Brain Action interrupt.
pub(crate) fn origin(request: &ActionRequest) -> String {
    let identity = Value::Object(vec![
        ("action".to_owned(), Value::String(request.action.clone())),
        (
            "assistant_id".to_owned(),
            Value::String(request.assistant_id.clone()),
        ),
        ("input".to_owned(), request.input.clone()),
        (
            "interrupt_id".to_owned(),
            Value::String(request.interrupt_id.clone()),
        ),
    ]);
    sha256_hex(&canonical(&identity))
}

fn pairs(values: &[(String, u64)]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|(id, generation)| {
                let generation = i64::try_from(*generation).unwrap_or(i64::MAX);
                Value::Array(vec![
                    Value::String(id.clone()),
                    Value::Number(strict_json::Number::Int(generation)),
                ])
            })
            .collect(),
    )
}

/// Journals `operation` `executing`, or finds it settled.
async fn begin(
    journal: &Arc<journal::Journal>,
    handle: &journal::Batch,
    operation: &Operation,
) -> Result<journal::Execution, ActionFailure> {
    let (journal, handle, operation) = (Arc::clone(journal), handle.clone(), operation.clone());
    tokio::task::spawn_blocking(move || journal.begin(&handle, &operation))
        .await
        .map_err(|_| ActionFailure::StateUnavailable)?
        .map_err(journal_failure)
}

fn journal_failure(error: JournalError) -> ActionFailure {
    match error {
        JournalError::Conflict(_) | JournalError::Archived => ActionFailure::ContextChanged,
        JournalError::Uncertain => ActionFailure::Uncertain,
        JournalError::Failure(_) | JournalError::Corrupt(_) => ActionFailure::StateUnavailable,
    }
}

impl Batch {
    pub(super) fn new(
        runner: Arc<Runner>,
        scope: BatchScope,
        observer: Option<Arc<dyn DispatchObserver>>,
    ) -> Self {
        Self {
            runner,
            scope,
            handle: None,
            prepared: Vec::new(),
            origins: Vec::new(),
            executing_here: Vec::new(),
            observer,
            injected: Vec::new(),
        }
    }

    /// The Integration and Stored Input generations of `action`; Stored Inputs whose origin is in `excluded` skip.
    fn generations(
        &self,
        binding: &Binding,
        action: &ActionSpec,
        excluded: &[String],
    ) -> Result<Generations, ActionFailure> {
        let view = super::assistant_view(binding);
        let declared: Vec<_> = view
            .integrations
            .iter()
            .filter(|(id, _)| action.integrations.contains(id))
            .cloned()
            .collect();
        let rows = self
            .runner
            .integrations
            .metadata(&self.scope.team_id, &binding.assistant_id, &declared)
            .map_err(|_| ActionFailure::StateUnavailable)?;
        if rows.len() != action.integrations.len() || rows.iter().any(|row| row.generation < 1) {
            return Err(ActionFailure::IntegrationUnavailable);
        }
        let integrations = rows
            .into_iter()
            .map(|row| (row.id, row.generation))
            .collect();
        let mut stored = Vec::new();
        for id in &action.stored_inputs {
            match self
                .runner
                .stored_inputs
                .resolve((&self.scope.team_id, &binding.assistant_id, id), "password")
            {
                Ok(value) if !excluded.contains(&value.origin) => {
                    stored.push((id.clone(), value.generation));
                }
                Ok(_) | Err(crate::stored_input::StoredInputError::Missing) => {}
                Err(_) => return Err(ActionFailure::StateUnavailable),
            }
        }
        Ok((integrations, stored))
    }

    /// The operation of one request under the given origin exclusions.
    async fn operation(
        &self,
        request: &ActionRequest,
        excluded: &[String],
    ) -> Result<(Operation, Vec<(String, u64)>), ActionFailure> {
        let (binding, action) = self.runner.admitted(&self.scope.team_id, request).await?;
        let (integrations, stored) = self.generations(&binding, &action, excluded)?;
        let (_, present) = self.generations(&binding, &action, &[])?;
        let file = files::action_file(&action.input_files, &request.input, &request.files)
            .map_err(|_| ActionFailure::FileUnavailable)?;
        let identity = Value::Object(vec![
            (
                "assistant_container_id".to_owned(),
                Value::String(binding.container.clone()),
            ),
            (
                "assistant_id".to_owned(),
                Value::String(request.assistant_id.clone()),
            ),
            (
                "assistant_image".to_owned(),
                Value::String(binding.image.clone()),
            ),
            ("integration_generations".to_owned(), pairs(&integrations)),
            ("stored_input_generations".to_owned(), pairs(&stored)),
            ("files".to_owned(), files::commitments(file)),
            ("input".to_owned(), request.input.clone()),
            ("action".to_owned(), Value::String(request.action.clone())),
        ]);
        let operation = Operation {
            interrupt_id: request.interrupt_id.clone(),
            fingerprint: sha256_hex(&canonical(&identity)),
            operation_id: self
                .observer
                .as_ref()
                .and_then(|observer| observer.logical_operation(request)),
        };
        Ok((operation, present))
    }

    /// The responses this batch's own continuation carries for `interrupt_id`, empty when it carries none.
    fn transcript(&self, interrupt_id: &str) -> ActionTranscript {
        self.scope
            .transcripts
            .iter()
            .find(|kept| kept.interrupt_id == interrupt_id)
            .cloned()
            .unwrap_or_else(|| ActionTranscript::new(interrupt_id))
    }

    /// The prepared operation of `request`, refused as a changed context unless its binding, input, and private-value
    /// generations are still the ones it was prepared with.
    async fn unchanged(&self, request: &ActionRequest) -> Result<Operation, ActionFailure> {
        let (operation, present) = self
            .prepared
            .iter()
            .find(|(interrupt, _)| *interrupt == request.interrupt_id)
            .map(|(_, prepared)| (prepared.operation.clone(), prepared.present.clone()))
            .ok_or(ActionFailure::ContextChanged)?;
        let (current, now_present) = self.operation(request, &self.origins.clone()).await?;
        let unchanged = present.iter().all(|entry| now_present.contains(entry));
        if current.fingerprint != operation.fingerprint || !unchanged {
            return Err(ActionFailure::ContextChanged);
        }
        Ok(operation)
    }

    async fn run(
        &mut self,
        request: &ActionRequest,
        stop: &Stop,
    ) -> Result<InvokeOutcome, ActionFailure> {
        self.injected.clear();
        let journal = Arc::clone(&self.runner.journal);
        let handle = self.handle.clone().ok_or(ActionFailure::ContextChanged)?;
        let operation = self.unchanged(request).await?;
        let (binding, action) = self.runner.admitted(&self.scope.team_id, request).await?;
        let file = files::action_file(&action.input_files, &request.input, &request.files)
            .map_err(|_| ActionFailure::FileUnavailable)?;
        let transcript = self.transcript(&request.interrupt_id);
        // A delivery holds the one file-RPC slot before its execution is journaled, so a refusal here leaves the
        // journal exactly as it was (ADR-0093).
        let runner = Arc::clone(&self.runner);
        let granted = files::grants(file, &action, &transcript);
        let admitted = runner.file_slot.admit(granted, stop).await?;
        let decision = begin(&journal, &handle, &operation).await?;
        if !decision.execute {
            return decision
                .result
                .map(InvokeOutcome::Completed)
                .ok_or(ActionFailure::StateUnavailable);
        }
        self.executing_here.push(operation.interrupt_id.clone());
        let target = super::Target {
            team: &self.scope.team_id,
            binding: &binding,
            action: &action,
            operation_id: &decision.operation_id,
        };
        let delivery = Delivery { file, admitted };
        let mut watch = Watch {
            observer: self.observer.as_deref().map(|observer| (observer, request)),
            injected: Vec::new(),
        };
        let attempt = runner
            .attempt(
                &target,
                &request.input,
                &transcript,
                delivery,
                (stop, &mut watch),
            )
            .await;
        self.injected = watch.injected;
        self.settle(&handle, &operation, &action, attempt).await
    }

    async fn settle(
        &mut self,
        handle: &journal::Batch,
        operation: &Operation,
        action: &ActionSpec,
        attempt: Result<Outcome, Failed>,
    ) -> Result<InvokeOutcome, ActionFailure> {
        let journal = Arc::clone(&self.runner.journal);
        let (handle, operation) = (handle.clone(), operation.clone());
        let interrupt = operation.interrupt_id.clone();
        let read_only = action.effect == "read_only";
        let (settled, answer) = match attempt {
            Ok(Outcome::Completed(result)) => {
                let kept = result.clone();
                let settled = tokio::task::spawn_blocking(move || {
                    journal.complete(&handle, &operation, &kept)
                })
                .await;
                (settled, Ok(InvokeOutcome::Completed(result)))
            }
            Ok(Outcome::Request(request)) => {
                let settled =
                    tokio::task::spawn_blocking(move || journal.suspend(&handle, &operation)).await;
                (settled, Ok(InvokeOutcome::HumanRequest(request.payload())))
            }
            Err(Failed::NotDispatched(failure)) => {
                let settled = tokio::task::spawn_blocking(move || {
                    journal.not_dispatched(&handle, &operation)
                })
                .await;
                (settled, Err(failure))
            }
            Err(Failed::Handled) if read_only => {
                let settled = tokio::task::spawn_blocking(move || {
                    journal.fail_without_effect(&handle, &operation)
                })
                .await;
                (settled, Err(ActionFailure::Failed))
            }
            // Any other failure, and any failure of a mutating Action, stays uncertain in the journal (ADR-0092).
            Err(Failed::Handled) => return Err(ActionFailure::Failed),
            Err(Failed::Uncertain(failure)) => return Err(failure),
        };
        settled
            .map_err(|_| ActionFailure::StateUnavailable)?
            .map_err(journal_failure)?;
        self.executing_here.retain(|id| *id != interrupt);
        answer
    }
}

impl ActionBatch for Batch {
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        Box::pin(async move {
            if self.handle.is_some() {
                return Err(ActionFailure::ContextChanged);
            }
            let origins: Vec<String> = requests.iter().map(origin).collect();
            let mut prepared = Vec::with_capacity(requests.len());
            for request in requests {
                let (operation, present) = self.operation(request, &origins).await?;
                prepared.push((
                    request.interrupt_id.clone(),
                    Prepared { operation, present },
                ));
            }
            let operations: Vec<Operation> = prepared
                .iter()
                .map(|(_, prepared)| prepared.operation.clone())
                .collect();
            let journal = Arc::clone(&self.runner.journal);
            let scope = self.scope.clone();
            let handle = tokio::task::spawn_blocking(move || {
                journal.prepare_batch(
                    &scope.generation,
                    &scope.thread_id,
                    operations,
                    scope.routine,
                )
            })
            .await
            .map_err(|_| ActionFailure::StateUnavailable)?
            .map_err(journal_failure)?;
            self.handle = Some(handle);
            self.prepared = prepared;
            self.origins = origins;
            Ok(())
        })
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionFailure>> {
        Box::pin(self.run(request, stop))
    }

    fn delivered<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        Box::pin(async move {
            let handle = self.handle.clone().ok_or(ActionFailure::ContextChanged)?;
            let expected: Vec<&str> = handle
                .operations
                .iter()
                .map(|operation| operation.interrupt_id.as_str())
                .collect();
            let given: Vec<&str> = requests
                .iter()
                .map(|request| request.interrupt_id.as_str())
                .collect();
            if expected != given {
                return Err(ActionFailure::ContextChanged);
            }
            let journal = Arc::clone(&self.runner.journal);
            tokio::task::spawn_blocking(move || journal.delivered(&handle))
                .await
                .map_err(|_| ActionFailure::StateUnavailable)?
                .map_err(journal_failure)?;
            self.release();
            Ok(())
        })
    }

    fn injected(&self) -> Vec<Secret> {
        self.injected.clone()
    }

    fn settle_undelivered(&mut self) -> BoxFuture<'_, Result<(), ActionFailure>> {
        Box::pin(async move {
            let journal = Arc::clone(&self.runner.journal);
            let generation = self.scope.generation.clone();
            tokio::task::spawn_blocking(move || journal.end_settled(&generation))
                .await
                .map_err(|_| ActionFailure::StateUnavailable)?
                .map_err(journal_failure)
        })
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionFailure>> {
        Box::pin(async move {
            let Some(handle) = self.handle.clone() else {
                return Ok(false);
            };
            let journal = Arc::clone(&self.runner.journal);
            let uncertain = !self.executing_here.is_empty();
            // A Routine run's uncertain outcome is held for recovery, never abandoned in-band (ADR-0092).
            if uncertain && self.scope.routine {
                return Ok(false);
            }
            let ended = tokio::task::spawn_blocking(move || {
                if uncertain {
                    journal.abandon_uncertain(&handle)
                } else {
                    journal.end(&handle)
                }
            })
            .await
            .map_err(|_| ActionFailure::StateUnavailable)?
            .map_err(journal_failure)?;
            if ended {
                self.release();
            }
            Ok(ended)
        })
    }
}

impl Batch {
    fn release(&mut self) {
        self.handle = None;
        self.prepared.clear();
        self.origins.clear();
        self.executing_here.clear();
    }
}
