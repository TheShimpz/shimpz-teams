//! Durable, fail-closed bindings for installed Assistants (`install/bindings.py`).
//!
//! One controller-private registry file (`publications/bindings.json`, canonical JSON v1) holds every Team's bindings
//! under an advisory lock. Each binding carries the digest of its exact canonical identity, checked on every read
//! before the current contract admits it again; a binding whose digest does not match was not written by Team, and a
//! refused binding fails the whole read.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};

use strict_json::{Number, Value};
use team_assistant::manifest::RESERVED_ASSISTANT_IDS;
use team_protocol::payload::{
    MAX_TEAM_ASSISTANTS, SOURCE_DIGEST_RE, canonical_assistant_id, canonical_team_id,
};
use team_protocol::python::json::canonical;
use team_protocol::python::value::py_eq;
use team_storage::json_record;

use crate::lock::FileLock;

const FORMAT_VERSION: i64 = 2;
const MAX_BINDINGS: usize = 4096;
/// The largest registry file.
pub const MAX_FILE_BYTES: usize = 8 * 1024 * 1024;
/// How many admitted binding digests a store remembers.
const MAX_ADMISSIONS: usize = 1024;

/// Why a binding operation failed; never with the document's content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BindingError {
    /// The registry or its lock cannot be read or written.
    Unavailable,
    /// A value violates the binding contract, or the registry is malformed.
    Invalid,
    /// The Team already binds this Assistant id to another artifact, or the binding changed underneath.
    Conflict,
    /// The Team already has the most Assistants one Team may have installed.
    LimitReached,
}

impl std::fmt::Display for BindingError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "the Assistant registry is unavailable",
            Self::Invalid => "the Assistant registry or binding is invalid",
            Self::Conflict => "the Assistant binding conflicts with the registry",
            Self::LimitReached => "the Team already has the maximum number of Assistants",
        })
    }
}

impl std::error::Error for BindingError {}

/// Where a binding's artifact came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Provenance {
    /// A Developers publication; the document is its resolution.
    Published,
    /// A staged Local snapshot; the document is its record.
    Local,
}

impl Provenance {
    /// The persisted name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Published => "published",
            Self::Local => "local",
        }
    }

    /// The member that holds the document.
    #[must_use]
    pub const fn document_name(self) -> &'static str {
        match self {
            Self::Published => "resolution",
            Self::Local => "local_record",
        }
    }
}

/// One Team's binding of one Assistant id to one artifact.
#[derive(Clone, Debug)]
pub struct Binding {
    /// The Team.
    pub team_id: String,
    /// `sha256:` over the canonical binding identity.
    pub binding_digest: String,
    /// Where the artifact came from.
    pub provenance: Provenance,
    /// The resolution or Local record.
    pub document: Value,
}

impl PartialEq for Binding {
    fn eq(&self, other: &Self) -> bool {
        (&self.team_id, &self.binding_digest, self.provenance)
            == (&other.team_id, &other.binding_digest, other.provenance)
            && py_eq(&self.document, &other.document)
    }
}

impl Binding {
    /// The bound Assistant id.
    #[must_use]
    pub fn assistant_id(&self) -> &str {
        match self.document.get("assistant_id") {
            Some(Value::String(id)) => id,
            _ => "",
        }
    }

    /// The persisted object.
    #[must_use]
    pub fn encode(&self) -> Value {
        Value::Object(vec![
            ("team_id".to_owned(), Value::String(self.team_id.clone())),
            (
                "binding_digest".to_owned(),
                Value::String(self.binding_digest.clone()),
            ),
            (
                "provenance".to_owned(),
                Value::String(self.provenance.name().to_owned()),
            ),
            (
                self.provenance.document_name().to_owned(),
                self.document.clone(),
            ),
        ])
    }
}

/// Admits one resolution document; production is always the Assistant-install contract.
pub type ResolutionAdmission = fn(&Value) -> bool;

/// Validates one Local snapshot record; the Local install service supplies it.
pub type LocalRecordValidator = Arc<dyn Fn(&Value) -> bool + Send + Sync>;

fn identity(team_id: &str, assistant_id: &Value) -> Result<(), BindingError> {
    let team = Value::String(team_id.to_owned());
    match (
        canonical_team_id(&team),
        canonical_assistant_id(assistant_id),
    ) {
        (Some(_), Some(assistant)) if !RESERVED_ASSISTANT_IDS.contains(&assistant) => Ok(()),
        _ => Err(BindingError::Invalid),
    }
}

/// The binding of an intact document, digested over its canonical identity.
///
/// # Errors
/// [`BindingError::Invalid`] for an invalid Team or Assistant id or a reserved Assistant id.
pub fn intact(
    team_id: &str,
    provenance: Provenance,
    document: Value,
) -> Result<Binding, BindingError> {
    identity(
        team_id,
        document.get("assistant_id").unwrap_or(&Value::Null),
    )?;
    let identity = Value::Object(vec![
        (
            "version".to_owned(),
            Value::Number(Number::Int(FORMAT_VERSION)),
        ),
        ("team_id".to_owned(), Value::String(team_id.to_owned())),
        (
            "provenance".to_owned(),
            Value::String(provenance.name().to_owned()),
        ),
        (provenance.document_name().to_owned(), document.clone()),
    ]);
    let digest = format!(
        "sha256:{}",
        assistant_protocol::python::sha256_hex(&canonical(&identity))
    );
    Ok(Binding {
        team_id: team_id.to_owned(),
        binding_digest: digest,
        provenance,
        document,
    })
}

/// Admits a resolution under the current Assistant-install contract.
fn contract_admits(resolution: &Value) -> bool {
    crate::contract::validate(resolution).is_ok()
}

fn admitted_resolution(
    team_id: &str,
    resolution: Value,
    admission: ResolutionAdmission,
) -> Result<Binding, BindingError> {
    if canonical_team_id(&Value::String(team_id.to_owned())).is_none()
        || !matches!(resolution, Value::Object(_))
        || !admission(&resolution)
    {
        return Err(BindingError::Invalid);
    }
    intact(team_id, Provenance::Published, resolution)
}

/// A Local binding whose record `validator` admits.
///
/// # Errors
/// [`BindingError::Invalid`] for a refused record or identity.
pub fn from_local_record(
    team_id: &str,
    record: Value,
    validator: &LocalRecordValidator,
) -> Result<Binding, BindingError> {
    if canonical_team_id(&Value::String(team_id.to_owned())).is_none()
        || !matches!(record, Value::Object(_))
        || !validator(&record)
    {
        return Err(BindingError::Invalid);
    }
    intact(team_id, Provenance::Local, record)
}

/// The controller-private registry shared by every request.
pub struct BindingStore {
    path: PathBuf,
    lock_path: PathBuf,
    validator: LocalRecordValidator,
    resolution_admission: ResolutionAdmission,
    admissions: Mutex<VecDeque<String>>,
}

impl std::fmt::Debug for BindingStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("BindingStore")
            .field("path", &self.path)
            .finish_non_exhaustive()
    }
}

impl BindingStore {
    /// The registry at `path`, locked through `<path>.lock`.
    #[must_use]
    pub fn new(path: &Path, validator: LocalRecordValidator) -> Self {
        let mut lock_path = path.as_os_str().to_owned();
        lock_path.push(".lock");
        Self {
            path: path.to_owned(),
            lock_path: PathBuf::from(lock_path),
            validator,
            resolution_admission: contract_admits,
            admissions: Mutex::new(VecDeque::new()),
        }
    }

    /// The same registry admitting any resolution object, as the controller suite's loopback registry needs (its
    /// images live outside the production repository). Never compiled into a shipped binary.
    #[cfg(feature = "testing")]
    #[must_use]
    pub fn admitting_any_resolution(mut self) -> Self {
        self.resolution_admission = |_| true;
        self
    }

    /// The binding of a resolution this registry admits.
    ///
    /// # Errors
    /// [`BindingError::Invalid`] for a refused resolution or identity.
    pub fn resolution_binding(
        &self,
        team_id: &str,
        resolution: Value,
    ) -> Result<Binding, BindingError> {
        admitted_resolution(team_id, resolution, self.resolution_admission)
    }

    /// The Local record validator bindings are admitted with.
    #[must_use]
    pub fn validator(&self) -> &LocalRecordValidator {
        &self.validator
    }

    fn exclusive(&self) -> Result<FileLock, BindingError> {
        FileLock::exclusive(&self.lock_path).map_err(|_| BindingError::Unavailable)
    }

    fn shared(&self) -> Result<FileLock, BindingError> {
        FileLock::shared(&self.lock_path).map_err(|_| BindingError::Unavailable)
    }

    /// Binds a publication; an identical binding is returned unchanged with `false`.
    ///
    /// # Errors
    /// [`BindingError`] for a refused resolution, another artifact under the id, the Team limit, or the registry.
    pub fn put(&self, team_id: &str, resolution: Value) -> Result<(Binding, bool), BindingError> {
        self.insert(self.resolution_binding(team_id, resolution)?)
    }

    /// Binds a Local snapshot record; an identical binding is returned unchanged with `false`.
    ///
    /// # Errors
    /// As [`Self::put`].
    pub fn put_local(&self, team_id: &str, record: Value) -> Result<(Binding, bool), BindingError> {
        self.insert(from_local_record(team_id, record, &self.validator)?)
    }

    fn insert(&self, binding: Binding) -> Result<(Binding, bool), BindingError> {
        let _lock = self.exclusive()?;
        let mut bindings = self.read()?;
        if let Some(existing) = find(&bindings, &binding.team_id, binding.assistant_id()) {
            return if *existing == binding {
                Ok((existing.clone(), false))
            } else {
                Err(BindingError::Conflict)
            };
        }
        if bindings
            .iter()
            .filter(|current| current.team_id == binding.team_id)
            .count()
            >= MAX_TEAM_ASSISTANTS
        {
            return Err(BindingError::LimitReached);
        }
        if bindings.len() >= MAX_BINDINGS {
            return Err(BindingError::Invalid);
        }
        bindings.push(binding.clone());
        self.write(&bindings)?;
        Ok((binding, true))
    }

    /// The binding of one Assistant id in one Team.
    ///
    /// # Errors
    /// [`BindingError`] for an invalid identity or the registry.
    pub fn get(&self, team_id: &str, assistant_id: &str) -> Result<Option<Binding>, BindingError> {
        identity(team_id, &Value::String(assistant_id.to_owned()))?;
        let _lock = self.shared()?;
        Ok(find(&self.read()?, team_id, assistant_id).cloned())
    }

    /// Replaces the publication binding whose digest is `expected` with `resolution`'s.
    ///
    /// # Errors
    /// [`BindingError::Conflict`] when the binding changed, its provenance differs, or `expected` is no digest.
    pub fn replace(
        &self,
        team_id: &str,
        expected: &str,
        resolution: Value,
    ) -> Result<Binding, BindingError> {
        self.swap(self.resolution_binding(team_id, resolution)?, expected)
    }

    /// Replaces the Local binding whose digest is `expected` with `record`'s.
    ///
    /// # Errors
    /// As [`Self::replace`].
    pub fn replace_local(
        &self,
        team_id: &str,
        expected: &str,
        record: Value,
    ) -> Result<Binding, BindingError> {
        self.swap(
            from_local_record(team_id, record, &self.validator)?,
            expected,
        )
    }

    fn swap(&self, replacement: Binding, expected: &str) -> Result<Binding, BindingError> {
        if !SOURCE_DIGEST_RE.is_full(expected) {
            return Err(BindingError::Conflict);
        }
        let _lock = self.exclusive()?;
        let mut bindings = self.read()?;
        let index = bindings
            .iter()
            .position(|binding| {
                binding.team_id == replacement.team_id
                    && binding.assistant_id() == replacement.assistant_id()
            })
            .filter(|index| bindings[*index].binding_digest == expected)
            .ok_or(BindingError::Conflict)?;
        if bindings[index].provenance != replacement.provenance {
            return Err(BindingError::Conflict);
        }
        if bindings[index] == replacement {
            return Ok(replacement);
        }
        bindings[index] = replacement.clone();
        self.write(&bindings)?;
        Ok(replacement)
    }

    /// One Team's bindings sorted by Assistant id.
    ///
    /// # Errors
    /// [`BindingError`] for an invalid Team id or the registry.
    pub fn list(&self, team_id: &str) -> Result<Vec<Binding>, BindingError> {
        if canonical_team_id(&Value::String(team_id.to_owned())).is_none() {
            return Err(BindingError::Invalid);
        }
        let _lock = self.shared()?;
        let mut bindings: Vec<Binding> = self
            .read()?
            .into_iter()
            .filter(|binding| binding.team_id == team_id)
            .collect();
        bindings.sort_by(|left, right| left.assistant_id().cmp(right.assistant_id()));
        Ok(bindings)
    }

    /// One validated point-in-time view of every binding.
    ///
    /// # Errors
    /// [`BindingError`] for the registry.
    pub fn snapshot(&self) -> Result<Vec<Binding>, BindingError> {
        let _lock = self.shared()?;
        self.read()
    }

    /// Removes one binding; `false` when there was none.
    ///
    /// # Errors
    /// [`BindingError`] for an invalid identity or the registry.
    pub fn delete(&self, team_id: &str, assistant_id: &str) -> Result<bool, BindingError> {
        identity(team_id, &Value::String(assistant_id.to_owned()))?;
        let _lock = self.exclusive()?;
        let mut bindings = self.read()?;
        let before = bindings.len();
        bindings.retain(|binding| {
            !(binding.team_id == team_id && binding.assistant_id() == assistant_id)
        });
        if bindings.len() == before {
            return Ok(false);
        }
        self.write(&bindings)?;
        Ok(true)
    }

    /// Removes one binding only while its digest is still `expected`.
    ///
    /// # Errors
    /// [`BindingError::Conflict`] when `expected` is no digest; [`BindingError`] for the registry.
    pub fn delete_if_matches(
        &self,
        team_id: &str,
        assistant_id: &str,
        expected: &str,
    ) -> Result<bool, BindingError> {
        identity(team_id, &Value::String(assistant_id.to_owned()))?;
        if !SOURCE_DIGEST_RE.is_full(expected) {
            return Err(BindingError::Conflict);
        }
        let _lock = self.exclusive()?;
        let mut bindings = self.read()?;
        let Some(index) = bindings.iter().position(|binding| {
            binding.team_id == team_id
                && binding.assistant_id() == assistant_id
                && binding.binding_digest == expected
        }) else {
            return Ok(false);
        };
        bindings.remove(index);
        self.write(&bindings)?;
        Ok(true)
    }

    fn read(&self) -> Result<Vec<Binding>, BindingError> {
        let document = match json_record::read(&self.path, MAX_FILE_BYTES) {
            Ok(None) => return Ok(Vec::new()),
            Ok(Some(document)) => document,
            Err(json_record::RecordError::Unavailable) => return Err(BindingError::Unavailable),
            Err(json_record::RecordError::Invalid) => return Err(BindingError::Invalid),
        };
        let shaped = matches!(&document, Value::Object(members) if members.len() == 2)
            && document
                .get("version")
                .is_some_and(|version| py_eq(version, &Value::Number(Number::Int(FORMAT_VERSION))));
        let Some(Value::Array(values)) = document.get("bindings").filter(|_| shaped) else {
            return Err(BindingError::Invalid);
        };
        if values.len() > MAX_BINDINGS {
            return Err(BindingError::Invalid);
        }
        let bindings = values
            .iter()
            .map(|value| self.decode(value))
            .collect::<Result<Vec<_>, _>>()?;
        let keys: Vec<(&str, &str)> = bindings
            .iter()
            .map(|binding| (binding.team_id.as_str(), binding.assistant_id()))
            .collect();
        if keys
            .iter()
            .enumerate()
            .any(|(index, key)| keys[..index].contains(key))
        {
            return Err(BindingError::Invalid);
        }
        Ok(bindings)
    }

    fn write(&self, bindings: &[Binding]) -> Result<(), BindingError> {
        let mut sorted: Vec<&Binding> = bindings.iter().collect();
        sorted.sort_by(|left, right| {
            (left.team_id.as_str(), left.assistant_id())
                .cmp(&(right.team_id.as_str(), right.assistant_id()))
        });
        let document = Value::Object(vec![
            (
                "version".to_owned(),
                Value::Number(Number::Int(FORMAT_VERSION)),
            ),
            (
                "bindings".to_owned(),
                Value::Array(sorted.into_iter().map(Binding::encode).collect()),
            ),
        ]);
        json_record::write(&self.path, &document, MAX_FILE_BYTES).map_err(|error| match error {
            json_record::RecordError::Invalid => BindingError::Invalid,
            json_record::RecordError::Unavailable => BindingError::Unavailable,
        })
    }

    /// Decodes one persisted binding: integrity first, then the current contract's admission.
    ///
    /// # Errors
    /// [`BindingError::Invalid`] for a malformed, tampered, or refused binding.
    pub fn decode(&self, value: &Value) -> Result<Binding, BindingError> {
        let provenance = match value.get("provenance") {
            Some(Value::String(name)) if name == "published" => Provenance::Published,
            Some(Value::String(name)) if name == "local" => Provenance::Local,
            _ => return Err(BindingError::Invalid),
        };
        let shaped = matches!(value, Value::Object(members) if members.len() == 4
            && ["team_id", "binding_digest", "provenance", provenance.document_name()].iter().all(|key| value.get(key).is_some()));
        let (true, Some(Value::String(team_id)), Some(document @ Value::Object(_))) = (
            shaped,
            value.get("team_id"),
            value.get(provenance.document_name()),
        ) else {
            return Err(BindingError::Invalid);
        };
        let binding = intact(team_id, provenance, document.clone())?;
        if value.get("binding_digest") != Some(&Value::String(binding.binding_digest.clone())) {
            return Err(BindingError::Invalid);
        }
        self.admit(&binding)?;
        Ok(binding)
    }

    /// Admits an intact binding under the current contract, once per binding digest.
    fn admit(&self, binding: &Binding) -> Result<(), BindingError> {
        let mut admissions = self
            .admissions
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if let Some(index) = admissions
            .iter()
            .position(|digest| *digest == binding.binding_digest)
        {
            let digest = admissions.remove(index).unwrap_or_default();
            admissions.push_back(digest);
            return Ok(());
        }
        drop(admissions);
        match binding.provenance {
            Provenance::Published => {
                self.resolution_binding(&binding.team_id, binding.document.clone())?
            }
            Provenance::Local => {
                from_local_record(&binding.team_id, binding.document.clone(), &self.validator)?
            }
        };
        let mut admissions = self
            .admissions
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        admissions.push_back(binding.binding_digest.clone());
        while admissions.len() > MAX_ADMISSIONS {
            admissions.pop_front();
        }
        Ok(())
    }
}

fn find<'a>(bindings: &'a [Binding], team_id: &str, assistant_id: &str) -> Option<&'a Binding> {
    bindings
        .iter()
        .find(|binding| binding.team_id == team_id && binding.assistant_id() == assistant_id)
}
