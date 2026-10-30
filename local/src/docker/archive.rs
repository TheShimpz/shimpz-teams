//! `GET /containers/{id}/archive?path=…` on its own connection: bollard does not expose the stat header, and the
//! tar stream must stop at the caller's bound instead of being collected whole.

use std::fmt::Write as _;
use std::path::Path;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use http_body_util::{BodyExt as _, Empty};
use hyper::header::HOST;
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use strict_json::Value;
use tokio::net::UnixStream;

use super::{Archive, DockerError};

/// The response header that carries the path's base64 JSON stat.
const STAT_HEADER: &str = "x-docker-container-path-stat";

/// `value` percent-encoded for a query: every byte but unreserved characters and `/`.
fn query_encoded(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~' | b'/') {
            encoded.push(char::from(byte));
        } else {
            // Writing to a String cannot fail.
            let _ = write!(encoded, "%{byte:02X}");
        }
    }
    encoded
}

/// The stat header's strict JSON object.
fn metadata(header: Option<&hyper::header::HeaderValue>) -> Result<Value, DockerError> {
    let decoded = header
        .and_then(|value| STANDARD.decode(value.as_bytes()).ok())
        .ok_or(DockerError::Unavailable)?;
    match strict_json::parse(&decoded) {
        Ok(object @ Value::Object(_)) => Ok(object),
        _ => Err(DockerError::Unavailable),
    }
}

/// The stat and the first `bound` + 1 tar bytes of an answered request.
async fn read(
    response: hyper::Response<hyper::body::Incoming>,
    bound: usize,
) -> Result<Archive, DockerError> {
    match response.status() {
        StatusCode::OK => {}
        StatusCode::NOT_FOUND => return Err(DockerError::NotFound),
        _ => return Err(DockerError::Unavailable),
    }
    let metadata = metadata(response.headers().get(STAT_HEADER))?;
    let mut body = response.into_body();
    let mut tar = Vec::new();
    while tar.len() <= bound {
        let Some(frame) = body.frame().await else {
            break;
        };
        let frame = frame.map_err(|_| DockerError::Unavailable)?;
        if let Ok(data) = frame.into_data() {
            let room = bound + 1 - tar.len();
            tar.extend_from_slice(&data[..data.len().min(room)]);
        }
    }
    Ok(Archive { metadata, tar })
}

/// The archive of `path` in `container` over a new connection to `socket`, read no further than `bound` + 1 bytes.
///
/// # Errors
/// [`DockerError::NotFound`] for Docker's 404; [`DockerError::Unavailable`] otherwise.
pub async fn get(
    socket: &Path,
    container: &str,
    path: &str,
    bound: usize,
) -> Result<Archive, DockerError> {
    let id_valid = !container.is_empty()
        && container
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'));
    if !id_valid {
        return Err(DockerError::NotFound);
    }
    let stream = UnixStream::connect(socket)
        .await
        .map_err(|_| DockerError::Unavailable)?;
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|_| DockerError::Unavailable)?;
    let driver = tokio::spawn(connection);
    let request = Request::get(format!(
        "/containers/{container}/archive?path={}",
        query_encoded(path)
    ))
    .header(HOST, "docker")
    .body(Empty::<&'static [u8]>::new())
    .map_err(|_| DockerError::Unavailable)?;
    let answered = match sender.send_request(request).await {
        Ok(response) => read(response, bound).await,
        Err(_) => Err(DockerError::Unavailable),
    };
    // The rest of a stream beyond the bound is never read: the connection closes with it.
    driver.abort();
    answered
}

#[cfg(test)]
mod tests;
