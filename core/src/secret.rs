//! A secret value: never printed, wiped from memory when dropped.

use zeroize::Zeroizing;

/// A bearer, API key, PKCE verifier, or other secret text. Its `Debug` shows nothing of it, and its memory is
/// overwritten when it is dropped; only [`Secret::expose`] reads it, at the one place it is used.
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(Zeroizing<String>);

impl Secret {
    /// Takes ownership of `value`.
    #[must_use]
    pub fn new(value: String) -> Self {
        Self(Zeroizing::new(value))
    }

    /// The secret text, for the one call that sends or compares it.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Secret(<redacted>)")
    }
}

#[cfg(test)]
mod tests {
    use super::Secret;

    #[test]
    fn debug_never_shows_the_value() {
        let secret = Secret::new("sk-test-value".to_owned());
        assert_eq!(format!("{secret:?}"), "Secret(<redacted>)");
        assert_eq!(secret.expose(), "sk-test-value");
    }
}
