//! The pattern dialect every Action schema `pattern` value and `patternProperties` name is admitted in.
//!
//! An admitted pattern uses only syntax that RE2, Python `re`, and the Rust `regex` engine read alike, nests its
//! syntax tree at most [`MAX_PATTERN_NESTING`] levels deep, keeps every counted repetition within RE2's count bound,
//! and fits [`MAX_PATTERN_PROGRAM`] instructions by an upper bound of its RE2 program ([`pattern_program`]). That bound
//! is also what one search of the pattern charges per byte of its subject against the payload's matching budget.
//!
//! [`pattern_preparation`] is what preparing the pattern (reading its syntax and compiling its program) charges, once
//! per payload validation: [`PREPARATION_BASE`] for building a matcher, [`PREPARATION_WEIGHT`] per unit of the program
//! bound, and, for every bracketed class, the case-folding work the Rust engine spends on it under the `i` flag, which
//! visits every code point the class's members cover and is therefore charged by code point, not by program size.

use crate::{PREPARATION_BASE, PREPARATION_WEIGHT};

use regex_syntax::ast::{
    self, AssertionKind, Ast, ClassBracketed, ClassSet, ClassSetItem, Flag, Flags, FlagsItemKind,
    GroupKind, HexLiteralKind, Literal, LiteralKind, Repetition, RepetitionKind, RepetitionRange,
};

/// The deepest an admitted pattern's syntax tree nests.
pub const MAX_PATTERN_NESTING: u32 = 32;
/// RE2 refuses a repetition count above 1,000 and nested counted repetitions whose counts multiply beyond it on any
/// path.
pub const MAX_REPEAT: u32 = 1000;
/// The most RE2 instructions an admitted pattern's program may hold, by the upper bound [`pattern_program`] computes.
pub const MAX_PATTERN_PROGRAM: u64 = 16_384;
/// Instructions RE2 spends on an empty pattern and an unanchored search.
const PROGRAM_OVERHEAD: u64 = 16;
/// Case folding a class visits every code point its members cover, a literal as one and a Perl class as the ASCII
/// range or, negated, every code point, whatever the flags: `FOLD_WEIGHT` per code point below `CASED_END`, where every
/// case mapping lies, and `SCAN_WEIGHT` per code point at or above it, which the fold only scans.
const FOLD_WEIGHT: u64 = 64;
const SCAN_WEIGHT: u64 = 4;
const CASED_END: u32 = 0x2_0000;

/// The parsed syntax tree of a pattern within the nesting bound, or `None` when the Rust parser refuses it.
pub(crate) fn parse(pattern: &str) -> Option<Ast> {
    ast::parse::ParserBuilder::new()
        .nest_limit(MAX_PATTERN_NESTING)
        .build()
        .parse(pattern)
        .ok()
}

/// The upper bound of an admitted pattern's RE2 program, the search overhead included, or `None` when the pattern is
/// outside the admitted dialect or its bound exceeds [`MAX_PATTERN_PROGRAM`].
#[must_use]
pub fn pattern_program(pattern: &str) -> Option<u64> {
    pattern_costs(pattern).map(|(bound, _)| bound)
}

/// What preparing an admitted pattern charges against the matching budget, or `None` when the pattern is refused.
#[must_use]
pub fn pattern_preparation(pattern: &str) -> Option<u64> {
    pattern_costs(pattern).map(|(_, preparation)| preparation)
}

/// An admitted pattern's program bound and its preparation, or `None` when the pattern is refused.
pub(crate) fn pattern_costs(pattern: &str) -> Option<(u64, u64)> {
    let parsed = parse(pattern)?;
    let bound = PROGRAM_OVERHEAD.saturating_add(program_bound(&parsed));
    let preparation = PREPARATION_BASE
        .saturating_add(PREPARATION_WEIGHT.saturating_mul(bound))
        .saturating_add(fold_work(&parsed));
    (ast_admitted(&parsed, pattern)
        && repetition_budget(&parsed, MAX_REPEAT) > 0
        && bound <= MAX_PATTERN_PROGRAM)
        .then_some((bound, preparation))
}

/// Whether a pattern is admitted: [`pattern_program`] bounds it.
#[must_use]
pub fn pattern_admitted(pattern: &str) -> bool {
    pattern_program(pattern).is_some()
}

fn ast_admitted(node: &Ast, pattern: &str) -> bool {
    match node {
        Ast::Empty(_) | Ast::Dot(_) | Ast::ClassPerl(_) => true,
        // Python accepts global flags only at the start of the expression.
        Ast::Flags(set) => set.span.start.offset == 0 && flags_admitted(&set.flags, false),
        Ast::Literal(literal) => literal_admitted(literal),
        Ast::Assertion(assertion) => matches!(
            assertion.kind,
            AssertionKind::StartLine
                | AssertionKind::EndLine
                | AssertionKind::StartText
                | AssertionKind::WordBoundary
                | AssertionKind::NotWordBoundary
        ),
        Ast::ClassUnicode(_) => false,
        Ast::ClassBracketed(class) => class_items(class).is_some_and(class_items_admitted),
        Ast::Repetition(repetition) => repetition_admitted(repetition, pattern),
        Ast::Group(group) => {
            let kind = match &group.kind {
                GroupKind::CaptureIndex(_) => true,
                GroupKind::CaptureName {
                    starts_with_p,
                    name,
                } => *starts_with_p && python_group_name(&name.name),
                GroupKind::NonCapturing(flags) => flags_admitted(flags, true),
            };
            kind && ast_admitted(&group.ast, pattern)
        }
        Ast::Alternation(alternation) => alternation
            .asts
            .iter()
            .all(|child| ast_admitted(child, pattern)),
        Ast::Concat(concat) => concat.asts.iter().all(|child| ast_admitted(child, pattern)),
    }
}

/// A scoped group may turn flags off. RE2 has no verbose mode, so `x` is refused.
fn flags_admitted(flags: &Flags, scoped: bool) -> bool {
    flags.items.iter().all(|item| match item.kind {
        FlagsItemKind::Negation => scoped,
        FlagsItemKind::Flag(flag) => matches!(
            flag,
            Flag::CaseInsensitive | Flag::MultiLine | Flag::DotMatchesNewLine
        ),
    })
}

/// RE2 reads neither `\uHHHH` nor `\UHHHHHHHH`, and Python has no braced or octal escape; every other escape means the
/// same literal to both engines.
const fn literal_admitted(literal: &Literal) -> bool {
    matches!(
        literal.kind,
        LiteralKind::Verbatim
            | LiteralKind::Meta
            | LiteralKind::Superfluous
            | LiteralKind::Special(_)
            | LiteralKind::HexFixed(HexLiteralKind::X)
    )
}

/// The members of a class that is not a nested class or a set operation.
fn class_items(class: &ClassBracketed) -> Option<&[ClassSetItem]> {
    match &class.kind {
        ClassSet::Item(ClassSetItem::Union(union)) => Some(&union.items),
        ClassSet::Item(item) => Some(std::slice::from_ref(item)),
        ClassSet::BinaryOp(_) => None,
    }
}

/// Python reads an unescaped `-` as a range operator wherever it can, so one may appear only as the first or last
/// class member.
fn class_items_admitted(items: &[ClassSetItem]) -> bool {
    let dashes: Vec<usize> = items
        .iter()
        .enumerate()
        .filter(|(_, item)| matches!(item, ClassSetItem::Literal(literal) if bare_dash(literal)))
        .map(|(position, _)| position)
        .collect();
    let dashes_admitted = match dashes.as_slice() {
        [] => true,
        [position] => *position == 0 || *position + 1 == items.len(),
        _ => false,
    };
    dashes_admitted
        && items.iter().all(|item| match item {
            ClassSetItem::Literal(literal) => literal_admitted(literal),
            // Neither end of a range can be a bare `-`: the parser reads one there as a literal or an operator.
            ClassSetItem::Range(range) => {
                literal_admitted(&range.start) && literal_admitted(&range.end)
            }
            ClassSetItem::Perl(_) => true,
            ClassSetItem::Empty(_)
            | ClassSetItem::Ascii(_)
            | ClassSetItem::Unicode(_)
            | ClassSetItem::Bracketed(_)
            | ClassSetItem::Union(_) => false,
        })
}

fn bare_dash(literal: &Literal) -> bool {
    literal.kind == LiteralKind::Verbatim && literal.c == '-'
}

/// The count RE2 multiplies along nested counted repetitions: the upper bound, or the lower bound when there is none.
/// Unbounded operators count zero.
const fn repetition_count(repetition: &Repetition) -> u32 {
    match repetition.op.kind {
        RepetitionKind::Range(
            RepetitionRange::Exactly(count)
            | RepetitionRange::AtLeast(count)
            | RepetitionRange::Bounded(_, count),
        ) => count,
        RepetitionKind::ZeroOrOne | RepetitionKind::ZeroOrMore | RepetitionKind::OneOrMore => 0,
    }
}

fn repetition_admitted(repetition: &Repetition, pattern: &str) -> bool {
    // A bounded range's lower count never exceeds its upper count.
    let counts_admitted = repetition_count(repetition) <= MAX_REPEAT;
    // Rust skips whitespace inside a counted repetition, where RE2 and Python read the braces literally, and RE2 reads
    // a count with a leading zero as literal text, where Rust and Python read its value.
    let operator = &pattern[repetition.op.span.start.offset..repetition.op.span.end.offset];
    let compact = !operator.chars().any(char::is_whitespace)
        && !operator
            .split(|character: char| !character.is_ascii_digit())
            .any(|count| count.len() > 1 && count.starts_with('0'));
    // Python refuses to repeat an assertion, a repetition, or nothing.
    let operand_admitted = matches!(
        *repetition.ast,
        Ast::Literal(_) | Ast::Dot(_) | Ast::ClassPerl(_) | Ast::ClassBracketed(_) | Ast::Group(_)
    );
    counts_admitted && compact && operand_admitted && ast_admitted(&repetition.ast, pattern)
}

/// RE2's repetition walk: each counted repetition divides the remaining budget by its count, and RE2 refuses the
/// pattern when any path reaches zero.
fn repetition_budget(node: &Ast, budget: u32) -> u32 {
    match node {
        Ast::Repetition(repetition) => {
            let count = repetition_count(repetition);
            let budget = budget.checked_div(count).unwrap_or(budget);
            repetition_budget(&repetition.ast, budget)
        }
        Ast::Group(group) => repetition_budget(&group.ast, budget),
        Ast::Alternation(alternation) => alternation
            .asts
            .iter()
            .map(|child| repetition_budget(child, budget))
            .fold(budget, u32::min),
        Ast::Concat(concat) => concat
            .asts
            .iter()
            .map(|child| repetition_budget(child, budget))
            .fold(budget, u32::min),
        _ => budget,
    }
}

/// An upper bound of the RE2 instructions one node compiles to under any combination of the `i`, `m`, and `s` flags.
/// A literal costs at most 8 even when case folding adds its variants, `.` at most 16, and a Perl class at most 32 once
/// folding reaches `K` (U+212A) and `ſ` (U+017F). A bracketed class of ASCII members without a negated Perl class costs
/// at most 32 plus 4 per member; any other class at most 128 plus 32 per member. A counted repetition copies its
/// operand once per possible occurrence.
fn program_bound(node: &Ast) -> u64 {
    match node {
        Ast::Empty(_) => 1,
        Ast::Flags(_) => 0,
        Ast::Literal(_) => 8,
        Ast::Dot(_) => 16,
        Ast::Assertion(_) => 2,
        Ast::ClassPerl(_) | Ast::ClassUnicode(_) => 32,
        Ast::ClassBracketed(class) => class_items(class).map_or(u64::MAX, class_bound),
        Ast::Repetition(repetition) => {
            let operand = program_bound(&repetition.ast);
            match repetition.op.kind {
                RepetitionKind::Range(_) => u64::from(repetition_count(repetition).max(1))
                    .saturating_mul(operand.saturating_add(1))
                    .saturating_add(1),
                RepetitionKind::ZeroOrOne
                | RepetitionKind::ZeroOrMore
                | RepetitionKind::OneOrMore => operand.saturating_add(2),
            }
        }
        Ast::Group(group) => program_bound(&group.ast).saturating_add(2),
        Ast::Alternation(alternation) => alternation
            .asts
            .iter()
            .fold(alternation.asts.len() as u64, |total, child| {
                total.saturating_add(program_bound(child))
            }),
        Ast::Concat(concat) => concat
            .asts
            .iter()
            .fold(0, |total, child| total.saturating_add(program_bound(child))),
    }
}

fn class_bound(items: &[ClassSetItem]) -> u64 {
    let ascii = items.iter().all(|item| match item {
        ClassSetItem::Literal(literal) => literal.c.is_ascii(),
        ClassSetItem::Range(range) => range.end.c.is_ascii(),
        ClassSetItem::Perl(perl) => !perl.negated,
        _ => false,
    });
    let members = items.len() as u64;
    if ascii {
        32 + 4 * members
    } else {
        128_u64.saturating_add(members.saturating_mul(32))
    }
}

/// The folding work of every bracketed class in the tree, once each however often a repetition copies it.
fn fold_work(node: &Ast) -> u64 {
    match node {
        Ast::ClassBracketed(class) => class_items(class).map_or(u64::MAX, |items| {
            items
                .iter()
                .fold(0, |total, item| total.saturating_add(item_folds(item)))
        }),
        Ast::Repetition(repetition) => fold_work(&repetition.ast),
        Ast::Group(group) => fold_work(&group.ast),
        Ast::Alternation(alternation) => alternation
            .asts
            .iter()
            .fold(0, |total, child| total.saturating_add(fold_work(child))),
        Ast::Concat(concat) => concat
            .asts
            .iter()
            .fold(0, |total, child| total.saturating_add(fold_work(child))),
        _ => 0,
    }
}

/// The folding work of one class member: the code points it covers, weighted by `FOLD_WEIGHT` or `SCAN_WEIGHT`.
fn item_folds(item: &ClassSetItem) -> u64 {
    let (low, high) = match item {
        ClassSetItem::Literal(literal) => (u32::from(literal.c), u32::from(literal.c)),
        ClassSetItem::Range(range) => (u32::from(range.start.c), u32::from(range.end.c)),
        ClassSetItem::Perl(perl) => (
            0,
            if perl.negated {
                u32::from(char::MAX)
            } else {
                0x7F
            },
        ),
        _ => return u64::MAX,
    };
    let cased = u64::from((high.min(CASED_END - 1) + 1).saturating_sub(low));
    let covered = u64::from(high.saturating_sub(low)) + 1;
    FOLD_WEIGHT
        .saturating_mul(cased)
        .saturating_add(SCAN_WEIGHT.saturating_mul(covered.saturating_sub(cased)))
}

fn python_group_name(name: &str) -> bool {
    let mut characters = name.chars();
    characters
        .next()
        .is_some_and(|first| first == '_' || first.is_ascii_alphabetic())
        && characters.all(|character| character == '_' || character.is_ascii_alphanumeric())
}

#[cfg(test)]
mod tests;
