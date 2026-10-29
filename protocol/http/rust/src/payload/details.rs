//! A staged Local snapshot's inventory entry and an Assistant page in one interface language.

use std::sync::LazyLock;

use strict_json::Value;

use super::{
    MAX_SNAPSHOT_SUMMARY_CHARS, canonical_creator_links, canonical_help_url, canonical_locale,
    canonical_source_digest, canonical_stored_input_help, rendered_text,
};
use crate::identifiers::{canonical_assistant_id, canonical_identifier};
use crate::python::datetime::fromisoformat;
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{array, exact_object, field, has_exactly, object, string};

/// The members of a Local inventory entry, sorted.
pub const LOCAL_ASSISTANT_FIELDS: [&str; 14] = [
    "actions",
    "admission",
    "assistant_id",
    "assistant_version",
    "created_at",
    "declared_creators",
    "image_id",
    "integrations",
    "name",
    "platform",
    "provenance",
    "reason",
    "summary",
    "unpublished",
];
/// Team's admission verdicts, sorted.
pub const LOCAL_ASSISTANT_ADMISSIONS: [&str; 3] = ["admissible", "inadmissible", "unavailable"];
/// Why a snapshot is inadmissible, sorted.
pub const LOCAL_ASSISTANT_REASONS: [&str; 3] = [
    "declaration-invalid",
    "image-inconsistent",
    "labels-invalid",
];
/// The label projection a `labels-invalid` entry cannot carry: each of these members is null in it.
pub const LOCAL_ASSISTANT_LABEL_FIELDS: [&str; 7] = [
    "assistant_version",
    "name",
    "summary",
    "declared_creators",
    "actions",
    "integrations",
    "created_at",
];
/// The image platforms, sorted.
pub const LOCAL_ASSISTANT_PLATFORMS: [&str; 2] = ["linux/amd64", "linux/arm64"];
/// A self-declared creator handle on a staged image.
pub static LOCAL_ASSISTANT_CREATOR_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"@[a-z0-9][a-z0-9-]{1,30}[a-z0-9]\Z"));
/// A staged image's creation time, before it is parsed.
pub static LOCAL_ASSISTANT_CREATED_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[0-9TZ:+.-]{20,64}\Z"));
/// The most entries of the Local inventory.
pub const MAX_LOCAL_ASSISTANTS: usize = 50;
/// The most creators an entry lists.
pub const MAX_LOCAL_ASSISTANT_CREATORS: usize = 4;
/// The longest label text.
pub const MAX_LOCAL_ASSISTANT_TEXT_CHARS: usize = 80;
/// The members of an Assistant page, sorted.
pub const ASSISTANT_DETAILS_FIELDS: [&str; 11] = [
    "actions",
    "assistant_id",
    "assistant_version",
    "creators",
    "description",
    "integrations",
    "links",
    "locale",
    "name",
    "stored_inputs",
    "summary",
];
/// An Assistant version.
pub static ASSISTANT_VERSION_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"));
/// A creator handle on an Assistant page.
pub static DETAILS_CREATOR_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z"));
/// An Assistant's public GitHub repository, exactly the Developers manifest schema's pattern.
pub const GITHUB_REPOSITORY_PATTERN: &str = concat!(
    r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/",
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$",
);
/// [`GITHUB_REPOSITORY_PATTERN`] compiled.
pub static GITHUB_REPOSITORY_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(GITHUB_REPOSITORY_PATTERN));
/// The Action effects, sorted.
pub const ACTION_EFFECTS: [&str; 2] = ["mutating", "read_only"];
/// The longest Assistant name on its page.
pub const MAX_DETAILS_NAME_CHARS: usize = 80;
/// The longest description on an Assistant page.
pub const MAX_DETAILS_DESCRIPTION_CHARS: usize = 500;
/// The longest Action description or Stored Input label.
pub const MAX_DETAILS_LINE_CHARS: usize = 120;
/// The most creators of an Assistant page.
pub const MAX_DETAILS_CREATORS: usize = 16;
/// The most Actions of an Assistant page.
pub const MAX_DETAILS_ACTIONS: usize = 128;
/// The most Integrations of an Assistant page.
pub const MAX_DETAILS_INTEGRATIONS: usize = 16;
/// The most Stored Inputs of an Assistant page.
pub const MAX_DETAILS_STORED_INPUTS: usize = 8;

/// `canonical_local_assistant`: one staged Local snapshot's inventory entry.
#[must_use]
pub fn canonical_local_assistant(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &LOCAL_ASSISTANT_FIELDS)?;
    let get = |key| field(members, key);
    let identity = canonical_assistant_id(get("assistant_id")).is_some()
        && canonical_source_digest(get("image_id")).is_some()
        && string(get("platform"))
            .is_some_and(|platform| LOCAL_ASSISTANT_PLATFORMS.contains(&platform))
        && string(get("provenance")) == Some("local")
        && *get("unpublished") == Value::Bool(true)
        && local_admission(get("admission"), get("reason"));
    if !identity {
        return None;
    }
    if string(get("reason")) == Some("labels-invalid") {
        return LOCAL_ASSISTANT_LABEL_FIELDS
            .iter()
            .all(|key| *get(key) == Value::Null)
            .then_some(value);
    }
    local_labels(members).then_some(value)
}

fn local_admission(admission: &Value, reason: &Value) -> bool {
    if string(admission) == Some("inadmissible") {
        return string(reason).is_some_and(|reason| LOCAL_ASSISTANT_REASONS.contains(&reason));
    }
    string(admission).is_some_and(|admission| LOCAL_ASSISTANT_ADMISSIONS.contains(&admission))
        && *reason == Value::Null
}

fn local_labels(members: &[(String, Value)]) -> bool {
    let get = |key| field(members, key);
    ASSISTANT_VERSION_RE.is_full_value(get("assistant_version"))
        && label_text(get("name"))
        && label_text(get("summary"))
        && array(get("declared_creators")).is_some_and(|creators| {
            (1..=MAX_LOCAL_ASSISTANT_CREATORS).contains(&creators.len())
                && creators
                    .iter()
                    .all(|creator| LOCAL_ASSISTANT_CREATOR_RE.is_full_value(creator))
                && distinct(creators)
        })
        && sorted_identifiers(get("actions"), 1, MAX_DETAILS_ACTIONS)
        && sorted_identifiers(get("integrations"), 0, MAX_DETAILS_INTEGRATIONS)
        && canonical_created_at(get("created_at")).is_some()
}

/// `canonical_created_at`: a staged image's Docker creation time, a bounded, real, zone-qualified ISO 8601 instant.
#[must_use]
pub fn canonical_created_at(value: &Value) -> Option<&str> {
    string(value).filter(|created| {
        LOCAL_ASSISTANT_CREATED_RE.is_full(created)
            && fromisoformat(created).is_ok_and(|parsed| parsed.offset.is_some())
    })
}

fn label_text(value: &Value) -> bool {
    string(value).is_some_and(|label| {
        (1..=MAX_LOCAL_ASSISTANT_TEXT_CHARS).contains(&text::len(label))
            && text::strip(label) == label
            && !label
                .chars()
                .any(|character| character < ' ' || character == '\u{7f}')
    })
}

/// Distinct strings (`len(set(items)) == len(items)` over strings).
fn distinct(items: &[Value]) -> bool {
    items
        .iter()
        .enumerate()
        .all(|(index, item)| !items[..index].contains(item))
}

/// Strings in strictly increasing code point order (`items == sorted(set(items))`).
fn increasing(items: &[&str]) -> bool {
    items.windows(2).all(|pair| pair[0] < pair[1])
}

fn sorted_identifiers(value: &Value, minimum: usize, maximum: usize) -> bool {
    array(value).is_some_and(|items| {
        (minimum..=maximum).contains(&items.len())
            && items
                .iter()
                .map(canonical_identifier)
                .collect::<Option<Vec<_>>>()
                .is_some_and(|identifiers| increasing(&identifiers))
    })
}

/// `canonical_assistant_details`: one exact Assistant page in one interface language.
#[must_use]
pub fn canonical_assistant_details(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &ASSISTANT_DETAILS_FIELDS)?;
    let get = |key| field(members, key);
    let identity = canonical_locale(get("locale")).is_some()
        && canonical_assistant_id(get("assistant_id")).is_some()
        && ASSISTANT_VERSION_RE.is_full_value(get("assistant_version"))
        && details_creators(get("creators"))
        && canonical_creator_links(get("links")).is_some();
    let texts = [
        (get("name"), MAX_DETAILS_NAME_CHARS),
        (get("summary"), MAX_SNAPSHOT_SUMMARY_CHARS),
        (get("description"), MAX_DETAILS_DESCRIPTION_CHARS),
    ];
    (identity
        && texts
            .iter()
            .all(|(text, maximum)| rendered_text(text, text, *maximum, false))
        && details_capabilities(members))
    .then_some(value)
}

fn details_creators(value: &Value) -> bool {
    array(value).is_some_and(|creators| {
        (1..=MAX_DETAILS_CREATORS).contains(&creators.len())
            && creators
                .iter()
                .all(|creator| DETAILS_CREATOR_RE.is_full_value(creator))
            && distinct(creators)
    })
}

fn details_capabilities(members: &[(String, Value)]) -> bool {
    let get = |key| field(members, key);
    let line = |value: &Value| rendered_text(value, value, MAX_DETAILS_LINE_CHARS, false);
    details_items(
        get("actions"),
        1,
        MAX_DETAILS_ACTIONS,
        &["description", "effect", "id"],
    )
    .is_some_and(|actions| {
        actions.iter().all(|action| {
            string(field(action, "effect")).is_some_and(|effect| ACTION_EFFECTS.contains(&effect))
                && line(field(action, "description"))
        })
    }) && details_items(
        get("integrations"),
        0,
        MAX_DETAILS_INTEGRATIONS,
        &["id", "provider"],
    )
    .is_some_and(|items| {
        items
            .iter()
            .all(|item| canonical_identifier(field(item, "provider")).is_some())
    }) && details_items(
        get("stored_inputs"),
        0,
        MAX_DETAILS_STORED_INPUTS,
        &["description", "help_url", "id", "label"],
    )
    .is_some_and(|items| {
        items.iter().all(|item| {
            line(field(item, "label"))
                && canonical_stored_input_help(field(item, "description")).is_some()
                && canonical_help_url(field(item, "help_url")).is_some()
        })
    })
}

/// A bounded list of objects with exactly `keys`, sorted by unique identifier `id`.
fn details_items<'a>(
    value: &'a Value,
    minimum: usize,
    maximum: usize,
    keys: &[&str],
) -> Option<Vec<&'a [(String, Value)]>> {
    let items = array(value).filter(|items| (minimum..=maximum).contains(&items.len()))?;
    let objects: Vec<&[(String, Value)]> = items
        .iter()
        .map(|item| object(item).filter(|members| has_exactly(members, keys)))
        .collect::<Option<_>>()?;
    let identifiers: Vec<&str> = objects
        .iter()
        .map(|members| canonical_identifier(field(members, "id")))
        .collect::<Option<_>>()?;
    increasing(&identifiers).then_some(objects)
}
