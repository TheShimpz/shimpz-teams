//! One small JSON document per file, in canonical JSON v1: the format of Team's plaintext records (inference
//! settings, memory, names, the chat limit, publication bindings and updates).

use std::fs::File;
use std::io::Read;
use std::path::Path;

use rustix::fs::{Mode as RawMode, OFlags};
use strict_json::Value;
use team_protocol::python::json;

use crate::durable;

/// Why a record could not be read or written; it never carries the path or the content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecordError {
    /// The file could not be opened, read, or durably written, or is not a regular file (Python's `OSError`).
    Unavailable,
    /// The content is oversized, not one strict JSON document, or `null` (Python's `ValueError`).
    Invalid,
}

impl std::fmt::Display for RecordError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "the record is unavailable",
            Self::Invalid => "the record is invalid",
        })
    }
}

impl std::error::Error for RecordError {}

/// The strict JSON document in the regular file `path` of at most `maximum` bytes, or `None` when it is absent.
///
/// No link is followed and a special file never blocks the read.
///
/// # Errors
/// [`RecordError::Unavailable`] for a file that cannot be read or is not regular; [`RecordError::Invalid`] for an
/// oversized file, a `null` document (`None` means only "absent"), or anything strict JSON refuses.
pub fn read(path: &Path, maximum: usize) -> Result<Option<Value>, RecordError> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let file = match rustix::fs::open(path, flags, RawMode::empty()) {
        Ok(descriptor) => File::from(descriptor),
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(_) => return Err(RecordError::Unavailable),
    };
    if !file
        .metadata()
        .map_err(|_| RecordError::Unavailable)?
        .file_type()
        .is_file()
    {
        return Err(RecordError::Unavailable);
    }
    let mut payload = Vec::new();
    file.take(maximum as u64 + 1)
        .read_to_end(&mut payload)
        .map_err(|_| RecordError::Unavailable)?;
    if payload.len() > maximum {
        return Err(RecordError::Invalid);
    }
    match strict_json::parse(&payload) {
        Ok(Value::Null) | Err(_) => Err(RecordError::Invalid),
        Ok(document) => Ok(Some(document)),
    }
}

/// Atomically and durably replaces owner-only `path` with `value`'s canonical JSON of at most `maximum` bytes.
///
/// The parent directory is created `0700` when absent.
///
/// # Errors
/// [`RecordError::Invalid`] for `null` or oversized content, before anything is written; [`RecordError::Unavailable`]
/// when it cannot be persisted.
pub fn write(path: &Path, value: &Value, maximum: usize) -> Result<(), RecordError> {
    if *value == Value::Null {
        return Err(RecordError::Invalid);
    }
    let payload = json::canonical(value);
    if payload.len() > maximum {
        return Err(RecordError::Invalid);
    }
    let parent = path.parent().ok_or(RecordError::Unavailable)?;
    durable::create_private_directory(parent).map_err(|_| RecordError::Unavailable)?;
    durable::replace(path, &payload, 0o600, None).map_err(|_| RecordError::Unavailable)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, symlink};

    use strict_json::{Number, Value};

    use super::{RecordError, read, write};

    fn record() -> Value {
        Value::Object(vec![
            ("name".to_owned(), Value::String("Équipe".to_owned())),
            ("count".to_owned(), Value::Number(Number::Int(1))),
        ])
    }

    #[test]
    fn a_record_round_trips_as_canonical_owner_only_json() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("records/record.json");
        assert_eq!(read(&path, 64), Ok(None));
        write(&path, &record(), 64).expect("written");
        assert_eq!(
            fs::read(&path).expect("read"),
            "{\"count\":1,\"name\":\"Équipe\"}".as_bytes()
        );
        assert_eq!(fs::metadata(&path).expect("stat").mode() & 0o7777, 0o600);
        assert_eq!(
            fs::metadata(path.parent().expect("parent"))
                .expect("stat")
                .mode()
                & 0o7777,
            0o700
        );
        let Some(Value::Object(members)) = read(&path, 64).expect("read") else {
            panic!("an object");
        };
        assert_eq!(members.len(), 2);
    }

    #[test]
    fn a_null_or_oversized_record_is_never_written() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("records/record.json");
        assert_eq!(write(&path, &Value::Null, 64), Err(RecordError::Invalid));
        assert_eq!(write(&path, &record(), 16), Err(RecordError::Invalid));
        assert!(!path.parent().expect("parent").exists());
    }

    #[test]
    fn only_a_strict_json_document_in_a_regular_file_is_read() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("record.json");
        fs::write(&path, format!("\"{}\"", "x".repeat(16))).expect("written");
        assert_eq!(read(&path, 16), Err(RecordError::Invalid));
        for raw in [
            b"".to_vec(),
            b"null".to_vec(),
            b"\xff".to_vec(),
            br#"{"a":1,"a":2}"#.to_vec(),
            b"NaN".to_vec(),
            b"1e999".to_vec(),
            b"[".repeat(100_000),
        ] {
            fs::write(&path, &raw).expect("written");
            assert_eq!(read(&path, 200_000), Err(RecordError::Invalid));
        }
        fs::remove_file(&path).expect("removed");
        fs::create_dir(&path).expect("created");
        assert_eq!(read(&path, 64), Err(RecordError::Unavailable));
        fs::remove_dir(&path).expect("removed");
        fs::write(root.path().join("target.json"), b"{}").expect("written");
        symlink(root.path().join("target.json"), &path).expect("linked");
        assert_eq!(read(&path, 64), Err(RecordError::Unavailable));
    }
}
