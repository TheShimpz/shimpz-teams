//! A hold's four durable steps, each idempotent (`incident.hold`, `incident.reconcile`): the live lease fenced, the
//! incident's evidence sealed with its own copy of the run's snapshot, the batch archived, and the run indexed as its
//! own incident. A run of a Routine being deleted is set aside as it is indexed, so what of it cannot be read is lost
//! and audited rather than waited on.

use strict_json::Value;
use team_routine::claim::verification_of;
use team_routine::hold::{self, HeldStep};
use team_routine::record::{self, Lease, Run, RunStatus, TeamRoutines};

use super::{Evidence, Recovery, evidence, read_evidence};
use crate::chat::problem;
use crate::http::service::Problem;
use crate::routine::service::RoutineService;

/// Whether `routine_id` is being deleted.
pub(crate) fn deleting(state: &TeamRoutines, routine_id: &str) -> bool {
    state
        .routines
        .iter()
        .any(|item| item.routine_id == routine_id && item.deleting)
}

/// The call a sealed cursor stopped at: its current replay step, the last one once every step completed.
#[must_use]
pub fn held_call(step: usize, steps: &[Value]) -> HeldStep {
    let text = |value: Option<&Value>| match value {
        Some(Value::String(text)) => text.clone(),
        _ => String::new(),
    };
    let index = step.min(steps.len().saturating_sub(1));
    let Some(found) = steps.get(index) else {
        return hold::unknown_step();
    };
    let position = i64::try_from(index + 1).unwrap_or(i64::MAX);
    let total = i64::try_from(steps.len()).unwrap_or(i64::MAX);
    (
        text(found.get("assistant")),
        text(found.get("action")),
        Some(position),
        total,
    )
}

/// Why a held run's evidence could not be had: the journal (never lost), or anything else.
enum Unread {
    Journal,
    State,
}

impl Unread {
    fn problem(&self) -> Problem {
        match self {
            Self::Journal => problem::action_state_unavailable(),
            Self::State => problem::routine_state_unavailable(),
        }
    }
}

impl RoutineService {
    /// Fences a leased run's live lease, then holds it until its incident is durable and indexed.
    ///
    /// # Errors
    /// `routine-lease-invalid`, or a state or journal problem.
    pub(crate) fn hold(&self, team_id: &str, run_id: &str, lease: &Lease) -> Result<(), Problem> {
        let now = self.now();
        if !self.applied(team_id, |state| hold::fence(state, run_id, lease, now))? {
            return Err(problem::routine_lease_invalid());
        }
        self.reconcile(team_id, run_id).map(|_| ())
    }

    /// Finishes one held run's hold from its last durable step; `false` when the run is no longer held.
    ///
    /// # Errors
    /// A state or journal problem; the run stays held for the next pass.
    pub(crate) fn reconcile(&self, team_id: &str, run_id: &str) -> Result<bool, Problem> {
        let state = self.load(team_id)?;
        let Some(value) = state.runs.iter().find(|item| item.run_id == run_id) else {
            return Ok(false);
        };
        if value.status != RunStatus::Held {
            return Ok(false);
        }
        let deleting = deleting(&state, &value.routine_id);
        let held = match self.held_evidence(team_id, value) {
            Ok(held) => Some(held),
            Err(Unread::State) if deleting => None,
            Err(unread) => return Err(unread.problem()),
        };
        let found = match &held {
            None => None,
            Some(held) => self.held_step(team_id, held.recovery.as_ref(), deleting)?,
        };
        // A loss is audited where it is met, before the write that sets the run aside.
        if found.is_none() {
            self.audit_later("routine-evidence-lost", team_id, run_id);
        }
        let step = found.unwrap_or_else(hold::unknown_step);
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        self.applied(team_id, |state| {
            hold::settle_hold(state, run_id, now, step, entropy.as_ref())
        })
    }

    /// Seals the held run's evidence now from its current batch and snapshot; an archived batch no evidence names
    /// was lost, since archiving follows durable evidence.
    fn seal_evidence(
        &self,
        lock: &crate::routine::store::TeamLock<'_>,
        value: &Run,
        current: Option<&(String, String)>,
    ) -> Result<Evidence, Unread> {
        if current.is_some_and(|(_, state)| state == "archived") {
            return Err(Unread::State);
        }
        let fingerprint = current.map(|(fingerprint, _)| fingerprint.as_str());
        let operations = match fingerprint {
            Some(fingerprint) => self
                .parts
                .journal
                .snapshot(&value.generation, fingerprint)
                .map_err(|_| Unread::Journal)?,
            None => Vec::new(),
        };
        let snapshot = super::snapshot_locked(lock, value).map_err(|_| Unread::State)?;
        let payload = evidence(
            &value.run_id,
            value,
            (fingerprint, &operations),
            snapshot.as_ref(),
        );
        lock.put_incident(&value.run_id, &payload)
            .map_err(|_| Unread::State)?;
        read_evidence(&payload, &value.run_id).ok_or(Unread::State)
    }

    /// The held run's sealed evidence, sealed now from its batch and snapshot when there is none; its batch archived.
    fn held_evidence(&self, team_id: &str, value: &Run) -> Result<Evidence, Unread> {
        let journal = &self.parts.journal;
        let current = journal
            .current_batch(&value.generation)
            .map_err(|_| Unread::Journal)?;
        let lock = self.parts.store.lock(team_id).map_err(|_| Unread::State)?;
        let payload = lock.incident(&value.run_id).map_err(|_| Unread::State)?;
        let sealed = match payload {
            Some(payload) => Some(read_evidence(&payload, &value.run_id).ok_or(Unread::State)?),
            None => None,
        };
        // Evidence of an earlier hold this run resumed from: its generation is gone, and this hold seals its own.
        let sealed = sealed.filter(|sealed| sealed.generation == value.generation);
        let sealed = if let Some(sealed) = sealed {
            sealed
        } else {
            self.seal_evidence(&lock, value, current.as_ref())?
        };
        if sealed.fingerprint.as_deref()
            != current
                .as_ref()
                .map(|(fingerprint, _)| fingerprint.as_str())
        {
            return Err(Unread::State);
        }
        if let Some(fingerprint) = &sealed.fingerprint {
            journal
                .archive(&value.generation, fingerprint)
                .map_err(|_| Unread::Journal)?;
        }
        Ok(sealed)
    }

    /// The call the held run's sealed cursor stopped at; `None` for a cursor of a Routine being deleted that cannot be
    /// read, which is lost rather than waited on.
    fn held_step(
        &self,
        team_id: &str,
        snapshot: Option<&Recovery>,
        deleting: bool,
    ) -> Result<Option<HeldStep>, Problem> {
        let Some(snapshot) = snapshot else {
            return Ok(Some(hold::unknown_step()));
        };
        let found = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.cursor(&snapshot.binding));
        match found {
            Ok(None) => Ok(Some(hold::unknown_step())),
            Ok(Some(found)) => Ok(Some(held_call(found.step, snapshot.steps()))),
            Err(_) if deleting => Ok(None),
            Err(_) => Err(problem::routine_state_unavailable()),
        }
    }

    /// Releases what every set-aside incident still keeps once nothing executes for it, then finishes every held
    /// run; each run is its own, so one that fails never holds back the Team's. The ids of the runs that failed.
    ///
    /// # Errors
    /// `routine-state-unavailable` when the Team's state cannot be read at all.
    pub(crate) fn reconcile_team(&self, team_id: &str) -> Result<Vec<String>, Problem> {
        let state = self.load(team_id)?;
        let mut failed = Vec::new();
        for item in record::incidents(&state) {
            if item.status == RunStatus::Skipped && self.release(team_id, item).is_err() {
                failed.push(item.run_id.clone());
            }
        }
        for value in &state.runs {
            if value.status == RunStatus::Held && self.reconcile(team_id, &value.run_id).is_err() {
                failed.push(value.run_id.clone());
            }
        }
        Ok(failed)
    }

    /// Releases a set-aside incident unless something still executes for it; the watchdog's next pass retries.
    ///
    /// # Errors
    /// A state or journal problem.
    pub(crate) fn release(&self, team_id: &str, item: &Run) -> Result<(), Problem> {
        if self.executing(&item.run_id) {
            return Ok(());
        }
        self.release_now(team_id, item)
    }

    /// After a person's committed transition: releases now if possible, else leaves it to the watchdog.
    pub(crate) fn settled(&self, team_id: &str, item: &Run) {
        let _released = self.release(team_id, item);
    }

    /// The incident's readable evidence, or `None` when a deletion takes it as lost.
    fn release_evidence(
        &self,
        team_id: &str,
        item: &Run,
        deleting: bool,
    ) -> Result<(Option<Evidence>, bool), Problem> {
        let read = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.incident(&item.run_id))
            .ok()
            .map(|payload| payload.map(|payload| read_evidence(&payload, &item.run_id)));
        match read {
            Some(None) => Ok((None, false)),
            Some(Some(Some(held))) => Ok((Some(held), false)),
            _ if deleting => {
                self.audit_later("routine-evidence-lost", team_id, &item.run_id);
                Ok((None, true))
            }
            _ => Err(problem::routine_state_unavailable()),
        }
    }

    /// Removes what a skipped incident no longer needs, then its record; each step is idempotent.
    fn release_now(&self, team_id: &str, item: &Run) -> Result<(), Problem> {
        let deleting = deleting(&self.load(team_id)?, &item.routine_id);
        let (mut held, lost) = self.release_evidence(team_id, item, deleting)?;
        let journal = &self.parts.journal;
        let unavailable = |_| problem::action_state_unavailable();
        if !item.generation.is_empty() {
            let current = journal
                .current_batch(&item.generation)
                .map_err(unavailable)?;
            let fingerprint = held.as_ref().and_then(|held| held.fingerprint.clone());
            let foreign = current.as_ref().is_some_and(|(found, state)| {
                state == "archived" && Some(found) != fingerprint.as_ref()
            });
            if !lost && foreign {
                // An archived batch no readable evidence names: archiving follows durable evidence, so it was lost.
                if !deleting {
                    return Err(problem::routine_state_unavailable());
                }
                self.audit_later("routine-evidence-lost", team_id, &item.run_id);
                held = None;
            }
            self.release_journal(team_id, item, held.as_ref(), deleting)?;
        }
        let lock = self
            .parts
            .store
            .lock(team_id)
            .map_err(|_| problem::routine_state_unavailable())?;
        lock.delete_cursor(&item.run_id)
            .and_then(|()| lock.delete_incident(&item.run_id))
            .map_err(|_| problem::routine_state_unavailable())?;
        self.update_locked(&lock, |state| {
            (
                hold::release_incident(&state, &item.run_id).unwrap_or(state),
                (),
            )
        })?;
        // Nothing of the released run can show anything any more, so the values it protected leave memory with it.
        self.parts.protections.drop_run(&item.run_id);
        Ok(())
    }

    fn release_journal(
        &self,
        team_id: &str,
        item: &Run,
        held: Option<&Evidence>,
        deleting: bool,
    ) -> Result<(), Problem> {
        let journal = &self.parts.journal;
        let unavailable = |_| problem::action_state_unavailable();
        if let Some(held) = held {
            self.seal_terminal(team_id, &item.run_id, held.recovery.as_ref());
            if let Some(fingerprint) = &held.fingerprint {
                journal
                    .release_archive(&item.generation, fingerprint)
                    .map_err(unavailable)?;
            }
        } else if deleting {
            // No evidence names an archived batch of the run any more: its deletion releases the whole generation.
            journal.purge(&item.generation).map_err(unavailable)?;
        }
        // Its verification's generation goes too, whatever its own release left.
        if let Ok(verification) = verification_of(&item.generation, &item.run_id) {
            journal.purge(&verification).map_err(unavailable)?;
        }
        Ok(())
    }
}
