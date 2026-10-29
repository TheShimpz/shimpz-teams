//! A Team file's metadata and the storage answers Admin projects from Team.

use std::cmp::Ordering;

use strict_json::Value;

use super::{
    FILE_ID_RE, MAX_FILE_UPLOAD_BYTES, MAX_FILENAME_BYTES, MAX_MEDIA_TYPE_CHARS, MAX_TEAM_FILES,
    MEDIA_TYPE_RE, SHA256_RE,
};
use crate::python::text;
use crate::python::value::{array, natural_cmp, natural_digits, natural_sub, object, string};

/// `canonical_file_id`.
#[must_use]
pub fn canonical_file_id(value: &Value) -> Option<&str> {
    string(value).filter(|id| FILE_ID_RE.is_full(id))
}

/// `canonical_filename`: trimmed, at most 255 UTF-8 bytes, no path separator or ASCII control, not `.` or `..`.
#[must_use]
pub fn canonical_filename(value: &Value) -> Option<&str> {
    string(value).filter(|name| {
        !name.is_empty()
            && text::strip(name) == *name
            && name.len() <= MAX_FILENAME_BYTES
            && *name != "."
            && *name != ".."
            && !name.contains('/')
            && !name.contains('\\')
            && !name
                .chars()
                .any(|character| character < ' ' || character == '\u{7f}')
    })
}

/// `canonical_media_type`: lowercased; absent or empty is `application/octet-stream`.
#[must_use]
pub fn canonical_media_type(value: &Value) -> Option<String> {
    if *value == Value::Null || string(value) == Some("") {
        return Some("application/octet-stream".to_owned());
    }
    let media_type =
        string(value).filter(|media_type| text::len(media_type) <= MAX_MEDIA_TYPE_CHARS)?;
    let lowered = text::lower(media_type);
    MEDIA_TYPE_RE.is_full(&lowered).then_some(lowered)
}

/// `integer(value, minimum=...)`: an integer (never a boolean) at least `minimum`, as its decimal digits.
#[must_use]
pub fn integer(value: &Value, minimum: u8) -> Option<String> {
    natural_digits(value)
        .filter(|digits| natural_cmp(digits, &minimum.to_string()) != Ordering::Less)
}

fn get<'a>(members: &'a [(String, Value)], key: &str) -> &'a Value {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map_or(&Value::Null, |(_, member)| member)
}

/// `project_storage_usage`: exact usage arithmetic, including cleanup after a quota reduction.
#[must_use]
pub fn project_storage_usage(value: &Value) -> Option<Value> {
    usage(value).map(Value::Object)
}

fn usage(value: &Value) -> Option<Vec<(String, Value)>> {
    let members = object(value)?;
    let (used_value, limit_value, remaining_value) = (
        get(members, "used_bytes"),
        get(members, "limit_bytes"),
        get(members, "remaining_bytes"),
    );
    let used = integer(used_value, 0)?;
    let limit = integer(limit_value, 1)?;
    let remaining = integer(remaining_value, 0)?;
    let order = natural_cmp(&used, &limit);
    let within_quota = order != Ordering::Greater && remaining == natural_sub(&limit, &used);
    let over_quota = order != Ordering::Less && remaining == "0";
    (within_quota || over_quota).then(|| {
        vec![
            ("used_bytes".to_owned(), used_value.clone()),
            ("limit_bytes".to_owned(), limit_value.clone()),
            ("remaining_bytes".to_owned(), remaining_value.clone()),
        ]
    })
}

/// `project_file_metadata(value, include_usage=...)`: one file's metadata, with the Team's usage when asked.
#[must_use]
pub fn project_file_metadata(value: &Value, include_usage: bool) -> Option<Value> {
    metadata(value, include_usage).map(Value::Object)
}

fn metadata(value: &Value, include_usage: bool) -> Option<Vec<(String, Value)>> {
    let members = object(value)?;
    let id = canonical_file_id(get(members, "id"))?;
    let name = canonical_filename(get(members, "name"))?;
    let media_type = canonical_media_type(get(members, "media_type"))?;
    let size_value = get(members, "size");
    let size = integer(size_value, 1)?;
    let sha256 = get(members, "sha256");
    let created_at = get(members, "created_at");
    let valid = natural_cmp(&size, &MAX_FILE_UPLOAD_BYTES.to_string()) != Ordering::Greater
        && string(sha256).is_some_and(|digest| SHA256_RE.is_full(digest))
        && integer(created_at, 1).is_some();
    if !valid {
        return None;
    }
    let mut metadata = vec![
        ("id".to_owned(), Value::String(id.to_owned())),
        ("name".to_owned(), Value::String(name.to_owned())),
        ("media_type".to_owned(), Value::String(media_type)),
        ("size".to_owned(), size_value.clone()),
        ("sha256".to_owned(), sha256.clone()),
        ("created_at".to_owned(), created_at.clone()),
    ];
    if include_usage {
        metadata.extend(usage(value)?);
    }
    Some(metadata)
}

/// `project_storage_response(value, kind=..., expected_team_id=...)`: an upload's or a listing's projection.
#[must_use]
pub fn project_storage_response(
    value: &Value,
    kind: &str,
    expected_team_id: &str,
) -> Option<Value> {
    let members = object(value)?;
    if string(get(members, "team_id")) != Some(expected_team_id) {
        return None;
    }
    let mut projected = vec![(
        "team_id".to_owned(),
        Value::String(expected_team_id.to_owned()),
    )];
    match kind {
        "upload" => {
            let mut file = metadata(get(members, "file"), true)?;
            let usage = file.split_off(file.len() - 3);
            projected.push(("file".to_owned(), Value::Object(file)));
            projected.extend(usage);
        }
        "list" => {
            let raw = array(get(members, "files")).filter(|files| files.len() <= MAX_TEAM_FILES)?;
            let files: Vec<Option<Value>> = raw
                .iter()
                .map(|item| project_file_metadata(item, false))
                .collect();
            let usage = usage(value)?;
            let files: Vec<Value> = files.into_iter().collect::<Option<_>>()?;
            let ids: Vec<&Value> = files.iter().map(|file| get_value(file, "id")).collect();
            if !ids
                .iter()
                .enumerate()
                .all(|(index, id)| !ids[..index].contains(id))
            {
                return None;
            }
            projected.push(("files".to_owned(), Value::Array(files)));
            projected.extend(usage);
        }
        _ => return None,
    }
    Some(Value::Object(projected))
}

fn get_value<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}
