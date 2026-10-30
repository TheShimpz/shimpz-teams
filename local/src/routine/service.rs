//! The Local Routine service (`Group::Routines`, `local/http/routine.py`): what every Routine operation runs with,
//! and how it reads and writes a Team's Routine state, each write first marking the runs that lost their protection.

use std::collections::BTreeSet;
use std::sync::Arc;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_integrations::challenge::ChallengeError;
use team_routine::record::{TeamRoutines, Transition};
use team_storage::private::epoch_seconds;

use super::diagnostics::DiagnosticStore;
use super::executor::{RoutineActions, RoutineJournal, RoutineKey, Workloads};
use super::protection::RunProtections;
use super::state;
use super::store::{RoutineStore, TeamLock};
use crate::audit::{Outcome, Principal};
use crate::chat::ports::TeamView;
use crate::chat::problem;
use crate::chat::service::ChatService;
use crate::coordination::Coordination;
use crate::http::service::Problem;

#[cfg(test)]
pub(crate) mod harness;
pub mod http;
#[cfg(test)]
mod tests;

/// Everything the Routine service runs with.
#[derive(Clone)]
pub struct RoutineParts {
    /// The Space's id, which every run's journal thread names.
    pub space_id: String,
    /// The one coordination authority.
    pub coordination: Coordination,
    /// The Team as the controller holds it.
    pub team: Arc<dyn TeamView>,
    /// Runs a run's Actions.
    pub actions: Arc<dyn RoutineActions>,
    /// The Action journal incidents read and archive.
    pub journal: Arc<dyn RoutineJournal>,
    /// Docker's view of a held attempt's workload.
    pub workloads: Arc<dyn Workloads>,
    /// The routine key runs are claimed under.
    pub key: Arc<dyn RoutineKey>,
    /// Every Team's Routine state.
    pub store: Arc<RoutineStore>,
    /// Every run's diagnostics and step records.
    pub diagnostics: Arc<DiagnosticStore>,
    /// The values each run protects, in this Team boot.
    pub protections: Arc<RunProtections>,
    /// The chat service: its pending challenges hold Routines back, it seals Stored Input answers, and its
    /// [`crate::routine::proposal::RoutineChat`] holds the recordings and cards a Routine's lifecycle drops.
    pub chat: ChatService,
    /// The audit journal.
    pub audit: Arc<dyn AuditSink>,
    /// The wall clock.
    pub clock: Arc<dyn Clock>,
    /// The random source.
    pub entropy: Arc<dyn Entropy>,
}

/// The Routine service's state, shared by every task it runs.
pub struct Inner {
    pub(super) parts: RoutineParts,
    pub(super) registry: std::sync::Mutex<super::run::Registry>,
    pub(super) human: super::human::RoutineChallenges,
}

/// The Routine service; clones share one state.
#[derive(Clone)]
pub struct RoutineService(Arc<Inner>);

impl std::ops::Deref for RoutineService {
    type Target = Inner;

    fn deref(&self) -> &Inner {
        &self.0
    }
}

impl std::fmt::Debug for RoutineService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RoutineService")
    }
}

impl RoutineService {
    /// The service over `parts`.
    ///
    /// # Errors
    /// [`ChallengeError`] when the Routine human challenge store cannot be built.
    pub fn new(parts: RoutineParts) -> Result<Self, ChallengeError> {
        let human = super::human::challenges(Arc::clone(&parts.entropy))?;
        Ok(Self(Arc::new(Inner {
            parts,
            registry: std::sync::Mutex::default(),
            human,
        })))
    }

    /// The parts it runs with.
    #[must_use]
    pub fn parts(&self) -> &RoutineParts {
        &self.parts
    }

    /// Now, in whole epoch seconds.
    pub(super) fn now(&self) -> i64 {
        epoch_seconds(self.parts.clock.as_ref())
    }

    /// The Team's Routine state.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn load(&self, team_id: &str) -> Result<TeamRoutines, Problem> {
        state::load(&self.parts.store, team_id).map_err(|_| problem::routine_state_unavailable())
    }

    /// The runs of a Team whose protection is lost, under its held lock (`replay.lost_runs`).
    pub(super) fn lost_in(&self, lock: &TeamLock<'_>, state: &TeamRoutines) -> BTreeSet<String> {
        super::replay::lost_runs(
            lock,
            state,
            self.parts.journal.as_ref(),
            &self.parts.protections,
        )
    }

    /// Applies `change` to the Team's state in one write, after marking every run that lost its protection.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn update<T>(
        &self,
        team_id: &str,
        change: impl FnOnce(TeamRoutines) -> (TeamRoutines, T),
    ) -> Result<T, Problem> {
        let lost = |lock: &TeamLock<'_>, state: &TeamRoutines| self.lost_in(lock, state);
        state::update(&self.parts.store, team_id, &lost, |state| {
            Ok::<_, state::StateUnavailable>(change(state))
        })
        .map_err(|_| problem::routine_state_unavailable())
    }

    /// [`RoutineService::update`] whose `change` also reads the Team's sealed records under the write's own lock.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn update_held<T>(
        &self,
        team_id: &str,
        change: impl FnOnce(&TeamLock<'_>, TeamRoutines) -> (TeamRoutines, T),
    ) -> Result<T, Problem> {
        let lock = self
            .parts
            .store
            .lock(team_id)
            .map_err(|_| problem::routine_state_unavailable())?;
        self.update_locked(&lock, |state| change(&lock, state))
    }

    /// [`RoutineService::update`] for a caller that already holds the Team's Routine lock.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn update_locked<T>(
        &self,
        lock: &TeamLock<'_>,
        change: impl FnOnce(TeamRoutines) -> (TeamRoutines, T),
    ) -> Result<T, Problem> {
        let lost = |lock: &TeamLock<'_>, state: &TeamRoutines| self.lost_in(lock, state);
        state::update_locked(lock, &lost, |state| {
            Ok::<_, state::StateUnavailable>(change(state))
        })
        .map_err(|_| problem::routine_state_unavailable())
    }

    /// Writes `transition`; `false` when it refused, which leaves the state as it was.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn applied(
        &self,
        team_id: &str,
        transition: impl FnOnce(&TeamRoutines) -> Transition,
    ) -> Result<bool, Problem> {
        self.update(team_id, |state| match transition(&state) {
            Ok(next) => (next, true),
            Err(_refused) => (state, false),
        })
    }

    /// Records one metadata-only failure audit event as Team itself without waiting for it, from work that cannot.
    pub(super) fn audit_later(&self, operation: &'static str, team_id: &str, detail: &str) {
        let (service, team, detail) = (self.clone(), team_id.to_owned(), detail.to_owned());
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move { service.audit(operation, false, &team, &detail).await });
        }
    }

    /// Records one metadata-only audit event as Team itself; a failure to record it changes nothing.
    pub(super) async fn audit(&self, operation: &str, ok: bool, team_id: &str, detail: &str) {
        let result = if ok { Outcome::Ok } else { Outcome::Error };
        let text = |value: &str| Value::String(value.to_owned());
        let event = Principal::team()
            .event(operation, result)
            .with("team_id", text(team_id))
            .with("detail", text(detail));
        let _ = self.parts.audit.record(event).await;
    }
}
