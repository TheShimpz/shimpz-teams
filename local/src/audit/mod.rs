//! The hash-chained audit log, `/var/log/shimpz-local/audit.jsonl`: Team's [`AuditSink`].
//!
//! Each event is one line of compact JSON with sorted keys and ASCII escapes, carrying `prev_sha256`, the SHA-256 of
//! the exact bytes of the line before it (its newline included); the first line ever written carries 64 zeros. A
//! restarted writer continues from the last line on disk, and a rotated-in file (at 1 MiB, two rotations kept) starts
//! from the last line of the file it replaced, so the chain runs across every retained file. The chain makes a
//! removed, inserted, reordered, or altered line inside the retained lines evident. It is not protection against a
//! wholesale rewrite: whoever can write the log can recompute every link, drop the newest lines, or delete a file.
//!
//! A record resolves once its line is written to the file, so a process crash keeps it. A background group commit
//! fsyncs the first unsynced line within 50 ms, so a sudden power loss may discard that window; [`AuditLog::flush`]
//! synchronizes everything recorded before it. A failed sync closes the log for good: every later record fails, and the
//! operation that needed it is refused.

mod event;

use std::fs::File;
use std::io::Write;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};
use team_core::clock::Clock;
use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_protocol::python::json::{self, Options};
use team_storage::custody::{self, FileCustody, Mode, current_uid};
use team_storage::durable;
use team_storage::private::timestamp;

pub use self::event::{
    ACTIVITY_OPERATIONS, CREDENTIAL_STATES, Inadmissible, MAX_ACTIVITY_ORIGIN_CHARS, Outcome,
    Principal, PrincipalClass,
};

/// The `prev_sha256` of the first line ever written.
pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
/// A file is rotated once it is larger than this.
pub const MAX_BYTES: u64 = 1024 * 1024;
/// How many rotated files are kept (`audit.jsonl.1`, `audit.jsonl.2`).
pub const BACKUPS: u32 = 2;
/// The longest the first unsynced line waits for its fsync.
pub const GROUP_COMMIT: Duration = Duration::from_millis(50);
/// The most a file's last line is searched for when the chain continues: a file holds at most [`MAX_BYTES`] plus the
/// line that crossed it.
const MAX_SCANNED_BYTES: u64 = 8 * MAX_BYTES;

/// The line encoding: sorted keys, compact separators, ASCII escapes.
const LINE: Options = Options {
    ensure_ascii: true,
    sort_keys: true,
    compact: true,
};

/// Why a record or flush failed; it never carries an event's content or a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AuditError {
    /// The event is not admissible.
    Inadmissible,
    /// The line could not be written.
    Unwritten,
    /// A sync failed earlier, or the log is closing; nothing more is recorded.
    Closed,
}

impl std::fmt::Display for AuditError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Inadmissible => "the audit event is not admissible",
            Self::Unwritten => "the local audit journal could not be written",
            Self::Closed => "the local audit journal is closed",
        })
    }
}

impl std::error::Error for AuditError {}

/// The writer's limits; [`Settings::default`] is the shipped contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Settings {
    /// The rotation size.
    pub max_bytes: u64,
    /// The group commit window.
    pub group_commit: Duration,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            max_bytes: MAX_BYTES,
            group_commit: GROUP_COMMIT,
        }
    }
}

#[derive(Default)]
struct Writer {
    file: Option<File>,
    /// The SHA-256 of the last line written or found; `None` while the log is not open.
    chain: Option<String>,
    dirty_since: Option<Instant>,
    failed: bool,
    stopping: bool,
    flusher: Option<std::thread::JoinHandle<()>>,
    syncs: u64,
}

struct Shared {
    path: PathBuf,
    settings: Settings,
    clock: Arc<dyn Clock>,
    entropy: Arc<dyn Entropy>,
    writer: Mutex<Writer>,
    changed: Condvar,
}

/// The audit log at one path, shared by every recorder.
#[derive(Clone)]
pub struct AuditLog {
    shared: Arc<Shared>,
}

impl std::fmt::Debug for AuditLog {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AuditLog")
            .field("path", &self.shared.path)
            .finish_non_exhaustive()
    }
}

fn custody() -> FileCustody {
    FileCustody {
        uid: Some(current_uid()),
        gid: None,
        mode: Mode::Exact(0o600),
        size: None,
    }
}

/// Requires the file at `path`, when it exists, to be a single-link `0600` regular file this process owns.
fn safe_file(path: &Path) -> Result<bool, AuditError> {
    match custody::facts(path) {
        Ok(None) => Ok(false),
        Ok(Some(facts)) if custody().holds(&facts) => Ok(true),
        _ => Err(AuditError::Unwritten),
    }
}

fn sibling(path: &Path, index: u32) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{index}"));
    path.with_file_name(name)
}

fn sha256_hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut hex = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        // Writing to a String cannot fail.
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

/// The last line of one log file, or `None` when it holds none. A torn final write (a crash inside one append) is
/// ended with its newline first, so it keeps its place in the chain and the next line never runs into it.
fn last_line(path: &Path) -> Result<Option<Vec<u8>>, AuditError> {
    if !safe_file(path)? {
        return Ok(None);
    }
    let bounded = FileCustody {
        size: Some((0, MAX_SCANNED_BYTES)),
        ..custody()
    };
    let Some(mut content) =
        custody::read(path, &bounded, MAX_SCANNED_BYTES).map_err(|_| AuditError::Unwritten)?
    else {
        return Ok(None);
    };
    if content.is_empty() {
        return Ok(None);
    }
    if content.last() != Some(&b'\n') {
        let mut file = durable::open_append(path, false).map_err(|_| AuditError::Unwritten)?;
        file.write_all(b"\n").map_err(|_| AuditError::Unwritten)?;
        content.push(b'\n');
    }
    let start = content[..content.len() - 1]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(0, |index| index + 1);
    Ok(Some(content[start..].to_vec()))
}

impl AuditLog {
    /// The log at `path`, opened on its first record.
    #[must_use]
    pub fn new(
        path: PathBuf,
        settings: Settings,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
    ) -> Self {
        Self {
            shared: Arc::new(Shared {
                path,
                settings,
                clock,
                entropy,
                writer: Mutex::new(Writer::default()),
                changed: Condvar::new(),
            }),
        }
    }

    fn lock(&self) -> Result<MutexGuard<'_, Writer>, AuditError> {
        self.shared.writer.lock().map_err(|_| AuditError::Closed)
    }

    /// Appends one admitted event and answers its trace id; the line is written when this returns.
    ///
    /// # Errors
    /// [`AuditError`] when the event is not admissible, the line could not be written, or the log is closed.
    pub fn record_blocking(&self, event: &AuditEvent) -> Result<String, AuditError> {
        let mut fields = event::admit(event).map_err(|Inadmissible| AuditError::Inadmissible)?;
        let given = fields.iter().find_map(|(name, value)| match value {
            Value::String(trace_id) if name == "trace_id" => Some(trace_id.clone()),
            _ => None,
        });
        let trace_id = if let Some(trace_id) = given {
            trace_id
        } else {
            let trace_id =
                token_hex(self.shared.entropy.as_ref(), 16).map_err(|_| AuditError::Unwritten)?;
            fields.push(("trace_id".to_owned(), Value::String(trace_id.clone())));
            trace_id
        };
        fields.push((
            "ts".to_owned(),
            Value::String(timestamp(self.shared.clock.as_ref())),
        ));
        fields.push(("service".to_owned(), Value::String("team-local".to_owned())));
        let mut writer = self.lock()?;
        if writer.failed || writer.stopping {
            return Err(AuditError::Closed);
        }
        self.open_locked(&mut writer)?;
        let chain = writer.chain.clone().unwrap_or_else(|| GENESIS.to_owned());
        fields.push(("prev_sha256".to_owned(), Value::String(chain)));
        let mut line = json::dumps(&Value::Object(fields), LINE).into_bytes();
        line.push(b'\n');
        let file = writer.file.as_mut().ok_or(AuditError::Unwritten)?;
        file.write_all(&line).map_err(|_| AuditError::Unwritten)?;
        writer.chain = Some(sha256_hex(&line));
        if writer.dirty_since.is_none() {
            writer.dirty_since = Some(Instant::now());
        }
        self.ensure_flusher(&mut writer);
        self.shared.changed.notify_all();
        Ok(trace_id)
    }

    /// Opens the current file, continuing the chain, or keeps the open one while it is still the file at the path and
    /// not over its size; an oversized file is synchronized, closed, and rotated first.
    fn open_locked(&self, writer: &mut Writer) -> Result<(), AuditError> {
        let path = &self.shared.path;
        if let Some(file) = &writer.file {
            let on_disk = std::fs::symlink_metadata(path).map_err(|_| AuditError::Unwritten)?;
            if !safe_file(path)? {
                return Err(AuditError::Unwritten);
            }
            let open = file.metadata().map_err(|_| AuditError::Unwritten)?;
            if (on_disk.dev(), on_disk.ino()) != (open.dev(), open.ino()) {
                return Err(AuditError::Unwritten);
            }
            if open.size() <= self.shared.settings.max_bytes {
                return Ok(());
            }
            self.sync_locked(writer)?;
            writer.file = None;
            writer.chain = None;
        }
        let parent = path.parent().ok_or(AuditError::Unwritten)?;
        durable::create_private_directory(parent).map_err(|_| AuditError::Unwritten)?;
        safe_file(path)?;
        // The chain continues after the current file's last line, read before a rotation moves it, or after the newest
        // rotated file's when a rotation was interrupted before its first line.
        let found = match last_line(path)? {
            Some(line) => Some(line),
            None => last_line(&sibling(path, 1))?,
        };
        self.rotate(path)?;
        let file = durable::open_append(path, true).map_err(|_| AuditError::Unwritten)?;
        safe_file(path)?;
        writer.file = Some(file);
        writer.chain = Some(found.map_or_else(|| GENESIS.to_owned(), |line| sha256_hex(&line)));
        Ok(())
    }

    fn rotate(&self, path: &Path) -> Result<(), AuditError> {
        let size = custody::facts(path)
            .map_err(|_| AuditError::Unwritten)?
            .map(|facts| facts.size);
        if size.is_none_or(|size| size <= self.shared.settings.max_bytes) {
            return Ok(());
        }
        let oldest = sibling(path, BACKUPS);
        if safe_file(&oldest)? {
            std::fs::remove_file(&oldest).map_err(|_| AuditError::Unwritten)?;
        }
        for index in (1..BACKUPS).rev() {
            let source = sibling(path, index);
            if safe_file(&source)? {
                std::fs::rename(&source, sibling(path, index + 1))
                    .map_err(|_| AuditError::Unwritten)?;
            }
        }
        std::fs::rename(path, sibling(path, 1)).map_err(|_| AuditError::Unwritten)
    }

    fn sync_locked(&self, writer: &mut Writer) -> Result<(), AuditError> {
        let (Some(file), Some(_)) = (&writer.file, writer.dirty_since) else {
            return Ok(());
        };
        if file.sync_all().is_err() {
            writer.failed = true;
            return Err(AuditError::Closed);
        }
        writer.dirty_since = None;
        writer.syncs += 1;
        self.shared.changed.notify_all();
        Ok(())
    }

    fn ensure_flusher(&self, writer: &mut Writer) {
        if writer.flusher.is_some() {
            return;
        }
        let log = self.clone();
        writer.flusher = std::thread::Builder::new()
            .name("local-audit-flush".to_owned())
            .spawn(move || log.flush_worker())
            .ok();
    }

    fn flush_worker(&self) {
        let Ok(mut writer) = self.lock() else {
            return;
        };
        loop {
            if writer.stopping {
                let _ = self.sync_locked(&mut writer);
                writer.file = None;
                writer.chain = None;
                return;
            }
            if writer.failed {
                writer.file = None;
                writer.chain = None;
                return;
            }
            let Some(dirty_since) = writer.dirty_since else {
                writer = match self.shared.changed.wait(writer) {
                    Ok(writer) => writer,
                    Err(_) => return,
                };
                continue;
            };
            let deadline = dirty_since + self.shared.settings.group_commit;
            let now = Instant::now();
            if now < deadline {
                writer = match self.shared.changed.wait_timeout(writer, deadline - now) {
                    Ok((writer, _)) => writer,
                    Err(_) => return,
                };
                continue;
            }
            if self.sync_locked(&mut writer).is_err() {
                writer.file = None;
                writer.chain = None;
                return;
            }
        }
    }

    /// Synchronizes every event recorded before this call.
    ///
    /// # Errors
    /// [`AuditError::Closed`] when a sync failed now or earlier, or the log is closing.
    pub fn flush(&self) -> Result<(), AuditError> {
        let mut writer = self.lock()?;
        if writer.failed || writer.stopping {
            return Err(AuditError::Closed);
        }
        self.sync_locked(&mut writer)
    }

    /// Flushes and closes the writer; a later record reopens the log and continues the chain on disk.
    ///
    /// # Errors
    /// [`AuditError::Closed`] when a sync failed.
    pub fn close(&self) -> Result<(), AuditError> {
        let flusher = {
            let mut writer = self.lock()?;
            let Some(flusher) = writer.flusher.take() else {
                let synced = self.sync_locked(&mut writer);
                writer.file = None;
                writer.chain = None;
                return synced.and(if writer.failed {
                    Err(AuditError::Closed)
                } else {
                    Ok(())
                });
            };
            writer.stopping = true;
            self.shared.changed.notify_all();
            flusher
        };
        let _ = flusher.join();
        let mut writer = self.lock()?;
        writer.stopping = false;
        if writer.failed {
            Err(AuditError::Closed)
        } else {
            Ok(())
        }
    }

    #[cfg(test)]
    fn syncs(&self) -> u64 {
        self.lock().map_or(0, |writer| writer.syncs)
    }
}

impl AuditSink for AuditLog {
    fn record(&self, event: AuditEvent) -> BoxFuture<'_, Result<(), AuditUnavailable>> {
        let log = self.clone();
        Box::pin(async move {
            match tokio::task::spawn_blocking(move || log.record_blocking(&event)).await {
                Ok(Ok(_trace_id)) => Ok(()),
                _ => Err(AuditUnavailable),
            }
        })
    }
}

#[cfg(test)]
mod tests;
