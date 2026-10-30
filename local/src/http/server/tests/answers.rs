//! How the server frames and records each kind of answer.

use std::sync::Arc;
use std::time::Duration;

use team_core::future::BoxFuture;
use team_protocol::python::value::{int, object_of, text};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{Notify, mpsc};

use super::{Fake, Harness, Pending, Reply, TOKEN, assert_private, is_trace, quick, raw, unedited};
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Problem, Relay, StreamRecord, Upgraded};

const RUN: &str = "/v1/teams/alpha/routines/runs/0123456789abcdef0123456789abcdef/segment";
const VIEW: &str = "/v1/teams/alpha/browser/views/0123456789abcdef0123456789abcdef/stream";

fn answering(
    respond: impl Fn(Operation) -> Result<Answer, Problem> + Send + Sync + 'static,
) -> Fake {
    let respond = Arc::new(respond);
    Fake::answering(move |operation| {
        let respond = Arc::clone(&respond);
        Box::pin(async move { respond(operation) })
    })
}

fn harness(fake: Fake) -> Harness {
    Harness::with(quick(), fake, Pending(None))
}

#[tokio::test]
async fn a_json_answer_carries_its_trace_and_stays_within_its_bound() {
    let harness = harness(answering(|operation| {
        let size = if operation == Operation::RoutineNotices {
            200 * 1024
        } else {
            10
        };
        let body = object_of(vec![
            ("text", text(&"é".repeat(size / 2))),
            ("trace_id", text("forged")),
        ]);
        Ok(Answer::Json { status: 200, body })
    }));
    let reply = harness.request("GET", "/v1/activity", &[], b"").await;
    assert_eq!(reply.status, 200);
    assert_private(&reply);
    assert_eq!(reply.header("content-type"), Some("application/json"));
    let trace = reply.text("trace_id").expect("trace");
    assert!(is_trace(&trace));
    assert_eq!(
        reply.body,
        format!(r#"{{"text":"ééééé","trace_id":"{trace}"}}"#).into_bytes()
    );
    let reply = harness
        .request("GET", "/v1/routines/notices", &[], b"")
        .await;
    assert_eq!(reply.status, 500);
    assert_eq!(reply.body, br#"{"error":"response exceeded its limit"}"#);
}

#[tokio::test]
async fn a_problem_is_projected_without_upstream_text() {
    let harness = harness(answering(|operation| match operation {
        Operation::RoutineNotices => Err(Problem {
            status: 503,
            message: "file preview capacity is busy".into(),
            code: Some("file-preview-busy"),
            retry_after_ms: Some(250),
        }),
        Operation::Activity => panic!("upstream detail sk-test-secret"),
        _ => Err(Problem::new(
            409,
            "Team already has an active chat turn",
            "chat-active",
        )),
    }));
    let reply = harness
        .request("GET", "/v1/routines/notices", &[], b"")
        .await;
    assert_eq!(reply.status, 503);
    assert_eq!(reply.json().get("retry_after_ms"), Some(&int(250)));
    assert_eq!(reply.json().get("code"), Some(&text("file-preview-busy")));
    let reply = harness.request("GET", "/v1/activity", &[], b"").await;
    assert_eq!(reply.status, 500);
    assert_eq!(reply.text("error").as_deref(), Some("internal error"));
    assert_eq!(reply.json().get("code"), None);
    assert!(!String::from_utf8_lossy(&reply.body).contains("sk-test"));
    assert!(harness.journaled(&[
        r#""operation":"activity""#,
        r#""result":"error""#,
        r#""detail":"internal-error""#
    ]));
    assert!(!harness.journaled(&["sk-test"]));
}

#[tokio::test]
async fn an_unrecorded_request_never_reaches_its_service() {
    let harness = Harness::start();
    harness.audit.fail(true);
    let reply = harness.request("GET", "/healthz", &[], b"").await;
    assert_eq!(reply.status, 500);
    assert_eq!(reply.text("error").as_deref(), Some("internal error"));
    assert!(harness.fake.seen().is_empty());
    let reply = harness
        .exchange(&raw("GET", "/healthz", None, &[], b""))
        .await;
    assert_eq!(reply.status, 500);
    // A journal-tolerant operation proceeds unrecorded (ADR-0120).
    let path = "/v1/teams/alpha/browser/sign-in/cancel";
    let body = br#"{"sign_in_session_id":"x"}"#;
    let reply = harness
        .as_supervisor("POST", path, &[], body, unedited)
        .await;
    assert_eq!(reply.status, 200);
    assert_eq!(harness.fake.seen().len(), 1);
}

fn progress(seq: i64) -> StreamRecord {
    StreamRecord::Progress(object_of(vec![
        ("seq", int(seq)),
        ("phase", text("model")),
        ("state", text("started")),
    ]))
}

fn streaming(records: Vec<StreamRecord>) -> Fake {
    let records = Arc::new(records);
    Fake::answering(move |_| {
        let records = Arc::clone(&records);
        Box::pin(async move {
            let (sender, receiver) = mpsc::channel(4);
            tokio::spawn(async move {
                for record in records.iter() {
                    let _ = sender.send(record.clone()).await;
                }
            });
            Ok(Answer::Stream(receiver))
        })
    })
}

async fn segment(records: Vec<StreamRecord>) -> (Harness, Reply) {
    let harness = harness(streaming(records));
    let reply = harness.as_routine(RUN, b"{}", &harness.keys.routine).await;
    (harness, reply)
}

#[tokio::test]
async fn a_stream_writes_progress_then_one_recorded_terminal_record() {
    let paused = object_of(vec![("status", text("paused"))]);
    let (harness, reply) = segment(vec![
        progress(1),
        StreamRecord::Terminal {
            status: 428,
            body: paused,
        },
    ])
    .await;
    assert_eq!(reply.status, 200);
    assert_private(&reply);
    assert_eq!(reply.header("content-type"), Some("application/x-ndjson"));
    assert_eq!(reply.header("transfer-encoding"), Some("chunked"));
    let (chunks, complete) = reply.chunks();
    assert!(complete);
    assert_eq!(
        chunks[0],
        b"{\"phase\":\"model\",\"seq\":1,\"state\":\"started\",\"type\":\"progress\"}\n"
    );
    let terminal = strict_json::parse(&chunks[1]).expect("terminal");
    assert_eq!(terminal.get("status"), Some(&int(428)));
    let Some(strict_json::Value::String(trace)) =
        terminal.get("body").and_then(|body| body.get("trace_id"))
    else {
        panic!("no trace id");
    };
    assert!(harness.journaled(&[r#""operation":"routine-run""#, r#""result":"ok""#, trace]));
}

#[tokio::test]
async fn a_stream_that_ends_without_its_terminal_record_fails_closed() {
    let (_harness, reply) = segment(vec![progress(1)]).await;
    let (chunks, complete) = reply.chunks();
    assert!(complete);
    let terminal = strict_json::parse(&chunks[1]).expect("terminal");
    assert_eq!(terminal.get("status"), Some(&int(500)));
    let error = terminal.get("body").and_then(|body| body.get("error"));
    assert_eq!(error, Some(&text("internal error")));
}

#[tokio::test]
async fn a_record_outside_the_protocol_ends_the_stream_incomplete() {
    let invalid =
        StreamRecord::Progress(object_of(vec![("seq", int(1)), ("phase", text("secret"))]));
    let terminal = StreamRecord::Terminal {
        status: 200,
        body: object_of(vec![]),
    };
    let (_harness, reply) = segment(vec![invalid, terminal]).await;
    assert_eq!(reply.status, 200);
    let (chunks, complete) = reply.chunks();
    assert_eq!(chunks.len(), 0);
    assert!(!complete);
}

#[tokio::test]
async fn an_unrecordable_terminal_record_never_completes_the_stream() {
    let release = Arc::new(Notify::new());
    let gate = Arc::clone(&release);
    let fake = Fake::answering(move |_| {
        let gate = Arc::clone(&gate);
        Box::pin(async move {
            let (sender, receiver) = mpsc::channel(4);
            tokio::spawn(async move {
                let _ = sender.send(progress(1)).await;
                gate.notified().await;
                let body = object_of(vec![]);
                let _ = sender
                    .send(StreamRecord::Terminal { status: 200, body })
                    .await;
            });
            Ok(Answer::Stream(receiver))
        })
    });
    let harness = Arc::new(harness(fake));
    let reply = tokio::spawn({
        let harness = Arc::clone(&harness);
        async move { harness.as_routine(RUN, b"{}", &harness.keys.routine).await }
    });
    for _ in 0..500 {
        if !harness.fake.seen().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    harness.audit.fail(true);
    release.notify_one();
    let reply = reply.await.expect("joined");
    assert_eq!(reply.status, 200);
    let (chunks, complete) = reply.chunks();
    assert_eq!(chunks.len(), 1, "only the progress record was written");
    assert!(!complete);
}

#[tokio::test]
async fn bytes_are_framed_exactly() {
    let harness = harness(answering(|_| {
        Ok(Answer::Binary {
            media_type: "image/png",
            body: b"\x89PNG-bytes".to_vec(),
            headers: vec![("Repr-Digest", "sha-256=:AAAA:".to_owned())],
        })
    }));
    let path = "/v1/teams/alpha/assistants/demo/icon";
    let reply = harness.as_supervisor("GET", path, &[], b"", unedited).await;
    assert_eq!(reply.status, 200);
    assert_private(&reply);
    assert_eq!(reply.header("content-type"), Some("image/png"));
    assert_eq!(reply.header("content-length"), Some("10"));
    assert_eq!(reply.header("repr-digest"), Some("sha-256=:AAAA:"));
    assert_eq!(reply.body, b"\x89PNG-bytes");
    assert!(harness.journaled(&[r#""assistant":"demo""#]));
}

struct Echo;

impl Relay for Echo {
    fn run(self: Box<Self>, client: Box<dyn Upgraded>) -> BoxFuture<'static, ()> {
        Box::pin(async move {
            let (mut reader, mut writer) = tokio::io::split(client);
            let _ = tokio::io::copy(&mut reader, &mut writer).await;
        })
    }
}

/// The headers of one admitted view upgrade.
const UPGRADE: [(&str, &str); 5] = [
    ("Upgrade", "websocket"),
    ("Connection", "Upgrade"),
    ("Sec-WebSocket-Version", "13"),
    ("Sec-WebSocket-Protocol", "binary"),
    ("Sec-WebSocket-Key", "AAAAAAAAAAAAAAAAAAAAAA=="),
];

/// Opens the view upgrade and reads the answer head.
async fn upgraded(harness: &Harness) -> (TcpStream, String) {
    let assertion = harness.supervisor("GET", VIEW, super::body_claims(b""));
    let mut headers = vec![("X-Shimpz-Supervisor", assertion.as_str())];
    headers.extend(UPGRADE);
    let mut stream = TcpStream::connect(harness.address)
        .await
        .expect("connected");
    stream
        .write_all(&raw("GET", VIEW, Some(TOKEN), &headers, b""))
        .await
        .expect("sent");
    let mut head = vec![0; 512];
    let read = tokio::time::timeout(Duration::from_secs(2), stream.read(&mut head))
        .await
        .expect("answered")
        .expect("read");
    (
        stream,
        String::from_utf8_lossy(&head[..read]).to_ascii_lowercase(),
    )
}

#[tokio::test]
async fn an_accepted_upgrade_is_answered_101_then_relayed() {
    let harness = harness(Fake::answering(|_| {
        Box::pin(async { Ok(Answer::Relay(Box::new(Echo))) })
    }));
    let (mut stream, head) = upgraded(&harness).await;
    assert!(
        head.starts_with("http/1.1 101 switching protocols\r\n"),
        "{head}"
    );
    assert!(head.contains("sec-websocket-accept: icx+yqv66kxgm0fcwalwlflwtai=\r\n"));
    assert!(head.contains("sec-websocket-protocol: binary\r\n"));
    assert!(head.contains("upgrade: websocket\r\n"));
    stream.write_all(b"frame-bytes").await.expect("relayed");
    let mut echoed = [0; 11];
    tokio::time::timeout(Duration::from_secs(2), stream.read_exact(&mut echoed))
        .await
        .expect("echoed")
        .expect("read");
    assert_eq!(&echoed, b"frame-bytes");
    let key = harness.fake.seen()[0]
        .context
        .as_ref()
        .and_then(|context| context.websocket_key.clone());
    assert_eq!(key.as_deref(), Some("AAAAAAAAAAAAAAAAAAAAAA=="));
}

#[tokio::test]
async fn an_upgrade_outside_its_closed_form_is_refused() {
    let harness = harness(Fake::answering(|_| {
        Box::pin(async { Ok(Answer::Relay(Box::new(Echo))) })
    }));
    let reply = harness
        .as_supervisor("GET", VIEW, &[("Upgrade", "websocket")], b"", unedited)
        .await;
    assert_eq!(reply.status, 400);
    assert_eq!(
        reply.text("code").as_deref(),
        Some("browser-view-upgrade-invalid")
    );
    assert!(harness.fake.seen().is_empty());
}

const CONTENT: &[u8] = b"plain text content";

async fn upload(harness: &Harness, binding: strict_json::Value) -> Reply {
    let path = "/v1/teams/alpha/files";
    let assertion = harness.supervisor("POST", path, binding);
    let headers = [
        ("X-Shimpz-Supervisor", assertion.as_str()),
        ("Content-Type", "text/plain"),
        ("X-Shimpz-Filename", "notes%20final.txt"),
    ];
    harness.request("POST", path, &headers, CONTENT).await
}

fn file_binding() -> strict_json::Value {
    object_of(vec![
        ("kind", text("file")),
        ("length", int(18)),
        ("filename", text("notes final.txt")),
        ("media_type", text("text/plain")),
    ])
}

/// Files answer only once released.
fn held(release: &Arc<Notify>) -> Fake {
    let gate = Arc::clone(release);
    Fake::answering(move |_| {
        let gate = Arc::clone(&gate);
        Box::pin(async move {
            gate.notified().await;
            Ok(Answer::Json {
                status: 200,
                body: object_of(vec![]),
            })
        })
    })
}

#[tokio::test]
async fn one_upload_at_a_time_reads_its_content_after_authorization() {
    let release = Arc::new(Notify::new());
    let harness = Arc::new(harness(held(&release)));
    let first = tokio::spawn({
        let harness = Arc::clone(&harness);
        async move { upload(&harness, file_binding()).await }
    });
    for _ in 0..500 {
        if !harness.fake.seen().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let second = upload(&harness, file_binding()).await;
    assert_eq!(second.status, 429);
    assert_eq!(second.text("code").as_deref(), Some("file-upload-busy"));
    release.notify_one();
    assert_eq!(first.await.expect("joined").status, 200);
    let seen = harness.fake.seen();
    let Body::File(file) = &seen[0].body else {
        panic!("a file");
    };
    assert_eq!(
        (file.filename.as_str(), file.media_type.as_str()),
        ("notes final.txt", "text/plain")
    );
    assert_eq!(file.content, CONTENT);
    // An upload whose assertion is refused never has its content read.
    assert_eq!(upload(&harness, super::body_claims(b"")).await.status, 403);
    assert_eq!(harness.fake.seen().len(), 1);
}

#[tokio::test]
async fn a_supervisor_mutation_keeps_the_team_busy() {
    let harness = Harness::start();
    assert_eq!(harness.activity.state(), "idle");
    let reply = harness
        .request("POST", "/v1/routines/claim", &[super::JSON], b"{}")
        .await;
    assert_eq!(reply.status, 200);
    assert_eq!(
        harness.activity.state(),
        "idle",
        "a machine mutation leaves no quiet window"
    );
    let body = br#"{"team_name":"Alpha"}"#;
    let reply = harness
        .as_supervisor("PATCH", "/v1/teams/alpha", &[], body, unedited)
        .await;
    assert_eq!(reply.status, 200);
    assert_eq!(harness.activity.state(), "busy");
}
