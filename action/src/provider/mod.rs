//! Team-made provider calls of one Action attempt (`action/provider.py`, ADR-0106).
//!
//! The Assistant never holds a credential. Its Action asks for each provider call on its exec channel; Team admits
//! the call against the reviewed declarations, injects every credential the Action declares for that host only on the
//! calls its reviewed routes admit, sends it through the Assistant's own egress policy, and returns only a complete,
//! credential-free response. Every dispatch is audited before the call is sent, and every outcome after it.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use strict_json::{Number, Value};
use team_core::audit::{AuditEvent, AuditSink};
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_protocol::python::json::{self, Options};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::Instant;

use crate::frames::{CallBroker, FrameError};

mod credential;
pub mod frame;
mod transport;

pub use credential::{Credential, Placement, Routes, action_credentials};
pub use transport::Transport;

/// The most calls one attempt makes.
pub const MAX_CALLS: usize = 16;
/// The longest call URL.
pub const MAX_URL_CHARACTERS: usize = 8192;
/// The most headers one call carries.
pub const MAX_HEADERS: usize = 32;
/// The largest sum of a call's header names and values.
pub const MAX_HEADER_BYTES: usize = 8 * 1024;
/// The largest call body.
pub const MAX_REQUEST_BYTES: usize = 256 * 1024;
/// The largest response body.
pub const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;
/// The largest sum of a response's header names and values.
pub const MAX_RESPONSE_HEADER_BYTES: usize = 64 * 1024;
/// The methods a call may use.
pub const METHODS: [&str; 6] = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
/// Team-wide provider calls in flight at once, which bounds their buffers within Team's memory limit.
pub const MAX_CONCURRENT_CALLS: usize = 4;
const REPLY_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

/// Team did not send, or did not complete, the call: `code` is the closed error the Action receives, `reason` the
/// audit's.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refusal {
    /// `refused`, `credential-missing`, `unavailable`, or `failed`.
    pub code: &'static str,
    /// Why.
    pub reason: &'static str,
}

impl Refusal {
    /// A refusal.
    #[must_use]
    pub const fn new(code: &'static str, reason: &'static str) -> Self {
        Self { code, reason }
    }
}

/// One admitted call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Call {
    /// The method.
    pub method: String,
    /// The reviewed host.
    pub host: String,
    /// The request target as sent: path, then any raw query.
    pub target: String,
    /// The headers, names lowercased.
    pub headers: Vec<(String, String)>,
    /// The body.
    pub body: Option<Vec<u8>>,
    /// The call's own timeout.
    pub timeout_ms: Option<u64>,
}

/// The Assistant's admitted egress policy: its policy token and reviewed hosts, read only once the attempt makes its
/// first call. `None` when the Assistant has no policy, which refuses every host.
pub trait EgressRoute: Send + Sync {
    /// The route.
    ///
    /// # Errors
    /// [`Refusal`] (`unavailable`, `egress-policy`) when the policy cannot be read.
    fn route(&self) -> Result<Option<(Secret, Vec<String>)>, Refusal>;
}

/// The Team-wide call capacity.
#[derive(Clone, Debug)]
pub struct Capacity(Arc<Semaphore>);

impl Default for Capacity {
    fn default() -> Self {
        Self(Arc::new(Semaphore::new(MAX_CONCURRENT_CALLS)))
    }
}

impl Capacity {
    /// The free slots now.
    #[must_use]
    pub fn available(&self) -> usize {
        self.0.available_permits()
    }
}

/// Who an attempt's audit records name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Attempt {
    /// The Team.
    pub team_id: String,
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action_id: String,
    /// The logical operation.
    pub operation_id: String,
}

/// Everything one attempt's calls are admitted against; no value comes from the Action.
pub struct CallScope {
    /// Who the audit names.
    pub attempt: Attempt,
    /// The placed credentials.
    pub credentials: Vec<Credential>,
    /// Hosts a declared credential of this Action goes to while Team does not hold it.
    pub missing: BTreeSet<String>,
    /// False while the Action declares an authorization its transcript does not yet hold.
    pub authorized: bool,
    /// The Assistant's egress policy.
    pub route: Box<dyn EgressRoute>,
    /// Where dispatch and outcome records go; a record that fails refuses the call.
    pub audit: Arc<dyn AuditSink>,
    /// The dispatching turn's Stop.
    pub stop: Stop,
    /// The Team-wide capacity.
    pub capacity: Capacity,
    /// How a call reaches the provider.
    pub transport: Arc<Transport>,
}

/// Answers one attempt's provider calls, strictly in order, within the attempt's deadline.
pub struct Broker {
    scope: CallScope,
    route_read: bool,
    route: Option<(Secret, Vec<String>)>,
    held: Option<OwnedSemaphorePermit>,
    calls: usize,
}

impl std::fmt::Debug for Broker {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Broker")
            .field("calls", &self.calls)
            .finish_non_exhaustive()
    }
}

/// An audit record could not be written; the attempt ends as a transport fault.
struct Unaudited;

impl Broker {
    /// A broker over `scope`.
    #[must_use]
    pub fn new(scope: CallScope) -> Self {
        Self {
            scope,
            route_read: false,
            route: None,
            held: None,
            calls: 0,
        }
    }

    async fn audit(&self, ordinal: usize, fields: Vec<(&str, Value)>) -> Result<(), Unaudited> {
        let attempt = &self.scope.attempt;
        let text = |value: &str| Value::String(value.to_owned());
        let phase = fields
            .iter()
            .find(|(name, _)| *name == "phase")
            .map(|(_, value)| value.clone());
        let failed = fields.iter().any(|(name, _)| *name == "error");
        let result = match (&phase, failed) {
            (Some(Value::String(phase)), _) if phase == "refused" => "denied",
            (_, true) => "error",
            _ => "ok",
        };
        let mut event = AuditEvent::new()
            .with("operation", text("assistant-provider-call"))
            .with("result", text(result))
            .with("team_id", text(&attempt.team_id))
            .with("assistant", text(&attempt.assistant_id))
            .with("action", text(&attempt.action_id))
            .with("operation_id", text(&attempt.operation_id))
            .with(
                "call",
                Value::Number(Number::Int(i64::try_from(ordinal).unwrap_or(i64::MAX))),
            );
        for (name, value) in fields {
            event = event.with(name, value);
        }
        self.scope.audit.record(event).await.map_err(|_| Unaudited)
    }

    /// Admits one frame and places its credentials; nothing is sent.
    fn admit(&mut self, frame: &Value) -> Result<Call, Refusal> {
        let scope = &self.scope;
        if self.calls > MAX_CALLS {
            return Err(Refusal::new("refused", "call-limit"));
        }
        if !scope.authorized {
            return Err(Refusal::new("refused", "authorization-pending"));
        }
        if !self.route_read {
            self.route = scope.route.route()?;
            self.route_read = true;
        }
        let hosts = self
            .route
            .as_ref()
            .map_or(&[][..], |(_, hosts)| hosts.as_slice());
        let call = frame::parse(frame, hosts)?;
        let owned = credential::owned_fields(&scope.credentials, &call.host);
        let named = call.headers.iter().any(|(name, _)| owned.contains(name));
        if named || !credential::query_names(&call.target).is_disjoint(&owned) {
            return Err(Refusal::new("refused", "credential-field"));
        }
        if scope.missing.contains(&call.host) {
            return Err(Refusal::new("credential-missing", "credential-missing"));
        }
        // Every credential placed on the host must admit the call: none is sent outside its reviewed routes.
        let outside = scope.credentials.iter().any(|credential| {
            credential.host == call.host && !credential.routes.admits(&call.method, &call.target)
        });
        if outside {
            return Err(Refusal::new("refused", "route"));
        }
        credential::inject(&call, &scope.credentials)
    }

    /// Waits for Team-wide capacity, observing Stop and the deadline; nothing is sent on refusal.
    async fn acquire(&self, deadline: Instant) -> Result<OwnedSemaphorePermit, Refusal> {
        let stop = &self.scope.stop;
        if stop.is_stopped() {
            return Err(Refusal::new("refused", "stopped"));
        }
        let permit = tokio::select! {
            permit = Arc::clone(&self.scope.capacity.0).acquire_owned() => permit.map_err(|_| Refusal::new("unavailable", "deadline"))?,
            () = stop.stopped() => return Err(Refusal::new("refused", "stopped")),
            () = tokio::time::sleep_until(deadline) => return Err(Refusal::new("unavailable", "deadline")),
        };
        if stop.is_stopped() {
            return Err(Refusal::new("refused", "stopped"));
        }
        Ok(permit)
    }

    /// Sends one admitted call: audited before it is sent and after, holding capacity until its reply is released.
    async fn send(
        &mut self,
        call: Call,
        ordinal: usize,
        deadline: Instant,
    ) -> Result<Result<Value, Refusal>, Unaudited> {
        let injected = self.injected(&call.host);
        let permit = match self.acquire(deadline).await {
            Ok(permit) => permit,
            Err(refusal) => return Ok(Err(refusal)),
        };
        self.held = Some(permit);
        let text = |value: &str| Value::String(value.to_owned());
        let dispatch = vec![
            ("phase", text("dispatch")),
            ("method", text(&call.method)),
            ("host", text(&call.host)),
            ("credentials", Value::Array(injected)),
        ];
        self.audit(ordinal, dispatch).await?;
        let outcome = if self.scope.stop.is_stopped() {
            // A Stop that arrived while the dispatch record was written still prevents the call.
            Err(Refusal::new("refused", "stopped"))
        } else {
            self.exchange(&call, deadline).await
        };
        let (fields, outcome) = match outcome {
            Ok((fields, reply)) => (fields, Ok(reply)),
            Err(refusal) => (outcome_refused(refusal), Err(refusal)),
        };
        self.audit(ordinal, fields).await?;
        Ok(outcome)
    }

    /// The ids of every credential placed on `host`, sorted.
    fn injected(&self, host: &str) -> Vec<Value> {
        self.scope
            .credentials
            .iter()
            .filter(|credential| credential.host == host)
            .map(|credential| credential.id.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .map(Value::String)
            .collect()
    }

    async fn exchange(
        &self,
        call: &Call,
        deadline: Instant,
    ) -> Result<(Vec<(&'static str, Value)>, Value), Refusal> {
        let deadline = call.timeout_ms.map_or(deadline, |milliseconds| {
            deadline.min(Instant::now() + Duration::from_millis(milliseconds))
        });
        let token = match self.route.as_ref() {
            Some((token, _)) => token.clone(),
            None => return Err(Refusal::new("refused", "host")),
        };
        let started = Instant::now();
        let response = tokio::select! {
            response = self.scope.transport.send(call, &token, deadline) => response?,
            () = self.scope.stop.stopped() => return Err(Refusal::new("failed", "timeout")),
        };
        if credential::echoes(&response.headers, &response.body, &self.scope.credentials) {
            return Err(Refusal::new("failed", "credential-echo"));
        }
        let int =
            |value: usize| Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)));
        let fields = vec![
            ("phase", Value::String("outcome".to_owned())),
            (
                "status",
                Value::Number(Number::Int(i64::from(response.status))),
            ),
            ("request_bytes", int(call.body.as_ref().map_or(0, Vec::len))),
            ("response_bytes", int(response.body.len())),
            (
                "duration_ms",
                int(usize::try_from(started.elapsed().as_millis()).unwrap_or(usize::MAX)),
            ),
        ];
        let reply = reply_of(&response);
        Ok((fields, reply))
    }

    async fn reply(&mut self, frame: Value, deadline: Instant) -> Result<Value, Unaudited> {
        self.calls += 1;
        let ordinal = self.calls;
        let refusal = match self.admit(&frame) {
            Ok(call) => match self.send(call, ordinal, deadline).await? {
                Ok(reply) => return Ok(reply),
                // The outcome of a dispatched call is already audited; a capacity refusal is audited as refused.
                Err(refusal) if self.held.is_some() => return Ok(error(refusal)),
                Err(refusal) => refusal,
            },
            Err(refusal) => refusal,
        };
        let text = |value: &str| Value::String(value.to_owned());
        let fields = vec![
            ("phase", text("refused")),
            ("error", text(refusal.code)),
            ("reason", text(refusal.reason)),
        ];
        self.audit(ordinal, fields).await?;
        Ok(error(refusal))
    }
}

/// The reply an Action receives: `{"status", "headers": [[name, value]...], "body": base64}`.
fn reply_of(response: &team_integrations::https::Response) -> Value {
    let headers = response
        .headers
        .iter()
        .map(|(name, value)| {
            Value::Array(vec![
                Value::String(name.clone()),
                Value::String(value.clone()),
            ])
        })
        .collect();
    Value::Object(vec![
        (
            "status".to_owned(),
            Value::Number(Number::Int(i64::from(response.status))),
        ),
        ("headers".to_owned(), Value::Array(headers)),
        (
            "body".to_owned(),
            Value::String(STANDARD.encode(&response.body)),
        ),
    ])
}

fn outcome_refused(refusal: Refusal) -> Vec<(&'static str, Value)> {
    let text = |value: &str| Value::String(value.to_owned());
    vec![
        ("phase", text("outcome")),
        ("error", text(refusal.code)),
        ("reason", text(refusal.reason)),
    ]
}

fn error(refusal: Refusal) -> Value {
    Value::Object(vec![(
        "error".to_owned(),
        Value::String(refusal.code.to_owned()),
    )])
}

impl CallBroker for Broker {
    fn answer(
        &mut self,
        frame: Value,
        deadline: Instant,
    ) -> BoxFuture<'_, Result<Vec<u8>, FrameError>> {
        Box::pin(async move {
            let reply = self
                .reply(frame, deadline)
                .await
                .map_err(|Unaudited| FrameError::Transport)?;
            Ok(json::dumps(&reply, REPLY_JSON).into_bytes())
        })
    }

    fn calls(&self) -> usize {
        self.calls
    }

    fn release(&mut self) {
        self.held = None;
    }
}

#[cfg(test)]
mod tests;
