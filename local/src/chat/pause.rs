//! A turn that pauses for a person or an Integration (`local/chat/pause.py`): its one challenge, the encrypted
//! continuation a restart restores it from, and the commit that makes the pause exist only when Stop did not win.

use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_action::challenges::{HumanRequirement, challenge_payload};
use team_action::executor::BatchScope;
use team_chat::orchestrator::{ChatHumanSuspension, ChatSuspension, executor_request};
use team_integrations::challenge::{ChallengeError, Pending};
use team_integrations::flow;
use team_integrations::pending::IntegrationRequirement;
use team_protocol::python::value::object_of;
use team_storage::private::epoch_seconds;
use tokio::time::Instant;

use super::continuation::{MAX_PLAINTEXT_BYTES, encode};
use super::continuation_store::StoreError;
use super::pending::{PendingLocalChat, Requirements};
use super::problem;
use super::segment::brain_thread_id;
use super::service::{Admitted, ChatService};
use crate::http::service::Problem;

/// The paused-turn state cannot be kept or read.
#[must_use]
pub const fn continuation_unavailable() -> Problem {
    Problem::new(
        503,
        "Team chat continuation state is unavailable",
        "chat-state-unavailable",
    )
}

/// Which challenge a paused turn waits on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A person's answer.
    Human,
    /// An Integration's connection.
    Integrations,
}

fn remaining(expires_at: Instant) -> Duration {
    expires_at.saturating_duration_since(Instant::now())
}

impl ChatService {
    /// Keeps one paused turn's continuation for the challenge `challenge_id`, expiring with it.
    pub(super) fn persist(
        &self,
        team_id: &str,
        challenge_id: &str,
        expires_at: Instant,
        requirements: &Requirements,
        pending: &PendingLocalChat,
    ) -> Result<(), Problem> {
        let left = remaining(expires_at);
        let seconds =
            i64::try_from(left.as_secs() + u64::from(left.subsec_nanos() > 0)).unwrap_or(0);
        if !(1..=super::continuation_store::MAX_TTL_SECONDS).contains(&seconds) {
            return Err(continuation_unavailable());
        }
        let (bound, payload) = encode(requirements, pending, MAX_PLAINTEXT_BYTES)
            .map_err(|_| continuation_unavailable())?;
        let expires = epoch_seconds(self.parts.clock.as_ref()) + seconds;
        self.parts
            .continuations
            .put(
                team_id,
                requirements.kind(),
                challenge_id,
                expires,
                &bound,
                &payload,
            )
            .map(|_| ())
            .map_err(|_| continuation_unavailable())
    }

    /// Deletes the Team's continuation, or only `challenge`'s; another challenge's belongs to a newer turn.
    pub(super) fn delete_continuation(
        &self,
        team_id: &str,
        challenge: Option<&str>,
    ) -> Result<bool, Problem> {
        match self.parts.continuations.delete(team_id, challenge) {
            Ok(deleted) => Ok(deleted),
            Err(StoreError::NotFound) => Ok(false),
            Err(_) => Err(continuation_unavailable()),
        }
    }

    /// Removes exactly the Action batch a paused turn holds; a newer turn's batch in its generation stays.
    pub(super) async fn purge(
        &self,
        team_id: &str,
        pending: &PendingLocalChat,
    ) -> Result<(), Problem> {
        let generation = pending.identity.network_id().to_owned();
        let thread_id = brain_thread_id(&self.parts.space_id, team_id, &generation)?;
        let mut batch = self.parts.actions.batch(BatchScope {
            team_id: team_id.to_owned(),
            generation,
            thread_id,
            routine: false,
            transcripts: Vec::new(),
        });
        let requests: Vec<_> = pending
            .continuation
            .turn
            .actions
            .iter()
            .map(|request| executor_request(request, &[]))
            .collect();
        batch
            .prepare(&requests)
            .await
            .map_err(|_| problem::action_state_unavailable())?;
        batch
            .terminate()
            .await
            .map_err(|_| problem::action_state_unavailable())?;
        Ok(())
    }

    /// Ends a paused human turn without its Action: its challenge, continuation, and batch go, and Stop decides
    /// whether the ending commits.
    pub(super) async fn human_failure(
        &self,
        team_id: &str,
        admitted: &Admitted,
        pending: &PendingLocalChat,
        reason: &str,
    ) -> Result<Value, Problem> {
        let _ = self.human.cancel_team(team_id);
        self.delete_continuation(team_id, None)?;
        self.purge(team_id, pending).await?;
        if admitted.execution.commit(|| Ok::<(), ()>(())).is_none() {
            return Err(problem::chat_stopped());
        }
        Ok(object_of(vec![
            ("team_id", Value::String(team_id.to_owned())),
            ("status", Value::String("human-denied".to_owned())),
            ("reason", Value::String(reason.to_owned())),
        ]))
    }

    /// The public descriptor of a human challenge.
    pub(super) fn human_response(
        challenge: &Pending<HumanRequirement, PendingLocalChat>,
    ) -> Result<Value, Problem> {
        challenge_payload(challenge).map_err(|_| problem::human_request_invalid())
    }

    /// Pauses the turn for its one person's answer.
    pub(super) async fn pause_human(
        &self,
        team_id: &str,
        admitted: &Admitted,
        suspension: &ChatHumanSuspension,
        requirement: HumanRequirement,
        pending: PendingLocalChat,
    ) -> Result<Value, Problem> {
        let canonical = team_protocol::python::json::canonical;
        if canonical(&requirement.request.payload()) != canonical(&suspension.request) {
            return self
                .human_failure(team_id, admitted, &pending, "request-invalid")
                .await;
        }
        if requirement.request.kind.starts_with("auth:")
            && requirement.request.kind != "auth:password"
        {
            return self
                .human_failure(team_id, admitted, &pending, "authentication-unavailable")
                .await;
        }
        let _team = self.parts.coordination.locks.team(team_id).await;
        let challenge = self
            .human
            .create(team_id, requirement.clone(), pending.clone())
            .map_err(|error| match error {
                ChallengeError::AlreadyPending | ChallengeError::Capacity => Problem::new(
                    409,
                    "Action human request is already pending",
                    "human-request-conflict",
                ),
                _ => problem::internal_error(),
            })?;
        let requirements = Requirements::Human(Box::new(requirement));
        if let Err(failure) = self.persist(
            team_id,
            &challenge.id,
            challenge.expires_at,
            &requirements,
            &pending,
        ) {
            let _ = self.human.cancel_team(team_id);
            self.purge(team_id, &pending).await?;
            return Err(failure);
        }
        if admitted.execution.commit(|| Ok::<(), ()>(())).is_none() {
            let _ = self.human.cancel_team(team_id);
            self.delete_continuation(team_id, Some(&challenge.id))?;
            // A Stop that came first found no challenge to end this turn with, so its own batch goes here (ADR-0038).
            self.purge(team_id, &pending).await?;
            return Err(problem::chat_stopped());
        }
        Self::human_response(&challenge)
    }

    /// The public descriptor of an Integration challenge, against the Team's running Assistants.
    pub(super) async fn integration_response(
        &self,
        challenge: &Pending<Vec<IntegrationRequirement>, PendingLocalChat>,
    ) -> Result<Value, Problem> {
        let scope = self.parts.team.scope(&challenge.team_id, true).await?;
        let views: Vec<_> = scope
            .assistants
            .iter()
            .map(super::ports::ActiveAssistant::view)
            .collect();
        flow::challenge_payload(challenge, &views).map_err(|_| {
            Problem::new(
                409,
                "Assistant integration contract changed; retry the message",
                "assistant-integration-contract-invalid",
            )
        })
    }

    /// Pauses the turn's whole batch until its Integrations are connected.
    pub(super) async fn pause_integrations(
        &self,
        team_id: &str,
        admitted: &Admitted,
        suspension: &ChatSuspension,
        requirements: Vec<IntegrationRequirement>,
        pending: PendingLocalChat,
    ) -> Result<Value, Problem> {
        let _ = suspension;
        let store = Arc::clone(&self.parts.integration_challenges);
        let challenge = {
            let _team = self.parts.coordination.locks.team(team_id).await;
            let challenge = store
                .create(team_id, requirements.clone(), pending.clone())
                .map_err(|error| match error {
                    ChallengeError::AlreadyPending | ChallengeError::Capacity => Problem::new(
                        409,
                        "Assistant integration request is already pending",
                        "assistant-integration-challenge-conflict",
                    ),
                    _ => problem::internal_error(),
                })?;
            let kept = Requirements::Integrations(requirements);
            if let Err(failure) = self.persist(
                team_id,
                &challenge.id,
                challenge.expires_at,
                &kept,
                &pending,
            ) {
                let _ = store.cancel_team(team_id);
                return Err(failure);
            }
            if admitted.execution.commit(|| Ok::<(), ()>(())).is_none() {
                let _ = store.cancel_team(team_id);
                self.delete_continuation(team_id, Some(&challenge.id))?;
                return Err(problem::chat_stopped());
            }
            challenge
        };
        self.integration_response(&challenge).await
    }

    /// Ends exactly the paused turn whose context drifted, when its challenge is still the live one, and refuses. A
    /// challenge no longer live has another owner (Stop, expiry, an answer, or a reissue), and nothing of it is touched.
    pub(super) async fn end_drifted(
        &self,
        team_id: &str,
        kind: Kind,
        challenge_id: &str,
    ) -> Problem {
        let ended = match kind {
            Kind::Human => match self.human.current(team_id) {
                Ok(Some(live)) if live.id == challenge_id => {
                    let _ = self.human.withdraw_team(team_id);
                    Some(Some(live.payload.clone()))
                }
                _ => None,
            },
            Kind::Integrations => match self.parts.integration_challenges.current(team_id) {
                Ok(Some(live)) if live.id == challenge_id => {
                    let _ = self.parts.integration_challenges.withdraw_team(team_id);
                    let _ = self.parts.pkce.cancel_team(team_id);
                    Some(None)
                }
                _ => None,
            },
        };
        let Some(held) = ended else {
            return problem::team_context_changed();
        };
        if let Err(failure) = self.delete_continuation(team_id, Some(challenge_id)) {
            return failure;
        }
        if let Some(pending) = held
            && let Err(failure) = self.purge(team_id, &pending).await
        {
            return failure;
        }
        problem::team_context_changed()
    }
}
