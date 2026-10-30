use std::collections::BTreeSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_core::testing::MemoryAudit;
use team_integrations::https::Tunnel;
use team_integrations::providers;
use tokio::time::Instant;

use super::{
    Attempt, Broker, Call, CallScope, Capacity, Credential, EgressRoute, Placement, Refusal,
    Routes, Transport, action_credentials, frame,
};
use crate::frames::CallBroker;

mod transport;

pub(crate) const HOST: &str = "api.example.com";
pub(crate) const TOKEN: &str = "test-token";
const SECRET: &str = "test-secret";
pub(crate) const PROXY_TOKEN: &str = "ffffffffffffffffffffffffffffffff";

pub(crate) fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn vectors(name: &str) -> Vec<Value> {
    let path = format!(
        "{}/../protocol/assistant/v1/vectors/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    let vectors = strict_json::parse(&std::fs::read(path).expect("vectors")).expect("json");
    assert_eq!(
        vectors.get("version"),
        Some(&Value::Number(strict_json::Number::Int(1)))
    );
    let Some(Value::Array(cases)) = vectors.get("cases") else {
        panic!("cases")
    };
    cases.clone()
}

/// Counts reads and answers a fixed route.
pub(crate) struct Route(
    pub Result<Option<(Secret, Vec<String>)>, Refusal>,
    pub AtomicUsize,
);

impl EgressRoute for Route {
    fn route(&self) -> Result<Option<(Secret, Vec<String>)>, Refusal> {
        self.1.fetch_add(1, Ordering::SeqCst);
        self.0.clone()
    }
}

pub(crate) fn route(hosts: &[&str]) -> Route {
    let hosts = hosts.iter().map(|host| (*host).to_owned()).collect();
    Route(
        Ok(Some((Secret::new(PROXY_TOKEN.to_owned()), hosts))),
        AtomicUsize::new(0),
    )
}

pub(crate) fn any_route() -> Routes {
    Routes::Declared(json(
        r#"[{"method":"GET","path":"/v1/items"},{"method":"POST","path":"/v1/items"}]"#,
    ))
}

pub(crate) fn credential(
    id: &str,
    header: Option<&str>,
    query: Option<&str>,
    value: &str,
    protected: &[&str],
) -> Credential {
    Credential {
        id: id.to_owned(),
        host: HOST.to_owned(),
        header: header.map(str::to_owned),
        query: query.map(str::to_owned),
        value: Secret::new(value.to_owned()),
        protected: protected
            .iter()
            .map(|value| Secret::new((*value).to_owned()))
            .collect(),
        routes: any_route(),
    }
}

pub(crate) struct Scope {
    pub credentials: Vec<Credential>,
    pub missing: BTreeSet<String>,
    pub authorized: bool,
    pub route: Route,
    pub audit: Arc<MemoryAudit>,
    pub stop: Stop,
    pub capacity: Capacity,
    pub tunnel: Tunnel,
}

pub(crate) fn scope(credentials: Vec<Credential>) -> Scope {
    Scope {
        credentials,
        missing: BTreeSet::new(),
        authorized: true,
        route: route(&[HOST]),
        audit: Arc::new(MemoryAudit::default()),
        stop: Stop::new(),
        capacity: Capacity::default(),
        tunnel: Tunnel::new(
            "127.0.0.1".to_owned(),
            1,
            Duration::from_secs(1),
            rustls::RootCertStore::empty(),
        ),
    }
}

impl Scope {
    pub(crate) fn broker(self) -> Broker {
        let audit: Arc<dyn AuditSink> = self.audit.clone();
        self.broker_with(audit)
    }

    pub(crate) fn broker_with(self, audit: Arc<dyn AuditSink>) -> Broker {
        Broker::new(CallScope {
            attempt: Attempt {
                team_id: "team_1".to_owned(),
                assistant_id: "meta-ads".to_owned(),
                action_id: "list-campaigns".to_owned(),
                operation_id: "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6".to_owned(),
            },
            credentials: self.credentials,
            missing: self.missing,
            authorized: self.authorized,
            route: Box::new(self.route),
            audit,
            stop: self.stop,
            capacity: self.capacity,
            transport: Arc::new(Transport::new(self.tunnel)),
        })
    }
}

pub(crate) fn fetch(method: &str, url: &str, headers: &str) -> Value {
    json(&format!(
        r#"{{"type":"fetch","method":"{method}","url":"{url}","headers":{headers}}}"#
    ))
}

pub(crate) fn items() -> Value {
    fetch("GET", &format!("https://{HOST}/v1/items?limit=2"), "[]")
}

pub(crate) async fn answer(broker: &mut Broker, frame: Value) -> Value {
    let reply = broker
        .answer(frame, Instant::now() + Duration::from_secs(5))
        .await
        .expect("answered");
    strict_json::parse(&reply).expect("reply json")
}

pub(crate) fn refused(code: &str) -> Value {
    json(&format!(r#"{{"error":"{code}"}}"#))
}

pub(crate) fn phases(audit: &MemoryAudit) -> Vec<(String, Option<String>)> {
    let text = |value: Option<&(String, Value)>| match value {
        Some((_, Value::String(text))) => Some(text.clone()),
        _ => None,
    };
    audit
        .events()
        .iter()
        .map(|event| {
            let field = |name: &str| text(event.fields().iter().find(|(key, _)| key == name));
            (field("phase").unwrap_or_default(), field("reason"))
        })
        .collect()
}

#[test]
fn the_published_call_vectors_match_frame_admission() {
    let hosts = [HOST.to_owned()];
    for case in vectors("fetch.json") {
        let admitted = frame::parse(case.get("frame").expect("frame"), &hosts).is_ok();
        assert_eq!(
            Some(&Value::Bool(admitted)),
            case.get("valid"),
            "{:?}",
            case.get("name")
        );
    }
}

#[test]
fn every_undeclared_destination_and_team_owned_field_is_refused() {
    let hosts = [HOST.to_owned()];
    let url = |url: &str| fetch("GET", url, "[]");
    let header = |headers: &str| fetch("GET", &format!("https://{HOST}/v1"), headers);
    let refused = [
        url("https://collector.example.org/steal"),
        url(&format!("https://{HOST}:8443/v1")),
        url(&format!("https://user@{HOST}/v1")),
        url("https://API.EXAMPLE.COM/v1"),
        url(&format!(r"https://{HOST}\\@collector.example.org/v1")),
        url(&format!("https://{HOST}/v1#fragment")),
        url(&format!("http://{HOST}/v1")),
        header(r#"[["Host","collector.example.org"]]"#),
        header(r#"[["Accept-Encoding","gzip"]]"#),
        header(r#"[["x-a","1"],["X-A","2"]]"#),
        header(r#"[["x-a","a\nb"]]"#),
        header(r#"{"accept":"*/*"}"#),
    ];
    let admitted: Vec<_> = refused
        .iter()
        .filter(|frame| frame::parse(frame, &hosts).is_ok())
        .collect();
    assert!(admitted.is_empty(), "{admitted:?}");
}

#[test]
fn call_count_url_header_body_response_bounds() {
    let hosts = [HOST.to_owned()];
    let long = format!("https://{HOST}/{}", "a".repeat(super::MAX_URL_CHARACTERS));
    let headers = format!(
        "[{}]",
        vec![r#"["x-a","1"]"#; super::MAX_HEADERS + 1].join(",")
    );
    let oversized = format!(
        r#"{{"type":"fetch","method":"POST","url":"https://{HOST}/","headers":[],"body":"{}"}}"#,
        "A".repeat((super::MAX_REQUEST_BYTES + 3) / 3 * 4 + 4)
    );
    let cases = [
        fetch("GET", &long, "[]"),
        fetch("GET", &format!("https://{HOST}/"), &headers),
        json(&oversized),
    ];
    assert!(
        cases
            .iter()
            .all(|frame| frame::parse(frame, &hosts).is_err())
    );
    let timeout = |value: &str| {
        json(&format!(
            r#"{{"type":"fetch","method":"GET","url":"https://{HOST}/","headers":[],"timeout_ms":{value}}}"#
        ))
    };
    assert!(frame::parse(&timeout("30000"), &hosts).is_ok());
    assert!(
        ["0", "30001", "true", "1.5"]
            .iter()
            .all(|value| frame::parse(&timeout(value), &hosts).is_err())
    );
}

#[tokio::test]
async fn call_count_is_bounded_to_sixteen_per_attempt() {
    let mut scope = scope(Vec::new());
    scope.missing.insert(HOST.to_owned());
    let mut broker = scope.broker();
    let mut replies = Vec::new();
    for _ in 0..=super::MAX_CALLS {
        replies.push(answer(&mut broker, items()).await);
    }
    assert_eq!(replies[0], refused("credential-missing"));
    assert_eq!(replies[super::MAX_CALLS], refused("refused"));
    assert_eq!(broker.calls(), super::MAX_CALLS + 1);
}

#[tokio::test]
async fn credential_field_override_refused() {
    let header = credential("stored-input:k", Some("x-api-key"), None, "v", &["v"]);
    let proof = credential("stored-input:p", None, Some("appsecret_proof"), "p", &["p"]);
    let cases = [
        (
            header,
            fetch(
                "GET",
                &format!("https://{HOST}/v1/items"),
                r#"[["X-Api-Key","mine"]]"#,
            ),
        ),
        (
            proof.clone(),
            fetch(
                "GET",
                &format!("https://{HOST}/v1/items?appsecret_proof=forged"),
                "[]",
            ),
        ),
        (
            proof.clone(),
            fetch(
                "GET",
                &format!("https://{HOST}/v1/items?appsecret%5Fproof=forged"),
                "[]",
            ),
        ),
        (
            proof,
            fetch(
                "GET",
                &format!("https://{HOST}/v1/items?appsecret+proof=x&appsecret_proof"),
                "[]",
            ),
        ),
    ];
    for (credential, frame) in cases {
        let scope = scope(vec![credential]);
        let audit = Arc::clone(&scope.audit);
        assert_eq!(answer(&mut scope.broker(), frame).await, refused("refused"));
        assert_eq!(
            phases(&audit),
            [("refused".to_owned(), Some("credential-field".to_owned()))]
        );
    }
}

#[tokio::test]
async fn missing_credential_refused_before_dispatch() {
    let mut scope = scope(Vec::new());
    scope.missing.insert(HOST.to_owned());
    let audit = Arc::clone(&scope.audit);
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("credential-missing")
    );
    assert_eq!(
        phases(&audit),
        [("refused".to_owned(), Some("credential-missing".to_owned()))]
    );
}

#[tokio::test]
async fn no_dispatch_before_authorization() {
    let mut scope = scope(Vec::new());
    scope.authorized = false;
    let audit = Arc::clone(&scope.audit);
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("refused")
    );
    assert_eq!(
        phases(&audit),
        [(
            "refused".to_owned(),
            Some("authorization-pending".to_owned())
        )]
    );
}

#[tokio::test]
async fn the_egress_route_is_read_once_on_the_first_call_and_its_absence_refuses() {
    let mut unavailable = scope(Vec::new());
    unavailable.route = Route(
        Err(Refusal::new("unavailable", "egress-policy")),
        AtomicUsize::new(0),
    );
    let mut broker = unavailable.broker();
    assert_eq!(answer(&mut broker, items()).await, refused("unavailable"));
    let mut absent = scope(Vec::new());
    absent.route = Route(Ok(None), AtomicUsize::new(0));
    let mut broker = absent.broker();
    assert_eq!(answer(&mut broker, items()).await, refused("refused"));
    assert_eq!(answer(&mut broker, items()).await, refused("refused"));
}

fn placement(
    header: Option<&str>,
    query: Option<&str>,
    scheme: Option<&str>,
    hmac: Option<&str>,
    routes: &str,
) -> Placement {
    Placement {
        host: HOST.to_owned(),
        header: header.map(str::to_owned),
        query: query.map(str::to_owned),
        scheme: scheme.map(str::to_owned),
        hmac: hmac.map(str::to_owned),
        routes: json(routes),
    }
}

fn held(values: &[(&str, &str)]) -> Vec<(String, Secret)> {
    values
        .iter()
        .map(|(id, value)| ((*id).to_owned(), Secret::new((*value).to_owned())))
        .collect()
}

#[test]
fn a_bearer_header_and_an_hmac_proof_are_placed_and_unheld_ones_mark_their_host_missing() {
    let routes = r#"[{"method":"GET","path":"/v1/items"}]"#;
    let stored = vec![
        (
            "token".to_owned(),
            placement(Some("Authorization"), None, Some("Bearer"), None, routes),
        ),
        (
            "proof".to_owned(),
            placement(None, Some("appsecret_proof"), None, Some("token"), routes),
        ),
    ];
    let (credentials, missing) =
        action_credentials(&stored, &held(&[("token", TOKEN), ("proof", SECRET)]), &[]);
    assert!(missing.is_empty());
    assert_eq!(
        (
            credentials[0].header.as_deref(),
            credentials[0].value.expose()
        ),
        (Some("authorization"), "Bearer test-token")
    );
    let expected = crate::journal::hex(&{
        use hmac::digest::KeyInit;
        use hmac::{Hmac, Mac};
        let mut mac = Hmac::<sha2::Sha256>::new_from_slice(SECRET.as_bytes()).expect("key");
        mac.update(TOKEN.as_bytes());
        mac.finalize().into_bytes()
    });
    assert_eq!(credentials[1].value.expose(), expected);
    let protected: Vec<&str> = credentials[1]
        .protected
        .iter()
        .map(Secret::expose)
        .collect();
    assert_eq!(protected, [SECRET, expected.as_str()]);
    for values in [held(&[("proof", SECRET)]), held(&[("token", TOKEN)])] {
        let (credentials, missing) = action_credentials(&stored[1..], &values, &[]);
        assert!(credentials.is_empty() && missing.contains(HOST));
    }
}

#[test]
fn an_integration_bearer_goes_only_to_its_provider_api_hosts() {
    let cloudflare = providers::resolve("cloudflare").expect("reviewed");
    let integrations = [(
        "cloudflare".to_owned(),
        cloudflare,
        Some(Secret::new(TOKEN.to_owned())),
    )];
    let (credentials, missing) = action_credentials(&[], &[], &integrations);
    let placed: Vec<(&str, &str)> = credentials
        .iter()
        .map(|credential| (credential.host.as_str(), credential.value.expose()))
        .collect();
    assert_eq!(
        (placed, missing.len()),
        (vec![("api.cloudflare.com", "Bearer test-token")], 0)
    );
    let (none, missing) =
        action_credentials(&[], &[], &[("cloudflare".to_owned(), cloudflare, None)]);
    assert!(none.is_empty() && missing.contains("api.cloudflare.com"));
}

#[test]
fn a_stored_input_admits_exactly_the_calls_every_published_route_match_vector_admits() {
    for case in vectors("route-match.json") {
        let routes = Routes::Declared(case.get("routes").expect("routes").clone());
        let text = |key| match case.get(key) {
            Some(Value::String(text)) => text.clone(),
            _ => panic!("{key}"),
        };
        let admitted = routes.admits(&text("method"), &text("target"));
        assert_eq!(
            Some(&Value::Bool(admitted)),
            case.get("valid"),
            "{:?}",
            case.get("name")
        );
    }
}

#[tokio::test]
async fn every_host_credential_admits_exact_method_raw_path_and_selectors() {
    let stored = vec![
        (
            "token".to_owned(),
            placement(
                Some("Authorization"),
                None,
                Some("Bearer"),
                None,
                r#"[{"method":"GET","path":"/v1/*"}]"#,
            ),
        ),
        (
            "proof".to_owned(),
            placement(
                None,
                Some("appsecret_proof"),
                None,
                Some("token"),
                r#"[{"method":"GET","path":"/v1/items","query":[{"name":"fields","values":["id%2Cname"]}]}]"#,
            ),
        ),
        (
            "elsewhere".to_owned(),
            Placement {
                host: "other.example.com".to_owned(),
                ..placement(
                    Some("X-Key"),
                    None,
                    None,
                    None,
                    r#"[{"method":"POST","path":"/x"}]"#,
                )
            },
        ),
    ];
    let (credentials, _) = action_credentials(
        &stored,
        &held(&[("token", TOKEN), ("proof", SECRET), ("elsewhere", "v")]),
        &[],
    );
    let mut broker = scope(credentials).broker();
    let admitted = broker
        .admit(&fetch(
            "GET",
            &format!("https://{HOST}/v1/items?fields=id%2Cname"),
            "[]",
        ))
        .expect("admitted");
    assert!(
        admitted
            .headers
            .contains(&("authorization".to_owned(), format!("Bearer {TOKEN}")))
    );
    let proof = admitted
        .target
        .strip_prefix("/v1/items?fields=id%2Cname&appsecret_proof=")
        .expect("placed last");
    assert!(proof.len() == 64 && proof.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert!(!admitted.headers.iter().any(|(name, _)| name == "x-key"));
    for target in [
        "/v1/other",
        "/v1/items",
        "/v1/items?fields=id",
        "/v1/items?fields=id%2cname",
    ] {
        let refusal = broker.admit(&fetch("GET", &format!("https://{HOST}{target}"), "[]"));
        assert_eq!(refusal, Err(Refusal::new("refused", "route")), "{target}");
    }
}

#[tokio::test]
async fn an_integration_bearer_is_never_sent_outside_its_providers_reviewed_routes() {
    let cloudflare = providers::resolve("cloudflare").expect("reviewed");
    let (credentials, _) = action_credentials(
        &[],
        &[],
        &[(
            "cloudflare".to_owned(),
            cloudflare,
            Some(Secret::new(TOKEN.to_owned())),
        )],
    );
    let mut scope = scope(credentials);
    scope.route = route(&["api.cloudflare.com"]);
    let audit = Arc::clone(&scope.audit);
    let mut broker = scope.broker();
    let zones = broker
        .admit(&fetch(
            "GET",
            "https://api.cloudflare.com/client/v4/zones?page=1&per_page=25",
            "[]",
        ))
        .expect("admitted");
    assert!(
        zones
            .headers
            .contains(&("authorization".to_owned(), format!("Bearer {TOKEN}")))
    );
    let zone = "a".repeat(32);
    let outside = [
        ("GET", "/client/v4/user/tokens/verify".to_owned()),
        ("POST", "/client/v4/user/tokens".to_owned()),
        ("DELETE", format!("/client/v4/zones/{zone}")),
        ("GET", format!("/client/v4/zones/{zone}/../../user/tokens")),
    ];
    for (method, path) in outside {
        assert_eq!(
            answer(
                &mut broker,
                fetch(method, &format!("https://api.cloudflare.com{path}"), "[]")
            )
            .await,
            refused("refused")
        );
    }
    assert!(
        phases(&audit)
            .iter()
            .all(|phase| *phase == ("refused".to_owned(), Some("route".to_owned())))
    );
}

#[test]
fn a_credential_value_http_cannot_carry_is_refused_and_another_hosts_is_not_placed() {
    let call = Call {
        method: "GET".to_owned(),
        host: HOST.to_owned(),
        target: "/".to_owned(),
        headers: Vec::new(),
        body: None,
        timeout_ms: None,
    };
    let euro = credential("stored-input:e", Some("x-key"), None, "k€y", &["k€y"]);
    assert_eq!(
        super::credential::inject(&call, &[euro]),
        Err(Refusal::new("refused", "credential-value"))
    );
    let other = Credential {
        host: "other.example.com".to_owned(),
        ..credential("stored-input:o", Some("x-key"), None, "value", &["value"])
    };
    assert_eq!(super::credential::inject(&call, &[other]), Ok(call));
}
