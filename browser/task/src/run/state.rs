//! The task's running state, rebuilt on every replay from the run's input and its steps' results: its budget, its
//! thread and screen, and its Brain calls with their records and the check of its answer.

use holdpoint::Interrupt;
use serde_json::json;
use strict_json::Value;
use team_protocol::browser::{
    MAX_CALL_ATTEMPTS, MAX_CALL_OUTPUT_TOKENS, MAX_NOTE_CHARS, canonical_check, canonical_memory,
    entry_bytes, request_text_bytes,
};

use super::drive::{Loop, now_of};
use super::handlers::BRAIN;
use super::wire::{self, count};
use super::{
    FINISH_MS, FINISH_SHARE, IMAGE_TOKENS, MAX_SPEND, Outcome, PROMPT_TOKENS, Status, TaskInput,
    as_str, object, text, with,
};
use crate::brain::BrowserCall;
use crate::cua::Observation;
use crate::screen::Screen;
use crate::thread::Thread;

/// An observation as the run holds it: the page without its screenshot, and the transient blob the screenshot is in.
#[derive(Clone, Debug)]
pub(super) struct Seen {
    pub(super) observation: Observation,
    pub(super) blob: Option<u64>,
}

/// The completeness check's verdict: `None` when it was not checked.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct Verdict {
    pub(super) complete: Option<bool>,
    pub(super) missing: Option<String>,
}

/// The screenshot member of a request whose screenshot is in blob `id`: its bytes join the request only when the
/// model step is built, so the ledger keeps nothing of them but the request's digest.
const BLOB_PREFIX: &str = "blob:";

/// Everything a task holds between its steps.
pub(super) struct State {
    pub(super) team_id: String,
    pub(super) objective: String,
    pub(super) language: String,
    pub(super) deadline_ms: i64,
    /// The latest wall-clock time a step's result reported.
    pub(super) now_ms: i64,
    pub(super) spent: u64,
    pub(super) recovered: u32,
    pub(super) relaunched: u32,
    /// Said with the next observation after the Browser was restarted or replaced.
    pub(super) notice: Option<&'static str>,
    pub(super) thread: Thread,
    pub(super) screen: Screen,
    pub(super) screenshot_at: i64,
    pub(super) wants_screenshot: bool,
    /// The input and output price of the model that answered last, or the dearest before any answer.
    pub(super) price: (u64, u64),
    pub(super) last_good: Option<String>,
    pub(super) section: String,
    pub(super) returned_unscreened: u32,
    pub(super) returned_incomplete: u32,
    /// Screenshot blobs no later request reads; the next Brain call releases them.
    pub(super) spent_blobs: Vec<u64>,
    /// The screenshot blob of the current observation.
    pub(super) current_blob: Option<u64>,
}

impl State {
    pub(super) fn new(input: &TaskInput) -> Self {
        Self {
            team_id: input.team_id.clone(),
            objective: input.objective.clone(),
            language: input.language.clone(),
            deadline_ms: input.deadline_ms(),
            now_ms: input.started_ms,
            spent: 0,
            recovered: 0,
            relaunched: 0,
            notice: None,
            thread: Thread::default(),
            screen: Screen::default(),
            screenshot_at: -super::SCREENSHOT_EVERY,
            wants_screenshot: false,
            price: input.dearest,
            last_good: None,
            section: String::new(),
            returned_unscreened: 0,
            returned_incomplete: 0,
            spent_blobs: Vec::new(),
            current_blob: None,
        }
    }

    pub(super) fn counted(&self, outcome: Outcome) -> Outcome {
        Outcome {
            recovered: self.recovered,
            relaunched: self.relaunched,
            ..outcome
        }
    }

    /// Takes the time a step's result reports; time never runs backwards in the run.
    pub(super) fn clock(&mut self, value: &serde_json::Value) {
        if let Some(now) = now_of(value) {
            self.now_ms = self.now_ms.max(now);
        }
    }

    /// The soft cutoff: past it there is never another step.
    pub(super) fn cutoff_ms(&self) -> i64 {
        self.deadline_ms.saturating_sub(FINISH_MS)
    }

    pub(super) fn finishing(&self) -> bool {
        self.now_ms >= self.cutoff_ms()
    }

    pub(super) fn affords(&self, reserve: u64, finishing: bool) -> bool {
        let share = if finishing { 100 } else { 100 - FINISH_SHARE };
        self.spent.saturating_add(reserve) <= MAX_SPEND * share / 100
    }

    /// One provider attempt's worst cost: its input bound and the most output a call may write.
    pub(super) fn attempt(&self, bound: u64) -> u64 {
        bound
            .saturating_mul(self.price.0)
            .saturating_add(u64::from(MAX_CALL_OUTPUT_TOKENS).saturating_mul(self.price.1))
    }

    /// What a step request reserves: every provider attempt at its worst case.
    pub(super) fn reserve(&self, request: &Value) -> u64 {
        self.attempt(bound(request))
            .saturating_mul(u64::from(MAX_CALL_ATTEMPTS))
    }

    /// The step request of an observation and its downloads; the screenshot is named by its blob.
    pub(super) fn request(&mut self, seen: &Seen, downloads: Option<&Value>) -> Value {
        if let Some(previous) = self.current_blob.take() {
            self.spent_blobs.push(previous);
        }
        self.current_blob = seen.blob;
        let screenshot = seen
            .blob
            .map_or(Value::Null, |blob| text(&format!("{BLOB_PREFIX}{blob}")));
        object(vec![
            ("objective", text(&self.objective)),
            ("language", text(&self.language)),
            ("thread", self.thread.wire()),
            ("observation", text(&seen.observation.text)),
            ("screenshot", screenshot),
            ("downloads", downloads.cloned().unwrap_or(Value::Null)),
        ])
    }
}

/// The most input tokens a step request can cost: its rendered text, its screenshot, and the fixed prompt.
pub(super) fn bound(request: &Value) -> u64 {
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

/// `request` with its screenshot's bytes in place of its blob's name.
fn materialize(request: &Value, blobs: &holdpoint::Blobs<'_>) -> Result<Value, Interrupt> {
    let Value::Object(members) = request else {
        return Ok(request.clone());
    };
    let mut members = members.clone();
    for (name, value) in &mut members {
        let blob = match value {
            Value::String(named) if name == "screenshot" => named
                .strip_prefix(BLOB_PREFIX)
                .and_then(|id| id.parse::<u64>().ok()),
            _ => None,
        };
        if let Some(blob) = blob {
            *value = match blobs.get(blob)? {
                Some(bytes) => text(&crate::cua::base64(&bytes)),
                None => Value::Null,
            };
        }
    }
    Ok(Value::Object(members))
}

/// A done outcome with what screening and the completeness check left unresolved.
pub(super) fn done(
    summary: String,
    steps: usize,
    unscreened: Vec<String>,
    verdict: &Verdict,
) -> Outcome {
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

impl Loop<'_, '_> {
    /// One Brain call under the spend ceiling; `input` is the call's input bound when its request is no step request.
    /// `Err(())` when it was not answered.
    pub(super) fn ask(
        &mut self,
        call: BrowserCall,
        request: &Value,
        finishing: bool,
        input: Option<u64>,
    ) -> Result<Result<Value, ()>, Interrupt> {
        let attempt = self.state.attempt(input.unwrap_or_else(|| bound(request)));
        let reserve = attempt.saturating_mul(u64::from(MAX_CALL_ATTEMPTS));
        if !self.state.affords(reserve, finishing) {
            return Ok(Err(()));
        }
        let until = if finishing {
            self.state.deadline_ms
        } else {
            self.state.cutoff_ms()
        };
        if until <= self.state.now_ms {
            return Ok(Err(()));
        }
        let release = std::mem::take(&mut self.state.spent_blobs);
        let answered = self.ctx.model(BRAIN, &release, |blobs| {
            let body = materialize(request, blobs)?;
            Ok(json!({
                "call": call.operation(),
                "body": wire::text(&body),
                "until_ms": until,
                "attempt": attempt,
                "reserve": reserve,
            }))
        })?;
        match answered {
            Ok(output) => Ok(self.answered(&output.value)),
            Err(failure) => {
                if failure.code == "cutoff" {
                    self.state.now_ms = self.state.now_ms.max(until);
                }
                if !failure.ended {
                    self.state.spent = self.state.spent.saturating_add(reserve);
                }
                Ok(Err(()))
            }
        }
    }

    /// An answer's value, its cost and price and time taken into the state.
    fn answered(&mut self, reply: &serde_json::Value) -> Result<Value, ()> {
        let state = &mut self.state;
        state.clock(reply);
        if let Some([price_in, price_out]) = reply
            .get("price")
            .and_then(serde_json::Value::as_array)
            .and_then(|price| <&[serde_json::Value; 2]>::try_from(price.as_slice()).ok())
            && let (Some(price_in), Some(price_out)) = (price_in.as_u64(), price_out.as_u64())
        {
            state.price = (price_in, price_out);
        }
        let cost = reply
            .get("cost")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0);
        state.spent = state.spent.saturating_add(cost);
        wire::member(reply, "value").ok_or(())
    }

    /// Appends a step as its outcome arrives, folding the oldest steps first when it would not fit.
    pub(super) fn record(
        &mut self,
        request: &Value,
        step: Value,
        outcome: &str,
        seen: &Seen,
        downloads: Option<&Value>,
    ) -> Result<(), Interrupt> {
        let observation = &seen.observation;
        let entry = object(vec![
            ("step", step),
            ("outcome", text(outcome)),
            (
                "page",
                object(vec![
                    ("url", text(&observation.url)),
                    ("title", text(&observation.title)),
                ]),
            ),
            ("text", text(&observation.text)),
            ("downloads", downloads.cloned().unwrap_or(Value::Null)),
            ("screenshot", Value::Bool(seen.blob.is_some())),
        ]);
        let folding = self.state.thread.to_fold(entry_bytes(&entry));
        if folding > 0 {
            let (first, last) = (
                self.state.thread.number(0),
                self.state.thread.number(folding - 1),
            );
            let memory = self.memory(request, first, last)?;
            self.state.thread.fold(folding, memory);
        }
        self.state.thread.append(entry);
        Ok(())
    }

    /// The agent's whole rewritten memory as steps leave its view; `None` when it could not be written.
    fn memory(
        &mut self,
        request: &Value,
        first: u64,
        last: u64,
    ) -> Result<Option<String>, Interrupt> {
        let request = with(
            request,
            vec![("first", count(first)), ("last", count(last))],
        );
        let Ok(value) = self.ask(BrowserCall::Fold, &request, false, None)? else {
            return Ok(None);
        };
        Ok(match &value {
            Value::Object(members) if members.len() == 1 => value
                .get("memory")
                .and_then(canonical_memory)
                .map(str::to_owned),
            _ => None,
        })
    }

    /// Gives a done answer back to the agent: a `returned` record whose note says what it still needs.
    pub(super) fn give_back(
        &mut self,
        request: &Value,
        seen: &Seen,
        downloads: Option<&Value>,
        note: &str,
    ) -> Result<(), Interrupt> {
        let note: String = note.chars().take(MAX_NOTE_CHARS).collect();
        let step = object(vec![("kind", text("wait")), ("note", text(&note))]);
        self.record(request, step, "returned", seen, downloads)
    }

    /// The completeness check's verdict, or not checked when it could not run or answered outside its form.
    pub(super) fn check(&mut self, summary: &str) -> Result<Verdict, Interrupt> {
        let request = object(vec![
            ("objective", text(&self.state.objective)),
            ("language", text(&self.state.language)),
            ("summary", text(summary)),
        ]);
        let bound = (self.state.objective.len() + self.state.language.len() + summary.len()) as u64
            + PROMPT_TOKENS;
        let Ok(value) = self.ask(BrowserCall::Check, &request, true, Some(bound))? else {
            return Ok(Verdict::default());
        };
        let Some(verdict) = canonical_check(&value) else {
            return Ok(Verdict::default());
        };
        Ok(Verdict {
            complete: match verdict.get("complete") {
                Some(Value::Bool(complete)) => Some(*complete),
                _ => None,
            },
            missing: match verdict.get("missing") {
                Some(Value::String(missing)) => Some(missing.clone()),
                _ => None,
            },
        })
    }
}
