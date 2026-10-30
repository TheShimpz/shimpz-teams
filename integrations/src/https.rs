//! One HTTPS exchange through a CONNECT proxy on Team's private network: the broker's path through the Account egress
//! and an Action's provider calls through its Assistant's egress.
//!
//! The proxy must answer the CONNECT with status 200 and an empty line, within 1,024 bytes and before any other byte.
//! TLS 1.2 or newer then verifies the destination's name against the trusted roots, and HTTP/1.1 carries exactly one
//! request. The response is read whole and bounded: its headers within their byte bound, its body within its own, and a
//! body cut short is refused rather than returned. The caller bounds the whole exchange with its deadline; dropping the
//! exchange drops the connection.

use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper_util::rt::TokioIo;
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, RootCertStore};
use team_core::secret::Secret;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_rustls::client::TlsStream;

const MAX_PROXY_RESPONSE_BYTES: usize = 1024;
const PROXY_READ_BYTES: usize = 256;

/// Why an exchange failed; it never carries the peer's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HttpsError {
    /// The proxy could not be reached in time.
    Connect,
    /// The proxy refused or garbled the tunnel.
    Tunnel,
    /// TLS failed.
    Tls,
    /// The HTTP exchange failed: an I/O error, a malformed or cut-short message.
    Transport,
    /// The response headers exceed their bound.
    HeadersTooLarge,
    /// The response body exceeds its bound.
    BodyTooLarge,
}

/// Where the proxy listens and which roots verify the destination.
#[derive(Clone)]
pub struct Tunnel {
    host: String,
    port: u16,
    connect_timeout: Duration,
    tls: Arc<ClientConfig>,
}

impl std::fmt::Debug for Tunnel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Tunnel")
            .field("host", &self.host)
            .field("port", &self.port)
            .finish_non_exhaustive()
    }
}

/// The system trust roots (which honour `SSL_CERT_FILE`), read now.
#[must_use]
pub fn native_roots() -> RootCertStore {
    let mut roots = RootCertStore::empty();
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    roots
}

impl Tunnel {
    /// A tunnel through the proxy at `host:port`, verifying destinations against `roots`; connecting to the proxy is
    /// bounded by `connect_timeout`.
    ///
    /// # Panics
    /// Never: the ring provider supports TLS 1.2 and 1.3.
    #[must_use]
    pub fn new(host: String, port: u16, connect_timeout: Duration, roots: RootCertStore) -> Self {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let tls = ClientConfig::builder_with_provider(provider)
            .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])
            .expect("the ring provider supports TLS 1.2 and 1.3")
            .with_root_certificates(roots)
            .with_no_client_auth();
        Self {
            host,
            port,
            connect_timeout,
            tls: Arc::new(tls),
        }
    }

    /// A verified TLS stream to `destination:443` through the proxy, authorized by `capability` as the Basic user
    /// with an empty password.
    ///
    /// # Errors
    /// [`HttpsError::Connect`], [`HttpsError::Tunnel`], or [`HttpsError::Tls`].
    pub async fn open(
        &self,
        destination: &str,
        capability: &Secret,
    ) -> Result<TlsStream<TcpStream>, HttpsError> {
        let name = ServerName::try_from(destination.to_owned()).map_err(|_| HttpsError::Tls)?;
        let connect = TcpStream::connect((self.host.as_str(), self.port));
        let mut stream = tokio::time::timeout(self.connect_timeout, connect)
            .await
            .map_err(|_| HttpsError::Connect)?
            .map_err(|_| HttpsError::Connect)?;
        let credential =
            zeroize::Zeroizing::new(STANDARD.encode(format!("{}:", capability.expose())));
        let request = zeroize::Zeroizing::new(format!(
            "CONNECT {destination}:443 HTTP/1.1\r\nHost: {destination}:443\r\nProxy-Authorization: Basic {}\r\n\r\n",
            credential.as_str()
        ));
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|_| HttpsError::Tunnel)?;
        established(&mut stream).await?;
        TlsConnector::from(Arc::clone(&self.tls))
            .connect(name, stream)
            .await
            .map_err(|_| HttpsError::Tls)
    }
}

/// Reads the proxy's reply head and requires status 200 with nothing after the empty line.
async fn established(stream: &mut TcpStream) -> Result<(), HttpsError> {
    let mut reply = Vec::new();
    let mut chunk = [0_u8; PROXY_READ_BYTES];
    let end = loop {
        if let Some(position) = reply.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        let read = stream
            .read(&mut chunk)
            .await
            .map_err(|_| HttpsError::Tunnel)?;
        if read == 0 {
            return Err(HttpsError::Tunnel);
        }
        reply.extend_from_slice(&chunk[..read]);
        if reply.len() > MAX_PROXY_RESPONSE_BYTES {
            return Err(HttpsError::Tunnel);
        }
    };
    let status = reply
        .split(|byte| *byte == b'\r')
        .next()
        .unwrap_or_default();
    let accepted = [b"HTTP/1.1 200".as_slice(), b"HTTP/1.0 200".as_slice()]
        .iter()
        .any(|prefix| {
            status
                .strip_prefix(*prefix)
                .is_some_and(|rest| rest.is_empty() || rest[0] == b' ')
        });
    if !accepted || end != reply.len() {
        return Err(HttpsError::Tunnel);
    }
    Ok(())
}

/// One request: its method, request target, headers, and optional body.
#[derive(Debug)]
pub struct Request<'a> {
    /// The method.
    pub method: &'a str,
    /// The `Host` header value.
    pub host: &'a str,
    /// The request target (path, then any query).
    pub target: &'a str,
    /// Headers besides `Host` and `Content-Length`, which the exchange sets.
    pub headers: &'a [(String, String)],
    /// The body, when the request has one.
    pub body: Option<&'a [u8]>,
}

/// The bounds a response must fit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// The largest sum of every header's name and value length.
    pub header_bytes: usize,
    /// The largest body.
    pub body_bytes: usize,
}

/// One complete response: its status, its headers (names lowercased, values read as Latin-1), and its body.
#[derive(Debug, PartialEq, Eq)]
pub struct Response {
    /// The status code.
    pub status: u16,
    /// The headers in received order.
    pub headers: Vec<(String, String)>,
    /// The whole body.
    pub body: Vec<u8>,
}

impl Response {
    /// The first value of header `name` (lowercase), if present.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(header, _)| header == name)
            .map(|(_, value)| value.as_str())
    }
}

/// Aborts the connection task when the exchange ends or is dropped, so a cancelled exchange closes its stream.
struct Driver(tokio::task::JoinHandle<()>);

impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Sends `request` on `stream` and reads its complete bounded response.
///
/// # Errors
/// [`HttpsError::Transport`] for any I/O or protocol failure, a body cut short included; the bound errors otherwise.
pub async fn exchange<S>(
    stream: S,
    request: &Request<'_>,
    limits: Limits,
) -> Result<Response, HttpsError>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Send + Unpin + 'static,
{
    let mut builder = hyper::client::conn::http1::Builder::new();
    builder
        .max_buf_size(limits.header_bytes.max(8192) + 8192)
        .max_headers(1024);
    let (mut sender, connection) = builder
        .handshake::<_, Full<Bytes>>(TokioIo::new(stream))
        .await
        .map_err(|_| HttpsError::Transport)?;
    let _driver = Driver(tokio::spawn(async move {
        let _ = connection.await;
    }));
    let response = sender
        .send_request(build(request)?)
        .await
        .map_err(|_| HttpsError::Transport)?;
    let status = response.status().as_u16();
    let headers = latin1_headers(response.headers());
    if headers
        .iter()
        .map(|(name, value)| name.len() + value.chars().count())
        .sum::<usize>()
        > limits.header_bytes
    {
        return Err(HttpsError::HeadersTooLarge);
    }
    Ok(Response {
        status,
        headers,
        body: read_body(response.into_body(), limits.body_bytes).await?,
    })
}

fn build(request: &Request<'_>) -> Result<hyper::Request<Full<Bytes>>, HttpsError> {
    let mut built = hyper::Request::builder()
        .method(request.method)
        .uri(request.target)
        .header("host", request.host);
    for (name, value) in request.headers {
        built = built.header(name.as_str(), value.as_str());
    }
    let body = request.body.map(Bytes::copy_from_slice).unwrap_or_default();
    built
        .body(Full::new(body))
        .map_err(|_| HttpsError::Transport)
}

/// Every header, its name lowercased and its value read as Latin-1, as Python's `http.client` reads it.
fn latin1_headers(headers: &hyper::HeaderMap) -> Vec<(String, String)> {
    headers
        .iter()
        .map(|(name, value)| {
            let text = value
                .as_bytes()
                .iter()
                .map(|byte| char::from(*byte))
                .collect();
            (name.as_str().to_owned(), text)
        })
        .collect()
}

/// The whole body, refused past `maximum` bytes or when the message ends early.
async fn read_body(mut body: hyper::body::Incoming, maximum: usize) -> Result<Vec<u8>, HttpsError> {
    let mut content = Vec::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| HttpsError::Transport)?;
        if let Ok(data) = frame.into_data() {
            content.extend_from_slice(&data);
            if content.len() > maximum {
                return Err(HttpsError::BodyTooLarge);
            }
        }
    }
    Ok(content)
}

#[cfg(any(test, feature = "testing"))]
pub mod testing;
