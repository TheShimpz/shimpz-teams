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
mod runs;

use std::collections::HashMap;
use std::sync::Arc;

use holdpoint::{Clock as _, RunOptions, SystemClock};
use strict_json::{Number, Value};
use team_browser::ingest::{self, DownloadSink, Ingested, PutRefused, ReadLost};
use team_browser::run::{
    DEFINITION, MAX_SECONDS, MAX_SPEND, Outcome, Price, RunCredentials, Status, TaskBrain,
    TaskInput, outcome_of,
};
use team_core::audit::AuditSink;
use team_core::entropy::Entropy;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser::MAX_UNSCREENED_URLS;
use team_protocol::browser_view::{BUSY, NOT_RUNNING};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};
use tokio::time::Instant;

use super::audit::{self, Actor, Lines, short};
use super::runtime::{BrowserError, BrowserRuntime, Lease, LeaseRelays};
use super::view::{Refused, RfbRelease, Stream, View, ViewParts};
use crate::http::service::{Relay, Upgraded};

pub(crate) use drivers::plain as drivers_channel;
pub(crate) use runs::{Runs, RunsParts};
pub use runs::{TurnContext, Unsettled};

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
    /// The directory each Team's task ledger lives under.
    pub runs: std::path::PathBuf,
}

/// Runs Browser tasks for chat turns and carries the views of the Space's held Browser.
pub struct BrowserService {
    runtime: Arc<BrowserRuntime>,
    files: Arc<dyn WebFiles>,
    audit: Arc<dyn AuditSink>,
    entropy: Arc<dyn Entropy>,
    running: Running,
    views: Arc<Semaphore>,
    runs: Runs,
}

impl std::fmt::Debug for BrowserService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BrowserService")
    }
}

impl BrowserService {
    /// The service over `parts`, its tasks' attempts driving Team's current runtime.
    ///
    /// # Errors
    /// [`Unsettled`] when the runs cannot be set up.
    pub fn new(parts: ServiceParts) -> Result<Self, Unsettled> {
        let running: Running = Arc::new(Mutex::new(HashMap::new()));
        let runs = Runs::new(RunsParts {
            root: parts.runs,
            runtime: Arc::clone(&parts.runtime),
            running: Arc::clone(&running),
            audit: Arc::clone(&parts.audit),
            entropy: Arc::clone(&parts.entropy),
        })?;
        Ok(Self {
            runtime: parts.runtime,
            files: parts.files,
            audit: parts.audit,
            entropy: parts.entropy,
            running,
            views: Arc::new(Semaphore::new(VIEWS)),
            runs,
        })
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
        view_of(&self.runtime, &self.entropy, lease, lines, private)
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

    /// Runs one turn's Browser task for `objective` as the durable run answering the Brain's Browser
    /// `interrupt_id`: the result the Brain resumes with, and the terminal's `browser` field. A run an earlier Team
    /// process started for the same interrupt is driven on from its ledger.
    pub async fn run(
        &self,
        turn: &BrowserTurn,
        objective: &str,
        interrupt_id: &str,
    ) -> (Value, Value) {
        let page_language = language(turn.locale.as_deref());
        let input = TaskInput {
            team_id: turn.team_id.clone(),
            interrupt_id: interrupt_id.to_owned(),
            objective: objective.to_owned(),
            language: page_language.to_owned(),
            started_ms: SystemClock.now_ms(),
            dearest: (turn.price)(None),
        };
        let run_id = input.run_id();
        let worker = match self.runs.worker(&turn.team_id) {
            Ok(worker) => worker,
            Err(unsettled) => return self.denied(turn, unsettled.0).await,
        };
        let options = RunOptions {
            deadline_ms: Some(input.deadline_ms()),
            spend_ceiling: Some(MAX_SPEND),
        };
        // The same run submitted again is the same run; one an earlier process started keeps its own input.
        match worker.submit(DEFINITION, &run_id, &input.to_json(), &options) {
            Ok(_) | Err(holdpoint::Error::Conflict(_)) => {}
            Err(_) => return self.denied(turn, "browser-unavailable").await,
        }
        let context = Arc::new(TurnContext {
            team_id: turn.team_id.clone(),
            incarnation: turn.incarnation.clone(),
            language: page_language.to_owned(),
            stop: turn.stop.clone(),
            actor: turn.actor.clone(),
        });
        let credentials = Arc::new(RunCredentials {
            brain: Arc::clone(&turn.brain),
            price: Arc::clone(&turn.price),
        });
        self.runs.lend(&run_id, context, credentials);
        let outcome = self.drive(turn, &worker, &run_id).await;
        let (outcome, ingested) = self.settle(turn, &run_id, outcome).await;
        self.runs.unlend(&run_id);
        (
            result(&outcome, ingested.as_ref()),
            terminal(ingested.as_ref()),
        )
    }

    /// Acknowledges the run answering `interrupt_id` once the Brain resumed with its result: it is never run again.
    pub fn acknowledge(&self, team_id: &str, interrupt_id: &str) {
        self.runs
            .acknowledge(team_id, &team_browser::run::run_id(team_id, interrupt_id));
    }

    /// Discards the task answering `interrupt_id` of a turn that was settled without it: cancelled, then deleted once
    /// its executors are proven ended; one that cannot be deleted yet goes with the Team's ledger.
    pub fn discard(&self, team_id: &str, interrupt_id: &str) {
        self.runs
            .discard(team_id, &team_browser::run::run_id(team_id, interrupt_id));
    }

    /// Deletes a Team's task ledger, for its teardown.
    ///
    /// # Errors
    /// [`BrowserError`] when it may remain.
    pub fn forget_team(&self, team_id: &str) -> Result<(), BrowserError> {
        self.runs
            .forget_team(team_id)
            .map_err(|_| BrowserError::UNAVAILABLE)
    }

    /// Deletes every Team's task ledger, for the Space reset.
    ///
    /// # Errors
    /// [`BrowserError`] when one may remain.
    pub fn forget_all(&self) -> Result<(), BrowserError> {
        self.runs
            .forget_all()
            .map_err(|_| BrowserError::UNAVAILABLE)
    }

    /// A task refused before any Browser, audited as denied.
    async fn denied(&self, turn: &BrowserTurn, reason: &'static str) -> (Value, Value) {
        let _ = audit::record(
            self.audit.as_ref(),
            &turn.actor,
            "browser-task",
            "denied",
            &turn.team_id,
            reason,
        )
        .await;
        let failed = Outcome {
            reason: Some(reason.to_owned()),
            ..Outcome::new(Status::Failed)
        };
        (result(&failed, None), terminal(None))
    }

    /// Drives the run to its end; the turn's Stop cancels it and removes its Browser at once.
    async fn drive(
        &self,
        turn: &BrowserTurn,
        worker: &Arc<holdpoint::Worker>,
        run_id: &str,
    ) -> Outcome {
        let deadline = Instant::now() + MAX_SECONDS + team_browser::run::FINISH_SECONDS;
        let done = Stop::new();
        let driven = async {
            let driven = self.runs.drive(worker, run_id, deadline).await;
            done.stop();
            driven
        };
        let watched = async {
            tokio::select! {
                biased;
                () = turn.stop.stopped() => {
                    let _ = worker.cancel(run_id);
                    self.runs.interrupt(run_id).await;
                }
                () = done.stopped() => {}
            }
        };
        let (driven, ()) = tokio::join!(driven, watched);
        match driven {
            Ok(ended) => outcome_of(&ended),
            Err(unsettled) => lost(
                Outcome::new(Status::Failed),
                &format!("run/{}", unsettled.0),
            ),
        }
    }

    /// After the run: its downloads while the turn runs, then its Browser released and the ending audited.
    async fn settle(
        &self,
        turn: &BrowserTurn,
        run_id: &str,
        outcome: Outcome,
    ) -> (Outcome, Option<Ingested>) {
        let (outcome, ingested) = match self.runs.held(run_id) {
            Some((lease, token))
                if outcome.status != Status::Stopped && !turn.stop.is_stopped() =>
            {
                let deadline = Instant::now() + team_browser::run::FINISH_SECONDS;
                let (outcome, ingested) = self
                    .downloads(turn, &lease, &token, deadline, outcome)
                    .await;
                if self.runtime.intact(&lease).await {
                    (outcome, ingested)
                } else {
                    (lost(outcome, "runtime/not-intact"), ingested)
                }
            }
            _ => (outcome, None),
        };
        let (released, lease) = self.runs.release(run_id).await;
        self.audit_ending(turn, lease.as_ref(), &outcome, released)
            .await;
        let outcome = if released {
            outcome
        } else {
            lost(outcome, "release")
        };
        (outcome, ingested)
    }

    /// The task's ending in the audit: denied when no Browser was ever held, else its Browser's line.
    async fn audit_ending(
        &self,
        turn: &BrowserTurn,
        lease: Option<&Lease>,
        outcome: &Outcome,
        released: bool,
    ) {
        let Some(lease) = lease else {
            if let (Status::Failed, Some(reason)) = (outcome.status, &outcome.reason) {
                let sink = self.audit.as_ref();
                let _ = audit::record(
                    sink,
                    &turn.actor,
                    "browser-task",
                    "denied",
                    &turn.team_id,
                    reason,
                )
                .await;
            }
            return;
        };
        let context = TurnContext {
            team_id: turn.team_id.clone(),
            incarnation: turn.incarnation.clone(),
            language: String::new(),
            stop: turn.stop.clone(),
            actor: turn.actor.clone(),
        };
        let ending = format!("{}:released={}", ending(outcome), u8::from(released));
        let _ = audit_task(self.audit.as_ref(), &context, lease, &ending).await;
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
}

/// One audit line of a task's Browser: its Team and incarnation, the container, its address, and the egress process
/// and generation it ran on, never a URL or page text.
pub(super) async fn audit_task(
    sink: &dyn AuditSink,
    turn: &TurnContext,
    lease: &Lease,
    state: &str,
) -> Result<(), ()> {
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
        sink,
        &turn.actor,
        "browser-task",
        "ok",
        &turn.team_id,
        &detail,
    )
    .await
    .map_err(drop)
}

/// A new view of `lease` for `lines`, controlled with `lease`'s credentials when it has them.
pub(super) fn view_of(
    runtime: &Arc<BrowserRuntime>,
    entropy: &Arc<dyn Entropy>,
    lease: &Lease,
    lines: Lines,
    private: bool,
) -> Result<Arc<View>, Refused> {
    View::new(ViewParts {
        relays: Arc::new(LeaseRelays {
            runtime: Arc::clone(runtime),
            lease: lease.clone(),
        }),
        lines,
        credentials: lease.credentials.clone(),
        private,
        entropy: Arc::clone(entropy),
        release: Arc::new(RfbRelease(Arc::clone(entropy))),
    })
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
