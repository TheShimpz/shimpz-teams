//! Every Team's one execution slot and the Space's chat admission (ADR-0079, ADR-0086, ADR-0119).
//!
//! A Team runs at most one execution at a time: a chat turn, or a Routine run's segment. Admission is non-blocking
//! and, under one guard, refuses while the Space resets and counts a chat turn against the Space chat limit in the
//! same step that registers it. Stop, a Team's destruction, and the Space reset cancel an execution through its
//! [`Stop`]; destruction then waits for the slot, and the reset drains every slot.

use std::collections::HashMap;
use std::sync::{Arc, Mutex as StdMutex, MutexGuard, PoisonError};
use std::time::Duration;

use team_core::stop::Stop;
use tokio::sync::{Mutex, OwnedMutexGuard};
use tokio::time::{Instant, timeout, timeout_at};

/// How long a Team's destruction waits for its slot, and the Space reset for every slot.
pub const DRAIN: Duration = Duration::from_secs(30);

/// The Space chat limit's store (`inference/chat-limit.json`, 1 or 2). [`ChatLimit::current`] is read under the
/// admission guard, so it must answer from memory without blocking.
pub trait ChatLimit: Send + Sync {
    /// The current limit.
    fn current(&self) -> usize;

    /// Durably saves `limit` and returns it.
    ///
    /// # Errors
    /// [`LimitUnavailable`] when it could not be saved; the old limit stays.
    fn save(&self, limit: u8) -> Result<u8, LimitUnavailable>;
}

/// The chat limit could not be saved.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LimitUnavailable;

/// What an execution is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExecutionKind {
    /// A chat turn, counted against the Space chat limit.
    Chat,
    /// A Routine run's segment, never counted against the chat limit.
    Routine {
        /// The Routine whose run holds the slot.
        routine_id: String,
    },
}

/// Why an execution was not admitted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The Team's slot is held by a chat turn or a lifecycle operation (`chat-active`).
    ChatActive,
    /// The Team's slot is held by a Routine run's segment (`routine-active`).
    RoutineActive,
    /// The Space is being reset (`space-resetting`).
    SpaceResetting,
    /// The Space already runs as many chat turns as its limit (`space-busy`).
    SpaceBusy,
}

/// The slot (or every slot) was not free within the wait.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DrainTimeout;

/// A person's demand for a Team whose slot a Routine held: chat goes first at the next boundary.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Demand {
    /// The Routine still holds the slot.
    Waiting,
    /// The Routine freed the slot at this instant; the demand's bounded grace starts here.
    Since(Instant),
}

/// Every Team's execution slot.
#[derive(Clone)]
pub struct Executions {
    inner: Arc<Inner>,
}

struct Inner {
    state: StdMutex<State>,
    limit: Arc<dyn ChatLimit>,
}

#[derive(Default)]
struct State {
    closed: bool,
    slots: HashMap<String, Arc<Mutex<()>>>,
    active: HashMap<String, Active>,
    demand: HashMap<String, Demand>,
}

struct Active {
    token: String,
    stop: Stop,
    routine: Option<String>,
    cancelled: bool,
}

impl State {
    fn chat_executions(&self) -> usize {
        self.active
            .values()
            .filter(|active| active.routine.is_none())
            .count()
    }
}

impl std::fmt::Debug for Executions {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Executions")
    }
}

impl Executions {
    /// No execution yet; `limit` is the Space chat limit's store.
    #[must_use]
    pub fn new(limit: Arc<dyn ChatLimit>) -> Self {
        Self {
            inner: Arc::new(Inner {
                state: StdMutex::new(State::default()),
                limit,
            }),
        }
    }

    fn state(&self) -> MutexGuard<'_, State> {
        // A panic while the guard was held leaves only bookkeeping behind; the state stays usable.
        self.inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// The Team's slot, shared by every holder and waiter; slots nobody references are forgotten.
    fn slot(&self, team_id: &str) -> Arc<Mutex<()>> {
        let mut state = self.state();
        state.slots.retain(|_, slot| Arc::strong_count(slot) > 1);
        Arc::clone(state.slots.entry(team_id.to_owned()).or_default())
    }

    /// Admits one execution of `team_id` with the fresh `token`, or refuses at once.
    ///
    /// # Errors
    /// [`Refusal`] when the slot is held, the Space is resetting, or a chat turn would exceed the chat limit.
    pub fn admit(
        &self,
        team_id: &str,
        kind: ExecutionKind,
        token: String,
    ) -> Result<Execution, Refusal> {
        let Ok(held) = self.slot(team_id).try_lock_owned() else {
            return Err(self.refuse_held(team_id, &kind));
        };
        let mut state = self.state();
        if state.closed {
            return Err(Refusal::SpaceResetting);
        }
        let routine = match kind {
            ExecutionKind::Chat if state.chat_executions() >= self.inner.limit.current() => {
                return Err(Refusal::SpaceBusy);
            }
            ExecutionKind::Chat => {
                state.demand.remove(team_id);
                None
            }
            ExecutionKind::Routine { routine_id } => Some(routine_id),
        };
        let stop = Stop::new();
        let is_routine = routine.is_some();
        let active = Active {
            token: token.clone(),
            stop: stop.clone(),
            routine,
            cancelled: false,
        };
        state.active.insert(team_id.to_owned(), active);
        drop(state);
        Ok(Execution {
            executions: self.clone(),
            team_id: team_id.to_owned(),
            token,
            stop,
            is_routine,
            _held: held,
        })
    }

    fn refuse_held(&self, team_id: &str, kind: &ExecutionKind) -> Refusal {
        let mut state = self.state();
        let routine = state
            .active
            .get(team_id)
            .is_some_and(|active| active.routine.is_some());
        if !routine {
            return Refusal::ChatActive;
        }
        if *kind == ExecutionKind::Chat {
            state.demand.insert(team_id.to_owned(), Demand::Waiting);
        }
        Refusal::RoutineActive
    }

    /// Cancels `team_id`'s registered execution (sets its Stop) and returns its token; with `spare_routine`, a
    /// Routine segment holding the slot keeps running.
    #[must_use]
    pub fn cancel(&self, team_id: &str, spare_routine: bool) -> Option<String> {
        let mut state = self.state();
        let active = state.active.get_mut(team_id)?;
        if spare_routine && active.routine.is_some() {
            return None;
        }
        active.cancelled = true;
        active.stop.stop();
        Some(active.token.clone())
    }

    /// Waits up to `wait` for `team_id`'s slot and holds it (a Team's destruction, after cancelling its execution).
    ///
    /// # Errors
    /// [`DrainTimeout`] when the slot did not free in time.
    pub async fn exclusive(
        &self,
        team_id: &str,
        wait: Duration,
    ) -> Result<SlotGuard, DrainTimeout> {
        let slot = self.slot(team_id);
        let held = timeout(wait, slot.lock_owned())
            .await
            .map_err(|_| DrainTimeout)?;
        Ok(SlotGuard { _held: held })
    }

    /// Closes admission Space-wide, cancels every registered execution, and holds every slot that exists, all within
    /// `wait` (the Space reset). Admission reopens when the returned guard drops, or at once on a timeout.
    ///
    /// # Errors
    /// [`DrainTimeout`] when a slot did not free in time.
    pub async fn drain(&self, wait: Duration) -> Result<Drained, DrainTimeout> {
        let (running, slots) = {
            let mut state = self.state();
            state.closed = true;
            let running: Vec<String> = state.active.keys().cloned().collect();
            let slots: Vec<Arc<Mutex<()>>> = state.slots.values().cloned().collect();
            (running, slots)
        };
        // Built before waiting, so a timeout reopens admission when it drops.
        let mut drained = Drained {
            executions: self.clone(),
            held: Vec::with_capacity(slots.len()),
        };
        for team_id in running {
            let _cancelled = self.cancel(&team_id, false);
        }
        let deadline = Instant::now() + wait;
        for slot in slots {
            let held = timeout_at(deadline, slot.lock_owned())
                .await
                .map_err(|_| DrainTimeout)?;
            drained.held.push(held);
        }
        Ok(drained)
    }

    /// Saves the chat limit under the admission guard, so no admission counts against an older limit.
    ///
    /// # Errors
    /// [`LimitUnavailable`] when it could not be saved.
    pub fn save_limit(&self, limit: u8) -> Result<u8, LimitUnavailable> {
        let _state = self.state();
        self.inner.limit.save(limit)
    }

    /// The chat turns holding a slot now, Routine segments not among them.
    #[must_use]
    pub fn chat_executions(&self) -> usize {
        self.state().chat_executions()
    }

    /// The Routine whose segment holds `team_id`'s slot, if one does.
    #[must_use]
    pub fn routine_holding(&self, team_id: &str) -> Option<String> {
        self.state()
            .active
            .get(team_id)
            .and_then(|active| active.routine.clone())
    }

    /// A person's pending demand for `team_id`, if any.
    #[must_use]
    pub fn demand(&self, team_id: &str) -> Option<Demand> {
        self.state().demand.get(team_id).copied()
    }

    /// Forgets `team_id`'s demand (it was served or its grace ended).
    pub fn clear_demand(&self, team_id: &str) {
        self.state().demand.remove(team_id);
    }
}

/// One admitted execution: it holds the Team's slot until dropped.
pub struct Execution {
    executions: Executions,
    team_id: String,
    token: String,
    stop: Stop,
    is_routine: bool,
    _held: OwnedMutexGuard<()>,
}

impl std::fmt::Debug for Execution {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Execution")
            .field("team_id", &self.team_id)
            .finish_non_exhaustive()
    }
}

impl Execution {
    /// The Team it runs for.
    #[must_use]
    pub fn team_id(&self) -> &str {
        &self.team_id
    }

    /// Its token.
    #[must_use]
    pub fn token(&self) -> &str {
        &self.token
    }

    /// Its Stop, set when it is cancelled.
    #[must_use]
    pub fn stop(&self) -> &Stop {
        &self.stop
    }

    /// Whether it was cancelled, or already committed.
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.executions
            .state()
            .active
            .get(&self.team_id)
            .is_none_or(|active| active.token != self.token || active.cancelled)
    }

    /// Commits the execution's outcome only when Stop did not win: `before` runs under the admission guard, and only
    /// when it succeeds is the execution no longer registered (it keeps its slot until dropped). `None` when it was
    /// cancelled or already committed; `before` then never runs. `before` must not block.
    pub fn commit<T, E>(&self, before: impl FnOnce() -> Result<T, E>) -> Option<Result<T, E>> {
        let mut state = self.executions.state();
        let current = state.active.get(&self.team_id)?;
        if current.token != self.token || current.cancelled {
            return None;
        }
        let outcome = before();
        if outcome.is_ok() {
            state.active.remove(&self.team_id);
        }
        Some(outcome)
    }
}

impl Drop for Execution {
    fn drop(&mut self) {
        let mut state = self.executions.state();
        if state
            .active
            .get(&self.team_id)
            .is_some_and(|active| active.token == self.token)
        {
            state.active.remove(&self.team_id);
        }
        if self.is_routine && state.demand.contains_key(&self.team_id) {
            state
                .demand
                .insert(self.team_id.clone(), Demand::Since(Instant::now()));
        }
        // The slot (`_held`) is released after this, once the bookkeeping no longer names the execution.
    }
}

/// One Team's slot, held by its destruction.
#[derive(Debug)]
pub struct SlotGuard {
    _held: OwnedMutexGuard<()>,
}

/// Every slot held and admission closed, for the Space reset.
pub struct Drained {
    executions: Executions,
    held: Vec<OwnedMutexGuard<()>>,
}

impl std::fmt::Debug for Drained {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Drained")
            .field("slots", &self.held.len())
            .finish_non_exhaustive()
    }
}

impl Drop for Drained {
    fn drop(&mut self) {
        // Every slot is released first, then admission reopens.
        self.held.clear();
        self.executions.state().closed = false;
    }
}

#[cfg(test)]
mod tests;
