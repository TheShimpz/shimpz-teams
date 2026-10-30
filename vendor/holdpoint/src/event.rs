//! The closed event types of a run's history and their strict codec.
//!
//! A history is append-only. Every event is validated when it is written and again when it is read: it decodes
//! through [`crate::json::parse`] (no duplicate keys, bounded depth, exact integers) and then into these types, which
//! refuse unknown fields. The transitions between events are validated by the replay index (`history.rs`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{Error, LimitKind};
use crate::json;
use crate::policy::Failure;

/// The version of the event encoding; a store written by another version is refused.
pub const EVENT_VERSION: u32 = 1;

/// What a step does.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StepKind {
    /// A call to a registered model; no side effects, so it is always retried.
    Model,
    /// A pure computation inside the definition's process.
    Local,
    /// An effect a registered activity carries out.
    Activity,
    /// A result the host delivers later.
    Deferred,
    /// A durable wait until a wall-clock time.
    Timer,
}

/// How a run ended. Every run ends in exactly one of these.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "kebab-case", deny_unknown_fields)]
pub enum Outcome {
    /// The definition finished with a result.
    Done {
        /// The definition's result.
        result: Value,
    },
    /// The definition needs a person before it can go on.
    NeedsPerson {
        /// What the definition asks of the person.
        request: Value,
    },
    /// The run was cancelled.
    Cancelled {
        /// The durable progress the run had made.
        progress: Value,
    },
    /// A bound ended the run.
    Limited {
        /// Which bound.
        kind: LimitKind,
        /// The durable progress the run had made.
        progress: Value,
    },
    /// The run's wall-clock deadline passed.
    Expired {
        /// The durable progress the run had made.
        progress: Value,
    },
    /// The run cannot continue (its definition changed, its authority was revoked, or it failed for good).
    Unrecoverable {
        /// A short machine name of the cause.
        reason: String,
    },
}

/// One entry of a run's history.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Event {
    /// The run was submitted.
    RunStarted {
        /// The definition's identifier.
        definition: String,
        /// The definition's version.
        version: u32,
        /// The run's captured input.
        input: Value,
        /// The wall-clock time past which the run expires.
        deadline_ms: Option<i64>,
        /// The most the run may spend, in the host's cost unit.
        spend_ceiling: Option<u64>,
    },
    /// The definition called its next step.
    StepScheduled {
        /// The step's position in the run, from 0.
        step: u32,
        /// What the step does.
        kind: StepKind,
        /// The registered model, activity, or the definition's own name for the step.
        name: String,
        /// The digest of the step's input (for a model, of its request).
        digest: String,
        /// The step's input; a model request is not stored, only its digest.
        input: Option<Value>,
        /// The digest of the retry, timeout, idempotency, and containment policy the step runs under (empty for
        /// steps without attempts); an unfinished step never runs under another policy.
        policy: String,
    },
    /// An attempt was dispatched.
    StepStarted {
        /// The step.
        step: u32,
        /// The attempt, from 1.
        attempt: u32,
        /// The fencing token of the session that dispatched it.
        fence: u64,
        /// The wall-clock dispatch time.
        at_ms: i64,
        /// The spend reserved for this attempt's worst case.
        reserved: u64,
    },
    /// A running attempt reported progress.
    StepHeartbeat {
        /// The step.
        step: u32,
        /// The attempt.
        attempt: u32,
        /// What the attempt reported; the next attempt receives it.
        progress: Value,
    },
    /// The step finished with a result.
    StepCompleted {
        /// The step.
        step: u32,
        /// The attempt that produced the result.
        attempt: u32,
        /// The result.
        result: Value,
        /// What the attempt cost, settling its reservation.
        cost: u64,
        /// Transient blobs this step produced, stored with this event.
        blobs: Vec<u64>,
        /// Transient blobs whose last consumer this step was, deleted with this event.
        release: Vec<u64>,
    },
    /// An attempt failed.
    StepFailed {
        /// The step.
        step: u32,
        /// The attempt.
        attempt: u32,
        /// Why.
        failure: Failure,
        /// When the next attempt may start, or `None` when the step failed for good.
        retry_at_ms: Option<i64>,
        /// What the attempt cost, settling its reservation.
        cost: u64,
    },
    /// An interrupted non-idempotent attempt: it may or may not have acted, and is never repeated.
    StepUncertain {
        /// The step.
        step: u32,
        /// The attempt.
        attempt: u32,
    },
    /// The host delivered a deferred step's result.
    DeferredResolved {
        /// The step.
        step: u32,
        /// The delivered result.
        result: Value,
    },
    /// Someone asked to cancel the run.
    CancelRequested {},
    /// The run ended.
    RunCompleted {
        /// How it ended.
        outcome: Outcome,
    },
}

impl Event {
    /// The event's canonical encoding.
    ///
    /// # Errors
    ///
    /// [`Error::Limit`] when the encoding is larger than `max_bytes` or nests deeper than the decoder reads.
    pub fn encode(&self, max_bytes: usize) -> Result<String, Error> {
        let value = serde_json::to_value(self).map_err(|error| Error::Contract(error.to_string()))?;
        if json::depth(&value) > json::MAX_DEPTH {
            // The decoder would refuse it: never write what cannot be read back.
            return Err(Error::Limit(LimitKind::Value));
        }
        let encoded = json::to_canonical(&value);
        if encoded.len() > max_bytes {
            return Err(Error::Limit(LimitKind::Value));
        }
        Ok(encoded)
    }

    /// Decodes one stored event strictly.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the bytes are not one valid event.
    pub fn decode(bytes: &[u8], max_bytes: usize) -> Result<Self, Error> {
        let value = json::parse(bytes, max_bytes).map_err(|error| Error::Storage(format!("corrupt event: {error}")))?;
        serde_json::from_value(value).map_err(|error| Error::Storage(format!("corrupt event: {error}")))
    }

    /// The step this event belongs to, if any.
    #[must_use]
    pub fn step(&self) -> Option<u32> {
        match self {
            Self::StepScheduled { step, .. }
            | Self::StepStarted { step, .. }
            | Self::StepHeartbeat { step, .. }
            | Self::StepCompleted { step, .. }
            | Self::StepFailed { step, .. }
            | Self::StepUncertain { step, .. }
            | Self::DeferredResolved { step, .. } => Some(*step),
            Self::RunStarted { .. } | Self::CancelRequested {} | Self::RunCompleted { .. } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn every_event() -> Vec<Event> {
        let mut events = vec![
            Event::RunStarted {
                definition: "agent".into(),
                version: 1,
                input: json!({"q": 1}),
                deadline_ms: Some(5),
                spend_ceiling: None,
            },
            Event::StepScheduled {
                step: 0,
                kind: StepKind::Activity,
                name: "click".into(),
                digest: "d".into(),
                input: Some(json!([])),
                policy: "p".into(),
            },
            Event::StepStarted { step: 0, attempt: 1, fence: 2, at_ms: 3, reserved: 4 },
            Event::StepHeartbeat { step: 0, attempt: 1, progress: json!({"done": 1}) },
        ];
        events.extend(ending_events());
        events
    }

    fn ending_events() -> Vec<Event> {
        vec![
            Event::StepCompleted { step: 0, attempt: 1, result: json!("ok"), cost: 0, blobs: vec![], release: vec![1] },
            Event::StepFailed {
                step: 0,
                attempt: 1,
                failure: Failure::retryable("timeout", "slow"),
                retry_at_ms: Some(9),
                cost: 1,
            },
            Event::StepUncertain { step: 0, attempt: 1 },
            Event::DeferredResolved { step: 0, result: json!(null) },
            Event::CancelRequested {},
            Event::RunCompleted { outcome: Outcome::Limited { kind: LimitKind::Steps, progress: json!({}) } },
        ]
    }

    #[test]
    fn every_event_round_trips_through_its_canonical_encoding() {
        for event in every_event() {
            let encoded = event.encode(1 << 20).unwrap();
            assert_eq!(Event::decode(encoded.as_bytes(), 1 << 20).unwrap(), event);
        }
    }

    #[test]
    fn every_outcome_round_trips() {
        for outcome in [
            Outcome::Done { result: json!(1) },
            Outcome::NeedsPerson { request: json!("why") },
            Outcome::Cancelled { progress: json!(null) },
            Outcome::Limited { kind: LimitKind::Spend, progress: json!([]) },
            Outcome::Expired { progress: json!({}) },
            Outcome::Unrecoverable { reason: "definition-changed".into() },
        ] {
            let value = serde_json::to_value(&outcome).unwrap();
            assert_eq!(serde_json::from_value::<Outcome>(value).unwrap(), outcome);
        }
    }

    #[test]
    fn unknown_fields_and_kinds_are_refused() {
        for text in [
            r#"{"cancel_requested":{"x":1}}"#,
            r#"{"step_uncertain":{"step":0,"attempt":1,"extra":true}}"#,
            r#"{"run_completed":{"outcome":{"outcome":"done","result":1,"extra":2}}}"#,
            r#"{"step_scheduled":{"step":0,"kind":"teleport","name":"x","digest":"d","input":null,"policy":""}}"#,
            r#"{"nonsense":{}}"#,
            r#"{"step_uncertain":{"step":0,"attempt":1}"#,
            r#"{"step_uncertain":{"step":0,"step":0,"attempt":1}}"#,
        ] {
            assert!(matches!(Event::decode(text.as_bytes(), 1 << 20), Err(Error::Storage(_))), "{text}");
        }
    }

    #[test]
    fn oversized_events_are_refused_both_ways() {
        let event = Event::DeferredResolved { step: 0, result: json!("x".repeat(64)) };
        assert_eq!(event.encode(32), Err(Error::Limit(LimitKind::Value)));
        let encoded = event.encode(1 << 20).unwrap();
        assert!(matches!(Event::decode(encoded.as_bytes(), 32), Err(Error::Storage(_))));
    }

    #[test]
    fn events_too_deep_to_read_back_are_never_encoded() {
        let mut value = json!(1);
        for _ in 0..json::MAX_DEPTH - 1 {
            value = json!([value]);
        }
        assert!(json::parse(json::to_canonical(&value).as_bytes(), 1 << 20).is_ok());
        let event = Event::DeferredResolved { step: 0, result: value };
        assert_eq!(event.encode(1 << 20), Err(Error::Limit(LimitKind::Value)));
    }

    #[test]
    fn step_numbers_are_exposed() {
        let steps: Vec<_> = every_event().iter().map(Event::step).collect();
        assert_eq!(steps, [None, Some(0), Some(0), Some(0), Some(0), Some(0), Some(0), Some(0), None, None]);
    }
}
