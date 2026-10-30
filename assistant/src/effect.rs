//! Team admission of Action effect classes, verifiers, and provider idempotency (`assistant/effect.py`, ADR-0092).
//!
//! The Developers reference rules fix the closed shapes. Team then enforces, independently, what only the admitted
//! manifest and the verifier's own declaration can prove: an idempotency provider is one of the Assistant's exact
//! outbound hosts, and a verifier runs without a person, asking at most for its own declared Stored Input.

use assistant_protocol::action_effect::effect_error;
use strict_json::Value;

/// The effect of an Action that changes nothing.
pub const READ_ONLY: &str = "read_only";

/// A stable reason when the Actions' effect, verifier, or idempotency declarations are refused, or `None`.
#[must_use]
pub fn refusal(actions: &[Value], allowed_hosts: &[String]) -> Option<&'static str> {
    if let Some(error) = effect_error(&Value::Array(actions.to_vec())) {
        return Some(error);
    }
    for action in actions {
        if let Some(Value::Object(_)) = action.get("idempotency") {
            let provider = action
                .get("idempotency")
                .and_then(|idempotency| idempotency.get("provider"));
            if !matches!(provider, Some(Value::String(host)) if allowed_hosts.contains(host)) {
                return Some("idempotency_provider_undeclared");
            }
        }
        if let Some(Value::Object(_)) = action.get("verifier") {
            let target = action
                .get("verifier")
                .and_then(|verifier| verifier.get("action"));
            let verifier = actions
                .iter()
                .rev()
                .find(|candidate| candidate.get("id") == target);
            if !verifier.is_some_and(unattended) {
                return Some("verifier_interactive");
            }
        }
    }
    None
}

/// A verifier declares no human request, or only a password request with at least one declared Stored Input.
#[must_use]
pub fn unattended(verifier: &Value) -> bool {
    let requests = match verifier.get("human_requests") {
        Some(Value::Array(items)) => items.as_slice(),
        _ => return false,
    };
    let has_stored_input =
        matches!(verifier.get("stored_inputs"), Some(Value::Array(items)) if !items.is_empty());
    requests.is_empty()
        || (requests == [Value::String("input:password".to_owned())] && has_stored_input)
}
