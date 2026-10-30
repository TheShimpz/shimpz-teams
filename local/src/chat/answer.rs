//! A person answers a paused chat turn (`local/chat/human.py`, `local/chat/api.py`): the pending challenge, opened in
//! one interface language, a decision or an Integration connected, and the turn's admitted remainder.

use std::sync::Arc;

use strict_json::Value;
use team_action::challenges::{HumanRequirement, render_copy};
use team_action::human::{HumanRequest, append_response};
use team_assistant::language::catalog_digest;
use team_assistant::manifest::StoredInputDeclaration;
use team_chat::progress::Reporter;
use team_core::future::BoxFuture;
use team_integrations::challenge::{Claim, Pending};
use team_integrations::flow::{Requested, requirements_for_batch};
use team_protocol::python::value::{object_of, string};
use team_protocol::routine_run::canonical_challenge_open;

use super::inference::team_id;
use super::pause::Kind;
use super::pending::PendingLocalChat;
use super::ports::ActiveAssistant;
use super::problem;
use super::segment::{SegmentRequest, Setup};
use super::service::{Admitted, ChatService, stream};
use super::turn::{Request, Response};
use crate::http::operation::Operation;
use crate::http::server::{ChallengeBook, PendingChallenge};
use crate::http::service::{Answer, Body, Call, Problem};

type HumanChallenge = Arc<Pending<HumanRequirement, PendingLocalChat>>;

/// The English help text and help link the reviewed binding declares for the Stored Input a password request names.
#[must_use]
pub fn declared_help(
    request: &HumanRequest,
    stored_inputs: &[StoredInputDeclaration],
) -> Option<(String, String)> {
    if request.kind != "input:password" {
        return None;
    }
    let name = request.stored_input.as_deref()?;
    let declaration = stored_inputs
        .iter()
        .find(|declaration| declaration.id == name)?;
    Some((
        declaration.description.clone(),
        declaration.help_url.clone(),
    ))
}

/// Whether the requirement's Assistant still runs the catalog and pack its copy was rendered from (ADR-0091).
fn copy_binding_current(requirement: &HumanRequirement, assistants: &[ActiveAssistant]) -> bool {
    assistants
        .iter()
        .find(|active| active.id() == requirement.assistant_id)
        .is_some_and(|active| {
            let current = catalog_digest(&active.binding.contract.machine_contract);
            requirement.copy.catalog_digest == current
                && requirement.copy.pack_digest == active.pack.pack_digest
        })
}

/// The requirement's copy rendered in `locale` from exactly the binding's pack it was rendered from before.
fn rendered_in(
    requirement: &HumanRequirement,
    setup: &Setup,
    locale: &str,
) -> Result<HumanRequirement, Problem> {
    let active = setup
        .assistants
        .iter()
        .find(|active| active.id() == requirement.assistant_id);
    let pack = active
        .map(|active| Arc::clone(&active.pack))
        .ok_or(problem::human_request_invalid())?;
    let copy = &requirement.copy;
    if (pack.catalog_digest.as_str(), pack.pack_digest.as_str())
        != (copy.catalog_digest.as_str(), copy.pack_digest.as_str())
    {
        return Err(problem::human_request_invalid());
    }
    let copy = render_copy(
        &requirement.request,
        &pack,
        locale,
        requirement.help_text.as_deref(),
    )
    .map_err(|_| problem::human_request_invalid())?;
    Ok(HumanRequirement {
        copy,
        ..requirement.clone()
    })
}

fn none(team_id: &str) -> Value {
    object_of(vec![
        ("team_id", Value::String(team_id.to_owned())),
        ("status", Value::String("none".to_owned())),
    ])
}

/// A person's answer: its challenge, `submit` or `deny`, and its value.
fn resume_body(body: &Body) -> Result<(String, bool, Value), Problem> {
    let Body::Json(body @ Value::Object(members)) = body else {
        return Err(problem::human_response_invalid());
    };
    let submit = match body.get("decision").and_then(string) {
        Some("submit") => true,
        Some("deny") => false,
        _ => return Err(problem::human_response_invalid()),
    };
    let expected = if submit { 3 } else { 2 };
    let challenge = body.get("challenge_id").and_then(string);
    let (Some(challenge), true) = (challenge, members.len() == expected) else {
        return Err(problem::human_response_invalid());
    };
    if submit && body.get("value").is_none() {
        return Err(problem::human_response_invalid());
    }
    let value = body.get("value").cloned().unwrap_or(Value::Null);
    Ok((challenge.to_owned(), submit, value))
}

impl ChatService {
    /// Deletes what expired human pauses, every Team's or one Team's, left behind.
    pub(super) async fn expire_human(&self, team_id: Option<&str>) -> Result<(), Problem> {
        let expired = self.human.drain_expired(team_id).unwrap_or_default();
        for challenge in expired {
            self.delete_continuation(&challenge.team_id, Some(&challenge.id))?;
            self.purge(&challenge.team_id, &challenge.payload).await?;
        }
        Ok(())
    }

    /// The paused turn's Team now, ending the turn when its provider changed; the caller holds the Team's stripe.
    async fn paused_setup(
        &self,
        team_id: &str,
        provider: &str,
        (kind, challenge_id): (Kind, &str),
        pending: &PendingLocalChat,
    ) -> Result<Setup, Problem> {
        if pending.provider.as_deref() != Some(provider) {
            return Err(self.end_drifted(team_id, kind, challenge_id).await);
        }
        match self
            .setup_unlocked(team_id, &pending.file_ids, provider, &pending.assistant_ids)
            .await
        {
            Err(failure) if failure.code == Some("inference-provider-mismatch") => {
                Err(self.end_drifted(team_id, kind, challenge_id).await)
            }
            other => other,
        }
    }

    /// The challenge's paused turn and the Team's running Assistants, which must be exactly as the turn left them.
    async fn validate_human(
        &self,
        team_id: &str,
        provider: &str,
        challenge: &HumanChallenge,
    ) -> Result<Setup, Problem> {
        let pending = &challenge.payload;
        let setup = self
            .paused_setup(team_id, provider, (Kind::Human, &challenge.id), pending)
            .await?;
        if setup.identity() != pending.identity
            || !copy_binding_current(&challenge.metadata, &setup.assistants)
        {
            return Err(self.end_drifted(team_id, Kind::Human, &challenge.id).await);
        }
        Ok(setup)
    }

    /// The pending request rendered in `locale`: the same canonical request re-rendered from the same binding's pack,
    /// as a fresh challenge that keeps the expiry and continuation; the caller holds the Team's stripe.
    pub(super) async fn relocalized(
        &self,
        team_id: &str,
        challenge: &HumanChallenge,
        locale: &str,
    ) -> Result<HumanChallenge, Problem> {
        let provider = challenge.payload.provider.clone().unwrap_or_default();
        let setup = self.validate_human(team_id, &provider, challenge).await?;
        if challenge.metadata.copy.locale == locale {
            return Ok(Arc::clone(challenge));
        }
        let fresh = rendered_in(&challenge.metadata, &setup, locale)?;
        let reissued = self
            .human
            .reissue(team_id, &challenge.id, fresh.clone())
            .map_err(|error| match error {
                team_integrations::challenge::ChallengeError::NotFound => {
                    problem::human_request_expired()
                }
                _ => problem::human_request_invalid(),
            })?;
        let kept = super::pending::Requirements::Human(Box::new(fresh));
        let (id, expires, pending) = (&reissued.id, reissued.expires_at, &reissued.payload);
        if let Err(failure) = self.persist(team_id, id, expires, &kept, pending) {
            // The earlier challenge no longer answers, so a fresh one that cannot be kept ends the paused turn.
            let _ = self.human.cancel_team(team_id);
            self.delete_continuation(team_id, None)?;
            self.purge(team_id, pending).await?;
            return Err(failure);
        }
        Ok(reissued)
    }

    /// The Team's active human challenge, if any.
    pub(super) async fn pending_human(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        self.parts.team.network(team_id).await?;
        self.expire_human(None).await?;
        match self.human.current(team_id) {
            Ok(Some(challenge)) => Self::human_response(&challenge),
            _ => Ok(none(team_id)),
        }
    }

    /// Opens the Team's pending human challenge in the Admin interface language (ADR-0091).
    pub(super) async fn open_human(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        let opening = match &call.body {
            Body::Json(body) => canonical_challenge_open(body),
            _ => None,
        };
        let locale = opening
            .as_ref()
            .and_then(|opening| opening.get("locale").and_then(string).map(str::to_owned))
            .ok_or(problem::challenge_locale_only())?;
        self.parts.team.network(team_id).await?;
        self.expire_human(None).await?;
        let _team = self.parts.coordination.locks.team(team_id).await;
        let Ok(Some(challenge)) = self.human.current(team_id) else {
            return Ok(none(team_id));
        };
        let opened = self.relocalized(team_id, &challenge, &locale).await?;
        Self::human_response(&opened)
    }

    /// Consumes one exact decision and replays its Action, streaming the turn's remainder.
    pub(super) fn submit_human(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let request = self.request(call)?;
        let answer = resume_body(&call.body)?;
        let service = self.clone();
        Ok(stream(move |progress| async move {
            service.answer_human(request, answer, progress).await
        }))
    }

    async fn answer_human(
        &self,
        request: Request,
        answer: (String, bool, Value),
        progress: Reporter,
    ) -> Result<Value, Problem> {
        let team_id = request.team_id.clone();
        let admitted = self.admit(&team_id)?;
        let (pending, transcripts) = {
            let _team = self.parts.coordination.locks.team(&team_id).await;
            let Ok(challenge) = self.human.get(&team_id, &answer.0) else {
                self.expire_human(None).await?;
                return Err(problem::human_request_expired());
            };
            let setup = self
                .validate_human(&team_id, &request.credential.0, &challenge)
                .await?;
            let pending = challenge.payload.clone();
            if !answer.1 {
                let _ = self.human.claim(&team_id, &challenge.id);
                self.delete_continuation(&team_id, Some(&challenge.id))?;
                return self
                    .human_failure(&team_id, &admitted, &pending, "denied")
                    .await;
            }
            let transcripts =
                self.admit_answer(&team_id, &challenge, &answer.2, &setup.assistants)?;
            (pending, transcripts)
        };
        for transcript in &transcripts.0 {
            self.parts.transcripts.set(&team_id, transcript.clone());
        }
        self.continue_paused(&admitted, request, pending, transcripts, progress)
            .await
    }

    /// Admits one response, sealing a Stored Input answer while the challenge is still held, so it is consumed only
    /// once its value is kept.
    fn admit_answer(
        &self,
        team_id: &str,
        challenge: &HumanChallenge,
        value: &Value,
        assistants: &[ActiveAssistant],
    ) -> Result<(Vec<team_action::human::ActionTranscript>, usize), Problem> {
        let pending = &challenge.payload;
        let requirement = &challenge.metadata;
        let admission = append_response(
            &pending.transcripts,
            &requirement.interrupt_id,
            &requirement.request,
            value,
            pending.requests_used,
        )
        .map_err(|_| problem::human_response_mismatch())?;
        let claimed = self.human.claim_after(team_id, &challenge.id, |claimed| {
            match &admission.stored_input {
                Some((name, secret)) => {
                    super::stored::seal(self, team_id, claimed, assistants, (name, secret))
                }
                None => Ok(()),
            }
        });
        match claimed {
            Ok(_) => {}
            Err(Claim::Commit(failure)) => return Err(failure),
            Err(Claim::Store(_)) => return Err(problem::human_request_expired()),
        }
        self.delete_continuation(team_id, Some(&challenge.id))?;
        Ok((admission.transcripts, admission.requests_used))
    }

    /// Runs the admitted remainder of a paused turn from the transcripts and request count it starts from.
    pub(super) async fn continue_paused(
        &self,
        admitted: &Admitted,
        request: Request,
        pending: PendingLocalChat,
        transcripts: (Vec<team_action::human::ActionTranscript>, usize),
        progress: Reporter,
    ) -> Result<Value, Problem> {
        let segment = SegmentRequest {
            team_id: request.team_id.clone(),
            token: admitted.execution.token().to_owned(),
            stop: admitted.execution.stop().clone(),
            file_ids: pending.file_ids.clone(),
            assistant_ids: pending.assistant_ids.clone(),
            credential: request.credential.clone(),
            message: None,
            conversation: Vec::new(),
            locale: pending.locale.clone(),
            continuation: Some(pending.continuation.clone()),
            expected: Some(pending.identity.clone()),
            progress: self.recorded_progress(
                &request.team_id,
                pending.recording.as_deref(),
                progress,
            ),
            model: pending.model.clone(),
            usage: Arc::clone(&request.usage),
            actor: request.actor.clone(),
            recording: pending.recording.clone(),
            refine: None,
        };
        let segment = self.run_segment(&segment).await?;
        let usage = pending
            .usage
            .clone()
            .unwrap_or_else(|| team_inference::usage::TurnUsage::start(self.now_ms()));
        let response = Response {
            request,
            segment,
            selection: (pending.assistant_ids.clone(), pending.file_ids.clone()),
            transcripts,
            usage,
            locale: pending.locale.clone(),
            source: pending.source.clone(),
            recording: pending.recording.clone(),
        };
        self.respond(admitted, response).await
    }

    /// The Team's pending Integration challenge, if any.
    pub(super) async fn pending_integrations(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        self.parts.team.network(team_id).await?;
        match self.parts.integration_challenges.current(team_id) {
            Ok(Some(challenge)) => self.integration_response(&challenge).await,
            _ => Ok(none(team_id)),
        }
    }

    /// Resumes a turn whose Integrations are now connected, streaming its remainder.
    pub(super) fn submit_integrations(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let request = self.request(call)?;
        let shape = Problem::new(
            422,
            "Assistant integration resume requires only challenge_id",
            "invalid-body",
        );
        let challenge_id = match &call.body {
            Body::Json(body @ Value::Object(members)) if members.len() == 1 => {
                body.get("challenge_id").and_then(string)
            }
            _ => None,
        }
        .ok_or(shape)?
        .to_owned();
        let service = self.clone();
        Ok(stream(move |progress| async move {
            service
                .answer_integrations(request, challenge_id, progress)
                .await
        }))
    }

    async fn answer_integrations(
        &self,
        request: Request,
        challenge_id: String,
        progress: Reporter,
    ) -> Result<Value, Problem> {
        let team_id = request.team_id.clone();
        let admitted = self.admit(&team_id)?;
        let pending = {
            let _team = self.parts.coordination.locks.team(&team_id).await;
            let store = &self.parts.integration_challenges;
            let challenge = store
                .get(&team_id, &challenge_id)
                .map_err(|_| problem::integration_challenge_expired())?;
            let pending = challenge.payload.clone();
            let kind = (Kind::Integrations, challenge.id.as_str());
            let setup = self
                .paused_setup(&team_id, &request.credential.0, kind, &pending)
                .await?;
            if setup.identity() != pending.identity {
                return Err(self
                    .end_drifted(&team_id, Kind::Integrations, &challenge.id)
                    .await);
            }
            let views: Vec<_> = setup.assistants.iter().map(ActiveAssistant::view).collect();
            let actions = &pending.continuation.turn.actions;
            let requested: Vec<Requested<'_>> = actions
                .iter()
                .map(|action| Requested {
                    assistant_id: &action.assistant_id,
                    action: &action.action,
                })
                .collect();
            let missing =
                requirements_for_batch(&team_id, &views, &requested, &self.parts.integrations)
                    .map_err(|_| problem::integration_contract_unavailable())?;
            if !missing.is_empty() {
                return self.integration_response(&challenge).await;
            }
            store
                .claim(&team_id, &challenge.id)
                .map_err(|_| problem::integration_challenge_expired())?;
            pending
        };
        let transcripts = (pending.transcripts.clone(), pending.requests_used);
        self.continue_paused(&admitted, request, pending, transcripts, progress)
            .await
    }
}

impl ChallengeBook for ChatService {
    fn pending<'a>(
        &'a self,
        operation: Operation,
        team_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<PendingChallenge>, Problem>> {
        Box::pin(async move {
            if operation != Operation::ChatHumanSubmit {
                return Ok(None);
            }
            self.expire_human(Some(team_id)).await?;
            Ok(self
                .human
                .current(team_id)
                .ok()
                .flatten()
                .map(|challenge| PendingChallenge {
                    kind: challenge.metadata.request.kind.clone(),
                    challenge_id: challenge.id.clone(),
                }))
        })
    }
}
