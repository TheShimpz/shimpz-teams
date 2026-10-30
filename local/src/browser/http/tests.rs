use std::sync::Arc;

use strict_json::Value;

use super::BrowserGroup;
use crate::browser::sign_in::tests::Case;
use crate::http::route::{Method, resolve};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service};

const ORIGIN: &str = "https://admin.example";

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

async fn call(
    group: &BrowserGroup,
    method: Method,
    path: &str,
    body: Body,
    key: Option<&str>,
) -> Result<Value, Problem> {
    let route = resolve(method, path).expect("a route");
    let answered = group
        .call(Call {
            route: &route,
            principal: Principal::Supervisor {
                supervisor_id: "5".repeat(32),
                assurance: None,
            },
            body,
            model: None,
            decision_key: None,
            trace_id: "a".repeat(32),
            upgrade_key: key.map(str::to_owned),
        })
        .await;
    match answered {
        Ok(Answer::Json { status: 200, body }) => Ok(body),
        Ok(other) => panic!("an unexpected answer: {other:?}"),
        Err(problem) => Err(problem),
    }
}

fn group(case: &Case) -> BrowserGroup {
    BrowserGroup {
        service: Arc::clone(case.sessions.service()),
        sessions: Arc::clone(&case.sessions),
    }
}

fn code(answer: &Result<Value, Problem>) -> Option<(u16, &'static str)> {
    answer
        .as_ref()
        .err()
        .map(|problem| (problem.status, problem.code.unwrap_or_default()))
}

#[tokio::test(start_paused = true)]
async fn a_session_starts_and_cancels_and_the_sign_in_is_read() {
    let case = Case::new().await;
    let group = group(&case);
    let path = "/v1/teams/neo/browser/sign-in";
    let started = call(
        &group,
        Method::Post,
        path,
        Body::Json(json(&format!(
            r#"{{"url":"https://www.linkedin.com/login","locale":"pt","origin":"{ORIGIN}"}}"#
        ))),
        None,
    )
    .await
    .expect("started");
    assert!(team_protocol::sign_in::canonical_started(&started).is_some());
    let Some(Value::String(session)) = started.get("sign_in_session_id") else {
        panic!("a session")
    };
    let body = json(&format!(
        r#"{{"sign_in_session_id":"{session}","origin":"{ORIGIN}"}}"#
    ));
    let cancelled = call(
        &group,
        Method::Post,
        &format!("{path}/cancel"),
        Body::Json(body.clone()),
        None,
    )
    .await;
    assert_eq!(cancelled, Ok(json(r#"{"cancelled":true}"#)));
    let again = call(
        &group,
        Method::Post,
        &format!("{path}/cancel"),
        Body::Json(body),
        None,
    )
    .await;
    assert_eq!(code(&again), Some((404, "sign-in-not-running")));
    let current = call(&group, Method::Get, path, Body::None, None).await;
    assert_eq!(current, Ok(json(r#"{"sign_in":null}"#)));
    // The Supervisor who asked is the activity's actor.
    let named = case.audit.events().iter().any(|event| {
        event
            .fields()
            .iter()
            .any(|(name, value)| name == "principal_id" && *value == Value::String("5".repeat(32)))
    });
    assert!(named);
}

#[tokio::test(start_paused = true)]
async fn every_body_outside_its_exact_form_is_refused() {
    let case = Case::new().await;
    let group = group(&case);
    let path = "/v1/teams/neo/browser/sign-in";
    for (suffix, body) in [
        (
            "",
            r#"{"url":"https://www.linkedin.com/login","locale":"pt-BR"}"#,
        ),
        (
            "",
            r#"{"url":"https://x.example","locale":"pt-BR","origin":"https://admin.example","extra":1}"#,
        ),
        (
            "/save",
            r#"{"sign_in_session_id":"nope","origin":"https://admin.example"}"#,
        ),
        ("/cancel", r#"{"origin":"https://admin.example"}"#),
        (
            "/delete",
            r#"{"sign_in_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
        ),
    ] {
        let answer = call(
            &group,
            Method::Post,
            &format!("{path}{suffix}"),
            Body::Json(json(body)),
            None,
        )
        .await;
        assert_eq!(code(&answer), Some((400, "invalid-body")), "{body}");
    }
    let answer = call(&group, Method::Post, path, Body::None, None).await;
    assert_eq!(code(&answer), Some((400, "invalid-body")));
    let answer = call(
        &group,
        Method::Get,
        "/v1/teams/Not%20A%20Team/browser/sign-in",
        Body::None,
        None,
    )
    .await;
    assert_eq!(code(&answer), Some((422, "invalid-team-id")));
}

#[tokio::test(start_paused = true)]
async fn each_refusal_before_a_stream_is_closed() {
    let case = Case::new().await;
    let group = group(&case);
    let opened = call(
        &group,
        Method::Post,
        "/v1/teams/neo/browser/view",
        Body::None,
        None,
    )
    .await;
    assert_eq!(code(&opened), Some((404, "browser-not-running")));
    let key = "AAAAAAAAAAAAAAAAAAAAAA==";
    let stream = format!("/v1/teams/neo/browser/views/{}/stream", "0".repeat(32));
    for (path, key, expected) in [
        (
            "/v1/teams/neo/browser/views/nope/stream".to_owned(),
            Some(key),
            (404, "browser-not-running"),
        ),
        (stream.clone(), None, (400, "browser-view-upgrade-invalid")),
        (
            stream.clone(),
            Some("not-a-key"),
            (400, "browser-view-upgrade-invalid"),
        ),
        (stream, Some(key), (404, "browser-not-running")),
    ] {
        let route = resolve(Method::Get, &path).expect("a route");
        let answer = group
            .call(Call {
                route: &route,
                principal: Principal::Machine,
                body: Body::None,
                model: None,
                decision_key: None,
                trace_id: "a".repeat(32),
                upgrade_key: key.map(str::to_owned),
            })
            .await;
        let Err(problem) = answer else {
            panic!("refused")
        };
        assert_eq!(
            (problem.status, problem.code.unwrap_or_default()),
            expected,
            "{path}"
        );
    }
}

#[test]
fn each_session_refusal_has_its_closed_answer() {
    use super::session_problem;
    use crate::browser::sign_in::SessionError;
    for (code, status) in [
        ("sign-in-not-running", 404),
        ("sign-in-url-invalid", 400),
        ("sign-in-busy", 409),
        ("sign-in-changed", 409),
        ("sign-in-absent", 404),
        ("sign-in-windows", 409),
        ("sign-in-identity-provider", 409),
        ("sign-in-unsaved", 503),
        ("browser-busy", 409),
        ("browser-unavailable", 503),
        ("team-not-found", 404),
    ] {
        let problem = session_problem(SessionError(code));
        assert_eq!((problem.status, problem.code), (status, Some(code)));
    }
    let unknown = session_problem(SessionError("browser-lost"));
    assert_eq!(unknown.code, Some("browser-unavailable"));
}
