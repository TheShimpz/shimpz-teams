//! The replay index: a run's history folded into the state of each step, with every transition validated.
//!
//! A history that breaks a transition rule (a step out of order, an attempt that skips a number, an event after the
//! run ended) is corrupt; nothing is ever executed from it.

use serde_json::Value;

use crate::error::Error;
use crate::event::{Event, Outcome, StepKind};
use crate::json;
use crate::policy::Failure;

/// What the run was submitted with.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Start {
    pub(crate) definition: String,
    pub(crate) version: u32,
    pub(crate) input: Value,
    pub(crate) input_digest: String,
    pub(crate) deadline_ms: Option<i64>,
    pub(crate) spend_ceiling: Option<u64>,
}

/// Where one step stands.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum State {
    Scheduled,
    Running { attempt: u32, fence: u64, reserved: u64 },
    Waiting { attempt: u32, retry_at_ms: i64 },
    Completed { result: Value, blobs: Vec<u64> },
    Failed { failure: Failure },
    Uncertain,
    Resolved { result: Value },
}

/// One step of the run.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Step {
    pub(crate) kind: StepKind,
    pub(crate) name: String,
    pub(crate) digest: String,
    pub(crate) input: Option<Value>,
    pub(crate) policy: String,
    pub(crate) state: State,
    pub(crate) progress: Option<Value>,
    pub(crate) first_started_ms: Option<i64>,
}

impl Step {
    pub(crate) fn is_finished(&self) -> bool {
        matches!(self.state, State::Completed { .. } | State::Failed { .. } | State::Uncertain | State::Resolved { .. })
    }
}

/// A run's history folded for replay.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct History {
    pub(crate) start: Start,
    pub(crate) steps: Vec<Step>,
    pub(crate) cancel: bool,
    pub(crate) outcome: Option<Outcome>,
    pub(crate) events: u64,
    pub(crate) spent: u64,
    /// Transient blobs stored and not yet released.
    held: std::collections::BTreeSet<u64>,
}

fn corrupt(what: &str) -> Error {
    Error::Storage(format!("corrupt history: {what}"))
}

impl History {
    /// Folds a complete history.
    pub(crate) fn from_events<'a>(events: impl IntoIterator<Item = &'a Event>) -> Result<Self, Error> {
        let mut events = events.into_iter();
        let Some(Event::RunStarted { definition, version, input, deadline_ms, spend_ceiling }) = events.next() else {
            return Err(corrupt("does not begin with run_started"));
        };
        let start = Start {
            definition: definition.clone(),
            version: *version,
            input_digest: json::digest(input),
            input: input.clone(),
            deadline_ms: *deadline_ms,
            spend_ceiling: *spend_ceiling,
        };
        let mut history = Self {
            start,
            steps: vec![],
            cancel: false,
            outcome: None,
            events: 1,
            spent: 0,
            held: std::collections::BTreeSet::new(),
        };
        for event in events {
            history.apply(event)?;
        }
        Ok(history)
    }

    /// How many transient blobs the run holds.
    pub(crate) fn held_blobs(&self) -> usize {
        self.held.len()
    }

    /// The spend committed so far: settled costs plus the reservation of every attempt still running.
    pub(crate) fn committed_spend(&self) -> u64 {
        let running: u64 = self
            .steps
            .iter()
            .map(|step| match step.state {
                State::Running { reserved, .. } => reserved,
                _ => 0,
            })
            .sum();
        self.spent.saturating_add(running)
    }

    /// Applies one event, refusing any transition the run model does not allow.
    pub(crate) fn apply(&mut self, event: &Event) -> Result<(), Error> {
        if self.outcome.is_some() {
            return Err(corrupt("event after run_completed"));
        }
        match event {
            Event::RunStarted { .. } => return Err(corrupt("second run_started")),
            Event::StepScheduled { step, kind, name, digest, input, policy } => {
                self.schedule(*step, *kind, name, digest, input.as_ref(), policy)?;
            }
            Event::StepStarted { step, attempt, fence, at_ms, reserved } => {
                self.start_attempt(*step, *attempt, *fence, *at_ms, *reserved)?;
            }
            Event::StepHeartbeat { step, attempt, progress } => self.heartbeat(*step, *attempt, progress)?,
            Event::StepCompleted { step, attempt, result, cost, blobs, release } => {
                self.complete(*step, *attempt, result, blobs, *cost)?;
                self.held.extend(blobs);
                for id in release {
                    self.held.remove(id);
                }
            }
            Event::StepFailed { step, attempt, failure, retry_at_ms, cost } => {
                self.fail(*step, *attempt, failure, *retry_at_ms, *cost)?;
            }
            Event::StepUncertain { step, attempt } => self.uncertain(*step, *attempt)?,
            Event::DeferredResolved { step, result } => self.resolve(*step, result)?,
            Event::CancelRequested {} if self.cancel => return Err(corrupt("second cancel_requested")),
            Event::CancelRequested {} => self.cancel = true,
            Event::RunCompleted { outcome } => self.outcome = Some(outcome.clone()),
        }
        self.events += 1;
        Ok(())
    }

    fn step_mut(&mut self, step: u32) -> Result<&mut Step, Error> {
        let index = usize::try_from(step).map_err(|_| corrupt("step out of range"))?;
        self.steps.get_mut(index).ok_or_else(|| corrupt("event for an unscheduled step"))
    }

    fn schedule(
        &mut self,
        step: u32,
        kind: StepKind,
        name: &str,
        digest: &str,
        input: Option<&Value>,
        policy: &str,
    ) -> Result<(), Error> {
        if usize::try_from(step).ok() != Some(self.steps.len()) {
            return Err(corrupt("steps out of order"));
        }
        if input.is_some() == (kind == StepKind::Model) {
            return Err(corrupt("a model step stores only its digest; every other step stores its input"));
        }
        self.steps.push(Step {
            kind,
            name: name.to_owned(),
            digest: digest.to_owned(),
            input: input.cloned(),
            policy: policy.to_owned(),
            state: State::Scheduled,
            progress: None,
            first_started_ms: None,
        });
        Ok(())
    }

    fn start_attempt(&mut self, step: u32, attempt: u32, fence: u64, at_ms: i64, reserved: u64) -> Result<(), Error> {
        let record = self.step_mut(step)?;
        if !matches!(record.kind, StepKind::Model | StepKind::Activity) {
            return Err(corrupt("only model and activity steps have attempts"));
        }
        let previous = match record.state {
            State::Scheduled => 0,
            State::Waiting { attempt, .. } => attempt,
            _ => return Err(corrupt("attempt started on a running or finished step")),
        };
        if attempt != previous + 1 {
            return Err(corrupt("attempt numbers must increase by one"));
        }
        record.first_started_ms.get_or_insert(at_ms);
        record.state = State::Running { attempt, fence, reserved };
        Ok(())
    }

    fn running(&mut self, step: u32, attempt: u32) -> Result<(&mut Step, u64), Error> {
        let record = self.step_mut(step)?;
        match record.state {
            State::Running { attempt: current, reserved, .. } if current == attempt => Ok((record, reserved)),
            _ => Err(corrupt("event for an attempt that is not running")),
        }
    }

    /// Steps without attempts (local computations and timers) finish straight from `Scheduled` as attempt 1.
    fn settle(&mut self, step: u32, attempt: u32) -> Result<&mut Step, Error> {
        let record = self.step_mut(step)?;
        if !matches!(record.kind, StepKind::Local | StepKind::Timer) {
            return Ok(self.running(step, attempt)?.0);
        }
        let record = self.step_mut(step)?;
        if attempt != 1 || record.state != State::Scheduled {
            return Err(corrupt("a local step or timer finishes once, as attempt 1"));
        }
        Ok(record)
    }

    fn heartbeat(&mut self, step: u32, attempt: u32, progress: &Value) -> Result<(), Error> {
        self.running(step, attempt)?.0.progress = Some(progress.clone());
        Ok(())
    }

    fn complete(&mut self, step: u32, attempt: u32, result: &Value, blobs: &[u64], cost: u64) -> Result<(), Error> {
        self.settle(step, attempt)?.state = State::Completed { result: result.clone(), blobs: blobs.to_vec() };
        self.spent = self.spent.saturating_add(cost);
        Ok(())
    }

    fn fail(
        &mut self,
        step: u32,
        attempt: u32,
        failure: &Failure,
        retry_at_ms: Option<i64>,
        cost: u64,
    ) -> Result<(), Error> {
        let record = self.settle(step, attempt)?;
        record.state = match retry_at_ms {
            Some(_) if matches!(record.kind, StepKind::Local | StepKind::Timer) => {
                return Err(corrupt("a local step or timer is never retried"));
            }
            Some(retry_at_ms) => State::Waiting { attempt, retry_at_ms },
            None => State::Failed { failure: failure.clone() },
        };
        self.spent = self.spent.saturating_add(cost);
        Ok(())
    }

    fn uncertain(&mut self, step: u32, attempt: u32) -> Result<(), Error> {
        let (record, reserved) = self.running(step, attempt)?;
        if record.kind != StepKind::Activity {
            return Err(corrupt("only an activity can be uncertain"));
        }
        record.state = State::Uncertain;
        self.spent = self.spent.saturating_add(reserved);
        Ok(())
    }

    fn resolve(&mut self, step: u32, result: &Value) -> Result<(), Error> {
        let record = self.step_mut(step)?;
        if record.kind != StepKind::Deferred || record.state != State::Scheduled {
            return Err(corrupt("resolution of a step that is not an open deferred request"));
        }
        record.state = State::Resolved { result: result.clone() };
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::LimitKind;
    use serde_json::json;

    fn started() -> Event {
        Event::RunStarted {
            definition: "d".into(),
            version: 1,
            input: json!({"a": 1}),
            deadline_ms: None,
            spend_ceiling: Some(10),
        }
    }

    fn scheduled(step: u32, kind: StepKind) -> Event {
        let input = (kind != StepKind::Model).then(|| json!({}));
        Event::StepScheduled { step, kind, name: "n".into(), digest: "d".into(), input, policy: String::new() }
    }

    fn fold(events: &[Event]) -> Result<History, Error> {
        History::from_events(&std::iter::once(started()).chain(events.iter().cloned()).collect::<Vec<_>>())
    }

    /// The corruption's message; a valid history yields a message no assertion expects.
    fn corrupt_message(events: &[Event]) -> String {
        fold(events).map_or_else(|error| error.to_string(), |_| "valid".to_owned())
    }

    #[test]
    fn folds_a_complete_activity_history() {
        let history = fold(&[
            scheduled(0, StepKind::Activity),
            Event::StepStarted { step: 0, attempt: 1, fence: 1, at_ms: 5, reserved: 0 },
            Event::StepHeartbeat { step: 0, attempt: 1, progress: json!(1) },
            Event::StepFailed {
                step: 0,
                attempt: 1,
                failure: Failure::retryable("t", "m"),
                retry_at_ms: Some(9),
                cost: 0,
            },
            Event::StepStarted { step: 0, attempt: 2, fence: 2, at_ms: 9, reserved: 0 },
            Event::StepCompleted { step: 0, attempt: 2, result: json!("r"), cost: 0, blobs: vec![], release: vec![] },
            Event::RunCompleted { outcome: Outcome::Done { result: json!("r") } },
        ])
        .unwrap();
        assert_eq!(history.steps[0].state, State::Completed { result: json!("r"), blobs: vec![] });
        assert_eq!(history.steps[0].progress, Some(json!(1)));
        assert_eq!(history.steps[0].first_started_ms, Some(5));
        assert_eq!(history.events, 8);
        assert_eq!(history.start.input_digest, json::digest(&json!({"a": 1})));
        assert!(history.outcome.is_some());
    }

    #[test]
    fn spend_counts_settled_costs_and_running_reservations() {
        let history = fold(&[
            scheduled(0, StepKind::Model),
            Event::StepStarted { step: 0, attempt: 1, fence: 1, at_ms: 0, reserved: 7 },
            Event::StepFailed {
                step: 0,
                attempt: 1,
                failure: Failure::retryable("t", "m"),
                retry_at_ms: Some(1),
                cost: 3,
            },
            Event::StepStarted { step: 0, attempt: 2, fence: 1, at_ms: 1, reserved: 5 },
        ])
        .unwrap();
        assert_eq!(history.spent, 3);
        assert_eq!(history.committed_spend(), 8);
    }

    #[test]
    fn uncertain_keeps_the_reservation() {
        let history = fold(&[
            scheduled(0, StepKind::Activity),
            Event::StepStarted { step: 0, attempt: 1, fence: 1, at_ms: 0, reserved: 4 },
            Event::StepUncertain { step: 0, attempt: 1 },
        ])
        .unwrap();
        assert_eq!(history.steps[0].state, State::Uncertain);
        assert!(history.steps[0].is_finished());
        assert_eq!(history.committed_spend(), 4);
    }

    #[test]
    fn local_steps_timers_and_deferred_requests_finish_without_attempts() {
        let history = fold(&[
            scheduled(0, StepKind::Local),
            Event::StepCompleted { step: 0, attempt: 1, result: json!(1), cost: 0, blobs: vec![], release: vec![] },
            scheduled(1, StepKind::Timer),
            Event::StepCompleted { step: 1, attempt: 1, result: json!(null), cost: 0, blobs: vec![], release: vec![] },
            scheduled(2, StepKind::Deferred),
            Event::DeferredResolved { step: 2, result: json!("yes") },
            scheduled(3, StepKind::Local),
            Event::StepFailed {
                step: 3,
                attempt: 1,
                failure: Failure::permanent("p", "m"),
                retry_at_ms: None,
                cost: 0,
            },
            Event::CancelRequested {},
        ])
        .unwrap();
        assert!(history.steps.iter().all(Step::is_finished));
        assert!(history.cancel);
    }

    fn attempt_cases() -> Vec<(Vec<Event>, &'static str)> {
        let start = |step, attempt| Event::StepStarted { step, attempt, fence: 1, at_ms: 0, reserved: 0 };
        let done = |step, attempt| Event::StepCompleted {
            step,
            attempt,
            result: json!(1),
            cost: 0,
            blobs: vec![],
            release: vec![],
        };
        let failed = |step, retry_at_ms| Event::StepFailed {
            step,
            attempt: 1,
            failure: Failure::retryable("t", "m"),
            retry_at_ms,
            cost: 0,
        };
        let heartbeat = Event::StepHeartbeat { step: 0, attempt: 1, progress: json!(1) };
        vec![
            (vec![start(0, 1)], "unscheduled step"),
            (vec![start(u32::MAX, 1)], "unscheduled step"),
            (vec![scheduled(0, StepKind::Local), start(0, 1)], "only model and activity"),
            (vec![scheduled(0, StepKind::Activity), start(0, 2)], "increase by one"),
            (vec![scheduled(0, StepKind::Activity), start(0, 1), start(0, 2)], "running or finished"),
            (vec![scheduled(0, StepKind::Activity), done(0, 1)], "not running"),
            (vec![scheduled(0, StepKind::Activity), start(0, 1), done(0, 2)], "not running"),
            (vec![scheduled(0, StepKind::Local), done(0, 2)], "finishes once"),
            (vec![scheduled(0, StepKind::Local), done(0, 1), done(0, 1)], "finishes once"),
            (vec![scheduled(0, StepKind::Local), failed(0, Some(1))], "never retried"),
            (
                vec![scheduled(0, StepKind::Model), start(0, 1), Event::StepUncertain { step: 0, attempt: 1 }],
                "only an activity",
            ),
            (vec![scheduled(0, StepKind::Activity), heartbeat], "not running"),
        ]
    }

    fn run_cases() -> Vec<(Vec<Event>, &'static str)> {
        let model = Event::StepScheduled {
            step: 0,
            kind: StepKind::Model,
            name: "m".into(),
            digest: "d".into(),
            input: Some(json!({})),
            policy: String::new(),
        };
        let resolved = Event::DeferredResolved { step: 0, result: json!(1) };
        let limited = Event::RunCompleted { outcome: Outcome::Limited { kind: LimitKind::Steps, progress: json!(1) } };
        vec![
            (vec![started()], "second run_started"),
            (vec![scheduled(1, StepKind::Local)], "out of order"),
            (vec![model], "stores only its digest"),
            (vec![scheduled(0, StepKind::Local), resolved.clone()], "not an open deferred"),
            (vec![scheduled(0, StepKind::Deferred), resolved.clone(), resolved], "not an open deferred"),
            (vec![Event::CancelRequested {}, Event::CancelRequested {}], "second cancel"),
            (vec![limited, Event::CancelRequested {}], "after run_completed"),
        ]
    }

    #[test]
    fn refuses_histories_that_break_the_run_model() {
        for (events, expected) in attempt_cases().into_iter().chain(run_cases()) {
            let message = corrupt_message(&events);
            assert!(message.contains(expected), "{events:?}: {message}");
        }
        assert!(matches!(History::from_events(&[Event::CancelRequested {}]), Err(Error::Storage(_))));
        assert!(matches!(History::from_events(&[]), Err(Error::Storage(_))));
    }
}
