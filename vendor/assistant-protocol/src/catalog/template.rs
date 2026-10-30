//! Template placeholders, their budget, and rendering.

use strict_json::{Number, Value};

use super::param_name;
use crate::python::{char_len, member, object, string};
use crate::unicode::category;

/// Whose template is checked, which names its error codes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Role {
    /// A catalog message's English template.
    Message,
    /// A pack's translation of one message.
    Translation,
}

impl Role {
    const fn placeholders(self) -> &'static str {
        match self {
            Self::Message => "message_placeholders",
            Self::Translation => "translation_placeholders",
        }
    }

    const fn budget(self) -> &'static str {
        match self {
            Self::Message => "message_budget",
            Self::Translation => "translation_budget",
        }
    }
}

/// The named fields of `template` in order, or `None` for any other brace syntax.
#[must_use]
pub fn placeholders(template: &str) -> Option<Vec<&str>> {
    let mut names = Vec::new();
    let mut rest = template;
    while rest.contains(['{', '}']) {
        let start = rest.find('{')?;
        let end = rest.find('}').filter(|end| *end >= start)?;
        let name = &rest[start + 1..end];
        if !param_name(name) {
            return None;
        }
        names.push(name);
        rest = &rest[end + 1..];
    }
    Some(names)
}

/// The byte ranges of each `{name}` field, as Python's `PLACEHOLDER.finditer` finds them.
fn fields(template: &str) -> Vec<(usize, usize)> {
    let bytes = template.as_bytes();
    let mut found = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'{' {
            let length = bytes[index + 1..]
                .iter()
                .take_while(|byte| {
                    byte.is_ascii_lowercase() || byte.is_ascii_digit() || **byte == b'_'
                })
                .count();
            let end = index + 1 + length;
            if bytes.get(end) == Some(&b'}') && param_name(&template[index + 1..end]) {
                found.push((index, end + 1));
                index = end + 1;
                continue;
            }
        }
        index += 1;
    }
    found
}

/// Whether a combining mark (general category M) directly follows a placeholder: under NFC an ASCII parameter value
/// would compose with it.
fn mark_follows_field(template: &str) -> bool {
    fields(template).into_iter().any(|(_, end)| {
        template[end..]
            .chars()
            .next()
            .is_some_and(|character| category(character).starts_with('M'))
    })
}

/// The first placeholder or budget error of `template` against its declarations and field `bound`, or `None`.
pub(super) fn template_error(
    template: &str,
    params: &[(&str, usize)],
    bound: usize,
    role: Role,
) -> Option<&'static str> {
    let Some(names) = placeholders(template) else {
        return Some(role.placeholders());
    };
    let unique = names
        .iter()
        .enumerate()
        .all(|(index, name)| !names[..index].contains(name));
    let same_set =
        names.len() == params.len() && params.iter().all(|(name, _)| names.contains(name));
    if !unique || !same_set || mark_follows_field(template) {
        return Some(role.placeholders());
    }
    let fields: usize = names.iter().map(|name| char_len(name) + 2).sum();
    let parameters: usize = params.iter().map(|(_, length)| length).sum();
    (char_len(template) - fields + parameters > bound).then(|| role.budget())
}

/// Inserts each already-validated parameter of `reference` once into the English or translated `template`.
#[must_use]
pub fn render(reference: &Value, template: &str) -> String {
    let params = reference.get("params").and_then(object).unwrap_or_default();
    let mut rendered = String::with_capacity(template.len());
    let mut last = 0;
    for (start, end) in fields(template) {
        let value = member(params, &template[start + 1..end - 1]).map(|value| match value {
            Value::Number(Number::Int(integer)) => integer.to_string(),
            Value::Number(Number::BigInt(digits)) => digits.clone(),
            other => string(other).unwrap_or_default().to_owned(),
        });
        rendered.push_str(&template[last..start]);
        rendered.push_str(value.as_deref().unwrap_or(&template[start..end]));
        last = end;
    }
    rendered.push_str(&template[last..]);
    rendered
}
