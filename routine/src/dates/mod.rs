//! Dates written inside a recorded text input, which each run writes again for its own date (ADR-0101).
//!
//! A recorded search such as "AI news 15 October 2026" must search the date of each run, not the recording's. Such a
//! value is kept as a template: its literal text and, in place of each date, a part holding a whole-day offset from the
//! run date and a closed format. A part's format is literal characters and the tokens `{d}` `{dd}` `{M}` `{MM}`
//! `{yyyy}`, or a month name `{mmmm}` (lowercase), `{Mmmm}` (capitalized), or `{MMMM}` (uppercase) in one interface
//! language.
//!
//! Recognition reads only whole dates in a closed set of forms: ISO `2026-10-15`; numeric `15/10/2026`, `15.10.2026`
//! and `15/10`, day first or month first; `2026年10月15日` and `10月15日`; and a day with a month name, day first
//! (`15 de outubro de 2026`, `15. Oktober`) or month first (`October 15, 2026`). A date counts only within
//! [`MAX_OFFSET_DAYS`] of the recording's date; a written form that reads as two dates, two orders, or two languages
//! there is decided by the person's interface language, else the text stays a literal. A bare day joined to a date by a
//! range word ("15 a 21/10/2026") is never guessed, so such a text stays a literal too. The template must write the
//! recorded text again byte for byte on the recording's date, or it is no template.

pub mod date;

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::sync::LazyLock;

use jiff::tz::TimeZone;
use strict_json::Value;
use team_protocol::python::regex::{Match, MatchError, Pattern, declared};
use team_protocol::python::text;
use team_protocol::routine_context::{MAX_DATE_OFFSET_DAYS, UNSPACED_DATE_FORMS};

use crate::dates::date::Date;
use crate::json::{as_str, at, elements, has_fields, int, object, text as string};
use crate::pattern::Folded;

/// How far from the recording's date a written date still counts, in days.
pub const MAX_OFFSET_DAYS: i32 = MAX_DATE_OFFSET_DAYS;
/// The parts of one template at most.
pub const MAX_PARTS: usize = 32;
/// The characters of one part's format at most.
pub const MAX_FORMAT_CHARS: usize = 64;
/// The languages whose month names a template writes.
pub const LANGUAGES: [&str; 6] = ["pt", "en", "es", "fr", "de", "ar"];
/// Each language's month names, January first.
pub const MONTHS: [(&str, [&str; 12]); 6] = [
    (
        "pt",
        [
            "janeiro",
            "fevereiro",
            "março",
            "abril",
            "maio",
            "junho",
            "julho",
            "agosto",
            "setembro",
            "outubro",
            "novembro",
            "dezembro",
        ],
    ),
    (
        "en",
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
    ),
    (
        "es",
        [
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ],
    ),
    (
        "fr",
        [
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        ],
    ),
    (
        "de",
        [
            "januar",
            "februar",
            "märz",
            "april",
            "mai",
            "juni",
            "juli",
            "august",
            "september",
            "oktober",
            "november",
            "dezember",
        ],
    ),
    (
        "ar",
        [
            "يناير",
            "فبراير",
            "مارس",
            "أبريل",
            "مايو",
            "يونيو",
            "يوليو",
            "أغسطس",
            "سبتمبر",
            "أكتوبر",
            "نوفمبر",
            "ديسمبر",
        ],
    ),
];
const TOKENS: [&str; 8] = ["d", "dd", "M", "MM", "yyyy", "mmmm", "Mmmm", "MMMM"];
const NAMED: [&str; 3] = ["mmmm", "Mmmm", "MMMM"];
/// Numeric orders: month first is the order only English writes.
const MONTH_FIRST: [&str; 1] = ["en"];
/// How a person reads each token: the date's own fields in capitals, a month name as the word "month" in its case.
const SHOWN_TOKENS: [(&str, &str); 8] = [
    ("d", "D"),
    ("dd", "DD"),
    ("M", "M"),
    ("MM", "MM"),
    ("yyyy", "YYYY"),
    ("mmmm", "month"),
    ("Mmmm", "Month"),
    ("MMMM", "MONTH"),
];

/// One way a form is read: a pattern without flags, or a case-insensitive one.
enum Form {
    Plain(Pattern),
    Folded(Folded),
}

impl Form {
    fn finditer(&self, text: &str) -> Result<Vec<Match>, MatchError> {
        match self {
            Self::Plain(pattern) => pattern.finditer(text),
            Self::Folded(pattern) => pattern.finditer(text),
        }
    }
}

/// Every month name, longest first, as one Python alternation.
fn name_alternation() -> String {
    let mut names: Vec<&str> = MONTHS
        .iter()
        .flat_map(|(_, names)| names.iter().copied())
        .collect();
    names.sort_unstable();
    names.dedup();
    // `sorted(..., key=len, reverse=True)` over a set: the order among equal lengths never changes what matches,
    // since no two names of one length overlap at one position with different lengths.
    names.sort_by_key(|name| std::cmp::Reverse(text::len(name)));
    names.join("|")
}

static FORMS: LazyLock<Vec<(&'static str, Form)>> = LazyLock::new(|| {
    let mut forms: Vec<(&'static str, Form)> = UNSPACED_DATE_FORMS
        .iter()
        .map(|(kind, pattern)| (*kind, Form::Plain(declared(pattern))))
        .collect();
    let names = name_alternation();
    forms.push((
        "day_name",
        Form::Folded(Folded::declared(&format!(
            r"(?<![\w.])(\d{{1,2}})(\.?\s+(?:de\s+)?)({names})(?:(,?\s+(?:de\s+)?)(\d{{4}}))?(?!\w)"
        ))),
    ));
    forms.push((
        "name_day",
        Form::Folded(Folded::declared(&format!(
            r"(?<!\w)({names})(\s+)(\d{{1,2}})(?:(,?\s+)(\d{{4}}))?(?!\w)"
        ))),
    ));
    forms
});
/// A bare day joined to a date by a range word: the day is never guessed.
static RANGE_BEFORE: LazyLock<Folded> = LazyLock::new(|| {
    Folded::declared(r"(?<![\w/.])\d{1,2}\s*(?:-|–|—|a|até|to|al|au|bis|~|〜|至)\s*\Z")
});
static RANGE_AFTER: LazyLock<Folded> =
    LazyLock::new(|| Folded::declared(r"\A\s*(?:-|–|—|a|até|to|al|au|bis|~|〜|至)\s*\d"));

/// One reading of a written date: its template part, its date, and the language whose order or name writes it.
type Reading = (Value, Date, Option<&'static str>);

fn names_of(language: &str) -> Option<&'static [&'static str; 12]> {
    MONTHS
        .iter()
        .find(|(name, _)| *name == language)
        .map(|(_, names)| names)
}

fn capitalized(name: &str) -> String {
    let mut characters = name.chars();
    characters.next().map_or_else(String::new, |first| {
        first.to_uppercase().chain(characters).collect()
    })
}

fn cased(case: &str, name: &str) -> String {
    match case {
        "mmmm" => text::lower(name),
        "Mmmm" => capitalized(name),
        _ => name.to_uppercase(),
    }
}

/// Each `{token}` of a format, with what lies between, in order: `(literal, None)` or `("", Some(token))`.
fn tokenized(format: &str) -> Vec<(&str, Option<&'static str>)> {
    let mut pieces = Vec::new();
    let mut copied = 0;
    let mut position = 0;
    while let Some(offset) = format[position..].find('{') {
        let open = position + offset;
        let token = format[open + 1..]
            .find('}')
            .map(|close| &format[open + 1..open + 1 + close])
            .and_then(|name| TOKENS.iter().find(|token| **token == name).copied());
        if let Some(token) = token {
            if open > copied {
                pieces.push((&format[copied..open], None));
            }
            pieces.push(("", Some(token)));
            position = open + token.len() + 2;
            copied = position;
        } else {
            position = open + 1;
        }
    }
    if copied < format.len() {
        pieces.push((&format[copied..], None));
    }
    pieces
}

/// The text a template writes for a run on `run_date`: each date part at its offset from that date.
///
/// `None` only when an offset leaves the calendar, which an admitted template near any real date never does.
#[must_use]
pub fn render(parts: &[Value], run_date: Date) -> Option<String> {
    let mut written = String::new();
    for part in parts {
        match part {
            Value::String(literal) => written.push_str(literal),
            _ => written.push_str(&written_part(part, run_date)?),
        }
    }
    Some(written)
}

fn written_part(part: &Value, run_date: Date) -> Option<String> {
    let days = team_protocol::python::value::small_int(at(part, "days"))?;
    let day = run_date.add_days(days)?;
    let format = as_str(at(part, "format"))?;
    let mut written = String::new();
    for (literal, token) in tokenized(format) {
        let Some(token) = token else {
            written.push_str(literal);
            continue;
        };
        if NAMED.contains(&token) {
            let names = names_of(as_str(at(part, "lang"))?)?;
            let index = usize::try_from(day.month - 1).ok()?;
            written.push_str(&cased(token, names[index]));
            continue;
        }
        let value = match token.as_bytes()[0] {
            b'd' => day.day,
            b'M' => day.month,
            _ => day.year,
        };
        if token == "dd" || token == "MM" {
            let _ = write!(written, "{value:02}");
        } else {
            written.push_str(&value.to_string());
        }
    }
    Some(written)
}

/// A plan's run-clock value at a run's start: its date in the plan's timezone, or a template written for it.
///
/// `None` when the instant has no date there or the template leaves the calendar.
#[must_use]
pub fn clock_value(source: &Value, instant: i64, zone: &TimeZone) -> Option<Value> {
    let day = Date::at(instant, zone)?;
    if as_str(at(source, "format")) == Some("date") {
        return Some(string(&day.isoformat()));
    }
    render(elements(at(source, "parts")), day).map(Value::String)
}

/// Whether a run-clock source is the run date alone or a text template with its parts.
#[must_use]
pub fn admitted_clock(source: &Value) -> bool {
    let format = at(source, "format");
    let parts = source.get("parts");
    if as_str(format) == Some("date") {
        return parts.is_none();
    }
    as_str(format) == Some("text") && parts.is_some_and(admitted)
}

/// Whether a template is in its closed shape: text and date parts, at least one date, no two texts adjacent.
#[must_use]
pub fn admitted(parts: &Value) -> bool {
    let Value::Array(parts) = parts else {
        return false;
    };
    if parts.is_empty() || parts.len() > MAX_PARTS {
        return false;
    }
    let mut previous_text = false;
    let mut dated = false;
    for part in parts {
        if let Value::String(literal) = part {
            if literal.is_empty() || previous_text {
                return false;
            }
            previous_text = true;
            continue;
        }
        if !date_part(part) {
            return false;
        }
        previous_text = false;
        dated = true;
    }
    dated
}

/// Whether a format is literal characters and tokens only, and the tokens it holds.
fn format_tokens(format: &str) -> Option<BTreeSet<&'static str>> {
    let mut tokens = BTreeSet::new();
    for (literal, token) in tokenized(format) {
        if literal.contains(['{', '}']) {
            return None;
        }
        if let Some(token) = token {
            tokens.insert(token);
        }
    }
    (!format.is_empty()).then_some(tokens)
}

fn date_part(part: &Value) -> bool {
    if !has_fields(part, &["days", "format", "lang"]) {
        return false;
    }
    let Some(days) = team_protocol::python::value::small_int(at(part, "days")) else {
        return false;
    };
    if !(-i64::from(MAX_OFFSET_DAYS)..=i64::from(MAX_OFFSET_DAYS)).contains(&days) {
        return false;
    }
    let Some(format) = as_str(at(part, "format")) else {
        return false;
    };
    if text::len(format) > MAX_FORMAT_CHARS {
        return false;
    }
    let Some(tokens) = format_tokens(format) else {
        return false;
    };
    if tokens.is_empty() {
        return false;
    }
    let language = at(part, "lang");
    if tokens.iter().any(|token| NAMED.contains(token)) {
        as_str(language).is_some_and(|language| LANGUAGES.contains(&language))
    } else {
        matches!(language, Value::Null)
    }
}

/// The template as a person reads it, the same whatever the date.
///
/// Its text has every literal brace doubled, and each date is in braces as its format ("D month YYYY"), followed
/// after `|` by its month-name language and its offset in days.
#[must_use]
pub fn shown(parts: &[Value]) -> String {
    let mut written = String::new();
    for part in parts {
        if let Value::String(literal) = part {
            written.push_str(&literal.replace('{', "{{").replace('}', "}}"));
            continue;
        }
        let format = as_str(at(part, "format")).unwrap_or_default();
        let mut form = String::new();
        for (literal, token) in tokenized(format) {
            match token {
                Some(token) => form.push_str(
                    SHOWN_TOKENS
                        .iter()
                        .find(|(name, _)| *name == token)
                        .map_or(token, |(_, shown)| shown),
                ),
                None => form.push_str(literal),
            }
        }
        let mut notes = vec![form];
        if let Some(language) = as_str(at(part, "lang")) {
            notes.push(language.to_owned());
        }
        let days = team_protocol::python::value::small_int(at(part, "days")).unwrap_or(0);
        if days != 0 {
            notes.push(format!("{days:+}"));
        }
        written.push('{');
        written.push_str(&notes.join("|"));
        written.push('}');
    }
    written
}

// --- Recognition -----------------------------------------------------------------------------------------------------

/// The template whose run on `recorded_on` writes `text` exactly, or `None` when the text holds no date to move.
///
/// `named` says whether the person's own words named a date, which then stays as written. Any date form the table
/// reads but cannot decide, or a bare day joined to a date by a range word, leaves the whole text a literal; so does a
/// search that cannot complete.
#[must_use]
pub fn template(
    text: &str,
    recorded_on: Date,
    locale: Option<&str>,
    named: impl Fn(Date) -> bool,
) -> Option<Vec<Value>> {
    let found = found(text, recorded_on, locale).ok()??;
    let mut parts: Vec<Value> = Vec::new();
    let mut cursor = 0;
    for (start, end, part, day) in found {
        if named(day) {
            continue;
        }
        if start > cursor {
            parts.push(string(&text[cursor..start]));
        }
        parts.push(part);
        cursor = end;
    }
    if cursor < text.len() {
        parts.push(string(&text[cursor..]));
    }
    let array = Value::Array(parts);
    if !elements(&array)
        .iter()
        .any(|part| matches!(part, Value::Object(_)))
        || !admitted(&array)
    {
        return None;
    }
    let Value::Array(parts) = array else {
        return None;
    };
    (render(&parts, recorded_on).as_deref() == Some(text)).then_some(parts)
}

/// Every date a text may write in a form the table reads near `recorded_on`, in any order or language.
///
/// It reads the person's own words, so it keeps every reading, an undecided one or one beside a range included: a
/// date it may name stays as written.
///
/// # Errors
///
/// [`MatchError`] when a search cannot complete.
pub fn dates(text: &str, recorded_on: Date) -> Result<BTreeSet<Date>, MatchError> {
    let mut found = BTreeSet::new();
    for (kind, form) in FORMS.iter() {
        for matched in form.finditer(text)? {
            for (_part, day, _writer) in readings(kind, &matched, text, recorded_on) {
                found.insert(day);
            }
        }
    }
    Ok(found)
}

/// One date a text writes: its byte span, its part, and its date.
type Found = (usize, usize, Value, Date);

/// Each date the text writes, or `None` when one is undecidable.
///
/// The longest written date wins wherever two would overlap, whatever its form, and the shorter one stays text.
fn found(
    text: &str,
    recorded_on: Date,
    locale: Option<&str>,
) -> Result<Option<Vec<Found>>, MatchError> {
    let mut matches: Vec<(&str, Match)> = Vec::new();
    for (kind, form) in FORMS.iter() {
        for matched in form.finditer(text)? {
            matches.push((kind, matched));
        }
    }
    // A stable sort, longest first then earliest, as Python's `sorted` orders them.
    matches.sort_by_key(|(_, matched)| {
        (
            matched.start.cast_signed() - matched.end.cast_signed(),
            matched.start,
        )
    });
    let mut spans: Vec<(usize, usize, (Value, Date))> = Vec::new();
    let mut taken: Vec<(usize, usize)> = Vec::new();
    for (kind, matched) in &matches {
        if taken
            .iter()
            .any(|(start, end)| matched.start < *end && *start < matched.end)
        {
            continue;
        }
        taken.push((matched.start, matched.end));
        let Some(mut decided) = decided(readings(kind, matched, text, recorded_on), locale) else {
            return Ok(None);
        };
        if let Some(reading) = decided.pop() {
            spans.push((matched.start, matched.end, reading));
        }
    }
    spans.sort_by_key(|(start, _, _)| *start);
    let mut found = Vec::new();
    for (position, (start, end, (part, day))) in spans.iter().enumerate() {
        // Only the text between this date and its neighbours counts: a range of two whole dates is two dates.
        let before_start = if position == 0 {
            0
        } else {
            spans[position - 1].1
        };
        let after_end = spans.get(position + 1).map_or(text.len(), |next| next.0);
        let before = &text[before_start..*start];
        let after = &text[*end..after_end];
        if RANGE_BEFORE.found(before)? || RANGE_AFTER.found(after)? {
            return Ok(None);
        }
        found.push((*start, *end, part.clone(), *day));
    }
    Ok(Some(found))
}

/// The one reading of a written date: alone, or the one the interface language writes; `None` when undecided.
fn decided(readings: Vec<Reading>, locale: Option<&str>) -> Option<Vec<(Value, Date)>> {
    // Keyed by (format, language), each key in its first place and holding its last reading, as a `dict` does.
    let mut distinct: Vec<((Value, Value), Reading)> = Vec::new();
    for reading in readings {
        let key = (
            at(&reading.0, "format").clone(),
            at(&reading.0, "lang").clone(),
        );
        if let Some(slot) = distinct.iter_mut().find(|(known, _)| *known == key) {
            slot.1 = reading;
        } else {
            distinct.push((key, reading));
        }
    }
    if distinct.len() > 1 {
        distinct.retain(|(_, (_, _, writer))| writes(*writer, locale));
        if distinct.len() != 1 {
            return None;
        }
    }
    Some(
        distinct
            .into_iter()
            .map(|(_, (part, day, _))| (part, day))
            .collect(),
    )
}

/// Whether the interface language writes a reading: a month name in its own language, numbers in its order.
fn writes(writer: Option<&str>, locale: Option<&str>) -> bool {
    match writer {
        Some("dmy") => locale.is_some_and(|locale| !MONTH_FIRST.contains(&locale)),
        Some("mdy") => locale.is_some_and(|locale| MONTH_FIRST.contains(&locale)),
        None => true,
        Some(writer) => locale == Some(writer),
    }
}

/// The value of a run of decimal digits of any script, as Python's `int()` reads it.
fn number(digits: &str) -> Option<i32> {
    let mut value: i32 = 0;
    for character in digits.chars() {
        value = value.checked_mul(10)?.checked_add(digit(character)?)?;
    }
    Some(value)
}

/// One decimal digit's value: its distance from the zero that starts its run of ten (Unicode encodes every decimal
/// digit in such runs).
fn digit(character: char) -> Option<i32> {
    if let Some(value) = character.to_digit(10) {
        return i32::try_from(value).ok();
    }
    if text::category(character) != "Nd" {
        return None;
    }
    let mut start = u32::from(character);
    while let Some(previous) = start.checked_sub(1).and_then(char::from_u32) {
        if text::category(previous) != "Nd" {
            break;
        }
        start -= 1;
    }
    i32::try_from((u32::from(character) - start) % 10).ok()
}

/// Two-digit tokens unless either number is written with one digit.
fn padding(day: &str, month: &str) -> (&'static str, &'static str) {
    if text::len(day) == 1 || text::len(month) == 1 {
        ("d", "M")
    } else {
        ("dd", "MM")
    }
}

/// One candidate reading before its date is placed: (format, year, month, day, language, writer).
type Candidate = (
    String,
    Option<i32>,
    i32,
    i32,
    Option<&'static str>,
    Option<&'static str>,
);

/// Every (part, date, writing language) a match may be within reach of the recording's date.
fn readings(kind: &str, matched: &Match, text: &str, recorded_on: Date) -> Vec<Reading> {
    let group = |index: usize| matched.group(text, index);
    let mut candidates: Vec<Candidate> = Vec::new();
    match kind {
        "iso" | "cjk" => fixed(kind, &group, &mut candidates),
        "numeric" | "short" => numeric(kind, &group, &mut candidates),
        _ => named(kind, &group, &mut candidates),
    }
    let mut readings = Vec::new();
    for (format, year, month, day, language, writer) in candidates {
        if let Some(written) = near(year, month, day, recorded_on) {
            let part = object(vec![
                ("days", int(i64::from(written.days_since(recorded_on)))),
                ("format", Value::String(format)),
                ("lang", language.map_or(Value::Null, string)),
            ]);
            readings.push((part, written, writer));
        }
    }
    readings
}

/// The one reading of an ISO or CJK date, whose order is fixed.
fn fixed<'t>(
    kind: &str,
    group: &dyn Fn(usize) -> Option<&'t str>,
    candidates: &mut Vec<Candidate>,
) {
    if kind == "iso" {
        if let (Some(year), Some(month), Some(day)) = (
            group(1).and_then(number),
            group(2).and_then(number),
            group(3).and_then(number),
        ) {
            candidates.push((
                "{yyyy}-{MM}-{dd}".to_owned(),
                Some(year),
                month,
                day,
                None,
                None,
            ));
        }
        return;
    }
    let (Some(month_text), Some(day_text)) = (group(2), group(3)) else {
        return;
    };
    let (day_style, month_style) = padding(day_text, month_text);
    let year = group(1).filter(|year| !year.is_empty());
    let head = if year.is_some() { "{yyyy}年" } else { "" };
    if let (Some(month), Some(day)) = (number(month_text), number(day_text)) {
        candidates.push((
            format!("{head}{{{month_style}}}月{{{day_style}}}日"),
            year.and_then(number),
            month,
            day,
            None,
            None,
        ));
    }
}

/// The two orders of a numeric date, day first and month first.
fn numeric<'t>(
    kind: &str,
    group: &dyn Fn(usize) -> Option<&'t str>,
    candidates: &mut Vec<Candidate>,
) {
    // The protocol's numeric form spells Python's repeated separator as one alternative per separator, so its second
    // number is group 2 after `/` and group 3 after `.`, and its year group 4.
    let (first, second, separator, year) = if kind == "numeric" {
        let (second, separator) = match (group(2), group(3)) {
            (Some(second), _) => (second, "/"),
            (None, Some(second)) => (second, "."),
            (None, None) => return,
        };
        (group(1), second, separator, group(4).and_then(number))
    } else {
        let Some(second) = group(2) else {
            return;
        };
        (group(1), second, "/", None)
    };
    let Some(first) = first else {
        return;
    };
    let (day_style, month_style) = padding(first, second);
    let tail = if year.is_some() {
        format!("{separator}{{yyyy}}")
    } else {
        String::new()
    };
    let (Some(first_number), Some(second_number)) = (number(first), number(second)) else {
        return;
    };
    candidates.push((
        format!("{{{day_style}}}{separator}{{{month_style}}}{tail}"),
        year,
        second_number,
        first_number,
        None,
        Some("dmy"),
    ));
    candidates.push((
        format!("{{{month_style}}}{separator}{{{day_style}}}{tail}"),
        year,
        first_number,
        second_number,
        None,
        Some("mdy"),
    ));
}

/// The readings of a day with a month name, in every language that names that month so.
fn named<'t>(
    kind: &str,
    group: &dyn Fn(usize) -> Option<&'t str>,
    candidates: &mut Vec<Candidate>,
) {
    let day_first = kind == "day_name";
    let [day_text, joiner, name, year_joiner, year_text] = if day_first {
        [1, 2, 3, 4, 5]
    } else {
        [3, 2, 1, 4, 5]
    }
    .map(group);
    let (Some(day_text), Some(joiner), Some(name)) = (day_text, joiner, name) else {
        return;
    };
    let lowered = text::lower(name);
    let Some(case) = NAMED
        .iter()
        .find(|case| cased(case, &lowered) == name)
        .copied()
    else {
        return;
    };
    let day_style = if day_text.starts_with('0') { "dd" } else { "d" };
    let (first, second) = if day_first {
        (day_style, case)
    } else {
        (case, day_style)
    };
    let year_text = year_text.filter(|year| !year.is_empty());
    let year_suffix = year_text.map_or_else(String::new, |_| {
        format!("{}{{yyyy}}", year_joiner.unwrap_or_default())
    });
    let form = format!("{{{first}}}{joiner}{{{second}}}{year_suffix}");
    let year = year_text.and_then(number);
    let Some(day) = number(day_text) else {
        return;
    };
    for (language, names) in &MONTHS {
        if let Some(index) = names.iter().position(|known| *known == lowered) {
            let month = i32::try_from(index).unwrap_or(0) + 1;
            candidates.push((
                form.clone(),
                year,
                month,
                day,
                Some(language),
                Some(language),
            ));
        }
    }
}

/// The date within reach of the recording's date: the written year's, else the nearest year's; `None` when none is.
fn near(year: Option<i32>, month: i32, day: i32, recorded_on: Date) -> Option<Date> {
    let years = year.map_or_else(
        || vec![recorded_on.year - 1, recorded_on.year, recorded_on.year + 1],
        |year| vec![year],
    );
    years.into_iter().find_map(|candidate| {
        Date::new(candidate, month, day)
            .filter(|written| written.days_since(recorded_on).abs() <= MAX_OFFSET_DAYS)
    })
}

#[cfg(test)]
mod tests;
