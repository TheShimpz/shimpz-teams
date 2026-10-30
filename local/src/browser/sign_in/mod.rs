//! A person signing in to a site in a Browser only they drive, and saving it as a Sign-in (ADR-0117, Sign-ins 2-5).
//!
//! A sign-in session holds the Space's Browser in `person` mode: it starts at the URL the person chose, shows
//! Chromium's own address bar, runs no agent and sends nothing to Brain. The person controls it through its private
//! view, which no one can watch. It ends by Save, Cancel, its 30-minute limit, or 10 minutes without the person
//! connected to it.
//!
//! Save checks that one window is open, then fences the session: the person's control ends and its release must be
//! proven before anything else acts, and the window count must still be one. Chromium rests so its stores commit, stops
//! cleanly, and its profile streams through the capsule check into a sealed draft; a profile with a session at a known
//! identity provider's sign-in host is refused. While it saves, the session stays registered: Cancel, the limit, the
//! Team's deletion, or a reset still end it, and then nothing is kept. The Sign-in is committed only if the session was
//! not ended meanwhile and its Team is still the same incarnation. Whatever happens, the Browser is released.
//!
//! A session that began starting before its Team was deleted, its Sign-in deleted, or the Space reset is never
//! registered: each end bumps an epoch that a starting session must still see unchanged once its Browser exists.
//!
//! A Supervisor's deletion is `deleted` only once the Sign-in's capsules are proven gone, no session of the Team is
//! running or starting, its Browser's removal was proven, and its completion is recorded; until then it is `pending`
//! and Team keeps trying, across a restart too. Every step records its activity event (ADR-0120): starting and Saving
//! delegate the Sign-in, so they refuse when the audit log cannot record them; Cancel and deletion protect it, so they
//! never wait on it.

mod deletion;
mod persist;

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::sign_in::{NOT_RUNNING, URL_INVALID, start_site};
use tokio::time::{Instant, sleep};

use super::audit::{self, Actor, Field, Lines, short};
use super::runtime::{Acquire, Lease, Mode};
use super::service::{BrowserService, language};
use super::sign_ins::{Checkout, Kind, SignInError, SignInStore};
use super::view::View;

/// The session's limit.
pub const SESSION_SECONDS: Duration = Duration::from_mins(30);
/// How long a session may go without its person in control.
pub const IDLE_SECONDS: Duration = Duration::from_mins(10);
/// How often a session's limits are checked.
pub const WATCH_SECONDS: Duration = Duration::from_secs(5);
/// The longest a Cancel waits for a Save under way to release the Browser: its quiesce, stop, export, and removal.
pub const SAVE_SECONDS: Duration = Duration::from_secs(150);
/// A Browser whose removal failed is cleaned again in the background this often, until its absence is proven.
pub const RECOVER_SECONDS: Duration = Duration::from_secs(30);
/// The Browser could not do what was asked.
pub const UNAVAILABLE: &str = "browser-unavailable";
/// The Team does not exist.
pub const TEAM_NOT_FOUND: &str = "team-not-found";
/// The origin of what Team does on its own, beside the verified Admin Origin of what a person asked (ADR-0120).
pub const TEAM_ORIGIN: &str = "team";
const START: &str = "browser-sign-in-start";
const SAVE: &str = "browser-sign-in-save";
const CANCEL: &str = "browser-sign-in-cancel";
const DELETE: &str = "browser-sign-in-delete";
const EXPIRE: &str = "browser-sign-in-expire";

/// A Team's current incarnation (its network's id), read anew each time; `None` once it is gone or unknowable.
pub trait Incarnations: Send + Sync {
    /// The Team's current incarnation.
    fn current<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Option<String>>;
}

/// A sign-in session could not do what was asked; the closed code says why.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SessionError(pub &'static str);

impl From<SignInError> for SessionError {
    fn from(error: SignInError) -> Self {
        Self(error.code.unwrap_or(UNAVAILABLE))
    }
}

/// What a session's mutable state holds, under its lock.
#[derive(Debug, Default)]
struct Flags {
    // A Save holds the session, then commits it unless it was cancelled first.
    saving: bool,
    cancelled: bool,
    committed: bool,
}

/// One running sign-in session.
pub(crate) struct Session {
    id: String,
    team_id: String,
    incarnation: String,
    site: String,
    lease: Lease,
    view: Arc<View>,
    started: Instant,
    // The Team's Sign-in, checked out to this session before its Browser started.
    checkout: Checkout,
    // Set once the session ends, however it ends; a Save under way sees it and keeps nothing.
    ended: Stop,
    // Set once a Save released the Browser, `removed` saying whether its absence was proven.
    released: Stop,
    removed: AtomicBool,
    flags: Mutex<Flags>,
    // The verified person who started it, and the Origin they started it from, which Team's own later events name.
    initiator: String,
    origin: String,
}

impl Session {
    fn flags(&self) -> MutexGuard<'_, Flags> {
        self.flags.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[derive(Default)]
struct Guarded {
    sessions: HashMap<String, Arc<Session>>,
    // Bumped by every end of a Team's sessions (its deletion) and of all of them (a reset).
    epochs: HashMap<String, u64>,
    epoch: u64,
    // Teams with a sign-in session starting.
    starting: HashSet<String>,
    // Each Browser whose removal is not proven yet, by its Team and container.
    residue: HashSet<(String, String)>,
}

/// What the sessions run on.
pub struct SessionParts {
    /// The Browser service, whose runtime holds the Space's one Browser.
    pub service: Arc<BrowserService>,
    /// The Sign-ins.
    pub store: Arc<SignInStore>,
    /// The Teams' incarnations.
    pub incarnations: Arc<dyn Incarnations>,
    /// The audit log.
    pub audit: Arc<dyn AuditSink>,
    /// The random source of session and event ids.
    pub entropy: Arc<dyn Entropy>,
}

/// The Space's sign-in sessions: at most one, holding the Space's one Browser.
pub struct SignInSessions {
    service: Arc<BrowserService>,
    store: Arc<SignInStore>,
    incarnations: Arc<dyn Incarnations>,
    audit: Arc<dyn AuditSink>,
    entropy: Arc<dyn Entropy>,
    guard: Mutex<Guarded>,
    // Set once the Browsers a previous Team process left are proven gone; until then no deletion completes.
    cleaned: AtomicBool,
}

impl std::fmt::Debug for SignInSessions {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SignInSessions")
    }
}

/// One activity event: its operation, phase, event id, origin, and details.
struct Activity<'a> {
    operation: &'static str,
    phase: &'static str,
    event_id: &'a str,
    origin: &'a str,
    // Recorded as Team's own work, naming the person who began it (empty when none), instead of the request's actor.
    own: Option<&'a str>,
    details: Vec<(&'static str, Field)>,
}

impl SignInSessions {
    /// The sessions over `parts`.
    #[must_use]
    pub fn new(parts: SessionParts) -> Self {
        Self {
            service: parts.service,
            store: parts.store,
            incarnations: parts.incarnations,
            audit: parts.audit,
            entropy: parts.entropy,
            guard: Mutex::new(Guarded::default()),
            cleaned: AtomicBool::new(false),
        }
    }

    /// The Browser service the sessions hold the Space's Browser through.
    #[must_use]
    pub fn service(&self) -> &Arc<BrowserService> {
        &self.service
    }

    fn guard(&self) -> MutexGuard<'_, Guarded> {
        self.guard.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn token(&self) -> Result<String, SessionError> {
        token_hex(self.entropy.as_ref(), 16).map_err(|_| SessionError(UNAVAILABLE))
    }

    /// Records one activity event of the Team's Sign-in (ADR-0120); whether it was recorded. The actor is the
    /// request's verified principal and the origin its verified Admin Origin; Team's own work names Team.
    async fn activity(&self, actor: &Actor, team_id: &str, event: Activity<'_>) -> bool {
        let mut fields = vec![
            ("event_id", Field::Text(event.event_id.to_owned())),
            ("phase", Field::Text(event.phase.to_owned())),
            ("subject", Field::Text("sign-in".to_owned())),
        ];
        fields.extend(event.details);
        let origin = if event.own.is_some() {
            TEAM_ORIGIN
        } else {
            event.origin
        };
        fields.push(("origin", Field::Text(origin.to_owned())));
        if let Some(initiator) = event.own.filter(|initiator| !initiator.is_empty()) {
            fields.push(("initiator_id", Field::Text(initiator.to_owned())));
        }
        let team = Actor::team();
        let actor = if event.own.is_some() { &team } else { actor };
        let result = if event.phase == "failed" {
            "error"
        } else {
            "ok"
        };
        audit::activity(
            self.audit.as_ref(),
            actor,
            event.operation,
            result,
            team_id,
            &fields,
        )
        .await
        .is_ok()
    }

    /// A session's state line, never a URL path, page content, or input; Team's own when no request made it. A log
    /// that cannot record one never stops what the session does.
    async fn line(&self, actor: &Actor, lease: &Lease, state: &str) {
        let detail = format!("{state}:container={}", short(&lease.container_id));
        let _ = audit::record(
            self.audit.as_ref(),
            actor,
            "browser-sign-in",
            "ok",
            &lease.team_id,
            &detail,
        )
        .await;
    }

    /// Records, as Team's own work, every Sign-in the store deleted as expired while reading it (ADR-0117 item 7).
    async fn record_expired(&self) {
        for expired in self.store.take_expired() {
            let Ok(event_id) = self.token() else { return };
            let event = Activity {
                operation: EXPIRE,
                phase: "completed",
                event_id: &event_id,
                origin: TEAM_ORIGIN,
                own: Some(""),
                details: vec![
                    ("generation", Field::Count(expired.generation)),
                    ("incarnation", Field::Text(expired.incarnation)),
                    ("outcome", Field::Text("expired".to_owned())),
                ],
            };
            self.activity(&Actor::team(), &expired.team_id, event).await;
        }
    }

    /// The Team's Sign-in as Admin shows it: needing a person when this Team's Browser image cannot restore it.
    ///
    /// # Errors
    /// [`SignInError`] when the Sign-ins cannot be read.
    pub async fn current(&self, team_id: &str) -> Result<Option<Value>, SignInError> {
        let current = self.store.current(team_id, self.service.runtime().image());
        self.record_expired().await;
        current
    }

    /// Opens a sign-in session for the Team at `url`: its id, the view the person controls it through, and its site.
    ///
    /// # Errors
    /// [`SessionError`] with the closed code of the refusal.
    pub async fn start(
        self: &Arc<Self>,
        team_id: &str,
        actor: &Actor,
        url: &Value,
        locale: Option<&str>,
        origin: &str,
    ) -> Result<Value, SessionError> {
        let site = start_site(url).ok_or(SessionError(URL_INVALID))?;
        let Value::String(url) = url else {
            return Err(SessionError(URL_INVALID));
        };
        let admission = {
            let mut guard = self.guard();
            if !guard.starting.insert(team_id.to_owned()) {
                return Err(SessionError(team_protocol::sign_in::BUSY));
            }
            (guard.epoch, guard.epochs.get(team_id).copied().unwrap_or(0))
        };
        let request = Start {
            team_id,
            actor,
            url,
            locale,
            origin,
            site: &site,
            admission,
        };
        let started = self.started(request).await;
        self.guard().starting.remove(team_id);
        started
    }

    async fn started(self: &Arc<Self>, request: Start<'_>) -> Result<Value, SessionError> {
        let team_id = request.team_id;
        let current = self
            .incarnations
            .current(team_id)
            .await
            .ok_or(SessionError(TEAM_NOT_FOUND))?;
        let event = self.token()?;
        let requested = Activity {
            operation: START,
            phase: "requested",
            event_id: &event,
            origin: request.origin,
            own: None,
            details: vec![("incarnation", Field::Text(current.clone()))],
        };
        // Starting delegates the Team's Sign-in to a person: nothing starts unless that is durably recorded first.
        if !self.activity(request.actor, team_id, requested).await {
            return Err(SessionError(UNAVAILABLE));
        }
        let checkout = self.checkout(&request, &current, &event).await?;
        let session = self.launch(&request, &current, &event, checkout).await?;
        self.register(&request, &current, &event, session).await
    }

    async fn checkout(
        &self,
        request: &Start<'_>,
        current: &str,
        event: &str,
    ) -> Result<Checkout, SessionError> {
        let checkout = self.store.checkout(request.team_id, current, Kind::Person);
        self.record_expired().await;
        match checkout {
            Ok(checkout) => Ok(checkout),
            Err(error) => {
                let failed = SessionError::from(error);
                let event = Activity {
                    operation: START,
                    phase: "failed",
                    event_id: event,
                    origin: request.origin,
                    own: None,
                    details: vec![
                        ("incarnation", Field::Text(current.to_owned())),
                        ("outcome", Field::Text(failed.0.to_owned())),
                    ],
                };
                self.activity(request.actor, request.team_id, event).await;
                Err(failed)
            }
        }
    }

    fn start_details(checkout: &Checkout, current: &str) -> Vec<(&'static str, Field)> {
        vec![
            ("generation", Field::Count(checkout.generation)),
            ("incarnation", Field::Text(current.to_owned())),
        ]
    }

    async fn start_failed(
        &self,
        request: &Start<'_>,
        event: &str,
        checkout: &Checkout,
        current: &str,
        code: &str,
    ) {
        let _ = self.store.finish(checkout);
        let mut details = Self::start_details(checkout, current);
        details.push(("outcome", Field::Text(code.to_owned())));
        let failed = Activity {
            operation: START,
            phase: "failed",
            event_id: event,
            origin: request.origin,
            own: None,
            details,
        };
        self.activity(request.actor, request.team_id, failed).await;
    }

    /// The checked-out Sign-in's capsule when this image wrote it; a refusal ends the start.
    async fn restored(
        &self,
        request: &Start<'_>,
        current: &str,
        event: &str,
        checkout: &Checkout,
    ) -> Result<Option<super::sign_ins::Capsule>, SessionError> {
        let image = self.service.runtime().image().unwrap_or_default();
        match self.store.capsule(checkout, image) {
            Ok(capsule) => Ok(capsule),
            Err(error) => {
                let failed = SessionError::from(error);
                self.start_failed(request, event, checkout, current, failed.0)
                    .await;
                Err(failed)
            }
        }
    }

    /// Starts the person's Browser with the Team's Sign-in restored when this image wrote it, holds it, and builds the
    /// session around it.
    async fn launch(
        self: &Arc<Self>,
        request: &Start<'_>,
        current: &str,
        event: &str,
        checkout: Checkout,
    ) -> Result<Arc<Session>, SessionError> {
        let runtime = self.service.runtime();
        let capsule = self.restored(request, current, event, &checkout).await?;
        let acquired = runtime
            .acquire(Acquire {
                team_id: request.team_id,
                language: language(request.locale),
                mode: Mode::Person(request.url.to_owned()),
                capsule: capsule.map(|capsule| Box::new(capsule) as super::runtime::Capsule),
                deadline: None,
                stop: None,
            })
            .await;
        let lease = match acquired {
            Ok(lease) => lease,
            Err(error) => {
                self.start_failed(request, event, &checkout, current, error.0)
                    .await;
                return Err(SessionError(error.0));
            }
        };
        let session = match self.session(request, current, lease.clone(), checkout.clone()) {
            Ok(session) => session,
            Err(code) => {
                self.release(&lease, request.actor, false).await;
                self.start_failed(request, event, &checkout, current, code.0)
                    .await;
                return Err(code);
            }
        };
        let holder = format!("sign-in:{}", session.id);
        if let Err(error) = self
            .service
            .hold(request.team_id, &holder, &lease, &session.view)
            .await
        {
            self.release(&lease, request.actor, false).await;
            self.start_failed(request, event, &checkout, current, error.0)
                .await;
            return Err(SessionError(error.0));
        }
        Ok(session)
    }

    fn session(
        &self,
        request: &Start<'_>,
        current: &str,
        lease: Lease,
        checkout: Checkout,
    ) -> Result<Arc<Session>, SessionError> {
        let lines = Lines::new(
            Arc::clone(&self.audit),
            request.actor.clone(),
            "browser-sign-in",
            request.team_id,
            "view:",
            format!(":container={}", short(&lease.container_id)),
        );
        let view = self
            .service
            .view(&lease, lines, true)
            .map_err(|_| SessionError(UNAVAILABLE))?;
        Ok(Arc::new(Session {
            id: self.token()?,
            team_id: request.team_id.to_owned(),
            incarnation: current.to_owned(),
            site: request.site.to_owned(),
            lease,
            view,
            started: Instant::now(),
            checkout,
            ended: Stop::new(),
            released: Stop::new(),
            removed: AtomicBool::new(false),
            flags: Mutex::new(Flags::default()),
            initiator: audit::human(request.actor),
            origin: request.origin.to_owned(),
        }))
    }

    /// Registers the session unless its Team, its Sign-in, or the Space ended while its Browser started; then its
    /// limits hold, and its start is recorded or it ends at once.
    async fn register(
        self: &Arc<Self>,
        request: &Start<'_>,
        current: &str,
        event: &str,
        session: Arc<Session>,
    ) -> Result<Value, SessionError> {
        let team_id = request.team_id;
        let admitted = {
            let mut guard = self.guard();
            let admitted =
                (guard.epoch, guard.epochs.get(team_id).copied().unwrap_or(0)) == request.admission;
            if admitted {
                guard
                    .sessions
                    .insert(team_id.to_owned(), Arc::clone(&session));
            }
            admitted
        };
        let still = self.incarnations.current(team_id).await.as_deref() == Some(current);
        if !admitted || !still {
            self.abandon(request, &session, admitted).await;
            self.start_failed(request, event, &session.checkout, current, NOT_RUNNING)
                .await;
            return Err(SessionError(NOT_RUNNING));
        }
        // The session's limits hold from here, whatever happens to its answer.
        tokio::spawn(Arc::clone(self).watch(Arc::clone(&session)));
        let completed = Activity {
            operation: START,
            phase: "completed",
            event_id: event,
            origin: request.origin,
            own: None,
            details: Self::start_details(&session.checkout, current),
        };
        if !self.activity(request.actor, team_id, completed).await {
            // A delegation the log could not record does not stand: the session ends at once, keeping nothing.
            let _ = self
                .cancel(team_id, request.actor, &session.id, "", false, "unrecorded")
                .await;
            return Err(SessionError(UNAVAILABLE));
        }
        self.line(
            request.actor,
            &session.lease,
            &format!("started:site={}", session.site),
        )
        .await;
        Ok(started(&session))
    }

    /// A session that may not run: its removal has one owner, this start, unless a Cancel or the Team's end took it.
    async fn abandon(
        self: &Arc<Self>,
        request: &Start<'_>,
        session: &Arc<Session>,
        admitted: bool,
    ) {
        let owned = {
            let mut guard = self.guard();
            let registered = guard
                .sessions
                .get(request.team_id)
                .is_some_and(|known| Arc::ptr_eq(known, session));
            let owned = !admitted || registered;
            if admitted && registered {
                guard.sessions.remove(request.team_id);
            }
            if owned {
                guard.residue.insert((
                    request.team_id.to_owned(),
                    session.lease.container_id.clone(),
                ));
            }
            owned
        };
        if owned {
            session.ended.stop();
            self.fence(session).await;
            self.release(&session.lease, request.actor, false).await;
        }
    }

    /// No one acts on the session's Browser any more: its hold and its view end.
    async fn fence(&self, session: &Session) {
        self.service
            .drop_view(&session.team_id, &session.view)
            .await;
        session.view.end();
    }

    /// Ends the session: it, and whether a Save holds it. A session not saving is taken at once, for its caller to
    /// release; one saving is marked cancelled instead, so its Save keeps nothing and releases the Browser itself.
    async fn end(
        &self,
        team_id: &str,
        session_id: &str,
    ) -> Result<(Arc<Session>, bool), SessionError> {
        let session = {
            let mut guard = self.guard();
            let session = guard
                .sessions
                .get(team_id)
                .filter(|session| session.id == session_id)
                .cloned()
                .ok_or(SessionError(NOT_RUNNING))?;
            let mut flags = session.flags();
            if flags.committed {
                return Err(SessionError(NOT_RUNNING));
            }
            if flags.saving {
                flags.cancelled = true;
                drop(flags);
                session.ended.stop();
                return Ok((session, true));
            }
            drop(flags);
            guard.sessions.remove(team_id);
            // Until its release proves the Browser gone, no deletion of the Sign-in it restored is complete.
            guard
                .residue
                .insert((team_id.to_owned(), session.lease.container_id.clone()));
            session
        };
        session.ended.stop();
        self.fence(&session).await;
        Ok((session, false))
    }

    /// Ends the session, keeping nothing, once its Browser was proven removed; else it is refused as unavailable. A
    /// Save under way removes the Browser itself: Cancel answers once it did. Its activity event names why it ended
    /// (`reason`); without its own Origin, it names the session's.
    ///
    /// # Errors
    /// [`SessionError`] `sign-in-not-running`, or `browser-unavailable` when the removal was not proven.
    pub async fn cancel(
        self: &Arc<Self>,
        team_id: &str,
        actor: &Actor,
        session_id: &str,
        origin: &str,
        background: bool,
        reason: &str,
    ) -> Result<(), SessionError> {
        let (session, saving) = self.end(team_id, session_id).await?;
        let line_actor = if background {
            Actor::team()
        } else {
            actor.clone()
        };
        self.line(&line_actor, &session.lease, "cancelled").await;
        let removed = self
            .removed(&session, saving, &line_actor, background)
            .await;
        let event = Activity {
            operation: CANCEL,
            phase: if removed { "completed" } else { "failed" },
            event_id: &session.id,
            origin: if origin.is_empty() {
                &session.origin
            } else {
                origin
            },
            own: background.then_some(session.initiator.as_str()),
            details: vec![
                ("generation", Field::Count(session.checkout.generation)),
                ("incarnation", Field::Text(session.incarnation.clone())),
                ("outcome", Field::Text(reason.to_owned())),
            ],
        };
        self.activity(actor, team_id, event).await;
        if removed {
            Ok(())
        } else {
            Err(SessionError(UNAVAILABLE))
        }
    }

    /// Whether an ended session's Browser was proven removed: by the Save under way, or by its release here.
    async fn removed(
        self: &Arc<Self>,
        session: &Session,
        saving: bool,
        actor: &Actor,
        background: bool,
    ) -> bool {
        if saving {
            let released = tokio::time::timeout(SAVE_SECONDS, session.released.stopped()).await;
            return released.is_ok() && session.removed.load(Ordering::SeqCst);
        }
        let removed = self.release(&session.lease, actor, background).await;
        let _ = self.store.finish(&session.checkout);
        removed
    }

    /// Ends the session at its limit, once the person left it alone too long, or once its screen is lost: a session
    /// no one can drive any more is never left running.
    async fn watch(self: Arc<Self>, session: Arc<Session>) {
        loop {
            if tokio::time::timeout(WATCH_SECONDS, session.ended.stopped())
                .await
                .is_ok()
            {
                return;
            }
            let Some(reason) = ending(&session) else {
                continue;
            };
            let state = if reason == "lost" { "lost" } else { "expired" };
            self.line(&Actor::team(), &session.lease, state).await;
            let _ = self
                .cancel(
                    &session.team_id,
                    &Actor::team(),
                    &session.id,
                    "",
                    true,
                    reason,
                )
                .await;
            return;
        }
    }

    /// The Team is deleted: its sign-in session ends at once, keeping nothing, and one still starting never runs.
    pub async fn end_team(self: &Arc<Self>, team_id: &str) {
        let session = {
            let mut guard = self.guard();
            *guard.epochs.entry(team_id.to_owned()).or_insert(0) += 1;
            guard.sessions.get(team_id).cloned()
        };
        if let Some(session) = session {
            // The Team's Browser is removed and proven absent next, whatever this removal did.
            let _ = self
                .cancel(
                    team_id,
                    &Actor::team(),
                    &session.id,
                    "",
                    true,
                    "team-deleted",
                )
                .await;
        }
    }

    /// A Space reset: every sign-in session ends at once, keeping nothing, and none still starting runs.
    pub async fn end_all(self: &Arc<Self>) {
        let teams: Vec<String> = {
            let mut guard = self.guard();
            guard.epoch += 1;
            guard.sessions.keys().cloned().collect()
        };
        for team_id in teams {
            self.end_team(&team_id).await;
        }
    }

    /// Removes the session's Browser; whether it was proven gone. A failure leaves the runtime to clean every Browser
    /// before the next one starts, and meanwhile it is cleaned again in the background until its absence is proven.
    async fn release(self: &Arc<Self>, lease: &Lease, actor: &Actor, background: bool) -> bool {
        if self.service.runtime().release(lease).await.is_ok() {
            self.guard()
                .residue
                .remove(&(lease.team_id.clone(), lease.container_id.clone()));
            return true;
        }
        let line_actor = if background {
            Actor::team()
        } else {
            actor.clone()
        };
        self.line(&line_actor, lease, "release-failed").await;
        self.guard()
            .residue
            .insert((lease.team_id.clone(), lease.container_id.clone()));
        tokio::spawn(Arc::clone(self).recover(lease.clone()));
        false
    }

    /// Cleans again until no residue is proven left; a restarted Team process cleans up after a stopped one.
    async fn recover(self: Arc<Self>, lease: Lease) {
        loop {
            sleep(RECOVER_SECONDS).await;
            if self.service.runtime().recover().await {
                self.guard()
                    .residue
                    .remove(&(lease.team_id.clone(), lease.container_id.clone()));
                self.line(&Actor::team(), &lease, "recovered").await;
                return;
            }
        }
    }

    fn running(&self, team_id: &str, session_id: &str) -> Result<Arc<Session>, SessionError> {
        let guard = self.guard();
        guard
            .sessions
            .get(team_id)
            .filter(|session| session.id == session_id && !session.flags().saving)
            .cloned()
            .ok_or(SessionError(NOT_RUNNING))
    }

    fn cleaned(&self) -> bool {
        self.cleaned.load(Ordering::SeqCst)
    }
}

/// A started session's id, its view, and its site.
fn started(session: &Session) -> Value {
    Value::Object(vec![
        (
            "sign_in_session_id".to_owned(),
            Value::String(session.id.clone()),
        ),
        (
            "view_id".to_owned(),
            Value::String(session.view.id().to_owned()),
        ),
        ("site".to_owned(), Value::String(session.site.clone())),
    ])
}

/// Why a session ends now: its screen is lost, its limit passed, or its person left it alone too long.
fn ending(session: &Session) -> Option<&'static str> {
    if session.view.unavailable() {
        Some("lost")
    } else if session.started.elapsed() >= SESSION_SECONDS {
        Some("session-limit")
    } else if session.view.quiet_for() >= IDLE_SECONDS {
        Some("idle")
    } else {
        None
    }
}

/// One start's request and its admission.
struct Start<'a> {
    team_id: &'a str,
    actor: &'a Actor,
    url: &'a str,
    locale: Option<&'a str>,
    origin: &'a str,
    site: &'a str,
    // The Space's and the Team's epochs when it began.
    admission: (u64, u64),
}

#[cfg(test)]
pub(crate) mod tests;
