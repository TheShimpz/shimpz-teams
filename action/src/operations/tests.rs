use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::clock::Clock;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::testing::{CountingEntropy, FakeClock};
use team_integrations::broker::{BrokerClient, BrokerError, BrokerResponse, BrokerTransport};
use team_integrations::flow::{ActionView, AssistantView};
use team_integrations::pending::{self, IntegrationRequirement, RequiredIntegration};
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::service::OAuthService;
use team_integrations::store::{Declaration as IntegrationDeclaration, IntegrationStore};

use super::{
    AUTHORIZATION_INVALID, BODY_INVALID, CHALLENGE_EXPIRED, OAUTH_UNAVAILABLE,
    STORED_INPUT_NOT_FOUND, authorize, callback_team, cancel, clear_stored_input, complete,
    list_integrations,
};
use crate::stored_input::StoredInputStore;

const SESSION: &str = "browser-session-private-123456789";

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn scopes() -> Vec<String> {
    ["dns.read", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec()
}

/// Answers every broker call with a grant, recording the operations.
#[derive(Default)]
struct Granting(Arc<Mutex<Vec<String>>>);

impl BrokerTransport for Granting {
    fn post<'a>(
        &'a self,
        url: &'a str,
        _headers: &'a [(String, String)],
        _body: Vec<u8>,
    ) -> BoxFuture<'a, Result<BrokerResponse, BrokerError>> {
        self.0
            .lock()
            .expect("usable")
            .push(url.rsplit('/').next().unwrap_or_default().to_owned());
        let lease = format!(
            "l3.{}.1999999999.{}.{}.{}.{}",
            "a".repeat(11),
            "b".repeat(43),
            "c".repeat(43),
            "d".repeat(43),
            "e".repeat(43)
        );
        let body = format!(
            r#"{{"access_token":"access-token-private-1234","refresh_token":"refresh-token-private-1234","expires_in":3600,"scopes":["dns.read","offline_access","zone.read"],"broker_lease":"{lease}"}}"#
        );
        Box::pin(async move {
            Ok(BrokerResponse {
                status: 200,
                content_type: "application/json".to_owned(),
                body: body.into_bytes(),
            })
        })
    }
}

struct World {
    _root: tempfile::TempDir,
    store: Arc<IntegrationStore>,
    pkce: Arc<PkceStore>,
    service: OAuthService,
    calls: Arc<Mutex<Vec<String>>>,
}

fn world() -> World {
    let root = tempfile::tempdir().expect("temporary directory");
    let clock: Arc<dyn Clock> = Arc::new(FakeClock::at(
        UNIX_EPOCH + Duration::from_secs(1_000_000_000),
    ));
    let entropy = Arc::new(CountingEntropy::default());
    let store = IntegrationStore::new(
        root.path().join("i/state/i.json"),
        root.path().join("i/key/k"),
        clock,
        entropy.clone(),
    );
    let store = Arc::new(store.expect("store"));
    let pkce = Arc::new(PkceStore::new(PkceLimits::default(), entropy).expect("pkce"));
    let transport = Granting::default();
    let calls = Arc::clone(&transport.0);
    let service = OAuthService::new(
        Arc::clone(&pkce),
        Arc::clone(&store),
        Arc::new(BrokerClient::new(Box::new(transport))),
    );
    World {
        _root: root,
        store,
        pkce,
        service,
        calls,
    }
}

fn paused() -> Arc<team_integrations::challenge::Pending<Vec<IntegrationRequirement>, ()>> {
    let requirement = IntegrationRequirement {
        assistant_id: "shimpz-cloudflare".to_owned(),
        assistant_name: "Shimpz Cloudflare".to_owned(),
        action_ids: vec!["list-zones".to_owned()],
        integrations: vec![RequiredIntegration {
            id: "cloudflare".to_owned(),
            provider: "cloudflare".to_owned(),
            scopes: scopes(),
        }],
    };
    let store = pending::default_store(Arc::new(CountingEntropy::default())).expect("defaults");
    store
        .create("team_1", vec![requirement], ())
        .expect("created")
}

fn authorization(mode: &str) -> Value {
    json(&format!(
        r#"{{"assistant_id":"shimpz-cloudflare","integration_id":"cloudflare","callback_mode":"{mode}","session_binding":"{SESSION}"}}"#
    ))
}

fn state(url: &str) -> String {
    url.split("state=")
        .nth(1)
        .and_then(|rest| rest.split('&').next())
        .expect("a state")
        .to_owned()
}

#[test]
fn internal_oauth_routes_are_closed_and_exact() {
    let world = world();
    assert_eq!(
        authorize(
            Ok(paused()),
            &world.service,
            &authorization("https://attacker.example")
        ),
        Err(AUTHORIZATION_INVALID)
    );
    assert_eq!(
        authorize(
            Ok(paused()),
            &world.service,
            &json(r#"{"session_binding":"x"}"#)
        ),
        Err(AUTHORIZATION_INVALID)
    );
    let expired = Err(team_integrations::challenge::ChallengeError::NotFound);
    assert_eq!(
        authorize::<()>(expired, &world.service, &authorization("loopback")),
        Err(CHALLENGE_EXPIRED)
    );
    let started =
        authorize(Ok(paused()), &world.service, &authorization("out-of-band")).expect("started");
    assert!(
        matches!(started.get("authorization_url"), Some(Value::String(url)) if url.starts_with("https://shimpz.com/api/oauth/cloudflare/start?"))
    );
    assert_eq!(
        cancel(
            &world.service,
            &json(&format!(r#"{{"session_binding":"{SESSION}"}}"#))
        ),
        Ok(json(r#"{"cancelled":true}"#))
    );
    assert_eq!(
        cancel(
            &world.service,
            &json(r#"{"session_binding":"x","extra":1}"#)
        ),
        Err(BODY_INVALID)
    );
    assert_eq!(world.calls.lock().expect("usable").len(), 0);
}

fn callback(url: &str) -> Value {
    json(&format!(
        r#"{{"state":"{}","claim":"{}","session_binding":"{SESSION}"}}"#,
        state(url),
        "a".repeat(64)
    ))
}

fn declaration() -> IntegrationDeclaration {
    IntegrationDeclaration {
        provider: "cloudflare".to_owned(),
        scopes: scopes(),
    }
}

#[tokio::test]
async fn a_completion_is_bound_to_its_team_and_current_declaration() {
    let world = world();
    let started =
        authorize(Ok(paused()), &world.service, &authorization("loopback")).expect("started");
    let Some(Value::String(url)) = started.get("authorization_url") else {
        panic!("a url")
    };
    let body = callback(url);
    assert_eq!(callback_team(&world.pkce, &body), Ok("team_1".to_owned()));
    // A declaration resolved under another Team's lock is refused before any exchange.
    let foreign = complete(&world.service, "team_2", &body, |_| {
        Box::pin(async { Ok(declaration()) })
    })
    .await;
    assert_eq!(foreign, Err(OAUTH_UNAVAILABLE));
    assert_eq!(world.calls.lock().expect("usable").len(), 0);
    let retry =
        authorize(Ok(paused()), &world.service, &authorization("loopback")).expect("started");
    let Some(Value::String(url)) = retry.get("authorization_url") else {
        panic!("a url")
    };
    let body = callback(url);
    let completed = complete(&world.service, "team_1", &body, |_| {
        Box::pin(async { Ok(declaration()) })
    })
    .await;
    assert_eq!(
        completed,
        Ok(json(
            r#"{"connected":true,"team_id":"team_1","assistant_id":"shimpz-cloudflare","integration_id":"cloudflare"}"#
        ))
    );
    assert_eq!(*world.calls.lock().expect("usable"), ["claim"]);
}

#[test]
fn an_unknown_callback_is_refused_before_any_exchange() {
    let world = world();
    let unknown = json(&format!(
        r#"{{"state":"{}","claim":"{}","session_binding":"{SESSION}"}}"#,
        "s".repeat(43),
        "a".repeat(64)
    ));
    assert_eq!(callback_team(&world.pkce, &unknown), Err(OAUTH_UNAVAILABLE));
    assert_eq!(callback_team(&world.pkce, &json("{}")), Err(BODY_INVALID));
}

#[test]
fn the_integration_inventory_has_one_exact_shape_without_tokens() {
    let world = world();
    let assistant = AssistantView {
        assistant_id: "shimpz-cloudflare".to_owned(),
        version: "0.4.1".to_owned(),
        name: "Shimpz Cloudflare".to_owned(),
        summary: "Cloudflare test fixture".to_owned(),
        actions: vec![ActionView {
            id: "list-zones".to_owned(),
            summary: "List zones.".to_owned(),
            integrations: vec!["cloudflare".to_owned()],
        }],
        integrations: vec![("cloudflare".to_owned(), declaration())],
    };
    let listed = list_integrations("team_1", &[assistant], &world.store).expect("listed");
    let encoded =
        team_protocol::python::json::dumps(&listed, team_protocol::python::json::CANONICAL);
    assert!(
        encoded.starts_with(r#"{"integrations":[{"assistant_id":"shimpz-cloudflare","#)
            && encoded.ends_with(r#""team_id":"team_1"}"#)
    );
    assert!(
        encoded.contains(r#""status":"missing""#)
            && !encoded.contains("token")
            && !encoded.contains("generation")
    );
}

#[test]
fn only_a_declared_stored_input_is_cleared() {
    let root = tempfile::tempdir().expect("temporary directory");
    let clock: Arc<dyn Clock> = Arc::new(FakeClock::at(UNIX_EPOCH));
    let store = StoredInputStore::new(
        root.path().join("s/state/s.json"),
        root.path().join("s/key/k"),
        clock,
        Arc::new(CountingEntropy::default()),
    )
    .expect("store");
    store
        .seal(
            ("team_1", "whatsapp", "token"),
            "password",
            &Secret::new("value-1234".to_owned()),
            &"a".repeat(64),
        )
        .expect("sealed");
    let declared = ["token".to_owned()];
    assert_eq!(
        clear_stored_input(&store, "team_1", "whatsapp", &declared, "other"),
        Err(STORED_INPUT_NOT_FOUND)
    );
    let cleared =
        clear_stored_input(&store, "team_1", "whatsapp", &declared, "token").expect("cleared");
    assert_eq!(cleared.get("cleared"), Some(&Value::Bool(true)));
    let again =
        clear_stored_input(&store, "team_1", "whatsapp", &declared, "token").expect("cleared");
    assert_eq!(again.get("cleared"), Some(&Value::Bool(false)));
}
