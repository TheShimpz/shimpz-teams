//! The SDK-generated machine contract: the Actions and the English message catalog (ADR-0091, ADR-0092, ADR-0093).

use assistant_protocol::catalog::{LINE_CHARS, catalog_error, display_error, display_uses};
use assistant_protocol::input_file::declaration_error;
use strict_json::{Number, Value};
use team_protocol::payload::canonical_identifier;
use team_protocol::python::value::py_eq;

use super::{MAX_CONTRACT_BYTES, ManifestContract, StoredInputDeclaration};
use crate::ManifestError;
use crate::effect::refusal;
use crate::text::display_text;

/// Each contract holds at most this many JSON values; each Action schema has its own bound besides.
pub const MAX_CONTRACT_NODES: usize = 32_768;
/// The most Actions of one contract.
pub const MAX_ACTIONS: usize = 128;
/// The most Integrations one Action uses.
pub const MAX_ACTION_INTEGRATIONS: usize = 4;
/// The most human requests one Action declares.
pub const MAX_HUMAN_REQUESTS: usize = 8;
/// Every human request kind.
pub const HUMAN_REQUEST_KINDS: [&str; 11] = [
    "approval",
    "auth:passkey",
    "auth:password",
    "auth:totp",
    "input:choice",
    "input:choices",
    "input:password",
    "input:phone",
    "input:select",
    "input:text",
    "input:textarea",
];
/// The kinds that authorize an Action.
pub const AUTHORIZATION_REQUEST_KINDS: [&str; 4] =
    ["approval", "auth:passkey", "auth:password", "auth:totp"];
const ACTION_FIELDS: [&str; 8] = [
    "id",
    "description",
    "input_schema",
    "output_schema",
    "integrations",
    "stored_inputs",
    "input_files",
    "human_requests",
];
const ACTION_OPTIONAL: [&str; 2] = ["verifier", "idempotency"];

/// Parses a bounded SDK artifact without executing Assistant code.
///
/// # Errors
/// [`ManifestError`] for a size outside 1 to [`MAX_CONTRACT_BYTES`], invalid strict JSON, or a refused contract.
pub fn parse_machine_contract(
    raw: &[u8],
    contract: &ManifestContract,
    summary: &str,
    description: &str,
) -> Result<Value, ManifestError> {
    if !(1..=MAX_CONTRACT_BYTES).contains(&raw.len()) {
        return Err(ManifestError::Invalid("machine contract size"));
    }
    let value =
        strict_json::parse(raw).map_err(|_| ManifestError::Invalid("machine contract JSON"))?;
    canonical_machine_contract(&value, contract, summary, description)
}

/// Validates and canonicalizes an untrusted SDK-generated Action contract and its English message catalog.
///
/// The published summary must be one catalog message, and so must every other displayed static text: the Assistant
/// `description`, each Action description, and each Stored Input label and help text. The Actions may use only the
/// manifest `contract`'s Integrations and Stored Inputs, and must use every declared Integration.
///
/// # Errors
/// [`ManifestError`] for any refused part.
pub fn canonical_machine_contract(
    value: &Value,
    contract: &ManifestContract,
    summary: &str,
    description: &str,
) -> Result<Value, ManifestError> {
    check_shape(value)?;
    let messages = value.get("messages").unwrap_or(&Value::Null);
    if catalog_error(messages, &Value::String(summary.to_owned())).is_some() {
        return Err(ManifestError::Invalid("machine contract message catalog"));
    }
    let Some(Value::Array(raw_actions)) = value.get("actions").filter(|actions| {
        matches!(actions, Value::Array(items) if (1..=MAX_ACTIONS).contains(&items.len()))
    }) else {
        return Err(ManifestError::Invalid("machine contract Actions"));
    };
    let mut actions = raw_actions
        .iter()
        .map(|raw| canonical_action(raw, contract))
        .collect::<Result<Vec<_>, _>>()?;
    check_actions(&actions, contract)?;
    check_display(messages, &actions, contract, description)?;
    actions.sort_by(|left, right| action_id(left).cmp(action_id(right)));
    let canonical_messages =
        strict_json::parse(&assistant_protocol::catalog::canonical_json(messages))
            .map_err(|_| ManifestError::Invalid("machine contract message catalog"))?;
    Ok(Value::Object(vec![
        ("version".to_owned(), Value::Number(Number::Int(1))),
        ("actions".to_owned(), Value::Array(actions)),
        ("messages".to_owned(), canonical_messages),
    ]))
}

/// The closed `{version: 1, actions, messages}` object within [`MAX_CONTRACT_NODES`] values.
fn check_shape(value: &Value) -> Result<(), ManifestError> {
    let shaped = matches!(value, Value::Object(members) if members.len() == 3
        && ["version", "actions", "messages"].iter().all(|key| value.get(key).is_some()))
        && value
            .get("version")
            .is_some_and(|version| py_eq(version, &Value::Number(Number::Int(1))));
    if !shaped {
        return Err(ManifestError::Invalid("machine contract shape"));
    }
    if !action_schema::nodes_within(value, MAX_CONTRACT_NODES) {
        return Err(ManifestError::Invalid("machine contract size"));
    }
    Ok(())
}

/// Every displayed static text is one parameterless catalog message within its bound.
fn check_display(
    messages: &Value,
    actions: &[Value],
    contract: &ManifestContract,
    description: &str,
) -> Result<(), ManifestError> {
    let uses = display_uses(
        Some(description),
        actions
            .iter()
            .map(|action| action.get("description").and_then(text)),
        contract
            .stored_inputs
            .iter()
            .map(|item| Some(item.label.as_str())),
        contract
            .stored_inputs
            .iter()
            .map(|item| Some(item.description.as_str())),
    );
    if display_error(messages, uses).is_some() {
        return Err(ManifestError::Invalid("machine contract displayed copy"));
    }
    Ok(())
}

fn text(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

fn action_id(action: &Value) -> &str {
    action.get("id").and_then(text).unwrap_or_default()
}

fn strings(value: &Value, key: &str) -> Vec<String> {
    match value.get(key) {
        Some(Value::Array(items)) => items.iter().filter_map(text).map(str::to_owned).collect(),
        _ => Vec::new(),
    }
}

/// The contract-wide rules: unique Action ids, every declared Integration used, proofs beside what they sign, and
/// the effect declarations.
fn check_actions(actions: &[Value], contract: &ManifestContract) -> Result<(), ManifestError> {
    let ids: Vec<&str> = actions.iter().map(action_id).collect();
    if ids
        .iter()
        .enumerate()
        .any(|(index, id)| ids[..index].contains(id))
    {
        return Err(ManifestError::Invalid(
            "machine contract Action id is duplicated",
        ));
    }
    let mut used: Vec<String> = actions
        .iter()
        .flat_map(|action| strings(action, "integrations"))
        .collect();
    used.sort();
    used.dedup();
    let mut declared: Vec<&str> = contract
        .integrations
        .iter()
        .map(|integration| integration.id.as_str())
        .collect();
    declared.sort_unstable();
    declared.dedup();
    if used != declared {
        return Err(ManifestError::Invalid(
            "machine contract must use every declared integration",
        ));
    }
    if !proofs_signed(actions, &contract.stored_inputs) {
        return Err(ManifestError::Invalid(
            "machine contract proof without the Stored Input it signs",
        ));
    }
    if refusal(actions, &contract.allowed_hosts).is_some() {
        return Err(ManifestError::Invalid("machine contract Action effect"));
    }
    Ok(())
}

/// Whether every Action declaring a proof also declares the Stored Input it signs, which Team places beside it.
#[must_use]
pub fn proofs_signed(actions: &[Value], declarations: &[StoredInputDeclaration]) -> bool {
    actions.iter().all(|action| {
        let slots = strings(action, "stored_inputs");
        slots.iter().all(|slot| {
            declarations
                .iter()
                .rev()
                .find(|declaration| &declaration.id == slot)
                .and_then(|declaration| declaration.hmac.as_ref())
                .is_none_or(|signed| slots.contains(signed))
        })
    })
}

fn listed<'a>(value: &'a Value, key: &str, maximum: usize) -> Option<Vec<&'a str>> {
    let Some(Value::Array(items)) = value.get(key) else {
        return None;
    };
    let texts: Vec<&str> = items.iter().filter_map(text).collect();
    (items.len() <= maximum && texts.len() == items.len()).then_some(texts)
}

fn unique(texts: &[&str]) -> bool {
    texts
        .iter()
        .enumerate()
        .all(|(index, text)| !texts[..index].contains(text))
}

/// One Action's closed members, its declared capabilities, and its admitted schemas.
fn canonical_action(raw: &Value, contract: &ManifestContract) -> Result<Value, ManifestError> {
    let Value::Object(members) = raw else {
        return Err(ManifestError::Invalid("machine contract Action"));
    };
    let has = |key: &str| members.iter().any(|(name, _)| name == key);
    let closed = ACTION_FIELDS.iter().chain(&["effect"]).all(|key| has(key))
        && members.iter().all(|(name, _)| {
            ACTION_FIELDS.contains(&name.as_str())
                || name == "effect"
                || ACTION_OPTIONAL.contains(&name.as_str())
        });
    if !closed {
        return Err(ManifestError::Invalid("machine contract Action"));
    }
    let get = |key: &str| raw.get(key).unwrap_or(&Value::Null);
    let id = canonical_identifier(get("id")).ok_or(ManifestError::Invalid("Action identifier"))?;
    let (integrations, stored_inputs, human_requests) = capabilities(raw, members, contract)?;
    let description = display_text(get("description"), "Action description", LINE_CHARS)?;
    for position in ["input_schema", "output_schema"] {
        action_schema::admit_schema(get(position))
            .map_err(|_| ManifestError::Invalid("Action schema"))?;
    }
    let sorted = |mut items: Vec<&str>| {
        items.sort_unstable();
        Value::Array(
            items
                .into_iter()
                .map(|item| Value::String(item.to_owned()))
                .collect(),
        )
    };
    let mut canonical = vec![
        ("id".to_owned(), Value::String(id.to_owned())),
        (
            "description".to_owned(),
            Value::String(description.to_owned()),
        ),
        ("input_schema".to_owned(), get("input_schema").clone()),
        ("output_schema".to_owned(), get("output_schema").clone()),
        ("integrations".to_owned(), sorted(integrations)),
        ("stored_inputs".to_owned(), sorted(stored_inputs)),
        ("input_files".to_owned(), get("input_files").clone()),
        ("human_requests".to_owned(), sorted(human_requests)),
    ];
    let mut extra: Vec<&(String, Value)> = members
        .iter()
        .filter(|(name, _)| !ACTION_FIELDS.contains(&name.as_str()))
        .collect();
    extra.sort_by(|left, right| left.0.cmp(&right.0));
    canonical.extend(extra.into_iter().cloned());
    Ok(Value::Object(canonical))
}

/// The Action's Integrations, Stored Inputs, and human requests; a Stored Input needs the password request, and a
/// file input is one required direct file-id property behind exactly one authorization (ADR-0093).
type Capabilities<'a> = (Vec<&'a str>, Vec<&'a str>, Vec<&'a str>);

fn capabilities<'a>(
    raw: &'a Value,
    members: &[(String, Value)],
    contract: &ManifestContract,
) -> Result<Capabilities<'a>, ManifestError> {
    let integrations = action_integrations(raw, contract)?;
    let stored_inputs = action_stored_inputs(raw, contract)?;
    let human_requests = action_human_requests(raw)?;
    if !stored_inputs.is_empty() && !human_requests.contains(&"input:password") {
        return Err(ManifestError::Invalid(
            "machine contract Action Stored Input request is undeclared",
        ));
    }
    if declaration_error(members).is_some() {
        return Err(ManifestError::Invalid("machine contract Action file input"));
    }
    Ok((integrations, stored_inputs, human_requests))
}

fn action_integrations<'a>(
    raw: &'a Value,
    contract: &ManifestContract,
) -> Result<Vec<&'a str>, ManifestError> {
    let declared = |id: &&str| {
        contract
            .integrations
            .iter()
            .any(|integration| integration.id == *id)
    };
    listed(raw, "integrations", MAX_ACTION_INTEGRATIONS)
        .filter(|ids| ids.iter().all(declared) && unique(ids))
        .ok_or(ManifestError::Invalid(
            "machine contract Action integrations",
        ))
}

/// An Action may use any of its manifest's Stored Inputs, as one sorted, unique list (ADR-0059).
fn action_stored_inputs<'a>(
    raw: &'a Value,
    contract: &ManifestContract,
) -> Result<Vec<&'a str>, ManifestError> {
    let declared = |id: &&str| contract.stored_inputs.iter().any(|item| item.id == *id);
    listed(raw, "stored_inputs", super::MAX_STORED_INPUTS)
        .filter(|ids| ids.iter().all(declared) && ids.windows(2).all(|pair| pair[0] < pair[1]))
        .ok_or(ManifestError::Invalid(
            "machine contract Action Stored Inputs",
        ))
}

fn action_human_requests(raw: &Value) -> Result<Vec<&str>, ManifestError> {
    listed(raw, "human_requests", MAX_HUMAN_REQUESTS)
        .filter(|kinds| {
            kinds.iter().all(|kind| HUMAN_REQUEST_KINDS.contains(kind))
                && unique(kinds)
                && kinds
                    .iter()
                    .filter(|kind| AUTHORIZATION_REQUEST_KINDS.contains(kind))
                    .count()
                    <= 1
        })
        .ok_or(ManifestError::Invalid(
            "machine contract Action human requests",
        ))
}
