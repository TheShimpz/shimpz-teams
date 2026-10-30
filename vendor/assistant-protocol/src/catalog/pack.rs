//! Language packs: exact canonical bytes holding one admitted translation per message and locale.

use strict_json::Value;

use super::template::{Role, template_error};
use super::{
    LOCALES, MAX_PACK_BYTES, MAX_TEMPLATE_CHARS, Message, canonical_json, catalog_by_id,
    catalog_digest, max_length, public_str,
};
use crate::python::{array, has_exactly, member, object, sha256_hex, small_int, string};

/// The digest of the exact canonical pack bytes.
#[must_use]
pub fn pack_digest(raw: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(raw))
}

/// The first error of exact pack bytes against an already admitted catalog `messages`, or `None`.
#[must_use]
pub fn pack_error(raw: &[u8], messages: &Value) -> Option<&'static str> {
    if raw.len() > MAX_PACK_BYTES {
        return Some("pack_bytes");
    }
    let Some(pack) = strict_json::parse(raw)
        .ok()
        .filter(|pack| canonical_json(pack) == raw)
    else {
        return Some("pack_encoding");
    };
    let Some(locales) = pack_locales(&pack) else {
        return Some("pack_shape");
    };
    if pack.get("catalog").and_then(string) != Some(catalog_digest(messages).as_str()) {
        return Some("pack_catalog");
    }
    translations_error(locales, messages)
}

fn digest(value: Option<&Value>) -> bool {
    value.and_then(string).is_some_and(|text| {
        text.strip_prefix("sha256:").is_some_and(|hex| {
            hex.len() == 64
                && hex
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
    })
}

/// The pack's locale table when its shape is closed: the four members, the format, two digests, and one object per
/// locale.
fn pack_locales(pack: &Value) -> Option<&[(String, Value)]> {
    let members = object(pack)
        .filter(|members| has_exactly(members, &["format", "catalog", "policy", "locales"]))?;
    let shaped = member(members, "format").and_then(string) == Some(super::PACK_FORMAT)
        && digest(member(members, "catalog"))
        && digest(member(members, "policy"));
    let locales = member(members, "locales")
        .and_then(object)
        .filter(|locales| {
            has_exactly(locales, &LOCALES)
                && locales.iter().all(|(_, entries)| object(entries).is_some())
        })?;
    shaped.then_some(locales)
}

fn translations_error(locales: &[(String, Value)], messages: &Value) -> Option<&'static str> {
    let catalog = catalog_by_id(messages);
    for locale in LOCALES {
        let entries = member(locales, locale).and_then(object).unwrap_or_default();
        let complete = entries.len() == catalog.len()
            && entries
                .iter()
                .all(|(identifier, _)| catalog.contains_key(identifier.as_str()));
        if !complete {
            return Some("pack_incomplete");
        }
        for (identifier, text) in entries {
            if let Some(error) = translation_error(text, catalog[identifier.as_str()]) {
                return Some(error);
            }
        }
    }
    None
}

fn translation_error(text: &Value, message: &Message) -> Option<&'static str> {
    let Some(text) = string(text).filter(|text| public_str(text, MAX_TEMPLATE_CHARS)) else {
        return Some("public_text");
    };
    let params: Vec<(&str, usize)> = member(message, "params")
        .and_then(array)
        .unwrap_or_default()
        .iter()
        .filter_map(|param| {
            let name = param.get("name").and_then(string)?;
            let length = param.get("max_length").and_then(small_int)?;
            Some((name, usize::try_from(length).ok()?))
        })
        .collect();
    template_error(
        text,
        &params,
        max_length(message).unwrap_or(0),
        Role::Translation,
    )
}
