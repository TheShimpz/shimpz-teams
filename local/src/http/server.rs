//! The controller's bounded HTTP/1.1 server on port 7077 (workstream W8).
//!
//! - **Admission:** at most [`Limits::slots`] (16) connections are served at once; one more is closed unanswered, and
//!   the listen backlog is 32. A connection holds its slot until its answer, stream, or relayed upgrade ends.
//! - **Deadlines:** the request head must arrive within 10 s of the connection's start and the body within 10 s of
//!   the head's end; each write waits at most 10 s for the peer to read. Every answer closes its connection.
//! - **Authorization:** every request presents the controller bearer. Then the route's
//!   [`Authority`](crate::http::route::Authority) decides: the bearer alone, a Routine assertion bound to the run's
//!   lease, or a Supervisor (or, for the Space reset, host-reset) assertion bound to the method, path, body, model
//!   and decision credentials, and human assurance.
//! - **Bodies:** each operation admits no body, one JSON object within its bound, or (the upload) one file read only
//!   after authorization, in the one upload slot.
//! - **Answers:** compact sorted JSON with a trace id within its bound, `Cache-Control: no-store`, `nosniff`, and
//!   `Connection: close`; NDJSON progress and one terminal record; bytes; or `101` and a byte relay.
//!
//! Every outcome is recorded in the audit journal before it is answered, metadata only; a request whose record fails
//! is refused unless its operation is journal tolerant.

mod audit;
mod handle;
pub mod io;
pub mod limits;
pub mod request;
pub mod respond;
mod stream;

use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::pin::pin;
use std::sync::Arc;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use strict_json::Value;
use team_core::audit::{AuditEvent, AuditSink};
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use tokio::net::{TcpListener, TcpSocket, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

pub use self::limits::Limits;
use crate::authority::Verifier;
use crate::http::activity::Activity;
use crate::http::operation::Operation;
use crate::http::service::{Problem, Services};

/// The Team's pending human challenge an approval may answer: its request kind and id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingChallenge {
    /// The request kind (`auth:password`, `auth:totp`, `auth:passkey`, or another kind).
    pub kind: String,
    /// The challenge id.
    pub challenge_id: String,
}

/// Where the server finds the challenge an approval answers, so the assertion must bind its human assurance (the
/// chat service for `chat-human-submit`, the Routine service for `routine-human-submit`). The service that takes the
/// answer still checks it against the challenge it holds then.
pub trait ChallengeBook: Send + Sync {
    /// The Team's pending challenge for `operation`, after expiring stale ones; `None` when nothing is pending.
    ///
    /// # Errors
    /// [`Problem`] when the pending challenge cannot be read; the request is refused with it.
    fn pending<'a>(
        &'a self,
        operation: Operation,
        team_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<PendingChallenge>, Problem>>;
}

/// What one request proved, readable by the service answering it through [`current_request`]: the analog of the
/// Python controller's request-scoped principal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestContext {
    /// The request's trace id.
    pub trace_id: String,
    /// The principal: `admin`, `admin-routine`, or the Supervisor's opaque id.
    pub principal_id: Option<String>,
    /// `machine` or `human`.
    pub principal_class: &'static str,
    /// The credential state the request was judged in.
    pub credential_state: &'static str,
    /// For a Supervisor assertion, the fingerprint of the pinned key that verified it.
    pub supervisor_key: Option<String>,
    /// For a Browser view upgrade, the viewer's admitted WebSocket key the display must answer.
    pub websocket_key: Option<String>,
}

tokio::task_local! {
    static REQUEST: RequestContext;
}

/// The context of the request whose service call is running on this task, if any. A service that answers on another
/// task (a stream's producer) reads it before it spawns that task.
#[must_use]
pub fn current_request() -> Option<RequestContext> {
    REQUEST.try_with(Clone::clone).ok()
}

/// The metadata-only audit event a service records under its request's principal: the trace id, operation, result,
/// principal, and credential state, to which the service adds only metadata (a Team, an Assistant, a detail).
#[must_use]
pub fn audit_event(context: &RequestContext, operation: &str, result: &str) -> AuditEvent {
    let text = |value: &str| Value::String(value.to_owned());
    AuditEvent::new()
        .with("trace_id", text(&context.trace_id))
        .with("operation", text(operation))
        .with("result", text(result))
        .with(
            "principal_id",
            context.principal_id.as_deref().map_or(Value::Null, text),
        )
        .with("principal_class", text(context.principal_class))
        .with("credential_state", text(context.credential_state))
}

/// Everything the server is built from.
pub struct ServerParts {
    /// The controller bearer issued at this start.
    pub token: Secret,
    /// The service groups.
    pub services: Services,
    /// The assertion verifier.
    pub verifier: Arc<Verifier>,
    /// Where approvals find their pending challenge.
    pub challenges: Arc<dyn ChallengeBook>,
    /// The activity counters.
    pub activity: Arc<Activity>,
    /// The audit journal.
    pub audit: Arc<dyn AuditSink>,
    /// The wall clock assertions are judged by.
    pub clock: Arc<dyn Clock>,
    /// The source of trace ids.
    pub entropy: Arc<dyn Entropy>,
    /// The admission and time bounds.
    pub limits: Limits,
}

/// The server, shared by every connection.
#[derive(Clone)]
pub struct Server(Arc<Inner>);

struct Inner {
    parts: ServerParts,
    slots: Arc<Semaphore>,
    upload: Semaphore,
}

impl std::fmt::Debug for Server {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Server")
    }
}

impl Server {
    /// The server of `parts`.
    #[must_use]
    pub fn new(parts: ServerParts) -> Self {
        let slots = Arc::new(Semaphore::new(parts.limits.slots));
        Self(Arc::new(Inner {
            parts,
            slots,
            upload: Semaphore::new(1),
        }))
    }

    /// Serves connections from `listener` until `shutdown` completes; a connection beyond the free slots is closed
    /// unanswered.
    pub async fn serve(&self, listener: TcpListener, shutdown: impl Future<Output = ()>) {
        let mut shutdown = pin!(shutdown);
        loop {
            let accepted = tokio::select! {
                () = &mut shutdown => return,
                accepted = listener.accept() => accepted,
            };
            let Ok((stream, _)) = accepted else {
                // A peer's aborted connection, or a transient shortage of descriptors: pause rather than spin.
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                continue;
            };
            match Arc::clone(&self.0.slots).try_acquire_owned() {
                Ok(slot) => {
                    tokio::spawn(Arc::clone(&self.0).connection(stream, Arc::new(slot)));
                }
                Err(_) => drop(stream),
            }
        }
    }
}

/// A listener on `address` with the controller's backlog of 32.
///
/// # Errors
/// The operating system's error when the address cannot be bound.
pub fn bind(address: SocketAddr) -> std::io::Result<TcpListener> {
    let socket = if address.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
    socket.set_reuseaddr(true)?;
    socket.bind(address)?;
    socket.listen(limits::BACKLOG)
}

impl Inner {
    async fn connection(self: Arc<Self>, stream: TcpStream, slot: Arc<OwnedSemaphorePermit>) {
        let limits = self.parts.limits;
        let io = TokioIo::new(io::IdleWrite::new(stream, limits.write_idle));
        let service = service_fn(move |request| {
            let server = Arc::clone(&self);
            let slot = Arc::clone(&slot);
            async move { Ok::<_, Infallible>(server.handle(request, slot).await) }
        });
        let connection = http1::Builder::new()
            .timer(TokioTimer::new())
            .header_read_timeout(limits.header_deadline)
            .keep_alive(false)
            .max_headers(limits::MAX_HEADERS)
            .max_header_size(limits::MAX_HEAD_BYTES)
            .serve_connection(io, service)
            .with_upgrades();
        // A malformed head, an expired deadline, or a peer gone concerns only this connection.
        let _ended = connection.await;
    }
}

#[cfg(test)]
mod tests;
