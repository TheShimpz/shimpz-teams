//! How a replayed segment that reached an outcome ends (`local/routine/run.py`): a completed run by its output
//! disposition, unless a person's Stop won it; a paused run frozen for a person or an Integration, its continuation
//! kept first; and the public copy of a frozen run's request, which never shows what the run protects (ADR-0101).

use std::collections::BTreeSet;

use base64::Engine as _;
use strict_json::Value;
use team_action::human::{COPY_FIELDS, OPTION_COPY_FIELDS, retain_unfinished};
use team_chat::orchestrator::Outcome;
use team_protocol::challenge::canonical_rendered;
use team_protocol::python::json::canonical;
use team_protocol::python::value::object_of;
use team_routine::record::{RoutineStateError, Run, TeamRoutines};
use team_routine::runs::{self, Request};
use team_routine::trace::{Protection, exposes};

use super::LeasedRun;
use crate::chat::continuation::encode;
use crate::chat::pending::{PendingLocalChat, Requirements};
use crate::chat::problem;
use crate::http::service::Problem;
use crate::routine::replay::segment::Finished;
use crate::routine::service::RoutineService;
use crate::routine::store::MAX_ROUTINE_PLAINTEXT_BYTES;

/// What a person's copy shows in place of a withheld value.
pub const REDACTED: &str = "[redacted]";

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn code_detail(code: &str) -> Value {
    object_of(vec![("code", text(code))])
}

impl RoutineService {
    /// A replayed run completed every step: its end commits exactly when Stop did not win it. A deadline is no
    /// person's Stop: a run its deadline cut after its sealed cursor completed every step is complete.
    ///
    /// # Errors
    /// A state problem.
    pub(crate) fn finish_run(
        &self,
        run: &LeasedRun<'_>,
        value: &Run,
        shown: Option<&Value>,
    ) -> Result<&'static str, Problem> {
        if run.execution.commit(|| Ok::<(), ()>(())).is_none() {
            let done = self.progress(&run.team_id, value) == crate::routine::replay::Progress::Done;
            if self.deadline_cut(&run.run_id, run.token()) && done {
                return self.complete_sealed(run, shown);
            }
            return self.end_run(&run.team_id, &run.run_id, "stopped", object_of(Vec::new()));
        }
        self.complete(run, value, shown)
    }

    /// Records a run whose every step completed; when its lease or time ran out meanwhile, it failed instead.
    fn complete(
        &self,
        run: &LeasedRun<'_>,
        value: &Run,
        shown: Option<&Value>,
    ) -> Result<&'static str, Problem> {
        let now = self.now();
        let outcome = runs::completed(value);
        let entropy = self.parts.entropy.clone();
        self.update(&run.team_id, |state| {
            if let Ok(next) = runs::finish(
                &state,
                &run.run_id,
                &run.lease,
                now,
                shown,
                entropy.as_ref(),
            ) {
                return (next, outcome);
            }
            let detail = code_detail("lease-expired");
            match runs::end(
                &state,
                &run.run_id,
                now,
                "failed",
                detail,
                None,
                entropy.as_ref(),
            ) {
                Ok(next) => (next, "failed"),
                Err(_) => (state, "failed"),
            }
        })
    }

    /// Records a run its own deadline cut after its sealed cursor completed every step as complete, bound to its
    /// exact lease but not to time left on it; a run whose lease changed since is never touched.
    ///
    /// # Errors
    /// `routine-lease-invalid`, or a state problem.
    pub(crate) fn complete_sealed(
        &self,
        run: &LeasedRun<'_>,
        shown: Option<&Value>,
    ) -> Result<&'static str, Problem> {
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let outcome = self.update(&run.team_id, |state| {
            let current = state
                .runs
                .iter()
                .find(|item| item.run_id == run.run_id)
                .cloned();
            let lease = &run.lease.sha256;
            match runs::complete_recovered(&state, &run.run_id, lease, now, shown, entropy.as_ref())
            {
                Ok(next) => (next, current.map(|current| runs::completed(&current))),
                Err(_) => (state, None),
            }
        })?;
        outcome.ok_or_else(problem::routine_lease_invalid)
    }

    /// A run paused for a person or an Integration at the call `step`: its continuation is kept, then it freezes.
    ///
    /// # Errors
    /// A state problem.
    pub(crate) fn suspended(
        &self,
        run: &LeasedRun<'_>,
        finished: &Finished,
        step: usize,
    ) -> Result<&'static str, Problem> {
        let (continuation, completed) = match &finished.outcome {
            Outcome::Human(paused) => {
                let mut completed = paused.continuation.seen_interrupts.clone();
                completed.extend(paused.completed_interrupts.iter().cloned());
                (paused.continuation.clone(), completed)
            }
            Outcome::Suspended(paused) => (
                paused.continuation.clone(),
                paused.continuation.seen_interrupts.clone(),
            ),
            Outcome::Completed(_) => return Err(problem::internal_error()),
        };
        let pending = PendingLocalChat {
            continuation,
            assistant_ids: run
                .routine
                .assistants
                .iter()
                .map(|(id, _)| id.clone())
                .collect(),
            file_ids: Vec::new(),
            provider: None,
            identity: finished.identity.clone(),
            transcripts: retain_unfinished(&run.transcripts, &completed),
            requests_used: run.requests_used,
            locale: None,
            usage: None,
            paused_batch: finished.paused_batch.clone(),
            recording: None,
            model: None,
            source: None,
        };
        self.freeze(run, &pending, finished, step)
    }

    /// Whether a paused run's human request has no public copy that hides every value its run protects.
    fn unshowable(&self, run_id: &str, finished: &Finished) -> bool {
        let Some(requirement) = &finished.human else {
            return false;
        };
        let protection = self.parts.protections.grow(run_id, std::iter::empty());
        let descriptor = object_of(vec![
            ("request", requirement.request.payload()),
            ("rendered", requirement.copy.rendered.clone()),
        ]);
        public_challenge(&descriptor, &protection).is_none()
    }

    /// Keeps a paused run for a person: its continuation first, then the frozen record commits the freeze under the
    /// guard a Stop cancels under, so a Stop either wins first or finds the run frozen.
    fn freeze(
        &self,
        run: &LeasedRun<'_>,
        pending: &PendingLocalChat,
        finished: &Finished,
        step: usize,
    ) -> Result<&'static str, Problem> {
        let frozen = frozen_request(finished);
        let Some((requirements, assistant_id, action)) =
            frozen.filter(|_| !self.unshowable(&run.run_id, finished))
        else {
            let _ = run.execution.commit(|| Ok::<(), ()>(()));
            let detail = code_detail("request-unavailable");
            return self.end_run(&run.team_id, &run.run_id, "failed", detail);
        };
        let blob = sealed_continuation(&requirements, pending)?;
        self.parts
            .store
            .lock(&run.team_id)
            .and_then(|lock| lock.put_continuation(&run.run_id, &blob))
            .map_err(|_| problem::routine_state_unavailable())?;
        let request = Request {
            kind: requirements.kind().to_owned(),
            assistant_id,
            action,
            position: i64::try_from(step).unwrap_or(i64::MAX),
        };
        let committed = run
            .execution
            .commit(|| self.update(&run.team_id, |state| self.frozen(state, run, &request)));
        match committed {
            Some(outcome) => outcome,
            None => self.end_run(&run.team_id, &run.run_id, "stopped", object_of(Vec::new())),
        }
    }

    fn frozen(
        &self,
        state: TeamRoutines,
        run: &LeasedRun<'_>,
        request: &Request,
    ) -> (TeamRoutines, &'static str) {
        let now = self.now();
        let entropy = self.parts.entropy.as_ref();
        let refused = match runs::freeze(&state, &run.run_id, &run.lease, now, request, entropy) {
            Ok(next) => return (next, "frozen"),
            Err(refused) => refused,
        };
        // The deletion stops every run it saw leased; one reaching its pause meanwhile ends stopped.
        let (outcome, detail) = if refused == RoutineStateError::new("routine-deleting") {
            ("stopped", object_of(Vec::new()))
        } else {
            ("failed", code_detail("freeze-unavailable"))
        };
        match runs::end(&state, &run.run_id, now, outcome, detail, None, entropy) {
            Ok(next) => (next, outcome),
            Err(_) => (state, "failed"),
        }
    }
}

/// The one human or Integration requirement a run may freeze on, or `None` when the pause cannot be answered: an
/// authentication other than a password never freezes.
fn frozen_request(finished: &Finished) -> Option<(Requirements, String, String)> {
    if let Some(requirement) = &finished.human {
        let Outcome::Human(paused) = &finished.outcome else {
            return None;
        };
        let kind = requirement.request.kind.as_str();
        let unanswerable = kind.starts_with("auth:") && kind != "auth:password";
        if canonical(&requirement.request.payload()) != canonical(&paused.request) || unanswerable {
            return None;
        }
        let (assistant_id, action) = (
            requirement.assistant_id.clone(),
            requirement.action_id.clone(),
        );
        return Some((
            Requirements::Human(Box::new(requirement.clone())),
            assistant_id,
            action,
        ));
    }
    let first = finished.integrations.first()?;
    let action = first.action_ids.first()?.clone();
    let assistant_id = first.assistant_id.clone();
    Some((
        Requirements::Integrations(finished.integrations.clone()),
        assistant_id,
        action,
    ))
}

/// A frozen run's sealed continuation: `{"kind", "bindings", "payload"}` as compact ASCII JSON, the payload the
/// chat codec's plaintext within the Routine bound.
fn sealed_continuation(
    requirements: &Requirements,
    pending: &PendingLocalChat,
) -> Result<Vec<u8>, Problem> {
    let (bindings, payload) = encode(requirements, pending, MAX_ROUTINE_PLAINTEXT_BYTES)
        .map_err(|_| problem::routine_state_unavailable())?;
    let encoded = base64::engine::general_purpose::STANDARD.encode(payload);
    let document = object_of(vec![
        ("kind", text(requirements.kind())),
        (
            "bindings",
            Value::Array(bindings.iter().map(|item| text(item)).collect()),
        ),
        ("payload", text(&encoded)),
    ]);
    let options = team_protocol::python::json::Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    Ok(team_protocol::python::json::dumps(&document, options).into_bytes())
}

fn params_of(reference: &mut Value) -> Option<&mut Vec<(String, Value)>> {
    match reference {
        Value::Object(members) => members.iter_mut().find_map(|(name, value)| match value {
            Value::Object(params) if name == "params" => Some(params),
            _ => None,
        }),
        _ => None,
    }
}

fn withhold(reference: &mut Value, protection: &Protection, hidden: &mut BTreeSet<String>) {
    let Some(params) = params_of(reference) else {
        return;
    };
    params.retain(|(_name, value)| match value {
        Value::String(value) if protection.lost || exposes(&text(value), &protection.values) => {
            hidden.insert(value.clone());
            false
        }
        _ => true,
    });
}

/// Withholds every copy parameter holding a protected value, or every text one after a loss; what it hid.
fn withheld(request: &mut Value, protection: &Protection) -> BTreeSet<String> {
    let mut hidden = BTreeSet::new();
    let Value::Object(members) = request else {
        return hidden;
    };
    for (name, value) in members.iter_mut() {
        if COPY_FIELDS.contains(&name.as_str()) {
            withhold(value, protection, &mut hidden);
        }
        if let (true, Value::Array(options)) = (name == "options", value) {
            for option in options.iter_mut() {
                let Value::Object(fields) = option else {
                    continue;
                };
                for (field, reference) in fields.iter_mut() {
                    if OPTION_COPY_FIELDS.contains(&field.as_str()) {
                        withhold(reference, protection, &mut hidden);
                    }
                }
            }
        }
    }
    hidden
}

/// `value` with every occurrence of `hidden` (longest first, leftmost) replaced by [`REDACTED`].
fn redacted(value: &Value, hidden: &[&str]) -> Value {
    match value {
        Value::String(text) if !hidden.is_empty() => Value::String(redact_text(text, hidden)),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| redacted(item, hidden)).collect())
        }
        Value::Object(members) => Value::Object(
            members
                .iter()
                .map(|(name, item)| (name.clone(), redacted(item, hidden)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn redact_text(text: &str, hidden: &[&str]) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(first) = rest.chars().next() {
        if let Some(found) = hidden
            .iter()
            .find(|item| !item.is_empty() && rest.starts_with(**item))
        {
            out.push_str(REDACTED);
            rest = &rest[found.len()..];
        } else {
            out.push(first);
            rest = &rest[first.len_utf8()..];
        }
    }
    out
}

fn without(value: &Value, removed: &str) -> Value {
    match value {
        Value::Object(members) => Value::Object(
            members
                .iter()
                .filter(|(name, _)| name != removed)
                .cloned()
                .collect(),
        ),
        other => other.clone(),
    }
}

fn replaced(value: &Value, updates: Vec<(&str, Value)>) -> Value {
    let Value::Object(members) = value else {
        return value.clone();
    };
    let mut members = members.clone();
    for (name, update) in updates {
        match members.iter_mut().find(|(field, _)| field == name) {
            Some(member) => member.1 = update,
            None => members.push((name.to_owned(), update)),
        }
    }
    Value::Object(members)
}

/// A frozen run's human request as the person may see it, or `None` when nothing shown can hide what it protects.
///
/// Only this public copy changes; the sealed request and its challenge stay exact (ADR-0101). A copy parameter
/// holding a protected value is withheld, as is every text parameter once the run lost its protection, and the
/// rendered copy shows `[redacted]` in its place; the request's fingerprint is that of what is shown. After a loss,
/// no option value or purpose can be shown.
#[must_use]
pub fn public_challenge(descriptor: &Value, protection: &Protection) -> Option<Value> {
    let mut request = without(descriptor.get("request")?, "fingerprint");
    if protection.lost && (request.get("options").is_some() || descriptor.get("purpose").is_some())
    {
        // The run no longer knows what it protects: only reviewed catalog copy and Team's own fields may be shown.
        return None;
    }
    let mut hidden = withheld(&mut request, protection);
    hidden.extend(protection.values.iter().cloned());
    let mut ordered: Vec<&str> = hidden.iter().map(String::as_str).collect();
    ordered.sort_by_key(|item| std::cmp::Reverse(item.len()));
    let rendered = redacted(descriptor.get("rendered")?, &ordered);
    let shown = replaced(
        descriptor,
        vec![("request", request.clone()), ("rendered", rendered)],
    );
    let showable = canonical_rendered(shown.get("rendered")?, &request).is_some();
    if exposes(&shown, &hidden) || !showable {
        return None;
    }
    let fingerprint = assistant_protocol::human_request::fingerprint(&request);
    let request = replaced(&request, vec![("fingerprint", text(&fingerprint))]);
    Some(replaced(&shown, vec![("request", request)]))
}
