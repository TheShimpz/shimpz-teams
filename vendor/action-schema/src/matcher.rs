//! Matching of an admitted pattern with the RE2 semantics Team applies, under one payload's work budget.
//!
//! RE2's `\d`, `\w`, `\s`, and `\b` are ASCII (`\s` is tab, newline, form feed, carriage return, and space). The Rust
//! engine reads those escapes as Unicode, so each one is rewritten to its explicit ASCII form before compilation, inside
//! a class as a nested class. RE2 also searches UTF-8 bytes, so `\B` holds between the bytes of one non-ASCII character;
//! the byte-oriented Rust engine reproduces that empty match. Every other admitted construct already means the same
//! thing to both engines.

use std::collections::HashMap;
use std::convert::Infallible;
use std::fmt;

use regex::bytes::Regex;
use regex_syntax::ast::{
    AssertionKind, Ast, ClassPerl, ClassPerlKind, ClassSetItem, Span, Visitor, visit,
};

use crate::MAX_PATTERN_WORK;
use crate::pattern::{parse, pattern_costs};

/// Why a pattern search did not run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PatternError {
    /// The pattern is outside the admitted dialect.
    Refused,
    /// The search would charge more than the payload's remaining matching work.
    Exhausted,
}

impl fmt::Display for PatternError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Refused => "pattern is outside the linear-time matcher",
            Self::Exhausted => "pattern matching exceeds its work budget",
        })
    }
}

impl std::error::Error for PatternError {}

/// One payload validation's matching work: [`MAX_PATTERN_WORK`] at first, less each preparation's and each search's
/// charge, and every pattern prepared so far, which later searches reuse.
#[derive(Debug)]
pub struct Budget {
    remaining: u64,
    prepared: HashMap<String, (Regex, u64)>,
}

impl Default for Budget {
    fn default() -> Self {
        Self {
            remaining: MAX_PATTERN_WORK,
            prepared: HashMap::new(),
        }
    }
}

impl Budget {
    /// The work not yet charged.
    #[must_use]
    pub const fn remaining(&self) -> u64 {
        self.remaining
    }

    /// Charges `units`, or refuses them, leaving the budget as it was, when they exceed what remains.
    fn charge(&mut self, units: u64) -> Result<(), PatternError> {
        self.remaining = self
            .remaining
            .checked_sub(units)
            .ok_or(PatternError::Exhausted)?;
        Ok(())
    }

    /// Prepares a pattern once per budget: its syntax is read for its bound and preparation, the preparation is
    /// charged, and only then is the program compiled.
    fn prepare(&mut self, pattern: &str) -> Result<u64, PatternError> {
        if let Some((_, bound)) = self.prepared.get(pattern) {
            return Ok(*bound);
        }
        let (bound, preparation) = pattern_costs(pattern).ok_or(PatternError::Refused)?;
        self.charge(preparation)?;
        let regex = compile(pattern).ok_or(PatternError::Refused)?;
        self.prepared.insert(pattern.to_owned(), (regex, bound));
        Ok(bound)
    }
}

/// Whether `pattern` matches anywhere in `subject`, as JSON Schema `pattern` requires.
///
/// A pattern this budget has not prepared yet is first charged its [`crate::pattern_preparation`]; the search then
/// charges `subject`'s UTF-8 length, at least 1, times its [`crate::pattern_program`] bound.
///
/// # Errors
///
/// [`PatternError::Refused`] when the pattern is outside the admitted dialect, and [`PatternError::Exhausted`], before
/// it compiles or searches, when a charge exceeds the remaining work.
pub fn pattern_matches(
    pattern: &str,
    subject: &str,
    budget: &mut Budget,
) -> Result<bool, PatternError> {
    let bound = budget.prepare(pattern)?;
    let bytes = u64::try_from(subject.len()).unwrap_or(u64::MAX).max(1);
    budget.charge(bytes.saturating_mul(bound))?;
    Ok(budget
        .prepared
        .get(pattern)
        .is_some_and(|(regex, _)| regex.is_match(subject.as_bytes())))
}

/// Compiles one admitted pattern for an unanchored RE2-equivalent search.
fn compile(pattern: &str) -> Option<Regex> {
    let Ok(rewrites) = visit(&parse(pattern)?, Rewrites::default());
    let mut translated = String::with_capacity(pattern.len());
    let mut copied = 0;
    for (span, replacement) in rewrites {
        translated.push_str(&pattern[copied..span.start.offset]);
        translated.push_str(replacement);
        copied = span.end.offset;
    }
    translated.push_str(&pattern[copied..]);
    Regex::new(&translated).ok()
}

/// Source spans to replace, in pattern order.
#[derive(Default)]
struct Rewrites(Vec<(Span, &'static str)>);

impl Visitor for Rewrites {
    type Output = Vec<(Span, &'static str)>;
    type Err = Infallible;

    fn finish(self) -> Result<Self::Output, Infallible> {
        Ok(self.0)
    }

    fn visit_pre(&mut self, ast: &Ast) -> Result<(), Infallible> {
        match ast {
            Ast::ClassPerl(class) => self.0.push((class.span, ascii_class(class))),
            Ast::Assertion(assertion) => match assertion.kind {
                AssertionKind::WordBoundary => self.0.push((assertion.span, "(?-u:\\b)")),
                AssertionKind::NotWordBoundary => self.0.push((assertion.span, "(?-u:\\B)")),
                _ => {}
            },
            _ => {}
        }
        Ok(())
    }

    fn visit_class_set_item_pre(&mut self, item: &ClassSetItem) -> Result<(), Infallible> {
        // A nested class is a union member in the Rust engine.
        if let ClassSetItem::Perl(class) = item {
            self.0.push((class.span, ascii_class(class)));
        }
        Ok(())
    }
}

const fn ascii_class(class: &ClassPerl) -> &'static str {
    match (&class.kind, class.negated) {
        (ClassPerlKind::Digit, false) => "[0-9]",
        (ClassPerlKind::Digit, true) => "[^0-9]",
        (ClassPerlKind::Word, false) => "[0-9A-Za-z_]",
        (ClassPerlKind::Word, true) => "[^0-9A-Za-z_]",
        (ClassPerlKind::Space, false) => "[\\t\\n\\f\\r ]",
        (ClassPerlKind::Space, true) => "[^\\t\\n\\f\\r ]",
    }
}

#[cfg(test)]
mod tests;
