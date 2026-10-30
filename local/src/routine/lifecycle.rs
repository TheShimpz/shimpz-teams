//! What a run's end, a Routine's deletion, and a Team's or the Space's teardown remove (`local/routine/manage.py`,
//! `local/routine/lifecycle.py`): each ended run's journal generations, continuation, snapshot, and cursor, queued in
//! the same write that ended it, so nothing it held is lost to a crash and nothing is removed while it could resume.

use strict_json::Value;
use team_protocol::python::value::object_of;
use team_protocol::routine::ROUTINE_ID_RE;
use team_routine::claim::verification_of;
use team_routine::hold;
use team_routine::record::{self, Run, RunStatus};
use team_routine::runs;

use super::incident::read_sealed;
use super::service::RoutineService;
use super::store::TeamLock;
use crate::chat::problem;
use crate::http::service::Problem;

fn unavailable<E>(_error: E) -> Problem {
    problem::routine_state_unavailable()
}

impl RoutineService {
    /// Removes one ended run's journal generation, continuation, and cursor under the Team's Routine lock; an
    /// unresolved incident's archive marker, cursor, and evidence stay, and a resumed run's live files stay.
    fn discard(
        &self,
        lock: &TeamLock<'_>,
        run: (&str, &str),
        held: (bool, bool),
    ) -> Result<(), Problem> {
        let ((run_id, generation), (incident, live)) = (run, held);
        if !generation.is_empty() {
            let journal = &self.parts.journal;
            let removed = if incident {
                journal.discard(generation)
            } else {
                journal.purge(generation)
            };
            removed.map_err(unavailable)?;
            if !incident
                && !live
                && let Ok(verification) = verification_of(generation, run_id)
            {
                journal.purge(&verification).map_err(unavailable)?;
            }
        }
        if live {
            return Ok(());
        }
        if !incident {
            // The run ended for good: its terminal record proves which steps never started, before its cursor goes.
            let snapshot = lock
                .recovery(run_id)
                .ok()
                .flatten()
                .and_then(|payload| read_sealed(&payload, run_id));
            self.seal_terminal_locked(lock, run_id, snapshot.as_ref());
        }
        lock.delete_continuation(run_id).map_err(unavailable)?;
        // An incident keeps its own sealed copy of the recovery snapshot.
        lock.delete_recovery(run_id).map_err(unavailable)?;
        if !incident {
            lock.delete_cursor(run_id).map_err(unavailable)?;
            lock.delete_incident(run_id).map_err(unavailable)?;
            // Nothing of the run can show anything any more, so its protection goes with it.
            self.parts.protections.drop_run(run_id);
        }
        Ok(())
    }

    /// Removes what the Team's ended runs still hold; a failure keeps the rest queued for the next drain.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(crate) fn drain(&self, team_id: &str) -> Result<(), Problem> {
        for (run_id, generation) in self.load(team_id)?.discards {
            // Each removal acts on the state as it is under the Team's Routine lock, so a held run resumed since the
            // first read is treated as the live run it now is, and a removal another drain finished is not repeated.
            let lock = self.parts.store.lock(team_id).map_err(unavailable)?;
            let state = lock.load().map_err(unavailable)?;
            if !state
                .discards
                .contains(&(run_id.clone(), generation.clone()))
            {
                continue;
            }
            let incident = record::incidents(&state)
                .iter()
                .any(|item| item.run_id == run_id);
            let live = record::live_runs(&state)
                .iter()
                .any(|item| item.run_id == run_id);
            self.discard(&lock, (&run_id, &generation), (incident, live))?;
            self.update_locked(&lock, |state| {
                (record::discarded(&state, &run_id, &generation), ())
            })?;
        }
        Ok(())
    }

    /// After a run ended: removes what it held, then finishes a deletion it was blocking.
    ///
    /// # Errors
    /// A state problem.
    pub(crate) fn settle(&self, team_id: &str, routine_id: &str) -> Result<bool, Problem> {
        self.drain(team_id)?;
        self.complete_deletion(team_id, routine_id)
    }

    /// Retries every pending removal and deletion of a Team, as the watchdog does after a failure or a restart.
    ///
    /// # Errors
    /// A state problem.
    pub(crate) fn settle_team(&self, team_id: &str) -> Result<(), Problem> {
        self.drain(team_id)?;
        for item in self.load(team_id)?.routines {
            if item.deleting {
                self.complete_deletion(team_id, &item.routine_id)?;
            }
        }
        Ok(())
    }

    /// Ends a run only while it is still frozen; `false` when a replay or another ending reached it first.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(crate) fn end_frozen(
        &self,
        team_id: &str,
        run_id: &str,
        outcome: &str,
        detail: Value,
    ) -> Result<bool, Problem> {
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        self.applied(team_id, |state| {
            runs::end(
                state,
                run_id,
                now,
                outcome,
                detail,
                Some(RunStatus::Frozen),
                entropy.as_ref(),
            )
        })
    }

    /// Removes a deleting Routine once none of its runs remains, set-aside ones released included; `false` until
    /// then. Its diagnostics go first, while it is still listed as deleting; the removal publishes its notice.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(crate) fn complete_deletion(
        &self,
        team_id: &str,
        routine_id: &str,
    ) -> Result<bool, Problem> {
        let state = self.load(team_id)?;
        let current = state
            .routines
            .iter()
            .find(|item| item.routine_id == routine_id);
        let busy = state.runs.iter().any(|item| item.routine_id == routine_id);
        if current.is_some_and(|current| !current.deleting || busy) {
            return Ok(false);
        }
        // A Routine already gone keeps nothing either: residue a failed earlier attempt left is removed again.
        self.parts
            .diagnostics
            .delete_routine(team_id, routine_id)
            .map_err(unavailable)?;
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        self.update(team_id, |state| {
            if !state
                .routines
                .iter()
                .any(|item| item.routine_id == routine_id)
            {
                return (state, true);
            }
            match record::complete_delete(&state, routine_id, now, entropy.as_ref()) {
                Ok(next) => (next, true),
                Err(_) => (state, false),
            }
        })
    }

    /// Deletes a Routine: its running run is stopped, a frozen one ended, and every held run of it set aside, in the
    /// one write that marks it deleting; a Routine already gone is deleted.
    ///
    /// # Errors
    /// `routine-not-found` for a malformed id, or a state problem.
    pub async fn delete_routine(&self, team_id: &str, routine_id: &str) -> Result<Value, Problem> {
        if !ROUTINE_ID_RE.is_full(routine_id) {
            return Err(problem::routine_not_found());
        }
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let unresolved =
            |item: &Run| item.routine_id == routine_id && item.status == RunStatus::Unresolved;
        let (runs, held) = self.marking_losses(team_id, &unresolved, |state| {
            if !state
                .routines
                .iter()
                .any(|item| item.routine_id == routine_id)
            {
                return (state, (Vec::new(), Vec::new()));
            }
            let Ok((mut next, runs)) = record::begin_delete(&state, routine_id) else {
                return (state, (Vec::new(), Vec::new()));
            };
            let held: Vec<String> = next
                .runs
                .iter()
                .filter(|item| unresolved(item))
                .map(|item| item.run_id.clone())
                .collect();
            for incident_id in &held {
                if let Ok(skipped) =
                    hold::skip_incident(&next, incident_id, now, None, entropy.as_ref())
                {
                    next = skipped;
                }
            }
            (next, (runs, held))
        })?;
        // No proposal that would replace a deleting Routine can be confirmed any more.
        self.parts
            .chat
            .parts()
            .routine
            .proposals
            .drop_routine(team_id, routine_id);
        self.end_deleted_runs(team_id, &runs).await?;
        self.release_deleted(team_id, &held)?;
        let deleted = self.settle(team_id, routine_id)?;
        let text = |value: &str| Value::String(value.to_owned());
        Ok(object_of(vec![
            ("team_id", text(team_id)),
            ("routine_id", text(routine_id)),
            ("deleted", Value::Bool(deleted)),
        ]))
    }

    async fn end_deleted_runs(&self, team_id: &str, runs: &[Run]) -> Result<(), Problem> {
        for value in runs {
            match value.status {
                RunStatus::Leased => {
                    self.halt_routine_run(team_id, &value.run_id)?;
                }
                RunStatus::Frozen => {
                    // Serialized with an opening by the Team lock, as Stop is: no challenge outlives the run.
                    let _team = self.parts.coordination.locks.team(team_id).await;
                    self.cancel_routine_challenge(team_id, &value.run_id);
                    self.end_frozen(team_id, &value.run_id, "stopped", object_of(Vec::new()))?;
                }
                // A held run is set aside as its incident is indexed; that ending completes the deletion.
                _ => {}
            }
        }
        Ok(())
    }

    fn release_deleted(&self, team_id: &str, held: &[String]) -> Result<(), Problem> {
        for incident_id in held {
            // A verification or recovery episode still in progress is stopped; it changes nothing after this.
            let _stopped = self.stop_routine_run(team_id, incident_id);
        }
        let state = self.load(team_id)?;
        for item in record::incidents(&state) {
            if held.contains(&item.run_id) {
                self.settled(team_id, item);
            }
        }
        Ok(())
    }

    /// Deletes every live or queued run's journal generations, then the Team's Routine state, under the Team's Routine
    /// lock; a run's generation names the network it ran in, so this works after the network is gone.
    ///
    /// # Errors
    /// `routine-state-unavailable`, or `action-state-unavailable`.
    pub fn delete_team_routines(&self, team_id: &str) -> Result<(), Problem> {
        {
            let lock = self.parts.store.lock(team_id).map_err(unavailable)?;
            self.delete_locked(&lock)?;
        }
        let _cancelled = self.human_challenges().cancel_team(team_id);
        let routine = &self.parts.chat.parts().routine;
        routine.recordings.drop_team(team_id);
        routine.proposals.drop_team(team_id);
        Ok(())
    }

    fn delete_locked(&self, lock: &TeamLock<'_>) -> Result<(), Problem> {
        let state = lock.load().map_err(unavailable)?;
        let mut held: Vec<(String, String)> = state
            .runs
            .iter()
            .map(|run| (run.run_id.clone(), run.generation.clone()))
            .collect();
        held.extend(state.discards.iter().cloned());
        for (run_id, generation) in &held {
            if generation.is_empty() {
                continue;
            }
            let journal = &self.parts.journal;
            journal
                .purge(generation)
                .map_err(|_| problem::action_state_unavailable())?;
            // A run's verification generation goes with its own, whatever its own release left.
            if let Ok(verification) = verification_of(generation, run_id) {
                journal
                    .purge(&verification)
                    .map_err(|_| problem::action_state_unavailable())?;
            }
        }
        // The values each run protected leave memory before the state naming the runs goes.
        for (run_id, _) in &held {
            self.parts.protections.drop_run(run_id);
        }
        lock.delete().map_err(unavailable)?;
        self.parts
            .diagnostics
            .delete(lock.team())
            .map_err(unavailable)
    }

    /// Deletes every Team's Routines, a Team whose network is already gone included, and the Routine keyring; the
    /// store is exclusive for the whole reset.
    ///
    /// # Errors
    /// `routine-state-unavailable`, or `action-state-unavailable`.
    pub fn delete_all_routines(&self) -> Result<(), Problem> {
        let _cancelled = self.human_challenges().cancel_all();
        let routine = &self.parts.chat.parts().routine;
        routine.recordings.clear();
        routine.proposals.clear();
        self.parts.protections.clear();
        let mut exclusive = self.parts.store.exclusive();
        for team_id in exclusive.teams().map_err(unavailable)? {
            let lock = exclusive.team(&team_id).map_err(unavailable)?;
            self.delete_locked(lock)?;
        }
        exclusive.delete_all().map_err(unavailable)?;
        self.parts.diagnostics.delete_all().map_err(unavailable)
    }
}
