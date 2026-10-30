//! How a provider call reaches its provider: a CONNECT tunnel through the Assistant's own egress
//! (`shimpz-assistant-egress:8889`, authorized by that Assistant's policy token), TLS to the provider's name, and one
//! complete, bounded, uncompressed HTTP/1.1 exchange within the call's deadline.

use std::time::Duration;

use team_core::secret::Secret;
use team_integrations::https::{self, HttpsError, Limits, Request, Response, Tunnel};
use tokio::time::Instant;

use super::{Call, MAX_RESPONSE_BYTES, MAX_RESPONSE_HEADER_BYTES, Refusal};

/// The Assistant egress on Team's private network.
pub const PROXY: (&str, u16) = ("shimpz-assistant-egress", 8889);

/// The provider-call transport.
#[derive(Debug)]
pub struct Transport {
    tunnel: Tunnel,
}

impl Transport {
    /// The Local transport: the Assistant egress and the system roots.
    #[must_use]
    pub fn local() -> Self {
        Self::new(Tunnel::new(
            PROXY.0.to_owned(),
            PROXY.1,
            Duration::from_secs(10),
            https::native_roots(),
        ))
    }

    /// A transport through `tunnel`.
    #[must_use]
    pub fn new(tunnel: Tunnel) -> Self {
        Self { tunnel }
    }

    /// Sends `call` through the egress authorized by `token` and reads its complete response by `deadline`.
    ///
    /// # Errors
    /// `unavailable` when the tunnel or TLS cannot be established; `failed` for a failed, late, compressed, oversized,
    /// or cut-short response.
    pub async fn send(
        &self,
        call: &Call,
        token: &Secret,
        deadline: Instant,
    ) -> Result<Response, Refusal> {
        if Instant::now() >= deadline {
            return Err(Refusal::new("unavailable", "deadline"));
        }
        let attempt = async {
            let stream = self
                .tunnel
                .open(&call.host, token)
                .await
                .map_err(|_| Refusal::new("unavailable", "connect"))?;
            let mut headers = call.headers.clone();
            headers.push(("accept-encoding".to_owned(), "identity".to_owned()));
            let request = Request {
                method: &call.method,
                host: &call.host,
                target: &call.target,
                headers: &headers,
                body: call.body.as_deref(),
            };
            let limits = Limits {
                header_bytes: MAX_RESPONSE_HEADER_BYTES,
                body_bytes: MAX_RESPONSE_BYTES,
            };
            https::exchange(stream, &request, limits)
                .await
                .map_err(|error| match error {
                    HttpsError::HeadersTooLarge => Refusal::new("failed", "response-headers"),
                    HttpsError::BodyTooLarge => Refusal::new("failed", "response-size"),
                    _ => Refusal::new("failed", "transport"),
                })
        };
        let response = tokio::time::timeout_at(deadline, attempt)
            .await
            .map_err(|_| Refusal::new("failed", "timeout"))??;
        let encoding = response
            .header("content-encoding")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        if !matches!(encoding.as_str(), "" | "identity") {
            return Err(Refusal::new("failed", "response-headers"));
        }
        Ok(response)
    }
}
