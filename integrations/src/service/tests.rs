use std::sync::{Arc, Mutex};
use std::time::Duration;

use team_core::secret::Secret;
use team_core::testing::CountingEntropy;

use super::{OAuthService, ServiceError, Start};
use crate::broker::BrokerResponse;
use crate::broker::tests::{Sent, client, grant_body, json, lease_text};
use crate::challenge::Pending;
use crate::pending::{self, IntegrationRequirement, RequiredIntegration};
use crate::pkce::{CallbackBinding, PkceLimits, PkceStore};
use crate::store::tests::{Fixture, fixture, put, scopes, tokens};
use crate::store::{Declaration, Status};
use crate::token::TokenSet;

const SESSION: &str = "browser-session-private-123456789";
const OTHER_SESSION: &str = "other-browser-session-123456789";

struct Harness {
    store: Fixture,
    pkce: Arc<PkceStore>,
    service: OAuthService,
    sent: Arc<Mutex<Vec<Sent>>>,
}

fn harness(answers: Vec<BrokerResponse>) -> Harness {
    let store = fixture();
    let pkce = Arc::new(
        PkceStore::new(PkceLimits::default(), Arc::new(CountingEntropy::default())).expect("valid"),
    );
    let (broker, sent) = client(answers);
    let service = OAuthService::new(
        Arc::clone(&pkce),
        Arc::clone(&store.store),
        Arc::new(broker),
    );
    Harness {
        store,
        pkce,
        service,
        sent,
    }
}

fn requirement(assistant: &str, provider: &str, requested: Vec<String>) -> IntegrationRequirement {
    IntegrationRequirement {
        assistant_id: assistant.to_owned(),
        assistant_name: assistant.to_owned(),
        action_ids: vec!["list-zones".to_owned()],
        integrations: vec![RequiredIntegration {
            id: "cloudflare".to_owned(),
            provider: provider.to_owned(),
            scopes: requested,
        }],
    }
}

type Paused = Arc<Pending<Vec<IntegrationRequirement>, &'static str>>;

fn paused(requirements: Vec<IntegrationRequirement>) -> Paused {
    let store = pending::default_store(Arc::new(CountingEntropy::default())).expect("defaults");
    store
        .create("team_1", requirements, "paused user input")
        .expect("created")
}

fn start<'a>(assistant: &'a str, mode: &'a str) -> Start<'a> {
    Start {
        session_binding: SESSION,
        assistant_id: assistant,
        integration_id: "cloudflare",
        callback_mode: mode,
        resource_binding: None,
    }
}

fn state(url: &str) -> String {
    url.split("state=")
        .nth(1)
        .and_then(|rest| rest.split('&').next())
        .expect("a state")
        .to_owned()
}

fn declared(provider: &str, requested: Vec<String>) -> Declaration {
    Declaration {
        provider: provider.to_owned(),
        scopes: requested,
    }
}

async fn complete(
    harness: &Harness,
    state: &str,
    session: &str,
    current: Option<Declaration>,
) -> Result<super::Completion, ServiceError> {
    let claim = "a".repeat(64);
    harness
        .service
        .complete(state, &claim, session, move |_binding: &CallbackBinding| {
            Box::pin(async move { current.ok_or(()) })
        })
        .await
}

fn operations(harness: &Harness) -> Vec<String> {
    let sent = harness.sent.lock().expect("usable");
    sent.iter()
        .map(|(url, _, _)| url.rsplit('/').next().unwrap_or_default().to_owned())
        .collect()
}

#[tokio::test]
async fn the_url_selects_the_exact_requested_unconfigured_requirement() {
    let harness = harness(Vec::new());
    harness
        .store
        .store
        .put(
            ("team_1", "a-assistant", "cloudflare"),
            ("cloudflare", &scopes()),
            &tokens("access-token-a-1234567", None, 3600),
            None,
        )
        .expect("sealed");
    let flow = paused(vec![
        requirement("z-assistant", "cloudflare", scopes()),
        requirement("a-assistant", "cloudflare", scopes()),
    ]);
    let url = harness
        .service
        .authorization_url(&flow, &start("z-assistant", "local-domain"))
        .expect("started");
    assert!(url.contains("&scope=dns.read+offline_access+zone.read&callback=local-domain"));
    let completed = complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", scopes())),
    )
    .await
    .expect("completed");
    let ids = (
        completed.team_id.as_str(),
        completed.assistant_id.as_str(),
        completed.integration_id.as_str(),
    );
    assert_eq!(ids, ("team_1", "z-assistant", "cloudflare"));
    // Generations are store-wide: the pre-connected a-assistant grant consumed generation 1.
    assert_eq!(
        (completed.provider.as_str(), completed.generation),
        ("cloudflare", 2)
    );
    let rendered = format!("{completed:?}");
    let private = [
        &"a".repeat(64),
        &state(&url),
        &lease_text(),
        "verifier",
        "access-token",
    ];
    assert!(!private.iter().any(|private| rendered.contains(*private)));
}

#[tokio::test]
async fn authorize_body_exact_fields() {
    let harness = harness(Vec::new());
    let url = harness
        .service
        .authorization_url(
            &paused(vec![requirement(
                "shimpz-cloudflare",
                "cloudflare",
                scopes(),
            )]),
            &start("shimpz-cloudflare", "local-domain"),
        )
        .expect("started");
    complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", scopes())),
    )
    .await
    .expect("completed");
    let sent = harness.sent.lock().expect("usable");
    let body = strict_json::parse(&sent[0].2).expect("json");
    let strict_json::Value::Object(members) = body else {
        panic!("an object")
    };
    let fields: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    assert_eq!(fields, ["claim", "state", "code_verifier"]);
    assert!(!String::from_utf8_lossy(&sent[0].2).contains("secret"));
}

#[tokio::test]
async fn a_pair_outside_the_pending_challenge_is_refused_without_state() {
    let harness = harness(Vec::new());
    let flow = paused(vec![
        requirement("a-assistant", "cloudflare", scopes()),
        requirement("z-assistant", "cloudflare", scopes()),
    ]);
    assert_eq!(
        harness
            .service
            .authorization_url(&flow, &start("missing-assistant", "loopback"))
            .err(),
        Some(ServiceError::NotPending)
    );
    assert_eq!(harness.pkce.cancel_all(), Ok(0));
    let url = harness
        .service
        .authorization_url(&flow, &start("z-assistant", "loopback"))
        .expect("started");
    let completed = complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", scopes())),
    )
    .await;
    assert_eq!(completed.expect("completed").assistant_id, "z-assistant");
}

#[tokio::test]
async fn claim_requires_state_and_session_binding_constant_time() {
    let harness = harness(Vec::new());
    let url = harness
        .service
        .authorization_url(
            &paused(vec![requirement(
                "shimpz-cloudflare",
                "cloudflare",
                scopes(),
            )]),
            &start("shimpz-cloudflare", "loopback"),
        )
        .expect("started");
    let current = || Some(declared("cloudflare", scopes()));
    assert_eq!(
        complete(&harness, &state(&url), OTHER_SESSION, current())
            .await
            .err(),
        Some(ServiceError::Failed)
    );
    assert_eq!(operations(&harness).len(), 0);
    assert!(
        complete(&harness, &state(&url), SESSION, current())
            .await
            .is_ok()
    );
    assert_eq!(operations(&harness), ["claim"]);
}

#[tokio::test]
async fn claim_one_use() {
    let harness = harness(Vec::new());
    let url = harness
        .service
        .authorization_url(
            &paused(vec![requirement(
                "shimpz-cloudflare",
                "cloudflare",
                scopes(),
            )]),
            &start("shimpz-cloudflare", "loopback"),
        )
        .expect("started");
    let current = || Some(declared("cloudflare", scopes()));
    assert!(
        complete(&harness, &state(&url), SESSION, current())
            .await
            .is_ok()
    );
    assert_eq!(
        complete(&harness, &state(&url), SESSION, current())
            .await
            .err(),
        Some(ServiceError::Failed)
    );
    assert_eq!(operations(&harness), ["claim"]);
}

#[tokio::test]
async fn install_or_scope_drift_consumes_the_state_before_any_exchange() {
    let harness = harness(Vec::new());
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        scopes(),
    )]);
    for current in [
        None,
        Some(declared("cloudflare", vec!["dns.read".to_owned()])),
    ] {
        let url = harness
            .service
            .authorization_url(&flow, &start("shimpz-cloudflare", "loopback"))
            .expect("started");
        assert_eq!(
            complete(&harness, &state(&url), SESSION, current)
                .await
                .err(),
            Some(ServiceError::Failed)
        );
        let replay = complete(
            &harness,
            &state(&url),
            SESSION,
            Some(declared("cloudflare", scopes())),
        )
        .await;
        assert_eq!(replay.err(), Some(ServiceError::Failed));
    }
    assert_eq!(operations(&harness).len(), 0);
}

#[tokio::test]
async fn provider_and_scope_injection_and_expired_challenges_fail_closed() {
    let harness = harness(Vec::new());
    let injected = [
        requirement("shimpz-cloudflare", "https://evil.example/token", scopes()),
        requirement(
            "shimpz-cloudflare",
            "cloudflare",
            vec!["dns.read".to_owned(), "https://evil.example".to_owned()],
        ),
    ];
    for malicious in injected {
        let refused = harness.service.authorization_url(
            &paused(vec![malicious]),
            &start("shimpz-cloudflare", "loopback"),
        );
        assert_eq!(refused.err(), Some(ServiceError::Failed));
    }
    let mut duplicate = requirement("shimpz-cloudflare", "cloudflare", scopes());
    duplicate
        .integrations
        .push(duplicate.integrations[0].clone());
    let refused = harness.service.authorization_url(
        &paused(vec![duplicate]),
        &start("shimpz-cloudflare", "loopback"),
    );
    assert_eq!(refused.err(), Some(ServiceError::Failed));
    assert_eq!(harness.pkce.cancel_all(), Ok(0));
    assert_eq!(operations(&harness).len(), 0);
}

#[tokio::test(start_paused = true)]
async fn an_expired_pending_challenge_starts_nothing() {
    let harness = harness(Vec::new());
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        scopes(),
    )]);
    tokio::time::advance(Duration::from_secs(600)).await;
    let refused = harness
        .service
        .authorization_url(&flow, &start("shimpz-cloudflare", "loopback"));
    assert_eq!(refused.err(), Some(ServiceError::Failed));
    assert_eq!(harness.pkce.cancel_all(), Ok(0));
}

#[tokio::test]
async fn a_broker_answer_outside_its_shape_never_reflects_private_values() {
    let leaked = "broker-private-response-123456789";
    let harness = harness(vec![json(&format!(
        r#"{{"access_token":"{leaked}","unexpected":"{leaked}"}}"#
    ))]);
    let url = harness
        .service
        .authorization_url(
            &paused(vec![requirement(
                "shimpz-cloudflare",
                "cloudflare",
                scopes(),
            )]),
            &start("shimpz-cloudflare", "loopback"),
        )
        .expect("started");
    let error = complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", scopes())),
    )
    .await
    .expect_err("refused");
    assert_eq!(error, ServiceError::Failed);
    assert!(!format!("{error:?} {error}").contains(leaked));
    let row = crate::store::tests::metadata(&harness.store.store).expect("listed");
    assert_eq!(row[0].status, Status::Missing);
}

const FULL: &str = r#"["dns.read","offline_access","zone.read"]"#;

fn narrowed() -> Vec<String> {
    vec!["dns.read".to_owned(), "zone.read".to_owned()]
}

fn leased(expires_in: i64) -> TokenSet {
    let refresh = Some("old-refresh-token-1234567");
    TokenSet {
        broker_lease: Some(Secret::new(lease_text())),
        ..tokens("old-access-token-123456", refresh, expires_in)
    }
}

#[tokio::test]
async fn reconsent_revokes_the_old_broker_grant_before_claiming_the_replacement() {
    let grant = json(&grant_body(3600).replace(FULL, r#"["dns.read","zone.read"]"#));
    let harness = harness(vec![grant, json(r#"{"revoked":true}"#)]);
    let first = put(&harness.store.store, "team_1", "cloudflare", &leased(3600)).expect("sealed");
    // The Assistant now declares fewer scopes, so the stored grant needs a new consent.
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        narrowed(),
    )]);
    let url = harness
        .service
        .authorization_url(&flow, &start("shimpz-cloudflare", "loopback"))
        .expect("started");
    let completed = complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", narrowed())),
    )
    .await;
    assert_eq!(
        (first.generation, completed.expect("completed").generation),
        (1, 2)
    );
    assert_eq!(operations(&harness), ["revoke", "claim"]);
}

#[tokio::test]
async fn a_refresh_required_grant_does_not_start_a_new_authorization() {
    let harness = harness(Vec::new());
    put(&harness.store.store, "team_1", "cloudflare", &leased(30)).expect("sealed");
    harness.store.clock.advance(Duration::from_secs(31));
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        scopes(),
    )]);
    let refused = harness
        .service
        .authorization_url(&flow, &start("shimpz-cloudflare", "loopback"));
    assert_eq!(refused.err(), Some(ServiceError::AlreadyConfigured));
    assert_eq!(harness.pkce.cancel_all(), Ok(0));
    assert_eq!(operations(&harness).len(), 0);
}

#[tokio::test]
async fn a_grant_without_a_lease_cannot_be_revoked_so_it_is_never_replaced() {
    let harness = harness(Vec::new());
    put(
        &harness.store.store,
        "team_1",
        "cloudflare",
        &tokens("old-access-token-123456", None, 3600),
    )
    .expect("sealed");
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        narrowed(),
    )]);
    let url = harness
        .service
        .authorization_url(&flow, &start("shimpz-cloudflare", "loopback"))
        .expect("started");
    let completed = complete(
        &harness,
        &state(&url),
        SESSION,
        Some(declared("cloudflare", narrowed())),
    )
    .await;
    assert_eq!(completed.err(), Some(ServiceError::Failed));
    assert_eq!(operations(&harness).len(), 0);
}

#[tokio::test]
async fn an_invalid_callback_mode_creates_no_challenge_and_cancel_is_scoped() {
    let harness = harness(Vec::new());
    let flow = paused(vec![requirement(
        "shimpz-cloudflare",
        "cloudflare",
        scopes(),
    )]);
    let refused = harness.service.authorization_url(
        &flow,
        &start("shimpz-cloudflare", "https://attacker.example"),
    );
    assert_eq!(refused.err(), Some(ServiceError::CallbackMode));
    assert_eq!(harness.pkce.cancel_all(), Ok(0));
    let url = harness
        .service
        .authorization_url(&flow, &start("shimpz-cloudflare", "out-of-band"))
        .expect("started");
    assert_eq!(
        (
            harness.service.cancel(SESSION),
            harness.service.cancel(SESSION)
        ),
        (Ok(true), Ok(false))
    );
    assert_eq!(harness.service.cancel("short"), Err(ServiceError::Failed));
    assert!(
        complete(
            &harness,
            &state(&url),
            SESSION,
            Some(declared("cloudflare", scopes()))
        )
        .await
        .is_err()
    );
    assert_eq!(operations(&harness).len(), 0);
}

#[tokio::test]
async fn a_refresh_goes_through_the_broker_and_needs_a_lease() {
    let harness = harness(Vec::new());
    let refresh = Secret::new("refresh-token-private-123456789".to_owned());
    let refused = harness
        .service
        .refresh("cloudflare", &scopes(), &refresh, None)
        .await;
    assert_eq!(refused.err(), Some(ServiceError::Failed));
    let lease = Secret::new(lease_text());
    assert!(
        harness
            .service
            .refresh("cloudflare", &scopes(), &refresh, Some(&lease))
            .await
            .is_ok()
    );
    assert_eq!(
        harness
            .service
            .refresh("missing", &scopes(), &refresh, Some(&lease))
            .await
            .err(),
        Some(ServiceError::Failed)
    );
    assert_eq!(operations(&harness), ["refresh"]);
}
