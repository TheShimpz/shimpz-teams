//! The closed public and private contracts of Assistant Integrations (`integrations/flow.py`).
//!
//! Public projections hold only reviewed intent and bounded account metadata; tokens stay Team's and are resolved
//! into one Action's provider calls only at the last private boundary before it runs.

use std::collections::BTreeMap;
use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::secret::Secret;
use team_protocol::payload::ASSISTANT_VERSION_RE;
use tokio::time::Instant;

use crate::challenge::{Pending, team_id};
use crate::pending::{IntegrationRequirement, RequiredIntegration};
use crate::providers::{self, identifier};
use crate::service::OAuthService;
use crate::store::{
    Declaration, Identity, IntegrationStore, Metadata, Status, StoreError, assistant_id, intent,
};
use crate::text::public_bytes;
use crate::token::is_bearer;

/// The most Actions one batch holds.
pub const MAX_BATCH_ACTIONS: usize = 64;
/// The most requirements one paused batch carries.
pub const MAX_INTEGRATION_REQUIREMENTS: usize = 64;
/// The most Assistants one inventory lists.
pub const MAX_INVENTORY_ASSISTANTS: usize = 64;
/// The most Integrations one inventory lists.
pub const MAX_INVENTORY_INTEGRATIONS: usize = 256;
/// The most Integrations one Action or Assistant declares.
pub const MAX_INTEGRATIONS_PER_ACTION: usize = 16;
/// The longest public text, in bytes.
pub const MAX_PUBLIC_TEXT_BYTES: usize = 512;
const FORBIDDEN_PUBLIC_FIELDS: [&str; 8] = [
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "authorization_code",
    "code",
    "code_verifier",
    "token",
];

/// An Integration request broke its closed contract; never carries private text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlowError {
    /// An id, name, summary, version, or declaration is invalid, or a projection would carry a sensitive field.
    Invalid,
    /// The Assistant of a request or challenge is not installed.
    Unavailable,
    /// An Action names an Integration its Assistant does not declare.
    Undeclared,
    /// A batch, requirement list, or inventory exceeds its bound.
    TooMany,
    /// The paused challenge no longer matches the Assistant's binding or has expired.
    Changed,
    /// The Integration inventory or a token could not be read.
    Unresolved,
}

impl std::fmt::Display for FlowError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Invalid => "the Assistant Integration contract is invalid",
            Self::Unavailable => "the Assistant is unavailable",
            Self::Undeclared => "an Action references an undeclared Integration",
            Self::TooMany => "the Integration request exceeds its bound",
            Self::Changed => "the Integration challenge changed or expired",
            Self::Unresolved => "the Assistant Integration could not be resolved",
        })
    }
}

impl std::error::Error for FlowError {}

/// One declared Action as the Integration flow reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionView {
    /// The Action id.
    pub id: String,
    /// Its reviewed summary.
    pub summary: String,
    /// The Integrations it uses, in declared order.
    pub integrations: Vec<String>,
}

/// One installed Assistant binding as the Integration flow reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AssistantView {
    /// The Assistant id.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its public name.
    pub name: String,
    /// Its public summary.
    pub summary: String,
    /// Its Actions.
    pub actions: Vec<ActionView>,
    /// Its Integration declarations.
    pub integrations: Vec<(String, Declaration)>,
}

impl AssistantView {
    /// The binding's id, name, and declaration count are admitted.
    fn admitted(&self) -> Result<&Self, FlowError> {
        if !assistant_id(&self.assistant_id)
            || !public_bytes(&self.name, MAX_PUBLIC_TEXT_BYTES)
            || self.integrations.len() > MAX_INTEGRATIONS_PER_ACTION
        {
            return Err(FlowError::Invalid);
        }
        Ok(self)
    }

    fn action(&self, id: &str) -> Result<&ActionView, FlowError> {
        if !action_id(id) {
            return Err(FlowError::Invalid);
        }
        let action = self
            .actions
            .iter()
            .find(|action| action.id == id)
            .ok_or(FlowError::Invalid)?;
        if !public_bytes(&action.summary, MAX_PUBLIC_TEXT_BYTES)
            || action.integrations.len() > MAX_INTEGRATIONS_PER_ACTION
            || !unique(&action.integrations)
        {
            return Err(FlowError::Invalid);
        }
        Ok(action)
    }

    fn declaration(&self, integration: &str) -> Option<&Declaration> {
        self.integrations
            .iter()
            .find(|(id, _)| id == integration)
            .map(|(_, declaration)| declaration)
    }

    /// The reviewed intent of one declared Integration.
    fn intent(&self, integration: &str) -> Result<(&'static str, Vec<String>), FlowError> {
        let declaration = self.declaration(integration).ok_or(FlowError::Undeclared)?;
        intent(&declaration.provider, &declaration.scopes).map_err(|_| FlowError::Invalid)
    }
}

/// Whether no item repeats.
fn unique(items: &[String]) -> bool {
    items
        .iter()
        .enumerate()
        .all(|(index, item)| !items[..index].contains(item))
}

fn action_id(text: &str) -> bool {
    team_protocol::identifiers::canonical_action_id(&Value::String(text.to_owned())).is_some()
}

fn binding<'a>(
    bindings: &'a [AssistantView],
    assistant: &str,
) -> Result<&'a AssistantView, FlowError> {
    bindings
        .iter()
        .find(|binding| binding.assistant_id == assistant)
        .ok_or(FlowError::Unavailable)?
        .admitted()
}

/// Every declared Integration's inventory row, checked against its declaration.
fn metadata_for(
    team: &str,
    assistant: &AssistantView,
    declared: &[(String, Declaration)],
    store: &IntegrationStore,
) -> Result<BTreeMap<String, Metadata>, FlowError> {
    let rows = store
        .metadata(team, &assistant.assistant_id, declared)
        .map_err(|_| FlowError::Unresolved)?;
    if rows.len() != declared.len() {
        return Err(FlowError::Unresolved);
    }
    let mut indexed = BTreeMap::new();
    for (row, (id, declaration)) in rows.into_iter().zip(declared) {
        let (provider, scopes) =
            intent(&declaration.provider, &declaration.scopes).map_err(|_| FlowError::Invalid)?;
        if row.id != *id || row.provider != provider || row.scopes != scopes {
            return Err(FlowError::Unresolved);
        }
        indexed.insert(id.clone(), row);
    }
    Ok(indexed)
}

/// One Action of a batch: its Assistant and Action ids.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Requested<'a> {
    /// The Assistant.
    pub assistant_id: &'a str,
    /// The Action.
    pub action: &'a str,
}

type Grouped<'a> = BTreeMap<&'a str, BTreeMap<String, Vec<String>>>;

/// Every Integration of the batch a person must connect before the batch's first Action may run: one requirement per
/// unusable Integration, sorted by Assistant and Integration. A connected or merely expired (refreshable) grant needs
/// no person.
///
/// # Errors
/// [`FlowError`] for an oversized batch, an unavailable Assistant, an invalid or undeclared contract, or an
/// unreadable inventory.
pub fn requirements_for_batch(
    team: &str,
    bindings: &[AssistantView],
    requests: &[Requested<'_>],
    store: &IntegrationStore,
) -> Result<Vec<IntegrationRequirement>, FlowError> {
    if !team_id(team) {
        return Err(FlowError::Invalid);
    }
    if requests.len() > MAX_BATCH_ACTIONS {
        return Err(FlowError::TooMany);
    }
    let mut grouped: Grouped<'_> = BTreeMap::new();
    for request in requests {
        let assistant = binding(bindings, request.assistant_id)?;
        let action = assistant.action(request.action)?;
        for integration in &action.integrations {
            if !identifier(integration) || assistant.declaration(integration).is_none() {
                return Err(FlowError::Undeclared);
            }
            let actions = grouped
                .entry(request.assistant_id)
                .or_default()
                .entry(integration.clone())
                .or_default();
            if !actions.contains(&action.id) {
                actions.push(action.id.clone());
            }
        }
    }
    let mut requirements = Vec::new();
    for (assistant_id, integrations) in grouped {
        let assistant = binding(bindings, assistant_id)?;
        requirements.extend(unusable(team, assistant, integrations, store)?);
        if requirements.len() > MAX_INTEGRATION_REQUIREMENTS {
            return Err(FlowError::TooMany);
        }
    }
    Ok(requirements)
}

fn unusable(
    team: &str,
    assistant: &AssistantView,
    integrations: BTreeMap<String, Vec<String>>,
    store: &IntegrationStore,
) -> Result<Vec<IntegrationRequirement>, FlowError> {
    let declared: Vec<(String, Declaration)> = integrations
        .keys()
        .map(|id| {
            (
                id.clone(),
                assistant
                    .declaration(id)
                    .cloned()
                    .unwrap_or_else(|| unreachable!()),
            )
        })
        .collect();
    let rows = metadata_for(team, assistant, &declared, store)?;
    let mut requirements = Vec::new();
    for (id, mut actions) in integrations {
        if matches!(
            rows.get(&id).map(|row| row.status),
            Some(Status::Connected | Status::RefreshRequired)
        ) {
            continue;
        }
        let (provider, scopes) = assistant.intent(&id)?;
        actions.sort_unstable();
        requirements.push(IntegrationRequirement {
            assistant_id: assistant.assistant_id.clone(),
            assistant_name: assistant.name.clone(),
            action_ids: actions,
            integrations: vec![RequiredIntegration {
                id,
                provider: provider.to_owned(),
                scopes,
            }],
        });
    }
    Ok(requirements)
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn strings(values: &[String]) -> Value {
    Value::Array(values.iter().cloned().map(Value::String).collect())
}

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(name, value)| (name.to_owned(), value))
            .collect(),
    )
}

/// `" ".join(part.capitalize() for part in action_id.split("-"))`.
fn action_name(id: &str) -> String {
    id.split('-')
        .map(|part| {
            let mut characters = part.chars();
            characters.next().map_or_else(String::new, |first| {
                first
                    .to_uppercase()
                    .chain(characters.flat_map(char::to_lowercase))
                    .collect()
            })
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Refuses a projection that carries any sensitive field name, at any depth.
fn assert_public(value: &Value) -> Result<(), FlowError> {
    match value {
        Value::Object(members) => members.iter().try_for_each(|(name, nested)| {
            if FORBIDDEN_PUBLIC_FIELDS.contains(&name.to_lowercase().as_str()) {
                return Err(FlowError::Invalid);
            }
            assert_public(nested)
        }),
        Value::Array(items) => items.iter().try_for_each(assert_public),
        _ => Ok(()),
    }
}

/// The whole seconds left before `expires_at`, rounded up, from 1 to 900.
fn expires_in(expires_at: Instant) -> Result<i64, FlowError> {
    let remaining = expires_at.saturating_duration_since(Instant::now());
    let seconds = remaining.as_secs() + u64::from(remaining.subsec_nanos() > 0);
    if !(1..=900).contains(&seconds) {
        return Err(FlowError::Changed);
    }
    i64::try_from(seconds).map_err(|_| FlowError::Changed)
}

/// The public projection of one paused turn: its requirements, without Action input, OAuth state, or tokens. Each
/// requirement is checked against the Assistant's current binding.
///
/// # Errors
/// [`FlowError::Changed`] when the challenge expired or its Assistant, declaration, or Actions changed;
/// [`FlowError::Unavailable`] when its Assistant is no longer installed.
pub fn challenge_payload<P>(
    pending: &Pending<Vec<IntegrationRequirement>, P>,
    bindings: &[AssistantView],
) -> Result<Value, FlowError> {
    if !(1..=MAX_INTEGRATION_REQUIREMENTS).contains(&pending.metadata.len()) {
        return Err(FlowError::Invalid);
    }
    let requirements = pending
        .metadata
        .iter()
        .map(|requirement| requirement_payload(requirement, bindings))
        .collect::<Result<Vec<_>, _>>()?;
    let payload = object(vec![
        ("team_id", text(&pending.team_id)),
        ("status", text("integrations-required")),
        ("turn_id", text(&pending.id)),
        ("challenge_id", text(&pending.id)),
        (
            "expires_in",
            Value::Number(Number::Int(expires_in(pending.expires_at)?)),
        ),
        ("requirements", Value::Array(requirements)),
    ]);
    assert_public(&payload)?;
    Ok(payload)
}

fn requirement_payload(
    requirement: &IntegrationRequirement,
    bindings: &[AssistantView],
) -> Result<Value, FlowError> {
    let [
        RequiredIntegration {
            id,
            provider,
            scopes,
        },
    ] = requirement.integrations.as_slice()
    else {
        return Err(FlowError::Invalid);
    };
    let assistant = binding(bindings, &requirement.assistant_id)?;
    if assistant.name != requirement.assistant_name {
        return Err(FlowError::Changed);
    }
    let (declared, declared_scopes) = assistant.intent(id).map_err(|_| FlowError::Changed)?;
    if declared != provider || declared_scopes != *scopes {
        return Err(FlowError::Changed);
    }
    let mut actions = Vec::new();
    for action_id in &requirement.action_ids {
        let action = assistant
            .action(action_id)
            .map_err(|_| FlowError::Changed)?;
        if !action.integrations.contains(id) {
            return Err(FlowError::Changed);
        }
        actions.push(object(vec![
            ("id", text(&action.id)),
            ("name", text(&action_name(&action.id))),
            ("summary", text(&action.summary)),
        ]));
    }
    if actions.is_empty() || !unique(&requirement.action_ids) {
        return Err(FlowError::Invalid);
    }
    let reviewed = providers::resolve(provider).map_err(|_| FlowError::Invalid)?;
    Ok(object(vec![
        ("assistant_id", text(&assistant.assistant_id)),
        ("assistant_name", text(&assistant.name)),
        ("integration_id", text(id)),
        ("provider", text(provider)),
        ("name", text(reviewed.name)),
        ("summary", text(reviewed.summary)),
        ("scopes", strings(scopes)),
        ("actions", Value::Array(actions)),
    ]))
}

fn identity_payload(identity: Option<&Identity>) -> Value {
    identity.map_or(Value::Null, |identity| {
        let optional = |field: &Option<String>| field.clone().map_or(Value::Null, Value::String);
        object(vec![
            ("id", text(&identity.id)),
            ("name", optional(&identity.name)),
            ("username", optional(&identity.username)),
        ])
    })
}

fn expiry_payload(expires_at: Option<i64>) -> Result<Value, FlowError> {
    match expires_at {
        None => Ok(Value::Null),
        // An expiry past year 9999 has no wire text.
        Some(epoch) if (1..=253_402_300_799).contains(&epoch) => {
            Ok(text(&team_storage::private::instant_text(epoch)))
        }
        Some(_) => Err(FlowError::Invalid),
    }
}

/// The Admin inventory: one status-only row per declared Integration of every Assistant, sorted by Assistant and
/// Integration, without tokens or generations.
///
/// # Errors
/// [`FlowError`] for an oversized or duplicated inventory, an invalid binding, or an unreadable store.
pub fn inventory_payload(
    team: &str,
    assistants: &[AssistantView],
    store: &IntegrationStore,
) -> Result<Value, FlowError> {
    if !team_id(team) {
        return Err(FlowError::Invalid);
    }
    if assistants.len() > MAX_INVENTORY_ASSISTANTS {
        return Err(FlowError::TooMany);
    }
    let mut sorted: Vec<&AssistantView> = assistants.iter().collect();
    sorted.sort_by(|left, right| left.assistant_id.cmp(&right.assistant_id));
    if sorted
        .windows(2)
        .any(|pair| pair[0].assistant_id == pair[1].assistant_id)
    {
        return Err(FlowError::Invalid);
    }
    let mut listing = Vec::new();
    for assistant in sorted {
        listing.extend(inventory_rows(team, assistant.admitted()?, store)?);
        if listing.len() > MAX_INVENTORY_INTEGRATIONS {
            return Err(FlowError::TooMany);
        }
    }
    let payload = object(vec![("integrations", Value::Array(listing))]);
    assert_public(&payload)?;
    Ok(payload)
}

fn inventory_rows(
    team: &str,
    assistant: &AssistantView,
    store: &IntegrationStore,
) -> Result<Vec<Value>, FlowError> {
    if !ASSISTANT_VERSION_RE.is_full(&assistant.version)
        || !public_bytes(&assistant.summary, MAX_PUBLIC_TEXT_BYTES)
    {
        return Err(FlowError::Invalid);
    }
    let mut declared = assistant.integrations.clone();
    declared.sort_by(|left, right| left.0.cmp(&right.0));
    let rows = metadata_for(team, assistant, &declared, store)?;
    declared
        .iter()
        .map(|(id, _)| {
            let row = rows.get(id).ok_or(FlowError::Unresolved)?;
            let (provider, scopes) = assistant.intent(id)?;
            let reviewed = providers::resolve(provider).map_err(|_| FlowError::Invalid)?;
            let status = if row.status == Status::RefreshRequired {
                "expired"
            } else {
                row.status.text()
            };
            Ok(object(vec![
                ("assistant_id", text(&assistant.assistant_id)),
                ("assistant_name", text(&assistant.name)),
                ("assistant_version", text(&assistant.version)),
                ("assistant_summary", text(&assistant.summary)),
                ("id", text(id)),
                ("provider", text(provider)),
                ("name", text(reviewed.name)),
                ("summary", text(reviewed.summary)),
                ("scopes", strings(&scopes)),
                ("status", text(status)),
                ("integration", identity_payload(row.integration.as_ref())),
                ("expires_at", expiry_payload(row.expires_at)?),
            ]))
        })
        .collect()
}

/// The access tokens of exactly the Integrations one Action declares, each refreshed through the broker when it
/// expires within the refresh window.
///
/// # Errors
/// [`FlowError::Invalid`] or [`FlowError::Undeclared`] for a broken contract; [`FlowError::Unresolved`] when any token
/// is missing, needs a person, cannot be refreshed, or is not a bounded bearer.
pub async fn resolve_action_integrations(
    team: &str,
    assistant: &AssistantView,
    action: &str,
    store: &Arc<IntegrationStore>,
    service: &Arc<OAuthService>,
) -> Result<Vec<(String, Secret)>, FlowError> {
    if !team_id(team) {
        return Err(FlowError::Invalid);
    }
    let action = assistant.admitted()?.action(action)?;
    let mut resolved = Vec::new();
    for id in &action.integrations {
        if !identifier(id) {
            return Err(FlowError::Invalid);
        }
        let (provider, scopes) = assistant.intent(id)?;
        let service = Arc::clone(service);
        let granted = scopes.clone();
        let refresh = move |token: Secret,
                            lease: Option<Secret>|
              -> team_core::future::BoxFuture<
            'static,
            Result<crate::token::TokenSet, StoreError>,
        > {
            Box::pin(async move {
                service
                    .refresh(provider, &granted, &token, lease.as_ref())
                    .await
                    .map_err(|_| StoreError::Exchange)
            })
        };
        let token = store
            .resolve(
                (team, &assistant.assistant_id, id),
                (provider, &scopes),
                refresh,
            )
            .await
            .map_err(|_| FlowError::Unresolved)?;
        if !is_bearer(token.expose()) {
            return Err(FlowError::Unresolved);
        }
        resolved.push((id.clone(), token));
    }
    Ok(resolved)
}

#[cfg(test)]
mod tests;
