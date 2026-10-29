//! The downloads a Browser step sees, the completeness check's verdict, and the `browser` field of a chat terminal.

use strict_json::Value;

use super::{plain, sentence};
use crate::payload::{
    MAX_FILE_UPLOAD_BYTES, canonical_file_id, canonical_filename, canonical_media_type,
};
use crate::python::text;
use crate::python::value::{exact_object, field, is_bool, object_of, small_int, string};

/// A finished task's completeness, as the Brain's advisory check judged it or not.
pub const COMPLETENESS: [&str; 3] = ["complete", "incomplete", "not-checked"];
/// The most cited URLs provenance screening could not match that a task result lists.
pub const MAX_UNSCREENED_URLS: usize = 20;
/// The most completed file names a step sees.
pub const MAX_DOWNLOAD_NAMES: usize = 20;
/// The most still-downloading files a step counts.
pub const MAX_PENDING_DOWNLOADS: i64 = 1_000;
/// Why a named download was not kept, sorted.
pub const TERMINAL_LOSSES: [&str; 7] = [
    "browser-lost",
    "empty",
    "incomplete",
    "name-refused",
    "over-limit",
    "quota-exceeded",
    "store-unavailable",
];
/// The most kept files a terminal lists.
pub const MAX_TERMINAL_FILES: usize = 8;
/// The most named losses a terminal lists.
pub const MAX_TERMINAL_LOSSES: usize = 128;
/// The most unnamed losses a terminal counts.
pub const MAX_TERMINAL_SKIPPED: i64 = 1_000;
/// The longest download name, in UTF-8 bytes.
pub const MAX_NAME_BYTES: usize = 255;

/// `canonical_check`: complete, or incomplete with one sentence, or null when not checked.
#[must_use]
pub fn canonical_check(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["complete", "missing"])?;
    let (complete, missing) = (field(members, "complete"), field(members, "missing"));
    let admitted = match complete {
        Value::Bool(true) | Value::Null => *missing == Value::Null,
        Value::Bool(false) => sentence(missing).is_some(),
        _ => false,
    };
    admitted.then(|| {
        object_of(vec![
            ("complete", complete.clone()),
            ("missing", missing.clone()),
        ])
    })
}

fn download_name(value: &Value) -> Option<&str> {
    plain(value, MAX_NAME_BYTES, false).filter(|name| name.len() <= MAX_NAME_BYTES)
}

/// `canonical_downloads`: the completed files present, sorted and distinct, how many still download, and whether
/// names were left out.
#[must_use]
pub fn canonical_downloads(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["files", "pending", "truncated"])?;
    let Value::Array(files) = field(members, "files") else {
        return None;
    };
    let names: Vec<&str> = files.iter().filter_map(download_name).collect();
    let pending = field(members, "pending");
    let admitted = files.len() <= MAX_DOWNLOAD_NAMES
        && names.len() == files.len()
        && names.windows(2).all(|pair| pair[0] < pair[1])
        && small_int(pending).is_some_and(|count| (0..=MAX_PENDING_DOWNLOADS).contains(&count))
        && is_bool(field(members, "truncated"));
    admitted.then(|| {
        object_of(vec![
            ("files", Value::Array(files.clone())),
            ("pending", pending.clone()),
            ("truncated", field(members, "truncated").clone()),
        ])
    })
}

/// `canonical_file_reference`: one kept download as a reply shows it.
#[must_use]
pub fn canonical_file_reference(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["id", "name", "media_type", "size"])?;
    let name = string(field(members, "name"))?;
    let media_type = string(field(members, "media_type"))?;
    let admitted = canonical_file_id(field(members, "id")).is_some()
        && canonical_filename(field(members, "name")) == Some(name)
        && canonical_media_type(field(members, "media_type")).as_deref() == Some(media_type)
        && small_int(field(members, "size"))
            .is_some_and(|size| (1..=MAX_FILE_UPLOAD_BYTES).contains(&size));
    admitted.then(|| {
        object_of(
            ["id", "name", "media_type", "size"]
                .into_iter()
                .map(|key| (key, field(members, key).clone()))
                .collect(),
        )
    })
}

/// One named download a reply says it did not keep, with its closed reason.
fn loss(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["name", "reason"])?;
    let reason =
        string(field(members, "reason")).filter(|reason| TERMINAL_LOSSES.contains(reason))?;
    let name = string(field(members, "name")).filter(|name| {
        !name.is_empty() && name.len() <= MAX_NAME_BYTES && text::isprintable(name)
    })?;
    Some(object_of(vec![
        ("name", Value::String(name.to_owned())),
        ("reason", Value::String(reason.to_owned())),
    ]))
}

/// `canonical_terminal`: the kept files' references, the named losses, and the unnamed count.
#[must_use]
pub fn canonical_terminal(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["files", "lost", "skipped"])?;
    let (Value::Array(files), Value::Array(lost)) =
        (field(members, "files"), field(members, "lost"))
    else {
        return None;
    };
    let skipped = field(members, "skipped");
    if files.len() > MAX_TERMINAL_FILES
        || lost.len() > MAX_TERMINAL_LOSSES
        || !small_int(skipped).is_some_and(|count| (0..=MAX_TERMINAL_SKIPPED).contains(&count))
    {
        return None;
    }
    let references: Vec<Value> = files
        .iter()
        .map(canonical_file_reference)
        .collect::<Option<_>>()?;
    let losses: Vec<Value> = lost.iter().map(loss).collect::<Option<_>>()?;
    Some(object_of(vec![
        ("files", Value::Array(references)),
        ("lost", Value::Array(losses)),
        ("skipped", skipped.clone()),
    ]))
}
