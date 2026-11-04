use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::testing::FakeClock;

use super::record::Deleting;
use super::{Checkout, Expired, IDLE_SECONDS, Kind, Pending, SignInError, SignInStore};

const TEAM: &str = "neo";
const EVENT: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";

fn incarnation() -> String {
    "n".repeat(64)
}

fn deleting() -> Deleting {
    Deleting {
        event_id: EVENT.to_owned(),
        initiator_id: "s".repeat(32),
        factor: "totp".to_owned(),
        origin: "https://admin.example".to_owned(),
    }
}

/// A temporary directory removed with the case.
pub(crate) struct Scratch(pub(crate) PathBuf);

impl Scratch {
    pub(crate) fn new() -> Self {
        let mut random = [0_u8; 8];
        team_core::entropy::Entropy::fill(&OsEntropy, &mut random).expect("random");
        let name = u64::from_be_bytes(random);
        let path = std::env::temp_dir().join(format!("w7-sign-ins-{name:016x}"));
        std::fs::create_dir_all(&path).expect("created");
        Self(path)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct Case {
    scratch: Scratch,
    clock: Arc<FakeClock>,
    store: Arc<SignInStore>,
}

impl Case {
    fn new() -> Self {
        let scratch = Scratch::new();
        let clock = Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_hours(500_000)));
        let store = Arc::new(Self::open(&scratch.0, &clock));
        Self {
            scratch,
            clock,
            store,
        }
    }

    fn open(root: &Path, clock: &Arc<FakeClock>) -> SignInStore {
        SignInStore::new(
            &root.join("state"),
            &root.join("key").join("aes256.key"),
            clock.clone(),
            Arc::new(OsEntropy),
        )
        .expect("a store")
    }

    fn current(&self, team: &str) -> Option<Value> {
        self.store.current(team).expect("readable")
    }

    fn checkout(&self, team: &str, kind: Kind) -> Result<Checkout, SignInError> {
        self.store.checkout(team, &incarnation(), kind)
    }

    /// Checks the Team's Sign-in out and Saves its next generation.
    fn save(&self, team: &str) -> Result<Value, SignInError> {
        let checkout = self.checkout(team, Kind::Person)?;
        if self.store.profile(&checkout)?.is_none() {
            self.store.attach(&checkout, "profile-1")?;
        }
        let fence = self.store.save_fence(team);
        self.store.commit(&checkout, fence, || true)
    }

    fn saved(&self) -> Value {
        self.save(TEAM).expect("saved")
    }

    fn teams(&self) -> Vec<String> {
        self.store
            .state()
            .expect("readable")
            .into_iter()
            .map(|(team, _)| team)
            .collect()
    }
}

fn get<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).expect("a field")
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(strict_json::Number::Int(value))
}

#[test]
fn a_saved_sign_in_shows_its_state_and_times() {
    let case = Case::new();
    assert_eq!(case.current(TEAM), None);
    let saved = case.saved();
    let Value::Object(members) = &saved else {
        panic!("an object")
    };
    let mut keys: Vec<&str> = members.iter().map(|(key, _)| key.as_str()).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "created_at",
            "expires_at",
            "generation",
            "last_used_at",
            "saved_at",
            "sign_in_id",
            "state",
            "team_incarnation"
        ]
    );
    assert_eq!(get(&saved, "state"), &text("ready"));
    assert_eq!(get(&saved, "generation"), &int(1));
    assert_eq!(get(&saved, "expires_at"), &text("2027-01-22T08:00:00Z"));
    assert_eq!(case.current(TEAM), Some(saved));
    assert_eq!(case.current("other"), None);
}

#[test]
fn a_save_writes_the_next_generation_on_the_same_profile() {
    let case = Case::new();
    let first = case.saved();
    let second = case.save(TEAM).expect("saved");
    assert_eq!(get(&second, "sign_in_id"), get(&first, "sign_in_id"));
    assert_eq!(get(&second, "generation"), &int(2));
    assert_eq!(
        case.store.named_profiles(),
        Ok(vec!["profile-1".to_owned()])
    );
}

#[test]
fn the_sign_in_has_one_checkout_at_a_time_and_a_stale_end_changes_nothing() {
    let case = Case::new();
    case.saved();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(
        case.checkout(TEAM, Kind::Person),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
    let fence = case.store.save_fence(TEAM);
    case.store.finish(&checkout).expect("finished");
    // An end without a Save leaves the restored generation needing a person; a Save of that checkout is refused.
    let current = case.current(TEAM).expect("a Sign-in");
    assert_eq!(get(&current, "state"), &text("needs-person"));
    assert_eq!(get(&current, "generation"), &int(1));
    assert!(case.store.commit(&checkout, fence, || true).is_err());
    case.store
        .finish(&checkout)
        .expect("a stale end changes nothing");
    assert_eq!(
        case.checkout(TEAM, Kind::Agent),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
    // A person reconciles it: their Save makes it ready again.
    assert_eq!(get(&case.saved(), "state"), &text("ready"));
    assert!(case.store.profile(&checkout).is_err());
}

#[test]
fn a_team_s_first_checkout_that_ends_without_a_save_leaves_no_sign_in() {
    let case = Case::new();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(checkout.generation, 0);
    assert_eq!(case.store.profile(&checkout), Ok(None));
    assert_eq!(case.current(TEAM), None);
    case.store.finish(&checkout).expect("finished");
    assert_eq!(case.teams(), Vec::<String>::new());
}

#[test]
fn a_sign_in_expires_after_a_month_from_its_save_or_a_week_unused_and_is_told() {
    let case = Case::new();
    let week = Duration::from_secs_f64(IDLE_SECONDS);
    case.saved();
    case.clock.advance(week);
    assert_eq!(case.current(TEAM), None);
    let told = Expired {
        team_id: TEAM.to_owned(),
        generation: 1,
        incarnation: incarnation(),
    };
    assert_eq!(case.store.take_expired(), vec![told.clone()]);
    case.saved();
    for _ in 0..4 {
        case.clock
            .advance(week.saturating_sub(Duration::from_mins(1)));
        let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
        case.store.finish(&checkout).expect("finished");
    }
    case.clock.advance(Duration::from_hours(4 * 24));
    assert_eq!(case.current(TEAM), None);
    // A new checkout of an expired Sign-in deletes it first, and tells.
    case.saved();
    case.clock.advance(week);
    let _ = case.store.take_expired();
    assert_eq!(
        case.checkout(TEAM, Kind::Person)
            .expect("checked out")
            .generation,
        0
    );
    assert_eq!(case.store.take_expired(), [told]);
}

fn confirmed(saved: &Value) -> (String, i64) {
    let Value::String(sign_in_id) = get(saved, "sign_in_id") else {
        panic!("an id")
    };
    (sign_in_id.clone(), 1)
}

#[test]
fn deletion_needs_the_exact_confirmed_sign_in_and_fences_a_save_under_way() {
    let case = Case::new();
    let saved = case.saved();
    let (sign_in_id, generation) = confirmed(&saved);
    let other = "m".repeat(64);
    for expected in [
        (sign_in_id.as_str(), 2, incarnation()),
        ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 1, incarnation()),
        (sign_in_id.as_str(), 1, other),
    ] {
        assert_eq!(
            case.store
                .begin_delete(TEAM, (expected.0, expected.1, &expected.2), deleting()),
            Err(SignInError::refused(team_protocol::sign_in::CHANGED))
        );
    }
    let inc = incarnation();
    let exact = (sign_in_id.as_str(), generation, inc.as_str());
    let unnamed = Deleting {
        event_id: String::new(),
        ..deleting()
    };
    let long = Deleting {
        factor: "x".repeat(256),
        ..deleting()
    };
    for metadata in [unnamed, long] {
        assert!(case.store.begin_delete(TEAM, exact, metadata).is_err());
    }
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let fence = case.store.save_fence(TEAM);
    case.store
        .begin_delete(TEAM, exact, deleting())
        .expect("fenced");
    assert!(case.store.commit(&checkout, fence, || true).is_err());
    assert_eq!(case.current(TEAM), None);
    assert_eq!(
        case.store.fenced_profile(TEAM, EVENT),
        Ok(Some("profile-1".to_owned()))
    );
}

#[test]
fn a_fenced_record_keeps_its_metadata_and_every_checkout_out_until_settled() {
    let case = Case::new();
    let (sign_in_id, generation) = confirmed(&case.saved());
    let inc = incarnation();
    let exact = (sign_in_id.as_str(), generation, inc.as_str());
    case.store
        .begin_delete(TEAM, exact, deleting())
        .expect("fenced");
    // Until its completion is recorded, the fenced record keeps the deletion's metadata and every checkout out.
    case.save("trinity").expect("saved");
    assert_eq!(
        case.store.deletions(),
        Ok(vec![Pending {
            team_id: TEAM.to_owned(),
            deleting: deleting(),
            generation: 1,
            incarnation: incarnation(),
        }])
    );
    assert!(case.checkout(TEAM, Kind::Person).is_err());
    assert!(case.store.begin_delete(TEAM, exact, deleting()).is_err());
    assert_eq!(case.store.settle(TEAM, EVENT), Ok(true));
    assert_eq!(case.teams(), ["trinity"]);
    assert_eq!(case.store.deletions(), Ok(Vec::new()));
    assert_eq!(
        case.store.begin_delete(TEAM, exact, deleting()),
        Err(SignInError::refused(team_protocol::sign_in::ABSENT))
    );
    // Another deletion's purge or settle, or one already settled, leaves a Sign-in started afterwards as it is.
    after_settled(&case);
}

fn after_settled(case: &Case) {
    case.saved();
    assert_eq!(case.store.fenced_profile(TEAM, EVENT), Ok(None));
    assert_eq!(case.store.settle(TEAM, EVENT), Ok(true));
    assert_eq!(
        get(&case.current(TEAM).expect("kept"), "generation"),
        &int(1)
    );
}

#[test]
fn a_starting_team_ends_open_checkouts_and_deletes_what_was_left() {
    let case = Case::new();
    case.saved();
    case.checkout(TEAM, Kind::Person).expect("checked out");
    case.store.reconcile().expect("reconciled");
    assert_eq!(
        get(&case.current(TEAM).expect("kept"), "state"),
        &text("needs-person")
    );
    assert_eq!(case.teams(), [TEAM]);
    assert_eq!(case.store.delete_team(TEAM), Ok(true));
    case.store.reconcile().expect("reconciled");
    assert_eq!(case.teams(), Vec::<String>::new());
    let (sign_in_id, generation) = confirmed(&case.saved());
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    case.store.reconcile().expect("reconciled");
    assert_eq!(case.store.deletions().expect("readable").len(), 1);
    case.store.delete_all().expect("reset");
    case.store.reconcile().expect("reconciled");
}

#[test]
fn a_sign_in_of_an_earlier_team_incarnation_is_gone_before_a_new_checkout() {
    let case = Case::new();
    case.saved();
    let checkout = case
        .store
        .checkout(TEAM, &"m".repeat(64), Kind::Person)
        .expect("checked out");
    assert_eq!(checkout.generation, 0);
}

#[test]
fn a_team_deletion_and_a_reset_take_everything_and_fence_saves() {
    let case = Case::new();
    case.saved();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let fence = case.store.save_fence(TEAM);
    assert_eq!(case.store.delete_team(TEAM), Ok(true));
    assert!(case.store.commit(&checkout, fence, || true).is_err());
    assert_eq!(case.store.delete_team("nobody"), Ok(true));
    case.saved();
    case.save("trinity").expect("saved");
    let late = case.checkout("neo2", Kind::Person).expect("checked out");
    let fence = case.store.save_fence("neo2");
    case.store.delete_all().expect("reset");
    assert_eq!((case.current(TEAM), case.current("trinity")), (None, None));
    assert!(case.store.commit(&late, fence, || true).is_err());
}

#[test]
fn a_team_deletion_or_a_reset_keeps_only_a_pending_deletion_s_metadata() {
    let case = Case::new();
    let (sign_in_id, generation) = confirmed(&case.saved());
    case.save("trinity").expect("saved");
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    assert_eq!(case.store.delete_team(TEAM), Ok(true));
    assert_eq!(case.store.deletions().expect("readable").len(), 1);
    case.store.delete_all().expect("reset");
    assert_eq!(case.teams(), [TEAM]);
    assert_eq!(
        case.store.deletions().expect("readable")[0].deleting,
        deleting()
    );
    assert_eq!(case.store.settle(TEAM, EVENT), Ok(true));
    case.store.delete_all().expect("reset");
    assert!(!case.scratch.0.join("state/index.json").exists());
}

fn write_index(case: &Case, bytes: &[u8]) {
    use std::os::unix::fs::PermissionsExt;
    let index = case.scratch.0.join("state/index.json");
    let parent = index.parent().expect("a parent");
    std::fs::create_dir_all(parent).expect("created");
    std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700)).expect("private");
    std::fs::write(&index, bytes).expect("written");
    std::fs::set_permissions(&index, std::fs::Permissions::from_mode(0o600)).expect("private");
}

#[test]
fn a_state_or_record_not_this_store_s_own_is_refused() {
    let case = Case::new();
    for written in [
        &b"not json"[..],
        b"[]",
        b"{\"schema\": 2, \"teams\": []}",
        b"{\"schema\": 1, \"teams\": {}}",
    ] {
        write_index(&case, written);
        assert!(case.store.current(TEAM).is_err());
    }
    let sign_in_id = "a".repeat(32);
    let key = case.store.key(true).expect("a key");
    let aad = super::record_aad(TEAM, &sign_in_id);
    let seal = |plaintext: &[u8]| {
        team_storage::envelope::Envelope::seal_identified(&key, plaintext, &aad, &OsEntropy)
            .expect("sealed")
            .to_value()
    };
    for envelope in [
        format!(r#"{{"sign_in_id":"{sign_in_id}"}}"#),
        r#"{"sign_in_id":"nope","record":{}}"#.to_owned(),
        format!(
            r#"{{"sign_in_id":"{sign_in_id}","record":{}}}"#,
            team_protocol::python::json::dumps(&seal(b"not json"), super::private::STATE_JSON)
        ),
        format!(
            r#"{{"sign_in_id":"{sign_in_id}","record":{}}}"#,
            team_protocol::python::json::dumps(
                &seal(br#"{"team_id":"neo"}"#),
                super::private::STATE_JSON
            )
        ),
    ] {
        write_index(
            &case,
            format!(r#"{{"schema":2,"teams":{{"neo":{envelope}}}}}"#).as_bytes(),
        );
        assert!(case.store.current(TEAM).is_err(), "{envelope}");
    }
}
