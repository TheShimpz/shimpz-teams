//! Python `re` patterns run on `fancy-regex` with Python's meaning.
//!
//! [`translate`] rewrites the Python syntax this protocol uses into `fancy-regex` syntax: `\w` becomes
//! `[\p{L}\p{N}_]` (Python's word characters, `str.isalnum()` or `_`, which the tests prove equal on every code
//! point), `\d` `\p{Nd}`, `\s` the explicit `str.isspace()` set, `\b` and `\B` lookarounds over that `\w`, `$` the
//! Python end (`(?=\n?\z)`), `\Z` `\z`, `{` that opens no quantifier a literal, and every literal of a class a hex
//! escape, so a Python class never reads as a nested class or a set operation. Anything else is refused, never
//! guessed. A [`Pattern`] runs with a pinned backtracking limit: reaching it refuses the whole operation
//! ([`MatchError`]); it is never read as "no match".

use std::fmt::Write as _;

use fancy_regex::{Regex, RegexBuilder};

use super::tables::WHITESPACE;

/// The most backtracking steps one search may take before it refuses the operation.
///
/// `fancy-regex` counts the steps of one search across every start position it tries, so the limit also bounds the
/// length of text a lookaround pattern can scan: about twenty steps per character for the phrase tables, so this limit
/// refuses only texts of several hundred thousand characters, far beyond the 16,000-character chat message, the
/// longest text any reader is given, while an exponential pattern still stops within a fraction of a second.
pub const BACKTRACK_LIMIT: usize = 10_000_000;

const WORD_MEMBERS: &str = r"\p{L}\p{N}\x{5f}";
const WORD: &str = r"[\p{L}\p{N}\x{5f}]";
const NOT_WORD: &str = r"[^\p{L}\p{N}\x{5f}]";
const EMPTY_CLASS: &str = r"[^\x{0}-\x{10ffff}]";
const ANY_CLASS: &str = r"[\x{0}-\x{10ffff}]";
const SURROGATES: (u32, u32) = (0xd800, 0xdfff);
const MAX_CODE_POINT: u32 = 0x10_ffff;

/// Why a Python pattern could not be translated.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TranslateError {
    /// The pattern ends inside an escape, class, or group opener.
    Truncated,
    /// The pattern uses Python syntax this translator does not reproduce.
    Unsupported,
    /// A class range runs backwards or from a class escape.
    BadRange,
    /// The translated pattern did not compile.
    Engine,
}

/// A search could not complete, so the operation that needed it is refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MatchError {
    /// The engine stopped, for example at its backtracking limit.
    Engine,
    /// An iterated pattern matched the empty string, whose Python iteration rules this module does not reproduce.
    EmptyMatch,
}

/// One match: byte spans of the whole match and of each group, `None` for a group that did not participate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Match {
    /// Byte offset where the match starts.
    pub start: usize,
    /// Byte offset where the match ends.
    pub end: usize,
    /// The span of each group, from group 1.
    pub groups: Vec<Option<(usize, usize)>>,
}

impl Match {
    /// `match.group(index)` for `index >= 1` over `text`, the searched text.
    #[must_use]
    pub fn group<'a>(&self, text: &'a str, index: usize) -> Option<&'a str> {
        self.groups
            .get(index - 1)
            .copied()
            .flatten()
            .map(|(start, end)| &text[start..end])
    }
}

/// A compiled Python pattern.
#[derive(Debug)]
pub struct Pattern {
    search: Regex,
    full: Regex,
}

impl Pattern {
    /// Translates and compiles a Python pattern.
    ///
    /// # Errors
    ///
    /// Returns a [`TranslateError`] for syntax outside the translated subset.
    pub fn new(python: &str) -> Result<Self, TranslateError> {
        Self::with_limit(python, BACKTRACK_LIMIT)
    }

    /// [`Pattern::new`] with its own backtracking limit instead of [`BACKTRACK_LIMIT`].
    ///
    /// # Errors
    ///
    /// Returns a [`TranslateError`] for syntax outside the translated subset.
    pub fn with_limit(python: &str, limit: usize) -> Result<Self, TranslateError> {
        let translated = translate(python)?;
        Ok(Self {
            search: build(&translated, limit)?,
            full: build(&format!(r"\A(?:{translated})\z"), limit)?,
        })
    }

    /// `pattern.fullmatch(text) is not None`.
    ///
    /// # Errors
    ///
    /// Returns [`MatchError::Engine`] when the search cannot complete.
    pub fn fullmatch(&self, text: &str) -> Result<bool, MatchError> {
        self.full.is_match(text).map_err(|_| MatchError::Engine)
    }

    /// `pattern.search(text)`, the first match.
    ///
    /// # Errors
    ///
    /// Returns [`MatchError::Engine`] when the search cannot complete.
    pub fn search(&self, text: &str) -> Result<Option<Match>, MatchError> {
        self.search_from(text, 0)
    }

    /// `pattern.search(text, position)`: the first match starting at or after byte `position`, whose lookbehinds
    /// still see the text before it.
    ///
    /// # Errors
    ///
    /// Returns [`MatchError::Engine`] when the search cannot complete.
    pub fn search_from(&self, text: &str, position: usize) -> Result<Option<Match>, MatchError> {
        let captures = self
            .search
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

    /// `list(pattern.finditer(text))` for a pattern whose matches are never empty.
    ///
    /// # Errors
    ///
    /// Returns [`MatchError`] when a search cannot complete or a match is empty.
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

    /// `pattern.sub(replace, text)` for a pattern whose matches are never empty.
    ///
    /// # Errors
    ///
    /// Returns [`MatchError`] when a search cannot complete or a match is empty.
    pub fn sub(&self, text: &str, replace: impl Fn(&str) -> String) -> Result<String, MatchError> {
        let mut replaced = String::with_capacity(text.len());
        let mut copied = 0;
        for found in self.finditer(text)? {
            replaced.push_str(&text[copied..found.start]);
            replaced.push_str(&replace(&text[found.start..found.end]));
            copied = found.end;
        }
        replaced.push_str(&text[copied..]);
        Ok(replaced)
    }
}

/// Compiles a pattern this crate declares.
///
/// # Panics
///
/// Panics when the pattern is outside the translated subset; the tests compile every declared pattern.
#[must_use]
pub fn declared(python: &str) -> Pattern {
    Pattern::new(python).expect("a declared pattern translates")
}

impl Pattern {
    /// `fullmatch(text) is not None`, where a search that cannot complete counts as no match, which refuses.
    #[must_use]
    pub fn is_full(&self, text: &str) -> bool {
        self.fullmatch(text).unwrap_or(false)
    }

    /// `fullmatch` of a value: a string that matches whole.
    #[must_use]
    pub fn is_full_value(&self, value: &strict_json::Value) -> bool {
        matches!(value, strict_json::Value::String(text) if self.is_full(text))
    }

    /// `search(text) is None`, where a search that cannot complete counts as a match, which refuses.
    #[must_use]
    pub fn is_absent(&self, text: &str) -> bool {
        matches!(self.search(text), Ok(None))
    }
}

fn build(pattern: &str, limit: usize) -> Result<Regex, TranslateError> {
    RegexBuilder::new(pattern)
        .backtrack_limit(limit)
        .build()
        .map_err(|_| TranslateError::Engine)
}

fn hex(character: char) -> String {
    format!(r"\x{{{:x}}}", u32::from(character))
}

fn whitespace_members() -> String {
    WHITESPACE.iter().map(|character| hex(*character)).collect()
}

fn word_boundary(negated: bool) -> String {
    if negated {
        format!("(?:(?<={WORD})(?={WORD})|(?<!{WORD})(?!{WORD}))")
    } else {
        format!("(?:(?<={WORD})(?!{WORD})|(?<!{WORD})(?={WORD}))")
    }
}

/// One parsed Python escape or literal: a character, or a class of characters.
enum Atom {
    Character(u32),
    Class(String),
}

struct Reader {
    characters: Vec<char>,
    index: usize,
}

impl Reader {
    fn new(source: &str) -> Self {
        Self {
            characters: source.chars().collect(),
            index: 0,
        }
    }

    fn peek(&self) -> Option<char> {
        self.characters.get(self.index).copied()
    }

    fn peek_at(&self, offset: usize) -> Option<char> {
        self.characters.get(self.index + offset).copied()
    }

    fn next(&mut self) -> Result<char, TranslateError> {
        let character = self.peek().ok_or(TranslateError::Truncated)?;
        self.index += 1;
        Ok(character)
    }

    fn eat(&mut self, expected: &str) -> bool {
        let matches = expected
            .chars()
            .enumerate()
            .all(|(offset, character)| self.peek_at(offset) == Some(character));
        if matches {
            self.index += expected.chars().count();
        }
        matches
    }

    fn hex_digits(&mut self, count: usize) -> Result<u32, TranslateError> {
        let mut value = 0;
        for _ in 0..count {
            let digit = self
                .next()?
                .to_digit(16)
                .ok_or(TranslateError::Unsupported)?;
            value = value * 16 + digit;
        }
        Ok(value)
    }

    fn digits(&mut self) -> String {
        let mut digits = String::new();
        while let Some(character) = self.peek().filter(char::is_ascii_digit) {
            digits.push(character);
            self.index += 1;
        }
        digits
    }
}

/// An escape that reads one character in and out of classes, or the class escapes `\w \W \d \D \s \S`.
fn common_escape(reader: &mut Reader, escaped: char) -> Result<Atom, TranslateError> {
    let class = |text: String| Ok(Atom::Class(text));
    match escaped {
        'w' => class(WORD.to_owned()),
        'W' => class(NOT_WORD.to_owned()),
        'd' => class(r"\p{Nd}".to_owned()),
        'D' => class(r"\P{Nd}".to_owned()),
        's' => class(format!("[{}]", whitespace_members())),
        'S' => class(format!("[^{}]", whitespace_members())),
        'n' => Ok(Atom::Character(0x0a)),
        't' => Ok(Atom::Character(0x09)),
        'r' => Ok(Atom::Character(0x0d)),
        'f' => Ok(Atom::Character(0x0c)),
        'v' => Ok(Atom::Character(0x0b)),
        'a' => Ok(Atom::Character(0x07)),
        'x' => Ok(Atom::Character(reader.hex_digits(2)?)),
        'u' => Ok(Atom::Character(reader.hex_digits(4)?)),
        'U' => Ok(Atom::Character(reader.hex_digits(8)?)),
        other if other.is_ascii_alphanumeric() => Err(TranslateError::Unsupported),
        other => Ok(Atom::Character(u32::from(other))),
    }
}

fn literal(point: u32) -> Result<String, TranslateError> {
    if (SURROGATES.0..=SURROGATES.1).contains(&point) {
        // A Rust string never holds a lone surrogate, so this literal matches nothing.
        return Ok(EMPTY_CLASS.to_owned());
    }
    char::from_u32(point)
        .map(hex)
        .ok_or(TranslateError::Unsupported)
}

/// Translates one Python pattern (no flags, no backreferences) into `fancy-regex` syntax.
///
/// # Errors
///
/// Returns a [`TranslateError`] for syntax outside the translated subset.
pub fn translate(python: &str) -> Result<String, TranslateError> {
    let mut reader = Reader::new(python);
    let mut out = String::with_capacity(python.len() * 2);
    while let Some(character) = reader.peek() {
        reader.index += 1;
        match character {
            '\\' => out.push_str(&outside_escape(&mut reader)?),
            '[' => out.push_str(&class(&mut reader)?),
            '(' => out.push_str(group_opener(&mut reader)?),
            ')' | '|' | '*' | '+' | '?' | '.' | '^' => out.push(character),
            '$' => out.push_str(r"(?=\n?\z)"),
            '{' => out.push_str(&brace(&mut reader)),
            other => out.push_str(&hex(other)),
        }
    }
    Ok(out)
}

fn outside_escape(reader: &mut Reader) -> Result<String, TranslateError> {
    let escaped = reader.next()?;
    match escaped {
        'b' => Ok(word_boundary(false)),
        'B' => Ok(word_boundary(true)),
        'A' => Ok(r"\A".to_owned()),
        'Z' => Ok(r"\z".to_owned()),
        other => match common_escape(reader, other)? {
            Atom::Character(point) => literal(point),
            Atom::Class(text) => Ok(text),
        },
    }
}

fn group_opener(reader: &mut Reader) -> Result<&'static str, TranslateError> {
    if !reader.eat("?") {
        return Ok("(");
    }
    for (python, rust) in [
        (":", "(?:"),
        ("=", "(?="),
        ("!", "(?!"),
        ("<=", "(?<="),
        ("<!", "(?<!"),
    ] {
        if reader.eat(python) {
            return Ok(rust);
        }
    }
    Err(TranslateError::Unsupported)
}

/// Python reads `{m}`, `{m,}`, `{,n}`, `{m,n}`, and `{,}` as quantifiers and any other `{` as a literal.
fn brace(reader: &mut Reader) -> String {
    let start = reader.index;
    let low = reader.digits();
    let high = if reader.eat(",") {
        Some(reader.digits())
    } else {
        None
    };
    if (low.is_empty() && high.is_none()) || !reader.eat("}") {
        reader.index = start;
        return hex('{');
    }
    let low = if low.is_empty() { "0".to_owned() } else { low };
    match high {
        None => format!("{{{low}}}"),
        Some(high) => format!("{{{low},{high}}}"),
    }
}

/// One member of a class: a character (which may start a range) or a class escape.
fn class_atom(reader: &mut Reader) -> Result<Atom, TranslateError> {
    let character = reader.next()?;
    if character != '\\' {
        return Ok(Atom::Character(u32::from(character)));
    }
    match reader.next()? {
        'b' => Ok(Atom::Character(0x08)),
        escaped => common_escape(reader, escaped),
    }
}

fn class(reader: &mut Reader) -> Result<String, TranslateError> {
    let negated = reader.eat("^");
    let mut members = String::new();
    let mut ranges: Vec<(u32, u32)> = Vec::new();
    let mut first = true;
    loop {
        if reader.peek() == Some(']') && !first {
            reader.index += 1;
            break;
        }
        first = false;
        let atom = class_atom(reader)?;
        let is_range =
            reader.peek() == Some('-') && reader.peek_at(1).is_some_and(|next| next != ']');
        match atom {
            Atom::Class(_) if is_range => return Err(TranslateError::BadRange),
            Atom::Class(text) => members.push_str(&class_member(&text)),
            Atom::Character(low) if is_range => {
                reader.index += 1;
                let Atom::Character(high) = class_atom(reader)? else {
                    return Err(TranslateError::BadRange);
                };
                if high < low {
                    return Err(TranslateError::BadRange);
                }
                if high > MAX_CODE_POINT {
                    return Err(TranslateError::Unsupported);
                }
                ranges.push((low, high));
            }
            Atom::Character(point) if point > MAX_CODE_POINT => {
                return Err(TranslateError::Unsupported);
            }
            Atom::Character(point) => ranges.push((point, point)),
        }
    }
    for (low, high) in ranges {
        members.push_str(&class_range(low, high));
    }
    Ok(match (members.is_empty(), negated) {
        (true, false) => EMPTY_CLASS.to_owned(),
        (true, true) => ANY_CLASS.to_owned(),
        (false, false) => format!("[{members}]"),
        (false, true) => format!("[^{members}]"),
    })
}

/// A class escape as a class member: `\w` and `\s` contribute members, a negated or Unicode class nests whole.
fn class_member(text: &str) -> String {
    if text == WORD {
        WORD_MEMBERS.to_owned()
    } else {
        text.to_owned()
    }
}

/// A range of code points without the surrogates, which no Rust string holds.
fn class_range(low: u32, high: u32) -> String {
    let mut text = String::new();
    let parts = [
        (low, high.min(SURROGATES.0 - 1)),
        (low.max(SURROGATES.1 + 1), high),
    ];
    for (start, end) in parts {
        if start > end {
            continue;
        }
        if start == end {
            let _ = write!(text, r"\x{{{start:x}}}");
        } else {
            let _ = write!(text, r"\x{{{start:x}}}-\x{{{end:x}}}");
        }
    }
    text
}

#[cfg(test)]
mod tests;
