//! Test doubles for hosts' tests and this crate's own: a scripted model, a recording activity, and a host whose
//! answers the test controls.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use serde_json::Value;

use crate::handler::{Activity, ActivityCall, ActivityReply, Model, ModelCall, ModelReply};
use crate::host::{Authority, Credentials, Host, Proof, RunInfo};
use crate::policy::Failure;
use crate::store::Obligation;

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A model that answers from a script, in order, and records every request it receives.
#[derive(Debug, Default, Clone)]
pub struct ScriptedModel {
    script: Arc<Mutex<VecDeque<Result<ModelReply, Failure>>>>,
    requests: Arc<Mutex<Vec<Value>>>,
    reserve: u64,
}

impl ScriptedModel {
    /// A model that answers `responses` in order, each costing `cost`.
    #[must_use]
    pub fn new(responses: impl IntoIterator<Item = Value>, cost: u64) -> Self {
        let script = responses.into_iter().map(|response| Ok(ModelReply { response, cost })).collect();
        Self { script: Arc::new(Mutex::new(script)), ..Self::default() }
    }

    /// Appends one scripted answer or failure.
    pub fn push(&self, answer: Result<ModelReply, Failure>) {
        lock(&self.script).push_back(answer);
    }

    /// Reserves `reserve` before each attempt.
    #[must_use]
    pub fn reserving(mut self, reserve: u64) -> Self {
        self.reserve = reserve;
        self
    }

    /// Every request received so far.
    #[must_use]
    pub fn requests(&self) -> Vec<Value> {
        lock(&self.requests).clone()
    }
}

impl Model for ScriptedModel {
    fn reserve(&self, _request: &Value) -> Option<u64> {
        Some(self.reserve)
    }

    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        lock(&self.requests).push(call.request.clone());
        lock(&self.script)
            .pop_front()
            .unwrap_or_else(|| Err(Failure::permanent("script-ended", "no scripted answer left")))
    }
}

/// What a [`RecordingActivity`] does on each attempt.
#[derive(Debug, Clone)]
pub enum Behavior {
    /// Returns its input.
    Echo,
    /// Returns its input together with one blob.
    EchoWithBlob(Vec<u8>),
    /// Fails with this failure.
    Fail(Failure),
    /// Sleeps (heartbeating `progress` first when set), then returns its input; checks for abandonment every 5 ms.
    Sleep {
        /// How long.
        ms: u64,
        /// The heartbeat sent first.
        progress: Option<Value>,
    },
    /// Blocks until abandoned, then keeps "acting" for `linger_ms` before returning.
    Hang {
        /// How long it keeps acting after it was abandoned.
        linger_ms: u64,
    },
    /// Panics.
    Panic,
    /// Returns this value, whatever the input.
    Value(Value),
    /// Returns its input with `count` blobs of `size` bytes each.
    Blobs {
        /// How many blobs.
        count: usize,
        /// The size of each.
        size: usize,
    },
}

/// One attempt a [`RecordingActivity`] received.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordedCall {
    /// The step's effect key.
    pub effect_key: String,
    /// The attempt number.
    pub attempt: u32,
    /// The session's fencing token.
    pub fence: u64,
    /// The progress the attempt received.
    pub progress: Option<Value>,
}

/// An activity that records every attempt and follows a script of behaviors (the last one repeats).
#[derive(Debug, Clone)]
pub struct RecordingActivity {
    behaviors: Arc<Mutex<VecDeque<Behavior>>>,
    calls: Arc<Mutex<Vec<RecordedCall>>>,
}

impl RecordingActivity {
    /// An activity following `behaviors`.
    #[must_use]
    pub fn new(behaviors: impl IntoIterator<Item = Behavior>) -> Self {
        Self { behaviors: Arc::new(Mutex::new(behaviors.into_iter().collect())), calls: Arc::default() }
    }

    /// Every attempt so far.
    #[must_use]
    pub fn calls(&self) -> Vec<RecordedCall> {
        lock(&self.calls).clone()
    }

    fn next(&self) -> Behavior {
        let mut behaviors = lock(&self.behaviors);
        if behaviors.len() > 1 {
            behaviors.pop_front().unwrap_or(Behavior::Echo)
        } else {
            behaviors.front().cloned().unwrap_or(Behavior::Echo)
        }
    }
}

impl Activity for RecordingActivity {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let attempt = call.attempt;
        lock(&self.calls).push(RecordedCall {
            effect_key: attempt.effect_key(),
            attempt: attempt.attempt,
            fence: attempt.fence,
            progress: attempt.progress.clone(),
        });
        match self.next() {
            Behavior::Echo => Ok(ActivityReply::of(call.input.clone())),
            Behavior::EchoWithBlob(blob) => Ok(ActivityReply { result: call.input.clone(), blobs: vec![blob] }),
            Behavior::Fail(failure) => Err(failure),
            Behavior::Sleep { ms, progress } => {
                if let Some(progress) = progress {
                    attempt.heartbeat(progress).map_err(|_| Failure::permanent("abandoned", "abandoned"))?;
                }
                for _ in 0..ms.div_ceil(5) {
                    attempt.check().map_err(|_| Failure::permanent("abandoned", "abandoned"))?;
                    std::thread::sleep(Duration::from_millis(5));
                }
                Ok(ActivityReply::of(call.input.clone()))
            }
            Behavior::Hang { linger_ms } => {
                while attempt.check().is_ok() {
                    std::thread::sleep(Duration::from_millis(2));
                }
                std::thread::sleep(Duration::from_millis(linger_ms));
                Err(Failure::retryable("hung", "it hung"))
            }
            Behavior::Panic => std::panic::resume_unwind(Box::new("scripted panic")),
            Behavior::Value(value) => Ok(ActivityReply::of(value)),
            Behavior::Blobs { count, size } => {
                Ok(ActivityReply { result: call.input.clone(), blobs: vec![vec![7; size]; count] })
            }
        }
    }
}

/// A host whose answers a test sets.
#[derive(Debug, Clone)]
pub struct TestHost {
    state: Arc<Mutex<HostState>>,
}

#[derive(Debug)]
struct HostState {
    authority: HostAuthority,
    proof: Proof,
    release_fails: bool,
    authorized: u32,
    proofs: Vec<Obligation>,
    released: Vec<u64>,
}

/// What [`TestHost::authorize`](crate::Host::authorize) answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostAuthority {
    /// Grants credentials holding the string `test-token`.
    Granted,
    /// Credentials are not available yet.
    Wait,
    /// The authority was revoked.
    Revoked,
}

impl Default for TestHost {
    fn default() -> Self {
        Self {
            state: Arc::new(Mutex::new(HostState {
                authority: HostAuthority::Granted,
                proof: Proof::Ended,
                release_fails: false,
                authorized: 0,
                proofs: vec![],
                released: vec![],
            })),
        }
    }
}

impl TestHost {
    /// Sets the authority answer.
    pub fn set_authority(&self, authority: HostAuthority) {
        lock(&self.state).authority = authority;
    }

    /// Sets the proof answer.
    pub fn set_proof(&self, proof: Proof) {
        lock(&self.state).proof = proof;
    }

    /// Makes every clean release fail.
    pub fn fail_releases(&self) {
        lock(&self.state).release_fails = true;
    }

    /// How many times authority was asked for.
    #[must_use]
    pub fn authorized(&self) -> u32 {
        lock(&self.state).authorized
    }

    /// Every obligation the host was asked to prove.
    #[must_use]
    pub fn proofs(&self) -> Vec<Obligation> {
        lock(&self.state).proofs.clone()
    }

    /// The fences of every clean release.
    #[must_use]
    pub fn released(&self) -> Vec<u64> {
        lock(&self.state).released.clone()
    }
}

impl Host for TestHost {
    fn authorize(&self, _run: &RunInfo<'_>) -> Authority {
        let mut state = lock(&self.state);
        state.authorized += 1;
        match state.authority {
            HostAuthority::Granted => Authority::Granted(Credentials::new(String::from("test-token"))),
            HostAuthority::Wait => Authority::Wait,
            HostAuthority::Revoked => Authority::Revoked,
        }
    }

    fn prove_ended(&self, _run: &RunInfo<'_>, obligation: &Obligation) -> Proof {
        let mut state = lock(&self.state);
        state.proofs.push(obligation.clone());
        state.proof
    }

    fn release(&self, _run: &RunInfo<'_>, fence: u64) -> Result<(), String> {
        let mut state = lock(&self.state);
        if state.release_fails {
            return Err("release failed".into());
        }
        state.released.push(fence);
        Ok(())
    }
}

/// A detached attempt for calling a [`Model`] or [`Activity`] directly in a test: never abandoned, its heartbeats
/// go nowhere.
#[must_use]
pub fn attempt(run_id: &str, step: u32, deadline_ms: Option<i64>) -> crate::handler::Attempt {
    crate::handler::Attempt {
        run_id: run_id.to_owned(),
        step,
        attempt: 1,
        fence: 1,
        progress: None,
        deadline_ms,
        abandoned: Arc::default(),
        beats: std::sync::mpsc::sync_channel(1).0,
    }
}
