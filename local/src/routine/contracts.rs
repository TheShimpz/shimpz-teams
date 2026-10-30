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
    let _team = locks.team(team_id).await;
    current_pins_unlocked(team, team_id, assistant_ids).await
}

/// [`current_pins`] for a caller that already holds the Team's stripe.
///
/// # Errors
/// [`ContractsUnavailable`] when the Team cannot be read.
pub async fn current_pins_unlocked(
    team: &dyn TeamView,
    team_id: &str,
    assistant_ids: &[String],
) -> Result<BTreeMap<String, String>, ContractsUnavailable> {
    let scope = team
        .scope(team_id, true)
        .await
        .map_err(|_| ContractsUnavailable)?;
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

#[cfg(test)]
mod tests {
    //! The routine scope and action contracts a recording turn pins (`tests/test_routine_pin.py`).

    use strict_json::Value;
    use team_assistant::spec::ActionSpec;
    use team_routine::pin::action_pins;

    use super::contracts;
    use crate::chat::ports::ActiveAssistant;
    use crate::chat::tests::fakes::{action, assistant};

    fn changing(id: &str) -> ActionSpec {
        let mut spec = action(id);
        spec.effect = "change".to_owned();
        spec
    }

    #[test]
    fn a_turn_pins_each_action_contract_with_its_effect() {
        let active = assistant(
            "shimpz-cloudflare",
            vec![action("list-zones"), changing("create-record")],
        );
        let found = contracts(std::slice::from_ref(&active));
        let pins = action_pins(&active.pinned, ["create-record", "list-zones"]).expect("pins");
        let pinned: Vec<(String, String, bool)> = found
            .iter()
            .map(|((_, action), contract)| {
                (action.clone(), contract.pin.clone(), contract.read_only)
            })
            .collect();
        let expected: Vec<(String, String, bool)> = pins
            .into_iter()
            .map(|(action, pin)| {
                let read_only = action == "list-zones";
                (action, pin, read_only)
            })
            .collect();
        assert_eq!(pinned, expected);
        assert_eq!(
            action_pins(&active.pinned, []),
            Some(std::collections::BTreeMap::new())
        );
    }

    #[test]
    fn a_routine_scope_detects_drift_its_brain_contract_cannot_see() {
        let active = assistant("shimpz-cloudflare", vec![action("list-zones")]);
        let scope = active.routine_scope().expect("scope");
        assert_eq!(active.routine_scope(), Some(scope.clone()));
        let guided = ActiveAssistant {
            genesis: "Other guidance.".to_owned(),
            ..active.clone()
        };
        assert_ne!(guided.routine_scope(), Some(scope.clone()));
        // An output schema the Brain never sees is still part of the pin.
        let mut titled = action("list-zones");
        if let Value::Object(members) = &mut titled.output_schema {
            members.push(("title".to_owned(), Value::String("x".to_owned())));
        }
        let changed = assistant("shimpz-cloudflare", vec![titled]);
        assert_ne!(changed.routine_scope(), Some(scope));
    }
}
