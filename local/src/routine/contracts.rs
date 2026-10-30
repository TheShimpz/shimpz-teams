//! The Team's current Assistant contracts as Routines pin and admit them (`local/routine/contracts.py`): recording,
//! confirmation, every run, and every recovery read each running Assistant's Actions at their complete pin, with the
//! reviewed schemas, file inputs, whether the reviewed effect proves the Action read-only, and its Stored Inputs.

use std::collections::BTreeMap;

use team_routine::pin::action_pins;
use team_routine::plan::{ActionContract, Contracts};

use crate::chat::ports::{ActiveAssistant, TeamView};
use crate::coordination::locks::SpaceLocks;
use crate::http::service::Problem;

/// The retryable problem of a turn or step whose Team could not be read; nothing it held changed.
#[must_use]
pub const fn context_unavailable() -> Problem {
    Problem::new(
        503,
        "Team capabilities could not be checked; retry",
        "team-context-unavailable",
    )
}

/// Each Action of these Assistants with its complete current pin, its reviewed schemas, and its effect; an Action
/// whose pin cannot be computed is left out, so a Routine naming it reads as changed.
#[must_use]
pub fn contracts(assistants: &[ActiveAssistant]) -> Contracts {
    let mut admitted = BTreeMap::new();
    for active in assistants {
        let actions = &active.binding.contract.actions;
        let Some(pins) = action_pins(
            &active.pinned,
            actions.iter().map(|action| action.id.as_str()),
        ) else {
            continue;
        };
        for action in actions {
            let Some(pin) = pins.get(&action.id) else {
                continue;
            };
            let mut stored_inputs = action.stored_inputs.clone();
            stored_inputs.sort();
            let contract = ActionContract {
                pin: pin.clone(),
                input_schema: action.input_schema.clone(),
                input_files: action.input_files.clone(),
                output_schema: action.output_schema.clone(),
                read_only: action.effect == "read_only",
                stored_inputs,
            };
            admitted.insert((active.id().to_owned(), action.id.clone()), contract);
        }
    }
    admitted
}

/// The Team could not be read, so nothing about a Routine's scope is proven either way.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ContractsUnavailable;

/// Each named Assistant's current Routine scope pin, exactly as a recording turn computes it; an Assistant the Team
/// no longer runs has no entry, which proves the scope changed.
///
/// # Errors
/// [`ContractsUnavailable`] when the Team cannot be read, so a transient failure never reads as a changed scope.
pub async fn current_pins(
    team: &dyn TeamView,
    locks: &SpaceLocks,
    team_id: &str,
    assistant_ids: &[String],
) -> Result<BTreeMap<String, String>, ContractsUnavailable> {
    let scope = {
        let _team = locks.team(team_id).await;
        team.scope(team_id, true)
            .await
            .map_err(|_| ContractsUnavailable)?
    };
    Ok(assistant_ids
        .iter()
        .filter_map(|id| Some((id.clone(), scope.assistant(id)?.routine_scope()?)))
        .collect())
}

/// Refuses with `changed` unless every pinned Assistant still runs at its pin, retryably when unreadable.
///
/// # Errors
/// `changed`, or [`context_unavailable`].
pub async fn require_pinned(
    team: &dyn TeamView,
    locks: &SpaceLocks,
    team_id: &str,
    pinned: &BTreeMap<String, String>,
    changed: Problem,
) -> Result<(), Problem> {
    let ids: Vec<String> = pinned.keys().cloned().collect();
    let current = current_pins(team, locks, team_id, &ids)
        .await
        .map_err(|_| context_unavailable())?;
    if current == *pinned {
        Ok(())
    } else {
        Err(changed)
    }
}
