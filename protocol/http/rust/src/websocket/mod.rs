//! The bounded `shimpz.chat.v7` frame primitives and Origin canonicalization.
//!
//! An Origin is printable ASCII without spaces, brackets, `?`, or `#`, read as `urllib.parse.urlparse` reads it; a
//! frame is one text message of bounded UTF-8 strict JSON whose value is an object; a human response is one exact
//! closed frame.

use std::sync::LazyLock;

use strict_json::Value;

use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{field, has_exactly, object, py_eq, small_int, string};

/// A lowercase 32-hex identifier, as `HEX_ID_RE` matches it.
pub const HEX_ID_PATTERN: &str = r"[0-9a-f]{32}\Z";
/// A challenge id: the hex identifier grammar.
pub const CHALLENGE_ID_PATTERN: &str = HEX_ID_PATTERN;
/// The longest submitted text of a human response.
pub const MAX_HUMAN_TEXT_CHARS: usize = 16_000;
/// The most choices one human response submits.
pub const MAX_HUMAN_CHOICES: usize = 32;
/// The longest submitted choice.
pub const MAX_HUMAN_CHOICE_CHARS: usize = 128;

/// [`HEX_ID_PATTERN`] compiled.
pub static HEX_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(HEX_ID_PATTERN));
/// [`CHALLENGE_ID_PATTERN`] compiled.
pub static CHALLENGE_ID_RE: &LazyLock<Pattern> = &HEX_ID_RE;

/// A refused frame (`FrameError`): the HTTP status and WebSocket close code Python carries, and its detail.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FrameError {
    /// The HTTP status.
    pub status: u16,
    /// The detail Python carries.
    pub detail: &'static str,
    /// The WebSocket close code.
    pub close_code: u16,
}

impl FrameError {
    const fn new(status: u16, detail: &'static str, close_code: u16) -> Self {
        Self {
            status,
            detail,
            close_code,
        }
    }
}

impl std::fmt::Display for FrameError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.detail)
    }
}

impl std::error::Error for FrameError {}

/// `public_text` refused its value (Python raises `ValueError`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidText;

impl std::fmt::Display for InvalidText {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("invalid public text")
    }
}

impl std::error::Error for InvalidText {}

/// The parts of an authority as `urllib.parse` splits them: whether it has userinfo, the hostname text before any port
/// colon, and the port text after it.
pub(crate) struct Authority<'a> {
    /// The username and password: `None` without an `@`, else the text before and after the first `:`.
    pub(crate) userinfo: Option<(&'a str, Option<&'a str>)>,
    /// The host text before the first `:` of what follows the last `@`.
    pub(crate) host: &'a str,
    /// The text after that `:`, `None` when there is none or it is empty.
    pub(crate) port: Option<&'a str>,
}

/// Splits a bracket-free authority as `SplitResult`'s `_userinfo` and `_hostinfo` do.
pub(crate) fn authority(netloc: &str) -> Authority<'_> {
    let (userinfo, hostinfo) = match netloc.rsplit_once('@') {
        Some((userinfo, hostinfo)) => (
            Some(
                userinfo
                    .split_once(':')
                    .map_or((userinfo, None), |(name, password)| (name, Some(password))),
            ),
            hostinfo,
        ),
        None => (None, netloc),
    };
    let (host, port) = hostinfo.split_once(':').unwrap_or((hostinfo, ""));
    Authority {
        userinfo,
        host,
        port: (!port.is_empty()).then_some(port),
    }
}

/// `SplitResult.port`: `None` without a port, the port when it is ASCII digits within 0 to 65535, else an error (`Err`).
pub(crate) fn port(text: Option<&str>) -> Result<Option<u32>, ()> {
    let Some(text) = text else {
        return Ok(None);
    };
    if !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(());
    }
    let digits = text.trim_start_matches('0');
    if digits.len() > 5 {
        return Err(());
    }
    let port = digits.parse::<u32>().unwrap_or(0);
    if port > 65_535 {
        Err(())
    } else {
        Ok(Some(port))
    }
}

/// The scheme `urlsplit` reads before the first `:`, lowercased, and the rest after it; no scheme leaves the text whole.
pub(crate) fn scheme(url: &str) -> (String, &str) {
    if let Some((scheme, rest)) = url.split_once(':')
        && scheme
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_alphabetic())
        && scheme
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"+-.".contains(&byte))
    {
        return (scheme.to_ascii_lowercase(), rest);
    }
    (String::new(), url)
}

/// `canonical_origin`: one exact HTTP(S) Origin, its scheme and authority lowercased, an explicit port kept.
#[must_use]
pub fn canonical_origin(value: Option<&str>) -> Option<String> {
    let value = value?;
    // Python also refuses the empty Origin and `null` by name; neither has a scheme and an authority, so both are
    // refused below.
    if !value.bytes().all(|byte| (b'!'..=b'~').contains(&byte))
        || value.contains(['[', ']', '?', '#'])
    {
        return None;
    }
    let (scheme, rest) = scheme(value);
    // Without `?` or `#`, everything after the authority is its path, which an Origin never has.
    let netloc = rest
        .strip_prefix("//")
        .filter(|netloc| !netloc.contains('/'))?;
    let parts = authority(netloc);
    let admitted = (scheme == "http" || scheme == "https")
        && port(parts.port).is_ok()
        && !parts.host.is_empty()
        && parts.userinfo.is_none();
    admitted.then(|| format!("{scheme}://{}", netloc.to_ascii_lowercase()))
}

/// `public_text`: non-empty, trimmed, bounded text without Other or line and paragraph separator characters.
///
/// # Errors
///
/// Returns [`InvalidText`] for any other value.
pub fn public_text(value: &Value, maximum: usize) -> Result<&str, InvalidText> {
    let text = string(value).ok_or(InvalidText)?;
    let admitted = !text.is_empty()
        && text::strip(text) == text
        && text::len(text) <= maximum
        && !text.chars().any(|character| {
            let category = text::category(character);
            category.starts_with('C') || category == "Zl" || category == "Zp"
        });
    if admitted { Ok(text) } else { Err(InvalidText) }
}

/// `decode_bounded_json_frame`: the object one text frame of at most `max_bytes` UTF-8 bytes holds.
///
/// # Errors
///
/// Returns the [`FrameError`] Python raises: 400 for another message type, 415/1003 for a binary frame, 413/1009 for
/// an oversized one, and 400/1007 for anything but a unique-key JSON object.
pub fn decode_bounded_json_frame(
    message: &[(String, Value)],
    max_bytes: i64,
) -> Result<Value, FrameError> {
    let get = |key: &str| {
        message
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    };
    if get("type").and_then(string) != Some("websocket.receive") {
        return Err(FrameError::new(400, "invalid WebSocket frame", 1007));
    }
    let (Some(Value::String(text)), None | Some(Value::Null)) = (get("text"), get("bytes")) else {
        return Err(FrameError::new(
            415,
            "WebSocket frame must be text JSON",
            1003,
        ));
    };
    if i64::try_from(text.len()).unwrap_or(i64::MAX) > max_bytes {
        return Err(FrameError::new(413, "WebSocket frame too large", 1009));
    }
    let value = strict_json::parse(text.as_bytes())
        .map_err(|_| FrameError::new(400, "WebSocket frame must be valid unique-key JSON", 1007))?;
    if object(&value).is_none() {
        return Err(FrameError::new(
            400,
            "WebSocket JSON must be an object",
            1007,
        ));
    }
    Ok(value)
}

/// `safe_status`: an error status as given when it is an integer from 400 to 599, else 502.
#[must_use]
pub fn safe_status(value: &Value) -> i64 {
    small_int(value)
        .filter(|status| (400..=599).contains(status))
        .unwrap_or(502)
}

/// `error_terminal`: an error terminal record carrying a safe status and `detail` when it is non-empty, bounded, and
/// free of controls, else `fallback_detail`.
#[must_use]
pub fn error_terminal(
    status: &Value,
    detail: &str,
    fallback_detail: &str,
    max_detail_chars: i64,
) -> Value {
    let length = i64::try_from(text::len(detail)).unwrap_or(i64::MAX);
    let safe = 0 < length
        && length <= max_detail_chars
        && !detail
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}');
    Value::Object(vec![
        ("type".to_owned(), Value::String("error".to_owned())),
        (
            "status".to_owned(),
            Value::Number(strict_json::Number::Int(safe_status(status))),
        ),
        (
            "detail".to_owned(),
            Value::String(if safe { detail } else { fallback_detail }.to_owned()),
        ),
    ])
}

/// `valid_challenge_id`.
#[must_use]
pub fn valid_challenge_id(value: &Value) -> bool {
    CHALLENGE_ID_RE.is_full_value(value)
}

/// `challenge_identity`: the challenge and turn ids of a challenge of `expected_team_id`, which are one id.
#[must_use]
pub fn challenge_identity<'a>(
    value: &'a Value,
    expected_team_id: &str,
) -> Option<(&'a str, &'a str)> {
    let members = object(value)?;
    let get = |key: &str| {
        members
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    };
    let challenge = get("challenge_id")?;
    let turn = get("turn_id")?;
    (get("team_id").and_then(string) == Some(expected_team_id)
        && valid_challenge_id(challenge)
        && py_eq(turn, challenge))
    .then(|| {
        (
            string(challenge).unwrap_or_default(),
            string(turn).unwrap_or_default(),
        )
    })
}

/// `canonical_human_response`: one exact Local Admin response to a pending human challenge.
///
/// # Errors
///
/// Returns [`FrameError`] 400 for any other value.
pub fn canonical_human_response(value: &Value) -> Result<Value, FrameError> {
    let refused = FrameError::new(400, "invalid human response frame", 1007);
    let members = object(value).ok_or(refused)?;
    let Some(decision @ ("submit" | "deny")) = value.get("decision").and_then(string) else {
        return Err(refused);
    };
    if value.get("type").and_then(string) != Some("human-response")
        || !value.get("challenge_id").is_some_and(valid_challenge_id)
    {
        return Err(refused);
    }
    let admitted = if decision == "submit" {
        has_exactly(members, &["type", "challenge_id", "decision", "value"])
            && human_value(field(members, "value"))
    } else {
        has_exactly(members, &["type", "challenge_id", "decision"])
    };
    if admitted {
        Ok(value.clone())
    } else {
        Err(refused)
    }
}

/// `human_value`: an approval (`true`), bounded text, or distinct bounded choices.
#[must_use]
pub fn human_value(value: &Value) -> bool {
    match value {
        Value::Bool(true) => true,
        Value::String(text) => text::len(text) <= MAX_HUMAN_TEXT_CHARS,
        Value::Array(items) => {
            let choices: Vec<&str> = items.iter().filter_map(string).collect();
            items.len() <= MAX_HUMAN_CHOICES
                && choices.len() == items.len()
                && choices
                    .iter()
                    .all(|choice| text::len(choice) <= MAX_HUMAN_CHOICE_CHARS)
                && choices
                    .iter()
                    .enumerate()
                    .all(|(index, choice)| !choices[..index].contains(choice))
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests;
