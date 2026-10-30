//! Who may call each route: the bearer on every route, then the route's own authority.

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_protocol::python::value::{object_of, text};

use super::{
    Fake, Harness, JSON, Pending, Reply, TOKEN, assert_private, edited, is_trace, json_binding,
    quick, raw, unedited,
};
use crate::authority::fixture::{LEASE, SUPERVISOR_ID};
use crate::authority::key::hex;
use crate::http::operation::Operation;
use crate::http::route::{Authority, Method, ROUTES, Route};
use crate::http::server::PendingChallenge;
use crate::http::server::limits::{BodyKind, body, model_bound};
use crate::http::service::{Body, Principal};

const RUN: &str = "/v1/teams/alpha/routines/runs/0123456789abcdef0123456789abcdef/segment";
const MODEL_KEY: &str = "sk-test-model-key-value";

/// A concrete path for a route pattern.
fn concrete(pattern: &str) -> String {
    pattern
        .split('/')
        .map(|part| match part {
            ":team_id" => "alpha",
            ":image_hash" => "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            ":locale" => "en",
            part if part.starts_with(':') => "0123456789abcdef0123456789abcdef",
            part => part,
        })
        .collect::<Vec<_>>()
        .join("/")
}

const fn method(route: &Route) -> &'static str {
    match route.method {
        Method::Get => "GET",
        Method::Post => "POST",
        Method::Put => "PUT",
        Method::Patch => "PATCH",
        Method::Delete => "DELETE",
    }
}

fn digest(value: &str) -> Value {
    text(&hex(&Sha256::digest(value.as_bytes())))
}

fn binding(provider: &str, key: &str) -> Value {
    object_of(vec![
        ("provider", text(provider)),
        ("key_sha256", digest(key)),
    ])
}

#[tokio::test]
async fn every_route_requires_bearer() {
    let harness = Harness::start();
    for route in &ROUTES {
        let path = concrete(route.pattern);
        for token in [None, Some("f".repeat(64).as_str()), Some(&TOKEN[..63])] {
            let reply = harness
                .exchange(&raw(method(route), &path, token, &[JSON], b"{}"))
                .await;
            assert_eq!(reply.status, 401, "{}", route.name);
            assert_eq!(
                reply.header("www-authenticate"),
                Some("Bearer realm=\"shimpz-local\"")
            );
            assert_private(&reply);
            assert_eq!(
                reply.text("error").as_deref(),
                Some("authentication required")
            );
            assert!(reply.text("trace_id").is_some_and(|trace| is_trace(&trace)));
        }
    }
    assert!(harness.fake.seen().is_empty());
    let journal = harness.journal();
    assert_eq!(journal.len(), ROUTES.len() * 3);
    assert!(journal.iter().all(|line| {
        line.contains(r#""credential_state":"machine_bearer_rejected""#)
            && line.contains(r#""operation":"authentication""#)
            && line.contains(r#""detail":"invalid-bearer""#)
    }));
}

#[tokio::test]
async fn stale_bearer_refused() {
    let harness = Harness::start();
    // The bearer an earlier start issued is any other 64-hex value: it never authorizes this process.
    let earlier = "1".repeat(64);
    let reply = harness
        .exchange(&raw("GET", "/healthz", Some(&earlier), &[], b""))
        .await;
    assert_eq!(reply.status, 401);
    let twice = format!("Authorization: Bearer {TOKEN}\r\n");
    let request = format!("GET /healthz HTTP/1.1\r\nHost: x\r\n{twice}{twice}\r\n");
    assert_eq!(harness.exchange(request.as_bytes()).await.status, 401);
    // A header byte outside ASCII is refused as unauthenticated, never compared.
    let mut latin = b"GET /healthz HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer ".to_vec();
    latin.extend_from_slice(&TOKEN.as_bytes()[..63]);
    latin.extend_from_slice(b"\xe9\r\n\r\n");
    assert_eq!(harness.exchange(&latin).await.status, 401);
    assert_eq!(
        harness.request("GET", "/healthz", &[], b"").await.status,
        200
    );
}

/// What a request with the bearer alone gets: the machine routes answer, every other one is refused.
fn assert_bearer_alone(route: &Route, reply: &Reply) {
    let (status, code) = match route.authority {
        Authority::Machine => (200, None),
        Authority::Routine => (403, Some("invalid-routine")),
        // A model-bound operation needs its credential before any authority is judged.
        _ if model_bound(route.operation) => (422, Some("invalid-model-credential")),
        Authority::Supervisor | Authority::SupervisorOrHostReset => {
            (403, Some("invalid-supervisor"))
        }
    };
    assert_eq!(reply.status, status, "{}", route.name);
    if let Some(code) = code {
        assert_eq!(reply.text("code").as_deref(), Some(code), "{}", route.name);
    }
}

#[tokio::test]
async fn each_route_is_answered_only_under_its_own_authority() {
    let harness = Harness::start();
    for route in ROUTES
        .iter()
        .filter(|route| route.operation != Operation::FileUpload)
    {
        let json = matches!(body(route.operation), BodyKind::Json(_));
        let (headers, content): (&[(&str, &str)], &[u8]) =
            if json { (&[JSON], b"{}") } else { (&[], b"") };
        let reply = harness
            .request(method(route), &concrete(route.pattern), headers, content)
            .await;
        assert_bearer_alone(route, &reply);
    }
    let seen = harness.fake.seen();
    assert_eq!(seen.len(), 7);
    assert!(seen.iter().all(|call| call.principal == Principal::Machine));
}

#[tokio::test]
async fn a_bound_supervisor_assertion_authorizes_with_its_context() {
    let harness = Harness::start();
    let body = br#"{"team_name":"Alpha"}"#;
    let path = "/v1/teams/alpha/create";
    let reply = harness
        .as_supervisor("POST", path, &[], body, unedited)
        .await;
    assert_eq!(reply.status, 200);
    assert_eq!(reply.json().get("done"), Some(&Value::Bool(true)));
    let trace = reply.text("trace_id").expect("trace");
    let seen = harness.fake.seen();
    assert_eq!(seen[0].operation, Operation::TeamCreate);
    let unassured = Principal::Supervisor {
        assurance: String::new(),
    };
    assert_eq!(seen[0].principal, unassured);
    assert_eq!(
        seen[0].body,
        Body::Json(object_of(vec![("team_name", text("Alpha"))]))
    );
    let context = seen[0].context.clone().expect("context");
    assert_eq!(context.trace_id, trace);
    assert_eq!(context.principal_id.as_deref(), Some(SUPERVISOR_ID));
    let pinned = harness.keys.supervisor.public().fingerprint();
    assert_eq!(context.supervisor_key, Some(pinned));
    assert!(harness.journaled(&[
        r#""operation":"human-authority""#,
        r#""result":"ok""#,
        SUPERVISOR_ID
    ]));
    assert!(harness.journaled(&[
        r#""operation":"team-create""#,
        r#""team_id":"alpha""#,
        &trace
    ]));
}

#[tokio::test]
async fn a_supervisor_assertion_authorizes_one_exact_request_once() {
    let harness = Harness::start();
    let body = br#"{"team_name":"Alpha"}"#;
    let path = "/v1/teams/alpha/create";
    let assertion = harness.supervisor("POST", path, json_binding(body));
    let headers = [JSON, ("X-Shimpz-Supervisor", assertion.as_str())];
    assert_eq!(
        harness.request("POST", path, &headers, body).await.status,
        200
    );
    // The same assertion again is a replay.
    assert_eq!(
        harness.request("POST", path, &headers, body).await.status,
        403
    );
    // Bound to another body, path, or method, a fresh assertion is refused.
    for (method, bound_path, binding) in [
        ("POST", path, json_binding(br#"{"team_name":"Beta"}"#)),
        ("POST", "/v1/teams/beta/create", json_binding(body)),
        ("PATCH", path, json_binding(body)),
    ] {
        let assertion = harness.supervisor(method, bound_path, binding);
        let headers = [JSON, ("X-Shimpz-Supervisor", assertion.as_str())];
        assert_eq!(
            harness.request("POST", path, &headers, body).await.status,
            403
        );
    }
    assert_eq!(harness.fake.seen().len(), 1);
    assert!(harness.journaled(&[r#""credential_state":"assertion_rejected""#]));
}

#[tokio::test]
async fn host_reset_authorizes_only_the_space_reset() {
    let harness = Harness::start();
    let host = || edited("authority", text("host-reset"));
    let reply = harness
        .as_supervisor("DELETE", "/v1/space", &[], b"", host())
        .await;
    assert_eq!(reply.status, 200);
    let reply = harness
        .as_supervisor("GET", "/v1/teams", &[], b"", host())
        .await;
    assert_eq!(reply.status, 403);
    let seen = harness.fake.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].principal, Principal::HostReset);
}

#[tokio::test]
async fn routine_assertion_audience_and_lease_fingerprint() {
    let harness = Harness::start();
    let body = br#"{"revision":1}"#;
    assert_eq!(
        harness
            .as_routine(RUN, body, &harness.keys.routine)
            .await
            .status,
        200
    );
    let by_supervisor_key = harness
        .as_routine(RUN, body, &harness.keys.supervisor)
        .await;
    assert_eq!(by_supervisor_key.status, 403);
    // A Supervisor assertion in the Supervisor header never runs a Routine.
    let session = harness
        .as_supervisor("POST", RUN, &[], body, unedited)
        .await;
    assert_eq!(session.status, 403);
    let seen = harness.fake.seen();
    assert_eq!(seen.len(), 1);
    let expected = Principal::Routine {
        key_fingerprint: harness.keys.routine.public().fingerprint(),
        lease_sha256: LEASE.to_owned(),
    };
    assert_eq!(seen[0].principal, expected);
    let principal = seen[0]
        .context
        .as_ref()
        .and_then(|context| context.principal_id.clone());
    assert_eq!(principal.as_deref(), Some("admin-routine"));
}

#[tokio::test]
async fn an_unreadable_key_is_unavailable_not_denied() {
    let harness = Harness::start();
    std::fs::remove_file(&harness.keys.files.routine).expect("removed");
    let reply = harness.as_routine(RUN, b"{}", &harness.keys.routine).await;
    assert_eq!(reply.status, 503);
    assert_eq!(reply.text("code").as_deref(), Some("routine-unavailable"));
    std::fs::remove_file(&harness.keys.files.supervisor).expect("removed");
    let reply = harness
        .as_supervisor("GET", "/v1/teams", &[], b"", unedited)
        .await;
    assert_eq!(reply.status, 503);
    assert_eq!(
        reply.text("code").as_deref(),
        Some("supervisor-unavailable")
    );
    assert!(harness.fake.seen().is_empty());
}

#[tokio::test]
async fn model_key_header_never_persisted_echoed_or_forwarded() {
    let harness = Harness::start();
    let body = br#"{"message":"hello-body-marker"}"#;
    let path = "/v1/teams/alpha/chat/capability-plan";
    let credential = [
        ("X-Shimpz-Model-Provider", "openai"),
        ("X-Shimpz-Model-Api-Key", MODEL_KEY),
    ];
    let bound = edited("model", binding("openai", MODEL_KEY));
    let reply = harness
        .as_supervisor("POST", path, &credential, body, bound)
        .await;
    assert_eq!(reply.status, 200);
    assert!(!String::from_utf8_lossy(&reply.body).contains(MODEL_KEY));
    let forwarded = harness.fake.seen()[0].model.clone();
    assert_eq!(forwarded, Some(("openai".to_owned(), MODEL_KEY.to_owned())));
    // Without the model binding in the assertion, the same request is refused.
    let unbound = harness
        .as_supervisor("POST", path, &credential, body, unedited)
        .await;
    assert_eq!(unbound.status, 403);
    // A model-bound operation without its credential is refused before any authority is judged.
    let missing = harness
        .as_supervisor("POST", path, &[], body, unedited)
        .await;
    assert_eq!(missing.status, 422);
    assert_eq!(
        missing.text("code").as_deref(),
        Some("invalid-model-credential")
    );
    for line in harness.journal() {
        for secret in [MODEL_KEY, "hello-body-marker", TOKEN] {
            assert!(!line.contains(secret), "{line}");
        }
    }
}

#[tokio::test]
async fn a_decision_key_binds_only_intent_routing() {
    let harness = Harness::start();
    let key = "ts-test-decision-key";
    let path = "/v1/teams/alpha/chat/intent-route";
    let headers = [
        ("X-Shimpz-Model-Provider", "anthropic"),
        ("X-Shimpz-Model-Api-Key", MODEL_KEY),
        ("X-Shimpz-Decision-Api-Key", key),
    ];
    let bound = |claims| {
        let claims = edited("model", binding("anthropic", MODEL_KEY))(claims);
        edited("decision", binding("typesafe", key))(claims)
    };
    let reply = harness
        .as_supervisor("POST", path, &headers, br#"{"intent":"x"}"#, bound)
        .await;
    assert_eq!(reply.status, 200);
    assert_eq!(harness.fake.seen()[0].decision.as_deref(), Some(key));
    let reply = harness
        .request(
            "GET",
            "/healthz",
            &[("X-Shimpz-Decision-Api-Key", key)],
            b"",
        )
        .await;
    assert_eq!(reply.status, 422);
    assert_eq!(
        reply.text("code").as_deref(),
        Some("invalid-decision-credential")
    );
}

fn answer(challenge: &str, decision: &str) -> Vec<u8> {
    format!(r#"{{"challenge_id":"{challenge}","decision":"{decision}","value":true}}"#).into_bytes()
}

#[tokio::test]
async fn an_approval_of_an_authentication_challenge_must_bind_its_assurance() {
    let challenge = "3".repeat(32);
    let pending = Pending(Some(PendingChallenge {
        kind: "auth:totp".to_owned(),
        challenge_id: challenge.clone(),
    }));
    let harness = Harness::with(quick(), Fake::done(), pending);
    let path = "/v1/teams/alpha/routines/runs/0123456789abcdef0123456789abcdef/human";
    let approve = answer(&challenge, "submit");
    let without = harness
        .as_supervisor("POST", path, &[], &approve, unedited)
        .await;
    assert_eq!(without.status, 403);
    let assurance = object_of(vec![
        ("kind", text("auth:totp")),
        ("challenge_id", text(&challenge)),
    ]);
    let with = harness
        .as_supervisor("POST", path, &[], &approve, edited("assurance", assurance))
        .await;
    assert_eq!(with.status, 200);
    let assured = Principal::Supervisor {
        assurance: "auth:totp".to_owned(),
    };
    assert_eq!(harness.fake.seen()[0].principal, assured);
    // A denial binds no assurance.
    let deny = answer(&challenge, "deny");
    let denial = harness
        .as_supervisor("POST", path, &[], &deny, unedited)
        .await;
    assert_eq!(denial.status, 200);
}

#[tokio::test]
async fn an_invalid_team_id_is_refused() {
    let harness = Harness::start();
    // After authorization for an ordinary request.
    let reply = harness
        .as_supervisor("GET", "/v1/teams/al-pha/files", &[], b"", unedited)
        .await;
    assert_eq!(reply.status, 422);
    assert_eq!(reply.text("code").as_deref(), Some("invalid-team-id"));
    // Before its challenge is looked up for an approval.
    let path = "/v1/teams/Alpha!/routines/runs/0123456789abcdef0123456789abcdef/human";
    let approve = answer(&"3".repeat(32), "submit");
    let reply = harness
        .as_supervisor("POST", path, &[], &approve, unedited)
        .await;
    assert_eq!(reply.status, 422);
    assert_eq!(reply.text("code").as_deref(), Some("invalid-team-id"));
    assert!(harness.fake.seen().is_empty());
}
