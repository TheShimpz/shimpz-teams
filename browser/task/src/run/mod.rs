//! One Browser task as a durable Holdpoint run (ADR-0117 §5; ADR-0125): the agent's loop between its Brain steps and
//! the Cua Driver, journaled step by step in Team's ledger so that a Team restart, a lost Browser, a Brain or provider
//! outage, or a crash in the middle of an action resumes the task where it stopped instead of losing it.
//!
//! Each step observes the page (`browser.observe`, an idempotent read), asks the Brain for one step (`brain`, the
//! `browser-step` operation) with the objective, the language, the task's thread ([`crate::thread`]), the bounded
//! observation, its downloads, and at most one screenshot; admits exactly one step from the closed set; checks a
//! targeted ref against the observation it was chosen from; carries it out (`browser.act` or `browser.read`, never
//! repeated: an action a crash interrupted is `uncertain` and recorded as `interrupted`); and appends the step and its
//! outcome to the thread, folding the oldest steps and having the agent rewrite its task memory (`browser-fold`)
//! before an entry would pass the history's bound. The definition is deterministic: every clock reading and price it
//! decides by comes back journaled in a step's result.
//!
//! Spend is a ceiling of reservations: before each Brain call the run reserves every provider attempt at its worst
//! case and settles only the attempts whose usage the Brain reports. The last [`FINISH_SECONDS`] and [`FINISH_SHARE`]
//! of the spend are kept for finishing: past them there is never another step, and the Brain answers from the thread
//! (`browser-final`). A done answer is screened for provenance ([`crate::screen`]) and judged complete by the Brain's
//! advisory check (`browser-check`); either may return it to the agent a bounded number of times.
//!
//! The Browser is replaceable mid-run: a Driver session that fails while the Browser is intact is replaced on the same
//! Browser; a Browser that is gone (Team restarted, the container or its egress generation replaced) is acquired again
//! on a fresh egress generation through `browser.open`, which reopens the last good page. Either way the agent is
//! told plainly what was lost, its thread and memory continue, and every old ref is invalid since a step may only
//! target the observation it was chosen from. At most [`MAX_RECOVERIES`] replacements happen in one task.

mod drive;
pub mod handlers;
mod state;
pub mod wire;

use std::fmt::Write as _;

use holdpoint::{Ctx, Definition, Interrupt};
use serde_json::json;
use sha2::{Digest, Sha256};
use strict_json::Value;

pub use crate::brain::{Answered, BrainCalls, BrowserCall, CallFailure, Meter, TaskBrain, Usage};
pub use handlers::{
    Executors, Mode, Now, OpenFailure, Price, Recovered, Recovery, RunCredentials, RunHost,
    Session, Steps,
};

/// The definition's id in the ledger.
pub const DEFINITION: &str = "browser-task";
/// The definition's version: a run recorded under another one ends `unrecoverable(definition-changed)`.
pub const VERSION: u32 = 1;
/// A task's one absolute deadline from its start.
pub const MAX_SECONDS: std::time::Duration =
    std::time::Duration::from_secs(team_protocol::browser::MAX_TASK_SECONDS);
/// US$0.50 in millionths of a cent: a token count times a catalog price in cents per million tokens.
pub const MAX_SPEND: u64 = 50 * 1_000_000;
/// The time kept for finishing, in milliseconds.
pub const FINISH_MS: i64 = 60_000;
/// The time kept for finishing.
pub const FINISH_SECONDS: std::time::Duration = std::time::Duration::from_secs(60);
/// The percentage of the spend kept for finishing: a final answer from a thread at every bound, each attempt at the
/// dearest Browser model's price, and the completeness check after it.
pub const FINISH_SHARE: u64 = 16;
/// A screenshot of the fixed 1280x800 window on either provider, in input tokens.
pub const IMAGE_TOKENS: u64 = 3_000;
/// The Brain's system prompt, step schema, and the instruction a fold, check, or final answer adds.
pub const PROMPT_TOKENS: u64 = 10_000;
/// A screenshot is granted at most once in this many steps.
pub const SCREENSHOT_EVERY: i64 = 3;
/// How long a `wait` step waits, in milliseconds.
pub const WAIT_MS: i64 = 2_000;
/// The most Driver sessions or Browsers one task replaces.
pub const MAX_RECOVERIES: u32 = 3;
/// Said with the next observation after Chromium was started again.
pub const RESTARTED_NOTICE: &str = "The Browser stopped answering on its last page and was started again: its tabs, forms, and page state are gone, so open again what you still need.\n";
/// Said with the next observation after the whole Browser was replaced.
pub const REPLACED_NOTICE: &str = "The Browser was lost and replaced by a new one, reopened on the last page that loaded: its tabs, forms, element refs, and downloads in progress are gone, and the page may differ now, so check it and open again what you still need.\n";
/// How often a done answer citing unscreened URLs goes back to the agent.
pub const MAX_RETURNS_UNSCREENED: u32 = 2;
/// How often an answer the check judged incomplete goes back to the agent.
pub const MAX_RETURNS_INCOMPLETE: u32 = 4;
/// The note a done answer citing unscreened URLs returns with.
pub const UNSCREENED_NOTE: &str = "Open each of these URLs to confirm it, or leave it out of your answer; no page of this task showed: ";
/// The note an incomplete answer returns with.
pub const INCOMPLETE_NOTE: &str = "Not finished. If sources you have not read yet may hold it, keep gathering there; if you covered them all, answer done again with what you verified and what is missing. Missing: ";

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

    fn parse(text: &str) -> Option<Self> {
        [
            Self::Done,
            Self::NeedsPerson,
            Self::Stopped,
            Self::Budget,
            Self::Failed,
        ]
        .into_iter()
        .find(|status| status.as_str() == text)
    }
}

/// How a task ended, with its closed details.
///
/// `cause` names, for the audit only, where a failure came from and its closed code; `recovered` counts the Driver
/// sessions or Browsers replaced on the way, and `relaunched` the ones that started Chromium again. A done answer
/// carries what screening and the completeness check left unresolved.
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
    /// How many Driver sessions or Browsers were replaced.
    pub recovered: u32,
    /// How many of those started Chromium again.
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

    pub(crate) const fn steps(mut self, steps: usize) -> Self {
        self.steps = steps;
        self
    }

    pub(crate) fn failed(reason: &str, steps: usize, cause: Option<String>) -> Self {
        Self {
            reason: Some(reason.to_owned()),
            cause,
            ..Self::new(Status::Failed).steps(steps)
        }
    }

    /// The outcome as the ledger keeps it.
    #[must_use]
    pub fn to_json(&self) -> serde_json::Value {
        json!({
            "status": self.status.as_str(),
            "summary": self.summary,
            "reason": self.reason,
            "sentence": self.sentence,
            "steps": self.steps,
            "cause": self.cause,
            "recovered": self.recovered,
            "relaunched": self.relaunched,
            "unscreened": self.unscreened,
            "completeness": self.completeness,
            "missing": self.missing,
        })
    }

    fn from_json(value: &serde_json::Value) -> Option<Self> {
        let text = |name: &str| optional_text(value, name).ok();
        let (steps, recovered, relaunched) = recorded_counts(value)?;
        let unscreened = value
            .get("unscreened")?
            .as_array()?
            .iter()
            .map(|url| url.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()?;
        Some(Self {
            status: Status::parse(value.get("status")?.as_str()?)?,
            summary: text("summary")?,
            reason: text("reason")?,
            sentence: text("sentence")?,
            steps,
            cause: text("cause")?,
            recovered,
            relaunched,
            unscreened,
            completeness: completeness(text("completeness")?.as_deref()).ok()?,
            missing: text("missing")?,
        })
    }
}

/// A recorded outcome's step and recovery counts.
fn recorded_counts(value: &serde_json::Value) -> Option<(usize, u32, u32)> {
    let number = |name: &str| value.get(name).and_then(serde_json::Value::as_u64);
    Some((
        usize::try_from(number("steps")?).ok()?,
        u32::try_from(number("recovered")?).ok()?,
        u32::try_from(number("relaunched")?).ok()?,
    ))
}

/// A recorded member that is not a string or null.
struct Malformed;

fn optional_text(value: &serde_json::Value, name: &str) -> Result<Option<String>, Malformed> {
    match value.get(name) {
        Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(text)) => Ok(Some(text.clone())),
        _ => Err(Malformed),
    }
}

/// A recorded completeness word from its closed set.
fn completeness(word: Option<&str>) -> Result<Option<&'static str>, Malformed> {
    match word {
        None => Ok(None),
        Some(word) => ["complete", "incomplete", "not-checked"]
            .into_iter()
            .find(|known| *known == word)
            .map(Some)
            .ok_or(Malformed),
    }
}

/// What a run starts from, captured in the ledger: every field is non-secret.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TaskInput {
    /// The Team.
    pub team_id: String,
    /// The Brain's Browser interrupt the task answers.
    pub interrupt_id: String,
    /// The objective.
    pub objective: String,
    /// The page language.
    pub language: String,
    /// When the task started, in wall-clock milliseconds.
    pub started_ms: i64,
    /// The provider's dearest Browser model's input and output price, in cents per million tokens.
    pub dearest: (u64, u64),
}

impl TaskInput {
    /// The task's absolute deadline.
    #[must_use]
    pub fn deadline_ms(&self) -> i64 {
        self.started_ms
            .saturating_add(i64::try_from(MAX_SECONDS.as_millis()).unwrap_or(i64::MAX))
    }

    /// The run's input as the ledger keeps it.
    #[must_use]
    pub fn to_json(&self) -> serde_json::Value {
        json!({
            "team_id": self.team_id,
            "interrupt_id": self.interrupt_id,
            "objective": self.objective,
            "language": self.language,
            "started_ms": self.started_ms,
            "dearest": [self.dearest.0, self.dearest.1],
        })
    }

    /// The input [`TaskInput::to_json`] wrote, or `None`.
    #[must_use]
    pub fn from_json(value: &serde_json::Value) -> Option<Self> {
        let text = |name: &str| value.get(name)?.as_str().map(str::to_owned);
        let dearest = value.get("dearest")?.as_array()?;
        if value.as_object()?.len() != 6 || dearest.len() != 2 {
            return None;
        }
        Some(Self {
            team_id: text("team_id")?,
            interrupt_id: text("interrupt_id")?,
            objective: text("objective")?,
            language: text("language")?,
            started_ms: value.get("started_ms")?.as_i64()?,
            dearest: (dearest[0].as_u64()?, dearest[1].as_u64()?),
        })
    }

    /// The run's id: one per Team and Browser interrupt (the Brain derives each interrupt id from its turn's own
    /// run), so submitting it again is the same run.
    #[must_use]
    pub fn run_id(&self) -> String {
        run_id(&self.team_id, &self.interrupt_id)
    }
}

/// The run id of the Team's task answering the Brain's Browser `interrupt_id`.
#[must_use]
pub fn run_id(team_id: &str, interrupt_id: &str) -> String {
    let digest = Sha256::digest(format!("{team_id}\0{interrupt_id}").as_bytes());
    let hex = digest[..16]
        .iter()
        .fold(String::with_capacity(32), |mut hex, byte| {
            // Writing to a String cannot fail.
            let _ = write!(hex, "{byte:02x}");
            hex
        });
    format!("browser-{hex}")
}

/// The Browser task definition.
#[derive(Clone, Copy, Debug, Default)]
pub struct BrowserTask;

impl Definition for BrowserTask {
    fn id(&self) -> &str {
        DEFINITION
    }

    fn version(&self) -> u32 {
        VERSION
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<holdpoint::Outcome, Interrupt> {
        let input = TaskInput::from_json(ctx.input())
            .ok_or_else(|| Interrupt::Unrecoverable("input-invalid".to_owned()))?;
        let outcome = drive::Loop::new(ctx, &input).run()?;
        Ok(holdpoint::Outcome::Done {
            result: outcome.to_json(),
        })
    }
}

fn progress_counts(progress: &serde_json::Value) -> (usize, u32, u32) {
    let number = |name: &str| progress.get(name).and_then(serde_json::Value::as_u64);
    (
        number("steps")
            .and_then(|steps| usize::try_from(steps).ok())
            .unwrap_or(0),
        number("recovered")
            .and_then(|count| u32::try_from(count).ok())
            .unwrap_or(0),
        number("relaunched")
            .and_then(|count| u32::try_from(count).ok())
            .unwrap_or(0),
    )
}

/// How a run's ending reads as a task's: a stop is `stopped`, the deadline or a bound is `budget`, and a run that
/// cannot continue (its authority revoked, its ledger refused) failed.
#[must_use]
pub fn outcome_of(ended: &holdpoint::Outcome) -> Outcome {
    let counted = |status: Status, progress: &serde_json::Value| {
        let (steps, recovered, relaunched) = progress_counts(progress);
        Outcome {
            recovered,
            relaunched,
            ..Outcome::new(status).steps(steps)
        }
    };
    match ended {
        holdpoint::Outcome::Done { result } => Outcome::from_json(result).unwrap_or_else(|| {
            Outcome::failed("browser-lost", 0, Some("run/result-invalid".to_owned()))
        }),
        holdpoint::Outcome::Cancelled { progress } => counted(Status::Stopped, progress),
        holdpoint::Outcome::Expired { progress } | holdpoint::Outcome::Limited { progress, .. } => {
            counted(Status::Budget, progress)
        }
        holdpoint::Outcome::NeedsPerson { .. } => {
            Outcome::failed("browser-lost", 0, Some("run/needs-person".to_owned()))
        }
        holdpoint::Outcome::Unrecoverable { reason } => {
            let reason: String = reason
                .chars()
                .filter(|character| character.is_ascii_alphanumeric() || *character == '-')
                .take(64)
                .collect();
            Outcome::failed("browser-lost", 0, Some(format!("run/{reason}")))
        }
    }
}

/// Aborts the process at the `n`th time `point` is reached when `TEAM_RUN_FAILPOINT` names `point` or `point:n`, leaving
/// the ledger exactly as a crash there would: the crash tests' only seam, absent from every other build.
#[cfg(test)]
pub(crate) fn failpoint(point: &str) {
    use std::collections::HashMap;
    use std::sync::{Mutex, PoisonError};
    static HITS: Mutex<Option<HashMap<String, u32>>> = Mutex::new(None);
    let Ok(selected) = std::env::var("TEAM_RUN_FAILPOINT") else {
        return;
    };
    let (name, nth) = selected
        .split_once(':')
        .map_or((selected.as_str(), 1), |(name, nth)| {
            (name, nth.parse().unwrap_or(1))
        });
    if name != point {
        return;
    }
    let mut hits = HITS.lock().unwrap_or_else(PoisonError::into_inner);
    let count = hits
        .get_or_insert_with(HashMap::new)
        .entry(name.to_owned())
        .or_default();
    *count += 1;
    if *count == nth {
        std::process::abort();
    }
}

/// Crash points exist only in the crash tests.
#[cfg(not(test))]
pub(crate) const fn failpoint(_point: &str) {}

/// The strict JSON object of `members`.
pub(crate) fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

/// A strict JSON string.
pub(crate) fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// `base` with `extra` members appended.
pub(crate) fn with(base: &Value, extra: Vec<(&str, Value)>) -> Value {
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

/// A string member's text, or empty.
pub(crate) fn as_str(value: Option<&Value>) -> &str {
    match value {
        Some(Value::String(text)) => text,
        _ => "",
    }
}

#[cfg(test)]
mod tests;
