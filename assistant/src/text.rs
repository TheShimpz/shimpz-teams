//! The text rules of manifest admission: public one-line text, displayed copy, Genesis, and credential-shaped text.

use std::sync::LazyLock;

use fancy_regex::Regex;
use strict_json::Value;
use team_protocol::python::text;

use crate::ManifestError;

/// The longest Genesis.
pub const MAX_GENESIS_LENGTH: usize = 65_536;

/// Python's `\s`: its whitespace characters, which include the four information separators U+001C to U+001F.
const SPACE: &str =
    r"\t-\r\x1c-\x20\x85\xa0\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}";

static SECRET_VALUE_RE: LazyLock<Regex> = LazyLock::new(|| {
    let pattern = format!(
        concat!(
            r"(?i)(?:bearer[{s}]+[a-z0-9._~-]{{12,}}|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)",
            r"[{s}]*[:=][{s}]*[^{s}]+|(?:sk|ghp|github_pat|glpat|xox[baprs])[-_][a-z0-9_-]{{12,}})",
        ),
        s = SPACE
    );
    Regex::new(&pattern).unwrap_or_else(|_| unreachable!("the credential pattern compiles"))
});
static JWT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\AeyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\z")
        .unwrap_or_else(|_| unreachable!("the token pattern compiles"))
});

/// Whether text looks like credential material: a bearer token, an assigned secret, a provider key, or a JWT.
///
/// A search the engine cannot finish counts as credential material, so the text is refused.
#[must_use]
pub fn resembles_credential(value: &str) -> bool {
    SECRET_VALUE_RE.is_match(value).unwrap_or(true)
        || JWT_RE.is_match(text::strip(value)).unwrap_or(true)
}

/// One trimmed line of 1 to `maximum` characters without control characters or credential material.
///
/// # Errors
/// [`ManifestError`] naming `kind`.
pub fn public_text<'a>(
    value: &'a Value,
    kind: &'static str,
    maximum: usize,
) -> Result<&'a str, ManifestError> {
    let Value::String(line) = value else {
        return Err(ManifestError::Invalid(kind));
    };
    let shaped = !line.is_empty()
        && text::strip(line) == line
        && text::len(line) <= maximum
        && !line
            .chars()
            .any(|character| character < ' ' || character == '\u{7f}');
    if !shaped || resembles_credential(line) {
        return Err(ManifestError::Invalid(kind));
    }
    Ok(line)
}

/// Public text an Assistant page displays: one printable NFC line, as every translation of it must be.
///
/// # Errors
/// [`ManifestError`] naming `kind`.
pub fn display_text<'a>(
    value: &'a Value,
    kind: &'static str,
    maximum: usize,
) -> Result<&'a str, ManifestError> {
    let line = public_text(value, kind, maximum)?;
    if !text::isprintable(line) || !text::is_nfc(line) {
        return Err(ManifestError::Invalid(kind));
    }
    Ok(line)
}

/// The model guidance: non-blank printable text with line feeds and tabs, at most [`MAX_GENESIS_LENGTH`] characters.
///
/// # Errors
/// [`ManifestError`] when it is anything else.
pub fn genesis(value: &Value) -> Result<&str, ManifestError> {
    let Value::String(guidance) = value else {
        return Err(ManifestError::Invalid("Genesis"));
    };
    let admitted = !text::strip(guidance).is_empty()
        && text::len(guidance) <= MAX_GENESIS_LENGTH
        && guidance.chars().all(|character| {
            character == '\n' || character == '\t' || text::isprintable_char(character)
        });
    if admitted {
        Ok(guidance)
    } else {
        Err(ManifestError::Invalid("Genesis"))
    }
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{display_text, genesis, public_text, resembles_credential};

    fn text(value: &str) -> Value {
        Value::String(value.to_owned())
    }

    #[test]
    fn credential_shaped_text_is_recognized() {
        for value in [
            "Bearer abcdefghijkl1234",
            "api_key: x",
            "PASSWORD=hunter2",
            "sk-abcdefghijkl1234",
            "  eyJabcdefgh.abcdefgh.abcdefgh  ",
        ] {
            assert!(resembles_credential(value), "{value}");
        }
        for value in [
            "Manage DNS records",
            "a token-free summary",
            "eyJabc.def.ghi",
        ] {
            assert!(!resembles_credential(value), "{value}");
        }
    }

    #[test]
    fn public_and_displayed_text_are_bounded_single_lines() {
        assert!(public_text(&text("Hello"), "name", 5).is_ok());
        for value in ["", " Hello", "Hello\n", "a\u{7f}", "Hello!"] {
            assert!(public_text(&text(value), "name", 5).is_err(), "{value:?}");
        }
        assert!(public_text(&Value::Null, "name", 5).is_err());
        assert!(public_text(&text("a\u{200b}"), "name", 5).is_ok());
        assert!(display_text(&text("a\u{200b}"), "name", 5).is_err());
        assert!(display_text(&text("e\u{301}"), "name", 5).is_err());
        assert!(display_text(&text("\u{e9}"), "name", 5).is_ok());
    }

    #[test]
    fn genesis_admits_lines_and_tabs_only() {
        assert!(genesis(&text("Use the tools.\n\tCarefully.")).is_ok());
        for value in ["", " \n ", "a\rb", "a\u{0}"] {
            assert!(genesis(&text(value)).is_err(), "{value:?}");
        }
        assert!(genesis(&text(&"a".repeat(65_537))).is_err());
    }
}
