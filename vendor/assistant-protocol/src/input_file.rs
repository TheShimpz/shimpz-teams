//! Action file inputs and their invocation delivery (`validators/input_file.py`, Assistant Spec v1, ADR-0093).
//!
//! An Action names in `input_files` the input property that carries one opaque Team file id. Its invocation carries
//! the selected file in `files`: metadata first, and the original bytes only once the Action's authorization is
//! admitted.

use strict_json::Value;

use crate::human_request::AUTHORIZATION_KINDS;
use crate::python::{
    array, char_len, contains, has_exactly, int_within, is_stripped, is_true, member, object,
    py_eq, same, sha256_hex, small_int, string,
};

/// The most file inputs of one Action.
pub const MAX_INPUT_FILES: usize = 1;
/// The longest file input property name.
pub const MAX_PROPERTY_NAME: usize = 128;
/// The largest delivered file.
pub const MAX_FILE_BYTES: i64 = 8 * 1024 * 1024;
/// The longest base64 text of a delivered file.
pub const MAX_BASE64_CHARACTERS: usize = 4 * (8 * 1024 * 1024_usize).div_ceil(3);
/// The longest file name in UTF-8 bytes.
pub const MAX_NAME_BYTES: usize = 255;
/// The longest media type.
pub const MAX_MEDIA_TYPE: usize = 127;
/// The largest invocation without delivered file content.
pub const MAX_INVOCATION_BYTES: usize = 512 * 1024;
/// The largest invocation that delivers file content.
pub const MAX_FILE_INVOCATION_BYTES: usize = 12 * 1024 * 1024;
/// The most replay responses of one invocation.
pub const MAX_RESPONSES: usize = 8;
/// The most choices of one `input:choices` response.
pub const MAX_CHOICES: usize = 32;
/// The longest choice of one response.
pub const MAX_CHOICE_CHARACTERS: usize = 128;
/// The longest string response of each string-valued kind.
pub const TEXT_RESPONSE_BOUNDS: [(&str, usize); 6] = [
    ("input:text", 16_000),
    ("input:textarea", 16_000),
    ("input:password", 1024),
    ("input:phone", 64),
    ("input:select", 128),
    ("input:choice", 128),
];

const FILE_KEYS: [&str; 5] = ["name", "media_type", "size", "sha256", "content"];

/// The exact schema of a declared file input property: one opaque Team file id.
#[must_use]
pub fn file_id_schema() -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    let integer = |value: i64| Value::Number(strict_json::Number::Int(value));
    Value::Object(vec![
        ("type".to_owned(), text("string")),
        ("minLength".to_owned(), integer(32)),
        ("maxLength".to_owned(), integer(32)),
        ("pattern".to_owned(), text("^[0-9a-f]{32}$")),
    ])
}

fn withheld() -> Value {
    Value::Object(vec![(
        "type".to_owned(),
        Value::String("withheld".to_owned()),
    )])
}

fn lowercase_hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Whether `kind` is one of the authorization kinds.
fn authorizes(kind: &Value) -> bool {
    string(kind).is_some_and(|kind| AUTHORIZATION_KINDS.contains(&kind))
}

/// The stable reason an Action's `input_files` declaration is refused, or `None`.
#[must_use]
pub fn input_files_error(actions: &Value) -> Option<&'static str> {
    let Some(actions) =
        array(actions).filter(|actions| actions.iter().all(|action| object(action).is_some()))
    else {
        return Some("actions_invalid");
    };
    actions
        .iter()
        .find_map(|action| declaration_error(object(action).unwrap_or_default()))
}

/// A declared name is a required direct property with the exact file-id schema behind exactly one authorization.
#[must_use]
pub fn declaration_error(action: &[(String, Value)]) -> Option<&'static str> {
    let Some(declared) = member(action, "input_files")
        .and_then(array)
        .filter(|declared| {
            declared.len() <= MAX_INPUT_FILES
                && declared.iter().all(|name| {
                    string(name)
                        .is_some_and(|name| (1..=MAX_PROPERTY_NAME).contains(&char_len(name)))
                })
        })
    else {
        return Some("input_files_invalid");
    };
    if declared.is_empty() {
        return None;
    }
    if let Some(error) = property_error(member(action, "input_schema"), declared) {
        return Some(error);
    }
    let authorizations = member(action, "human_requests")
        .and_then(array)
        .map(|requests| {
            requests
                .iter()
                .filter(|request| authorizes(request))
                .count()
        });
    (authorizations != Some(1)).then_some("input_file_unauthorized")
}

fn property_error(schema: Option<&Value>, declared: &[Value]) -> Option<&'static str> {
    let schema = schema.and_then(object);
    let properties = schema
        .and_then(|schema| member(schema, "properties"))
        .and_then(object);
    let required =
        schema.and_then(|schema| member(schema, "required").map_or(Some(&[][..]), array));
    let (Some(properties), Some(required)) = (properties, required) else {
        return Some("input_file_unknown");
    };
    let names: Vec<&str> = declared.iter().filter_map(string).collect();
    if !names.iter().all(|name| {
        member(properties, name).is_some() && contains(required, &Value::String((*name).to_owned()))
    }) {
        return Some("input_file_unknown");
    }
    let exact = file_id_schema();
    let schemas_exact = names
        .iter()
        .all(|name| member(properties, name).is_some_and(|schema| same(schema, &exact)));
    (!schemas_exact).then_some("input_file_schema")
}

/// The stable reason an invocation's `files` do not match the Action's declared file input, or `None`.
#[must_use]
pub fn invocation_files_error(action: &Value, invocation: &Value) -> Option<&'static str> {
    let Some(action) = object(action).filter(|action| declaration_error(action).is_none()) else {
        return Some("invocation_invalid");
    };
    let empty = Value::Array(Vec::new());
    let responses = invocation.get("responses").unwrap_or(&empty);
    let files = invocation.get("files");
    if let Some(error) = responses_error(action, responses).or_else(|| files_shape_error(files)) {
        return Some(error);
    }
    let files = files.and_then(object).unwrap_or_default();
    let declared = member(action, "input_files")
        .and_then(array)
        .unwrap_or_default();
    let selected: Option<Vec<&str>> = invocation.get("input").and_then(object).and_then(|inputs| {
        declared
            .iter()
            .map(|name| {
                string(name)
                    .and_then(|name| member(inputs, name))
                    .and_then(string)
            })
            .collect()
    });
    let matched = selected.is_some_and(|selected| {
        files
            .iter()
            .all(|(file_id, _)| selected.contains(&file_id.as_str()))
            && selected
                .iter()
                .all(|file_id| member(files, file_id).is_some())
    });
    if !matched {
        return Some("files_mismatch");
    }
    let authorized = authorized(action, array(responses).unwrap_or_default());
    files
        .iter()
        .find_map(|(_, record)| content_error(object(record).unwrap_or_default(), authorized))
}

/// The stable reason a replay transcript is malformed for this Action before any file is bound, or `None`.
#[must_use]
pub fn responses_error(action: &[(String, Value)], responses: &Value) -> Option<&'static str> {
    let declared = member(action, "human_requests").and_then(array);
    let (Some(responses), Some(declared)) = (
        array(responses).filter(|responses| responses.len() <= MAX_RESPONSES),
        declared,
    ) else {
        return Some("responses_invalid");
    };
    for (ordinal, response) in responses.iter().enumerate() {
        if !valid_response(response, ordinal) {
            return Some("responses_invalid");
        }
        if !response
            .get("kind")
            .is_some_and(|kind| contains(declared, kind))
        {
            return Some("response_undeclared");
        }
    }
    let authorizations = responses
        .iter()
        .filter(|response| response.get("kind").is_some_and(authorizes))
        .count();
    (authorizations > 1).then_some("authorization_duplicate")
}

/// The stable reason `files` is refused before the Action's declaration is known, or `None`.
#[must_use]
pub fn files_shape_error(files: Option<&Value>) -> Option<&'static str> {
    let Some(files) = files
        .and_then(object)
        .filter(|files| files.len() <= MAX_INPUT_FILES)
    else {
        return Some("files_invalid");
    };
    let valid = files
        .iter()
        .all(|(file_id, record)| lowercase_hex(file_id, 32) && valid_record(record));
    (!valid).then_some("file_invalid")
}

/// Whether an invocation carries delivered file content and so admits the larger size bound.
#[must_use]
pub fn delivers_content(invocation: &Value) -> bool {
    invocation
        .get("files")
        .and_then(object)
        .is_some_and(|files| {
            files.iter().any(|(_, record)| {
                record
                    .get("content")
                    .and_then(|content| content.get("type"))
                    .and_then(string)
                    == Some("delivered")
            })
        })
}

/// Decodes canonical padded standard base64 of at most [`MAX_BASE64_CHARACTERS`] characters.
#[must_use]
pub fn decode_content(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if bytes.len() > MAX_BASE64_CHARACTERS || !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut data = Vec::with_capacity(bytes.len() / 4 * 3);
    for (index, chunk) in bytes.chunks(4).enumerate() {
        let last = index + 1 == bytes.len() / 4;
        decode_chunk(chunk, last, &mut data)?;
    }
    Some(data)
}

fn sextet(byte: u8) -> Option<u32> {
    match byte {
        b'A'..=b'Z' => Some(u32::from(byte - b'A')),
        b'a'..=b'z' => Some(u32::from(byte - b'a') + 26),
        b'0'..=b'9' => Some(u32::from(byte - b'0') + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// One four-character group; padding only at the end, and the unused bits of a padded group must be zero.
fn decode_chunk(chunk: &[u8], last: bool, data: &mut Vec<u8>) -> Option<()> {
    let padding = chunk.iter().rev().take_while(|byte| **byte == b'=').count();
    if padding > 2 || (padding > 0 && !last) {
        return None;
    }
    let mut bits = 0_u32;
    for byte in &chunk[..4 - padding] {
        bits = (bits << 6) | sextet(*byte)?;
    }
    bits <<= 6 * padding;
    let decoded = bits.to_be_bytes();
    let kept = 3 - padding;
    if decoded[1 + kept..].iter().any(|byte| *byte != 0) {
        return None;
    }
    data.extend_from_slice(&decoded[1..=kept]);
    Some(())
}

/// A filename is trimmed text of at most 255 UTF-8 bytes without controls, slashes, or a dot path.
#[must_use]
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && is_stripped(name)
        && name.len() <= MAX_NAME_BYTES
        && name != "."
        && name != ".."
        && !name.chars().any(|character| {
            character < ' ' || character == '\u{7f}' || character == '/' || character == '\\'
        })
}

/// `[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*`.
#[must_use]
pub fn media_type(text: &str) -> bool {
    let part = |part: &str| {
        let bytes = part.as_bytes();
        bytes
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            && bytes.iter().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"!#$&^_.+-".contains(byte)
            })
    };
    text.split_once('/')
        .is_some_and(|(kind, subtype)| part(kind) && part(subtype))
}

fn content_error(record: &[(String, Value)], authorized: bool) -> Option<&'static str> {
    let content = member(record, "content").unwrap_or(&Value::Null);
    if py_eq(content, &withheld()) {
        return authorized.then_some("file_withheld_after_authorization");
    }
    if !authorized {
        return Some("file_delivered_without_authorization");
    }
    let data = content
        .get("base64")
        .and_then(string)
        .and_then(decode_content);
    let size = member(record, "size").and_then(small_int);
    let digest = member(record, "sha256").and_then(string);
    let matches = data.is_some_and(|data| {
        i64::try_from(data.len()).ok() == size && Some(sha256_hex(&data).as_str()) == digest
    });
    (!matches).then_some("file_content_mismatch")
}

fn valid_record(record: &Value) -> bool {
    object(record).is_some_and(|members| {
        has_exactly(members, &FILE_KEYS)
            && member(members, "content").is_some_and(valid_content_branch)
            && member(members, "name")
                .and_then(string)
                .is_some_and(valid_name)
            && member(members, "media_type")
                .and_then(string)
                .is_some_and(|text| char_len(text) <= MAX_MEDIA_TYPE && media_type(text))
            && member(members, "size").is_some_and(|size| int_within(size, 1, MAX_FILE_BYTES))
            && member(members, "sha256")
                .and_then(string)
                .is_some_and(|digest| lowercase_hex(digest, 64))
    })
}

fn valid_content_branch(content: &Value) -> bool {
    py_eq(content, &withheld())
        || object(content).is_some_and(|members| {
            has_exactly(members, &["type", "base64"])
                && member(members, "type").and_then(string) == Some("delivered")
                && member(members, "base64")
                    .and_then(string)
                    .is_some_and(|text| char_len(text) <= MAX_BASE64_CHARACTERS)
        })
}

/// Content is delivered exactly when the well-formed transcript holds the Action's own authorization response.
fn authorized(action: &[(String, Value)], responses: &[Value]) -> bool {
    let declared: Vec<&Value> = member(action, "human_requests")
        .and_then(array)
        .unwrap_or_default()
        .iter()
        .filter(|kind| authorizes(kind))
        .collect();
    matches!(declared.as_slice(), [kind]
        if responses.iter().any(|response| response.get("kind").is_some_and(|candidate| py_eq(candidate, kind))))
}

fn valid_response(response: &Value, ordinal: usize) -> bool {
    object(response).is_some_and(|members| {
        has_exactly(members, &["ordinal", "fingerprint", "kind", "value"])
            && member(members, "ordinal")
                .and_then(small_int)
                .is_some_and(|value| usize::try_from(value).ok() == Some(ordinal))
            && member(members, "fingerprint")
                .and_then(string)
                .is_some_and(|digest| lowercase_hex(digest, 64))
            && member(members, "kind")
                .and_then(string)
                .zip(member(members, "value"))
                .is_some_and(|(kind, value)| valid_response_value(kind, value))
    })
}

fn valid_response_value(kind: &str, value: &Value) -> bool {
    if AUTHORIZATION_KINDS.contains(&kind) {
        return is_true(value);
    }
    if kind == "input:choices" {
        let items = array(value).unwrap_or_default();
        let texts: Vec<&str> = items.iter().filter_map(string).collect();
        return array(value).is_some_and(|items| items.len() <= MAX_CHOICES)
            && texts.len() == items.len()
            && texts
                .iter()
                .all(|text| char_len(text) <= MAX_CHOICE_CHARACTERS)
            && texts
                .iter()
                .enumerate()
                .all(|(index, text)| !texts[..index].contains(text));
    }
    TEXT_RESPONSE_BOUNDS
        .iter()
        .find(|(name, _)| *name == kind)
        .is_some_and(|(_, bound)| string(value).is_some_and(|text| char_len(text) <= *bound))
}
