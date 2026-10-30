//! The golden state the Python Team wrote (`local/tests/fixtures/state`): every W1 store reads it, and each store whose
//! writer is deterministic writes it back byte for byte.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use rusqlite::Connection;
use strict_json::{Number, Value};
use team_core::entropy::{Entropy, EntropyUnavailable};
use team_core::testing::{CountingEntropy, FakeClock};
use team_protocol::python::json;
use team_storage::envelope::{Bounds, Envelope, Key, decode_base64};
use team_storage::files::FileStore;
use team_storage::private::{Keyring, STATE_JSON};
use team_storage::record_store::{Outcome, RecordPolicy, RecordStore};

const EPOCH: u64 = 1_800_000_000;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../local/tests/fixtures/state")
}

fn manifest() -> Value {
    strict_json::parse(&fs::read(fixtures().join("manifest.json")).expect("manifest"))
        .expect("JSON")
}

fn text<'a>(value: &'a Value, path: &[&str]) -> &'a str {
    let mut current = value;
    for name in path {
        current = current.get(name).unwrap_or_else(|| panic!("{name}"));
    }
    match current {
        Value::String(text) => text,
        other => panic!("{other:?}"),
    }
}

fn key() -> Key {
    let bytes = hex(text(&manifest(), &["key_hex"]));
    Key::from_bytes(bytes.try_into().expect("32 bytes"))
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&text[index..index + 2], 16).expect("hex"))
        .collect()
}

/// Copies one fixture subtree below `root` as a store owns it: directories `0700`, files `0600`.
fn install(relative: &str, root: &Path) -> PathBuf {
    let source = fixtures().join(relative);
    let target = root.join(relative);
    copy_private(&source, &target);
    target
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
        let parent = target.parent().expect("parent");
        fs::create_dir_all(parent).expect("created");
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).expect("mode set");
        fs::copy(source, target).expect("copied");
        fs::set_permissions(target, fs::Permissions::from_mode(0o600)).expect("mode set");
    }
}

/// The one nonce an envelope was sealed with, so a reseal reproduces it.
struct Nonce(Vec<u8>);

impl Entropy for Nonce {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        bytes.copy_from_slice(&self.0);
        Ok(())
    }
}

fn nonce_of(envelope: &Value) -> Nonce {
    Nonce(decode_base64(text(envelope, &["nonce"])).expect("base64"))
}

const SORTED_ASCII: json::Options = STATE_JSON;

#[test]
fn the_python_envelopes_open_and_reseal_byte_for_byte() {
    let manifest = manifest();
    let bounds = Bounds {
        maximum_encoded_part: 4096,
        maximum_plaintext: 1024,
    };
    for (name, identified) in [("plain", false), ("identified", true)] {
        let raw = fs::read(fixtures().join(format!("envelope/{name}.json"))).expect("read");
        let stored = strict_json::parse(&raw).expect("JSON");
        let plaintext = hex(text(
            &manifest,
            &["stores", "envelope", name, "plaintext_hex"],
        ));
        let aad = hex(text(&manifest, &["stores", "envelope", name, "aad_hex"]));
        let envelope = if identified {
            Envelope::admit_identified(&stored, bounds)
        } else {
            Envelope::admit(&stored, bounds)
        }
        .expect("admitted");
        assert_eq!(
            envelope.open(&key(), &aad).expect("opened").as_slice(),
            plaintext
        );
        let resealed = if identified {
            Envelope::seal_identified(&key(), &plaintext, &aad, &nonce_of(&stored))
        } else {
            Envelope::seal(&key(), &plaintext, &aad, &nonce_of(&stored))
        }
        .expect("sealed");
        assert_eq!(
            json::dumps(&resealed.to_value(), SORTED_ASCII).into_bytes(),
            raw,
            "{name}"
        );
    }
    assert_eq!(
        key().id(),
        text(&manifest, &["stores", "envelope", "key_id"])
    );
}

#[test]
fn every_python_keyring_holds_the_fixed_key() {
    let root = tempfile::tempdir().expect("temporary directory");
    for store in [
        "assistant-stored-inputs",
        "assistant-integrations",
        "chat-continuations",
        "browser-sign-ins",
        "routines",
    ] {
        let path = install(
            &format!("var/lib/shimpz-local/{store}/key/aes256.key"),
            root.path(),
        );
        let loaded = Keyring::new(path)
            .load(false, &CountingEntropy::default())
            .expect("loaded");
        assert_eq!(loaded.expose(), key().expose(), "{store}");
    }
}

/// The record shape both Python record stores share: an object with a positive integer generation.
struct Generations;

impl RecordPolicy for Generations {
    fn maximum_state_bytes(&self) -> usize {
        4 * 1024 * 1024
    }

    fn records_per_assistant(&self) -> usize {
        16
    }

    fn maximum_records(&self) -> usize {
        4096
    }

    fn team_id(&self, id: &str) -> bool {
        team_protocol::identifiers::canonical_team_id(&Value::String(id.to_owned())).is_some()
    }

    fn assistant_id(&self, id: &str) -> bool {
        team_protocol::identifiers::canonical_assistant_id(&Value::String(id.to_owned())).is_some()
    }

    fn record_id(&self, id: &str) -> bool {
        team_protocol::identifiers::canonical_identifier(&Value::String(id.to_owned())).is_some()
    }

    fn record_generation(&self, record: &Value) -> Option<u64> {
        match record.get("generation") {
            Some(Value::Number(Number::Int(generation))) if *generation >= 1 => {
                u64::try_from(*generation).ok()
            }
            _ => None,
        }
    }
}

fn record_store(root: &Path, store: &str, file: &str) -> (PathBuf, RecordStore<Generations>) {
    let state = install(&format!("var/lib/shimpz-local/{store}/state/{file}"), root);
    let key = install(
        &format!("var/lib/shimpz-local/{store}/key/aes256.key"),
        root,
    );
    let opened = RecordStore::new(
        state.clone(),
        key,
        Generations,
        Arc::new(CountingEntropy::default()),
    )
    .expect("store");
    (state, opened)
}

#[test]
fn the_python_record_stores_are_admitted_and_rewritten_byte_for_byte() {
    let root = tempfile::tempdir().expect("temporary directory");
    for (store, file) in [
        ("assistant-stored-inputs", "stored-inputs.json"),
        ("assistant-integrations", "integrations.json"),
    ] {
        let (path, opened) = record_store(root.path(), store, file);
        let before = fs::read(&path).expect("read");
        opened
            .update(|_state| Ok::<_, team_storage::private::StateError>(Outcome::Changed(())))
            .expect("rewritten");
        assert_eq!(fs::read(&path).expect("read"), before, "{store}");
    }
}

/// Every record of a record-store state, with its Team, Assistant, and id.
fn records(state: &Value) -> Vec<(String, String, String, Value)> {
    let mut found = Vec::new();
    let Some(Value::Object(teams)) = state.get("teams") else {
        panic!("teams");
    };
    for (team, assistants) in teams {
        let Value::Object(assistants) = assistants else {
            panic!("assistants")
        };
        for (assistant, stored) in assistants {
            let Value::Object(stored) = stored else {
                panic!("records")
            };
            for (id, record) in stored {
                found.push((team.clone(), assistant.clone(), id.clone(), record.clone()));
            }
        }
    }
    found
}

/// Opens one Stored Input record under its AAD, proves a reseal reproduces it, and answers its plaintext.
fn open_stored_input(team: &str, assistant: &str, id: &str, record: &Value) -> String {
    let bounds = Bounds {
        maximum_encoded_part: 16 * 1024 * 2 + 160 * 2 + 128,
        maximum_plaintext: 16 * 1024 + 160,
    };
    let generation = Generations.record_generation(record).expect("generation");
    let aad = json::canonical(&Value::Array(vec![
        Value::String("shimpz-stored-input-v1".to_owned()),
        Value::String(team.to_owned()),
        Value::String(assistant.to_owned()),
        Value::String(id.to_owned()),
        Value::String("password".to_owned()),
        Value::Number(Number::Int(i64::try_from(generation).expect("small"))),
    ]));
    let stored = record.get("envelope").expect("envelope");
    let envelope = Envelope::admit_identified(stored, bounds).expect("admitted");
    let plaintext = envelope.open(&key(), &aad).expect("opened");
    let resealed =
        Envelope::seal_identified(&key(), &plaintext, &aad, &nonce_of(stored)).expect("sealed");
    assert_eq!(
        json::canonical(&resealed.to_value()),
        json::canonical(stored)
    );
    String::from_utf8(plaintext.to_vec()).expect("UTF-8")
}

#[test]
fn the_python_stored_inputs_open_under_their_aad_and_reseal_byte_for_byte() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (_path, opened) =
        record_store(root.path(), "assistant-stored-inputs", "stored-inputs.json");
    let state = opened
        .read(|state| Ok::<_, team_storage::private::StateError>(state.value().clone()))
        .expect("read");
    let values: Vec<String> = records(&state)
        .iter()
        .map(|(team, assistant, id, record)| open_stored_input(team, assistant, id, record))
        .collect();
    let origin = "a".repeat(64);
    assert_eq!(
        values[0],
        format!("{{\"origin\":\"{origin}\",\"value\":\"p\u{e4}ssword-1\"}}")
    );
    assert_eq!(values.len(), 3);
}

fn team_files(root: &Path) -> (PathBuf, FileStore) {
    install("var/lib/shimpz-local/storage", root);
    let storage = root.join("var/lib/shimpz-local/storage");
    let clock = Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_secs(EPOCH)));
    let store = FileStore::new(
        storage.clone(),
        100 * 1024 * 1024,
        None,
        clock,
        Arc::new(CountingEntropy::default()),
    )
    .expect("store");
    (storage, store)
}

#[test]
fn the_python_team_files_database_is_read_with_its_marks() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (_storage, store) = team_files(root.path());
    let manifest = manifest();
    let upload = text(&manifest, &["stores", "team_files", "upload"]);
    let web = text(&manifest, &["stores", "team_files", "web"]);
    let listing = store.list("team_1").expect("listed");
    assert_eq!(listing.files.len(), 3);
    assert_eq!(listing.usage.used_bytes, 13 + 22 + 22);
    let (stored, content) = store.get("team_1", upload).expect("read");
    assert_eq!(
        (stored.metadata.name.as_str(), content.as_slice()),
        ("nota.txt", b"plain upload\n".as_slice())
    );
    assert_eq!(stored.created_at, i64::try_from(EPOCH).expect("small"));
    assert_eq!(store.released("team_1", upload, None), Ok(true));
    assert_eq!(store.released("team_1", web, None), Ok(false));
    assert_eq!(
        store.released("team_1", web, Some(&"a".repeat(32))),
        Ok(true)
    );
    // The copy of the web bytes under another name carries the mark too.
    let copy = listing
        .files
        .iter()
        .find(|file| file.metadata.name == "copia.bin")
        .expect("copy");
    assert_eq!(copy.metadata.media_type, "application/octet-stream");
    assert_eq!(store.released("team_1", &copy.metadata.id, None), Ok(false));
}

#[test]
fn a_rust_team_files_database_has_the_python_schema() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (storage, store) = team_files(root.path());
    store
        .put("team_2", "x.bin", b"x", None, false)
        .expect("stored");
    let schema = |team: &str| -> Vec<(String, String, Option<String>)> {
        let connection =
            Connection::open(storage.join(team).join("files.sqlite3")).expect("opened");
        let mut statement = connection
            .prepare("SELECT type,name,sql FROM sqlite_master ORDER BY rowid")
            .expect("prepared");
        let rows = statement
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .expect("queried")
            .collect::<rusqlite::Result<Vec<_>>>()
            .expect("read");
        let version: i64 = connection
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .expect("pragma");
        assert_eq!(version, 2);
        rows
    };
    assert_eq!(schema("team_2"), schema("team_1"));
}
