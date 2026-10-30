//! The controller's own operations (`Group::Controller`): health, activity, and the Supervisor key rotation.

use std::sync::Arc;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::future::BoxFuture;
use team_protocol::python::value::object_of;

use super::activity::Activity;
use super::operation::Operation;
use super::server::current_request;
use super::service::{Answer, Body, Call, Problem, Service};
use crate::authority::{Refusal, Verifier};
use crate::docker::DockerEngine;

/// The controller service.
pub struct Controller {
    /// The activity counters `activity` reads.
    pub activity: Arc<Activity>,
    /// The verifier whose pin a rotation replaces.
    pub verifier: Arc<Verifier>,
    /// The daemon `health` proves reachable.
    pub docker: Arc<dyn DockerEngine>,
    /// The audit journal a rotation is recorded in.
    pub audit: Arc<dyn AuditSink>,
}

impl std::fmt::Debug for Controller {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Controller")
    }
}

fn ok(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

fn supervisor_unavailable() -> Problem {
    Problem::new(
        503,
        "Supervisor authority state is unavailable",
        "supervisor-unavailable",
    )
}

impl Controller {
    async fn health(&self) -> Result<Answer, Problem> {
        self.docker
            .info()
            .await
            .map_err(|_| Problem::new(503, "Docker is unavailable", "docker-unavailable"))?;
        Ok(ok(object_of(vec![(
            "status",
            Value::String("ok".to_owned()),
        )])))
    }

    /// Replaces the pin with the rotation's key, signed by the key that verified this request.
    async fn rotate(&self, body: Body) -> Result<Answer, Problem> {
        let Body::Json(body) = body else {
            return Err(Problem::new(
                400,
                "request body is unavailable",
                "invalid-body",
            ));
        };
        let context = current_request().ok_or_else(supervisor_unavailable)?;
        let signed_by = context.supervisor_key.clone().unwrap_or_default();
        let verifier = Arc::clone(&self.verifier);
        let rotated = tokio::task::spawn_blocking(move || verifier.rotate(&body, &signed_by))
            .await
            .unwrap_or(Err(Refusal::Unavailable))
            .map_err(|refusal| match refusal {
                Refusal::Denied => Problem::new(
                    409,
                    "the Supervisor key rotation is invalid or no longer current",
                    "supervisor-key-rotation-refused",
                ),
                Refusal::Unavailable => supervisor_unavailable(),
            })?;
        let fingerprint = rotated
            .get("key_sha256")
            .and_then(|value| match value {
                Value::String(text) => text.get(..16),
                _ => None,
            })
            .unwrap_or_default();
        let event = super::server::audit_event(&context, "supervisor-key-rotate", "ok")
            .with("detail", Value::String(fingerprint.to_owned()));
        self.audit.record(event).await.map_err(|_| Problem {
            status: 500,
            message: "internal error".into(),
            code: None,
            retry_after_ms: None,
        })?;
        Ok(ok(rotated))
    }
}

impl Service for Controller {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move {
            match call.operation() {
                Operation::Health => self.health().await,
                Operation::Activity => Ok(ok(object_of(vec![(
                    "state",
                    Value::String(self.activity.state().to_owned()),
                )]))),
                Operation::SupervisorKeyRotate => self.rotate(call.body).await,
                _ => Err(Problem::new(404, "route not found", "route-not-found")),
            }
        })
    }
}
