//! The two viewer credentials of a Browser a person may control (ADR-0117, Sign-ins amendment item 3).
//!
//! Team draws them for each Browser, writes their hashes into its display (`browser-host credentials`), and puts the
//! matching one into the one upgrade request it sends for each stream, so a person's page never holds either. The
//! display is `KasmVNC`, whose password file holds SHA-256-crypt hashes under its fixed salt `kasm`.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use sha2::{Digest, Sha256};
use team_core::entropy::{Entropy, EntropyUnavailable, token_hex};
use team_core::secret::Secret;

const SALT: &[u8] = b"kasm";
const ROUNDS: usize = 5000;
const ALPHABET: &[u8; 64] = b"./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
// The SHA-256-crypt output order: each triple of digest bytes becomes four characters.
const ORDER: [(usize, usize, usize); 10] = [
    (0, 10, 20),
    (21, 1, 11),
    (12, 22, 2),
    (3, 13, 23),
    (24, 4, 14),
    (15, 25, 5),
    (6, 16, 26),
    (27, 7, 17),
    (18, 28, 8),
    (9, 19, 29),
];

fn encode(mut value: u32, length: usize, out: &mut String) {
    for _ in 0..length {
        out.push(char::from(ALPHABET[(value & 0x3f) as usize]));
        value >>= 6;
    }
}

fn repeated(digest: &[u8], length: usize) -> Vec<u8> {
    digest.iter().copied().cycle().take(length).collect()
}

fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
    }
    hasher.finalize().into()
}

/// `$5$kasm$…`: the SHA-256-crypt (Drepper) hash of `password` under `KasmVNC`'s salt, at the default 5000 rounds.
#[must_use]
pub fn kasm_hash(password: &str) -> String {
    let key = password.as_bytes();
    let alternate = sha256(&[key, SALT, key]);
    let mut first = Sha256::new();
    first.update(key);
    first.update(SALT);
    first.update(repeated(&alternate, key.len()));
    let mut length = key.len();
    while length > 0 {
        if length & 1 == 1 {
            first.update(alternate);
        } else {
            first.update(key);
        }
        length >>= 1;
    }
    let digest: [u8; 32] = first.finalize().into();
    let digest = rounds(key, digest);
    let mut encoded = String::from("$5$kasm$");
    for (a, b, c) in ORDER {
        let value =
            (u32::from(digest[a]) << 16) | (u32::from(digest[b]) << 8) | u32::from(digest[c]);
        encode(value, 4, &mut encoded);
    }
    encode(
        (u32::from(digest[31]) << 8) | u32::from(digest[30]),
        3,
        &mut encoded,
    );
    encoded
}

/// SHA-256-crypt's 5000 rounds over the first digest.
fn rounds(key: &[u8], mut digest: [u8; 32]) -> [u8; 32] {
    let p_bytes = repeated(&sha256(&[&key.repeat(key.len())]), key.len());
    let s_bytes = repeated(
        &sha256(&[&SALT.repeat(16 + usize::from(digest[0]))]),
        SALT.len(),
    );
    for round in 0..ROUNDS {
        let mut step = Sha256::new();
        if round & 1 == 1 {
            step.update(&p_bytes);
        } else {
            step.update(digest);
        }
        if round % 3 != 0 {
            step.update(&s_bytes);
        }
        if round % 7 != 0 {
            step.update(&p_bytes);
        }
        if round & 1 == 1 {
            step.update(digest);
        } else {
            step.update(&p_bytes);
        }
        digest = step.finalize().into();
    }
    digest
}

/// One Browser's read-only and read-write viewer passwords, held only in Team's memory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Credentials {
    viewer: Secret,
    controller: Secret,
}

impl Credentials {
    /// A fresh pair of 48-hex passwords.
    ///
    /// # Errors
    /// [`EntropyUnavailable`] when no random bytes could be drawn.
    pub fn draw(entropy: &dyn Entropy) -> Result<Self, EntropyUnavailable> {
        Ok(Self {
            viewer: Secret::new(token_hex(entropy, 24)?),
            controller: Secret::new(token_hex(entropy, 24)?),
        })
    }

    /// The display's password file: the read-only `viewer`, then the read-write `controller`.
    #[must_use]
    pub fn file(&self) -> Vec<u8> {
        format!(
            "viewer:{}:r\ncontroller:{}:rw\n",
            kasm_hash(self.viewer.expose()),
            kasm_hash(self.controller.expose())
        )
        .into_bytes()
    }

    /// The Basic credential the upgrade of one stream carries: the controller's only for the person in control.
    #[must_use]
    pub fn authorization(&self, control: bool) -> Secret {
        let (user, password) = if control {
            ("controller", &self.controller)
        } else {
            ("viewer", &self.viewer)
        };
        let pair = format!("{user}:{}", password.expose());
        Secret::new(format!("Basic {}", STANDARD.encode(pair.as_bytes())))
    }
}

#[cfg(test)]
mod tests {
    use base64::Engine;
    use base64::engine::general_purpose::STANDARD;
    use team_core::entropy::OsEntropy;

    use super::{Credentials, kasm_hash};

    #[test]
    fn the_hash_is_sha256_crypt_under_kasm_s_salt() {
        // Vectors from `openssl passwd -5 -salt kasm`, the hash kasmvncpasswd writes.
        assert_eq!(
            kasm_hash("viewerpass"),
            "$5$kasm$22E8Xs61cUGvV2nVcl8DbHiizSEilH1zFOaWAOxMzEA"
        );
        assert_eq!(
            kasm_hash("longer password with spaces"),
            "$5$kasm$GFmDoKMvmzCDcckA9YLL/eFES9k9C3kca4sX.2Dgb67"
        );
        assert_eq!(
            kasm_hash(&"a".repeat(40)),
            "$5$kasm$bww6oQ6V8NoXUx.nhJsThql1a01zxA/udVZ5PetgV9B"
        );
    }

    fn decoded(authorization: &str) -> String {
        let encoded = authorization
            .strip_prefix("Basic ")
            .expect("a Basic credential");
        String::from_utf8(STANDARD.decode(encoded).expect("base64")).expect("text")
    }

    #[test]
    fn each_browser_draws_its_own_pair_and_only_control_carries_the_writer() {
        let first = Credentials::draw(&OsEntropy).expect("drawn");
        let second = Credentials::draw(&OsEntropy).expect("drawn");
        assert_ne!(first, second);
        assert_ne!(first.viewer, first.controller);
        assert_eq!(first.viewer.expose().len(), 48);
        let file = String::from_utf8(first.file()).expect("text");
        let lines: Vec<&str> = file.lines().collect();
        assert_eq!(
            lines,
            [
                format!("viewer:{}:r", kasm_hash(first.viewer.expose())),
                format!("controller:{}:rw", kasm_hash(first.controller.expose())),
            ]
        );
        assert_eq!(
            decoded(first.authorization(false).expose()),
            format!("viewer:{}", first.viewer.expose())
        );
        assert_eq!(
            decoded(first.authorization(true).expose()),
            format!("controller:{}", first.controller.expose())
        );
        assert_eq!(
            format!("{first:?}").matches(first.viewer.expose()).count(),
            0
        );
    }
}
