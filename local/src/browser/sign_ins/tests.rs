use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::testing::FakeClock;

use super::record::Deleting;
use super::{Checkout, Expired, IDLE_SECONDS, Kind, Pending, SignInError, SignInStore};

const TEAM: &str = "neo";
const WRITER: &str = "image@sha256:x";
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
        self.store.current(team, Some(WRITER)).expect("readable")
    }

    fn checkout(&self, team: &str, kind: Kind) -> Result<Checkout, SignInError> {
        self.store.checkout(team, &incarnation(), kind)
    }

    /// Checks the Team's Sign-in out and Saves its next generation.
    fn save(&self, team: &str, chunks: &[&[u8]], sites: &[&str]) -> Result<Value, SignInError> {
        let checkout = self.checkout(team, Kind::Person)?;
        let mut draft = self.store.draft(&checkout)?;
        let saved = chunks
            .iter()
            .try_for_each(|chunk| draft.add(chunk))
            .and_then(|()| {
                let sites: Vec<String> = sites.iter().map(|site| (*site).to_owned()).collect();
                draft.commit(&sites, false, WRITER, || true)
            });
        if saved.is_err() {
            draft.discard();
            let _ = self.store.finish(&checkout);
        }
        saved
    }

    fn saved(&self) -> Value {
        self.save(TEAM, &[b"first", b"second"], &["linkedin.com"])
            .expect("saved")
    }

    fn capsules(&self) -> Vec<String> {
        let Ok(entries) = std::fs::read_dir(self.scratch.0.join("state/capsules")) else {
            return Vec::new();
        };
        let mut names: Vec<String> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    fn teams(&self) -> Vec<String> {
        self.store
            .state()
            .expect("readable")
            .into_iter()
            .map(|(team, _)| team)
            .collect()
    }

    fn restore(&self, checkout: &Checkout) -> Vec<Vec<u8>> {
        self.store
            .capsule(checkout, WRITER)
            .expect("restorable")
            .expect("a capsule")
            .collect::<Result<_, _>>()
            .expect("authentic")
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
fn a_saved_sign_in_shows_without_its_capsule() {
    let case = Case::new();
    assert_eq!(case.current(TEAM), None);
    let saved = case
        .save(
            TEAM,
            &[b"first", b"second"],
            &["linkedin.com", "example.com"],
        )
        .expect("saved");
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
            "sites",
            "sites_truncated",
            "state",
            "team_incarnation"
        ]
    );
    assert_eq!(get(&saved, "state"), &text("ready"));
    assert_eq!(get(&saved, "generation"), &int(1));
    assert_eq!(
        get(&saved, "sites"),
        &Value::Array(vec![text("example.com"), text("linkedin.com")])
    );
    assert_eq!(get(&saved, "expires_at"), &text("2027-01-22T08:00:00Z"));
    assert_eq!(case.current(TEAM), Some(saved));
    assert_eq!(case.current("other"), None);
}

#[test]
fn a_saved_capsule_restores_exactly_and_nothing_is_readable_without_the_key() {
    let case = Case::new();
    case.save(TEAM, &[b"first", b"second"], &["linkedin.com"])
        .expect("saved");
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(checkout.generation, 1);
    assert_eq!(
        case.restore(&checkout),
        [b"first".to_vec(), b"second".to_vec()]
    );
    // Nothing of the capsule or the record is readable without the key.
    let mut stored = Vec::new();
    for entry in walk(&case.scratch.0.join("state")) {
        stored.extend(std::fs::read(entry).expect("read"));
    }
    let haystack = String::from_utf8_lossy(&stored);
    assert!(!haystack.contains("first"));
    assert!(!haystack.contains("linkedin"));
}

fn walk(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in std::fs::read_dir(root).into_iter().flatten().flatten() {
        let path = entry.path();
        if path.is_dir() {
            files.extend(walk(&path));
        } else {
            files.push(path);
        }
    }
    files
}

#[test]
fn a_save_writes_the_next_generation_and_replaces_the_one_it_restored() {
    let case = Case::new();
    let first = case.saved();
    let second = case
        .save(TEAM, &[b"third"], &["github.com"])
        .expect("saved");
    assert_eq!(get(&second, "sign_in_id"), get(&first, "sign_in_id"));
    assert_eq!(get(&second, "generation"), &int(2));
    let Value::String(sign_in_id) = get(&first, "sign_in_id") else {
        panic!("an id")
    };
    assert_eq!(case.capsules(), [format!("{sign_in_id}-2")]);
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(case.restore(&checkout), [b"third".to_vec()]);
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
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"x").expect("added");
    case.store.finish(&checkout).expect("finished");
    // An end without a Save leaves the restored generation needing a person; a Save of that checkout is refused.
    let current = case.current(TEAM).expect("a Sign-in");
    assert_eq!(get(&current, "state"), &text("needs-person"));
    assert_eq!(get(&current, "generation"), &int(1));
    assert!(
        draft
            .commit(&["linkedin.com".to_owned()], false, "image", || true)
            .is_err()
    );
    case.store
        .finish(&checkout)
        .expect("a stale end changes nothing");
    assert_eq!(
        case.checkout(TEAM, Kind::Agent),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
    // A person reconciles it: their Save makes it ready again.
    assert_eq!(get(&case.saved(), "state"), &text("ready"));
    assert!(case.store.capsule(&checkout, WRITER).is_err());
    assert!(case.store.draft(&checkout).is_err());
}

#[test]
fn a_team_s_first_checkout_that_ends_without_a_save_leaves_no_sign_in() {
    let case = Case::new();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(checkout.generation, 0);
    assert!(
        case.store
            .capsule(&checkout, WRITER)
            .expect("readable")
            .is_none()
    );
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
    assert_eq!(case.capsules(), Vec::<String>::new());
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

#[test]
fn an_expired_sign_in_whose_deletion_is_not_proven_keeps_a_new_checkout_out() {
    let case = Case::new();
    case.saved();
    case.clock.advance(Duration::from_secs_f64(IDLE_SECONDS));
    case.store.faults().remove = true;
    assert_eq!(
        case.checkout(TEAM, Kind::Person),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
    assert_eq!(case.store.take_expired(), Vec::new());
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
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"under way").expect("added");
    case.store
        .begin_delete(TEAM, exact, deleting())
        .expect("fenced");
    assert!(draft.add(b"after the fence").is_err());
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(true));
    assert_eq!(case.current(TEAM), None);
    assert_eq!(case.capsules(), Vec::<String>::new());
    assert!(!case.scratch.0.join("state/drafts").join(TEAM).exists());
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
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(true));
    // Until its completion is recorded, the fenced record keeps the deletion's metadata and every checkout out.
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
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
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(true));
    assert_eq!(case.store.settle(TEAM, EVENT), Ok(true));
    assert_eq!(
        get(&case.current(TEAM).expect("kept"), "generation"),
        &int(1)
    );
}

#[test]
fn a_deletion_it_cannot_prove_keeps_its_fence_and_completes_after_a_restart() {
    let case = Case::new();
    let (sign_in_id, generation) = confirmed(&case.saved());
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    case.store.faults().remove = true;
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(false));
    // The fence and its metadata survive a restart, whose start removes what it left.
    let restarted = Case::open(&case.scratch.0, &case.clock);
    assert_eq!(restarted.current(TEAM, Some(WRITER)), Ok(None));
    restarted.reconcile().expect("reconciled");
    assert_eq!(case.capsules(), Vec::<String>::new());
    let pending = restarted.deletions().expect("readable");
    assert_eq!(pending.len(), 1);
    assert_eq!(restarted.settle(TEAM, EVENT), Ok(true));
    assert_eq!(case.teams(), Vec::<String>::new());
}

#[test]
fn a_starting_team_ends_open_checkouts_and_deletes_what_was_left() {
    let case = Case::new();
    case.saved();
    case.checkout(TEAM, Kind::Person).expect("checked out");
    let orphan = case
        .scratch
        .0
        .join(format!("state/capsules/{}-1", "d".repeat(32)));
    std::fs::create_dir(&orphan).expect("created");
    let trinity = case.checkout("trinity", Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&trinity).expect("a draft");
    draft.add(b"interrupted").expect("added");
    case.store.reconcile().expect("reconciled");
    assert_eq!(
        get(&case.current(TEAM).expect("kept"), "state"),
        &text("needs-person")
    );
    assert_eq!(case.teams(), [TEAM]);
    assert!(!orphan.exists());
    assert!(!case.scratch.0.join("state/drafts").exists());
    // A lifecycle deletion left undone is completed outright; a Supervisor's keeps its record until it is recorded.
    case.store.faults().remove = true;
    assert_eq!(case.store.delete_team(TEAM), Ok(false));
    case.store.faults().remove = false;
    case.store.reconcile().expect("reconciled");
    assert_eq!(case.teams(), Vec::<String>::new());
    let (sign_in_id, generation) = confirmed(&case.saved());
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    case.store.reconcile().expect("reconciled");
    assert_eq!(case.capsules(), Vec::<String>::new());
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
    assert_eq!(case.capsules(), Vec::<String>::new());
}

#[test]
fn a_team_deletion_and_a_reset_take_everything_and_fence_saves() {
    let case = Case::new();
    case.saved();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"under way").expect("added");
    assert_eq!(case.store.delete_team(TEAM), Ok(true));
    assert!(
        draft
            .commit(&["linkedin.com".to_owned()], false, "image", || true)
            .is_err()
    );
    assert_eq!(case.store.delete_team("nobody"), Ok(true));
    case.saved();
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
    let late = case.checkout("neo2", Kind::Person).expect("checked out");
    let mut late = case.store.draft(&late).expect("a draft");
    case.store.delete_all().expect("reset");
    assert_eq!((case.current(TEAM), case.current("trinity")), (None, None));
    assert!(!case.scratch.0.join("state/capsules").exists());
    assert!(late.add(b"after the reset").is_err());
}

#[test]
fn only_an_admissible_bounded_capsule_with_admissible_sites_is_kept() {
    let case = Case::new();
    assert!(case.save(TEAM, &[], &["linkedin.com"]).is_err());
    assert!(case.save(TEAM, &[b"x"], &["Example.com"]).is_err());
    assert!(case.save(TEAM, &[b""], &["linkedin.com"]).is_err());
    let oversized = vec![b'x'; super::CHUNK_BYTES + 1];
    assert!(case.save(TEAM, &[&oversized], &["linkedin.com"]).is_err());
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"x").expect("added");
    assert!(
        draft
            .commit(&["linkedin.com".to_owned()], false, WRITER, || false)
            .is_err()
    );
    draft.discard();
    case.store.finish(&checkout).expect("finished");
    assert_eq!(case.current(TEAM), None);
}

#[test]
fn a_removal_that_leaves_a_capsule_in_place_proves_nothing() {
    let case = Case::new();
    let (sign_in_id, generation) = confirmed(&case.saved());
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    case.store.faults().remove = true;
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(false));
    case.store.faults().remove = false;
    assert_eq!(case.store.purge(TEAM, EVENT), Ok(true));
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
    case.store.faults().remove = true;
    // A new incarnation's checkout must delete the earlier Sign-in first, and cannot.
    assert_eq!(
        case.store
            .checkout("trinity", &"m".repeat(64), Kind::Person),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
}

#[test]
fn a_team_deletion_or_a_reset_keeps_only_a_pending_deletion_s_metadata() {
    let case = Case::new();
    let (sign_in_id, generation) = confirmed(&case.saved());
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (&sign_in_id, generation, &inc), deleting())
        .expect("fenced");
    assert_eq!(case.store.delete_team(TEAM), Ok(true));
    assert_eq!(case.store.deletions().expect("readable").len(), 1);
    case.store.delete_all().expect("reset");
    assert_eq!(case.capsules(), Vec::<String>::new());
    assert_eq!(case.teams(), [TEAM]);
    assert_eq!(
        case.store.deletions().expect("readable")[0].deleting,
        deleting()
    );
    assert_eq!(case.store.settle(TEAM, EVENT), Ok(true));
    case.store.delete_all().expect("reset");
    assert!(!case.scratch.0.join("state/index.json").exists());
}

#[test]
fn a_capsule_restores_only_into_the_image_that_wrote_it() {
    let case = Case::new();
    case.saved();
    // Another image shows it as needing a person, starts a person's session without it, and refuses an agent.
    for reader in [Some("image@sha256:y"), None] {
        let current = case
            .store
            .current(TEAM, reader)
            .expect("readable")
            .expect("kept");
        assert_eq!(get(&current, "state"), &text("needs-person"));
    }
    let person = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert!(
        case.store
            .capsule(&person, "image@sha256:y")
            .expect("readable")
            .is_none()
    );
    case.store.finish(&person).expect("finished");
    case.saved();
    let agent = case.checkout(TEAM, Kind::Agent).expect("checked out");
    assert_eq!(
        case.store.capsule(&agent, "image@sha256:y").map(|_| ()),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
}

#[test]
fn a_save_whose_index_write_failed_is_kept_exactly_when_the_index_names_it() {
    let case = Case::new();
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"kept").expect("added");
    case.store.faults().write = Some(true);
    let saved = draft
        .commit(&["linkedin.com".to_owned()], false, WRITER, || true)
        .expect("kept: the index names it");
    case.store.faults().write = None;
    assert_eq!(get(&saved, "generation"), &int(1));
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    assert_eq!(case.restore(&checkout), [b"kept".to_vec()]);
}

#[test]
fn a_save_whose_publication_is_unknown_keeps_its_capsule_and_needs_a_person() {
    let case = Case::new();
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
    let checkout = case.checkout("trinity", Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"named").expect("added");
    // Named but not proven durable: the capsule stays, and the generation is held.
    case.store.faults().write = Some(true);
    case.store.faults().index_unsynced = true;
    assert!(
        draft
            .commit(&["linkedin.com".to_owned()], false, WRITER, || true)
            .is_err()
    );
    *case.store.faults() = super::Faults::default();
    draft.discard();
    assert!(
        case.capsules()
            .contains(&format!("{}-2", checkout.sign_in_id))
    );
    // Published after all, that generation needs a person: no agent takes it, even before that is recorded.
    case.store.faults().write = Some(false);
    let current = case.current("trinity").expect("kept");
    assert_eq!(get(&current, "state"), &text("needs-person"));
    assert_eq!(
        case.checkout("trinity", Kind::Agent),
        Err(SignInError::refused(team_protocol::sign_in::BUSY))
    );
    case.store.faults().write = None;
    let current = case.current("trinity").expect("kept");
    assert_eq!(get(&current, "state"), &text("needs-person"));
    assert!(!case.store.lock().quarantine.contains_key("trinity"));
}

#[test]
fn a_refused_write_that_named_nothing_is_discarded_at_once() {
    let case = Case::new();
    case.save("trinity", &[b"x"], &["linkedin.com"])
        .expect("saved");
    let checkout = case.checkout("trinity", Kind::Person).expect("checked out");
    let mut draft = case.store.draft(&checkout).expect("a draft");
    draft.add(b"refused").expect("added");
    case.store.faults().write = Some(false);
    assert!(
        draft
            .commit(&["linkedin.com".to_owned()], false, WRITER, || true)
            .is_err()
    );
    case.store.faults().write = None;
    draft.discard();
    assert!(
        !case
            .capsules()
            .contains(&format!("{}-3", checkout.sign_in_id))
    );
}

#[test]
fn only_a_person_s_save_resolves_a_held_generation() {
    let case = Case::new();
    case.saved();
    let agent = case.checkout(TEAM, Kind::Agent).expect("checked out");
    case.store.lock().quarantine.insert(TEAM.to_owned(), 7);
    let mut draft = case.store.draft(&agent).expect("a draft");
    draft.add(b"agent").expect("added");
    draft
        .commit(&["linkedin.com".to_owned()], false, WRITER, || true)
        .expect("saved");
    assert!(case.store.lock().quarantine.contains_key(TEAM));
    case.store.lock().quarantine.clear();
    let person = case.checkout(TEAM, Kind::Person).expect("checked out");
    case.store.lock().quarantine.insert(TEAM.to_owned(), 7);
    let mut draft = case.store.draft(&person).expect("a draft");
    draft.add(b"person").expect("added");
    draft
        .commit(&["linkedin.com".to_owned()], false, WRITER, || true)
        .expect("saved");
    assert!(case.store.lock().quarantine.is_empty());
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
        assert!(case.store.current(TEAM, Some(WRITER)).is_err());
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
        assert!(
            case.store.current(TEAM, Some(WRITER)).is_err(),
            "{envelope}"
        );
    }
}

#[test]
fn a_chunk_not_this_capsule_s_own_is_refused() {
    let case = Case::new();
    case.saved();
    let chunk = walk(&case.scratch.0.join("state/capsules"))
        .into_iter()
        .next()
        .expect("a chunk");
    std::fs::write(&chunk, [0_u8; 40]).expect("tampered");
    let checkout = case.checkout(TEAM, Kind::Person).expect("checked out");
    let mut capsule = case
        .store
        .capsule(&checkout, WRITER)
        .expect("readable")
        .expect("a capsule");
    assert!(capsule.any(|chunk| chunk.is_err()));
}

#[test]
fn the_python_team_s_sign_in_is_read_and_restored_as_it_wrote_it() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/state");
    let manifest =
        strict_json::parse(&std::fs::read(fixtures.join("manifest.json")).expect("read"))
            .expect("json");
    let expected = manifest
        .get("stores")
        .and_then(|stores| stores.get("sign_ins"))
        .expect("the Sign-ins' parameters");
    let scratch = Scratch::new();
    copy(
        &fixtures.join("var/lib/shimpz-local/browser-sign-ins"),
        &scratch.0,
    );
    let clock = Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_hours(500_000)));
    let store = Case::open(&scratch.0, &clock);
    let Some(Value::String(writer)) = expected.get("writer") else {
        panic!("a writer")
    };
    let current = store
        .current(TEAM, Some(writer))
        .expect("readable")
        .expect("kept");
    let sorted =
        |value: &Value| team_protocol::python::json::dumps(value, super::private::STATE_JSON);
    assert_eq!(
        sorted(&current),
        sorted(expected.get("committed").expect("the committed Sign-in"))
    );
    let checkout = store
        .checkout(TEAM, &"n".repeat(64), Kind::Person)
        .expect("checked out");
    let chunks: Vec<Vec<u8>> = store
        .capsule(&checkout, writer)
        .expect("readable")
        .expect("a capsule")
        .collect::<Result<_, _>>()
        .expect("authentic");
    let Some(Value::Array(wanted)) = expected.get("chunks") else {
        panic!("chunks")
    };
    let wanted: Vec<Vec<u8>> = wanted
        .iter()
        .map(|chunk| match chunk {
            Value::String(text) => text.as_bytes().to_vec(),
            _ => panic!("a chunk"),
        })
        .collect();
    assert_eq!(chunks, wanted);
}

fn copy(from: &Path, to: &Path) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(to).expect("created");
    std::fs::set_permissions(to, std::fs::Permissions::from_mode(0o700)).expect("private");
    for entry in std::fs::read_dir(from).expect("listed").flatten() {
        let target = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).expect("copied");
            std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600))
                .expect("private");
        }
    }
}
