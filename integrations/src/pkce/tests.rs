use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use sha2::{Digest, Sha256};
use team_core::entropy::{Entropy, EntropyUnavailable};
use team_core::testing::CountingEntropy;
use tokio::time::advance;

use super::{CallbackBinding, PkceError, PkceLimits, PkceStore, Request, ResourceBinding};

const SESSION_ONE: &str = "session-binding-one-123456789";
const SESSION_TWO: &str = "session-binding-two-123456789";

fn scopes() -> Vec<String> {
    ["dns.read", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec()
}

fn pkce(limits: PkceLimits) -> PkceStore {
    PkceStore::new(limits, Arc::new(CountingEntropy::default())).expect("valid limits")
}

fn request(scopes: &[String]) -> Request<'_> {
    Request {
        session_binding: SESSION_ONE,
        team_id: "team_1",
        assistant_id: "shimpz-cloudflare",
        integration_id: "cloudflare",
        provider_id: "cloudflare",
        scopes,
        resource_binding: None,
    }
}

#[tokio::test]
async fn pkce_state_verifier_lengths_s256() {
    let store = pkce(PkceLimits::default());
    let scopes = scopes();
    let challenge = store.create(&request(&scopes)).expect("created");
    assert_eq!(challenge.state.len(), 43);
    assert!(
        !format!("{challenge:?} {store:?}")
            .to_lowercase()
            .contains("verifier")
    );
    assert!(!format!("{store:?}").contains(SESSION_ONE));
    assert_eq!(
        store.claim_callback(&challenge.state, SESSION_TWO).err(),
        Some(PkceError::NotFound)
    );

    let exchange = store
        .claim_callback(&challenge.state, SESSION_ONE)
        .expect("claimed");
    assert_eq!(exchange.code_verifier.expose().len(), 86);
    assert_eq!(
        URL_SAFE_NO_PAD.encode(Sha256::digest(exchange.code_verifier.expose().as_bytes())),
        challenge.code_challenge
    );
    assert_eq!(exchange.provider_id, "cloudflare");
    assert_eq!(exchange.scopes, scopes);
    assert_eq!(
        (
            exchange.binding.team_id.as_str(),
            exchange.binding.assistant_id.as_str(),
            exchange.binding.integration_id.as_str()
        ),
        ("team_1", "shimpz-cloudflare", "cloudflare")
    );
    assert!(!format!("{exchange:?}").contains(exchange.code_verifier.expose()));
    assert_eq!(
        store.claim_callback(&challenge.state, SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
}

#[tokio::test]
async fn the_callback_recovers_the_private_binding_only_for_the_starting_browser() {
    let store = pkce(PkceLimits::default());
    let scopes = scopes();
    let resource: ResourceBinding = ("a".repeat(32), "shimpz-team-team_1".to_owned());
    let challenge = store
        .create(&Request {
            resource_binding: Some(&resource),
            ..request(&scopes)
        })
        .expect("created");
    assert_eq!(
        store.claim_callback(&challenge.state, SESSION_TWO).err(),
        Some(PkceError::NotFound)
    );
    assert_eq!(
        store.inspect_callback(&challenge.state, SESSION_TWO).err(),
        Some(PkceError::NotFound)
    );
    let expected = CallbackBinding {
        team_id: "team_1".to_owned(),
        assistant_id: "shimpz-cloudflare".to_owned(),
        integration_id: "cloudflare".to_owned(),
        resource_binding: Some(resource),
    };
    let inspected = store
        .inspect_callback(&challenge.state, SESSION_ONE)
        .expect("inspected");
    assert_eq!(inspected, expected);
    assert!(!format!("{inspected:?}").contains(SESSION_ONE));
    let exchange = store
        .claim_callback(&challenge.state, SESSION_ONE)
        .expect("claimed");
    assert_eq!(exchange.binding, expected);
    assert_eq!(
        store.claim_callback(&challenge.state, SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
}

#[tokio::test(start_paused = true)]
async fn expiry_and_a_binding_collision_fail_closed() {
    let store = pkce(PkceLimits {
        ttl: Duration::from_secs(30),
        ..PkceLimits::default()
    });
    let scopes = scopes();
    let challenge = store.create(&request(&scopes)).expect("created");
    assert_eq!(
        store.create(&request(&scopes)).err(),
        Some(PkceError::AlreadyPending)
    );
    advance(Duration::from_secs(30)).await;
    assert_eq!(
        store.claim_callback(&challenge.state, SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
}

#[tokio::test]
async fn global_session_and_team_caps_are_independent() {
    let scopes = scopes();
    let limited = |capacity, per_session, per_team| {
        pkce(PkceLimits {
            capacity,
            per_session,
            per_team,
            ttl: Duration::from_secs(600),
        })
    };
    let global = limited(1, 1, 1);
    global.create(&request(&scopes)).expect("created");
    let other = Request {
        session_binding: SESSION_TWO,
        team_id: "team_2",
        ..request(&scopes)
    };
    assert_eq!(global.create(&other).err(), Some(PkceError::Capacity));

    let session = limited(3, 1, 3);
    session.create(&request(&scopes)).expect("created");
    let same_session = Request {
        team_id: "team_2",
        ..request(&scopes)
    };
    assert_eq!(
        session.create(&same_session).err(),
        Some(PkceError::Capacity)
    );

    let team = limited(3, 3, 1);
    team.create(&request(&scopes)).expect("created");
    let same_team = Request {
        session_binding: SESSION_TWO,
        assistant_id: "other-assistant",
        ..request(&scopes)
    };
    assert_eq!(team.create(&same_team).err(), Some(PkceError::Capacity));
}

#[tokio::test]
async fn cancel_is_scoped_to_its_session_or_team() {
    let store = pkce(PkceLimits {
        capacity: 4,
        per_session: 4,
        per_team: 4,
        ttl: Duration::from_secs(600),
    });
    let scopes = scopes();
    let first = store.create(&request(&scopes)).expect("created");
    let second = store
        .create(&Request {
            session_binding: SESSION_TWO,
            team_id: "team_2",
            ..request(&scopes)
        })
        .expect("created");
    assert_eq!(store.cancel_session(SESSION_ONE), Ok(1));
    assert_eq!(
        store.claim_callback(&first.state, SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
    assert_eq!(store.cancel_team("team_2"), Ok(1));
    assert_eq!(store.cancel_all(), Ok(0));
    assert_eq!(
        store.claim_callback(&second.state, SESSION_TWO).err(),
        Some(PkceError::NotFound)
    );
}

#[tokio::test]
async fn invalid_inputs_never_create_state() {
    let store = pkce(PkceLimits::default());
    let scopes = scopes();
    let unreviewed = vec!["dm.read".to_owned()];
    let invalid = [
        Request {
            session_binding: "short",
            ..request(&scopes)
        },
        Request {
            team_id: "../team",
            ..request(&scopes)
        },
        Request {
            assistant_id: "Assistant",
            ..request(&scopes)
        },
        Request {
            integration_id: "x/evil",
            ..request(&scopes)
        },
        Request {
            provider_id: "evil",
            ..request(&scopes)
        },
        Request {
            scopes: &unreviewed,
            ..request(&scopes)
        },
    ];
    for request in invalid {
        assert_eq!(store.create(&request).err(), Some(PkceError::Invalid));
    }
    for binding in [
        (String::new(), "x".to_owned()),
        ("valid".to_owned(), "\ninvalid".to_owned()),
        ("valid".to_owned(), "x".repeat(257)),
    ] {
        let request = Request {
            resource_binding: Some(&binding),
            ..request(&scopes)
        };
        assert_eq!(store.create(&request).err(), Some(PkceError::Invalid));
    }
    assert_eq!(store.cancel_all(), Ok(0));
}

#[test]
fn session_state_and_limit_edges_fail_closed() {
    for session in ["\u{0}".repeat(16), "é".repeat(16), "x".repeat(2049)] {
        assert_eq!(
            super::session_digest(&session).err(),
            Some(PkceError::Invalid)
        );
    }
    let store = pkce(PkceLimits::default());
    assert_eq!(
        store.claim_callback("invalid", SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
    assert_eq!(
        store.cancel_session("short").err(),
        Some(PkceError::Invalid)
    );
    for limits in [
        PkceLimits {
            capacity: 0,
            ..PkceLimits::default()
        },
        PkceLimits {
            per_session: 0,
            ..PkceLimits::default()
        },
        PkceLimits {
            per_team: 0,
            ..PkceLimits::default()
        },
        PkceLimits {
            per_team: 129,
            ..PkceLimits::default()
        },
        PkceLimits {
            ttl: Duration::from_secs(29),
            ..PkceLimits::default()
        },
    ] {
        assert_eq!(
            PkceStore::new(limits, Arc::new(CountingEntropy::default())).err(),
            Some(PkceError::Invalid)
        );
    }
}

/// Fills with the queued bytes in order, then with a counter.
struct Scripted(std::sync::Mutex<Vec<u8>>, CountingEntropy);

impl Entropy for Scripted {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        match self.0.lock().expect("usable").pop() {
            Some(byte) => {
                bytes.fill(byte);
                Ok(())
            }
            None => self.1.fill(bytes),
        }
    }
}

#[tokio::test]
async fn a_colliding_state_is_drawn_again_and_inspection_of_another_session_refuses() {
    // States 0xaa, then 0xaa again (collision), then 0xbb; verifiers from the counter.
    let entropy = Scripted(
        std::sync::Mutex::new(vec![0xbb, 0xaa, 0x11, 0xaa]),
        CountingEntropy::default(),
    );
    let store = PkceStore::new(PkceLimits::default(), Arc::new(entropy)).expect("valid");
    let scopes = scopes();
    let existing = store.create(&request(&scopes)).expect("created");
    let created = store
        .create(&Request {
            session_binding: SESSION_TWO,
            team_id: "team_2",
            ..request(&scopes)
        })
        .expect("created");
    assert_ne!(created.state, existing.state);
    assert_eq!(created.state, URL_SAFE_NO_PAD.encode([0xbb; 32]));
    assert_eq!(
        store.inspect_callback(&created.state, SESSION_ONE).err(),
        Some(PkceError::NotFound)
    );
}
