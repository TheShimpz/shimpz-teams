//! Team-owned, quota-bounded file storage: one SQLite blob database per Team, `<root>/<team_id>/files.sqlite3`.
//!
//! The Brain and Assistant containers never mount this directory. Files are opaque blobs reached only through named
//! controller operations, so uploaded bytes cannot be executed or traversed as paths by tenant workloads.
//!
//! A file is kept while the Team's current chat turn references it: from the moment a turn selects it until a later
//! turn completes without it or the Team's conversation state is purged. An unreferenced file is collected once it has
//! been idle for the grace period, so a file removed from a message by mistake can be uploaded again within it and
//! reuses the stored copy without charging the quota twice (ADR-0093, amended 2026-10-03).
//!
//! A file the Browser downloaded is marked as from the web in its own row, so its marker exists exactly as long as the
//! file does. The mark follows the bytes: every file of the Team with the same digest carries it, and a later upload of
//! the same bytes is marked too. Its original leaves Team only for a Supervisor who acknowledged that exact file, and
//! each acknowledgement goes with its file (ADR-0117 §7).
//!
//! The schema (`user_version` 2, STRICT tables) and every pragma are the current format, byte for byte; any other
//! schema is refused, never upgraded.

use std::fs::OpenOptions;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use rusqlite::{Connection, ErrorCode, OpenFlags, OptionalExtension, TransactionBehavior, params};
use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_core::clock::Clock;
use team_core::entropy::{Entropy, token_hex};
use team_protocol::identifiers::canonical_team_id;
use team_protocol::payload::canonical_media_type;
use team_protocol::payload::{FILE_ID_RE, MAX_FILENAME_BYTES, MAX_MEDIA_TYPE_CHARS};
use team_protocol::python::text;

use crate::custody::{self, DirectoryCustody, Facts, FileCustody, Mode, current_uid};
use crate::private::epoch_seconds;

/// A Team's content quota when no resolver states another.
pub const DEFAULT_LIMIT_BYTES: i64 = 100 * 1024 * 1024;
/// How long an unreferenced file stays after its upload, an identical upload, or the turn that last referenced it.
pub const UNREFERENCED_GRACE_SECONDS: i64 = 24 * 60 * 60;
/// The schema version (`PRAGMA user_version`).
pub const SCHEMA_VERSION: i64 = 2;
/// The pages a database may hold beyond its content quota.
pub const DATABASE_HEADROOM_BYTES: i64 = 8 * 1024 * 1024;
/// The most files one Team holds.
pub const MAX_FILES: i64 = 256;
/// The most acknowledgements one Team holds.
pub const MAX_ACKNOWLEDGEMENTS: i64 = 4_096;
/// The most files one turn selects.
pub const MAX_SELECTED: usize = 8;

const FILES_TABLE: &str = "
                    CREATE TABLE files (
                        id TEXT PRIMARY KEY CHECK(length(id) = 32),
                        name TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        size INTEGER NOT NULL CHECK(size > 0),
                        sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                        created_at INTEGER NOT NULL,
                        referenced INTEGER NOT NULL CHECK(referenced IN (0, 1)),
                        idle_since INTEGER NOT NULL,
                        web INTEGER NOT NULL CHECK(web IN (0, 1)),
                        content BLOB NOT NULL,
                        UNIQUE(sha256, name, media_type)
                    ) STRICT
                    ";
const ACKNOWLEDGEMENTS_TABLE: &str = "
                    CREATE TABLE acknowledgements (
                        file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                        principal TEXT NOT NULL CHECK(length(principal) BETWEEN 1 AND 128),
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY (file_id, principal)
                    ) STRICT
                    ";
const METADATA_SELECTS: [&str; MAX_SELECTED] = [
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?,?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?,?,?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?,?,?,?,?)",
    "SELECT id,name,media_type,size,sha256 FROM files WHERE id IN (?,?,?,?,?,?,?,?)",
];

/// The class of a storage refusal; the HTTP boundary maps each to its answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageErrorKind {
    /// The storage boundary could not prove a safe operation.
    Unsafe,
    /// The write would exceed the Team's fixed content quota or one of its counts.
    Quota,
    /// The file, or the Team's storage, does not exist.
    NotFound,
    /// Client-supplied file metadata or content is invalid.
    Input,
    /// The file did not come from the web, so it has nothing to acknowledge.
    NotWeb,
}

/// A storage refusal: its class and its fixed public message (never a path, an SQLite message, or content).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StorageError {
    /// Its class.
    pub kind: StorageErrorKind,
    /// Its message; the quota and input messages are shown to the person.
    pub message: &'static str,
}

impl StorageError {
    const fn new(kind: StorageErrorKind, message: &'static str) -> Self {
        Self { kind, message }
    }

    const fn unsafe_(message: &'static str) -> Self {
        Self::new(StorageErrorKind::Unsafe, message)
    }

    const fn input(message: &'static str) -> Self {
        Self::new(StorageErrorKind::Input, message)
    }
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for StorageError {}

const FILE_NOT_FOUND: StorageError =
    StorageError::new(StorageErrorKind::NotFound, "file not found");
const TEAM_NOT_FOUND: StorageError =
    StorageError::new(StorageErrorKind::NotFound, "Team storage not found");
const QUOTA_EXCEEDED: StorageError =
    StorageError::new(StorageErrorKind::Quota, "Team storage quota exceeded");
const TRANSACTION_FAILED: StorageError = StorageError::unsafe_("Team storage transaction failed");

/// One file's metadata.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileMetadata {
    /// Its opaque id, 32 hex digits.
    pub id: String,
    /// Its name.
    pub name: String,
    /// Its media type.
    pub media_type: String,
    /// Its size in bytes.
    pub size: i64,
    /// The SHA-256 of its bytes, in hex.
    pub sha256: String,
}

impl FileMetadata {
    fn members(&self) -> Vec<(String, Value)> {
        vec![
            ("id".to_owned(), text_value(&self.id)),
            ("name".to_owned(), text_value(&self.name)),
            ("media_type".to_owned(), text_value(&self.media_type)),
            ("size".to_owned(), int_value(self.size)),
            ("sha256".to_owned(), text_value(&self.sha256)),
        ]
    }

    /// `{"id","name","media_type","size","sha256"}`.
    #[must_use]
    pub fn to_value(&self) -> Value {
        Value::Object(self.members())
    }
}

/// One stored file's metadata with its creation time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredFile {
    /// Its metadata.
    pub metadata: FileMetadata,
    /// When it was first stored, in epoch seconds.
    pub created_at: i64,
}

impl StoredFile {
    fn members(&self) -> Vec<(String, Value)> {
        let mut members = self.metadata.members();
        members.push(("created_at".to_owned(), int_value(self.created_at)));
        members
    }

    /// `{"id","name","media_type","size","sha256","created_at"}`.
    #[must_use]
    pub fn to_value(&self) -> Value {
        Value::Object(self.members())
    }
}

/// A Team's content usage.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Usage {
    /// Bytes stored.
    pub used_bytes: i64,
    /// The Team's quota.
    pub limit_bytes: i64,
    /// What remains of it (never negative, even after a quota reduction).
    pub remaining_bytes: i64,
}

impl Usage {
    fn new(used_bytes: i64, limit_bytes: i64) -> Self {
        Self {
            used_bytes,
            limit_bytes,
            remaining_bytes: (limit_bytes - used_bytes).max(0),
        }
    }

    fn members(self) -> [(String, Value); 3] {
        [
            ("used_bytes".to_owned(), int_value(self.used_bytes)),
            ("limit_bytes".to_owned(), int_value(self.limit_bytes)),
            (
                "remaining_bytes".to_owned(),
                int_value(self.remaining_bytes),
            ),
        ]
    }
}

/// The answer to one upload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Upload {
    /// The stored, or reused, file.
    pub file: StoredFile,
    /// The Team's usage after it.
    pub usage: Usage,
}

impl Upload {
    /// The upload answer: the file's fields, then the usage fields.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut members = self.file.members();
        members.extend(self.usage.members());
        Value::Object(members)
    }
}

/// A Team's file listing.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Listing {
    /// Every file still kept, oldest first.
    pub files: Vec<StoredFile>,
    /// The Team's usage.
    pub usage: Usage,
}

impl Listing {
    /// `{"files":[…],"used_bytes","limit_bytes","remaining_bytes"}`.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut members = vec![(
            "files".to_owned(),
            Value::Array(self.files.iter().map(StoredFile::to_value).collect()),
        )];
        members.extend(self.usage.members());
        Value::Object(members)
    }
}

fn text_value(text: &str) -> Value {
    Value::String(text.to_owned())
}

fn int_value(number: i64) -> Value {
    Value::Number(Number::Int(number))
}

/// The quota of one Team, from a server-trusted resolver.
pub type QuotaResolver = Arc<dyn Fn(&str) -> i64 + Send + Sync>;

/// A connection held across one chat turn to read its selected files' metadata.
pub struct MetadataReader {
    team: String,
    connection: Connection,
}

impl std::fmt::Debug for MetadataReader {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("MetadataReader")
            .field("team", &self.team)
            .finish_non_exhaustive()
    }
}

/// The Team files of one controller volume.
pub struct FileStore {
    root: PathBuf,
    fixed_limit_bytes: i64,
    quota_for: Option<QuotaResolver>,
    clock: Arc<dyn Clock>,
    entropy: Arc<dyn Entropy>,
}

impl std::fmt::Debug for FileStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("FileStore")
            .field("root", &self.root)
            .field("fixed_limit_bytes", &self.fixed_limit_bytes)
            .finish_non_exhaustive()
    }
}

fn team_id(value: &str) -> Result<String, StorageError> {
    canonical_team_id(&Value::String(value.to_owned()))
        .map(str::to_owned)
        .ok_or(StorageError::unsafe_("invalid Team id"))
}

fn file_id(value: &str) -> Result<&str, StorageError> {
    if FILE_ID_RE.is_full(value) {
        Ok(value)
    } else {
        Err(FILE_NOT_FOUND)
    }
}

fn principal(value: &str) -> Result<&str, StorageError> {
    if (1..=128).contains(&value.chars().count()) && text::isprintable(value) {
        Ok(value)
    } else {
        Err(StorageError::input(
            "the acknowledging principal is invalid",
        ))
    }
}

fn filename(value: &str) -> Result<&str, StorageError> {
    if value.is_empty() || text::strip(value) != value {
        return Err(StorageError::input(
            "filename must be non-empty and trimmed",
        ));
    }
    if value.len() > MAX_FILENAME_BYTES {
        return Err(StorageError::input("filename is too long"));
    }
    if value == "." || value == ".." || value.contains('/') || value.contains('\\') {
        return Err(StorageError::input("filename must not contain a path"));
    }
    if value
        .chars()
        .any(|character| character < ' ' || character == '\u{7f}')
    {
        return Err(StorageError::input("filename contains control characters"));
    }
    Ok(value)
}

fn media_type(value: Option<&str>) -> Result<String, StorageError> {
    let invalid = StorageError::input("invalid media type");
    match value {
        None | Some("") => Ok("application/octet-stream".to_owned()),
        Some(text) if !text.is_ascii() || text.len() > MAX_MEDIA_TYPE_CHARS => Err(invalid),
        Some(text) => canonical_media_type(&Value::String(text.to_owned())).ok_or(invalid),
    }
}

fn selected(file_ids: &[&str]) -> Result<Vec<String>, StorageError> {
    if file_ids.len() > MAX_SELECTED {
        return Err(StorageError::input("at most 8 file ids may be selected"));
    }
    let mut ids: Vec<String> = Vec::with_capacity(file_ids.len());
    for id in file_ids {
        let id = file_id(id)?.to_owned();
        if ids.contains(&id) {
            return Err(StorageError::input("file ids must be unique"));
        }
        ids.push(id);
    }
    Ok(ids)
}

/// A failed SQLite operation: a full database is the quota, anything else an unproven transaction.
fn database(error: &rusqlite::Error) -> StorageError {
    match error.sqlite_error_code() {
        Some(ErrorCode::DiskFull) => QUOTA_EXCEEDED,
        _ => TRANSACTION_FAILED,
    }
}

fn sweep(connection: &Connection, now: i64) -> rusqlite::Result<()> {
    connection.execute(
        "DELETE FROM files WHERE referenced=0 AND idle_since<=?",
        [now - UNREFERENCED_GRACE_SECONDS],
    )?;
    Ok(())
}

fn usage(connection: &Connection) -> rusqlite::Result<(i64, i64)> {
    connection.query_row(
        "SELECT count(*), coalesce(sum(size), 0) FROM files",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
}

fn owner_only_directory(facts: &Facts) -> bool {
    DirectoryCustody {
        uid: Some(current_uid()),
        gid: None,
        mode: Mode::OwnerOnly,
    }
    .holds(facts)
}

impl FileStore {
    /// The store under `root`, created `0700` when absent, with a fixed quota or a per-Team resolver.
    ///
    /// # Errors
    /// [`StorageErrorKind::Unsafe`] for a non-positive quota or a root that is unavailable or not an owner-only
    /// directory of this process.
    pub fn new(
        root: PathBuf,
        limit_bytes: i64,
        quota_for: Option<QuotaResolver>,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, StorageError> {
        let store = Self {
            root,
            fixed_limit_bytes: validate_limit(limit_bytes)?,
            quota_for,
            clock,
            entropy,
        };
        store.ensure_root()?;
        Ok(store)
    }

    /// The store's root.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn ensure_root(&self) -> Result<(), StorageError> {
        crate::durable::create_private_directory(&self.root)
            .map_err(|_| StorageError::unsafe_("storage root is unavailable"))?;
        let facts = custody::facts(&self.root)
            .ok()
            .flatten()
            .ok_or(StorageError::unsafe_("storage root is unavailable"))?;
        if owner_only_directory(&facts) && facts.links >= 2 {
            Ok(())
        } else {
            Err(StorageError::unsafe_(
                "storage root has unsafe ownership or permissions",
            ))
        }
    }

    fn limit(&self, team: &str) -> Result<i64, StorageError> {
        match &self.quota_for {
            None => Ok(self.fixed_limit_bytes),
            Some(resolver) => validate_limit(resolver(team)),
        }
    }

    fn now(&self) -> i64 {
        epoch_seconds(self.clock.as_ref())
    }

    fn team_directory(&self, team: &str, create: bool) -> Result<PathBuf, StorageError> {
        let directory = self.root.join(team);
        if create {
            match std::fs::DirBuilder::new().mode(0o700).create(&directory) {
                Err(error) if error.kind() != std::io::ErrorKind::AlreadyExists => {
                    return Err(StorageError::unsafe_("Team storage is unavailable"));
                }
                _ => {}
            }
        }
        let facts = custody::facts(&directory)
            .map_err(|_| StorageError::unsafe_("Team storage is unavailable"))?
            .ok_or(TEAM_NOT_FOUND)?;
        if owner_only_directory(&facts) {
            Ok(directory)
        } else {
            Err(StorageError::unsafe_(
                "Team storage has unsafe ownership or permissions",
            ))
        }
    }

    fn database_path(&self, team: &str, create: bool) -> Result<PathBuf, StorageError> {
        let path = self.team_directory(team, create)?.join("files.sqlite3");
        if create {
            let created = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits().cast_signed())
                .open(&path);
            if let Err(error) = created
                && error.kind() != std::io::ErrorKind::AlreadyExists
            {
                return Err(StorageError::unsafe_(
                    "Team storage database is unavailable",
                ));
            }
        }
        match custody::facts(&path) {
            Ok(None) if !create => Err(TEAM_NOT_FOUND),
            Ok(Some(facts))
                if (FileCustody {
                    uid: Some(current_uid()),
                    gid: None,
                    mode: Mode::OwnerOnly,
                    size: None,
                })
                .holds(&facts) =>
            {
                Ok(path)
            }
            _ => Err(StorageError::unsafe_(
                "Team storage database has an unsafe shape",
            )),
        }
    }

    fn connect(
        &self,
        team: &str,
        create: bool,
        limit_bytes: i64,
    ) -> Result<Connection, StorageError> {
        let path = self.database_path(team, create)?;
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection =
            Connection::open_with_flags(&path, flags).map_err(|error| database(&error))?;
        Self::prepare(&connection, limit_bytes).map_err(|error| match error {
            Prepared::Storage(error) => error,
            Prepared::Database(error) => database(&error),
        })?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .map_err(|_| StorageError::unsafe_("Team storage database is unavailable"))?;
        Ok(connection)
    }

    fn prepare(connection: &Connection, limit_bytes: i64) -> Result<(), Prepared> {
        connection.busy_timeout(Duration::from_secs(5))?;
        connection.execute_batch("PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF;")?;
        connection.query_row("PRAGMA journal_mode=DELETE", [], |_| Ok(()))?;
        connection.execute_batch("PRAGMA synchronous=FULL;")?;
        connection.query_row("PRAGMA secure_delete=ON", [], |_| Ok(()))?;
        Self::schema(connection)?;
        let page_size: i64 = connection.query_row("PRAGMA page_size", [], |row| row.get(0))?;
        let logical = (limit_bytes + DATABASE_HEADROOM_BYTES + page_size - 1) / page_size;
        let current: i64 = connection.query_row("PRAGMA page_count", [], |row| row.get(0))?;
        // A plan downgrade must block new payload bytes without making the existing database impossible to open and
        // clean. SQLite cannot set max_page_count below the pages already allocated, so those pages are retained
        // while the transactional content ceiling enforces the lower quota at once.
        let physical = logical.max(current);
        let applied: i64 =
            connection.query_row(&format!("PRAGMA max_page_count={physical}"), [], |row| {
                row.get(0)
            })?;
        if applied != physical {
            return Err(Prepared::Storage(StorageError::unsafe_(
                "Team storage page limit could not be applied",
            )));
        }
        Ok(())
    }

    /// Creates the current schema in an empty database; any other shape is refused, never upgraded.
    fn schema(connection: &Connection) -> Result<(), Prepared> {
        let version = |connection: &Connection| {
            connection.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
        };
        if version(connection)? == SCHEMA_VERSION {
            return Ok(());
        }
        connection.execute_batch("BEGIN IMMEDIATE")?;
        let created = (|| {
            let current = version(connection)?;
            let tables: i64 =
                connection.query_row("SELECT count(*) FROM sqlite_master", [], |row| row.get(0))?;
            if current == 0 && tables == 0 {
                connection.execute_batch(FILES_TABLE)?;
                connection.execute_batch(ACKNOWLEDGEMENTS_TABLE)?;
                connection.execute_batch(&format!("PRAGMA user_version={SCHEMA_VERSION}"))?;
            } else if current != SCHEMA_VERSION {
                return Err(Prepared::Storage(StorageError::unsafe_(
                    "Team storage schema is not current",
                )));
            }
            Ok(())
        })();
        match created {
            Ok(()) => Ok(connection.execute_batch("COMMIT")?),
            Err(error) => {
                connection.execute_batch("ROLLBACK")?;
                Err(error)
            }
        }
    }

    /// One immediate write transaction on the Team's database: `work` commits only when it succeeds.
    fn transaction<T>(
        &self,
        team: &str,
        create: bool,
        limit_bytes: i64,
        work: impl FnOnce(&Connection) -> Result<T, Failure>,
    ) -> Result<T, Failure> {
        let mut connection = self.connect(team, create, limit_bytes)?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database(&error))?;
        let answer = work(&transaction)?;
        transaction.commit().map_err(|error| database(&error))?;
        Ok(answer)
    }

    /// Stores one file, or reuses the identical one the Team still holds without charging its quota again.
    ///
    /// A file from the web is marked in the same write that stores it, and so is every file of the same bytes; a file
    /// of bytes already marked is marked too.
    ///
    /// # Errors
    /// [`StorageErrorKind::Input`] for an invalid name, media type, or empty content; [`StorageErrorKind::Quota`]
    /// when the content, file count, or database would exceed the Team's limits; otherwise an unsafe refusal.
    pub fn put(
        &self,
        team: &str,
        name: &str,
        content: &[u8],
        media_type_value: Option<&str>,
        web: bool,
    ) -> Result<Upload, StorageError> {
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let name = filename(name)?.to_owned();
        let media_type = media_type(media_type_value)?;
        if content.is_empty() {
            return Err(StorageError::input("file must contain bytes"));
        }
        let size = i64::try_from(content.len()).map_err(|_| QUOTA_EXCEEDED)?;
        if size > limit_bytes {
            return Err(QUOTA_EXCEEDED);
        }
        let digest = hex(&Sha256::digest(content));
        let now = self.now();
        let new = NewFile {
            name: &name,
            media_type: &media_type,
            digest: &digest,
            content,
            size,
            web,
        };
        let (id, created_at, used) = self
            .transaction(&team, true, limit_bytes, |connection| {
                self.store_in(connection, &new, now, limit_bytes)
            })
            .map_err(Failure::into_storage)?;
        Ok(Upload {
            file: StoredFile {
                metadata: FileMetadata {
                    id,
                    name,
                    media_type,
                    size,
                    sha256: digest,
                },
                created_at,
            },
            usage: Usage::new(used, limit_bytes),
        })
    }

    /// Stores `new` in the open transaction, or reuses its identical file; answers its id, creation time, and the
    /// Team's usage after it.
    fn store_in(
        &self,
        connection: &Connection,
        new: &NewFile<'_>,
        now: i64,
        limit_bytes: i64,
    ) -> Result<(String, i64, i64), Failure> {
        sweep(connection, now)?;
        let marked = new.web
            || connection
                .query_row(
                    "SELECT 1 FROM files WHERE sha256=? AND web=1",
                    [new.digest],
                    |_| Ok(()),
                )
                .optional()?
                .is_some();
        if marked {
            connection.execute("UPDATE files SET web=1 WHERE sha256=?", [new.digest])?;
        }
        let existing: Option<(String, i64)> = connection
            .query_row(
                "SELECT id,created_at FROM files WHERE sha256=? AND name=? AND media_type=?",
                params![new.digest, new.name, new.media_type],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (id, created_at) = if let Some((id, created_at)) = existing {
            // An identical upload restarts the grace period of a file no turn references.
            connection.execute(
                "UPDATE files SET idle_since=max(idle_since, ?) WHERE id=?",
                params![now, id],
            )?;
            (id, created_at)
        } else {
            let (count, used) = usage(connection)?;
            if count >= MAX_FILES {
                return Err(Failure::Storage(StorageError::new(
                    StorageErrorKind::Quota,
                    "Team file count limit reached",
                )));
            }
            if used + new.size > limit_bytes {
                return Err(Failure::Storage(QUOTA_EXCEEDED));
            }
            let id = token_hex(self.entropy.as_ref(), 16)
                .map_err(|_| StorageError::unsafe_("Team storage is unavailable"))?;
            connection.execute(
                "INSERT INTO files(id,name,media_type,size,sha256,created_at,referenced,idle_since,web,content) \
                 VALUES(?,?,?,?,?,?,0,?,?,?)",
                params![id, new.name, new.media_type, new.size, new.digest, now, now, i64::from(marked), new.content],
            )?;
            (id, now)
        };
        let (_count, used) = usage(connection)?;
        Ok((id, created_at, used))
    }

    /// Every file the Team still keeps, oldest first, and its usage; a Team without storage has none.
    ///
    /// # Errors
    /// An unsafe refusal when the database cannot be read safely.
    pub fn list(&self, team: &str) -> Result<Listing, StorageError> {
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let now = self.now();
        let listed = self.transaction(&team, false, limit_bytes, |connection| {
            sweep(connection, now)?;
            let mut statement =
                connection.prepare("SELECT id,name,media_type,size,sha256,created_at FROM files ORDER BY created_at,id")?;
            let files = statement
                .query_map([], |row| {
                    Ok(StoredFile {
                        metadata: FileMetadata {
                            id: row.get(0)?,
                            name: row.get(1)?,
                            media_type: row.get(2)?,
                            size: row.get(3)?,
                            sha256: row.get(4)?,
                        },
                        created_at: row.get(5)?,
                    })
                })?
                .collect::<rusqlite::Result<Vec<StoredFile>>>()?;
            let (_count, used) = usage(connection)?;
            Ok((files, used))
        });
        let (files, used) = match listed {
            Err(Failure::Storage(error)) if error == TEAM_NOT_FOUND => (Vec::new(), 0),
            other => other.map_err(Failure::into_storage)?,
        };
        Ok(Listing {
            files,
            usage: Usage::new(used, limit_bytes),
        })
    }

    /// Marks the files a new turn selected as referenced before its Brain start can read them.
    ///
    /// The Team's conversation may still reference the previous turn's files until this turn completes, so the marks
    /// add to those already held. A file already collected fails the turn as not found. Returns the files this call
    /// newly marked, which a turn that fails releases again.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] for a collected file or absent storage; input or unsafe refusals.
    pub fn reference(&self, team: &str, file_ids: &[&str]) -> Result<Vec<String>, StorageError> {
        let ids = selected(file_ids)?;
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        self.transaction(&team, false, limit_bytes, |connection| {
            let mut added = Vec::new();
            for id in ids {
                let referenced: Option<i64> = connection
                    .query_row("SELECT referenced FROM files WHERE id=?", [&id], |row| {
                        row.get(0)
                    })
                    .optional()?;
                match referenced {
                    None => return Err(Failure::Storage(FILE_NOT_FOUND)),
                    Some(0) => {
                        connection.execute("UPDATE files SET referenced=1 WHERE id=?", [&id])?;
                        added.push(id);
                    }
                    Some(_) => {}
                }
            }
            Ok(added)
        })
        .map_err(Failure::into_storage)
    }

    /// Releases the files a failed turn newly referenced; each starts its grace period now. Absent storage releases
    /// nothing.
    ///
    /// # Errors
    /// Input or unsafe refusals.
    pub fn release(&self, team: &str, file_ids: &[&str]) -> Result<(), StorageError> {
        let ids = selected(file_ids)?;
        if ids.is_empty() {
            return Ok(());
        }
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let now = self.now();
        let released = self.transaction(&team, false, limit_bytes, |connection| {
            for id in &ids {
                connection.execute(
                    "UPDATE files SET referenced=0, idle_since=? WHERE id=? AND referenced=1",
                    params![now, id],
                )?;
            }
            Ok(())
        });
        not_found_is_done(released)
    }

    /// A completed turn, or a purged conversation with no files, leaves exactly these files referenced.
    ///
    /// Every other file is released and starts its grace period now; files idle past it are collected. Absent storage
    /// settles nothing.
    ///
    /// # Errors
    /// Input or unsafe refusals.
    pub fn settle(&self, team: &str, file_ids: &[&str]) -> Result<(), StorageError> {
        let mut ids = selected(file_ids)?;
        ids.sort();
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let now = self.now();
        let settled = self.transaction(&team, false, limit_bytes, |connection| {
            let mut statement = connection.prepare("SELECT id FROM files WHERE referenced=1")?;
            let mut held = statement
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<String>>>()?;
            held.sort();
            for id in held.iter().filter(|id| !ids.contains(id)) {
                connection.execute(
                    "UPDATE files SET referenced=0, idle_since=? WHERE id=?",
                    params![now, id],
                )?;
            }
            for id in ids.iter().filter(|id| !held.contains(id)) {
                connection.execute("UPDATE files SET referenced=1 WHERE id=?", [id])?;
            }
            sweep(connection, now)?;
            Ok(())
        });
        not_found_is_done(settled)
    }

    /// The metadata of one file the Team's listing still shows, without reading its bytes or renewing its grace.
    ///
    /// A file that is absent, or unreferenced and idle past its grace period, is not found: the next sweep collects it.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], or an unsafe refusal.
    pub fn listed(&self, team: &str, file: &str) -> Result<FileMetadata, StorageError> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let limit_bytes = self.limit(&team)?;
        let connection = match self.connect(&team, false, limit_bytes) {
            Err(error) if error == TEAM_NOT_FOUND => return Err(FILE_NOT_FOUND),
            other => other?,
        };
        let row = connection
            .query_row(
                "SELECT name,media_type,size,sha256 FROM files WHERE id=? AND (referenced=1 OR idle_since>?)",
                params![id, self.now() - UNREFERENCED_GRACE_SECONDS],
                |row| {
                    Ok(FileMetadata {
                        id: id.clone(),
                        name: row.get(0)?,
                        media_type: row.get(1)?,
                        size: row.get(2)?,
                        sha256: row.get(3)?,
                    })
                },
            )
            .optional()
            .map_err(|error| database(&error))?;
        row.ok_or(FILE_NOT_FOUND)
    }

    /// Whether the file's original may leave Team for `principal`: it is not from the web, or they acknowledged it.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] for an absent file or storage; an unsafe refusal when it cannot be read.
    pub fn released(
        &self,
        team: &str,
        file: &str,
        who: Option<&str>,
    ) -> Result<bool, StorageError> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let limit_bytes = self.limit(&team)?;
        self.transaction(&team, false, limit_bytes, |connection| {
            let web: Option<i64> = connection
                .query_row("SELECT web FROM files WHERE id=?", [&id], |row| row.get(0))
                .optional()?;
            match web {
                None => Err(Failure::Storage(FILE_NOT_FOUND)),
                Some(0) => Ok(true),
                Some(_) => {
                    let Some(who) = who else {
                        return Ok(false);
                    };
                    let who = principal(who)?;
                    Ok(connection
                        .query_row(
                            "SELECT 1 FROM acknowledgements WHERE file_id=? AND principal=?",
                            params![id, who],
                            |_| Ok(()),
                        )
                        .optional()?
                        .is_some())
                }
            }
        })
        .map_err(Failure::into_storage)
    }

    /// Records that `who` accepted the risk of one listed file from the web; `record` audits it first.
    ///
    /// The acknowledgement commits only once `record` returned, so no authorization exists without its audit.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], [`StorageErrorKind::NotWeb`], [`StorageErrorKind::Quota`] at the
    /// acknowledgement limit, input or unsafe refusals, or `record`'s own error (nothing is then committed).
    pub fn acknowledge<E: From<StorageError>>(
        &self,
        team: &str,
        file: &str,
        who: &str,
        record: impl FnOnce(&str) -> Result<(), E>,
    ) -> Result<(), E> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let who = principal(who)?;
        let limit_bytes = self.limit(&team)?;
        let now = self.now();
        let mut record = Some(record);
        let mut recorded: Result<(), E> = Ok(());
        let committed = self.transaction(&team, false, limit_bytes, |connection| {
            let web: Option<i64> = connection
                .query_row(
                    "SELECT web FROM files WHERE id=? AND (referenced=1 OR idle_since>?)",
                    params![id, now - UNREFERENCED_GRACE_SECONDS],
                    |row| row.get(0),
                )
                .optional()?;
            match web {
                None => return Err(Failure::Storage(FILE_NOT_FOUND)),
                Some(0) => {
                    return Err(Failure::Storage(StorageError::new(
                        StorageErrorKind::NotWeb,
                        "the file did not come from the web",
                    )));
                }
                Some(_) => {}
            }
            let count: i64 = connection.query_row("SELECT count(*) FROM acknowledgements", [], |row| row.get(0))?;
            if count >= MAX_ACKNOWLEDGEMENTS {
                return Err(Failure::Storage(StorageError::new(
                    StorageErrorKind::Quota,
                    "Team acknowledgement limit reached",
                )));
            }
            connection.execute(
                "INSERT OR IGNORE INTO acknowledgements(file_id, principal, created_at) VALUES(?,?,?)",
                params![id, who, now],
            )?;
            if let Some(record) = record.take() {
                recorded = record(&id);
            }
            if recorded.is_err() {
                return Err(Failure::Refused);
            }
            Ok(())
        });
        match committed {
            Ok(()) => Ok(()),
            Err(Failure::Refused) => recorded,
            Err(other) => Err(other.into_storage().into()),
        }
    }

    /// One file's metadata and bytes, proven equal to its stored size and digest.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], or an unsafe refusal, including a failed integrity check.
    pub fn get(&self, team: &str, file: &str) -> Result<(StoredFile, Vec<u8>), StorageError> {
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let id = file_id(file)?.to_owned();
        let connection = match self.connect(&team, false, limit_bytes) {
            Err(error) if error == TEAM_NOT_FOUND => return Err(FILE_NOT_FOUND),
            other => other?,
        };
        let row = connection
            .query_row(
                "SELECT name,media_type,size,sha256,created_at,content FROM files WHERE id=?",
                [&id],
                |row| {
                    Ok((
                        StoredFile {
                            metadata: FileMetadata {
                                id: id.clone(),
                                name: row.get(0)?,
                                media_type: row.get(1)?,
                                size: row.get(2)?,
                                sha256: row.get(3)?,
                            },
                            created_at: row.get(4)?,
                        },
                        row.get::<_, Vec<u8>>(5)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| database(&error))?;
        let (stored, content) = row.ok_or(FILE_NOT_FOUND)?;
        if i64::try_from(content.len()).ok() != Some(stored.metadata.size)
            || hex(&Sha256::digest(&content)) != stored.metadata.sha256
        {
            return Err(StorageError::unsafe_(
                "stored file failed its integrity check",
            ));
        }
        Ok((stored, content))
    }

    /// One selected-file reader to keep open across a chat turn; `None` when the turn selected no file.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] for absent storage; input or unsafe refusals.
    pub fn metadata_reader(
        &self,
        team: &str,
        file_ids: &[&str],
    ) -> Result<Option<MetadataReader>, StorageError> {
        if selected(file_ids)?.is_empty() {
            return Ok(None);
        }
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let connection = self.connect(&team, false, limit_bytes)?;
        Ok(Some(MetadataReader { team, connection }))
    }

    /// The selected files' metadata in their selected order, through `reader` or a reader of its own.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] when any is absent; a reader of another Team, input, or unsafe refusals.
    pub fn metadata(
        &self,
        team: &str,
        file_ids: &[&str],
        reader: Option<&MetadataReader>,
    ) -> Result<Vec<FileMetadata>, StorageError> {
        let ids = selected(file_ids)?;
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let team = team_id(team)?;
        let Some(reader) = reader else {
            let own = self.metadata_reader(&team, file_ids)?;
            return self.metadata(&team, file_ids, own.as_ref());
        };
        if reader.team != team {
            return Err(StorageError::unsafe_(
                "metadata reader belongs to another Team",
            ));
        }
        let read = || -> rusqlite::Result<Vec<FileMetadata>> {
            let mut statement = reader.connection.prepare(METADATA_SELECTS[ids.len() - 1])?;
            statement
                .query_map(rusqlite::params_from_iter(&ids), |row| {
                    Ok(FileMetadata {
                        id: row.get(0)?,
                        name: row.get(1)?,
                        media_type: row.get(2)?,
                        size: row.get(3)?,
                        // The original digest binds a turn to these exact bytes across its continuations (ADR-0093).
                        sha256: row.get(4)?,
                    })
                })?
                .collect()
        };
        let rows = read().map_err(|error| database(&error))?;
        ids.iter()
            .map(|id| {
                rows.iter()
                    .find(|row| row.id == *id)
                    .cloned()
                    .ok_or(FILE_NOT_FOUND)
            })
            .collect()
    }

    /// Removes one Team's storage; whether it existed.
    ///
    /// # Errors
    /// An unsafe refusal for a directory outside custody or a failed removal.
    pub fn destroy(&self, team: &str) -> Result<bool, StorageError> {
        let team = team_id(team)?;
        let directory = self.root.join(&team);
        let Some(facts) = custody::facts(&directory)
            .map_err(|_| StorageError::unsafe_("Team storage is unavailable"))?
        else {
            return Ok(false);
        };
        if !owner_only_directory(&facts) {
            return Err(StorageError::unsafe_(
                "Team storage has unsafe ownership or permissions",
            ));
        }
        std::fs::remove_dir_all(&directory)
            .map_err(|_| StorageError::unsafe_("Team storage could not be removed"))?;
        Ok(true)
    }

    /// Removes every strictly shaped Team directory from this dedicated volume; how many it removed.
    ///
    /// # Errors
    /// An unsafe refusal for an entry that is not a Team's storage, or a failed removal.
    pub fn destroy_all(&self) -> Result<usize, StorageError> {
        self.ensure_root()?;
        let mut names = std::fs::read_dir(&self.root)
            .map_err(|_| StorageError::unsafe_("storage root is unavailable"))?
            .map(|entry| {
                entry
                    .ok()
                    .and_then(|entry| entry.file_name().into_string().ok())
                    .ok_or(StorageError::unsafe_("invalid Team id"))
            })
            .collect::<Result<Vec<String>, StorageError>>()?;
        names.sort();
        let mut removed = 0;
        for name in names {
            if self.destroy(&name)? {
                removed += 1;
            }
        }
        Ok(removed)
    }
}

fn validate_limit(limit: i64) -> Result<i64, StorageError> {
    if limit >= 1 {
        Ok(limit)
    } else {
        Err(StorageError::unsafe_(
            "storage limit must be a positive integer",
        ))
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        // Writing to a String cannot fail.
        let _ = write!(text, "{byte:02x}");
    }
    text
}

fn not_found_is_done(outcome: Result<(), Failure>) -> Result<(), StorageError> {
    match outcome {
        Err(Failure::Storage(error)) if error == TEAM_NOT_FOUND => Ok(()),
        other => other.map_err(Failure::into_storage),
    }
}

/// One upload about to be stored.
struct NewFile<'a> {
    name: &'a str,
    media_type: &'a str,
    digest: &'a str,
    content: &'a [u8],
    size: i64,
    web: bool,
}

/// Why a transaction's work stopped: a storage refusal, an SQLite failure, or the caller's own refusal.
enum Failure {
    Storage(StorageError),
    Database(rusqlite::Error),
    Refused,
}

impl Failure {
    fn into_storage(self) -> StorageError {
        match self {
            Self::Storage(error) => error,
            Self::Database(error) => database(&error),
            Self::Refused => TRANSACTION_FAILED,
        }
    }
}

impl From<StorageError> for Failure {
    fn from(error: StorageError) -> Self {
        Self::Storage(error)
    }
}

impl From<rusqlite::Error> for Failure {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

enum Prepared {
    Storage(StorageError),
    Database(rusqlite::Error),
}

impl From<rusqlite::Error> for Prepared {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

#[cfg(test)]
mod tests;
