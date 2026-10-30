//! The semantic rules of a schema-valid resolve response (`../v1/verify.py` `validate_resolve`): its image, catalog,
//! Integrations, file inputs, and Stored Inputs bound to one another. Each check is public so a consumer that keeps
//! stricter rules of its own for one step (Team's file-input and placement admission) composes the others unchanged.

use std::fmt::Write as _;

use sha2::{Digest, Sha256};
use strict_json::Value;

use crate::Violation;
use crate::json::{Members, array, canonical, get, object, string, text_eq};

/// The repository every resolved executable image lives in.
pub const IMAGE_REPOSITORY: &str = "ghcr.io/theshimpz/shimpz-assistant";
/// The requests that authorize an Action.
pub const AUTHORIZATION_REQUESTS: [&str; 4] =
    ["approval", "auth:password", "auth:totp", "auth:passkey"];
/// The catalog bound of the summary.
pub const SUMMARY_BOUND: i64 = 80;
/// The catalog bound of a description or Stored Input help text.
pub const DESCRIPTION_BOUND: i64 = 500;
/// The catalog bound of an Action description or Stored Input label.
pub const LINE_BOUND: i64 = 120;
/// The headers Team owns in every provider call, compared without case.
pub const RESERVED_HEADERS: [&str; 12] = [
    "accept-encoding",
    "connection",
    "content-length",
    "expect",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];
/// The stems that make a literal route segment name a credential endpoint.
pub const CREDENTIAL_STEMS: [&str; 7] = [
    "apikey",
    "authoriz",
    "credential",
    "oauth",
    "password",
    "secret",
    "token",
];

/// The exact schema of a declared file input: one opaque Team file id.
#[must_use]
pub fn file_id_schema() -> String {
    r#"{"maxLength":32,"minLength":32,"pattern":"^[0-9a-f]{32}$","type":"string"}"#.to_owned()
}

/// Every neutral rule, in the reference's order.
///
/// # Errors
/// The first [`Violation`] of a schema-valid resolve response.
pub fn validate(value: &Value) -> Result<(), Violation> {
    image_bound(value)?;
    catalog_bound(value)?;
    let (Some(intents), Some(contract)) = (
        get(value, "integrations").and_then(array),
        get(value, "machine_contract").and_then(object),
    ) else {
        return Ok(());
    };
    let Some(actions) = member(contract, "actions").and_then(array) else {
        return Ok(());
    };
    if !actions.iter().filter_map(object).all(input_files_admitted) {
        return Err(Violation::InputFileMismatch);
    }
    integrations_bound(intents, actions)?;
    let Some(stored_inputs) = get(value, "stored_inputs").and_then(array) else {
        return Ok(());
    };
    stored_inputs_bound(stored_inputs, actions)?;
    if !placements_admitted(stored_inputs, get(value, "allowed_hosts"))
        || !proofs_signed(actions, stored_inputs)
    {
        return Err(Violation::StoredInputPlacement);
    }
    Ok(())
}

fn member<'a>(members: &'a Members, key: &str) -> Option<&'a Value> {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

/// The image reference is the executable repository at exactly the resolved OCI digest.
///
/// # Errors
/// [`Violation::DigestMismatch`].
pub fn image_bound(value: &Value) -> Result<(), Violation> {
    let digest = get(value, "oci_digest").map_or_else(|| "None".to_owned(), python_str);
    let expected = format!("{IMAGE_REPOSITORY}@{digest}");
    if text_eq(get(value, "image_reference"), &expected) {
        Ok(())
    } else {
        Err(Violation::DigestMismatch)
    }
}

/// Python `str(value)` of a schema-valid scalar.
fn python_str(value: &Value) -> String {
    string(value).map_or_else(|| canonical(value), str::to_owned)
}

/// Each catalog id is the SHA-256 of its template, sorted and unique, and the summary and every displayed static text
/// are parameterless messages within their bounds.
///
/// # Errors
/// [`Violation::CatalogMismatch`], [`Violation::SummaryMismatch`], or [`Violation::DisplayMismatch`].
pub fn catalog_bound(value: &Value) -> Result<(), Violation> {
    let Some(messages) = get(value, "machine_contract")
        .and_then(|contract| contract.get("messages"))
        .and_then(array)
    else {
        return Ok(());
    };
    let ids: Vec<&str> = messages
        .iter()
        .filter_map(|message| message.get("id").and_then(string))
        .collect();
    let bound = ids.len() == messages.len()
        && ids.windows(2).all(|pair| pair[0] < pair[1])
        && messages.iter().all(|message| {
            let template = message.get("msgid").and_then(string).unwrap_or_default();
            text_eq(
                message.get("id"),
                &hex(&Sha256::digest(template.as_bytes())),
            )
        });
    if !bound {
        return Err(Violation::CatalogMismatch);
    }
    if !cataloged(messages, get(value, "summary"), SUMMARY_BOUND) {
        return Err(Violation::SummaryMismatch);
    }
    if !display_uses(value)
        .into_iter()
        .all(|(text, bound)| cataloged(messages, text, bound))
    {
        return Err(Violation::DisplayMismatch);
    }
    Ok(())
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

/// The description and each Stored Input help text, then each Action description and Stored Input label, with their
/// bounds.
fn display_uses(value: &Value) -> Vec<(Option<&Value>, i64)> {
    let actions = get(value, "machine_contract")
        .and_then(|contract| contract.get("actions"))
        .and_then(array)
        .unwrap_or_default();
    let stored_inputs = get(value, "stored_inputs")
        .and_then(array)
        .unwrap_or_default();
    let mut uses = vec![(get(value, "description"), DESCRIPTION_BOUND)];
    uses.extend(
        stored_inputs
            .iter()
            .map(|item| (item.get("description"), DESCRIPTION_BOUND)),
    );
    uses.extend(
        actions
            .iter()
            .map(|action| (action.get("description"), LINE_BOUND)),
    );
    uses.extend(
        stored_inputs
            .iter()
            .map(|item| (item.get("label"), LINE_BOUND)),
    );
    uses
}

/// Whether one parameterless message has exactly this template within this bound.
fn cataloged(messages: &[Value], text: Option<&Value>, bound: i64) -> bool {
    let Some(text) = text.and_then(string) else {
        return false;
    };
    messages.iter().any(|message| {
        text_eq(message.get("msgid"), text)
            && message.get("params").and_then(array).is_some_and(<[Value]>::is_empty)
            && matches!(message.get("max_length"), Some(Value::Number(strict_json::Number::Int(length))) if *length <= bound)
    })
}

fn authorizes(request: &Value) -> bool {
    string(request).is_some_and(|request| AUTHORIZATION_REQUESTS.contains(&request))
}

/// Each declared file input is a required direct property with the exact file-id schema behind exactly one
/// authorization.
#[must_use]
pub fn input_files_admitted(action: &Members) -> bool {
    let declared = member(action, "input_files");
    if declared.is_none_or(|declared| !truthy(declared)) {
        return true;
    }
    let schema = member(action, "input_schema").and_then(object);
    let properties = schema
        .and_then(|schema| member(schema, "properties"))
        .and_then(object);
    let required = schema.map(|schema| member(schema, "required").map_or(Some(&[][..]), array));
    let (Some(declared), Some(properties), Some(Some(required)), Some(requests)) = (
        declared.and_then(array),
        properties,
        required,
        member(action, "human_requests").and_then(array),
    ) else {
        return false;
    };
    let exact = file_id_schema();
    declared.iter().all(|name| {
        required.contains(name)
            && string(name)
                .and_then(|name| member(properties, name))
                .is_some_and(|schema| canonical(schema) == exact)
    }) && requests
        .iter()
        .filter(|request| authorizes(request))
        .count()
        == 1
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(members) => !members.is_empty(),
        _ => true,
    }
}

/// The string `id`s of the objects of a list.
fn ids(items: &[Value]) -> Vec<&str> {
    items
        .iter()
        .filter_map(|item| item.get("id").and_then(string))
        .collect()
}

/// Every id the Actions name in one requirement field.
fn required_ids<'a>(actions: &'a [Value], field: &str) -> Vec<&'a str> {
    let mut required: Vec<&str> = actions
        .iter()
        .filter_map(|action| action.get(field).and_then(array))
        .flatten()
        .filter_map(string)
        .collect();
    required.sort_unstable();
    required.dedup();
    required
}

fn unique(texts: &[&str]) -> bool {
    texts
        .iter()
        .enumerate()
        .all(|(index, text)| !texts[..index].contains(text))
}

/// The Integration intents are exactly the ones the Actions use, once each.
///
/// # Errors
/// [`Violation::IntegrationMismatch`].
pub fn integrations_bound(intents: &[Value], actions: &[Value]) -> Result<(), Violation> {
    let intent_ids = ids(intents);
    let required = required_ids(actions, "integrations");
    let mut sorted = intent_ids.clone();
    sorted.sort_unstable();
    if intent_ids.len() != intents.len() || !unique(&intent_ids) || sorted != required {
        return Err(Violation::IntegrationMismatch);
    }
    Ok(())
}

/// The Stored Inputs are unique, hold every one an Action uses, and only an Action asking for a password uses one.
///
/// # Errors
/// [`Violation::StoredInputMismatch`].
pub fn stored_inputs_bound(stored_inputs: &[Value], actions: &[Value]) -> Result<(), Violation> {
    let declared = ids(stored_inputs);
    let required = required_ids(actions, "stored_inputs");
    let password = Value::String("input:password".to_owned());
    let undeclared_request = actions.iter().any(|action| {
        action.get("stored_inputs").is_some_and(truthy)
            && !action
                .get("human_requests")
                .and_then(array)
                .is_some_and(|requests| requests.contains(&password))
    });
    if declared.len() != stored_inputs.len()
        || !unique(&declared)
        || !required.iter().all(|id| declared.contains(id))
        || undeclared_request
    {
        return Err(Violation::StoredInputMismatch);
    }
    Ok(())
}

/// Whether one path segment names an endpoint that may issue, list, or exchange credentials.
#[must_use]
pub fn credential_segment(segment: &str) -> bool {
    let folded: String = segment
        .chars()
        .flat_map(char::to_lowercase)
        .filter(|character| !matches!(character, '-' | '_' | '.' | '~'))
        .collect();
    CREDENTIAL_STEMS.iter().any(|stem| folded.contains(stem))
}

/// Schema-valid routes are unique by method and path, name no credential endpoint, and repeat no selector name
/// regardless of case.
#[must_use]
pub fn routes_admitted(routes: &[Value]) -> bool {
    let keys: Vec<String> = routes
        .iter()
        .map(|route| {
            canonical(&Value::Array(vec![
                field(route, "method"),
                field(route, "path"),
            ]))
        })
        .collect();
    let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
    let credential = routes.iter().any(|route| {
        let path = python_str(&field(route, "path"));
        path.split('/')
            .skip(1)
            .any(|segment| segment != "*" && credential_segment(segment))
    });
    let selectors_unique = routes.iter().all(|route| {
        let names: Vec<String> = route
            .get("query")
            .and_then(array)
            .unwrap_or_default()
            .iter()
            .map(|selector| python_str(&field(selector, "name")).to_lowercase())
            .collect();
        unique(&names.iter().map(String::as_str).collect::<Vec<_>>())
    });
    unique(&keys) && !credential && selectors_unique
}

fn field(value: &Value, key: &str) -> Value {
    value.get(key).cloned().unwrap_or(Value::Null)
}

/// Each placement goes to one declared host and a field nothing else uses there, is sent only on admitted routes,
/// and a proof signs exactly one plain Stored Input of its own host (ADR-0106).
#[must_use]
pub fn placements_admitted(stored_inputs: &[Value], allowed_hosts: Option<&Value>) -> bool {
    let declared: Vec<(&str, &Members)> = stored_inputs
        .iter()
        .filter_map(object)
        .filter_map(|item| Some((member(item, "id").and_then(string)?, item)))
        .collect();
    let lookup = |id: &str| {
        declared
            .iter()
            .rev()
            .find(|(candidate, _)| *candidate == id)
            .map(|(_, item)| *item)
    };
    let hosts = allowed_hosts.and_then(array).unwrap_or_default();
    let mut fields: Vec<(String, String)> = Vec::new();
    for (identifier, item) in &declared {
        let host = member(item, "host").cloned().unwrap_or(Value::Null);
        let placement = match member(item, "header").and_then(string) {
            Some(header) => format!("header:{}", header.to_lowercase()),
            None => format!(
                "query:{}",
                member(item, "query").map_or_else(|| "None".to_owned(), python_str)
            ),
        };
        let proof = member(item, "hmac");
        let target = proof.and_then(string).and_then(lookup);
        let key = (canonical(&host), placement.clone());
        let refused = !hosts.contains(&host)
            || !member(item, "routes")
                .and_then(array)
                .is_some_and(routes_admitted)
            || RESERVED_HEADERS.contains(&placement.strip_prefix("header:").unwrap_or(&placement))
            || fields.contains(&key)
            || proof.is_some_and(|proof| {
                target.is_none_or(|target| member(target, "hmac").is_some())
                    || text_eq(Some(proof), identifier)
            })
            || target.is_some_and(|target| member(target, "host") != Some(&host));
        if refused {
            return false;
        }
        fields.push(key);
    }
    true
}

/// An Action that uses a proof also uses the Stored Input it signs, which Team places beside it.
#[must_use]
pub fn proofs_signed(actions: &[Value], stored_inputs: &[Value]) -> bool {
    let signed: Vec<(&str, &Value)> = stored_inputs
        .iter()
        .filter_map(|item| Some((item.get("id").and_then(string)?, item.get("hmac")?)))
        .collect();
    actions
        .iter()
        .filter_map(|action| action.get("stored_inputs").and_then(array))
        .all(|slots| {
            slots.iter().filter_map(string).all(|slot| {
                signed
                    .iter()
                    .rev()
                    .find(|(id, _)| *id == slot)
                    .is_none_or(|(_, signs)| slots.contains(signs))
            })
        })
}
