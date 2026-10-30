//! The worker: registers definitions, models, and activities; submits, drives, and resumes runs.

use std::collections::{HashMap, HashSet};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use serde_json::Value;

use crate::clock::{Clock, SystemClock};
use crate::ctx::Ctx;
use crate::definition::{Definition, Driven, Interrupt};
use crate::error::Error;
use crate::event::{Event, Outcome};
use crate::failpoint;
use crate::handler::{Activity, Model};
use crate::history::History;
use crate::host::{Host, Proof, RunInfo};
use crate::limits::WorkerOptions;
use crate::policy::{ActivityOptions, RetryPolicy, Timeouts};
use crate::session::Session;
use crate::store::{NewRun, Obligation, PendingRequest, Resolution, Store, Submitted, check_id};

/// How a registered model's calls are retried and bounded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelOptions {
    /// Retries of failed calls; by default retryable failures are retried until the run's deadline.
    pub retry: RetryPolicy,
    /// Time bounds of each call.
    pub timeouts: Timeouts,
}

impl ModelOptions {
    /// The digest of everything that decides how the model's calls are retried and bounded.
    #[must_use]
    pub fn digest(&self) -> String {
        crate::json::digest(&crate::policy::describe(&self.retry, &self.timeouts))
    }
}

impl Default for ModelOptions {
    fn default() -> Self {
        Self {
            retry: RetryPolicy::default(),
            timeouts: Timeouts { start_to_close_ms: Some(300_000), ..Timeouts::default() },
        }
    }
}

/// Attempts this worker abandoned whose threads have not returned yet.
#[derive(Debug, Default)]
pub(crate) struct Abandoned(pub(crate) Mutex<HashSet<(String, u32, u32)>>);

impl Abandoned {
    pub(crate) fn count(&self) -> usize {
        self.0.lock().unwrap_or_else(PoisonError::into_inner).len()
    }
}

/// What every session of a worker shares.
pub(crate) struct Shared {
    pub(crate) store: Arc<Store>,
    pub(crate) host: Arc<dyn Host>,
    pub(crate) clock: Arc<dyn Clock>,
    pub(crate) options: WorkerOptions,
    pub(crate) incarnation: String,
    pub(crate) definitions: HashMap<String, Arc<dyn Definition>>,
    pub(crate) models: HashMap<String, (Arc<dyn Model>, ModelOptions)>,
    pub(crate) activities: HashMap<String, (Arc<dyn Activity>, ActivityOptions)>,
    pub(crate) abandoned: Arc<Abandoned>,
    pub(crate) driving: Mutex<HashMap<String, Arc<AtomicBool>>>,
    pub(crate) stopping: AtomicBool,
    sessions: AtomicU64,
}

impl Shared {
    pub(crate) fn session_id(&self) -> String {
        format!("{}:{}", self.incarnation, self.sessions.fetch_add(1, Ordering::SeqCst))
    }

    pub(crate) fn is_abandoned(&self, run_id: &str, step: u32, attempt: u32) -> bool {
        self.abandoned.0.lock().unwrap_or_else(PoisonError::into_inner).contains(&(run_id.to_owned(), step, attempt))
    }
}

/// Builds a [`Worker`].
pub struct WorkerBuilder {
    store: Arc<Store>,
    owner: String,
    host: Arc<dyn Host>,
    clock: Arc<dyn Clock>,
    options: WorkerOptions,
    definitions: HashMap<String, Arc<dyn Definition>>,
    models: HashMap<String, (Arc<dyn Model>, ModelOptions)>,
    activities: HashMap<String, (Arc<dyn Activity>, ActivityOptions)>,
}

impl WorkerBuilder {
    /// Registers a definition under its id.
    #[must_use]
    pub fn definition(mut self, definition: impl Definition + 'static) -> Self {
        self.definitions.insert(definition.id().to_owned(), Arc::new(definition));
        self
    }

    /// Registers a model under `name`.
    #[must_use]
    pub fn model(mut self, name: &str, options: ModelOptions, model: impl Model + 'static) -> Self {
        self.models.insert(name.to_owned(), (Arc::new(model), options));
        self
    }

    /// Registers an activity under `name`.
    #[must_use]
    pub fn activity(mut self, name: &str, options: ActivityOptions, activity: impl Activity + 'static) -> Self {
        self.activities.insert(name.to_owned(), (Arc::new(activity), options));
        self
    }

    /// Replaces the worker's clock.
    #[must_use]
    pub fn clock(mut self, clock: impl Clock + 'static) -> Self {
        self.clock = Arc::new(clock);
        self
    }

    /// Replaces the worker's timing options.
    #[must_use]
    pub fn options(mut self, options: WorkerOptions) -> Self {
        self.options = options;
        self
    }

    /// Builds the worker.
    #[must_use]
    pub fn build(self) -> Worker {
        let started = SystemClock.now_ms();
        let incarnation = format!("{}:{}:{started}", self.owner, std::process::id());
        Worker {
            shared: Arc::new(Shared {
                store: self.store,
                host: self.host,
                clock: self.clock,
                options: self.options,
                incarnation,
                definitions: self.definitions,
                models: self.models,
                activities: self.activities,
                abandoned: Arc::default(),
                driving: Mutex::default(),
                stopping: AtomicBool::new(false),
                sessions: AtomicU64::new(0),
            }),
            scheduler: Mutex::default(),
        }
    }
}

/// Drives runs. Cheap to share across threads behind an `Arc`.
pub struct Worker {
    pub(crate) shared: Arc<Shared>,
    pub(crate) scheduler: Mutex<Option<crate::scheduler::Scheduler>>,
}

/// Submission options of a run.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunOptions {
    /// The wall-clock time past which the run expires.
    pub deadline_ms: Option<i64>,
    /// The most the run may spend, in the hosts' cost unit.
    pub spend_ceiling: Option<u64>,
}

struct Driving<'a> {
    shared: &'a Shared,
    run_id: String,
}

impl Drop for Driving<'_> {
    fn drop(&mut self) {
        self.shared.driving.lock().unwrap_or_else(PoisonError::into_inner).remove(&self.run_id);
    }
}

impl Worker {
    /// Starts building a worker named `owner` over `store`, with `host`'s hooks.
    pub fn builder(store: Arc<Store>, owner: &str, host: impl Host + 'static) -> WorkerBuilder {
        WorkerBuilder {
            store,
            owner: owner.to_owned(),
            host: Arc::new(host),
            clock: Arc::new(SystemClock),
            options: WorkerOptions::default(),
            definitions: HashMap::new(),
            models: HashMap::new(),
            activities: HashMap::new(),
        }
    }

    /// The worker's store.
    #[must_use]
    pub fn store(&self) -> &Arc<Store> {
        &self.shared.store
    }

    /// Submits a run of `definition`, idempotently by `run_id`: the same id with the same definition, version, and
    /// input is the same run.
    ///
    /// # Errors
    ///
    /// [`Error::Contract`] for an unknown definition or a bad id; [`Error::Conflict`] when the id names another
    /// run; [`Error::Limit`] when the store is full.
    pub fn submit(
        &self,
        definition: &str,
        run_id: &str,
        input: &Value,
        options: &RunOptions,
    ) -> Result<Submitted, Error> {
        let registered = self
            .shared
            .definitions
            .get(definition)
            .ok_or_else(|| Error::Contract(format!("unknown definition {definition}")))?;
        let submitted = self.shared.store.create(&NewRun {
            id: run_id,
            definition,
            version: registered.version(),
            input,
            deadline_ms: options.deadline_ms,
            spend_ceiling: options.spend_ceiling,
            now_ms: self.shared.clock.now_ms(),
        })?;
        failpoint::hit("submit.committed");
        self.notify();
        Ok(submitted)
    }

    /// Drives a run until it suspends or ends, on the calling thread.
    ///
    /// # Errors
    ///
    /// [`Error::Busy`] when another session holds the run; [`Error::LeaseLost`] when this session's lease ended;
    /// [`Error::State`] for an unknown run or one whose definition this worker does not know; [`Error::Storage`]
    /// for a corrupt history or a ledger failure.
    pub fn drive(&self, run_id: &str) -> Result<Driven, Error> {
        check_id(run_id)?;
        let cancel = Arc::new(AtomicBool::new(false));
        {
            let mut driving = self.shared.driving.lock().unwrap_or_else(PoisonError::into_inner);
            if driving.contains_key(run_id) {
                return Err(Error::Busy(format!("run {run_id} is being driven by this worker")));
            }
            driving.insert(run_id.to_owned(), Arc::clone(&cancel));
        }
        let _driving = Driving { shared: &self.shared, run_id: run_id.to_owned() };
        let driven = (0..MAX_RELOADS).find_map(|_| self.drive_once(run_id, &cancel).transpose());
        driven.unwrap_or_else(|| Err(moving_on(run_id)))
    }

    /// One drive session; `None` when another session moved the run on before this one wrote anything.
    fn drive_once(&self, run_id: &str, cancel: &Arc<AtomicBool>) -> Result<Option<Driven>, Error> {
        let loaded = self.shared.store.load(run_id)?;
        if !loaded.open {
            return self.ended(run_id).map(Some);
        }
        let history = History::from_events(&loaded.events)?;
        let definition = self
            .shared
            .definitions
            .get(&history.start.definition)
            .cloned()
            .ok_or_else(|| Error::State(format!("run {run_id} needs definition {}", history.start.definition)))?;
        let mut session =
            Session::new(Arc::clone(&self.shared), run_id.to_owned(), history, loaded.seq, Arc::clone(cancel));
        let driven = execute(&mut session, definition.as_ref());
        match driven {
            Err(_) if session.stale => Ok(None),
            driven => driven.map(Some),
        }
    }

    fn ended(&self, run_id: &str) -> Result<Driven, Error> {
        let outcome = self.shared.store.result(run_id)?;
        outcome.map(Driven::Ended).ok_or_else(|| Error::State(format!("run {run_id} is open")))
    }

    /// Records the host's result for a deferred request and wakes the run.
    ///
    /// # Errors
    ///
    /// See [`Store::resolve`].
    pub fn resolve(&self, run_id: &str, step: u32, result: &Value) -> Result<Resolution, Error> {
        let resolution = self.shared.store.resolve(run_id, step, result, self.shared.clock.now_ms())?;
        failpoint::hit("resolve.committed");
        self.notify();
        Ok(resolution)
    }

    /// Asks a run to cancel; a run being driven by this worker observes it at once, any other at its next poll.
    ///
    /// # Errors
    ///
    /// See [`Store::cancel`].
    pub fn cancel(&self, run_id: &str) -> Result<bool, Error> {
        let cancelled = self.shared.store.cancel(run_id, self.shared.clock.now_ms())?;
        if let Some(signal) = self.shared.driving.lock().unwrap_or_else(PoisonError::into_inner).get(run_id) {
            signal.store(true, Ordering::SeqCst);
        }
        self.notify();
        Ok(cancelled)
    }

    /// The run's deferred requests still waiting for the host.
    ///
    /// # Errors
    ///
    /// See [`Store::pending`].
    pub fn pending(&self, run_id: &str) -> Result<Vec<PendingRequest>, Error> {
        self.shared.store.pending(run_id)
    }

    /// The run's outcome, or `None` while it is open.
    ///
    /// # Errors
    ///
    /// See [`Store::result`].
    pub fn result(&self, run_id: &str) -> Result<Option<Outcome>, Error> {
        self.shared.store.result(run_id)
    }

    /// The run's complete history.
    ///
    /// # Errors
    ///
    /// See [`Store::history`].
    pub fn history(&self, run_id: &str) -> Result<Vec<Event>, Error> {
        self.shared.store.history(run_id)
    }

    /// Records that the receiver durably committed the run's outcome (see [`Store::acknowledge`]).
    ///
    /// # Errors
    ///
    /// See [`Store::acknowledge`].
    pub fn acknowledge(&self, run_id: &str) -> Result<(), Error> {
        self.shared.store.acknowledge(run_id, self.shared.clock.now_ms())
    }

    /// Deletes a run once the host proves every executor it may still own ended: each recorded obligation, and the
    /// session of a lease that expired without a clean release. The deletion is refused if anything changed while
    /// the proofs ran. The ledger's log is emptied afterwards, also for a run already gone, so the deleted content
    /// survives nowhere in the ledger's files.
    ///
    /// # Errors
    ///
    /// [`Error::State`] while an executor is unproven or the run changed; [`Error::Busy`] while a live session
    /// holds the run; [`Error::Storage`] when the log cannot be emptied.
    pub fn delete(&self, run_id: &str) -> Result<(), Error> {
        self.delete_run(run_id)?;
        self.shared.store.truncate_log()
    }

    fn delete_run(&self, run_id: &str) -> Result<(), Error> {
        let now = self.shared.clock.now_ms();
        let Some(deletion) = self.shared.store.deletion(run_id)? else {
            return Ok(());
        };
        if deletion.lease_expires_ms.is_some_and(|expires| expires > now) {
            return Err(Error::Busy(format!("run {run_id} is held by a live session")));
        }
        let mut owed = deletion.obligations.clone();
        if deletion.lease_expires_ms.is_some() {
            owed.push(Obligation { id: 0, fence: deletion.fence, attempt: None, incarnation: String::new() });
        }
        let history = self.shared.store.load(run_id).ok().and_then(|loaded| History::from_events(&loaded.events).ok());
        let empty = Value::Null;
        let (definition, version, input) = history.as_ref().map_or(("", 0, &empty), |history| {
            (history.start.definition.as_str(), history.start.version, &history.start.input)
        });
        let info = RunInfo { id: run_id, definition, version, input };
        if owed.iter().any(|obligation| self.shared.host.prove_ended(&info, obligation) != Proof::Ended) {
            return Err(Error::State(format!("run {run_id} has executors not yet proven ended")));
        }
        let proven: Vec<i64> = deletion.obligations.iter().map(|obligation| obligation.id).collect();
        self.shared.store.delete_proven(run_id, deletion.fence, &proven, self.shared.clock.now_ms())
    }

    /// Wakes a parked run (for example after the host can supply its authority again).
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be written.
    pub fn wake(&self, run_id: &str) -> Result<(), Error> {
        self.shared.store.wake(run_id, self.shared.clock.now_ms())?;
        self.notify();
        Ok(())
    }

    pub(crate) fn notify(&self) {
        if let Some(scheduler) = self.scheduler.lock().unwrap_or_else(PoisonError::into_inner).as_ref() {
            scheduler.notify();
        }
    }
}

/// The livelock guard: other sessions moved the run on before every one of this call's sessions could write.
#[cfg_attr(coverage_nightly, coverage(off))]
fn moving_on(run_id: &str) -> Error {
    Error::Busy(format!("run {run_id} keeps moving on under other sessions"))
}

/// How many times one drive call starts over when other sessions keep moving the run on.
const MAX_RELOADS: usize = 64;

/// Executes the definition until the run suspends or ends.
fn execute(session: &mut Session, definition: &dyn Definition) -> Result<Driven, Error> {
    if definition.version() != session.history.start.version {
        return Ok(Driven::Ended(session.finish(unrecoverable("definition-changed"), &Value::Null)?));
    }
    loop {
        let mut ctx = Ctx::new(session);
        let executed = catch_unwind(AssertUnwindSafe(|| definition.run(&mut ctx)));
        let (cursor, progress) = (ctx.cursor, std::mem::take(&mut ctx.progress));
        let outcome = match executed {
            Err(_) => unrecoverable("definition-panicked"),
            Ok(Ok(_)) if cursor < session.history.steps.len() => unrecoverable("nondeterminism"),
            // The deadline is absolute: a run that reaches its end after it passed expires, keeping its progress.
            Ok(Ok(Outcome::Done { .. } | Outcome::NeedsPerson { .. } | Outcome::Limited { .. }))
                if session.expired() =>
            {
                Outcome::Expired { progress: progress.clone() }
            }
            Ok(Ok(outcome)) => outcome,
            Ok(Err(Interrupt::Suspend(wait))) => {
                if session.park(wait)? {
                    return Ok(Driven::Suspended(wait));
                }
                continue;
            }
            Ok(Err(Interrupt::Lost(error))) => return Err(error),
            Ok(Err(Interrupt::Cancelled)) => Outcome::Cancelled { progress: progress.clone() },
            Ok(Err(Interrupt::Expired)) => Outcome::Expired { progress: progress.clone() },
            Ok(Err(Interrupt::Limited(kind))) => Outcome::Limited { kind, progress: progress.clone() },
            Ok(Err(Interrupt::Unrecoverable(reason))) => Outcome::Unrecoverable { reason },
        };
        return Ok(Driven::Ended(session.finish(outcome, &progress)?));
    }
}

fn unrecoverable(reason: &str) -> Outcome {
    Outcome::Unrecoverable { reason: reason.to_owned() }
}
