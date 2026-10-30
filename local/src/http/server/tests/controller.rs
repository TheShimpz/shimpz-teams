//! The controller's own operations, answered by their owners.

use std::sync::Arc;

use strict_json::Value;
use team_core::testing::MemoryAudit;
use team_protocol::python::value::{object_of, text};

use crate::authority::Verifier;
use crate::authority::fixture::{Keys, NOW, Signer, no_body, session};
use crate::http::activity::Activity;
use crate::http::controller::Controller;
use crate::http::operation::Operation;
use crate::http::route::RouteMatch;
use crate::http::server::{REQUEST, RequestContext};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service};
use crate::testing::FakeDocker;

struct Owners {
    keys: Keys,
    verifier: Arc<Verifier>,
    docker: Arc<FakeDocker>,
    audit: Arc<MemoryAudit>,
    controller: Controller,
}

fn owners() -> Owners {
    let keys = Keys::new();
    let verifier = Arc::new(keys.verifier(16));
    let docker = Arc::new(FakeDocker::default());
    let audit = Arc::new(MemoryAudit::default());
    let controller = Controller {
        activity: Arc::new(Activity::default()),
        verifier: Arc::clone(&verifier),
        docker: docker.clone(),
        audit: audit.clone(),
    };
    Owners {
        keys,
        verifier,
        docker,
        audit,
        controller,
    }
}

/// Calls `operation` as a Supervisor whose assertion `key` verified.
async fn call(
    owners: &Owners,
    operation: Operation,
    body: Body,
    key: Option<String>,
) -> Result<Answer, Problem> {
    let route = RouteMatch {
        operation,
        params: vec![],
    };
    let call = Call {
        route: &route,
        principal: Principal::Supervisor {
            assurance: String::new(),
        },
        body,
        model: None,
        decision_key: None,
        trace_id: "0".repeat(32),
    };
    let context = RequestContext {
        trace_id: "0".repeat(32),
        principal_id: Some("1".repeat(32)),
        principal_class: "human",
        credential_state: "assertion_present",
        supervisor_key: key,
        websocket_key: None,
    };
    REQUEST
        .scope(context, async { owners.controller.call(call).await })
        .await
}

fn body(answer: Result<Answer, Problem>) -> Value {
    match answer {
        Ok(Answer::Json { status: 200, body }) => body,
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn health_needs_the_daemon_and_activity_reads_the_counters() {
    let owners = owners();
    let health = body(call(&owners, Operation::Health, Body::None, None).await);
    assert_eq!(health, object_of(vec![("status", text("ok"))]));
    owners.docker.set_unavailable(true);
    let problem = call(&owners, Operation::Health, Body::None, None)
        .await
        .expect_err("down");
    assert_eq!(
        (problem.status, problem.code),
        (503, Some("docker-unavailable"))
    );
    let activity = body(call(&owners, Operation::Activity, Body::None, None).await);
    assert_eq!(activity, object_of(vec![("state", text("idle"))]));
}

fn rotation(signer: &Signer) -> Body {
    Body::Json(object_of(vec![(
        "public_key",
        text(&signer.public_base64url()),
    )]))
}

/// Pins the published Supervisor key by verifying one assertion under it.
fn pin(owners: &Owners) {
    let listing = format!(
        "Bearer {}",
        owners
            .keys
            .supervisor
            .supervisor(&Value::Object(session("GET", "/v1/teams", no_body())))
    );
    let binding = crate::authority::RequestBinding {
        method: "GET".to_owned(),
        path: "/v1/teams".to_owned(),
        body: no_body(),
        model: None,
        decision: None,
        assurance: None,
        authorities: &["session"],
    };
    owners
        .verifier
        .verify_supervisor(&[listing.as_bytes()], &binding, NOW)
        .expect("pinned");
}

#[tokio::test]
async fn a_rotation_is_signed_by_the_key_that_verified_its_request() {
    let owners = owners();
    pin(&owners);
    let pinned = owners.keys.supervisor.public().fingerprint();
    let next = Signer::new(4);
    let rotated = body(
        call(
            &owners,
            Operation::SupervisorKeyRotate,
            rotation(&next),
            Some(pinned.clone()),
        )
        .await,
    );
    assert_eq!(
        rotated.get("key_sha256"),
        Some(&text(&next.public().fingerprint()))
    );
    let recorded = owners.audit.events().iter().any(|event| {
        event
            .fields()
            .iter()
            .any(|(name, value)| name == "operation" && value == &text("supervisor-key-rotate"))
    });
    assert!(recorded);
    let stale = call(
        &owners,
        Operation::SupervisorKeyRotate,
        rotation(&Signer::new(6)),
        Some(pinned),
    )
    .await
    .expect_err("stale signer");
    assert_eq!(
        (stale.status, stale.code),
        (409, Some("supervisor-key-rotation-refused"))
    );
}
