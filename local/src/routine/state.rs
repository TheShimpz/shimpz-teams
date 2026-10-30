//! Reading and changing a Team's Routine state, failing closed with one retryable problem (ADR-0086).
//!
//! Every Routine state write goes through [`update`], which first marks each live run that lost its protection, in
//! the same write and under the same Team lock (ADR-0101): whatever ends a run (its worker, a person's Stop, denial,
//! deletion, or the watchdog) then records the loss in that run's notice and shows nothing the run produced.

use std::collections::BTreeSet;

use team_routine::record::{TeamRoutines, Transition};
use team_routine::runs;

use super::store::{RoutineStore, StoreError, TeamLock};

/// The Team's Routine state could not be read or written (`routine-state-unavailable`, 503, retryable).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StateUnavailable;

impl std::fmt::Display for StateUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Routine state is unavailable")
    }
}

impl std::error::Error for StateUnavailable {}

impl From<StoreError> for StateUnavailable {
    fn from(_error: StoreError) -> Self {
        Self
    }
}

/// Which of the Team's live runs lost their protection, each loss already sealed in its run's cursor; called under the
/// Team's held lock with the state the write starts from (`replay.lost_runs`).
pub type LostRuns<'a> = &'a dyn Fn(&TeamLock<'_>, &TeamRoutines) -> BTreeSet<String>;

/// No run lost its protection: for writes that end no run's output, and for tests.
#[must_use]
pub fn none_lost(_lock: &TeamLock<'_>, _state: &TeamRoutines) -> BTreeSet<String> {
    BTreeSet::new()
}

/// The Team's state.
///
/// # Errors
/// [`StateUnavailable`].
pub fn load(store: &RoutineStore, team: &str) -> Result<TeamRoutines, StateUnavailable> {
    Ok(store.load(team)?)
}

/// What failed in a write: the store, or the change itself.
enum Failed<E> {
    Store,
    Change(E),
}

impl<E> From<StoreError> for Failed<E> {
    fn from(_error: StoreError) -> Self {
        Self::Store
    }
}

/// `state` with every run in `lost` marked as having lost its protection.
fn reconciled(state: TeamRoutines, lost: &BTreeSet<String>) -> TeamRoutines {
    let marked: Vec<String> = state
        .runs
        .iter()
        .filter(|item| lost.contains(&item.run_id) && !item.protection_lost)
        .map(|item| item.run_id.clone())
        .collect();
    marked.iter().fold(state, |state, run_id| {
        runs::lose_protection(&state, run_id).unwrap_or(state)
    })
}

/// Applies `change` under the Team's held `lock`, after marking every run that lost its protection.
///
/// # Errors
/// `change`'s own error, or [`StateUnavailable`] converted into it.
pub fn update_locked<T, E: From<StateUnavailable>>(
    lock: &TeamLock<'_>,
    lost: LostRuns<'_>,
    change: impl FnOnce(TeamRoutines) -> Result<(TeamRoutines, T), E>,
) -> Result<T, E> {
    let written = lock.update(|state: TeamRoutines| {
        let found = lost(lock, &state);
        change(reconciled(state, &found)).map_err(Failed::Change)
    });
    match written {
        Ok(result) => Ok(result),
        Err(Failed::Store) => Err(E::from(StateUnavailable)),
        Err(Failed::Change(error)) => Err(error),
    }
}

/// Applies `change` under the Team's lock, after marking every run that lost its protection.
///
/// # Errors
/// `change`'s own error, or [`StateUnavailable`] converted into it.
pub fn update<T, E: From<StateUnavailable>>(
    store: &RoutineStore,
    team: &str,
    lost: LostRuns<'_>,
    change: impl FnOnce(TeamRoutines) -> Result<(TeamRoutines, T), E>,
) -> Result<T, E> {
    let lock = store.lock(team).map_err(|_| E::from(StateUnavailable))?;
    update_locked(&lock, lost, change)
}

/// Writes `transition` through [`update`]; `false` when it refused, which leaves the state as it was.
///
/// # Errors
/// [`StateUnavailable`].
pub fn applied(
    store: &RoutineStore,
    team: &str,
    lost: LostRuns<'_>,
    transition: impl FnOnce(&TeamRoutines) -> Transition,
) -> Result<bool, StateUnavailable> {
    update(store, team, lost, |state: TeamRoutines| {
        Ok::<_, StateUnavailable>(match transition(&state) {
            Ok(next) => (next, true),
            Err(_refused) => (state, false),
        })
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::path::Path;

    use team_routine::record::{RoutineStateError, RunStatus, TeamRoutines};
    use tempfile::TempDir;

    use super::{StateUnavailable, applied, load, none_lost, update};
    use crate::routine::store::{RoutineStore, TeamLock};
    use crate::routine::testing::{busy_state, entropy};

    fn store(directory: &Path) -> RoutineStore {
        RoutineStore::new(
            &directory.join("state"),
            &directory.join("key/aes256.key"),
            entropy(),
        )
        .expect("a store")
    }

    #[test]
    fn every_write_marks_the_runs_that_lost_their_protection_first() {
        let directory = TempDir::new().expect("a directory");
        let store = store(directory.path());
        let state = busy_state();
        update(&store, "team_1", &none_lost, |_before| {
            Ok::<_, StateUnavailable>((state.clone(), ()))
        })
        .expect("written");
        let frozen = state
            .runs
            .iter()
            .find(|run| run.status == RunStatus::Frozen)
            .expect("frozen")
            .run_id
            .clone();
        let lost = |_lock: &TeamLock<'_>, _state: &TeamRoutines| BTreeSet::from([frozen.clone()]);
        let seen = update(&store, "team_1", &lost, |state| {
            let marked = state.runs.iter().filter(|run| run.protection_lost).count();
            Ok::<_, StateUnavailable>((state, marked))
        });
        assert_eq!(seen, Ok(1));
        let loaded = load(&store, "team_1").expect("loaded");
        assert!(
            loaded
                .runs
                .iter()
                .any(|run| run.run_id == frozen && run.protection_lost)
        );
    }

    #[test]
    fn a_refused_transition_leaves_the_state_and_a_broken_store_is_one_retryable_problem() {
        let directory = TempDir::new().expect("a directory");
        let store = store(directory.path());
        let refused = applied(&store, "team_1", &none_lost, |_state| {
            Err(RoutineStateError::new("refused"))
        });
        assert_eq!(refused, Ok(false));
        let team_dir = directory
            .path()
            .join("state")
            .join(RoutineStore::team_dir_name("team_1"));
        assert!(!team_dir.exists());
        assert_eq!(load(&store, "Team"), Err(StateUnavailable));
        std::fs::create_dir_all(&team_dir).expect("a directory");
        std::fs::write(team_dir.join("state.json"), b"{").expect("written");
        assert_eq!(load(&store, "team_1"), Err(StateUnavailable));
        assert_eq!(
            applied(&store, "team_1", &none_lost, |state| Ok(state.clone())),
            Err(StateUnavailable)
        );
    }
}
