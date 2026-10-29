//! The presentation a Team human-required challenge carries beside its canonical request (`challenge.py`).
//!
//! The rendered copy of the request's catalog references, the one file an authorization discloses, and an
//! authorization's projection of the validated Action input: display values a client shows and validates, never part
//! of the request's fingerprint.

use std::cmp::Ordering;

use strict_json::Value;

use crate::payload::{
    SHA256_RE, canonical_file_id, canonical_filename, canonical_media_type, integer, rendered_text,
};
use crate::python::text;
use crate::python::value::{array, exact_object, field, natural_cmp, object, small_int, string};

/// The rendered copy bound of each request field, in the order a challenge lists them.
pub const RENDERED_FIELD_CHARS: [(&str, usize); 4] = [
    ("title", 80),
    ("description", 500),
    ("label", 80),
    ("placeholder", 120),
];
/// The rendered copy bound of each choice option field.
pub const RENDERED_OPTION_CHARS: [(&str, usize); 2] = [("label", 80), ("description", 160)];

fn member<'a>(members: &'a [(String, Value)], key: &str) -> Option<&'a Value> {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, member)| member)
}

/// `canonical_rendered`: the rendered copy of exactly the canonical request's copy fields, or `None`.
///
/// The block carries only display text in the challenge's interface language, in the request's field and option
/// order, within each field's bound.
#[must_use]
pub fn canonical_rendered<'a>(value: &'a Value, request: &Value) -> Option<&'a Value> {
    let rendered = object(value)?;
    let request = object(request)?;
    let fields: Vec<(&str, usize)> = RENDERED_FIELD_CHARS
        .into_iter()
        .filter(|(name, _)| member(request, name).is_some())
        .collect();
    let options = member(request, "options");
    let expected = fields.len() + usize::from(options.is_some());
    let exact = rendered.len() == expected
        && fields
            .iter()
            .all(|(name, _)| member(rendered, name).is_some())
        && options.is_none_or(|_| member(rendered, "options").is_some());
    if !exact
        || !fields.iter().all(|(name, maximum)| {
            rendered_text(
                field(rendered, name),
                field(request, name),
                *maximum,
                *name == "placeholder",
            )
        })
    {
        return None;
    }
    if let Some(options) = options
        && !rendered_options(field(rendered, "options"), options)
    {
        return None;
    }
    Some(value)
}

fn rendered_options(values: &Value, options: &Value) -> bool {
    let (Some(values), Some(options)) = (array(values), array(options)) else {
        return false;
    };
    values.len() == options.len()
        && values
            .iter()
            .zip(options)
            .all(|(item, option)| rendered_option(item, option))
}

fn rendered_option(item: &Value, option: &Value) -> bool {
    let (Some(item), Some(option)) = (
        exact_object(item, &["label", "description"]),
        object(option),
    ) else {
        return false;
    };
    let reference = |key| member(option, key).unwrap_or(&Value::Null);
    rendered_text(
        field(item, "label"),
        reference("label"),
        RENDERED_OPTION_CHARS[0].1,
        false,
    ) && rendered_text(
        field(item, "description"),
        reference("description"),
        RENDERED_OPTION_CHARS[1].1,
        true,
    )
}

/// The largest original an Action may receive.
pub const MAX_ACTION_FILE_BYTES: i64 = 8 * 1024 * 1024;
/// The members of a disclosed file, in the order a disclosure lists them.
pub const FILE_DISCLOSURE_KEYS: [&str; 5] = ["id", "name", "media_type", "size", "sha256"];

/// `canonical_file_disclosure`: the one file an authorization challenge discloses, or `None`.
///
/// Its opaque id, literal filename, Team-determined media type, size, and original SHA-256 name exactly the selected
/// file whose original bytes the approved replay delivers to the Action.
#[must_use]
pub fn canonical_file_disclosure(value: &Value) -> Option<Value> {
    let members = exact_object(value, &FILE_DISCLOSURE_KEYS)?;
    let media_type = string(field(members, "media_type"))?;
    let size = integer(field(members, "size"), 1)?;
    let admitted = canonical_file_id(field(members, "id")).is_some()
        && canonical_filename(field(members, "name")).is_some()
        && canonical_media_type(field(members, "media_type")).as_deref() == Some(media_type)
        && natural_cmp(&size, &MAX_ACTION_FILE_BYTES.to_string()) != Ordering::Greater
        && SHA256_RE.is_full_value(field(members, "sha256"));
    admitted.then(|| {
        Value::Object(
            FILE_DISCLOSURE_KEYS
                .iter()
                .map(|key| ((*key).to_owned(), field(members, key).clone()))
                .collect(),
        )
    })
}

/// The members of an input projection.
pub const INPUT_PROJECTION_KEYS: [&str; 2] = ["fields", "omitted"];
/// The members of one projected argument.
pub const INPUT_PROJECTION_FIELD_KEYS: [&str; 3] = ["name", "value", "truncated"];
/// The most projected arguments.
pub const INPUT_PROJECTION_MAX_FIELDS: usize = 16;
/// The longest projected argument name.
pub const INPUT_PROJECTION_NAME_CHARS: usize = 128;
/// The longest projected argument value.
pub const INPUT_PROJECTION_VALUE_CHARS: usize = 400;
/// The most arguments past the projected rows.
pub const INPUT_PROJECTION_MAX_OMITTED: i64 = 4096;

/// `canonical_input_projection`: one authorization challenge's projection of the validated Action input, or `None`.
///
/// `fields` holds at most 16 rows in strictly ascending `name` order, each name and escaped JSON value printable and
/// within its bound, and `omitted` counts the arguments past the sixteenth row.
#[must_use]
pub fn canonical_input_projection(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &INPUT_PROJECTION_KEYS)?;
    let fields = array(field(members, "fields"))?;
    let omitted = small_int(field(members, "omitted"))
        .filter(|omitted| (0..=INPUT_PROJECTION_MAX_OMITTED).contains(omitted))?;
    if fields.len() > INPUT_PROJECTION_MAX_FIELDS
        || (omitted != 0 && fields.len() != INPUT_PROJECTION_MAX_FIELDS)
    {
        return None;
    }
    let mut previous: Option<&str> = None;
    for row in fields {
        let name = projected_row(row)?;
        if previous.is_some_and(|previous| name <= previous) {
            return None;
        }
        previous = Some(name);
    }
    Some(value)
}

/// One row's name, when the row is exactly a printable bounded name, value, and truncation flag.
fn projected_row(row: &Value) -> Option<&str> {
    let members = exact_object(row, &INPUT_PROJECTION_FIELD_KEYS)?;
    let name = projected_text(field(members, "name"), INPUT_PROJECTION_NAME_CHARS)?;
    projected_text(field(members, "value"), INPUT_PROJECTION_VALUE_CHARS)?;
    matches!(field(members, "truncated"), Value::Bool(_)).then_some(name)
}

fn projected_text(value: &Value, maximum: usize) -> Option<&str> {
    string(value).filter(|text| (1..=maximum).contains(&text::len(text)) && text::isprintable(text))
}
