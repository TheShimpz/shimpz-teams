use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;

use super::super::{BrokerError, BrokerTransport, FixedTransport};
use crate::capability::{CapabilitySource, GroupSource};
use crate::https::testing::{Behaviour, Fake, Proxy, fake, reply};

const DEADLINE: Duration = Duration::from_millis(300);
const CLAIM: &str = "https://shimpz.com/api/oauth/cloudflare/claim";

struct Broker {
    directory: tempfile::TempDir,
    fake: Fake,
    transport: FixedTransport,
}

fn capability(directory: &std::path::Path) -> CapabilitySource {
    let path = directory.join("token");
    fs::write(&path, "a".repeat(64)).expect("written");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o440)).expect("mode");
    let metadata = fs::metadata(directory).expect("stat");
    CapabilitySource {
        path,
        owner: metadata.uid(),
        group: GroupSource::Id(metadata.gid()),
    }
}

async fn broker(proxy: Proxy, behaviour: Behaviour) -> Broker {
    let directory = tempfile::tempdir().expect("temporary directory");
    let fake = fake(proxy, &["shimpz.com"], move |_| behaviour.clone()).await;
    let transport =
        FixedTransport::new(fake.tunnel.clone(), capability(directory.path()), DEADLINE);
    Broker {
        directory,
        fake,
        transport,
    }
}

async fn claim(broker: &Broker) -> Result<super::super::BrokerResponse, BrokerError> {
    broker
        .transport
        .post(
            CLAIM,
            &[("Content-Type".to_owned(), "application/json".to_owned())],
            b"{}".to_vec(),
        )
        .await
}

#[tokio::test]
async fn broker_via_account_egress_with_capability() {
    let broker = broker(
        Proxy::Established,
        Behaviour::Reply(reply("200 OK", "application/json", "{}")),
    )
    .await;
    let response = claim(&broker).await.expect("answered");
    assert_eq!(
        (
            response.status,
            response.content_type.as_str(),
            response.body.as_slice()
        ),
        (200, "application/json", b"{}".as_slice())
    );
    let received = broker.fake.received.lock().expect("usable");
    let basic = STANDARD.encode(format!("{}:", "a".repeat(64)));
    assert!(
        received[0]
            .connect
            .starts_with("CONNECT shimpz.com:443 HTTP/1.1\r\n")
    );
    assert!(
        received[0]
            .connect
            .contains(&format!("Proxy-Authorization: Basic {basic}\r\n"))
    );
    assert!(
        received[0]
            .head
            .starts_with("POST /api/oauth/cloudflare/claim HTTP/1.1\r\n")
    );
    assert!(
        received[0]
            .head
            .to_ascii_lowercase()
            .contains("host: shimpz.com\r\n")
    );
    assert_eq!(received[0].body, b"{}");
}

#[tokio::test]
async fn only_the_three_reviewed_operations_are_reachable() {
    let broker = broker(
        Proxy::Established,
        Behaviour::Reply(reply("200 OK", "application/json", "{}")),
    )
    .await;
    for url in [
        "http://shimpz.com/api/oauth/cloudflare/claim",
        "https://evil.example/api/oauth/cloudflare/claim",
        "https://shimpz.com/api/oauth/cloudflare/start",
        "https://shimpz.com:443/api/oauth/cloudflare/claim",
        "https://user@shimpz.com/api/oauth/cloudflare/claim",
        "https://shimpz.com/api/oauth/cloudflare/claim?x=1",
    ] {
        assert_eq!(
            broker.transport.post(url, &[], Vec::new()).await.err(),
            Some(BrokerError::Endpoint),
            "{url}"
        );
    }
    assert!(broker.fake.received.lock().expect("usable").is_empty());
}

#[tokio::test]
async fn an_unavailable_capability_sends_nothing() {
    let broker = broker(
        Proxy::Established,
        Behaviour::Reply(reply("200 OK", "application/json", "{}")),
    )
    .await;
    let foreign = CapabilitySource {
        path: broker.directory.path().join("token"),
        owner: u32::MAX,
        group: GroupSource::Id(0),
    };
    let transport = FixedTransport::new(broker.fake.tunnel.clone(), foreign, DEADLINE);
    assert_eq!(
        transport.post(CLAIM, &[], Vec::new()).await.err(),
        Some(BrokerError::Capability)
    );
    assert!(broker.fake.received.lock().expect("usable").is_empty());
}

#[tokio::test]
async fn an_oversized_answer_or_a_refused_tunnel_is_unavailable() {
    let oversized = reply(
        "200 OK",
        "application/json",
        &"x".repeat(super::super::MAX_RESPONSE_BYTES + 1),
    );
    let large = broker(Proxy::Established, Behaviour::Reply(oversized)).await;
    assert_eq!(claim(&large).await.err(), Some(BrokerError::Unavailable));
    let refused = broker(
        Proxy::Reply(b"HTTP/1.1 403 Forbidden\r\n\r\n"),
        Behaviour::Stall,
    )
    .await;
    assert_eq!(claim(&refused).await.err(), Some(BrokerError::Unavailable));
    let garbled = broker(
        Proxy::Reply(b"HTTP/1.1 200 Connection established\r\n\r\nextra"),
        Behaviour::Stall,
    )
    .await;
    assert_eq!(claim(&garbled).await.err(), Some(BrokerError::Unavailable));
}

async fn ends_by_the_deadline(proxy: Proxy, behaviour: Behaviour) {
    let broker = broker(proxy, behaviour).await;
    let started = Instant::now();
    assert_eq!(claim(&broker).await.err(), Some(BrokerError::Unavailable));
    assert!(started.elapsed() < super::super::HTTP_TIMEOUT / 2);
}

#[tokio::test]
async fn a_stalled_proxy_tunnel_ends_at_the_deadline() {
    ends_by_the_deadline(Proxy::Stall, Behaviour::Stall).await;
}

#[tokio::test]
async fn trickled_broker_headers_end_at_the_deadline() {
    ends_by_the_deadline(
        Proxy::Established,
        Behaviour::Trickle(b"HTTP/1.1 200 OK\r\nX-Slow: ".to_vec()),
    )
    .await;
}

#[tokio::test]
async fn a_trickled_broker_body_is_refused_not_truncated() {
    let prefix =
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{".to_vec();
    ends_by_the_deadline(Proxy::Established, Behaviour::Trickle(prefix)).await;
}

#[tokio::test]
async fn an_overdue_response_is_refused() {
    let late = Behaviour::Late(DEADLINE * 2, reply("200 OK", "application/json", "{}"));
    ends_by_the_deadline(Proxy::Established, late).await;
}
