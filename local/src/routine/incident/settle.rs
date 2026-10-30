//! How an incident is settled (`incident.py`): reopened for verification or its card, paused by recovery, set aside by
//! a person, and sealed as a terminal record; and a person's Pausar or Retomar of a whole Routine.

use strict_json::Value;
use team_protocol::python::value::object_of;
use team_protocol::routine::ROUTINE_ID_RE;
use team_routine::hold::{self, PauseReason};
use team_routine::record::{self, Run, RunStatus, TeamRoutines, Transition};
use team_routine::runs;

use super::{OpenedRecovery, Recovery, read_evidence};
use crate::chat::problem;
use crate::http::service::Problem;
use crate::routine::diagnostics::{RunBinding, RunRecord};
use crate::routine::service::RoutineService;
use crate::routine::store::TeamLock;

/// A refused transition, with what refused it: a stale card, a Team admission limit, or an incident gone.
#[must_use]
pub fn transition_problem(code: &'static str) -> Problem {
    if matches!(code, "incident-changed" | "routine-revision-changed") {
        return problem::routine_card_stale();
    }
    if code.starts_with("routine-") || code == "notices-full" {
        return Problem::new(409, "the Team cannot hold this Routine change", code);
    }
    problem::routine_incident_unavailable()
}

/// Whether an incident's cursor, under its sealed evidence's binding, records a lost protection; evidence or a cursor
/// that cannot be read, is missing, or names another run, revision, or plan is taken as lost (ADR-0101 §6).
fn sealed_loss(lock: &TeamLock<'_>, value: &Run) -> bool {
    let Ok(sealed) = lock.incident(&value.run_id) else {
        return true;
    };
    let Some(held) = sealed.and_then(|sealed| read_evidence(&sealed, &value.run_id)) else {
        return true;
    };
    if (held.routine_id.as_str(), held.generation.as_str())
        != (value.routine_id.as_str(), value.generation.as_str())
    {
        return true;
    }
    let Some(snapshot) = held.recovery else {
        // A run with no cursor never replayed and has no protection to lose; one left behind cannot prove it held.
        return lock
            .cursors()
            .map_or(true, |runs| runs.contains(&value.run_id));
    };
    match lock.cursor(&snapshot.binding) {
        Ok(Some(found)) => {
            let bound = snapshot.binding.revision == value.revision && snapshot.runs(&found);
            !bound || found.protection_lost
        }
        _ => true,
    }
}

/// `state` with every chosen incident whose unmarked loss is sealed or cannot be ruled out marked lost.
fn losses_marked(
    lock: &TeamLock<'_>,
    state: TeamRoutines,
    chosen: &dyn Fn(&Run) -> bool,
) -> TeamRoutines {
    let marked: Vec<String> = record::incidents(&state)
        .into_iter()
        .filter(|item| !item.protection_lost && chosen(item) && sealed_loss(lock, item))
        .map(|item| item.run_id.clone())
        .collect();
    marked.iter().fold(state, |state, run_id| {
        runs::lose_protection(&state, run_id).unwrap_or(state)
    })
}

impl RoutineService {
    /// Applies `change` after every `chosen` incident whose loss is sealed or cannot be ruled out is marked: only a
    /// write that publishes an incident's notice reads its sealed cursor, inside that write.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(crate) fn marking_losses<T>(
        &self,
        team_id: &str,
        chosen: &dyn Fn(&Run) -> bool,
        change: impl FnOnce(TeamRoutines) -> (TeamRoutines, T),
    ) -> Result<T, Problem> {
        self.update_held(team_id, |lock, state| {
            change(losses_marked(lock, state, chosen))
        })
    }

    /// Applies a person's transition that sets the incident aside, then releases what it kept; a failed release only
    /// waits for the watchdog's next pass.
    ///
    /// # Errors
    /// The transition's refusal, or a state problem.
    pub(crate) fn set_aside(
        &self,
        team_id: &str,
        incident_id: &str,
        change: impl FnOnce(&TeamRoutines) -> Transition,
    ) -> Result<Run, Problem> {
        let chosen = |item: &Run| item.run_id == incident_id;
        let skipped = self.marking_losses(team_id, &chosen, |state| match change(&state) {
            Ok(changed) => {
                let skipped = hold::incident(&changed, incident_id)
                    .cloned()
                    .map_err(|error| error.code);
                (changed, skipped)
            }
            Err(error) => (state, Err(error.code)),
        })?;
        let skipped = skipped.map_err(transition_problem)?;
        self.settled(team_id, &skipped);
        Ok(skipped)
    }

    /// Seals an ended run's terminal record from its sealed snapshot and cursor; a display record, so nothing is kept
    /// when they prove nothing, and a failure to keep one is audited only.
    pub(crate) fn seal_terminal(&self, team_id: &str, run_id: &str, snapshot: Option<&Recovery>) {
        if let Ok(lock) = self.parts.store.lock(team_id) {
            self.seal_terminal_locked(&lock, run_id, snapshot);
        }
    }

    /// [`RoutineService::seal_terminal`] under the Team's held Routine lock.
    pub(crate) fn seal_terminal_locked(
        &self,
        lock: &TeamLock<'_>,
        run_id: &str,
        snapshot: Option<&Recovery>,
    ) {
        let Some(snapshot) = snapshot else {
            return;
        };
        let found = lock.cursor(&snapshot.binding).ok().flatten();
        let Some(found) = found.filter(|found| snapshot.runs(found)) else {
            return;
        };
        let team_id = lock.team();
        let binding = &snapshot.binding;
        let terminal = RunRecord {
            binding: RunBinding {
                routine_id: binding.routine_id.clone(),
                run_id: run_id.to_owned(),
                revision: binding.revision,
                plan_digest: snapshot.plan_digest(),
                total: i64::try_from(snapshot.steps().len()).unwrap_or(i64::MAX),
            },
            reached: i64::try_from(found.step).unwrap_or(i64::MAX),
            dispatched: found.operation_id.is_some(),
            recorded_at: self.now(),
        };
        let diagnostics = &self.parts.diagnostics;
        if diagnostics
            .record_run(team_id, &binding.incarnation, &terminal)
            .is_err()
        {
            self.audit_later("routine-run-record", team_id, run_id);
        }
    }

    /// The Team's indexed incident, which must still be unresolved.
    ///
    /// # Errors
    /// `routine-incident-unavailable`, or `routine-state-unavailable`.
    pub(crate) fn unresolved(&self, team_id: &str, incident_id: &str) -> Result<Run, Problem> {
        let state = self.load(team_id)?;
        let value = hold::incident(&state, incident_id)
            .map_err(|_| problem::routine_incident_unavailable())?;
        if value.status != RunStatus::Unresolved {
            return Err(problem::routine_incident_unavailable());
        }
        Ok(value.clone())
    }

    /// Reopens an unresolved incident's cursor under its own sealed binding, for verification or its card; it needs
    /// neither the Routine record nor the archived journal rows.
    ///
    /// # Errors
    /// `routine-incident-unavailable`, `routine-incident-unverifiable`, or `routine-state-unavailable`.
    pub(crate) fn open_recovery(
        &self,
        team_id: &str,
        incident_id: &str,
    ) -> Result<OpenedRecovery, Problem> {
        let indexed = self.unresolved(team_id, incident_id)?;
        let unavailable = problem::routine_state_unavailable;
        let lock = self.parts.store.lock(team_id).map_err(|_| unavailable())?;
        let sealed = lock
            .incident(incident_id)
            .map_err(|_| unavailable())?
            .ok_or_else(unavailable)?;
        let held = read_evidence(&sealed, incident_id).ok_or_else(unavailable)?;
        let Some(snapshot) = held.recovery else {
            return Err(Problem::new(
                409,
                "Routine incident cannot be verified",
                "routine-incident-unverifiable",
            ));
        };
        let binding = &snapshot.binding;
        if (binding.routine_id.as_str(), binding.revision)
            != (indexed.routine_id.as_str(), indexed.revision)
        {
            return Err(unavailable());
        }
        let found = lock.cursor(binding).map_err(|_| unavailable())?;
        let found = found
            .filter(|found| snapshot.runs(found))
            .ok_or_else(unavailable)?;
        Ok(OpenedRecovery {
            recovery: snapshot,
            cursor: found,
        })
    }

    /// Recovery pauses the Routine an unresolved incident holds, and says why on the held run's notice.
    ///
    /// # Errors
    /// The transition's refusal, or a state problem.
    pub(crate) fn pause_incident(
        &self,
        team_id: &str,
        incident_id: &str,
        reason: PauseReason,
    ) -> Result<(), Problem> {
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let chosen = |item: &Run| item.run_id == incident_id;
        let refused =
            self.marking_losses(team_id, &chosen, |state| {
                match hold::pause_incident(&state, incident_id, now, reason, entropy.as_ref()) {
                    Ok(next) => (next, None),
                    Err(error) => (state, Some(error.code)),
                }
            })?;
        refused.map_or(Ok(()), |code| Err(transition_problem(code)))
    }

    /// A person's Pausar (`paused`) or Retomar of a whole Routine: pausing disables dispatch while every incident
    /// stays; resuming never bypasses an unresolved one.
    ///
    /// # Errors
    /// `routine-not-found`, or `routine-state-unavailable`.
    pub async fn set_routine_paused(
        &self,
        team_id: &str,
        routine_id: &str,
        paused: bool,
    ) -> Result<Value, Problem> {
        if !ROUTINE_ID_RE.is_full(routine_id) {
            return Err(problem::routine_not_found());
        }
        if !self.applied(team_id, |state| {
            record::set_paused(state, routine_id, paused)
        })? {
            return Err(problem::routine_not_found());
        }
        let operation = if paused {
            "routine-pause"
        } else {
            "routine-resume"
        };
        self.audit(operation, true, team_id, routine_id).await;
        let text = |value: &str| Value::String(value.to_owned());
        Ok(object_of(vec![
            ("team_id", text(team_id)),
            ("routine_id", text(routine_id)),
            ("paused", Value::Bool(paused)),
        ]))
    }
}
