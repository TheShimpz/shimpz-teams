//! The traits a host implements to carry out steps: models and activities, and what one attempt receives.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::SyncSender;

use serde_json::Value;

use crate::host::Credentials;
use crate::policy::Failure;

/// The attempt was abandoned (timed out, cancelled, or its lease was lost); the handler must stop acting.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("the attempt was abandoned")]
pub struct Abandoned;

/// What one attempt of a step is, and its link back to the driving session. Clones share that link.
#[derive(Debug, Clone)]
pub struct Attempt {
    /// The run's identifier.
    pub run_id: String,
    /// The step's position in the run.
    pub step: u32,
    /// The attempt number, from 1.
    pub attempt: u32,
    /// The fencing token of the session that dispatched this attempt; an executor must refuse a stale one.
    pub fence: u64,
    /// The progress the previous attempt reported last, if any.
    pub progress: Option<Value>,
    /// The wall-clock time this attempt must finish by, if bounded.
    pub deadline_ms: Option<i64>,
    pub(crate) abandoned: Arc<AtomicBool>,
    pub(crate) beats: SyncSender<Value>,
}

impl Attempt {
    /// The stable effect key of this step, unchanged across attempts: `run_id/step`. Executors that deduplicate
    /// by it make an activity idempotent.
    #[must_use]
    pub fn effect_key(&self) -> String {
        format!("{}/{}", self.run_id, self.step)
    }

    /// Refuses once the attempt was abandoned. A handler calls it before each effect.
    ///
    /// # Errors
    ///
    /// [`Abandoned`] once the session gave up on this attempt.
    pub fn check(&self) -> Result<(), Abandoned> {
        if self.abandoned.load(Ordering::SeqCst) { Err(Abandoned) } else { Ok(()) }
    }

    /// Reports progress; the session journals it (coalesced) and a later attempt receives it.
    ///
    /// # Errors
    ///
    /// [`Abandoned`] once the session gave up on this attempt.
    pub fn heartbeat(&self, progress: Value) -> Result<(), Abandoned> {
        self.check()?;
        // A full channel means a heartbeat is already queued; the session journals the newest one it reads.
        let _ = self.beats.try_send(progress);
        Ok(())
    }
}

/// One call to a model.
#[derive(Debug)]
pub struct ModelCall<'a> {
    /// The request the definition built.
    pub request: &'a Value,
    /// The attempt.
    pub attempt: &'a Attempt,
    /// The session's credentials.
    pub credentials: &'a Credentials,
}

/// A model's answer and what it cost.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelReply {
    /// The response, in the definition's own shape.
    pub response: Value,
    /// What the call cost, in the host's cost unit.
    pub cost: u64,
}

/// A model provider. Calls have no side effects, so an interrupted call is always retried.
pub trait Model: Send + Sync {
    /// The worst-case cost of `request`, reserved before each attempt so retries can never exceed a run's ceiling;
    /// `None` (the default) when the model cannot estimate it, which a run with a spend ceiling refuses to call; a
    /// free model returns `Some(0)` explicitly.
    fn reserve(&self, request: &Value) -> Option<u64> {
        let _ = request;
        None
    }

    /// Calls the model. A failure marked [`Failure::ended`] cost nothing; any other failure keeps the attempt's
    /// whole reservation.
    ///
    /// # Errors
    ///
    /// The provider's failure, classified as retryable or not.
    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure>;
}

/// One call to an activity.
#[derive(Debug)]
pub struct ActivityCall<'a> {
    /// The step's input.
    pub input: &'a Value,
    /// The attempt.
    pub attempt: &'a Attempt,
    /// The session's credentials.
    pub credentials: &'a Credentials,
}

/// An activity's result.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ActivityReply {
    /// The result.
    pub result: Value,
    /// Transient blobs (a screenshot, for example) kept privately until their last consumer commits.
    pub blobs: Vec<Vec<u8>>,
}

impl ActivityReply {
    /// A reply with no blobs.
    #[must_use]
    pub fn of(result: Value) -> Self {
        Self { result, blobs: vec![] }
    }
}

/// An effect the host carries out.
pub trait Activity: Send + Sync {
    /// Carries out one attempt.
    ///
    /// # Errors
    ///
    /// The attempt's failure, classified as retryable or not, and marked [`Failure::ended`] when the executor
    /// confirmed it stopped without acting.
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure>;
}

impl<F> Activity for F
where
    F: Fn(ActivityCall<'_>) -> Result<ActivityReply, Failure> + Send + Sync,
{
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        self(call)
    }
}

impl<F> Model for F
where
    F: Fn(ModelCall<'_>) -> Result<ModelReply, Failure> + Send + Sync,
{
    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        self(call)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::sync_channel;

    fn attempt() -> (Attempt, std::sync::mpsc::Receiver<Value>) {
        let (beats, received) = sync_channel(1);
        let attempt = Attempt {
            run_id: "run".into(),
            step: 3,
            attempt: 1,
            fence: 2,
            progress: None,
            deadline_ms: None,
            abandoned: Arc::new(AtomicBool::new(false)),
            beats,
        };
        (attempt, received)
    }

    #[test]
    fn heartbeats_reach_the_session_until_the_attempt_is_abandoned() {
        let (attempt, received) = attempt();
        assert_eq!(attempt.effect_key(), "run/3");
        assert_eq!(attempt.heartbeat(Value::from(1)), Ok(()));
        assert_eq!(attempt.heartbeat(Value::from(2)), Ok(()));
        assert_eq!(received.try_recv(), Ok(Value::from(1)));
        attempt.abandoned.store(true, Ordering::SeqCst);
        assert_eq!(attempt.check(), Err(Abandoned));
        assert_eq!(attempt.heartbeat(Value::from(3)), Err(Abandoned));
    }

    #[test]
    fn closures_are_handlers() {
        let (attempt, _received) = attempt();
        let credentials = Credentials::none();
        let model = |call: ModelCall<'_>| Ok(ModelReply { response: call.request.clone(), cost: 1 });
        let request = Value::from("hi");
        let reply = Model::call(&model, ModelCall { request: &request, attempt: &attempt, credentials: &credentials });
        assert_eq!(reply.map(|reply| reply.response), Ok(Value::from("hi")));
        assert_eq!(Model::reserve(&model, &request), None);
        let activity = |call: ActivityCall<'_>| Ok(ActivityReply::of(call.input.clone()));
        let reply = activity.run(ActivityCall { input: &request, attempt: &attempt, credentials: &credentials });
        assert_eq!(reply, Ok(ActivityReply::of(Value::from("hi"))));
    }
}
