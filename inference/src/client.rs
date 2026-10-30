//! The Brain runtime client: one bounded HTTP/1.1 POST per operation over the private `brain_runtime` network.
//!
//! Each call reads the shared Brain token under its custody, places the request-only model credential where the
//! operation carries it, and posts compact JSON. The call is abandoned at its deadline and at once when its Stop is
//! set: the connection is closed, so the Brain sees the disconnect and cancels its provider I/O. The answer must be
//! status 200 and one strict JSON document of at most 1 MiB; no failure carries the Brain's text or the key.

use std::path::PathBuf;
use std::time::Duration;

use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper_util::rt::TokioIo;
use strict_json::Value;
use team_core::future::BoxFuture;
use team_protocol::python::json::dumps;
use tokio::net::TcpStream;
use tokio::task::JoinHandle;
use tokio::time::{sleep_until, timeout};

use crate::brain::{BrainClient, BrainError, BrainRequest};
use crate::runtime::{BODY, with_credential};
use crate::token;

/// The Brain runtime's address on the private network.
pub const RUNTIME_URL: &str = "http://brain:8080";
/// A turn's whole answer: its reply, Action requests, clarification, memory, Routine record, usage, and envelope.
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
/// Brain shares a local network with Team, so a connection is established quickly or not at all.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// The base URL is not a plain `http://host[:port]` origin.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidUrl;

impl std::fmt::Display for InvalidUrl {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Brain runtime URL is invalid")
    }
}

impl std::error::Error for InvalidUrl {}

/// The host and port of a plain `http://host[:port]` origin with an empty or `/` path; anything else is refused.
fn origin(base_url: &str) -> Option<(String, u16)> {
    let rest = base_url.strip_prefix("http://")?;
    let authority = rest.strip_suffix('/').unwrap_or(rest);
    if authority.contains(['/', '?', '#', '@']) {
        return None;
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => {
            let digits = !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit());
            (
                host,
                if digits {
                    port.parse::<u16>().ok().filter(|port| *port > 0)?
                } else {
                    return None;
                },
            )
        }
        None => (authority, 80),
    };
    let label =
        |character: char| character.is_ascii_alphanumeric() || matches!(character, '.' | '-');
    (!host.is_empty() && host.chars().all(label)).then(|| (host.to_ascii_lowercase(), port))
}

/// The Brain runtime over HTTP.
#[derive(Clone, Debug)]
pub struct HttpBrain {
    host: String,
    port: u16,
    token_path: PathBuf,
    token_group: u32,
}

/// Aborts the connection's driver task, and so closes the connection, however the call ends.
struct Driver(JoinHandle<()>);

impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

impl HttpBrain {
    /// A client of the Brain at `base_url`, authenticated with the token at `token_path` in group `token_group`.
    ///
    /// # Errors
    /// [`InvalidUrl`] when `base_url` is not a plain `http://host[:port]` origin.
    pub fn new(base_url: &str, token_path: PathBuf, token_group: u32) -> Result<Self, InvalidUrl> {
        let (host, port) = origin(base_url).ok_or(InvalidUrl)?;
        Ok(Self {
            host,
            port,
            token_path,
            token_group,
        })
    }

    fn host_header(&self) -> String {
        if self.port == 80 {
            self.host.clone()
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }

    async fn exchange(
        &self,
        path: &'static str,
        bearer: String,
        body: Vec<u8>,
    ) -> Result<Value, BrainError> {
        let stream = timeout(
            CONNECT_TIMEOUT,
            TcpStream::connect((self.host.as_str(), self.port)),
        )
        .await
        .map_err(|_| BrainError::Unavailable)?
        .map_err(|_| BrainError::Unavailable)?;
        let (mut sender, connection) =
            hyper::client::conn::http1::handshake::<_, Full<Bytes>>(TokioIo::new(stream))
                .await
                .map_err(|_| BrainError::Unavailable)?;
        let _driver = Driver(tokio::spawn(async move {
            let _ = connection.await;
        }));
        let request = hyper::Request::builder()
            .method("POST")
            .uri(path)
            .header("host", self.host_header())
            .header("authorization", bearer)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(body)))
            .map_err(|_| BrainError::Unavailable)?;
        let response = sender
            .send_request(request)
            .await
            .map_err(|_| BrainError::Unavailable)?;
        let status = response.status().as_u16();
        let raw = read_bounded(response.into_body()).await?;
        if status != 200 {
            return Err(BrainError::Rejected(status));
        }
        strict_json::parse(&raw).map_err(|_| BrainError::Protocol)
    }
}

/// The whole answer body, refused past [`MAX_RESPONSE_BYTES`].
async fn read_bounded(mut body: Incoming) -> Result<Vec<u8>, BrainError> {
    let mut content = Vec::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| BrainError::Unavailable)?;
        if let Ok(data) = frame.into_data() {
            content.extend_from_slice(&data);
            if content.len() > MAX_RESPONSE_BYTES {
                return Err(BrainError::Protocol);
            }
        }
    }
    Ok(content)
}

impl BrainClient for HttpBrain {
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>> {
        Box::pin(async move {
            if request.stop.is_stopped() {
                return Err(BrainError::Stopped);
            }
            let token = token::read(&self.token_path, self.token_group)
                .map_err(|_| BrainError::Unavailable)?;
            let bearer = format!("Bearer {}", token.expose());
            let body = match request.credential {
                Some(credential) => with_credential(request.body, credential),
                None => request.body,
            };
            let payload = dumps(&body, BODY).into_bytes();
            tokio::select! {
                biased;
                () = request.stop.stopped() => Err(BrainError::Stopped),
                () = sleep_until(request.deadline) => Err(BrainError::Deadline),
                answer = self.exchange(request.operation.path(), bearer, payload) => answer,
            }
        })
    }
}

#[cfg(test)]
mod tests;
