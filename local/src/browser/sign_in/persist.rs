//! Saving a sign-in session: fence, rest, stop, export, check, and commit; anything short of a commit keeps nothing,
//! and the Browser is released however it ends.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use strict_json::Value;
use team_browser::cua::{CALL_SECONDS, CuaSession};
use team_core::stop::Stop;
use team_protocol::sign_in::{IDENTITY_PROVIDER, NOT_RUNNING, UNSAVED, WINDOWS};
use tokio::time::Instant;

use super::{Activity, SAVE, Session, SessionError, SignInSessions, UNAVAILABLE};
use crate::browser::audit::{Actor, Field};
use crate::browser::capsule::Check;
use crate::browser::service::drivers_channel;
use crate::browser::sign_ins::Draft;

/// Chromium commits its cookie store every 30 seconds; Save lets it rest that long first (owner-accepted, item 4).
pub const REST_SECONDS: Duration = Duration::from_secs(31);
/// How long Save waits for the person's control to end and its release to be proven.
pub const QUIESCE_SECONDS: Duration = Duration::from_secs(15);
/// The deadline to prove a window count's Driver exec ended.
const COUNT_END_SECONDS: Duration = Duration::from_secs(5);

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
        if self.windows(&session).await? != 1 {
            return Err(SessionError(WINDOWS));
        }
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
                .insert((session.team_id.clone(), session.lease.container_id.clone()));
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

    /// Fences, rests, stops, exports, checks, and commits; anything short of a commit keeps nothing.
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
        // Nothing acts on the Browser until the person's control ended and what they held was proven released.
        if !session.view.quiesce(Instant::now() + QUIESCE_SECONDS).await
            || self.windows(session).await? != 1
        {
            return Err(SessionError(UNSAVED));
        }
        // The rest is the session's own; an end meanwhile (Cancel, the limit, the Team's deletion) cuts it short.
        if tokio::time::timeout(REST_SECONDS, session.ended.stopped())
            .await
            .is_ok()
        {
            return Err(SessionError(NOT_RUNNING));
        }
        let runtime = self.service.runtime();
        runtime.stop(&session.lease).await.map_err(unsaved)?;
        let mut draft = self
            .store
            .draft(&session.checkout)
            .map_err(|_| SessionError(UNSAVED))?;
        let checked = self.exported(session, &mut draft).await;
        let still = self.incarnations.current(&session.team_id).await.as_deref()
            == Some(session.incarnation.as_str());
        let committed = checked.and_then(|check| {
            let (sites, truncated) = check.sites();
            draft
                .commit(
                    &sites,
                    truncated,
                    runtime.image().unwrap_or_default(),
                    || still && self.commits(session),
                )
                .map_err(|_| SessionError(UNSAVED))
        });
        if committed.is_err() {
            draft.discard();
        }
        committed
    }

    /// Streams the stopped Browser's profile through the capsule check into `draft`; the check, once the capsule is
    /// whole and holds no session at a known identity provider.
    async fn exported(&self, session: &Session, draft: &mut Draft) -> Result<Check, SessionError> {
        let mut check = Check::default();
        self.service
            .runtime()
            .export(&session.lease, &mut |chunk: &[u8]| {
                check.feed(chunk).map_err(drop)?;
                draft.add(chunk).map_err(drop)
            })
            .await
            .map_err(|_| SessionError(UNSAVED))?;
        check.finish().map_err(|_| SessionError(UNSAVED))?;
        if !check.identity_hosts().is_empty() {
            return Err(SessionError(IDENTITY_PROVIDER));
        }
        Ok(check)
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

    /// How many Chromium windows the person's Browser shows, through a Driver session that binds nothing.
    async fn windows(&self, session: &Session) -> Result<usize, SessionError> {
        let runtime = self.service.runtime();
        let deadline = Instant::now() + CALL_SECONDS;
        let (exec_id, channel) = runtime
            .open_stream(&session.lease, deadline, &Stop::new())
            .await
            .map_err(|_| SessionError(UNSAVED))?;
        let mut cua = CuaSession::new(drivers_channel(channel));
        cua.set_deadline(Some(deadline));
        let counted = match cua.initialize().await {
            Ok(()) => cua.chromium_windows().await,
            Err(error) => Err(error),
        };
        drop(cua);
        runtime
            .exited(&exec_id, Instant::now() + COUNT_END_SECONDS)
            .await;
        counted.map_err(|_| SessionError(UNSAVED))
    }
}

fn count(value: &Value) -> Option<i64> {
    match value {
        Value::Number(strict_json::Number::Int(count)) => Some(*count),
        _ => None,
    }
}
