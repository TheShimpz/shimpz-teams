//! A minimal HTTP/1.1 client for the controller's loopback port: one request per connection (`Connection: close`),
//! the whole answer read to its end within a bound, chunked framing decoded.

use std::fmt::Write as _;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use crate::report::{Failure, Outcome, fail};

/// The largest answer read.
const MAX_RESPONSE_BYTES: u64 = 16 * 1024 * 1024;
/// How long one exchange may be silent.
const TIMEOUT: Duration = Duration::from_secs(30);

/// One answer.
#[derive(Debug)]
pub struct Response {
    /// The status code.
    pub status: u16,
    /// The header fields, names lowercased.
    pub headers: Vec<(String, String)>,
    /// The body, framing removed.
    pub body: Vec<u8>,
}

impl Response {
    /// The value of the header field `name` (lowercase), when present once or more.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    /// The body as one strict JSON object.
    pub fn json(&self) -> Outcome<strict_json::Value> {
        match strict_json::parse(&self.body) {
            Ok(value @ strict_json::Value::Object(_)) => Ok(value),
            _ => fail(format!(
                "the {} answer is not one JSON object: {}",
                self.status,
                String::from_utf8_lossy(&self.body[..self.body.len().min(512)])
            )),
        }
    }
}

/// Sends one request to `127.0.0.1:port` and reads the whole answer.
pub fn exchange(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(String, String)],
    body: Option<&[u8]>,
) -> std::io::Result<Response> {
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(TIMEOUT))?;
    stream.set_write_timeout(Some(TIMEOUT))?;
    let mut head =
        format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    for (name, value) in headers {
        let _ = write!(head, "{name}: {value}\r\n");
    }
    if let Some(body) = body {
        let _ = write!(head, "Content-Length: {}\r\n", body.len());
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    if let Some(body) = body {
        stream.write_all(body)?;
    }
    let mut raw = Vec::new();
    stream.take(MAX_RESPONSE_BYTES + 1).read_to_end(&mut raw)?;
    parse(&raw).map_err(|failure| std::io::Error::other(failure.0))
}

/// Parses one complete HTTP/1.1 answer.
pub fn parse(raw: &[u8]) -> Outcome<Response> {
    if raw.len() as u64 > MAX_RESPONSE_BYTES {
        return fail("the answer exceeds its bound");
    }
    let split = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| Failure("the answer has no complete head".into()))?;
    let head =
        std::str::from_utf8(&raw[..split]).map_err(|_| Failure("the head is not text".into()))?;
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| {
            line.strip_prefix("HTTP/1.1 ")
                .or_else(|| line.strip_prefix("HTTP/1.0 "))
        })
        .and_then(|rest| rest.get(..3))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| Failure(format!("the status line is invalid: {head:.80}")))?;
    let mut headers = Vec::new();
    for line in lines {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| Failure(format!("a header line is invalid: {line:.80}")))?;
        headers.push((name.trim().to_ascii_lowercase(), value.trim().to_owned()));
    }
    let content = &raw[split + 4..];
    let chunked = headers
        .iter()
        .any(|(name, value)| name == "transfer-encoding" && value.eq_ignore_ascii_case("chunked"));
    let body = if chunked {
        dechunk(content)?
    } else {
        content.to_vec()
    };
    Ok(Response {
        status,
        headers,
        body,
    })
}

/// The payload of a complete chunked body.
fn dechunk(mut content: &[u8]) -> Outcome<Vec<u8>> {
    let mut body = Vec::new();
    loop {
        let line_end = content
            .windows(2)
            .position(|window| window == b"\r\n")
            .ok_or_else(|| Failure("a chunk size line is incomplete".into()))?;
        let size_text = std::str::from_utf8(&content[..line_end])
            .ok()
            .and_then(|line| line.split(';').next())
            .ok_or_else(|| Failure("a chunk size is not text".into()))?;
        let size = usize::from_str_radix(size_text.trim(), 16)
            .map_err(|_| Failure(format!("a chunk size is invalid: {size_text:.20}")))?;
        content = &content[line_end + 2..];
        if size == 0 {
            return Ok(body);
        }
        if content.len() < size + 2 || &content[size..size + 2] != b"\r\n" {
            return fail("a chunk is incomplete");
        }
        body.extend_from_slice(&content[..size]);
        content = &content[size + 2..];
    }
}

#[cfg(test)]
mod tests {
    use super::parse;

    #[test]
    fn a_length_framed_answer_is_read_whole() {
        let response = parse(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\nWWW-Authenticate: Bearer realm=\"x\"\r\n\r\n{}")
            .expect("an answer");
        assert_eq!(response.status, 401);
        assert_eq!(
            response.header("www-authenticate"),
            Some("Bearer realm=\"x\"")
        );
        assert_eq!(response.body, b"{}");
        assert!(response.json().is_ok());
    }

    #[test]
    fn a_chunked_answer_is_decoded_and_a_truncated_one_refused() {
        let response = parse(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\n{\"a\r\n4;x=y\r\n\":1}\r\n0\r\n\r\n")
            .expect("an answer");
        assert_eq!(response.body, b"{\"a\":1}");
        assert!(parse(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab").is_err());
        assert!(parse(b"HTTP/1.1 200 OK\r\nno head end").is_err());
        assert!(parse(b"HTTP/2 200\r\n\r\n").is_err());
    }
}
