//! The closed Local record of an admitted snapshot, and its validation after every durable read.

use strict_json::{Number, Value};
use team_assistant::language::admit_pack;
use team_assistant::manifest::{
    ManifestContract, StoredInputDeclaration, canonical_machine_contract,
    canonical_manifest_contract, canonical_manifest_creators, canonical_manifest_identity,
    canonical_manifest_presentation, parse_machine_contract, parse_manifest_contract,
    parse_manifest_creators, parse_manifest_identity, parse_manifest_presentation,
    stored_input_declarations_from_documents,
};
use team_protocol::payload::SOURCE_DIGEST_RE;
use team_protocol::python::value::py_eq;

use super::{
    Candidate, PLATFORMS, RUNTIME_ENTRYPOINT, RUNTIME_USER, SnapshotError, inconsistent, invalid,
    text_value,
};
use crate::install::source_package::SourcePackage;

const RECORD_FIELDS: [&str; 20] = [
    "version",
    "assistant_id",
    "assistant_version",
    "name",
    "summary",
    "description",
    "links",
    "declared_creators",
    "image_id",
    "platform",
    "source_digest",
    "manifest_digest",
    "machine_contract_digest",
    "icon_digest",
    "pack_digest",
    "runtime",
    "allowed_hosts",
    "integrations",
    "stored_inputs",
    "machine_contract",
];
const DIGESTS: [&str; 6] = [
    "image_id",
    "source_digest",
    "manifest_digest",
    "machine_contract_digest",
    "icon_digest",
    "pack_digest",
];

fn digest(raw: &[u8]) -> String {
    format!("sha256:{}", assistant_protocol::python::sha256_hex(raw))
}

fn strings(items: &[String]) -> Value {
    Value::Array(items.iter().map(|item| text_value(item)).collect())
}

/// The fixed runtime every staged image declares.
fn runtime() -> Value {
    Value::Object(vec![
        ("user".to_owned(), text_value(RUNTIME_USER)),
        (
            "entrypoint".to_owned(),
            Value::Array(
                RUNTIME_ENTRYPOINT
                    .iter()
                    .map(|item| text_value(item))
                    .collect(),
            ),
        ),
    ])
}

fn integrations(contract: &ManifestContract) -> Value {
    let entry = |item: &team_assistant::manifest::IntegrationDeclaration| {
        Value::Object(vec![
            ("id".to_owned(), text_value(&item.id)),
            ("provider".to_owned(), text_value(&item.provider)),
            ("scopes".to_owned(), strings(&item.scopes)),
        ])
    };
    Value::Array(contract.integrations.iter().map(entry).collect())
}

fn documents(contract: &ManifestContract) -> Value {
    Value::Array(
        contract
            .stored_inputs
            .iter()
            .map(StoredInputDeclaration::document)
            .collect(),
    )
}

fn action_ids(machine: &Value) -> Vec<String> {
    match machine.get("actions") {
        Some(Value::Array(actions)) => actions
            .iter()
            .filter_map(|action| match action.get("id") {
                Some(Value::String(id)) => Some(id.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// The package's declaration, checked against the image's labels.
struct Declaration {
    identity: team_assistant::manifest::ManifestIdentity,
    contract: ManifestContract,
    presentation: team_assistant::manifest::ManifestPresentation,
    machine: Value,
}

fn declaration(
    candidate: &Candidate,
    package: &SourcePackage,
    raw_contract: &[u8],
) -> Result<Declaration, SnapshotError> {
    let identity = parse_manifest_identity(&package.manifest).map_err(invalid)?;
    if (identity.assistant_id.as_str(), identity.version.as_str())
        != (candidate.assistant_id.as_str(), candidate.version.as_str())
    {
        return Err(inconsistent());
    }
    let contract = parse_manifest_contract(&package.manifest).map_err(invalid)?;
    let presentation = parse_manifest_presentation(&package.manifest).map_err(invalid)?;
    let creators: Vec<String> = parse_manifest_creators(&package.manifest)
        .map_err(invalid)?
        .into_iter()
        .take(4)
        .collect();
    if creators != candidate.declared_creators {
        return Err(inconsistent());
    }
    let machine = parse_machine_contract(
        raw_contract,
        &contract,
        &identity.summary,
        &presentation.description,
    )
    .map_err(invalid)?;
    let providers: Vec<String> = contract
        .integrations
        .iter()
        .map(|item| item.provider.clone())
        .collect();
    if action_ids(&machine) != candidate.actions || providers != candidate.integrations {
        return Err(inconsistent());
    }
    Ok(Declaration {
        identity,
        contract,
        presentation,
        machine,
    })
}

/// The closed record of an admitted snapshot. Local admission is unsigned self-consistency (ADR-0060): the pack must
/// be complete and valid for this exact catalog, and the record binds its digest (ADR-0091).
pub(super) fn record(
    candidate: &Candidate,
    package: &SourcePackage,
    raw_contract: &[u8],
    raw_pack: &[u8],
) -> Result<Value, SnapshotError> {
    let declared = declaration(candidate, package, raw_contract)?;
    let messages = declared.machine.get("messages").unwrap_or(&Value::Null);
    let pack = admit_pack(raw_pack, messages, &digest(raw_pack)).map_err(invalid)?;
    let Declaration {
        identity,
        contract,
        presentation,
        machine,
    } = declared;
    let entries: Vec<(&str, Value)> = vec![
        ("version", Value::Number(Number::Int(1))),
        ("assistant_id", text_value(&identity.assistant_id)),
        ("assistant_version", text_value(&identity.version)),
        ("name", text_value(&identity.name)),
        ("summary", text_value(&identity.summary)),
        ("description", text_value(&presentation.description)),
        ("links", presentation.links),
        ("declared_creators", strings(&candidate.declared_creators)),
        ("image_id", text_value(&candidate.image_id)),
        ("platform", text_value(&candidate.platform)),
        ("source_digest", text_value(&package.digest)),
        ("manifest_digest", text_value(&digest(&package.manifest))),
        ("machine_contract_digest", text_value(&digest(raw_contract))),
        ("icon_digest", text_value(&digest(&package.icon))),
        ("pack_digest", text_value(&pack.pack_digest)),
        ("runtime", runtime()),
        ("allowed_hosts", strings(&contract.allowed_hosts)),
        ("integrations", integrations(&contract)),
        ("stored_inputs", documents(&contract)),
        ("machine_contract", machine),
    ];
    Ok(Value::Object(
        entries
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    ))
}

/// Whether a durable Local binding record is exactly the closed record admission derives.
#[must_use]
pub fn record_valid(record: &Value) -> bool {
    let Value::Object(members) = record else {
        return false;
    };
    let field = |key: &str| record.get(key).unwrap_or(&Value::Null);
    let closed = members.len() == RECORD_FIELDS.len()
        && RECORD_FIELDS.iter().all(|key| record.get(key).is_some())
        && py_eq(field("version"), &Value::Number(Number::Int(1)));
    let digests = DIGESTS
        .iter()
        .all(|key| matches!(field(key), Value::String(digest) if SOURCE_DIGEST_RE.is_full(digest)));
    let platform = matches!(field("platform"), Value::String(platform) if PLATFORMS.contains(&platform.as_str()));
    closed && digests && platform && py_eq(field("runtime"), &runtime()) && contract_valid(record)
}

/// The record's Integrations as admission declarations: each exactly `{id, provider, scopes}`.
fn declared_integrations(value: &Value) -> Option<Vec<(String, Value)>> {
    let Value::Array(items) = value else {
        return None;
    };
    items
        .iter()
        .map(|item| match (item, item.get("id"), item.get("scopes")) {
            (Value::Object(members), Some(Value::String(id)), Some(scopes))
                if members.len() == 3 && item.get("provider").is_some() =>
            {
                Some((id.clone(), scopes.clone()))
            }
            _ => None,
        })
        .collect()
}

/// The record's canonical security contract, rebuilt from its own declarations.
fn record_contract(record: &Value) -> Option<ManifestContract> {
    let field = |key: &str| record.get(key).unwrap_or(&Value::Null);
    let declared = declared_integrations(field("integrations"))?;
    let Value::Array(stored) = field("stored_inputs") else {
        return None;
    };
    let contract = canonical_manifest_contract(field("allowed_hosts"), &declared, &[]).ok()?;
    let stored_inputs =
        stored_input_declarations_from_documents(stored, &contract.allowed_hosts).ok()?;
    Some(ManifestContract {
        stored_inputs,
        ..contract
    })
}

fn contract_valid(record: &Value) -> bool {
    let field = |key: &str| record.get(key).unwrap_or(&Value::Null);
    let identity = canonical_manifest_identity(
        field("assistant_id"),
        field("assistant_version"),
        field("name"),
        field("summary"),
    );
    let presentation = canonical_manifest_presentation(field("description"), field("links"));
    let creators = canonical_manifest_creators(field("declared_creators"), 4);
    let (Ok(identity), Ok(presentation), Ok(_), Some(contract)) =
        (identity, presentation, creators, record_contract(record))
    else {
        return false;
    };
    let machine = canonical_machine_contract(
        field("machine_contract"),
        &contract,
        &identity.summary,
        &presentation.description,
    );
    machine.is_ok_and(|machine| py_eq(field("machine_contract"), &machine))
        && py_eq(field("allowed_hosts"), &strings(&contract.allowed_hosts))
        && py_eq(field("integrations"), &integrations(&contract))
        && py_eq(field("stored_inputs"), &documents(&contract))
}
