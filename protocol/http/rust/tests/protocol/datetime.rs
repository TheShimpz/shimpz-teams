//! `datetime.datetime.fromisoformat` and the calendar helpers, against what Python 3.14 answers.

use team_protocol::python::datetime::{
    DateTime, InvalidIsoformat, days_in_month, fromisoformat, is_leap, ord_to_ymd, ymd_to_ord,
};

fn parsed(text: &str) -> DateTime {
    fromisoformat(text).unwrap_or_else(|_| panic!("{text} parses"))
}

fn fields(text: &str) -> [i32; 7] {
    let value = parsed(text);
    [
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    ]
}

#[test]
fn calendar_helpers_match_the_proleptic_gregorian_calendar() {
    assert!(is_leap(2024) && is_leap(2000) && !is_leap(1900) && !is_leap(2026));
    assert_eq!(
        [
            days_in_month(2024, 2),
            days_in_month(2026, 2),
            days_in_month(2026, 4),
            days_in_month(2026, 12)
        ],
        [29, 28, 30, 31]
    );
    assert_eq!(ymd_to_ord(1, 1, 1), 1);
    for ordinal in [1, 59, 60, 365, 366, 146_097, 146_098, 730_120, 3_652_059] {
        let (year, month, day) = ord_to_ymd(ordinal);
        assert_eq!(ymd_to_ord(year, month, day), ordinal);
    }
    assert_eq!(ord_to_ymd(3_652_059), (9999, 12, 31));
}

#[test]
fn every_python_date_and_time_form_parses() {
    for (text, expected) in [
        ("2026-08-28T17:00:00", [2026, 8, 28, 17, 0, 0, 0]),
        ("20260828T170102", [2026, 8, 28, 17, 1, 2, 0]),
        ("2026-08-28 17:01:02.5", [2026, 8, 28, 17, 1, 2, 500_000]),
        (
            "2026-08-28T17:01:02,1234567",
            [2026, 8, 28, 17, 1, 2, 123_456],
        ),
        ("2026-W01-1", [2025, 12, 29, 0, 0, 0, 0]),
        ("2026W017", [2026, 1, 4, 0, 0, 0, 0]),
        ("2026W01", [2025, 12, 29, 0, 0, 0, 0]),
        ("2026-W01", [2025, 12, 29, 0, 0, 0, 0]),
        ("2020-W53-7T01", [2021, 1, 3, 1, 0, 0, 0]),
        ("2015-W53-1", [2015, 12, 28, 0, 0, 0, 0]),
        ("2026W01710", [2025, 12, 29, 10, 0, 0, 0]),
        ("2026-W53-1", [2026, 12, 28, 0, 0, 0, 0]),
        ("2026-W01-0000", [2025, 12, 29, 0, 0, 0, 0]),
        ("2026-01-31T24:00", [2026, 2, 1, 0, 0, 0, 0]),
        ("2026-12-31T24:00:00", [2027, 1, 1, 0, 0, 0, 0]),
        ("2026-01-01T24:00", [2026, 1, 2, 0, 0, 0, 0]),
        ("2026-01-01\u{e9}12", [2026, 1, 1, 12, 0, 0, 0]),
        ("2026-01-01\u{4e2d}12", [2026, 1, 1, 12, 0, 0, 0]),
        ("2026-01-01\u{430}12", [2026, 1, 1, 12, 0, 0, 0]),
        ("2026-01-01\u{1f600}12", [2026, 1, 1, 12, 0, 0, 0]),
    ] {
        assert_eq!(fields(text), expected, "{text}");
    }
}

#[test]
fn offsets_follow_python() {
    for (text, expected) in [
        ("2026-01-01T12Z", Some(0)),
        ("2026-01-01T12+0530", Some(19_800_000_000)),
        ("2026-01-01T12-00:00:01.5", Some(-1_500_000)),
        ("2026-01-01T12+00:00:00.5", Some(0)),
        ("2026-01-01T12", None),
        ("2026-01-01", None),
    ] {
        assert_eq!(parsed(text).offset, expected, "{text}");
    }
}

/// Strings Python refuses.
const REFUSED: [&str; 48] = [
    "2026-01",
    "2026-W0",
    "2026-W01-",
    "2026-0101",
    "2026-01-0x",
    "202x-01-01",
    "2026-W0x",
    "2026-W01x1",
    "2026-W01-x",
    "2026-W54-1",
    "2025-W53-1",
    "2027-W53-1",
    "2026W0110",
    "2026-W00-1",
    "2026-W01-8",
    "0000-W01-1",
    "2026-13-01",
    "2026-02-29",
    "2026-01-00",
    "0000-01-01",
    "2026-01-01T",
    "2026-01-01T1",
    "2026-01-01T12:3",
    "2026-01-01T1230:00",
    "2026-01-01T12:30:00:00",
    "2026-01-01T12.5",
    "2026-01-01T12:30.5",
    "2026-01-01T12:30:00.",
    "2026-01-01T12:30:00.x",
    "2026-01-01T12:30x",
    "2026-01-01T12:30:00Zx",
    "2026-01-01T12+",
    "2026-01-01T12+1",
    "2026-01-01T12+123",
    "2026-01-01T12+24",
    "2026-01-01T12-24:00",
    "2026-01-01T12Z+01",
    "2026-01-01T25",
    "2026-01-01T12:60",
    "2026-01-01T12:00:60",
    "2026-01-01T24:00:01",
    "2026-01-01T24:00:00.1",
    "2026-01-01T24:01",
    "2026-13-01T24:00",
    "2026-02-30T24:00",
    "2026-01-01T12x",
    "2026-01-01T1230.5x",
    "9999-12-31T24:00",
];

#[test]
fn every_python_refusal_refuses() {
    for text in REFUSED {
        assert_eq!(fromisoformat(text), Err(InvalidIsoformat), "{text}");
    }
}
