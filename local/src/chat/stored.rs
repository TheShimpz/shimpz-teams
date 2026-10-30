//! A person's answer to a Stored Input request, sealed for exactly the paused Action that asked (ADR-0059).

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_action::challenges::HumanRequirement;
use team_core::secret::Secret;
use team_integrations::challenge::Pending;
use team_protocol::python::json::canonical;
use team_protocol::python::value::object_of;

use super::pending::PendingLocalChat;
use super::ports::ActiveAssistant;
use super::problem;
use super::service::ChatService;
use crate::http::service::Problem;

/// The origin binding a newly sealed Stored Input to one exact Brain Action interrupt.
#[must_use]
pub fn origin(request: &team_inference::runtime::ActionRequest) -> String {
    let identity = object_of(vec![
        ("action", Value::String(request.action.clone())),
        ("assistant_id", Value::String(request.assistant_id.clone())),
        ("input", request.input.clone()),
        ("interrupt_id", Value::String(request.interrupt_id.clone())),
    ]);
    super::hex(&Sha256::digest(canonical(&identity)))
}

/// Seals an admitted Stored Input answer for the paused Action that asked for it: only a slot the current reviewed
/// Action declares, bound to that Action interrupt's origin, so the suspended batch keeps its journal fingerprint.
///
/// # Errors
/// `422 invalid-human-response` for an answer that is not exactly the paused Action's; the Stored Input problem when
/// it cannot be sealed.
pub fn seal(
    service: &ChatService,
    team_id: &str,
    claimed: &Pending<HumanRequirement, PendingLocalChat>,
    assistants: &[ActiveAssistant],
    (name, value): (&str, &Secret),
) -> Result<(), Problem> {
    let requirement = &claimed.metadata;
    let mismatch = problem::human_response_mismatch;
    let mut matching = claimed
        .payload
        .continuation
        .turn
        .actions
        .iter()
        .filter(|request| request.interrupt_id == requirement.interrupt_id);
    let (Some(paused), None) = (matching.next(), matching.next()) else {
        return Err(mismatch());
    };
    let exact = paused.assistant_id == requirement.assistant_id
        && paused.action == requirement.action_id
        && requirement.request.stored_input.as_deref() == Some(name);
    let contract = assistants
        .iter()
        .find(|active| active.id() == requirement.assistant_id)
        .map(|active| &active.binding.contract)
        .ok_or_else(mismatch)?;
    let declared = contract
        .action(&requirement.action_id)
        .is_some_and(|action| action.stored_inputs.iter().any(|slot| slot == name));
    let declaration = contract
        .stored_inputs
        .iter()
        .find(|declaration| declaration.id == name);
    let (true, true, Some(declaration)) = (exact, declared, declaration) else {
        return Err(mismatch());
    };
    service
        .parts
        .stored_inputs
        .seal(
            (team_id, &requirement.assistant_id, name),
            &declaration.kind,
            value,
            &origin(paused),
        )
        .map(|_| ())
        .map_err(|_| problem::stored_input_unavailable())
}
