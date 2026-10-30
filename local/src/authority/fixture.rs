//! Test fixtures: Admin's published key directory, signing keys, and assertions signed as Admin signs them.

use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use ring::signature::{Ed25519KeyPair, KeyPair};
use strict_json::Value;
use team_protocol::python::json::{CANONICAL, dumps};
use team_protocol::python::value::{int, object_of, text};
use team_protocol::supervisor::{
    ASSERTION_AUDIENCE, JWT_HEADER, ROUTINE_AUDIENCE, ROUTINE_JWT_HEADER,
};

use super::assertion::encode_base64url;
use super::{KeyFiles, PublicKey, ReplayGuard, Verifier};

/// The fixed instant assertions are judged at.
pub const NOW: i64 = 1_800_000_000;
/// The Supervisor's opaque id.
pub const SUPERVISOR_ID: &str = "0123456789abcdef0123456789abcdef";
/// A run lease digest.
pub const LEASE: &str = "1111111111111111111111111111111111111111111111111111111111111111";

/// One Ed25519 signing key.
pub struct Signer(Ed25519KeyPair);

impl Signer {
    /// The key of a seed of 32 `seed` bytes.
    pub fn new(seed: u8) -> Self {
        Self(Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).expect("every seed is a key"))
    }

    /// Its verification key.
    pub fn public(&self) -> PublicKey {
        PublicKey::from_raw(self.0.public_key().as_ref().try_into().expect("32 bytes"))
    }

    /// Its unpadded base64url raw verification key, as a rotation body carries it.
    pub fn public_base64url(&self) -> String {
        encode_base64url(self.0.public_key().as_ref())
    }

    /// A compact JWT of `header` and `claims`, each segment canonical JSON.
    pub fn sign(&self, header: &[(&str, &str); 3], claims: &Value) -> String {
        let header = object_of(
            header
                .iter()
                .map(|(name, value)| (*name, text(value)))
                .collect(),
        );
        let signing = format!(
            "{}.{}",
            encode_base64url(dumps(&header, CANONICAL).as_bytes()),
            encode_base64url(dumps(claims, CANONICAL).as_bytes())
        );
        let signature = self.0.sign(signing.as_bytes());
        format!("{signing}.{}", encode_base64url(signature.as_ref()))
    }

    /// A Supervisor assertion of `claims`.
    pub fn supervisor(&self, claims: &Value) -> String {
        self.sign(&JWT_HEADER, claims)
    }

    /// A Routine assertion of `claims`.
    pub fn routine(&self, claims: &Value) -> String {
        self.sign(&ROUTINE_JWT_HEADER, claims)
    }
}

/// A fresh 32-hex assertion id.
pub fn nonce() -> String {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    format!("{:032x}", NEXT.fetch_add(1, Ordering::Relaxed))
}

/// The binding of no body.
pub fn no_body() -> Value {
    object_of(vec![
        ("kind", text("none")),
        ("length", int(0)),
        ("sha256", text(team_protocol::supervisor::EMPTY_SHA256)),
    ])
}

/// Session claims for `method` `path` with `body`, issued at [`NOW`] for 10 s, as `(name, value)` members to edit.
pub fn session(method: &str, path: &str, body: Value) -> Vec<(String, Value)> {
    claims(ASSERTION_AUDIENCE, "session", LEASE, method, path, body)
}

/// Routine claims for `method` `path` with `body` under [`LEASE`].
pub fn routine(method: &str, path: &str, body: Value) -> Vec<(String, Value)> {
    claims(ROUTINE_AUDIENCE, "routine", LEASE, method, path, body)
}

fn claims(
    audience: &str,
    authority: &str,
    authority_sha256: &str,
    method: &str,
    path: &str,
    body: Value,
) -> Vec<(String, Value)> {
    [
        ("v", int(1)),
        ("aud", text(audience)),
        ("sub", text(SUPERVISOR_ID)),
        ("authority", text(authority)),
        ("authority_sha256", text(authority_sha256)),
        ("jti", text(&nonce())),
        ("iat", int(NOW)),
        ("exp", int(NOW + 10)),
        ("method", text(method)),
        ("path", text(path)),
        ("body", body),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_owned(), value))
    .collect()
}

/// `members` with `name` set to `value`.
pub fn with(mut members: Vec<(String, Value)>, name: &str, value: Value) -> Vec<(String, Value)> {
    members.retain(|(existing, _)| existing != name);
    members.push((name.to_owned(), value));
    members
}

/// Admin's published key directory and Team's pin path below a temporary root.
pub struct Keys {
    /// The temporary root.
    pub _root: tempfile::TempDir,
    /// The key files.
    pub files: KeyFiles,
    /// The Supervisor's signing key, published.
    pub supervisor: Signer,
    /// The Routine identity's signing key, published.
    pub routine: Signer,
}

impl Keys {
    /// A key directory (`2770` in this process's group) holding both published keys.
    pub fn new() -> Self {
        let root = tempfile::tempdir().expect("temporary directory");
        let directory = root.path().join("run/shimpz-local-supervisor");
        std::fs::create_dir_all(&directory).expect("key directory");
        std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o2770))
            .expect("key directory mode");
        let files = KeyFiles {
            supervisor: directory.join("public.pem"),
            routine: directory.join("routine.pem"),
            pin: root
                .path()
                .join("var/lib/shimpz-local/inference/supervisor.pem"),
            key_group: Some(rustix::process::getegid().as_raw()),
        };
        let keys = Self {
            _root: root,
            files,
            supervisor: Signer::new(1),
            routine: Signer::new(2),
        };
        Self::publish(&keys.files.supervisor, &keys.supervisor);
        Self::publish(&keys.files.routine, &keys.routine);
        keys
    }

    /// Publishes `signer`'s key at `path` as Admin does: `0440`, replacing any earlier one.
    pub fn publish(path: &Path, signer: &Signer) {
        let _ = std::fs::remove_file(path);
        std::fs::write(path, signer.public().to_pem()).expect("published");
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o440)).expect("mode");
    }

    /// A verifier of these keys with a replay guard of `capacity`.
    pub fn verifier(&self, capacity: usize) -> Verifier {
        Verifier::new(self.files.clone(), ReplayGuard::with_capacity(capacity))
    }
}
