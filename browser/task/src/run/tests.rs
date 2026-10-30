//! The run's behaviour and fault tests over a real ledger: a scripted Brain, a fake Browser behind the Cua Driver
//! that logs every page command to a file, and Holdpoint's manual clock.

mod behaviour;
mod faults;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use holdpoint::{
    Authority, Clock, Credentials, Driven, Host, Limits, ManualClock, Obligation, Proof, RunInfo,
    RunOptions, Store, Wait, Worker, WorkerOptions,
};
use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::time::Instant;

use super::handlers::{Executors, Mode, OpenFailure, Recovered, Recovery, RunHost, Session, Steps};
use super::{
    Answered, BrowserCall, BrowserTask, CallFailure, MAX_SPEND, Outcome, RunCredentials, TaskBrain,
    TaskInput, Usage, outcome_of,
};
use crate::cua::CuaSession;
use crate::testing::{Browser, Driver, json};

/// The fixed start of every test run's manual clock.
pub(super) const T0: i64 = 1_800_000_000_000;

/// One scripted Brain answer for a call and its request, at the manual clock's time.
pub(super) type Script =
    Box<dyn Fn(BrowserCall, &Value, i64) -> Result<Value, CallFailure> + Send + Sync>;

/// A Brain answering from a script, recording every call.
pub(super) struct FakeBrain {
    script: Script,
    clock: ManualClock,
    calls: Mutex<Vec<(BrowserCall, Value)>>,
    /// Told the number of recorded steps before each step is answered: where a test injects its fault.
    pub(super) on_step: Mutex<Option<Hook>>,
}

/// A fault a test injects before the Brain answers the step with this many recorded steps.
pub(super) type Hook = Box<dyn Fn(usize) + Send + Sync>;

impl TaskBrain for FakeBrain {
    fn call(
        &self,
        call: BrowserCall,
        request: Value,
        _deadline: Instant,
    ) -> BoxFuture<'_, Result<Answered, CallFailure>> {
        if call == BrowserCall::Step
            && let Some(hook) = self
                .on_step
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .as_ref()
        {
            hook(recorded(&request));
        }
        let answer = (self.script)(call, &request, self.clock.now_ms());
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((call, request));
        Box::pin(async move {
            Ok(Answered {
                value: answer?,
                usage: Usage {
                    model_calls: 1,
                    provider_requests: 1,
                    input_tokens: 1_000,
                    output_tokens: 100,
                    ..Usage::default()
                },
                model: "luna-test".to_owned(),
            })
        })
    }
}

impl FakeBrain {
    pub(super) fn calls(&self) -> Vec<(BrowserCall, Value)> {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

/// The fake Browser host: one fake Browser at a time, its Driver sessions, and whether it is intact.
pub(super) struct FakeHost {
    pub(super) browser: Mutex<Option<Driver>>,
    sessions: Mutex<HashMap<String, Session>>,
    pub(super) intact: AtomicBool,
    pub(super) opens: Mutex<Vec<(Mode, Recovered)>>,
    log: PathBuf,
    setup: fn(&mut Browser),
    /// Applied once, to the next Browser the host creates.
    pub(super) next: Mutex<Option<fn(&mut Browser)>>,
}

impl FakeHost {
    fn new(log: PathBuf, setup: fn(&mut Browser)) -> Arc<Self> {
        Arc::new(Self {
            browser: Mutex::new(None),
            sessions: Mutex::new(HashMap::new()),
            intact: AtomicBool::new(true),
            opens: Mutex::new(Vec::new()),
            log,
            setup,
            next: Mutex::new(None),
        })
    }

    fn fresh(&self, url: Option<&str>) -> Driver {
        let mut browser = Browser {
            log: Some(self.log.clone()),
            ..Browser::default()
        };
        (self.setup)(&mut browser);
        if let Some(next) = self
            .next
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
        {
            next(&mut browser);
        }
        if let Some(url) = url {
            browser.url = url.to_owned();
        }
        Driver::new(browser)
    }

    pub(super) fn driver(&self) -> Option<Driver> {
        self.browser
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// The Browser is gone, as when its container was removed.
    pub(super) fn kill(&self) {
        *self.browser.lock().unwrap_or_else(PoisonError::into_inner) = None;
    }
}

async fn session(driver: &Driver) -> Session {
    let mut session = CuaSession::new(Box::new(driver.clone()));
    session.open().await.expect("the fake Driver opens");
    Arc::new(tokio::sync::Mutex::new(session))
}

impl RunHost for FakeHost {
    fn session(&self, run_id: &str) -> Option<Session> {
        self.driver()?;
        self.sessions
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(run_id)
            .cloned()
    }

    fn intact<'a>(&'a self, _run_id: &'a str) -> BoxFuture<'a, bool> {
        Box::pin(async move { self.intact.load(Ordering::SeqCst) && self.driver().is_some() })
    }

    fn open<'a>(
        &'a self,
        run_id: &'a str,
        recovery: Recovery,
    ) -> BoxFuture<'a, Result<Recovered, OpenFailure>> {
        Box::pin(async move {
            let usable = self.intact.load(Ordering::SeqCst)
                && self.driver().is_some()
                && self.session(run_id).is_some();
            let recovered = if recovery.mode == Mode::Reopen && usable {
                Recovered::default()
            } else if recovery.mode == Mode::Relaunch && usable {
                // Chromium started again on the same Browser: its page state is gone, its page is the same.
                let url = self.driver().map(|driver| driver.browser().url.clone());
                let driver = self.fresh(url.as_deref());
                *self.browser.lock().unwrap_or_else(PoisonError::into_inner) = Some(driver);
                Recovered {
                    relaunched: true,
                    replaced: false,
                }
            } else {
                let driver = self.fresh(recovery.open.as_deref());
                *self.browser.lock().unwrap_or_else(PoisonError::into_inner) = Some(driver);
                self.intact.store(true, Ordering::SeqCst);
                Recovered {
                    relaunched: recovery.mode != Mode::Start,
                    replaced: recovery.mode != Mode::Start,
                }
            };
            let driver = self.driver().ok_or(OpenFailure {
                code: "browser-lost",
                retryable: false,
            })?;
            let opened = session(&driver).await;
            self.sessions
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .insert(run_id.to_owned(), opened);
            self.opens
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push((recovery.mode, recovered));
            Ok(recovered)
        })
    }

    fn downloads<'a>(
        &'a self,
        _run_id: &'a str,
        _deadline: Instant,
    ) -> BoxFuture<'a, Option<Value>> {
        Box::pin(async { None })
    }
}

/// The hooks: authority while the test grants it, and an executor proven ended once no attempt of its run runs.
pub(super) struct Hooks {
    executors: Arc<Executors>,
    pub(super) credentials: Mutex<Option<Arc<RunCredentials>>>,
    pub(super) revoked: AtomicBool,
}

struct Shared(Arc<Hooks>);

impl Host for Shared {
    fn authorize(&self, _run: &RunInfo<'_>) -> Authority {
        if self.0.revoked.load(Ordering::SeqCst) {
            return Authority::Revoked;
        }
        self.0
            .credentials
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .map_or(Authority::Wait, |credentials| {
                Authority::Granted(Credentials::new(credentials))
            })
    }

    fn prove_ended(&self, run: &RunInfo<'_>, _obligation: &Obligation) -> Proof {
        if self.0.executors.idle(run.id) {
            Proof::Ended
        } else {
            Proof::NotYet
        }
    }
}

/// One process's view of a task: its ledger, clock, runtime, Browser host, Brain, and worker.
pub(super) struct World {
    pub(super) clock: ManualClock,
    pub(super) host: Arc<FakeHost>,
    pub(super) brain: Arc<FakeBrain>,
    pub(super) hooks: Arc<Hooks>,
    pub(super) worker: Arc<Worker>,
    pub(super) log: PathBuf,
    // Dropped last: the worker's attempts drive the runtime.
    _runtime: tokio::runtime::Runtime,
}

impl World {
    /// The world over the ledger in `dir`, the fake Browser `setup` makes, and the Brain `script`.
    pub(super) fn open(dir: &Path, setup: fn(&mut Browser), script: Script) -> Self {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .expect("runtime");
        let clock = ManualClock::at(T0);
        let log = dir.join("effects.log");
        let host = FakeHost::new(log.clone(), setup);
        let brain = Arc::new(FakeBrain {
            script,
            clock: clock.clone(),
            calls: Mutex::new(Vec::new()),
            on_step: Mutex::new(None),
        });
        Self::with(dir, (clock, log, runtime), host, brain)
    }

    fn with(
        dir: &Path,
        (clock, log, runtime): (ManualClock, PathBuf, tokio::runtime::Runtime),
        host: Arc<FakeHost>,
        brain: Arc<FakeBrain>,
    ) -> Self {
        let executors = Arc::new(Executors::default());
        let hooks = Arc::new(Hooks {
            executors: Arc::clone(&executors),
            credentials: Mutex::new(Some(Arc::new(RunCredentials {
                brain: Arc::clone(&brain) as Arc<dyn TaskBrain>,
                price: Arc::new(|_| (25, 200)),
            }))),
            revoked: AtomicBool::new(false),
        });
        let store =
            Arc::new(Store::open(&dir.join("ledger.sqlite3"), Limits::default()).expect("ledger"));
        let reading = clock.clone();
        let steps = Steps {
            runtime: runtime.handle().clone(),
            host: Arc::clone(&host) as Arc<dyn RunHost>,
            executors,
            now: Arc::new(move || reading.now_ms()),
        };
        let builder = Worker::builder(store, "team-test", Shared(Arc::clone(&hooks)))
            .definition(BrowserTask)
            .clock(clock.clone())
            .options(WorkerOptions {
                inline_wait_ms: 120_000,
                lease_ms: 2_000,
                ..WorkerOptions::default()
            });
        let worker = Arc::new(steps.register(builder).build());
        Self {
            clock,
            host,
            brain,
            hooks,
            worker,
            log,
            _runtime: runtime,
        }
    }

    pub(super) fn input() -> TaskInput {
        TaskInput {
            team_id: "team-a".to_owned(),
            interrupt_id: "browser-1".to_owned(),
            objective: "Find the open jobs.".to_owned(),
            language: "en".to_owned(),
            started_ms: T0,
            dearest: (25, 200),
        }
    }

    /// Submits the test task, idempotently.
    pub(super) fn submit(&self) -> String {
        let input = Self::input();
        let run_id = input.run_id();
        self.worker
            .submit(
                super::DEFINITION,
                &run_id,
                &input.to_json(),
                &RunOptions {
                    deadline_ms: Some(input.deadline_ms()),
                    spend_ceiling: Some(MAX_SPEND),
                },
            )
            .expect("submitted");
        run_id
    }

    /// Drives the run until it ends or waits for authority, serving executor waits on the manual clock.
    pub(super) fn drive(&self, run_id: &str) -> Driven {
        for _ in 0..200 {
            match self.worker.drive(run_id) {
                Ok(Driven::Suspended(Wait::Until(at) | Wait::Executor(at))) => {
                    let left = at - self.clock.now_ms();
                    self.clock.advance(left.max(1));
                }
                Ok(Driven::Suspended(Wait::Shutdown)) => {}
                Ok(driven) => return driven,
                // A crashed process's lease is waited out.
                Err(holdpoint::Error::Busy(_)) => self.clock.advance(1_000),
                Err(error) => panic!("drive failed: {error}"),
            }
        }
        panic!("the run did not settle");
    }

    /// The task's outcome once the run ended.
    pub(super) fn outcome(&self, run_id: &str) -> Outcome {
        match self.drive(run_id) {
            Driven::Ended(ended) => outcome_of(&ended),
            Driven::Suspended(wait) => panic!("the run waits: {wait:?}"),
        }
    }

    /// Injects `hook` before each step the Brain answers.
    pub(super) fn on_step(&self, hook: impl Fn(usize) + Send + Sync + 'static) {
        *self
            .brain
            .on_step
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(Box::new(hook));
    }

    /// How often `effect` reached a page.
    pub(super) fn count(&self, effect: &str) -> usize {
        self.effects().iter().filter(|line| *line == effect).count()
    }

    /// Every page command that reached any fake Browser, in order, across processes.
    pub(super) fn effects(&self) -> Vec<String> {
        std::fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect()
    }
}

/// The value of a JSON literal.
pub(super) fn value(text: &str) -> Value {
    json(text)
}

/// How many steps the request's thread already holds.
pub(super) fn recorded(request: &Value) -> usize {
    match request.get("thread").and_then(|thread| thread.get("steps")) {
        Some(Value::Array(steps)) => steps.len(),
        _ => 0,
    }
}
