//! The sanitized Action failure envelope (`validators/failure.py`, Assistant Spec v1).
//!
//! A handled failure is `{"type": "failure", "failure": {...}}` with exactly the closed diagnostic members of
//! [`FAILURE_KEYS`]. Text bounds count UTF-8 bytes, so a stored diagnostic stays bounded whatever its script.

use strict_json::Value;

use crate::python::{has_exactly, int_within, member, object, string};

/// The closed members of a failure, in the protocol's order.
pub const FAILURE_KEYS: [&str; 7] = [
    "error_type",
    "message",
    "provider",
    "http_status",
    "response_excerpt",
    "redacted",
    "truncated",
];
/// The longest diagnostic text, in UTF-8 bytes.
pub const MAX_TEXT_BYTES: usize = 2048;
/// The longest provider host.
pub const MAX_PROVIDER: usize = 253;
/// The longest error type.
pub const MAX_ERROR_TYPE: usize = 128;

type Members = [(String, Value)];
/// One member rule: its reason code and whether a failure satisfies it.
type Rule = (&'static str, fn(&Members) -> bool);

/// The stable reason one stdout failure envelope is refused, or `None`.
#[must_use]
pub fn failure_error(envelope: &Value) -> Option<&'static str> {
    let envelope = object(envelope).filter(|members| {
        has_exactly(members, &["type", "failure"])
            && member(members, "type").and_then(string) == Some("failure")
    });
    let Some(envelope) = envelope else {
        return Some("envelope_invalid");
    };
    let Some(failure) = member(envelope, "failure")
        .and_then(object)
        .filter(|members| has_exactly(members, &FAILURE_KEYS))
    else {
        return Some("failure_invalid");
    };
    let rules: [Rule; 6] = [
        ("error_type_invalid", error_type_admitted),
        ("message_invalid", |failure| {
            field(failure, "message").is_some_and(text_admitted)
        }),
        ("response_excerpt_invalid", excerpt_admitted),
        ("provider_invalid", provider_admitted),
        ("http_status_invalid", status_admitted),
        ("flag_invalid", flags_admitted),
    ];
    rules
        .into_iter()
        .find(|(_, admitted)| !admitted(failure))
        .map(|(code, _)| code)
}

fn field<'a>(failure: &'a Members, key: &str) -> Option<&'a Value> {
    member(failure, key)
}

fn error_type_admitted(failure: &Members) -> bool {
    field(failure, "error_type")
        .and_then(string)
        .is_some_and(|text| {
            (1..=MAX_ERROR_TYPE).contains(&text.len())
                && text.bytes().all(|byte| (b'!'..=b'~').contains(&byte))
        })
}

fn excerpt_admitted(failure: &Members) -> bool {
    field(failure, "response_excerpt")
        .is_some_and(|excerpt| *excerpt == Value::Null || text_admitted(excerpt))
}

fn provider_admitted(failure: &Members) -> bool {
    field(failure, "provider").is_some_and(|provider| match provider {
        Value::Null => true,
        Value::String(host) => host.len() <= MAX_PROVIDER && dns_labels(host, 1),
        _ => false,
    })
}

fn status_admitted(failure: &Members) -> bool {
    field(failure, "http_status")
        .is_some_and(|status| *status == Value::Null || int_within(status, 100, 599))
}

fn flags_admitted(failure: &Members) -> bool {
    ["redacted", "truncated"]
        .iter()
        .all(|key| matches!(field(failure, key), Some(Value::Bool(_))))
}

/// Whether `host` is `minimum` or more dot-separated lowercase DNS labels: `[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?`.
#[must_use]
pub fn dns_labels(host: &str, minimum: usize) -> bool {
    let labels: Vec<&str> = host.split('.').collect();
    labels.len() >= minimum
        && labels.iter().all(|label| {
            let bytes = label.as_bytes();
            (1..=63).contains(&bytes.len())
                && bytes
                    .iter()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
                && bytes.first() != Some(&b'-')
                && bytes.last() != Some(&b'-')
        })
}

/// Diagnostic text keeps tab and line feed; every other control, bidi override or isolate, and zero-width formatting
/// character is refused.
const fn unsafe_character(character: char) -> bool {
    matches!(
        character,
        '\u{0}'..='\u{8}'
            | '\u{b}'..='\u{1f}'
            | '\u{7f}'..='\u{9f}'
            | '\u{200b}'..='\u{200f}'
            | '\u{202a}'..='\u{202e}'
            | '\u{2060}'..='\u{206f}'
            | '\u{feff}'
    )
}

/// Whether one diagnostic text is safe Unicode within [`MAX_TEXT_BYTES`].
#[must_use]
pub fn text_admitted(value: &Value) -> bool {
    string(value)
        .is_some_and(|text| text.len() <= MAX_TEXT_BYTES && !text.chars().any(unsafe_character))
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{MAX_TEXT_BYTES, text_admitted};

    #[test]
    fn diagnostic_text_refuses_every_hiding_or_reordering_character() {
        for character in [
            '\u{0}', '\u{8}', '\u{b}', '\u{1f}', '\u{7f}', '\u{9f}', '\u{200b}', '\u{200f}',
            '\u{202a}', '\u{202e}', '\u{2060}', '\u{206f}', '\u{feff}',
        ] {
            assert!(
                !text_admitted(&Value::String(format!("a{character}b"))),
                "U+{:04X}",
                u32::from(character)
            );
        }
        for text in ["tab\tand\nline", "é", "\u{a0}", "\u{2010}"] {
            assert!(text_admitted(&Value::String(text.to_owned())), "{text:?}");
        }
        assert!(text_admitted(&Value::String(
            "é".repeat(MAX_TEXT_BYTES / 2)
        )));
        assert!(!text_admitted(&Value::String(format!(
            "{}a",
            "é".repeat(MAX_TEXT_BYTES / 2)
        ))));
    }
}
