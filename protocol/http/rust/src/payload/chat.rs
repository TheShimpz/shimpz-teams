//! Chat-turn presentation values: history, Creator links, rendered text, Team names, and turn usage.

use std::sync::LazyLock;

use strict_json::Value;

use super::{
    CREATOR_LINK_PREFIXES, HELP_URL_RE, MAX_CONVERSATION_ENTRIES, MAX_CONVERSATION_TEXT_CHARS,
    MAX_CREATOR_LINK_CHARS, MAX_HELP_URL_CHARS, MAX_SNAPSHOT_SUMMARY_CHARS,
    MAX_STORED_INPUT_HELP_CHARS, MAX_TEAM_NAME_CHARS, SNAPSHOT_SUMMARY_FIELDS, SOURCE_DIGEST_RE,
    canonical_locale,
};
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{
    array, exact_object, field, has_exactly, object, object_of, string, whole,
};

/// The longest completed turn, in milliseconds.
pub const MAX_TURN_DURATION_MS: i64 = 86_400_000;
/// The most models one turn's usage lists.
pub const MAX_TURN_USAGE_MODELS: usize = 16;
/// The most tokens one model's usage counts.
pub const MAX_TURN_USAGE_TOKENS: i64 = 1_000_000_000;
/// A usage provider or model id.
pub static TURN_USAGE_ID_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[a-z0-9][a-z0-9._-]{0,63}\Z"));
/// The members of one model's usage, sorted.
pub const TURN_USAGE_MODEL_FIELDS: [&str; 4] =
    ["input_tokens", "model", "output_tokens", "provider"];

fn conversation_text(value: &Value) -> bool {
    string(value).is_some_and(|entry| {
        (1..=MAX_CONVERSATION_TEXT_CHARS).contains(&text::len(entry))
            && text::is_nfc(entry)
            && text::strip(entry) == entry
            && !entry.chars().any(|character| {
                let category = text::category(character);
                category.starts_with('C')
                    && category != "Cf"
                    && !matches!(character, '\n' | '\r' | '\t')
            })
    })
}

/// `canonical_conversation`: one exact window of committed presentation history.
#[must_use]
pub fn canonical_conversation(value: &Value) -> Option<Value> {
    let entries = array(value).filter(|entries| entries.len() <= MAX_CONVERSATION_ENTRIES)?;
    let mut canonical = Vec::with_capacity(entries.len());
    for entry in entries {
        let members = exact_object(entry, &["role", "text", "truncated"])?;
        let (role, entry_text, truncated) = (
            field(members, "role"),
            field(members, "text"),
            field(members, "truncated"),
        );
        let valid = string(role).is_some_and(|role| role == "user" || role == "assistant")
            && matches!(truncated, Value::Bool(_))
            && conversation_text(entry_text);
        if !valid {
            return None;
        }
        canonical.push(object_of(vec![
            ("role", role.clone()),
            ("text", entry_text.clone()),
            ("truncated", truncated.clone()),
        ]));
    }
    Some(Value::Array(canonical))
}

/// `canonical_help_url`: one exact Stored Input key page.
#[must_use]
pub fn canonical_help_url(value: &Value) -> Option<&str> {
    string(value).filter(|url| text::len(url) <= MAX_HELP_URL_CHARS && HELP_URL_RE.is_full(url))
}

/// `canonical_stored_input_help`: one Stored Input help text rendered in one interface language.
#[must_use]
pub fn canonical_stored_input_help(value: &Value) -> Option<&str> {
    rendered_text(value, value, MAX_STORED_INPUT_HELP_CHARS, false)
        .then(|| string(value))
        .flatten()
}

/// `canonical_creator_links`: zero to six Creator links in canonical display order.
#[must_use]
pub fn canonical_creator_links(value: &Value) -> Option<Value> {
    let members = object(value)?;
    if !members
        .iter()
        .all(|(kind, _)| CREATOR_LINK_PREFIXES.iter().any(|(known, _)| known == kind))
    {
        return None;
    }
    let mut links = Vec::new();
    for (kind, prefixes) in CREATOR_LINK_PREFIXES {
        let Some((_, url)) = members.iter().find(|(name, _)| name == kind) else {
            continue;
        };
        let valid = string(url).is_some_and(|link| {
            text::len(link) <= MAX_CREATOR_LINK_CHARS
                && canonical_help_url(url).is_some()
                && prefixes.iter().any(|prefix| link.starts_with(prefix))
        });
        if !valid {
            return None;
        }
        links.push((kind, url.clone()));
    }
    Some(object_of(links))
}

/// `canonical_pack_digest`: one `sha256:` language-pack digest.
#[must_use]
pub fn canonical_pack_digest(value: &Value) -> Option<&str> {
    string(value).filter(|digest| SOURCE_DIGEST_RE.is_full(digest))
}

/// `rendered_text`: a nullable field renders to null exactly when its reference is null; anything else is bounded
/// public text (trimmed, printable, NFC, 1 to `maximum` characters).
#[must_use]
pub fn rendered_text(value: &Value, reference: &Value, maximum: usize, nullable: bool) -> bool {
    if nullable && *reference == Value::Null {
        return *value == Value::Null;
    }
    string(value).is_some_and(|rendered| {
        text::strip(rendered) == rendered
            && (1..=maximum).contains(&text::len(rendered))
            && text::isprintable(rendered)
            && text::is_nfc(rendered)
    })
}

/// `canonical_snapshot_summary`: one Local snapshot's summary in one interface language.
#[must_use]
pub fn canonical_snapshot_summary(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &SNAPSHOT_SUMMARY_FIELDS)?;
    let summary = field(members, "summary");
    (canonical_locale(field(members, "locale")).is_some()
        && rendered_text(summary, summary, MAX_SNAPSHOT_SUMMARY_CHARS, false))
    .then_some(value)
}

/// `canonical_team_name`: 1 to 80 characters, trimmed, no ASCII control character.
#[must_use]
pub fn canonical_team_name(value: &Value) -> Option<&str> {
    string(value).filter(|name| {
        (1..=MAX_TEAM_NAME_CHARS).contains(&text::len(name))
            && text::strip(name) == *name
            && !name
                .chars()
                .any(|character| character < ' ' || character == '\u{7f}')
    })
}

/// `canonical_local_team_name`: the shared bounds, already NFC, and public text (no Other, line, or paragraph
/// separator character).
#[must_use]
pub fn canonical_local_team_name(value: &Value) -> Option<&str> {
    canonical_team_name(value).filter(|name| {
        text::is_nfc(name)
            && !name.chars().any(|character| {
                let category = text::category(character);
                category.starts_with('C') || category == "Zl" || category == "Zp"
            })
    })
}

fn usage_model(value: &Value) -> Option<(&str, &str)> {
    let members = exact_object(value, &TURN_USAGE_MODEL_FIELDS)?;
    let provider = string(field(members, "provider")).filter(|id| TURN_USAGE_ID_RE.is_full(id))?;
    let model = string(field(members, "model")).filter(|id| TURN_USAGE_ID_RE.is_full(id))?;
    (whole(field(members, "input_tokens"), 0, MAX_TURN_USAGE_TOKENS)
        && whole(field(members, "output_tokens"), 0, MAX_TURN_USAGE_TOKENS))
    .then_some((provider, model))
}

/// `canonical_turn_usage`: a duration and, per provider and model sorted and distinct, the tokens it reported.
#[must_use]
pub fn canonical_turn_usage(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["duration_ms", "models"])?;
    let duration = field(members, "duration_ms");
    let models = array(field(members, "models"))?;
    if !whole(duration, 0, MAX_TURN_DURATION_MS)
        || !(1..=MAX_TURN_USAGE_MODELS).contains(&models.len())
    {
        return None;
    }
    let keys: Vec<(&str, &str)> = models.iter().map(usage_model).collect::<Option<_>>()?;
    if !keys.windows(2).all(|pair| pair[0] < pair[1]) {
        return None;
    }
    Some(object_of(vec![
        ("duration_ms", duration.clone()),
        ("models", Value::Array(models.to_vec())),
    ]))
}

/// `canonical_run_usage`: exactly `{"duration_ms"}`, a Routine run's active duration.
#[must_use]
pub fn canonical_run_usage(value: &Value) -> Option<Value> {
    let members = object(value).filter(|members| has_exactly(members, &["duration_ms"]))?;
    let duration = field(members, "duration_ms");
    whole(duration, 0, MAX_TURN_DURATION_MS)
        .then(|| object_of(vec![("duration_ms", duration.clone())]))
}
