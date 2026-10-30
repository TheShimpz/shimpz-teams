//! Verified binary storage for canonical Assistant icons (`install/icons.py`).
//!
//! Icons live in provenance-separated namespaces (`published-<source>.png`, `local-<image>.png`). An install retains
//! its icon under a pin until its binding commits or fails. Reference-aware deletion reads the current bindings and
//! the pins under one custody lock and never removes a pinned or bound icon, so one Team's failed install or uninstall
//! cannot delete an icon that another Team is about to bind.

use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use rustix::fs::{Mode, OFlags};
use strict_json::Value;
use team_protocol::payload::SOURCE_DIGEST_RE;
use team_storage::durable;

use crate::bindings::{Binding, BindingError, Provenance};

/// The Developers source-package contract's icon limit; Local admission of a package enforces the same bound.
pub const MAX_ICON_BYTES: usize = 1024 * 1024;

/// An icon is missing, unavailable, or violates its binding.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IconError {
    /// The icon's identity or bytes are invalid.
    Invalid,
    /// The icon cannot be read, written, or removed.
    Unavailable,
}

impl std::fmt::Display for IconError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Invalid => "the Assistant icon is invalid",
            Self::Unavailable => "the Assistant icon is unavailable",
        })
    }
}

impl std::error::Error for IconError {}

/// Reads the current bindings; deletion calls it only inside the custody lock, so it sees every committed binding.
pub type References<'a> = &'a (dyn Fn() -> Result<Vec<Binding>, BindingError> + Send + Sync);

/// One icon's namespace, key, and expected digest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IconIdentity {
    provenance: Provenance,
    key: String,
    digest: String,
}

fn digest_member<'a>(document: &'a Value, key: &str) -> Option<&'a str> {
    match document.get(key) {
        Some(Value::String(digest)) if SOURCE_DIGEST_RE.is_full(digest) => Some(digest),
        _ => None,
    }
}

impl IconIdentity {
    /// A publication's icon: keyed by its source digest, verified by its icon digest.
    ///
    /// # Errors
    /// [`IconError::Invalid`] when either digest is missing or malformed.
    pub fn publication(resolution: &Value) -> Result<Self, IconError> {
        Self::of(Provenance::Published, resolution, "source_digest")
    }

    /// A Local snapshot's icon: keyed by its image id, verified by its icon digest.
    ///
    /// # Errors
    /// [`IconError::Invalid`] when either digest is missing or malformed.
    pub fn local(record: &Value) -> Result<Self, IconError> {
        Self::of(Provenance::Local, record, "image_id")
    }

    /// The icon of a binding's document.
    ///
    /// # Errors
    /// [`IconError::Invalid`] when its document names no valid icon.
    pub fn binding(binding: &Binding) -> Result<Self, IconError> {
        match binding.provenance {
            Provenance::Published => Self::publication(&binding.document),
            Provenance::Local => Self::local(&binding.document),
        }
    }

    fn of(provenance: Provenance, document: &Value, key: &str) -> Result<Self, IconError> {
        let (Some(key), Some(digest)) = (
            digest_member(document, key),
            digest_member(document, "icon_digest"),
        ) else {
            return Err(IconError::Invalid);
        };
        Ok(Self {
            provenance,
            key: key.to_owned(),
            digest: digest.to_owned(),
        })
    }

    fn reference(&self) -> (Provenance, &str) {
        (self.provenance, &self.key)
    }
}

fn reference(binding: &Binding) -> (Provenance, Option<&str>) {
    let key = match binding.provenance {
        Provenance::Published => "source_digest",
        Provenance::Local => "image_id",
    };
    let value = match binding.document.get(key) {
        Some(Value::String(text)) => Some(text.as_str()),
        _ => None,
    };
    (binding.provenance, value)
}

fn verify(contents: &[u8], expected: &str) -> Result<(), IconError> {
    let actual = format!(
        "sha256:{}",
        assistant_protocol::python::sha256_hex(contents)
    );
    if contents.is_empty() || contents.len() > MAX_ICON_BYTES || actual != expected {
        return Err(IconError::Invalid);
    }
    Ok(())
}

/// The icon store under one root directory.
#[derive(Debug)]
pub struct IconStore {
    root: PathBuf,
    pins: Mutex<HashMap<PathBuf, usize>>,
}

/// A held pin on one icon; [`Retained::release`] ends it and discards the icon unless a binding references it.
pub struct Retained<'a> {
    store: &'a IconStore,
    identity: IconIdentity,
    released: bool,
}

impl Retained<'_> {
    /// Ends the pin, then discards the icon unless a pin or a current binding still references it.
    ///
    /// # Errors
    /// [`IconError`] when the bindings cannot be read or the icon cannot be removed.
    pub fn release(mut self, references: References<'_>) -> Result<(), IconError> {
        self.released = true;
        self.store.unpin(&self.identity);
        self.store.discard(&self.identity, references)
    }
}

impl Drop for Retained<'_> {
    fn drop(&mut self) {
        if !self.released {
            self.store.unpin(&self.identity);
        }
    }
}

impl IconStore {
    /// The store under `root`.
    #[must_use]
    pub fn new(root: &Path) -> Arc<Self> {
        Arc::new(Self {
            root: root.to_owned(),
            pins: Mutex::new(HashMap::new()),
        })
    }

    fn custody(&self) -> MutexGuard<'_, HashMap<PathBuf, usize>> {
        self.pins.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn path(&self, identity: &IconIdentity) -> PathBuf {
        let hex = identity.key.trim_start_matches("sha256:");
        self.root
            .join(format!("{}-{hex}.png", identity.provenance.name()))
    }

    /// Pins `identity` and stores its verified `contents` while an install commits its binding.
    ///
    /// # Errors
    /// [`IconError::Invalid`] for bytes of another digest or conflicting with the stored icon; [`IconError::Unavailable`]
    /// when it cannot be persisted.
    pub fn retain(
        &self,
        identity: IconIdentity,
        contents: &[u8],
    ) -> Result<Retained<'_>, IconError> {
        *self.custody().entry(self.path(&identity)).or_default() += 1;
        let retained = Retained {
            store: self,
            identity,
            released: false,
        };
        self.put(&retained.identity, contents)?;
        Ok(retained)
    }

    fn unpin(&self, identity: &IconIdentity) {
        let path = self.path(identity);
        let mut pins = self.custody();
        if let Some(count) = pins.get_mut(&path) {
            *count -= 1;
            if *count == 0 {
                pins.remove(&path);
            }
        }
    }

    fn put(&self, identity: &IconIdentity, contents: &[u8]) -> Result<(), IconError> {
        verify(contents, &identity.digest)?;
        let destination = self.path(identity);
        if std::fs::symlink_metadata(&destination).is_ok() {
            return if self.read(identity)? == contents {
                Ok(())
            } else {
                Err(IconError::Invalid)
            };
        }
        durable::create_private_directory(&self.root).map_err(|_| IconError::Unavailable)?;
        durable::replace(&destination, contents, 0o600, None).map_err(|_| IconError::Unavailable)
    }

    /// The verified icon of `identity`.
    ///
    /// # Errors
    /// [`IconError`] when it is absent, not a regular file, oversized, or of another digest.
    pub fn read(&self, identity: &IconIdentity) -> Result<Vec<u8>, IconError> {
        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let file = File::from(
            rustix::fs::open(self.path(identity), flags, Mode::empty())
                .map_err(|_| IconError::Unavailable)?,
        );
        let metadata = file.metadata().map_err(|_| IconError::Unavailable)?;
        if !metadata.file_type().is_file() || metadata.len() > MAX_ICON_BYTES as u64 {
            return Err(IconError::Unavailable);
        }
        let mut contents = Vec::new();
        file.take(MAX_ICON_BYTES as u64 + 1)
            .read_to_end(&mut contents)
            .map_err(|_| IconError::Unavailable)?;
        verify(&contents, &identity.digest)?;
        Ok(contents)
    }

    /// Removes the icon unless a pin or a current binding references it.
    ///
    /// # Errors
    /// [`IconError::Unavailable`] when the bindings cannot be read or the icon cannot be removed.
    pub fn discard(
        &self,
        identity: &IconIdentity,
        references: References<'_>,
    ) -> Result<(), IconError> {
        let path = self.path(identity);
        let pins = self.custody();
        if pins.contains_key(&path) {
            return Ok(());
        }
        let bindings = references().map_err(|_| IconError::Unavailable)?;
        let (provenance, key) = identity.reference();
        if bindings
            .iter()
            .any(|binding| reference(binding) == (provenance, Some(key)))
        {
            return Ok(());
        }
        unlink(&path)
    }

    /// Deletes a binding and its icon in one custody transaction, keeping an icon another binding or install holds.
    ///
    /// The icon goes first: when its removal fails, the binding stays as the retry anchor.
    ///
    /// # Errors
    /// [`IconError`] for the bindings, the icon, or `delete_binding`'s own failure as [`IconError::Unavailable`].
    pub fn retire(
        &self,
        retiring: &Binding,
        references: References<'_>,
        delete_binding: impl FnOnce() -> Result<(), BindingError>,
    ) -> Result<(), IconError> {
        let identity = IconIdentity::binding(retiring)?;
        let path = self.path(&identity);
        let pins = self.custody();
        let others = references().map_err(|_| IconError::Unavailable)?;
        let (provenance, key) = identity.reference();
        let bound_elsewhere = others.iter().any(|binding| {
            (binding.team_id.as_str(), binding.assistant_id())
                != (retiring.team_id.as_str(), retiring.assistant_id())
                && reference(binding) == (provenance, Some(key))
        });
        if !pins.contains_key(&path) && !bound_elsewhere {
            unlink(&path)?;
        }
        delete_binding().map_err(|_| IconError::Unavailable)
    }
}

fn unlink(path: &Path) -> Result<(), IconError> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(IconError::Unavailable),
    }
}
