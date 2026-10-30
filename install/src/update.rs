//! Durable transactions for Assistant replacement, and the retryable exact-image cleanup queue (`install/update.py`).
//!
//! One canonical JSON transaction file per Team-owned Assistant (`updates/<team>--<assistant>.json`) records the
//! previous and successor bindings and the previous image while a replacement runs; each residue file
//! (`residues/<hex>.json`) names one exact image still to remove.

use std::path::{Path, PathBuf};

use strict_json::{Number, Value};
use team_protocol::payload::{SOURCE_DIGEST_RE, canonical_assistant_id, canonical_team_id};
use team_protocol::python::value::py_eq;
use team_storage::{durable, json_record};

use crate::bindings::{Binding, BindingError, BindingStore, Provenance, from_local_record};
use crate::lock::FileLock;

const UPDATE_FORMAT_VERSION: i64 = 2;
const RESIDUE_FORMAT_VERSION: i64 = 1;
const MAX_BYTES: usize = 2 * 1024 * 1024;
const MAX_RESIDUE_BYTES: usize = 1024;
const UPDATE_KEYS: [&str; 6] = [
    "version",
    "team_id",
    "assistant_id",
    "previous",
    "successor",
    "previous_image_id",
];

/// One active replacement of a Team's Assistant.
#[derive(Clone, Debug, PartialEq)]
pub struct AssistantUpdate {
    /// The Team.
    pub team_id: String,
    /// The Assistant.
    pub assistant_id: String,
    /// The binding being replaced.
    pub previous: Binding,
    /// The binding replacing it.
    pub successor: Binding,
    /// The image the previous binding ran.
    pub previous_image_id: String,
}

impl AssistantUpdate {
    fn encode(&self) -> Value {
        let text = |value: &str| Value::String(value.to_owned());
        Value::Object(vec![
            (
                "version".to_owned(),
                Value::Number(Number::Int(UPDATE_FORMAT_VERSION)),
            ),
            ("team_id".to_owned(), text(&self.team_id)),
            ("assistant_id".to_owned(), text(&self.assistant_id)),
            ("previous".to_owned(), self.previous.encode()),
            ("successor".to_owned(), self.successor.encode()),
            (
                "previous_image_id".to_owned(),
                text(&self.previous_image_id),
            ),
        ])
    }
}

fn digest(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) if SOURCE_DIGEST_RE.is_full(text) => Some(text),
        _ => None,
    }
}

/// A `sha256:` image id.
fn image(image_id: &str) -> Result<&str, BindingError> {
    if SOURCE_DIGEST_RE.is_full(image_id) {
        Ok(image_id)
    } else {
        Err(BindingError::Invalid)
    }
}

fn record_error(error: json_record::RecordError) -> BindingError {
    match error {
        json_record::RecordError::Unavailable => BindingError::Unavailable,
        json_record::RecordError::Invalid => BindingError::Invalid,
    }
}

fn remove_durably(path: &Path) -> Result<(), BindingError> {
    std::fs::remove_file(path).map_err(|_| BindingError::Unavailable)?;
    durable::fsync_directory(path.parent().unwrap_or(Path::new(".")))
        .map_err(|_| BindingError::Unavailable)
}

fn json_paths(root: &Path) -> Result<Vec<PathBuf>, BindingError> {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(BindingError::Unavailable),
    };
    let mut paths = Vec::new();
    for entry in entries {
        let path = entry.map_err(|_| BindingError::Unavailable)?.path();
        if path
            .extension()
            .is_some_and(|extension| extension == "json")
        {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}

/// The update transaction files of one root.
#[derive(Debug)]
pub struct UpdateStore<'a> {
    root: PathBuf,
    bindings: &'a BindingStore,
}

impl<'a> UpdateStore<'a> {
    /// The transactions under `root`, decoding bindings as `bindings` admits them.
    #[must_use]
    pub fn new(root: &Path, bindings: &'a BindingStore) -> Self {
        Self {
            root: root.to_owned(),
            bindings,
        }
    }

    fn path(&self, team_id: &str, assistant_id: &str) -> Result<PathBuf, BindingError> {
        let valid = canonical_team_id(&Value::String(team_id.to_owned())).is_some()
            && canonical_assistant_id(&Value::String(assistant_id.to_owned())).is_some();
        if valid {
            Ok(self.root.join(format!("{team_id}--{assistant_id}.json")))
        } else {
            Err(BindingError::Invalid)
        }
    }

    /// Begins replacing `previous` with `successor_document`; an identical active transaction is returned unchanged.
    ///
    /// # Errors
    /// [`BindingError::Conflict`] for another active transaction, a successor of another Assistant or identical to the
    /// previous binding, or an invalid image id; [`BindingError`] for a refused successor or the files.
    pub fn begin(
        &self,
        previous: &Binding,
        successor_document: Value,
        previous_image_id: &str,
    ) -> Result<AssistantUpdate, BindingError> {
        let successor = match previous.provenance {
            Provenance::Published => self
                .bindings
                .resolution_binding(&previous.team_id, successor_document)?,
            Provenance::Local => from_local_record(
                &previous.team_id,
                successor_document,
                self.bindings.validator(),
            )?,
        };
        if successor.assistant_id() != previous.assistant_id()
            || successor == *previous
            || !SOURCE_DIGEST_RE.is_full(previous_image_id)
        {
            return Err(BindingError::Conflict);
        }
        let update = AssistantUpdate {
            team_id: previous.team_id.clone(),
            assistant_id: previous.assistant_id().to_owned(),
            previous: previous.clone(),
            successor,
            previous_image_id: previous_image_id.to_owned(),
        };
        let path = self.path(&update.team_id, &update.assistant_id)?;
        let _lock = FileLock::exclusive(&path.with_extension("lock"))
            .map_err(|_| BindingError::Unavailable)?;
        match self.read(&path)? {
            Some(current) if current == update => Ok(current),
            Some(_) => Err(BindingError::Conflict),
            None => {
                json_record::write(&path, &update.encode(), MAX_BYTES).map_err(record_error)?;
                Ok(update)
            }
        }
    }

    /// The active transaction of one Team's Assistant.
    ///
    /// # Errors
    /// [`BindingError`] for an invalid identity, a misnamed or malformed transaction, or the files.
    pub fn get(
        &self,
        team_id: &str,
        assistant_id: &str,
    ) -> Result<Option<AssistantUpdate>, BindingError> {
        let path = self.path(team_id, assistant_id)?;
        let _lock = FileLock::shared(&path.with_extension("lock"))
            .map_err(|_| BindingError::Unavailable)?;
        let update = self.read(&path)?;
        if update.as_ref().is_some_and(|update| {
            (update.team_id.as_str(), update.assistant_id.as_str()) != (team_id, assistant_id)
        }) {
            return Err(BindingError::Invalid);
        }
        Ok(update)
    }

    /// Every active transaction, sorted by Team and Assistant.
    ///
    /// # Errors
    /// [`BindingError`] for a misnamed or malformed transaction or the files.
    pub fn list(&self) -> Result<Vec<AssistantUpdate>, BindingError> {
        let mut updates = Vec::new();
        for path in json_paths(&self.root)? {
            let _lock = FileLock::shared(&path.with_extension("lock"))
                .map_err(|_| BindingError::Unavailable)?;
            if let Some(update) = self.read(&path)? {
                if self.path(&update.team_id, &update.assistant_id)? != path {
                    return Err(BindingError::Invalid);
                }
                updates.push(update);
            }
        }
        updates.sort_by(|left, right| {
            (&left.team_id, &left.assistant_id).cmp(&(&right.team_id, &right.assistant_id))
        });
        Ok(updates)
    }

    /// Ends `update`; an absent transaction is already ended.
    ///
    /// # Errors
    /// [`BindingError::Conflict`] when another transaction took its place; [`BindingError`] for the files.
    pub fn clear(&self, update: &AssistantUpdate) -> Result<(), BindingError> {
        let path = self.path(&update.team_id, &update.assistant_id)?;
        let _lock = FileLock::exclusive(&path.with_extension("lock"))
            .map_err(|_| BindingError::Unavailable)?;
        match self.read(&path)? {
            None => Ok(()),
            Some(current) if current == *update => remove_durably(&path),
            Some(_) => Err(BindingError::Conflict),
        }
    }

    fn read(&self, path: &Path) -> Result<Option<AssistantUpdate>, BindingError> {
        json_record::read(path, MAX_BYTES)
            .map_err(record_error)?
            .map(|value| self.decode(&value))
            .transpose()
    }

    fn decode(&self, value: &Value) -> Result<AssistantUpdate, BindingError> {
        let shaped = matches!(value, Value::Object(members) if members.len() == UPDATE_KEYS.len()
            && UPDATE_KEYS.iter().all(|key| value.get(key).is_some()))
            && value.get("version").is_some_and(|version| {
                py_eq(version, &Value::Number(Number::Int(UPDATE_FORMAT_VERSION)))
            });
        let field = |key: &str| value.get(key).unwrap_or(&Value::Null);
        let (true, Some(image)) = (shaped, digest(field("previous_image_id"))) else {
            return Err(BindingError::Invalid);
        };
        let previous = self.bindings.decode(field("previous"))?;
        let successor = self.bindings.decode(field("successor"))?;
        let same = |binding: &Binding| {
            field("assistant_id") == &Value::String(binding.assistant_id().to_owned())
                && field("team_id") == &Value::String(binding.team_id.clone())
        };
        if !same(&previous) || !same(&successor) || previous.provenance != successor.provenance {
            return Err(BindingError::Invalid);
        }
        Ok(AssistantUpdate {
            team_id: previous.team_id.clone(),
            assistant_id: previous.assistant_id().to_owned(),
            previous,
            successor,
            previous_image_id: image.to_owned(),
        })
    }
}

/// The retryable exact-image cleanup queue.
#[derive(Debug)]
pub struct ResidueStore {
    root: PathBuf,
}

impl ResidueStore {
    /// The queue under `root`, locked through `<root>/.lock`.
    #[must_use]
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_owned(),
        }
    }

    fn lock(&self, exclusive: bool) -> Result<FileLock, BindingError> {
        let path = self.root.join(".lock");
        if exclusive {
            FileLock::exclusive(&path)
        } else {
            FileLock::shared(&path)
        }
        .map_err(|_| BindingError::Unavailable)
    }

    fn path(&self, image_id: &str) -> PathBuf {
        self.root
            .join(format!("{}.json", image_id.trim_start_matches("sha256:")))
    }

    /// Queues the exact image `image_id` for removal.
    ///
    /// # Errors
    /// [`BindingError::Invalid`] for another value than a `sha256:` image id; [`BindingError`] for the files.
    pub fn add(&self, image_id: &str) -> Result<(), BindingError> {
        let image = image(image_id)?;
        let path = self.path(image);
        let _lock = self.lock(true)?;
        match Self::read(&path)? {
            Some(current) if current == image => Ok(()),
            Some(_) => Err(BindingError::Invalid),
            None => {
                let record = Value::Object(vec![
                    (
                        "version".to_owned(),
                        Value::Number(Number::Int(RESIDUE_FORMAT_VERSION)),
                    ),
                    ("image_id".to_owned(), Value::String(image.to_owned())),
                ]);
                json_record::write(&path, &record, MAX_RESIDUE_BYTES).map_err(record_error)
            }
        }
    }

    /// Every queued image id, sorted.
    ///
    /// # Errors
    /// [`BindingError`] for a misnamed or malformed residue or the files.
    pub fn list(&self) -> Result<Vec<String>, BindingError> {
        let _lock = self.lock(false)?;
        let mut residues = Vec::new();
        for path in json_paths(&self.root)? {
            if let Some(image) = Self::read(&path)? {
                if self.path(&image) != path {
                    return Err(BindingError::Invalid);
                }
                residues.push(image);
            }
        }
        residues.sort();
        Ok(residues)
    }

    /// Removes the queued `image_id`; an absent one is already cleared.
    ///
    /// # Errors
    /// [`BindingError`] for the files.
    pub fn clear(&self, image_id: &str) -> Result<(), BindingError> {
        let image = image(image_id)?;
        let path = self.path(image);
        let _lock = self.lock(true)?;
        match Self::read(&path)? {
            None => Ok(()),
            Some(current) if current == image => remove_durably(&path),
            Some(_) => Err(BindingError::Invalid),
        }
    }

    fn read(path: &Path) -> Result<Option<String>, BindingError> {
        let Some(value) = json_record::read(path, MAX_RESIDUE_BYTES).map_err(record_error)? else {
            return Ok(None);
        };
        let shaped = matches!(&value, Value::Object(members) if members.len() == 2)
            && value.get("version").is_some_and(|version| {
                py_eq(version, &Value::Number(Number::Int(RESIDUE_FORMAT_VERSION)))
            });
        match value.get("image_id").and_then(digest) {
            Some(image) if shaped => Ok(Some(image.to_owned())),
            _ => Err(BindingError::Invalid),
        }
    }
}
