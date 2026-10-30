//! The policy store: under the shared policy directory (`0750`, the egress group), one owner-only token file per
//! Assistant identity in `.tokens/` (`<sha256(identity)>.token`, exactly 32 hex digits and a newline, `0600`), and one
//! policy file per token (`<token>.json`, the canonical hosts, `0640` in the egress group) that the egress proxy reads.
//!
//! Team presents the token for the Assistant's provider calls; no workload ever receives it (ADR-0106). Every file is
//! read with its exact custody and must equal its canonical bytes; every write is durable.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use team_core::entropy::{Entropy, token_hex};
use team_core::secret::Secret;
use team_storage::custody::{self, DirectoryCustody, FileCustody, Mode, current_uid};
use team_storage::durable;

use crate::hosts::{canonical_allowed_hosts, encoded_hosts};

/// The largest policy file.
pub const MAX_POLICY_BYTES: u64 = 16 * 1024;
/// The exact size of a token file: 32 hex digits and a newline.
pub const TOKEN_FILE_BYTES: u64 = 33;

/// Why the policy store refused; it never carries a token, a host, or a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PolicyError {
    /// The store could not complete an otherwise valid operation.
    Unavailable,
    /// Persisted policy state, or the policy asked for, does not satisfy its contract.
    Drift,
}

impl std::fmt::Display for PolicyError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "the egress policy store is unavailable",
            Self::Drift => "the egress policy drifted from its contract",
        })
    }
}

impl std::error::Error for PolicyError {}

/// One Assistant's admitted policy: its token and its reviewed hosts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Admitted {
    /// The token Team presents for the Assistant's provider calls.
    pub token: Secret,
    /// The canonical hosts the proxy admits for it.
    pub hosts: Vec<String>,
}

/// The shared policy directory and the egress group that reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PolicyStore {
    root: PathBuf,
    policy_gid: u32,
}

impl PolicyStore {
    /// The store at `root`, read by the proxy through group `policy_gid`.
    #[must_use]
    pub fn new(root: PathBuf, policy_gid: u32) -> Self {
        Self { root, policy_gid }
    }

    fn require_root(&self) -> Result<&Path, PolicyError> {
        let facts = custody::facts(&self.root)
            .ok()
            .flatten()
            .ok_or(PolicyError::Unavailable)?;
        let required = DirectoryCustody {
            uid: None,
            gid: Some(self.policy_gid),
            mode: Mode::Exact(0o750),
        };
        if required.holds(&facts) {
            Ok(&self.root)
        } else {
            Err(PolicyError::Drift)
        }
    }

    fn token_directory(&self) -> Result<PathBuf, PolicyError> {
        let directory = self.require_root()?.join(".tokens");
        durable::ensure_directory(&directory, &DirectoryCustody::private()).map_err(|error| {
            match error {
                durable::DirectoryError::Unavailable => PolicyError::Unavailable,
                durable::DirectoryError::Unsafe => PolicyError::Drift,
            }
        })?;
        Ok(directory)
    }

    fn token_path(&self, identity: &str) -> Result<PathBuf, PolicyError> {
        if identity.is_empty() {
            return Err(PolicyError::Drift);
        }
        let name = sha256_hex(identity);
        Ok(self.token_directory()?.join(format!("{name}.token")))
    }

    /// The token of `identity`; with `create`, a fresh one is durably stored first when none exists.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] for an empty identity or a token outside its custody or canonical form;
    /// [`PolicyError::Unavailable`] when the store or a new token cannot be written.
    pub fn token(
        &self,
        identity: &str,
        create: bool,
        entropy: &dyn Entropy,
    ) -> Result<Option<Secret>, PolicyError> {
        let path = self.token_path(identity)?;
        match custody::facts(&path) {
            Ok(Some(_)) => return read_token(&path).map(Some),
            Ok(None) if !create => return Ok(None),
            Ok(None) => {}
            Err(_) => return Err(PolicyError::Drift),
        }
        let token = token_hex(entropy, 16).map_err(|_| PolicyError::Unavailable)?;
        durable::replace(&path, format!("{token}\n").as_bytes(), 0o600, None)
            .map_err(|_| PolicyError::Unavailable)?;
        read_token(&path).map(Some)
    }

    /// Writes `token`'s policy: `hosts`, which must already be canonical and non-empty.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] for a malformed token, non-canonical hosts, or a written file outside its custody;
    /// [`PolicyError::Unavailable`] when it cannot be written.
    pub fn write(&self, token: &Secret, hosts: &[String]) -> Result<(), PolicyError> {
        if !canonical_token(token.expose()) {
            return Err(PolicyError::Drift);
        }
        let encoded = encoded_hosts(&canonical_hosts(hosts)?);
        let path = self
            .require_root()?
            .join(format!("{}.json", token.expose()));
        durable::replace(&path, &encoded, 0o640, Some(self.policy_gid))
            .map_err(|_| PolicyError::Unavailable)?;
        let facts = custody::facts(&path)
            .ok()
            .flatten()
            .ok_or(PolicyError::Unavailable)?;
        if self.policy_custody().holds(&facts) {
            Ok(())
        } else {
            Err(PolicyError::Drift)
        }
    }

    fn policy_custody(&self) -> FileCustody {
        FileCustody {
            uid: Some(current_uid()),
            gid: Some(self.policy_gid),
            mode: Mode::Exact(0o640),
            size: None,
        }
    }

    /// The admitted policy of `identity`, or `None` when it has no token.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] for a policy file outside its custody, or whose bytes are not the canonical encoding of
    /// a non-empty canonical host list.
    pub fn admitted(
        &self,
        identity: &str,
        entropy: &dyn Entropy,
    ) -> Result<Option<Admitted>, PolicyError> {
        let Some(token) = self.token(identity, false, entropy)? else {
            return Ok(None);
        };
        let path = self
            .require_root()?
            .join(format!("{}.json", token.expose()));
        let custody = FileCustody {
            size: Some((1, MAX_POLICY_BYTES)),
            ..self.policy_custody()
        };
        let raw = custody::read(&path, &custody, MAX_POLICY_BYTES)
            .ok()
            .flatten()
            .ok_or(PolicyError::Drift)?;
        let hosts = match strict_json::parse(&raw) {
            Ok(strict_json::Value::Array(items)) => items
                .into_iter()
                .map(|item| match item {
                    strict_json::Value::String(host) => Some(host),
                    _ => None,
                })
                .collect::<Option<Vec<String>>>()
                .and_then(|hosts| canonical_allowed_hosts(&hosts)),
            _ => None,
        }
        .ok_or(PolicyError::Drift)?;
        if hosts.is_empty() || raw != encoded_hosts(&hosts) {
            return Err(PolicyError::Drift);
        }
        Ok(Some(Admitted { token, hosts }))
    }

    /// The token of `identity` when its policy admits exactly the reviewed `hosts`.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] when the policy is missing, unreadable, or admits other hosts.
    pub fn validate(
        &self,
        identity: &str,
        hosts: &[String],
        entropy: &dyn Entropy,
    ) -> Result<Secret, PolicyError> {
        Self::validate_admitted(self.admitted(identity, entropy)?, hosts)
    }

    /// [`PolicyStore::validate`] of an already admitted policy, for the use-time check against the reviewed hosts.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] when it is missing or admits other hosts.
    pub fn validate_admitted(
        admitted: Option<Admitted>,
        hosts: &[String],
    ) -> Result<Secret, PolicyError> {
        let expected = canonical_hosts(hosts)?;
        let admitted = admitted.ok_or(PolicyError::Drift)?;
        if admitted.hosts != expected {
            return Err(PolicyError::Drift);
        }
        Ok(admitted.token)
    }

    /// Removes `identity`'s policy and token, then commits both directories; absence is success.
    ///
    /// # Errors
    /// [`PolicyError::Drift`] for a token or policy outside its custody (nothing is removed);
    /// [`PolicyError::Unavailable`] when a removal or directory commit fails.
    pub fn remove(&self, identity: &str) -> Result<(), PolicyError> {
        let token_path = self.token_path(identity)?;
        match custody::facts(&token_path) {
            Ok(Some(_)) => {}
            Ok(None) => return Ok(()),
            Err(_) => return Err(PolicyError::Unavailable),
        }
        let token = read_token(&token_path)?;
        let root = self.require_root()?;
        let policy_path = root.join(format!("{}.json", token.expose()));
        let policy = custody::facts(&policy_path).map_err(|_| PolicyError::Unavailable)?;
        if let Some(facts) = policy {
            if !self.policy_custody().holds(&facts) {
                return Err(PolicyError::Drift);
            }
            std::fs::remove_file(&policy_path).map_err(|_| PolicyError::Unavailable)?;
        }
        std::fs::remove_file(&token_path).map_err(|_| PolicyError::Unavailable)?;
        durable::fsync_directory(root).map_err(|_| PolicyError::Unavailable)?;
        let token_directory = token_path.parent().ok_or(PolicyError::Unavailable)?;
        durable::fsync_directory(token_directory).map_err(|_| PolicyError::Unavailable)
    }
}

fn sha256_hex(text: &str) -> String {
    let mut hex = String::with_capacity(64);
    for byte in Sha256::digest(text.as_bytes()) {
        // Writing to a String cannot fail.
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

fn canonical_token(token: &str) -> bool {
    token.len() == 32
        && token
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn canonical_hosts(hosts: &[String]) -> Result<Vec<String>, PolicyError> {
    match canonical_allowed_hosts(hosts) {
        Some(canonical) if !canonical.is_empty() && canonical == hosts => Ok(canonical),
        _ => Err(PolicyError::Drift),
    }
}

fn read_token(path: &Path) -> Result<Secret, PolicyError> {
    let required = FileCustody {
        uid: Some(current_uid()),
        gid: None,
        mode: Mode::Exact(0o600),
        size: Some((TOKEN_FILE_BYTES, TOKEN_FILE_BYTES)),
    };
    let raw = custody::read(path, &required, TOKEN_FILE_BYTES)
        .ok()
        .flatten()
        .ok_or(PolicyError::Drift)?;
    match raw.split_last() {
        Some((b'\n', token)) => {
            let token = std::str::from_utf8(token).map_err(|_| PolicyError::Drift)?;
            if canonical_token(token) {
                Ok(Secret::new(token.to_owned()))
            } else {
                Err(PolicyError::Drift)
            }
        }
        _ => Err(PolicyError::Drift),
    }
}

/// Docker's `KEY=value` environment list as pairs, refusing an empty or repeated name; `None` refuses the list.
#[must_use]
pub fn environment_map(raw: &[String]) -> Option<Vec<(String, String)>> {
    let mut environment: Vec<(String, String)> = Vec::with_capacity(raw.len());
    for item in raw {
        let (key, value) = item.split_once('=')?;
        if key.is_empty() || environment.iter().any(|(existing, _)| existing == key) {
            return None;
        }
        environment.push((key.to_owned(), value.to_owned()));
    }
    Some(environment)
}

#[cfg(test)]
mod tests;
