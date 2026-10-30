//! A recorded Routine run, executed step by step with no model (`local/routine/replay.py`, ADR-0092 sections 3, 5,
//! and 6; ADR-0101).
//!
//! The run drives the same turn engine a chat uses, but its turns come from the confirmed plan instead of the Brain:
//! each step is one Action request whose input is resolved from literals, the run's one start instant, and values
//! earlier steps of the same run returned. Its recovery snapshot is sealed before the first dispatch; its sealed cursor
//! records each dispatch's logical operation and exact input before the RPC, and keeps each completed step's selected
//! values before the journal may drop its receipts, so a completed step never runs again. A healthy run makes no model
//! call and needs no model-provider key.
//!
//! The run's protection is bound with its first cursor and grows before every RPC and with every result's secret
//! strings before anything outward sees it; a run that loses it shows nothing it produced afterwards.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Instant;

use strict_json::{Number, Value};
use team_action::executor::ActionRequest as ExecutorRequest;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_inference::brain::BrainError;
use team_inference::runtime::{
    ActionRequest, ConversationEntry, Runtime, RuntimeContext, RuntimeTurn, TurnStatus,
};
use team_protocol::python::value::object_of;
use team_protocol::routine::{MAX_STEP_VIEW_BYTES, encoded_bytes};
use team_routine::claim::network_of;
use team_routine::cursor::{self, Binding, Cursor};
use team_routine::plan::{
    self, Plan, Step, input_preview, output_compared, output_safe, output_shown, output_state,
};
use team_routine::record::{self, Routine, Run, TeamRoutines};
use team_routine::runs;
use team_routine::trace::{self, Protection};

use super::diagnostics::{self, Diagnostic, Fault, RunBinding, StepRecord};
use super::executor::{DispatchHooks, Failed, Refused, RoutineJournal};
use super::incident::{Recovery, snapshot_locked};
use super::protection::RunProtections;
use super::service::RoutineService;
use super::store::TeamLock;

pub mod execute;
pub mod segment;

/// The private values each operation's last failed attempt was given, by operation id: held in memory only, for this
/// execution's one automatic recovery episode (ADR-0092).
pub type Protected = Arc<Mutex<BTreeMap<String, Vec<String>>>>;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn int(value: usize) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// The dispatched step's attempt, in memory only: its position, start, exact input, and the values it protects.
struct Attempt {
    position: usize,
    started: Instant,
    input: Value,
    protected: Vec<String>,
    ended: Option<Instant>,
}

struct Replay {
    cursor: Cursor,
    attempt: Option<Attempt>,
    failure: Option<&'static str>,
}

/// The Brain's turn interface, answered from the plan, and the hooks that seal each dispatch; it never calls a model.
pub struct ReplayRuntime {
    service: RoutineService,
    team_id: String,
    plan: Plan,
    reply: String,
    stop: Stop,
    protected: Protected,
    state: Mutex<Replay>,
}

impl std::fmt::Debug for ReplayRuntime {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ReplayRuntime")
    }
}

/// The interrupt of step `index`.
#[must_use]
pub fn interrupt(index: usize) -> String {
    format!("routine-step-{index}")
}

fn turn_of(status: TurnStatus, reply: String, actions: Vec<ActionRequest>) -> RuntimeTurn {
    RuntimeTurn {
        status,
        reply,
        actions,
        clarification: None,
        memory: Vec::new(),
        routine: None,
        browser: None,
    }
}

impl ReplayRuntime {
    fn replay(&self) -> MutexGuard<'_, Replay> {
        lock(&self.state)
    }

    /// The run's sealed cursor as this runtime last sealed it.
    #[must_use]
    pub fn cursor(&self) -> Cursor {
        self.replay().cursor.clone()
    }

    /// The admitted plan it runs.
    #[must_use]
    pub const fn plan(&self) -> &Plan {
        &self.plan
    }

    /// Why a step could not be resolved or advanced, the first such reason; the run is never retried for it.
    #[must_use]
    pub fn failure(&self) -> Option<&'static str> {
        self.replay().failure
    }

    fn fail(&self, code: &'static str) -> &'static str {
        self.replay().failure.get_or_insert(code);
        code
    }

    fn seal(&self, cursor: Cursor) -> Result<(), &'static str> {
        let store = &self.service.parts.store;
        store
            .lock(&self.team_id)
            .and_then(|lock| lock.put_cursor(&cursor))
            .map_err(|_| self.fail("routine-cursor-unavailable"))?;
        self.replay().cursor = cursor;
        Ok(())
    }

    /// The run's protection now; one that is not held in this process is lost.
    pub fn protection(&self) -> Protection {
        self.protect(&[])
    }

    /// Protects more of the run's values; a protection that saturates or is missing is lost, and the run says so.
    fn protect(&self, values: &[String]) -> Protection {
        let current = self.cursor();
        let protections = &self.service.parts.protections;
        let found = protections.grow(&current.binding.run_id, values.iter().map(String::as_str));
        if found.lost && !current.protection_lost {
            match cursor::lose_protection(&current) {
                Ok(lost) => {
                    let _sealed = self.seal(lost);
                }
                Err(error) => {
                    self.fail(error.code);
                }
            }
            if self
                .service
                .mark_lost(&self.team_id, &current.binding.run_id)
                .is_err()
            {
                self.fail("routine-state-unavailable");
            }
        }
        found
    }

    fn turn(&self) -> Result<RuntimeTurn, &'static str> {
        let current = self.cursor();
        if current.done(&self.plan) {
            return Ok(turn_of(
                TurnStatus::Completed,
                self.reply.clone(),
                Vec::new(),
            ));
        }
        let step = &self.plan.steps[current.step];
        let selected = current.selections();
        // The orchestrator then validates this exact input against the Action's reviewed schema before dispatch.
        let resolved = plan::resolve(&self.plan, step, &selected, current.started_at, |_| Ok(()))
            .map_err(|error| self.fail(error.code))?;
        let request = ActionRequest {
            interrupt_id: interrupt(current.step),
            assistant_id: step.assistant_id.clone(),
            action: step.action.clone(),
            input: resolved,
        };
        Ok(turn_of(
            TurnStatus::ActionRequired,
            String::new(),
            vec![request],
        ))
    }

    /// The segment's first turn; a carried operation is retried only after proven absence, once per run.
    fn begin(&self) -> Result<RuntimeTurn, &'static str> {
        let current = self.cursor();
        if current.episode.carried() {
            let retried = cursor::retry(&current).map_err(|error| self.fail(error.code))?;
            self.seal(retried)?;
        }
        self.turn()
    }

    /// Seals the completed step's selected values, before the journal may drop its receipts, then goes on.
    fn advanced(&self, results: &Value) -> Result<RuntimeTurn, &'static str> {
        let dispatched = self.cursor();
        let step = self
            .plan
            .steps
            .get(dispatched.step)
            .ok_or_else(|| self.fail("cursor-not-dispatched"))?;
        let result = results
            .get(&interrupt(dispatched.step))
            .cloned()
            .unwrap_or(Value::Null);
        // Every secret string the result holds is protected before anything of it is kept or shown.
        let secrets = trace::secret_values(&result, &step.output_schema)
            .map_err(|error| self.fail(error.code))?;
        let protection = self.protect(&secrets);
        if let Some(code) = self.failure() {
            return Err(code);
        }
        let completed = (&result, &protection);
        let next = advance(
            &self.service,
            &self.team_id,
            &dispatched,
            &self.plan,
            completed,
        )
        .map_err(|code| self.fail(code))?;
        self.seal(next)?;
        self.record_step("done", Some(&dispatched));
        self.turn()
    }

    /// Seals what the dispatched attempt did, once, after it ended; a record that cannot be kept is audited only.
    ///
    /// A step record is a display record, never safety evidence. A `stopped` attempt was cut by Stop or the run's
    /// deadline, and says nothing about whether it acted.
    pub fn record_step(&self, status: &str, cursor: Option<&Cursor>) {
        let Some(attempt) = self.replay().attempt.take() else {
            return;
        };
        let current = cursor.cloned().unwrap_or_else(|| self.cursor());
        let protection = self.protection();
        let mut protected = attempt.protected.clone();
        protected.extend(protection.values.iter().cloned());
        let record = self.step_record(status, &attempt, &current, (&protection, &protected));
        let diagnostics = &self.service.parts.diagnostics;
        let binding = &current.binding;
        let kept =
            diagnostics.record_step(&self.team_id, &binding.incarnation, &record, &protected);
        if kept.is_err() {
            let run_id = binding.run_id.clone();
            self.service
                .audit_later("routine-step-record", &self.team_id, &run_id);
        }
    }

    fn step_record(
        &self,
        status: &str,
        attempt: &Attempt,
        current: &Cursor,
        protection: (&Protection, &[String]),
    ) -> StepRecord {
        let (protection, protected) = protection;
        let step = &self.plan.steps[attempt.position - 1];
        let inputs = (!protection.lost).then(|| {
            input_preview(
                &self.plan,
                step,
                &attempt.input,
                &current.selections(),
                protected,
            )
        });
        let binding = &current.binding;
        let ended = attempt.ended.unwrap_or_else(Instant::now);
        // A wait has no end yet, and a verified occurrence's time is verification's, never the Action's own.
        let elapsed = ended.saturating_duration_since(attempt.started).as_millis();
        let duration_ms = (!matches!(status, "waiting" | "recovered"))
            .then(|| i64::try_from(elapsed).unwrap_or(i64::MAX));
        let mut record = StepRecord {
            binding: run_binding(binding, &self.plan),
            position: i64::try_from(attempt.position).unwrap_or(i64::MAX),
            assistant_id: step.assistant_id.clone(),
            action: step.action.clone(),
            status: status.to_owned(),
            attempt: current.attempts.max(1),
            duration_ms,
            recorded_at: self.service.now(),
            inputs,
        };
        if encoded_bytes(&object_of(record.view())) > MAX_STEP_VIEW_BYTES {
            record.inputs = None;
        }
        record
    }

    /// Seals the dispatch's logical operation, exact input, and workload before its RPC.
    fn dispatched(
        &self,
        request: &ExecutorRequest,
        operation_id: &str,
        workload: &str,
        protected: &[String],
    ) -> Result<(), &'static str> {
        let current = self.cursor();
        if request.interrupt_id != interrupt(current.step) {
            return Err(self.fail("routine-step-changed"));
        }
        let commitment = plan::commitment(&request.input);
        let now = self.service.now();
        let dispatched = cursor::dispatch(
            &current,
            &self.plan,
            operation_id,
            &commitment,
            workload,
            now,
        )
        .map_err(|error| self.fail(error.code))?;
        self.seal(dispatched)?;
        self.protect(protected);
        self.replay().attempt = Some(Attempt {
            position: current.step + 1,
            started: Instant::now(),
            input: request.input.clone(),
            protected: Vec::new(),
            ended: None,
        });
        Ok(())
    }

    /// The dispatched Action returned or raised: its duration ends here, before any Team work on its result.
    fn returned_now(&self) {
        if let Some(attempt) = self.replay().attempt.as_mut() {
            attempt.ended.get_or_insert_with(Instant::now);
        }
    }

    /// Keeps a failed attempt's Team classification, sealed first, then its sanitized failure or safe transport
    /// condition; a diagnostic is never safety evidence, so one that cannot be kept is audited only.
    fn classified(&self, request: &ExecutorRequest, failed: &Failed) -> Result<(), &'static str> {
        self.returned_now();
        let classified = cursor::failed(&self.cursor(), failed.fault.as_str())
            .map_err(|error| self.fail(error.code))?;
        self.seal(classified)?;
        let status = if self.stop.is_stopped() {
            "stopped"
        } else {
            "failed"
        };
        self.record_step(status, None);
        lock(&self.protected).insert(failed.operation_id.clone(), failed.protected.clone());
        let protection = self.protect(&failed.protected);
        let fault = match (&failed.failure, &failed.condition) {
            (Some(failure), _) => Fault::Failure(failure),
            (None, Some(condition)) => Fault::Condition(condition),
            (None, None) => Fault::Other,
        };
        if let Some(found) = diagnostics::evidence(fault, &protection) {
            self.diagnose(request, &failed.operation_id, found, &protection);
        }
        Ok(())
    }

    fn diagnose(
        &self,
        request: &ExecutorRequest,
        operation_id: &str,
        found: (Option<Value>, Option<String>),
        protection: &Protection,
    ) {
        let current = self.cursor();
        let binding = &current.binding;
        let diagnostic = Diagnostic {
            routine_id: binding.routine_id.clone(),
            run_id: binding.run_id.clone(),
            operation_id: operation_id.to_owned(),
            attempt: current.attempts,
            assistant_id: request.assistant_id.clone(),
            action: request.action.clone(),
            position: i64::try_from(current.step + 1).unwrap_or(i64::MAX),
            recorded_at: self.service.now(),
            failure: found.0,
            condition: found.1,
        };
        let values: Vec<String> = protection.values.iter().cloned().collect();
        let diagnostics = &self.service.parts.diagnostics;
        if diagnostics
            .record(&self.team_id, &binding.incarnation, &diagnostic, &values)
            .is_err()
        {
            self.service
                .audit_later("routine-diagnostic", &self.team_id, &binding.run_id);
        }
    }
}

fn run_binding(binding: &Binding, plan: &Plan) -> RunBinding {
    RunBinding {
        routine_id: binding.routine_id.clone(),
        run_id: binding.run_id.clone(),
        revision: binding.revision,
        plan_digest: plan.digest.clone(),
        total: i64::try_from(plan.steps.len()).unwrap_or(i64::MAX),
    }
}

impl Runtime for ReplayRuntime {
    fn start<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _message: &'a str,
        _conversation: &'a [ConversationEntry],
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move { self.begin().map_err(|_| BrainError::Protocol) })
    }

    fn resume<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        results: Value,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move { self.advanced(&results).map_err(|_| BrainError::Protocol) })
    }

    fn purpose<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _request: &'a ActionRequest,
        _assistant_name: &'a str,
        _summary: &'a str,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Option<String>> {
        // A replayed run asks no model why it pauses.
        Box::pin(async { None })
    }
}

impl DispatchHooks for ReplayRuntime {
    fn logical_operation(&self, request: &ExecutorRequest) -> Option<String> {
        let current = self.cursor();
        (current.episode.carried() && request.interrupt_id == interrupt(current.step))
            .then_some(current.operation_id)
            .flatten()
    }

    fn dispatching(
        &self,
        request: &ExecutorRequest,
        operation_id: &str,
        workload: &str,
        protected: &[String],
    ) -> Result<(), Refused> {
        self.dispatched(request, operation_id, workload, protected)
            .map_err(|_| Refused)
    }

    fn returned(&self, _request: &ExecutorRequest) {
        self.returned_now();
    }

    fn failed(&self, request: &ExecutorRequest, failed: &Failed) {
        let _classified = self.classified(request, failed);
    }
}

/// The cursor past its completed step; the plan's shown step also keeps its bounded result and keyed digest.
///
/// # Errors
/// A cursor or selection code, or `routine-cursor-unavailable` when the digest cannot be keyed.
pub fn advance(
    service: &RoutineService,
    team_id: &str,
    current: &Cursor,
    plan: &Plan,
    completed: (&Value, &Protection),
) -> Result<Cursor, &'static str> {
    let slot = match plan.shown() {
        Some(shown) if current.step + 1 == plan.steps.len() => Some(slot(
            service,
            team_id,
            &current.binding,
            (shown, current.step + 1),
            completed,
        )?),
        _ => None,
    };
    let (result, _protection) = completed;
    cursor::complete(current, plan, result, slot.as_ref()).map_err(|error| error.code)
}

/// One shown step's result as its notice will show it, with the keyed digest a change is compared on; one that
/// cannot be projected, or any after the run lost its protection, is kept as unavailable with no digest.
fn slot(
    service: &RoutineService,
    team_id: &str,
    binding: &Binding,
    shown: (&Step, usize),
    completed: (&Value, &Protection),
) -> Result<Value, &'static str> {
    let ((step, position), (result, protection)) = (shown, completed);
    let unavailable = || {
        object_of(vec![
            ("step", text(&step.step_id)),
            ("output", output_state(&int(position), "unavailable")),
            ("digest", Value::Null),
        ])
    };
    if protection.lost {
        return Ok(unavailable());
    }
    let values: Vec<String> = protection.values.iter().cloned().collect();
    let Ok(node) = output_safe(result, &step.output_schema, &values) else {
        return Ok(unavailable());
    };
    let digest = match output_compared(&node) {
        None => Value::Null,
        Some(material) => text(
            &service
                .parts
                .store
                .output_digest(team_id, binding, &step.step_id, &material)
                .map_err(|_| "routine-cursor-unavailable")?,
        ),
    };
    Ok(object_of(vec![
        ("step", text(&step.step_id)),
        ("output", output_shown(&int(position), &node)),
        ("digest", digest),
    ]))
}

/// What durable state proves about one replayed run, never what a segment remembers in memory.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Progress {
    /// Nothing of it can have been dispatched.
    None,
    /// Its sealed cursor completed every step of its sealed plan.
    Done,
    /// Anything else, including state that cannot be read.
    Partial,
}

/// The run's journal batch, sealed recovery snapshot, and cursor, read under the Team's held Routine lock.
type Sealed = (Option<(String, String)>, Option<Recovery>, Option<Cursor>);

fn sealed_locked(
    lock: &TeamLock<'_>,
    journal: &dyn RoutineJournal,
    value: &Run,
) -> Result<Sealed, ()> {
    let batch = journal.current_batch(&value.generation).map_err(|_| ())?;
    let Some(snapshot) = snapshot_locked(lock, value).map_err(|_| ())? else {
        return Ok((batch, None, None));
    };
    let found = lock.cursor(&snapshot.binding).map_err(|_| ())?;
    if found.as_ref().is_some_and(|found| !snapshot.runs(found)) {
        // A cursor of another plan proves nothing about this run's steps, whether clean or complete.
        return Err(());
    }
    Ok((batch, Some(snapshot), found))
}

/// Whether one run lost its protection, the loss sealed in its cursor first (`replay.protection_lost`).
fn protection_lost_locked(
    lock: &TeamLock<'_>,
    journal: &dyn RoutineJournal,
    protections: &RunProtections,
    value: &Run,
) -> bool {
    if value.generation.is_empty() {
        return false;
    }
    let Ok((_batch, snapshot, found)) = sealed_locked(lock, journal, value) else {
        return true;
    };
    // Without a snapshot the run's cursor cannot be bound; one left behind cannot prove the protection held.
    let unbound = match (&snapshot, lock.cursors()) {
        (Some(_), _) => false,
        (None, Ok(runs)) => runs.contains(&value.run_id),
        (None, Err(_)) => return true,
    };
    let Some(found) = found else {
        return unbound;
    };
    if found.protection_lost {
        return true;
    }
    if !protections.current(&value.run_id, &found.boot).lost {
        return false;
    }
    let _sealed = lock.lose_cursor(&found.binding);
    true
}

/// The Team's live runs that lost their protection, each loss sealed in its run's cursor first (ADR-0101 §6).
#[must_use]
pub fn lost_runs(
    lock: &TeamLock<'_>,
    state: &TeamRoutines,
    journal: &dyn RoutineJournal,
    protections: &RunProtections,
) -> BTreeSet<String> {
    record::live_runs(state)
        .into_iter()
        .filter(|item| protection_lost_locked(lock, journal, protections, item))
        .map(|item| item.run_id.clone())
        .collect()
}

/// The lost-runs check a chat turn's Routine writes run with (`RoutineChat::lost`), over the same journal and
/// protections the Routine service reads.
#[must_use]
pub fn lost_check(
    journal: Arc<dyn RoutineJournal>,
    protections: Arc<RunProtections>,
) -> super::proposal::LostCheck {
    Arc::new(move |lock: &TeamLock<'_>, state: &TeamRoutines| {
        lost_runs(lock, state, journal.as_ref(), &protections)
    })
}

impl RoutineService {
    fn sealed(&self, team_id: &str, value: &Run) -> Result<Sealed, ()> {
        let lock = self.parts.store.lock(team_id).map_err(|_| ())?;
        sealed_locked(&lock, self.parts.journal.as_ref(), value)
    }

    /// Marks one run of the Team's state as having lost its protection.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn mark_lost(
        &self,
        team_id: &str,
        run_id: &str,
    ) -> Result<(), crate::http::service::Problem> {
        self.update(team_id, |state| {
            (runs::lose_protection(&state, run_id).unwrap_or(state), ())
        })
    }

    /// Whether a run has lost its protection, sealing the loss in its cursor first.
    pub(super) fn protection_lost(&self, team_id: &str, value: &Run) -> bool {
        let Ok(lock) = self.parts.store.lock(team_id) else {
            return true;
        };
        let parts = &self.parts;
        protection_lost_locked(&lock, parts.journal.as_ref(), &parts.protections, value)
    }

    /// What durable state proves about one replayed run.
    pub(super) fn progress(&self, team_id: &str, value: &Run) -> Progress {
        if value.generation.is_empty() {
            // A run binds its journal generation before anything of it can run.
            return Progress::None;
        }
        let Ok((batch, snapshot, found)) = self.sealed(team_id, value) else {
            return Progress::Partial;
        };
        let (Some(snapshot), Some(found)) = (snapshot, found) else {
            return if batch.is_none() {
                Progress::None
            } else {
                Progress::Partial
            };
        };
        if found.step == 0 && found.operation_id.is_none() {
            // Every dispatch is sealed in the cursor before its RPC: whatever the journal began never reached anything.
            return Progress::None;
        }
        if found.operation_id.is_none() && found.step == snapshot.steps().len() {
            Progress::Done
        } else {
            Progress::Partial
        }
    }

    /// The shown result a run's sealed cursor kept, or `None` when it kept none or cannot be read.
    pub(super) fn sealed_shown(&self, team_id: &str, value: &Run) -> Option<Value> {
        let (_batch, _snapshot, found) = self.sealed(team_id, value).ok()?;
        found?.shown
    }

    /// Whether a dispatch of the run may have acted without its outcome being known; unreadable counts as yes.
    pub(super) fn uncertain(&self, value: &Run, held: bool) -> bool {
        if held {
            return true;
        }
        if value.generation.is_empty() {
            return false;
        }
        self.parts
            .journal
            .uncertain_fingerprint(&value.generation)
            .map_or(true, |found| found.is_some())
    }

    /// Whether every step the run's sealed cursor dispatched is one its Routine's revision permits as read-only;
    /// anything unreadable or a Routine since changed proves nothing.
    pub(super) fn read_only_prefix(&self, team_id: &str, value: &Run) -> bool {
        let Ok((_batch, Some(snapshot), Some(found))) = self.sealed(team_id, value) else {
            return false;
        };
        let Ok(state) = self.load(team_id) else {
            return false;
        };
        let Ok(current) = record::routine(&state, &value.routine_id) else {
            return false;
        };
        if current.revision != snapshot.binding.revision {
            return false;
        }
        let read_only: BTreeSet<(String, String)> = current
            .permitted
            .iter()
            .filter(|item| item.get("read_only") == Some(&Value::Bool(true)))
            .filter_map(|item| Some((string(item, "assistant")?, string(item, "action")?)))
            .collect();
        let reached = found.step + usize::from(found.operation_id.is_some());
        snapshot.steps().iter().take(reached).all(|step| {
            string(step, "assistant")
                .zip(string(step, "action"))
                .is_some_and(|pair| read_only.contains(&pair))
        })
    }

    /// The Routine's plan, admitted again against the exact current contracts, so drift never runs.
    ///
    /// # Errors
    /// The plan's refusal code, or the Team read's.
    pub(super) async fn current_plan(
        &self,
        team_id: &str,
        document: &Value,
    ) -> Result<Plan, &'static str> {
        let scope = {
            let _team = self.parts.coordination.locks.team(team_id).await;
            self.parts.team.scope(team_id, true).await
        }
        .map_err(|problem| problem.code.unwrap_or("team-context-unavailable"))?;
        let contracts = super::contracts::contracts(&scope.assistants);
        plan::admit(document, &contracts).map_err(|error| error.code)
    }

    /// The run's runtime: its recovery snapshot sealed, and its cursor reopened or started with its protection.
    ///
    /// A fresh run binds its protection in this Team boot with its first cursor, before anything can dispatch or
    /// freeze; a reopened one keeps it only when it was bound in this boot and is still held here.
    ///
    /// # Errors
    /// The stable reason the run cannot replay.
    pub(super) async fn runtime(
        &self,
        team_id: &str,
        value: &Run,
        routine: &Routine,
        held: (Stop, Protected),
    ) -> Result<Arc<ReplayRuntime>, &'static str> {
        let network = network_of(&value.generation, &value.run_id).ok_or("generation-invalid")?;
        let admitted = self.current_plan(team_id, &routine.plan).await?;
        // Write-once and sealed before the run's first dispatch; every later segment reseals the exact same bytes.
        let snapshot = Recovery {
            binding: Binding {
                incarnation: network,
                routine_id: routine.routine_id.clone(),
                revision: routine.revision,
                run_id: value.run_id.clone(),
            },
            name: routine.name.clone(),
            plan: routine.plan.clone(),
        };
        self.seal_recovery(team_id, &snapshot)
            .map_err(|_| "routine-state-unavailable")?;
        let found = self.reopened(team_id, &snapshot)?;
        let (stop, protected) = held;
        let started = self.started(&admitted, &snapshot, found.as_ref())?;
        let replay = Arc::new(ReplayRuntime {
            service: self.clone(),
            team_id: team_id.to_owned(),
            plan: admitted,
            reply: routine.name.clone(),
            stop,
            protected,
            state: Mutex::new(Replay {
                cursor: started.clone(),
                attempt: None,
                failure: None,
            }),
        });
        if found.is_none() {
            replay.seal(started)?;
        } else {
            replay.protection();
        }
        Ok(replay)
    }

    /// The run's cursor reopened with the protection it keeps here, or its first cursor with its protection bound now.
    fn started(
        &self,
        admitted: &Plan,
        snapshot: &Recovery,
        found: Option<&Cursor>,
    ) -> Result<Cursor, &'static str> {
        let protections = &self.parts.protections;
        let run_id = &snapshot.binding.run_id;
        if let Some(found) = found {
            protections.current(run_id, &found.boot);
            return Ok(found.clone());
        }
        protections.bind(run_id);
        cursor::start(
            admitted,
            snapshot.binding.clone(),
            self.now(),
            protections.boot(),
        )
        .map_err(|error| error.code)
    }

    fn reopened(&self, team_id: &str, snapshot: &Recovery) -> Result<Option<Cursor>, &'static str> {
        let found = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.cursor(&snapshot.binding))
            .map_err(|_| "routine-cursor-unavailable")?;
        if found.as_ref().is_some_and(|found| !snapshot.runs(found)) {
            return Err("cursor-plan-changed");
        }
        Ok(found)
    }

    /// A verified occurrence completed the held step: its record says recovered, with the inputs it was given.
    pub(super) fn record_recovered(
        &self,
        team_id: &str,
        cursors: (&Cursor, &Cursor),
        admitted: &Plan,
        protected: &[String],
    ) {
        let (held, completed) = cursors;
        if completed.protection_lost && !held.protection_lost {
            let _marked = self.mark_lost(team_id, &held.binding.run_id);
        }
        let input = admitted
            .steps
            .get(held.step)
            .and_then(|step| {
                plan::resolve(admitted, step, &held.selections(), held.started_at, |_| {
                    Ok(())
                })
                .ok()
            })
            .unwrap_or(Value::Object(Vec::new()));
        let runtime = ReplayRuntime {
            service: self.clone(),
            team_id: team_id.to_owned(),
            plan: admitted.clone(),
            reply: String::new(),
            stop: Stop::new(),
            protected: Protected::default(),
            state: Mutex::new(Replay {
                cursor: completed.clone(),
                attempt: Some(Attempt {
                    position: held.step + 1,
                    started: Instant::now(),
                    input,
                    protected: protected.to_vec(),
                    ended: None,
                }),
                failure: None,
            }),
        };
        runtime.record_step("recovered", Some(held));
    }
}

fn string(value: &Value, name: &str) -> Option<String> {
    match value.get(name) {
        Some(Value::String(text)) => Some(text.clone()),
        _ => None,
    }
}
