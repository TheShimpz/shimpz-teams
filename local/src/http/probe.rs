//! The `healthcheck` and `activity` subcommands: one authenticated, bounded `GET` to the controller on loopback, so the
//! image needs no shell or interpreter. Every failure is a nonzero exit, which the caller reads as unhealthy or
//! unknown.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::Path;
use std::time::Duration;

use strict_json::Value;
use team_protocol::python::value::{object, string};

use super::token;

/// The controller's port.
pub const PORT: u16 = 7077;
const TIMEOUT: Duration = Duration::from_secs(3);
/// The largest answer body a probe admits.
const MAX_BODY: usize = 1024;
/// The largest answer head a probe reads.
const MAX_HEAD: usize = 4096;

/// `healthcheck`: whether `GET /healthz` answers exactly `{"status": "ok", "trace_id": <32 hex>}`.
#[must_use]
pub fn healthy(token_path: &Path, port: u16) -> bool {
    get(token_path, port, "/healthz").is_some_and(|document| {
        exactly(&document, "status", "trace_id")
            .is_some_and(|(status, trace_id)| status == "ok" && is_trace_id(trace_id))
    })
}

/// `activity`: `idle` or `busy` from `GET /v1/activity`, else `None`.
#[must_use]
pub fn activity(token_path: &Path, port: u16) -> Option<&'static str> {
    let document = get(token_path, port, "/v1/activity")?;
    let (state, trace_id) = exactly(&document, "state", "trace_id")?;
    if !is_trace_id(trace_id) {
        return None;
    }
    ["idle", "busy"].into_iter().find(|known| *known == state)
}

fn is_trace_id(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// The two string members of an object that has exactly `first` and `second`.
fn exactly<'a>(document: &'a Value, first: &str, second: &str) -> Option<(&'a str, &'a str)> {
    let members = object(document)?;
    if members.len() != 2 {
        return None;
    }
    Some((
        string(document.get(first)?)?,
        string(document.get(second)?)?,
    ))
}

/// One authenticated `GET` whose answer is `200`, `application/json`, and one strict JSON document of 1 to 1024
/// bytes.
fn get(token_path: &Path, port: u16, path: &str) -> Option<Value> {
    let bearer = token::read(token_path)?;
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, TIMEOUT).ok()?;
    stream.set_read_timeout(Some(TIMEOUT)).ok()?;
    stream.set_write_timeout(Some(TIMEOUT)).ok()?;
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {}\r\nConnection: close\r\n\r\n",
        bearer.expose()
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = Vec::new();
    (&stream)
        .take((MAX_HEAD + MAX_BODY + 1) as u64)
        .read_to_end(&mut response)
        .ok()?;
    let body = admitted_body(&response)?;
    strict_json::parse(body).ok()
}

/// The body of an exact `200` JSON answer framed by one `Content-Length` of 1 to 1024.
fn admitted_body(response: &[u8]) -> Option<&[u8]> {
    let end = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")?;
    let head = std::str::from_utf8(&response[..end]).ok()?;
    let body = &response[end + 4..];
    let mut lines = head.split("\r\n");
    let status = lines.next()?;
    if !status.starts_with("HTTP/1.1 200 ") {
        return None;
    }
    let mut content_type = None;
    let mut length = None;
    for line in lines {
        let (name, value) = line.split_once(':')?;
        let value = value.trim_matches([' ', '\t']);
        if name.eq_ignore_ascii_case("content-type") {
            content_type.replace(value).is_none().then_some(())?;
        } else if name.eq_ignore_ascii_case("content-length") {
            length
                .replace(value.parse::<usize>().ok()?)
                .is_none()
                .then_some(())?;
        }
    }
    let length = length.filter(|length| (1..=MAX_BODY).contains(length))?;
    (content_type == Some("application/json") && body.len() == length).then_some(body)
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::PermissionsExt;
    use std::thread;

    use super::{activity, admitted_body, healthy};

    const TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn json_200(body: &str) -> Vec<u8> {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .into_bytes()
    }

    fn answer_once(response: Vec<u8>) -> (u16, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bound");
        let port = listener.local_addr().expect("address").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accepted");
            let mut request = [0_u8; 1024];
            let read = stream.read(&mut request).expect("read");
            stream.write_all(&response).expect("written");
            String::from_utf8_lossy(&request[..read]).into_owned()
        });
        (port, server)
    }

    fn token_file() -> tempfile::TempDir {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("token");
        std::fs::write(&path, TOKEN).expect("written");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o440)).expect("mode");
        root
    }

    #[test]
    fn a_healthy_answer_passes_and_carries_the_bearer() {
        let root = token_file();
        let (port, server) = answer_once(json_200(
            r#"{"status":"ok","trace_id":"0123456789abcdef0123456789abcdef"}"#,
        ));
        assert!(healthy(&root.path().join("token"), port));
        let request = server.join().expect("served");
        assert!(request.starts_with("GET /healthz HTTP/1.1\r\n"));
        assert!(request.contains(&format!("Authorization: Bearer {TOKEN}\r\n")));
    }

    #[test]
    fn activity_reads_only_an_exact_state() {
        let root = token_file();
        let (port, server) = answer_once(json_200(
            r#"{"state":"busy","trace_id":"0123456789abcdef0123456789abcdef"}"#,
        ));
        assert_eq!(activity(&root.path().join("token"), port), Some("busy"));
        server.join().expect("served");
        let (port, server) = answer_once(json_200(
            r#"{"state":"gone","trace_id":"0123456789abcdef0123456789abcdef"}"#,
        ));
        assert_eq!(activity(&root.path().join("token"), port), None);
        server.join().expect("served");
    }

    #[test]
    fn only_an_exact_bounded_json_200_is_admitted() {
        let ok =
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 2\r\n\r\n{}";
        assert_eq!(admitted_body(ok), Some(&b"{}"[..]));
        for refused in [
            &b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"[..],
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 3\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        ] {
            assert_eq!(admitted_body(refused), None);
        }
    }

    #[test]
    fn a_missing_token_or_closed_port_is_unhealthy() {
        let root = tempfile::tempdir().expect("temporary directory");
        assert!(!healthy(&root.path().join("token"), 1));
        let root = token_file();
        let listener = TcpListener::bind("127.0.0.1:0").expect("bound");
        let port = listener.local_addr().expect("address").port();
        drop(listener);
        assert!(!healthy(&root.path().join("token"), port));
    }
}
