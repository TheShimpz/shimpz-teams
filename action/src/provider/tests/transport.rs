use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use strict_json::Value;
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_integrations::https::testing::{Behaviour, Fake, Proxy, Recorded, fake, reply};

use super::{
    HOST, PROXY_TOKEN, Scope, TOKEN, answer, credential, items, json, phases, refused, scope,
};
use crate::frames::CallBroker;

const ANSWER: &str = r#"{"data":[{"id":"1"}]}"#;

async fn origin(behaviour: impl Fn(&Recorded) -> Behaviour + Send + Sync + 'static) -> Fake {
    fake(Proxy::Established, &[HOST], behaviour).await
}

fn served(scope: &mut Scope, fake: &Fake) {
    scope.tunnel = fake.tunnel.clone();
}

fn bearer() -> super::Credential {
    credential(
        "stored-input:t",
        Some("authorization"),
        None,
        &format!("Bearer {TOKEN}"),
        &[TOKEN],
    )
}

#[tokio::test]
async fn team_places_the_credential_and_the_action_receives_only_the_response() {
    let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", ANSWER))).await;
    let proof = credential(
        "stored-input:p",
        None,
        Some("appsecret_proof"),
        "abc123",
        &["test-secret", "abc123"],
    );
    let mut scope = scope(vec![bearer(), proof]);
    served(&mut scope, &fake);
    let audit = Arc::clone(&scope.audit);
    let body = STANDARD.encode(b"status=PAUSED");
    let frame = json(&format!(
        r#"{{"type":"fetch","method":"POST","url":"https://{HOST}/v1/items?limit=2","headers":[["content-type","application/x-www-form-urlencoded"]],"body":"{body}"}}"#
    ));
    let answered = answer(&mut scope.broker(), frame).await;
    assert_eq!(
        answered.get("status"),
        Some(&Value::Number(strict_json::Number::Int(200)))
    );
    let Some(Value::String(body)) = answered.get("body") else {
        panic!("a body")
    };
    assert_eq!(STANDARD.decode(body).expect("base64"), ANSWER.as_bytes());
    let received = fake.received.lock().expect("usable").remove(0);
    let head = received.head.to_ascii_lowercase();
    assert!(
        received
            .head
            .starts_with("POST /v1/items?limit=2&appsecret_proof=abc123 HTTP/1.1\r\n")
    );
    assert!(
        head.contains(&format!("authorization: bearer {TOKEN}\r\n"))
            && head.contains("accept-encoding: identity\r\n")
    );
    assert_eq!(received.body, b"status=PAUSED");
    let events = format!("{:?}", audit.events());
    assert!(!events.contains(TOKEN) && !events.contains("abc123"));
    assert_eq!(
        phases(&audit)
            .iter()
            .map(|(phase, _)| phase.as_str())
            .collect::<Vec<_>>(),
        ["dispatch", "outcome"]
    );
}

#[tokio::test]
async fn assistant_specific_proxy_capability() {
    let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", "{}"))).await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    answer(&mut scope.broker(), items()).await;
    let received = fake.received.lock().expect("usable");
    let basic = STANDARD.encode(format!("{PROXY_TOKEN}:"));
    assert!(
        received[0]
            .connect
            .starts_with(&format!("CONNECT {HOST}:443 HTTP/1.1\r\n"))
    );
    assert!(
        received[0]
            .connect
            .contains(&format!("Proxy-Authorization: Basic {basic}\r\n"))
    );
}

/// Every character as a JSON `\\uXXXX` escape.
fn escape(text: &str, upper: bool) -> String {
    use std::fmt::Write as _;
    text.chars().fold(String::new(), |mut escaped, character| {
        let code = u32::from(character);
        let _ = if upper {
            write!(escaped, "\\u{code:04X}")
        } else {
            write!(escaped, "\\u{code:04x}")
        };
        escaped
    })
}

#[tokio::test]
async fn response_echoing_any_protected_form_refused() {
    let escaped = escape(TOKEN, false);
    let mixed = escape(TOKEN, true);
    let bodies = [
        TOKEN.to_owned(),
        STANDARD.encode(TOKEN),
        format!(r#"{{"echo": "{TOKEN}"}}"#),
        format!(r#"{{"echo": "{escaped}"}}"#),
        format!(r#"{{"{escaped}": 1}}"#),
        format!(r#"{{"a":"safe","a":"{mixed}"}}"#),
        format!(r#"not json "{mixed}""#),
    ];
    for body in bodies {
        let fake =
            origin(move |_| Behaviour::Reply(reply("200 OK", "application/json", &body))).await;
        let mut scope = scope(vec![bearer()]);
        served(&mut scope, &fake);
        assert_eq!(
            answer(&mut scope.broker(), items()).await,
            refused("failed")
        );
    }
    let header =
        format!("HTTP/1.1 200 OK\r\nX-{TOKEN}: 1\r\nContent-Length: 2\r\n\r\n{{}}").into_bytes();
    let fake = origin(move |_| Behaviour::Reply(header.clone())).await;
    let mut scope = scope(vec![bearer()]);
    served(&mut scope, &fake);
    let audit = Arc::clone(&scope.audit);
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("failed")
    );
    assert_eq!(
        phases(&audit).last(),
        Some(&("outcome".to_owned(), Some("credential-echo".to_owned())))
    );
}

#[tokio::test]
async fn a_unicode_value_is_found_in_utf8_text() {
    let unicode = format!("tök€n-{TOKEN}");
    let body = format!("plain {unicode}");
    let fake = origin(move |_| Behaviour::Reply(reply("200 OK", "text/plain", &body))).await;
    let accented = credential("stored-input:u", None, Some("key"), &unicode, &[&unicode]);
    let mut scope = scope(vec![accented]);
    served(&mut scope, &fake);
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("failed")
    );
}

#[tokio::test]
async fn compressed_cut_short_and_oversized_responses_are_refused() {
    let compressed =
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\n\x1f\x8b".to_vec();
    let cut = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n{}".to_vec();
    let large = reply(
        "200 OK",
        "text/plain",
        &"a".repeat(super::super::MAX_RESPONSE_BYTES + 1),
    );
    for response in [compressed, cut, large] {
        let fake = origin(move |_| Behaviour::Reply(response.clone())).await;
        let mut scope = scope(Vec::new());
        served(&mut scope, &fake);
        assert_eq!(
            answer(&mut scope.broker(), items()).await,
            refused("failed")
        );
    }
}

#[tokio::test]
async fn a_proxy_refusal_is_unavailable_and_nothing_reaches_the_provider() {
    let fake = fake(
        Proxy::Reply(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"),
        &[HOST],
        |_| Behaviour::Stall,
    )
    .await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("unavailable")
    );
    let untrusted = fake_origin_with_untrusted_certificate().await;
    let mut scope = super::scope(Vec::new());
    scope.tunnel = untrusted;
    assert_eq!(
        answer(&mut scope.broker(), items()).await,
        refused("unavailable")
    );
}

/// A tunnel to a TLS origin whose certificate the tunnel does not trust.
async fn fake_origin_with_untrusted_certificate() -> team_integrations::https::Tunnel {
    let untrusted = origin(|_| Behaviour::Stall).await;
    let roots = rustls::RootCertStore::empty();
    team_integrations::https::Tunnel::new(
        "127.0.0.1".to_owned(),
        untrusted.port,
        Duration::from_secs(5),
        roots,
    )
}

#[tokio::test]
async fn reply_released_once_written_or_abandoned() {
    let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", "{}"))).await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    let capacity = scope.capacity.clone();
    let mut broker = scope.broker();
    answer(&mut broker, items()).await;
    assert_eq!(capacity.available(), super::super::MAX_CONCURRENT_CALLS - 1);
    broker.release();
    broker.release();
    assert_eq!(capacity.available(), super::super::MAX_CONCURRENT_CALLS);
}

#[tokio::test]
async fn outcome_audited_after_dispatch() {
    let fake = origin(move |_| Behaviour::Reply(reply("200 OK", "text/plain", TOKEN))).await;
    let mut scope = scope(vec![bearer()]);
    served(&mut scope, &fake);
    let (audit, capacity) = (Arc::clone(&scope.audit), scope.capacity.clone());
    let mut broker = scope.broker();
    assert_eq!(answer(&mut broker, items()).await, refused("failed"));
    let recorded: Vec<String> = phases(&audit).into_iter().map(|(phase, _)| phase).collect();
    assert_eq!(recorded, ["dispatch", "outcome"]);
    // A failed call keeps its capacity through its audit until its reply is released.
    assert_eq!(capacity.available(), super::super::MAX_CONCURRENT_CALLS - 1);
    broker.release();
    assert_eq!(capacity.available(), super::super::MAX_CONCURRENT_CALLS);
}

#[tokio::test]
async fn no_dispatch_after_stop() {
    let fake = origin(|_| {
        Behaviour::Late(
            Duration::from_secs(2),
            reply("200 OK", "application/json", "{}"),
        )
    })
    .await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    let stop = scope.stop.clone();
    let mut broker = scope.broker();
    let stopper = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        stop.stop();
    });
    let started = std::time::Instant::now();
    assert_eq!(answer(&mut broker, items()).await, refused("failed"));
    assert!(started.elapsed() < Duration::from_millis(1500));
    stopper.await.expect("joined");
    broker.release();
    let sent = fake.received.lock().expect("usable").len();
    assert_eq!(answer(&mut broker, items()).await, refused("refused"));
    assert_eq!(fake.received.lock().expect("usable").len(), sent);
}

/// Records events and sets the Stop while the dispatch record is written.
struct StoppingAudit(Stop, team_core::testing::MemoryAudit);

impl AuditSink for StoppingAudit {
    fn record(&self, event: AuditEvent) -> BoxFuture<'_, Result<(), AuditUnavailable>> {
        if event
            .fields()
            .iter()
            .any(|(name, value)| name == "phase" && *value == Value::String("dispatch".to_owned()))
        {
            self.0.stop();
        }
        self.1.record(event)
    }
}

#[tokio::test]
async fn a_stop_racing_the_dispatch_record_sends_nothing() {
    let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", "{}"))).await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    let audit = Arc::new(StoppingAudit(
        scope.stop.clone(),
        team_core::testing::MemoryAudit::default(),
    ));
    let mut broker = scope.broker_with(audit.clone());
    assert_eq!(answer(&mut broker, items()).await, refused("refused"));
    assert_eq!(fake.received.lock().expect("usable").len(), 0);
    let recorded = phases(&audit.1);
    assert_eq!(
        recorded,
        [
            ("dispatch".to_owned(), None),
            ("outcome".to_owned(), Some("stopped".to_owned()))
        ]
    );
}

#[tokio::test]
async fn no_dispatch_when_audit_fails() {
    let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", "{}"))).await;
    let mut scope = scope(Vec::new());
    served(&mut scope, &fake);
    scope.audit.fail(true);
    let mut broker = scope.broker();
    let answered = broker
        .answer(
            items(),
            tokio::time::Instant::now() + Duration::from_secs(5),
        )
        .await;
    assert_eq!(answered, Err(crate::frames::FrameError::Transport));
    assert_eq!(fake.received.lock().expect("usable").len(), 0);
}

#[tokio::test]
async fn team_wide_capacity_4_observes_stop_and_deadline() {
    let capacity = super::super::Capacity::default();
    let mut holders = Vec::new();
    for _ in 0..super::super::MAX_CONCURRENT_CALLS {
        let fake = origin(|_| Behaviour::Reply(reply("200 OK", "application/json", "{}"))).await;
        let mut held = scope(Vec::new());
        served(&mut held, &fake);
        held.capacity = capacity.clone();
        let mut broker = held.broker();
        answer(&mut broker, items()).await;
        holders.push((broker, fake));
    }
    assert_eq!(capacity.available(), 0);
    let mut waiting = scope(Vec::new());
    waiting.capacity = capacity.clone();
    let audit = Arc::clone(&waiting.audit);
    let mut broker = waiting.broker();
    let deadline = tokio::time::Instant::now() + Duration::from_millis(100);
    let reply = broker.answer(items(), deadline).await.expect("answered");
    assert_eq!(
        strict_json::parse(&reply).expect("json"),
        refused("unavailable")
    );
    assert_eq!(
        phases(&audit),
        [("refused".to_owned(), Some("deadline".to_owned()))]
    );
    let mut stopped = scope(Vec::new());
    stopped.capacity = capacity.clone();
    stopped.stop.stop();
    assert_eq!(
        answer(&mut stopped.broker(), items()).await,
        refused("refused")
    );
    for (broker, _) in &mut holders {
        broker.release();
    }
    assert_eq!(capacity.available(), super::super::MAX_CONCURRENT_CALLS);
}
