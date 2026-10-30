//! The context a definition executes through: every durable operation is a step.
//!
//! On replay a recorded step returns its stored result without running; the step at each position must match the
//! recorded kind, name, and input digest (and an unfinished model step's rebuilt request must match its recorded
//! digest), or the run ends `unrecoverable(nondeterminism)`.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Arc;

use serde_json::{Value, json};

use crate::attempt::{self, Bounds, Ended, Why};
use crate::definition::{Interrupt, Output, StepResult, Wait};
use crate::event::{Event, StepKind};
use crate::failpoint;
use crate::handler::{Activity, ActivityCall, ActivityReply, Attempt, Model, ModelCall, ModelReply};
use crate::history::{State, Step};
use crate::json as canonical;
use crate::policy::{ActivityOptions, Containment, Failure, RetryPolicy, Timeouts};
use crate::session::Session;

/// How many transient blobs one step may produce; blob ids are `step * BLOBS_PER_STEP + index`.
pub const BLOBS_PER_STEP: u64 = 256;

/// The deepest an event wraps a value (`run_completed` → `outcome` → `result`), with a margin of one.
const ENVELOPE_DEPTH: usize = 4;

/// The context of one execution of a definition.
pub struct Ctx<'a> {
    pub(crate) session: &'a mut Session,
    pub(crate) cursor: usize,
    pub(crate) progress: Value,
}

/// Read access to the run's transient blobs while a model request is built.
pub struct Blobs<'a> {
    session: &'a Session,
}

impl Blobs<'_> {
    /// The blob's bytes, or `None` once it was released.
    ///
    /// # Errors
    ///
    /// [`Interrupt::Lost`] when the ledger cannot be read.
    pub fn get(&self, id: u64) -> Result<Option<Vec<u8>>, Interrupt> {
        // An activity's blobs are committed with its result, before any later step can read them.
        Ok(self.session.shared.store.blob(&self.session.run_id, id)?)
    }
}

fn nondeterminism() -> Interrupt {
    Interrupt::Unrecoverable("nondeterminism".into())
}

fn result_of(step: &Step) -> Option<StepResult> {
    match &step.state {
        State::Completed { result, blobs } => {
            Some(StepResult::Ok(Output { value: result.clone(), blobs: blobs.clone() }))
        }
        State::Failed { failure } => Some(StepResult::Failed(failure.clone())),
        State::Uncertain => Some(StepResult::Uncertain),
        // A deferred step's result is read by `deferred`; the others have not finished.
        State::Resolved { .. } | State::Scheduled | State::Running { .. } | State::Waiting { .. } => None,
    }
}

/// A finished model step's result: a model step is never uncertain.
fn model_result_of(step: &Step) -> Option<Result<Output, Failure>> {
    match &step.state {
        State::Completed { result, .. } => Some(Ok(Output { value: result.clone(), blobs: vec![] })),
        State::Failed { failure } => Some(Err(failure.clone())),
        _ => None,
    }
}

fn step_number(index: usize) -> Result<u32, Interrupt> {
    u32::try_from(index).map_err(|_| Interrupt::Limited(crate::LimitKind::History))
}

/// What a model or activity step needs to run its attempts.
struct Spec {
    retry: RetryPolicy,
    timeouts: Timeouts,
    kind: Kind,
}

enum Kind {
    Model { model: Arc<dyn Model>, request: Value, release: Vec<u64> },
    Activity { activity: Arc<dyn Activity>, input: Value, options: ActivityOptions },
}

impl<'a> Ctx<'a> {
    pub(crate) fn new(session: &'a mut Session) -> Self {
        Self { session, cursor: 0, progress: Value::Null }
    }

    /// The run's identifier.
    #[must_use]
    pub fn run_id(&self) -> &str {
        &self.session.run_id
    }

    /// The run's captured input.
    #[must_use]
    pub fn input(&self) -> &Value {
        &self.session.history.start.input
    }

    /// Sets the durable progress a `cancelled`, `limited`, or `expired` outcome carries. Derive it only from the
    /// input and step results, so replay reconstructs it.
    pub fn set_progress(&mut self, progress: Value) {
        self.progress = progress;
    }

    /// The spend committed so far (settled costs and running reservations).
    #[must_use]
    pub fn spent(&self) -> u64 {
        self.session.committed_spend()
    }

    /// Reads a transient blob.
    ///
    /// # Errors
    ///
    /// [`Interrupt::Lost`] when the ledger cannot be read.
    pub fn blob(&self, id: u64) -> Result<Option<Vec<u8>>, Interrupt> {
        Blobs { session: self.session }.get(id)
    }

    /// Positions the next step: checks a recorded one against the call, or records a new one.
    fn position(
        &mut self,
        kind: StepKind,
        name: &str,
        digest: &str,
        input: Option<&Value>,
        policy: &str,
    ) -> Result<usize, Interrupt> {
        let index = self.cursor;
        self.cursor += 1;
        if let Some(recorded) = self.session.history.steps.get(index) {
            let same_digest = kind == StepKind::Model || recorded.digest == digest;
            if recorded.kind != kind || recorded.name != name || !same_digest {
                return Err(nondeterminism());
            }
            if !recorded.is_finished() && recorded.policy != policy {
                return Err(Interrupt::Unrecoverable("definition-changed".into()));
            }
            return Ok(index);
        }
        self.session.gate()?;
        let event = Event::StepScheduled {
            step: step_number(index)?,
            kind,
            name: name.to_owned(),
            digest: digest.to_owned(),
            input: input.cloned(),
            policy: policy.to_owned(),
        };
        self.session.record(event)?;
        Ok(index)
    }

    /// Calls the registered model `name` with the request `build` makes. `release` names the transient blobs whose
    /// last consumer this call is: they are deleted when its result commits.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn model(
        &mut self,
        name: &str,
        release: &[u64],
        build: impl FnOnce(&Blobs<'_>) -> Result<Value, Interrupt>,
    ) -> Result<Result<Output, Failure>, Interrupt> {
        let index = self.cursor;
        if let Some(result) = self.session.history.steps.get(index).and_then(model_result_of) {
            self.position(StepKind::Model, name, "", None, "")?;
            return Ok(result);
        }
        let (model, options) = self
            .session
            .shared
            .models
            .get(name)
            .cloned()
            .ok_or_else(|| Interrupt::Unrecoverable(format!("unknown-model:{name}")))?;
        let request = build(&Blobs { session: self.session })?;
        let digest = canonical::digest(&request);
        if self.session.history.steps.get(index).is_some_and(|recorded| recorded.digest != digest) {
            return Err(nondeterminism());
        }
        self.position(StepKind::Model, name, &digest, None, &options.digest())?;
        let kind = Kind::Model { model, request, release: release.to_vec() };
        self.run_step(index, &Spec { retry: options.retry, timeouts: options.timeouts, kind })?;
        let finished = model_result_of(&self.session.history.steps[index]);
        Ok(finished.unwrap_or_else(|| Err(Failure::permanent("unfinished", "the model step did not finish"))))
    }

    /// Carries out the registered activity `name` with `input`.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn activity(&mut self, name: &str, input: Value) -> Result<StepResult, Interrupt> {
        let registered = self.session.shared.activities.get(name).cloned();
        let policy = registered.as_ref().map_or_else(String::new, |(_, options)| options.digest());
        let index = self.position(StepKind::Activity, name, &canonical::digest(&input), Some(&input), &policy)?;
        if let Some(result) = result_of(&self.session.history.steps[index]) {
            return Ok(result);
        }
        let (activity, options) =
            registered.ok_or_else(|| Interrupt::Unrecoverable(format!("unknown-activity:{name}")))?;
        let spec = Spec {
            retry: options.retry.clone(),
            timeouts: options.timeouts,
            kind: Kind::Activity { activity, input, options },
        };
        self.run_step(index, &spec)?;
        Ok(result_of(&self.session.history.steps[index]).unwrap_or(StepResult::Uncertain))
    }

    /// Runs the pure computation `compute` on `input` once, journaling its result.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn local(
        &mut self,
        name: &str,
        input: &Value,
        compute: impl FnOnce(&Value) -> Result<Value, Failure>,
    ) -> Result<StepResult, Interrupt> {
        let index = self.position(StepKind::Local, name, &canonical::digest(input), Some(input), "")?;
        if let Some(result) = result_of(&self.session.history.steps[index]) {
            return Ok(result);
        }
        let computed = catch_unwind(AssertUnwindSafe(|| compute(input)))
            .unwrap_or_else(|_| Err(Failure::permanent("panic", "the local computation panicked")));
        let step = step_number(index)?;
        let event = match computed.and_then(|value| self.fit(value)) {
            Ok(result) => Event::StepCompleted { step, attempt: 1, result, cost: 0, blobs: vec![], release: vec![] },
            Err(failure) => Event::StepFailed { step, attempt: 1, failure, retry_at_ms: None, cost: 0 },
        };
        self.session.record(event)?;
        Ok(result_of(&self.session.history.steps[index]).unwrap_or(StepResult::Uncertain))
    }

    /// Registers every request durably, then returns their results in order once all are resolved; until then the
    /// run waits ([`Wait::Deferred`]). Resolutions may arrive partially and in any order.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn deferred(&mut self, requests: &[(&str, Value)]) -> Result<Vec<Value>, Interrupt> {
        let results = self.deferred_results(requests)?;
        if results.iter().all(Option::is_some) {
            return Ok(results.into_iter().flatten().collect());
        }
        self.await_deferred()
    }

    /// Registers every request durably, like [`Ctx::deferred`], and returns the answers that already arrived
    /// without waiting for the others.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn deferred_results(&mut self, requests: &[(&str, Value)]) -> Result<Vec<Option<Value>>, Interrupt> {
        let mut results = Vec::with_capacity(requests.len());
        for (name, request) in requests {
            let index = self.position(StepKind::Deferred, name, &canonical::digest(request), Some(request), "")?;
            results.push(match &self.session.history.steps[index].state {
                State::Resolved { result } => Some(result.clone()),
                _ => None,
            });
        }
        Ok(results)
    }

    /// Suspends the run until the host resolves its open deferred requests ([`Wait::Deferred`]).
    ///
    /// # Errors
    ///
    /// Always an [`Interrupt`]: the suspension, or the cancellation or expiry that ends the wait.
    pub fn await_deferred<T>(&mut self) -> Result<T, Interrupt> {
        self.session.gate()?;
        Err(Interrupt::Suspend(Wait::Deferred))
    }

    /// Waits durably until `due_ms`; the wait survives restarts and fires once.
    ///
    /// # Errors
    ///
    /// An [`Interrupt`] to propagate.
    pub fn sleep_until(&mut self, name: &str, due_ms: i64) -> Result<(), Interrupt> {
        let input = json!({ "due_ms": due_ms });
        let index = self.position(StepKind::Timer, name, &canonical::digest(&input), Some(&input), "")?;
        if self.session.history.steps[index].is_finished() {
            return Ok(());
        }
        failpoint::hit("timer.waiting");
        self.session.wait_until(due_ms)?;
        let step = step_number(index)?;
        self.session.record(Event::StepCompleted {
            step,
            attempt: 1,
            result: Value::Null,
            cost: 0,
            blobs: vec![],
            release: vec![],
        })
    }

    /// A result the ledger can hold and read back: within the value bound, and shallow enough to nest inside the
    /// deepest event envelope.
    fn fit(&self, value: Value) -> Result<Value, Failure> {
        if canonical::depth(&value) + ENVELOPE_DEPTH > canonical::MAX_DEPTH {
            return Err(Failure::permanent("result-too-deep", "the step's result nests too deep"));
        }
        match self.session.shared.store.limits().check_value(&canonical::to_canonical(&value)) {
            Ok(()) => Ok(value),
            Err(_) => Err(Failure::permanent("result-too-large", "the step's result is over the value bound")),
        }
    }

    /// Runs attempts until the step finished.
    fn run_step(&mut self, index: usize, spec: &Spec) -> Result<(), Interrupt> {
        let step = step_number(index)?;
        loop {
            let record = &self.session.history.steps[index];
            if record.is_finished() {
                return Ok(());
            }
            match record.state.clone() {
                State::Running { attempt, reserved, .. } => self.interrupted(step, attempt, reserved, spec)?,
                State::Waiting { attempt, retry_at_ms } => {
                    self.session.wait_until(retry_at_ms)?;
                    self.attempt(index, attempt + 1, spec)?;
                }
                _ => self.attempt(index, 1, spec)?,
            }
        }
    }

    /// An attempt an earlier session started and never finished: its fate is unknown.
    fn interrupted(&mut self, step: u32, attempt: u32, reserved: u64, spec: &Spec) -> Result<(), Interrupt> {
        let failure = Failure::retryable("interrupted", "the attempt was interrupted by a crash");
        let event = match &spec.kind {
            Kind::Activity { options, .. } if !options.idempotent => Event::StepUncertain { step, attempt },
            _ => Event::StepFailed {
                step,
                attempt,
                retry_at_ms: self.retry_at(index_of(step), attempt, &spec.retry, &spec.timeouts, &failure),
                failure,
                cost: reserved,
            },
        };
        self.session.record(event)
    }

    fn retry_at(
        &self,
        index: usize,
        attempt: u32,
        retry: &RetryPolicy,
        timeouts: &Timeouts,
        failure: &Failure,
    ) -> Option<i64> {
        let delay = i64::try_from(retry.next_delay(attempt, failure)?).unwrap_or(i64::MAX);
        let at = self.session.now().saturating_add(delay);
        let first = self.session.history.steps.get(index).and_then(|step| step.first_started_ms);
        let closes = timeouts.schedule_to_close_ms.zip(first).map(|(ms, first)| first.saturating_add(to_ms(ms)));
        if closes.is_some_and(|closes| at >= closes) { None } else { Some(at) }
    }

    fn bounds(&self, index: usize, timeouts: &Timeouts) -> Bounds {
        let now = self.session.now();
        let first = self.session.history.steps.get(index).and_then(|step| step.first_started_ms).unwrap_or(now);
        let candidates = [
            timeouts.start_to_close_ms.map(|ms| now.saturating_add(to_ms(ms))),
            timeouts.schedule_to_close_ms.map(|ms| first.saturating_add(to_ms(ms))),
            self.session.history.start.deadline_ms,
        ];
        Bounds { deadline_ms: candidates.into_iter().flatten().min(), heartbeat_ms: timeouts.heartbeat_ms }
    }

    fn attempt(&mut self, index: usize, number: u32, spec: &Spec) -> Result<(), Interrupt> {
        let step = step_number(index)?;
        self.session.gate()?;
        if self.session.shared.abandoned.count() >= self.session.shared.options.max_abandoned {
            let retry = to_ms(self.session.shared.options.proof_retry_ms);
            return Err(Interrupt::Suspend(Wait::Executor(self.session.now().saturating_add(retry))));
        }
        let credentials = self.session.authorize()?;
        // The host's answer may have taken time: the run may have been cancelled or expired meanwhile.
        self.session.gate()?;
        let reserved = match &spec.kind {
            Kind::Model { model, request, .. } => model.reserve(request),
            Kind::Activity { .. } => Some(0),
        };
        let reserved = self.session.reserve(reserved)?;
        let bounds = self.bounds(index, &spec.timeouts);
        let dispatch = crate::store::Dispatch { step, attempt: number, at_ms: self.session.now(), reserved };
        // A slow commit may cross the deadline: the attempt's thread checks it again before it starts any work.
        let fence = self.session.dispatch(dispatch)?;
        let attempt = Attempt {
            run_id: self.session.run_id.clone(),
            step,
            attempt: number,
            fence,
            progress: self.session.history.steps[index].progress.clone(),
            deadline_ms: bounds.deadline_ms,
            abandoned: Arc::default(),
            beats: std::sync::mpsc::sync_channel(1).0,
        };
        match &spec.kind {
            Kind::Model { model, request, release } => {
                let (model, request) = (Arc::clone(model), request.clone());
                let work = Box::new(move |attempt: &Attempt| {
                    model.call(ModelCall { request: &request, attempt, credentials: &credentials })
                });
                let ended = attempt::run(self.session, attempt, &bounds, work);
                failpoint::hit("model.returned");
                self.settle_model(index, number, reserved, release, spec, ended)
            }
            Kind::Activity { activity, input, options } => {
                let (activity, input) = (Arc::clone(activity), input.clone());
                let work = Box::new(move |attempt: &Attempt| {
                    activity.run(ActivityCall { input: &input, attempt, credentials: &credentials })
                });
                let ended = attempt::run(self.session, attempt, &bounds, work);
                failpoint::hit("activity.returned");
                self.settle_activity(index, number, options, spec, ended)
            }
        }
    }

    fn settle_model(
        &mut self,
        index: usize,
        attempt: u32,
        reserved: u64,
        release: &[u64],
        spec: &Spec,
        ended: Ended<ModelReply>,
    ) -> Result<(), Interrupt> {
        let step = step_number(index)?;
        let (returned, why) = split(ended);
        let event = match returned {
            Some((Ok(reply), _)) => match self.fit(reply.response) {
                Ok(result) => Event::StepCompleted {
                    step,
                    attempt,
                    result,
                    cost: reply.cost,
                    blobs: vec![],
                    release: release.to_vec(),
                },
                Err(failure) => self.failed(index, attempt, failure, reply.cost, spec),
            },
            // A call interrupted by cancellation or expiry without an answer is not a failed one: it stays retryable,
            // and the run ends with the interrupt.
            Some((Err(_), _)) | None if matches!(why, Some(Why::Cancelled | Why::Expired)) => {
                let failure = abandoned_failure(why.as_ref());
                let retry_at_ms = Some(self.session.now());
                self.session.record(Event::StepFailed { step, attempt, failure, retry_at_ms, cost: reserved })?;
                return Err(if why == Some(Why::Cancelled) { Interrupt::Cancelled } else { Interrupt::Expired });
            }
            Some((Err(failure), _)) => {
                let cost = if failure.ended { 0 } else { reserved };
                self.failed(index, attempt, failure, cost, spec)
            }
            None => self.failed(index, attempt, abandoned_failure(why.as_ref()), reserved, spec),
        };
        self.conclude(event, why)
    }

    fn settle_activity(
        &mut self,
        index: usize,
        attempt: u32,
        options: &ActivityOptions,
        spec: &Spec,
        ended: Ended<ActivityReply>,
    ) -> Result<(), Interrupt> {
        let step = step_number(index)?;
        let (returned, why) = split(ended);
        let external = options.containment == Containment::External;
        let event = match returned {
            Some((Ok(reply), _)) => self.completed(step, attempt, reply, spec),
            // A non-idempotent attempt may have acted unless its executor confirmed it did not: never repeat it.
            Some((Err(failure), panicked)) if !options.idempotent && (panicked || !failure.ended) => {
                if external {
                    self.session.add_obligation(step, attempt);
                }
                Event::StepUncertain { step, attempt }
            }
            Some((Err(failure), _)) => {
                if external && !failure.ended {
                    self.session.add_obligation(step, attempt);
                }
                self.failed(index, attempt, failure, 0, spec)
            }
            None => {
                self.session.add_obligation(step, attempt);
                if options.idempotent {
                    self.failed(index, attempt, abandoned_failure(why.as_ref()), 0, spec)
                } else {
                    Event::StepUncertain { step, attempt }
                }
            }
        };
        self.conclude(event, why)?;
        self.session.flush_receipt()?;
        failpoint::hit("activity.committed");
        Ok(())
    }

    fn completed(&mut self, step: u32, attempt: u32, reply: ActivityReply, spec: &Spec) -> Event {
        let index = index_of(step);
        match self.fit(reply.result) {
            Ok(result) => {
                // Transient blobs are extras (a screenshot): the ones past their bounds are left out, never the result.
                let (max_blobs, max_bytes) = {
                    let limits = self.session.shared.store.limits();
                    (limits.max_blobs_per_run, limits.max_blob_bytes)
                };
                let room =
                    usize::try_from(max_blobs).unwrap_or(usize::MAX).saturating_sub(self.session.history.held_blobs());
                let base = u64::from(step) * BLOBS_PER_STEP;
                let kept = reply.blobs.into_iter().filter(|blob| blob.len() <= max_bytes);
                let mut ids = Vec::new();
                for (offset, data) in (0_u64..BLOBS_PER_STEP).zip(kept).take(room) {
                    self.session.add_blob(base + offset, data);
                    ids.push(base + offset);
                }
                Event::StepCompleted { step, attempt, result, cost: 0, blobs: ids, release: vec![] }
            }
            Err(failure) => self.failed(index, attempt, failure, 0, spec),
        }
    }

    fn failed(&self, index: usize, attempt: u32, failure: Failure, cost: u64, spec: &Spec) -> Event {
        let retry_at_ms = self.retry_at(index, attempt, &spec.retry, &spec.timeouts, &failure);
        Event::StepFailed { step: u32::try_from(index).unwrap_or(u32::MAX), attempt, failure, retry_at_ms, cost }
    }

    /// Records the attempt's event. A cancellation or an expiry that made the session give up is raised by the next
    /// step's gate, so the definition first sees what this attempt returned (and its progress keeps it).
    fn conclude(&mut self, event: Event, why: Option<Why>) -> Result<(), Interrupt> {
        if let Some(Why::Lost(interrupt)) = why {
            return Err(interrupt);
        }
        self.session.record(event)
    }
}

fn index_of(step: u32) -> usize {
    usize::try_from(step).unwrap_or(usize::MAX)
}

fn to_ms(ms: u64) -> i64 {
    i64::try_from(ms).unwrap_or(i64::MAX)
}

type Returned<R> = Option<(Result<R, Failure>, bool)>;

/// Splits how an attempt ended. A failure an abandoned attempt returned within its grace period is the session's
/// own reason for abandoning it (keeping whether the executor confirmed it ended), not the handler's reaction.
fn split<R>(ended: Ended<R>) -> (Returned<R>, Option<Why>) {
    match ended {
        Ended::Returned { result, panicked } => (Some((result, panicked)), None),
        Ended::Abandoned { why, returned: Some((Err(failure), panicked)) } => {
            let mut reason = abandoned_failure(Some(&why));
            reason.ended = failure.ended;
            (Some((Err(reason), panicked)), Some(why))
        }
        Ended::Abandoned { why, returned } => (returned, Some(why)),
    }
}

fn abandoned_failure(why: Option<&Why>) -> Failure {
    match why {
        Some(Why::Heartbeat) => Failure::retryable("heartbeat-timeout", "the attempt stopped reporting progress"),
        Some(Why::Cancelled) => Failure::permanent("cancelled", "the run was cancelled"),
        Some(Why::Expired) => Failure::permanent("expired", "the run's deadline passed"),
        _ => Failure::retryable("timeout", "the attempt ran out of time"),
    }
}
