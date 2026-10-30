//! The run's steps as Team carries them out: the Brain model and the Browser activities, each attempt on Holdpoint's
//! own thread, driving Team's async Browser and Brain clients on Team's runtime.
//!
//! Every activity counts itself in [`Executors`] for as long as it runs, so the host proves an abandoned attempt ended
//! only once its thread let go of the Browser. A Driver failure comes back as a result naming its closed code (`lost`),
//! never as a Holdpoint failure, so the run keeps today's causes; only a crash or an abandoned attempt leaves a page
//! action `uncertain`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use holdpoint::{
    Activity, ActivityCall, ActivityOptions, ActivityReply, Attempt, Containment, Failure, Model,
    ModelCall, ModelOptions, ModelReply, RetryPolicy, Timeouts,
};
use serde_json::json;
use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::runtime::Handle;
use tokio::time::Instant;

use super::failpoint;
use super::wire::{self, int, string};
use crate::brain::{BrowserCall, CallFailure, TaskBrain, Usage};
use crate::cua::{CuaError, CuaSession};
use crate::read::{self, ReadFailure, Reading};

/// The model's name in the ledger.
pub const BRAIN: &str = "brain";
/// The activity that acquires, replaces, or reopens the run's Browser.
pub const OPEN: &str = "browser.open";
/// The activity that observes the page and lists the downloads.
pub const OBSERVE: &str = "browser.observe";
/// The activity that carries out one page action.
pub const ACT: &str = "browser.act";
/// The activity that opens a step's links in turn.
pub const READ: &str = "browser.read";

/// A model's input and output price in cents per million tokens; `None` names the provider's dearest model.
pub type Price = Arc<dyn Fn(Option<&str>) -> (u64, u64) + Send + Sync>;
/// The wall clock, in milliseconds.
pub type Now = Arc<dyn Fn() -> i64 + Send + Sync>;
/// One run's Driver session, held by one activity at a time.
pub type Session = Arc<tokio::sync::Mutex<CuaSession>>;

/// What the live chat turn lends the run it drives: its Brain calls under the turn's key and Stop, and the provider's
/// prices. Kept in memory only.
pub struct RunCredentials {
    /// The turn's Brain calls.
    pub brain: Arc<dyn TaskBrain>,
    /// The provider's model prices.
    pub price: Price,
}

impl std::fmt::Debug for RunCredentials {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RunCredentials(redacted)")
    }
}

/// How `browser.open` treats the run's Browser.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// The task's first Browser.
    Start,
    /// A fresh Driver session on the same Browser, or a new Browser when it is gone.
    Reopen,
    /// Chromium started again, or a new Browser when it is gone.
    Relaunch,
}

impl Mode {
    /// The closed word.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Reopen => "reopen",
            Self::Relaunch => "relaunch",
        }
    }

    fn parse(text: &str) -> Option<Self> {
        [Self::Start, Self::Reopen, Self::Relaunch]
            .into_iter()
            .find(|mode| mode.as_str() == text)
    }
}

/// What `browser.open` is asked.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Recovery {
    /// The Team.
    pub team_id: String,
    /// The page language.
    pub language: String,
    /// How to treat the Browser.
    pub mode: Mode,
    /// The last good page, opened on a new Browser.
    pub open: Option<String>,
    /// The attempt's deadline.
    pub deadline: Instant,
}

/// What `browser.open` did.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Recovered {
    /// Chromium was started again.
    pub relaunched: bool,
    /// The whole Browser was replaced.
    pub replaced: bool,
}

/// Why `browser.open` did not open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OpenFailure {
    /// The closed code.
    pub code: &'static str,
    /// Whether a later attempt may succeed (the Space's Browser is held by another task).
    pub retryable: bool,
}

/// Where a run's Browser lives in Team.
pub trait RunHost: Send + Sync {
    /// The run's current Driver session, or `None` when the run holds no Browser in this process.
    fn session(&self, run_id: &str) -> Option<Session>;

    /// Whether the run's Browser still runs on the egress process it started on.
    fn intact<'a>(&'a self, run_id: &'a str) -> BoxFuture<'a, bool>;

    /// Acquires the run's first Browser, or repairs it: a fresh Driver session on the intact Browser, Chromium started
    /// again, or a new Browser on a fresh egress generation that reopens `recovery.open`; an earlier Browser of the run
    /// is proven removed before a new one is created.
    ///
    /// # Errors
    /// [`OpenFailure`] when no usable Browser could be opened.
    fn open<'a>(
        &'a self,
        run_id: &'a str,
        recovery: Recovery,
    ) -> BoxFuture<'a, Result<Recovered, OpenFailure>>;

    /// The run's downloads so far in the protocol's form, read by `deadline`; `None` when unreadable.
    fn downloads<'a>(&'a self, run_id: &'a str, deadline: Instant) -> BoxFuture<'a, Option<Value>>;
}

/// The activity attempts running per run, so an abandoned one is proven ended only once it let go.
#[derive(Debug, Default)]
pub struct Executors(Mutex<HashMap<String, usize>>);

/// One running attempt, counted until dropped.
pub struct Running {
    executors: Arc<Executors>,
    run_id: String,
}

impl Executors {
    fn enter(self: &Arc<Self>, run_id: &str) -> Running {
        *self
            .0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .entry(run_id.to_owned())
            .or_default() += 1;
        Running {
            executors: Arc::clone(self),
            run_id: run_id.to_owned(),
        }
    }

    /// Whether no attempt of `run_id` runs now.
    #[must_use]
    pub fn idle(&self, run_id: &str) -> bool {
        !self
            .0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .contains_key(run_id)
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        let mut running = self
            .executors
            .0
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if let Some(count) = running.get_mut(&self.run_id) {
            *count -= 1;
            if *count == 0 {
                running.remove(&self.run_id);
            }
        }
    }
}

/// What every handler of a worker shares: Team's runtime, its Browser host, the running attempts, and the clock.
#[derive(Clone)]
pub struct Steps {
    /// Team's runtime, which the attempts' threads drive the async clients on.
    pub runtime: Handle,
    /// The Browser host.
    pub host: Arc<dyn RunHost>,
    /// The running attempts.
    pub executors: Arc<Executors>,
    /// The wall clock.
    pub now: Now,
}

impl std::fmt::Debug for Steps {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Steps")
    }
}

impl Steps {
    /// The attempt's deadline on Team's monotonic clock, at most `bound` from now.
    fn deadline(&self, attempt: &Attempt, bound: Duration) -> Instant {
        let now = (self.now)();
        let left = attempt
            .deadline_ms
            .map_or(bound, |deadline| {
                Duration::from_millis(u64::try_from(deadline.saturating_sub(now)).unwrap_or(0))
            })
            .min(bound);
        Instant::now() + left
    }

    fn lost(&self, code: &str, acted: bool) -> ActivityReply {
        ActivityReply::of(json!({
            "lost": code,
            "acted": acted,
            "now_ms": (self.now)(),
        }))
    }

    /// Registers the model and the activities on `builder`.
    #[must_use]
    pub fn register(&self, builder: holdpoint::WorkerBuilder) -> holdpoint::WorkerBuilder {
        let never = |seconds: u64| ActivityOptions {
            idempotent: false,
            containment: Containment::External,
            retry: RetryPolicy::never(),
            timeouts: Timeouts {
                start_to_close_ms: Some(seconds * 1000),
                ..Timeouts::default()
            },
        };
        let open = ActivityOptions {
            idempotent: true,
            retry: RetryPolicy {
                initial_ms: 2_000,
                backoff_percent: 200,
                max_interval_ms: 10_000,
                max_attempts: Some(30),
                non_retryable: vec![],
            },
            ..never(240)
        };
        let observe = ActivityOptions {
            idempotent: true,
            ..never(120)
        };
        let model = ModelOptions {
            retry: RetryPolicy {
                initial_ms: 2_000,
                backoff_percent: 200,
                max_interval_ms: 15_000,
                max_attempts: Some(8),
                non_retryable: vec![],
            },
            timeouts: Timeouts::default(),
        };
        builder
            .model(BRAIN, model, BrainModel(self.clone()))
            .activity(OPEN, open, Open(self.clone()))
            .activity(OBSERVE, observe, Observe(self.clone()))
            .activity(ACT, never(120), Act(self.clone()))
            .activity(READ, never(600), Read(self.clone()))
    }
}

/// The Brain's Browser operations under the turn's key.
struct BrainModel(Steps);

/// A call's reported usage at its model's price, and every attempt that reported none at its worst case.
#[must_use]
pub fn settled(usage: &Usage, attempt: u64, price: (u64, u64)) -> u64 {
    let reported = usage
        .model_calls
        .saturating_sub(usage.failed_calls)
        .saturating_sub(usage.unreported_calls);
    let unaccounted = usage.provider_requests.saturating_sub(reported);
    usage
        .input_tokens
        .saturating_mul(price.0)
        .saturating_add(usage.output_tokens.saturating_mul(price.1))
        .saturating_add(unaccounted.saturating_mul(attempt))
}

/// A model step's request: its operation, body, time bound, and one attempt's worst cost.
fn brain_request(request: &serde_json::Value) -> Result<(BrowserCall, Value, i64, u64), Failure> {
    let (Some(operation), Some(body), Some(until_ms), Some(attempt_cost)) = (
        string(request, "call").and_then(BrowserCall::parse),
        wire::member(request, "body"),
        int(request, "until_ms"),
        request.get("attempt").and_then(serde_json::Value::as_u64),
    ) else {
        return Err(Failure::permanent("request-invalid", "the request is malformed").with_ended());
    };
    Ok((operation, body, until_ms, attempt_cost))
}

impl Model for BrainModel {
    fn reserve(&self, request: &serde_json::Value) -> Option<u64> {
        request.get("reserve").and_then(serde_json::Value::as_u64)
    }

    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        let steps = &self.0;
        let credentials = call
            .credentials
            .get::<Arc<RunCredentials>>()
            .ok_or_else(|| {
                Failure::permanent("no-authority", "the run holds no turn").with_ended()
            })?;
        let (operation, body, until_ms, attempt_cost) = brain_request(call.request)?;
        let now = (steps.now)();
        let until = call
            .attempt
            .deadline_ms
            .map_or(until_ms, |deadline| deadline.min(until_ms));
        if until <= now || call.attempt.check().is_err() {
            return Err(Failure::permanent("cutoff", "no time is left for the call").with_ended());
        }
        let deadline =
            Instant::now() + Duration::from_millis(u64::try_from(until - now).unwrap_or(0));
        failpoint("model.before");
        let answered = steps
            .runtime
            .block_on(credentials.brain.call(operation, body, deadline));
        match answered {
            Ok(answered) => {
                failpoint("model.answered");
                let price = (credentials.price)(Some(&answered.model));
                let cost = settled(&answered.usage, attempt_cost, price);
                Ok(ModelReply {
                    response: json!({
                        "value": wire::text(&answered.value),
                        "model": answered.model,
                        "price": [price.0, price.1],
                        "cost": cost,
                        "now_ms": (steps.now)(),
                    }),
                    cost,
                })
            }
            Err(CallFailure::Unavailable) => Err(Failure::retryable(
                "brain-unavailable",
                "the Brain or its provider did not answer",
            )),
            Err(CallFailure::Refused) => Err(Failure::permanent(
                "brain-refused",
                "the Brain answered without a step",
            )),
        }
    }
}

/// `browser.open`.
struct Open(Steps);

impl Activity for Open {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let steps = &self.0;
        let input = call.input;
        let (Some(team_id), Some(language), Some(mode)) = (
            string(input, "team_id"),
            string(input, "language"),
            string(input, "mode").and_then(Mode::parse),
        ) else {
            return Err(Failure::permanent("input-invalid", "the input is malformed").with_ended());
        };
        let open = string(input, "open").map(str::to_owned);
        call.attempt.check().map_err(|_| {
            Failure::permanent("abandoned", "the attempt was abandoned").with_ended()
        })?;
        let _running = steps.executors.enter(&call.attempt.run_id);
        let recovery = Recovery {
            team_id: team_id.to_owned(),
            language: language.to_owned(),
            mode,
            open,
            deadline: steps.deadline(call.attempt, Duration::from_secs(240)),
        };
        let opened = steps
            .runtime
            .block_on(steps.host.open(&call.attempt.run_id, recovery));
        match opened {
            Ok(recovered) => {
                failpoint("open.after");
                Ok(ActivityReply::of(json!({
                "relaunched": recovered.relaunched,
                "replaced": recovered.replaced,
                "now_ms": (steps.now)(),
                })))
            }
            Err(failure) if failure.retryable => Err(Failure::retryable(
                failure.code,
                "the Space's Browser is held by another task",
            )
            .with_ended()),
            Err(failure) => Err(Failure::permanent(
                failure.code,
                "no Browser could be opened",
            )),
        }
    }
}

/// `browser.observe`: the page, a screenshot when asked, and the downloads; a read only.
struct Observe(Steps);

impl Activity for Observe {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let steps = &self.0;
        let screenshot = call
            .input
            .get("screenshot")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let run_id = call.attempt.run_id.as_str();
        let _running = steps.executors.enter(run_id);
        failpoint("observe.before");
        let deadline = steps.deadline(call.attempt, Duration::from_secs(120));
        let observed = steps.runtime.block_on(async {
            let session = steps.host.session(run_id).ok_or("browser-lost")?;
            if !steps.host.intact(run_id).await {
                return Err("not-intact");
            }
            let mut session = session.lock().await;
            if call.attempt.check().is_err() {
                return Err("abandoned");
            }
            session.set_deadline(Some(deadline));
            let observation = session
                .observe(screenshot)
                .await
                .map_err(|error| error.code)?;
            let downloads = steps.host.downloads(run_id, deadline).await;
            Ok((observation, downloads))
        });
        let (mut observation, downloads) = match observed {
            Ok(observed) => observed,
            Err(code) => return Ok(steps.lost(code, false)),
        };
        failpoint("observe.after");
        let image = observation.screenshot.take();
        Ok(ActivityReply {
            result: json!({
                "observation": wire::text(&wire::observation(&observation)),
                "screenshot": image.is_some(),
                "downloads": downloads.as_ref().map(wire::text),
                "now_ms": (steps.now)(),
            }),
            blobs: image.into_iter().collect(),
        })
    }
}

/// `browser.act`: one navigate, click, type, or scroll; never repeated.
struct Act(Steps);

async fn act(session: &mut CuaSession, input: &serde_json::Value) -> Result<(), CuaError> {
    let field = |name: &str| string(input, name).unwrap_or_default();
    match field("kind") {
        "navigate" => session.navigate(field("url")).await,
        "click" => session.click(field("ref")).await,
        "type" => session.type_text(field("ref"), field("text")).await,
        "scroll" => session.scroll(field("direction")).await,
        _ => Err(CuaError::fatal("step-invalid")),
    }
}

impl Activity for Act {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let steps = &self.0;
        let run_id = call.attempt.run_id.as_str();
        let _running = steps.executors.enter(run_id);
        let deadline = steps.deadline(call.attempt, Duration::from_secs(120));
        let acted = steps.runtime.block_on(async {
            let session = steps.host.session(run_id).ok_or(("browser-lost", false))?;
            if !steps.host.intact(run_id).await {
                return Err(("not-intact", false));
            }
            let mut session = session.lock().await;
            if call.attempt.check().is_err() {
                return Err(("abandoned", false));
            }
            session.set_deadline(Some(deadline));
            failpoint("act.before");
            let acted = act(&mut session, call.input).await;
            failpoint("act.after");
            match acted {
                Ok(()) => Ok("ok"),
                Err(error) if error.fatal => Err((error.code, true)),
                Err(error) if error.code == "navigation-failed" => Ok("navigation-failed"),
                Err(_) => Ok("refused"),
            }
        });
        match acted {
            Ok(outcome) => Ok(ActivityReply::of(json!({
                "outcome": outcome,
                "now_ms": (steps.now)(),
            }))),
            Err((code, acted)) => Ok(steps.lost(code, acted)),
        }
    }
}

/// `browser.read`: the step's links opened in turn; never repeated.
struct Read(Steps);

/// What a read reports as it goes.
struct Reader<'a> {
    steps: &'a Steps,
    attempt: &'a Attempt,
    until_ms: i64,
    seen: Vec<(String, String)>,
    section: String,
}

impl Reading for Reader<'_> {
    async fn may_act(&mut self) -> bool {
        self.attempt.check().is_ok()
            && (self.steps.now)() < self.until_ms
            && self.steps.host.intact(&self.attempt.run_id).await
    }

    fn seen(&mut self, url: &str, text: &str) {
        failpoint("read.seen");
        self.seen.push((url.to_owned(), text.to_owned()));
    }

    fn keep(&mut self, section: String) {
        self.section = section;
    }
}

impl Activity for Read {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let steps = &self.0;
        let input = call.input;
        let (Some(decision), Some(observation), Some(until_ms)) = (
            wire::member(input, "decision"),
            wire::member(input, "observation").and_then(|value| wire::read_observation(&value)),
            int(input, "until_ms"),
        ) else {
            return Err(Failure::permanent("input-invalid", "the input is malformed").with_ended());
        };
        let run_id = call.attempt.run_id.as_str();
        let _running = steps.executors.enter(run_id);
        let deadline = steps.deadline(call.attempt, Duration::from_secs(600));
        let mut reader = Reader {
            steps,
            attempt: call.attempt,
            until_ms,
            seen: Vec::new(),
            section: String::new(),
        };
        let read = steps.runtime.block_on(async {
            let Some(session) = steps.host.session(run_id) else {
                return Err(ReadFailure::Unstarted(CuaError::fatal("browser-lost")));
            };
            if !steps.host.intact(run_id).await {
                return Err(ReadFailure::Unstarted(CuaError::fatal("not-intact")));
            }
            let mut session = session.lock().await;
            session.set_deadline(Some(deadline));
            read::run(&mut session, &decision, &observation, &mut reader).await
        });
        let (outcome, lost, clicked) = match read {
            Ok(outcome) => (outcome, None, false),
            Err(ReadFailure::Unstarted(error)) => ("refused", Some(error.code), false),
            Err(ReadFailure::Failed(error)) => ("interrupted", Some(error.code), true),
        };
        Ok(ActivityReply::of(json!({
            "outcome": outcome,
            "seen": reader.seen,
            "section": reader.section,
            "lost": lost,
            "clicked": clicked,
            "now_ms": (steps.now)(),
        })))
    }
}
