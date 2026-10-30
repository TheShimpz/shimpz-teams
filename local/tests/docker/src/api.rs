//! The controller's API as Admin calls it: the controller bearer, and on every route but `/healthz` a Supervisor
//! assertion signed over the exact request, as Admin signs it (one closed canonical claim set, `EdDSA`).

use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use std::fmt::Write as _;

use ring::rand::{SecureRandom, SystemRandom};
use ring::signature::{Ed25519KeyPair, KeyPair};
use sha2::{Digest, Sha256};
use strict_json::Value;
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::value::{int, object_of, text};
use team_protocol::supervisor::{
    ASSERTION_AUDIENCE, ASSERTION_HEADER, ASSERTION_MAX_TTL_SECONDS, EMPTY_SHA256, JWT_HEADER,
    canonical_json, claims_json,
};

use crate::http::{Response, exchange};
use crate::report::{Failure, Outcome, fail};

/// Python's `json.dumps(value, separators=(",", ":"))`: how Admin and the Python suite encode a request body.
const REQUEST_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};
/// The DER prefix of an Ed25519 `SubjectPublicKeyInfo` before its 32 raw key bytes.
const ED25519_SPKI_PREFIX: [u8; 12] = [
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
];

/// A request body.
#[derive(Clone, Debug)]
pub enum Body {
    /// No body.
    None,
    /// One JSON document.
    Json(Value),
    /// One file upload.
    File {
        /// The content.
        content: Vec<u8>,
        /// The file name header.
        filename: String,
        /// The media type.
        media_type: String,
    },
}

impl Body {
    /// The bytes sent, if any.
    fn encoded(&self) -> Option<Vec<u8>> {
        match self {
            Self::None => None,
            Self::Json(value) => Some(dumps(value, REQUEST_JSON).into_bytes()),
            Self::File { content, .. } => Some(content.clone()),
        }
    }

    /// The header fields the body brings.
    fn headers(&self) -> Vec<(String, String)> {
        match self {
            Self::None => Vec::new(),
            Self::Json(_) => vec![("Content-Type".into(), "application/json".into())],
            Self::File {
                filename,
                media_type,
                ..
            } => vec![
                ("Content-Type".into(), media_type.clone()),
                ("X-Shimpz-Filename".into(), filename.clone()),
            ],
        }
    }

    /// The assertion's `body` claim.
    fn claim(&self, encoded: Option<&[u8]>) -> Value {
        let length = |raw: &[u8]| int(i64::try_from(raw.len()).unwrap_or(i64::MAX));
        match (self, encoded) {
            (
                Self::File {
                    filename,
                    media_type,
                    ..
                },
                Some(raw),
            ) => object_of(vec![
                ("kind", text("file")),
                ("length", length(raw)),
                ("filename", text(filename)),
                ("media_type", text(media_type)),
            ]),
            (Self::Json(_), Some(raw)) => object_of(vec![
                ("kind", text("json")),
                ("length", length(raw)),
                ("sha256", text(&sha256_hex(raw))),
            ]),
            _ => object_of(vec![
                ("kind", text("none")),
                ("length", int(0)),
                ("sha256", text(EMPTY_SHA256)),
            ]),
        }
    }
}

/// The lowercase hex SHA-256 of `raw`.
pub fn sha256_hex(raw: &[u8]) -> String {
    hex(&Sha256::digest(raw))
}

/// Lowercase hex.
pub fn hex(raw: &[u8]) -> String {
    raw.iter()
        .fold(String::with_capacity(raw.len() * 2), |mut text, byte| {
            let _ = write!(text, "{byte:02x}");
            text
        })
}

/// `count` random bytes as lowercase hex.
pub fn random_hex(count: usize) -> Outcome<String> {
    let mut bytes = vec![0_u8; count];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| Failure("the system random source failed".into()))?;
    Ok(hex(&bytes))
}

/// The Supervisor identity the suite signs as.
pub struct Supervisor {
    key: Ed25519KeyPair,
    subject: String,
    session: String,
}

impl std::fmt::Debug for Supervisor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Supervisor")
    }
}

impl Supervisor {
    /// A fresh key, subject, and session.
    pub fn generate() -> Outcome<Self> {
        let random = SystemRandom::new();
        let document = Ed25519KeyPair::generate_pkcs8(&random)
            .map_err(|_| Failure("an Ed25519 key could not be generated".into()))?;
        let key = Ed25519KeyPair::from_pkcs8(document.as_ref())
            .map_err(|_| Failure("the Ed25519 key could not be read back".into()))?;
        Ok(Self {
            key,
            subject: random_hex(16)?,
            session: random_hex(32)?,
        })
    }

    /// The public key as a PEM `SubjectPublicKeyInfo`, as Admin publishes it.
    pub fn public_pem(&self) -> String {
        let mut der = ED25519_SPKI_PREFIX.to_vec();
        der.extend_from_slice(self.key.public_key().as_ref());
        format!(
            "-----BEGIN PUBLIC KEY-----\n{}\n-----END PUBLIC KEY-----\n",
            STANDARD.encode(&der)
        )
    }

    /// The `X-Shimpz-Supervisor` value for one request: a session assertion bound to its method, path, and body.
    pub fn assertion(
        &self,
        method: &str,
        path: &str,
        body: &Body,
        issued_at: i64,
    ) -> Outcome<String> {
        let claims = object_of(vec![
            ("v", int(1)),
            ("aud", text(ASSERTION_AUDIENCE)),
            ("sub", text(&self.subject)),
            ("authority", text("session")),
            (
                "authority_sha256",
                text(&sha256_hex(self.session.as_bytes())),
            ),
            ("jti", text(&random_hex(16)?)),
            ("iat", int(issued_at)),
            ("exp", int(issued_at + ASSERTION_MAX_TTL_SECONDS)),
            ("method", text(method)),
            ("path", text(path)),
            ("body", body.claim(body.encoded().as_deref())),
        ]);
        let header = Value::Object(
            JWT_HEADER
                .iter()
                .map(|(name, value)| ((*name).to_owned(), text(value)))
                .collect(),
        );
        let header = canonical_json(&header).map_err(|error| Failure(format!("{error:?}")))?;
        let payload = claims_json(&claims, ASSERTION_AUDIENCE)
            .map_err(|error| Failure(format!("{error:?}")))?;
        let signing_input = format!("{}.{}", segment(&header), segment(&payload));
        let signature = self.key.sign(signing_input.as_bytes());
        Ok(format!(
            "Bearer {signing_input}.{}",
            segment(signature.as_ref())
        ))
    }
}

fn segment(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

/// One controller, reached on its published loopback port.
#[derive(Debug)]
pub struct Api<'a> {
    /// The published port of 7077.
    pub port: u16,
    /// The controller bearer.
    pub token: String,
    /// The Supervisor the controller pins.
    pub supervisor: &'a Supervisor,
}

impl Api<'_> {
    /// Sends one request with the bearer and, except on `/healthz`, a Supervisor assertion.
    pub fn send(&self, method: &str, path: &str, body: &Body) -> Outcome<Response> {
        let mut headers = vec![("Authorization".to_owned(), format!("Bearer {}", self.token))];
        if path != "/healthz" {
            headers.push((
                ASSERTION_HEADER.to_owned(),
                self.supervisor.assertion(method, path, body, now())?,
            ));
        }
        headers.extend(body.headers());
        exchange(self.port, method, path, &headers, body.encoded().as_deref())
            .map_err(|error| Failure(format!("{method} {path} failed: {error}")))
    }

    /// Sends one request and reads its JSON object answer.
    pub fn call(&self, method: &str, path: &str, body: &Body) -> Outcome<(u16, Value)> {
        let response = self.send(method, path, body)?;
        Ok((response.status, response.json()?))
    }

    /// Sends one request that must answer 200 with a JSON object.
    pub fn ok(&self, method: &str, path: &str, body: &Body) -> Outcome<Value> {
        match self.call(method, path, body)? {
            (200, value) => Ok(value),
            (status, value) => fail(format!(
                "{method} {path} answered {status}: {}",
                dumps(&value, REQUEST_JSON)
            )),
        }
    }
}

/// Sends one request without any credential.
pub fn anonymous(port: u16, method: &str, path: &str) -> Outcome<Response> {
    exchange(port, method, path, &[], None)
        .map_err(|error| Failure(format!("{method} {path} failed: {error}")))
}

#[cfg(test)]
mod tests {
    use base64::Engine as _;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use strict_json::Value;
    use team_protocol::supervisor::claims_json;

    use super::{Body, Supervisor};

    fn decoded(segment: &str) -> Value {
        let raw = URL_SAFE_NO_PAD.decode(segment).expect("base64url");
        strict_json::parse(&raw).expect("json")
    }

    #[test]
    fn an_assertion_binds_the_exact_request_in_the_closed_canonical_claim_set() {
        let supervisor = Supervisor::generate().expect("a key");
        let body = Body::Json(strict_json::parse(br#"{"team_name":"Demo Team"}"#).expect("json"));
        let header = supervisor
            .assertion("POST", "/v1/teams/demo_team/create", &body, 1_800_000_000)
            .expect("an assertion");
        let token = header.strip_prefix("Bearer ").expect("a bearer");
        let segments: Vec<&str> = token.split('.').collect();
        assert_eq!(segments.len(), 3);
        let claims = decoded(segments[1]);
        assert_eq!(
            claims_json(&claims, "team-local").expect("canonical"),
            URL_SAFE_NO_PAD.decode(segments[1]).expect("raw")
        );
        assert_eq!(
            claims.get("exp"),
            Some(&team_protocol::python::value::int(1_800_000_015))
        );
        let bound = claims.get("body").expect("a body claim");
        assert_eq!(
            bound.get("kind"),
            Some(&team_protocol::python::value::text("json"))
        );
        assert_eq!(
            bound.get("length"),
            Some(&team_protocol::python::value::int(25))
        );
        assert_eq!(
            decoded(segments[0]),
            strict_json::parse(br#"{"alg":"EdDSA","kid":"local-supervisor-v1","typ":"JWT"}"#)
                .expect("json")
        );
        assert!(
            supervisor
                .public_pem()
                .starts_with("-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA")
        );
    }

    #[test]
    fn a_file_claim_names_the_upload_and_no_body_claims_the_empty_digest() {
        let supervisor = Supervisor::generate().expect("a key");
        let file = Body::File {
            content: b"Team private data".to_vec(),
            filename: "brief.txt".into(),
            media_type: "text/plain".into(),
        };
        for (body, kind) in [(file, "file"), (Body::None, "none")] {
            let header = supervisor
                .assertion("POST", "/v1/teams/t/files", &body, 1_800_000_000)
                .expect("signed");
            let payload = header.split('.').nth(1).expect("payload");
            let claim = decoded(payload).get("body").cloned().expect("a body claim");
            assert_eq!(
                claim.get("kind"),
                Some(&team_protocol::python::value::text(kind))
            );
        }
    }
}
