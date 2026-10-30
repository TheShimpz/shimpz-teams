//! Fixtures the Routine tests share (`routine_fixture.py`).

use jiff::civil::date;
use strict_json::Value;

/// The epoch second of a UTC civil time.
pub fn epoch(year: i16, month: i8, day: i8, hour: i8, minute: i8, second: i8) -> i64 {
    date(year, month, day)
        .at(hour, minute, second, 0)
        .to_zoned(jiff::tz::TimeZone::UTC)
        .expect("a UTC instant")
        .timestamp()
        .as_second()
}

/// A JSON document a test writes.
pub fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a test document")
}
