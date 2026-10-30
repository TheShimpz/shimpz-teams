//! The closed HTTPS client of the platform OAuth broker on shimpz.com (`integrations/broker.py`).
//!
//! Team starts, claims, refreshes, and revokes Cloudflare grants without ever receiving the OAuth Client Secret. The
//! host, paths, scopes, response shapes, and lack of redirects are fixed here; every call reaches the broker only
//! through the Account egress with its capability, re-read with custody each time, within a 30-second total deadline.

use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_protocol::python::json::{self, Options};

use crate::capability::CapabilitySource;
use crate::https::{self, Limits, Request, Tunnel};
use crate::providers;
use crate::token::{MAX_TOKEN_BYTES, TokenSet, is_token};

/// The broker's origin.
pub const BROKER_ORIGIN: &str = "https://shimpz.com";
/// The broker's host.
pub const BROKER_HOST: &str = "shimpz.com";
/// The callback modes the broker serves.
pub const CALLBACK_MODES: [&str; 3] = ["loopback", "local-domain", "out-of-band"];
/// The largest broker response body.
pub const MAX_RESPONSE_BYTES: usize = 32 * 1024;
/// The bound on connecting to the Account egress.
pub const HTTP_TIMEOUT: Duration = Duration::from_secs(10);
/// The bound on one whole exchange, whatever phase a slow peer stalls it in.
pub const TOTAL_TIMEOUT: Duration = Duration::from_secs(30);
/// The Account egress on Team's private network.
pub const PROXY: (&str, u16) = ("shimpz-account-egress", 8889);
const USER_AGENT: &str = "shimpz-local-controller/1";
const COMPACT_ASCII: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

/// A broker operation failed; it never reflects private response data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrokerError {
    /// A request input is invalid (provider, scopes, state, challenge, claim, verifier, token, lease, or callback).
    Invalid,
    /// The URL is not one reviewed broker operation.
    Endpoint,
    /// The Account egress capability is unavailable.
    Capability,
    /// The broker could not be reached or answered within the deadline.
    Unavailable,
    /// The broker answered something other than success.
    Failed,
    /// The broker's answer is not the closed response shape.
    Response,
}

impl std::fmt::Display for BrokerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Invalid => "an OAuth broker request input is invalid",
            Self::Endpoint => "the OAuth broker endpoint is invalid",
            Self::Capability => "the OAuth broker proxy capability is unavailable",
            Self::Unavailable => "the OAuth broker is unavailable",
            Self::Failed => "the OAuth broker operation failed",
            Self::Response => "the OAuth broker response is invalid",
        })
    }
}

impl std::error::Error for BrokerError {}

/// One broker answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrokerResponse {
    /// The status.
    pub status: u16,
    /// The `Content-Type`.
    pub content_type: String,
    /// The body, at most [`MAX_RESPONSE_BYTES`] bytes.
    pub body: Vec<u8>,
}

/// Sends one POST to a broker operation.
pub trait BrokerTransport: Send + Sync {
    /// POSTs `body` with `headers` to `url`.
    ///
    /// # Errors
    /// [`BrokerError`] for an unreviewed URL, an unavailable capability, or a failed exchange.
    fn post<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        body: Vec<u8>,
    ) -> BoxFuture<'a, Result<BrokerResponse, BrokerError>>;
}

/// The production transport: only the three reviewed operations, only through the Account egress.
#[derive(Debug)]
pub struct FixedTransport {
    tunnel: Tunnel,
    capability: CapabilitySource,
    total: Duration,
}

impl FixedTransport {
    /// The Local transport: the Account egress, its capability, and the system roots.
    #[must_use]
    pub fn local() -> Self {
        Self::new(
            Tunnel::new(
                PROXY.0.to_owned(),
                PROXY.1,
                HTTP_TIMEOUT,
                https::native_roots(),
            ),
            CapabilitySource::local(),
            TOTAL_TIMEOUT,
        )
    }

    /// A transport through `tunnel` authorized by `capability`, each exchange bounded by `total`.
    #[must_use]
    pub fn new(tunnel: Tunnel, capability: CapabilitySource, total: Duration) -> Self {
        Self {
            tunnel,
            capability,
            total,
        }
    }

    async fn exchange(
        &self,
        path: &str,
        headers: &[(String, String)],
        body: &[u8],
    ) -> Result<BrokerResponse, BrokerError> {
        let capability = self
            .capability
            .read()
            .map_err(|_| BrokerError::Capability)?;
        let attempt = async {
            let stream = self.tunnel.open(BROKER_HOST, &capability).await.ok()?;
            let request = Request {
                method: "POST",
                host: BROKER_HOST,
                target: path,
                headers,
                body: Some(body),
            };
            let limits = Limits {
                header_bytes: 64 * 1024,
                body_bytes: MAX_RESPONSE_BYTES,
            };
            https::exchange(stream, &request, limits).await.ok()
        };
        let response = tokio::time::timeout(self.total, attempt)
            .await
            .ok()
            .flatten()
            .ok_or(BrokerError::Unavailable)?;
        Ok(BrokerResponse {
            status: response.status,
            content_type: response
                .header("content-type")
                .unwrap_or_default()
                .to_owned(),
            body: response.body,
        })
    }
}

/// The path of a reviewed broker operation URL.
fn operation_path(url: &str) -> Option<&str> {
    let path = url.strip_prefix(BROKER_ORIGIN)?;
    ["claim", "refresh", "revoke"]
        .iter()
        .any(|operation| path.strip_prefix("/api/oauth/cloudflare/") == Some(operation))
        .then_some(path)
}

impl BrokerTransport for FixedTransport {
    fn post<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        body: Vec<u8>,
    ) -> BoxFuture<'a, Result<BrokerResponse, BrokerError>> {
        Box::pin(async move {
            let path = operation_path(url).ok_or(BrokerError::Endpoint)?;
            self.exchange(path, headers, &body).await
        })
    }
}

/// Whether `text` is a state or challenge binding: 43 base64url characters.
#[must_use]
pub fn binding(text: &str) -> bool {
    text.len() == 43
        && text
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

/// Whether `text` is a broker lease: `l3.<key id>.<expiry>.<access>.<refresh>.<scopes>.<signature>`.
#[must_use]
pub fn lease(text: &str) -> bool {
    let base64url = |part: &str, length: usize| {
        part.len() == length
            && part
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    };
    let parts: Vec<&str> = text.split('.').collect();
    parts.len() == 7
        && parts[0] == "l3"
        && base64url(parts[1], 11)
        && parts[2].len() == 10
        && parts[2].bytes().all(|byte| byte.is_ascii_digit())
        && parts[3..].iter().all(|part| base64url(part, 43))
}

/// Whether `text` is a broker claim: 64 lowercase hexadecimal characters.
#[must_use]
pub fn claim(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// `urllib.parse.quote_plus` of ASCII text: unreserved characters stay, a space becomes `+`, the rest `%XX`.
fn quote_plus(text: &str) -> String {
    use std::fmt::Write as _;
    let mut quoted = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                quoted.push(char::from(byte));
            }
            b' ' => quoted.push('+'),
            _ => {
                let _ = write!(quoted, "%{byte:02X}");
            }
        }
    }
    quoted
}

/// The reviewed Cloudflare intent of `provider` and `scopes`.
fn cloudflare(provider: &str, scopes: &[String]) -> Result<Vec<String>, BrokerError> {
    let intent = providers::intent(provider, scopes).map_err(|_| BrokerError::Invalid)?;
    if intent.provider.id != "cloudflare" {
        return Err(BrokerError::Invalid);
    }
    Ok(intent.scopes)
}

/// The broker client.
pub struct BrokerClient {
    transport: Box<dyn BrokerTransport>,
}

impl std::fmt::Debug for BrokerClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BrokerClient(shimpz.com)")
    }
}

impl BrokerClient {
    /// A client over `transport`.
    #[must_use]
    pub fn new(transport: Box<dyn BrokerTransport>) -> Self {
        Self { transport }
    }

    /// The broker's start URL for one consent: only the public PKCE fields, the sorted scopes, and the callback mode.
    ///
    /// # Errors
    /// [`BrokerError::Invalid`] for an unreviewed provider or scopes, a malformed binding, or an unknown mode.
    pub fn authorization_url(
        &self,
        provider: &str,
        state: &str,
        code_challenge: &str,
        scopes: &[String],
        callback_mode: &str,
    ) -> Result<String, BrokerError> {
        let scopes = cloudflare(provider, scopes)?;
        if !CALLBACK_MODES.contains(&callback_mode) || !binding(state) || !binding(code_challenge) {
            return Err(BrokerError::Invalid);
        }
        Ok(format!(
            "{BROKER_ORIGIN}/api/oauth/cloudflare/start?state={}&code_challenge={}&scope={}&callback={}",
            quote_plus(state),
            quote_plus(code_challenge),
            quote_plus(&scopes.join(" ")),
            quote_plus(callback_mode)
        ))
    }

    async fn post(&self, operation: &str, payload: Value) -> Result<Value, BrokerError> {
        if !["claim", "refresh", "revoke"].contains(&operation) {
            return Err(BrokerError::Invalid);
        }
        let headers = [
            ("Accept", "application/json"),
            ("Content-Type", "application/json"),
            ("User-Agent", USER_AGENT),
        ]
        .map(|(name, value)| (name.to_owned(), value.to_owned()));
        let body = zeroize::Zeroizing::new(json::dumps(&payload, COMPACT_ASCII).into_bytes());
        let url = format!("{BROKER_ORIGIN}/api/oauth/cloudflare/{operation}");
        let response = self.transport.post(&url, &headers, body.to_vec()).await?;
        object(&response)
    }

    /// Exchanges a broker claim for the grant's tokens, proving the PKCE verifier.
    ///
    /// # Errors
    /// [`BrokerError`] for an invalid input, a failed exchange, or a response outside its closed shape.
    pub async fn claim(
        &self,
        provider: &str,
        claim_id: &str,
        state: &str,
        code_verifier: &Secret,
        scopes: &[String],
    ) -> Result<TokenSet, BrokerError> {
        let scopes = cloudflare(provider, scopes)?;
        if !claim(claim_id) || !binding(state) || !is_token(code_verifier.expose(), 43, 128) {
            return Err(BrokerError::Invalid);
        }
        let payload = Value::Object(vec![
            ("claim".to_owned(), Value::String(claim_id.to_owned())),
            ("state".to_owned(), Value::String(state.to_owned())),
            (
                "code_verifier".to_owned(),
                Value::String(code_verifier.expose().to_owned()),
            ),
        ]);
        tokens(&self.post("claim", payload).await?, scopes)
    }

    /// Refreshes a grant under its broker lease.
    ///
    /// # Errors
    /// As [`BrokerClient::claim`].
    pub async fn refresh(
        &self,
        provider: &str,
        refresh_token: &Secret,
        broker_lease: &Secret,
        scopes: &[String],
    ) -> Result<TokenSet, BrokerError> {
        let scopes = cloudflare(provider, scopes)?;
        if !lease(broker_lease.expose()) || !is_token(refresh_token.expose(), 16, MAX_TOKEN_BYTES) {
            return Err(BrokerError::Invalid);
        }
        let payload = Value::Object(vec![
            (
                "refresh_token".to_owned(),
                Value::String(refresh_token.expose().to_owned()),
            ),
            (
                "broker_lease".to_owned(),
                Value::String(broker_lease.expose().to_owned()),
            ),
            (
                "scopes".to_owned(),
                Value::Array(scopes.iter().cloned().map(Value::String).collect()),
            ),
        ]);
        tokens(&self.post("refresh", payload).await?, scopes)
    }

    /// Revokes a grant; the broker answers exactly `{"revoked": true}`.
    ///
    /// # Errors
    /// As [`BrokerClient::claim`].
    pub async fn revoke(
        &self,
        provider: &str,
        token: &Secret,
        broker_lease: &Secret,
    ) -> Result<(), BrokerError> {
        let reviewed = providers::resolve(provider).map_err(|_| BrokerError::Invalid)?;
        if reviewed.id != "cloudflare"
            || !lease(broker_lease.expose())
            || !is_token(token.expose(), 16, MAX_TOKEN_BYTES)
        {
            return Err(BrokerError::Invalid);
        }
        let payload = Value::Object(vec![
            ("token".to_owned(), Value::String(token.expose().to_owned())),
            (
                "broker_lease".to_owned(),
                Value::String(broker_lease.expose().to_owned()),
            ),
        ]);
        let answer = self.post("revoke", payload).await?;
        if answer != Value::Object(vec![("revoked".to_owned(), Value::Bool(true))]) {
            return Err(BrokerError::Response);
        }
        Ok(())
    }
}

/// The broker's one JSON object: a 200 `application/json` answer with a non-empty bounded strict body.
fn object(response: &BrokerResponse) -> Result<Value, BrokerError> {
    let media = response
        .content_type
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if response.status != 200
        || media != "application/json"
        || response.body.is_empty()
        || response.body.len() > MAX_RESPONSE_BYTES
    {
        return Err(BrokerError::Failed);
    }
    match strict_json::parse(&response.body) {
        Ok(value @ Value::Object(_)) => Ok(value),
        _ => Err(BrokerError::Response),
    }
}

/// The closed token response: exactly its five fields, a bounded lifetime, the requested scopes, a lease, and tokens.
fn tokens(value: &Value, scopes: Vec<String>) -> Result<TokenSet, BrokerError> {
    let Value::Object(members) = value else {
        return Err(BrokerError::Response);
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    let text = |key: &str| match value.get(key) {
        Some(Value::String(text)) => Some(text.as_str()),
        _ => None,
    };
    let token = |key| text(key).filter(|text| is_token(text, 16, MAX_TOKEN_BYTES));
    let shaped = names
        == [
            "access_token",
            "broker_lease",
            "expires_in",
            "refresh_token",
            "scopes",
        ]
        && returned_scopes(value.get("scopes")).is_some_and(|returned| returned == scopes);
    let expires_in = match value.get("expires_in") {
        Some(Value::Number(strict_json::Number::Int(seconds)))
            if (30..=31_536_000).contains(seconds) =>
        {
            *seconds
        }
        _ => return Err(BrokerError::Response),
    };
    let broker_lease = text("broker_lease").filter(|text| lease(text));
    let (Some(access), Some(refresh), Some(broker_lease), true) = (
        token("access_token"),
        token("refresh_token"),
        broker_lease,
        shaped,
    ) else {
        return Err(BrokerError::Response);
    };
    Ok(TokenSet {
        access_token: Secret::new(access.to_owned()),
        refresh_token: Some(Secret::new(refresh.to_owned())),
        scopes,
        expires_in,
        broker_lease: Some(Secret::new(broker_lease.to_owned())),
    })
}

fn returned_scopes(value: Option<&Value>) -> Option<Vec<&str>> {
    let Some(Value::Array(items)) = value else {
        return None;
    };
    items
        .iter()
        .map(|item| match item {
            Value::String(scope) => Some(scope.as_str()),
            _ => None,
        })
        .collect()
}

#[cfg(test)]
mod tests;
