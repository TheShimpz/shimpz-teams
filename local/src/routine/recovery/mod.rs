//! Verifying and continuing a held Routine run, with no model (`local/routine/recovery.py`, ADR-0092 sections 4, 6,
//! and 7).
//!
//! A held run's failed step is assessed from its sealed cursor, recovery snapshot, and incident evidence. Its operation
//! is proven absent when Team-admitted evidence says so: a reviewed read-only Action whose failure Team classified, a
//! journal that settled it without effect, or a suspension before any effect. Anything else stays uncertain until the
//! Action's fixed read-only verifier is run under its exact descriptor. `occurred` with a recovered result valid under
//! the original output schema completes the step; `not_occurred` proves absence, which retries the same operation once
//! in the whole run; anything else leaves the run held for a person. A continuation never passes an uncertain
//! operation: it is retried only after proven absence, or the run is held again.

use std::sync::{Arc, Mutex, PoisonError};

use strict_json::Value;
use team_action::executor::ActionRequest as ExecutorRequest;
use team_assistant::spec::{ActionSpec, Direction};
use team_chat::orchestrator::Outcome;
use team_chat::progress::Reporter;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_inference::brain::BrainError;
use team_inference::runtime::{
    ActionRequest, ConversationEntry, Runtime, RuntimeContext, RuntimeTurn, TurnStatus,
};
use team_routine::claim::{VERIFY_SUFFIX, generation_for};
use team_routine::cursor::{self, Cursor};
use team_routine::plan::{self, Plan};
use team_routine::trace::secret_values;

use super::executor::{DispatchHooks, Failed, Refused};
use super::incident::OpenedRecovery;
use super::protection::RunProtections;
use super::replay::advance;
use super::replay::segment::Segment;
use super::run::LeasedRun;
use super::service::RoutineService;
use crate::chat::problem;
use crate::http::service::Problem;

pub mod episode;

/// The interrupt of a verifier call.
pub const VERIFY_INTERRUPT: &str = "routine-verify";
/// What proves an operation of a mutating Action absent without a verifier: it settled without effect, or it paused
/// for a person before acting.
const ABSENT_STATES: [&str; 2] = ["no_effect", "prepared"];
/// Team's own classifications of a failed attempt that admit it had no business effect when its Action is read-only.
const TRUSTED_FAULTS: [&str; 3] = ["handled", "transport", "other"];

/// A held run's failed step, its Action's reviewed contract, and what the incident evidence says of it.
struct Assessment {
    opened: OpenedRecovery,
    plan: Plan,
    action: Option<ActionSpec>,
    network_id: String,
    state: Option<String>,
}

impl Assessment {
    const fn cursor(&self) -> &Cursor {
        &self.opened.cursor
    }

    fn step(&self) -> &plan::Step {
        &self.plan.steps[self.cursor().step]
    }
}

fn drift() -> Problem {
    Problem::new(
        409,
        "Routine contracts changed since the run",
        "routine-drift",
    )
}

/// What Team-admitted evidence proves of the held step's operation: `none`, `policy`, `unquiesced`, `absent`,
/// `unclassified` (a read-only step whose failure Team never classified), or `uncertain`.
fn proven(assessment: &Assessment) -> &'static str {
    let held = assessment.cursor();
    let Some(action) = &assessment.action else {
        return "none";
    };
    if held.operation_id.is_none() {
        return "none";
    }
    match held.fault.as_str() {
        "policy" => return "policy",
        // Team could not prove the workload stopped after an ambiguous outcome: nothing may verify or retry it yet.
        "unquiesced" => return "unquiesced",
        _ => {}
    }
    let state = assessment.state.as_deref().unwrap_or_default();
    if held.absent || (ABSENT_STATES.contains(&state) && !held.episode.carried()) {
        return "absent";
    }
    if action.effect == "read_only" {
        return if TRUSTED_FAULTS.contains(&held.fault.as_str()) {
            "absent"
        } else {
            "unclassified"
        };
    }
    "uncertain"
}

/// Whether a held run may not continue: its operation is still uncertain, held for policy, or not proven stopped.
#[must_use]
pub fn blocked(held: &Cursor) -> bool {
    held.operation_id.is_some()
        && (matches!(held.fault.as_str(), "policy" | "unquiesced") || !held.absent)
}

/// The exact input the operation was dispatched with, recomputed and bound by its sealed commitment.
fn original_input(assessment: &Assessment) -> Option<Value> {
    let held = assessment.cursor();
    let resolved = plan::resolve(
        &assessment.plan,
        assessment.step(),
        &held.selections(),
        held.started_at,
        |_| Ok(()),
    )
    .ok()?;
    (Some(plan::commitment(&resolved)) == held.commitment).then_some(resolved)
}

/// The fixed verifier call the descriptor defines, or `None` when the original input cannot be proven.
fn verifier_request(assessment: &Assessment) -> Option<ActionRequest> {
    let verifier = assessment.action.as_ref()?.verifier.as_ref()?;
    let original = original_input(assessment)?;
    let Some(Value::Object(inputs)) = verifier.get("input") else {
        return None;
    };
    let operation = Value::Object(vec![(
        "from".to_owned(),
        Value::String("operation_id".to_owned()),
    )]);
    let mut payload = Vec::with_capacity(inputs.len());
    for (name, binding) in inputs {
        let value = if *binding == operation {
            Value::String(assessment.cursor().operation_id.clone()?)
        } else {
            plan::select(&original, binding.get("pointer")?).ok()?
        };
        payload.push((name.clone(), value));
    }
    let Some(Value::String(action)) = verifier.get("action") else {
        return None;
    };
    Some(ActionRequest {
        interrupt_id: VERIFY_INTERRUPT.to_owned(),
        assistant_id: assessment.step().assistant_id.clone(),
        action: action.clone(),
        input: Value::Object(payload),
    })
}

/// One fixed read-only verifier call in place of a Brain turn; it never calls a model. The call belongs to the held
/// run, so every value Team injects into it joins that run's protection before its RPC.
struct VerifierRuntime {
    request: ActionRequest,
    run_id: String,
    protections: Arc<RunProtections>,
    result: Mutex<Option<Value>>,
}

fn turn(status: TurnStatus, actions: Vec<ActionRequest>) -> RuntimeTurn {
    RuntimeTurn {
        status,
        reply: String::new(),
        actions,
        clarification: None,
        memory: Vec::new(),
        routine: None,
        browser: None,
    }
}

impl Runtime for VerifierRuntime {
    fn start<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _message: &'a str,
        _conversation: &'a [ConversationEntry],
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move { Ok(turn(TurnStatus::ActionRequired, vec![self.request.clone()])) })
    }

    fn resume<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        results: Value,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move {
            *self.result.lock().unwrap_or_else(PoisonError::into_inner) =
                results.get(&self.request.interrupt_id).cloned();
            Ok(turn(TurnStatus::Completed, Vec::new()))
        })
    }

    fn purpose<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _request: &'a ActionRequest,
        _assistant_name: &'a str,
        _summary: &'a str,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Option<String>> {
        Box::pin(async { None })
    }
}

impl DispatchHooks for VerifierRuntime {
    fn logical_operation(&self, _request: &ExecutorRequest) -> Option<String> {
        None
    }

    fn dispatching(
        &self,
        _request: &ExecutorRequest,
        _operation_id: &str,
        _workload: &str,
        protected: &[String],
    ) -> Result<(), Refused> {
        self.protections
            .grow(&self.run_id, protected.iter().map(String::as_str));
        Ok(())
    }

    fn returned(&self, _request: &ExecutorRequest) {}

    fn failed(&self, _request: &ExecutorRequest, _failed: &Failed) {}
}

impl RoutineService {
    fn seal(&self, team_id: &str, sealed: &Cursor) -> Result<(), Problem> {
        self.parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.put_cursor(sealed))
            .map_err(|_| problem::routine_state_unavailable())
    }

    fn operation_state(
        &self,
        team_id: &str,
        incident_id: &str,
        operation_id: Option<&str>,
    ) -> Result<Option<String>, Problem> {
        let unavailable = problem::routine_state_unavailable;
        let sealed = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.incident(incident_id))
            .map_err(|_| unavailable())?
            .ok_or_else(unavailable)?;
        let held = super::incident::read_evidence(&sealed, incident_id).ok_or_else(unavailable)?;
        Ok(held.state_of(operation_id))
    }

    /// The held run's failed step under the Team's exact current contracts; any drift fails closed.
    async fn assess(&self, team_id: &str, incident_id: &str) -> Result<Assessment, Problem> {
        let opened = self.open_recovery(team_id, incident_id)?;
        let scope = {
            let _team = self.parts.coordination.locks.team(team_id).await;
            self.parts.team.scope(team_id, true).await?
        };
        if scope.network_id != opened.recovery.binding.incarnation {
            return Err(drift());
        }
        let contracts = super::contracts::contracts(&scope.assistants);
        let admitted = plan::admit(&opened.recovery.plan, &contracts).map_err(|_| drift())?;
        let mut assessment = Assessment {
            opened,
            plan: admitted,
            action: None,
            network_id: scope.network_id.clone(),
            state: None,
        };
        if assessment.cursor().done(&assessment.plan) {
            return Ok(assessment);
        }
        let step = assessment.step();
        assessment.action = scope
            .assistant(&step.assistant_id)
            .and_then(|active| active.action(&step.action))
            .cloned();
        let operation = assessment.cursor().operation_id.clone();
        assessment.state = self.operation_state(team_id, incident_id, operation.as_deref())?;
        Ok(assessment)
    }

    /// Whether the workload an attempt was dispatched to is proven to have stopped since: its container is gone or
    /// replaced, stopped, or started again after the attempt; one that cannot be read proves nothing.
    async fn stopped_since(&self, team_id: &str, assistant_id: &str, held: &Cursor) -> bool {
        if held.workload.is_empty() {
            return false;
        }
        let Some(state) = self
            .parts
            .workloads
            .state(team_id, assistant_id, &held.workload)
            .await
        else {
            return false;
        };
        !state.running
            || state
                .started_at
                .is_some_and(|started| started > held.dispatched_at)
    }

    /// Whether nothing of the held attempt can still be running, so a fresh run may start: no operation is open, the
    /// attempt ended in a way Team classified, the journal proves it never acted, or its workload is proven stopped.
    ///
    /// # Errors
    /// `routine-state-unavailable` when the evidence cannot be read.
    pub(crate) async fn workload_stopped(
        &self,
        team_id: &str,
        opened: &OpenedRecovery,
    ) -> Result<bool, Problem> {
        let held = &opened.cursor;
        if held.operation_id.is_none() || TRUSTED_FAULTS.contains(&held.fault.as_str()) {
            return Ok(true);
        }
        if held.workload.is_empty() {
            let run_id = &held.binding.run_id;
            let state = self.operation_state(team_id, run_id, held.operation_id.as_deref())?;
            return Ok(
                state.is_some_and(|state| ABSENT_STATES.contains(&state.as_str()))
                    && !held.episode.carried(),
            );
        }
        let steps = opened.recovery.steps();
        let index = held.step.min(steps.len().saturating_sub(1));
        let assistant_id = match steps.get(index).and_then(|step| step.get("assistant")) {
            Some(Value::String(assistant)) => assistant.clone(),
            _ => return Ok(false),
        };
        Ok(self.stopped_since(team_id, &assistant_id, held).await)
    }

    /// An uncertain operation whose failure Team never classified needs its workload proven stopped first.
    async fn quiescence(
        &self,
        team_id: &str,
        assessment: &Assessment,
        verdict: &'static str,
    ) -> &'static str {
        let held = assessment.cursor();
        let assistant_id = &assessment.step().assistant_id;
        if verdict == "uncertain"
            && held.fault.is_empty()
            && !self.stopped_since(team_id, assistant_id, held).await
        {
            return "unquiesced";
        }
        verdict
    }

    /// Runs the verifier in its own released generation; `None` when it failed, paused for a person, or was refused.
    async fn call_verifier(
        &self,
        run: &LeasedRun<'_>,
        assessment: &Assessment,
        request: ActionRequest,
    ) -> Option<Value> {
        let run_id = assessment.cursor().binding.run_id.clone();
        let runtime = Arc::new(VerifierRuntime {
            request,
            run_id: run_id.clone(),
            protections: Arc::clone(&self.parts.protections),
            result: Mutex::new(None),
        });
        let generation = generation_for(&assessment.network_id, &run_id, VERIFY_SUFFIX);
        let journal = &self.parts.journal;
        journal.discard(&generation).ok()?;
        let ids = vec![assessment.step().assistant_id.clone()];
        let progress = Reporter::silent();
        let segment = Segment {
            team_id: &run.team_id,
            run_id: &run_id,
            generation: &generation,
            assistant_ids: &ids,
            stop: run.stop(),
            progress: &progress,
            locale: None,
            continuation: None,
            transcripts: &[],
            max_rounds: 1,
            held: false,
        };
        let hooks: Arc<dyn DispatchHooks> = runtime.clone();
        let finished = self.run_segment(&segment, runtime.as_ref(), hooks).await;
        let _released = journal.discard(&generation);
        match finished {
            Ok(finished) if matches!(finished.outcome, Outcome::Completed(_)) => runtime
                .result
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take(),
            _ => None,
        }
    }

    /// Admits the verifier's evidence: completes the step on occurrence, records proven absence, else inconclusive.
    fn judge(
        &self,
        team_id: &str,
        assessment: &Assessment,
        result: &Value,
        protected: Option<&Vec<String>>,
    ) -> Result<&'static str, Problem> {
        let held = assessment.cursor();
        let Some(verifier) = assessment
            .action
            .as_ref()
            .and_then(|action| action.verifier.as_ref())
        else {
            return Ok("inconclusive");
        };
        let found = |name: &str| {
            verifier
                .get(name)
                .and_then(|pointer| plan::select(result, pointer).ok())
        };
        match found("outcome") {
            Some(Value::String(outcome)) if outcome == "not_occurred" => {
                let absent = cursor::proven_absent(held)
                    .map_err(|_| problem::routine_state_unavailable())?;
                self.seal(team_id, &absent)?;
                return Ok("absent");
            }
            Some(Value::String(outcome)) if outcome == "occurred" => {}
            _ => return Ok("inconclusive"),
        }
        let (Some(protected), Some(recovered)) = (protected, found("result")) else {
            return Ok("inconclusive");
        };
        let Some(completed) = self.recovered(team_id, assessment, &recovered, protected) else {
            return Ok("inconclusive");
        };
        self.seal(team_id, &completed)?;
        self.record_recovered(team_id, (held, &completed), &assessment.plan, protected);
        Ok("occurred")
    }

    /// The cursor a recovered result completes the held step with, or `None` when it is invalid or echoes a private
    /// value the failed attempt itself was given.
    fn recovered(
        &self,
        team_id: &str,
        assessment: &Assessment,
        recovered: &Value,
        protected: &[String],
    ) -> Option<Cursor> {
        let held = assessment.cursor();
        let action = assessment.action.as_ref()?;
        action.validate(Direction::Output, recovered).ok()?;
        let secrets: Vec<&str> = protected.iter().map(String::as_str).collect();
        if team_action::projection::contains_secret(recovered, &secrets) {
            return None;
        }
        let step = assessment.step();
        let found = secret_values(recovered, &step.output_schema).ok()?;
        let protections = &self.parts.protections;
        let protection = protections.grow(&held.binding.run_id, found.iter().map(String::as_str));
        let completed = advance(
            self,
            team_id,
            held,
            &assessment.plan,
            (recovered, &protection),
        )
        .ok()?;
        if protection.lost {
            // A loss met at the completion is sealed with it, in the same write, so the cursor never rolls back.
            return cursor::lose_protection(&completed).ok();
        }
        Some(completed)
    }

    /// Verifies a held run's failed step with no model, once, inside the run's one recovery episode: `occurred`,
    /// `absent`, `none`, `inconclusive`, or `unverifiable`; or `policy`, `unquiesced`, or `unclassified` when
    /// verification may not run.
    ///
    /// # Errors
    /// A drift, state, or Team-read problem.
    pub(crate) async fn verify(&self, run: &LeasedRun<'_>) -> Result<&'static str, Problem> {
        let team_id = &run.team_id;
        let assessment = self.assess(team_id, &run.run_id).await?;
        let verdict = self
            .quiescence(team_id, &assessment, proven(&assessment))
            .await;
        if matches!(verdict, "none" | "policy" | "unquiesced" | "unclassified") {
            return Ok(verdict);
        }
        let held = assessment.cursor();
        if verdict == "absent" {
            if !held.absent {
                let absent = cursor::proven_absent(held)
                    .map_err(|_| problem::routine_state_unavailable())?;
                self.seal(team_id, &absent)?;
            }
            return Ok("absent");
        }
        let Some(request) = verifier_request(&assessment) else {
            return Ok("unverifiable");
        };
        let Some(result) = self.call_verifier(run, &assessment, request).await else {
            return Ok("inconclusive");
        };
        // Without the failed attempt's own private values in memory, as after a restart, nothing proves the recovered
        // result echoes none of them: the effect stays inconclusive.
        let original = held.operation_id.as_ref().and_then(|operation| {
            run.protected
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .get(operation)
                .cloned()
        });
        self.judge(team_id, &assessment, &result, original.as_ref())
    }
}
