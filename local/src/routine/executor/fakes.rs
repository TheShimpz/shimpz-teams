//! In-memory doubles of the Routine seams: an Action runner over a journal whose operations move exactly as the real
//! one's do (`prepared`, `executing`, `completed`, `no_effect`), Docker's view of a workload, and the routine key.

use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_action::executor::{
    ActionBatch, ActionFailure as Refusal, ActionRequest, BatchScope, InvokeOutcome,
};
use team_action::failure::ActionFailure;
use team_action::journal::OperationRecord;
use team_core::future::BoxFuture;
use team_core::stop::Stop;

use super::{
    DispatchHooks, Failed, FaultClass, JournalUnavailable, RoutineActions, RoutineJournal,
    RoutineKey, WorkloadState, Workloads,
};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The workload every fake dispatch is sent to.
pub const WORKLOAD: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

/// One generation's batch.
#[derive(Clone, Debug)]
pub struct FakeBatchState {
    /// Its fingerprint.
    pub fingerprint: String,
    /// `open`, `ended`, or `archived`.
    pub state: String,
    /// Its operations, in order.
    pub operations: Vec<OperationRecord>,
    /// Each completed operation's result, by interrupt.
    pub results: BTreeMap<String, Value>,
}

/// The journal: each generation's one batch.
#[derive(Debug, Default)]
pub struct FakeJournal {
    /// The batches.
    pub batches: Mutex<BTreeMap<String, FakeBatchState>>,
    /// While set, every journal read and write fails.
    pub failing: AtomicBool,
    /// Every generation purged or discarded, in order.
    pub removed: Mutex<Vec<String>>,
}

impl FakeJournal {
    fn checked(
        &self,
    ) -> Result<MutexGuard<'_, BTreeMap<String, FakeBatchState>>, JournalUnavailable> {
        if self.failing.load(Ordering::SeqCst) {
            return Err(JournalUnavailable);
        }
        Ok(lock(&self.batches))
    }

    /// The state of every operation of `generation`'s batch.
    pub fn states(&self, generation: &str) -> Vec<String> {
        lock(&self.batches)
            .get(generation)
            .map(|batch| {
                batch
                    .operations
                    .iter()
                    .map(|item| item.state.clone())
                    .collect()
            })
            .unwrap_or_default()
    }
}

impl RoutineJournal for FakeJournal {
    fn current_batch(
        &self,
        generation: &str,
    ) -> Result<Option<(String, String)>, JournalUnavailable> {
        Ok(self
            .checked()?
            .get(generation)
            .map(|batch| (batch.fingerprint.clone(), batch.state.clone())))
    }

    fn snapshot(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<Vec<OperationRecord>, JournalUnavailable> {
        match self.checked()?.get(generation) {
            Some(batch) if batch.fingerprint == fingerprint && batch.state != "archived" => {
                Ok(batch.operations.clone())
            }
            _ => Err(JournalUnavailable),
        }
    }

    fn archive(&self, generation: &str, fingerprint: &str) -> Result<(), JournalUnavailable> {
        match self.checked()?.get_mut(generation) {
            Some(batch) if batch.fingerprint == fingerprint => {
                "archived".clone_into(&mut batch.state);
                Ok(())
            }
            _ => Err(JournalUnavailable),
        }
    }

    fn release_archive(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<(), JournalUnavailable> {
        let mut batches = self.checked()?;
        if batches
            .get(generation)
            .is_some_and(|batch| batch.fingerprint == fingerprint)
        {
            batches.remove(generation);
        }
        Ok(())
    }

    fn discard(&self, generation: &str) -> Result<(), JournalUnavailable> {
        let mut batches = self.checked()?;
        if batches
            .get(generation)
            .is_some_and(|batch| batch.state != "archived")
        {
            batches.remove(generation);
        }
        lock(&self.removed).push(generation.to_owned());
        Ok(())
    }

    fn purge(&self, generation: &str) -> Result<(), JournalUnavailable> {
        self.checked()?.remove(generation);
        lock(&self.removed).push(generation.to_owned());
        Ok(())
    }

    fn uncertain_fingerprint(
        &self,
        generation: &str,
    ) -> Result<Option<String>, JournalUnavailable> {
        Ok(self.checked()?.get(generation).and_then(|batch| {
            batch
                .operations
                .iter()
                .any(|item| item.state == "executing")
                .then(|| batch.fingerprint.clone())
        }))
    }

    fn end_settled(&self, generation: &str) -> Result<(), JournalUnavailable> {
        let mut batches = self.checked()?;
        let settled = batches.get(generation).is_some_and(|batch| {
            batch.state == "open"
                && batch
                    .operations
                    .iter()
                    .all(|item| item.state != "executing")
        });
        if settled {
            batches.remove(generation);
        }
        Ok(())
    }
}

/// How one dispatched Action answers.
#[derive(Clone, Debug)]
pub enum Outcome {
    /// It completes with this result.
    Complete(Value),
    /// It asks a person.
    Human(Value),
    /// It fails as Team classifies it, with the handled failure document or transport condition it carried.
    Fail(FaultClass, Option<ActionFailure>, Option<String>),
    /// It runs until the run's Stop cuts it; its outcome is then unknown.
    UntilStopped,
}

/// Answers each dispatch with the next scripted outcome, then completes with `{}`.
#[derive(Default)]
pub struct FakeRoutineActions {
    /// The journal it keeps.
    pub journal: Arc<FakeJournal>,
    script: Mutex<VecDeque<Outcome>>,
    /// Each dispatch: its interrupt, logical operation, and input.
    pub dispatched: Mutex<Vec<(String, String, Value)>>,
    /// The values each dispatch injects, reported to its hooks.
    pub injected: Mutex<Vec<String>>,
    minted: AtomicU64,
}

impl std::fmt::Debug for FakeRoutineActions {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("FakeRoutineActions")
    }
}

impl FakeRoutineActions {
    /// Queues the next dispatch's outcome.
    pub fn answer(&self, outcome: Outcome) {
        lock(&self.script).push_back(outcome);
    }

    /// Each dispatch's interrupt, in order.
    pub fn interrupts(&self) -> Vec<String> {
        lock(&self.dispatched)
            .iter()
            .map(|item| item.0.clone())
            .collect()
    }

    /// Each dispatch's logical operation, in order.
    pub fn operations(&self) -> Vec<String> {
        lock(&self.dispatched)
            .iter()
            .map(|item| item.1.clone())
            .collect()
    }

    fn mint(&self) -> String {
        format!("{:032x}", self.minted.fetch_add(1, Ordering::SeqCst) + 1)
    }

    fn next(&self) -> Outcome {
        lock(&self.script)
            .pop_front()
            .unwrap_or(Outcome::Complete(Value::Object(Vec::new())))
    }
}

impl RoutineActions for Arc<FakeRoutineActions> {
    fn batch(&self, scope: BatchScope, hooks: Arc<dyn DispatchHooks>) -> Box<dyn ActionBatch> {
        Box::new(FakeBatch {
            actions: Arc::clone(self),
            scope,
            hooks,
        })
    }
}

struct FakeBatch {
    actions: Arc<FakeRoutineActions>,
    scope: BatchScope,
    hooks: Arc<dyn DispatchHooks>,
}

fn fingerprint(thread_id: &str, requests: &[ActionRequest]) -> String {
    let mut digest = Sha256::new();
    digest.update(thread_id.as_bytes());
    for request in requests {
        digest.update(request.interrupt_id.as_bytes());
        digest.update(team_protocol::python::json::canonical(&request.input));
    }
    crate::chat::hex(&digest.finalize())
}

impl FakeBatch {
    fn generation(&self) -> &str {
        &self.scope.generation
    }

    fn fresh(&self, requests: &[ActionRequest], fingerprint: String) -> FakeBatchState {
        let operations = requests
            .iter()
            .map(|request| OperationRecord {
                interrupt_id: request.interrupt_id.clone(),
                operation_id: self
                    .hooks
                    .logical_operation(request)
                    .unwrap_or_else(|| self.actions.mint()),
                state: "prepared".to_owned(),
                attempts: 0,
                origin: None,
            })
            .collect();
        FakeBatchState {
            fingerprint,
            state: "open".to_owned(),
            operations,
            results: BTreeMap::new(),
        }
    }

    fn prepared(&self, requests: &[ActionRequest]) -> Result<(), Refusal> {
        let journal = &self.actions.journal;
        let mut batches = journal.checked().map_err(|_| Refusal::StateUnavailable)?;
        let fingerprint = fingerprint(&self.scope.thread_id, requests);
        match batches.get(self.generation()) {
            Some(batch) if batch.state == "archived" => return Err(Refusal::StateUnavailable),
            Some(batch) if batch.fingerprint == fingerprint => return Ok(()),
            Some(batch)
                if batch
                    .operations
                    .iter()
                    .any(|item| item.state == "executing") =>
            {
                return Err(Refusal::Uncertain);
            }
            _ => {}
        }
        let fresh = self.fresh(requests, fingerprint);
        batches.insert(self.generation().to_owned(), fresh);
        Ok(())
    }

    /// The operation's journal state and id, or its replayed result.
    fn begin(&self, request: &ActionRequest) -> Result<Result<String, Value>, Refusal> {
        let journal = &self.actions.journal;
        let batches = journal.checked().map_err(|_| Refusal::StateUnavailable)?;
        let batch = batches
            .get(self.generation())
            .ok_or(Refusal::StateUnavailable)?;
        let operation = batch
            .operations
            .iter()
            .find(|item| item.interrupt_id == request.interrupt_id)
            .ok_or(Refusal::StateUnavailable)?;
        match operation.state.as_str() {
            "completed" => Ok(Err(batch
                .results
                .get(&request.interrupt_id)
                .cloned()
                .unwrap_or(Value::Null))),
            "executing" => Err(Refusal::Uncertain),
            _ => Ok(Ok(operation.operation_id.clone())),
        }
    }

    fn set(&self, interrupt: &str, state: &str, result: Option<Value>) {
        let mut batches = lock(&self.actions.journal.batches);
        let Some(batch) = batches.get_mut(&self.scope.generation) else {
            return;
        };
        for item in &mut batch.operations {
            if item.interrupt_id == interrupt {
                state.clone_into(&mut item.state);
                if state == "executing" {
                    item.attempts += 1;
                }
            }
        }
        if let Some(result) = result {
            batch.results.insert(interrupt.to_owned(), result);
        }
    }

    fn failed(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        fault: (FaultClass, Option<ActionFailure>, Option<String>),
    ) -> Refusal {
        let (class, failure, condition) = fault;
        let protected = lock(&self.actions.injected).clone();
        let failed = Failed {
            operation_id: operation_id.to_owned(),
            fault: class,
            failure,
            condition,
            protected,
        };
        self.hooks.failed(request, &failed);
        if class == FaultClass::Handled {
            self.set(&request.interrupt_id, "no_effect", None);
            return Refusal::Failed;
        }
        if class == FaultClass::Unquiesced {
            return Refusal::Blocked;
        }
        Refusal::Uncertain
    }

    async fn dispatch(
        &self,
        request: &ActionRequest,
        stop: &Stop,
    ) -> Result<InvokeOutcome, Refusal> {
        if stop.is_stopped() {
            return Err(Refusal::Stopped);
        }
        let operation_id = match self.begin(request)? {
            Ok(operation_id) => operation_id,
            Err(result) => return Ok(InvokeOutcome::Completed(result)),
        };
        self.set(&request.interrupt_id, "executing", None);
        let injected = lock(&self.actions.injected).clone();
        if self
            .hooks
            .dispatching(request, &operation_id, WORKLOAD, &injected)
            .is_err()
        {
            self.set(&request.interrupt_id, "prepared", None);
            return Err(Refusal::StateUnavailable);
        }
        lock(&self.actions.dispatched).push((
            request.interrupt_id.clone(),
            operation_id.clone(),
            request.input.clone(),
        ));
        self.answered(request, &operation_id, stop).await
    }

    async fn answered(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        stop: &Stop,
    ) -> Result<InvokeOutcome, Refusal> {
        match self.actions.next() {
            Outcome::Complete(result) => {
                self.hooks.returned(request);
                self.set(&request.interrupt_id, "completed", Some(result.clone()));
                Ok(InvokeOutcome::Completed(result))
            }
            Outcome::Human(human) => {
                self.hooks.returned(request);
                self.set(&request.interrupt_id, "prepared", None);
                Ok(InvokeOutcome::HumanRequest(human))
            }
            Outcome::Fail(class, failure, condition) => {
                Err(self.failed(request, operation_id, (class, failure, condition)))
            }
            Outcome::UntilStopped => {
                stop.stopped().await;
                let fault = (FaultClass::Transport, None, None);
                Err(self.failed(request, operation_id, fault))
            }
        }
    }
}

impl ActionBatch for FakeBatch {
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), Refusal>> {
        Box::pin(async move { self.prepared(requests) })
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, Refusal>> {
        Box::pin(async move { self.dispatch(request, stop).await })
    }

    fn delivered<'a>(
        &'a mut self,
        _requests: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<(), Refusal>> {
        Box::pin(async move {
            let journal = &self.actions.journal;
            let mut batches = journal.checked().map_err(|_| Refusal::StateUnavailable)?;
            batches.remove(&self.scope.generation);
            Ok(())
        })
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, Refusal>> {
        Box::pin(async move {
            let journal = &self.actions.journal;
            let mut batches = journal.checked().map_err(|_| Refusal::StateUnavailable)?;
            let uncertain = batches.get(&self.scope.generation).is_some_and(|batch| {
                batch.state == "archived"
                    || batch
                        .operations
                        .iter()
                        .any(|item| item.state == "executing")
            });
            if uncertain {
                return Ok(false);
            }
            batches.remove(&self.scope.generation);
            Ok(true)
        })
    }
}

/// Docker's view of every workload: one answer for all.
#[derive(Debug, Default)]
pub struct FakeWorkloads {
    /// What Docker says; `None` when it cannot be read.
    pub state: Mutex<Option<WorkloadState>>,
}

impl Workloads for FakeWorkloads {
    fn state<'a>(
        &'a self,
        _team_id: &'a str,
        _assistant_id: &'a str,
        _workload: &'a str,
    ) -> BoxFuture<'a, Option<WorkloadState>> {
        Box::pin(async move { *lock(&self.state) })
    }
}

/// The routine key: present unless cleared.
#[derive(Debug)]
pub struct FakeKey(pub Mutex<Option<String>>);

impl Default for FakeKey {
    fn default() -> Self {
        Self(Mutex::new(Some("e".repeat(64))))
    }
}

impl RoutineKey for FakeKey {
    fn fingerprint(&self) -> Option<String> {
        lock(&self.0).clone()
    }
}
