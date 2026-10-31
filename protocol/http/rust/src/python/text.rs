//! Python 3.14 `str` semantics over Unicode 16.0.0, which this protocol's validators depend on.
//!
//! Each function is named after the Python operation it reproduces. Lengths count code points, `strip` and `isspace`
//! use Python's whitespace (which includes U+001C..U+001F and U+0085), `isprintable` refuses every Other and Separator
//! character except the ASCII space, `casefold` and `lower` read the tables generated from Python itself,
//! and NFC is `unicode-normalization` 0.1.24, whose Unicode 16 data the tests prove equal to Python's on every code
//! point.

use std::sync::LazyLock;

use unicode_normalization::UnicodeNormalization;

use super::tables::{CASE_IGNORABLE, CASED, CASEFOLD, CATEGORY_RUNS, LOWER, WHITESPACE};

const CAPITAL_SIGMA: char = '\u{3a3}';

/// `len(text)`: the number of code points.
#[must_use]
pub fn len(text: &str) -> usize {
    text.chars().count()
}

/// `character.isspace()`.
#[must_use]
pub fn isspace(character: char) -> bool {
    WHITESPACE.binary_search(&character).is_ok()
}

/// `text.strip()` with no argument: leading and trailing Python whitespace removed.
#[must_use]
pub fn strip(text: &str) -> &str {
    text.trim_matches(isspace)
}

/// `text.rstrip()` with no argument.
#[must_use]
pub fn rstrip(text: &str) -> &str {
    text.trim_end_matches(isspace)
}

/// `text.strip(characters)`: leading and trailing members of `characters` removed.
#[must_use]
pub fn strip_chars<'a>(text: &'a str, characters: &str) -> &'a str {
    text.trim_matches(|character| characters.contains(character))
}

/// `unicodedata.category(character)`: the two-letter `General_Category` code.
#[must_use]
pub fn category(character: char) -> &'static str {
    let runs = category_runs();
    let point = u32::from(character);
    let index = runs.partition_point(|(start, _)| *start <= point) - 1;
    runs[index].1
}

/// The parsed [`CATEGORY_RUNS`]: each run's first code point and its code.
fn category_runs() -> &'static [(u32, &'static str)] {
    static RUNS: LazyLock<Vec<(u32, &'static str)>> = LazyLock::new(|| {
        (0..CATEGORY_RUNS.len())
            .step_by(8)
            .map(|offset| {
                let start = &CATEGORY_RUNS[offset..offset + 6];
                let start = u32::from_str_radix(start, 16).expect("a hex table");
                (start, &CATEGORY_RUNS[offset + 6..offset + 8])
            })
            .collect()
    });
    &RUNS
}

/// `character.isprintable()`: false for every Other (C*) and Separator (Z*) character except the ASCII space.
#[must_use]
pub fn isprintable_char(character: char) -> bool {
    character == ' ' || !matches!(category(character).as_bytes()[0], b'C' | b'Z')
}

/// `text.isprintable()`: every character printable (true for the empty string).
#[must_use]
pub fn isprintable(text: &str) -> bool {
    text.chars().all(isprintable_char)
}

/// Whether `character` is a Python `re` word character (`\w`): a letter, a number, or `_`.
#[must_use]
pub fn is_word(character: char) -> bool {
    character == '_' || matches!(category(character).as_bytes()[0], b'L' | b'N')
}

/// `unicodedata.normalize("NFC", text)`.
#[must_use]
pub fn nfc(text: &str) -> String {
    text.nfc().collect()
}

/// `unicodedata.is_normalized("NFC", text)`, which is `normalize("NFC", text) == text`.
#[must_use]
pub fn is_nfc(text: &str) -> bool {
    text.nfc().eq(text.chars())
}

fn mapped(table: &'static [(char, &'static str)], character: char) -> Option<&'static str> {
    table
        .binary_search_by_key(&character, |(key, _)| *key)
        .ok()
        .map(|index| table[index].1)
}

fn in_ranges(table: &[(char, char)], character: char) -> bool {
    table
        .binary_search_by(|(start, end)| {
            if *end < character {
                std::cmp::Ordering::Less
            } else if *start > character {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `text.casefold()`: Python's full case folding.
#[must_use]
pub fn casefold(text: &str) -> String {
    let mut folded = String::with_capacity(text.len());
    for character in text.chars() {
        match mapped(CASEFOLD, character) {
            Some(folding) => folded.push_str(folding),
            None => folded.push(character),
        }
    }
    folded
}

/// `text.lower()`: Python's full lowercase mapping, with U+03A3 lowered to final sigma in its `Final_Sigma` context.
#[must_use]
pub fn lower(text: &str) -> String {
    let characters: Vec<char> = text.chars().collect();
    let mut lowered = String::with_capacity(text.len());
    for (index, character) in characters.iter().enumerate() {
        if *character == CAPITAL_SIGMA {
            lowered.push(if final_sigma(&characters, index) {
                '\u{3c2}'
            } else {
                '\u{3c3}'
            });
        } else {
            match mapped(LOWER, *character) {
                Some(lowering) => lowered.push_str(lowering),
                None => lowered.push(*character),
            }
        }
    }
    lowered
}

/// The C `handle_capital_sigma` of Python: a cased character before the sigma, skipping case-ignorable ones, and no cased
/// character after it, skipping case-ignorable ones.
fn final_sigma(characters: &[char], index: usize) -> bool {
    let cased_stop = |character: &char| !in_ranges(CASE_IGNORABLE, *character);
    let before = characters[..index].iter().rev().find(|c| cased_stop(c));
    let after = characters[index + 1..].iter().find(|c| cased_stop(c));
    before.is_some_and(|character| in_ranges(CASED, *character))
        && after.is_none_or(|character| !in_ranges(CASED, *character))
}

#[cfg(test)]
mod tests {
    use super::{
        casefold, category, is_nfc, is_word, isprintable, isspace, len, lower, nfc, rstrip, strip,
        strip_chars,
    };

    #[test]
    fn lengths_count_code_points() {
        assert_eq!(len("aé😀"), 3);
        assert_eq!(len(""), 0);
    }

    #[test]
    fn whitespace_is_python_whitespace() {
        for character in [
            '\t', '\u{b}', '\u{1c}', '\u{1f}', ' ', '\u{85}', '\u{a0}', '\u{3000}',
        ] {
            assert!(isspace(character), "{character:?}");
        }
        for character in ['a', '\u{200b}', '\u{feff}', '\u{180e}'] {
            assert!(!isspace(character), "{character:?}");
        }
        assert_eq!(strip("\u{1c} a b\u{85}\n"), "a b");
        assert_eq!(rstrip(" a \u{2029}"), " a");
        assert_eq!(strip_chars("¿¡a?!", "¿¡?!"), "a");
    }

    #[test]
    fn categories_and_printability_follow_python() {
        assert_eq!(category('a'), "Ll");
        assert_eq!(category('\u{378}'), "Cn");
        assert!(isprintable("a b é"));
        assert!(isprintable(""));
        for text in [
            "\u{a0}", "\u{2028}", "\u{200b}", "\u{7f}", "\u{e000}", "\u{378}",
        ] {
            assert!(!isprintable(text), "{text:?}");
        }
        assert!(
            is_word('_') && is_word('٣') && is_word('ж') && !is_word('-') && !is_word('\u{300}')
        );
    }

    #[test]
    fn normalization_is_nfc() {
        assert_eq!(nfc("e\u{301}"), "é");
        assert!(is_nfc("é") && !is_nfc("e\u{301}"));
    }

    #[test]
    fn folding_and_lowering_follow_python() {
        assert_eq!(casefold("Straße ﬁ İ"), "strasse fi i\u{307}");
        assert_eq!(lower("İ ΑΣ"), "i\u{307} ας");
        assert_eq!(lower("Σ"), "σ");
        assert_eq!(lower("ΑΣΑ"), "ασα");
        assert_eq!(lower("Α\u{301}Σ\u{301}"), "α\u{301}ς\u{301}");
        assert_eq!(lower("A\u{2e}Σ b"), "a\u{2e}ς b");
        assert_eq!(lower("1Σ"), "1σ");
        assert_eq!(lower("\u{212a}"), "k");
    }
}
