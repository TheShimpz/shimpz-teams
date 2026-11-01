//! The durable batch a chat segment runs its Actions through: it marks which Team's turn has an Action executing, so
//! Stop reports it, keeps a recording turn's successful calls, and ends the batch of a failed turn without losing the
//! turn's own cause.

use team_action::executor::{ActionBatch, ActionError, ActionRequest, InvokeOutcome};
use team_chat::orchestrator::ChatError;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_protocol::payload::cause::Cause;

use super::super::ports::ActiveAssistant;
use super::super::problem;
use super::super::service::{ChatService, chat_problem};
use crate::http::service::Problem;

/// A durable batch that records which Team's turn has an Action executing, so Stop reports it.
pub(super) struct Tracked {
    pub(super) inner: Box<dyn ActionBatch>,
    pub(super) service: ChatService,
    pub(super) team_id: String,
    pub(super) token: String,
    /// The send a recording turn keeps each successful call in, with the Assistants it runs against.
    pub(super) recording: Option<(String, Vec<ActiveAssistant>)>,
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
    fn record(&self, request: &ActionRequest, outcome: &Result<InvokeOutcome, ActionError>) {
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
    ) -> BoxFuture<'a, Result<(), ActionError>> {
        self.inner.prepare(requests)
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionError>> {
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
    ) -> BoxFuture<'a, Result<(), ActionError>> {
        self.inner.delivered(requests)
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionError>> {
        self.inner.terminate()
    }

    fn settle_undelivered(&mut self) -> BoxFuture<'_, Result<(), ActionError>> {
        self.inner.settle_undelivered()
    }

    fn injected(&self) -> Vec<Secret> {
        self.inner.injected()
    }
}

/// A failed turn's problem once its batch is ended; a batch the journal could not end answers that, with the turn's
/// own cause kept.
pub(super) async fn failed_turn(batch: &mut Tracked, error: ChatError) -> Problem {
    match batch.terminate().await {
        Err(journal) => problem::action_state_unavailable().with_cause(unended(&error, journal)),
        Ok(_) => chat_problem(error),
    }
}

/// The cause of a turn whose batch could not end after it failed: the turn's own failure stays the cause, the
/// journal's failure what became of its cleanup; without one, the journal's own cause.
fn unended(error: &ChatError, journal: ActionError) -> Option<Cause> {
    let initiating = match error {
        ChatError::Action(failed) => failed.cause,
        _ => None,
    };
    initiating.map_or(journal.cause, |cause| {
        Some(team_action::executor::journal_cleanup(cause))
    })
}
