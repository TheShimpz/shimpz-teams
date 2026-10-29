//! The base64 forms Python's `base64` module writes, for the WebSocket key and accept value and the Supervisor's
//! public key: encoding with padding, and decoding of text a grammar already admitted.

/// The standard alphabet (`b64encode`).
pub const STANDARD: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
/// The URL-safe alphabet (`urlsafe_b64encode`).
pub const URL_SAFE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// The padded base64 text of `bytes` in `alphabet`.
#[must_use]
pub fn encode(bytes: &[u8], alphabet: &[u8; 64]) -> String {
    let mut text = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let group = chunk
            .iter()
            .enumerate()
            .fold(0_u32, |group, (index, byte)| {
                group + (u32::from(*byte) << (16 - 8 * index))
            });
        for index in 0..4 {
            if index <= chunk.len() {
                let sextet = (group >> (18 - 6 * index)) & 0x3f;
                text.push(char::from(
                    alphabet[usize::try_from(sextet).unwrap_or_default()],
                ));
            } else {
                text.push('=');
            }
        }
    }
    text
}

/// The bytes the characters of `text` before any `=` encode in `alphabet`, as Python decodes them: every complete
/// byte, and the bits that complete none dropped. A character outside the alphabet yields `None`.
#[must_use]
pub fn decode(text: &str, alphabet: &[u8; 64]) -> Option<Vec<u8>> {
    let mut bytes = Vec::with_capacity(text.len() * 3 / 4);
    let mut buffer = 0_u32;
    let mut bits = 0;
    for character in text.bytes().take_while(|character| *character != b'=') {
        let sextet = alphabet.iter().position(|known| *known == character)?;
        buffer = (buffer << 6) + u32::try_from(sextet).unwrap_or_default();
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            bytes.push(u8::try_from((buffer >> bits) & 0xff).unwrap_or_default());
        }
    }
    Some(bytes)
}

#[cfg(test)]
mod tests {
    use super::{STANDARD, URL_SAFE, decode, encode};

    #[test]
    fn encodes_and_decodes_as_python() {
        // base64.b64encode(b""), b"f", b"fo", b"foo", b"foob"
        for (bytes, text) in [
            (&b""[..], ""),
            (b"f", "Zg=="),
            (b"fo", "Zm8="),
            (b"foo", "Zm9v"),
            (b"foob", "Zm9vYg=="),
            (b"\xfb\xff", "+/8="),
        ] {
            assert_eq!(encode(bytes, STANDARD), text);
            assert_eq!(decode(text, STANDARD).as_deref(), Some(bytes));
        }
        assert_eq!(encode(b"\xfb\xff", URL_SAFE), "-_8=");
        // The low bits of a last character that complete no byte are dropped, as Python drops them.
        assert_eq!(decode("Zh==", STANDARD).as_deref(), Some(&b"f"[..]));
        assert_eq!(decode("Z@==", STANDARD), None);
    }
}
