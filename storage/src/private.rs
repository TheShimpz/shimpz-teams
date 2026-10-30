//! Encrypted Team-owned state on disk: its owner-only files and directories, its keyring, and the separation of the
//! keyring from the state it seals.
//!
//! A state file is canonical ASCII JSON (`sort_keys`, compact, `ensure_ascii`) in a `0700` directory this process owns,
//! replaced durably; its keyring is one 32-byte `0600` file on its own volume. Every refusal is a closed
//! [`StateError`]: it never carries a path, a key, or file content.

use std::path::{Component, Path, PathBuf};

use strict_json::Value;
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_protocol::python::datetime::{ord_to_ymd, ymd_to_ord};
use team_protocol::python::json::{self, Options};

use crate::custody::{self, CustodyError, DirectoryCustody, FileCustody, FileIdentity, Snapshot};
use crate::durable::{self, DirectoryError};
use crate::envelope::Key;

/// Why encrypted state could not be read, written, or trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StateError {
    /// A file, directory, or keyring could not be read or created, or a required keyring is absent.
    Unavailable,
    /// A file or directory failed its ownership contract (kind, links, owner, mode, or size).
    Unsafe,
    /// Content exceeds its fixed byte limit.
    TooLarge,
    /// A write could not be made durable.
    NotPersisted,
    /// The keyring does not hold exactly one 256-bit key.
    InvalidKey,
    /// The state and keyring paths are not absolute or share a directory.
    Layout,
    /// The stored state or one of its envelopes is malformed.
    Malformed,
    /// The stored state has more records than its limit.
    RecordLimit,
    /// An envelope did not authenticate under the keyring.
    Unauthentic,
}

impl std::fmt::Display for StateError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "private state is unavailable",
            Self::Unsafe => "private state failed its ownership contract",
            Self::TooLarge => "private state exceeds its fixed byte limit",
            Self::NotPersisted => "private state could not be persisted",
            Self::InvalidKey => "the private state keyring is invalid",
            Self::Layout => "the private state keyring must be separate from encrypted state",
            Self::Malformed => "private state is malformed",
            Self::RecordLimit => "private state exceeds its record limit",
            Self::Unauthentic => "private state could not be authenticated",
        })
    }
}

impl std::error::Error for StateError {}

impl From<CustodyError> for StateError {
    fn from(error: CustodyError) -> Self {
        match error {
            CustodyError::Unavailable | CustodyError::Changed => Self::Unavailable,
            CustodyError::Unsafe => Self::Unsafe,
            CustodyError::TooLarge => Self::TooLarge,
        }
    }
}

impl From<DirectoryError> for StateError {
    fn from(error: DirectoryError) -> Self {
        match error {
            DirectoryError::Unavailable => Self::Unavailable,
            DirectoryError::Unsafe => Self::Unsafe,
        }
    }
}

/// The private file at `path` (owner-only `0600`, this process's, at most `maximum` bytes), or `None` when absent.
///
/// # Errors
/// [`StateError`] for a file outside custody, oversized, or unreadable.
pub fn read_private(path: &Path, maximum: u64) -> Result<Option<Vec<u8>>, StateError> {
    Ok(custody::read(
        path,
        &FileCustody::private(maximum),
        maximum,
    )?)
}

/// [`read_private`] that skips the read when the file still has the `previous` identity.
///
/// # Errors
/// As [`read_private`].
pub fn read_private_if_changed(
    path: &Path,
    maximum: u64,
    previous: Option<FileIdentity>,
) -> Result<Snapshot, StateError> {
    Ok(custody::read_if_changed(
        path,
        &FileCustody::private(maximum),
        maximum,
        previous,
    )?)
}

/// Durably replaces private file `path` with `payload`, after requiring its directory to be a `0700` directory this
/// process owns (created when absent).
///
/// # Errors
/// [`StateError`] when the directory is unsafe or unavailable, or the write is not durable.
pub fn write_private(path: &Path, payload: &[u8]) -> Result<(), StateError> {
    let parent = path.parent().ok_or(StateError::Unavailable)?;
    durable::ensure_directory(parent, &DirectoryCustody::private())?;
    durable::replace(path, payload, 0o600, None).map_err(|_| StateError::NotPersisted)
}

/// The canonical ASCII JSON of a sealed state file: `sort_keys`, compact, `ensure_ascii`.
pub const STATE_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: true,
    compact: true,
};

/// Persists `value` as [`STATE_JSON`] of at most `maximum` bytes with [`write_private`].
///
/// # Errors
/// [`StateError::TooLarge`] before anything is written, or as [`write_private`].
pub fn write_private_json(path: &Path, value: &Value, maximum: usize) -> Result<(), StateError> {
    let payload = json::dumps(value, STATE_JSON);
    if payload.len() > maximum {
        return Err(StateError::TooLarge);
    }
    write_private(path, payload.as_bytes())
}

/// The keyring file of one store: one 256-bit key, owner-only, in its own directory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Keyring {
    path: PathBuf,
}

impl Keyring {
    /// The keyring at `path`.
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// Its path.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The key; when absent and `create` allows it, a fresh key is generated and durably stored first.
    ///
    /// # Errors
    /// [`StateError::Unavailable`] when absent and not created, [`StateError::InvalidKey`] for anything but 32 bytes,
    /// or a custody or persistence failure.
    pub fn load(&self, create: bool, entropy: &dyn Entropy) -> Result<Key, StateError> {
        let Some(payload) = read_private(&self.path, 32)? else {
            if !create {
                return Err(StateError::Unavailable);
            }
            let key = Key::generate(entropy).map_err(|_| StateError::Unavailable)?;
            write_private(&self.path, key.expose())?;
            return Ok(key);
        };
        let bytes: [u8; 32] = payload
            .as_slice()
            .try_into()
            .map_err(|_| StateError::InvalidKey)?;
        Ok(Key::from_bytes(bytes))
    }
}

/// Requires absolute state and keyring paths whose parent directories differ once resolved (links followed as far as
/// the path exists, as Python's non-strict `Path.resolve`).
///
/// # Errors
/// [`StateError::Layout`] for a relative or shared path; [`StateError::Unavailable`] when a parent cannot be resolved.
pub fn separate_paths(state: &Path, key: &Path) -> Result<(), StateError> {
    if !state.is_absolute() || !key.is_absolute() {
        return Err(StateError::Layout);
    }
    let parent = |path: &Path| {
        path.parent()
            .map_or_else(|| path.to_path_buf(), Path::to_path_buf)
    };
    if resolve(&parent(state))? == resolve(&parent(key))? {
        return Err(StateError::Layout);
    }
    Ok(())
}

/// The longest existing prefix of absolute `path` canonicalized, with its missing remainder applied lexically.
fn resolve(path: &Path) -> Result<PathBuf, StateError> {
    let mut existing = path.to_path_buf();
    let mut missing: Vec<Component<'static>> = Vec::new();
    let mut names: Vec<std::ffi::OsString> = Vec::new();
    loop {
        match std::fs::canonicalize(&existing) {
            Ok(mut resolved) => {
                for (component, name) in missing.iter().zip(&names).rev() {
                    match component {
                        Component::ParentDir => {
                            resolved.pop();
                        }
                        Component::Normal(_) => resolved.push(name),
                        _ => {}
                    }
                }
                return Ok(resolved);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let (kind, name) = match existing.components().next_back() {
                    Some(Component::Normal(name)) => {
                        (Component::Normal("".as_ref()), name.to_owned())
                    }
                    Some(Component::ParentDir) => (Component::ParentDir, "..".into()),
                    Some(Component::CurDir) => (Component::CurDir, ".".into()),
                    _ => return Err(StateError::Unavailable),
                };
                missing.push(kind);
                names.push(name);
                if !existing.pop() {
                    return Err(StateError::Unavailable);
                }
            }
            Err(_) => return Err(StateError::Unavailable),
        }
    }
}

/// The UTC wire text of whole second `epoch`, `YYYY-MM-DDTHH:MM:SSZ`, as `core/instant.py` writes it.
#[must_use]
pub fn instant_text(epoch: i64) -> String {
    let days = epoch.div_euclid(86_400);
    let seconds = epoch.rem_euclid(86_400);
    let ordinal = i64::from(ymd_to_ord(1970, 1, 1)) + days;
    let (year, month, day) = ord_to_ymd(i32::try_from(ordinal).unwrap_or(i32::MAX));
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        seconds / 3600,
        seconds % 3600 / 60,
        seconds % 60
    )
}

/// The whole seconds since the epoch on `clock` (Python's `int(time.time())`).
#[must_use]
pub fn epoch_seconds(clock: &dyn Clock) -> i64 {
    match clock.now().duration_since(std::time::UNIX_EPOCH) {
        Ok(elapsed) => i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX),
        // `int()` truncates toward zero.
        Err(before) => -i64::try_from(before.duration().as_secs()).unwrap_or(i64::MAX),
    }
}

/// The record time Team stamps now: [`instant_text`] of [`epoch_seconds`].
#[must_use]
pub fn timestamp(clock: &dyn Clock) -> String {
    instant_text(epoch_seconds(clock))
}

/// Whether `text` has exactly the form [`instant_text`] writes.
#[must_use]
pub fn is_timestamp(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 20
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            4 | 7 => *byte == b'-',
            10 => *byte == b'T',
            13 | 16 => *byte == b':',
            19 => *byte == b'Z',
            _ => byte.is_ascii_digit(),
        })
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    use std::time::{Duration, UNIX_EPOCH};

    use strict_json::{Number, Value};
    use team_core::testing::{CountingEntropy, FakeClock};

    use super::{
        Keyring, StateError, instant_text, is_timestamp, read_private, separate_paths, timestamp,
        write_private, write_private_json,
    };

    #[test]
    fn instants_are_utc_whole_seconds() {
        assert_eq!(instant_text(0), "1970-01-01T00:00:00Z");
        assert_eq!(instant_text(1_700_000_000), "2023-11-14T22:13:20Z");
        assert_eq!(instant_text(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(instant_text(-1), "1969-12-31T23:59:59Z");
        let clock = FakeClock::at(UNIX_EPOCH + Duration::from_millis(1_700_000_000_999));
        assert_eq!(timestamp(&clock), "2023-11-14T22:13:20Z");
        assert!(is_timestamp("2023-11-14T22:13:20Z"));
        for refused in [
            "2023-11-14T22:13:20",
            "2023-11-14 22:13:20Z",
            "2023-11-14T22:13:2xZ",
            "x",
        ] {
            assert!(!is_timestamp(refused), "{refused}");
        }
    }

    #[test]
    fn keyring_is_created_once_owner_only_and_refused_when_invalid() {
        let root = tempfile::tempdir().expect("temporary directory");
        let keyring = Keyring::new(root.path().join("key/aes256.key"));
        let entropy = CountingEntropy::default();
        assert_eq!(
            keyring.load(false, &entropy).map(|_| ()),
            Err(StateError::Unavailable)
        );
        let created = keyring.load(true, &entropy).expect("created");
        assert_eq!(
            keyring.load(false, &entropy).expect("read").expose(),
            created.expose()
        );
        assert_eq!(
            fs::metadata(keyring.path()).expect("stat").mode() & 0o7777,
            0o600
        );
        assert_eq!(
            fs::metadata(root.path().join("key")).expect("stat").mode() & 0o7777,
            0o700
        );
        fs::write(keyring.path(), b"short").expect("written");
        assert_eq!(
            keyring.load(true, &entropy).map(|_| ()),
            Err(StateError::InvalidKey)
        );
        fs::set_permissions(keyring.path(), fs::Permissions::from_mode(0o640)).expect("mode set");
        assert_eq!(
            keyring.load(true, &entropy).map(|_| ()),
            Err(StateError::Unsafe)
        );
    }

    #[test]
    fn keyring_dir_differs_from_state_dir() {
        let root = tempfile::tempdir().expect("temporary directory");
        let state = root.path().join("chat/state/state.json");
        let key = root.path().join("chat/key/aes256.key");
        assert_eq!(separate_paths(&state, &key), Ok(()));
        assert_eq!(
            separate_paths(&state, &root.path().join("chat/state/aes256.key")),
            Err(StateError::Layout)
        );
        assert_eq!(
            separate_paths(std::path::Path::new("state.json"), &key),
            Err(StateError::Layout)
        );
        // A link that makes the keyring directory the state directory is resolved and refused.
        fs::create_dir_all(root.path().join("chat/state")).expect("created");
        symlink(
            root.path().join("chat/state"),
            root.path().join("chat/alias"),
        )
        .expect("linked");
        assert_eq!(
            separate_paths(&state, &root.path().join("chat/alias/aes256.key")),
            Err(StateError::Layout)
        );
        assert_eq!(
            separate_paths(
                &state,
                &root.path().join("chat/missing/../state/aes256.key")
            ),
            Err(StateError::Layout)
        );
    }

    #[test]
    fn private_files_are_written_in_owner_only_directories_only() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("private/state.json");
        let value = Value::Object(vec![
            ("b".to_owned(), Value::String("é".to_owned())),
            ("a".to_owned(), Value::Number(Number::Int(1))),
        ]);
        write_private_json(&path, &value, 64).expect("written");
        assert_eq!(fs::read(&path).expect("read"), br#"{"a":1,"b":"\u00e9"}"#);
        assert_eq!(
            read_private(&path, 64),
            Ok(Some(br#"{"a":1,"b":"\u00e9"}"#.to_vec()))
        );
        assert_eq!(
            write_private_json(&path, &value, 8),
            Err(StateError::TooLarge)
        );
        fs::set_permissions(
            root.path().join("private"),
            fs::Permissions::from_mode(0o755),
        )
        .expect("mode set");
        assert_eq!(write_private(&path, b"x"), Err(StateError::Unsafe));
        assert_eq!(read_private(&path, 4), Err(StateError::Unsafe));
    }
}
