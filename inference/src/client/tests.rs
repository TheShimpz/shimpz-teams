use std::path::PathBuf;
use std::time::Duration;

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::secret::Secret;
use team_core::stop::Stop;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio::time::Instant;

use super::{HttpBrain, InvalidUrl, MAX_RESPONSE_BYTES, origin};
use crate::brain::{BrainClient, BrainError, BrainOperation, BrainRequest, ModelCredential};
use crate::token;

const KEY: &str = "test-key-not-real-0000";

struct Fixture {
    _directory: tempfile::TempDir,
    token_path: PathBuf,
    group: u32,
    token: String,
}

fn fixture() -> Fixture {
    let directory = tempfile::tempdir().expect("tempdir");
    let token_path = directory.path().join("runtime").join("token");
    let group = rustix::process::getegid().as_raw();
    let token = token::ensure(&token_path, group, &OsEntropy)
        .expect("token")
        .expose()
        .to_owned();
    Fixture {
        _directory: directory,
        token_path,
        group,
        token,
    }
}

/// What the fake Brain saw: the request head, its body, and whether the client then closed the connection.
#[derive(Debug, Default)]
struct Seen {
    head: String,
    body: Vec<u8>,
    closed: bool,
}

enum Reply {
    Raw(Vec<u8>),
    Hang,
}

fn http(status: u16, body: &[u8]) -> Reply {
    let mut raw = format!(
        "HTTP/1.1 {status} X\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    raw.extend_from_slice(body);
    Reply::Raw(raw)
}

async fn read_request(stream: &mut tokio::net::TcpStream) -> Seen {
    let mut raw = Vec::new();
    let mut buffer = [0_u8; 8192];
    let end = loop {
        let read = stream.read(&mut buffer).await.expect("read");
        assert_ne!(read, 0, "the request ended early");
        raw.extend_from_slice(&buffer[..read]);
        if let Some(end) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break end + 4;
        }
    };
    let head = String::from_utf8(raw[..end].to_vec()).expect("head");
    let length: usize = head
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length: ")
                .map(str::to_owned)
        })
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0);
    let mut body = raw[end..].to_vec();
    while body.len() < length {
        let read = stream.read(&mut buffer).await.expect("read");
        assert_ne!(read, 0, "the body ended early");
        body.extend_from_slice(&buffer[..read]);
    }
    Seen {
        head,
        body,
        closed: false,
    }
}

async fn serve(reply: Reply) -> (String, JoinHandle<Seen>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bound");
    let port = listener.local_addr().expect("address").port();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accepted");
        let mut seen = read_request(&mut stream).await;
        match reply {
            Reply::Raw(raw) => {
                let _ = stream.write_all(&raw).await;
                let _ = stream.shutdown().await;
            }
            Reply::Hang => {
                let mut buffer = [0_u8; 64];
                seen.closed = matches!(stream.read(&mut buffer).await, Ok(0) | Err(_));
            }
        }
        seen
    });
    (format!("http://127.0.0.1:{port}"), task)
}

fn credential() -> ModelCredential {
    ModelCredential {
        provider: "openai".to_owned(),
        model: "gpt-test".to_owned(),
        api_key: Secret::new(KEY.to_owned()),
    }
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

async fn call(
    brain: &HttpBrain,
    operation: BrainOperation,
    credential: Option<&ModelCredential>,
    deadline: Duration,
    stop: &Stop,
) -> Result<Value, BrainError> {
    brain
        .call(BrainRequest {
            operation,
            body: json(r#"{"thread_id":"team:t:1","message":"Hello"}"#),
            credential,
            deadline: Instant::now() + deadline,
            stop,
        })
        .await
}

#[tokio::test]
async fn a_call_posts_to_the_operation_path_with_the_private_token_and_the_credential_first() {
    let fixture = fixture();
    let (url, server) = serve(http(200, br#"{"status":"completed"}"#)).await;
    let brain = HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
    let answer = call(
        &brain,
        BrainOperation::Turn,
        Some(&credential()),
        Duration::from_secs(5),
        &Stop::new(),
    )
    .await;
    assert_eq!(answer, Ok(json(r#"{"status":"completed"}"#)));
    let seen = server.await.expect("served");
    assert!(
        seen.head.starts_with("POST /v1/turns HTTP/1.1\r\n"),
        "{}",
        seen.head
    );
    let lower = seen.head.to_ascii_lowercase();
    assert!(lower.contains(&format!(
        "authorization: bearer {}\r\n",
        fixture.token.to_ascii_lowercase()
    )));
    assert!(lower.contains("content-type: application/json\r\n"));
    assert_eq!(
        String::from_utf8(seen.body).expect("body"),
        format!(
            r#"{{"provider":{{"provider":"openai","model":"gpt-test","api_key":"{KEY}"}},"thread_id":"team:t:1","message":"Hello"}}"#
        )
    );
}

#[tokio::test]
async fn a_call_without_a_credential_carries_none() {
    let fixture = fixture();
    let (url, server) = serve(http(200, br#"{"status":"deleted"}"#)).await;
    let brain = HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
    let answer = call(
        &brain,
        BrainOperation::DeleteThread,
        None,
        Duration::from_secs(5),
        &Stop::new(),
    )
    .await;
    assert_eq!(answer, Ok(json(r#"{"status":"deleted"}"#)));
    let seen = server.await.expect("served");
    assert!(
        seen.head
            .starts_with("POST /v1/threads/delete HTTP/1.1\r\n")
    );
    assert_eq!(seen.body, br#"{"thread_id":"team:t:1","message":"Hello"}"#);
}

#[tokio::test]
async fn failed_answers_fail_closed_without_echoing_their_text_or_the_key() {
    let fixture = fixture();
    let deep = format!("{}{}", "[".repeat(600), "]".repeat(600));
    let cases = [
        (http(502, KEY.as_bytes()), BrainError::Rejected(502)),
        (
            http(200, format!("not-json{KEY}").as_bytes()),
            BrainError::Protocol,
        ),
        (
            http(200, br#"{"status":"a","status":"b"}"#),
            BrainError::Protocol,
        ),
        (http(200, deep.as_bytes()), BrainError::Protocol),
        (
            http(200, &vec![b' '; MAX_RESPONSE_BYTES + 1]),
            BrainError::Protocol,
        ),
        (Reply::Raw(b"garbage".to_vec()), BrainError::Unavailable),
    ];
    for (reply, expected) in cases {
        let (url, server) = serve(reply).await;
        let brain =
            HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
        let answer = call(
            &brain,
            BrainOperation::Turn,
            Some(&credential()),
            Duration::from_secs(5),
            &Stop::new(),
        )
        .await;
        assert_eq!(answer, Err(expected));
        assert!(!format!("{answer:?}").contains(KEY));
        server.await.expect("served");
    }
}

#[tokio::test]
async fn the_deadline_abandons_the_call_and_closes_its_connection() {
    let fixture = fixture();
    let (url, server) = serve(Reply::Hang).await;
    let brain = HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
    let answer = call(
        &brain,
        BrainOperation::Purpose,
        Some(&credential()),
        Duration::from_millis(200),
        &Stop::new(),
    )
    .await;
    assert_eq!(answer, Err(BrainError::Deadline));
    assert!(server.await.expect("served").closed);
}

#[tokio::test]
async fn stop_abandons_a_call_in_flight_and_closes_its_connection() {
    let fixture = fixture();
    let (url, server) = serve(Reply::Hang).await;
    let brain = HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
    let stop = Stop::new();
    let stopper = stop.clone();
    let stopping = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        stopper.stop();
    });
    let answer = call(
        &brain,
        BrainOperation::Turn,
        Some(&credential()),
        Duration::from_secs(30),
        &stop,
    )
    .await;
    assert_eq!(answer, Err(BrainError::Stopped));
    stopping.await.expect("stopped");
    assert!(server.await.expect("served").closed);
    let already = Stop::new();
    already.stop();
    let answer = call(
        &brain,
        BrainOperation::Turn,
        None,
        Duration::from_secs(30),
        &already,
    )
    .await;
    assert_eq!(answer, Err(BrainError::Stopped));
}

#[tokio::test]
async fn a_missing_unsafe_or_unreachable_brain_is_unavailable() {
    let fixture = fixture();
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bound");
    let port = listener.local_addr().expect("address").port();
    drop(listener);
    let url = format!("http://127.0.0.1:{port}");
    let brain = HttpBrain::new(&url, fixture.token_path.clone(), fixture.group).expect("client");
    let refused = call(
        &brain,
        BrainOperation::Turn,
        None,
        Duration::from_secs(5),
        &Stop::new(),
    )
    .await;
    assert_eq!(refused, Err(BrainError::Unavailable));
    let missing = HttpBrain::new(
        &url,
        fixture.token_path.with_file_name("absent"),
        fixture.group,
    )
    .expect("client");
    assert_eq!(
        call(
            &missing,
            BrainOperation::Turn,
            None,
            Duration::from_secs(5),
            &Stop::new()
        )
        .await,
        Err(BrainError::Unavailable)
    );
    let wrong_group =
        HttpBrain::new(&url, fixture.token_path.clone(), fixture.group + 1).expect("client");
    assert_eq!(
        call(
            &wrong_group,
            BrainOperation::Turn,
            None,
            Duration::from_secs(5),
            &Stop::new()
        )
        .await,
        Err(BrainError::Unavailable)
    );
}

#[test]
fn the_runtime_url_cannot_carry_credentials_paths_or_queries() {
    assert_eq!(
        origin("http://brain:8080"),
        Some(("brain".to_owned(), 8080))
    );
    assert_eq!(origin("http://Brain/"), Some(("brain".to_owned(), 80)));
    assert_eq!(origin(super::RUNTIME_URL), Some(("brain".to_owned(), 8080)));
    for url in [
        "https://brain:8080",
        "http://user:pass@brain:8080",
        "http://brain:8080/v1",
        "http://brain:8080?x=1",
        "http://brain:8080#x",
        "http://:8080",
        "http://brain:",
        "http://brain:0",
        "http://brain:99999",
        "http://brain:80a",
        "brain:8080",
    ] {
        assert_eq!(
            HttpBrain::new(url, PathBuf::from("/x"), 1).err(),
            Some(InvalidUrl),
            "{url}"
        );
    }
}
