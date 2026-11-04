//! Saving a sign-in session: fence, stop, read what easi.run saved, and commit; anything short of a commit keeps nothing,
//! and the Browser is released however it ends.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use hyper::Method;
use strict_json::Value;
use team_protocol::sign_in::{NOT_RUNNING, UNSAVED};
use tokio::time::Instant;

use super::{Activity, SAVE, Session, SessionError, SignInSessions, UNAVAILABLE};
use crate::browser::audit::{Actor, Field};
use crate::browser::easi::CALL_SECONDS;

/// How long Save waits for the person's control to end.
pub const QUIESCE_SECONDS: Duration = Duration::from_secs(15);

impl SignInSessions {
    /// Keeps the session's profile as the Team's Sign-in's next generation; the Browser is released however it ends.
    /// Saving delegates the logins it holds, so nothing is saved unless that is durably recorded first. A completion
    /// that could not be recorded after the commit is answered as committed, with `recorded` false (ADR-0120).
    ///
    /// # Errors
    /// [`SessionError`] with the closed code of the refusal.
    pub async fn save(
        self: &Arc<Self>,
        team_id: &str,
        actor: &Actor,
        session_id: &str,
        origin: &str,
    ) -> Result<Value, SessionError> {
        let session = self.running(team_id, session_id)?;
        let requested = Activity {
            operation: SAVE,
            phase: "requested",
            event_id: &session.id,
            origin,
            own: None,
            details: vec![
                ("generation", Field::Count(session.checkout.generation)),
                ("incarnation", Field::Text(session.incarnation.clone())),
            ],
        };
        if !self.activity(actor, team_id, requested).await {
            return Err(SessionError(UNAVAILABLE));
        }
        self.claim(&session)?;
        let saved = self.persist(&session, actor).await;
        self.conclude(&session, actor, origin, saved).await
    }

    /// Ends a saving session however its Save went: released, its checkout ended unless committed, and its outcome
    /// recorded. A completion the log could not record after the commit is answered with `recorded` false.
    async fn conclude(
        self: &Arc<Self>,
        session: &Arc<Session>,
        actor: &Actor,
        origin: &str,
        saved: Result<Value, SessionError>,
    ) -> Result<Value, SessionError> {
        // Nothing else takes a saving session away: it leaves here, ended.
        {
            let mut guard = self.guard();
            guard.sessions.remove(&session.team_id);
            guard
                .residue
                .insert((session.team_id.clone(), session.lease.session_id.clone()));
        }
        session.ended.stop();
        self.fence(session).await;
        let removed = self.release(&session.lease, actor, false).await;
        session.removed.store(removed, Ordering::SeqCst);
        let (phase, generation, outcome) = match &saved {
            Ok(public) => (
                "completed",
                public.get("generation").and_then(count),
                "saved",
            ),
            Err(error) => {
                let _ = self.store.finish(&session.checkout);
                ("failed", Some(session.checkout.generation), error.0)
            }
        };
        session.released.stop();
        let event = Activity {
            operation: SAVE,
            phase,
            event_id: &session.id,
            origin,
            own: None,
            details: vec![
                ("generation", Field::Count(generation.unwrap_or_default())),
                ("incarnation", Field::Text(session.incarnation.clone())),
                ("outcome", Field::Text(outcome.to_owned())),
            ],
        };
        let recorded = self.activity(actor, &session.team_id, event).await;
        let public = saved?;
        let state = format!("saved:released={}", u8::from(removed));
        self.line(actor, &session.lease, &state).await;
        Ok(Value::Object(vec![
            ("sign_in".to_owned(), public),
            ("recorded".to_owned(), Value::Bool(recorded)),
        ]))
    }

    /// Marks the session saving, unless it ended or is already saving.
    fn claim(&self, session: &Arc<Session>) -> Result<(), SessionError> {
        let guard = self.guard();
        let current = guard
            .sessions
            .get(&session.team_id)
            .is_some_and(|known| Arc::ptr_eq(known, session));
        let mut flags = session.flags();
        if !current || flags.saving || session.ended.is_stopped() {
            return Err(SessionError(NOT_RUNNING));
        }
        flags.saving = true;
        Ok(())
    }

    /// Fences, stops, and commits; anything short of a commit keeps nothing new.
    async fn persist(&self, session: &Arc<Session>, actor: &Actor) -> Result<Value, SessionError> {
        self.fence(session).await;
        let kept = self.kept(session).await;
        if let Err(error) = &kept {
            let state = if error.0 == UNSAVED {
                "unsaved".to_owned()
            } else {
                format!("refused:{}", error.0)
            };
            self.line(actor, &session.lease, &state).await;
        }
        kept
    }

    async fn kept(&self, session: &Arc<Session>) -> Result<Value, SessionError> {
        let unsaved = |_| SessionError(UNSAVED);
        let fence = self.store.save_fence(&session.team_id);
        // Nothing acts on the Browser until the person's control ended.
        if !session.view.quiesce(Instant::now() + QUIESCE_SECONDS).await {
            return Err(SessionError(UNSAVED));
        }
        if session.ended.is_stopped() {
            return Err(SessionError(NOT_RUNNING));
        }
        // Stopping the session is what saves it; what easi.run says of that save decides whether it is kept.
        let runtime = self.service.runtime();
        runtime.kill(&session.lease).await.map_err(unsaved)?;
        if !self.durably_saved(session).await {
            return Err(SessionError(UNSAVED));
        }
        let still = self.incarnations.current(&session.team_id).await.as_deref()
            == Some(session.incarnation.as_str());
        self.store
            .commit(&session.checkout, fence, || still && self.commits(session))
            .map_err(|_| SessionError(UNSAVED))
    }

    /// Whether easi.run reports the session's profile saved, durably, with nothing wrong.
    async fn durably_saved(&self, session: &Session) -> bool {
        let Ok(Some(profile)) = self.store.profile(&session.checkout) else {
            return false;
        };
        let answer = self
            .service
            .runtime()
            .easi()
            .call(
                Method::GET,
                "/api/profiles",
                None,
                Instant::now() + CALL_SECONDS,
            )
            .await;
        let Ok(answer) = answer else {
            return false;
        };
        let Ok(body) = answer.json() else {
            return false;
        };
        let Some(Value::Array(profiles)) = body.get("profiles") else {
            return false;
        };
        profiles.iter().any(|listed| {
            matches!(listed.get("id"), Some(Value::String(id)) if *id == profile)
                && matches!(listed.get("unsaved"), None | Some(Value::Null))
                && matches!(listed.get("used"), Some(Value::Number(_)))
        })
    }

    /// Under the store's commit: whether the session may still be kept, after which it can no longer be ended.
    fn commits(&self, session: &Session) -> bool {
        let _guard = self.guard();
        let mut flags = session.flags();
        if flags.cancelled {
            return false;
        }
        flags.committed = true;
        true
    }
}

fn count(value: &Value) -> Option<i64> {
    match value {
        Value::Number(strict_json::Number::Int(count)) => Some(*count),
        _ => None,
    }
}
