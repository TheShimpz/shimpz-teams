//! The schedule, output, and timezone a person states in their own words, read with no model (`phrase.py`,
//! ADR-0101).
//!
//! Each reader reproduces the Python reader over the same tables ([`tables`], in Python `re` syntax through the
//! crate's pattern translator): the text's digits of three scripts become ASCII, the text is case-folded and split into
//! sentences after each sentence mark (a period after a digit is an ordinal, never an end), a sentence that asks states
//! nothing, and a negation rejects every reading it reaches. A captured count, hour, or day reads its Unicode decimal
//! digits of any script as Python's `int()` does. A zone is stated only as an exact member of the pinned zone names
//! ([`ZONE_NAMES`]). Spans are byte offsets, only ever compared within the one string they index. A search that cannot
//! complete refuses the whole reading ([`PhraseError::Match`]); it is never read as "no match".

mod generated;
mod schedule;
pub mod tables;

use std::sync::LazyLock;

pub use self::generated::{DECIMAL_ZEROS, TZDATA_VERSION, ZONE_NAMES};
pub use self::schedule::{COUNTED_UNITS, MAX_COUNT_DIGITS, UNIT_SECONDS, decimal};
use self::tables::{
    ALTERNATIVE, ASK_PHRASES, BARE_ASKS, DAILY, INTERVALS, MONTHLY, NEGATIONS, OUTPUTS,
    ROUTINE_NOUNS, TIMES, TRAILING_NEGATION, WEEKLY, ZONE,
};
use crate::python::regex::{BACKTRACK_LIMIT, Match, MatchError, Pattern};
use crate::python::text;
use strict_json::Value;

/// The marks that make a sentence a question.
pub const ASKS: [char; 3] = ['?', '\u{ff1f}', '\u{61f}'];
/// What a bare question may carry around it.
pub const BARE_PUNCTUATION: &str = " \u{bf}\u{a1}?\u{ff1f}\u{61f}.!\u{3002}";
/// The marks a sentence ends at, kept with it; a period ends one only after anything but a decimal digit.
pub const SENTENCE_ENDS: &str = "!?;\n\u{3002}\u{ff01}\u{ff1f}\u{61f}";
/// The digits `_DIGITS` makes ASCII: Arabic-Indic, Extended Arabic-Indic, and fullwidth, and the fullwidth colon.
#[rustfmt::skip]
pub const DIGITS: [(char, char); 31] = [
    ('\u{660}', '0'), ('\u{661}', '1'), ('\u{662}', '2'), ('\u{663}', '3'), ('\u{664}', '4'),
    ('\u{665}', '5'), ('\u{666}', '6'), ('\u{667}', '7'), ('\u{668}', '8'), ('\u{669}', '9'),
    ('\u{6f0}', '0'), ('\u{6f1}', '1'), ('\u{6f2}', '2'), ('\u{6f3}', '3'), ('\u{6f4}', '4'),
    ('\u{6f5}', '5'), ('\u{6f6}', '6'), ('\u{6f7}', '7'), ('\u{6f8}', '8'), ('\u{6f9}', '9'),
    ('\u{ff10}', '0'), ('\u{ff11}', '1'), ('\u{ff12}', '2'), ('\u{ff13}', '3'), ('\u{ff14}', '4'),
    ('\u{ff15}', '5'), ('\u{ff16}', '6'), ('\u{ff17}', '7'), ('\u{ff18}', '8'), ('\u{ff19}', '9'),
    ('\u{ff1a}', ':'),
];

/// Why a reading was refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PhraseError {
    /// A search could not complete.
    Match(MatchError),
    /// A reading met a negation table for a language this module has none for (Python raises `KeyError`).
    Language,
}

impl From<MatchError> for PhraseError {
    fn from(error: MatchError) -> Self {
        Self::Match(error)
    }
}

/// One output reading: its kind and its span.
type OutputReading = (&'static str, (usize, usize));

/// One table of patterns, each with the language that reads it.
type Table = Vec<(&'static str, Pattern)>;

/// One schedule reading pattern.
///
/// A Python pattern `P.*?S` (a monthly reading: its period phrase, anything, then its day) is searched as `P`, then
/// the leftmost `S` at or after `P`'s end on the same line, which is the match Python's lazy `.*?` finds: a sentence
/// holds a line break only at its end, `P` names no group, and no two alternatives of `P` match at one start. Searched
/// whole, every failed start would scan the rest of the sentence, so a long sentence repeating `P` without `S` would
/// spend time quadratic in its length, and exhaust the backtracking limit where Python only slows.
enum Reading {
    /// A pattern searched as written.
    Whole(Pattern),
    /// `P.*?S` as its two parts.
    Lazy { prefix: Pattern, suffix: Pattern },
}

impl Reading {
    fn compile(python: &str, limit: usize) -> Self {
        match python.split_once(".*?") {
            Some((prefix, suffix)) => Self::Lazy {
                prefix: limited(prefix, limit),
                suffix: limited(suffix, limit),
            },
            None => Self::Whole(limited(python, limit)),
        }
    }

    fn finditer(&self, sentence: &str) -> Result<Vec<Match>, MatchError> {
        match self {
            Self::Whole(pattern) => pattern.finditer(sentence),
            Self::Lazy { prefix, suffix } => lazy_finditer(prefix, suffix, sentence),
        }
    }
}

/// `finditer` of `prefix.*?suffix` (see [`Reading`]): each prefix match with the leftmost suffix match after it on
/// its line, whose groups are the match's groups.
fn lazy_finditer(
    prefix: &Pattern,
    suffix: &Pattern,
    sentence: &str,
) -> Result<Vec<Match>, MatchError> {
    let mut found = Vec::new();
    let mut position = 0;
    // No suffix match starts at or after this offset, so no later prefix can complete one.
    let mut no_suffix_from = usize::MAX;
    while let Some(head) = prefix.search_from(sentence, position)? {
        let tail = if head.end >= no_suffix_from {
            None
        } else {
            suffix.search_from(sentence, head.end)?
        };
        match tail {
            Some(tail) if !sentence[head.end..tail.start].contains('\n') => {
                position = tail.end;
                found.push(Match {
                    start: head.start,
                    end: tail.end,
                    groups: tail.groups,
                });
            }
            other => {
                if other.is_none() {
                    no_suffix_from = no_suffix_from.min(head.end);
                }
                position = head.start
                    + sentence[head.start..]
                        .chars()
                        .next()
                        .map_or(1, char::len_utf8);
            }
        }
    }
    Ok(found)
}

/// One reading table: each pattern with the language that reads it.
type ReadingTable = Vec<(&'static str, Reading)>;

fn reading_table(rows: &[(&'static str, &'static str)], limit: usize) -> ReadingTable {
    rows.iter()
        .map(|(language, pattern)| (*language, Reading::compile(pattern, limit)))
        .collect()
}

/// A table pattern compiled with a backtracking `limit`; the tests compile every table.
fn limited(python: &str, limit: usize) -> Pattern {
    Pattern::with_limit(python, limit).expect("a table pattern translates")
}

/// Every table, compiled once.
struct Compiled {
    negations: Table,
    /// The interval, monthly, weekly, and daily tables, in the order Python reads them.
    readings: [ReadingTable; 4],
    times: Vec<Pattern>,
    outputs: Vec<(&'static str, &'static str, Pattern)>,
    zone: Pattern,
    alternative: Pattern,
    nouns: Table,
    asks: Vec<Pattern>,
    bare: Vec<Pattern>,
}

fn table(rows: &[(&'static str, &'static str)], limit: usize) -> Table {
    rows.iter()
        .map(|(language, pattern)| (*language, limited(pattern, limit)))
        .collect()
}

impl Compiled {
    /// Every table, each pattern with this backtracking `limit`.
    fn new(limit: usize) -> Self {
        let each = |patterns: &[&str]| {
            patterns
                .iter()
                .map(|pattern| limited(pattern, limit))
                .collect()
        };
        Self {
            negations: table(&NEGATIONS, limit),
            readings: [
                reading_table(&INTERVALS, limit),
                reading_table(&MONTHLY, limit),
                reading_table(&WEEKLY, limit),
                reading_table(&DAILY, limit),
            ],
            times: each(&TIMES),
            outputs: OUTPUTS
                .iter()
                .map(|(kind, language, pattern)| (*kind, *language, limited(pattern, limit)))
                .collect(),
            zone: limited(ZONE, limit),
            alternative: limited(ALTERNATIVE, limit),
            nouns: table(&ROUTINE_NOUNS, limit),
            asks: each(&ASK_PHRASES),
            bare: each(&BARE_ASKS),
        }
    }
}

static COMPILED: LazyLock<Compiled> = LazyLock::new(|| Compiled::new(BACKTRACK_LIMIT));

/// `text.translate(_DIGITS)`.
#[must_use]
pub fn translate_digits(text: &str) -> String {
    text.chars()
        .map(|character| {
            DIGITS
                .iter()
                .find(|(from, _)| *from == character)
                .map_or(character, |(_, to)| *to)
        })
        .collect()
}

/// `_sentences`: each sentence of the folded text a person affirmed, never one that asks.
fn sentences(text: &str) -> Vec<String> {
    let folded = text::casefold(&translate_digits(text));
    let characters: Vec<char> = folded.chars().collect();
    let mut found = Vec::new();
    let mut current = String::new();
    for (index, character) in characters.iter().enumerate() {
        current.push(*character);
        let after_digit = index > 0 && text::category(characters[index - 1]) == "Nd";
        if SENTENCE_ENDS.contains(*character) || (*character == '.' && !after_digit) {
            found.push(std::mem::take(&mut current));
        }
    }
    found.push(current);
    found
        .into_iter()
        .filter(|sentence| {
            !text::strip(sentence).is_empty() && !text::rstrip(sentence).ends_with(ASKS)
        })
        .collect()
}

/// The negations of one sentence, searched once per language however many readings they are judged against.
struct Negations<'a> {
    compiled: &'a Compiled,
    sentence: &'a str,
    /// Each searched language with the start of every negation it found, in order.
    found: Vec<(String, Vec<usize>)>,
}

impl<'a> Negations<'a> {
    const fn new(compiled: &'a Compiled, sentence: &'a str) -> Self {
        Self {
            compiled,
            sentence,
            found: Vec::new(),
        }
    }

    /// `_negated`: whether a negation of `language` reaches a reading starting at `start`, before it or, where the
    /// negation trails, anywhere in the sentence.
    fn reach(&mut self, language: &str, start: usize) -> Result<bool, PhraseError> {
        if !self.found.iter().any(|(known, _)| known == language) {
            let pattern = self
                .compiled
                .negations
                .iter()
                .find(|(known, _)| *known == language)
                .map(|(_, pattern)| pattern)
                .ok_or(PhraseError::Language)?;
            let starts = negation_starts(pattern, self.sentence)?;
            self.found.push((language.to_owned(), starts));
        }
        let starts = self
            .found
            .iter()
            .find(|(known, _)| known == language)
            .map_or(&[][..], |(_, starts)| starts.as_slice());
        Ok(if TRAILING_NEGATION.contains(&language) {
            !starts.is_empty()
        } else {
            starts.first().is_some_and(|first| *first < start)
        })
    }
}

/// Where each match of one negation `pattern` starts in `sentence`.
fn negation_starts(pattern: &Pattern, sentence: &str) -> Result<Vec<usize>, PhraseError> {
    Ok(pattern
        .finditer(sentence)?
        .iter()
        .map(|negation| negation.start)
        .collect())
}

/// Whether a match overlaps any of these spans.
fn overlaps(found: &Match, spans: &[(usize, usize)]) -> bool {
    spans
        .iter()
        .any(|(start, end)| found.start.max(*start) < found.end.min(*end))
}

/// `_readings`: each kind's readings of one sentence that no negation reaches, in any language, or only in
/// `language`'s when the text is known to be written in it.
fn readings(
    compiled: &Compiled,
    sentence: &str,
    language: Option<&str>,
) -> Result<[Vec<Match>; 4], PhraseError> {
    let mut found: Vec<(usize, &str, Match)> = Vec::new();
    for (kind, table) in compiled.readings.iter().enumerate() {
        for (read, pattern) in table {
            found.extend(
                pattern
                    .finditer(sentence)?
                    .into_iter()
                    .map(|reading| (kind, *read, reading)),
            );
        }
    }
    let mut negations = Negations::new(compiled, sentence);
    let mut negated_spans = Vec::new();
    for (_, read, reading) in &found {
        let judge = language.filter(|known| !known.is_empty()).unwrap_or(read);
        if negations.reach(judge, reading.start)? {
            negated_spans.push((reading.start, reading.end));
        }
    }
    let mut admitted: [Vec<Match>; 4] = Default::default();
    for (kind, _, reading) in found {
        if !overlaps(&reading, &negated_spans) {
            admitted[kind].push(reading);
        }
    }
    Ok(admitted)
}

/// `stated`: every distinct canonical schedule one text states affirmatively, in the order first found.
///
/// A person's own text is read in every language (`language` `None` or empty); a text known to be written in one
/// interface language names it, and only its own negations reject a reading.
///
/// # Errors
///
/// [`PhraseError`] when a search cannot complete, or when a reading meets a `language` without a negation table.
pub fn stated(text: &str, language: Option<&str>) -> Result<Vec<Value>, PhraseError> {
    stated_in(&COMPILED, text, language)
}

/// [`stated`] over these tables.
fn stated_in(
    compiled: &Compiled,
    text: &str,
    language: Option<&str>,
) -> Result<Vec<Value>, PhraseError> {
    let mut found: Vec<Value> = Vec::new();
    for sentence in sentences(text) {
        let readings = readings(compiled, &sentence, language)?;
        let mut schedules: Vec<Value> = readings[0]
            .iter()
            .filter_map(|reading| {
                schedule::interval(
                    reading.group(&sentence, 1),
                    reading.group(&sentence, 2).unwrap_or_default(),
                )
            })
            .collect();
        schedules.extend(schedule::calendar(&sentence, &readings, &compiled.times)?);
        // Every schedule built here is exact, which Python's `exact_schedule` filter confirms; it drops only the
        // `None` of an interval outside its gap, which `interval` never returns here.
        for value in schedules {
            if !found.contains(&value) {
                found.push(value);
            }
        }
    }
    Ok(found)
}

/// `_one_clause`: whether two readings sit in one clause, nothing between them separating alternatives.
fn one_clause(
    alternative: &Pattern,
    sentence: &str,
    first: (usize, usize),
    second: (usize, usize),
) -> Result<bool, PhraseError> {
    let (start, end) = (first.1.min(second.1), first.0.max(second.0));
    // Python slices an empty string where the range runs backwards.
    let between = sentence.get(start..end).unwrap_or_default();
    Ok(alternative.search(between)?.is_none())
}

/// The output readings of one sentence: each kind with its span, a "nothing" reading always, any other only when
/// neither a "nothing" reading nor a negation of its own language reaches it.
fn output_readings(compiled: &Compiled, sentence: &str) -> Result<Vec<OutputReading>, PhraseError> {
    let mut found = Vec::new();
    for (kind, language, pattern) in &compiled.outputs {
        found.extend(
            pattern
                .finditer(sentence)?
                .into_iter()
                .map(|reading| (*kind, *language, reading)),
        );
    }
    let mut blocked: Vec<(usize, usize)> = found
        .iter()
        .filter(|(kind, _, _)| *kind == "none")
        .map(|(_, _, reading)| (reading.start, reading.end))
        .collect();
    let mut negations = Negations::new(compiled, sentence);
    for (kind, language, reading) in &found {
        if *kind != "none" && negations.reach(language, reading.start)? {
            blocked.push((reading.start, reading.end));
        }
    }
    Ok(found
        .into_iter()
        .filter(|(kind, _, reading)| *kind == "none" || !overlaps(reading, &blocked))
        .map(|(kind, _, reading)| (kind, (reading.start, reading.end)))
        .collect())
}

/// `outputs`: every distinct output choice one person-authored text states affirmatively, in the order first found.
///
/// # Errors
///
/// [`PhraseError::Match`] when a search cannot complete.
pub fn outputs(text: &str) -> Result<Vec<&'static str>, PhraseError> {
    outputs_in(&COMPILED, text)
}

/// [`outputs`] over these tables.
fn outputs_in(compiled: &Compiled, text: &str) -> Result<Vec<&'static str>, PhraseError> {
    let mut found: Vec<&'static str> = Vec::new();
    let (mut shown, mut conditioned) = (false, false);
    for sentence in sentences(text) {
        let admitted = output_readings(compiled, &sentence)?;
        let changes: Vec<(usize, usize)> = admitted
            .iter()
            .filter(|(kind, _)| *kind == "changes")
            .map(|(_, span)| *span)
            .collect();
        conditioned = conditioned || admitted.iter().any(|(kind, _)| *kind == "condition");
        for (kind, span) in admitted {
            if kind == "condition"
                || (kind == "result" && qualified(compiled, &sentence, span, &changes)?)
            {
                continue;
            }
            let chosen = if matches!(kind, "show" | "result") {
                "show"
            } else {
                kind
            };
            shown = shown || chosen == "show";
            if !found.contains(&chosen) {
                found.push(chosen);
            }
        }
    }
    if shown && conditioned && !found.contains(&"changes") {
        found.push("changes");
    }
    Ok(found)
}

/// Whether a "result" reading shares its clause with a "changes" reading, which then qualifies it.
fn qualified(
    compiled: &Compiled,
    sentence: &str,
    span: (usize, usize),
    changes: &[(usize, usize)],
) -> Result<bool, PhraseError> {
    for other in changes {
        if one_clause(&compiled.alternative, sentence, span, *other)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `requests_routine`: whether one person-authored text names a Routine affirmatively.
///
/// # Errors
///
/// [`PhraseError::Match`] when a search cannot complete.
pub fn requests_routine(text: &str) -> Result<bool, PhraseError> {
    requests_routine_in(&COMPILED, text)
}

/// [`requests_routine`] over these tables.
fn requests_routine_in(compiled: &Compiled, text: &str) -> Result<bool, PhraseError> {
    for sentence in sentences(text) {
        let mut found = Vec::new();
        for (language, pattern) in &compiled.nouns {
            found.extend(
                pattern
                    .finditer(&sentence)?
                    .into_iter()
                    .map(|noun| (*language, noun)),
            );
        }
        let mut negations = Negations::new(compiled, &sentence);
        let mut negated_spans = Vec::new();
        for (language, noun) in &found {
            if negations.reach(language, noun.start)? {
                negated_spans.push((noun.start, noun.end));
            }
        }
        if found
            .iter()
            .any(|(_, noun)| !overlaps(noun, &negated_spans))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `team_asks`: whether a text asks or states what Team reads from the person's words, so the Brain never asks it.
///
/// # Errors
///
/// [`PhraseError`] when a search cannot complete, or when a reading meets a `language` without a negation table.
pub fn team_asks(text: &str, language: Option<&str>) -> Result<bool, PhraseError> {
    team_asks_in(&COMPILED, text, language)
}

/// [`team_asks`] over these tables.
fn team_asks_in(
    compiled: &Compiled,
    text: &str,
    language: Option<&str>,
) -> Result<bool, PhraseError> {
    let plain: String = text
        .chars()
        .map(|character| {
            if ASKS.contains(&character) {
                '.'
            } else {
                character
            }
        })
        .collect();
    if !stated_in(compiled, &plain, language)?.is_empty()
        || !outputs_in(compiled, &plain)?.is_empty()
    {
        return Ok(true);
    }
    let folded = text::casefold(&translate_digits(text));
    let folded = text::strip_chars(&folded, BARE_PUNCTUATION);
    for pattern in &compiled.asks {
        if pattern.search(folded)?.is_some() {
            return Ok(true);
        }
    }
    for pattern in &compiled.bare {
        if pattern.fullmatch(folded)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `_loads`: whether a name is a zone of the pinned zone database, every one of which `canonical_timezone` admits.
#[must_use]
pub fn loads(name: &str) -> bool {
    ZONE_NAMES.binary_search(&name).is_ok()
}

/// `zones`: every distinct zone (or `UTC`) a person wrote exactly, that loads, in the order written.
///
/// # Errors
///
/// [`PhraseError::Match`] when a search cannot complete.
pub fn zones(text: &str) -> Result<Vec<String>, PhraseError> {
    zones_by(&COMPILED.zone, text)
}

/// [`zones`] over one zone token `pattern`.
fn zones_by(pattern: &Pattern, text: &str) -> Result<Vec<String>, PhraseError> {
    let mut found: Vec<String> = Vec::new();
    for zone in pattern.finditer(text)? {
        let name = zone.group(text, 1).unwrap_or_default();
        if loads(name) && !found.iter().any(|known| known == name) {
            found.push(name.to_owned());
        }
    }
    Ok(found)
}

#[cfg(test)]
mod tests;
