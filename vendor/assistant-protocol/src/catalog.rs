//! Message catalogs, displayed copy, rendering, and language packs (`validators/message_catalog.py`, Assistant Spec
//! v1, ADR-0091).
//!
//! A catalog is the sorted list of the English templates an Assistant declares, each `{id, msgid, max_length,
//! params}` with `id` the SHA-256 of its template; a language pack is the canonical JSON document holding one admitted
//! translation per message and locale. Admission is lexical and fail-closed: it never repairs or normalizes text.

mod pack;
mod template;

use std::collections::HashMap;

use strict_json::Value;
use unicode_normalization::is_nfc;

use crate::python::{
    array, canonical_json as canonical_text, char_len, has_exactly, int_within, is_stripped,
    member, object, py_eq, sha256_hex, small_int, string,
};
use crate::unicode::printable;

pub use self::pack::{pack_digest, pack_error};
use self::template::{Role, template_error};
pub use self::template::{placeholders, render};

/// The non-English interface locales a pack translates into, in canonical order; English is the catalog itself.
pub const LOCALES: [&str; 7] = ["ar", "de", "es", "fr", "ja", "pt", "zh"];
/// The exact `format` of every language pack.
pub const PACK_FORMAT: &str = "assistant-language-pack-v1";
/// The field bounds a message's `max_length` may take.
pub const FIELD_BOUNDS: [i64; 4] = [80, 120, 160, 500];
/// The bound of the summary message.
pub const SUMMARY_BOUND: usize = 80;
/// The longest authored Assistant or Stored Input description.
pub const DESCRIPTION_CHARS: usize = 400;
/// The bound of a description's catalog message.
pub const DESCRIPTION_BOUND: usize = 500;
/// The longest authored Action description or Stored Input label.
pub const LINE_CHARS: usize = 80;
/// The bound of a one-line text's catalog message.
pub const LINE_BOUND: usize = 120;
/// The longest template or translation.
pub const MAX_TEMPLATE_CHARS: usize = 500;
/// The most messages of one catalog.
pub const MAX_MESSAGES: usize = 256;
/// The most parameters of one message.
pub const MAX_PARAMS: usize = 8;
/// The largest canonical catalog.
pub const MAX_CATALOG_BYTES: usize = 131_072;
/// The most JSON values of one catalog.
pub const MAX_CATALOG_VALUES: usize = 4_096;
/// The largest language pack.
pub const MAX_PACK_BYTES: usize = 2_097_152;
/// Each parameter kind and the largest `max_length` it may declare.
pub const PARAM_BOUNDS: [(&str, i64); 4] = [
    ("integer", 15),
    ("domain", 253),
    ("dns_name", 253),
    ("identifier", 128),
];

const MESSAGE_KEYS: [&str; 4] = ["id", "msgid", "max_length", "params"];

/// The members of one catalog message.
pub type Message = [(String, Value)];

/// The canonical UTF-8 JSON encoding fingerprints and digests are taken over.
#[must_use]
pub fn canonical_json(value: &Value) -> Vec<u8> {
    canonical_text(value).into_bytes()
}

/// The lowercase SHA-256 of a template's UTF-8 bytes.
#[must_use]
pub fn message_id(msgid: &str) -> String {
    sha256_hex(msgid.as_bytes())
}

/// The digest of the canonical catalog.
#[must_use]
pub fn catalog_digest(messages: &Value) -> String {
    format!("sha256:{}", sha256_hex(&canonical_json(messages)))
}

/// Whether `text` is trimmed, printable under the pinned table, NFC, and 1 to `maximum` characters.
#[must_use]
pub fn public_str(text: &str, maximum: usize) -> bool {
    is_stripped(text) && (1..=maximum).contains(&char_len(text)) && printable(text) && is_nfc(text)
}

/// [`public_str`] for a value that must be a string.
#[must_use]
pub fn public_text(value: &Value, maximum: usize) -> bool {
    string(value).is_some_and(|text| public_str(text, maximum))
}

/// Each catalog message by its id; a later duplicate wins.
#[must_use]
pub fn catalog_by_id(messages: &Value) -> HashMap<&str, &Message> {
    array(messages)
        .unwrap_or_default()
        .iter()
        .filter_map(|message| {
            let members = object(message)?;
            Some((member(members, "id").and_then(string)?, members))
        })
        .collect()
}

/// The first catalog error for a contract whose manifest declares `summary`, or `None`.
#[must_use]
pub fn catalog_error(messages: &Value, summary: &Value) -> Option<&'static str> {
    if let Some(error) = aggregate_error(messages) {
        return Some(error);
    }
    let entries = array(messages).unwrap_or_default();
    if let Some(error) = entries.iter().find_map(message_error) {
        return Some(error);
    }
    let ids: Vec<&str> = entries
        .iter()
        .filter_map(|message| message.get("id").and_then(string))
        .collect();
    if ids.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Some("catalog_order");
    }
    summary_error(entries, summary)
}

/// Every displayed static text beyond the summary with the bound its catalog message must fit: the Assistant
/// description and each Stored Input help text, then each Action description and each Stored Input label.
#[must_use]
pub fn display_uses<'a, T: 'a>(
    description: T,
    action_descriptions: impl IntoIterator<Item = T>,
    labels: impl IntoIterator<Item = T>,
    help_texts: impl IntoIterator<Item = T>,
) -> Vec<(T, usize)> {
    std::iter::once(description)
        .chain(help_texts)
        .map(|text| (text, DESCRIPTION_BOUND))
        .chain(
            action_descriptions
                .into_iter()
                .map(|text| (text, LINE_BOUND)),
        )
        .chain(labels.into_iter().map(|text| (text, LINE_BOUND)))
        .collect()
}

/// `catalog_display` unless each displayed text (`None` for one that is not a string) is one parameterless message
/// of the admitted `messages` within its bound.
#[must_use]
pub fn display_error<'a>(
    messages: &Value,
    uses: impl IntoIterator<Item = (Option<&'a str>, usize)>,
) -> Option<&'static str> {
    let by_template: HashMap<&str, &Message> = array(messages)
        .unwrap_or_default()
        .iter()
        .filter_map(|message| {
            let members = object(message)?;
            Some((member(members, "msgid").and_then(string)?, members))
        })
        .collect();
    let refused = uses.into_iter().any(|(text, bound)| {
        text.and_then(|text| by_template.get(text))
            .is_none_or(|message| !parameterless_within(message, bound))
    });
    refused.then_some("catalog_display")
}

/// Whether a message declares no parameter and a `max_length` of at most `bound`.
fn parameterless_within(message: &Message, bound: usize) -> bool {
    let empty = member(message, "params")
        .and_then(array)
        .is_some_and(<[Value]>::is_empty);
    empty && max_length(message).is_some_and(|length| length <= bound)
}

/// A message's `max_length`.
#[must_use]
pub fn max_length(message: &Message) -> Option<usize> {
    member(message, "max_length")
        .and_then(small_int)
        .and_then(|length| usize::try_from(length).ok())
}

fn aggregate_error(messages: &Value) -> Option<&'static str> {
    if !array(messages).is_some_and(|entries| (1..=MAX_MESSAGES).contains(&entries.len())) {
        return Some("catalog_shape");
    }
    if values_exceed(messages, MAX_CATALOG_VALUES)
        || canonical_json(messages).len() > MAX_CATALOG_BYTES
    {
        return Some("catalog_bounds");
    }
    None
}

/// Whether a value holds more than `limit` values, counting itself and every array item and member value.
#[must_use]
pub fn values_exceed(value: &Value, limit: usize) -> bool {
    let mut pending = vec![value];
    let mut visited = 0_usize;
    while let Some(item) = pending.pop() {
        visited += 1;
        match item {
            Value::Array(items) => pending.extend(items),
            Value::Object(members) => pending.extend(members.iter().map(|(_, child)| child)),
            _ => {}
        }
        if visited + pending.len() > limit {
            return true;
        }
    }
    false
}

/// The first error of one catalog entry, or `None`.
#[must_use]
pub fn message_error(message: &Value) -> Option<&'static str> {
    let Some(members) = object(message).filter(|members| {
        has_exactly(members, &MESSAGE_KEYS)
            && member(members, "max_length").is_some_and(field_bound)
    }) else {
        return Some("message_shape");
    };
    let Some(msgid) = member(members, "msgid")
        .and_then(string)
        .filter(|msgid| public_str(msgid, MAX_TEMPLATE_CHARS))
    else {
        return Some("public_text");
    };
    if member(members, "id").and_then(string) != Some(message_id(msgid).as_str()) {
        return Some("message_id");
    }
    let Some(params) = member(members, "params").and_then(declarations) else {
        return Some("message_params");
    };
    template_error(
        msgid,
        &params,
        max_length(members).unwrap_or(0),
        Role::Message,
    )
}

/// Whether `value` is one of the [`FIELD_BOUNDS`].
#[must_use]
pub fn field_bound(value: &Value) -> bool {
    small_int(value).is_some_and(|bound| FIELD_BOUNDS.contains(&bound))
}

/// The parameter declarations by name with their `max_length`, when closed, bounded, sorted, and unique.
fn declarations(value: &Value) -> Option<Vec<(&str, usize)>> {
    let items = array(value).filter(|items| items.len() <= MAX_PARAMS)?;
    let declared: Vec<(&str, usize)> = items.iter().map(declaration).collect::<Option<_>>()?;
    declared
        .windows(2)
        .all(|pair| pair[0].0 < pair[1].0)
        .then_some(declared)
}

fn declaration(value: &Value) -> Option<(&str, usize)> {
    let members =
        object(value).filter(|members| has_exactly(members, &["name", "kind", "max_length"]))?;
    let name = member(members, "name")
        .and_then(string)
        .filter(|name| param_name(name))?;
    let kind = member(members, "kind").and_then(string)?;
    let bound = PARAM_BOUNDS
        .iter()
        .find(|(declared, _)| *declared == kind)
        .map(|(_, bound)| *bound)?;
    let length = member(members, "max_length").filter(|length| int_within(length, 1, bound))?;
    Some((name, usize::try_from(small_int(length)?).ok()?))
}

/// `[a-z][a-z0-9_]{0,31}`.
#[must_use]
pub fn param_name(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=32).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_')
}

fn summary_error(messages: &[Value], summary: &Value) -> Option<&'static str> {
    let target = messages
        .iter()
        .filter_map(object)
        .find(|message| member(message, "msgid").is_some_and(|msgid| py_eq(msgid, summary)));
    match target {
        Some(message) if parameterless_within(message, SUMMARY_BOUND) => None,
        _ => Some("catalog_summary"),
    }
}
