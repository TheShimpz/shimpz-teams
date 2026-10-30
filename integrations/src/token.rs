//! The OAuth token set the broker returns and Team seals (`integrations/http.py`). Team never holds an OAuth Client
//! Secret: the token set is the only OAuth material it keeps.

use team_core::secret::Secret;

/// The largest OAuth token Team accepts from the broker, hands an Action, or seals.
pub const MAX_TOKEN_BYTES: usize = 16 * 1024;

/// Whether `text` is visible ASCII of `minimum` to `maximum` bytes, as an OAuth token or PKCE verifier is.
#[must_use]
pub fn is_token(text: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&text.len()) && text.bytes().all(|byte| (33..127).contains(&byte))
}

/// [`is_token`] with the default bounds: 16 bytes to [`MAX_TOKEN_BYTES`].
#[must_use]
pub fn is_bearer(text: &str) -> bool {
    is_token(text, 16, MAX_TOKEN_BYTES)
}

/// One grant's tokens as the broker issued them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenSet {
    /// The access token.
    pub access_token: Secret,
    /// The refresh token, when the grant has one.
    pub refresh_token: Option<Secret>,
    /// The granted scopes, exactly the requested sorted set.
    pub scopes: Vec<String>,
    /// Seconds until the access token expires, counted from when it was delivered.
    pub expires_in: i64,
    /// The broker's lease binding the grant to this Space, when it issued one.
    pub broker_lease: Option<Secret>,
}

#[cfg(test)]
mod tests {
    use super::{is_bearer, is_token};

    #[test]
    fn a_token_is_bounded_visible_ascii() {
        assert!(is_bearer(&"x".repeat(16)));
        assert!(is_token(&"v".repeat(43), 43, 128));
        for refused in [
            "x".repeat(15),
            format!("{}\n", "x".repeat(19)),
            "é".repeat(20),
            format!("{} ", "x".repeat(19)),
            "x".repeat(16 * 1024 + 1),
        ] {
            assert!(!is_bearer(&refused), "{refused:?}");
        }
    }
}
