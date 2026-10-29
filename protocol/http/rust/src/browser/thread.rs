//! A Browser task's thread, oldest first: folded step records, the agent's task memory, and unfolded steps, with the
//! byte measures Team and the Brain bound a step request by.

use strict_json::Value;

use super::{
    ENTRY_OVERHEAD_BYTES, MAX_FOLDED_BYTES, MAX_HISTORY_BYTES, MAX_MEMORY_BYTES,
    MAX_OBSERVATION_BYTES, MAX_STEPS, MAX_TITLE_CHARS, MAX_URL_CHARS, OUTCOMES,
    canonical_downloads, canonical_step, plain,
};
use crate::python::json::{Options, encoded_len};
use crate::python::text;
use crate::python::value::{exact_object, field, is_bool, object_of, py_eq, small_int, string};

/// `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`.
const COMPACT: Options = Options {
    ensure_ascii: false,
    sort_keys: false,
    compact: true,
};

/// `_bytes`: the UTF-8 length of a value's compact JSON.
fn json_bytes(value: &Value) -> usize {
    encoded_len(value, COMPACT)
}

/// The UTF-8 length of a string member of a canonical record, entry, or thread.
fn text_bytes(value: &Value) -> usize {
    string(value).map_or(0, str::len)
}

/// The URL and title of the page a step was chosen on: one bounded printable line each.
fn page(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["url", "title"])?;
    let url = string(field(members, "url"))
        .filter(|url| text::len(url) <= MAX_URL_CHARS && text::isprintable(url))?;
    let title = string(field(members, "title"))
        .filter(|title| text::len(title) <= MAX_TITLE_CHARS && text::isprintable(title))?;
    Some(object_of(vec![
        ("url", Value::String(url.to_owned())),
        ("title", Value::String(title.to_owned())),
    ]))
}

/// `canonical_record`: one earlier step, its closed outcome, and the page it was chosen on.
#[must_use]
pub fn canonical_record(value: &Value) -> Option<Value> {
    record_members(value).map(Value::Object)
}

fn record_members(value: &Value) -> Option<Vec<(String, Value)>> {
    let members = exact_object(value, &["step", "outcome", "page"])?;
    let outcome = string(field(members, "outcome")).filter(|outcome| OUTCOMES.contains(outcome))?;
    let step = canonical_step(field(members, "step"))?;
    let page = page(field(members, "page"))?;
    let kind = step.get("kind").and_then(string);
    if !py_eq(&step, field(members, "step")) || matches!(kind, Some("done" | "needs_person")) {
        return None;
    }
    Some(vec![
        ("step".to_owned(), step),
        ("outcome".to_owned(), Value::String(outcome.to_owned())),
        ("page".to_owned(), page),
    ])
}

/// One unfolded step: its record, the observation it was chosen on, the downloads, and whether a screenshot came.
fn entry(value: &Value) -> Option<Value> {
    let members = exact_object(
        value,
        &["step", "outcome", "page", "text", "downloads", "screenshot"],
    )?;
    let mut entry = record_members(&object_of(vec![
        ("step", field(members, "step").clone()),
        ("outcome", field(members, "outcome").clone()),
        ("page", field(members, "page").clone()),
    ]))?;
    let downloads = field(members, "downloads");
    let downloads_admitted = *downloads == Value::Null
        || canonical_downloads(downloads).is_some_and(|canonical| py_eq(&canonical, downloads));
    if canonical_observation(field(members, "text")).is_none()
        || !downloads_admitted
        || !is_bool(field(members, "screenshot"))
    {
        return None;
    }
    for key in ["text", "downloads", "screenshot"] {
        entry.push((key.to_owned(), field(members, key).clone()));
    }
    Some(Value::Object(entry))
}

/// `entry_bytes`: at most what one canonical unfolded step renders to.
#[must_use]
pub fn entry_bytes(entry: &Value) -> usize {
    let page = entry.get("page").unwrap_or(&Value::Null);
    text_bytes(entry.get("text").unwrap_or(&Value::Null))
        + json_bytes(entry.get("step").unwrap_or(&Value::Null))
        + text_bytes(page.get("url").unwrap_or(&Value::Null))
        + text_bytes(page.get("title").unwrap_or(&Value::Null))
        + json_bytes(entry.get("downloads").unwrap_or(&Value::Null))
        + ENTRY_OVERHEAD_BYTES
}

/// `record_bytes`: at most what one folded step renders to, its whole record.
#[must_use]
pub fn record_bytes(record: &Value) -> usize {
    json_bytes(record) + 1
}

fn items<'a>(thread: &'a Value, key: &str) -> &'a [Value] {
    match thread.get(key) {
        Some(Value::Array(items)) => items,
        _ => &[],
    }
}

/// `history_bytes`: at most what a canonical thread's history renders to.
#[must_use]
pub fn history_bytes(thread: &Value) -> usize {
    let folded: usize = items(thread, "folded").iter().map(record_bytes).sum();
    let memory = text_bytes(thread.get("memory").unwrap_or(&Value::Null));
    ENTRY_OVERHEAD_BYTES
        + folded
        + memory
        + items(thread, "steps")
            .iter()
            .map(entry_bytes)
            .sum::<usize>()
}

/// `request_text_bytes`: at most the text a step request renders to.
#[must_use]
pub fn request_text_bytes(
    objective: &str,
    thread: &Value,
    observation: &str,
    downloads: &Value,
) -> usize {
    objective.len()
        + history_bytes(thread)
        + observation.len()
        + json_bytes(downloads)
        + ENTRY_OVERHEAD_BYTES
}

fn count(value: &Value) -> bool {
    let limit = i64::try_from(MAX_STEPS * MAX_STEPS).unwrap_or(i64::MAX);
    small_int(value).is_some_and(|count| (0..=limit).contains(&count))
}

/// `canonical_thread`: a task's thread within its bounds, each part canonical, or `None`.
#[must_use]
pub fn canonical_thread(value: &Value) -> Option<Value> {
    let members = exact_object(
        value,
        &["folded", "folded_omitted", "memory", "unnoted", "steps"],
    )?;
    let (Value::Array(folded), Value::Array(steps)) =
        (field(members, "folded"), field(members, "steps"))
    else {
        return None;
    };
    let memory = string(field(members, "memory"))?;
    if folded.len() + steps.len() > MAX_STEPS
        || !count(field(members, "folded_omitted"))
        || !count(field(members, "unnoted"))
        || (!memory.is_empty() && canonical_memory(field(members, "memory")) != Some(memory))
    {
        return None;
    }
    let records: Vec<Value> = folded.iter().map(canonical_record).collect::<Option<_>>()?;
    let entries: Vec<Value> = steps.iter().map(entry).collect::<Option<_>>()?;
    let folded_bytes: usize = records.iter().map(record_bytes).sum();
    let thread = object_of(vec![
        ("folded", Value::Array(records)),
        ("folded_omitted", field(members, "folded_omitted").clone()),
        ("memory", Value::String(memory.to_owned())),
        ("unnoted", field(members, "unnoted").clone()),
        ("steps", Value::Array(entries)),
    ]);
    (folded_bytes <= MAX_FOLDED_BYTES && history_bytes(&thread) <= MAX_HISTORY_BYTES)
        .then_some(thread)
}

/// `canonical_memory`: the agent's task memory, plain text within its bound, or `None`.
#[must_use]
pub fn canonical_memory(value: &Value) -> Option<&str> {
    plain(value, MAX_MEMORY_BYTES, true).filter(|memory| memory.len() <= MAX_MEMORY_BYTES)
}

/// `canonical_observation`: the bounded page text Team observed.
#[must_use]
pub fn canonical_observation(value: &Value) -> Option<&str> {
    string(value).filter(|text| text.len() <= MAX_OBSERVATION_BYTES)
}
