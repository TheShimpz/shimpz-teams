//! The source of every nonce, token, identifier, and key Team creates.

use std::fmt::Write;

/// The operating system's random source failed, so nothing that needed it was created.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EntropyUnavailable;

impl std::fmt::Display for EntropyUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("random bytes are unavailable")
    }
}

impl std::error::Error for EntropyUnavailable {}

/// Fills buffers with unpredictable bytes.
pub trait Entropy: Send + Sync {
    /// Fills `bytes` completely, or fails without a partial result being used.
    ///
    /// # Errors
    /// [`EntropyUnavailable`] when the source cannot answer.
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable>;
}

/// The operating system's random source.
#[derive(Clone, Copy, Debug, Default)]
pub struct OsEntropy;

impl Entropy for OsEntropy {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        getrandom::fill(bytes).map_err(|_| EntropyUnavailable)
    }
}

/// `length` random bytes as lowercase hexadecimal, as Python's `secrets.token_hex(length)` writes them.
///
/// # Errors
/// [`EntropyUnavailable`] when the source cannot answer.
pub fn token_hex(entropy: &dyn Entropy, length: usize) -> Result<String, EntropyUnavailable> {
    let mut bytes = vec![0_u8; length];
    entropy.fill(&mut bytes)?;
    let mut text = String::with_capacity(length * 2);
    for byte in bytes {
        // Writing to a String cannot fail.
        let _ = write!(text, "{byte:02x}");
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::{Entropy, EntropyUnavailable, OsEntropy, token_hex};

    struct Constant(u8);

    impl Entropy for Constant {
        fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
            bytes.fill(self.0);
            Ok(())
        }
    }

    #[test]
    fn token_hex_writes_two_lowercase_digits_per_byte() {
        assert_eq!(token_hex(&Constant(0xab), 3), Ok("ababab".to_owned()));
        assert_eq!(token_hex(&OsEntropy, 16).map(|token| token.len()), Ok(32));
    }
}
