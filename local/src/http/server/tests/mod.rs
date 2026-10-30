//! The server over real loopback connections, with fake services, a fixed clock, and Admin's keys as fixtures.

mod answers;
mod authorization;
#[cfg(feature = "testing")]
mod controller;
mod limits;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::testing::{FakeClock, MemoryAudit};
use team_protocol::python::value::{int, object_of, text};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use super::usage::{ModelUsageSink, UsageEntry};
use super::{
    ChallengeBook, Limits, PendingChallenge, RequestContext, Server, ServerParts, current_request,
};
use crate::authority::fixture::{Keys, NOW, Signer, no_body, session, with};
use crate::http::activity::Activity;
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service, Services};

const TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

/// What a fake service saw of one call.
#[derive(Clone, Debug)]
pub struct Seen {
    pub operation: Operation,
    pub principal: Principal,
    pub body: Body,
    pub model: Option<(String, String)>,
    pub decision: Option<String>,
    pub upgrade_key: Option<String>,
    pub context: Option<RequestContext>,
}

type Respond = dyn Fn(Operation) -> BoxFuture<'static, Result<Answer, Problem>> + Send + Sync;

/// A service that records each call and answers through `respond` (by default `{"done": true}`).
pub struct Fake {
    seen: Mutex<Vec<Seen>>,
    respond: Box<Respond>,
}

impl Fake {
    pub fn answering(
        respond: impl Fn(Operation) -> BoxFuture<'static, Result<Answer, Problem>>
        + Send
        + Sync
        + 'static,
    ) -> Self {
        Self {
            seen: Mutex::new(Vec::new()),
            respond: Box::new(respond),
        }
    }

    /// Answers `{"done": true}`, streamed as one terminal record on a streamed route.
    pub fn done() -> Self {
        Self::answering(|operation| {
            Box::pin(async move {
                let body = object_of(vec![("done", Value::Bool(true))]);
                if operation.route().reply != crate::http::route::Reply::Ndjson {
                    return Ok(Answer::Json { status: 200, body });
                }
                let (sender, receiver) = tokio::sync::mpsc::channel(1);
                let _ = sender
                    .send(crate::http::service::StreamRecord::Terminal { status: 200, body })
                    .await;
                Ok(Answer::Stream(receiver))
            })
        })
    }

    pub fn seen(&self) -> Vec<Seen> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl Service for Fake {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(Seen {
                operation: call.operation(),
                principal: call.principal.clone(),
                body: call.body.clone(),
                model: call
                    .model
                    .as_ref()
                    .map(|model| (model.provider.clone(), model.api_key.expose().to_owned())),
                decision: call
                    .decision_key
                    .as_ref()
                    .map(|key| key.expose().to_owned()),
                upgrade_key: call.upgrade_key.clone(),
                context: current_request(),
            });
        (self.respond)(call.operation())
    }
}

/// Each request's Brain usage in memory, and the requests whose meter ended.
#[derive(Default)]
pub struct MemoryUsage {
    entries: Mutex<Vec<(String, UsageEntry)>>,
    pub ended: Mutex<Vec<String>>,
}

impl ModelUsageSink for MemoryUsage {
    fn record(&self, trace_id: &str, entry: UsageEntry) {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((trace_id.to_owned(), entry));
    }

    fn drain(&self, trace_id: &str) -> Vec<UsageEntry> {
        let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        let (drained, kept) = entries.drain(..).partition(|(trace, _)| trace == trace_id);
        *entries = kept;
        drained.into_iter().map(|(_, entry)| entry).collect()
    }

    fn end(&self, trace_id: &str) {
        self.ended
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(trace_id.to_owned());
    }
}

/// The pending challenge every approval finds.
pub struct Pending(pub Option<PendingChallenge>);

impl ChallengeBook for Pending {
    fn pending<'a>(
        &'a self,
        _operation: Operation,
        _team_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<PendingChallenge>, Problem>> {
        Box::pin(std::future::ready(Ok(self.0.clone())))
    }
}

/// A running server.
pub struct Harness {
    pub address: SocketAddr,
    pub usage: Arc<MemoryUsage>,
    pub keys: Keys,
    pub audit: Arc<MemoryAudit>,
    pub fake: Arc<Fake>,
    pub activity: Arc<Activity>,
}

/// Test bounds: short deadlines, the production slot count.
pub fn quick() -> Limits {
    Limits {
        slots: 16,
        header_deadline: Duration::from_millis(400),
        body_deadline: Duration::from_millis(400),
        write_idle: Duration::from_millis(400),
    }
}

impl Harness {
    pub fn start() -> Self {
        Self::with(quick(), Fake::done(), Pending(None))
    }

    pub fn with(limits: Limits, fake: Fake, pending: Pending) -> Self {
        let keys = Keys::new();
        let audit = Arc::new(MemoryAudit::default());
        let usage = Arc::new(MemoryUsage::default());
        let fake = Arc::new(fake);
        let activity = Arc::new(Activity::default());
        let service: Arc<dyn Service> = fake.clone();
        let services = Services {
            controller: Arc::clone(&service),
            teams: Arc::clone(&service),
            files: Arc::clone(&service),
            browser: Arc::clone(&service),
            inference: Arc::clone(&service),
            chat: Arc::clone(&service),
            integrations: Arc::clone(&service),
            routines: Arc::clone(&service),
            assistants: service,
        };
        let now = UNIX_EPOCH + Duration::from_secs(u64::try_from(NOW).expect("positive"));
        let server = Server::new(ServerParts {
            token: Secret::new(TOKEN.to_owned()),
            services,
            verifier: Arc::new(keys.verifier(1024)),
            challenges: Arc::new(pending),
            activity: Arc::clone(&activity),
            audit: audit.clone(),
            usage: usage.clone(),
            clock: Arc::new(FakeClock::at(now)),
            entropy: Arc::new(OsEntropy),
            limits,
        });
        let listener = super::bind("127.0.0.1:0".parse().expect("address")).expect("bound");
        let address = listener.local_addr().expect("address");
        tokio::spawn(async move { server.serve(listener, std::future::pending()).await });
        Self {
            address,
            usage,
            keys,
            audit,
            fake,
            activity,
        }
    }

    /// A Supervisor assertion header for `method` `path` with a body binding.
    pub fn supervisor(&self, method: &str, path: &str, body: Value) -> String {
        self.supervisor_with(method, path, body, |claims| claims)
    }

    pub fn supervisor_with(
        &self,
        method: &str,
        path: &str,
        body: Value,
        edit: impl FnOnce(Vec<(String, Value)>) -> Vec<(String, Value)>,
    ) -> String {
        format!(
            "Bearer {}",
            self.keys
                .supervisor
                .supervisor(&Value::Object(edit(session(method, path, body))))
        )
    }

    /// A request authorized by a fresh Supervisor assertion bound to it, its claims edited by `edit`; a body is
    /// sent as JSON.
    pub async fn as_supervisor(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &[u8],
        edit: impl FnOnce(Vec<(String, Value)>) -> Vec<(String, Value)>,
    ) -> Reply {
        let assertion = self.supervisor_with(method, path, body_claims(body), edit);
        let mut all = vec![("X-Shimpz-Supervisor", assertion.as_str())];
        if !body.is_empty() {
            all.push(JSON);
        }
        all.extend_from_slice(headers);
        self.request(method, path, &all, body).await
    }

    /// A Routine segment request signed by `signer` for `path` and `body`.
    pub async fn as_routine(&self, path: &str, body: &[u8], signer: &Signer) -> Reply {
        let claims = crate::authority::fixture::routine("POST", path, json_binding(body));
        let assertion = format!("Bearer {}", signer.routine(&Value::Object(claims)));
        self.request(
            "POST",
            path,
            &[JSON, ("X-Shimpz-Routine", &assertion)],
            body,
        )
        .await
    }

    /// Whether one audit line holds every fragment.
    pub fn journaled(&self, fragments: &[&str]) -> bool {
        self.journal()
            .iter()
            .any(|line| fragments.iter().all(|fragment| line.contains(fragment)))
    }

    /// Sends `raw` and reads the whole answer until the server closes the connection.
    pub async fn exchange(&self, raw: &[u8]) -> Reply {
        let mut stream = TcpStream::connect(self.address).await.expect("connected");
        stream.write_all(raw).await.expect("sent");
        let mut answer = Vec::new();
        let _ = tokio::time::timeout(Duration::from_secs(5), stream.read_to_end(&mut answer)).await;
        Reply::parse(&answer)
    }

    /// One request with the bearer and `headers`.
    pub async fn request(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &[u8],
    ) -> Reply {
        self.exchange(&raw(method, path, Some(TOKEN), headers, body))
            .await
    }

    /// The audit events as compact JSON lines.
    pub fn journal(&self) -> Vec<String> {
        self.audit
            .events()
            .iter()
            .map(|event| {
                let members = event.fields().to_vec();
                team_protocol::python::json::dumps(
                    &Value::Object(members),
                    team_protocol::python::json::CANONICAL,
                )
            })
            .collect()
    }
}

/// A raw HTTP/1.1 request; a body gets its `Content-Length`.
pub fn raw(
    method: &str,
    path: &str,
    token: Option<&str>,
    headers: &[(&str, &str)],
    body: &[u8],
) -> Vec<u8> {
    let mut lines = vec![
        format!("{method} {path} HTTP/1.1"),
        "Host: 127.0.0.1".to_owned(),
    ];
    if let Some(token) = token {
        lines.push(format!("Authorization: Bearer {token}"));
    }
    lines.extend(
        headers
            .iter()
            .map(|(name, value)| format!("{name}: {value}")),
    );
    if !body.is_empty() {
        lines.push(format!("Content-Length: {}", body.len()));
    }
    let mut request = (lines.join("\r\n") + "\r\n\r\n").into_bytes();
    request.extend_from_slice(body);
    request
}

/// The JSON content headers of a body.
pub const JSON: (&str, &str) = ("Content-Type", "application/json");

/// The binding of a JSON body.
pub fn json_binding(body: &[u8]) -> Value {
    use sha2::{Digest, Sha256};
    object_of(vec![
        ("kind", text("json")),
        ("length", int(i64::try_from(body.len()).expect("small"))),
        (
            "sha256",
            text(&crate::authority::key::hex(&Sha256::digest(body))),
        ),
    ])
}

/// A parsed answer; `status` 0 when the server closed without one.
#[derive(Debug)]
pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Reply {
    fn parse(answer: &[u8]) -> Self {
        let Some(end) = answer.windows(4).position(|window| window == b"\r\n\r\n") else {
            return Self {
                status: 0,
                headers: Vec::new(),
                body: answer.to_vec(),
            };
        };
        let head = String::from_utf8_lossy(&answer[..end]).into_owned();
        let mut lines = head.split("\r\n");
        let status = lines
            .next()
            .and_then(|line| line.split(' ').nth(1))
            .and_then(|code| code.parse().ok())
            .unwrap_or(0);
        let headers = lines
            .filter_map(|line| line.split_once(':'))
            .map(|(name, value)| (name.to_ascii_lowercase(), value.trim().to_owned()))
            .collect();
        Self {
            status,
            headers,
            body: answer[end + 4..].to_vec(),
        }
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    pub fn json(&self) -> Value {
        strict_json::parse(&self.body).expect("one JSON document")
    }

    /// The member `name` of the JSON answer, as text.
    pub fn text(&self, name: &str) -> Option<String> {
        match self.json().get(name) {
            Some(Value::String(text)) => Some(text.clone()),
            _ => None,
        }
    }

    /// The chunks of a chunked body, and whether the terminating chunk arrived.
    pub fn chunks(&self) -> (Vec<Vec<u8>>, bool) {
        let mut chunks = Vec::new();
        let mut rest = self.body.as_slice();
        loop {
            let Some(end) = rest.windows(2).position(|window| window == b"\r\n") else {
                return (chunks, false);
            };
            let Ok(size) = usize::from_str_radix(&String::from_utf8_lossy(&rest[..end]), 16) else {
                return (chunks, false);
            };
            if size == 0 {
                return (chunks, rest.get(end..end + 4) == Some(b"\r\n\r\n"));
            }
            let Some(chunk) = rest.get(end + 2..end + 2 + size) else {
                return (chunks, false);
            };
            chunks.push(chunk.to_vec());
            rest = &rest[end + 4 + size..];
        }
    }
}

/// The standard private headers every answer but a `101` carries.
pub fn assert_private(reply: &Reply) {
    assert_eq!(reply.header("cache-control"), Some("no-store"));
    assert_eq!(reply.header("x-content-type-options"), Some("nosniff"));
    assert_eq!(reply.header("connection"), Some("close"));
}

/// A trace id: 32 lowercase hex digits.
pub fn is_trace(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// Helpers the test modules share.
pub fn body_claims(body: &[u8]) -> Value {
    if body.is_empty() {
        no_body()
    } else {
        json_binding(body)
    }
}

/// Claims with `name` set to `value`.
pub fn edited(
    name: &'static str,
    value: Value,
) -> impl FnOnce(Vec<(String, Value)>) -> Vec<(String, Value)> {
    move |claims| with(claims, name, value)
}

/// Claims as signed.
pub fn unedited(claims: Vec<(String, Value)>) -> Vec<(String, Value)> {
    claims
}
