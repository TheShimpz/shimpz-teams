//! The task's loop over its run's context: observe, decide, act, record, and finish, each Browser or Brain step one
//! journaled step.

use std::fmt::Write as _;

use holdpoint::{Ctx, Interrupt, StepResult};
use serde_json::json;
use strict_json::Value;
use team_protocol::browser::{MAX_STEPS, canonical_step};

use super::handlers::{ACT, Mode, OBSERVE, OPEN, READ};
use super::state::{Seen, State};
use super::wire::{self, int, string};
use super::{
    INCOMPLETE_NOTE, MAX_RECOVERIES, MAX_RETURNS_INCOMPLETE, MAX_RETURNS_UNSCREENED, Outcome,
    REPLACED_NOTICE, RESTARTED_NOTICE, SCREENSHOT_EVERY, Status, TaskInput, UNSCREENED_NOTE,
    WAIT_MS, as_str, object, text,
};
use crate::brain::BrowserCall;
use crate::cua::Observation;
use crate::read;

/// The step the Brain chose, or how the task ended instead.
enum Decision {
    Step(Value),
    Ended(Outcome),
}

/// What a turn's observation came to.
enum Observed {
    /// The page as the agent sees it, and its downloads.
    Page(Seen, Option<Value>),
    /// The task ended.
    Ended(Outcome),
    /// The Browser was repaired; the next step looks again.
    Recovered,
}

/// One observation activity's result.
enum Looked {
    Page(Seen, Option<Value>),
    Lost(String),
}

/// The task's loop over its run.
pub(super) struct Loop<'c, 'a> {
    pub(super) ctx: &'c mut Ctx<'a>,
    pub(super) state: State,
}

impl<'c, 'a> Loop<'c, 'a> {
    pub(super) fn new(ctx: &'c mut Ctx<'a>, input: &TaskInput) -> Self {
        Self {
            ctx,
            state: State::new(input),
        }
    }

    /// Runs the task to its end.
    pub(super) fn run(mut self) -> Result<Outcome, Interrupt> {
        if let Some(ended) = self.start()? {
            return Ok(ended);
        }
        for index in 0..MAX_STEPS {
            self.progress(index);
            if let Some(ended) = self.turn(index)? {
                return Ok(self.state.counted(ended));
            }
        }
        let ended = self.finish_from_thread(MAX_STEPS, None)?;
        Ok(self.state.counted(ended))
    }

    fn progress(&mut self, steps: usize) {
        self.ctx.set_progress(json!({
            "steps": steps,
            "recovered": self.state.recovered,
            "relaunched": self.state.relaunched,
        }));
    }

    /// The task's first Browser; how the task ends when none opened.
    fn start(&mut self) -> Result<Option<Outcome>, Interrupt> {
        self.progress(0);
        Ok(match self.open(Mode::Start)? {
            Ok(_) => None,
            Err(code) => Some(Outcome::failed(&code, 0, None)),
        })
    }

    /// `browser.open`: what it did, or the closed code of why nothing opened.
    fn open(&mut self, mode: Mode) -> Result<Result<(bool, bool), String>, Interrupt> {
        let input = json!({
            "team_id": self.state.team_id,
            "language": self.state.language,
            "mode": mode.as_str(),
            "open": self.state.last_good,
        });
        Ok(match self.ctx.activity(OPEN, input)? {
            StepResult::Ok(output) => {
                self.state.clock(&output.value);
                let flag = |name: &str| {
                    output
                        .value
                        .get(name)
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false)
                };
                Ok((flag("relaunched"), flag("replaced")))
            }
            StepResult::Failed(failure) => Err(closed(&failure.code)),
            StepResult::Uncertain => Err("browser-lost".to_owned()),
        })
    }

    /// Observes, asks for one step, and acts on it; the outcome that ends the task, or `None` to continue.
    fn turn(&mut self, index: usize) -> Result<Option<Outcome>, Interrupt> {
        if self.state.finishing() {
            return Ok(Some(self.finish_from_thread(index, None)?));
        }
        let (seen, downloads) = match self.observe(index)? {
            Observed::Page(seen, downloads) => (seen, downloads),
            Observed::Ended(outcome) => return Ok(Some(outcome)),
            Observed::Recovered => return Ok(None),
        };
        let request = self.state.request(&seen, downloads.as_ref());
        let decision = match self.decide(index, &request)? {
            Decision::Step(decision) => decision,
            Decision::Ended(outcome) => return Ok(Some(outcome)),
        };
        Ok(match as_str(decision.get("kind")) {
            "done" => {
                let summary = as_str(decision.get("summary")).to_owned();
                self.finish(index, &request, &seen, downloads.as_ref(), summary)?
            }
            "needs_person" => Some(Outcome {
                reason: Some(as_str(decision.get("reason")).to_owned()),
                sentence: Some(as_str(decision.get("sentence")).to_owned()),
                ..Outcome::new(Status::NeedsPerson).steps(index + 1)
            }),
            _ => self.act(index, &request, &decision, &seen, downloads.as_ref())?,
        })
    }

    /// One `browser.observe`.
    fn look(&mut self, screenshot: bool) -> Result<Looked, Interrupt> {
        let result = self
            .ctx
            .activity(OBSERVE, json!({ "screenshot": screenshot }))?;
        let StepResult::Ok(output) = result else {
            return Ok(Looked::Lost("observe-failed".to_owned()));
        };
        let value = &output.value;
        self.state.clock(value);
        if let Some(code) = string(value, "lost") {
            return Ok(Looked::Lost(closed(code)));
        }
        let observation = wire::member(value, "observation")
            .and_then(|observation| wire::read_observation(&observation));
        let Some(observation) = observation else {
            return Ok(Looked::Lost("observe-invalid".to_owned()));
        };
        let screenshot = value
            .get("screenshot")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let blob = output.blobs.first().copied().filter(|_| screenshot);
        let downloads = wire::member(value, "downloads");
        Ok(Looked::Page(Seen { observation, blob }, downloads))
    }

    /// The current page, back on the last page that loaded after an error page, with any read pages joined.
    fn observe(&mut self, index: usize) -> Result<Observed, Interrupt> {
        let mut notice = self.state.notice.take().unwrap_or_default().to_owned();
        let screenshot = self.state.wants_screenshot;
        let (mut seen, mut downloads) = match self.look(screenshot)? {
            Looked::Page(seen, downloads) => (seen, downloads),
            Looked::Lost(code) => return self.lost(index, &format!("observe/{code}")),
        };
        if let Some(last_good) = self.state.last_good.clone()
            && seen.observation.url.starts_with("chrome-error://")
        {
            let back = json!({ "kind": "navigate", "url": last_good });
            if let Err(cause) = self.page(&back)? {
                return self.lost(index, &format!("observe/{cause}"));
            }
            let title = seen.observation.title.clone();
            (seen, downloads) = match self.look(screenshot)? {
                Looked::Page(seen, downloads) => (seen, downloads),
                Looked::Lost(code) => return self.lost(index, &format!("observe/{code}")),
            };
            // Writing to a String cannot fail.
            let _ = writeln!(
                notice,
                "The last page you opened ({title}) did not load; you are back on {last_good}."
            );
        }
        let state = &mut self.state;
        let url = &seen.observation.url;
        if url.starts_with("https://") || url.starts_with("http://") {
            state.last_good = Some(url.clone());
        }
        if state.wants_screenshot {
            state.screenshot_at = i64::try_from(index).unwrap_or(i64::MAX);
            state.wants_screenshot = false;
        }
        seen.observation = read::shown(&seen.observation, &notice, &state.section);
        state.section.clear();
        state
            .screen
            .observed(&seen.observation.url, &seen.observation.text);
        Ok(Observed::Page(seen, downloads))
    }

    /// The Browser is lost: repaired, the next step looks again; otherwise the task ends.
    fn lost(&mut self, index: usize, cause: &str) -> Result<Observed, Interrupt> {
        Ok(match self.recover(index, cause.to_owned())? {
            Some(outcome) => Observed::Ended(outcome),
            None => Observed::Recovered,
        })
    }

    /// One page action through `browser.act`: its outcome, or the cause of the failure that interrupted it, and
    /// whether anything may have been done on the page.
    fn page(&mut self, step: &serde_json::Value) -> Result<Result<String, String>, Interrupt> {
        Ok(match self.ctx.activity(ACT, step.clone())? {
            StepResult::Ok(output) => {
                self.state.clock(&output.value);
                match string(&output.value, "lost") {
                    Some(code) => Err(closed(code)),
                    None => Ok(string(&output.value, "outcome")
                        .map_or_else(|| "refused".to_owned(), closed)),
                }
            }
            StepResult::Failed(failure) => Err(closed(&failure.code)),
            StepResult::Uncertain => Err("uncertain".to_owned()),
        })
    }

    /// The step the Brain chose, admitted only while the task may still act on it, or how the task ends.
    fn decide(&mut self, index: usize, request: &Value) -> Result<Decision, Interrupt> {
        let reserve = self.state.reserve(request);
        if !self.state.affords(reserve, false) {
            return Ok(Decision::Ended(
                self.finish_from_thread(index, Some(request))?,
            ));
        }
        let raw = match self.ask(BrowserCall::Step, request, false, None)? {
            Ok(raw) => raw,
            Err(()) => match self.retry(index, request, reserve)? {
                Decision::Step(raw) => raw,
                ended @ Decision::Ended(_) => return Ok(ended),
            },
        };
        // The soft cutoff passed while the Brain answered: no step is taken past it.
        if self.state.finishing() {
            return Ok(Decision::Ended(
                self.finish_from_thread(index + 1, Some(request))?,
            ));
        }
        Ok(canonical_step(&raw).map_or_else(
            || Decision::Ended(Outcome::failed("step-invalid", index + 1, None)),
            Decision::Step,
        ))
    }

    /// A model answers outside its form now and then: the step is asked once more, under its own reservation, unless
    /// the normal spend share can no longer reserve it, when the task finishes instead.
    fn retry(
        &mut self,
        index: usize,
        request: &Value,
        reserve: u64,
    ) -> Result<Decision, Interrupt> {
        if self.state.finishing() {
            return Ok(Decision::Ended(self.unanswered(index, request)?));
        }
        if !self.state.affords(reserve, false) {
            return Ok(Decision::Ended(
                self.finish_from_thread(index, Some(request))?,
            ));
        }
        Ok(match self.ask(BrowserCall::Step, request, false, None)? {
            Ok(raw) => Decision::Step(raw),
            Err(()) => Decision::Ended(self.unanswered(index, request)?),
        })
    }

    /// How a step the Brain did not answer ends the task: past the soft cutoff the task finishes from its thread.
    fn unanswered(&mut self, index: usize, request: &Value) -> Result<Outcome, Interrupt> {
        if self.state.finishing() {
            return self.finish_from_thread(index, Some(request));
        }
        Ok(Outcome::failed("model-unavailable", index, None))
    }

    /// Runs one admitted page step and records its outcome; the outcome that ends the task, or `None`.
    fn act(
        &mut self,
        index: usize,
        request: &Value,
        decision: &Value,
        seen: &Seen,
        downloads: Option<&Value>,
    ) -> Result<Option<Outcome>, Interrupt> {
        let kind = as_str(decision.get("kind"));
        let reference = as_str(decision.get("ref"));
        let observation = &seen.observation;
        let (outcome, failure) = match kind {
            "screenshot" => {
                let at = i64::try_from(index).unwrap_or(i64::MAX);
                self.state.wants_screenshot = at - self.state.screenshot_at >= SCREENSHOT_EVERY;
                let granted = if self.state.wants_screenshot {
                    "ok"
                } else {
                    "refused"
                };
                (granted.to_owned(), None)
            }
            "wait" => {
                let due = self.state.now_ms.saturating_add(WAIT_MS);
                self.ctx.sleep_until(&format!("wait-{index}"), due)?;
                self.state.now_ms = self.state.now_ms.max(due);
                ("ok".to_owned(), None)
            }
            "read" => self.read(decision, observation)?,
            _ if !offered(kind, reference, observation) => ("refused".to_owned(), None),
            _ => match self.page(&page_step(decision))? {
                Ok(outcome) => (outcome, None),
                // The Browser was found unusable before the action started: nothing was done on the page.
                Err(cause) if cause == "browser-lost" || cause == "not-intact" => {
                    ("refused".to_owned(), Some(format!("act/{cause}")))
                }
                Err(cause) => ("interrupted".to_owned(), Some(format!("act/{cause}"))),
            },
        };
        self.record(request, decision.clone(), &outcome, seen, downloads)?;
        match failure {
            None => Ok(None),
            Some(cause) => self.recover(index + 1, cause),
        }
    }

    /// A `read` step: its outcome, and the cause of a Driver failure that ended it.
    fn read(
        &mut self,
        decision: &Value,
        observation: &Observation,
    ) -> Result<(String, Option<String>), Interrupt> {
        let input = json!({
            "decision": wire::text(decision),
            "observation": wire::text(&wire::observation(observation)),
            "until_ms": self.state.cutoff_ms(),
        });
        let output = match self.ctx.activity(READ, input)? {
            StepResult::Ok(output) => output,
            StepResult::Failed(failure) => {
                return Ok((
                    "interrupted".to_owned(),
                    Some(format!("act/{}", closed(&failure.code))),
                ));
            }
            StepResult::Uncertain => {
                return Ok(("interrupted".to_owned(), Some("act/uncertain".to_owned())));
            }
        };
        let value = &output.value;
        self.state.clock(value);
        if let Some(serde_json::Value::Array(seen)) = value.get("seen") {
            for page in seen {
                if let (Some(url), Some(body)) = (
                    page.get(0).and_then(serde_json::Value::as_str),
                    page.get(1).and_then(serde_json::Value::as_str),
                ) {
                    self.state.screen.observed(url, body);
                }
            }
        }
        string(value, "section")
            .unwrap_or_default()
            .clone_into(&mut self.state.section);
        let outcome = string(value, "outcome").map_or_else(|| "refused".to_owned(), closed);
        let clicked = value
            .get("clicked")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(true);
        Ok(match string(value, "lost") {
            None => (outcome, None),
            Some(code) if clicked => (
                "interrupted".to_owned(),
                Some(format!("act/{}", closed(code))),
            ),
            Some(code) => ("refused".to_owned(), Some(format!("act/{}", closed(code)))),
        })
    }

    /// A done answer stands once screened and checked, or goes back to the agent with what it still needs.
    fn finish(
        &mut self,
        index: usize,
        request: &Value,
        seen: &Seen,
        downloads: Option<&Value>,
        summary: String,
    ) -> Result<Option<Outcome>, Interrupt> {
        let unscreened = self.state.screen.unscreened(&summary);
        if !unscreened.is_empty() && self.state.returned_unscreened < MAX_RETURNS_UNSCREENED {
            self.state.returned_unscreened += 1;
            let note = format!("{UNSCREENED_NOTE}{}", unscreened.join(", "));
            self.give_back(request, seen, downloads, &note)?;
            return Ok(None);
        }
        let verdict = self.check(&summary)?;
        if verdict.complete == Some(false)
            && self.state.returned_incomplete < MAX_RETURNS_INCOMPLETE
        {
            self.state.returned_incomplete += 1;
            let note = format!("{INCOMPLETE_NOTE}{}", verdict.missing.unwrap_or_default());
            self.give_back(request, seen, downloads, &note)?;
            return Ok(None);
        }
        Ok(Some(super::state::done(
            summary,
            index + 1,
            unscreened,
            &verdict,
        )))
    }

    /// Past the soft cutoff: the Brain answers from the thread once, never another step; else the budget ends it.
    fn finish_from_thread(
        &mut self,
        index: usize,
        request: Option<&Value>,
    ) -> Result<Outcome, Interrupt> {
        let request = if let Some(request) = request {
            request.clone()
        } else {
            let (seen, downloads) = match self.observe(index)? {
                Observed::Page(seen, downloads) => (seen, downloads),
                Observed::Ended(outcome) => return Ok(outcome),
                Observed::Recovered => return Ok(Outcome::new(Status::Budget).steps(index)),
            };
            self.state.request(&seen, downloads.as_ref())
        };
        let Ok(value) = self.ask(BrowserCall::Final, &request, true, None)? else {
            return Ok(Outcome::new(Status::Budget).steps(index));
        };
        let summary = match &value {
            Value::Object(members) if members.len() == 1 => value.get("summary"),
            _ => None,
        };
        let Some(Value::String(summary)) = summary else {
            return Ok(Outcome::new(Status::Budget).steps(index));
        };
        if canonical_step(&object(vec![
            ("kind", text("done")),
            ("summary", text(summary)),
        ]))
        .is_none()
        {
            return Ok(Outcome::new(Status::Budget).steps(index));
        }
        let unscreened = self.state.screen.unscreened(summary);
        Ok(super::state::done(
            summary.clone(),
            index,
            unscreened,
            &super::state::Verdict::default(),
        ))
    }

    /// Repairs the Browser while the task may continue; how the task ends when it cannot.
    fn recover(&mut self, steps: usize, cause: String) -> Result<Option<Outcome>, Interrupt> {
        if self.state.recovered >= MAX_RECOVERIES {
            return Ok(Some(Outcome::failed("browser-lost", steps, Some(cause))));
        }
        // A page Chromium no longer reads, through any Driver session, needs Chromium itself started again.
        let mode = if cause.ends_with("/browser-read-refused") {
            Mode::Relaunch
        } else {
            Mode::Reopen
        };
        Ok(match self.open(mode)? {
            Err(code) => Some(Outcome::failed(
                "browser-lost",
                steps,
                Some(format!("{cause}/reopen/{code}")),
            )),
            Ok((relaunched, replaced)) => {
                self.state.recovered += 1;
                if replaced {
                    self.state.relaunched += 1;
                    self.state.notice = Some(REPLACED_NOTICE);
                } else if relaunched {
                    self.state.relaunched += 1;
                    self.state.notice = Some(RESTARTED_NOTICE);
                }
                None
            }
        })
    }
}

/// A closed code from a ledger string: lowercase letters, digits, and dashes, at most 64.
fn closed(code: &str) -> String {
    code.chars()
        .filter(|character| {
            character.is_ascii_lowercase() || character.is_ascii_digit() || *character == '-'
        })
        .take(64)
        .collect()
}

/// The activity input of one admitted page step: only its kind and the fields that kind carries.
fn page_step(decision: &Value) -> serde_json::Value {
    let field = |name: &str| as_str(decision.get(name)).to_owned();
    match as_str(decision.get("kind")) {
        "navigate" => json!({ "kind": "navigate", "url": field("url") }),
        "click" => json!({ "kind": "click", "ref": field("ref") }),
        "type" => json!({ "kind": "type", "ref": field("ref"), "text": field("text") }),
        "scroll" => json!({ "kind": "scroll", "direction": field("direction") }),
        other => json!({ "kind": other }),
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

/// The time a step's result says it was taken at.
pub(super) fn now_of(value: &serde_json::Value) -> Option<i64> {
    int(value, "now_ms")
}
