use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;
use tokio::time::timeout;

use super::{get, query_encoded};
use crate::docker::DockerError;

/// Answers one request with `head` and then `body` (never ending when `open`), returning the request line.
async fn serve(
    head: String,
    body: Vec<u8>,
    open: bool,
) -> (Result<super::Archive, DockerError>, String) {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let path = directory.path().join("docker.sock");
    let listener = UnixListener::bind(&path).expect("bound");
    let daemon = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("one connection");
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            request.push(stream.read_u8().await.expect("the request"));
        }
        stream.write_all(head.as_bytes()).await.expect("the head");
        stream.write_all(&body).await.expect("the body");
        if open {
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
        String::from_utf8(request).expect("ASCII")
    });
    let archive = timeout(
        Duration::from_secs(5),
        get(&path, "abc123", "/opt/shimpz/manifest.json", 16),
    )
    .await
    .expect("the read stops at its bound");
    let request = if open {
        daemon.abort();
        String::new()
    } else {
        daemon.await.expect("the daemon")
    };
    (archive, request)
}

fn stat() -> String {
    STANDARD.encode(br#"{"name":"manifest.json","size":4,"mode":292,"mtime":"x","linkTarget":""}"#)
}

#[tokio::test]
async fn the_archive_carries_its_stat_and_stops_at_its_bound() {
    let head = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/x-tar\r\nX-Docker-Container-Path-Stat: {}\r\nContent-Length: 4\r\n\r\n",
        stat()
    );
    let (archive, request) = serve(head, b"tar!".to_vec(), false).await;
    let archive = archive.expect("read");
    assert_eq!(archive.tar, b"tar!");
    assert_eq!(
        archive.metadata.get("mode"),
        Some(&strict_json::Value::Number(strict_json::Number::Int(0o444)))
    );
    assert!(
        request.starts_with(
            "GET /containers/abc123/archive?path=/opt/shimpz/manifest.json HTTP/1.1\r\n"
        )
    );
    // A stream that never ends is read only to one byte past the bound.
    let head = format!(
        "HTTP/1.1 200 OK\r\nX-Docker-Container-Path-Stat: {}\r\nTransfer-Encoding: chunked\r\n\r\n",
        stat()
    );
    let chunk = format!("40\r\n{}\r\n", "a".repeat(64)).into_bytes();
    let (archive, _) = serve(head, chunk, true).await;
    assert_eq!(archive.map(|archive| archive.tar.len()), Ok(17));
}

#[tokio::test]
async fn a_missing_path_or_malformed_stat_is_refused() {
    let (archive, _) = serve(
        "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".to_owned(),
        Vec::new(),
        false,
    )
    .await;
    assert_eq!(archive.err(), Some(DockerError::NotFound));
    for header in [
        String::new(),
        "X-Docker-Container-Path-Stat: !!\r\n".to_owned(),
        format!(
            "X-Docker-Container-Path-Stat: {}\r\n",
            STANDARD.encode(b"[1]")
        ),
    ] {
        let head = format!("HTTP/1.1 200 OK\r\n{header}Content-Length: 0\r\n\r\n");
        let (archive, _) = serve(head, Vec::new(), false).await;
        assert_eq!(archive.err(), Some(DockerError::Unavailable), "{header}");
    }
    assert_eq!(query_encoded("/a b/é?&"), "/a%20b/%C3%A9%3F%26");
    let missing = std::path::Path::new("/nonexistent/docker.sock");
    assert_eq!(
        get(missing, "../x", "/", 1).await.err(),
        Some(DockerError::NotFound)
    );
}
