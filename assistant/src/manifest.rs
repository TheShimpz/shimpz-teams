//! Immutable Assistant manifest admission for reviewed security intent (`assistant/manifest.py`).
//!
//! A manifest declares its exact outbound hosts, its Integrations (reviewed providers and scopes), and its Stored
//! Inputs with their placements (ADR-0106). Admission canonicalizes them into one [`ManifestContract`] that the
//! controller compares with its review and with every immutable package it reads.

mod declaration;
mod machine;
mod toml;

use strict_json::Value;
use team_protocol::payload::{
    GITHUB_REPOSITORY_RE, LOCAL_ASSISTANT_CREATOR_RE, canonical_assistant_id,
    canonical_creator_links,
};
use team_protocol::python::regex::Pattern;

pub use self::declaration::{
    IntegrationDeclaration, MAX_ALLOWED_HOSTS, MAX_INTEGRATIONS, MAX_STORED_INPUTS,
    RESERVED_HEADERS, StoredInputDeclaration, automatic_update_preserves_egress,
    canonical_allowed_hosts, canonical_integration_declarations, canonical_manifest_contract,
    canonical_stored_input_declarations, placement_field, reviewed_manifest_contract,
    stored_input_declarations_from_documents,
};
pub use self::machine::{
    AUTHORIZATION_REQUEST_KINDS, HUMAN_REQUEST_KINDS, MAX_CONTRACT_NODES,
    canonical_machine_contract, parse_machine_contract, proofs_signed,
};
use self::toml::manifest_table;
use crate::ManifestError;
use crate::text::{display_text, genesis, public_text};

/// The fixed path of the manifest in every Assistant image.
pub const MANIFEST_PATH: &str = "/opt/shimpz/shimpz.toml";
/// The fixed path of the SDK-generated machine contract.
pub const CONTRACT_PATH: &str = "/opt/shimpz/shimpz.contract.json";
/// The largest manifest.
pub const MAX_MANIFEST_BYTES: usize = 256 * 1024;
/// The largest machine contract.
pub const MAX_CONTRACT_BYTES: usize = 512 * 1024;
/// Team infrastructure names an Assistant id may never take.
pub const RESERVED_ASSISTANT_IDS: [&str; 3] =
    ["postgres", "assistant-egress", "shimpz-assistant-egress"];
/// The longest Assistant name or summary.
pub const MAX_NAME_CHARS: usize = 80;
/// The most Creator handles a manifest declares.
pub const MAX_CREATORS: usize = 16;

/// Canonical security intent admitted from one immutable Assistant package.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ManifestContract {
    /// The exact public outbound hosts, sorted.
    pub allowed_hosts: Vec<String>,
    /// The Integrations, sorted by id.
    pub integrations: Vec<IntegrationDeclaration>,
    /// The Stored Inputs, sorted by id.
    pub stored_inputs: Vec<StoredInputDeclaration>,
}

/// Bounded Assistant identity and display metadata from one complete manifest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ManifestIdentity {
    /// The Assistant id.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its name.
    pub name: String,
    /// Its summary.
    pub summary: String,
}

/// Creator-declared Assistant page copy: one description paragraph and unverified public links.
#[derive(Clone, Debug, PartialEq)]
pub struct ManifestPresentation {
    /// The description.
    pub description: String,
    /// Kind to URL in canonical display order; empty when the manifest declares no links.
    pub links: Value,
}

fn full(pattern: &Pattern, value: &Value) -> bool {
    pattern.is_full_value(value)
}

/// The bounded public security contract of one UTF-8 TOML manifest.
///
/// # Errors
/// [`ManifestError`] when any part of the complete Spec v1 manifest is refused.
pub fn parse_manifest_contract(raw: &[u8]) -> Result<ManifestContract, ManifestError> {
    let manifest = manifest_table(raw)?;
    let metadata = manifest.get("shimpz").unwrap_or(&Value::Null);
    let spec_one = Value::Number(strict_json::Number::Int(1));
    if !metadata
        .get("spec")
        .is_some_and(|spec| team_protocol::python::value::py_eq(spec, &spec_one))
    {
        return Err(ManifestError::Invalid("spec"));
    }
    let identity =
        canonical_assistant_id(field(metadata, "id")).ok_or(ManifestError::Invalid("id"))?;
    if RESERVED_ASSISTANT_IDS.contains(&identity) {
        return Err(ManifestError::Invalid("id"));
    }
    if !full(
        &team_protocol::payload::ASSISTANT_VERSION_RE,
        field(metadata, "version"),
    ) {
        return Err(ManifestError::Invalid("version"));
    }
    public_text(field(metadata, "name"), "name", MAX_NAME_CHARS)?;
    public_text(field(metadata, "summary"), "summary", MAX_NAME_CHARS)?;
    manifest_presentation(metadata)?;
    genesis(field(metadata, "genesis"))?;
    canonical_manifest_creators(field(metadata, "creators"), MAX_CREATORS)?;
    if !full(&GITHUB_REPOSITORY_RE, field(metadata, "github")) {
        return Err(ManifestError::Invalid("github repository"));
    }
    let integrations = manifest_integrations(manifest.get("integrations"))?;
    let stored_inputs = match manifest.get("stored_inputs") {
        None => Vec::new(),
        Some(Value::Object(members)) => members.clone(),
        Some(_) => return Err(ManifestError::Invalid("Stored Input declarations")),
    };
    canonical_manifest_contract(
        field(
            manifest.get("network").unwrap_or(&Value::Null),
            "allowed_hosts",
        ),
        &integrations,
        &stored_inputs,
    )
}

fn field<'a>(table: &'a Value, key: &str) -> &'a Value {
    table.get(key).unwrap_or(&Value::Null)
}

/// Each `[integrations.<id>]` table holds exactly `scopes`.
fn manifest_integrations(value: Option<&Value>) -> Result<Vec<(String, Value)>, ManifestError> {
    let members = match value {
        None => return Ok(Vec::new()),
        Some(Value::Object(members)) => members,
        Some(_) => return Err(ManifestError::Invalid("integration declarations")),
    };
    members
        .iter()
        .map(|(id, metadata)| match metadata {
            Value::Object(table) if table.len() == 1 && table[0].0 == "scopes" => {
                Ok((id.clone(), table[0].1.clone()))
            }
            _ => Err(ManifestError::Invalid("integration declaration")),
        })
        .collect()
}

/// Identity, only after the complete Spec v1 manifest passes admission.
///
/// # Errors
/// [`ManifestError`] when the manifest is refused.
pub fn parse_manifest_identity(raw: &[u8]) -> Result<ManifestIdentity, ManifestError> {
    parse_manifest_contract(raw)?;
    let manifest = manifest_table(raw)?;
    let metadata = field(&manifest, "shimpz");
    canonical_manifest_identity(
        field(metadata, "id"),
        field(metadata, "version"),
        field(metadata, "name"),
        field(metadata, "summary"),
    )
}

/// The Creator-declared page copy, only after complete manifest admission.
///
/// # Errors
/// [`ManifestError`] when the manifest is refused.
pub fn parse_manifest_presentation(raw: &[u8]) -> Result<ManifestPresentation, ManifestError> {
    parse_manifest_contract(raw)?;
    manifest_presentation(field(&manifest_table(raw)?, "shimpz"))
}

/// The self-declared Creator handles, only after complete manifest admission.
///
/// # Errors
/// [`ManifestError`] when the manifest is refused.
pub fn parse_manifest_creators(raw: &[u8]) -> Result<Vec<String>, ManifestError> {
    parse_manifest_contract(raw)?;
    canonical_manifest_creators(
        field(field(&manifest_table(raw)?, "shimpz"), "creators"),
        MAX_CREATORS,
    )
}

/// The canonical model guidance of one complete Spec v1 manifest, trimmed.
///
/// # Errors
/// [`ManifestError`] when the manifest table or its Genesis is refused.
pub fn parse_manifest_genesis(raw: &[u8]) -> Result<String, ManifestError> {
    let manifest = manifest_table(raw)?;
    let guidance = genesis(field(field(&manifest, "shimpz"), "genesis"))?;
    Ok(team_protocol::python::text::strip(guidance).to_owned())
}

fn manifest_presentation(metadata: &Value) -> Result<ManifestPresentation, ManifestError> {
    // A declared links table names at least one link; a record or resolution carries an absent table as empty.
    let links = match metadata.get("links") {
        Some(Value::Object(members)) if members.is_empty() => {
            return Err(ManifestError::Invalid("links"));
        }
        Some(links) => links.clone(),
        None => Value::Object(Vec::new()),
    };
    canonical_manifest_presentation(field(metadata, "description"), &links)
}

/// The description and the zero to six Creator links a manifest, record, or resolution carries.
///
/// # Errors
/// [`ManifestError`] when either is refused.
pub fn canonical_manifest_presentation(
    description: &Value,
    links: &Value,
) -> Result<ManifestPresentation, ManifestError> {
    let links = canonical_creator_links(links).ok_or(ManifestError::Invalid("links"))?;
    let description = display_text(
        description,
        "description",
        assistant_protocol::catalog::DESCRIPTION_CHARS,
    )?;
    Ok(ManifestPresentation {
        description: description.to_owned(),
        links,
    })
}

/// 1 to `maximum` unique Creator handles, without granting identity authority.
///
/// # Errors
/// [`ManifestError`] for any other value.
pub fn canonical_manifest_creators(
    value: &Value,
    maximum: usize,
) -> Result<Vec<String>, ManifestError> {
    let Value::Array(items) = value else {
        return Err(ManifestError::Invalid("creators"));
    };
    let creators: Vec<String> = items
        .iter()
        .filter(|creator| full(&LOCAL_ASSISTANT_CREATOR_RE, creator))
        .filter_map(|creator| match creator {
            Value::String(text) => Some(text.clone()),
            _ => None,
        })
        .collect();
    let unique = creators
        .iter()
        .enumerate()
        .all(|(index, creator)| !creators[..index].contains(creator));
    if creators.len() != items.len() || !(1..=maximum).contains(&items.len()) || !unique {
        return Err(ManifestError::Invalid("creators"));
    }
    Ok(creators)
}

/// Persisted identity fields, without accepting publication attribution.
///
/// # Errors
/// [`ManifestError`] for any refused field.
pub fn canonical_manifest_identity(
    assistant_id: &Value,
    version: &Value,
    name: &Value,
    summary: &Value,
) -> Result<ManifestIdentity, ManifestError> {
    let Value::String(version) = version else {
        return Err(ManifestError::Invalid("version"));
    };
    if !team_protocol::payload::ASSISTANT_VERSION_RE.is_full(version) {
        return Err(ManifestError::Invalid("version"));
    }
    Ok(ManifestIdentity {
        assistant_id: canonical_assistant_id(assistant_id)
            .ok_or(ManifestError::Invalid("id"))?
            .to_owned(),
        version: version.clone(),
        name: public_text(name, "name", MAX_NAME_CHARS)?.to_owned(),
        summary: public_text(summary, "summary", MAX_NAME_CHARS)?.to_owned(),
    })
}
