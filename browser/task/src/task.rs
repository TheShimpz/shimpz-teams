//! One Browser task: Team's loop between the Browser agent's steps and the Cua Driver (ADR-0117 §5, 2026-10-22).
//!
//! Each step sends the Brain operation `browser-step` the objective, the language, the task's thread
//! ([`crate::thread`]), the current bounded observation, its downloads, and at most one screenshot; admits exactly one
//! step from the closed set; checks a targeted ref against the observation it was chosen from; executes it; and
//! appends the step and its outcome to the thread, folding the oldest steps and having the agent rewrite its task
//! memory (`browser-fold`) before an entry would pass the history's bound. Before every Brain call and every page
//! action the task proves it may continue: no Stop, time and spend left, and the Browser still on the egress process
//! it started on. No call outlives the task's deadline.
//!
//! Spend is a ceiling of reservations: before each Brain call Team reserves every provider attempt at its worst case
//! and settles only the attempts whose usage the Brain reports. The last [`FINISH_SECONDS`] and [`FINISH_SHARE`] of
//! the spend are kept for finishing: past them there is never another step, and the Brain answers from the thread
//! (`browser-final`). A done answer is screened for provenance ([`crate::screen`]) and judged complete by the Brain's
//! advisory check (`browser-check`); either may return it to the agent a bounded number of times, and what remains
//! travels with the result.
//!
//! A Cua Driver session that fails while the Browser is intact is replaced by a fresh one on the same Browser, at most
//! [`MAX_RECOVERIES`] times, and the task continues where it was with its whole thread: an action under way when it
//! failed is recorded as `interrupted`, since its effect is unknown. Every failure keeps its closed cause for the audit.

use std::fmt::Write as _;
use std::sync::Arc;
use std::time::Duration;

use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser::{
    MAX_CALL_ATTEMPTS, MAX_CALL_OUTPUT_TOKENS, MAX_NOTE_CHARS, MAX_STEPS, MAX_TASK_SECONDS,
    canonical_check, canonical_memory, canonical_step, entry_bytes, request_text_bytes,
};
use tokio::time::{Instant, sleep};

pub use crate::brain::{Answered, BrainCalls, BrowserCall, Meter, StepFailed, TaskBrain, Usage};
use crate::cua::{CuaError, CuaSession, Observation};
use crate::read::{self, ReadFailure, Reading};
use crate::screen::Screen;
use crate::thread::Thread;

/// A task's one absolute deadline from its start.
pub const MAX_SECONDS: Duration = Duration::from_secs(MAX_TASK_SECONDS);
/// US$0.50 in millionths of a cent: a token count times a catalog price in cents per million tokens.
pub const MAX_SPEND: u64 = 50 * 1_000_000;
/// The time kept for finishing.
pub const FINISH_SECONDS: Duration = Duration::from_secs(60);
/// The percentage of the spend kept for finishing: a final answer from a thread at every bound, each attempt at the
/// dearest Browser model's price, and the completeness check after it.
pub const FINISH_SHARE: u64 = 16;
/// A screenshot of the fixed 1280x800 window on either provider, in input tokens.
pub const IMAGE_TOKENS: u64 = 3_000;
/// The Brain's system prompt, step schema, and the instruction a fold, check, or final answer adds.
pub const PROMPT_TOKENS: u64 = 10_000;
/// A screenshot is granted at most once in this many steps.
pub const SCREENSHOT_EVERY: i64 = 3;
/// How long a `wait` step waits.
pub const WAIT_SECONDS: Duration = Duration::from_secs(2);
/// The most Driver sessions one task replaces.
pub const MAX_RECOVERIES: u32 = 3;
/// Said with the next observation after Chromium was started again.
pub const RESTARTED_NOTICE: &str = "The Browser stopped answering on its last page and was started again: its tabs, forms, and page state are gone, so open again what you still need.\n";
/// How often a done answer citing unscreened URLs goes back to the agent.
pub const MAX_RETURNS_UNSCREENED: u32 = 2;
/// How often an answer the check judged incomplete goes back to the agent.
pub const MAX_RETURNS_INCOMPLETE: u32 = 4;
/// The note a done answer citing unscreened URLs returns with.
pub const UNSCREENED_NOTE: &str = "Open each of these URLs to confirm it, or leave it out of your answer; no page of this task showed: ";
/// The note an incomplete answer returns with.
pub const INCOMPLETE_NOTE: &str = "Not finished. If sources you have not read yet may hold it, keep gathering there; if you covered them all, answer done again with what you verified and what is missing. Missing: ";

/// A model's input and output price in cents per million tokens; `None` names the provider's dearest model.
pub type Price = Arc<dyn Fn(Option<&str>) -> (u64, u64) + Send + Sync>;

/// Where the task's Browser lives: whether it is intact, its Driver sessions, and its downloads.
pub trait TaskHost: Send + Sync {
    /// Whether the Browser still runs on the egress process it started on.
    fn intact(&self) -> BoxFuture<'_, bool>;

    /// A fresh Driver session on the same Browser in place of `failed`, opened by `deadline`, and whether Chromium was
    /// started again for it; with `relaunch`, it always is.
    ///
    /// # Errors
    /// [`CuaError`] when the failed one could not be proven ended or the new one did not open.
    fn reopen(
        &self,
        failed: CuaSession,
        deadline: Instant,
        relaunch: bool,
    ) -> BoxFuture<'_, Result<(CuaSession, bool), CuaError>>;

    /// The task's downloads so far in the protocol's form, read by `deadline`; `None` when unreadable.
    fn downloads(&self, deadline: Instant) -> BoxFuture<'_, Option<Value>>;
}

/// How a task ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    /// The agent answered.
    Done,
    /// The page needs a person.
    NeedsPerson,
    /// The turn was stopped.
    Stopped,
    /// Time, steps, or spend ran out.
    Budget,
    /// The task failed.
    Failed,
}

impl Status {
    /// The closed word.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Done => "done",
            Self::NeedsPerson => "needs-person",
            Self::Stopped => "stopped",
            Self::Budget => "budget",
            Self::Failed => "failed",
        }
    }
}

/// How a task ended, with its closed details.
///
/// `cause` names, for the audit only, where a failure came from and its closed code; `recovered` counts the Driver
/// sessions replaced on the way, and `relaunched` the ones that needed Chromium started again. A done answer carries
/// what screening and the completeness check left unresolved.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// The ending.
    pub status: Status,
    /// A done answer's summary.
    pub summary: Option<String>,
    /// The closed reason of a needs-person or failed ending.
    pub reason: Option<String>,
    /// A needs-person ending's sentence.
    pub sentence: Option<String>,
    /// How many steps were taken.
    pub steps: usize,
    /// Where a failure came from, for the audit only.
    pub cause: Option<String>,
    /// How many Driver sessions were replaced.
    pub recovered: u32,
    /// How many of those needed Chromium started again.
    pub relaunched: u32,
    /// A done answer's cited URLs no page of the task showed.
    pub unscreened: Vec<String>,
    /// A done answer's completeness: `complete`, `incomplete`, or `not-checked`.
    pub completeness: Option<&'static str>,
    /// What an incomplete answer misses.
    pub missing: Option<String>,
}

impl Outcome {
    /// An ending with no details.
    #[must_use]
    pub const fn new(status: Status) -> Self {
        Self {
            status,
            summary: None,
            reason: None,
            sentence: None,
            steps: 0,
            cause: None,
            recovered: 0,
            relaunched: 0,
            unscreened: Vec::new(),
            completeness: None,
            missing: None,
        }
    }

    fn steps(mut self, steps: usize) -> Self {
        self.steps = steps;
        self
    }

    fn failed(reason: &str, steps: usize, cause: Option<String>) -> Self {
        Self {
            reason: Some(reason.to_owned()),
            cause,
            ..Self::new(Status::Failed).steps(steps)
        }
    }
}

/// The completeness check's verdict: `None` when it was not checked.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Verdict {
    complete: Option<bool>,
    missing: Option<String>,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn count(value: u64) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn with(base: &Value, extra: Vec<(&str, Value)>) -> Value {
    let mut members = match base {
        Value::Object(members) => members.clone(),
        _ => Vec::new(),
    };
    members.extend(
        extra
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value)),
    );
    Value::Object(members)
}

fn as_str(value: Option<&Value>) -> &str {
    match value {
        Some(Value::String(text)) => text,
        _ => "",
    }
}

/// The step the Brain chose, or how the task ended instead.
enum Decision {
    /// The admitted step.
    Step(Value),
    /// The task ended.
    Ended(Outcome),
}

/// What a turn's observation came to.
enum Observed {
    /// The page, shown as the agent sees it.
    Page(Observation),
    /// The task ended.
    Ended(Outcome),
    /// The Driver session was replaced; the next step looks again.
    Recovered,
}

/// What a task reaches beyond its Driver session: the Brain, the price of its models, its Stop, and its Browser.
#[derive(Clone)]
pub struct TaskSeams {
    /// The task's Brain calls.
    pub brain: Arc<dyn TaskBrain>,
    /// The price of the provider's models.
    pub price: Price,
    /// The turn's Stop.
    pub stop: Stop,
    /// The Browser the task runs in.
    pub host: Arc<dyn TaskHost>,
}

impl std::fmt::Debug for TaskSeams {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("TaskSeams")
    }
}

/// Everything a task holds beside its Driver session.
struct State {
    objective: String,
    language: String,
    brain: Arc<dyn TaskBrain>,
    price: Price,
    stop: Stop,
    host: Arc<dyn TaskHost>,
    deadline: Instant,
    steps: usize,
    spend: u64,
    spent: u64,
    recovered: u32,
    relaunched: u32,
    /// Said with the next observation after Chromium was started again: the agent's page state did not survive it.
    restarted: bool,
    thread: Thread,
    screen: Screen,
    screenshot_at: i64,
    wants_screenshot: bool,
    model: Option<String>,
    last_good: Option<String>,
    section: String,
    returned_unscreened: u32,
    returned_incomplete: u32,
}

/// One task's loop.
pub struct Task {
    session: Option<CuaSession>,
    state: State,
}

impl std::fmt::Debug for Task {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Task").finish_non_exhaustive()
    }
}

impl Task {
    /// A task toward `objective` in the page `language`, on its opened Driver `session`, ending by `deadline` at the
    /// latest.
    #[must_use]
    pub fn new(
        objective: String,
        language: String,
        session: CuaSession,
        seams: TaskSeams,
        deadline: Instant,
    ) -> Self {
        let TaskSeams {
            brain,
            price,
            stop,
            host,
        } = seams;
        Self {
            session: Some(session),
            state: State {
                objective,
                language,
                brain,
                price,
                stop,
                host,
                deadline,
                steps: MAX_STEPS,
                spend: MAX_SPEND,
                spent: 0,
                recovered: 0,
                relaunched: 0,
                restarted: false,
                thread: Thread::default(),
                screen: Screen::default(),
                screenshot_at: -SCREENSHOT_EVERY,
                wants_screenshot: false,
                model: None,
                last_good: None,
                section: String::new(),
                returned_unscreened: 0,
                returned_incomplete: 0,
            },
        }
    }

    /// Runs the task to its end.
    pub async fn run(mut self) -> Outcome {
        self.execute().await
    }

    async fn execute(&mut self) -> Outcome {
        if let Some(session) = self.session.as_mut() {
            session.set_deadline(Some(self.state.deadline));
        }
        for index in 0..self.state.steps {
            let ended = match self.state.halted(index).await {
                Some(ended) => Some(ended),
                None => self.turn(index).await,
            };
            if let Some(ended) = ended {
                return self.state.counted(ended);
            }
        }
        let ended = self.finish_from_thread(self.state.steps, None).await;
        self.state.counted(ended)
    }

    /// Observes, asks for one step, and acts on it; the outcome that ends the task, or `None` to continue.
    async fn turn(&mut self, index: usize) -> Option<Outcome> {
        if self.state.finishing() {
            return Some(self.finish_from_thread(index, None).await);
        }
        let observation = match self.observe(index).await {
            Observed::Page(observation) => observation,
            Observed::Ended(outcome) => return Some(outcome),
            Observed::Recovered => return None,
        };
        let downloads = self.state.host.downloads(self.state.deadline).await;
        let request = self.state.request(&observation, downloads.as_ref());
        let decision = match self.decide(index, &request).await {
            Decision::Step(decision) => decision,
            Decision::Ended(outcome) => return Some(outcome),
        };
        match as_str(decision.get("kind")) {
            "done" => {
                let summary = as_str(decision.get("summary")).to_owned();
                self.finish(index, &request, &observation, downloads.as_ref(), summary)
                    .await
            }
            "needs_person" => Some(Outcome {
                reason: Some(as_str(decision.get("reason")).to_owned()),
                sentence: Some(as_str(decision.get("sentence")).to_owned()),
                ..Outcome::new(Status::NeedsPerson).steps(index + 1)
            }),
            _ => {
                self.act(index, &request, &decision, &observation, downloads.as_ref())
                    .await
            }
        }
    }

    /// The current page, back on the last page that loaded after an error page, with any read pages joined.
    async fn observe(&mut self, index: usize) -> Observed {
        let mut notice = if self.state.restarted {
            RESTARTED_NOTICE.to_owned()
        } else {
            String::new()
        };
        let screenshot = self.state.wants_screenshot;
        let observed = match self.session.as_mut() {
            Some(session) => {
                Self::look(
                    session,
                    screenshot,
                    self.state.last_good.as_deref(),
                    &mut notice,
                )
                .await
            }
            None => Err(CuaError::fatal("channel-lost")),
        };
        let observation = match observed {
            Ok(observation) => observation,
            // Nothing was done on the page: the next step looks at it again on the fresh session.
            Err(error) => {
                return match self.recover(index, format!("observe/{}", error.code)).await {
                    Some(outcome) => Observed::Ended(outcome),
                    None => Observed::Recovered,
                };
            }
        };
        let state = &mut self.state;
        state.restarted = false;
        if observation.url.starts_with("https://") || observation.url.starts_with("http://") {
            state.last_good = Some(observation.url.clone());
        }
        if state.wants_screenshot {
            state.screenshot_at = i64::try_from(index).unwrap_or(i64::MAX);
            state.wants_screenshot = false;
        }
        let observation = read::shown(&observation, &notice, &state.section);
        state.section.clear();
        state.screen.observed(&observation.url, &observation.text);
        Observed::Page(observation)
    }

    async fn look(
        session: &mut CuaSession,
        screenshot: bool,
        last_good: Option<&str>,
        notice: &mut String,
    ) -> Result<Observation, CuaError> {
        let observation = session.observe(screenshot).await?;
        match last_good {
            Some(last_good) if observation.url.starts_with("chrome-error://") => {
                session.navigate(last_good).await?;
                let back = session.observe(screenshot).await?;
                // Writing to a String cannot fail.
                let _ = writeln!(
                    notice,
                    "The last page you opened ({}) did not load; you are back on {last_good}.",
                    observation.title
                );
                Ok(back)
            }
            _ => Ok(observation),
        }
    }

    /// The step the Brain chose, admitted only while the task may still act on it, or how the task ends.
    ///
    /// Even a final step counts only if the task could still have acted on it: no Stop, time left, and the same
    /// egress process. A step the normal spend share cannot reserve finishes the task instead.
    async fn decide(&mut self, index: usize, request: &Value) -> Decision {
        let reserve = self.state.attempt(bound(request)) * u64::from(MAX_CALL_ATTEMPTS);
        if !self.state.affords(reserve, false) {
            return Decision::Ended(self.finish_from_thread(index, Some(request)).await);
        }
        let asked = self
            .state
            .ask(BrowserCall::Step, request, false, None)
            .await;
        let raw = match asked {
            Ok(raw) => raw,
            Err(StepFailed) => match self.retry(index, request, reserve).await {
                Decision::Step(raw) => raw,
                ended @ Decision::Ended(_) => return ended,
            },
        };
        // The soft cutoff passed while the Brain answered: no step is taken past it.
        if let Some(ended) = self.state.halted(index + 1).await {
            return Decision::Ended(ended);
        }
        if self.state.finishing() {
            return Decision::Ended(self.finish_from_thread(index + 1, Some(request)).await);
        }
        canonical_step(&raw).map_or_else(
            || Decision::Ended(Outcome::failed("step-invalid", index + 1, None)),
            Decision::Step,
        )
    }

    /// A model answers outside its form now and then: the step is asked once more, under its own reservation, unless
    /// the normal spend share can no longer reserve it, when the task finishes instead.
    async fn retry(&mut self, index: usize, request: &Value, reserve: u64) -> Decision {
        if self.state.halted(index).await.is_some() || self.state.finishing() {
            return Decision::Ended(self.unanswered(index, request).await);
        }
        if !self.state.affords(reserve, false) {
            return Decision::Ended(self.finish_from_thread(index, Some(request)).await);
        }
        match self
            .state
            .ask(BrowserCall::Step, request, false, None)
            .await
        {
            Ok(raw) => Decision::Step(raw),
            Err(StepFailed) => Decision::Ended(self.unanswered(index, request).await),
        }
    }

    /// How a step the Brain did not answer ends the task.
    ///
    /// Stop aborts the Brain request in flight, which then fails: that task was stopped, not failed. A step cut short
    /// by the soft cutoff finishes the task from its thread.
    async fn unanswered(&mut self, index: usize, request: &Value) -> Outcome {
        if let Some(ended) = self.state.halted(index).await {
            return ended;
        }
        if self.state.finishing() {
            return self.finish_from_thread(index, Some(request)).await;
        }
        Outcome::failed("model-unavailable", index, None)
    }

    /// Runs one admitted page step and records its outcome; the outcome that ends the task, or `None`.
    async fn act(
        &mut self,
        index: usize,
        request: &Value,
        decision: &Value,
        observation: &Observation,
        downloads: Option<&Value>,
    ) -> Option<Outcome> {
        let mut failure = None;
        let outcome = if as_str(decision.get("kind")) == "screenshot" {
            let index = i64::try_from(index).unwrap_or(i64::MAX);
            self.state.wants_screenshot = index - self.state.screenshot_at >= SCREENSHOT_EVERY;
            if self.state.wants_screenshot {
                "ok"
            } else {
                "refused"
            }
        } else {
            match self.perform(decision, observation).await {
                Ok(outcome) => outcome,
                // The read never clicked: nothing was done on the page.
                Err(ReadFailure::Unstarted(error)) => {
                    failure = Some(format!("act/{}", error.code));
                    "refused"
                }
                Err(ReadFailure::Failed(error)) => {
                    failure = Some(format!("act/{}", error.code));
                    "interrupted"
                }
            }
        };
        self.state
            .record(request, decision.clone(), outcome, observation, downloads)
            .await;
        match failure {
            None => None,
            Some(cause) => self.recover(index + 1, cause).await,
        }
    }

    async fn perform(
        &mut self,
        decision: &Value,
        observation: &Observation,
    ) -> Result<&'static str, ReadFailure> {
        let Some(session) = self.session.as_mut() else {
            return Err(ReadFailure::Failed(CuaError::fatal("channel-lost")));
        };
        if as_str(decision.get("kind")) == "read" {
            return read::run(session, decision, observation, &mut self.state).await;
        }
        execute(session, decision, observation)
            .await
            .map_err(ReadFailure::Failed)
    }

    /// A done answer stands once screened and checked, or goes back to the agent with what it still needs.
    async fn finish(
        &mut self,
        index: usize,
        request: &Value,
        observation: &Observation,
        downloads: Option<&Value>,
        summary: String,
    ) -> Option<Outcome> {
        let unscreened = self.state.screen.unscreened(&summary);
        if !unscreened.is_empty() && self.state.returned_unscreened < MAX_RETURNS_UNSCREENED {
            self.state.returned_unscreened += 1;
            let note = format!("{UNSCREENED_NOTE}{}", unscreened.join(", "));
            self.state
                .give_back(request, observation, downloads, &note)
                .await;
            return None;
        }
        let verdict = self.state.check(&summary).await;
        if let Some(ended) = self.state.halted(index + 1).await {
            return Some(ended);
        }
        if verdict.complete == Some(false)
            && self.state.returned_incomplete < MAX_RETURNS_INCOMPLETE
        {
            self.state.returned_incomplete += 1;
            let note = format!("{INCOMPLETE_NOTE}{}", verdict.missing.unwrap_or_default());
            self.state
                .give_back(request, observation, downloads, &note)
                .await;
            return None;
        }
        Some(done(summary, index + 1, unscreened, &verdict))
    }

    /// Past the soft cutoff: the Brain answers from the thread once, never another step; else the budget ends it.
    async fn finish_from_thread(&mut self, index: usize, request: Option<&Value>) -> Outcome {
        let request = if let Some(request) = request {
            request.clone()
        } else {
            let observation = match self.observe(index).await {
                Observed::Page(observation) => observation,
                Observed::Ended(outcome) => return outcome,
                Observed::Recovered => return Outcome::new(Status::Budget).steps(index),
            };
            let downloads = self.state.host.downloads(self.state.deadline).await;
            self.state.request(&observation, downloads.as_ref())
        };
        let Ok(value) = self
            .state
            .ask(BrowserCall::Final, &request, true, None)
            .await
        else {
            return match self.state.halted(index).await {
                Some(ended) => ended,
                None => Outcome::new(Status::Budget).steps(index),
            };
        };
        if let Some(ended) = self.state.halted(index).await {
            return ended;
        }
        let summary = match &value {
            Value::Object(members) if members.len() == 1 => value.get("summary"),
            _ => None,
        };
        let Some(Value::String(summary)) = summary else {
            return Outcome::new(Status::Budget).steps(index);
        };
        if canonical_step(&object(vec![
            ("kind", text("done")),
            ("summary", text(summary)),
        ]))
        .is_none()
        {
            return Outcome::new(Status::Budget).steps(index);
        }
        let unscreened = self.state.screen.unscreened(summary);
        done(summary.clone(), index, unscreened, &Verdict::default())
    }

    /// Replaces the failed Driver session while the task may continue; how the task ends when it cannot.
    async fn recover(&mut self, steps: usize, cause: String) -> Option<Outcome> {
        // Stop, the deadline, and a lost egress win over the failure itself.
        if let Some(ended) = self.state.halted(steps).await {
            return Some(ended);
        }
        if self.state.recovered >= MAX_RECOVERIES {
            return Some(Outcome::failed("browser-lost", steps, Some(cause)));
        }
        // A page Chromium no longer reads, through any Driver session, needs Chromium itself started again.
        let relaunch = cause.ends_with("/browser-read-refused");
        let Some(failed) = self.session.take() else {
            return Some(Outcome::failed("browser-lost", steps, Some(cause)));
        };
        match self
            .state
            .host
            .reopen(failed, self.state.deadline, relaunch)
            .await
        {
            Err(error) => {
                // Stop and the deadline win over a recovery they cut short.
                Some(match self.state.halted(steps).await {
                    Some(ended) => ended,
                    None => Outcome::failed(
                        "browser-lost",
                        steps,
                        Some(format!("{cause}/reopen/{}", error.code)),
                    ),
                })
            }
            Ok((mut session, relaunched)) => {
                session.set_deadline(Some(self.state.deadline));
                self.session = Some(session);
                self.state.recovered += 1;
                if relaunched {
                    self.state.relaunched += 1;
                    self.state.restarted = true;
                }
                None
            }
        }
    }
}

impl Reading for State {
    async fn may_act(&mut self) -> bool {
        self.halted(0).await.is_none() && !self.finishing()
    }

    fn seen(&mut self, url: &str, text: &str) {
        self.screen.observed(url, text);
    }

    fn keep(&mut self, section: String) {
        self.section = section;
    }
}

/// The most input tokens a step request can cost: its rendered text, its screenshot, and the fixed prompt.
fn bound(request: &Value) -> u64 {
    let text = request_text_bytes(
        as_str(request.get("objective")),
        request.get("thread").unwrap_or(&Value::Null),
        as_str(request.get("observation")),
        request.get("downloads").unwrap_or(&Value::Null),
    );
    let image = if request
        .get("screenshot")
        .is_some_and(|shot| *shot != Value::Null)
    {
        IMAGE_TOKENS
    } else {
        0
    };
    text as u64 + image + PROMPT_TOKENS
}

/// A done outcome with what screening and the completeness check left unresolved.
fn done(summary: String, steps: usize, unscreened: Vec<String>, verdict: &Verdict) -> Outcome {
    let completeness = match verdict.complete {
        None => "not-checked",
        Some(true) => "complete",
        Some(false) => "incomplete",
    };
    Outcome {
        summary: Some(summary),
        unscreened,
        completeness: Some(completeness),
        missing: if completeness == "incomplete" {
            verdict.missing.clone()
        } else {
            None
        },
        ..Outcome::new(Status::Done).steps(steps)
    }
}

/// Whether a click or type step's ref was offered for its action; other steps name no ref.
fn offered(kind: &str, reference: &str, observation: &Observation) -> bool {
    match kind {
        "click" | "type" => observation
            .actions(reference)
            .is_some_and(|actions| actions.contains(kind)),
        _ => true,
    }
}

/// Executes one page step; `refused` when its ref was not offered for it or the page refused it.
async fn execute(
    session: &mut CuaSession,
    decision: &Value,
    observation: &Observation,
) -> Result<&'static str, CuaError> {
    let kind = as_str(decision.get("kind"));
    let reference = as_str(decision.get("ref"));
    if !offered(kind, reference, observation) {
        return Ok("refused");
    }
    let done = match kind {
        "navigate" => session.navigate(as_str(decision.get("url"))).await,
        "click" => session.click(reference).await,
        "type" => {
            session
                .type_text(reference, as_str(decision.get("text")))
                .await
        }
        "scroll" => session.scroll(as_str(decision.get("direction"))).await,
        _ => {
            sleep(WAIT_SECONDS).await;
            Ok(())
        }
    };
    match done {
        Ok(()) => Ok("ok"),
        Err(error) if error.fatal => Err(error),
        Err(error) if error.code == "navigation-failed" => Ok("navigation-failed"),
        Err(_) => Ok("refused"),
    }
}

mod state;
#[cfg(test)]
mod tests;
