//! Bounded HTTPS reads through the exact-allowlisted release proxy (`shimpz-assistant-release:8888`).
//!
//! Every Developers resolution and every registry read leaves Team only through this proxy, which admits CONNECT to
//! its allowlisted hosts alone. The proxy must answer the CONNECT with status 200 and an empty line, within 1,024
//! bytes and before any other byte; TLS 1.2 or newer then verifies the destination against the system roots, and one
//! HTTP/1.1 GET reads a body of at most the caller's bound. The whole exchange has one deadline, and dropping it closes
//! the connection.

use std::sync::Arc;
use std::time::Duration;

use rustls::pki_types::ServerName;
use rustls::{ClientConfig, RootCertStore};
use team_core::future::BoxFuture;
use team_integrations::https::{HttpsError, Limits, Request, exchange};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;

/// The release proxy's host on Team's network.
pub const RELEASE_PROXY_HOST: &str = "shimpz-assistant-release";
/// The release proxy's port.
pub const RELEASE_PROXY_PORT: u16 = 8888;
/// Every read's deadline.
pub const TIMEOUT: Duration = Duration::from_secs(10);
const MAX_PROXY_REPLY_BYTES: usize = 1024;
const MAX_HEADER_BYTES: usize = 64 * 1024;

/// Why a read failed; it never carries the peer's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadError {
    /// The proxy, the tunnel, TLS, or the HTTP exchange failed, or the deadline passed.
    Unavailable,
    /// The body exceeds the caller's bound.
    TooLarge,
}

/// One complete response.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Response {
    /// The status code.
    pub status: u16,
    /// The `Location` header, read as Latin-1, when present.
    pub location: Option<String>,
    /// The whole body.
    pub body: Vec<u8>,
}

/// One bounded GET of `https://<host><target>` with `headers` (besides `Host`); the seam the resolver and verifier
/// read through, so tests can script the network and observe every request.
pub trait Transport: Send + Sync {
    /// The response, with a body of at most `maximum` bytes.
    ///
    /// # Errors
    /// [`ReadError`] for every failure.
    fn get<'a>(
        &'a self,
        host: &'a str,
        target: &'a str,
        headers: &'a [(&'a str, String)],
        maximum: usize,
    ) -> BoxFuture<'a, Result<Response, ReadError>>;
}

/// The production transport through the release proxy.
#[derive(Clone)]
pub struct ReleaseProxy {
    host: String,
    port: u16,
    timeout: Duration,
    tls: Arc<ClientConfig>,
}

impl std::fmt::Debug for ReleaseProxy {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReleaseProxy")
            .field("host", &self.host)
            .field("port", &self.port)
            .finish_non_exhaustive()
    }
}

pub use team_integrations::https::native_roots;

impl ReleaseProxy {
    /// The proxy at `host:port`, verifying destinations against `roots`, every read within `timeout`.
    ///
    /// # Panics
    /// Never: the ring provider supports TLS 1.2 and 1.3.
    #[must_use]
    pub fn new(host: &str, port: u16, timeout: Duration, roots: RootCertStore) -> Self {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let tls = ClientConfig::builder_with_provider(provider)
            .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])
            .expect("the ring provider supports TLS 1.2 and 1.3")
            .with_root_certificates(roots)
            .with_no_client_auth();
        Self {
            host: host.to_owned(),
            port,
            timeout,
            tls: Arc::new(tls),
        }
    }

    /// The Team release proxy with the system roots.
    #[must_use]
    pub fn team() -> Self {
        Self::new(
            RELEASE_PROXY_HOST,
            RELEASE_PROXY_PORT,
            TIMEOUT,
            native_roots(),
        )
    }

    async fn read(
        &self,
        host: &str,
        target: &str,
        headers: &[(&str, String)],
        maximum: usize,
    ) -> Result<Response, ReadError> {
        let name = ServerName::try_from(host.to_owned()).map_err(|_| ReadError::Unavailable)?;
        let mut stream = TcpStream::connect((self.host.as_str(), self.port))
            .await
            .map_err(|_| ReadError::Unavailable)?;
        let request = format!("CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n");
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|_| ReadError::Unavailable)?;
        established(&mut stream).await?;
        let tls = TlsConnector::from(Arc::clone(&self.tls))
            .connect(name, stream)
            .await
            .map_err(|_| ReadError::Unavailable)?;
        let headers: Vec<(String, String)> = headers
            .iter()
            .map(|(name, value)| ((*name).to_owned(), value.clone()))
            .collect();
        let request = Request {
            method: "GET",
            host,
            target,
            headers: &headers,
            body: None,
        };
        let limits = Limits {
            header_bytes: MAX_HEADER_BYTES,
            body_bytes: maximum,
        };
        let response = exchange(tls, &request, limits)
            .await
            .map_err(|error| match error {
                HttpsError::BodyTooLarge => ReadError::TooLarge,
                _ => ReadError::Unavailable,
            })?;
        let location = response.header("location").map(str::to_owned);
        Ok(Response {
            status: response.status,
            location,
            body: response.body,
        })
    }
}

impl Transport for ReleaseProxy {
    fn get<'a>(
        &'a self,
        host: &'a str,
        target: &'a str,
        headers: &'a [(&'a str, String)],
        maximum: usize,
    ) -> BoxFuture<'a, Result<Response, ReadError>> {
        Box::pin(async move {
            tokio::time::timeout(self.timeout, self.read(host, target, headers, maximum))
                .await
                .map_err(|_| ReadError::Unavailable)?
        })
    }
}

/// Reads the proxy's reply head and requires status 200 with nothing after the empty line.
async fn established(stream: &mut TcpStream) -> Result<(), ReadError> {
    let mut reply = Vec::new();
    let mut chunk = [0_u8; 256];
    let end = loop {
        if let Some(position) = reply.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        let read = stream
            .read(&mut chunk)
            .await
            .map_err(|_| ReadError::Unavailable)?;
        if read == 0 || reply.len() + read > MAX_PROXY_REPLY_BYTES {
            return Err(ReadError::Unavailable);
        }
        reply.extend_from_slice(&chunk[..read]);
    };
    let status = reply
        .split(|byte| *byte == b'\r')
        .next()
        .unwrap_or_default();
    let accepted = [&b"HTTP/1.1 200"[..], &b"HTTP/1.0 200"[..]]
        .iter()
        .any(|prefix| {
            status
                .strip_prefix(*prefix)
                .is_some_and(|rest| rest.is_empty() || rest[0] == b' ')
        });
    if accepted && end == reply.len() {
        Ok(())
    } else {
        Err(ReadError::Unavailable)
    }
}
