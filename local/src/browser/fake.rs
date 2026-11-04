//! An in-memory easi.run admin for the Browser's tests, reached through Team's real client over in-process connections.

use std::collections::HashSet;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use http_body_util::{BodyExt as _, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response};
use hyper_util::rt::TokioIo;
use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _, duplex};

use super::easi::{Connection, Dial, Easi, EasiError};
use super::runtime::BrowserRuntime;

/// A profile as easi.run lists it.
#[derive(Clone, Debug)]
pub(crate) struct Profile {
    pub(crate) id: String,
    pub(crate) used: bool,
    pub(crate) unsaved: Option<&'static str>,
}

/// What the admin holds and how a test makes it fail.
#[derive(Debug, Default)]
pub(crate) struct State {
    pub(crate) password: Option<String>,
    pub(crate) logins: usize,
    pub(crate) sessions: Vec<String>,
    pub(crate) started: usize,
    pub(crate) bodies: Vec<String>,
    pub(crate) requests: Vec<String>,
    pub(crate) tools: Vec<String>,
    pub(crate) profiles: Vec<Profile>,
    pub(crate) next: usize,
    pub(crate) downloads: Vec<(String, Vec<u8>)>,
    pub(crate) streams: usize,
    /// How the admin fails: `unreachable`, `refuse-start`, `keep-stopped`, `profile-residue`, `listing-invalid`, and
    /// `lapse-login`.
    pub(crate) faults: HashSet<&'static str>,
    /// The next stop of a session with a saved profile reports this on it.
    pub(crate) save_problem: Option<&'static str>,
    /// The agent call this many calls from now gets no answer; `None` for none.
    pub(crate) lose_call_after: Option<usize>,
    /// The session's running profile, by session.
    pub(crate) in_use: Vec<(String, String)>,
}

/// The fake admin.
#[derive(Clone, Default)]
pub(crate) struct Fake(pub(crate) Arc<Mutex<State>>);

impl std::fmt::Debug for Fake {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Fake")
    }
}

const COOKIE: &str = "__Host-display=login; Secure; HttpOnly; SameSite=Strict; Path=/";
const CSRF: &str = "csrf-token";

pub(crate) fn field<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    value.get(key)
}

fn answer(status: u16, body: &str) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .body(Full::new(Bytes::copy_from_slice(body.as_bytes())))
        .expect("a response")
}

/// A tool's result as easi.run forwards it.
fn tool_result(tool: &str, arguments: &Value) -> Option<String> {
    let structured = match tool {
        "list_windows" => {
            r#"{"windows":[{"pid":7,"window_id":9,"app_name":"google-chrome"}]}"#.to_owned()
        }
        "browser_prepare" => r#"{"status":"ok"}"#.to_owned(),
        "get_browser_state" if arguments.get("snapshot_format").is_some() => {
            r#"{"page":{"url":"https://example.com/","title":"Example"},"refs":[],"outline":"Example page"}"#
                .to_owned()
        }
        "get_browser_state" => {
            r#"{"binding_quality":"exact","target_id":"t1","tabs":[{"tab_id":"tab1","active":true}]}"#
                .to_owned()
        }
        "browser_navigate" | "browser_click" | "browser_type" | "browser_pointer" => {
            r#"{"status":"ok"}"#.to_owned()
        }
        _ => return None,
    };
    Some(format!(
        r#"{{"structuredContent":{structured},"content":[]}}"#
    ))
}

impl Fake {
    pub(crate) fn state(&self) -> MutexGuard<'_, State> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A runtime over this admin, keeping its password under `state`.
    pub(crate) fn runtime(&self, state: &std::path::Path) -> BrowserRuntime {
        BrowserRuntime::new(Arc::new(self.client(state)))
    }

    /// A client of this admin, keeping its password under `state`.
    pub(crate) fn client(&self, state: &std::path::Path) -> Easi {
        Easi::over(
            Box::new(self.clone()),
            Arc::new(OsEntropy),
            state.to_path_buf(),
        )
    }

    /// One session already listed, as an earlier Team process would leave it.
    pub(crate) fn leave_session(&self, id: &str) {
        self.state().sessions.push(id.to_owned());
    }

    fn logged_in(request: &Request<Incoming>) -> bool {
        request
            .headers()
            .get("cookie")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|cookie| cookie == "__Host-display=login")
    }

    fn written(request: &Request<Incoming>) -> bool {
        let origin = request
            .headers()
            .get("origin")
            .is_some_and(|value| value == "https://localhost:39443");
        let csrf = request
            .headers()
            .get("x-csrf-token")
            .is_some_and(|value| value == CSRF);
        origin && csrf
    }

    async fn serve(self, request: Request<Incoming>) -> Option<Response<Full<Bytes>>> {
        let method = request.method().clone();
        let path = request.uri().path().to_owned();
        self.state().requests.push(format!("{method} {path}"));
        if path.starts_with("/ws/sessions/") {
            return Some(self.stream(request));
        }
        let authenticated =
            Self::logged_in(&request) && !self.state().faults.contains("lapse-login");
        let safe = method == Method::GET;
        let writable = safe || Self::written(&request);
        let body = request.into_body().collect().await.ok()?.to_bytes();
        let value = strict_json::parse(&body).ok();
        match (method.as_str(), path.as_str()) {
            ("GET", "/api/status") => {
                let enrolled = self.state().password.is_some();
                return Some(answer(200, &format!(r#"{{"enrolled":{enrolled}}}"#)));
            }
            ("POST", "/api/enroll" | "/api/login") => return Some(self.login(&path, value)),
            _ => {}
        }
        if !authenticated {
            return Some(answer(401, r#"{"code":"log-in"}"#));
        }
        if !writable {
            return Some(answer(403, r#"{"code":"forbidden"}"#));
        }
        self.authenticated(method.as_str(), &path, value, &body)
    }

    fn login(&self, path: &str, body: Option<Value>) -> Response<Full<Bytes>> {
        let Some(Value::String(password)) = body.and_then(|body| body.get("password").cloned())
        else {
            return answer(400, "{}");
        };
        let mut state = self.state();
        let admitted = match (path, &state.password) {
            ("/api/enroll", None) => {
                state.password = Some(password);
                true
            }
            ("/api/login", Some(held)) => *held == password,
            _ => false,
        };
        if !admitted {
            return answer(401, r#"{"code":"log-in"}"#);
        }
        state.logins += 1;
        state.faults.remove("lapse-login");
        Response::builder()
            .status(200)
            .header("set-cookie", COOKIE)
            .body(Full::new(Bytes::from(format!(r#"{{"csrf":"{CSRF}"}}"#))))
            .expect("a response")
    }

    fn authenticated(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
        raw: &[u8],
    ) -> Option<Response<Full<Bytes>>> {
        let parts: Vec<&str> = path.trim_start_matches('/').split('/').collect();
        Some(match (method, parts.as_slice()) {
            ("PUT", ["api", "agent-files"]) => answer(200, "{}"),
            ("GET", ["api", "sessions"]) => {
                let listed: Vec<String> = self
                    .state()
                    .sessions
                    .iter()
                    .map(|id| format!(r#"{{"id":"{id}","failed":null}}"#))
                    .collect();
                answer(200, &format!(r#"{{"sessions":[{}]}}"#, listed.join(",")))
            }
            ("POST", ["api", "sessions"]) => self.create(&body?, raw),
            ("DELETE", ["api", "sessions", id]) => self.stop(id),
            ("POST", ["api", "sessions", id, "agent"]) => return self.agent(id, &body?),
            ("GET", ["api", "sessions", _, "files", ticket]) => self.ticket(ticket),
            ("GET", ["api", "profiles"]) => self.profiles(),
            ("POST", ["api", "profiles"]) => {
                let mut state = self.state();
                state.next += 1;
                let id = format!("{:032x}", state.next);
                state.profiles.push(Profile {
                    id: id.clone(),
                    used: false,
                    unsaved: None,
                });
                answer(200, &format!(r#"{{"id":"{id}"}}"#))
            }
            ("DELETE", ["api", "profiles", id]) => {
                let mut state = self.state();
                if state.faults.contains("profile-residue") {
                    return Some(answer(500, r#"{"code":"saved-residue"}"#));
                }
                state.profiles.retain(|profile| profile.id != *id);
                answer(200, r#"{"ok":true}"#)
            }
            _ => answer(404, r#"{"code":"no-session"}"#),
        })
    }

    fn create(&self, body: &Value, raw: &[u8]) -> Response<Full<Bytes>> {
        let mut state = self.state();
        if state.faults.contains("refuse-start") {
            return answer(503, r#"{"code":"server-failed"}"#);
        }
        state.started += 1;
        let id = format!("{:012x}", state.started);
        state.sessions.push(id.clone());
        state.bodies.push(String::from_utf8_lossy(raw).into_owned());
        if let Some(Value::String(profile)) = body.get("profile") {
            state.in_use.push((id.clone(), profile.clone()));
        }
        answer(200, &format!(r#"{{"id":"{id}"}}"#))
    }

    fn stop(&self, id: &str) -> Response<Full<Bytes>> {
        let mut state = self.state();
        if !state.sessions.iter().any(|listed| listed == id) {
            return answer(404, r#"{"code":"no-session"}"#);
        }
        if state.faults.contains("keep-stopped") {
            return answer(500, r#"{"code":"stop-failed"}"#);
        }
        state.sessions.retain(|listed| listed != id);
        let problem = state.save_problem;
        let using: Vec<String> = state
            .in_use
            .iter()
            .filter(|(session, _)| session == id)
            .map(|(_, profile)| profile.clone())
            .collect();
        for profile in &mut state.profiles {
            if using.contains(&profile.id) {
                profile.used = true;
                profile.unsaved = problem;
            }
        }
        answer(200, r#"{"ok":true}"#)
    }

    fn agent(&self, id: &str, body: &Value) -> Option<Response<Full<Bytes>>> {
        let mut state = self.state();
        if !state.sessions.iter().any(|listed| listed == id) {
            return Some(answer(404, r#"{"code":"no-session"}"#));
        }
        let Some(Value::String(tool)) = body.get("tool") else {
            return Some(answer(400, "{}"));
        };
        state.tools.push(tool.clone());
        match state.lose_call_after {
            Some(0) => {
                state.lose_call_after = None;
                return None;
            }
            Some(left) => state.lose_call_after = Some(left - 1),
            None => {}
        }
        let arguments = body.get("arguments").cloned().unwrap_or(Value::Null);
        let result = match tool.as_str() {
            "list_files" if state.faults.contains("listing-invalid") => "not json".to_owned(),
            "list_files" => {
                let listed: Vec<String> = state
                    .downloads
                    .iter()
                    .enumerate()
                    .map(|(at, (name, data))| {
                        format!(
                            r#"{{"id":{},"name":"{name}","size":{}}}"#,
                            at + 1,
                            data.len()
                        )
                    })
                    .collect();
                format!(
                    r#"{{"structuredContent":{{"downloads":[{}]}},"content":[]}}"#,
                    listed.join(",")
                )
            }
            "save_download" => {
                let at = match arguments.get("download") {
                    Some(Value::Number(strict_json::Number::Int(at))) => *at,
                    _ => return Some(answer(400, "{}")),
                };
                format!(
                    r#"{{"structuredContent":{{"url":"https://localhost:39443/api/sessions/{id}/files/{at}"}},"content":[]}}"#
                )
            }
            other => tool_result(other, &arguments)?,
        };
        Some(answer(200, &result))
    }

    fn ticket(&self, ticket: &str) -> Response<Full<Bytes>> {
        let state = self.state();
        let data = ticket
            .parse::<usize>()
            .ok()
            .and_then(|at| state.downloads.get(at.checked_sub(1)?));
        match data {
            Some((_, data)) => Response::builder()
                .status(200)
                .body(Full::new(Bytes::copy_from_slice(data)))
                .expect("a response"),
            None => answer(404, "{}"),
        }
    }

    fn profiles(&self) -> Response<Full<Bytes>> {
        let listed: Vec<String> = self
            .state()
            .profiles
            .iter()
            .map(|profile| {
                let used = if profile.used { "1" } else { "null" };
                let unsaved = profile
                    .unsaved
                    .map_or("null".to_owned(), |code| format!(r#"{{"code":"{code}"}}"#));
                format!(
                    r#"{{"id":"{}","used":{used},"unsaved":{unsaved}}}"#,
                    profile.id
                )
            })
            .collect();
        answer(200, &format!(r#"{{"profiles":[{}]}}"#, listed.join(",")))
    }

    /// Answers the upgrade and echoes the stream.
    fn stream(&self, request: Request<Incoming>) -> Response<Full<Bytes>> {
        let id = request
            .uri()
            .path()
            .trim_start_matches("/ws/sessions/")
            .to_owned();
        if !self.state().sessions.contains(&id) {
            return answer(404, r#"{"code":"no-session"}"#);
        }
        if !Self::logged_in(&request) {
            return answer(401, r#"{"code":"log-in"}"#);
        }
        self.state().streams += 1;
        let upgrade = hyper::upgrade::on(request);
        tokio::spawn(async move {
            if let Ok(upgraded) = upgrade.await {
                let mut io = TokioIo::new(upgraded);
                let mut buffer = [0_u8; 1024];
                while let Ok(read) = io.read(&mut buffer).await {
                    if read == 0 || io.write_all(&buffer[..read]).await.is_err() {
                        break;
                    }
                }
            }
        });
        Response::builder()
            .status(101)
            .header("upgrade", "websocket")
            .header("connection", "Upgrade")
            .body(Full::new(Bytes::new()))
            .expect("a response")
    }
}

impl Dial for Fake {
    fn dial(&self) -> BoxFuture<'_, Result<Pin<Box<dyn Connection>>, EasiError>> {
        Box::pin(async move {
            if self.state().faults.contains("unreachable") {
                return Err(EasiError::Lost);
            }
            let (near, far) = duplex(1 << 20);
            let fake = self.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request| {
                    let fake = fake.clone();
                    async move {
                        // No answer at all is a connection that drops: the error ends it.
                        fake.serve(request)
                            .await
                            .ok_or_else(|| std::io::Error::other("the connection dropped"))
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(far), service)
                    .with_upgrades()
                    .await;
            });
            Ok(Box::pin(near) as Pin<Box<dyn Connection>>)
        })
    }
}
