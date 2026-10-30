//! The projection of one terminal RPC frame (`action/execution.py` `project_rpc_result`, ADR-0092, ADR-0106).
//!
//! A handled failure is re-redacted with every value Team injected and kept as a diagnostic. Every other branch refuses
//! an echo of any injected value outright: a result, a human request, or a Stored Input rejection. A request must pass
//! human-request admission, never re-ask an authorization the transcript holds, and never ask for a Stored Input Team
//! holds. A result must satisfy the Action's reviewed output schema and the journal's durable bound.

use strict_json::Value;
use team_core::secret::Secret;

use crate::failure::{self, ActionFailure};
use crate::human::{self, HumanRequest};
use crate::result::require_durable;

/// What one invocation's projection is checked against.
#[derive(Clone, Copy, Debug)]
pub struct ResultPolicy<'a> {
    /// The human-request kinds the Action declares.
    pub human_requests: &'a [String],
    /// Whether the transcript already holds an authorization.
    pub authorization_requested: bool,
    /// Every value Team injected (Integration tokens and held Stored Inputs), by id.
    pub injected: &'a [(String, Secret)],
    /// The held Stored Input ids, which alone may be rejected.
    pub held_stored_inputs: &'a [String],
    /// The Stored Input ids the Action declares.
    pub declared_stored_inputs: &'a [String],
    /// The binding's reviewed English messages.
    pub messages: &'a Value,
    /// A file-taking Action whose file content was withheld cannot have succeeded with it (ADR-0093).
    pub file_withheld: bool,
}

/// An admitted terminal frame.
#[derive(Clone, Debug, PartialEq)]
pub enum Projected {
    /// The Action's validated, durable result.
    Result(Value),
    /// The Action asks a person; the batch suspends.
    Request(HumanRequest),
}

/// A refused terminal frame.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refused {
    /// A handled failure, re-redacted.
    Failure(ActionFailure),
    /// The frame echoed an injected value; it never travels further.
    SecretExposure,
    /// The frame is not an admitted Spec v1 result, request, or rejection.
    Invalid,
    /// The Assistant rejected this held Stored Input, which Team then clears.
    StoredInputRejected(String),
}

/// Whether `value` holds any of `secrets` literally in a string or key, or nests beyond the inspection bound.
#[must_use]
pub fn contains_secret(value: &Value, secrets: &[&str]) -> bool {
    fn visit(value: &Value, secrets: &[&str], depth: usize) -> bool {
        if depth > 32 {
            return true;
        }
        match value {
            Value::String(text) => secrets.iter().any(|secret| text.contains(secret)),
            Value::Array(items) => items.iter().any(|item| visit(item, secrets, depth + 1)),
            Value::Object(members) => members.iter().any(|(key, item)| {
                visit(&Value::String(key.clone()), secrets, depth + 1)
                    || visit(item, secrets, depth + 1)
            }),
            _ => false,
        }
    }
    let secrets: Vec<&str> = secrets
        .iter()
        .copied()
        .filter(|secret| !secret.is_empty())
        .collect();
    !secrets.is_empty() && visit(value, &secrets, 0)
}

/// Projects one terminal frame; `validate` checks a result against the Action's output schema.
///
/// # Errors
/// [`Refused`] for every frame that is not an admitted result or request.
pub fn project(
    raw: &Value,
    policy: &ResultPolicy<'_>,
    validate: impl FnOnce(&Value) -> bool,
) -> Result<Projected, Refused> {
    let secrets: Vec<&str> = policy
        .injected
        .iter()
        .map(|(_, secret)| secret.expose())
        .collect();
    if failure::is_failure(raw) {
        return Err(failure::admit(raw, &secrets).map_or(Refused::Invalid, Refused::Failure));
    }
    if contains_secret(raw, &secrets) {
        return Err(Refused::SecretExposure);
    }
    let Value::Object(members) = raw else {
        return Err(Refused::Invalid);
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    let kind = match raw.get("type") {
        Some(Value::String(kind)) => kind.as_str(),
        _ => return Err(Refused::Invalid),
    };
    match (kind, names.as_slice()) {
        ("stored_input_rejected", ["stored_input", "type"]) => rejected(raw, policy),
        ("request", ["request", "type"]) => request(raw, policy),
        ("result", ["result", "type"]) if !policy.file_withheld => {
            let result = raw.get("result").ok_or(Refused::Invalid)?;
            if !validate(result) || require_durable(result).is_err() {
                return Err(Refused::Invalid);
            }
            Ok(Projected::Result(result.clone()))
        }
        _ => Err(Refused::Invalid),
    }
}

fn rejected(raw: &Value, policy: &ResultPolicy<'_>) -> Result<Projected, Refused> {
    match raw.get("stored_input") {
        Some(Value::String(id))
            if policy.declared_stored_inputs.contains(id)
                && policy.held_stored_inputs.contains(id) =>
        {
            Err(Refused::StoredInputRejected(id.clone()))
        }
        _ => Err(Refused::Invalid),
    }
}

fn request(raw: &Value, policy: &ResultPolicy<'_>) -> Result<Projected, Refused> {
    let request = human::validate_request(
        raw.get("request").ok_or(Refused::Invalid)?,
        policy.human_requests,
        policy.declared_stored_inputs,
        policy.messages,
    )
    .map_err(|_| Refused::Invalid)?;
    if policy.authorization_requested && human::authorizes(&request.kind) {
        return Err(Refused::Invalid);
    }
    // A held Stored Input is injected, so a request for it is never answerable.
    if request
        .stored_input
        .as_ref()
        .is_some_and(|id| policy.held_stored_inputs.contains(id))
    {
        return Err(Refused::Invalid);
    }
    Ok(Projected::Request(request))
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_core::secret::Secret;

    use super::{Projected, Refused, ResultPolicy, contains_secret, project};

    const TOKEN: &str = "oauth-Access-Token-7f3a9c";
    const STORED: &str = "stored/input+value=42";

    fn json(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("json")
    }

    fn projected(raw: &str, file_withheld: bool) -> Result<Projected, Refused> {
        let injected = [
            (
                "integration:cloudflare".to_owned(),
                Secret::new(TOKEN.to_owned()),
            ),
            (
                "stored-input:api-key".to_owned(),
                Secret::new(STORED.to_owned()),
            ),
        ];
        let held = ["api-key".to_owned()];
        let policy = ResultPolicy {
            human_requests: &[],
            authorization_requested: false,
            injected: &injected,
            held_stored_inputs: &held,
            declared_stored_inputs: &held,
            messages: &Value::Array(Vec::new()),
            file_withheld,
        };
        project(&json(raw), &policy, |_| true)
    }

    #[test]
    fn response_echoing_any_protected_form_refused_outside_the_failure_branch() {
        for raw in [
            format!(r#"{{"type":"result","result":{{"value":"{TOKEN}"}}}}"#),
            format!(r#"{{"type":"result","result":{{"{STORED}":1}}}}"#),
            format!(r#"{{"type":"stored_input_rejected","stored_input":"{STORED}"}}"#),
        ] {
            assert_eq!(projected(&raw, false), Err(Refused::SecretExposure));
        }
        let public = projected(r#"{"type":"result","result":{"value":"public"}}"#, false);
        assert_eq!(public, Ok(Projected::Result(json(r#"{"value":"public"}"#))));
    }

    #[test]
    fn a_handled_failure_is_re_redacted_instead_of_refused_as_an_echo() {
        let raw = format!(
            r#"{{"type":"failure","failure":{{"error_type":"E","message":"{TOKEN} {STORED}","provider":null,"http_status":null,"response_excerpt":null,"redacted":false,"truncated":false}}}}"#
        );
        let Err(Refused::Failure(failure)) = projected(&raw, false) else {
            panic!("a failure")
        };
        assert_eq!(
            (failure.message.as_str(), failure.redacted),
            ("[REDACTED] [REDACTED]", true)
        );
        for invalid in [
            r#"{"type":"failure"}"#,
            r#"{"type":"failure","failure":{},"extra":true}"#,
        ] {
            assert_eq!(projected(invalid, false), Err(Refused::Invalid));
        }
    }

    #[test]
    fn only_a_held_declared_stored_input_may_be_rejected_and_a_withheld_file_never_succeeds() {
        let rejected = projected(
            r#"{"type":"stored_input_rejected","stored_input":"api-key"}"#,
            false,
        );
        assert_eq!(
            rejected,
            Err(Refused::StoredInputRejected("api-key".to_owned()))
        );
        assert_eq!(
            projected(
                r#"{"type":"stored_input_rejected","stored_input":"other"}"#,
                false
            ),
            Err(Refused::Invalid)
        );
        assert_eq!(
            projected(r#"{"type":"result","result":{}}"#, true),
            Err(Refused::Invalid)
        );
        assert_eq!(
            projected(r#"{"type":"result","result":{},"extra":1}"#, false),
            Err(Refused::Invalid)
        );
        assert_eq!(
            projected(r#"{"type":"request","request":{}}"#, false),
            Err(Refused::Invalid)
        );
    }

    #[test]
    fn deep_nesting_is_treated_as_an_echo() {
        let deep = (0..40).fold(json(r#""x""#), |value, _| Value::Array(vec![value]));
        assert!(contains_secret(&deep, &["secret"]));
        assert!(!contains_secret(&deep, &[""]));
    }
}
