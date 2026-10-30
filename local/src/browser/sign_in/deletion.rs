//! A Supervisor's deletion of a Team's Sign-in, and Team's start: deletions are proven and recorded, now or in the
//! background, across a restart too; a Browser a previous Team process left is removed before any deletion completes.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use strict_json::{Number, Value};
use team_protocol::python::value::{exact_object, field};
use tokio::time::sleep;

use super::{Activity, DELETE, RECOVER_SECONDS, SessionError, SignInSessions, UNAVAILABLE};
use crate::browser::audit::{Actor, Field};
use crate::browser::sign_ins::{Deleting, Pending};

/// A Supervisor's deletion of a Team's Sign-in, as its activity events name it until its completion is recorded.
#[derive(Clone, Debug)]
struct Deletion {
    team_id: String,
    deleting: Deleting,
    generation: i64,
    incarnation: String,
}

impl Deletion {
    fn details(&self) -> Vec<(&'static str, Field)> {
        vec![
            ("generation", Field::Count(self.generation)),
            ("incarnation", Field::Text(self.incarnation.clone())),
            ("factor", Field::Text(self.deleting.factor.clone())),
        ]
    }
}

impl From<Pending> for Deletion {
    fn from(pending: Pending) -> Self {
        Self {
            team_id: pending.team_id,
            deleting: pending.deleting,
            generation: pending.generation,
            incarnation: pending.incarnation,
        }
    }
}

fn text<'a>(members: &'a [(String, Value)], key: &str) -> &'a str {
    match field(members, key) {
        Value::String(text) => text,
        _ => "",
    }
}

impl SignInSessions {
    /// Deletes the Team's Sign-in exactly as the Supervisor confirmed it with a second factor (ADR-0117 item 6), a
    /// body `sign_in::canonical_delete` admitted: Team re-checks the confirmed Sign-in, generation, and incarnation,
    /// fences it durably with the deletion's metadata, ends a session using it or starting, and removes everything.
    /// `deleted` once proven and recorded, else `pending` while it keeps trying in the background.
    ///
    /// # Errors
    /// [`SessionError`] `sign-in-absent`, `sign-in-changed`, or unavailable.
    pub async fn delete(
        self: &Arc<Self>,
        team_id: &str,
        actor: &Actor,
        confirmed: &Value,
    ) -> Result<&'static str, SessionError> {
        let (deletion, sign_in_id) = self.confirmed(team_id, actor, confirmed)?;
        let origin = deletion.deleting.origin.clone();
        // Deleting is protective: an audit log that cannot record it never stops it (ADR-0120).
        let requested = deletion.event("requested", None, None);
        self.activity(actor, team_id, requested).await;
        let expected = (
            sign_in_id.as_str(),
            deletion.generation,
            deletion.incarnation.as_str(),
        );
        if let Err(error) = self
            .store
            .begin_delete(team_id, expected, deletion.deleting.clone())
        {
            let failed = SessionError::from(error);
            let event = deletion.event("failed", None, Some(failed.0));
            self.activity(actor, team_id, event).await;
            return Err(failed);
        }
        let session = {
            let mut guard = self.guard();
            // A session still starting never becomes one; one running ends now, and the fence keeps its Save out.
            *guard.epochs.entry(team_id.to_owned()).or_insert(0) += 1;
            guard.sessions.get(team_id).cloned()
        };
        if let Some(session) = session {
            let _ = self
                .cancel(team_id, actor, &session.id, &origin, false, "deleted")
                .await;
        }
        if self.settle(&deletion, false, actor).await {
            return Ok("deleted");
        }
        tokio::spawn(Arc::clone(self).later(deletion));
        Ok("pending")
    }
}

impl Deletion {
    fn event<'a>(
        &'a self,
        phase: &'static str,
        own: Option<&'a str>,
        outcome: Option<&str>,
    ) -> Activity<'a> {
        let deletion = self;
        let mut details = deletion.details();
        if let Some(outcome) = outcome {
            details.push(("outcome", Field::Text(outcome.to_owned())));
        }
        Activity {
            operation: DELETE,
            phase,
            event_id: &deletion.deleting.event_id,
            origin: &deletion.deleting.origin,
            own,
            details,
        }
    }
}

impl SignInSessions {
    /// The deletion the Supervisor confirmed, and the Sign-in they confirmed it for.
    fn confirmed(
        &self,
        team_id: &str,
        actor: &Actor,
        confirmed: &Value,
    ) -> Result<(Deletion, String), SessionError> {
        let members = exact_object(
            confirmed,
            &[
                "sign_in_id",
                "generation",
                "team_incarnation",
                "factor",
                "origin",
            ],
        )
        .ok_or(SessionError(UNAVAILABLE))?;
        let Value::Number(Number::Int(generation)) = field(members, "generation") else {
            return Err(SessionError(UNAVAILABLE));
        };
        let deletion = Deletion {
            team_id: team_id.to_owned(),
            deleting: Deleting {
                event_id: self.token()?,
                initiator_id: actor.human(),
                factor: text(members, "factor").to_owned(),
                origin: text(members, "origin").to_owned(),
            },
            generation: *generation,
            incarnation: text(members, "team_incarnation").to_owned(),
        };
        Ok((deletion, text(members, "sign_in_id").to_owned()))
    }

    /// Completes the deletion if it can be proven now: whether its completion was recorded and its fence dropped.
    async fn settle(&self, deletion: &Deletion, background: bool, actor: &Actor) -> bool {
        let team_id = deletion.team_id.as_str();
        {
            let guard = self.guard();
            let residue =
                !self.cleaned() || guard.residue.iter().any(|(owner, _)| owner == team_id);
            if guard.starting.contains(team_id) || guard.sessions.contains_key(team_id) || residue {
                return false;
            }
        }
        if self.store.purge(team_id, &deletion.deleting.event_id) != Ok(true) {
            return false;
        }
        let own = background.then_some(deletion.deleting.initiator_id.as_str());
        let completed = deletion.event("completed", own, Some("deleted"));
        if !self.activity(actor, team_id, completed).await {
            return false;
        }
        self.store.settle(team_id, &deletion.deleting.event_id) == Ok(true)
    }

    /// Retries a fenced deletion until it is proven and recorded; a restarted Team process resumes it at its start.
    async fn later(self: Arc<Self>, deletion: Deletion) {
        loop {
            sleep(RECOVER_SECONDS).await;
            if self.settle(&deletion, true, &Actor::team()).await {
                return;
            }
        }
    }

    /// At Team start: the store ends every open checkout and removes what was left; a Browser a previous Team
    /// process left is removed and, until that is proven, no deletion of a Sign-in it may have restored completes;
    /// then every pending deletion resumes. Pending deletions the store cannot read yet are read again until it can.
    pub async fn startup(self: &Arc<Self>) {
        let _ = self.store.reconcile();
        // Cleanup never waits on the Sign-ins' metadata, and a cleanup that failed is tried again until it is proven.
        if self.service.runtime().startup().await {
            self.cleaned.store(true, Ordering::SeqCst);
        } else {
            tokio::spawn(Arc::clone(self).recover_startup());
        }
        let pending = loop {
            match self.store.deletions() {
                Ok(pending) => break pending,
                Err(_) => sleep(RECOVER_SECONDS).await,
            }
        };
        for deletion in pending.into_iter().map(Deletion::from) {
            if !self.settle(&deletion, true, &Actor::team()).await {
                tokio::spawn(Arc::clone(self).later(deletion));
            }
        }
    }

    /// Cleans again until no Browser a previous Team process left is proven left.
    async fn recover_startup(self: Arc<Self>) {
        loop {
            sleep(RECOVER_SECONDS).await;
            if self.service.runtime().recover().await {
                self.cleaned.store(true, Ordering::SeqCst);
                return;
            }
        }
    }
}
