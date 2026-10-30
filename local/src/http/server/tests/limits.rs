//! The server's bounds: deadlines, sizes, admission slots, and malformed framing.

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use super::{Fake, Harness, JSON, Pending, TOKEN, assert_private, quick, raw};
use crate::http::server::limits::MAX_FILE_BYTES;

/// Reads until the server closes; `None` when it does not within `bound`.
async fn until_closed(stream: &mut TcpStream, bound: Duration) -> Option<Vec<u8>> {
    let mut answer = Vec::new();
    tokio::time::timeout(bound, stream.read_to_end(&mut answer))
        .await
        .ok()
        .map(|_| answer)
}

#[tokio::test]
async fn a_head_not_complete_within_its_deadline_is_closed_unanswered() {
    let harness = Harness::start();
    let mut stream = TcpStream::connect(harness.address)
        .await
        .expect("connected");
    stream
        .write_all(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
        .await
        .expect("sent");
    // Trickling bytes does not extend an absolute deadline.
    for _ in 0..3 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        let _ = stream.write_all(b"X-Trickle: 1\r\n").await;
    }
    let answer = until_closed(&mut stream, Duration::from_secs(2))
        .await
        .expect("closed");
    assert!(
        !answer.starts_with(b"HTTP/1.1 200"),
        "{:?}",
        String::from_utf8_lossy(&answer)
    );
    assert!(harness.fake.seen().is_empty());
}

#[tokio::test]
async fn a_body_not_complete_within_its_deadline_is_invalid() {
    let harness = Harness::start();
    let mut stream = TcpStream::connect(harness.address)
        .await
        .expect("connected");
    let head = format!(
        "POST /v1/routines/claim HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\
         Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{{\"long\""
    );
    stream.write_all(head.as_bytes()).await.expect("sent");
    let started = tokio::time::Instant::now();
    let answer = until_closed(&mut stream, Duration::from_secs(2))
        .await
        .expect("answered");
    assert!(started.elapsed() >= Duration::from_millis(300));
    let text = String::from_utf8_lossy(&answer);
    assert!(text.starts_with("HTTP/1.1 400 "), "{text}");
    assert!(text.contains(r#""code":"invalid-json""#), "{text}");
    assert!(harness.fake.seen().is_empty());
}

#[tokio::test]
async fn connections_beyond_the_slots_are_closed_unanswered() {
    let mut limits = quick();
    limits.slots = 2;
    limits.header_deadline = Duration::from_millis(800);
    let harness = Harness::with(limits, Fake::done(), Pending(None));
    let mut held = Vec::new();
    for _ in 0..2 {
        let mut stream = TcpStream::connect(harness.address)
            .await
            .expect("connected");
        stream
            .write_all(b"GET /healthz HTTP/1.1\r\n")
            .await
            .expect("sent");
        held.push(stream);
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    let mut excess = TcpStream::connect(harness.address)
        .await
        .expect("connected");
    let _ = excess
        .write_all(&raw("GET", "/healthz", Some(TOKEN), &[], b""))
        .await;
    let answer = until_closed(&mut excess, Duration::from_millis(500)).await;
    assert_eq!(answer.as_deref().map(<[u8]>::len), Some(0), "{answer:?}");
    // Once the held connections expire, a slot serves the next request.
    for mut stream in held {
        until_closed(&mut stream, Duration::from_secs(2))
            .await
            .expect("expired");
    }
    assert_eq!(
        harness.request("GET", "/healthz", &[], b"").await.status,
        200
    );
}

#[tokio::test]
async fn request_bodies_are_bounded_by_their_operation() {
    let harness = Harness::start();
    let large = format!(r#"{{"x":"{}"}}"#, "a".repeat(16 * 1024));
    let reply = harness
        .request("POST", "/v1/routines/claim", &[JSON], large.as_bytes())
        .await;
    assert_eq!(reply.status, 413);
    assert_eq!(reply.text("code").as_deref(), Some("body-too-large"));
    assert_eq!(
        reply.text("error").as_deref(),
        Some("request body is too large (max 16384 bytes)")
    );
    let reply = harness
        .request(
            "POST",
            "/v1/routines/notices/ack",
            &[JSON],
            large.as_bytes(),
        )
        .await;
    assert_eq!(reply.status, 200, "a notice acknowledgement admits 64 KiB");
    let oversized = (MAX_FILE_BYTES + 1).to_string();
    let reply = harness
        .request(
            "POST",
            "/v1/teams/alpha/files",
            &[
                ("Content-Type", "text/plain"),
                ("X-Shimpz-Filename", "a.txt"),
                ("Content-Length", &oversized),
            ],
            b"",
        )
        .await;
    assert_eq!(reply.status, 413);
    assert_eq!(reply.text("code").as_deref(), Some("file-too-large"));
    let reply = harness
        .request("GET", &format!("/{}", "a".repeat(600)), &[], b"")
        .await;
    assert_eq!(reply.status, 414);
    assert_eq!(reply.text("code").as_deref(), Some("path-too-long"));
}

#[tokio::test]
async fn an_oversized_head_is_refused_before_any_route() {
    let harness = Harness::start();
    let many: Vec<(String, String)> = (0..120)
        .map(|index| (format!("X-H{index}"), "1".to_owned()))
        .collect();
    let headers: Vec<(&str, &str)> = many
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    let reply = harness.request("GET", "/healthz", &headers, b"").await;
    assert_ne!(reply.status, 200);
    let huge = "a".repeat(70 * 1024);
    let reply = harness
        .request("GET", "/healthz", &[("X-Huge", &huge)], b"")
        .await;
    assert_ne!(reply.status, 200);
    assert!(harness.fake.seen().is_empty());
}

/// Sends each request and checks its status and, for a routed refusal, its code, trace, and private headers.
async fn refused(harness: &Harness, cases: &[(String, u16, Option<&str>)]) {
    for (request, status, code) in cases {
        let reply = harness.exchange(request.as_bytes()).await;
        assert_eq!(reply.status, *status, "{request:?}");
        if let Some(code) = code {
            assert_eq!(reply.text("code").as_deref(), Some(*code), "{request:?}");
            assert!(reply.text("trace_id").is_some());
            assert_private(&reply);
        }
    }
    assert!(harness.fake.seen().is_empty());
}

/// A claim request with the bearer, `headers`, and `rest` (the body and anything after the head).
fn claim(headers: &str, rest: &str) -> String {
    format!(
        "POST /v1/routines/claim HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n{headers}\r\n{rest}"
    )
}

#[tokio::test]
async fn malformed_body_framing_is_refused() {
    let harness = Harness::start();
    let json = "Content-Type: application/json\r\n";
    let cases = [
        (
            claim(
                &format!("{json}Transfer-Encoding: chunked\r\n"),
                "2\r\n{}\r\n0\r\n\r\n",
            ),
            400,
            Some("chunked-request"),
        ),
        (claim(json, ""), 411, Some("content-length")),
        // Differing lengths are refused by the parser before any route (identical repeats are one length).
        (
            claim(
                &format!("{json}Content-Length: 2\r\nContent-Length: 3\r\n"),
                "{}",
            ),
            400,
            None,
        ),
        (
            claim("Content-Type: text/plain\r\nContent-Length: 2\r\n", "{}"),
            415,
            Some("content-type"),
        ),
        (
            claim(&format!("{json}Content-Length: 2\r\n"), "[]"),
            422,
            Some("invalid-body"),
        ),
        (
            claim(&format!("{json}Content-Length: 3\r\n"), "{x}"),
            400,
            Some("invalid-json"),
        ),
        (
            format!(
                "GET /healthz HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n\r\n{{}}"
            ),
            400,
            Some("unexpected-body"),
        ),
    ];
    refused(&harness, &cases).await;
}

#[tokio::test]
async fn malformed_targets_and_heads_are_refused() {
    let harness = Harness::start();
    let get = |target: &str| {
        format!("{target} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n")
    };
    let cases = [
        (get("GET /v1//teams"), 400, Some("invalid-path")),
        (get("GET /healthz?x=1"), 400, Some("invalid-path")),
        (get("GET /heal%74hz"), 400, Some("invalid-path")),
        (get("GET /v1/nothing"), 404, Some("route-not-found")),
        (get("PUT /healthz"), 404, Some("route-not-found")),
        (get("BREW /healthz"), 404, Some("route-not-found")),
        (
            "GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n".to_owned(),
            400,
            None,
        ),
        ("NOT A REQUEST\r\n\r\n".to_owned(), 400, None),
    ];
    refused(&harness, &cases).await;
}
