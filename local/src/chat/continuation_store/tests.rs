use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use team_core::entropy::OsEntropy;
use team_core::testing::FakeClock;

use super::{ContinuationStore, StoreError};

const EPOCH: u64 = 1_800_000_000;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/state/var/lib/shimpz-local/chat-continuations")
}

fn copy_private(source: &Path, target: &Path) {
    if source.is_dir() {
        fs::create_dir_all(target).expect("created");
        fs::set_permissions(target, fs::Permissions::from_mode(0o700)).expect("mode set");
        for entry in fs::read_dir(source).expect("listed") {
            let entry = entry.expect("entry");
            copy_private(&entry.path(), &target.join(entry.file_name()));
        }
    } else {
        fs::copy(source, target).expect("copied");
        fs::set_permissions(target, fs::Permissions::from_mode(0o600)).expect("mode set");
    }
}

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    clock: Arc<FakeClock>,
}

impl Fixture {
    fn empty() -> Self {
        let directory = tempfile::tempdir().expect("directory");
        let root = directory.path().join("chat-continuations");
        Self {
            root,
            _directory: directory,
            clock: Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_secs(EPOCH))),
        }
    }

    fn golden() -> Self {
        let fixture = Self::empty();
        copy_private(&fixtures(), &fixture.root);
        fixture
    }

    fn store(&self, capacity: usize) -> ContinuationStore {
        ContinuationStore::new(
            &self.root.join("state/continuations.json"),
            &self.root.join("key/aes256.key"),
            Arc::clone(&self.clock) as Arc<dyn team_core::clock::Clock>,
            Arc::new(OsEntropy),
            capacity,
        )
        .expect("store")
    }
}

fn bindings(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

#[test]
fn the_python_teams_continuations_open_and_expire_on_its_clock() {
    let fixture = Fixture::golden();
    let store = fixture.store(32);
    let active = store.active().expect("active");
    let teams: Vec<_> = active
        .iter()
        .map(|item| (item.team_id.as_str(), item.kind.as_str(), item.generation))
        .collect();
    assert_eq!(
        teams,
        vec![("team_1", "integrations", 1), ("team_2", "human", 1)]
    );
    assert_eq!(active[0].bindings, bindings(&["a-binding ✓", "b-binding"]));
    assert!(active.iter().all(|item| !item.payload.is_empty()));
    fixture.clock.advance(Duration::from_secs(350));
    let drained = store.drain_expired().expect("drained");
    assert_eq!(
        drained
            .iter()
            .map(|item| item.team_id.as_str())
            .collect::<Vec<_>>(),
        vec!["team_1"]
    );
    assert_eq!(store.active().expect("active").len(), 1);
    assert_eq!(store.drain_expired().expect("drained").len(), 0);
}

#[test]
fn one_continuation_per_team_is_replaced_under_the_next_generation() {
    let fixture = Fixture::empty();
    let store = fixture.store(32);
    let expires = i64::try_from(EPOCH).expect("epoch") + 300;
    let first = store
        .put(
            "team_1",
            "human",
            &"a".repeat(32),
            expires,
            &bindings(&["b", "a"]),
            b"first",
        )
        .expect("put");
    assert_eq!(
        (first.generation, first.bindings.clone()),
        (1, bindings(&["a", "b"]))
    );
    let second = store
        .put(
            "team_1",
            "integrations",
            &"b".repeat(32),
            expires,
            &bindings(&["c"]),
            b"second",
        )
        .expect("put");
    assert_eq!(second.generation, 2);
    let current = store.current("team_1").expect("read").expect("current");
    assert_eq!(
        (current.payload.as_slice(), current.kind.as_str()),
        (&b"second"[..], "integrations")
    );
    // A deletion bound to another challenge refuses; the right one deletes once.
    assert_eq!(
        store.delete("team_1", Some(&"a".repeat(32))),
        Err(StoreError::NotFound)
    );
    assert_eq!(store.delete("team_1", Some(&"b".repeat(32))), Ok(true));
    assert_eq!(store.delete("team_1", None), Ok(false));
    let state = fixture.root.join("state/continuations.json");
    assert_eq!(
        fs::metadata(&state).expect("state").permissions().mode() & 0o777,
        0o600
    );
}

/// One `put`'s arguments.
#[derive(Clone)]
struct Put {
    team: &'static str,
    kind: &'static str,
    challenge: String,
    expires: i64,
    bindings: Vec<String>,
    payload: Vec<u8>,
}

impl Put {
    fn valid() -> Self {
        Self {
            team: "team_1",
            kind: "human",
            challenge: "a".repeat(32),
            expires: i64::try_from(EPOCH).expect("epoch") + 10,
            bindings: bindings(&["a"]),
            payload: b"x".to_vec(),
        }
    }

    fn into(self, store: &ContinuationStore) -> Result<super::StoredContinuation, StoreError> {
        let Self {
            team,
            kind,
            challenge,
            expires,
            bindings,
            payload,
        } = self;
        store.put(team, kind, &challenge, expires, &bindings, &payload)
    }
}

#[test]
fn arguments_outside_the_contract_and_a_full_store_are_refused() {
    let fixture = Fixture::empty();
    let store = fixture.store(1);
    let now = i64::try_from(EPOCH).expect("epoch");
    let changes: [fn(&mut Put); 9] = [
        |put| put.team = "Team 1",
        |put| put.kind = "other",
        |put| put.challenge = "A".to_owned(),
        |put| put.expires -= 10,
        |put| put.expires += 900,
        |put| put.payload.clear(),
        |put| put.bindings.clear(),
        |put| put.bindings = bindings(&[" a"]),
        |put| put.bindings = bindings(&["a", "a"]),
    ];
    for change in changes {
        let mut put = Put::valid();
        change(&mut put);
        assert_eq!(put.into(&store), Err(StoreError::Invalid));
    }
    assert!(now > 0);
    Put::valid().into(&store).expect("put");
    let mut other = Put::valid();
    other.team = "team_2";
    assert_eq!(other.into(&store), Err(StoreError::Capacity));
}

#[test]
fn a_tampered_record_fails_closed() {
    let fixture = Fixture::empty();
    let store = fixture.store(32);
    let now = i64::try_from(EPOCH).expect("epoch");
    store
        .put(
            "team_1",
            "human",
            &"a".repeat(32),
            now + 10,
            &bindings(&["a"]),
            b"x",
        )
        .expect("put");
    let path = fixture.root.join("state/continuations.json");
    let text = fs::read_to_string(&path).expect("state");
    // Another expiry changes the authenticated data, so the envelope no longer opens.
    fs::write(
        &path,
        text.replace(&format!("{}", now + 10), &format!("{}", now + 11)),
    )
    .expect("written");
    assert_eq!(store.active(), Err(StoreError::Unavailable));
    fs::write(&path, "{}").expect("written");
    assert_eq!(store.active(), Err(StoreError::Unavailable));
    assert_eq!(store.clear(), Err(StoreError::Unavailable));
}
