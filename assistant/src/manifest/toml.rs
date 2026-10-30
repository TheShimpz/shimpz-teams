//! The manifest's TOML table: bounded UTF-8, printable text, the closed sections, and no credential-shaped field.
//!
//! The table is decoded to strict JSON values so every later rule reads one value model. A TOML date or time, or a
//! non-finite float, has no place in any admitted field, so the manifest is refused at decoding.

use strict_json::{Number, Value};
use team_protocol::python::text;

use super::MAX_MANIFEST_BYTES;
use crate::ManifestError;
use crate::text::resembles_credential;

const ROOT_REQUIRED: [&str; 2] = ["shimpz", "network"];
const ROOT_OPTIONAL: [&str; 2] = ["integrations", "stored_inputs"];
const METADATA: [&str; 9] = [
    "spec",
    "id",
    "version",
    "name",
    "summary",
    "description",
    "creators",
    "github",
    "genesis",
];
const CREDENTIAL_MARKERS: [&str; 7] = [
    "secret",
    "password",
    "token",
    "api_key",
    "private_key",
    "access_key",
    "env",
];
/// The deepest the manifest's values may nest.
const MAX_DEPTH: usize = 64;

/// The admitted manifest table.
pub(super) fn manifest_table(raw: &[u8]) -> Result<Value, ManifestError> {
    if !(1..=MAX_MANIFEST_BYTES).contains(&raw.len()) {
        return Err(ManifestError::Invalid("manifest size"));
    }
    let source =
        std::str::from_utf8(raw).map_err(|_| ManifestError::Invalid("manifest encoding"))?;
    if source.chars().any(|character| {
        !text::isprintable_char(character) && !matches!(character, '\n' | '\r' | '\t')
    }) {
        return Err(ManifestError::Invalid("manifest text"));
    }
    let table: ::toml::Table = source
        .parse()
        .map_err(|_| ManifestError::Invalid("manifest TOML"))?;
    let manifest = convert_table(&table)?;
    let Value::Object(root) = &manifest else {
        return Err(ManifestError::Invalid("manifest TOML"));
    };
    let has = |key: &str| root.iter().any(|(name, _)| name == key);
    let known = root.iter().all(|(name, _)| {
        ROOT_REQUIRED.contains(&name.as_str()) || ROOT_OPTIONAL.contains(&name.as_str())
    });
    if !ROOT_REQUIRED.iter().all(|key| has(key)) || !known {
        return Err(ManifestError::Invalid("manifest top-level field"));
    }
    let (Some(Value::Object(metadata)), Some(Value::Object(network))) =
        (manifest.get("shimpz"), manifest.get("network"))
    else {
        return Err(ManifestError::Invalid("manifest section"));
    };
    let metadata_closed = metadata.iter().filter(|(name, _)| name != "links").count()
        == METADATA.len()
        && METADATA
            .iter()
            .all(|key| metadata.iter().any(|(name, _)| name == key));
    if !metadata_closed || network.len() != 1 || network[0].0 != "allowed_hosts" {
        return Err(ManifestError::Invalid("manifest section field"));
    }
    reject_credential_material(&manifest)?;
    Ok(manifest)
}

fn convert_table(table: &::toml::Table) -> Result<Value, ManifestError> {
    table
        .iter()
        .map(|(key, value)| Ok((key.clone(), convert(value)?)))
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Object)
}

fn convert(value: &::toml::Value) -> Result<Value, ManifestError> {
    Ok(match value {
        ::toml::Value::String(text) => Value::String(text.clone()),
        ::toml::Value::Integer(integer) => Value::Number(Number::Int(*integer)),
        ::toml::Value::Float(float) if float.is_finite() => Value::Number(Number::Float(*float)),
        ::toml::Value::Boolean(flag) => Value::Bool(*flag),
        ::toml::Value::Array(items) => {
            Value::Array(items.iter().map(convert).collect::<Result<_, _>>()?)
        }
        ::toml::Value::Table(table) => convert_table(table)?,
        ::toml::Value::Float(_) | ::toml::Value::Datetime(_) => {
            return Err(ManifestError::Invalid("manifest value"));
        }
    })
}

/// No field name outside `stored_inputs` names a credential, no text is credential material, and nothing nests deeper
/// than [`MAX_DEPTH`].
fn reject_credential_material(manifest: &Value) -> Result<(), ManifestError> {
    let mut pending: Vec<(&Value, bool, usize)> = vec![(manifest, false, 0)];
    while let Some((current, in_stored_inputs_root, depth)) = pending.pop() {
        if depth > MAX_DEPTH {
            return Err(ManifestError::Invalid("manifest nesting"));
        }
        match current {
            Value::Object(members) => {
                for (key, child) in members {
                    let lowered = key.to_lowercase();
                    if !in_stored_inputs_root
                        && CREDENTIAL_MARKERS
                            .iter()
                            .any(|marker| lowered.contains(marker))
                    {
                        return Err(ManifestError::Invalid("manifest credential field"));
                    }
                    let stored_inputs_root = depth == 0 && key == "stored_inputs";
                    pending.push((child, stored_inputs_root, depth + 1));
                }
            }
            Value::Array(items) => {
                // A list keeps its holder's path, as the reference walks it.
                pending.extend(
                    items
                        .iter()
                        .map(|child| (child, in_stored_inputs_root, depth + 1)),
                );
            }
            Value::String(text) if resembles_credential(text) => {
                return Err(ManifestError::Invalid("manifest credential material"));
            }
            _ => {}
        }
    }
    Ok(())
}
