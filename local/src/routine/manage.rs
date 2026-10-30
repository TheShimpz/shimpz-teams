//! What a Local Supervisor reads of a Team's Routines (ADR-0086, ADR-0092): the list of Routines with their live runs
//! and unresolved incidents, and a Routine's plan page by page.

use strict_json::Value;
use team_protocol::python::value::object_of;
use team_routine::definition;
use team_routine::hold;
use team_routine::record::{self, Routine, Run, RunStatus};
use team_storage::private::instant_text;

use super::state::{self, StateUnavailable};
use super::store::RoutineStore;

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn optional(value: &str) -> Value {
    if value.is_empty() {
        Value::Null
    } else {
        text(value)
    }
}

/// A Routine as its Supervisor inspects it: what it is, when it runs, its plan's summary, and its output. Its steps
/// are read page by page ([`steps`]), bound to the summary's revision.
#[must_use]
pub fn routine_view(value: &Routine) -> Value {
    object_of(vec![
        ("routine_id", text(&value.routine_id)),
        ("name", text(&value.name)),
        ("plan", definition::summary(&value.plan, value.revision)),
        ("output", definition::disposition(&value.plan)),
        ("schedule", value.schedule.clone()),
        ("timezone", text(value.timezone())),
        ("timezone_source", text(&value.timezone_source)),
        ("next_run_at", text(&instant_text(value.next_run_at))),
        ("needs_reconfirm", Value::Bool(value.needs_reconfirm)),
        ("deleting", Value::Bool(value.deleting)),
        ("state", text(definition::state(value))),
    ])
}

/// What a Supervisor may act on: a frozen run's request; a leased or held run only shows that it is live.
#[must_use]
pub fn run_view(value: &Run) -> Value {
    object_of(vec![
        ("run_id", text(&value.run_id)),
        ("routine_id", text(&value.routine_id)),
        ("status", text(value.status.as_str())),
        ("request_kind", optional(&value.request_kind)),
        ("assistant_id", optional(&value.assistant_id)),
        ("action", optional(&value.action)),
    ])
}

/// An unresolved incident a card settles: its Routine and the call whose effect is unknown.
#[must_use]
pub fn incident_view(value: &Run) -> Value {
    let mut members = vec![
        ("run_id".to_owned(), text(&value.run_id)),
        ("routine_id".to_owned(), text(&value.routine_id)),
    ];
    if let Value::Object(detail) = hold::step_detail(&hold::held_step(value)) {
        members.extend(detail);
    }
    Value::Object(members)
}

/// The Team's Routines, live runs, and unresolved incidents.
///
/// # Errors
/// [`StateUnavailable`].
pub fn list(store: &RoutineStore, team: &str) -> Result<Value, StateUnavailable> {
    let current = state::load(store, team)?;
    let routines = current.routines.iter().map(routine_view).collect();
    let runs = record::live_runs(&current)
        .into_iter()
        .map(run_view)
        .collect();
    let incidents = current
        .runs
        .iter()
        .filter(|item| item.status == RunStatus::Unresolved)
        .map(incident_view)
        .collect();
    Ok(object_of(vec![
        ("team_id", text(team)),
        ("routines", Value::Array(routines)),
        ("runs", Value::Array(runs)),
        ("incidents", Value::Array(incidents)),
    ]))
}

/// Why a page of a Routine's plan could not be read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepsError {
    /// No such Routine, or it is being deleted (`routine-not-found`, 404).
    NotFound,
    /// The revision the reader names is no longer current (`routine-revision-changed`, 409).
    RevisionChanged,
    /// The offset is outside the plan's steps (`routine-steps-not-found`, 404).
    StepsNotFound,
    /// The state could not be read (`routine-state-unavailable`, 503).
    Unavailable,
}

impl From<StateUnavailable> for StepsError {
    fn from(_error: StateUnavailable) -> Self {
        Self::Unavailable
    }
}

/// One page of a Routine's current plan from `offset`, only for the revision the reader names: a revision that is no
/// longer current is refused, so a reader never combines two revisions' steps (ADR-0092 amendment, 2026-10-05).
///
/// # Errors
/// [`StepsError`].
pub fn steps(
    store: &RoutineStore,
    team: &str,
    routine_id: &str,
    revision: i64,
    offset: i64,
) -> Result<Value, StepsError> {
    let current = state::load(store, team)?;
    let value = record::routine(&current, routine_id).map_err(|_| StepsError::NotFound)?;
    if value.deleting {
        return Err(StepsError::NotFound);
    }
    if value.revision != revision {
        return Err(StepsError::RevisionChanged);
    }
    let total = match value.plan.get("steps") {
        Some(Value::Array(steps)) => i64::try_from(steps.len()).unwrap_or(i64::MAX),
        _ => 0,
    };
    if !(0..total).contains(&offset) {
        return Err(StepsError::StepsNotFound);
    }
    definition::page(
        &value.routine_id,
        value.revision,
        &value.plan,
        &value.permitted,
        offset,
    )
    .ok_or(StepsError::Unavailable)
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_protocol::routine_notice::{
        canonical_incident_view, canonical_routine_view, canonical_run_view,
    };
    use team_routine::hold;
    use team_routine::record::RunStatus;
    use tempfile::TempDir;

    use super::{StepsError, list, steps};
    use crate::routine::store::{RoutineStore, StoreError};
    use crate::routine::testing::{ENTROPY, NINE, busy_state, entropy};

    fn stored() -> (TempDir, RoutineStore) {
        let directory = TempDir::new().expect("a directory");
        let store = RoutineStore::new(
            &directory.path().join("state"),
            &directory.path().join("key/k"),
            entropy(),
        )
        .expect("a store");
        let mut state = busy_state();
        let held = state
            .runs
            .iter()
            .find(|run| run.status == RunStatus::Held)
            .expect("held")
            .run_id
            .clone();
        let step = ("dns".to_owned(), "check".to_owned(), Some(1), 1);
        state = hold::settle_hold(&state, &held, NINE, step, &ENTROPY).expect("settled");
        store
            .update("team_1", |_before| Ok::<_, StoreError>((state, ())))
            .expect("written");
        (directory, store)
    }

    fn items<'v>(value: &'v Value, name: &str) -> &'v [Value] {
        match value.get(name) {
            Some(Value::Array(items)) => items,
            _ => &[],
        }
    }

    #[test]
    fn the_list_shows_every_routine_live_run_and_unresolved_incident_in_its_protocol_view() {
        let (_directory, store) = stored();
        let listed = list(&store, "team_1").expect("a list");
        assert_eq!(items(&listed, "routines").len(), 3);
        assert!(
            items(&listed, "routines")
                .iter()
                .all(|item| canonical_routine_view(item).is_some())
        );
        assert!(
            items(&listed, "runs")
                .iter()
                .all(|item| canonical_run_view(item).is_some())
        );
        let incidents = items(&listed, "incidents");
        assert_eq!(incidents.len(), 1);
        assert!(canonical_incident_view(&incidents[0]).is_some());
        assert_eq!(
            incidents[0].get("action"),
            Some(&Value::String("check".to_owned()))
        );
        let frozen = items(&listed, "runs")
            .iter()
            .find(|run| run.get("status") == Some(&Value::String("frozen".to_owned())));
        assert_eq!(
            frozen.and_then(|run| run.get("request_kind")),
            Some(&Value::String("human".to_owned()))
        );
    }

    #[test]
    fn a_page_is_read_only_for_the_current_revision_and_a_step_inside_the_plan() {
        let (_directory, store) = stored();
        let routine = "a".repeat(32);
        let page = steps(&store, "team_1", &routine, 1, 0).expect("a page");
        assert_eq!(page.get("total"), Some(&crate::routine::testing::json("1")));
        assert_eq!(
            steps(&store, "team_1", &routine, 2, 0),
            Err(StepsError::RevisionChanged)
        );
        assert_eq!(
            steps(&store, "team_1", &routine, 1, 1),
            Err(StepsError::StepsNotFound)
        );
        assert_eq!(
            steps(&store, "team_1", &"f".repeat(32), 1, 0),
            Err(StepsError::NotFound)
        );
        assert_eq!(
            steps(&store, "Team", &routine, 1, 0),
            Err(StepsError::Unavailable)
        );
    }
}
