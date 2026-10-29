//! The Brain's multiple-choice clarification, how Admin composes its answer, and the Team's learned memory.

use strict_json::Value;

use super::MAX_CLARIFICATION_DESCRIPTION_CHARS;
use crate::python::text;
use crate::python::value::{array, exact_object, field, object_of, small_int, string};
use crate::turn::{
    MAX_CLARIFICATION_LABEL_CHARS, MAX_CLARIFICATION_OPTIONS, MAX_CLARIFICATION_QUESTION_CHARS,
    MAX_MEMORIES, MAX_MEMORY_CHANGES, MAX_MEMORY_PREFERENCE_CHARS, MEMORY_TOPIC_RE,
    MIN_CLARIFICATION_OPTIONS,
};

/// The labels Admin composes a clarified request with, in each interface language: `(locale, question, answer)`.
pub const CLARIFICATION_LABELS: [(&str, &str, &str); 8] = [
    ("ar", "السؤال", "الإجابة"),
    ("de", "Frage", "Antwort"),
    ("en", "Question", "Answer"),
    ("es", "Pregunta", "Respuesta"),
    ("fr", "Question", "Réponse"),
    ("ja", "質問", "回答"),
    ("pt", "Pergunta", "Resposta"),
    ("zh", "问题", "回答"),
];

/// `_clarification_text`: NFC, trimmed, within `maximum`, non-empty unless `empty`, and free of Other, line, and
/// paragraph separator characters.
fn clarification_text(value: &Value, maximum: usize, empty: bool) -> Option<&Value> {
    let line = string(value)?;
    let valid = text::is_nfc(line)
        && text::strip(line) == line
        && text::len(line) <= maximum
        && (empty || !line.is_empty())
        && !line.chars().any(|character| {
            let category = text::category(character);
            category.starts_with('C') || category == "Zl" || category == "Zp"
        });
    valid.then_some(value)
}

fn clarification(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["default_index", "options", "question"])?;
    let question = clarification_text(
        field(members, "question"),
        MAX_CLARIFICATION_QUESTION_CHARS,
        false,
    )?;
    let raw = array(field(members, "options")).filter(|options| {
        (MIN_CLARIFICATION_OPTIONS..=MAX_CLARIFICATION_OPTIONS).contains(&options.len())
    })?;
    let mut options = Vec::with_capacity(raw.len());
    let mut folded = Vec::with_capacity(raw.len());
    for option in raw {
        let option = exact_object(option, &["description", "label"])?;
        let label =
            clarification_text(field(option, "label"), MAX_CLARIFICATION_LABEL_CHARS, false)?;
        let description = clarification_text(
            field(option, "description"),
            MAX_CLARIFICATION_DESCRIPTION_CHARS,
            true,
        )?;
        folded.push(text::casefold(string(label)?));
        options.push(object_of(vec![
            ("label", label.clone()),
            ("description", description.clone()),
        ]));
    }
    folded.sort();
    folded.dedup();
    let default = field(members, "default_index");
    let default_valid = *default == Value::Null
        || small_int(default)
            .is_some_and(|index| usize::try_from(index).is_ok_and(|index| index < options.len()));
    (folded.len() == options.len() && default_valid).then(|| {
        object_of(vec![
            ("question", question.clone()),
            ("options", Value::Array(options)),
            ("default_index", default.clone()),
        ])
    })
}

/// `canonical_clarification`: one exact Brain multiple-choice clarification (ADR-0081).
#[must_use]
pub fn canonical_clarification(value: &Value) -> Option<Value> {
    clarification(value)
}

/// `compose_clarified`: the exact message a clarification's answer sends, as Admin composes it in `locale`; `None`
/// for a locale outside the labels (Python raises `KeyError`).
#[must_use]
pub fn compose_clarified(
    original: &str,
    question: &str,
    answer: &str,
    locale: &str,
) -> Option<String> {
    let (_, question_label, answer_label) = CLARIFICATION_LABELS
        .iter()
        .find(|(code, _, _)| *code == locale)?;
    Some(format!(
        "{}\n\n{question_label}: {question}\n{answer_label}: {}",
        text::strip(original),
        text::strip(answer)
    ))
}

/// `authored_segments`: what a person wrote in a sent message, in order: its original text, then each answer.
#[must_use]
pub fn authored_segments(message: &str) -> Vec<String> {
    let mut segments: Vec<Vec<&str>> = vec![Vec::new()];
    for line in message.split('\n') {
        let question = CLARIFICATION_LABELS.iter().any(|(_, label, _)| {
            line.strip_prefix(label)
                .is_some_and(|rest| rest.starts_with(": "))
        });
        let answer = CLARIFICATION_LABELS.iter().find_map(|(_, _, label)| {
            line.strip_prefix(label)
                .and_then(|rest| rest.strip_prefix(": "))
        });
        if question {
            segments.push(Vec::new());
        } else if let Some(answer) = answer {
            segments.push(vec![answer]);
        } else {
            let last = segments.len() - 1;
            segments[last].push(line);
        }
    }
    segments
        .iter()
        .map(|lines| text::strip(&lines.join("\n")).to_owned())
        .filter(|segment| !segment.is_empty())
        .collect()
}

/// `render_clarification`: the exact plain reply of one canonical clarification: the question, then numbered options,
/// a recommended default marked with " ✓" and a non-empty description after " — ".
#[must_use]
pub fn render_clarification(clarification: &Value) -> String {
    let mut lines = vec![
        string(field_of(clarification, "question"))
            .unwrap_or_default()
            .to_owned(),
        String::new(),
    ];
    let default = small_int(field_of(clarification, "default_index"));
    for (index, option) in array(field_of(clarification, "options"))
        .unwrap_or_default()
        .iter()
        .enumerate()
    {
        let mark = if default.is_some_and(|default| usize::try_from(default) == Ok(index)) {
            " ✓"
        } else {
            ""
        };
        let description = string(field_of(option, "description")).unwrap_or_default();
        let detail = if description.is_empty() {
            String::new()
        } else {
            format!(" — {description}")
        };
        let label = string(field_of(option, "label")).unwrap_or_default();
        lines.push(format!("{}. {label}{mark}{detail}", index + 1));
    }
    lines.join("\n")
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

fn memory_entry(
    topic: &Value,
    preference: &Value,
    empty: bool,
) -> Option<[(&'static str, Value); 2]> {
    if !MEMORY_TOPIC_RE.is_full_value(topic) {
        return None;
    }
    let preference = clarification_text(preference, MAX_MEMORY_PREFERENCE_CHARS, empty)?;
    Some([("topic", topic.clone()), ("preference", preference.clone())])
}

/// `canonical_memory`: at most 32 distinct topics, each with one public preference line (ADR-0084).
#[must_use]
pub fn canonical_memory(value: &Value) -> Option<Value> {
    let entries = array(value).filter(|entries| entries.len() <= MAX_MEMORIES)?;
    let mut canonical = Vec::with_capacity(entries.len());
    let mut topics: Vec<&Value> = Vec::with_capacity(entries.len());
    for entry in entries {
        let members = exact_object(entry, &["preference", "topic"])?;
        let topic = field(members, "topic");
        canonical.push(object_of(
            memory_entry(topic, field(members, "preference"), false)?.to_vec(),
        ));
        topics.push(topic);
    }
    let distinct = topics
        .iter()
        .enumerate()
        .all(|(index, topic)| !topics[..index].contains(topic));
    distinct.then_some(Value::Array(canonical))
}

/// `canonical_memory_changes`: at most 32 changes; `remember` carries a preference and `forget` the empty one.
#[must_use]
pub fn canonical_memory_changes(value: &Value) -> Option<Value> {
    let changes = array(value).filter(|changes| changes.len() <= MAX_MEMORY_CHANGES)?;
    let mut canonical = Vec::with_capacity(changes.len());
    for change in changes {
        let members = exact_object(change, &["op", "preference", "topic"])?;
        let (op, preference) = (field(members, "op"), field(members, "preference"));
        let forget = string(op) == Some("forget");
        let known = string(op).is_some_and(|op| op == "remember" || op == "forget");
        if !known || forget != (string(preference) == Some("")) {
            return None;
        }
        let [topic, preference] = memory_entry(field(members, "topic"), preference, forget)?;
        canonical.push(object_of(vec![("op", op.clone()), topic, preference]));
    }
    Some(Value::Array(canonical))
}

/// `apply_memory_changes`: canonical changes applied in order to a canonical memory, keeping the 32 newest entries.
#[must_use]
pub fn apply_memory_changes(memory: &Value, changes: &Value) -> Value {
    let mut entries: Vec<(String, Value)> = Vec::new();
    for entry in array(memory).unwrap_or_default() {
        let topic = string(field_of(entry, "topic"))
            .unwrap_or_default()
            .to_owned();
        let preference = field_of(entry, "preference").clone();
        match entries.iter_mut().find(|(known, _)| *known == topic) {
            Some(existing) => existing.1 = preference,
            None => entries.push((topic, preference)),
        }
    }
    for change in array(changes).unwrap_or_default() {
        let topic = string(field_of(change, "topic")).unwrap_or_default();
        entries.retain(|(known, _)| known != topic);
        if string(field_of(change, "op")) == Some("remember") {
            entries.push((topic.to_owned(), field_of(change, "preference").clone()));
        }
    }
    let skip = entries.len().saturating_sub(MAX_MEMORIES);
    Value::Array(
        entries
            .into_iter()
            .skip(skip)
            .map(|(topic, preference)| {
                object_of(vec![
                    ("topic", Value::String(topic)),
                    ("preference", preference),
                ])
            })
            .collect(),
    )
}
