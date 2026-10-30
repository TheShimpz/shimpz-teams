//! One chat turn's Browser task on the Local Team (ADR-0117 §5 to §8).
//!
//! Team acquires the Space's one Browser on a fresh egress generation, opens the Cua Driver's MCP session, runs the
//! task loop with the turn's Admin-delivered key, ingests the downloads, and always releases the Browser, which
//! removes it and ends its egress generation. A Driver session that fails mid-task is replaced on the same Browser
//! only once its exec is proven ended, so the failed one can never act beside its replacement. A Browser that could not
//! be proven removed fails the task. The turn's Stop removes the running Browser at once, so none of its calls reaches
//! the page again. Each task is audited with its Team and incarnation, the container, the Browser address, and the
//! egress process and generation its audit lines carry, with its closed ending and recoveries, never a URL or page
//! text. While a task runs, the Supervisor may watch its screen through the task's own view, which ends with the task.

mod drivers;

use std::collections::HashMap;
use std::sync::Arc;

use strict_json::{Number, Value};
use team_browser::cua::CuaError;
use team_browser::ingest::{self, DownloadSink, Ingested, PutRefused, ReadLost};
use team_browser::task::{MAX_SECONDS, Outcome, Price, Status, Task, TaskBrain, TaskSeams};
use team_core::audit::AuditSink;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser::MAX_UNSCREENED_URLS;
use team_protocol::browser_view::{BUSY, NOT_RUNNING};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};
use tokio::time::Instant;

use super::audit::{self, Actor, Lines, short};
use super::runtime::{Acquire, BrowserError, BrowserRuntime, Lease, LeaseRelays, Mode};
use super::view::{Refused, RfbRelease, Stream, View, ViewParts};
use crate::http::service::{Relay, Upgraded};

use drivers::Drivers;

/// Streams of watched screens the Space carries at once.
pub const VIEWS: usize = 2;

/// Where a task's downloads are kept: the Team's files.
pub trait WebFiles: Send + Sync {
    /// Stores `data` as an untrusted Team file marked as from the web, its media type detected from its bytes, and
    /// returns its reference (`id`, `name`, `media_type`, `size`).
    ///
    /// # Errors
    /// [`PutRefused`] when it was not stored (quota, name, or store).
    fn put<'a>(
        &'a self,
        team_id: &'a str,
        name: &'a str,
        data: Vec<u8>,
    ) -> BoxFuture<'a, Result<Value, PutRefused>>;
}

/// What one turn's task runs with.
pub struct BrowserTurn {
    /// The Team.
    pub team_id: String,
    /// The Team's incarnation (its network's id).
    pub incarnation: String,
    /// The turn's interface language.
    pub locale: Option<String>,
    /// The turn's Stop: it removes the running Browser at once.
    pub stop: Stop,
    /// The task's Brain calls under the turn's key.
    pub brain: Arc<dyn TaskBrain>,
    /// The provider's model prices.
    pub price: Price,
    /// The verified principal of the turn's request, for the audit.
    pub actor: Actor,
}

/// The Browser's page language for the turn's interface language.
#[must_use]
pub fn language(locale: Option<&str>) -> &'static str {
    if locale.is_some_and(|locale| locale.to_lowercase().starts_with("pt")) {
        "pt-BR"
    } else {
        "en"
    }
}

/// The Browser a Team holds now: whose it is, the lease, and its view.
#[derive(Clone, Debug)]
pub(crate) struct Held {
    pub(crate) token: String,
    pub(crate) lease: Lease,
    pub(crate) view: Arc<View>,
}

/// Each Team's held Browser.
pub(crate) type Running = Arc<Mutex<HashMap<String, Held>>>;

/// What the service runs on.
pub struct ServiceParts {
    /// The Space's Browser runtime.
    pub runtime: Arc<BrowserRuntime>,
    /// The Team files downloads go to.
    pub files: Arc<dyn WebFiles>,
    /// The audit log.
    pub audit: Arc<dyn AuditSink>,
    /// The random source of tokens and view ids.
    pub entropy: Arc<dyn Entropy>,
}

/// Runs Browser tasks for chat turns and carries the views of the Space's held Browser.
pub struct BrowserService {
    runtime: Arc<BrowserRuntime>,
    files: Arc<dyn WebFiles>,
    audit: Arc<dyn AuditSink>,
    entropy: Arc<dyn Entropy>,
    running: Running,
    views: Arc<Semaphore>,
}

impl std::fmt::Debug for BrowserService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BrowserService")
    }
}

/// How a task's work ended short of an outcome.
enum Lost {
    Runtime(BrowserError),
    Driver(CuaError),
}

impl BrowserService {
    /// The service over `parts`.
    #[must_use]
    pub fn new(parts: ServiceParts) -> Self {
        Self {
            runtime: parts.runtime,
            files: parts.files,
            audit: parts.audit,
            entropy: parts.entropy,
            running: Arc::new(Mutex::new(HashMap::new())),
            views: Arc::new(Semaphore::new(VIEWS)),
        }
    }

    /// The runtime.
    #[must_use]
    pub fn runtime(&self) -> &Arc<BrowserRuntime> {
        &self.runtime
    }

    /// Whether this Team image can run a Browser.
    #[must_use]
    pub fn available(&self) -> bool {
        self.runtime.available()
    }

    /// A new view of `lease` for `lines`, controlled with `lease`'s credentials when it has them.
    ///
    /// # Errors
    /// [`Refused`] when no id could be drawn.
    pub fn view(&self, lease: &Lease, lines: Lines, private: bool) -> Result<Arc<View>, Refused> {
        View::new(ViewParts {
            relays: Arc::new(LeaseRelays {
                runtime: Arc::clone(&self.runtime),
                lease: lease.clone(),
            }),
            lines,
            credentials: lease.credentials.clone(),
            private,
            entropy: Arc::clone(&self.entropy),
            release: Arc::new(RfbRelease(Arc::clone(&self.entropy))),
        })
    }

    /// Makes `lease` the Team's held Browser, watched through `view`; refused while the Team holds another.
    ///
    /// # Errors
    /// [`BrowserError::BUSY`] while the Team holds another.
    pub async fn hold(
        &self,
        team_id: &str,
        token: &str,
        lease: &Lease,
        view: &Arc<View>,
    ) -> Result<(), BrowserError> {
        let mut running = self.running.lock().await;
        if running.contains_key(team_id) {
            return Err(BrowserError::BUSY);
        }
        running.insert(
            team_id.to_owned(),
            Held {
                token: token.to_owned(),
                lease: lease.clone(),
                view: Arc::clone(view),
            },
        );
        Ok(())
    }

    /// No longer holds the Team's Browser watched through `view`; another one stays.
    pub async fn drop_view(&self, team_id: &str, view: &Arc<View>) {
        let mut running = self.running.lock().await;
        if running
            .get(team_id)
            .is_some_and(|held| Arc::ptr_eq(&held.view, view))
        {
            running.remove(team_id);
        }
    }

    /// The id of the screen view of the Browser the Team holds now; refused when it holds none or only a private one.
    ///
    /// # Errors
    /// [`Refused`] (`browser-not-running`).
    pub async fn open_view(&self, team_id: &str) -> Result<String, Refused> {
        let running = self.running.lock().await;
        match running.get(team_id) {
            Some(held) if !held.view.private() => Ok(held.view.id().to_owned()),
            _ => Err(Refused(NOT_RUNNING)),
        }
    }

    /// Opens one viewer's stream of the Team's view `view_id`, holding one of the Space's stream slots for the whole
    /// stream. `control` asks for the person's own control.
    ///
    /// # Errors
    /// [`Refused`] with a closed code before the viewer gets any byte.
    pub async fn open_stream(
        &self,
        team_id: &str,
        view_id: &str,
        key: &str,
        control: bool,
    ) -> Result<Box<dyn Relay>, Refused> {
        let permit = Arc::clone(&self.views)
            .try_acquire_owned()
            .map_err(|_| Refused(BUSY))?;
        let view = {
            let running = self.running.lock().await;
            match running.get(team_id) {
                Some(held) if held.view.id() == view_id => Arc::clone(&held.view),
                _ => return Err(Refused(NOT_RUNNING)),
            }
        };
        let stream = view.open(key, control).await?;
        Ok(Box::new(Slotted { stream, permit }))
    }

    /// Ends the turn `token`'s held Browser at once: its view ends and it is removed; its task's release proves it.
    async fn interrupt(&self, team_id: &str, token: &str) {
        let lease = {
            let mut running = self.running.lock().await;
            match running.get(team_id) {
                Some(held) if held.token == token => {
                    // From here no download of the task is stored: a store under way finished before this took the lock.
                    let held = running.remove(team_id);
                    held.map(|held| {
                        held.view.end();
                        held.lease
                    })
                }
                _ => None,
            }
        };
        if let Some(lease) = lease {
            // A failed removal leaves the release to prove it, which keeps the Space refusing tasks until it does.
            let _ = self.runtime.kill(&lease).await;
        }
    }

    /// Runs one turn's Browser task for `objective`: the result the Brain resumes with, and the terminal's `browser`
    /// field.
    pub async fn run(&self, turn: &BrowserTurn, objective: &str) -> (Value, Value) {
        let page_language = language(turn.locale.as_deref());
        // One absolute deadline from before the Browser starts, through its view and the task, to its ingest.
        let deadline = Instant::now() + MAX_SECONDS;
        let acquired = self
            .runtime
            .acquire(Acquire {
                team_id: &turn.team_id,
                language: page_language,
                mode: Mode::Agent,
                capsule: None,
                deadline: Some(deadline),
                stop: Some(&turn.stop),
            })
            .await;
        let lease = match acquired {
            Ok(lease) => lease,
            Err(error) => {
                let _ = audit::record(
                    self.audit.as_ref(),
                    &turn.actor,
                    "browser-task",
                    "denied",
                    &turn.team_id,
                    error.0,
                )
                .await;
                let failed = Outcome {
                    reason: Some(error.0.to_owned()),
                    ..Outcome::new(Status::Failed)
                };
                return (result(&failed, None), terminal(None));
            }
        };
        let (outcome, ingested) = self.held_task(turn, objective, &lease, deadline).await;
        let released = self.runtime.release(&lease).await.is_ok();
        let ending = format!("{}:released={}", ending(&outcome), u8::from(released));
        let _ = self.audit_task(turn, &lease, &ending).await;
        let outcome = if released {
            outcome
        } else {
            lost(outcome, "release")
        };
        (
            result(&outcome, ingested.as_ref()),
            terminal(ingested.as_ref()),
        )
    }

    /// From the moment it is held, the lease is released however the task ends; Stop removes it at once.
    async fn held_task(
        &self,
        turn: &BrowserTurn,
        objective: &str,
        lease: &Lease,
        deadline: Instant,
    ) -> (Outcome, Option<Ingested>) {
        let Some((token, view)) = self.hold_task(turn, lease).await else {
            return (
                lost(Outcome::new(Status::Failed), "runtime/browser-lost"),
                None,
            );
        };
        let done = Stop::new();
        let worked = async {
            let outcome = self.work(turn, objective, lease, &token, deadline).await;
            done.stop();
            outcome
        };
        let watched = async {
            // A Stop that came as the task ended still removes the Browser at once.
            tokio::select! {
                biased;
                () = turn.stop.stopped() => self.interrupt(&turn.team_id, &token).await,
                () = done.stopped() => {}
            }
        };
        let (worked, ()) = tokio::join!(worked, watched);
        self.drop_view(&turn.team_id, &view).await;
        view.end();
        worked.unwrap_or_else(|failure| {
            let outcome = match failure {
                Lost::Runtime(error) => lost(
                    Outcome::new(Status::Failed),
                    &format!("runtime/{}", error.0),
                ),
                Lost::Driver(_) if turn.stop.is_stopped() => Outcome::new(Status::Stopped),
                Lost::Driver(error) => lost(
                    Outcome::new(Status::Failed),
                    &format!("driver/{}", error.code),
                ),
            };
            (outcome, None)
        })
    }

    /// Holds `lease` as the Team's running task with its own token and view.
    async fn hold_task(&self, turn: &BrowserTurn, lease: &Lease) -> Option<(String, Arc<View>)> {
        let token = token_hex(self.entropy.as_ref(), 16).ok()?;
        let lines = Lines::new(
            Arc::clone(&self.audit),
            turn.actor.clone(),
            "browser-view",
            &turn.team_id,
            "",
            format!(":container={}", short(&lease.container_id)),
        );
        let view = self.view(lease, lines, false).ok()?;
        self.running.lock().await.insert(
            turn.team_id.clone(),
            Held {
                token: token.clone(),
                lease: lease.clone(),
                view: Arc::clone(&view),
            },
        );
        Some((token, view))
    }

    /// The task and then its downloads, each only while the turn runs; an egress replaced meanwhile loses it.
    async fn work(
        &self,
        turn: &BrowserTurn,
        objective: &str,
        lease: &Lease,
        token: &str,
        deadline: Instant,
    ) -> Result<(Outcome, Option<Ingested>), Lost> {
        self.audit_task(turn, lease, "started")
            .await
            .map_err(|()| Lost::Runtime(BrowserError::LOST))?;
        // A Stop during creation found no lease to remove: no channel opens once the turn is stopped.
        if turn.stop.is_stopped() {
            return Ok((Outcome::new(Status::Stopped), None));
        }
        // A start that took the whole deadline leaves the task no time.
        if Instant::now() >= deadline {
            return Ok((Outcome::new(Status::Budget), None));
        }
        let drivers = Arc::new(Drivers::new(
            Arc::clone(&self.runtime),
            Arc::clone(&self.running),
            lease.clone(),
            token.to_owned(),
            turn.stop.clone(),
        ));
        let session = drivers
            .fresh(deadline.min(Instant::now() + team_browser::cua::CALL_SECONDS))
            .await
            .map_err(|failure| match failure {
                drivers::Fresh::Runtime(error) => Lost::Runtime(error),
                drivers::Fresh::Driver(error) => Lost::Driver(error),
            })?;
        let seams = TaskSeams {
            brain: Arc::clone(&turn.brain),
            price: Arc::clone(&turn.price),
            stop: turn.stop.clone(),
            host: drivers,
        };
        let page_language = language(turn.locale.as_deref()).to_owned();
        let outcome = Task::new(
            objective.to_owned(),
            page_language,
            session,
            seams,
            deadline,
        )
        .run()
        .await;
        if outcome.status == Status::Stopped || turn.stop.is_stopped() {
            return Ok((outcome, None));
        }
        let (outcome, ingested) = self.downloads(turn, lease, token, deadline, outcome).await;
        if !self.runtime.intact(lease).await {
            return Ok((lost(outcome, "runtime/not-intact"), ingested));
        }
        Ok((outcome, ingested))
    }

    /// The task's downloads; when they cannot be read, its own answer stands without them, the audit saying why.
    async fn downloads(
        &self,
        turn: &BrowserTurn,
        lease: &Lease,
        token: &str,
        deadline: Instant,
        outcome: Outcome,
    ) -> (Outcome, Option<Ingested>) {
        let listing = match self.runtime.files(lease, Some(deadline), None).await {
            Ok(listing) => listing,
            Err(error) => {
                let cause = format!("ingest/{}", error.0);
                return (with_cause(outcome, &cause), None);
            }
        };
        let sink = TaskDownloads {
            service: self,
            turn,
            lease,
            token,
            deadline,
        };
        match ingest::ingest(&listing, &sink, &turn.stop).await {
            Ok(ingested) => (outcome, Some(ingested)),
            Err(_) => (with_cause(outcome, "ingest/listing"), None),
        }
    }

    async fn audit_task(&self, turn: &BrowserTurn, lease: &Lease, state: &str) -> Result<(), ()> {
        let egress = &lease.egress;
        let detail = format!(
            "{state}:incarnation={}:container={}:address={}:egress={}@{}:generation={}",
            turn.incarnation.get(..12).unwrap_or(&turn.incarnation),
            short(&lease.container_id),
            lease.address,
            short(&egress.container_id),
            egress.started,
            egress.generation
        );
        audit::record(
            self.audit.as_ref(),
            &turn.actor,
            "browser-task",
            "ok",
            &turn.team_id,
            &detail,
        )
        .await
        .map_err(drop)
    }
}

/// A stream holding one of the Space's stream slots until it ends.
struct Slotted {
    stream: Stream,
    permit: OwnedSemaphorePermit,
}

impl Relay for Slotted {
    fn run(self: Box<Self>, client: Box<dyn Upgraded>) -> BoxFuture<'static, ()> {
        let Self { stream, permit } = *self;
        Box::pin(async move {
            Box::new(stream).run(client).await;
            drop(permit);
        })
    }
}

/// The task's downloads, read from its Browser and stored while the turn still holds it, before the deadline.
struct TaskDownloads<'a> {
    service: &'a BrowserService,
    turn: &'a BrowserTurn,
    lease: &'a Lease,
    token: &'a str,
    deadline: Instant,
}

impl DownloadSink for TaskDownloads<'_> {
    fn read<'a>(&'a self, name: &'a str, size: u64) -> BoxFuture<'a, Result<Vec<u8>, ReadLost>> {
        Box::pin(async move {
            if Instant::now() >= self.deadline {
                return Err(ReadLost);
            }
            let data = self
                .service
                .runtime
                .read(self.lease, name, size, Some(self.deadline))
                .await
                .map_err(|_| ReadLost)?;
            // Nothing read at or past the task's deadline is stored.
            if Instant::now() >= self.deadline {
                return Err(ReadLost);
            }
            Ok(data)
        })
    }

    fn put<'a>(&'a self, name: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<Value, PutRefused>> {
        Box::pin(async move {
            // Under the lock Stop takes to remove the Browser, so no download is stored once Stop has done so.
            let running = self.service.running.lock().await;
            let held = running
                .get(&self.turn.team_id)
                .is_some_and(|held| held.token == self.token);
            if self.turn.stop.is_stopped() || !held {
                return Err(PutRefused::Stopped);
            }
            // Nothing is stored at or past the task's deadline, however long the read took.
            if Instant::now() >= self.deadline {
                return Err(PutRefused::ReadLost);
            }
            let stored = self.service.files.put(&self.turn.team_id, name, data).await;
            drop(running);
            stored
        })
    }
}

/// The task lost its Browser after `outcome`: its steps and recoveries kept, and where it was lost.
fn lost(outcome: Outcome, cause: &str) -> Outcome {
    Outcome {
        status: Status::Failed,
        summary: None,
        reason: Some("browser-lost".to_owned()),
        sentence: None,
        cause: Some(cause.to_owned()),
        ..outcome
    }
}

fn with_cause(outcome: Outcome, cause: &str) -> Outcome {
    Outcome {
        cause: Some(cause.to_owned()),
        ..outcome
    }
}

/// How the task ended, for its audit line: closed words and counts only, never page content.
fn ending(outcome: &Outcome) -> String {
    let ending = format!(
        "{}:steps={}:recovered={}:relaunched={}",
        outcome.status.as_str(),
        outcome.steps,
        outcome.recovered,
        outcome.relaunched
    );
    match &outcome.cause {
        Some(cause) => format!("{ending}:cause={cause}"),
        None => ending,
    }
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn count(value: u64) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn member<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    match value {
        Value::Object(members) => members
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value),
        _ => None,
    }
}

/// What the chat agent reads of the task: its ending, the agent's summary, and each download's outcome.
#[must_use]
pub fn result(outcome: &Outcome, ingested: Option<&Ingested>) -> Value {
    let status = match outcome.status {
        Status::Done => "done",
        Status::NeedsPerson => "needs-person",
        Status::Stopped => "stopped",
        Status::Budget | Status::Failed => "failed",
    };
    let mut members = vec![("status".to_owned(), text(status))];
    if let Some(summary) = &outcome.summary {
        members.push(("summary".to_owned(), text(summary)));
    }
    if let Some(completeness) = outcome.completeness {
        // What provenance screening and the advisory completeness check left unresolved, for the chat agent to say.
        members.push(("completeness".to_owned(), text(completeness)));
        if let Some(missing) = &outcome.missing {
            members.push(("missing".to_owned(), text(missing)));
        }
        let shown: Vec<Value> = outcome
            .unscreened
            .iter()
            .take(MAX_UNSCREENED_URLS)
            .map(|url| text(url))
            .collect();
        let more = outcome.unscreened.len() - shown.len();
        members.push(("unscreened_urls".to_owned(), Value::Array(shown)));
        members.push((
            "unscreened_more".to_owned(),
            count(u64::try_from(more).unwrap_or(u64::MAX)),
        ));
    }
    if let Some(reason) = &outcome.reason {
        members.push(("reason".to_owned(), text(reason)));
    }
    if let Some(sentence) = &outcome.sentence {
        members.push(("sentence".to_owned(), text(sentence)));
    }
    if outcome
        .cause
        .as_deref()
        .is_some_and(|cause| cause.starts_with("ingest/"))
    {
        // The task's downloads could not be read: none reached the Team, whatever the answer says about files.
        members.push(("downloads".to_owned(), text("unread")));
    }
    members.push(("files".to_owned(), Value::Array(files(ingested))));
    Value::Object(members)
}

fn files(ingested: Option<&Ingested>) -> Vec<Value> {
    let Some(ingested) = ingested else {
        return Vec::new();
    };
    let kept = ingested.kept.iter().map(|item| {
        let mut members: Vec<(String, Value)> = ["name", "media_type", "size"]
            .iter()
            .filter_map(|key| member(item, key).map(|value| ((*key).to_owned(), value.clone())))
            .collect();
        members.push(("outcome".to_owned(), text("kept")));
        Value::Object(members)
    });
    let lost = ingested.lost.iter().map(|(name, reason)| {
        Value::Object(vec![
            ("name".to_owned(), text(name)),
            ("outcome".to_owned(), text(reason)),
        ])
    });
    kept.chain(lost).collect()
}

/// The terminal's `browser` field: the kept files' references, the losses, and how many were unnamed.
#[must_use]
pub fn terminal(ingested: Option<&Ingested>) -> Value {
    let (kept, lost, skipped) = ingested.map_or((Vec::new(), Vec::new(), 0), |ingested| {
        let lost = ingested
            .lost
            .iter()
            .map(|(name, reason)| {
                Value::Object(vec![
                    ("name".to_owned(), text(name)),
                    ("reason".to_owned(), text(reason)),
                ])
            })
            .collect();
        (ingested.kept.clone(), lost, ingested.skipped)
    });
    Value::Object(vec![
        ("files".to_owned(), Value::Array(kept)),
        ("lost".to_owned(), Value::Array(lost)),
        ("skipped".to_owned(), count(skipped)),
    ])
}

#[cfg(test)]
mod tests;
