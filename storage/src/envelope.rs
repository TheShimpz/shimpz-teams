//! The AES-256-GCM private-state envelope: `{"algorithm":"AES-256-GCM","nonce":b64,"ciphertext":b64}`, and the
//! identified envelope that also names its sealing key, `"key_id"`, so a record sealed under another key is refused
//! before it is decrypted.
//!
//! The nonce is 96 random bits per seal; the ciphertext carries the 16-byte tag after the encrypted plaintext; both
//! parts are standard padded base64. The bytes each store binds as associated data stay that store's own format.

use std::fmt::Write as _;

use aes_gcm::Aes256Gcm;
use aes_gcm::aead::Nonce;
use aes_gcm::aead::{Aead, KeyInit, Payload};
use base64::Engine as _;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use strict_json::Value;
use subtle::ConstantTimeEq;
use team_core::entropy::{Entropy, EntropyUnavailable};
use zeroize::Zeroizing;

/// The one algorithm an envelope names.
pub const ALGORITHM: &str = "AES-256-GCM";
/// The nonce length in bytes.
pub const NONCE_BYTES: usize = 12;
/// The authentication tag length in bytes.
pub const TAG_BYTES: usize = 16;
const KEY_ID_LABEL: &[u8] = b"shimpz-team-private-state-key-id";

/// Python's `base64.b64encode`, and `base64.b64decode(validate=True)`: padding is required and exact, but unused
/// trailing bits of the last character are ignored, as Python ignores them.
const BASE64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_allow_trailing_bits(true)
        .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
);

/// A 256-bit keyring key. Its `Debug` shows nothing of it, and its memory is wiped when it is dropped.
#[derive(Clone)]
pub struct Key(Zeroizing<[u8; 32]>);

impl Key {
    /// The key with these bytes.
    #[must_use]
    pub fn from_bytes(bytes: [u8; 32]) -> Self {
        Self(Zeroizing::new(bytes))
    }

    /// A fresh random key.
    ///
    /// # Errors
    /// [`EntropyUnavailable`] when the random source fails.
    pub fn generate(entropy: &dyn Entropy) -> Result<Self, EntropyUnavailable> {
        let mut bytes = Zeroizing::new([0_u8; 32]);
        entropy.fill(bytes.as_mut())?;
        Ok(Self(bytes))
    }

    /// The key's bytes, for the keyring file that stores it.
    #[must_use]
    pub fn expose(&self) -> &[u8; 32] {
        &self.0
    }

    /// The key's public identifier: 128 bits of HMAC-SHA256 under it over a fixed label, in lowercase hex.
    ///
    /// It names which key sealed an identified envelope; it commits to the key without disclosing it.
    #[must_use]
    pub fn id(&self) -> String {
        // HMAC accepts a key of any length, so this cannot fail.
        let mut mac =
            Hmac::<Sha256>::new_from_slice(self.0.as_ref()).unwrap_or_else(|_| unreachable!());
        mac.update(KEY_ID_LABEL);
        let digest = mac.finalize().into_bytes();
        let mut text = String::with_capacity(32);
        for byte in &digest[..16] {
            // Writing to a String cannot fail.
            let _ = write!(text, "{byte:02x}");
        }
        text
    }
}

impl std::fmt::Debug for Key {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Key(<redacted>)")
    }
}

/// One admitted or freshly sealed envelope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Envelope {
    nonce: [u8; NONCE_BYTES],
    ciphertext: Vec<u8>,
    key_id: Option<String>,
}

/// The envelope is not one well-formed envelope of the expected kind and bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Malformed;

/// The envelope did not authenticate under the key, or names another key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Unauthentic;

/// The bounds one store admits its envelopes within.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bounds {
    /// The longest base64 text either part may have.
    pub maximum_encoded_part: usize,
    /// The largest plaintext the ciphertext may carry.
    pub maximum_plaintext: usize,
}

impl Envelope {
    /// Encrypts `plaintext` bound to `aad` under a fresh nonce into the plain envelope.
    ///
    /// # Errors
    /// [`EntropyUnavailable`] when no nonce could be drawn.
    pub fn seal(
        key: &Key,
        plaintext: &[u8],
        aad: &[u8],
        entropy: &dyn Entropy,
    ) -> Result<Self, EntropyUnavailable> {
        let mut nonce = [0_u8; NONCE_BYTES];
        entropy.fill(&mut nonce)?;
        let cipher = cipher(key);
        // AES-GCM refuses only a plaintext or associated data beyond 2^36 bytes, which no store admits.
        let ciphertext = cipher
            .encrypt(
                &Nonce::<Aes256Gcm>::from(nonce),
                Payload {
                    msg: plaintext,
                    aad,
                },
            )
            .unwrap_or_else(|_| unreachable!());
        Ok(Self {
            nonce,
            ciphertext,
            key_id: None,
        })
    }

    /// [`Envelope::seal`] into the identified envelope, which also names the sealing key.
    ///
    /// # Errors
    /// [`EntropyUnavailable`] when no nonce could be drawn.
    pub fn seal_identified(
        key: &Key,
        plaintext: &[u8],
        aad: &[u8],
        entropy: &dyn Entropy,
    ) -> Result<Self, EntropyUnavailable> {
        let mut envelope = Self::seal(key, plaintext, aad, entropy)?;
        envelope.key_id = Some(key.id());
        Ok(envelope)
    }

    /// Admits one plain envelope: exactly its three fields, the algorithm, a 12-byte nonce, and a ciphertext of a
    /// plaintext from 1 to `bounds.maximum_plaintext` bytes.
    ///
    /// # Errors
    /// [`Malformed`] for anything else.
    pub fn admit(value: &Value, bounds: Bounds) -> Result<Self, Malformed> {
        Self::admit_kind(value, bounds, false)
    }

    /// Admits one identified envelope: [`Envelope::admit`] plus a mandatory `key_id` of 32 lowercase hex digits.
    ///
    /// # Errors
    /// [`Malformed`] for anything else.
    pub fn admit_identified(value: &Value, bounds: Bounds) -> Result<Self, Malformed> {
        Self::admit_kind(value, bounds, true)
    }

    fn admit_kind(value: &Value, bounds: Bounds, identified: bool) -> Result<Self, Malformed> {
        let Value::Object(members) = value else {
            return Err(Malformed);
        };
        let expected: &[&str] = if identified {
            &["algorithm", "ciphertext", "key_id", "nonce"]
        } else {
            &["algorithm", "ciphertext", "nonce"]
        };
        let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
        names.sort_unstable();
        if names != expected || value.get("algorithm") != Some(&Value::String(ALGORITHM.to_owned()))
        {
            return Err(Malformed);
        }
        let key_id = if identified {
            match value.get("key_id") {
                Some(Value::String(id))
                    if id.len() == 32
                        && id
                            .bytes()
                            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')) =>
                {
                    Some(id.clone())
                }
                _ => return Err(Malformed),
            }
        } else {
            None
        };
        let nonce = decode_part(
            value.get("nonce"),
            bounds.maximum_encoded_part,
            NONCE_BYTES,
            NONCE_BYTES,
        )?;
        let ciphertext = decode_part(
            value.get("ciphertext"),
            bounds.maximum_encoded_part,
            TAG_BYTES + 1,
            bounds.maximum_plaintext + TAG_BYTES,
        )?;
        Ok(Self {
            nonce: nonce.try_into().map_err(|_| Malformed)?,
            ciphertext,
            key_id,
        })
    }

    /// Authenticates and decrypts the envelope under `key` with `aad`. An identified envelope opens only under the key
    /// it names, compared in constant time, before any decryption is attempted.
    ///
    /// # Errors
    /// [`Unauthentic`] for another key or a failed authentication tag.
    pub fn open(&self, key: &Key, aad: &[u8]) -> Result<Zeroizing<Vec<u8>>, Unauthentic> {
        if let Some(named) = &self.key_id
            && !bool::from(named.as_bytes().ct_eq(key.id().as_bytes()))
        {
            return Err(Unauthentic);
        }
        let cipher = cipher(key);
        cipher
            .decrypt(
                &Nonce::<Aes256Gcm>::from(self.nonce),
                Payload {
                    msg: &self.ciphertext,
                    aad,
                },
            )
            .map(Zeroizing::new)
            .map_err(|_| Unauthentic)
    }

    /// The `key_id` an identified envelope names.
    #[must_use]
    pub fn key_id(&self) -> Option<&str> {
        self.key_id.as_deref()
    }

    /// The stored form, with its fields in the order the Python writer inserted them (`algorithm`, `nonce`,
    /// `ciphertext`, then `key_id`); a sorted writer reorders them.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut members = vec![
            ("algorithm".to_owned(), Value::String(ALGORITHM.to_owned())),
            ("nonce".to_owned(), Value::String(BASE64.encode(self.nonce))),
            (
                "ciphertext".to_owned(),
                Value::String(BASE64.encode(&self.ciphertext)),
            ),
        ];
        if let Some(key_id) = &self.key_id {
            members.push(("key_id".to_owned(), Value::String(key_id.clone())));
        }
        Value::Object(members)
    }
}

/// The cipher under `key`; a 32-byte key is always the length AES-256 takes.
fn cipher(key: &Key) -> Aes256Gcm {
    Aes256Gcm::new_from_slice(key.0.as_ref()).unwrap_or_else(|_| unreachable!())
}

/// One base64 part of at most `maximum_encoded` characters decoding to `minimum..=maximum` bytes.
fn decode_part(
    value: Option<&Value>,
    maximum_encoded: usize,
    minimum: usize,
    maximum: usize,
) -> Result<Vec<u8>, Malformed> {
    let Some(Value::String(text)) = value else {
        return Err(Malformed);
    };
    if text.chars().count() > maximum_encoded {
        return Err(Malformed);
    }
    let decoded = BASE64.decode(text).map_err(|_| Malformed)?;
    if (minimum..=maximum).contains(&decoded.len()) {
        Ok(decoded)
    } else {
        Err(Malformed)
    }
}

/// Python's `base64.b64decode(text, validate=True)`.
///
/// # Errors
/// [`Malformed`] for text Python refuses.
pub fn decode_base64(text: &str) -> Result<Vec<u8>, Malformed> {
    BASE64.decode(text).map_err(|_| Malformed)
}

/// Python's `base64.b64encode(bytes).decode("ascii")`.
#[must_use]
pub fn encode_base64(bytes: &[u8]) -> String {
    BASE64.encode(bytes)
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_core::entropy::{Entropy, EntropyUnavailable};

    use super::{Bounds, Envelope, Key, Malformed, Unauthentic, decode_base64, encode_base64};

    struct Fixed([u8; 12]);

    impl Entropy for Fixed {
        fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
            bytes.copy_from_slice(&self.0[..bytes.len()]);
            Ok(())
        }
    }

    const BOUNDS: Bounds = Bounds {
        maximum_encoded_part: 4096,
        maximum_plaintext: 64,
    };

    fn key() -> Key {
        Key::from_bytes([b'k'; 32])
    }

    fn sealed() -> Envelope {
        Envelope::seal_identified(&key(), b"secret", b"aad", &Fixed([7; 12])).expect("sealed")
    }

    #[test]
    fn base64_is_python_strict_decoding() {
        for (text, decoded) in [
            ("QQ==", Some(b"A".to_vec())),
            ("QR==", Some(b"A".to_vec())),
            ("QUJD", Some(b"ABC".to_vec())),
            ("QUJ=", Some(b"AB".to_vec())),
            ("QQ", None),
            ("QQ===", None),
            ("Q Q==", None),
            ("QQ==\n", None),
            ("=QQ=", None),
            ("QQ-_", None),
        ] {
            assert_eq!(decode_base64(text).ok(), decoded, "{text}");
        }
        assert_eq!(encode_base64(b"AB"), "QUI=");
    }

    #[test]
    fn envelope_roundtrip_and_tamper_each_aad_field() {
        let envelope = sealed();
        let admitted = Envelope::admit_identified(&envelope.to_value(), BOUNDS).expect("admitted");
        assert_eq!(admitted, envelope);
        assert_eq!(
            admitted.open(&key(), b"aad").expect("opened").as_slice(),
            b"secret"
        );
        for tampered in [&b"aae"[..], b"aa", b"aad\0", b""] {
            assert_eq!(admitted.open(&key(), tampered), Err(Unauthentic));
        }
        let mut flipped = admitted.clone();
        flipped.ciphertext[0] ^= 1;
        assert_eq!(flipped.open(&key(), b"aad"), Err(Unauthentic));
        let mut renonced = admitted;
        renonced.nonce[0] ^= 1;
        assert_eq!(renonced.open(&key(), b"aad"), Err(Unauthentic));
    }

    #[test]
    fn an_identified_envelope_opens_only_under_the_key_it_names() {
        let other = Key::from_bytes([b'o'; 32]);
        let envelope = sealed();
        assert_eq!(envelope.key_id(), Some(key().id().as_str()));
        assert_ne!(key().id(), other.id());
        assert_eq!(envelope.open(&other, b"aad"), Err(Unauthentic));
        // The plain form of the same seal still authenticates only under the key.
        let plain = Envelope::seal(&key(), b"secret", b"aad", &Fixed([7; 12])).expect("sealed");
        assert_eq!(plain.open(&other, b"aad"), Err(Unauthentic));
        assert_eq!(
            Envelope::admit(&plain.to_value(), BOUNDS),
            Ok(plain.clone())
        );
        assert_eq!(
            Envelope::admit_identified(&plain.to_value(), BOUNDS),
            Err(Malformed)
        );
        assert_eq!(
            Envelope::admit(&envelope.to_value(), BOUNDS),
            Err(Malformed)
        );
    }

    #[test]
    fn key_id_compared_constant_time() {
        // The comparison runs over the whole id: a forged id equal but for its last digit is refused like any other.
        let mut forged = sealed();
        let mut id = key().id();
        let last = if id.ends_with('0') { "1" } else { "0" };
        id.replace_range(31.., last);
        forged.key_id = Some(id);
        let admitted = Envelope::admit_identified(&forged.to_value(), BOUNDS).expect("admitted");
        assert_eq!(admitted.open(&key(), b"aad"), Err(Unauthentic));
        assert_eq!(format!("{:?}", key()), "Key(<redacted>)");
    }

    /// The sealed envelope's stored form with `name` set to `value` (or removed when `None`).
    fn variant(name: &str, value: Option<&str>) -> Value {
        let Value::Object(mut members) = sealed().to_value() else {
            panic!("an object");
        };
        members.retain(|(member, _)| member != name);
        if let Some(value) = value {
            members.push((name.to_owned(), Value::String(value.to_owned())));
        }
        Value::Object(members)
    }

    #[test]
    fn each_malformed_envelope_is_refused() {
        let upper = key().id().to_uppercase();
        for refused in [
            Value::Array(Vec::new()),
            variant("key_id", None),
            variant("algorithm", Some("AES-128-GCM")),
            variant("nonce", Some(&encode_base64(&[0; 11]))),
            variant("nonce", Some("!")),
            variant("ciphertext", Some(&encode_base64(&[0; 16]))),
            variant("ciphertext", Some(&encode_base64(&[0; 64 + 17]))),
            variant("key_id", Some(&upper)),
            variant("key_id", Some("ab")),
            variant("extra", Some("")),
        ] {
            assert_eq!(
                Envelope::admit_identified(&refused, BOUNDS),
                Err(Malformed),
                "{refused:?}"
            );
        }
        let tight = Bounds {
            maximum_encoded_part: 15,
            maximum_plaintext: 64,
        };
        assert_eq!(
            Envelope::admit_identified(&sealed().to_value(), tight),
            Err(Malformed)
        );
    }
}
