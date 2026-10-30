use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use rusqlite::Connection;
use team_core::testing::{CountingEntropy, FakeClock};

use super::{
    DATABASE_HEADROOM_BYTES, FileStore, MAX_FILES, QuotaResolver, SCHEMA_VERSION, StorageError,
    StorageErrorKind, UNREFERENCED_GRACE_SECONDS, database,
};

const SUPERVISOR: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PDF: &[u8] = b"%PDF-1.7 from the web";
const GRACE: Duration = Duration::from_secs(UNREFERENCED_GRACE_SECONDS.unsigned_abs());
const ALMOST: Duration = Duration::from_secs(UNREFERENCED_GRACE_SECONDS.unsigned_abs() - 1);

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    clock: Arc<FakeClock>,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().expect("temporary directory");
        Self {
            root: directory.path().join("teams"),
            _directory: directory,
            clock: Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_secs(1_000_000))),
        }
    }

    fn store(&self, limit: i64) -> FileStore {
        self.store_with(limit, None)
    }

    fn store_with(&self, limit: i64, quota: Option<QuotaResolver>) -> FileStore {
        FileStore::new(
            self.root.clone(),
            limit,
            quota,
            self.clock.clone(),
            Arc::new(CountingEntropy::default()),
        )
        .expect("store")
    }

    fn referenced(&self, team: &str) -> Vec<String> {
        let path = self.root.join(team).join("files.sqlite3");
        if !path.exists() {
            return Vec::new();
        }
        let connection = Connection::open(path).expect("opened");
        let mut statement = connection
            .prepare("SELECT id FROM files WHERE referenced=1 ORDER BY id")
            .expect("prepared");
        statement
            .query_map([], |row| row.get(0))
            .expect("queried")
            .collect::<rusqlite::Result<Vec<String>>>()
            .expect("read")
    }
}

fn kind<T: std::fmt::Debug>(result: Result<T, StorageError>) -> StorageErrorKind {
    result.expect_err("refused").kind
}

fn ids(store: &FileStore, team: &str) -> Vec<String> {
    store
        .list(team)
        .expect("listed")
        .files
        .into_iter()
        .map(|file| file.metadata.id)
        .collect()
}

#[test]
fn round_trip_is_opaque_and_isolated_by_team() {
    let fixture = Fixture::new();
    let store = fixture.store(128);
    let first = store
        .put(
            "alpha",
            "brief.txt",
            b"confidential",
            Some("text/plain"),
            false,
        )
        .expect("stored");
    let second = store
        .put("beta", "brief.txt", b"different", Some("text/plain"), false)
        .expect("stored");
    let (metadata, content) = store.get("alpha", &first.file.metadata.id).expect("read");
    assert_eq!(content, b"confidential");
    assert_eq!(metadata.metadata.name, "brief.txt");
    assert_ne!(first.file.metadata.id, second.file.metadata.id);
    assert_eq!(
        kind(store.get("beta", &first.file.metadata.id)),
        StorageErrorKind::NotFound
    );
    let selected = store
        .metadata("alpha", &[&first.file.metadata.id], None)
        .expect("selected");
    assert_eq!(selected, vec![metadata.metadata.clone()]);
    assert_eq!(
        kind(store.metadata("beta", &[&first.file.metadata.id], None)),
        StorageErrorKind::NotFound
    );
    let team = fixture.root.join("alpha");
    let entries: Vec<String> = fs::read_dir(&team)
        .expect("listed")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .into_string()
                .expect("UTF-8")
        })
        .collect();
    assert_eq!(entries, ["files.sqlite3"]);
    assert_eq!(fs::metadata(&team).expect("stat").mode() & 0o7777, 0o700);
    assert_eq!(
        fs::metadata(team.join("files.sqlite3"))
            .expect("stat")
            .mode()
            & 0o7777,
        0o600
    );
}

#[test]
fn an_upload_answer_is_the_exact_protocol_upload_shape() {
    let fixture = Fixture::new();
    let store = fixture.store(128);
    let stored = store
        .put(
            "alpha",
            "brief.txt",
            b"confidential",
            Some("TEXT/Plain"),
            false,
        )
        .expect("stored");
    let answer = team_protocol::python::json::dumps(
        &stored.to_value(),
        team_protocol::python::json::CANONICAL,
    );
    let expected = format!(
        r#"{{"created_at":1000000,"id":"{}","limit_bytes":128,"media_type":"text/plain","name":"brief.txt","remaining_bytes":116,"sha256":"{}","size":12,"used_bytes":12}}"#,
        stored.file.metadata.id, stored.file.metadata.sha256
    );
    assert_eq!(answer, expected);
    let wrapped = strict_json::Value::Object(vec![
        (
            "team_id".to_owned(),
            strict_json::Value::String("alpha".to_owned()),
        ),
        ("file".to_owned(), stored.to_value()),
    ]);
    assert!(
        team_protocol::payload::project_storage_response(&wrapped, "upload", "alpha").is_some()
    );
    assert_eq!(store.list("alpha").expect("listed").files, [stored.file]);
}

#[test]
fn exact_content_quota_is_transactional() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    store
        .put("alpha", "first.bin", b"1234", None, false)
        .expect("stored");
    let exact = store
        .put("alpha", "second.bin", b"567890", None, false)
        .expect("stored");
    assert_eq!(
        (exact.usage.used_bytes, exact.usage.remaining_bytes),
        (10, 0)
    );
    let overflow = store.put("alpha", "overflow.bin", b"x", None, false);
    assert_eq!(overflow, Err(super::QUOTA_EXCEEDED));
    assert_eq!(
        overflow.expect_err("refused").to_string(),
        "Team storage quota exceeded"
    );
    let listing = store.list("alpha").expect("listed");
    assert_eq!((listing.usage.used_bytes, listing.files.len()), (10, 2));
}

#[test]
fn quota_resolver_is_team_scoped_and_a_downgrade_keeps_cleanup_available() {
    let fixture = Fixture::new();
    let limits = Arc::new(Mutex::new(HashMap::from([
        ("alpha".to_owned(), 8_i64),
        ("beta".to_owned(), 8),
    ])));
    let resolver: QuotaResolver = {
        let limits = Arc::clone(&limits);
        Arc::new(move |team| limits.lock().expect("limits")[team])
    };
    let store = fixture.store_with(1, Some(resolver));
    let before = store
        .put("alpha", "before.bin", b"12345678", None, false)
        .expect("stored");
    assert_eq!(before.usage.limit_bytes, 8);
    limits.lock().expect("limits").insert("alpha".to_owned(), 4);
    let listing = store.list("alpha").expect("listed");
    assert_eq!(
        (listing.usage.used_bytes, listing.usage.remaining_bytes),
        (8, 0)
    );
    assert_eq!(
        kind(store.put("alpha", "blocked.bin", b"x", None, false)),
        StorageErrorKind::Quota
    );
    // Collection still frees the quota under the smaller plan.
    fixture.clock.advance(GRACE);
    let replacement = store
        .put("alpha", "within.bin", b"1234", None, false)
        .expect("stored");
    assert_eq!(replacement.usage.remaining_bytes, 0);
    limits.lock().expect("limits").insert("beta".to_owned(), 0);
    assert_eq!(kind(store.list("beta")), StorageErrorKind::Unsafe);
}

#[test]
fn concurrent_writes_cannot_overbook_quota() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let results: Vec<bool> = std::thread::scope(|scope| {
        let writers: Vec<_> = (0..2)
            .map(|index| {
                let store = &store;
                scope.spawn(move || {
                    store
                        .put("alpha", &format!("{index}.bin"), b"123456", None, false)
                        .is_ok()
                })
            })
            .collect();
        writers
            .into_iter()
            .map(|writer| writer.join().expect("joined"))
            .collect()
    });
    let mut sorted = results;
    sorted.sort_unstable();
    assert_eq!(sorted, [false, true]);
    assert_eq!(store.list("alpha").expect("listed").usage.used_bytes, 6);
}

#[test]
fn destroy_is_scoped() {
    let fixture = Fixture::new();
    let store = fixture.store(8);
    store
        .put("alpha", "alpha.bin", b"12345678", None, false)
        .expect("stored");
    let beta = store
        .put("beta", "beta.bin", b"abcdefgh", None, false)
        .expect("stored");
    assert_eq!(store.destroy("alpha"), Ok(true));
    assert_eq!(store.destroy("alpha"), Ok(false));
    assert_eq!(
        store.get("beta", &beta.file.metadata.id).expect("read").1,
        b"abcdefgh"
    );
    store
        .put("orphan", "orphan.bin", b"x", None, false)
        .expect("stored");
    assert_eq!(store.destroy_all(), Ok(2));
    assert_eq!(store.list("beta").expect("listed").files, []);
    assert_eq!(store.list("orphan").expect("listed").files, []);
    // A foreign entry on the dedicated volume is refused, never removed as a Team.
    fs::create_dir(fixture.root.join("INVALID")).expect("created");
    assert_eq!(kind(store.destroy_all()), StorageErrorKind::Unsafe);
    assert!(fixture.root.join("INVALID").exists());
}

#[test]
fn file_count_and_metadata_are_bounded() {
    let fixture = Fixture::new();
    let store = fixture.store(MAX_FILES + 1);
    for index in 0..MAX_FILES {
        store
            .put(
                "alpha",
                &format!("{index}.txt"),
                b"x",
                Some("text/plain"),
                false,
            )
            .expect("stored");
    }
    let refused = store.put("alpha", "one-too-many.txt", b"x", None, false);
    assert_eq!(
        refused.expect_err("refused").message,
        "Team file count limit reached"
    );
    for (name, message) in [
        ("", "filename must be non-empty and trimmed"),
        (" ../secret", "filename must be non-empty and trimmed"),
        ("../secret", "filename must not contain a path"),
        ("..", "filename must not contain a path"),
        ("nested/file", "filename must not contain a path"),
        ("back\\slash", "filename must not contain a path"),
        ("line\u{7}bell", "filename contains control characters"),
        ("del\u{7f}", "filename contains control characters"),
    ] {
        let refused = store
            .put("beta", name, b"x", None, false)
            .expect_err("refused");
        assert_eq!(
            (refused.kind, refused.message),
            (StorageErrorKind::Input, message),
            "{name:?}"
        );
    }
}

#[test]
fn file_names_and_media_types_fail_closed_with_their_messages() {
    let fixture = Fixture::new();
    let store = fixture.store(64);
    let long = "x".repeat(256);
    assert_eq!(
        store
            .put("beta", &long, b"x", None, false)
            .expect_err("refused")
            .message,
        "filename is too long"
    );
    for media_type in [
        "text/plain; charset=utf-8",
        "text",
        "te\u{212a}t/plain",
        "x/\u{e9}",
    ] {
        assert_eq!(
            store
                .put("beta", "safe.txt", b"x", Some(media_type), false)
                .expect_err("refused")
                .message,
            "invalid media type",
            "{media_type}"
        );
    }
    let empty = store
        .put("beta", "safe.txt", b"x", Some(""), false)
        .expect("stored");
    assert_eq!(empty.file.metadata.media_type, "application/octet-stream");
}

#[test]
fn selected_metadata_reuses_one_bounded_reader() {
    let fixture = Fixture::new();
    let store = fixture.store(128);
    let selected = store
        .put(
            "alpha",
            "selected.txt",
            b"selected",
            Some("text/plain"),
            false,
        )
        .expect("stored");
    store
        .put("alpha", "other.txt", b"other", Some("text/plain"), false)
        .expect("stored");
    let id = selected.file.metadata.id.as_str();
    let reader = store
        .metadata_reader("alpha", &[id])
        .expect("opened")
        .expect("a reader");
    let first = store.metadata("alpha", &[id], Some(&reader)).expect("read");
    // The reader stays usable after the Team's directory entry is gone: it is one open connection.
    let second = store.metadata("alpha", &[id], Some(&reader)).expect("read");
    assert_eq!(first, second);
    assert_eq!(first[0].name, "selected.txt");
    assert_eq!(
        store
            .metadata("beta", &[id], Some(&reader))
            .expect_err("refused")
            .message,
        "metadata reader belongs to another Team"
    );
    assert!(store.metadata_reader("alpha", &[]).expect("none").is_none());
    assert_eq!(store.metadata("alpha", &[], None), Ok(Vec::new()));
    let missing = "0".repeat(32);
    let nine = vec![missing.as_str(); 9];
    assert_eq!(
        kind(store.metadata("alpha", &nine, None)),
        StorageErrorKind::Input
    );
    assert_eq!(
        kind(store.metadata("alpha", &[&missing, &missing], None)),
        StorageErrorKind::Input
    );
    assert_eq!(
        kind(store.get("alpha", "not-an-id")),
        StorageErrorKind::NotFound
    );
}

#[test]
fn database_page_ceiling_and_integrity_check_fail_closed() {
    let fixture = Fixture::new();
    let store = fixture.store(64);
    let stored = store
        .put("alpha", "safe.bin", b"safe", None, false)
        .expect("stored");
    let connection = Connection::open(fixture.root.join("alpha/files.sqlite3")).expect("opened");
    let pragma = |name: &str| -> i64 {
        connection
            .query_row(&format!("PRAGMA {name}"), [], |row| row.get(0))
            .expect("pragma")
    };
    let page_size = pragma("page_size");
    assert_eq!(pragma("user_version"), SCHEMA_VERSION);
    let expected = (64 + DATABASE_HEADROOM_BYTES + page_size - 1) / page_size;
    // max_page_count is per connection: Team's own connection applies it; another sees SQLite's default.
    let applied = store.connect("alpha", false, 64).expect("connected");
    assert_eq!(
        applied
            .query_row("PRAGMA max_page_count", [], |row| row.get::<_, i64>(0))
            .expect("pragma"),
        expected
    );
    let journal: String = applied
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .expect("pragma");
    assert_eq!(journal, "delete");
    connection
        .execute(
            "UPDATE files SET content=? WHERE id=?",
            (b"evil".as_slice(), &stored.file.metadata.id),
        )
        .expect("tampered");
    assert_eq!(
        store
            .get("alpha", &stored.file.metadata.id)
            .expect_err("refused")
            .message,
        "stored file failed its integrity check"
    );
}

#[test]
fn unsafe_storage_shapes_are_rejected() {
    let fixture = Fixture::new();
    fs::create_dir(&fixture.root).expect("created");
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o700)).expect("mode set");
    let team = fixture.root.join("alpha");
    fs::create_dir(&team).expect("created");
    fs::set_permissions(&team, fs::Permissions::from_mode(0o700)).expect("mode set");
    let target = fixture.root.join("target");
    fs::write(&target, b"outside").expect("written");
    symlink(&target, team.join("files.sqlite3")).expect("linked");
    let store = fixture.store(64);
    assert_eq!(
        kind(store.put("alpha", "safe.bin", b"safe", None, false)),
        StorageErrorKind::Unsafe
    );
    assert_eq!(fs::read(&target).expect("read"), b"outside");
}

#[test]
fn identifiers_limits_and_file_inputs_fail_closed() {
    let fixture = Fixture::new();
    let new = |limit| {
        FileStore::new(
            fixture.root.clone(),
            limit,
            None,
            fixture.clock.clone(),
            Arc::new(CountingEntropy::default()),
        )
    };
    assert_eq!(
        new(0).map(|_| ()).expect_err("refused").kind,
        StorageErrorKind::Unsafe
    );
    let store = fixture.store(4);
    assert_eq!(
        store.list("INVALID").expect_err("refused").message,
        "invalid Team id"
    );
    assert_eq!(
        store
            .put("alpha", "empty.bin", b"", None, false)
            .expect_err("refused")
            .message,
        "file must contain bytes"
    );
    assert_eq!(
        kind(store.put("alpha", "large.bin", b"12345", None, false)),
        StorageErrorKind::Quota
    );
}

#[test]
fn storage_paths_reject_unavailable_and_unsafe_directories() {
    let fixture = Fixture::new();
    fs::create_dir(&fixture.root).expect("created");
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o740)).expect("mode set");
    let new = || {
        FileStore::new(
            fixture.root.clone(),
            64,
            None,
            fixture.clock.clone(),
            Arc::new(CountingEntropy::default()),
        )
    };
    assert_eq!(
        new().map(|_| ()).expect_err("refused").message,
        "storage root has unsafe ownership or permissions"
    );
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o700)).expect("mode set");
    let store = new().expect("store");
    let team = fixture.root.join("alpha");
    fs::create_dir(&team).expect("created");
    fs::set_permissions(&team, fs::Permissions::from_mode(0o740)).expect("mode set");
    let unsafe_team = "Team storage has unsafe ownership or permissions";
    assert_eq!(
        store.list("alpha").expect_err("refused").message,
        unsafe_team
    );
    assert_eq!(
        store.destroy("alpha").expect_err("refused").message,
        unsafe_team
    );
    fs::set_permissions(&team, fs::Permissions::from_mode(0o700)).expect("mode set");
    assert_eq!(
        store.connect("alpha", false, 64).map(|_| ()),
        Err(super::TEAM_NOT_FOUND)
    );
    assert!(!team.join("files.sqlite3").exists());
    // A root that is a file is unavailable.
    let file_root = fixture.root.join("file");
    fs::write(&file_root, b"").expect("written");
    let refused = FileStore::new(
        file_root,
        64,
        None,
        fixture.clock.clone(),
        Arc::new(CountingEntropy::default()),
    );
    assert_eq!(
        refused.map(|_| ()).expect_err("refused").message,
        "storage root is unavailable"
    );
}

#[test]
fn database_errors_are_mapped_without_leaking_sqlite_failures() {
    let full =
        rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_FULL), None);
    assert_eq!(database(&full), super::QUOTA_EXCEEDED);
    let corrupt = rusqlite::Error::SqliteFailure(
        rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CORRUPT),
        None,
    );
    assert_eq!(
        database(&corrupt).message,
        "Team storage transaction failed"
    );
    let fixture = Fixture::new();
    let store = fixture.store(64);
    store
        .put("alpha", "x.bin", b"x", None, false)
        .expect("stored");
    fs::write(
        fixture.root.join("alpha/files.sqlite3"),
        b"not a database at all, just bytes",
    )
    .expect("written");
    for refused in [
        store.list("alpha").map(|_| ()),
        store.settle("alpha", &[]),
        store
            .put("alpha", "safe.bin", b"safe", None, false)
            .map(|_| ()),
    ] {
        assert_eq!(
            refused.expect_err("refused").message,
            "Team storage transaction failed"
        );
    }
}

#[test]
fn a_database_of_another_schema_is_refused_and_never_upgraded() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let directory = fixture.root.join("alpha");
    fs::create_dir(&directory).expect("created");
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).expect("mode set");
    let path = directory.join("files.sqlite3");
    Connection::open(&path)
        .expect("opened")
        .execute_batch("CREATE TABLE files (id TEXT PRIMARY KEY)")
        .expect("created");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("mode set");
    assert_eq!(
        store.list("alpha").expect_err("refused").message,
        "Team storage schema is not current"
    );
    let connection = Connection::open(&path).expect("opened");
    let version: i64 = connection
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .expect("pragma");
    assert_eq!(version, 0);
    connection
        .execute_batch("PRAGMA user_version=99")
        .expect("set");
    assert_eq!(
        store
            .put("alpha", "x.bin", b"x", None, false)
            .expect_err("refused")
            .message,
        "Team storage schema is not current"
    );
}

#[test]
fn the_schema_is_the_current_ddl_verbatim() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    store
        .put("alpha", "x.bin", b"x", None, false)
        .expect("stored");
    let connection = Connection::open(fixture.root.join("alpha/files.sqlite3")).expect("opened");
    let mut statement = connection
        .prepare("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY rowid")
        .expect("prepared");
    let rows: Vec<(String, String, Option<String>)> = statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(3)?)))
        .expect("queried")
        .collect::<rusqlite::Result<_>>()
        .expect("read");
    let trimmed = |sql: &str| sql.trim_start().to_owned();
    assert_eq!(
        rows,
        [
            (
                "table".to_owned(),
                "files".to_owned(),
                Some(trimmed(super::FILES_TABLE))
            ),
            (
                "index".to_owned(),
                "sqlite_autoindex_files_1".to_owned(),
                None
            ),
            (
                "index".to_owned(),
                "sqlite_autoindex_files_2".to_owned(),
                None
            ),
            (
                "table".to_owned(),
                "acknowledgements".to_owned(),
                Some(trimmed(super::ACKNOWLEDGEMENTS_TABLE))
            ),
            (
                "index".to_owned(),
                "sqlite_autoindex_acknowledgements_1".to_owned(),
                None
            ),
        ]
    );
}

#[test]
fn an_unreferenced_upload_is_collected_exactly_when_its_grace_ends() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let stored = store
        .put("alpha", "draft.txt", b"draft", Some("text/plain"), false)
        .expect("stored");
    fixture.clock.advance(ALMOST);
    assert_eq!(ids(&store, "alpha"), vec![stored.file.metadata.id.clone()]);
    fixture.clock.advance(Duration::from_secs(1));
    assert_eq!(ids(&store, "alpha"), Vec::<String>::new());
    assert_eq!(
        kind(store.get("alpha", &stored.file.metadata.id)),
        StorageErrorKind::NotFound
    );
}

#[test]
fn the_sweep_is_idempotent_and_scoped_to_its_team() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let alpha = store
        .put("alpha", "a.txt", b"alpha", Some("text/plain"), false)
        .expect("stored");
    let beta = store
        .put("beta", "b.txt", b"beta", Some("text/plain"), false)
        .expect("stored");
    store
        .reference("beta", &[&beta.file.metadata.id])
        .expect("referenced");
    fixture.clock.advance(GRACE);
    assert_eq!(ids(&store, "alpha"), Vec::<String>::new());
    assert_eq!(ids(&store, "alpha"), Vec::<String>::new());
    assert_eq!(ids(&store, "absent"), Vec::<String>::new());
    assert_eq!(ids(&store, "beta"), vec![beta.file.metadata.id.clone()]);
    assert_eq!(
        kind(store.get("alpha", &alpha.file.metadata.id)),
        StorageErrorKind::NotFound
    );
    // A reference names only the Team's own files.
    assert_eq!(
        kind(store.reference("alpha", &[&beta.file.metadata.id])),
        StorageErrorKind::NotFound
    );
    assert_eq!(fixture.referenced("alpha"), Vec::<String>::new());
}

#[test]
fn a_referenced_file_stays_until_a_later_turn_releases_it_and_then_its_grace() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let sent = store
        .put("alpha", "sent.txt", b"sent", Some("text/plain"), false)
        .expect("stored");
    let id = sent.file.metadata.id.as_str();
    store.reference("alpha", &[id]).expect("referenced");
    store.settle("alpha", &[id]).expect("settled");
    fixture.clock.advance(GRACE * 10);
    assert_eq!(ids(&store, "alpha"), [id]);
    assert_eq!(fixture.referenced("alpha"), [id]);
    // A later turn without the file releases it; its grace starts at that release, not at its upload.
    store.settle("alpha", &[]).expect("settled");
    assert_eq!(fixture.referenced("alpha"), Vec::<String>::new());
    fixture.clock.advance(ALMOST);
    assert_eq!(ids(&store, "alpha"), [id]);
    fixture.clock.advance(Duration::from_secs(1));
    store.settle("alpha", &[]).expect("settled");
    assert_eq!(ids(&store, "alpha"), Vec::<String>::new());
}

#[test]
fn a_new_turn_adds_to_the_previous_references_until_it_completes() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let first = store
        .put("alpha", "first.txt", b"1", Some("text/plain"), false)
        .expect("stored")
        .file
        .metadata
        .id;
    let second = store
        .put("alpha", "second.txt", b"2", Some("text/plain"), false)
        .expect("stored")
        .file
        .metadata
        .id;
    store.reference("alpha", &[&first]).expect("referenced");
    store.settle("alpha", &[&first]).expect("settled");
    store.reference("alpha", &[&second]).expect("referenced");
    let mut both = vec![first.clone(), second.clone()];
    both.sort();
    assert_eq!(fixture.referenced("alpha"), both);
    store.settle("alpha", &[&second]).expect("settled");
    assert_eq!(fixture.referenced("alpha"), [second]);
    assert_eq!(store.reference("alpha", &[]), Ok(Vec::new()));
    assert_eq!(store.settle("absent", &[]), Ok(()));
}

#[test]
fn reference_reports_what_it_added_and_release_returns_only_that() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let held = store
        .put("alpha", "held.txt", b"1", Some("text/plain"), false)
        .expect("stored")
        .file
        .metadata
        .id;
    let fresh = store
        .put("alpha", "fresh.txt", b"2", Some("text/plain"), false)
        .expect("stored")
        .file
        .metadata
        .id;
    assert_eq!(store.reference("alpha", &[&held]), Ok(vec![held.clone()]));
    assert_eq!(
        store.reference("alpha", &[&held, &fresh]),
        Ok(vec![fresh.clone()])
    );
    fixture.clock.advance(Duration::from_secs(100));
    store.release("alpha", &[&fresh]).expect("released");
    store.release("alpha", &[]).expect("released");
    store.release("absent", &[&fresh]).expect("released");
    assert_eq!(fixture.referenced("alpha"), vec![held.clone()]);
    // The released file's grace starts at its release.
    fixture.clock.advance(ALMOST);
    let mut both = vec![held.clone(), fresh];
    both.sort();
    let mut listed = ids(&store, "alpha");
    listed.sort();
    assert_eq!(listed, both);
    fixture.clock.advance(Duration::from_secs(1));
    assert_eq!(ids(&store, "alpha"), [held]);
}

#[test]
fn a_collected_file_cannot_be_referenced_by_a_turn() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let stored = store
        .put("alpha", "late.txt", b"late", Some("text/plain"), false)
        .expect("stored");
    fixture.clock.advance(GRACE);
    assert_eq!(ids(&store, "alpha"), Vec::<String>::new());
    assert_eq!(
        kind(store.reference("alpha", &[&stored.file.metadata.id])),
        StorageErrorKind::NotFound
    );
}

#[test]
fn an_identical_upload_reuses_the_stored_file_without_charging_quota() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let stored = store
        .put("alpha", "same.txt", b"123456", Some("text/plain"), false)
        .expect("stored");
    fixture.clock.advance(ALMOST);
    let again = store
        .put("alpha", "same.txt", b"123456", Some("text/plain"), false)
        .expect("stored");
    assert_eq!(again.file, stored.file);
    assert_eq!(
        (again.usage.used_bytes, again.usage.remaining_bytes),
        (6, 4)
    );
    // The identical upload restarts the grace period.
    fixture.clock.advance(ALMOST);
    assert_eq!(ids(&store, "alpha"), vec![stored.file.metadata.id.clone()]);
    // A different name or type is a different file, charged on its own.
    assert_eq!(
        kind(store.put("alpha", "other.txt", b"123456", Some("text/plain"), false)),
        StorageErrorKind::Quota
    );
    let beta = store
        .put("beta", "same.txt", b"123456", Some("text/plain"), false)
        .expect("stored");
    assert_eq!(beta.usage.used_bytes, 6);
}

#[test]
fn reuse_keeps_a_referenced_file_referenced_and_a_full_quota_still_admits_it() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    let stored = store
        .put("alpha", "full.bin", b"1234567890", None, false)
        .expect("stored");
    store
        .reference("alpha", &[&stored.file.metadata.id])
        .expect("referenced");
    let again = store
        .put("alpha", "full.bin", b"1234567890", None, false)
        .expect("stored");
    assert_eq!(again.file.metadata.id, stored.file.metadata.id);
    assert_eq!(fixture.referenced("alpha"), [stored.file.metadata.id]);
}

#[test]
fn quota_freed_by_collection_admits_the_next_upload() {
    let fixture = Fixture::new();
    let store = fixture.store(10);
    store
        .put("alpha", "old.bin", b"1234567890", None, false)
        .expect("stored");
    assert_eq!(
        kind(store.put("alpha", "new.bin", b"x", None, false)),
        StorageErrorKind::Quota
    );
    fixture.clock.advance(GRACE);
    let fresh = store
        .put("alpha", "new.bin", b"x", None, false)
        .expect("stored");
    assert_eq!(fresh.usage.used_bytes, 1);
    assert_eq!(ids(&store, "alpha"), [fresh.file.metadata.id]);
}

#[derive(Debug, PartialEq)]
enum Refusal {
    Audit,
    Storage(StorageError),
}

impl From<StorageError> for Refusal {
    fn from(error: StorageError) -> Self {
        Self::Storage(error)
    }
}

fn acknowledge(store: &FileStore, file: &str, who: &str) -> Result<(), StorageError> {
    store.acknowledge("team_1", file, who, |_| Ok::<(), StorageError>(()))
}

#[test]
fn a_web_mark_survives_an_identical_upload_and_goes_with_its_file() {
    let fixture = Fixture::new();
    let store = fixture.store(super::DEFAULT_LIMIT_BYTES);
    let stored = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    let id = stored.file.metadata.id.as_str();
    let again = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), false)
        .expect("stored");
    assert_eq!(again.file.metadata.id, id);
    assert_eq!(store.released("team_1", id, None), Ok(false));
    acknowledge(&store, id, SUPERVISOR).expect("acknowledged");
    assert_eq!(store.released("team_1", id, Some(SUPERVISOR)), Ok(true));
    assert_eq!(
        store.released("team_1", id, Some(&"b".repeat(32))),
        Ok(false)
    );
    let upload = store
        .put("team_1", "nota.txt", b"plain", Some("text/plain"), false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(true)
    );
    let not_web = acknowledge(&store, &upload.file.metadata.id, SUPERVISOR).expect_err("refused");
    assert_eq!(
        (not_web.kind, not_web.message),
        (
            StorageErrorKind::NotWeb,
            "the file did not come from the web"
        )
    );
    // Collected with its file, the acknowledgement is gone too.
    fixture.clock.advance(GRACE + Duration::from_secs(1));
    store.settle("team_1", &[]).expect("settled");
    assert_eq!(
        kind(store.released("team_1", id, Some(SUPERVISOR))),
        StorageErrorKind::NotFound
    );
    assert_eq!(
        kind(acknowledge(&store, id, SUPERVISOR)),
        StorageErrorKind::NotFound
    );
}

#[test]
fn the_web_mark_follows_the_bytes_whatever_their_name() {
    let fixture = Fixture::new();
    let store = fixture.store(super::DEFAULT_LIMIT_BYTES);
    let upload = store
        .put("team_1", "nota.pdf", PDF, Some("application/pdf"), false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(true)
    );
    store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    // The earlier upload of the same bytes is marked with them, and so is a later one under any name.
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(false)
    );
    let later = store
        .put("team_1", "copia.bin", PDF, None, false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &later.file.metadata.id, None),
        Ok(false)
    );
    let other = store
        .put(
            "team_1",
            "outro.txt",
            b"other bytes",
            Some("text/plain"),
            false,
        )
        .expect("stored");
    assert_eq!(
        store.released("team_1", &other.file.metadata.id, None),
        Ok(true)
    );
}

#[test]
fn no_acknowledgement_exists_without_its_audit_or_outside_its_form() {
    let fixture = Fixture::new();
    let store = fixture.store(super::DEFAULT_LIMIT_BYTES);
    assert_eq!(
        kind(store.released("team_1", &"f".repeat(32), Some(SUPERVISOR))),
        StorageErrorKind::NotFound
    );
    let stored = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    let id = stored.file.metadata.id.as_str();
    let audited = store.acknowledge("team_1", id, SUPERVISOR, |file| {
        assert_eq!(file, id);
        Err(Refusal::Audit)
    });
    assert_eq!(audited, Err(Refusal::Audit));
    assert_eq!(store.released("team_1", id, Some(SUPERVISOR)), Ok(false));
    for who in ["", &"x".repeat(129), "a\nb"] {
        assert_eq!(
            kind(acknowledge(&store, id, who)),
            StorageErrorKind::Input,
            "{who:?}"
        );
        assert_eq!(
            kind(store.released("team_1", id, Some(who))),
            StorageErrorKind::Input,
            "{who:?}"
        );
    }
    // A Team whose files are gone has nothing to acknowledge, and nothing is created for it.
    store.destroy("team_1").expect("destroyed");
    assert_eq!(
        kind(acknowledge(&store, id, SUPERVISOR)),
        StorageErrorKind::NotFound
    );
    assert!(!fixture.root.join("team_1").exists());
}
