//! The `Browser` route group: the watched screen of a Team's running task, a person's control of a Browser that
//! offers it, and a Team's Sign-in (ADR-0117).
//!
//! A stream request is one exact WebSocket upgrade, whose admitted key the HTTP layer hands over. Team admits it only
//! for the view of the very Browser it names, holds one of the Space's stream slots for the whole stream, and answers
//! with a relay only after the display itself answered the same upgrade exactly; before that, every refusal is a closed
//! JSON answer. Every Sign-in body and answer has the exact form of `team_protocol::sign_in`.

use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_protocol::browser_view::{self, BUSY, INVALID_UPGRADE, NOT_RUNNING, UNAVAILABLE};
use team_protocol::identifiers::canonical_team_id;
use team_protocol::sign_in::{
    self, ABSENT, CHANGED, IDENTITY_PROVIDER, URL_INVALID, WINDOWS, canonical_delete,
    canonical_session, canonical_start,
};

use super::audit::{Actor, request_actor};
use super::service::BrowserService;
use super::sign_in::{SessionError, SignInSessions, TEAM_NOT_FOUND};
use super::view::Refused;
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Problem, Service};

/// The group's service: the Space's Browser service and its sign-in sessions.
#[derive(Clone, Debug)]
pub struct BrowserGroup {
    /// The Browser service.
    pub service: Arc<BrowserService>,
    /// The sign-in sessions.
    pub sessions: Arc<SignInSessions>,
}

fn view_problem(Refused(code): Refused) -> Problem {
    match code {
        NOT_RUNNING => Problem::new(404, "the Browser task is not running", NOT_RUNNING),
        BUSY => Problem::new(503, "the Browser view is busy", BUSY),
        _ => Problem::new(503, "the Browser view is unavailable", UNAVAILABLE),
    }
}

fn session_problem(SessionError(code): SessionError) -> Problem {
    match code {
        sign_in::NOT_RUNNING => Problem::new(
            404,
            "the sign-in session is not running",
            sign_in::NOT_RUNNING,
        ),
        URL_INVALID => Problem::new(400, "the sign-in URL is not admissible", URL_INVALID),
        sign_in::BUSY => Problem::new(
            409,
            "the Team's Sign-in is in use or being deleted",
            sign_in::BUSY,
        ),
        CHANGED => Problem::new(
            409,
            "the Team's Sign-in changed since it was confirmed",
            CHANGED,
        ),
        ABSENT => Problem::new(404, "the Team has no Sign-in", ABSENT),
        WINDOWS => Problem::new(409, "close every window but one before saving", WINDOWS),
        IDENTITY_PROVIDER => Problem::new(
            409,
            "the profile holds a session at an identity provider; sign out of it before saving",
            IDENTITY_PROVIDER,
        ),
        sign_in::UNSAVED => Problem::new(503, "the Sign-in could not be saved", sign_in::UNSAVED),
        "browser-busy" => Problem::new(409, "the Space's Browser is busy", "browser-busy"),
        TEAM_NOT_FOUND => Problem::new(404, "the Team does not exist", TEAM_NOT_FOUND),
        _ => Problem::new(503, "the Browser is unavailable", "browser-unavailable"),
    }
}

fn invalid_body() -> Problem {
    Problem::new(400, "the request body is not admissible", "invalid-body")
}

/// The JSON body of a call, admitted by `admit`.
fn admitted<'a>(
    call: &'a Call<'_>,
    admit: fn(&Value) -> Option<&Value>,
) -> Result<&'a Value, Problem> {
    match &call.body {
        Body::Json(value) if admit(value).is_some() => Ok(value),
        _ => Err(invalid_body()),
    }
}

fn member<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    match member(value, key) {
        Value::String(text) => text,
        _ => "",
    }
}

fn json(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

impl BrowserGroup {
    fn team<'a>(call: &'a Call<'_>) -> Result<&'a str, Problem> {
        let team_id = call.route.param("team_id").unwrap_or_default();
        if canonical_team_id(&Value::String(team_id.to_owned())).is_some() {
            Ok(team_id)
        } else {
            Err(Problem::new(422, "invalid Team id", "invalid-team-id"))
        }
    }

    async fn answer(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let team_id = Self::team(call)?;
        let actor = request_actor(&call.principal, &call.trace_id);
        match call.operation() {
            Operation::BrowserViewOpen => {
                let view_id = self
                    .service
                    .open_view(team_id)
                    .await
                    .map_err(view_problem)?;
                Ok(json(Value::Object(vec![(
                    "view_id".to_owned(),
                    Value::String(view_id),
                )])))
            }
            Operation::BrowserViewStream | Operation::BrowserViewControl => {
                self.stream(call, team_id).await
            }
            Operation::BrowserSignIn => self.current(team_id).await,
            _ => self.sign_in(call, team_id, &actor).await,
        }
    }

    /// Carries one viewer's stream of the watched screen, or a person's control of it, once the display answered.
    async fn stream(&self, call: &Call<'_>, team_id: &str) -> Result<Answer, Problem> {
        let view_id = call.route.param("view_id").unwrap_or_default();
        if !browser_view::VIEW_ID_RE.is_full(view_id) {
            return Err(view_problem(Refused(NOT_RUNNING)));
        }
        let key = call
            .upgrade_key
            .as_deref()
            .filter(|key| browser_view::canonical_key(&Value::String((*key).to_owned())).is_some())
            .ok_or_else(|| Problem::new(400, "the stream upgrade is invalid", INVALID_UPGRADE))?;
        let control = call.operation() == Operation::BrowserViewControl;
        let relay = self
            .service
            .open_stream(team_id, view_id, key, control)
            .await
            .map_err(view_problem)?;
        Ok(Answer::Relay(relay))
    }

    async fn current(&self, team_id: &str) -> Result<Answer, Problem> {
        let current =
            self.sessions.current(team_id).await.map_err(|_| {
                Problem::new(503, "the Sign-in is unavailable", "sign-ins-unavailable")
            })?;
        Ok(json(Value::Object(vec![(
            "sign_in".to_owned(),
            current.unwrap_or(Value::Null),
        )])))
    }

    async fn sign_in(
        &self,
        call: &Call<'_>,
        team_id: &str,
        actor: &Actor,
    ) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::BrowserSignInStart => self.start(call, team_id, actor).await,
            Operation::BrowserSignInSave => {
                let body = admitted(call, canonical_session)?;
                self.sessions
                    .save(
                        team_id,
                        actor,
                        text(body, "sign_in_session_id"),
                        text(body, "origin"),
                    )
                    .await
                    .map(json)
                    .map_err(session_problem)
            }
            Operation::BrowserSignInCancel => self.cancel(call, team_id, actor).await,
            Operation::BrowserSignInDelete => self.delete(call, team_id, actor).await,
            _ => Err(Problem::new(404, "not found", "not-found")),
        }
    }

    async fn start(
        &self,
        call: &Call<'_>,
        team_id: &str,
        actor: &Actor,
    ) -> Result<Answer, Problem> {
        let body = admitted(call, canonical_start)?;
        let locale = match member(body, "locale") {
            Value::String(locale) => Some(locale.as_str()),
            _ => None,
        };
        self.sessions
            .start(
                team_id,
                actor,
                member(body, "url"),
                locale,
                text(body, "origin"),
            )
            .await
            .map(json)
            .map_err(session_problem)
    }

    async fn cancel(
        &self,
        call: &Call<'_>,
        team_id: &str,
        actor: &Actor,
    ) -> Result<Answer, Problem> {
        let body = admitted(call, canonical_session)?;
        let session = text(body, "sign_in_session_id");
        self.sessions
            .cancel(
                team_id,
                actor,
                session,
                text(body, "origin"),
                false,
                "cancelled",
            )
            .await
            .map_err(session_problem)?;
        Ok(json(Value::Object(vec![(
            "cancelled".to_owned(),
            Value::Bool(true),
        )])))
    }

    async fn delete(
        &self,
        call: &Call<'_>,
        team_id: &str,
        actor: &Actor,
    ) -> Result<Answer, Problem> {
        let body = admitted(call, canonical_delete)?;
        let deletion = self
            .sessions
            .delete(team_id, actor, body)
            .await
            .map_err(session_problem)?;
        Ok(json(Value::Object(vec![(
            "deletion".to_owned(),
            Value::String(deletion.to_owned()),
        )])))
    }
}

impl Service for BrowserGroup {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move { self.answer(&call).await })
    }
}

#[cfg(test)]
mod tests;
