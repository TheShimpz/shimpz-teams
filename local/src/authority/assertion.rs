//! One request-bound assertion: its header, its three canonical JWT segments, its closed claims, its signature, and
//! the exact request it binds.

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use strict_json::Value;
use team_protocol::python::value::{field, object, py_eq, small_int, string};
use team_protocol::supervisor::{ASSERTION_CLOCK_SKEW_SECONDS, canonical_claims, canonical_json};

use super::Refusal;
use super::key::PublicKey;

/// The longest assertion header value Team reads, after its `Bearer ` scheme.
pub const MAX_ASSERTION_BYTES: usize = 8192;

/// The one assertion of a request: exactly one header value, `Bearer ` and then non-empty ASCII without whitespace,
/// at most [`MAX_ASSERTION_BYTES`].
///
/// # Errors
/// [`Refusal::Denied`] for anything else.
pub fn one_assertion<'a>(values: &[&'a [u8]]) -> Result<&'a str, Refusal> {
    let [value] = values else {
        return Err(Refusal::Denied);
    };
    let encoded = value.strip_prefix(b"Bearer ").ok_or(Refusal::Denied)?;
    // Python's `str.isspace` over ASCII: the C0 separators 0x1c to 0x1f count as whitespace too.
    let admitted = !encoded.is_empty()
        && encoded.len() <= MAX_ASSERTION_BYTES
        && encoded
            .iter()
            .all(|byte| byte.is_ascii() && !matches!(byte, b'\t'..=b'\r' | 0x1c..=0x20));
    if !admitted {
        return Err(Refusal::Denied);
    }
    std::str::from_utf8(encoded).map_err(|_| Refusal::Denied)
}

/// Canonical unpadded base64url: the one encoding of its bytes, without `=`.
pub(crate) fn decode_base64url(encoded: &str) -> Option<Vec<u8>> {
    if !encoded.is_ascii() || encoded.contains('=') {
        return None;
    }
    let raw = URL_SAFE_NO_PAD.decode(encoded).ok()?;
    (encode_base64url(&raw) == encoded).then_some(raw)
}

/// Unpadded base64url.
pub(crate) fn encode_base64url(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

/// One JWT segment: canonical base64url of the canonical JSON of the value it holds.
fn json_segment(encoded: &str) -> Result<Value, Refusal> {
    let raw = decode_base64url(encoded).ok_or(Refusal::Denied)?;
    let value = strict_json::parse(&raw).map_err(|_| Refusal::Denied)?;
    let canonical = canonical_json(&value).map_err(|_| Refusal::Denied)?;
    if encode_base64url(&canonical) != encoded {
        return Err(Refusal::Denied);
    }
    Ok(value)
}

/// The exact JWT header of an assertion kind.
fn header_is(header: &Value, expected: &[(&str, &str); 3]) -> bool {
    object(header).is_some_and(|members| {
        members.len() == expected.len()
            && expected
                .iter()
                .all(|(name, value)| string(field(members, name)) == Some(*value))
    })
}

/// The claims of an assertion whose header, claims, and signature all hold under `key`, in that order of checks.
///
/// # Errors
/// [`Refusal::Denied`] for any segment, header, claim, or signature that does not hold.
pub fn verified_claims(
    encoded: &str,
    key: &PublicKey,
    header: &[(&str, &str); 3],
    audience: &str,
) -> Result<Value, Refusal> {
    let parts: Vec<&str> = encoded.split('.').collect();
    let [head, body, signature] = parts.as_slice() else {
        return Err(Refusal::Denied);
    };
    if head.is_empty() || body.is_empty() || signature.is_empty() {
        return Err(Refusal::Denied);
    }
    let decoded_header = json_segment(head)?;
    let untrusted = json_segment(body)?;
    if !header_is(&decoded_header, header) {
        return Err(Refusal::Denied);
    }
    let claims = canonical_claims(&untrusted, audience).map_err(|_| Refusal::Denied)?;
    let signature = decode_base64url(signature).ok_or(Refusal::Denied)?;
    if !key.verifies(format!("{head}.{body}").as_bytes(), &signature) {
        return Err(Refusal::Denied);
    }
    Ok(claims)
}

/// The exact request an assertion must bind.
#[derive(Clone, Debug, PartialEq)]
pub struct RequestBinding {
    /// The request method.
    pub method: String,
    /// The canonical request path.
    pub path: String,
    /// The captured body's binding: `{kind: none|json|file, …}`.
    pub body: Value,
    /// The model credential binding `{provider, key_sha256}`, on a model-bound operation.
    pub model: Option<Value>,
    /// The decision credential binding `{provider: typesafe, key_sha256}`, when the request carries one.
    pub decision: Option<Value>,
    /// The human assurance `{kind, challenge_id}` an approval of an authentication challenge must carry.
    pub assurance: Option<Value>,
    /// The authorities the operation admits.
    pub authorities: &'static [&'static str],
}

/// Checks the claims' time and their exact binding of `request` at `now` (Unix seconds); returns the assertion's
/// id and expiry for the replay guard.
///
/// # Errors
/// [`Refusal::Denied`] for an assertion outside its valid time or bound to anything but `request`.
pub fn bound(claims: &Value, request: &RequestBinding, now: i64) -> Result<(String, i64), Refusal> {
    let members = object(claims).ok_or(Refusal::Denied)?;
    let (Some(issued_at), Some(expires_at)) = (
        small_int(field(members, "iat")),
        small_int(field(members, "exp")),
    ) else {
        return Err(Refusal::Denied);
    };
    if issued_at > now + ASSERTION_CLOCK_SKEW_SECONDS || expires_at < now {
        return Err(Refusal::Denied);
    }
    let optional = |name: &str, expected: &Option<Value>| match (claims.get(name), expected) {
        (None, None) => true,
        (Some(actual), Some(expected)) => py_eq(actual, expected),
        _ => false,
    };
    let matches = string(field(members, "method")) == Some(request.method.as_str())
        && string(field(members, "path")) == Some(request.path.as_str())
        && py_eq(field(members, "body"), &request.body)
        && optional("model", &request.model)
        && optional("decision", &request.decision)
        && optional("assurance", &request.assurance)
        && string(field(members, "authority"))
            .is_some_and(|authority| request.authorities.contains(&authority));
    let assertion_id = string(field(members, "jti")).ok_or(Refusal::Denied)?;
    if !matches {
        return Err(Refusal::Denied);
    }
    Ok((assertion_id.to_owned(), expires_at))
}
