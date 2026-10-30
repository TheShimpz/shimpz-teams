use std::sync::Arc;

use strict_json::Value;
use team_inference::config::InferenceConfigStore;

use super::{InferenceService, model_price};
use crate::chat::fakes::{FakeTeam, json};
use crate::coordination::locks::SpaceLocks;
use crate::http::route::{Method, resolve};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service};

fn service(root: &std::path::Path) -> InferenceService {
    InferenceService::new(
        Arc::new(FakeTeam::with("team_1", Vec::new())),
        Arc::new(InferenceConfigStore::new(root.to_path_buf())),
        Arc::new(SpaceLocks::new()),
    )
}

async fn call(
    service: &InferenceService,
    method: Method,
    path: &str,
    body: Body,
) -> Result<Value, Problem> {
    let route = resolve(method, path).expect("route");
    let call = Call {
        route: &route,
        principal: Principal::Machine,
        body,
        model: None,
        decision_key: None,
        trace_id: "t".repeat(32),
        upgrade_key: None,
    };
    match service.call(call).await? {
        Answer::Json { body, .. } => Ok(body),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn the_supervisor_configures_and_reads_one_catalog_model() {
    let directory = tempfile::tempdir().expect("directory");
    let service = service(directory.path());
    let path = "/v1/teams/team_1/inference";
    let missing = call(&service, Method::Get, path, Body::None).await;
    assert_eq!(
        missing.map_err(|problem| problem.code),
        Err(Some("inference-not-configured"))
    );
    let body = Body::Json(json(r#"{"provider":"openai","model":"gpt-6-luna"}"#));
    let saved = call(&service, Method::Put, path, body)
        .await
        .expect("saved");
    let expected = json(r#"{"team_id":"team_1","provider":"openai","model":"gpt-6-luna"}"#);
    assert_eq!(saved, expected);
    assert_eq!(
        call(&service, Method::Get, path, Body::None).await,
        Ok(expected)
    );
}

#[tokio::test]
async fn an_unknown_provider_model_body_or_team_is_refused() {
    let directory = tempfile::tempdir().expect("directory");
    let service = service(directory.path());
    let path = "/v1/teams/team_1/inference";
    for (body, code) in [
        (
            r#"{"provider":"other","model":"gpt-6-luna"}"#,
            "invalid-inference",
        ),
        (
            r#"{"provider":"anthropic","model":"claude-haiku-5-5"}"#,
            "invalid-inference",
        ),
        (r#"{"provider":"openai"}"#, "invalid-body"),
        (
            r#"{"provider":"openai","model":"gpt-6-luna","extra":1}"#,
            "invalid-body",
        ),
    ] {
        let refused = call(&service, Method::Put, path, Body::Json(json(body))).await;
        assert_eq!(
            refused.map_err(|problem| problem.code),
            Err(Some(code)),
            "{body}"
        );
    }
    let absent = call(
        &service,
        Method::Get,
        "/v1/teams/team_2/inference",
        Body::None,
    )
    .await;
    assert_eq!(absent.map_err(|problem| problem.status), Err(404));
}

#[test]
fn a_model_price_falls_back_to_the_providers_dearest() {
    assert_eq!(model_price("openai", Some("gpt-6-luna")), (10, 50));
    assert_eq!(model_price("openai", None), (200, 1000));
    assert_eq!(model_price("unknown", None), (0, 0));
}
