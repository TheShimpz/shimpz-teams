//! Browser tasks as durable runs on Team's ledger (ADR-0125): each Team's runs in its own Holdpoint ledger on the
//! chat-continuation state volume, their Browsers in this process, and the hooks that keep a run from acting without
//! its turn or beside an executor not proven ended.
//!
//! A run acts only while a live chat turn lends it the turn's Brain calls and Stop; after a Team restart it waits
//! (`awaiting-authority`) until the turn is attached again. Its Browser lives only in this process: a restarted run
//! finds none and acquires a new one, and an obligation a previous Team process left is proven once this process
//! proved that no Browser of that process remains. An abandoned attempt of this process is proven ended once its
//! thread let go of the Browser. A session's clean release hands its Browser to the run's next session in this process.

use std::collections::HashMap;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use holdpoint::{
    Authority, Clock, Credentials, Driven, Error, Host, Limits, Obligation, Outcome as Ended,
    Proof, RunInfo, Store, SystemClock, Wait, Worker, WorkerOptions,
};
use strict_json::Value;
use team_browser::cua::CALL_SECONDS;
use team_browser::ingest::present;
use team_browser::run::{
    BrowserTask, Executors, Mode, OpenFailure, Recovered, Recovery, RunCredentials, RunHost,
    Session, Steps,
};
use team_core::audit::AuditSink;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::identifiers::TEAM_ID_RE;
use tokio::runtime::Handle;
use tokio::time::Instant;

use super::drivers::Drivers;
use super::{Held, Running};
use crate::browser::audit::{Actor, Lines, short};
use crate::browser::runtime::{Acquire, BrowserError, BrowserRuntime, Lease, Mode as RunMode};
use crate::browser::view::View;

/// The worker name every Team process prefixes with its own random incarnation.
const OWNER: &str = "team-browser";
/// The longest a drive waits for an earlier session's lease or a retry before it looks again.
const POLL: Duration = Duration::from_secs(1);

/// The turn a run serves: its Team, incarnation, language, Stop, and audit principal.
pub struct TurnContext {
    /// The Team.
    pub team_id: String,
    /// The Team's incarnation (its network's id).
    pub incarnation: String,
    /// The page language.
    pub language: String,
    /// The turn's Stop.
    pub stop: Stop,
    /// The verified principal of the turn's request.
    pub actor: Actor,
}

/// What the live turn lends the run it drives.
struct Lent {
    turn: Arc<TurnContext>,
    credentials: Arc<RunCredentials>,
}

/// The run's Browser in this process.
struct HeldRun {
    lease: Lease,
    token: String,
    view: Arc<View>,
    drivers: Arc<Drivers>,
    session: Option<Session>,
}

#[derive(Default)]
struct Slot {
    lent: Option<Arc<Lent>>,
    held: Option<HeldRun>,
}

/// Where Team's runs keep their Browsers.
pub(crate) struct RunBrowsers {
    runtime: Arc<BrowserRuntime>,
    running: Running,
    audit: Arc<dyn AuditSink>,
    entropy: Arc<dyn Entropy>,
    executors: Arc<Executors>,
    owner: String,
    slots: Mutex<HashMap<String, Slot>>,
}

impl std::fmt::Debug for RunBrowsers {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RunBrowsers")
    }
}

fn open_failure(error: BrowserError) -> OpenFailure {
    OpenFailure {
        code: error.0,
        retryable: error == BrowserError::BUSY,
    }
}

impl RunBrowsers {
    fn slots(&self) -> MutexGuard<'_, HashMap<String, Slot>> {
        self.slots.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn lent(&self, run_id: &str) -> Option<Arc<Lent>> {
        self.slots().get(run_id).and_then(|slot| slot.lent.clone())
    }

    fn take_held(&self, run_id: &str) -> Option<HeldRun> {
        self.slots()
            .get_mut(run_id)
            .and_then(|slot| slot.held.take())
    }

    fn put_held(&self, run_id: &str, held: HeldRun) {
        self.slots().entry(run_id.to_owned()).or_default().held = Some(held);
    }

    fn lease(&self, run_id: &str) -> Option<(Lease, String, Arc<Drivers>)> {
        self.slots().get(run_id).and_then(|slot| {
            slot.held.as_ref().map(|held| {
                (
                    held.lease.clone(),
                    held.token.clone(),
                    Arc::clone(&held.drivers),
                )
            })
        })
    }

    /// A fresh Driver session in place of the run's failed one on its intact Browser.
    async fn reopen(
        &self,
        run_id: &str,
        mut held: HeldRun,
        relaunch: bool,
        deadline: Instant,
    ) -> Result<Recovered, OpenFailure> {
        let failed = held
            .session
            .take()
            .and_then(|session| Arc::try_unwrap(session).ok())
            .map(tokio::sync::Mutex::into_inner);
        let Some(failed) = failed else {
            self.put_held(run_id, held);
            return Err(OpenFailure {
                code: "driver-not-ended",
                retryable: false,
            });
        };
        let replaced = held.drivers.replace(failed, deadline, relaunch).await;
        let outcome = match replaced {
            Ok((session, relaunched)) => {
                held.session = Some(Arc::new(tokio::sync::Mutex::new(session)));
                Ok(Recovered {
                    relaunched,
                    replaced: false,
                })
            }
            Err(error) => Err(OpenFailure {
                code: error.code,
                retryable: false,
            }),
        };
        self.put_held(run_id, held);
        outcome
    }

    /// Removes the run's Browser, proving it absent and ending its egress generation; whether it was proven.
    async fn remove(&self, held: HeldRun) -> bool {
        drop(held.session);
        held.view.end();
        {
            let mut running = self.running.lock().await;
            if running
                .get(&held.lease.team_id)
                .is_some_and(|current| current.token == held.token)
            {
                running.remove(&held.lease.team_id);
            }
        }
        self.runtime.release(&held.lease).await.is_ok()
    }

    /// Acquires a Browser for the run on a fresh egress generation, holds it, and opens its Driver session.
    async fn acquire(&self, lent: &Lent, recovery: &Recovery) -> Result<HeldRun, OpenFailure> {
        let turn = &lent.turn;
        let lease = self
            .runtime
            .acquire(Acquire {
                team_id: &turn.team_id,
                language: &recovery.language,
                mode: RunMode::Agent,
                capsule: None,
                deadline: Some(recovery.deadline),
                stop: Some(&turn.stop),
            })
            .await
            .map_err(open_failure)?;
        let held = match token_hex(self.entropy.as_ref(), 16) {
            Ok(token) => self.hold(turn, lease.clone(), token).await,
            Err(_) => Err(OpenFailure {
                code: "browser-unavailable",
                retryable: false,
            }),
        };
        let held = match held {
            Ok(held) => held,
            Err(failure) => {
                let _ = self.runtime.release(&lease).await;
                return Err(failure);
            }
        };
        let _ = super::audit_task(self.audit.as_ref(), turn, &held.lease, "started").await;
        self.opened(held, recovery).await
    }

    /// The held Browser's first Driver session; a replacement Browser reopens the run's last good page.
    async fn opened(&self, mut held: HeldRun, recovery: &Recovery) -> Result<HeldRun, OpenFailure> {
        let opened = held
            .drivers
            .fresh(recovery.deadline.min(Instant::now() + CALL_SECONDS))
            .await;
        let mut session = match opened {
            Ok(session) => session,
            Err(failure) => {
                let code = match failure {
                    super::drivers::Fresh::Runtime(error) => error.0,
                    super::drivers::Fresh::Driver(error) => error.code,
                };
                self.remove(held).await;
                return Err(OpenFailure {
                    code,
                    retryable: false,
                });
            }
        };
        if let (Some(url), true) = (&recovery.open, recovery.mode != Mode::Start) {
            session.set_deadline(Some(recovery.deadline));
            // The last good page is opened again; a page that does not load is what the agent sees next.
            let _ = session.navigate(url).await;
        }
        held.session = Some(Arc::new(tokio::sync::Mutex::new(session)));
        Ok(held)
    }

    /// Holds `lease` as the Team's running task with its own token and view.
    async fn hold(
        &self,
        turn: &TurnContext,
        lease: Lease,
        token: String,
    ) -> Result<HeldRun, OpenFailure> {
        let lines = Lines::new(
            Arc::clone(&self.audit),
            turn.actor.clone(),
            "browser-view",
            &turn.team_id,
            "",
            format!(":container={}", short(&lease.container_id)),
        );
        let view =
            super::view_of(&self.runtime, &self.entropy, &lease, lines, false).map_err(|_| {
                OpenFailure {
                    code: "browser-unavailable",
                    retryable: false,
                }
            })?;
        self.running.lock().await.insert(
            turn.team_id.clone(),
            Held {
                token: token.clone(),
                lease: lease.clone(),
                view: Arc::clone(&view),
            },
        );
        let drivers = Arc::new(Drivers::new(
            Arc::clone(&self.runtime),
            Arc::clone(&self.running),
            lease.clone(),
            token.clone(),
            turn.stop.clone(),
        ));
        Ok(HeldRun {
            lease,
            token,
            view,
            drivers,
            session: None,
        })
    }
}

impl RunHost for RunBrowsers {
    fn session(&self, run_id: &str) -> Option<Session> {
        self.slots()
            .get(run_id)
            .and_then(|slot| slot.held.as_ref())
            .and_then(|held| held.session.clone())
    }

    fn intact<'a>(&'a self, run_id: &'a str) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            match self.lease(run_id) {
                Some((lease, _, _)) => self.runtime.intact(&lease).await,
                None => false,
            }
        })
    }

    fn open<'a>(
        &'a self,
        run_id: &'a str,
        recovery: Recovery,
    ) -> BoxFuture<'a, Result<Recovered, OpenFailure>> {
        Box::pin(async move {
            let lent = self.lent(run_id).ok_or(OpenFailure {
                code: "browser-lost",
                retryable: false,
            })?;
            let held = self.take_held(run_id);
            if let Some(held) = held {
                let repairable = recovery.mode != Mode::Start
                    && held.session.is_some()
                    && self.runtime.intact(&held.lease).await;
                if repairable {
                    let relaunch = recovery.mode == Mode::Relaunch;
                    return self.reopen(run_id, held, relaunch, recovery.deadline).await;
                }
                // The run's earlier Browser is proven removed before another one is created.
                if !self.remove(held).await {
                    return Err(OpenFailure {
                        code: "browser-lost",
                        retryable: false,
                    });
                }
            }
            let held = self.acquire(&lent, &recovery).await?;
            self.put_held(run_id, held);
            let fresh = recovery.mode != Mode::Start;
            Ok(Recovered {
                relaunched: fresh,
                replaced: fresh,
            })
        })
    }

    fn downloads<'a>(&'a self, run_id: &'a str, deadline: Instant) -> BoxFuture<'a, Option<Value>> {
        Box::pin(async move {
            let (_, _, drivers) = self.lease(run_id)?;
            let listing = drivers.listing(deadline).await?;
            present(&listing).ok()
        })
    }
}

/// The hooks of one Team's worker.
struct Hooks(Arc<RunBrowsers>);

impl Host for Hooks {
    fn authorize(&self, run: &RunInfo<'_>) -> Authority {
        self.0.lent(run.id).map_or(Authority::Wait, |lent| {
            Authority::Granted(Credentials::new(Arc::clone(&lent.credentials)))
        })
    }

    fn prove_ended(&self, run: &RunInfo<'_>, obligation: &Obligation) -> Proof {
        let ours = obligation
            .incarnation
            .starts_with(&format!("{}:", self.0.owner));
        let ended = if ours {
            self.0.executors.idle(run.id)
        } else {
            // A previous Team process's executors ran in its Browser, which this process proved removed.
            self.0.runtime.previous_gone()
        };
        if ended { Proof::Ended } else { Proof::NotYet }
    }
}

/// Every Team's runs.
pub(crate) struct Runs {
    root: PathBuf,
    browsers: Arc<RunBrowsers>,
    runtime: Handle,
    workers: Mutex<HashMap<String, Arc<Worker>>>,
}

impl std::fmt::Debug for Runs {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Runs")
            .field("root", &self.root)
            .finish_non_exhaustive()
    }
}

/// What the runs are built over.
pub(crate) struct RunsParts {
    /// The directory every Team's ledger lives under.
    pub root: PathBuf,
    /// The Space's Browser runtime.
    pub runtime: Arc<BrowserRuntime>,
    /// Each Team's held Browser.
    pub running: Running,
    /// The audit log.
    pub audit: Arc<dyn AuditSink>,
    /// The random source.
    pub entropy: Arc<dyn Entropy>,
}

fn private_dir(path: &Path) -> std::io::Result<()> {
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
}

/// Why a run gave no outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Unsettled(pub &'static str);

impl Runs {
    /// The runs over `parts`, their attempts driving Team's current runtime.
    ///
    /// # Errors
    /// [`Unsettled`] when no random incarnation could be drawn.
    pub fn new(parts: RunsParts) -> Result<Self, Unsettled> {
        let incarnation =
            token_hex(parts.entropy.as_ref(), 8).map_err(|_| Unsettled("browser-unavailable"))?;
        Ok(Self {
            root: parts.root,
            browsers: Arc::new(RunBrowsers {
                runtime: parts.runtime,
                running: parts.running,
                audit: parts.audit,
                entropy: parts.entropy,
                executors: Arc::new(Executors::default()),
                owner: format!("{OWNER}-{incarnation}"),
                slots: Mutex::new(HashMap::new()),
            }),
            runtime: Handle::current(),
            workers: Mutex::new(HashMap::new()),
        })
    }

    fn workers(&self) -> MutexGuard<'_, HashMap<String, Arc<Worker>>> {
        self.workers.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The Team's worker over its own ledger, opened on first use.
    ///
    /// # Errors
    /// [`Unsettled`] when the Team id is not one or its ledger cannot be opened.
    pub fn worker(&self, team_id: &str) -> Result<Arc<Worker>, Unsettled> {
        if !TEAM_ID_RE.is_full(team_id) {
            return Err(Unsettled("browser-unavailable"));
        }
        if let Some(worker) = self.workers().get(team_id) {
            return Ok(Arc::clone(worker));
        }
        let directory = self.root.join(team_id);
        private_dir(&directory).map_err(|_| Unsettled("browser-unavailable"))?;
        let store = Store::open(&directory.join("ledger.sqlite3"), Limits::default())
            .map_err(|_| Unsettled("browser-unavailable"))?;
        let steps = Steps {
            runtime: self.runtime.clone(),
            host: Arc::clone(&self.browsers) as Arc<dyn RunHost>,
            executors: Arc::clone(&self.browsers.executors),
            now: Arc::new(|| SystemClock.now_ms()),
        };
        let builder = Worker::builder(
            Arc::new(store),
            &self.browsers.owner,
            Hooks(Arc::clone(&self.browsers)),
        )
        .definition(BrowserTask)
        .options(WorkerOptions {
            inline_wait_ms: 120_000,
            ..WorkerOptions::default()
        });
        let worker = Arc::new(steps.register(builder).build());
        Ok(Arc::clone(
            self.workers().entry(team_id.to_owned()).or_insert(worker),
        ))
    }

    /// Lends `run_id` its turn while the turn drives it.
    pub fn lend(&self, run_id: &str, turn: Arc<TurnContext>, credentials: Arc<RunCredentials>) {
        self.browsers
            .slots()
            .entry(run_id.to_owned())
            .or_default()
            .lent = Some(Arc::new(Lent { turn, credentials }));
    }

    /// Takes back what the turn lent `run_id`; the run cannot act again until a turn lends it anew.
    pub fn unlend(&self, run_id: &str) {
        let mut slots = self.browsers.slots();
        if let Some(slot) = slots.get_mut(run_id) {
            slot.lent = None;
            if slot.held.is_none() {
                slots.remove(run_id);
            }
        }
    }

    /// Drives `run_id` until it ends, waiting out retries, earlier executors, and a crashed session's lease.
    ///
    /// # Errors
    /// [`Unsettled`] when the run waits for authority, the ledger failed, or `deadline` passed.
    pub async fn drive(
        &self,
        worker: &Arc<Worker>,
        run_id: &str,
        deadline: Instant,
    ) -> Result<Ended, Unsettled> {
        loop {
            if Instant::now() >= deadline {
                return Err(Unsettled("browser-lost"));
            }
            let (driving, id) = (Arc::clone(worker), run_id.to_owned());
            let driven = tokio::task::spawn_blocking(move || driving.drive(&id))
                .await
                .map_err(|_| Unsettled("browser-lost"))?;
            let wait = match driven {
                Ok(Driven::Ended(ended)) => return Ok(ended),
                Ok(Driven::Suspended(Wait::Until(at) | Wait::Executor(at))) => {
                    let left = at.saturating_sub(SystemClock.now_ms());
                    Duration::from_millis(u64::try_from(left).unwrap_or(0)).min(POLL)
                }
                Ok(Driven::Suspended(Wait::Shutdown)) | Err(Error::Busy(_)) => POLL,
                Ok(Driven::Suspended(Wait::Authority | Wait::Deferred)) => {
                    return Err(Unsettled("awaiting-authority"));
                }
                Err(_) => return Err(Unsettled("browser-lost")),
            };
            tokio::time::sleep(wait).await;
        }
    }

    /// Removes the run's Browser at once, for Stop: its view ends and no call of the run reaches the page again.
    pub async fn interrupt(&self, run_id: &str) {
        if let Some((lease, _, _)) = self.browsers.lease(run_id) {
            let _ = self.browsers.runtime.kill(&lease).await;
        }
    }

    /// The run's held Browser and its token, while it holds one.
    #[must_use]
    pub fn held(&self, run_id: &str) -> Option<(Lease, String)> {
        self.browsers
            .lease(run_id)
            .map(|(lease, token, _)| (lease, token))
    }

    /// Releases the run's Browser: removed, proven absent, its egress generation ended; whether it was proven, and
    /// the lease it had.
    pub async fn release(&self, run_id: &str) -> (bool, Option<Lease>) {
        let Some(held) = self.browsers.take_held(run_id) else {
            return (true, None);
        };
        let lease = held.lease.clone();
        (self.browsers.remove(held).await, Some(lease))
    }

    /// Forgets a delivered run: its content goes and a tombstone keeps its id, so it is never run again.
    pub fn acknowledge(&self, team_id: &str, run_id: &str) {
        if let Ok(worker) = self.worker(team_id) {
            let _ = worker.acknowledge(run_id);
        }
    }

    /// Cancels a run nobody will drive again and deletes it once its executors are proven ended.
    pub fn discard(&self, team_id: &str, run_id: &str) {
        let Ok(worker) = self.worker(team_id) else {
            return;
        };
        let _ = worker.cancel(run_id);
        let run_id = run_id.to_owned();
        drop(tokio::task::spawn_blocking(move || worker.delete(&run_id)));
    }

    /// Deletes a Team's ledger with every run in it, its Browser already removed by the teardown.
    ///
    /// # Errors
    /// [`Unsettled`] when the ledger could not be removed.
    pub fn forget_team(&self, team_id: &str) -> Result<(), Unsettled> {
        if !TEAM_ID_RE.is_full(team_id) {
            return Ok(());
        }
        self.workers().remove(team_id);
        self.browsers.slots().retain(|_, slot| {
            slot.lent
                .as_ref()
                .is_none_or(|lent| lent.turn.team_id != team_id)
        });
        remove_all(&self.root.join(team_id))
    }

    /// Deletes every Team's ledger.
    ///
    /// # Errors
    /// [`Unsettled`] when a ledger could not be removed.
    pub fn forget_all(&self) -> Result<(), Unsettled> {
        self.workers().clear();
        self.browsers.slots().clear();
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err(Unsettled("state-unavailable")),
        };
        for entry in entries {
            let entry = entry.map_err(|_| Unsettled("state-unavailable"))?;
            remove_all(&entry.path())?;
        }
        Ok(())
    }
}

fn remove_all(path: &Path) -> Result<(), Unsettled> {
    match std::fs::remove_dir_all(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(Unsettled("state-unavailable")),
    }
}
