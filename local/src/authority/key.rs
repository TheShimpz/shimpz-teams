//! Ed25519 verification keys: the exact PEM `SubjectPublicKeyInfo` document Admin publishes and Team pins, the key's
//! fingerprint, and signature verification.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use ring::signature::{ED25519, UnparsedPublicKey};
use sha2::{Digest, Sha256};

/// The DER prefix of an Ed25519 `SubjectPublicKeyInfo` (RFC 8410), before the key's 32 raw bytes.
const SPKI_PREFIX: [u8; 12] = [
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
];
const PEM_BEGIN: &str = "-----BEGIN PUBLIC KEY-----\n";
const PEM_END: &str = "\n-----END PUBLIC KEY-----\n";

/// One Ed25519 verification key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PublicKey([u8; 32]);

impl PublicKey {
    /// The key of 32 raw bytes.
    #[must_use]
    pub const fn from_raw(raw: [u8; 32]) -> Self {
        Self(raw)
    }

    /// The key of one PEM document, only in its one canonical form: the single base64 line of the 44-byte DER
    /// between the `PUBLIC KEY` armor lines, each ended by a newline.
    #[must_use]
    pub fn from_pem(pem: &[u8]) -> Option<Self> {
        let text = std::str::from_utf8(pem).ok()?;
        let body = text.strip_prefix(PEM_BEGIN)?.strip_suffix(PEM_END)?;
        let der = STANDARD.decode(body).ok()?;
        if STANDARD.encode(&der) != body {
            return None;
        }
        let raw = der.strip_prefix(&SPKI_PREFIX)?;
        Some(Self(raw.try_into().ok()?))
    }

    /// The canonical PEM document of the key.
    #[must_use]
    pub fn to_pem(&self) -> Vec<u8> {
        let mut der = SPKI_PREFIX.to_vec();
        der.extend_from_slice(&self.0);
        format!("{PEM_BEGIN}{}{PEM_END}", STANDARD.encode(&der)).into_bytes()
    }

    /// The SHA-256 of the key's 32 raw bytes, in lowercase hex.
    #[must_use]
    pub fn fingerprint(&self) -> String {
        hex(&Sha256::digest(self.0))
    }

    /// Whether `signature` is this key's Ed25519 signature of `message`.
    #[must_use]
    pub fn verifies(&self, message: &[u8], signature: &[u8]) -> bool {
        UnparsedPublicKey::new(&ED25519, self.0)
            .verify(message, signature)
            .is_ok()
    }
}

/// Lowercase hexadecimal.
pub(crate) fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    bytes
        .iter()
        .flat_map(|byte| {
            [
                DIGITS[usize::from(byte >> 4)],
                DIGITS[usize::from(byte & 0xf)],
            ]
        })
        .map(char::from)
        .collect()
}
