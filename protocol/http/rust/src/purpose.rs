//! The Brain's task-bound sentence for why an Action pauses for a person (ADR-0090).

use strict_json::Value;

use crate::python::text;

/// The longest purpose.
pub const MAX_PURPOSE_CHARS: usize = 280;

/// `canonical_purpose`: one plain single-line purpose sentence.
///
/// It carries no control, format, or line-separator character, no dash punctuation other than a hyphen inside a word,
/// and nothing that reads as a link, so it can only explain, never point somewhere.
#[must_use]
pub fn canonical_purpose(value: &Value) -> Option<&str> {
    let Value::String(purpose) = value else {
        return None;
    };
    let plain = text::is_nfc(purpose)
        && text::strip(purpose) == purpose
        && (1..=MAX_PURPOSE_CHARS).contains(&text::len(purpose))
        && purpose.chars().all(|character| {
            let category = text::category(character);
            !(category.starts_with('C')
                || category == "Zl"
                || category == "Zp"
                || (category == "Pd" && character != '-'))
        })
        && !purpose.contains(" -")
        && !purpose.contains("- ")
        && !purpose.contains("://")
        && !text::casefold(purpose).contains("www.");
    plain.then_some(purpose.as_str())
}
