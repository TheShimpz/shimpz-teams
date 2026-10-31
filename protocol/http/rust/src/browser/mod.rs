//! The Browser agent's closed step set and the `browser-step` exchange Team and the Brain share (ADR-0117).
//!
//! The agent answers one step at a time from the closed set, and both sides admit a step only in its canonical form;
//! the model answers through the flat [`step_schema`], every field present and the unused ones null, which
//! [`canonical_step`] turns into the compact step for its kind or refuses. `thread` holds the task's thread and the
//! byte measures both sides bound a request by; `files` the downloads, the completeness check, and a chat terminal's
//! `browser` field.

mod files;
mod thread;

use std::sync::LazyLock;

use strict_json::{Number, Value};

use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{object, object_of, string};
use crate::websocket::authority;

pub use files::{
    COMPLETENESS, MAX_DOWNLOAD_NAMES, MAX_NAME_BYTES, MAX_PENDING_DOWNLOADS, MAX_TERMINAL_FILES,
    MAX_TERMINAL_LOSSES, MAX_TERMINAL_SKIPPED, MAX_UNSCREENED_URLS, TERMINAL_LOSSES,
    canonical_check, canonical_downloads, canonical_file_reference, canonical_terminal,
};
pub use thread::{
    canonical_memory, canonical_observation, canonical_record, canonical_thread, entry_bytes,
    history_bytes, record_bytes, request_text_bytes,
};

/// The longest objective.
pub const MAX_OBJECTIVE_CHARS: usize = 2_000;
/// The longest done summary, in UTF-8 bytes (and characters).
pub const MAX_SUMMARY_BYTES: usize = 4 * 1024;
/// The longest sentence a needs-person step or an incomplete check carries.
pub const MAX_SENTENCE_CHARS: usize = 300;
/// The longest text a type step types.
pub const MAX_TEXT_CHARS: usize = 2_000;
/// The longest URL.
pub const MAX_URL_CHARS: usize = 2_048;
/// The longest note of a step.
pub const MAX_NOTE_CHARS: usize = 300;
/// The longest page title.
pub const MAX_TITLE_CHARS: usize = 300;
/// The most page text one observation carries, in UTF-8 bytes.
pub const MAX_OBSERVATION_BYTES: usize = 24 * 1024;
/// A task's step ceiling.
pub const MAX_STEPS: usize = 300;
/// A task's one absolute deadline.
pub const MAX_TASK_SECONDS: u64 = 30 * 60;
/// The most links one read step opens.
pub const MAX_READ_REFS: usize = 10;
/// The most a thread's history renders to.
pub const MAX_HISTORY_BYTES: usize = 128 * 1024;
/// The longest task memory, in UTF-8 bytes.
pub const MAX_MEMORY_BYTES: usize = 12 * 1024;
/// The longest folded step line.
pub const MAX_FOLDED_LINE_BYTES: usize = 300;
/// The most the folded step records render to.
pub const MAX_FOLDED_BYTES: usize = 8 * 1024;
/// What each thread entry's rendering adds to its fields' bytes, at most.
pub const ENTRY_OVERHEAD_BYTES: usize = 512;
/// The whole text a request renders to.
pub const MAX_REQUEST_TEXT_BYTES: usize = 172 * 1024;
/// The provider attempts one Brain call of a Browser task may make.
pub const MAX_CALL_ATTEMPTS: u32 = 3;
/// The input tokens one Brain call admits.
pub const MAX_CALL_INPUT_TOKENS: u32 = 200_000;
/// The output tokens one Brain call lets a model write.
pub const MAX_CALL_OUTPUT_TOKENS: u32 = 4_096;
/// The languages a Browser's pages follow, sorted.
pub const LANGUAGES: [&str; 2] = ["en", "pt-BR"];
/// Why a step needs a person.
pub const REASONS: [&str; 5] = ["sign-in", "captcha", "payment", "confirmation", "blocked"];
/// A scroll step's directions.
pub const DIRECTIONS: [&str; 2] = ["up", "down"];
/// A step's outcomes.
pub const OUTCOMES: [&str; 5] = [
    "ok",
    "refused",
    "navigation-failed",
    "interrupted",
    "returned",
];
/// A ref: a page number and an element number.
pub const REF_PATTERN: &str = r"p[0-9]{1,6}:[0-9]{1,6}";
/// Every field each step kind carries besides `kind`, in Python's order.
pub const FIELDS: [(&str, &[&str]); 9] = [
    ("navigate", &["url"]),
    ("click", &["ref"]),
    ("type", &["ref", "text"]),
    ("scroll", &["direction"]),
    ("wait", &[]),
    ("screenshot", &[]),
    ("read", &["refs"]),
    ("done", &["summary"]),
    ("needs_person", &["reason", "sentence"]),
];
/// Every field a flat step answer carries besides `kind` and `note`.
pub const ALL_FIELDS: [&str; 8] = [
    "url",
    "ref",
    "refs",
    "text",
    "direction",
    "summary",
    "reason",
    "sentence",
];

/// [`REF_PATTERN`] compiled.
pub static REF_RE: LazyLock<Pattern> = LazyLock::new(|| declared(REF_PATTERN));

fn int(value: usize) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn strings(values: &[&str]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|value| Value::String((*value).to_owned()))
            .collect(),
    )
}

fn bounded_string(maximum: usize) -> Value {
    object_of(vec![
        ("type", Value::String("string".to_owned())),
        ("maxLength", int(maximum)),
    ])
}

fn closed_string(choices: &[&str]) -> Value {
    object_of(vec![
        ("type", Value::String("string".to_owned())),
        ("enum", strings(choices)),
    ])
}

fn nullable(schema: Value) -> Value {
    let null = object_of(vec![("type", Value::String("null".to_owned()))]);
    object_of(vec![("anyOf", Value::Array(vec![schema, null]))])
}

/// `STEP_SCHEMA`: the flat schema the model answers a step through.
pub static STEP_SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    let kinds: Vec<&str> = FIELDS.iter().map(|(kind, _)| *kind).collect();
    let refs = object_of(vec![
        ("type", Value::String("array".to_owned())),
        ("maxItems", int(MAX_READ_REFS)),
        ("items", bounded_string(16)),
    ]);
    let required: Vec<&str> = ["kind"]
        .into_iter()
        .chain(ALL_FIELDS)
        .chain(["note"])
        .collect();
    object_of(vec![
        ("type", Value::String("object".to_owned())),
        ("additionalProperties", Value::Bool(false)),
        ("required", strings(&required)),
        (
            "properties",
            object_of(vec![
                ("kind", closed_string(&kinds)),
                ("url", nullable(bounded_string(MAX_URL_CHARS))),
                ("ref", nullable(bounded_string(16))),
                ("refs", nullable(refs)),
                ("text", nullable(bounded_string(MAX_TEXT_CHARS))),
                ("direction", nullable(closed_string(&DIRECTIONS))),
                ("summary", nullable(bounded_string(MAX_SUMMARY_BYTES))),
                ("reason", nullable(closed_string(&REASONS))),
                ("sentence", nullable(bounded_string(MAX_SENTENCE_CHARS))),
                ("note", nullable(bounded_string(MAX_NOTE_CHARS))),
            ]),
        ),
    ])
});

/// `STEP_SCHEMA`.
#[must_use]
pub fn step_schema() -> &'static Value {
    &STEP_SCHEMA
}

/// `_plain`: non-blank text within `limit` characters, every character printable (or a newline or tab when
/// `multiline`).
fn plain(value: &Value, limit: usize, multiline: bool) -> Option<&str> {
    string(value).filter(|text| {
        !text::strip(text).is_empty()
            && text::len(text) <= limit
            && text.chars().all(|character| {
                text::isprintable_char(character) || (multiline && matches!(character, '\n' | '\t'))
            })
    })
}

/// `web_authority`: the lowercase scheme and the authority of an http(s) URL whose authority is ASCII without
/// brackets, else `None`. The scheme reads in any ASCII case; the authority runs up to the first `/`, `?`, or `#`.
#[must_use]
pub fn web_authority(url: &str) -> Option<(&'static str, &str)> {
    let starts = |prefix: &str| {
        url.get(..prefix.len())
            .is_some_and(|head| head.eq_ignore_ascii_case(prefix))
    };
    let (scheme, rest) = if starts("https://") {
        ("https", &url[8..])
    } else if starts("http://") {
        ("http", &url[7..])
    } else {
        return None;
    };
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or_default();
    (netloc.is_ascii() && !netloc.contains(['[', ']'])).then_some((scheme, netloc))
}

/// `canonical_url`: an absolute `https` or `http` URL with an ASCII host and no credentials.
#[must_use]
pub fn canonical_url(value: &Value) -> Option<&str> {
    let url = string(value)?;
    if url.is_empty() || text::len(url) > MAX_URL_CHARS || !text::isprintable(url) {
        return None;
    }
    let (_, netloc) = web_authority(url)?;
    let parts = authority(netloc);
    let credentials = parts.userinfo.is_some_and(|(name, password)| {
        !name.is_empty() || password.is_some_and(|text| !text.is_empty())
    });
    (!parts.host.is_empty() && !credentials && !url.contains(' ')).then_some(url)
}

/// `canonical_objective`: a self-contained task objective of at most 2,000 characters, stripped.
#[must_use]
pub fn canonical_objective(value: &Value) -> Option<&str> {
    plain(value, MAX_OBJECTIVE_CHARS, true).map(text::strip)
}

fn reference(value: &Value) -> Option<Value> {
    string(value)
        .filter(|reference| REF_RE.is_full(reference))
        .map(|reference| Value::String(reference.to_owned()))
}

/// One to ten distinct refs, in the order the step names them.
fn references(value: &Value) -> Option<Value> {
    let Value::Array(items) = value else {
        return None;
    };
    let named: Vec<&str> = items
        .iter()
        .filter_map(|item| string(item).filter(|reference| REF_RE.is_full(reference)))
        .collect();
    let distinct = named
        .iter()
        .enumerate()
        .all(|(index, reference)| !named[..index].contains(reference));
    ((1..=MAX_READ_REFS).contains(&items.len()) && named.len() == items.len() && distinct)
        .then(|| value.clone())
}

fn typed_text(value: &Value) -> Option<Value> {
    string(value)
        .filter(|text| text::len(text) <= MAX_TEXT_CHARS && text::isprintable(text))
        .map(|text| Value::String(text.to_owned()))
}

fn summary(value: &Value) -> Option<Value> {
    plain(value, MAX_SUMMARY_BYTES, true)
        .filter(|text| text.len() <= MAX_SUMMARY_BYTES)
        .map(|text| Value::String(text.to_owned()))
}

fn sentence(value: &Value) -> Option<&str> {
    plain(value, MAX_SENTENCE_CHARS, false)
}

fn choice(value: &Value, choices: &[&str]) -> Option<Value> {
    string(value)
        .filter(|text| choices.contains(text))
        .map(|text| Value::String(text.to_owned()))
}

/// `FIELD_CHECKS[name](value)`: one field of a step admitted, or `None`.
#[must_use]
pub fn field_check(name: &str, value: &Value) -> Option<Value> {
    match name {
        "url" => canonical_url(value).map(|url| Value::String(url.to_owned())),
        "ref" => reference(value),
        "refs" => references(value),
        "text" => typed_text(value),
        "direction" => choice(value, &DIRECTIONS),
        "summary" => summary(value),
        "reason" => choice(value, &REASONS),
        "sentence" => sentence(value).map(|text| Value::String(text.to_owned())),
        _ => None,
    }
}

/// `canonical_step`: the compact step a flat or compact answer names, or `None` outside the closed set.
///
/// Any step may carry a `note`; a null or blank one is no note.
#[must_use]
pub fn canonical_step(value: &Value) -> Option<Value> {
    let members = object(value)?;
    let kind = value.get("kind").and_then(string)?;
    let fields = FIELDS.iter().find(|(name, _)| *name == kind)?.1;
    let known = |key: &str| key == "kind" || key == "note" || ALL_FIELDS.contains(&key);
    let unused = ALL_FIELDS.iter().any(|name| {
        !fields.contains(name) && value.get(name).is_some_and(|field| *field != Value::Null)
    });
    if members.iter().any(|(key, _)| !known(key)) || unused {
        return None;
    }
    let mut step = vec![("kind".to_owned(), Value::String(kind.to_owned()))];
    for name in fields {
        let admitted = field_check(name, value.get(name).unwrap_or(&Value::Null))?;
        step.push(((*name).to_owned(), admitted));
    }
    match value.get("note") {
        None | Some(Value::Null) => {}
        Some(Value::String(note)) if text::strip(note).is_empty() => {}
        Some(note) => {
            let note = plain(note, MAX_NOTE_CHARS, false)?;
            step.push(("note".to_owned(), Value::String(note.to_owned())));
        }
    }
    Some(Value::Object(step))
}

#[cfg(test)]
mod tests;
