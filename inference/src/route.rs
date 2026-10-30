//! Capability planning and lifecycle intent routing: stateless, bounded Brain decisions over public Assistant
//! descriptions (ADR-0077, ADR-0079).
//!
//! Every request is admitted whole before anything is sent: a refused request never reaches the Brain. Every answer is
//! admitted whole or refused; its reported usage is recorded first.

use strict_json::Value;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_protocol::identifiers::{
    canonical_action_id, canonical_assistant_id, canonical_identifier,
};
use team_protocol::payload::canonical_locale;
use team_protocol::python::text;
use team_protocol::python::value::{object_of, string};
use team_protocol::turn::{
    MAX_CAPABILITY_ACTIONS, MAX_CAPABILITY_CANDIDATES, MAX_CAPABILITY_INTEGRATIONS,
    MAX_CAPABILITY_NAME_CHARS, MAX_CAPABILITY_SUMMARY_CHARS, MAX_INTENT_ROUTE_CANDIDATES,
    MAX_INTENT_ROUTE_NAME_CHARS, MAX_INTENT_ROUTE_SUMMARY_CHARS, MAX_OBJECTIVE_CHARS, SAFE_ID_RE,
};
use tokio::time::Instant;

use crate::brain::{BrainClient, BrainError, BrainOperation, BrainRequest, ModelCredential};
use crate::config::CATALOG_MODELS;
use crate::credential::admissible_api_key;
use crate::runtime::{
    ConversationEntry, RESPONSE_TIMEOUT, bounded_text, conversation_wire, metered,
    validate_conversation, with_credential,
};
use crate::usage::UsageRecorder;

mod parse;

pub use parse::{parse_capability_plan, parse_intent_route};

/// A capability plan ends within Admin's 65-second wait, with room to deliver its answer or failure (ADR-0079).
pub const CAPABILITY_PLAN_DEADLINE: std::time::Duration = std::time::Duration::from_secs(55);

/// Why a decision failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteError {
    /// The request is outside its contract; nothing was sent.
    Invalid,
    /// The Brain failed or answered outside the contract.
    Brain(BrainError),
}

impl From<BrainError> for RouteError {
    fn from(error: BrainError) -> Self {
        Self::Brain(error)
    }
}

/// One Integration a capability candidate declares.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct CapabilityIntegration {
    /// The Integration id.
    pub id: String,
    /// Its provider.
    pub provider: String,
}

/// One public Assistant a capability plan may select.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityCandidate {
    /// The Assistant id.
    pub id: String,
    /// Its public name.
    pub name: String,
    /// Its public summary.
    pub summary: String,
    /// Its Action ids, sorted and distinct.
    pub actions: Vec<String>,
    /// Its Integrations, sorted by id and provider and distinct.
    pub integrations: Vec<CapabilityIntegration>,
}

/// A capability plan's verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlanStatus {
    /// The installed Assistants suffice.
    Sufficient,
    /// The selected candidates must be installed.
    InstallRequired,
}

/// A capability plan.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityPlan {
    /// The verdict.
    pub status: PlanStatus,
    /// The selected candidates, sorted; empty exactly when sufficient.
    pub assistant_ids: Vec<String>,
}

fn strictly_sorted<T: Ord>(items: &[T]) -> bool {
    items.windows(2).all(|pair| pair[0] < pair[1])
}

fn credential_admitted(credential: &ModelCredential) -> bool {
    CATALOG_MODELS.contains_key(&credential.provider)
        && SAFE_ID_RE.is_full(&credential.model)
        && admissible_api_key(credential.api_key.expose())
}

fn candidate_admitted(candidate: &CapabilityCandidate) -> bool {
    let actions = &candidate.actions;
    let integrations = &candidate.integrations;
    canonical_assistant_id(&Value::String(candidate.id.clone())).is_some()
        && actions
            .iter()
            .all(|action| canonical_action_id(&Value::String(action.clone())).is_some())
        && (1..=MAX_CAPABILITY_ACTIONS).contains(&actions.len())
        && strictly_sorted(actions)
        && integrations.len() <= MAX_CAPABILITY_INTEGRATIONS
        && integrations.iter().all(|integration| {
            canonical_identifier(&Value::String(integration.id.clone())).is_some()
                && canonical_identifier(&Value::String(integration.provider.clone())).is_some()
        })
        && strictly_sorted(integrations)
        && bounded_text(&candidate.name, MAX_CAPABILITY_NAME_CHARS, false)
        && bounded_text(&candidate.summary, MAX_CAPABILITY_SUMMARY_CHARS, false)
}

/// Whether a capability plan's objective and candidates are inside their contract.
#[must_use]
pub fn capability_plan_admitted(objective: &str, candidates: &[CapabilityCandidate]) -> bool {
    let ids: Vec<&str> = candidates
        .iter()
        .map(|candidate| candidate.id.as_str())
        .collect();
    bounded_text(objective, MAX_OBJECTIVE_CHARS, true)
        && (1..=MAX_CAPABILITY_CANDIDATES).contains(&candidates.len())
        && candidates.iter().all(candidate_admitted)
        && strictly_sorted(&ids)
}

fn candidate_wire(candidate: &CapabilityCandidate) -> Value {
    object_of(vec![
        ("id", text(&candidate.id)),
        ("name", text(&candidate.name)),
        ("summary", text(&candidate.summary)),
        (
            "actions",
            Value::Array(
                candidate
                    .actions
                    .iter()
                    .map(|action| text(action))
                    .collect(),
            ),
        ),
        (
            "integrations",
            Value::Array(
                candidate
                    .integrations
                    .iter()
                    .map(|integration| {
                        object_of(vec![
                            ("id", text(&integration.id)),
                            ("provider", text(&integration.provider)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ])
}

async fn post(
    client: &dyn BrainClient,
    operation: BrainOperation,
    body: Value,
    deadline: std::time::Duration,
    stop: &Stop,
) -> Result<Value, BrainError> {
    client
        .call(BrainRequest {
            operation,
            body,
            credential: None,
            deadline: Instant::now() + deadline,
            stop,
        })
        .await
}

/// Selects the bounded subset of public `candidates` the `objective` needs installed.
///
/// # Errors
/// [`RouteError::Invalid`] before anything is sent; [`RouteError::Brain`] for a failed or invalid answer.
pub async fn capability_plan(
    client: &dyn BrainClient,
    usage: &dyn UsageRecorder,
    credential: &ModelCredential,
    objective: &str,
    candidates: &[CapabilityCandidate],
    stop: &Stop,
) -> Result<CapabilityPlan, RouteError> {
    if !credential_admitted(credential) || !capability_plan_admitted(objective, candidates) {
        return Err(RouteError::Invalid);
    }
    let body = object_of(vec![
        ("objective", Value::String(objective.to_owned())),
        (
            "candidates",
            Value::Array(candidates.iter().map(candidate_wire).collect()),
        ),
    ]);
    let body = with_credential(body, credential);
    let answer = post(
        client,
        BrainOperation::CapabilityPlan,
        body,
        CAPABILITY_PLAN_DEADLINE,
        stop,
    )
    .await?;
    let rest = metered(answer, "capability-plan", credential, usage).ok_or(BrainError::Protocol)?;
    Ok(parse_capability_plan(&rest, candidates).ok_or(BrainError::Protocol)?)
}

/// A lifecycle intent a route may expect.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LifecycleIntent {
    /// `assistant-install`.
    Install,
    /// `assistant-uninstall`.
    Uninstall,
}

impl LifecycleIntent {
    /// The wire name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Install => "assistant-install",
            Self::Uninstall => "assistant-uninstall",
        }
    }
}

/// The intent a route resolves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteIntent {
    /// `ordinary-task`.
    OrdinaryTask,
    /// `assistant-install`.
    Install,
    /// `assistant-uninstall`.
    Uninstall,
    /// `unresolved`.
    Unresolved,
}

impl RouteIntent {
    /// The intent of a wire name.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "ordinary-task" => Some(Self::OrdinaryTask),
            "assistant-install" => Some(Self::Install),
            "assistant-uninstall" => Some(Self::Uninstall),
            "unresolved" => Some(Self::Unresolved),
            _ => None,
        }
    }
}

/// Whether the task needs a capability, as only a classification that described installable Assistants answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability {
    /// `not-needed`.
    NotNeeded,
    /// `possible`.
    Possible,
}

/// One directory Assistant a lifecycle route may select.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirectoryCandidate {
    /// The Assistant id.
    pub id: String,
    /// Its name.
    pub name: String,
    /// Its summary; empty for an uninstall candidate.
    pub summary: String,
}

/// One Assistant the Team could install, as a classification may describe it (ADR-0077 amendment).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Installable {
    /// Its name; names may repeat.
    pub name: String,
    /// Its summary.
    pub summary: String,
}

/// The Assistant the conversation last named.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LifecycleReference {
    /// The Assistant id.
    pub id: String,
    /// Its name.
    pub name: String,
}

/// What a route sees beside the objective.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LifecycleContext {
    /// The Assistant the conversation last named.
    pub reference: Option<LifecycleReference>,
    /// The committed presentation history.
    pub conversation: Vec<ConversationEntry>,
    /// The interface language every route reply is written in (ADR-0090); required.
    pub locale: Option<String>,
}

/// One intent route request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IntentRouteRequest {
    /// The person's objective.
    pub objective: String,
    /// The lifecycle intent a selection expects, or `None` for a classification.
    pub expected: Option<LifecycleIntent>,
    /// The directory shortlist of a selection; empty for a classification.
    pub candidates: Vec<DirectoryCandidate>,
    /// The context.
    pub context: LifecycleContext,
    /// The installable Assistants a keyed classification describes (ADR-0077 amendment).
    pub installable: Vec<Installable>,
}

/// A resolved route.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IntentRoute {
    /// The intent.
    pub intent: RouteIntent,
    /// The directory query of an install or uninstall classification.
    pub query: String,
    /// The selected Assistants.
    pub assistant_ids: Vec<String>,
    /// The clarification reply.
    pub reply: String,
    /// Whether an install classification's task continues after the install.
    pub task_follows: bool,
    /// The capability answer, exactly when asked.
    pub capability: Option<Capability>,
}

fn context_admitted(context: &LifecycleContext, expected: Option<LifecycleIntent>) -> bool {
    let locale = context
        .locale
        .as_ref()
        .is_some_and(|locale| canonical_locale(&Value::String(locale.clone())).is_some());
    let reference = context.reference.as_ref().is_none_or(|reference| {
        canonical_assistant_id(&Value::String(reference.id.clone())).is_some()
            && bounded_text(&reference.name, MAX_INTENT_ROUTE_NAME_CHARS, false)
    });
    let conversation = validate_conversation(&context.conversation).is_some();
    let selection_alone =
        expected.is_none() || (context.reference.is_none() && context.conversation.is_empty());
    locale && reference && conversation && selection_alone
}

fn directory_admitted(
    candidates: &[DirectoryCandidate],
    expected: Option<LifecycleIntent>,
) -> bool {
    let Some(expected) = expected else {
        return candidates.is_empty();
    };
    let ids: Vec<&str> = candidates
        .iter()
        .map(|candidate| candidate.id.as_str())
        .collect();
    candidates.len() <= MAX_INTENT_ROUTE_CANDIDATES
        && candidates.iter().all(|candidate| {
            let summary = &candidate.summary;
            canonical_assistant_id(&Value::String(candidate.id.clone())).is_some()
                && bounded_text(&candidate.name, MAX_INTENT_ROUTE_NAME_CHARS, false)
                && text::len(summary) <= MAX_INTENT_ROUTE_SUMMARY_CHARS
                && (summary.is_empty()
                    || bounded_text(summary, MAX_INTENT_ROUTE_SUMMARY_CHARS, false))
                && (expected == LifecycleIntent::Install || summary.is_empty())
        })
        && strictly_sorted(&ids)
}

fn installable_admitted(installable: &[Installable]) -> bool {
    installable.len() <= MAX_CAPABILITY_CANDIDATES
        && installable.iter().all(|item| {
            bounded_text(&item.name, MAX_CAPABILITY_NAME_CHARS, false)
                && bounded_text(&item.summary, MAX_CAPABILITY_SUMMARY_CHARS, false)
        })
}

/// Whether a route request and its credentials are inside their contract.
#[must_use]
pub fn intent_route_admitted(
    credential: &ModelCredential,
    decision_key: Option<&Secret>,
    request: &IntentRouteRequest,
) -> bool {
    let keyed = decision_key
        .is_none_or(|key| request.expected.is_none() && admissible_api_key(key.expose()));
    let described = request.installable.is_empty()
        || (request.expected.is_none()
            && decision_key.is_some()
            && installable_admitted(&request.installable));
    credential_admitted(credential)
        && keyed
        && bounded_text(&request.objective, MAX_OBJECTIVE_CHARS, true)
        && context_admitted(&request.context, request.expected)
        && directory_admitted(&request.candidates, request.expected)
        && described
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn directory_wire(candidates: &[DirectoryCandidate]) -> Value {
    let items = candidates
        .iter()
        .map(|item| {
            object_of(vec![
                ("id", text(&item.id)),
                ("name", text(&item.name)),
                ("summary", text(&item.summary)),
            ])
        })
        .collect();
    Value::Array(items)
}

fn reference_wire(reference: Option<&LifecycleReference>) -> Value {
    reference.map_or(Value::Null, |reference| {
        object_of(vec![
            ("id", text(&reference.id)),
            ("name", text(&reference.name)),
        ])
    })
}

fn installable_wire(installable: &[Installable]) -> Value {
    let items = installable
        .iter()
        .map(|item| {
            object_of(vec![
                ("name", text(&item.name)),
                ("summary", text(&item.summary)),
            ])
        })
        .collect();
    Value::Array(items)
}

fn route_body(decision_key: Option<&Secret>, request: &IntentRouteRequest) -> Value {
    let mut members = vec![
        ("objective", text(&request.objective)),
        (
            "expected_intent",
            request
                .expected
                .map_or(Value::Null, |intent| text(intent.name())),
        ),
        ("candidates", directory_wire(&request.candidates)),
        (
            "lifecycle_reference",
            reference_wire(request.context.reference.as_ref()),
        ),
        (
            "conversation",
            conversation_wire(&request.context.conversation),
        ),
        (
            "locale",
            request.context.locale.as_deref().map_or(Value::Null, text),
        ),
    ];
    if let Some(key) = decision_key {
        let provider = object_of(vec![
            ("provider", text("typesafe")),
            ("api_key", text(key.expose())),
        ]);
        members.push(("decision_provider", provider));
    }
    if !request.installable.is_empty() {
        members.push(("installable", installable_wire(&request.installable)));
    }
    object_of(members)
}

/// Classifies an objective's lifecycle intent, or selects the Assistants an expected lifecycle intent names.
///
/// # Errors
/// [`RouteError::Invalid`] before anything is sent; [`RouteError::Brain`] for a failed or invalid answer.
pub async fn intent_route(
    client: &dyn BrainClient,
    usage: &dyn UsageRecorder,
    credential: &ModelCredential,
    decision_key: Option<&Secret>,
    request: &IntentRouteRequest,
    stop: &Stop,
) -> Result<IntentRoute, RouteError> {
    if !intent_route_admitted(credential, decision_key, request) {
        return Err(RouteError::Invalid);
    }
    let body = with_credential(route_body(decision_key, request), credential);
    let answer = post(
        client,
        BrainOperation::IntentRoute,
        body,
        RESPONSE_TIMEOUT,
        stop,
    )
    .await?;
    let rest = metered(answer, "intent-route", credential, usage).ok_or(BrainError::Protocol)?;
    let asked = !request.installable.is_empty();
    Ok(
        parse_intent_route(&rest, request.expected, &request.candidates, asked)
            .ok_or(BrainError::Protocol)?,
    )
}

#[cfg(test)]
mod tests;

/// The string members of an answer, refused when any is not a string.
fn strings(value: &Value) -> Option<Vec<String>> {
    let Value::Array(items) = value else {
        return None;
    };
    items
        .iter()
        .map(|item| string(item).map(str::to_owned))
        .collect()
}
