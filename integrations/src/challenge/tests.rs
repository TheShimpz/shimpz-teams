use std::sync::Arc;
use std::time::Duration;

use team_core::entropy::{Entropy, EntropyUnavailable};
use team_core::testing::CountingEntropy;
use tokio::time::{Instant, advance};

use super::{ChallengeError, ChallengeStore, Claim, Limits};
use crate::pending::{self, IntegrationRequirement, RequiredIntegration};

fn requirement() -> IntegrationRequirement {
    IntegrationRequirement {
        assistant_id: "shimpz-cloudflare".to_owned(),
        assistant_name: "Shimpz Cloudflare".to_owned(),
        action_ids: vec!["list-zones".to_owned()],
        integrations: vec![RequiredIntegration {
            id: "cloudflare".to_owned(),
            provider: "cloudflare".to_owned(),
            scopes: vec!["dns.read".to_owned(), "zone.read".to_owned()],
        }],
    }
}

fn entropy() -> Arc<dyn Entropy> {
    Arc::new(CountingEntropy::default())
}

fn store(capacity: usize, ttl: u64) -> pending::IntegrationChallengeStore<&'static str> {
    pending::store(capacity, Duration::from_secs(ttl), entropy()).expect("valid limits")
}

/// Answers the queued byte patterns in order, then counts.
struct Scripted(std::sync::Mutex<Vec<u8>>);

impl Entropy for Scripted {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        let next = self.0.lock().expect("usable").pop().unwrap_or(0xcc);
        bytes.fill(next);
        Ok(())
    }
}

#[tokio::test(start_paused = true)]
async fn the_default_lifetime_encloses_the_brokered_oauth_transaction() {
    let store = pending::default_store::<()>(entropy()).expect("defaults");
    let before = Instant::now();
    let challenge = store
        .create("team_1", vec![requirement()], ())
        .expect("created");
    assert_eq!(challenge.expires_at - before, Duration::from_secs(600));
}

#[tokio::test(start_paused = true)]
async fn a_challenge_is_team_bound_single_use_and_keeps_its_payload() {
    let store = store(32, 600);
    let challenge = store
        .create("team_1", vec![requirement()], "private user input")
        .expect("created");
    assert!(!format!("{store:?}").contains("private user input"));
    assert_eq!(
        store.get("team_2", &challenge.id).err(),
        Some(ChallengeError::NotFound)
    );
    let claimed = store.claim("team_1", &challenge.id).expect("claimed");
    assert!(Arc::ptr_eq(&claimed, &challenge));
    assert_eq!(claimed.payload, "private user input");
    assert_eq!(
        store.claim("team_1", &challenge.id).err(),
        Some(ChallengeError::NotFound)
    );
}

#[tokio::test(start_paused = true)]
async fn one_pending_turn_per_team_and_the_capacity_fail_closed() {
    let store = store(2, 600);
    store
        .create("team_1", vec![requirement()], "a")
        .expect("created");
    assert_eq!(
        store.create("team_1", vec![requirement()], "b").err(),
        Some(ChallengeError::AlreadyPending)
    );
    store
        .create("team_2", vec![requirement()], "c")
        .expect("created");
    assert_eq!(
        store.create("team_3", vec![requirement()], "d").err(),
        Some(ChallengeError::Capacity)
    );
}

#[tokio::test(start_paused = true)]
async fn expiry_cancel_and_invalid_identifiers_remove_no_other_team() {
    let store = store(32, 30);
    let expired = store
        .create("team_1", vec![requirement()], "a")
        .expect("created");
    advance(Duration::from_secs(30)).await;
    assert_eq!(
        store.get("team_1", &expired.id).err(),
        Some(ChallengeError::NotFound)
    );

    let active = store
        .create("team_2", vec![requirement()], "b")
        .expect("created");
    assert_eq!(
        store.get("../team", &active.id).err(),
        Some(ChallengeError::Invalid)
    );
    assert_eq!(
        store.get("team_2", "not-a-challenge").err(),
        Some(ChallengeError::NotFound)
    );
    assert_eq!(store.cancel_team("team_2"), Ok(true));
    assert_eq!(store.cancel_team("team_2"), Ok(false));
    assert_eq!(store.cancel_all(), Ok(0));
}

#[tokio::test(start_paused = true)]
async fn withdrawal_returns_exactly_the_cancelled_challenge_once() {
    let store = store(32, 600);
    let challenge = store
        .create("team_1", vec![requirement()], "one")
        .expect("created");
    let other = store
        .create("team_2", vec![requirement()], "two")
        .expect("created");
    let withdrawn = store.withdraw_team("team_1").expect("valid").expect("held");
    assert!(Arc::ptr_eq(&withdrawn, &challenge));
    assert!(store.withdraw_team("team_1").expect("valid").is_none());
    assert_eq!(
        store.claim("team_1", &challenge.id).err(),
        Some(ChallengeError::NotFound)
    );
    // A claimed challenge already belongs to its responder, so a later withdrawal finds nothing to end.
    assert!(Arc::ptr_eq(
        &store.claim("team_2", &other.id).expect("claimed"),
        &other
    ));
    assert!(store.withdraw_team("team_2").expect("valid").is_none());
}

#[test]
fn empty_requirements_and_limits_out_of_range_are_refused() {
    let store = pending::default_store::<()>(entropy()).expect("defaults");
    assert_eq!(
        store.create("team_1", Vec::new(), ()).err(),
        Some(ChallengeError::Invalid)
    );
    for (capacity, ttl) in [(0, 600), (1025, 600), (1, 29), (1, 901)] {
        assert_eq!(
            pending::store::<()>(capacity, Duration::from_secs(ttl), entropy()).err(),
            Some(ChallengeError::Invalid),
            "{capacity} {ttl}"
        );
    }
    let fractional = Limits {
        capacity: 1,
        ttl: Duration::from_millis(30_500),
        retain_expired: false,
    };
    assert!(!fractional.valid());
}

#[tokio::test(start_paused = true)]
async fn an_authenticated_restore_keeps_its_id_payload_and_remaining_lifetime() {
    let store = store(32, 30);
    let id = "a".repeat(32);
    let restored = store
        .restore(
            "team_1",
            &id,
            Duration::from_secs(7),
            vec![requirement()],
            "private",
        )
        .expect("restored");
    assert_eq!(
        (restored.id.as_str(), restored.payload),
        (id.as_str(), "private")
    );
    advance(Duration::from_secs(7)).await;
    assert!(store.current("team_1").expect("valid").is_none());
}

fn restore(
    store: &pending::IntegrationChallengeStore<&'static str>,
    team: &str,
    id: &str,
    seconds: u64,
    metadata: Vec<IntegrationRequirement>,
) -> Option<ChallengeError> {
    store
        .restore(team, id, Duration::from_secs(seconds), metadata, "r")
        .err()
}

#[tokio::test(start_paused = true)]
async fn a_drawn_id_that_collides_with_a_restored_one_is_drawn_again() {
    let scripted = Arc::new(Scripted(std::sync::Mutex::new(vec![0xbb, 0xaa])));
    let store = pending::store::<&str>(2, Duration::from_secs(30), scripted).expect("valid");
    let first = store
        .create("team_9", vec![requirement()], "x")
        .expect("created");
    assert_eq!(first.id, "aa".repeat(16));
    let restored = store
        .restore(
            "team_8",
            &"bb".repeat(16),
            Duration::from_secs(5),
            vec![requirement()],
            "y",
        )
        .expect("restored");
    store.cancel_team("team_9").expect("valid");
    let created = store
        .create("team_1", vec![requirement()], "z")
        .expect("created");
    assert_eq!(created.id, "cc".repeat(16));
    assert_ne!(created.id, restored.id);
}

#[tokio::test(start_paused = true)]
async fn restore_collisions_capacity_and_lifetime_bounds_fail_closed() {
    let store = store(2, 30);
    store
        .create("team_1", vec![requirement()], "z")
        .expect("created");
    store
        .create("team_3", vec![requirement()], "z")
        .expect("created");
    let id = "c".repeat(32);
    for (seconds, metadata) in [
        (0, vec![requirement()]),
        (31, vec![requirement()]),
        (1, Vec::new()),
    ] {
        assert_eq!(
            restore(&store, "team_2", &id, seconds, metadata),
            Some(ChallengeError::Invalid)
        );
    }
    assert_eq!(
        restore(&store, "team_1", &id, 1, vec![requirement()]),
        Some(ChallengeError::AlreadyPending)
    );
    assert_eq!(
        restore(&store, "team_2", &id, 1, vec![requirement()]),
        Some(ChallengeError::Capacity)
    );
    assert_eq!(
        restore(&store, "team_2", "short", 1, vec![requirement()]),
        Some(ChallengeError::NotFound)
    );
}

#[tokio::test(start_paused = true)]
async fn a_failed_commit_leaves_the_challenge_live_and_a_successful_one_consumes_it() {
    let store = store(32, 600);
    let challenge = store
        .create("team_1", vec![requirement()], "p")
        .expect("created");
    assert_eq!(
        store
            .claim_after("team_1", &challenge.id, |_| Err("journal unavailable"))
            .err(),
        Some(Claim::Commit("journal unavailable"))
    );
    assert!(store.get("team_1", &challenge.id).is_ok());
    assert!(
        store
            .claim_after("team_1", &challenge.id, |_| Ok::<(), ()>(()))
            .is_ok()
    );
    assert_eq!(
        store
            .claim_after("team_1", &challenge.id, |_| Ok::<(), ()>(()))
            .err(),
        Some(Claim::Store(ChallengeError::NotFound))
    );
}

#[tokio::test(start_paused = true)]
async fn reissue_replaces_the_id_and_metadata_but_keeps_the_payload_and_expiry() {
    let store = store(32, 300);
    let pending = store
        .create("team_1", vec![requirement()], "continuation")
        .expect("created");
    advance(Duration::from_secs(60)).await;
    let mut changed = requirement();
    changed.assistant_name = "Renamed".to_owned();
    let fresh = store
        .reissue("team_1", &pending.id, vec![changed.clone()])
        .expect("reissued");
    assert_ne!(fresh.id, pending.id);
    assert_eq!(
        (
            fresh.team_id.as_str(),
            fresh.expires_at,
            &fresh.metadata,
            fresh.payload
        ),
        (
            "team_1",
            pending.expires_at,
            &vec![changed.clone()],
            "continuation"
        )
    );
    assert!(Arc::ptr_eq(
        &store.current("team_1").expect("valid").expect("held"),
        &fresh
    ));
    assert_eq!(
        store.get("team_1", &pending.id).err(),
        Some(ChallengeError::NotFound)
    );
    assert_eq!(
        store
            .reissue("team_2", &fresh.id, vec![changed.clone()])
            .err(),
        Some(ChallengeError::NotFound)
    );
    assert_eq!(
        store.reissue("team_1", &fresh.id, Vec::new()).err(),
        Some(ChallengeError::Invalid)
    );
    advance(Duration::from_secs(240)).await;
    assert_eq!(
        store.reissue("team_1", &fresh.id, vec![changed]).err(),
        Some(ChallengeError::NotFound)
    );
}

#[tokio::test(start_paused = true)]
async fn an_expired_payload_is_drained_once_per_team_for_dependent_cleanup() {
    let store = ChallengeStore::<Vec<IntegrationRequirement>, &str>::new(
        Limits {
            capacity: 32,
            ttl: Duration::from_secs(30),
            retain_expired: true,
        },
        |requirements| !requirements.is_empty(),
        entropy(),
    )
    .expect("valid");
    let one = store
        .create("team_1", vec![requirement()], "one")
        .expect("created");
    let two = store
        .create("team_2", vec![requirement()], "two")
        .expect("created");
    advance(Duration::from_secs(30)).await;
    let drained = store.drain_expired(Some("team_1")).expect("retained");
    assert!(drained.len() == 1 && Arc::ptr_eq(&drained[0], &one));
    let rest = store.drain_expired(None).expect("retained");
    assert!(rest.len() == 1 && Arc::ptr_eq(&rest[0], &two));
    assert!(store.drain_expired(None).expect("retained").is_empty());
    assert!(store.current("team_1").expect("valid").is_none());
}

#[tokio::test(start_paused = true)]
async fn abandoned_continuations_expire_without_retaining_their_payloads() {
    let store = pending::store::<String>(1, Duration::from_secs(30), entropy()).expect("valid");
    for attempt in 0..100 {
        store
            .create(
                "team_1",
                vec![requirement()],
                format!("private input {attempt}"),
            )
            .expect("created");
        advance(Duration::from_secs(31)).await;
    }
    assert!(store.current("team_1").expect("valid").is_none());
    assert_eq!(store.cancel_team("team_1"), Ok(false));
    assert_eq!(
        store.drain_expired(None).err(),
        Some(ChallengeError::NotRetained)
    );
    let inner = store.inner.lock().expect("usable");
    assert!(inner.pending.is_empty() && inner.expired.is_empty() && inner.by_team.is_empty());
}

#[tokio::test(start_paused = true)]
async fn claim_and_expiry_keep_a_foreign_reverse_index_untouched() {
    let store = store(32, 30);
    let challenge = store
        .create("team_1", vec![requirement()], "a")
        .expect("created");
    store
        .inner
        .lock()
        .expect("usable")
        .by_team
        .insert("team_1".to_owned(), "foreign".to_owned());
    assert!(Arc::ptr_eq(
        &store.claim("team_1", &challenge.id).expect("claimed"),
        &challenge
    ));
    assert_eq!(
        store.inner.lock().expect("usable").by_team["team_1"],
        "foreign"
    );

    let _second = store
        .create("team_2", vec![requirement()], "b")
        .expect("created");
    store
        .inner
        .lock()
        .expect("usable")
        .by_team
        .insert("team_2".to_owned(), "foreign".to_owned());
    advance(Duration::from_secs(30)).await;
    assert!(store.current("team_2").expect("valid").is_none());
    assert_eq!(
        store.inner.lock().expect("usable").by_team["team_2"],
        "foreign"
    );
}
