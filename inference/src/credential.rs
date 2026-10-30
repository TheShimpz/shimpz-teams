//! The one shape every request-scoped provider or decision API key must have before Team uses it.

/// The shortest admitted key.
pub const MIN_API_KEY_BYTES: usize = 16;
/// The longest admitted key.
pub const MAX_API_KEY_BYTES: usize = 8 * 1024;

/// A bounded key of printable ASCII with no space or control character, so it never needs trimming or escaping.
#[must_use]
pub fn admissible_api_key(value: &str) -> bool {
    (MIN_API_KEY_BYTES..=MAX_API_KEY_BYTES).contains(&value.len())
        && value.bytes().all(|byte| (33..=126).contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::admissible_api_key;

    #[test]
    fn only_bounded_printable_ascii_without_spaces_is_admitted() {
        assert!(admissible_api_key("test-key-not-real-0000"));
        for refused in [
            "",
            "short",
            "test key not real 0000",
            "test-key-not-real-é000",
            "test-key-not-real\t0000",
        ] {
            assert!(!admissible_api_key(refused), "{refused}");
        }
        assert!(admissible_api_key(&"k".repeat(8 * 1024)));
        assert!(!admissible_api_key(&"k".repeat(8 * 1024 + 1)));
    }
}
