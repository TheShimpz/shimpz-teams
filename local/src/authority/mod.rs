//! Who may call the controller beyond its bearer: one-use, request-bound Supervisor and Routine assertions, and Team's
//! pin of the Supervisor's verification key.
//!
//! Team pins the Supervisor key Admin publishes the first time it verifies an assertion (trust on first use); from
//! then on only that pin verifies, only a rotation signed by the pinned key changes it, and only a bootstrap reset,
//! which first proves Admin publishes no key, removes it. The Routine key is read anew on every verification, so a
//! new Routine key fences every lease recorded under the old one. Every assertion is consumed once by one shared
//! [`ReplayGuard`].
//!
//! Every method here reads or writes small files and is meant for `spawn_blocking`.

pub mod assertion;
#[cfg(test)]
pub(crate) mod fixture;
pub mod key;
pub mod replay;

use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};

use strict_json::Value;
use subtle::ConstantTimeEq;
use team_protocol::python::value::{field, object, object_of, string, text};
use team_protocol::supervisor::{
    ASSERTION_AUDIENCE, JWT_HEADER, ROUTINE_AUDIENCE, ROUTINE_AUTHORITY, ROUTINE_JWT_HEADER,
    canonical_key_rotation,
};
use team_storage::custody::{self, DirectoryCustody, FileCustody, Mode};
use team_storage::durable;

pub use self::assertion::{MAX_ASSERTION_BYTES, RequestBinding, one_assertion};
use self::assertion::{bound, decode_base64url, verified_claims};
pub use self::key::PublicKey;
pub use self::replay::{MAX_REPLAY_ENTRIES, ReplayGuard};

/// The group that may read the published Supervisor and Routine keys.
pub const PUBLIC_KEY_GROUP: &str = "shimpzsupervisor-key";
/// The largest key file read.
const MAX_KEY_BYTES: u64 = 512;

/// Why an assertion or a key operation was refused; it never says which check failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The request does not carry valid evidence.
    Denied,
    /// The evidence could not be evaluated safely (a key unreadable or unsafe, the replay guard saturated).
    Unavailable,
}

/// The result of proving that Admin publishes no Supervisor key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Absence {
    /// A valid Supervisor key is published: a Supervisor is established.
    Established,
    /// The key directory or key could not be examined safely.
    Unavailable,
}

/// A Supervisor (or host-reset) assertion verified for one exact request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Evidence {
    /// The Supervisor's opaque 32-hex id.
    pub supervisor_id: String,
    /// The fingerprint of the pinned key that verified it.
    pub key_sha256: String,
    /// The authority it was signed under: `session` or `host-reset`.
    pub authority: String,
    /// The human assurance kind it bound, if any.
    pub assurance: Option<String>,
}

/// A Routine assertion verified for one exact request; the run then checks its lease against it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoutineEvidence {
    /// The fingerprint of the Routine key that signed it.
    pub key_fingerprint: String,
    /// The lease digest it bound.
    pub lease_sha256: String,
}

/// Where the keys live.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyFiles {
    /// The Supervisor key Admin publishes (`public.pem`).
    pub supervisor: PathBuf,
    /// The Routine key Admin publishes beside it (`routine.pem`).
    pub routine: PathBuf,
    /// Team's own pin of the Supervisor key, in its private settings volume.
    pub pin: PathBuf,
    /// The group of the published keys and their directory; `None` when the image names no such group, which leaves
    /// every assertion unavailable.
    pub key_group: Option<u32>,
}

/// The verifier of every assertion, and the custodian of the Supervisor key pin.
#[derive(Debug)]
pub struct Verifier {
    files: KeyFiles,
    replay: ReplayGuard,
    pin: Mutex<()>,
}

impl Verifier {
    /// A verifier of the keys in `files`, consuming assertions in `replay`.
    #[must_use]
    pub fn new(files: KeyFiles, replay: ReplayGuard) -> Self {
        Self {
            files,
            replay,
            pin: Mutex::new(()),
        }
    }

    /// Verifies and consumes the one Supervisor assertion of the `header` values for `request` at `now` (Unix
    /// seconds), under the pinned key (pinning Admin's published key first when there is no pin).
    ///
    /// # Errors
    /// [`Refusal::Denied`] for an invalid, mismatched, expired, or replayed assertion; [`Refusal::Unavailable`] when
    /// no key can be read safely or the replay guard is saturated.
    pub fn verify_supervisor(
        &self,
        header: &[&[u8]],
        request: &RequestBinding,
        now: i64,
    ) -> Result<Evidence, Refusal> {
        let encoded = one_assertion(header)?;
        let key = self.supervisor_key()?;
        let claims = verified_claims(encoded, &key, &JWT_HEADER, ASSERTION_AUDIENCE)?;
        self.consume(&claims, request, now)?;
        let member = |name: &str| claims.get(name).and_then(string).map(str::to_owned);
        Ok(Evidence {
            supervisor_id: member("sub").ok_or(Refusal::Denied)?,
            key_sha256: key.fingerprint(),
            authority: member("authority").ok_or(Refusal::Denied)?,
            assurance: claims
                .get("assurance")
                .and_then(|assurance| assurance.get("kind"))
                .and_then(string)
                .map(str::to_owned),
        })
    }

    /// Verifies and consumes the one Routine assertion of the `header` values for the request `method`, `path`, and
    /// `body` binding at `now`, under the Routine key read now. A run asks no model and no person, so it binds nothing
    /// else.
    ///
    /// # Errors
    /// As [`Verifier::verify_supervisor`], under the Routine key.
    pub fn verify_routine(
        &self,
        header: &[&[u8]],
        method: &str,
        path: &str,
        body: Value,
        now: i64,
    ) -> Result<RoutineEvidence, Refusal> {
        let key = self.published(&self.files.routine)?;
        let encoded = one_assertion(header)?;
        let claims = verified_claims(encoded, &key, &ROUTINE_JWT_HEADER, ROUTINE_AUDIENCE)?;
        let request = RequestBinding {
            method: method.to_owned(),
            path: path.to_owned(),
            body,
            model: None,
            decision: None,
            assurance: None,
            authorities: &[ROUTINE_AUTHORITY],
        };
        self.consume(&claims, &request, now)?;
        Ok(RoutineEvidence {
            key_fingerprint: key.fingerprint(),
            lease_sha256: claims
                .get("authority_sha256")
                .and_then(string)
                .ok_or(Refusal::Denied)?
                .to_owned(),
        })
    }

    /// The fingerprint of the current Routine key; a lease records it, so a new key fences every older lease.
    ///
    /// # Errors
    /// [`Refusal::Unavailable`] when the key cannot be read safely.
    pub fn routine_key_fingerprint(&self) -> Result<String, Refusal> {
        Ok(self.published(&self.files.routine)?.fingerprint())
    }

    /// Replaces the pin with the key of a rotation whose assertion the pinned key `signed_by` (its fingerprint)
    /// verified, and answers `{"rotated": true, "key_sha256": …}`.
    ///
    /// A retry of a rotation that already took effect succeeds unchanged, signed by either key; a rotation signed by
    /// a key that is no longer pinned is refused, so two rotations signed by one key never both take effect.
    ///
    /// # Errors
    /// [`Refusal::Denied`] for an invalid body or a stale signer; [`Refusal::Unavailable`] when the pin cannot be read
    /// or written.
    pub fn rotate(&self, body: &Value, signed_by: &str) -> Result<Value, Refusal> {
        let admitted = canonical_key_rotation(body).ok_or(Refusal::Denied)?;
        let encoded = admitted
            .get("public_key")
            .and_then(string)
            .ok_or(Refusal::Denied)?;
        let raw: [u8; 32] = decode_base64url(encoded)
            .and_then(|raw| raw.try_into().ok())
            .ok_or(Refusal::Denied)?;
        let new = PublicKey::from_raw(raw);
        let fingerprint = new.fingerprint();
        let _guard = self.pin.lock().unwrap_or_else(PoisonError::into_inner);
        let pinned = self.read_pin()?.ok_or(Refusal::Unavailable)?.fingerprint();
        if pinned != fingerprint {
            if !bool::from(pinned.as_bytes().ct_eq(signed_by.as_bytes())) {
                return Err(Refusal::Denied);
            }
            self.write_pin(&new)?;
        }
        Ok(object_of(vec![
            ("rotated", Value::Bool(true)),
            ("key_sha256", text(&fingerprint)),
        ]))
    }

    /// Removes the pin after a bootstrap reset proved that Admin publishes no Supervisor key; absence is success.
    ///
    /// # Errors
    /// [`Refusal::Unavailable`] when it cannot be removed durably.
    pub fn forget_pin(&self) -> Result<(), Refusal> {
        let _guard = self.pin.lock().unwrap_or_else(PoisonError::into_inner);
        match std::fs::remove_file(&self.files.pin) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(Refusal::Unavailable),
        }
        let parent = self.files.pin.parent().ok_or(Refusal::Unavailable)?;
        match durable::fsync_directory(parent) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(Refusal::Unavailable),
        }
    }

    /// Proves that Admin publishes no Supervisor key: its directory is in custody and holds no key.
    ///
    /// # Errors
    /// [`Absence::Established`] when a valid key is published; [`Absence::Unavailable`] when the directory or key
    /// cannot be examined safely, or a published key is invalid.
    pub fn require_supervisor_absent(&self) -> Result<(), Absence> {
        let group = self.files.key_group.ok_or(Absence::Unavailable)?;
        let path = &self.files.supervisor;
        let (Some(parent), Some(name)) = (path.parent(), path.file_name().and_then(|n| n.to_str()))
        else {
            return Err(Absence::Unavailable);
        };
        let directory = custody::open_directory(parent).map_err(|_| Absence::Unavailable)?;
        let facts = directory.metadata().map_err(|_| Absence::Unavailable)?;
        let directory_custody = DirectoryCustody {
            uid: None,
            gid: Some(group),
            mode: Mode::Exact(0o2770),
        };
        if !directory_custody.holds(&(&facts).into()) {
            return Err(Absence::Unavailable);
        }
        match custody::read_at(&directory, name, &published_custody(group), MAX_KEY_BYTES) {
            Ok(None) => Ok(()),
            Ok(Some(raw)) => match PublicKey::from_pem(&raw) {
                Some(_) => Err(Absence::Established),
                None => Err(Absence::Unavailable),
            },
            Err(_) => Err(Absence::Unavailable),
        }
    }

    /// The key every Supervisor assertion verifies under: the pin, first taken from Admin's published key.
    fn supervisor_key(&self) -> Result<PublicKey, Refusal> {
        let _guard = self.pin.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(pinned) = self.read_pin()? {
            return Ok(pinned);
        }
        let published = self.published(&self.files.supervisor)?;
        self.write_pin(&published)?;
        Ok(published)
    }

    /// A key Admin publishes: group-readable `0440` in the key group, owned by Admin, never absent.
    fn published(&self, path: &Path) -> Result<PublicKey, Refusal> {
        let group = self.files.key_group.ok_or(Refusal::Unavailable)?;
        custody::read(path, &published_custody(group), MAX_KEY_BYTES)
            .ok()
            .flatten()
            .and_then(|raw| PublicKey::from_pem(&raw))
            .ok_or(Refusal::Unavailable)
    }

    /// The pin, `None` before the first verification; an unsafe or damaged pin is unavailable.
    fn read_pin(&self) -> Result<Option<PublicKey>, Refusal> {
        let pin_custody = FileCustody {
            uid: Some(custody::current_uid()),
            gid: None,
            mode: Mode::Exact(0o600),
            size: Some((1, MAX_KEY_BYTES)),
        };
        match custody::read(&self.files.pin, &pin_custody, MAX_KEY_BYTES) {
            Ok(None) => Ok(None),
            Ok(Some(raw)) => PublicKey::from_pem(&raw)
                .map(Some)
                .ok_or(Refusal::Unavailable),
            Err(_) => Err(Refusal::Unavailable),
        }
    }

    /// Replaces the pin atomically and durably: a crash leaves the complete earlier key or the complete new one.
    fn write_pin(&self, key: &PublicKey) -> Result<(), Refusal> {
        let parent = self.files.pin.parent().ok_or(Refusal::Unavailable)?;
        durable::create_private_directory(parent)
            .and_then(|()| durable::replace(&self.files.pin, &key.to_pem(), 0o600, None))
            .map_err(|_| Refusal::Unavailable)
    }

    /// Checks the claims' binding of `request`, then consumes the assertion once.
    fn consume(&self, claims: &Value, request: &RequestBinding, now: i64) -> Result<(), Refusal> {
        let (assertion_id, expires_at) = bound(claims, request, now)?;
        self.replay.consume(&assertion_id, expires_at, now)
    }
}

fn published_custody(group: u32) -> FileCustody {
    // Admin publishes these keys, so their owner is not the Team controller; the group and mode are still exact.
    FileCustody {
        uid: None,
        gid: Some(group),
        mode: Mode::Exact(0o440),
        size: Some((1, MAX_KEY_BYTES)),
    }
}

/// The `challenge_id` member of an approval: exactly `{challenge_id, decision: "submit", value: true}`. Only an
/// approval of an authentication challenge binds a human assurance.
#[must_use]
pub fn approval(body: &Value) -> Option<&Value> {
    let members = object(body)?;
    let approval = members.len() == 3
        && members.iter().any(|(key, _)| key == "challenge_id")
        && string(field(members, "decision")) == Some("submit")
        && field(members, "value") == &Value::Bool(true);
    approval.then(|| field(members, "challenge_id"))
}

/// The assurance `{kind, challenge_id}` an approval naming `challenge_id` must bind, when the Team's `pending`
/// challenge (its request kind and id) is that challenge and asks for authentication.
#[must_use]
pub fn assurance(challenge_id: &Value, pending: Option<(&str, &str)>) -> Option<Value> {
    let (kind, id) = pending?;
    let admitted = string(challenge_id) == Some(id)
        && team_protocol::supervisor::ASSURANCE_KINDS.contains(&kind);
    admitted.then(|| object_of(vec![("kind", text(kind)), ("challenge_id", text(id))]))
}

#[cfg(test)]
mod tests;
