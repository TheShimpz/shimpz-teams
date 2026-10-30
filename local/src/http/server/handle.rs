//! One request, from its bearer to its answer.

use std::borrow::Cow;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::UNIX_EPOCH;

use http_body_util::BodyExt;
use hyper::Request;
use hyper::body::Incoming;
use hyper::http::request::Parts;
use hyper::upgrade::OnUpgrade;
use hyper_util::rt::TokioIo;
use strict_json::Value;
use team_core::entropy::token_hex;
use team_core::secret::Secret;
use team_inference::brain::ModelCredential;
use team_protocol::browser_view::{INVALID_UPGRADE, SUBPROTOCOL, accept};
use team_protocol::identifiers::canonical_team_id;
use team_protocol::supervisor::{ASSERTION_HEADER, ROUTINE_ASSERTION_HEADER};
use tokio::sync::{OwnedSemaphorePermit, SemaphorePermit, mpsc};
use tokio::time::Instant;

use super::audit::RequestAudit;
use super::limits::{self, BodyKind, MAX_API_RESPONSE_BYTES, MAX_FILE_BYTES};
use super::request::{self, FileMetadata};
use super::respond::{self, Answer};
use super::stream::Pump;
use super::{Inner, REQUEST, RequestContext};
use crate::authority::{self, Refusal, RequestBinding, one_assertion};
use crate::http::activity::{Activity, OwnedWorking};
use crate::http::operation::Operation;
use crate::http::route::{Authority, Method, Reply, RouteMatch, resolve};
use crate::http::service::{self, Body, Call, FileUpload, Principal, Problem, StreamRecord};

/// Why a request ends without its operation's answer.
enum Failure {
    /// A public refusal or failure, recorded and answered.
    Problem(Problem),
    /// The audit journal could not record an event the request needed: nothing proceeds, and the answer is a bare
    /// internal error.
    Unrecorded,
}

impl From<Problem> for Failure {
    fn from(problem: Problem) -> Self {
        Self::Problem(problem)
    }
}

fn problem(status: u16, message: &'static str, code: &'static str) -> Problem {
    Problem::new(status, message, code)
}

fn internal_problem() -> Problem {
    Problem {
        status: 500,
        message: Cow::Borrowed("internal error"),
        code: None,
        retry_after_ms: None,
    }
}

/// A captured body, before any file content is read.
enum Captured {
    None(Value),
    Json(Value, Value),
    File(FileMetadata),
}

impl Captured {
    /// The binding an assertion must carry.
    fn binding(&self) -> Value {
        match self {
            Self::None(binding) | Self::Json(_, binding) => binding.clone(),
            Self::File(metadata) => metadata.binding(),
        }
    }
}

/// A future whose panic becomes `None` instead of ending the connection, so a panicking service is answered with a
/// redacted internal error.
struct Unwinding<F>(Pin<Box<F>>);

impl<F: Future> Future for Unwinding<F> {
    type Output = Option<F::Output>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let future = &mut self.0;
        match std::panic::catch_unwind(AssertUnwindSafe(|| future.as_mut().poll(cx))) {
            Ok(Poll::Ready(output)) => Poll::Ready(Some(output)),
            Ok(Poll::Pending) => Poll::Pending,
            Err(_) => Poll::Ready(None),
        }
    }
}

/// What the authorization of one request proved.
struct Authorized {
    principal: Principal,
    captured: Captured,
    model: Option<(String, Secret)>,
    supervisor_key: Option<String>,
}

/// Everything one authorized call carries besides its route.
struct Ready {
    principal: Principal,
    body: Body,
    model: Option<(String, Secret)>,
    decision: Option<Secret>,
    supervisor_key: Option<String>,
    websocket_key: Option<String>,
}

/// How one kind of assertion's refusals are recorded and answered.
#[derive(Clone, Copy)]
struct Refusals {
    event: &'static str,
    denied: (&'static str, &'static str),
    unavailable: (&'static str, &'static str),
}

const SUPERVISOR: Refusals = Refusals {
    event: "human-authority",
    denied: ("Supervisor authority is required", "invalid-supervisor"),
    unavailable: (
        "Supervisor authority is unavailable",
        "supervisor-unavailable",
    ),
};

const ROUTINE: Refusals = Refusals {
    event: "routine-authority",
    denied: ("Routine authority is required", "invalid-routine"),
    unavailable: ("Routine authority is unavailable", "routine-unavailable"),
};

/// One request in flight.
struct Exchange<'a> {
    server: &'a Inner,
    head: &'a Parts,
    body: Incoming,
    body_deadline: Instant,
    audit: RequestAudit,
    limit: usize,
    mutating: bool,
    working: Option<OwnedWorking>,
    upgrade: Option<OnUpgrade>,
    slot: Arc<OwnedSemaphorePermit>,
    streaming: bool,
}

/// Runs key-file work off the async workers; a lost task is unavailable.
async fn blocking<T: Send + 'static>(
    work: impl FnOnce() -> Result<T, Refusal> + Send + 'static,
) -> Result<T, Refusal> {
    tokio::task::spawn_blocking(work)
        .await
        .unwrap_or(Err(Refusal::Unavailable))
}

fn slices(header: &[Vec<u8>]) -> Vec<&[u8]> {
    header.iter().map(Vec::as_slice).collect()
}

/// Records a request without the controller bearer and answers `401`.
async fn unauthenticated(audit: &mut RequestAudit) -> Answer {
    audit.absent("machine_bearer_rejected");
    let recorded = audit
        .record(
            Some("authentication"),
            "denied",
            None,
            Some("invalid-bearer"),
        )
        .await;
    if recorded.is_err() {
        return respond::internal(Some(&audit.trace_id), MAX_API_RESPONSE_BYTES);
    }
    let document = Value::Object(vec![
        (
            "error".to_owned(),
            Value::String("authentication required".to_owned()),
        ),
        ("trace_id".to_owned(), Value::String(audit.trace_id.clone())),
    ]);
    respond::json(401, &document, MAX_API_RESPONSE_BYTES)
}

impl Inner {
    /// Answers one request; every path records its outcome before it answers.
    pub(super) async fn handle(
        self: Arc<Self>,
        mut request: Request<Incoming>,
        slot: Arc<OwnedSemaphorePermit>,
    ) -> Answer {
        let body_deadline = Instant::now() + self.parts.limits.body_deadline;
        let Ok(trace_id) = token_hex(self.parts.entropy.as_ref(), 16) else {
            return respond::internal(None, MAX_API_RESPONSE_BYTES);
        };
        let mut audit = RequestAudit::new(
            Arc::clone(&self.parts.audit),
            Arc::clone(&self.parts.usage),
            trace_id,
        );
        if !request::bearer_matches(request.headers(), &self.parts.token) {
            let answer = unauthenticated(&mut audit).await;
            audit.end();
            return answer;
        }
        let mutating = !matches!(*request.method(), hyper::Method::GET | hyper::Method::HEAD);
        let working = mutating.then(|| Activity::owned_working(&self.parts.activity));
        let upgrade = hyper::upgrade::on(&mut request);
        let (head, body) = request.into_parts();
        let mut exchange = Exchange {
            server: &self,
            head: &head,
            body,
            body_deadline,
            audit,
            limit: MAX_API_RESPONSE_BYTES,
            mutating,
            working,
            upgrade: Some(upgrade),
            slot,
            streaming: false,
        };
        let answer = exchange.answered().await;
        // A stream ends its usage meter after its terminal record.
        if !exchange.streaming {
            exchange.audit.end();
        }
        // A stream took its work unit and marks the Supervisor's activity when it ends.
        if exchange.working.is_some() && exchange.audit.principal_class == "human" {
            self.parts.activity.supervised();
        }
        answer
    }
}

impl<'a> Exchange<'a> {
    /// The answer of the request, its failure recorded and projected.
    async fn answered(&mut self) -> Answer {
        match self.run().await {
            Ok(answer) => answer,
            Err(Failure::Problem(problem)) => self.fail(&problem).await,
            Err(Failure::Unrecorded) => respond::internal(Some(&self.audit.trace_id), self.limit),
        }
    }

    async fn run(&mut self) -> Result<Answer, Failure> {
        let head = self.head;
        let path = request::canonical_path(&head.uri)?;
        let route = self.route(path)?;
        let decision = request::decision_key(&head.headers, route.operation)?;
        let authorized = self.authorize(&route, path, decision.as_ref()).await?;
        let websocket_key = self.admit(&route)?;
        let (body, _upload) = self.content(authorized.captured).await?;
        let ready = Ready {
            principal: authorized.principal,
            body,
            model: authorized.model,
            decision,
            supervisor_key: authorized.supervisor_key,
            websocket_key,
        };
        self.dispatch(&route, ready).await
    }

    /// The route of `path`, whose operation then names the request's audit, journal tolerance, and answer bound.
    fn route(&mut self, path: &'a str) -> Result<RouteMatch<'a>, Problem> {
        let route = Method::parse(self.head.method.as_str())
            .and_then(|method| resolve(method, path))
            .ok_or_else(|| problem(404, "route not found", "route-not-found"))?;
        let operation = route.operation;
        self.audit.operation = operation.name();
        self.audit.tolerant = limits::journal_tolerant(operation);
        self.limit = limits::response(operation);
        Ok(route)
    }

    /// Proves the route's authority: the bearer alone, a Routine assertion, or a Supervisor assertion.
    async fn authorize(
        &mut self,
        route: &RouteMatch<'_>,
        path: &str,
        decision: Option<&Secret>,
    ) -> Result<Authorized, Failure> {
        match route.operation.route().authority {
            Authority::Machine => {
                let captured = self.capture(route.operation).await?;
                self.audit.machine();
                self.record(Some("machine-authority"), "ok", None).await?;
                Ok(Authorized {
                    principal: Principal::Machine,
                    captured,
                    model: None,
                    supervisor_key: None,
                })
            }
            Authority::Routine => self.routine(route.operation, path).await,
            Authority::Supervisor | Authority::SupervisorOrHostReset => {
                self.supervisor(route, path, decision).await
            }
        }
    }

    /// After authorization: the route's Team must be canonical, and a view upgrade exactly one admitted upgrade.
    fn admit(&mut self, route: &RouteMatch<'_>) -> Result<Option<String>, Problem> {
        if let Some(team_id) = route.param("team_id") {
            self.audit.team_id = Some(validated_team(team_id)?.to_owned());
        }
        if route.operation.route().reply != Reply::Upgrade {
            return Ok(None);
        }
        request::upgrade_key(&self.head.headers)
            .map(Some)
            .ok_or_else(|| problem(400, "the stream upgrade is invalid", INVALID_UPGRADE))
    }

    /// Calls the route's service under the request's context and frames its answer.
    async fn dispatch(&mut self, route: &RouteMatch<'_>, ready: Ready) -> Result<Answer, Failure> {
        let context = RequestContext {
            trace_id: self.audit.trace_id.clone(),
            principal_id: self.audit.principal_id.clone(),
            principal_class: self.audit.principal_class,
            credential_state: self.audit.credential_state,
            supervisor_key: ready.supervisor_key,
        };
        let call = Call {
            route,
            principal: ready.principal,
            body: ready.body,
            model: ready.model.map(|(provider, api_key)| ModelCredential {
                provider,
                model: String::new(),
                api_key,
            }),
            decision_key: ready.decision,
            trace_id: self.audit.trace_id.clone(),
            upgrade_key: ready.websocket_key.clone(),
        };
        let service = self
            .server
            .parts
            .services
            .group(route.operation.route().group);
        // The call starts inside the scope, so even a service that reads its context before its first await sees it.
        let scoped = REQUEST.scope(context, async move { service.call(call).await });
        match Unwinding(Box::pin(scoped)).await {
            None => Err(internal_problem().into()),
            Some(Err(problem)) => Err(problem.into()),
            Some(Ok(answer)) => self.answer(route, answer, ready.websocket_key).await,
        }
    }

    /// The body `operation` admits, captured exactly once before authorization; file content stays unread.
    async fn capture(&mut self, operation: Operation) -> Result<Captured, Problem> {
        let headers = &self.head.headers;
        Ok(match limits::body(operation) {
            BodyKind::None => Captured::None(request::empty_body(headers)?),
            BodyKind::File => Captured::File(request::file_metadata(headers, MAX_FILE_BYTES)?),
            BodyKind::Json(maximum) => {
                let length = request::json_length(headers, maximum)?;
                let raw = self
                    .read(length)
                    .await
                    .ok_or_else(|| problem(400, "invalid JSON body", "invalid-json"))?;
                let (document, binding) = request::json_document(&raw)?;
                Captured::Json(document, binding)
            }
        })
    }

    /// Exactly `length` body bytes within the body deadline, else `None`.
    async fn read(&mut self, length: usize) -> Option<Vec<u8>> {
        let mut raw = Vec::with_capacity(length.min(64 * 1024));
        loop {
            let frame = tokio::time::timeout_at(self.body_deadline, self.body.frame())
                .await
                .ok()?;
            let Some(frame) = frame else { break };
            if let Ok(data) = frame.ok()?.into_data() {
                if raw.len() + data.len() > length {
                    return None;
                }
                raw.extend_from_slice(&data);
            }
        }
        (raw.len() == length).then_some(raw)
    }

    /// The captured body as the service receives it: an upload's content is read now, in the one upload slot.
    async fn content(
        &mut self,
        captured: Captured,
    ) -> Result<(Body, Option<SemaphorePermit<'a>>), Problem> {
        let server = self.server;
        Ok(match captured {
            Captured::None(_) => (Body::None, None),
            Captured::Json(document, _) => (Body::Json(document), None),
            Captured::File(metadata) => {
                let slot = server.upload.try_acquire().map_err(|_| {
                    problem(
                        429,
                        "another Team file upload is in progress",
                        "file-upload-busy",
                    )
                })?;
                let content = self
                    .read(metadata.length)
                    .await
                    .ok_or_else(|| problem(400, "invalid file body", "invalid-file"))?;
                let upload = FileUpload {
                    filename: metadata.filename,
                    media_type: metadata.media_type,
                    content,
                };
                (Body::File(upload), Some(slot))
            }
        })
    }

    fn header(&self, name: &str) -> Vec<Vec<u8>> {
        self.head
            .headers
            .get_all(name)
            .iter()
            .map(|value| value.as_bytes().to_vec())
            .collect()
    }

    fn now(&self) -> i64 {
        let since = self
            .server
            .parts
            .clock
            .now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        i64::try_from(since.as_secs()).unwrap_or(i64::MAX)
    }

    /// Records an assertion's refusal and answers it: `403` when denied, `503` when it could not be judged.
    async fn refused(&mut self, refusal: Refusal, kind: Refusals) -> Failure {
        let (result, status, (message, code)) = match refusal {
            Refusal::Denied => ("denied", 403, kind.denied),
            Refusal::Unavailable => ("error", 503, kind.unavailable),
        };
        if self
            .record(Some(kind.event), result, Some(code))
            .await
            .is_err()
        {
            return Failure::Unrecorded;
        }
        problem(status, message, code).into()
    }

    /// One segment of a leased run under Admin's Routine identity; the run checks its lease against the evidence.
    async fn routine(&mut self, operation: Operation, path: &str) -> Result<Authorized, Failure> {
        let captured = self.capture(operation).await?;
        let header = self.header(ROUTINE_ASSERTION_HEADER);
        let checker = Arc::clone(&self.server.parts.verifier);
        let (method, path, binding, now) = (
            self.head.method.as_str().to_owned(),
            path.to_owned(),
            captured.binding(),
            self.now(),
        );
        let verified = blocking(move || {
            checker.verify_routine(&slices(&header), &method, &path, binding, now)
        })
        .await;
        let evidence = match verified {
            Ok(evidence) => evidence,
            Err(refusal) => return Err(self.refused(refusal, ROUTINE).await),
        };
        self.audit.routine();
        self.record(Some("routine-authority"), "ok", None).await?;
        Ok(Authorized {
            principal: Principal::Routine {
                key_fingerprint: evidence.key_fingerprint,
                lease_sha256: evidence.lease_sha256,
            },
            captured,
            model: None,
            supervisor_key: None,
        })
    }

    /// A request a Supervisor session (or, for the Space reset, the host-reset authority) authorizes.
    async fn supervisor(
        &mut self,
        route: &RouteMatch<'_>,
        path: &str,
        decision: Option<&Secret>,
    ) -> Result<Authorized, Failure> {
        let header = self.header(ASSERTION_HEADER);
        let shaped = one_assertion(&slices(&header)).is_ok();
        self.audit.absent(if shaped {
            "assertion_present"
        } else {
            "assertion_absent_or_malformed"
        });
        let captured = self.capture(route.operation).await?;
        let model = if limits::model_bound(route.operation) {
            Some(request::model_credential(&self.head.headers)?)
        } else {
            None
        };
        let binding = self
            .binding(route, path, &captured, model.as_ref(), decision)
            .await?;
        let (checker, now) = (Arc::clone(&self.server.parts.verifier), self.now());
        let verified =
            blocking(move || checker.verify_supervisor(&slices(&header), &binding, now)).await;
        let evidence = match verified {
            Ok(evidence) => evidence,
            Err(refusal) => {
                if shaped && refusal == Refusal::Denied {
                    self.audit.credential_state = "assertion_rejected";
                }
                return Err(self.refused(refusal, SUPERVISOR).await);
            }
        };
        self.audit.human(&evidence.supervisor_id);
        self.record(Some("human-authority"), "ok", None).await?;
        let principal = if evidence.authority == "host-reset" {
            Principal::HostReset
        } else {
            Principal::Supervisor {
                supervisor_id: evidence.supervisor_id,
                assurance: evidence.assurance,
                authority_sha256: evidence.authority_sha256,
            }
        };
        Ok(Authorized {
            principal,
            captured,
            model,
            supervisor_key: Some(evidence.key_sha256),
        })
    }

    /// The exact request a Supervisor assertion must bind.
    async fn binding(
        &self,
        route: &RouteMatch<'_>,
        path: &str,
        captured: &Captured,
        model: Option<&(String, Secret)>,
        decision: Option<&Secret>,
    ) -> Result<RequestBinding, Problem> {
        Ok(RequestBinding {
            method: self.head.method.as_str().to_owned(),
            path: path.to_owned(),
            body: captured.binding(),
            model: model.map(|(provider, key)| request::credential_binding(provider, key)),
            decision: decision.map(|key| request::credential_binding("typesafe", key)),
            assurance: self.expected_assurance(route, captured).await?,
            authorities: if route.operation == Operation::SpaceReset {
                &["session", "host-reset"]
            } else {
                &["session"]
            },
        })
    }

    /// The assurance an approval of the Team's pending authentication challenge must bind.
    async fn expected_assurance(
        &self,
        route: &RouteMatch<'_>,
        captured: &Captured,
    ) -> Result<Option<Value>, Problem> {
        if !matches!(
            route.operation,
            Operation::ChatHumanSubmit | Operation::RoutineHumanSubmit
        ) {
            return Ok(None);
        }
        let Captured::Json(document, _) = captured else {
            return Ok(None);
        };
        let Some(challenge_id) = authority::approval(document) else {
            return Ok(None);
        };
        let team_id = validated_team(route.param("team_id").unwrap_or_default())?;
        let pending = self
            .server
            .parts
            .challenges
            .pending(route.operation, team_id)
            .await?;
        Ok(authority::assurance(
            challenge_id,
            pending
                .as_ref()
                .map(|pending| (pending.kind.as_str(), pending.challenge_id.as_str())),
        ))
    }

    async fn record(
        &self,
        operation: Option<&str>,
        result: &str,
        detail: Option<&str>,
    ) -> Result<(), Failure> {
        self.audit
            .record(operation, result, None, detail)
            .await
            .map_err(|_| Failure::Unrecorded)
    }

    /// The service's answer, recorded and framed as the route declares; any other kind is an internal error.
    async fn answer(
        &mut self,
        route: &RouteMatch<'_>,
        answer: service::Answer,
        websocket_key: Option<String>,
    ) -> Result<Answer, Failure> {
        let assistant = route.param("assistant_id");
        match (answer, route.operation.route().reply) {
            (service::Answer::Json { status, body }, Reply::Json) => {
                let Value::Object(mut members) = body else {
                    return Err(internal_problem().into());
                };
                members.retain(|(name, _)| name != "trace_id");
                members.push((
                    "trace_id".to_owned(),
                    Value::String(self.audit.trace_id.clone()),
                ));
                self.recorded_ok(assistant, None).await?;
                Ok(respond::json(status, &Value::Object(members), self.limit))
            }
            (
                service::Answer::Binary {
                    media_type,
                    body,
                    headers,
                },
                Reply::Binary,
            ) => {
                let response =
                    respond::binary(media_type, body, &headers).ok_or_else(internal_problem)?;
                self.recorded_ok(assistant, None).await?;
                Ok(response)
            }
            (service::Answer::Stream(records), Reply::Ndjson) => Ok(self.stream(records)),
            (service::Answer::Relay(relay), Reply::Upgrade) => {
                let key = websocket_key.ok_or_else(internal_problem)?;
                let response =
                    respond::switching(&accept(&key), SUBPROTOCOL).ok_or_else(internal_problem)?;
                let upgrade = self.upgrade.take().ok_or_else(internal_problem)?;
                self.recorded_ok(assistant, Some("upgraded")).await?;
                let slot = Arc::clone(&self.slot);
                tokio::spawn(async move {
                    // The connection carries the stream: nothing else is ever read from it.
                    if let Ok(upgraded) = upgrade.await {
                        relay.run(Box::new(TokioIo::new(upgraded))).await;
                    }
                    drop(slot);
                });
                Ok(response)
            }
            _ => Err(internal_problem().into()),
        }
    }

    async fn recorded_ok(
        &self,
        assistant: Option<&str>,
        detail: Option<&str>,
    ) -> Result<(), Failure> {
        self.audit
            .record(None, "ok", assistant, detail)
            .await
            .map_err(|_| Failure::Unrecorded)
    }

    /// The NDJSON answer: its records are written as they come, and the terminal one is recorded first.
    fn stream(&mut self, records: mpsc::Receiver<StreamRecord>) -> Answer {
        let (response, frames) = respond::ndjson();
        self.streaming = true;
        let supervised = self
            .mutating
            .then(|| Arc::clone(&self.server.parts.activity));
        let pump = Pump {
            audit: self.audit.clone(),
            frames,
            working: self.working.take(),
            supervised,
            slot: Arc::clone(&self.slot),
        };
        tokio::spawn(pump.run(records));
        response
    }

    /// Records the failure and answers its public document.
    async fn fail(&mut self, problem: &Problem) -> Answer {
        let result = if problem.status < 500 {
            "denied"
        } else {
            "error"
        };
        let detail = problem.code.unwrap_or("internal-error");
        if self
            .audit
            .record(None, result, None, Some(detail))
            .await
            .is_err()
        {
            return respond::internal(Some(&self.audit.trace_id), self.limit);
        }
        respond::json(
            problem.status,
            &respond::problem_document(problem, &self.audit.trace_id),
            self.limit,
        )
    }
}

/// The route's Team id when it is canonical.
fn validated_team(team_id: &str) -> Result<&str, Problem> {
    canonical_team_id(&Value::String(team_id.to_owned()))
        .map(|_| team_id)
        .ok_or_else(|| problem(422, "invalid Team id", "invalid-team-id"))
}
