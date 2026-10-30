//! The operations of the HTTP Integrations group (`Group::Integrations`): the Integration inventory, OAuth
//! authorization start, cancellation, and broker completion, and the Stored Input inventory and clearing
//! (`local/chat/private.py`, `local/http/server.py`).
//!
//! The controller's service adapter takes the Team's lifecycle lock around each call (for the completion, around
//! [`complete`] with the Team [`callback_team`] names) and resolves the bindings; every answer here is the operation's
//! JSON body, and every refusal a fixed [`Problem`] that never carries a claim, state, verifier, or token.

use strict_json::Value;
use team_core::future::BoxFuture;
use team_integrations::challenge::{ChallengeError, Pending};
use team_integrations::flow::{self, AssistantView};
use team_integrations::pending::IntegrationRequirement;
use team_integrations::pkce::{CallbackBinding, PkceStore};
use team_integrations::service::{OAuthService, ServiceError, Start};
use team_integrations::store::{Declaration as IntegrationDeclaration, IntegrationStore};

use crate::stored_input::{Declaration, StoredInputStore};

/// A refused or failed operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Problem {
    /// The HTTP status.
    pub status: u16,
    /// The fixed public message.
    pub message: &'static str,
    /// The stable code.
    pub code: &'static str,
}

const fn problem(status: u16, message: &'static str, code: &'static str) -> Problem {
    Problem {
        status,
        message,
        code,
    }
}

/// The OAuth authorization body is not the closed request.
pub const AUTHORIZATION_INVALID: Problem =
    problem(422, "OAuth authorization is invalid", "invalid-body");
/// A cancellation or callback body is not the closed request.
pub const BODY_INVALID: Problem = problem(422, "the request body is invalid", "invalid-body");
/// The paused Integration challenge expired or was consumed.
pub const CHALLENGE_EXPIRED: Problem = problem(
    409,
    "Assistant integration request expired; retry the message",
    "assistant-integration-challenge-expired",
);
/// The Integration is not pending authorization.
pub const NOT_PENDING: Problem = problem(
    409,
    "Assistant integration is not pending",
    "assistant-integration-not-pending",
);
/// The OAuth broker path failed.
pub const OAUTH_UNAVAILABLE: Problem = problem(
    502,
    "Assistant integration authorization could not be completed",
    "assistant-integration-oauth-unavailable",
);
/// The Integration contract or inventory could not be read.
pub const CONTRACT_UNAVAILABLE: Problem = problem(
    409,
    "Assistant integration contract is unavailable",
    "assistant-integration-contract-invalid",
);
/// The Stored Input store is unavailable.
pub const STORED_INPUT_UNAVAILABLE: Problem = problem(
    503,
    "Assistant Stored Input state is unavailable",
    "assistant-stored-input-state-unavailable",
);
/// The Stored Input is not declared by the Assistant.
pub const STORED_INPUT_NOT_FOUND: Problem = problem(
    404,
    "Assistant Stored Input is not declared",
    "assistant-stored-input-not-found",
);

fn text<'a>(body: &'a Value, key: &str) -> Option<&'a str> {
    match body.get(key) {
        Some(Value::String(text)) => Some(text),
        _ => None,
    }
}

/// Whether `body` is an object with exactly `keys`, every one a string.
fn exactly<'a>(body: &'a Value, keys: &[&str]) -> Option<Vec<&'a str>> {
    let Value::Object(members) = body else {
        return None;
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    let mut expected = keys.to_vec();
    expected.sort_unstable();
    if names != expected {
        return None;
    }
    keys.iter().map(|key| text(body, key)).collect()
}

/// `assistant-integration-list`: `{"team_id", "integrations": [...]}`.
///
/// # Errors
/// [`CONTRACT_UNAVAILABLE`] when the contract or inventory cannot be read.
pub fn list_integrations(
    team: &str,
    assistants: &[AssistantView],
    store: &IntegrationStore,
) -> Result<Value, Problem> {
    let payload =
        flow::inventory_payload(team, assistants, store).map_err(|_| CONTRACT_UNAVAILABLE)?;
    let integrations = payload
        .get("integrations")
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));
    Ok(Value::Object(vec![
        ("team_id".to_owned(), Value::String(team.to_owned())),
        ("integrations".to_owned(), integrations),
    ]))
}

/// `assistant-integration-authorize`: the broker start URL for one pending Integration of the Team's paused turn.
///
/// # Errors
/// [`AUTHORIZATION_INVALID`], [`CHALLENGE_EXPIRED`], [`NOT_PENDING`], or [`OAUTH_UNAVAILABLE`].
pub fn authorize<P>(
    pending: Result<std::sync::Arc<Pending<Vec<IntegrationRequirement>, P>>, ChallengeError>,
    service: &OAuthService,
    body: &Value,
) -> Result<Value, Problem> {
    let fields = exactly(
        body,
        &[
            "assistant_id",
            "integration_id",
            "callback_mode",
            "session_binding",
        ],
    );
    let Some([assistant_id, integration_id, callback_mode, session_binding]) = fields.as_deref()
    else {
        return Err(AUTHORIZATION_INVALID);
    };
    if !team_integrations::broker::CALLBACK_MODES.contains(callback_mode) {
        return Err(AUTHORIZATION_INVALID);
    }
    let pending = pending.map_err(|_| CHALLENGE_EXPIRED)?;
    let start = Start {
        session_binding,
        assistant_id,
        integration_id,
        callback_mode,
        resource_binding: None,
    };
    let url = service
        .authorization_url(&pending, &start)
        .map_err(|error| match error {
            ServiceError::NotPending | ServiceError::AlreadyConfigured => NOT_PENDING,
            ServiceError::CallbackMode => AUTHORIZATION_INVALID,
            ServiceError::Failed => OAUTH_UNAVAILABLE,
        })?;
    Ok(Value::Object(vec![(
        "authorization_url".to_owned(),
        Value::String(url),
    )]))
}

/// `assistant-integration-cancel`: removes only the PKCE continuations one browser session holds.
///
/// # Errors
/// [`BODY_INVALID`] or [`OAUTH_UNAVAILABLE`].
pub fn cancel(service: &OAuthService, body: &Value) -> Result<Value, Problem> {
    let fields = exactly(body, &["session_binding"]);
    let Some([session]) = fields.as_deref() else {
        return Err(BODY_INVALID);
    };
    let cancelled = service.cancel(session).map_err(|_| OAUTH_UNAVAILABLE)?;
    Ok(Value::Object(vec![(
        "cancelled".to_owned(),
        Value::Bool(cancelled),
    )]))
}

/// The Team a broker callback's state was started for, for the session that started it; the adapter locks it.
///
/// # Errors
/// [`BODY_INVALID`] or [`OAUTH_UNAVAILABLE`].
pub fn callback_team(pkce: &PkceStore, body: &Value) -> Result<String, Problem> {
    let fields = exactly(body, &["state", "claim", "session_binding"]);
    let Some([state, _, session]) = fields.as_deref() else {
        return Err(BODY_INVALID);
    };
    Ok(pkce
        .inspect_callback(state, session)
        .map_err(|_| OAUTH_UNAVAILABLE)?
        .team_id)
}

/// `assistant-integration-complete`: claims the grant for the browser session that started it, provided the
/// Assistant still runs its current artifact with the consented declaration (`current_declaration`), under the lock
/// of `team`.
///
/// # Errors
/// [`BODY_INVALID`] or [`OAUTH_UNAVAILABLE`].
pub async fn complete<F>(
    service: &OAuthService,
    team: &str,
    body: &Value,
    current_declaration: F,
) -> Result<Value, Problem>
where
    F: for<'b> FnOnce(&'b CallbackBinding) -> BoxFuture<'b, Result<IntegrationDeclaration, ()>>,
{
    let fields = exactly(body, &["state", "claim", "session_binding"]);
    let Some([state, claim, session]) = fields.as_deref() else {
        return Err(BODY_INVALID);
    };
    let team = team.to_owned();
    let resolver = bound(move |binding| {
        if binding.team_id == team {
            current_declaration(binding)
        } else {
            Box::pin(async { Err(()) })
        }
    });
    let completed = service
        .complete(state, claim, session, resolver)
        .await
        .map_err(|_| OAUTH_UNAVAILABLE)?;
    let text = |value: String| Value::String(value);
    Ok(Value::Object(vec![
        ("connected".to_owned(), Value::Bool(true)),
        ("team_id".to_owned(), text(completed.team_id)),
        ("assistant_id".to_owned(), text(completed.assistant_id)),
        ("integration_id".to_owned(), text(completed.integration_id)),
    ]))
}

/// Fixes a declaration resolver's higher-ranked signature.
fn bound<F>(resolver: F) -> F
where
    F: for<'b> FnOnce(&'b CallbackBinding) -> BoxFuture<'b, Result<IntegrationDeclaration, ()>>,
{
    resolver
}

/// `assistant-stored-input-list`: every declared Stored Input of the Team's Assistants with its status.
///
/// # Errors
/// [`STORED_INPUT_UNAVAILABLE`].
pub fn list_stored_inputs(
    store: &StoredInputStore,
    team: &str,
    assistants: &[(String, Vec<(String, Declaration)>)],
) -> Result<Value, Problem> {
    store
        .inventory(team, assistants)
        .map_err(|_| STORED_INPUT_UNAVAILABLE)
}

/// `assistant-stored-input-clear`: deletes one declared value; `declared` lists the Assistant's Stored Input ids.
///
/// # Errors
/// [`STORED_INPUT_NOT_FOUND`] for an undeclared id, [`STORED_INPUT_UNAVAILABLE`] otherwise.
pub fn clear_stored_input(
    store: &StoredInputStore,
    team: &str,
    assistant: &str,
    declared: &[String],
    id: &str,
) -> Result<Value, Problem> {
    if !declared.iter().any(|known| known == id) {
        return Err(STORED_INPUT_NOT_FOUND);
    }
    let cleared = store
        .delete((team, assistant, id))
        .map_err(|_| STORED_INPUT_UNAVAILABLE)?;
    let text = |value: &str| Value::String(value.to_owned());
    Ok(Value::Object(vec![
        ("team_id".to_owned(), text(team)),
        ("assistant_id".to_owned(), text(assistant)),
        ("stored_input_id".to_owned(), text(id)),
        ("cleared".to_owned(), Value::Bool(cleared)),
    ]))
}

#[cfg(test)]
mod tests;
