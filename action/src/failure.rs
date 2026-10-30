//! The sanitized failure envelope of one handled Action failure (`action/failure.py`, ADR-0092 section 8).
//!
//! A handled failure is one `{"type": "failure", ...}` stdout frame after exit 0 with empty stderr. Team admits it with
//! the Assistant protocol's reference rules and then re-redacts every member itself, never trusting the Assistant's
//! own sanitization: each value Team holds for the invocation (Integration tokens and Stored Input values, which the
//! workload never receives) is replaced in every common encoding, matched ASCII-case-insensitively; secret-shaped text
//! is replaced through the end of its value; and a trailing prefix of a held value the producer may have clipped is
//! withheld. Redaction runs before the bound is enforced again, and both flags record what changed.
//!
//! Redaction is best effort, not a guarantee: a fragment shorter than [`MIN_CLIPPED`], a derived encoding shorter than
//! [`MIN_DERIVED`], a piece from the middle of a value, or an unlisted transformation may remain. Diagnostics stay
//! private, encrypted, bounded, and shown only to the Team's Supervisor.

use std::sync::LazyLock;

use assistant_protocol::failure::{MAX_ERROR_TYPE, MAX_TEXT_BYTES, failure_error};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use fancy_regex::{Captures, Regex};
use strict_json::{Number, Value};
use team_protocol::python::json::write_string;

/// What replaces a redacted value.
pub const REDACTED: &str = "[REDACTED]";
/// A derived encoding shorter than this matches too much ordinary text to identify a value.
pub const MIN_DERIVED: usize = 4;
/// The shortest trailing prefix of a held value that is withheld as possibly clipped.
pub const MIN_CLIPPED: usize = 4;

/// Python's `IGNORECASE` also folds `İ` and `ı` to `i`, `ſ` to `s`, and `K` to `k`; the patterns keep that reach.
const I: &str = "[iİı]";

fn keyword(text: &str) -> String {
    text.replace('i', I)
}

static SHAPED: LazyLock<[Regex; 4]> = LazyLock::new(|| {
    let build = |pattern: &str| {
        Regex::new(pattern).unwrap_or_else(|_| unreachable!("a reviewed pattern compiles"))
    };
    [
        build(
            r"(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\z)",
        ),
        build(&format!(r"(?i)\b(?:bearer|{})\s+\S+", keyword("basic"))),
        build(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*)?"),
        build(
            r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs]|AKIA|ASIA|AIza|ya29)[-_.]?[A-Za-z0-9_.-]{8,}",
        ),
    ]
});

static ASSIGNED: LazyLock<Regex> = LazyLock::new(|| {
    let keys = keyword(
        "api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|password|passwd|pwd\
         |token|authorization|cookie|session|signature|private[_-]?key",
    );
    let pattern = format!(
        r#"(?is)(?P<key>\b(?:{keys})\b["']?\s*[:=]\s*)(?:\[REDACTED\]|"(?:[^"\\]|\\.)*(?:"|\z)|'[^']*(?:'|\z)|[^\s,;&}}\]]+)"#
    );
    Regex::new(&pattern).unwrap_or_else(|_| unreachable!("a reviewed pattern compiles"))
});

static URL_USERINFO: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^\s/@]+@")
        .unwrap_or_else(|_| unreachable!("a reviewed pattern compiles"))
});

/// A failure frame broke the closed protocol: a policy hold, never a handled failure. It carries the reason code.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FailureEnvelopeError(pub &'static str);

/// One admitted, Team-sanitized handled failure diagnostic.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionFailure {
    /// The error type.
    pub error_type: String,
    /// The message.
    pub message: String,
    /// The provider host, withheld when it carried a held value.
    pub provider: Option<String>,
    /// The provider's HTTP status.
    pub http_status: Option<i64>,
    /// An excerpt of the provider's response.
    pub response_excerpt: Option<String>,
    /// Whether anything was redacted, by the Assistant or by Team.
    pub redacted: bool,
    /// Whether anything was cut to its bound.
    pub truncated: bool,
}

impl ActionFailure {
    /// The closed failure object, as the protocol and the diagnostic store carry it.
    #[must_use]
    pub fn document(&self) -> Value {
        let text = |value: &str| Value::String(value.to_owned());
        let optional = |value: &Option<String>| value.as_deref().map_or(Value::Null, text);
        Value::Object(vec![
            ("error_type".to_owned(), text(&self.error_type)),
            ("message".to_owned(), text(&self.message)),
            ("provider".to_owned(), optional(&self.provider)),
            (
                "http_status".to_owned(),
                self.http_status
                    .map_or(Value::Null, |status| Value::Number(Number::Int(status))),
            ),
            (
                "response_excerpt".to_owned(),
                optional(&self.response_excerpt),
            ),
            ("redacted".to_owned(), Value::Bool(self.redacted)),
            ("truncated".to_owned(), Value::Bool(self.truncated)),
        ])
    }

    /// The failure re-redacted with more protected values, such as a Routine run's whole protection (ADR-0101).
    #[must_use]
    pub fn redacted_with<S: AsRef<str>>(&self, secrets: &[S]) -> Self {
        sanitize(&Redactor::new(secrets), self)
    }

    /// The failure with every free-form member withheld, for a run whose protection was lost: only its status stays.
    #[must_use]
    pub fn withheld(&self) -> Self {
        Self {
            error_type: "withheld".to_owned(),
            message: String::new(),
            provider: None,
            http_status: self.http_status,
            response_excerpt: None,
            redacted: true,
            truncated: false,
        }
    }
}

/// Whether a decoded stdout frame claims the failure branch, which only failure admission may then judge.
#[must_use]
pub fn is_failure(raw: &Value) -> bool {
    matches!(raw, Value::Object(_)) && raw.get("type") == Some(&Value::String("failure".to_owned()))
}

/// Admits one failure frame and re-redacts every member with each value Team injected for the invocation.
///
/// # Errors
/// [`FailureEnvelopeError`] with the protocol's reason code for any frame outside the closed envelope.
pub fn admit<S: AsRef<str>>(
    envelope: &Value,
    secrets: &[S],
) -> Result<ActionFailure, FailureEnvelopeError> {
    if let Some(reason) = failure_error(envelope) {
        return Err(FailureEnvelopeError(reason));
    }
    let failure = envelope
        .get("failure")
        .ok_or(FailureEnvelopeError("failure_invalid"))?;
    let text = |key: &str| match failure.get(key) {
        Some(Value::String(text)) => Some(text.clone()),
        _ => None,
    };
    let flag = |key: &str| failure.get(key) == Some(&Value::Bool(true));
    let admitted = ActionFailure {
        error_type: text("error_type").unwrap_or_default(),
        message: text("message").unwrap_or_default(),
        provider: text("provider"),
        http_status: match failure.get("http_status") {
            Some(Value::Number(Number::Int(status))) => Some(*status),
            _ => None,
        },
        response_excerpt: text("response_excerpt"),
        redacted: flag("redacted"),
        truncated: flag("truncated"),
    };
    Ok(sanitize(&Redactor::new(secrets), &admitted))
}

fn sanitize(redactor: &Redactor, failure: &ActionFailure) -> ActionFailure {
    let (error_type, type_changed, type_cut) = redactor.text(&failure.error_type, MAX_ERROR_TYPE);
    let (message, message_changed, message_cut) = redactor.text(&failure.message, MAX_TEXT_BYTES);
    let (excerpt, excerpt_changed, excerpt_cut) = match &failure.response_excerpt {
        Some(excerpt) => {
            let (text, changed, cut) = redactor.text(excerpt, MAX_TEXT_BYTES);
            (Some(text), changed, cut)
        }
        None => (None, false, false),
    };
    let provider_changed = failure
        .provider
        .as_ref()
        .is_some_and(|provider| redactor.text(provider, MAX_TEXT_BYTES).1);
    ActionFailure {
        error_type,
        message,
        provider: if provider_changed {
            None
        } else {
            failure.provider.clone()
        },
        http_status: failure.http_status,
        response_excerpt: excerpt,
        redacted: failure.redacted
            || type_changed
            || message_changed
            || excerpt_changed
            || provider_changed,
        truncated: failure.truncated || type_cut || message_cut || excerpt_cut,
    }
}

/// Replaces every encoding of each held value, then secret-shaped text, then any clipped trailing prefix.
pub struct Redactor {
    /// The folded needles, longest first.
    needles: Vec<String>,
}

impl std::fmt::Debug for Redactor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Redactor(<redacted>)")
    }
}

impl Redactor {
    /// A redactor of `secrets` and their common encodings; an empty value is ignored.
    #[must_use]
    pub fn new<S: AsRef<str>>(secrets: &[S]) -> Self {
        let mut needles: Vec<String> = Vec::new();
        for secret in secrets
            .iter()
            .map(AsRef::as_ref)
            .filter(|secret| !secret.is_empty())
        {
            needles.push(secret.to_ascii_lowercase());
            needles.extend(
                encodings(secret)
                    .into_iter()
                    .filter(|form| form.chars().count() >= MIN_DERIVED)
                    .map(|form| form.to_ascii_lowercase()),
            );
        }
        needles.sort_by(|left, right| {
            right
                .chars()
                .count()
                .cmp(&left.chars().count())
                .then_with(|| left.cmp(right))
        });
        needles.dedup();
        Self { needles }
    }

    /// The redacted text, whether anything was replaced or withheld, and whether it was cut to `limit` bytes.
    #[must_use]
    pub fn text(&self, value: &str, limit: usize) -> (String, bool, bool) {
        let mut redacted = self.replace_held(value);
        for pattern in SHAPED.iter() {
            redacted = substitute(pattern, &redacted, |_| REDACTED.to_owned());
        }
        redacted = substitute(&ASSIGNED, &redacted, |captures| {
            format!("{}{REDACTED}", &captures["key"])
        });
        redacted = substitute(&URL_USERINFO, &redacted, |captures| {
            format!("{}{REDACTED}@", &captures["scheme"])
        });
        let redacted = self.withhold_clipped(&redacted);
        let changed = redacted != value;
        let bounded = bound(&redacted, limit);
        let cut = bounded.len() != redacted.len();
        (bounded.to_owned(), changed, cut)
    }

    /// Every ASCII-case-insensitive occurrence of each held value or encoding, longest first, replaced.
    fn replace_held(&self, value: &str) -> String {
        let mut value = value.to_owned();
        for needle in &self.needles {
            let folded = value.to_ascii_lowercase();
            if !folded.contains(needle.as_str()) {
                continue;
            }
            let mut replaced = String::with_capacity(value.len());
            let mut start = 0;
            for (position, _) in folded.match_indices(needle.as_str()) {
                replaced.push_str(&value[start..position]);
                replaced.push_str(REDACTED);
                start = position + needle.len();
            }
            replaced.push_str(&value[start..]);
            value = replaced;
        }
        value
    }

    /// The longest trailing piece of `value` that is a proper prefix of a held value of at least [`MIN_CLIPPED`]
    /// characters, withheld: a producer's bound may have cut the value there.
    fn withhold_clipped(&self, value: &str) -> String {
        let folded = value.to_ascii_lowercase();
        let mut start = folded.len();
        for needle in &self.needles {
            let earliest = folded.len().saturating_sub(needle.len().saturating_sub(1));
            let candidate = folded.char_indices().map(|(index, _)| index).find(|index| {
                *index >= earliest
                    && *index < start
                    && folded[*index..].chars().count() >= MIN_CLIPPED
                    && needle.starts_with(&folded[*index..])
            });
            if let Some(index) = candidate {
                start = index;
            }
        }
        if start == value.len() {
            value.to_owned()
        } else {
            format!("{}{REDACTED}", &value[..start])
        }
    }
}

fn substitute(
    pattern: &Regex,
    text: &str,
    replace: impl Fn(&Captures<'_, str>) -> String,
) -> String {
    // A plain pattern runs on the linear engine and never reaches a backtracking limit; a refusal keeps nothing.
    pattern
        .try_replacen(text, 0, |captures: &Captures<'_, str>| replace(captures))
        .map_or_else(|_| REDACTED.to_owned(), std::borrow::Cow::into_owned)
}

/// `value` cut to at most `limit` UTF-8 bytes on a character boundary.
fn bound(value: &str, limit: usize) -> &str {
    if value.len() <= limit {
        return value;
    }
    let mut end = limit;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    &value[..end]
}

/// `urllib.parse.quote(text, safe="")`, or `quote_plus` with `plus`.
pub(crate) fn percent(text: &str, plus: bool) -> String {
    use std::fmt::Write as _;
    let mut quoted = String::new();
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                quoted.push(char::from(byte));
            }
            b' ' if plus => quoted.push('+'),
            _ => {
                let _ = write!(quoted, "%{byte:02X}");
            }
        }
    }
    quoted
}

fn json_body(text: &str, ensure_ascii: bool) -> String {
    let mut quoted = String::new();
    write_string(text, ensure_ascii, &mut quoted);
    quoted[1..quoted.len() - 1].to_owned()
}

/// The common encodings an Action may print a value in: JSON (ASCII, Unicode, and with escaped slashes), percent
/// and form encoding, hexadecimal, and both base64 alphabets at each of the three byte alignments, keeping only the
/// characters that depend on the value alone, so it is found inside a longer encoding such as a Basic credential.
#[must_use]
pub fn encodings(secret: &str) -> Vec<String> {
    let raw = secret.as_bytes();
    let ascii = json_body(secret, true);
    let mut forms = vec![
        ascii.clone(),
        json_body(secret, false),
        ascii.replace('/', "\\/"),
        percent(secret, false),
        percent(secret, true),
        raw.iter().fold(String::new(), |mut hex, byte| {
            use std::fmt::Write as _;
            let _ = write!(hex, "{byte:02x}");
            hex
        }),
    ];
    for offset in 0..3_usize {
        let mut padded = vec![0_u8; offset];
        padded.extend_from_slice(raw);
        let encoded = STANDARD.encode(&padded);
        let first = (offset * 4).div_ceil(3);
        let last = (offset + raw.len()) * 8 / 6;
        let core = encoded.get(first..last).unwrap_or_default().to_owned();
        forms.push(core.replace('+', "-").replace('/', "_"));
        forms.push(core);
    }
    forms
}

#[cfg(test)]
mod tests;
