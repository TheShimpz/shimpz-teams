//! Fail-closed reading of one request: its bearer, target, body framing, file metadata, and credential headers.

use std::borrow::Cow;

use hyper::HeaderMap;
use hyper::header::{AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE, HeaderValue, TRANSFER_ENCODING};
use sha2::{Digest, Sha256};
use strict_json::Value;
use subtle::ConstantTimeEq;
use team_core::secret::Secret;
use team_protocol::payload::{MAX_FILENAME_BYTES, MAX_MEDIA_TYPE_CHARS, MEDIA_TYPE_RE};
use team_protocol::python::text;
use team_protocol::python::value::{int, object_of};
use team_protocol::supervisor::EMPTY_SHA256;

use super::limits::MAX_REQUEST_TARGET_BYTES;
use crate::authority::key::hex;
use crate::http::operation::Operation;
use crate::http::service::Problem;

/// The header that names an uploaded file, percent-encoded.
pub const FILE_NAME_HEADER: &str = "x-shimpz-filename";
/// The header that names the model provider of a model-bound request.
pub const MODEL_PROVIDER_HEADER: &str = "x-shimpz-model-provider";
/// The header that carries the provider API key of a model-bound request.
pub const MODEL_KEY_HEADER: &str = "x-shimpz-model-api-key";
/// The header that carries the intent classification key.
pub const DECISION_KEY_HEADER: &str = "x-shimpz-decision-api-key";
/// The model providers a Team may use (the model catalog's providers).
pub const MODEL_PROVIDERS: [&str; 2] = ["openai", "anthropic"];
const MIN_API_KEY_BYTES: usize = 16;
const MAX_API_KEY_BYTES: usize = 8 * 1024;

fn problem(status: u16, message: impl Into<Cow<'static, str>>, code: &'static str) -> Problem {
    Problem {
        status,
        message: message.into(),
        code: Some(code),
        retry_after_ms: None,
    }
}

fn values<'a>(headers: &'a HeaderMap, name: &str) -> Vec<&'a HeaderValue> {
    headers.get_all(name).iter().collect()
}

/// Whether the request carries exactly one `Authorization: Bearer <token>`, compared in constant time.
#[must_use]
pub fn bearer_matches(headers: &HeaderMap, token: &Secret) -> bool {
    let values = values(headers, AUTHORIZATION.as_str());
    let [value] = values.as_slice() else {
        return false;
    };
    let expected = format!("Bearer {}", token.expose());
    value.as_bytes().is_ascii() && bool::from(value.as_bytes().ct_eq(expected.as_bytes()))
}

/// The canonical path of an origin-form request target without a query or an encoded byte.
///
/// # Errors
/// `414` for a target over 512 bytes; `400` for any other form, a query, a `%`, or a path that is not canonical (an
/// empty segment or a trailing slash).
pub fn canonical_path(uri: &hyper::Uri) -> Result<&str, Problem> {
    let target = uri.path_and_query().map_or("", |target| target.as_str());
    if target.len() > MAX_REQUEST_TARGET_BYTES {
        return Err(problem(414, "request path is too long", "path-too-long"));
    }
    let path = uri.path();
    if uri.scheme().is_some()
        || uri.authority().is_some()
        || !path.starts_with('/')
        || path.contains('%')
        || uri.query().is_some_and(|query| !query.is_empty())
    {
        return Err(problem(
            400,
            "query and encoded paths are not accepted",
            "invalid-path",
        ));
    }
    let parts: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    if path != format!("/{}", parts.join("/")) {
        return Err(problem(
            400,
            "request path must be canonical",
            "invalid-path",
        ));
    }
    Ok(path)
}

/// One declared length: ASCII digits with optional surrounding space or tab.
fn declared_length(value: &HeaderValue) -> Result<u64, Problem> {
    let invalid = || problem(400, "invalid Content-Length", "content-length");
    let digits = std::str::from_utf8(value.as_bytes())
        .map_err(|_| invalid())?
        .trim_matches([' ', '\t']);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(invalid());
    }
    // A length beyond 64 bits exceeds every body bound.
    Ok(digits.parse().unwrap_or(u64::MAX))
}

/// The single declared length of an unchunked body.
fn content_length(headers: &HeaderMap) -> Result<u64, Problem> {
    if headers.contains_key(TRANSFER_ENCODING) {
        return Err(problem(
            400,
            "chunked requests are not accepted",
            "chunked-request",
        ));
    }
    let lengths = values(headers, CONTENT_LENGTH.as_str());
    let [length] = lengths.as_slice() else {
        return Err(problem(
            411,
            "one Content-Length is required",
            "content-length",
        ));
    };
    declared_length(length)
}

/// Python's ASCII whitespace for `str.strip`: space, `\t\n\v\f\r`, and the separators 0x1c to 0x1f.
fn python_space(character: char) -> bool {
    matches!(character, '\t'..='\r' | '\u{1c}'..=' ')
}

/// The length of a JSON body of 2 to `maximum` bytes declared as `application/json`, checked before it is read.
///
/// # Errors
/// The framing, size, or media type refusal.
pub fn json_length(headers: &HeaderMap, maximum: usize) -> Result<usize, Problem> {
    let length = content_length(headers)?;
    let length = usize::try_from(length)
        .ok()
        .filter(|length| (2..=maximum).contains(length))
        .ok_or_else(|| {
            problem(
                413,
                format!("request body is too large (max {maximum} bytes)"),
                "body-too-large",
            )
        })?;
    let admitted = match values(headers, CONTENT_TYPE.as_str()).as_slice() {
        [value] => value.to_str().is_ok_and(|media_type| {
            media_type
                .split(';')
                .next()
                .unwrap_or_default()
                .trim_matches(python_space)
                .eq_ignore_ascii_case("application/json")
        }),
        _ => false,
    };
    if !admitted {
        return Err(problem(
            415,
            "Content-Type must be application/json",
            "content-type",
        ));
    }
    Ok(length)
}

/// One captured JSON body: a strict JSON object, and its binding `{kind: json, length, sha256}`.
///
/// # Errors
/// `400` for bytes that are no strict JSON; `422` for a document that is no object.
pub fn json_document(raw: &[u8]) -> Result<(Value, Value), Problem> {
    let document =
        strict_json::parse(raw).map_err(|_| problem(400, "invalid JSON body", "invalid-json"))?;
    if !matches!(document, Value::Object(_)) {
        return Err(problem(422, "a JSON object is required", "invalid-body"));
    }
    let binding = object_of(vec![
        ("kind", Value::String("json".to_owned())),
        ("length", int(i64::try_from(raw.len()).unwrap_or(i64::MAX))),
        ("sha256", Value::String(hex(&Sha256::digest(raw)))),
    ]);
    Ok((document, binding))
}

/// A bodyless request's binding, once it is proven to declare no body.
///
/// # Errors
/// `400` for transfer framing, a repeated or invalid length, or a nonzero length.
pub fn empty_body(headers: &HeaderMap) -> Result<Value, Problem> {
    let unexpected = || problem(400, "this request cannot have a body", "unexpected-body");
    if headers.contains_key(TRANSFER_ENCODING) {
        return Err(unexpected());
    }
    match values(headers, CONTENT_LENGTH.as_str()).as_slice() {
        [] => {}
        [length] => {
            if declared_length(length)? != 0 {
                return Err(unexpected());
            }
        }
        _ => return Err(problem(400, "invalid Content-Length", "content-length")),
    }
    Ok(object_of(vec![
        ("kind", Value::String("none".to_owned())),
        ("length", int(0)),
        ("sha256", Value::String(EMPTY_SHA256.to_owned())),
    ]))
}

/// An upload's validated metadata, read before any content.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileMetadata {
    /// Its exact length.
    pub length: usize,
    /// Its decoded file name.
    pub filename: String,
    /// Its canonical media type.
    pub media_type: String,
}

impl FileMetadata {
    /// The binding `{kind: file, length, filename, media_type}` the assertion must carry.
    #[must_use]
    pub fn binding(&self) -> Value {
        object_of(vec![
            ("kind", Value::String("file".to_owned())),
            (
                "length",
                int(i64::try_from(self.length).unwrap_or(i64::MAX)),
            ),
            ("filename", Value::String(self.filename.clone())),
            ("media_type", Value::String(self.media_type.clone())),
        ])
    }
}

/// Validates an upload's framing and metadata before any untrusted content is read.
///
/// # Errors
/// The framing refusal, `413` for a size outside 1 to `maximum`, `415` for a media type that is not canonical, and
/// `422` for a file name that is not exactly percent-encoded or not admissible.
pub fn file_metadata(headers: &HeaderMap, maximum: usize) -> Result<FileMetadata, Problem> {
    let length = usize::try_from(content_length(headers)?)
        .ok()
        .filter(|length| (1..=maximum).contains(length))
        .ok_or_else(|| {
            problem(
                413,
                format!("file must contain 1 to {maximum} bytes"),
                "file-too-large",
            )
        })?;
    let media_type = match values(headers, CONTENT_TYPE.as_str()).as_slice() {
        [value] => value.to_str().ok().filter(|media_type| {
            !media_type.is_empty()
                && media_type.to_ascii_lowercase() == *media_type
                && media_type.len() <= MAX_MEDIA_TYPE_CHARS
                && MEDIA_TYPE_RE.is_full(media_type)
        }),
        _ => None,
    }
    .ok_or_else(|| {
        problem(
            415,
            "Content-Type must be a canonical media type",
            "content-type",
        )
    })?;
    let filename = match values(headers, FILE_NAME_HEADER).as_slice() {
        [value] => value.to_str().ok().and_then(file_name),
        _ => None,
    }
    .ok_or_else(|| problem(422, "invalid file name", "invalid-file"))?;
    Ok(FileMetadata {
        length,
        filename,
        media_type: media_type.to_owned(),
    })
}

/// `quote(name, safe="")`: every UTF-8 byte but ASCII letters, digits, and `_.-~` as uppercase `%XX`.
fn quote(name: &str) -> String {
    name.bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"_.-~".contains(&byte) {
                char::from(byte).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

/// `unquote_to_bytes`: each `%XX` with two hex digits as its byte, everything else as it is.
fn unquote(encoded: &str) -> Vec<u8> {
    let bytes = encoded.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let escaped = (bytes[index] == b'%')
            .then(|| bytes.get(index + 1..index + 3))
            .flatten()
            .and_then(|digits| std::str::from_utf8(digits).ok())
            .and_then(|digits| u8::from_str_radix(digits, 16).ok());
        if let Some(byte) = escaped {
            decoded.push(byte);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    decoded
}

/// The file name the header encodes exactly, when it is admissible: non-empty, at most 255 UTF-8 bytes, neither `.`
/// nor `..`, without a slash, a backslash, surrounding whitespace, or a control character.
fn file_name(encoded: &str) -> Option<String> {
    let name = String::from_utf8(unquote(encoded)).ok()?;
    let admitted = !name.is_empty()
        && name.len() <= MAX_FILENAME_BYTES
        && quote(&name) == encoded
        && name != "."
        && name != ".."
        && !name.contains(['/', '\\'])
        && text::strip(&name) == name
        && !name
            .chars()
            .any(|character| character < ' ' || character == '\u{7f}');
    admitted.then_some(name)
}

/// Whether `key` has the one admissible API key shape: 16 to 8192 bytes of printable ASCII without a space.
#[must_use]
pub fn admissible_api_key(key: &[u8]) -> bool {
    (MIN_API_KEY_BYTES..=MAX_API_KEY_BYTES).contains(&key.len())
        && key.iter().all(|byte| (33..=126).contains(byte))
}

/// `{provider, key_sha256}`: the digest binding of a request-only credential.
#[must_use]
pub fn credential_binding(provider: &str, key: &Secret) -> Value {
    object_of(vec![
        ("provider", Value::String(provider.to_owned())),
        (
            "key_sha256",
            Value::String(hex(&Sha256::digest(key.expose().as_bytes()))),
        ),
    ])
}

/// The one model credential of a model-bound request: its provider and key.
///
/// # Errors
/// `422` without exactly one known provider and one key, or for a key outside its shape. The key never appears in an
/// error.
pub fn model_credential(headers: &HeaderMap) -> Result<(String, Secret), Problem> {
    let required = || {
        problem(
            422,
            "one private model credential is required",
            "invalid-model-credential",
        )
    };
    let providers = values(headers, MODEL_PROVIDER_HEADER);
    let keys = values(headers, MODEL_KEY_HEADER);
    let ([provider], [key]) = (providers.as_slice(), keys.as_slice()) else {
        return Err(required());
    };
    let provider = provider
        .to_str()
        .ok()
        .filter(|provider| MODEL_PROVIDERS.contains(provider))
        .ok_or_else(required)?;
    if !admissible_api_key(key.as_bytes()) {
        return Err(problem(
            422,
            "private model credential is invalid",
            "invalid-model-credential",
        ));
    }
    let key = String::from_utf8(key.as_bytes().to_vec()).map_err(|_| required())?;
    Ok((provider.to_owned(), Secret::new(key)))
}

/// The decision key a request carries: at most one admissible key, and only on intent classification.
///
/// # Errors
/// `422` for a key on any other operation, a repeated key, or a key outside its shape.
pub fn decision_key(headers: &HeaderMap, operation: Operation) -> Result<Option<Secret>, Problem> {
    let keys = values(headers, DECISION_KEY_HEADER);
    if keys.is_empty() {
        return Ok(None);
    }
    let refused = || {
        problem(
            422,
            "a private decision credential is accepted only on intent classification",
            "invalid-decision-credential",
        )
    };
    let ([key], Operation::ChatIntentRoute) = (keys.as_slice(), operation) else {
        return Err(refused());
    };
    if !admissible_api_key(key.as_bytes()) {
        return Err(refused());
    }
    let key = String::from_utf8(key.as_bytes().to_vec()).map_err(|_| refused())?;
    Ok(Some(Secret::new(key)))
}

/// The WebSocket key of exactly one admitted Browser view upgrade (`browser_view` protocol): `Upgrade: websocket`,
/// `Connection` with `upgrade`, version 13, the `binary` subprotocol, no extension, and a canonical key.
#[must_use]
pub fn upgrade_key(headers: &HeaderMap) -> Option<String> {
    use team_protocol::browser_view::{SUBPROTOCOL, WEBSOCKET_VERSION, canonical_key};
    let one = |name: &str| match values(headers, name).as_slice() {
        [value] => value
            .to_str()
            .ok()
            .map(|text| text.trim_matches(python_space)),
        _ => None,
    };
    let connection_upgrades = one("connection").is_some_and(|connection| {
        connection.split(',').any(|token| {
            token
                .trim_matches(python_space)
                .eq_ignore_ascii_case("upgrade")
        })
    });
    let admitted = one("upgrade").is_some_and(|upgrade| upgrade.eq_ignore_ascii_case("websocket"))
        && connection_upgrades
        && one("sec-websocket-version") == Some(WEBSOCKET_VERSION)
        && one("sec-websocket-protocol") == Some(SUBPROTOCOL)
        && !headers.contains_key("sec-websocket-extensions");
    if !admitted {
        return None;
    }
    let key = Value::String(one("sec-websocket-key")?.to_owned());
    canonical_key(&key).map(str::to_owned)
}

#[cfg(test)]
mod tests;
