//! Public text admission (`core/text.py`): one bounded line a person may see.

use team_protocol::python::text;

/// Whether `value` is non-empty printable text without surrounding whitespace, of at most `max_bytes` UTF-8 bytes.
#[must_use]
pub fn public_bytes(value: &str, max_bytes: usize) -> bool {
    public(value) && value.len() <= max_bytes
}

/// Whether `value` is non-empty printable text without surrounding whitespace, of at most `max_chars` characters.
#[must_use]
pub fn public_chars(value: &str, max_chars: usize) -> bool {
    public(value) && text::len(value) <= max_chars
}

fn public(value: &str) -> bool {
    !value.is_empty() && text::strip(value) == value && text::isprintable(value)
}

#[cfg(test)]
mod tests {
    use super::{public_bytes, public_chars};

    #[test]
    fn public_text_is_one_bounded_printable_line() {
        assert!(public_bytes("Cloudflare", 10));
        assert!(public_chars("é", 1));
        for refused in ["", " padded", "padded ", "line\nbreak", "\u{202e}"] {
            assert!(!public_bytes(refused, 64), "{refused:?}");
        }
        assert!(!public_bytes("é", 1));
        assert!(!public_chars("ab", 1));
    }
}
