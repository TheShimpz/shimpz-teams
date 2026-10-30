//! Team-brokered delivery of one selected file to an Action that declares a file input (`action/files.py`, ADR-0093).
//!
//! The model names a file only by the opaque id of a file the current logical turn selected. The first invocation
//! carries the file's metadata with its content withheld; only the replay whose transcript holds the Action's own
//! declared authorization response carries the original bytes, read from Team storage and checked against the turn's
//! digest. Such a delivery first holds the one file-RPC slot, whose wait and exchange share one deadline.

use std::time::Duration;

use assistant_protocol::input_file;
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_assistant::spec::ActionSpec;
use team_core::stop::Stop;
use team_protocol::python::value::string;
use team_storage::files::FileStore;
use tokio::sync::{Semaphore, SemaphorePermit};
use tokio::time::Instant;

use crate::executor::{ActionFailure, ActionFile};
use crate::human::ActionTranscript;

/// The one deadline of an admitted delivery: its wait for the slot and its exchange together.
pub const FILE_RPC_TIMEOUT: Duration = Duration::from_secs(60);

/// The file is not one the turn selected, cannot be delivered, or no longer has its turn's bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileUnavailable;

/// One stored file's recorded size and digest, and its bytes.
#[derive(Clone, PartialEq, Eq)]
pub struct Stored {
    /// The size its store recorded.
    pub size: i64,
    /// The digest its store recorded.
    pub sha256: String,
    /// Its bytes.
    pub data: Vec<u8>,
}

impl std::fmt::Debug for Stored {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Stored").finish_non_exhaustive()
    }
}

/// Where Team reads a selected file's bytes.
pub trait FileSource: Send + Sync {
    /// The stored file `file` of `team`.
    ///
    /// # Errors
    /// [`FileUnavailable`] when it is gone or fails its integrity check.
    fn read(&self, team: &str, file: &str) -> Result<Stored, FileUnavailable>;
}

impl FileSource for FileStore {
    fn read(&self, team: &str, file: &str) -> Result<Stored, FileUnavailable> {
        let (stored, data) = self.get(team, file).map_err(|_| FileUnavailable)?;
        Ok(Stored {
            size: stored.metadata.size,
            sha256: stored.metadata.sha256,
            data,
        })
    }
}

impl ActionFile {
    /// The closed metadata both the invocation and the authorization disclosure carry.
    #[must_use]
    pub fn metadata(&self) -> Value {
        let text = |value: &str| Value::String(value.to_owned());
        Value::Object(vec![
            ("id".to_owned(), text(&self.id)),
            ("name".to_owned(), text(&self.name)),
            ("media_type".to_owned(), text(&self.media_type)),
            ("size".to_owned(), Value::Number(Number::Int(self.size))),
            ("sha256".to_owned(), text(&self.sha256)),
        ])
    }

    fn deliverable(&self) -> bool {
        (1..=input_file::MAX_FILE_BYTES).contains(&self.size)
            && input_file::valid_name(&self.name)
            && self.media_type.len() <= input_file::MAX_MEDIA_TYPE
            && input_file::media_type(&self.media_type)
    }
}

/// The files a turn selected, from its attachments, with the media type Team determined while preparing them.
#[must_use]
pub fn selected(attachments: &[Value]) -> Vec<ActionFile> {
    attachments
        .iter()
        .filter_map(|item| {
            let text = |key: &str| item.get(key).and_then(string).map(str::to_owned);
            let size = match item.get("size") {
                Some(Value::Number(Number::Int(size))) => Some(*size),
                _ => None,
            };
            Some(ActionFile {
                id: text("id")?,
                name: text("name")?,
                media_type: text("media_type")?,
                size: size?,
                sha256: text("sha256")?,
            })
        })
        .collect()
}

/// The one selected file a file-taking Action's declared input names, or `None` for an ordinary Action.
///
/// # Errors
/// [`FileUnavailable`] when the input names a file the turn did not select, or one that cannot be delivered.
pub fn action_file<'a>(
    input_files: &[String],
    input: &Value,
    selected: &'a [ActionFile],
) -> Result<Option<&'a ActionFile>, FileUnavailable> {
    let Some(name) = input_files.first() else {
        return Ok(None);
    };
    let named = input.get(name).and_then(string);
    let file = named
        .and_then(|id| selected.iter().find(|file| file.id == id))
        .ok_or(FileUnavailable)?;
    file.deliverable()
        .then_some(Some(file))
        .ok_or(FileUnavailable)
}

/// The file an authorization request of a file-taking Action discloses; no other request discloses one.
///
/// # Errors
/// [`FileUnavailable`] when the authorized Action's file is not deliverable.
pub fn disclosure(
    input_files: &[String],
    input: &Value,
    selected: &[ActionFile],
    kind: &str,
) -> Result<Option<Value>, FileUnavailable> {
    if !crate::human::authorizes(kind) {
        return Ok(None);
    }
    Ok(action_file(input_files, input, selected)?.map(ActionFile::metadata))
}

/// The file commitments of an operation fingerprint: always present, `[]` for an ordinary Action.
#[must_use]
pub fn commitments(file: Option<&ActionFile>) -> Value {
    Value::Array(file.map(ActionFile::metadata).into_iter().collect())
}

/// Whether this invocation delivers `file`'s bytes: the transcript holds the Action's own declared authorization.
#[must_use]
pub fn grants(
    file: Option<&ActionFile>,
    action: &ActionSpec,
    transcript: &ActionTranscript,
) -> bool {
    file.is_some() && transcript.authorized(&action.human_requests)
}

/// The invocation's `files`: metadata with withheld content, or `granted`'s verified original bytes.
///
/// # Errors
/// [`FileUnavailable`] when the stored bytes are not the turn's.
pub fn invocation_files(
    file: Option<&ActionFile>,
    granted: Option<&Stored>,
) -> Result<Value, FileUnavailable> {
    let Some(file) = file else {
        return Ok(Value::Object(Vec::new()));
    };
    let content = match granted {
        None => vec![("type".to_owned(), Value::String("withheld".to_owned()))],
        Some(stored) => {
            let exact = stored.sha256 == file.sha256
                && stored.size == file.size
                && i64::try_from(stored.data.len()).is_ok_and(|length| length == file.size)
                && hex(&Sha256::digest(&stored.data)) == file.sha256;
            if !exact {
                return Err(FileUnavailable);
            }
            vec![
                ("type".to_owned(), Value::String("delivered".to_owned())),
                (
                    "base64".to_owned(),
                    Value::String(STANDARD.encode(&stored.data)),
                ),
            ]
        }
    };
    let Value::Object(mut record) = file.metadata() else {
        return Err(FileUnavailable);
    };
    record.retain(|(key, _)| key != "id");
    record.push(("content".to_owned(), Value::Object(content)));
    Ok(Value::Object(vec![(
        file.id.clone(),
        Value::Object(record),
    )]))
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

/// The `files` of one invocation, refused unless the reference validator admits it for this exact Action.
///
/// A file-taking Action outside a turn that selected its file therefore never runs, and bytes travel only with the
/// Action's own admitted authorization response.
///
/// # Errors
/// [`FileUnavailable`] when the bytes changed or the validator refuses the invocation.
pub fn deliver(
    action: &ActionSpec,
    file: Option<&ActionFile>,
    transcript: &ActionTranscript,
    input: &Value,
    granted: Option<&Stored>,
) -> Result<Value, FileUnavailable> {
    if file.is_none() && action.input_files.is_empty() {
        return Ok(Value::Object(Vec::new()));
    }
    let files = invocation_files(file, granted)?;
    let names =
        |values: &[String]| Value::Array(values.iter().cloned().map(Value::String).collect());
    let declaration = Value::Object(vec![
        ("input_files".to_owned(), names(&action.input_files)),
        ("input_schema".to_owned(), action.input_schema.clone()),
        ("human_requests".to_owned(), names(&action.human_requests)),
    ]);
    let invocation = Value::Object(vec![
        ("input".to_owned(), input.clone()),
        ("files".to_owned(), files.clone()),
        ("responses".to_owned(), Value::Array(transcript.payloads())),
    ]);
    match input_file::invocation_files_error(&declaration, &invocation) {
        None => Ok(files),
        Some(_) => Err(FileUnavailable),
    }
}

/// The id and size of the one file whose original bytes `files` deliver, for audit; never its name or content.
#[must_use]
pub fn delivered(files: &Value) -> Option<(String, i64)> {
    let Value::Object(files) = files else {
        return None;
    };
    files.iter().find_map(|(id, record)| {
        let kind = record.get("content")?.get("type").and_then(string);
        let size = match record.get("size") {
            Some(Value::Number(Number::Int(size))) => *size,
            _ => return None,
        };
        (kind == Some("delivered")).then(|| (id.clone(), size))
    })
}

/// The one file-RPC slot of a Team controller.
#[derive(Debug)]
pub struct FileSlot(Semaphore);

impl Default for FileSlot {
    fn default() -> Self {
        Self(Semaphore::new(1))
    }
}

/// A held slot and its one deadline.
#[derive(Debug)]
pub struct Admitted<'a> {
    _permit: SemaphorePermit<'a>,
    /// The deadline the wait began, which the exchange inherits.
    pub deadline: Instant,
}

impl FileSlot {
    /// Holds the slot for an invocation that will deliver bytes, before its execution is journaled.
    ///
    /// The wait observes Stop, and the wait and the exchange share one deadline. An invocation that delivers nothing
    /// holds nothing.
    ///
    /// # Errors
    /// [`ActionFailure::Stopped`] when Stop wins, even while the slot frees; [`ActionFailure::FileBusy`] when the
    /// deadline passes first.
    pub async fn admit(
        &self,
        delivers: bool,
        stop: &Stop,
    ) -> Result<Option<Admitted<'_>>, ActionFailure> {
        if !delivers {
            return Ok(None);
        }
        let deadline = Instant::now() + FILE_RPC_TIMEOUT;
        let permit = tokio::select! {
            biased;
            () = stop.stopped() => return Err(ActionFailure::Stopped),
            permit = tokio::time::timeout_at(deadline, self.0.acquire()) => permit,
        };
        let permit = permit
            .map_err(|_| ActionFailure::FileBusy)?
            .map_err(|_| ActionFailure::FileBusy)?;
        if stop.is_stopped() {
            // Stop may win while the wait succeeds; the slot is released before anything is journaled.
            return Err(ActionFailure::Stopped);
        }
        Ok(Some(Admitted {
            _permit: permit,
            deadline,
        }))
    }

    /// Whether the slot is free.
    #[must_use]
    pub fn free(&self) -> bool {
        self.0.available_permits() == 1
    }
}

#[cfg(test)]
pub(crate) mod tests;
