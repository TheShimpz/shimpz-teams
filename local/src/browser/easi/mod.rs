//! Team's client of the Space's easi.run installation (ADR-0126): the administrator API on the internal Browser network.
//!
//! Team is easi.run's one administrator. It generates the password in its own state, enrolls with it once, and keeps
//! one login for every call and stream, replaced when easi.run answers that it lapsed. It connects only to the exact
//! certificate the admin container holds, read through Docker, and sends the admin's own `Origin`, its cookie, and its
//! CSRF token. An installation enrolled with a password Team does not hold is refused, never taken over.

mod tls;

use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use http_body_util::{BodyExt as _, Full};
use hyper::body::Bytes;
use hyper::client::conn::http1::{self, SendRequest};
use hyper::header::{CONTENT_TYPE, COOKIE, HOST, ORIGIN, SET_COOKIE};
use hyper::{Method, Request, StatusCode};
use hyper_util::rt::TokioIo;
use strict_json::Value;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_protocol::python::json::{Options, dumps};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::Mutex;
use tokio::time::{Instant, timeout_at};

use crate::docker::DockerEngine;

pub use tls::PinnedDial;

/// The admin's name on the internal Browser network.
pub const HOST_NAME: &str = "easi-run-admin";
/// The admin's HTTPS port on that network.
pub const PORT: u16 = 9443;
/// The port the CLI publishes the admin on, on loopback, for easi.run's own installer.
pub const LOOPBACK_PORT: u16 = 39443;
/// The admin's container, whose certificate Team pins.
pub const ADMIN_CONTAINER: &str = "easi-run-admin-1";
/// The most one answer may hold: above the largest file a task may keep, which comes through a ticket's read.
pub const MAX_ANSWER_BYTES: usize = 32 * 1024 * 1024;
/// The bound of a call that names none: login, listings, and profile changes.
pub const CALL_SECONDS: Duration = Duration::from_secs(30);
/// The most idle connections kept open to the admin.
const POOL: usize = 4;
const CSRF_HEADER: &str = "x-csrf-token";
const COOKIE_NAME: &str = "__Host-display";
const PASSWORD_FILE: &str = "easi-password";
const COMPACT: Options = Options {
    ensure_ascii: true,
    compact: true,
    sort_keys: false,
};

/// Why a call has no answer; closed, so it never carries a page's or easi.run's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EasiError {
    /// The admin could not be reached, or the connection failed before an answer: the outcome is unknown.
    Lost,
    /// No easi.run is installed here: no session of Team's can exist.
    Absent,
    /// The answer was not what easi.run's API answers.
    Invalid,
    /// An installation enrolled with a password Team does not hold, or one asking for a second step.
    Foreign,
    /// Team's own password file could not be read or written.
    State,
}

impl EasiError {
    /// The closed cause, for the audit.
    #[must_use]
    pub const fn cause(self) -> &'static str {
        match self {
            Self::Lost => "easi-lost",
            Self::Absent => "easi-absent",
            Self::Invalid => "easi-invalid",
            Self::Foreign => "easi-foreign",
            Self::State => "easi-state",
        }
    }
}

/// One answered call.
#[derive(Debug)]
pub struct Answer {
    /// The HTTP status.
    pub status: u16,
    /// The body.
    pub body: Vec<u8>,
}

impl Answer {
    /// The body as strict JSON.
    ///
    /// # Errors
    /// [`EasiError::Invalid`] when it is not.
    pub fn json(&self) -> Result<Value, EasiError> {
        strict_json::parse(&self.body).map_err(|_| EasiError::Invalid)
    }

    /// Whether easi.run answered success.
    #[must_use]
    pub const fn ok(&self) -> bool {
        self.status >= 200 && self.status < 300
    }
}

/// A connection to the admin, plain in a test and over the pinned certificate otherwise.
pub trait Connection: AsyncRead + AsyncWrite + Send + Unpin {}

impl<T: AsyncRead + AsyncWrite + Send + Unpin> Connection for T {}

/// Opens connections to the admin.
pub trait Dial: Send + Sync {
    /// One new connection.
    fn dial(&self) -> BoxFuture<'_, Result<Pin<Box<dyn Connection>>, EasiError>>;
}

/// An upgraded stream to the admin; the bytes after its `101`.
pub struct Upgraded(TokioIo<hyper::upgrade::Upgraded>);

impl AsyncRead for Upgraded {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        buffer: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        Pin::new(&mut self.0).poll_read(context, buffer)
    }
}

impl AsyncWrite for Upgraded {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        data: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        Pin::new(&mut self.0).poll_write(context, data)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        Pin::new(&mut self.0).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        Pin::new(&mut self.0).poll_shutdown(context)
    }
}

#[derive(Clone)]
struct Login {
    cookie: String,
    csrf: String,
}

/// The client of the Space's one easi.run.
pub struct Easi {
    dial: Box<dyn Dial>,
    entropy: Arc<dyn Entropy>,
    state: PathBuf,
    login: Mutex<Option<Login>>,
    idle: Mutex<Vec<SendRequest<Full<Bytes>>>>,
}

impl std::fmt::Debug for Easi {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Easi")
    }
}

impl Easi {
    /// A client reaching the admin through `docker`'s certificate, keeping its password under `state`.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>, entropy: Arc<dyn Entropy>, state: PathBuf) -> Self {
        Self::over(Box::new(PinnedDial::new(docker)), entropy, state)
    }

    /// A client over `dial`.
    #[must_use]
    pub fn over(dial: Box<dyn Dial>, entropy: Arc<dyn Entropy>, state: PathBuf) -> Self {
        Self {
            dial,
            entropy,
            state,
            login: Mutex::new(None),
            idle: Mutex::new(Vec::new()),
        }
    }

    /// The admin's origin, which it requires of every request that changes anything.
    #[must_use]
    pub fn origin() -> String {
        format!("https://localhost:{LOOPBACK_PORT}")
    }

    /// One call on Team's login, by `deadline`; a lapsed login is replaced and the call sent again.
    ///
    /// # Errors
    /// [`EasiError`] when no answer came.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        deadline: Instant,
    ) -> Result<Answer, EasiError> {
        let attempt = async {
            let encoded = body.map(|body| dumps(body, COMPACT));
            let login = self.current().await?;
            let answer = self
                .exchange(&method, path, encoded.as_deref(), Some(&login))
                .await?
                .0;
            if answer.status != StatusCode::UNAUTHORIZED.as_u16() {
                return Ok(answer);
            }
            // easi.run refused it before acting: the same call runs again on a new login.
            let login = self.renew(&login).await?;
            self.exchange(&method, path, encoded.as_deref(), Some(&login))
                .await
                .map(|(answer, _)| answer)
        };
        timeout_at(deadline, attempt)
            .await
            .unwrap_or(Err(EasiError::Lost))
    }

    /// Opens the viewer's stream of the session on Team's login, with the viewer's own `key`, by `deadline`.
    ///
    /// # Errors
    /// [`EasiError`] when easi.run did not answer the upgrade.
    pub async fn upgrade(
        &self,
        path: &str,
        key: &str,
        deadline: Instant,
    ) -> Result<Upgraded, EasiError> {
        let attempt = async {
            let login = self.current().await?;
            let mut sender = self.connect().await?;
            let request = Request::builder()
                .method(Method::GET)
                .uri(path)
                .header(HOST, format!("{HOST_NAME}:{PORT}"))
                .header(ORIGIN, Self::origin())
                .header(COOKIE, format!("{COOKIE_NAME}={}", login.cookie))
                .header("upgrade", "websocket")
                .header("connection", "Upgrade")
                .header("sec-websocket-version", "13")
                .header("sec-websocket-key", key)
                .body(Full::new(Bytes::new()))
                .map_err(|_| EasiError::Invalid)?;
            let response = sender
                .send_request(request)
                .await
                .map_err(|_| EasiError::Lost)?;
            match response.status() {
                StatusCode::SWITCHING_PROTOCOLS => hyper::upgrade::on(response)
                    .await
                    .map(|upgraded| Upgraded(TokioIo::new(upgraded)))
                    .map_err(|_| EasiError::Lost),
                StatusCode::UNAUTHORIZED => {
                    self.renew(&login).await?;
                    Err(EasiError::Lost)
                }
                _ => Err(EasiError::Invalid),
            }
        };
        timeout_at(deadline, attempt)
            .await
            .unwrap_or(Err(EasiError::Lost))
    }

    /// Team's login, logging in when it has none.
    async fn current(&self) -> Result<Login, EasiError> {
        let mut held = self.login.lock().await;
        if let Some(login) = held.as_ref() {
            return Ok(login.clone());
        }
        let login = self.logged_in().await?;
        *held = Some(login.clone());
        Ok(login)
    }

    /// A new login in place of `lapsed`; one a concurrent call already replaced it with stands.
    async fn renew(&self, lapsed: &Login) -> Result<Login, EasiError> {
        let mut held = self.login.lock().await;
        if let Some(login) = held.as_ref()
            && login.cookie != lapsed.cookie
        {
            return Ok(login.clone());
        }
        *held = None;
        let login = self.logged_in().await?;
        *held = Some(login.clone());
        Ok(login)
    }

    async fn logged_in(&self) -> Result<Login, EasiError> {
        let (status, _) = self
            .exchange(&Method::GET, "/api/status", None, None)
            .await?;
        let enrolled = match status.json()?.get("enrolled") {
            Some(Value::Bool(enrolled)) => *enrolled,
            _ => return Err(EasiError::Invalid),
        };
        let password = self.password(enrolled)?;
        let body = dumps(
            &Value::Object(vec![("password".to_owned(), Value::String(password))]),
            COMPACT,
        );
        let path = if enrolled {
            "/api/login"
        } else {
            "/api/enroll"
        };
        let (answer, cookie) = self
            .exchange(&Method::POST, path, Some(&body), None)
            .await?;
        if !answer.ok() {
            return Err(EasiError::Foreign);
        }
        let Some(cookie) = cookie else {
            return Err(EasiError::Foreign);
        };
        match answer.json()?.get("csrf") {
            Some(Value::String(csrf)) => Ok(Login {
                cookie,
                csrf: csrf.clone(),
            }),
            _ => Err(EasiError::Invalid),
        }
    }

    /// Team's password: the one it holds, or, for an installation nobody enrolled yet, a new one kept before use.
    fn password(&self, enrolled: bool) -> Result<String, EasiError> {
        let path = self.state.join(PASSWORD_FILE);
        match std::fs::read_to_string(&path) {
            Ok(password) => Ok(password),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                if enrolled {
                    return Err(EasiError::Foreign);
                }
                let password =
                    token_hex(self.entropy.as_ref(), 32).map_err(|_| EasiError::State)?;
                self.keep(&path, &password)?;
                Ok(password)
            }
            Err(_) => Err(EasiError::State),
        }
    }

    fn keep(&self, path: &std::path::Path, password: &str) -> Result<(), EasiError> {
        use std::io::Write as _;
        let temporary = self.state.join(format!("{PASSWORD_FILE}.new"));
        let write = || -> std::io::Result<()> {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&temporary)?;
            file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
            file.write_all(password.as_bytes())?;
            file.sync_all()?;
            std::fs::rename(&temporary, path)?;
            std::fs::File::open(&self.state)?.sync_all()
        };
        write().map_err(|_| EasiError::State)
    }

    /// One request on an idle or new connection: its answer and any login cookie it set.
    async fn exchange(
        &self,
        method: &Method,
        path: &str,
        body: Option<&str>,
        login: Option<&Login>,
    ) -> Result<(Answer, Option<String>), EasiError> {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header(HOST, format!("{HOST_NAME}:{PORT}"))
            .header(ORIGIN, Self::origin());
        if body.is_some() {
            request = request.header(CONTENT_TYPE, "application/json");
        }
        if let Some(login) = login {
            request = request
                .header(COOKIE, format!("{COOKIE_NAME}={}", login.cookie))
                .header(CSRF_HEADER, login.csrf.as_str());
        }
        let request = request
            .body(Full::new(Bytes::copy_from_slice(
                body.unwrap_or_default().as_bytes(),
            )))
            .map_err(|_| EasiError::Invalid)?;
        let mut sender = self.sender().await?;
        let response = sender
            .send_request(request)
            .await
            .map_err(|_| EasiError::Lost)?;
        let status = response.status().as_u16();
        let cookie = response
            .headers()
            .get_all(SET_COOKIE)
            .iter()
            .find_map(|value| login_cookie(value.to_str().ok()?));
        let mut incoming = response.into_body();
        let mut received = Vec::new();
        while let Some(frame) = incoming.frame().await {
            let frame = frame.map_err(|_| EasiError::Lost)?;
            if let Some(data) = frame.data_ref() {
                if received.len() + data.len() > MAX_ANSWER_BYTES {
                    return Err(EasiError::Invalid);
                }
                received.extend_from_slice(data);
            }
        }
        self.give_back(sender).await;
        Ok((
            Answer {
                status,
                body: received,
            },
            cookie,
        ))
    }

    async fn sender(&self) -> Result<SendRequest<Full<Bytes>>, EasiError> {
        loop {
            let Some(mut sender) = self.idle.lock().await.pop() else {
                return self.connect().await;
            };
            if sender.ready().await.is_ok() {
                return Ok(sender);
            }
        }
    }

    async fn give_back(&self, sender: SendRequest<Full<Bytes>>) {
        let mut idle = self.idle.lock().await;
        if idle.len() < POOL {
            idle.push(sender);
        }
    }

    async fn connect(&self) -> Result<SendRequest<Full<Bytes>>, EasiError> {
        let connection = self.dial.dial().await?;
        let (sender, driver) = http1::handshake(TokioIo::new(connection))
            .await
            .map_err(|_| EasiError::Lost)?;
        tokio::spawn(async move {
            let _ = driver.with_upgrades().await;
        });
        Ok(sender)
    }
}

/// The login cookie's value in one `Set-Cookie` header, if it is that header.
fn login_cookie(header: &str) -> Option<String> {
    let (pair, _) = header.split_once(';').unwrap_or((header, ""));
    let (name, value) = pair.split_once('=')?;
    (name.trim() == COOKIE_NAME && !value.is_empty()).then(|| value.to_owned())
}

#[cfg(test)]
mod tests;
