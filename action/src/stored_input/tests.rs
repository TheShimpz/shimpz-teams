use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use team_core::clock::Clock;
use team_core::entropy::{Entropy, EntropyUnavailable};
use team_core::secret::Secret;
use team_core::testing::{CountingEntropy, FakeClock};
use team_storage::private::StateError;

use super::{Declaration, Status, StoredInputError, StoredInputStore};
use crate::journal::sha256_hex;

const TOKEN: &str = "whatsapp-token-private-material-123456789";
const REFERENCE: (&str, &str, &str) = ("team_1", "whatsapp", "whatsapp-token");

fn origin(letter: char) -> String {
    letter.to_string().repeat(64)
}

fn declarations() -> Vec<(String, Declaration)> {
    vec![(
        "whatsapp-token".to_owned(),
        Declaration {
            kind: "password".to_owned(),
            label: "WhatsApp token".to_owned(),
            description: "Token used to call the WhatsApp API.".to_owned(),
        },
    )]
}

fn open(root: &Path, entropy: Arc<dyn Entropy>) -> StoredInputStore {
    let clock: Arc<dyn Clock> = Arc::new(FakeClock::at(
        UNIX_EPOCH + Duration::from_secs(1_000_000_000),
    ));
    StoredInputStore::new(
        root.join("state/stored-inputs.json"),
        root.join("key/aes256.key"),
        clock,
        entropy,
    )
    .expect("separate paths")
}

fn store(root: &Path) -> StoredInputStore {
    open(root, Arc::new(CountingEntropy::default()))
}

fn seal(store: &StoredInputStore, reference: (&str, &str, &str), value: &str, from: char) -> u64 {
    store
        .seal(
            reference,
            "password",
            &Secret::new(value.to_owned()),
            &origin(from),
        )
        .expect("sealed")
}

/// Fills each request with its next queued byte.
struct Nonces(Mutex<Vec<u8>>);

impl Entropy for Nonces {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        bytes.fill(self.0.lock().expect("usable").remove(0));
        Ok(())
    }
}

#[test]
fn sealed_state_bytes_stay_pinned_and_decode_after_reopen() {
    let root = tempfile::tempdir().expect("temporary directory");
    let key = root.path().join("key");
    fs::create_dir(&key).expect("created");
    fs::set_permissions(&key, fs::Permissions::from_mode(0o700)).expect("mode");
    fs::write(key.join("aes256.key"), (0..32).collect::<Vec<u8>>()).expect("written");
    fs::set_permissions(key.join("aes256.key"), fs::Permissions::from_mode(0o600)).expect("mode");
    let pinned = open(root.path(), Arc::new(Nonces(Mutex::new(vec![1, 2]))));
    seal(&pinned, REFERENCE, "pässwörd ✓", 'a');
    seal(&pinned, ("team_2", "other", "token"), TOKEN, 'b');
    // The bytes the Python writer produced for the same keyring, nonces, clock, and values.
    let bytes = fs::read(root.path().join("state/stored-inputs.json")).expect("read");
    assert_eq!(
        sha256_hex(&bytes),
        "63bb821348f1d85631e974c25e069208f2c5c8cf141589b82ab4beaf3e2f931a"
    );
    let resolved = store(root.path())
        .resolve(REFERENCE, "password")
        .expect("resolved");
    assert_eq!(
        (
            resolved.value.expose(),
            resolved.generation,
            resolved.origin
        ),
        ("pässwörd ✓", 1, origin('a'))
    );
}

#[test]
fn stored_input_ids_only_reach_the_inventory_never_values() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    let missing = store
        .metadata("team_1", "whatsapp", &declarations())
        .expect("listed");
    assert_eq!(
        (missing[0].status, missing[0].generation),
        (Status::Missing, 0)
    );
    assert_eq!(seal(&store, REFERENCE, TOKEN, 'a'), 1);
    let resolved = store.resolve(REFERENCE, "password").expect("resolved");
    assert_eq!((resolved.value.expose(), resolved.generation), (TOKEN, 1));
    assert!(!format!("{resolved:?}").contains(TOKEN));
    let state = fs::read(root.path().join("state/stored-inputs.json")).expect("read");
    let key = fs::read(root.path().join("key/aes256.key")).expect("read");
    let leaked = |needle: &[u8]| {
        [&state, &key]
            .iter()
            .any(|bytes| bytes.windows(needle.len()).any(|window| window == needle))
    };
    assert!(!leaked(TOKEN.as_bytes()) && !leaked(b"\"value\""));
    let modes: Vec<u32> = ["state/stored-inputs.json", "key/aes256.key", "state", "key"]
        .iter()
        .map(|path| fs::metadata(root.path().join(path)).expect("stat").mode() & 0o7777)
        .collect();
    assert_eq!(modes, [0o600, 0o600, 0o700, 0o700]);
    let inventory = store
        .inventory("team_1", &[("whatsapp".to_owned(), declarations())])
        .expect("listed");
    let expected = r#"{"stored_inputs":[{"assistant_id":"whatsapp","status":"stored","stored_input_id":"whatsapp-token"}],"team_id":"team_1"}"#;
    assert_eq!(
        team_protocol::python::json::dumps(&inventory, team_protocol::python::json::CANONICAL),
        expected
    );
}

#[test]
fn rotation_and_an_external_replacement_are_read_again() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (first, second) = (store(root.path()), store(root.path()));
    assert_eq!(seal(&first, REFERENCE, TOKEN, 'a'), 1);
    assert_eq!(
        first
            .resolve(REFERENCE, "password")
            .expect("resolved")
            .value
            .expose(),
        TOKEN
    );
    assert_eq!(
        seal(
            &second,
            REFERENCE,
            "replacement-whatsapp-token-material",
            'b'
        ),
        2
    );
    let refreshed = first.resolve(REFERENCE, "password").expect("resolved");
    assert_eq!(
        (
            refreshed.value.expose(),
            refreshed.generation,
            refreshed.origin
        ),
        ("replacement-whatsapp-token-material", 2, origin('b'))
    );
}

#[test]
fn generations_never_repeat_after_individual_or_bulk_deletion() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    let mut issued = vec![seal(&store, REFERENCE, TOKEN, 'a')];
    let deletions: [&dyn Fn() -> Result<bool, StoredInputError>; 5] = [
        &|| store.delete(REFERENCE),
        &|| store.retain_declared("team_1", "whatsapp", &[]),
        &|| store.delete_assistant("team_1", "whatsapp"),
        &|| store.delete_team("team_1"),
        &|| store.delete_all(),
    ];
    for delete in deletions {
        assert_eq!(delete(), Ok(true));
        issued.push(seal(&store, REFERENCE, "replacement-value-1234", 'a'));
    }
    assert_eq!(issued, [1, 2, 3, 4, 5, 6]);
    let reopened = open(root.path(), Arc::new(CountingEntropy::default()));
    assert_eq!(
        reopened
            .resolve(REFERENCE, "password")
            .expect("resolved")
            .generation,
        6
    );
}

#[test]
fn the_aad_refuses_a_value_moved_to_another_team_assistant_or_id() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    seal(&store, REFERENCE, TOKEN, 'a');
    let path = root.path().join("state/stored-inputs.json");
    let text = fs::read_to_string(&path).expect("read");
    let moved = text.replacen(
        "\"team_1\":{\"whatsapp\":{\"whatsapp-token\"",
        "\"team_2\":{\"other-assistant\":{\"other-token\"",
        1,
    );
    fs::write(&path, moved).expect("written");
    let declared = vec![("other-token".to_owned(), declarations().remove(0).1)];
    let rows = store
        .metadata("team_2", "other-assistant", &declared)
        .expect("listed");
    assert_eq!(rows[0].status, Status::Stored);
    let resolved = store.resolve(("team_2", "other-assistant", "other-token"), "password");
    assert_eq!(
        resolved.err(),
        Some(StoredInputError::State(StateError::Unauthentic))
    );
    assert_eq!(
        store.delete(("team_2", "other-assistant", "other-token")),
        Ok(true)
    );
    assert_eq!(
        store
            .metadata("team_2", "other-assistant", &declared)
            .expect("listed")[0]
            .status,
        Status::Missing
    );
}

#[test]
fn lifecycle_cleanup_is_team_isolated_and_idempotent() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    seal(&store, REFERENCE, TOKEN, 'a');
    seal(
        &store,
        ("team_1", "other", "other-token"),
        "other-assistant-token",
        'b',
    );
    seal(
        &store,
        ("team_2", "whatsapp", "whatsapp-token"),
        "other-team-token",
        'c',
    );
    assert_eq!(
        (
            store.retain_declared("team_1", "whatsapp", &[]),
            store.retain_declared("team_1", "whatsapp", &[])
        ),
        (Ok(true), Ok(false))
    );
    let value = |reference| {
        store
            .resolve(reference, "password")
            .expect("resolved")
            .value
            .expose()
            .to_owned()
    };
    assert_eq!(
        value(("team_1", "other", "other-token")),
        "other-assistant-token"
    );
    assert_eq!(
        value(("team_2", "whatsapp", "whatsapp-token")),
        "other-team-token"
    );
    let twice = |delete: &dyn Fn() -> Result<bool, StoredInputError>| (delete(), delete());
    assert_eq!(
        twice(&|| store.delete_team("team_1")),
        (Ok(true), Ok(false))
    );
    let assistant = || store.delete_assistant("team_2", "whatsapp");
    assert_eq!(twice(&assistant), (Ok(true), Ok(false)));
    assert_eq!(
        (store.delete_all(), store.delete_all()),
        (Ok(false), Ok(false))
    );
}

#[test]
fn a_missing_value_or_an_exact_delete_never_creates_storage() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    assert_eq!(
        store.resolve(REFERENCE, "password").err(),
        Some(StoredInputError::Missing)
    );
    assert_eq!(store.delete(REFERENCE), Ok(false));
    assert_eq!(
        (
            store.delete_assistant("team_1", "whatsapp"),
            store.delete_team("team_1"),
            store.delete_all()
        ),
        (Ok(false), Ok(false), Ok(false))
    );
    assert!(!root.path().join("state").exists() && !root.path().join("key").exists());
}

#[test]
fn kinds_values_origins_and_declarations_fail_closed() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    let secret = |value: &str| Secret::new(value.to_owned());
    assert_eq!(
        store.seal(REFERENCE, "text", &secret(TOKEN), &origin('a')),
        Err(StoredInputError::Invalid)
    );
    assert_eq!(
        store.resolve(REFERENCE, "text").err(),
        Some(StoredInputError::Invalid)
    );
    let values = [String::new(), "x".repeat(super::MAX_VALUE_CHARACTERS + 1)];
    let refused = values.iter().filter(|value| {
        store.seal(REFERENCE, "password", &secret(value), &origin('a'))
            == Err(StoredInputError::Invalid)
    });
    assert_eq!(refused.count(), 2);
    assert_eq!(
        store.seal(REFERENCE, "password", &secret(TOKEN), "not-an-origin"),
        Err(StoredInputError::Invalid)
    );
    assert_eq!(
        store.seal(
            ("Team", "whatsapp", "token"),
            "password",
            &secret(TOKEN),
            &origin('a')
        ),
        Err(StoredInputError::Invalid)
    );
}

#[test]
fn invalid_declarations_fail_closed() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    let mut bad = declarations();
    bad[0].0 = "WhatsApp_Token".to_owned();
    assert_eq!(
        store.metadata("team_1", "whatsapp", &bad).err(),
        Some(StoredInputError::Invalid)
    );
    let mut padded = declarations();
    padded[0].1.label = " padded ".to_owned();
    assert_eq!(
        store.metadata("team_1", "whatsapp", &padded).err(),
        Some(StoredInputError::Invalid)
    );
    let many: Vec<(String, Declaration)> = (0..9)
        .map(|index| (format!("token-{index}"), declarations().remove(0).1))
        .collect();
    assert_eq!(
        store.metadata("team_1", "whatsapp", &many).err(),
        Some(StoredInputError::Invalid)
    );
}

#[test]
fn corrupt_state_capacity_and_an_ambiguous_inventory_fail_closed() {
    let root = tempfile::tempdir().expect("temporary directory");
    let store = store(root.path());
    for index in 0..8 {
        seal(
            &store,
            ("team_1", "whatsapp", &format!("token-{index}")),
            TOKEN,
            'a',
        );
    }
    let ninth = store.seal(
        ("team_1", "whatsapp", "token-8"),
        "password",
        &Secret::new(TOKEN.to_owned()),
        &origin('a'),
    );
    assert_eq!(ninth, Err(StoredInputError::Capacity));
    let duplicate = ("whatsapp".to_owned(), declarations());
    assert_eq!(
        store
            .inventory("team_1", &[duplicate.clone(), duplicate])
            .err(),
        Some(StoredInputError::Invalid)
    );
    let path = root.path().join("state/stored-inputs.json");
    fs::write(&path, "{").expect("written");
    assert_eq!(
        store.metadata("team_1", "whatsapp", &declarations()).err(),
        Some(StoredInputError::State(StateError::Malformed))
    );
}

#[test]
fn only_the_exact_decrypted_shape_is_a_value() {
    for plaintext in [
        vec![b'x'; super::MAX_PLAINTEXT_BYTES + 1],
        b"[]".to_vec(),
        br#"{"origin":"bad","value":"secret"}"#.to_vec(),
        br#"{"value":"secret"}"#.to_vec(),
    ] {
        assert!(super::decrypted(&plaintext).is_none());
    }
    let valid = format!(r#"{{"origin":"{}","value":"secret"}}"#, origin('a'));
    assert_eq!(
        super::decrypted(valid.as_bytes()).map(|(value, _)| value.expose().to_owned()),
        Some("secret".to_owned())
    );
}
