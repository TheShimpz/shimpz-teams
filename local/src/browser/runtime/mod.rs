//! The Space's one Browser session on easi.run (ADR-0117, ADR-0126).
//!
//! One Browser exists per Space at a time. Before creating one, Team removes whatever easi.run still lists. A creation or
//! call whose answer was lost leaves its outcome unknown, so nothing replaces that session until it is proven gone. A failed or unproven removal keeps every later task refused until a cleanup proves the Space
//! clean again. Team is easi.run's one administrator, so every session listed is Team's.

mod agent;

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use hyper::Method;
use strict_json::{Number, Value};
use team_core::stop::Stop;
use tokio::time::{Instant, sleep};

use super::easi::{Answer, CALL_SECONDS, Easi, EasiError};

pub use agent::AgentCalls;

/// How long a session has to start.
pub const START_SECONDS: Duration = Duration::from_mins(1);
/// Removing a Browser ends by this one deadline: easi.run saves a saved session first, within its own two minutes.
pub const REMOVE_SECONDS: Duration = Duration::from_mins(3);
/// How long a creation whose answer was lost can still finish on easi.run.
pub const CREATE_SETTLE_SECONDS: Duration = Duration::from_mins(2);
/// How often a removed Browser's absence is checked again.
pub const REMOVE_POLL: Duration = Duration::from_millis(100);
/// The page a task's session opens on, before its first navigation: one easi.run's proxy refuses, so nothing leaves.
const BLANK: &str = "http://localhost/";

/// A closed reason no Browser task can run or continue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BrowserError(pub &'static str);

impl BrowserError {
    /// No Browser can run.
    pub const UNAVAILABLE: Self = Self("browser-unavailable");
    /// The Space's one Browser is held.
    pub const BUSY: Self = Self("browser-busy");
    /// The Browser failed, or could not be proven removed.
    pub const LOST: Self = Self("browser-lost");
}

impl std::fmt::Display for BrowserError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for BrowserError {}

/// Who drives a Browser: the agent alone, a person signing in at a URL, or a task restored from a Sign-in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Mode {
    /// The agent alone.
    Agent,
    /// A person signing in, starting at this URL.
    Person(String),
    /// A task restored from the Team's Sign-in.
    SignedIn,
}

impl Mode {
    const fn name(&self) -> &'static str {
        match self {
            Self::Agent => "agent",
            Self::Person(_) => "person",
            Self::SignedIn => "signed-in",
        }
    }
}

/// One running Browser: its Team, easi.run session, and who drives it.
#[derive(Clone, Debug)]
pub struct Lease {
    /// Its Team.
    pub team_id: String,
    /// Its easi.run session.
    pub session_id: String,
    /// `agent`, `person`, or `signed-in`.
    pub mode: &'static str,
}

/// What one Browser is acquired for.
pub struct Acquire<'a> {
    /// The Team.
    pub team_id: &'a str,
    /// Who drives it.
    pub mode: Mode,
    /// The easi.run profile restored into it.
    pub profile: Option<&'a str>,
    /// When the whole start stops being waited for.
    pub deadline: Option<Instant>,
    /// The task's Stop, which ends the wait at once.
    pub stop: Option<&'a Stop>,
}

/// Creation, calls, and proven removal of the Space's one Browser.
pub struct BrowserRuntime {
    easi: Arc<Easi>,
    // The Space's one slot: held from a lease's acquisition until its release.
    held: AtomicBool,
    // Set when a removal failed or was not proven: the next holder cleans first.
    dirty: AtomicBool,
    // Set once this process proved that no session of an earlier Team process is left.
    cleaned: AtomicBool,
    // Whether easi.run answered the last time it was asked.
    reachable: AtomicBool,
    // Set once easi.run keeps a task's downloads without asking a person.
    policy: AtomicBool,
    // When a creation's answer was lost: it may still finish until the settle time passed.
    creating: Mutex<Option<Instant>>,
    // Each Team's session now, for its teardown.
    sessions: Mutex<HashMap<String, String>>,
}

impl std::fmt::Debug for BrowserRuntime {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BrowserRuntime")
    }
}

/// What a session's viewers and agent may do, as easi.run's features name it: an agent's session keeps its downloads
/// for the task and nothing is recorded; a person signing in types, pastes, and uploads, and takes nothing out.
fn features(mode: &Mode) -> Value {
    let person = matches!(mode, Mode::Person(_));
    let on = |name: &str, value: bool| (name.to_owned(), Value::Bool(value));
    Value::Object(vec![
        on("keyboard_pointer", person),
        on("clipboard", person),
        on("uploads", person),
        on("downloads", !person),
        on("sound", false),
        on("recording", false),
        on("replay", false),
    ])
}

fn listed_id(session: &Value) -> Option<&str> {
    match session.get("id") {
        Some(Value::String(id)) => Some(id),
        _ => None,
    }
}

impl BrowserRuntime {
    /// The runtime over `easi`.
    #[must_use]
    pub fn new(easi: Arc<Easi>) -> Self {
        Self {
            easi,
            held: AtomicBool::new(false),
            dirty: AtomicBool::new(true),
            cleaned: AtomicBool::new(false),
            reachable: AtomicBool::new(false),
            policy: AtomicBool::new(false),
            creating: Mutex::new(None),
            sessions: Mutex::new(HashMap::new()),
        }
    }

    /// The client of the installation.
    #[must_use]
    pub fn easi(&self) -> &Arc<Easi> {
        &self.easi
    }

    /// Whether easi.run answered the last time it was asked; a task still asks it again.
    #[must_use]
    pub fn available(&self) -> bool {
        self.reachable.load(Ordering::SeqCst)
    }

    fn take_slot(&self) -> bool {
        self.held
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    }

    fn free_slot(&self) {
        self.held.store(false, Ordering::SeqCst);
    }

    /// Whether this process proved that no session an earlier Team process left remains, so none of its calls can act
    /// again.
    #[must_use]
    pub fn previous_gone(&self) -> bool {
        self.cleaned.load(Ordering::SeqCst)
    }

    /// Whether the last cleanup or release left residue unproven.
    #[must_use]
    pub fn dirty(&self) -> bool {
        self.dirty.load(Ordering::SeqCst)
    }

    /// Creates this task's Browser; the caller releases the returned lease.
    ///
    /// # Errors
    /// [`BrowserError`]: `browser-unavailable` when it cannot run, `browser-busy` while the Space's Browser is held.
    pub async fn acquire(&self, request: Acquire<'_>) -> Result<Lease, BrowserError> {
        let admitted = match request.mode {
            Mode::SignedIn => request.profile.is_some(),
            Mode::Agent => request.profile.is_none(),
            Mode::Person(_) => true,
        };
        if !admitted {
            return Err(BrowserError::UNAVAILABLE);
        }
        if !self.take_slot() {
            return Err(BrowserError::BUSY);
        }
        match self.acquired(&request).await {
            Ok(lease) => Ok(lease),
            Err(error) => {
                self.dirty.store(true, Ordering::SeqCst);
                self.free_slot();
                Err(error)
            }
        }
    }

    async fn acquired(&self, request: &Acquire<'_>) -> Result<Lease, BrowserError> {
        if self.dirty() {
            self.clean().await?;
        }
        // The slot is held and Team is the one administrator: whatever is listed is what a lost creation left.
        self.remove_listed().await?;
        self.keep_downloads().await?;
        let answer = self.created(request).await?;
        if !answer.ok() {
            return Err(BrowserError::UNAVAILABLE);
        }
        let session = answer.json().map_err(|_| BrowserError::UNAVAILABLE)?;
        let id = listed_id(&session).ok_or(BrowserError::UNAVAILABLE)?;
        self.sessions()
            .insert(request.team_id.to_owned(), id.to_owned());
        Ok(Lease {
            team_id: request.team_id.to_owned(),
            session_id: id.to_owned(),
            mode: request.mode.name(),
        })
    }

    /// easi.run's answer to creating the session, bounded by the start's deadline and the task's Stop.
    async fn created(&self, request: &Acquire<'_>) -> Result<Answer, BrowserError> {
        let deadline = request
            .deadline
            .map_or(Instant::now() + START_SECONDS, |deadline| {
                deadline.min(Instant::now() + START_SECONDS)
            });
        let body = Self::session_body(request);
        let created = self
            .easi
            .call(Method::POST, "/api/sessions", Some(&body), deadline);
        let answer = match request.stop {
            Some(stop) => tokio::select! {
                answer = created => answer,
                () = stop.stopped() => Err(EasiError::Lost),
            },
            None => created.await,
        };
        answer.map_err(|error| self.creation_lost(error))
    }

    fn session_body(request: &Acquire<'_>) -> Value {
        let url = match &request.mode {
            Mode::Person(url) => url.as_str(),
            Mode::Agent | Mode::SignedIn => BLANK,
        };
        let agent = !matches!(request.mode, Mode::Person(_));
        let mut members = vec![
            ("url".to_owned(), Value::String(url.to_owned())),
            ("devtools".to_owned(), Value::Bool(agent)),
            ("agent".to_owned(), Value::Bool(agent)),
            ("features".to_owned(), features(&request.mode)),
        ];
        if let Some(profile) = request.profile {
            members.push(("profile".to_owned(), Value::String(profile.to_owned())));
        }
        Value::Object(members)
    }

    /// The creation's answer never came: it may still finish, so what it made is removed once it can no longer appear.
    fn creation_lost(&self, error: EasiError) -> BrowserError {
        if error == EasiError::Lost {
            *self.creating.lock().unwrap_or_else(PoisonError::into_inner) = Some(Instant::now());
        }
        BrowserError::UNAVAILABLE
    }

    fn sessions(&self) -> std::sync::MutexGuard<'_, HashMap<String, String>> {
        self.sessions.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Lets a task keep its downloads without a person's answer, which it would otherwise wait two minutes for.
    async fn keep_downloads(&self) -> Result<(), BrowserError> {
        if self.policy.load(Ordering::SeqCst) {
            return Ok(());
        }
        let body = Value::Object(vec![
            ("downloads".to_owned(), Value::String("allow".to_owned())),
            ("transfers".to_owned(), Value::String("deny".to_owned())),
        ]);
        let answer = self
            .easi
            .call(
                Method::PUT,
                "/api/agent-files",
                Some(&body),
                Instant::now() + CALL_SECONDS,
            )
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        if !answer.ok() {
            return Err(BrowserError::UNAVAILABLE);
        }
        self.policy.store(true, Ordering::SeqCst);
        Ok(())
    }

    /// Every session easi.run lists.
    async fn listed(&self) -> Result<Vec<Value>, BrowserError> {
        let answered = self
            .easi
            .call(
                Method::GET,
                "/api/sessions",
                None,
                Instant::now() + CALL_SECONDS,
            )
            .await;
        self.reachable.store(answered.is_ok(), Ordering::SeqCst);
        let answer = answered.map_err(|_| BrowserError::UNAVAILABLE)?;
        if !answer.ok() {
            return Err(BrowserError::UNAVAILABLE);
        }
        match answer.json().map_err(|_| BrowserError::UNAVAILABLE)? {
            Value::Object(members) => {
                match members.into_iter().find(|(name, _)| name == "sessions") {
                    Some((_, Value::Array(sessions))) => Ok(sessions),
                    _ => Err(BrowserError::UNAVAILABLE),
                }
            }
            _ => Err(BrowserError::UNAVAILABLE),
        }
    }

    /// Removes the task's Browser and proves it absent; then frees the slot.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when that was not proven; the runtime stays dirty.
    pub async fn release(&self, lease: &Lease) -> Result<(), BrowserError> {
        let outcome = self.remove(&lease.session_id).await;
        self.forget(lease);
        self.dirty.store(outcome.is_err(), Ordering::SeqCst);
        self.free_slot();
        outcome
    }

    fn forget(&self, lease: &Lease) {
        let mut sessions = self.sessions();
        if sessions.get(&lease.team_id) == Some(&lease.session_id) {
            sessions.remove(&lease.team_id);
        }
    }

    /// Cleans up after a removal that failed; whether no residue is left, proven. While a task holds the slot, only
    /// its own cleanup can prove that.
    pub async fn recover(&self) -> bool {
        if !self.take_slot() {
            return !self.dirty();
        }
        let outcome = if self.dirty() {
            self.clean().await.is_ok()
        } else {
            true
        };
        self.free_slot();
        outcome
    }

    /// Removes every session a previous Team process left; whether it proved none is left.
    pub async fn startup(&self) -> bool {
        if !self.take_slot() {
            return false;
        }
        let outcome = self.clean().await.is_ok();
        self.free_slot();
        outcome
    }

    /// Removes what a lost creation added after the last cleanup, while no task holds the Browser.
    pub async fn sweep(&self) {
        if self.take_slot() {
            let _ = self.remove_listed().await;
            self.free_slot();
        }
    }

    async fn remove_listed(&self) -> Result<(), BrowserError> {
        for session in self.listed().await? {
            let id = listed_id(&session).ok_or(BrowserError::UNAVAILABLE)?;
            self.remove(id).await?;
        }
        Ok(())
    }

    /// Removes every session easi.run lists, once a lost creation can no longer add one, and proves none is left.
    ///
    /// # Errors
    /// [`BrowserError`] when that was not proven.
    pub async fn clean(&self) -> Result<(), BrowserError> {
        let creating = *self.creating.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(lost) = creating {
            let settled = lost + CREATE_SETTLE_SECONDS;
            let now = Instant::now();
            if settled > now {
                sleep(settled - now).await;
            }
        }
        for session in self.listed().await? {
            let id = listed_id(&session).ok_or(BrowserError::UNAVAILABLE)?;
            self.remove(id).await?;
        }
        *self.creating.lock().unwrap_or_else(PoisonError::into_inner) = None;
        self.cleaned.store(true, Ordering::SeqCst);
        self.dirty.store(false, Ordering::SeqCst);
        Ok(())
    }

    /// Removes one Team's Browser, as its teardown requires; an absent Browser is success.
    ///
    /// # Errors
    /// [`BrowserError`] when it may remain.
    pub async fn remove_team(&self, team_id: &str) -> Result<(), BrowserError> {
        let session = self.sessions().get(team_id).cloned();
        match session {
            Some(session) => {
                self.remove(&session).await?;
                self.sessions().remove(team_id);
                Ok(())
            }
            None => Ok(()),
        }
    }

    /// Whether the lease's session still runs on easi.run, not failed.
    pub async fn intact(&self, lease: &Lease) -> bool {
        self.listed().await.is_ok_and(|sessions| {
            sessions.iter().any(|session| {
                listed_id(session) == Some(lease.session_id.as_str())
                    && matches!(session.get("failed"), None | Some(Value::Null))
            })
        })
    }

    /// Removes the lease's Browser at once, for Stop; its task's release proves the removal and frees the slot.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when the removal was not proven.
    pub async fn kill(&self, lease: &Lease) -> Result<(), BrowserError> {
        self.remove(&lease.session_id).await
    }

    async fn remove(&self, session_id: &str) -> Result<(), BrowserError> {
        let deadline = Instant::now() + REMOVE_SECONDS;
        let answer = self
            .easi
            .call(
                Method::DELETE,
                &format!("/api/sessions/{session_id}"),
                None,
                deadline,
            )
            .await
            .map_err(|_| BrowserError::LOST)?;
        if !answer.ok() && answer.status != 404 {
            return Err(BrowserError::LOST);
        }
        while Instant::now() < deadline {
            let sessions = self.listed().await.map_err(|_| BrowserError::LOST)?;
            if !sessions
                .iter()
                .any(|session| listed_id(session) == Some(session_id))
            {
                return Ok(());
            }
            sleep(REMOVE_POLL).await;
        }
        Err(BrowserError::LOST)
    }

    /// easi.run's viewer SDK as the installed release serves it.
    ///
    /// # Errors
    /// [`BrowserError::UNAVAILABLE`] when it could not be read.
    pub async fn viewer(&self) -> Result<Vec<u8>, BrowserError> {
        let answer = self
            .easi
            .call(
                Method::GET,
                "/sdk/viewer.js",
                None,
                Instant::now() + CALL_SECONDS,
            )
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        if answer.ok() {
            Ok(answer.body)
        } else {
            Err(BrowserError::UNAVAILABLE)
        }
    }

    /// One tool call on the lease's session, its MCP result, by `deadline`.
    ///
    /// # Errors
    /// [`EasiError`] when no answer came, so the call's outcome is unknown.
    pub async fn tool(
        &self,
        session_id: &str,
        tool: &str,
        arguments: Value,
        deadline: Instant,
    ) -> Result<Answer, EasiError> {
        let body = Value::Object(vec![
            ("tool".to_owned(), Value::String(tool.to_owned())),
            ("arguments".to_owned(), arguments),
        ]);
        self.easi
            .call(
                Method::POST,
                &format!("/api/sessions/{session_id}/agent"),
                Some(&body),
                deadline,
            )
            .await
    }

    /// The task's downloads in the shape its ingest reads: `files`, `refused`, and how many it could not name a size
    /// for (`skipped`), by `deadline`.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when they could not be read.
    pub async fn files(
        &self,
        lease: &Lease,
        deadline: Option<Instant>,
        stop: Option<&Stop>,
    ) -> Result<Value, BrowserError> {
        let end = deadline.unwrap_or_else(|| Instant::now() + CALL_SECONDS);
        let listing = self
            .structured(lease, "list_files", Value::Object(Vec::new()), end)
            .await?;
        if stop.is_some_and(Stop::is_stopped) {
            return Err(BrowserError::LOST);
        }
        let Some(Value::Array(downloads)) = listing.get("downloads") else {
            return Err(BrowserError::LOST);
        };
        let mut files = Vec::new();
        let mut skipped = 0_i64;
        for download in downloads {
            match (download.get("name"), download.get("size")) {
                (Some(Value::String(name)), Some(Value::Number(Number::Int(size)))) => {
                    files.push(Value::Object(vec![
                        ("name".to_owned(), Value::String(name.clone())),
                        ("size".to_owned(), Value::Number(Number::Int(*size))),
                    ]));
                }
                _ => skipped += 1,
            }
        }
        Ok(Value::Object(vec![
            ("files".to_owned(), Value::Array(files)),
            ("refused".to_owned(), Value::Array(Vec::new())),
            ("skipped".to_owned(), Value::Number(Number::Int(skipped))),
        ]))
    }

    /// One listed download's exact bytes, through its single-use ticket, by `deadline`.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when they could not be read whole.
    pub async fn read(
        &self,
        lease: &Lease,
        name: &str,
        size: u64,
        deadline: Option<Instant>,
    ) -> Result<Vec<u8>, BrowserError> {
        let end = deadline.unwrap_or_else(|| Instant::now() + CALL_SECONDS);
        let listing = self
            .structured(lease, "list_files", Value::Object(Vec::new()), end)
            .await?;
        let Some(Value::Array(downloads)) = listing.get("downloads") else {
            return Err(BrowserError::LOST);
        };
        let id = downloads
            .iter()
            .find(|download| {
                matches!(download.get("name"), Some(Value::String(listed)) if listed == name)
            })
            .and_then(|download| match download.get("id") {
                Some(Value::Number(Number::Int(id))) => Some(*id),
                _ => None,
            })
            .ok_or(BrowserError::LOST)?;
        let arguments = Value::Object(vec![(
            "download".to_owned(),
            Value::Number(Number::Int(id)),
        )]);
        let ticket = self
            .structured(lease, "save_download", arguments, end)
            .await?;
        let Some(Value::String(url)) = ticket.get("url") else {
            return Err(BrowserError::LOST);
        };
        let path = url.find("/api/").map_or(url.as_str(), |at| &url[at..]);
        let answer = self
            .easi
            .call(Method::GET, path, None, end)
            .await
            .map_err(|_| BrowserError::LOST)?;
        if !answer.ok() || u64::try_from(answer.body.len()) != Ok(size) {
            return Err(BrowserError::LOST);
        }
        Ok(answer.body)
    }

    /// A tool's structured answer.
    async fn structured(
        &self,
        lease: &Lease,
        tool: &str,
        arguments: Value,
        deadline: Instant,
    ) -> Result<Value, BrowserError> {
        let answer = self
            .tool(&lease.session_id, tool, arguments, deadline)
            .await
            .map_err(|_| BrowserError::LOST)?;
        if !answer.ok() {
            return Err(BrowserError::LOST);
        }
        let result = answer.json().map_err(|_| BrowserError::LOST)?;
        match result.get("structuredContent") {
            Some(structured) => Ok(structured.clone()),
            None => Ok(result),
        }
    }
}

#[cfg(test)]
mod tests;
