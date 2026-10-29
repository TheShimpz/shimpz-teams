//! The Local Supervisor assertion contract shared by Team and Admin (`supervisor.py`).
//!
//! Admin signs one closed claim set per request, encoded as canonical JSON v1; Team admits only that exact set. A
//! Routine run is started by a separate machine identity whose claims bind the run lease instead of a human session.

use std::fmt;
use std::sync::LazyLock;

use strict_json::Value;

use crate::base64::{self, URL_SAFE};
use crate::payload::{
    MAX_FILE_UPLOAD_BYTES, MAX_FILENAME_BYTES, MAX_MEDIA_TYPE_CHARS, MEDIA_TYPE_RE,
};
use crate::python::json::{CANONICAL, dumps};
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{exact_object, field, object, object_of, small_int, string};

/// The header that carries a Supervisor assertion.
pub const ASSERTION_HEADER: &str = "X-Shimpz-Supervisor";
/// The audience of a Supervisor assertion.
pub const ASSERTION_AUDIENCE: &str = "team-local";
/// The key id of a Supervisor assertion.
pub const ASSERTION_KEY_ID: &str = "local-supervisor-v1";
/// The longest an assertion lives.
pub const ASSERTION_MAX_TTL_SECONDS: i64 = 15;
/// The only decision credential an assertion can bind: the `TypeSafe` key for intent classification.
pub const DECISION_PROVIDER: &str = "typesafe";
/// The clock skew Team tolerates.
pub const ASSERTION_CLOCK_SKEW_SECONDS: i64 = 5;
/// The longest encoded claim set.
pub const ASSERTION_MAX_BYTES: usize = 4096;
/// The largest JSON body an assertion binds: the largest any operation admits, a capability plan.
pub const MAX_JSON_BODY_BYTES: i64 = 256 * 1024;
/// The JWT header of a Supervisor assertion.
pub const JWT_HEADER: [(&str, &str); 3] =
    [("alg", "EdDSA"), ("kid", ASSERTION_KEY_ID), ("typ", "JWT")];
/// The header that carries a Routine assertion.
pub const ROUTINE_ASSERTION_HEADER: &str = "X-Shimpz-Routine";
/// The audience of a Routine assertion.
pub const ROUTINE_AUDIENCE: &str = "team-local-routine";
/// The key id of a Routine assertion.
pub const ROUTINE_KEY_ID: &str = "local-routine-v1";
/// The one authority of a Routine assertion.
pub const ROUTINE_AUTHORITY: &str = "routine";
/// The JWT header of a Routine assertion.
pub const ROUTINE_JWT_HEADER: [(&str, &str); 3] =
    [("alg", "EdDSA"), ("kid", ROUTINE_KEY_ID), ("typ", "JWT")];
/// The SHA-256 of no bytes.
pub const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
/// The human assurances an assertion may bind.
pub const ASSURANCE_KINDS: [&str; 3] = ["auth:passkey", "auth:password", "auth:totp"];
/// The authorities of a Supervisor assertion.
pub const AUTHORITY_KINDS: [&str; 2] = ["host-reset", "session"];
/// The members of a key rotation body.
pub const KEY_ROTATION_FIELDS: [&str; 1] = ["public_key"];

static HEX_32: LazyLock<Pattern> = LazyLock::new(|| declared("^[0-9a-f]{32}$"));
static HEX_64: LazyLock<Pattern> = LazyLock::new(|| declared("^[0-9a-f]{64}$"));
static PATH: LazyLock<Pattern> = LazyLock::new(|| declared("^/[a-z0-9_/-]+$"));
static PROVIDER: LazyLock<Pattern> = LazyLock::new(|| declared("^[a-z][a-z0-9-]{0,31}$"));
static RAW_PUBLIC_KEY: LazyLock<Pattern> = LazyLock::new(|| declared("^[A-Za-z0-9_-]{43}$"));
const METHODS: [&str; 5] = ["DELETE", "GET", "PATCH", "POST", "PUT"];
const REQUIRED: [&str; 11] = [
    "v",
    "aud",
    "sub",
    "authority",
    "authority_sha256",
    "jti",
    "iat",
    "exp",
    "method",
    "path",
    "body",
];
const OPTIONAL: [&str; 3] = ["model", "decision", "assurance"];

/// A Local Supervisor assertion does not match the closed wire contract (`SupervisorAssertionError`); `Display` is
/// Python's message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SupervisorAssertionError {
    /// The claims are no object, or their members are not the closed set.
    Claims,
    /// The version or audience is not supported.
    Unsupported,
    /// The subject is no 32-hex id.
    Subject,
    /// The nonce is no 32-hex id.
    Nonce,
    /// An instant is no integer from 1 to 9,999,999,999; the field names which.
    Instant(&'static str),
    /// The lifetime is not 1 to 15 seconds.
    Lifetime,
    /// The method is not a closed one.
    Method,
    /// The path is outside its grammar.
    Path,
    /// A digest is no lowercase SHA-256; the field names which.
    Digest(&'static str),
    /// The body is no object of a known kind.
    Body,
    /// An empty body is not exactly zero bytes.
    EmptyBody,
    /// An empty body's digest is not the digest of no bytes.
    EmptyDigest,
    /// A JSON body is outside its bounds.
    JsonBody,
    /// A file body is outside its bounds.
    FileBody,
    /// The model binding is not exactly a provider and a key digest.
    Model,
    /// The model provider is outside its grammar.
    ModelProvider,
    /// The decision binding is not the `TypeSafe` key's.
    Decision,
    /// The human assurance binding is outside its closed form.
    Assurance,
    /// The authority does not fit the audience.
    Authority,
    /// A Routine assertion binds a model, human assurance, or decision.
    RoutineBinding,
    /// The encoded claim set exceeds [`ASSERTION_MAX_BYTES`].
    TooLarge,
}

impl fmt::Display for SupervisorAssertionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::Claims => "invalid Supervisor assertion claims",
            Self::Unsupported => "unsupported Supervisor assertion",
            Self::Subject => "invalid Supervisor subject",
            Self::Nonce => "invalid Supervisor assertion nonce",
            Self::Instant(label) | Self::Digest(label) => {
                return write!(formatter, "invalid {label}");
            }
            Self::Lifetime => "invalid Supervisor assertion lifetime",
            Self::Method => "invalid assertion method",
            Self::Path => "invalid assertion path",
            Self::Body => "invalid assertion body",
            Self::EmptyBody => "invalid empty assertion body",
            Self::EmptyDigest => "invalid empty body digest",
            Self::JsonBody => "invalid JSON assertion body",
            Self::FileBody => "invalid file assertion body",
            Self::Model => "invalid model credential binding",
            Self::ModelProvider => "invalid model provider binding",
            Self::Decision => "invalid decision credential binding",
            Self::Assurance => "invalid human assurance binding",
            Self::Authority => "invalid Supervisor authority kind",
            Self::RoutineBinding => {
                "a Routine assertion binds no model, human assurance, or decision"
            }
            Self::TooLarge => "assertion JSON exceeds its limit",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for SupervisorAssertionError {}

type Checked<T> = Result<T, SupervisorAssertionError>;

fn instant(value: &Value, label: &'static str) -> Checked<i64> {
    small_int(value)
        .filter(|instant| (1..=9_999_999_999).contains(instant))
        .ok_or(SupervisorAssertionError::Instant(label))
}

fn digest(value: &Value, label: &'static str) -> Checked<Value> {
    string(value)
        .filter(|digest| HEX_64.is_full(digest))
        .map(|_| value.clone())
        .ok_or(SupervisorAssertionError::Digest(label))
}

fn body(value: &Value) -> Checked<Value> {
    let members = object(value).ok_or(SupervisorAssertionError::Body)?;
    match string(field(members, "kind")) {
        Some("none") => empty_body(value),
        Some("json") => json_body(value),
        Some("file") => file_body(value),
        _ => Err(SupervisorAssertionError::Body),
    }
}

fn empty_body(value: &Value) -> Checked<Value> {
    let members = exact_object(value, &["kind", "length", "sha256"])
        .filter(|members| small_int(field(members, "length")) == Some(0))
        .ok_or(SupervisorAssertionError::EmptyBody)?;
    let sha256 = digest(field(members, "sha256"), "empty body digest")?;
    if string(&sha256) != Some(EMPTY_SHA256) {
        return Err(SupervisorAssertionError::EmptyDigest);
    }
    Ok(object_of(vec![
        ("kind", Value::String("none".to_owned())),
        ("length", crate::python::value::int(0)),
        ("sha256", sha256),
    ]))
}

fn json_body(value: &Value) -> Checked<Value> {
    let members = exact_object(value, &["kind", "length", "sha256"])
        .filter(|members| {
            small_int(field(members, "length"))
                .is_some_and(|length| (2..=MAX_JSON_BODY_BYTES).contains(&length))
        })
        .ok_or(SupervisorAssertionError::JsonBody)?;
    Ok(object_of(vec![
        ("kind", Value::String("json".to_owned())),
        ("length", field(members, "length").clone()),
        (
            "sha256",
            digest(field(members, "sha256"), "JSON body digest")?,
        ),
    ]))
}

fn file_body(value: &Value) -> Checked<Value> {
    let members = exact_object(value, &["kind", "length", "filename", "media_type"])
        .filter(|members| {
            small_int(field(members, "length"))
                .is_some_and(|length| (1..=MAX_FILE_UPLOAD_BYTES).contains(&length))
                && string(field(members, "filename")).is_some_and(filename)
                && string(field(members, "media_type")).is_some_and(|media_type| {
                    (1..=MAX_MEDIA_TYPE_CHARS).contains(&text::len(media_type))
                        && MEDIA_TYPE_RE.is_full(media_type)
                })
        })
        .ok_or(SupervisorAssertionError::FileBody)?;
    Ok(object_of(vec![
        ("kind", Value::String("file".to_owned())),
        ("length", field(members, "length").clone()),
        ("filename", field(members, "filename").clone()),
        ("media_type", field(members, "media_type").clone()),
    ]))
}

fn filename(name: &str) -> bool {
    !name.is_empty()
        && text::strip(name) == name
        && name != "."
        && name != ".."
        && !name.contains(['/', '\\'])
        && name.len() <= MAX_FILENAME_BYTES
}

fn model(value: &Value) -> Checked<Value> {
    let members =
        exact_object(value, &["provider", "key_sha256"]).ok_or(SupervisorAssertionError::Model)?;
    let provider = field(members, "provider");
    if !PROVIDER.is_full_value(provider) {
        return Err(SupervisorAssertionError::ModelProvider);
    }
    Ok(object_of(vec![
        ("provider", provider.clone()),
        (
            "key_sha256",
            digest(field(members, "key_sha256"), "model credential digest")?,
        ),
    ]))
}

fn decision(value: &Value) -> Checked<Value> {
    let members = exact_object(value, &["provider", "key_sha256"])
        .filter(|members| string(field(members, "provider")) == Some(DECISION_PROVIDER))
        .ok_or(SupervisorAssertionError::Decision)?;
    Ok(object_of(vec![
        ("provider", Value::String(DECISION_PROVIDER.to_owned())),
        (
            "key_sha256",
            digest(field(members, "key_sha256"), "decision credential digest")?,
        ),
    ]))
}

fn assurance(value: &Value) -> Checked<Value> {
    let members = exact_object(value, &["kind", "challenge_id"])
        .filter(|members| {
            string(field(members, "kind")).is_some_and(|kind| ASSURANCE_KINDS.contains(&kind))
                && HEX_32.is_full_value(field(members, "challenge_id"))
        })
        .ok_or(SupervisorAssertionError::Assurance)?;
    Ok(object_of(vec![
        ("kind", field(members, "kind").clone()),
        ("challenge_id", field(members, "challenge_id").clone()),
    ]))
}

fn target(members: &[(String, Value)]) -> Checked<(Value, Value)> {
    let method = field(members, "method");
    if !string(method).is_some_and(|method| METHODS.contains(&method)) {
        return Err(SupervisorAssertionError::Method);
    }
    let path = field(members, "path");
    let admitted = string(path).is_some_and(|path| {
        path.is_ascii()
            && path.len() <= 512
            && PATH.is_full(path)
            && !path.contains("//")
            && !path.ends_with('/')
    });
    if !admitted {
        return Err(SupervisorAssertionError::Path);
    }
    Ok((method.clone(), path.clone()))
}

/// The members of a claim set whose names are the closed set, its version and audience supported.
fn envelope<'a>(value: &'a Value, audience: &str) -> Checked<&'a [(String, Value)]> {
    let members = object(value).ok_or(SupervisorAssertionError::Claims)?;
    let names_known = members
        .iter()
        .all(|(name, _)| REQUIRED.contains(&name.as_str()) || OPTIONAL.contains(&name.as_str()));
    let names_present = REQUIRED
        .iter()
        .all(|name| members.iter().any(|(key, _)| key == name));
    if !names_known || !names_present {
        return Err(SupervisorAssertionError::Claims);
    }
    if ![ASSERTION_AUDIENCE, ROUTINE_AUDIENCE].contains(&audience)
        || small_int(field(members, "v")) != Some(1)
        || string(field(members, "aud")) != Some(audience)
    {
        return Err(SupervisorAssertionError::Unsupported);
    }
    Ok(members)
}

fn identity(members: &[(String, Value)]) -> Checked<(Value, Value)> {
    let subject = field(members, "sub");
    if !HEX_32.is_full_value(subject) {
        return Err(SupervisorAssertionError::Subject);
    }
    let nonce = field(members, "jti");
    if !HEX_32.is_full_value(nonce) {
        return Err(SupervisorAssertionError::Nonce);
    }
    Ok((subject.clone(), nonce.clone()))
}

fn authority(members: &[(String, Value)], routine: bool) -> Checked<Value> {
    let authority = field(members, "authority");
    let admitted = string(authority).is_some_and(|kind| {
        (kind == ROUTINE_AUTHORITY) == routine && (routine || AUTHORITY_KINDS.contains(&kind))
    });
    if !admitted {
        return Err(SupervisorAssertionError::Authority);
    }
    if routine
        && OPTIONAL
            .iter()
            .any(|name| members.iter().any(|(key, _)| key == name))
    {
        return Err(SupervisorAssertionError::RoutineBinding);
    }
    Ok(authority.clone())
}

/// `canonical_claims`: one closed canonical claim set for the Supervisor or, with [`ROUTINE_AUDIENCE`], the Routine
/// identity, members in Python's order.
///
/// A Routine assertion's authority is always `routine` and its `authority_sha256` is its run lease digest; it never
/// binds a human assurance or a decision credential.
///
/// # Errors
///
/// Returns the [`SupervisorAssertionError`] Python raises first.
pub fn canonical_claims(value: &Value, audience: &str) -> Checked<Value> {
    let members = envelope(value, audience)?;
    let (subject, nonce) = identity(members)?;
    let issued_at = instant(field(members, "iat"), "assertion issued time")?;
    let expires_at = instant(field(members, "exp"), "assertion expiry")?;
    if !(1..=ASSERTION_MAX_TTL_SECONDS).contains(&(expires_at - issued_at)) {
        return Err(SupervisorAssertionError::Lifetime);
    }
    let (method, path) = target(members)?;
    let authority_sha256 = digest(field(members, "authority_sha256"), "authority digest")?;
    let body = body(field(members, "body"))?;
    let authority = authority(members, audience == ROUTINE_AUDIENCE)?;
    let mut claims = vec![
        ("v", crate::python::value::int(1)),
        ("aud", Value::String(audience.to_owned())),
        ("sub", subject),
        ("authority", authority),
        ("authority_sha256", authority_sha256),
        ("jti", nonce),
        ("iat", field(members, "iat").clone()),
        ("exp", field(members, "exp").clone()),
        ("method", method),
        ("path", path),
        ("body", body),
    ];
    for (name, check) in [
        ("model", model as fn(&Value) -> Checked<Value>),
        ("decision", decision),
        ("assurance", assurance),
    ] {
        if let Some((_, binding)) = members.iter().find(|(key, _)| key == name) {
            claims.push((name, check(binding)?));
        }
    }
    Ok(object_of(claims))
}

/// `canonical_json`: the assertion contract's deterministic encoding, canonical JSON v1 within
/// [`ASSERTION_MAX_BYTES`].
///
/// # Errors
///
/// [`SupervisorAssertionError::TooLarge`] beyond the bound.
pub fn canonical_json(value: &Value) -> Checked<Vec<u8>> {
    let encoded = dumps(value, CANONICAL).into_bytes();
    if encoded.len() > ASSERTION_MAX_BYTES {
        return Err(SupervisorAssertionError::TooLarge);
    }
    Ok(encoded)
}

/// `claims_json`: validates and encodes one canonical claim set.
///
/// # Errors
///
/// The error of [`canonical_claims`] or of [`canonical_json`].
pub fn claims_json(value: &Value, audience: &str) -> Checked<Vec<u8>> {
    canonical_json(&canonical_claims(value, audience)?)
}

/// `canonical_key_rotation`: exactly `{"public_key": key}`, the key the canonical unpadded base64url of 32 raw bytes.
#[must_use]
pub fn canonical_key_rotation(value: &Value) -> Option<Value> {
    let members = exact_object(value, &KEY_ROTATION_FIELDS)?;
    let key = string(field(members, "public_key")).filter(|key| RAW_PUBLIC_KEY.is_full(key))?;
    let raw = base64::decode(key, URL_SAFE)?;
    (base64::encode(&raw, URL_SAFE).trim_end_matches('=') == key)
        .then(|| object_of(vec![("public_key", Value::String(key.to_owned()))]))
}
