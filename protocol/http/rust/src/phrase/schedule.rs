//! The schedules a sentence's readings state: an interval from its count and unit, and a day, weekday, or day of the
//! month at each time of day the sentence names (`_interval`, `_time`, `_times`, `_calendar`).

use std::collections::BTreeSet;

use strict_json::Value;

use super::PhraseError;
use super::generated::DECIMAL_ZEROS;
use super::tables::{FIXED_UNITS, UNIT, WEEKDAYS};
use crate::python::regex::{Match, Pattern};
use crate::python::text;
use crate::python::value::{int, object_of};
use crate::routine::{MAX_CONTINUOUS_GAP_SECONDS, MIN_CONTINUOUS_GAP_SECONDS};

/// The longest count any schedule can hold: the longest gap in seconds has this many digits.
pub const MAX_COUNT_DIGITS: usize = 8;
/// The seconds of each interval unit.
pub const UNIT_SECONDS: [(&str, i64); 5] = [
    ("second", 1),
    ("minute", 60),
    ("hour", 3600),
    ("day", 86_400),
    ("week", 604_800),
];
/// A day or week without a count is a calendar phrase, which needs its time or weekday.
pub const COUNTED_UNITS: [&str; 2] = ["day", "week"];

/// Python's `int()` of a string of Unicode decimal digits of any script: each digit counts its place in its run of
/// ten. Every caller passes only what a `\d` matched.
#[must_use]
pub fn decimal(digits: &str) -> i64 {
    digits.chars().fold(0, |total, character| {
        let point = u32::from(character);
        let run = DECIMAL_ZEROS.partition_point(|zero| *zero <= point);
        let zero = DECIMAL_ZEROS[run.saturating_sub(1)];
        total * 10 + i64::from(point.saturating_sub(zero))
    })
}

fn lookup(table: &[(&'static str, &'static str)], key: &str) -> Option<&'static str> {
    table
        .iter()
        .find(|(name, _)| *name == key)
        .map(|(_, kind)| *kind)
}

/// `_interval`: a continuous schedule `count` units apart (one without a count), when its gap is admitted.
pub(super) fn interval(count: Option<&str>, unit: &str) -> Option<Value> {
    let count = count.filter(|count| !count.is_empty());
    if count.is_some_and(|count| text::len(count) > MAX_COUNT_DIGITS) {
        return None;
    }
    let every = count.map_or(1, decimal);
    // Every unit a table captures names one of these, so neither default is ever read.
    let kind = lookup(&FIXED_UNITS, unit)
        .or_else(|| lookup(&UNIT, unit))
        .unwrap_or("second");
    if COUNTED_UNITS.contains(&kind) && count.is_none() {
        return None;
    }
    let seconds = UNIT_SECONDS
        .iter()
        .find(|(name, _)| *name == kind)
        .map_or(1, |(_, seconds)| *seconds);
    let gap = every * seconds;
    (MIN_CONTINUOUS_GAP_SECONDS..=MAX_CONTINUOUS_GAP_SECONDS)
        .contains(&gap)
        .then(|| {
            object_of(vec![
                ("kind", Value::String("continuous".to_owned())),
                ("gap", int(gap)),
            ])
        })
}

/// `_time`: a time of day as `HH:MM`, from an hour, an optional minute, and an optional half of the day.
fn time(hour: &str, minute: Option<&str>, half: &str) -> Option<String> {
    let (written, minutes) = (decimal(hour), minute.map_or(0, decimal));
    let mut value = written;
    if half.starts_with('p') && value < 12 {
        value += 12;
    } else if half.starts_with('a') && value == 12 {
        value = 0;
    }
    if value > 23 || minutes > 59 || (!half.is_empty() && !(1..=12).contains(&written)) {
        return None;
    }
    Some(format!("{value:02}:{minutes:02}"))
}

/// The sentence with each match's text replaced by as many spaces as it has characters.
fn blanked(sentence: &str, found: &[Match]) -> String {
    let mut blanked = String::with_capacity(sentence.len());
    let mut copied = 0;
    for reading in found {
        blanked.push_str(&sentence[copied..reading.start]);
        blanked.push_str(&" ".repeat(text::len(&sentence[reading.start..reading.end])));
        copied = reading.end;
    }
    blanked.push_str(&sentence[copied..]);
    blanked
}

/// `_times`: each time of day a sentence names, sorted; a more specific form read first hides the shorter forms
/// inside it.
fn times(sentence: &str, patterns: &[Pattern]) -> Result<BTreeSet<String>, PhraseError> {
    let mut current = sentence.to_owned();
    let mut found = BTreeSet::new();
    for pattern in patterns {
        let readings = pattern.finditer(&current)?;
        found.extend(readings.iter().filter_map(|reading| {
            time(
                reading.group(&current, 1).unwrap_or_default(),
                reading
                    .group(&current, 2)
                    .filter(|minute| !minute.is_empty()),
                reading.group(&current, 3).unwrap_or_default(),
            )
        }));
        current = blanked(&current, &readings);
    }
    Ok(found)
}

/// `_calendar`: the day, weekday, or monthly schedules one sentence states, one for each time it names.
pub(super) fn calendar(
    sentence: &str,
    readings: &[Vec<Match>; 4],
    patterns: &[Pattern],
) -> Result<Vec<Value>, PhraseError> {
    let times = times(sentence, patterns)?;
    let mut days: Vec<Vec<(&str, Value)>> = readings[1]
        .iter()
        .map(|reading| decimal(reading.group(sentence, 1).unwrap_or_default()))
        .filter(|day| (1..=28).contains(day))
        .map(|day| {
            vec![
                ("kind", Value::String("monthly".to_owned())),
                ("day", int(day)),
            ]
        })
        .collect();
    days.extend(readings[2].iter().map(|reading| {
        let name = reading.group(sentence, 1).unwrap_or_default();
        let weekday = WEEKDAYS
            .iter()
            .find(|(known, _)| *known == name)
            .map_or(0, |(_, weekday)| *weekday);
        vec![
            ("kind", Value::String("weekly".to_owned())),
            ("weekday", int(weekday)),
        ]
    }));
    if days.is_empty() && !readings[3].is_empty() {
        days.push(vec![("kind", Value::String("daily".to_owned()))]);
    }
    Ok(days
        .iter()
        .flat_map(|day| {
            times.iter().map(move |time| {
                let mut members = day.clone();
                members.push(("time", Value::String(time.clone())));
                object_of(members)
            })
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::{MAX_COUNT_DIGITS, decimal, interval, time};
    use crate::routine::MAX_CONTINUOUS_GAP_SECONDS;

    #[test]
    fn digits_of_any_script_read_as_python_int() {
        assert_eq!(decimal("0123456789"), 123_456_789);
        assert_eq!(decimal("\u{9e9}\u{9e6}"), 30);
        assert_eq!(decimal("\u{967}\u{966}"), 10);
        assert_eq!(decimal("\u{1d7d3}"), 5);
        assert_eq!(decimal("\u{11068}"), 2);
        assert_eq!(decimal("\u{661}2\u{9e9}"), 123);
        assert_eq!(decimal("\u{1fbf9}"), 9);
        assert_eq!(decimal(""), 0);
        assert_eq!(
            MAX_COUNT_DIGITS,
            MAX_CONTINUOUS_GAP_SECONDS.to_string().len()
        );
    }

    #[test]
    fn intervals_need_a_count_for_days_and_weeks_and_an_admitted_gap() {
        assert_eq!(
            interval(Some("30"), "segundo"),
            Some(crate::python::value::object_of(vec![
                ("kind", crate::python::value::text("continuous")),
                ("gap", crate::python::value::int(30)),
            ]))
        );
        assert!(interval(None, "hora").is_some());
        assert!(interval(Some(""), "stünd").is_some());
        assert!(interval(None, "dia").is_none());
        assert!(interval(Some("2"), "week").is_some());
        assert!(interval(Some("4"), "sec").is_none());
        assert!(interval(Some("123456789"), "second").is_none());
        assert!(interval(Some("53"), "week").is_none());
    }

    #[test]
    fn times_read_the_half_of_the_day() {
        let cases = [
            (("9", None, "pm"), Some("21:00")),
            (("12", Some("30"), "am"), Some("00:30")),
            (("12", None, "p.m."), Some("12:00")),
            (("0", None, "am"), None),
            (("13", None, "pm"), None),
            (("24", None, ""), None),
            (("9", Some("60"), ""), None),
            (("09", Some("5"), ""), Some("09:05")),
            (("23", None, ""), Some("23:00")),
            (("9", Some("59"), ""), Some("09:59")),
        ];
        for ((hour, minute, half), expected) in cases {
            assert_eq!(
                time(hour, minute, half).as_deref(),
                expected,
                "{hour} {minute:?} {half}"
            );
        }
    }
}
