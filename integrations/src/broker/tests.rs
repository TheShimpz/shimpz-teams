use std::sync::{Arc, Mutex};

use team_core::future::BoxFuture;
use team_core::secret::Secret;

use super::{
    BrokerClient, BrokerError, BrokerResponse, BrokerTransport, binding, lease, object, tokens,
};

mod transport;

pub(crate) const STATE: &str = "sssssssssssssssssssssssssssssssssssssssssss";
pub(crate) const CHALLENGE: &str = "ccccccccccccccccccccccccccccccccccccccccccc";
pub(crate) const ACCESS: &str = "access-token-private-123456789";
pub(crate) const REFRESH: &str = "refresh-token-private-123456789";

pub(crate) fn claim_id() -> String {
    "a".repeat(64)
}

pub(crate) fn lease_text() -> String {
    format!(
        "l3.{}.1999999999.{}.{}.{}.{}",
        "a".repeat(11),
        "b".repeat(43),
        "c".repeat(43),
        "d".repeat(43),
        "e".repeat(43)
    )
}

pub(crate) fn scopes() -> Vec<String> {
    ["dns.read", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec()
}

pub(crate) fn grant_body(expires_in: i64) -> String {
    format!(
        r#"{{"access_token":"{ACCESS}","refresh_token":"{REFRESH}","expires_in":{expires_in},"scopes":["dns.read","offline_access","zone.read"],"broker_lease":"{}"}}"#,
        lease_text()
    )
}

/// One recorded request: its URL, headers, and body.
pub(crate) type Sent = (String, Vec<(String, String)>, Vec<u8>);

/// A transport that records each request and answers the next queued response, or a grant (revoked for `revoke`).
#[derive(Default)]
pub(crate) struct Scripted {
    pub sent: Arc<Mutex<Vec<Sent>>>,
    pub answers: Mutex<Vec<BrokerResponse>>,
}

pub(crate) fn json(body: &str) -> BrokerResponse {
    BrokerResponse {
        status: 200,
        content_type: "application/json".to_owned(),
        body: body.as_bytes().to_vec(),
    }
}

impl BrokerTransport for Scripted {
    fn post<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        body: Vec<u8>,
    ) -> BoxFuture<'a, Result<BrokerResponse, BrokerError>> {
        self.sent
            .lock()
            .expect("usable")
            .push((url.to_owned(), headers.to_vec(), body));
        let queued = self.answers.lock().expect("usable").pop();
        let answer = queued.unwrap_or_else(|| {
            if url.ends_with("/revoke") {
                json(r#"{"revoked":true}"#)
            } else {
                json(&grant_body(3600))
            }
        });
        Box::pin(async move { Ok(answer) })
    }
}

pub(crate) fn client(answers: Vec<BrokerResponse>) -> (BrokerClient, Arc<Mutex<Vec<Sent>>>) {
    let transport = Scripted {
        answers: Mutex::new(answers),
        ..Scripted::default()
    };
    let sent = Arc::clone(&transport.sent);
    (BrokerClient::new(Box::new(transport)), sent)
}

#[test]
fn broker_url_fixed_no_port_userinfo_query() {
    let (client, sent) = client(Vec::new());
    let url = client
        .authorization_url("cloudflare", STATE, CHALLENGE, &scopes(), "loopback")
        .expect("built");
    assert_eq!(
        url,
        format!(
            "https://shimpz.com/api/oauth/cloudflare/start?state={STATE}&code_challenge={CHALLENGE}&scope=dns.read+offline_access+zone.read&callback=loopback"
        )
    );
    assert!(!url.contains("client"));
    for mode in ["local-domain", "out-of-band"] {
        let url = client
            .authorization_url("cloudflare", STATE, CHALLENGE, &scopes(), mode)
            .expect("built");
        assert!(url.ends_with(&format!("&callback={mode}")));
    }
    let refused = [
        ("cloudflare", STATE, CHALLENGE, "https://evil.example"),
        ("https://evil.example", STATE, CHALLENGE, "loopback"),
        ("cloudflare", "short", CHALLENGE, "loopback"),
        ("cloudflare", STATE, "short", "loopback"),
    ];
    let invalid = refused.iter().filter(|(provider, state, challenge, mode)| {
        client.authorization_url(provider, state, challenge, &scopes(), mode)
            == Err(BrokerError::Invalid)
    });
    assert_eq!(invalid.count(), 4);
    assert!(sent.lock().expect("usable").is_empty());
}

#[tokio::test]
async fn claim_refresh_and_revoke_use_only_fixed_broker_operations() {
    let (client, sent) = client(Vec::new());
    let verifier = Secret::new("v".repeat(43));
    let lease = Secret::new(lease_text());
    let claimed = client
        .claim("cloudflare", &claim_id(), STATE, &verifier, &scopes())
        .await
        .expect("claimed");
    let refreshed = client
        .refresh(
            "cloudflare",
            &Secret::new(REFRESH.to_owned()),
            &lease,
            &scopes(),
        )
        .await
        .expect("refreshed");
    client
        .revoke("cloudflare", &Secret::new(ACCESS.to_owned()), &lease)
        .await
        .expect("revoked");
    assert_eq!(
        claimed.broker_lease.as_ref().map(Secret::expose),
        Some(lease_text().as_str())
    );
    assert_eq!(refreshed.access_token.expose(), ACCESS);
    let sent = sent.lock().expect("usable");
    let operations: Vec<&str> = sent.iter().map(|(url, _, _)| url.as_str()).collect();
    assert_eq!(
        operations,
        ["claim", "refresh", "revoke"]
            .map(|operation| format!("https://shimpz.com/api/oauth/cloudflare/{operation}"))
    );
    let headers = [
        ("Accept", "application/json"),
        ("Content-Type", "application/json"),
        ("User-Agent", "shimpz-local-controller/1"),
    ]
    .map(|(name, value)| (name.to_owned(), value.to_owned()));
    assert!(sent.iter().all(|(_, sent, body)| *sent == headers
        && !String::from_utf8_lossy(body).contains("client_secret")));
    assert_eq!(sent[0].2, br#"{"claim":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","state":"sssssssssssssssssssssssssssssssssssssssssss","code_verifier":"vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv"}"#);
}

#[tokio::test]
async fn invalid_provider_and_response_shapes_fail_without_reflection() {
    let private = "private-broker-response-123456789";
    let lease = Secret::new(lease_text());
    let (client, _) = client(vec![json(&format!(r#"{{"unexpected":"{private}"}}"#))]);
    assert_eq!(
        client
            .revoke("unknown", &Secret::new(ACCESS.to_owned()), &lease)
            .await,
        Err(BrokerError::Invalid)
    );
    let refused = client
        .claim(
            "cloudflare",
            &claim_id(),
            STATE,
            &Secret::new("v".repeat(43)),
            &scopes(),
        )
        .await;
    let error = refused.expect_err("refused");
    assert_eq!(error, BrokerError::Response);
    assert!(!format!("{error:?} {error}").contains(private));
}

#[tokio::test]
async fn claim_lease_and_revoke_inputs_fail_closed() {
    let (client, sent) = client(vec![json(r#"{"revoked":false}"#)]);
    let verifier = Secret::new("v".repeat(43));
    assert_eq!(
        client
            .claim("cloudflare", "invalid", STATE, &verifier, &scopes())
            .await
            .err(),
        Some(BrokerError::Invalid)
    );
    let short = Secret::new("v".repeat(42));
    assert_eq!(
        client
            .claim("cloudflare", &claim_id(), STATE, &short, &scopes())
            .await
            .err(),
        Some(BrokerError::Invalid)
    );
    // Only ASCII digits are an expiry: Unicode decimal digits never pass.
    let arabic = lease_text().replace("1999999999", &"\u{661}".repeat(10));
    for refused in ["invalid".to_owned(), arabic] {
        let refused = Secret::new(refused);
        let token = Secret::new(REFRESH.to_owned());
        assert_eq!(
            client
                .refresh("cloudflare", &token, &refused, &scopes())
                .await
                .err(),
            Some(BrokerError::Invalid)
        );
        assert_eq!(
            client.revoke("cloudflare", &token, &refused).await,
            Err(BrokerError::Invalid)
        );
    }
    assert!(sent.lock().expect("usable").is_empty());
    let revoked = client
        .revoke(
            "cloudflare",
            &Secret::new(ACCESS.to_owned()),
            &Secret::new(lease_text()),
        )
        .await;
    assert_eq!(revoked, Err(BrokerError::Response));
}

#[test]
fn only_a_bounded_200_json_object_is_a_broker_answer() {
    let answers = [
        BrokerResponse {
            status: 503,
            ..json("{}")
        },
        BrokerResponse {
            content_type: "text/plain".to_owned(),
            ..json("{}")
        },
        json(""),
        BrokerResponse {
            body: vec![b' '; super::MAX_RESPONSE_BYTES + 1],
            ..json("")
        },
    ];
    assert!(
        answers
            .iter()
            .all(|answer| object(answer) == Err(BrokerError::Failed))
    );
    assert_eq!(object(&json("{")), Err(BrokerError::Response));
    assert_eq!(object(&json("[]")), Err(BrokerError::Response));
    let charset = BrokerResponse {
        content_type: "Application/JSON; charset=utf-8".to_owned(),
        ..json("{}")
    };
    assert!(object(&charset).is_ok());
}

#[test]
fn lease_format_l3_exact() {
    assert!(lease(&lease_text()));
    for refused in [
        lease_text().replacen("l3", "l2", 1),
        lease_text() + ".x",
        lease_text().replace(&"b".repeat(43), &"b".repeat(42)),
        lease_text().replace(&"a".repeat(11), &"a/".repeat(5)),
    ] {
        assert!(!lease(&refused), "{refused}");
    }
    assert!(binding(STATE) && !binding("short") && !binding(&"+".repeat(43)));
}

#[test]
fn returned_scopes_equal_requested() {
    let valid = strict_json::parse(grant_body(3600).as_bytes()).expect("json");
    assert!(tokens(&valid, scopes()).is_ok());
    let changes = [
        grant_body(3600).replacen('{', r#"{"extra":true,"#, 1),
        grant_body(3600).replace("3600", "true"),
        grant_body(29),
        grant_body(3600).replace(r#"["dns.read","offline_access","zone.read"]"#, "[]"),
        grant_body(3600).replace(
            r#"["dns.read","offline_access","zone.read"]"#,
            r#"["zone.read","dns.read","offline_access"]"#,
        ),
        grant_body(3600).replace(&lease_text(), "invalid"),
        grant_body(3600).replace(ACCESS, "short"),
    ];
    let refused = changes.iter().filter(|body| {
        let value = strict_json::parse(body.as_bytes()).expect("json");
        tokens(&value, scopes()).is_err()
    });
    assert_eq!(refused.count(), 7);
}
