//! The Routine routes (`local/http/routine.py`, ADR-0086): Admin's scheduler claims runs and delivers notices under
//! the Team bearer; its routine identity runs one leased run under a routine assertion bound to the run's lease; a
//! Supervisor manages Routines, answers frozen runs, and settles held runs through their recovery cards.

use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_protocol::python::value::{object_of, small_int, string};
use team_protocol::routine::{ROUTINE_ID_RE, valid_revision};
use team_protocol::routine_run::{
    SNAPSHOT_RE, canonical_challenge_open, canonical_claim_request, canonical_segment_request,
};
use team_routine::record::Lease;

use super::RoutineService;
use crate::audit::{Principal as AuditPrincipal, PrincipalClass};
use crate::chat::inference::team_id;
use crate::chat::problem;
use crate::chat::service::{ChatService, stream};
use crate::http::operation::Operation;
use crate::http::server::{ChallengeBook, PendingChallenge, current_request};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service};
use crate::routine::diagnostics::view::{self, StepsError as RunStepsError};
use crate::routine::manage::{self, StepsError};
use crate::routine::notices::{self, AckError};
use crate::routine::proposal::confirm::Answering;
use crate::routine::store::TeamLock;

fn json(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

fn empty(call: &Call<'_>) -> Result<(), Problem> {
    match &call.body {
        Body::Json(Value::Object(members)) if members.is_empty() => Ok(()),
        _ => Err(problem::empty_body_required()),
    }
}

fn body<'a>(call: &'a Call<'_>) -> &'a Value {
    match &call.body {
        Body::Json(body) => body,
        _ => &Value::Null,
    }
}

fn run_id<'a>(call: &Call<'a>, missing: fn() -> Problem) -> Result<&'a str, Problem> {
    call.route
        .param("run_id")
        .filter(|run| ROUTINE_ID_RE.is_full(run))
        .ok_or_else(missing)
}

fn count(text: &str) -> Option<i64> {
    let canonical = text == "0" || (!text.starts_with('0') && (1..=10).contains(&text.len()));
    if canonical && text.bytes().all(|byte| byte.is_ascii_digit()) {
        text.parse().ok()
    } else {
        None
    }
}

/// A Supervisor alone answers a recovery card.
fn person(call: &Call<'_>) -> Result<(), Problem> {
    match call.principal {
        Principal::Supervisor { .. } => Ok(()),
        _ => Err(problem::routine_card_person_required()),
    }
}

impl RoutineService {
    async fn claim_call(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let request = canonical_claim_request(body(call)).ok_or(Problem::new(
            422,
            "Routine claim is invalid",
            "invalid-body",
        ))?;
        let long = request.get("long") == Some(&Value::Bool(true));
        let run = self.claim_routine_run(long).await?;
        let next = match &run {
            Some(_) => None,
            None => self.next_routine_due().await?,
        };
        Ok(object_of(vec![
            ("run", run.unwrap_or(Value::Null)),
            (
                "next_due_at",
                next.map_or(Value::Null, team_protocol::python::value::int),
            ),
        ]))
    }

    async fn notices_call(&self) -> Result<Value, Problem> {
        let batch =
            notices::batch(&self.parts.store).map_err(|_| problem::routine_state_unavailable())?;
        for team_id in &batch.skipped {
            self.audit(
                "routine-notices",
                false,
                team_id,
                "routine-state-unavailable",
            )
            .await;
        }
        Ok(batch.body)
    }

    fn acknowledge_call(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let lost = |lock: &TeamLock<'_>, state: &team_routine::record::TeamRoutines| {
            self.lost_in(lock, state)
        };
        notices::acknowledge(&self.parts.store, &lost, body(call)).map_err(
            |error| match error {
                AckError::Invalid => problem::routine_deliveries_invalid(),
                AckError::InvalidTeam => Problem::new(422, "invalid Team id", "invalid-team-id"),
                AckError::Unavailable => problem::routine_state_unavailable(),
            },
        )?;
        Ok(object_of(vec![("acknowledged", Value::Bool(true))]))
    }

    fn steps_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let param = |name| call.route.param(name).unwrap_or_default();
        let (routine_id, revision, offset) = (
            param("routine_id"),
            count(param("revision")),
            count(param("offset")),
        );
        let (true, Some(revision), Some(offset)) =
            (ROUTINE_ID_RE.is_full(routine_id), revision, offset)
        else {
            return Err(problem::routine_steps_not_found());
        };
        if !valid_revision(&team_protocol::python::value::int(revision)) {
            return Err(problem::routine_steps_not_found());
        }
        manage::steps(&self.parts.store, team, routine_id, revision, offset).map_err(|error| {
            match error {
                StepsError::NotFound => problem::routine_not_found(),
                StepsError::RevisionChanged => {
                    Problem::new(409, "Routine revision changed", "routine-revision-changed")
                }
                StepsError::StepsNotFound => problem::routine_steps_not_found(),
                StepsError::Unavailable => problem::routine_state_unavailable(),
            }
        })
    }

    async fn run_steps_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let missing = problem::routine_run_steps_not_found;
        let run = run_id(call, problem::routine_run_not_found)?;
        let snapshot = call.route.param("snapshot").unwrap_or_default();
        let offset = count(call.route.param("offset").unwrap_or_default()).ok_or_else(missing)?;
        if snapshot != "latest" && !SNAPSHOT_RE.is_full(snapshot) {
            return Err(missing());
        }
        let incarnation = self.parts.team.network(team).await?;
        let diagnostics = &self.parts.diagnostics;
        view::run_steps(
            diagnostics,
            team,
            &incarnation,
            run,
            snapshot,
            offset,
            self.now(),
        )
        .map_err(|error| match error {
            RunStepsError::Changed => {
                Problem::new(409, "Routine run records changed", "routine-run-changed")
            }
            RunStepsError::NotFound => missing(),
            RunStepsError::Unavailable => problem::routine_diagnostics_unavailable(),
        })
    }

    async fn diagnostics_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let run = run_id(call, problem::routine_run_not_found)?;
        let incarnation = self.parts.team.network(team).await?;
        view::run_diagnostics(&self.parts.diagnostics, team, &incarnation, run, self.now())
            .map_err(|_| problem::routine_diagnostics_unavailable())
    }

    async fn card_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        person(call)?;
        let incident = run_id(call, problem::routine_incident_unavailable)?;
        if call.operation() == Operation::RoutineCardOpen {
            empty(call)?;
            return self.open_routine_card(team, incident);
        }
        self.answer_routine_card(team, incident, &call.body).await
    }

    async fn proposal_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let proposal = call
            .route
            .param("proposal_id")
            .filter(|proposal| ROUTINE_ID_RE.is_full(proposal))
            .ok_or(Problem::new(
                404,
                "Routine card is unavailable",
                "routine-proposal-expired",
            ))?;
        let context = current_request();
        let actor = AuditPrincipal {
            id: context
                .as_ref()
                .and_then(|context| context.principal_id.clone()),
            class: PrincipalClass::Human,
            credential_state: context.as_ref().map(|context| context.credential_state),
            trace_id: Some(call.trace_id.clone()),
        };
        let supervisor = match &call.principal {
            Principal::Supervisor { supervisor_id, .. } => Some(supervisor_id.as_str()),
            _ => None,
        };
        let parts = &self.parts;
        let answering = Answering {
            team: parts.team.as_ref(),
            locks: &parts.coordination.locks,
            audit: parts.audit.as_ref(),
            actor: &actor,
            supervisor,
        };
        let routine = &parts.chat.parts().routine;
        if call.operation() == Operation::RoutineProposalConfirm {
            empty(call)?;
            return routine.confirm(&answering, team, proposal).await;
        }
        routine.revoke(&answering, team, proposal).await
    }

    async fn challenge_call(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let run = run_id(call, problem::routine_run_not_found)?;
        let opening = canonical_challenge_open(body(call));
        let locale = opening
            .as_ref()
            .and_then(|opening| opening.get("locale").and_then(string))
            .ok_or(problem::challenge_locale_only())?
            .to_owned();
        self.open_routine_challenge(team, run, &locale).await
    }

    /// A Supervisor's operations on one Team's Routines.
    async fn session(&self, call: &Call<'_>, team: &str) -> Result<Value, Problem> {
        let routine = || call.route.param("routine_id").unwrap_or_default();
        match call.operation() {
            Operation::RoutineList => manage::list(&self.parts.store, team)
                .map_err(|_| problem::routine_state_unavailable()),
            Operation::RoutineSteps => self.steps_call(call, team),
            Operation::RoutineRunSteps => self.run_steps_call(call, team).await,
            Operation::RoutineDiagnostics => self.diagnostics_call(call, team).await,
            Operation::RoutineDelete => self.delete_routine(team, routine()).await,
            Operation::RoutineCardOpen | Operation::RoutineCardAnswer => {
                self.card_call(call, team).await
            }
            Operation::RoutineResume | Operation::RoutinePause => {
                empty(call)?;
                let paused = call.operation() == Operation::RoutinePause;
                self.set_routine_paused(team, routine(), paused).await
            }
            Operation::RoutineProposalConfirm | Operation::RoutineProposalRevoke => {
                self.proposal_call(call, team).await
            }
            Operation::RoutineChallengeOpen => self.challenge_call(call, team).await,
            _ => Err(problem::internal_error()),
        }
    }

    /// One segment of a leased run under Admin's routine identity; its lease is checked by the run itself.
    fn run_call(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let Principal::Routine {
            key_fingerprint,
            lease_sha256,
        } = &call.principal
        else {
            return Err(Problem::new(
                403,
                "Routine authority is required",
                "invalid-routine",
            ));
        };
        let team = team_id(call)?.to_owned();
        let run = run_id(call, problem::routine_run_not_found)?.to_owned();
        let shape = Problem::new(
            422,
            "a segment names the claimed revision and plan",
            "invalid-body",
        );
        let claimed = canonical_segment_request(body(call)).ok_or_else(|| shape.clone())?;
        let revision = claimed
            .get("revision")
            .and_then(small_int)
            .ok_or_else(|| shape.clone())?;
        let digest = claimed
            .get("plan_digest")
            .and_then(string)
            .ok_or(shape)?
            .to_owned();
        let lease = Lease {
            sha256: lease_sha256.clone(),
            key: key_fingerprint.clone(),
        };
        let service = self.clone();
        Ok(stream(move |progress| async move {
            service
                .run_routine(&team, &run, lease, (revision, digest), &progress)
                .await
        }))
    }

    /// A Supervisor's answer or Integration resume of a frozen run, streamed like the chat turn it continues.
    fn resume_call(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let team = team_id(call)?.to_owned();
        let run = run_id(call, problem::routine_run_not_found)?.to_owned();
        let service = self.clone();
        if call.operation() == Operation::RoutineHumanSubmit {
            let answer = call.body.clone();
            return Ok(stream(move |progress| async move {
                service
                    .resume_routine_human(&team, &run, &answer, &progress)
                    .await
            }));
        }
        empty(call)?;
        Ok(stream(move |progress| async move {
            service
                .resume_routine_integrations(&team, &run, &progress)
                .await
        }))
    }

    async fn answer(&self, call: Call<'_>) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::RoutineClaim => self.claim_call(&call).await.map(json),
            Operation::RoutineNotices => self.notices_call().await.map(json),
            Operation::RoutineNoticeAck => self.acknowledge_call(&call).map(json),
            Operation::RoutineRun => self.run_call(&call),
            Operation::RoutineHumanSubmit | Operation::RoutineIntegrationSubmit => {
                self.resume_call(&call)
            }
            _ => {
                let team = team_id(&call)?;
                self.session(&call, team).await.map(json)
            }
        }
    }
}

impl Service for RoutineService {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(self.answer(call))
    }
}

/// Where an approval finds the challenge it answers: chat's for `chat-human-submit`, a frozen run's for
/// `routine-human-submit`.
#[derive(Clone, Debug)]
pub struct Challenges {
    /// The chat service.
    pub chat: ChatService,
    /// The Routine service.
    pub routines: RoutineService,
}

impl ChallengeBook for Challenges {
    fn pending<'a>(
        &'a self,
        operation: Operation,
        team_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<PendingChallenge>, Problem>> {
        Box::pin(async move {
            match operation {
                Operation::ChatHumanSubmit => self.chat.pending(operation, team_id).await,
                Operation::RoutineHumanSubmit => Ok(self
                    .routines
                    .human_challenges()
                    .current(team_id)
                    .ok()
                    .flatten()
                    .map(|challenge| PendingChallenge {
                        kind: challenge.metadata.request.kind.clone(),
                        challenge_id: challenge.id.clone(),
                    })),
                _ => Ok(None),
            }
        })
    }
}

/// The book of one chat and one Routine service.
#[must_use]
pub fn challenges(chat: ChatService, routines: RoutineService) -> Arc<dyn ChallengeBook> {
    Arc::new(Challenges { chat, routines })
}
