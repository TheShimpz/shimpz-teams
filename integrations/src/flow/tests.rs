use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_core::testing::CountingEntropy;

use super::{
    ActionView, AssistantView, FlowError, Requested, assert_public, challenge_payload,
    inventory_payload, requirements_for_batch, resolve_action_integrations,
};
use crate::broker::tests::client;
use crate::pending::{self, IntegrationRequirement, RequiredIntegration};
use crate::pkce::{PkceLimits, PkceStore};
use crate::service::OAuthService;
use crate::store::tests::{Fixture, fixture, tokens};
use crate::store::{Declaration, Identity};

const ASSISTANT: &str = "cloudflare-assistant";

fn owned(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

fn read_scopes() -> Vec<String> {
    owned(&["dns.read", "zone.read"])
}

fn write_scopes() -> Vec<String> {
    owned(&["dns.read", "offline_access", "zone.read"])
}

fn action(id: &str, summary: &str, integrations: &[&str]) -> ActionView {
    ActionView {
        id: id.to_owned(),
        summary: summary.to_owned(),
        integrations: owned(integrations),
    }
}

fn spec() -> AssistantView {
    let declaration = |scopes| Declaration {
        provider: "cloudflare".to_owned(),
        scopes,
    };
    AssistantView {
        assistant_id: ASSISTANT.to_owned(),
        version: "0.4.1".to_owned(),
        name: "Cloudflare Assistant".to_owned(),
        summary: "test".to_owned(),
        actions: vec![
            action(
                "read-profile",
                "Read one external profile.",
                &["cloudflare-read"],
            ),
            action(
                "publish-post",
                "Publish one approved external update.",
                &["cloudflare-write"],
            ),
        ],
        integrations: vec![
            ("cloudflare-read".to_owned(), declaration(read_scopes())),
            ("cloudflare-write".to_owned(), declaration(write_scopes())),
        ],
    }
}

fn seal(fixture: &Fixture, integration: &str, scopes: &[String], expires_in: i64) {
    let tokens = crate::token::TokenSet {
        scopes: scopes.to_vec(),
        ..tokens(
            "private-access-token-123456",
            Some("private-refresh-token-1234"),
            expires_in,
        )
    };
    let identity = Identity {
        id: "123".to_owned(),
        username: Some("juliano".to_owned()),
        name: Some("Juliano".to_owned()),
    };
    fixture
        .store
        .put(
            ("team_1", ASSISTANT, integration),
            ("cloudflare", scopes),
            &tokens,
            Some(&identity),
        )
        .expect("sealed");
}

fn requests<'a>(actions: &[&'a str]) -> Vec<Requested<'a>> {
    actions
        .iter()
        .map(|action| Requested {
            assistant_id: ASSISTANT,
            action,
        })
        .collect()
}

fn write_requirement() -> IntegrationRequirement {
    IntegrationRequirement {
        assistant_id: ASSISTANT.to_owned(),
        assistant_name: "Cloudflare Assistant".to_owned(),
        action_ids: owned(&["publish-post"]),
        integrations: vec![RequiredIntegration {
            id: "cloudflare-write".to_owned(),
            provider: "cloudflare".to_owned(),
            scopes: write_scopes(),
        }],
    }
}

#[test]
fn a_batch_collects_every_unusable_integration_before_any_action() {
    let fixture = fixture();
    seal(&fixture, "cloudflare-read", &read_scopes(), 3600);
    // A grant sealed for other scopes than the current declaration needs a new consent.
    seal(&fixture, "cloudflare-write", &read_scopes(), 3600);
    let found = requirements_for_batch(
        "team_1",
        &[spec()],
        &requests(&["read-profile", "publish-post"]),
        &fixture.store,
    );
    assert_eq!(found, Ok(vec![write_requirement()]));
}

#[test]
fn a_refresh_required_integration_is_machine_recoverable() {
    let fixture = fixture();
    seal(&fixture, "cloudflare-write", &write_scopes(), 30);
    fixture.clock.advance(Duration::from_secs(31));
    let found = requirements_for_batch(
        "team_1",
        &[spec()],
        &requests(&["publish-post"]),
        &fixture.store,
    );
    assert_eq!(found, Ok(Vec::new()));
}

#[test]
fn a_batch_names_each_action_once_and_sorts_requirements() {
    let fixture = fixture();
    let found = requirements_for_batch(
        "team_1",
        &[spec()],
        &requests(&["publish-post", "read-profile", "publish-post"]),
        &fixture.store,
    );
    let found = found.expect("collected");
    let ids: Vec<(&str, &[String])> = found
        .iter()
        .map(|item| (item.integrations[0].id.as_str(), item.action_ids.as_slice()))
        .collect();
    assert_eq!(
        ids,
        [
            ("cloudflare-read", owned(&["read-profile"]).as_slice()),
            ("cloudflare-write", owned(&["publish-post"]).as_slice())
        ]
    );
}

#[test]
fn batch_bindings_declarations_and_limits_fail_closed() {
    let fixture = fixture();
    let many = vec![
        Requested {
            assistant_id: ASSISTANT,
            action: "read-profile"
        };
        65
    ];
    assert_eq!(
        requirements_for_batch("team_1", &[spec()], &many, &fixture.store),
        Err(FlowError::TooMany)
    );
    assert_eq!(
        requirements_for_batch("team_1", &[], &requests(&["read-profile"]), &fixture.store),
        Err(FlowError::Unavailable)
    );
    assert_eq!(
        requirements_for_batch("team_1", &[spec()], &requests(&["missing"]), &fixture.store),
        Err(FlowError::Invalid)
    );
    let mut undeclared = spec();
    undeclared.actions[0].integrations = owned(&["missing"]);
    let refused = requirements_for_batch(
        "team_1",
        &[undeclared],
        &requests(&["read-profile"]),
        &fixture.store,
    );
    assert_eq!(refused, Err(FlowError::Undeclared));
    assert_eq!(
        requirements_for_batch("../team", &[spec()], &requests(&[]), &fixture.store),
        Err(FlowError::Invalid)
    );
}

type Paused = Arc<crate::challenge::Pending<Vec<IntegrationRequirement>, &'static str>>;

fn paused(requirements: Vec<IntegrationRequirement>) -> Paused {
    let store = pending::default_store(Arc::new(CountingEntropy::default())).expect("defaults");
    store
        .create("team_1", requirements, "must-never-be-public")
        .expect("created")
}

fn member<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).expect("member")
}

#[tokio::test(start_paused = true)]
async fn the_challenge_is_exact_bounded_public_metadata() {
    let challenge = paused(vec![write_requirement()]);
    tokio::time::advance(Duration::from_millis(500)).await;
    let payload = challenge_payload(&challenge, &[spec()]).expect("projected");
    let Value::Object(members) = &payload else {
        panic!("an object")
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    assert_eq!(
        names,
        [
            "challenge_id",
            "expires_in",
            "requirements",
            "status",
            "team_id",
            "turn_id"
        ]
    );
    assert_eq!(
        member(&payload, "expires_in"),
        &Value::Number(strict_json::Number::Int(600))
    );
    let expected = r#"[{"actions":[{"id":"publish-post","name":"Publish Post","summary":"Publish one approved external update."}],"assistant_id":"cloudflare-assistant","assistant_name":"Cloudflare Assistant","integration_id":"cloudflare-write","name":"Cloudflare","provider":"cloudflare","scopes":["dns.read","offline_access","zone.read"],"summary":"Connect your Cloudflare integration so this Assistant can use only its reviewed Cloudflare permissions."}]"#;
    assert_eq!(
        team_protocol::python::json::dumps(
            member(&payload, "requirements"),
            team_protocol::python::json::CANONICAL
        ),
        expected
    );
    assert!(!format!("{payload:?}").contains("must-never-be-public"));
}

#[tokio::test(start_paused = true)]
async fn an_expired_or_malformed_challenge_is_refused() {
    let challenge = paused(vec![write_requirement()]);
    tokio::time::advance(Duration::from_secs(600)).await;
    assert_eq!(
        challenge_payload(&challenge, &[spec()]),
        Err(FlowError::Changed)
    );
    let mut empty = write_requirement();
    empty.integrations.clear();
    assert_eq!(
        challenge_payload(&paused(vec![empty]), &[spec()]),
        Err(FlowError::Invalid)
    );
    let mut duplicate = write_requirement();
    duplicate.action_ids = owned(&["publish-post", "publish-post"]);
    assert_eq!(
        challenge_payload(&paused(vec![duplicate]), &[spec()]),
        Err(FlowError::Invalid)
    );
}

#[tokio::test]
async fn a_challenge_whose_binding_changed_is_refused() {
    let challenge = paused(vec![write_requirement()]);
    assert_eq!(
        challenge_payload(&challenge, &[]),
        Err(FlowError::Unavailable)
    );
    let renamed = AssistantView {
        name: "Changed".to_owned(),
        ..spec()
    };
    assert_eq!(
        challenge_payload(&challenge, &[renamed]),
        Err(FlowError::Changed)
    );
    let mut undeclared = spec();
    undeclared.integrations.pop();
    assert_eq!(
        challenge_payload(&challenge, &[undeclared]),
        Err(FlowError::Changed)
    );
    let mut unused = spec();
    unused.actions[1].integrations.clear();
    assert_eq!(
        challenge_payload(&challenge, &[unused]),
        Err(FlowError::Changed)
    );
}

#[test]
fn the_inventory_flattens_status_without_tokens_or_generations() {
    let fixture = fixture();
    seal(&fixture, "cloudflare-write", &write_scopes(), 30);
    fixture.clock.advance(Duration::from_secs(31));
    let payload = inventory_payload("team_1", &[spec()], &fixture.store).expect("listed");
    let Value::Array(rows) = member(&payload, "integrations") else {
        panic!("rows");
    };
    let statuses: Vec<&Value> = rows.iter().map(|row| member(row, "status")).collect();
    assert_eq!(
        statuses,
        [
            &Value::String("missing".to_owned()),
            &Value::String("expired".to_owned())
        ]
    );
    assert_eq!(
        member(&rows[0], "assistant_version"),
        &Value::String("0.4.1".to_owned())
    );
    let identity = team_protocol::python::json::dumps(
        member(&rows[1], "integration"),
        team_protocol::python::json::CANONICAL,
    );
    assert_eq!(
        identity,
        r#"{"id":"123","name":"Juliano","username":"juliano"}"#
    );
    assert_eq!(
        member(&rows[1], "expires_at"),
        &Value::String("2001-09-09T01:47:10Z".to_owned())
    );
    let encoded = format!("{payload:?}");
    assert!(
        ![
            "access_token",
            "refresh_token",
            "private-access",
            "generation"
        ]
        .iter()
        .any(|forbidden| encoded.contains(*forbidden))
    );
}

#[test]
fn inventory_versions_summaries_duplicates_and_bounds_fail_closed() {
    let fixture = fixture();
    let store = &fixture.store;
    let latest = AssistantView {
        version: "latest".to_owned(),
        ..spec()
    };
    assert_eq!(
        inventory_payload("team_1", &[latest], store),
        Err(FlowError::Invalid)
    );
    let padded = AssistantView {
        summary: " summary".to_owned(),
        ..spec()
    };
    assert_eq!(
        inventory_payload("team_1", &[padded], store),
        Err(FlowError::Invalid)
    );
    let bare = AssistantView {
        integrations: Vec::new(),
        ..spec()
    };
    assert_eq!(
        inventory_payload("team_1", &[bare.clone(), bare], store),
        Err(FlowError::Invalid)
    );
    let many: Vec<AssistantView> = (0..65)
        .map(|index| AssistantView {
            assistant_id: format!("assistant-{index}"),
            ..spec()
        })
        .collect();
    assert_eq!(
        inventory_payload("team_1", &many, store),
        Err(FlowError::TooMany)
    );
    assert_eq!(
        super::expiry_payload(Some((1 << 53) - 1)),
        Err(FlowError::Invalid)
    );
}

#[test]
fn a_projection_with_a_sensitive_field_is_never_produced() {
    let nested = Value::Object(vec![(
        "rows".to_owned(),
        Value::Array(vec![Value::Object(vec![(
            "Access_Token".to_owned(),
            Value::Null,
        )])]),
    )]);
    assert_eq!(assert_public(&nested), Err(FlowError::Invalid));
    assert_eq!(
        assert_public(&Value::Object(vec![("summary".to_owned(), Value::Null)])),
        Ok(())
    );
}

fn service(fixture: &Fixture) -> Arc<OAuthService> {
    let pkce = Arc::new(
        PkceStore::new(PkceLimits::default(), Arc::new(CountingEntropy::default())).expect("valid"),
    );
    let (broker, _) = client(Vec::new());
    Arc::new(OAuthService::new(
        pkce,
        Arc::clone(&fixture.store),
        Arc::new(broker),
    ))
}

#[tokio::test]
async fn private_resolution_returns_only_the_selected_action_integration() {
    let fixture = fixture();
    seal(&fixture, "cloudflare-read", &read_scopes(), 3600);
    seal(&fixture, "cloudflare-write", &write_scopes(), 3600);
    let resolved = resolve_action_integrations(
        "team_1",
        &spec(),
        "publish-post",
        &fixture.store,
        &service(&fixture),
    )
    .await;
    let resolved = resolved.expect("resolved");
    let ids: Vec<&str> = resolved.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(ids, ["cloudflare-write"]);
    assert_eq!(resolved[0].1.expose(), "private-access-token-123456");
}

#[tokio::test]
async fn private_resolution_fails_closed_without_a_usable_token() {
    let fixture = fixture();
    let service = service(&fixture);
    let missing =
        resolve_action_integrations("team_1", &spec(), "read-profile", &fixture.store, &service)
            .await;
    assert_eq!(missing.err(), Some(FlowError::Unresolved));
    let mut undeclared = spec();
    undeclared.actions[0].integrations = owned(&["missing"]);
    let refused = resolve_action_integrations(
        "team_1",
        &undeclared,
        "read-profile",
        &fixture.store,
        &service,
    )
    .await;
    assert_eq!(refused.err(), Some(FlowError::Undeclared));
    // An expired grant whose refresh has no broker lease cannot be renewed.
    seal(&fixture, "cloudflare-read", &read_scopes(), 30);
    fixture.clock.advance(Duration::from_secs(31));
    let expired =
        resolve_action_integrations("team_1", &spec(), "read-profile", &fixture.store, &service)
            .await;
    assert_eq!(expired.err(), Some(FlowError::Unresolved));
}
