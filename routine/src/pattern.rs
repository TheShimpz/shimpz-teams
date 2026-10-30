//! Python `re` patterns compiled with `re.IGNORECASE`, on the same translation the Team protocol's patterns use.
//!
//! The protocol's translator reproduces Python's syntax without flags; a case-insensitive pattern here is that
//! translation under the engine's own case folding, which matches every letter Python's does except the dotted and
//! dotless `i` (U+0130, U+0131), which Python also takes for `i`. A caller whose pattern holds an `i` that must read
//! them folds its subject first (see [`fold_dotted_i`]).

use fancy_regex::{Regex, RegexBuilder};
use team_protocol::python::regex::{BACKTRACK_LIMIT, Match, MatchError, translate};

/// A compiled case-insensitive Python pattern.
#[derive(Debug)]
pub struct Folded(Regex);

impl Folded {
    /// Compiles a pattern this crate declares.
    ///
    /// # Panics
    ///
    /// When the pattern is outside the translated subset; the tests compile every declared pattern.
    #[must_use]
    pub fn declared(python: &str) -> Self {
        let translated = translate(python).expect("a declared pattern translates");
        Self(
            RegexBuilder::new(&format!("(?i){translated}"))
                .backtrack_limit(BACKTRACK_LIMIT)
                .build()
                .expect("a declared pattern compiles"),
        )
    }

    /// `pattern.search(text, position)`: the first match at or after byte `position`.
    ///
    /// # Errors
    ///
    /// [`MatchError::Engine`] when the search cannot complete.
    pub fn search_from(&self, text: &str, position: usize) -> Result<Option<Match>, MatchError> {
        let captures = self
            .0
            .captures_from_pos(text, position)
            .map_err(|_| MatchError::Engine)?;
        Ok(captures.map(|captures| {
            let whole = captures
                .get(0)
                .map_or((position, position), |found| (found.start(), found.end()));
            Match {
                start: whole.0,
                end: whole.1,
                groups: (1..captures.len())
                    .map(|index| {
                        captures
                            .get(index)
                            .map(|group| (group.start(), group.end()))
                    })
                    .collect(),
            }
        }))
    }

    /// `pattern.search(text) is not None`.
    ///
    /// # Errors
    ///
    /// [`MatchError::Engine`] when the search cannot complete.
    pub fn found(&self, text: &str) -> Result<bool, MatchError> {
        Ok(self.search_from(text, 0)?.is_some())
    }

    /// `list(pattern.finditer(text))` for a pattern whose matches are never empty.
    ///
    /// # Errors
    ///
    /// [`MatchError`] when a search cannot complete or a match is empty.
    pub fn finditer(&self, text: &str) -> Result<Vec<Match>, MatchError> {
        let mut found = Vec::new();
        let mut position = 0;
        while let Some(next) = self.search_from(text, position)? {
            if next.start == next.end {
                return Err(MatchError::EmptyMatch);
            }
            position = next.end;
            found.push(next);
        }
        Ok(found)
    }
}

/// The text with U+0130 and U+0131 written as `i`, which Python's case-insensitive `i` and `[a-z]` also match.
#[must_use]
pub fn fold_dotted_i(text: &str) -> String {
    text.chars()
        .map(|character| match character {
            '\u{130}' | '\u{131}' => 'i',
            other => other,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::{Folded, fold_dotted_i};

    #[test]
    fn a_folded_pattern_reads_every_case_of_its_letters_and_keeps_its_lookarounds() {
        let pattern = Folded::declared(r"(?<![\w.])(\d{1,2})\s+(outubro|märz|août)(?!\w)");
        for text in ["15 outubro", "15 OUTUBRO", "1 MÄRZ", "3 Août"] {
            assert!(pattern.found(text).expect("a search"), "{text}");
        }
        for text in ["a15 outubro", "15 outubros", "x.15 outubro"] {
            assert!(!pattern.found(text).expect("a search"), "{text}");
        }
        let found = pattern
            .finditer("15 outubro e 16 Outubro")
            .expect("a search");
        assert_eq!(found.len(), 2);
        assert_eq!(
            found[1].group("15 outubro e 16 Outubro", 2),
            Some("Outubro")
        );
        assert_eq!(fold_dotted_i("apİ_KEY=ı"), "api_KEY=i");
    }
}
