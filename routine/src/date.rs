//! A calendar date as Python's `datetime.date` is one: the proleptic Gregorian calendar from year 1 to 9999, counted
//! by its ordinal, and the instants and zones that name one.

use jiff::Timestamp;
use jiff::tz::TimeZone;
use team_protocol::python::datetime::{days_in_month, ord_to_ymd, ymd_to_ord};

/// The latest year a date may have.
const MAX_YEAR: i32 = 9999;

/// One calendar date.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date {
    /// The year, 1 to 9999.
    pub year: i32,
    /// The month, 1 to 12.
    pub month: i32,
    /// The day of the month.
    pub day: i32,
}

impl Date {
    /// `datetime.date(year, month, day)`, or `None` where Python raises `ValueError`.
    #[must_use]
    pub const fn new(year: i32, month: i32, day: i32) -> Option<Self> {
        if year < 1 || year > MAX_YEAR || month < 1 || month > 12 || day < 1 {
            return None;
        }
        if day > days_in_month(year, month) {
            return None;
        }
        Some(Self { year, month, day })
    }

    /// `date.toordinal()`: 1 for 1 January of year 1.
    #[must_use]
    pub const fn ordinal(self) -> i32 {
        ymd_to_ord(self.year, self.month, self.day)
    }

    /// `date.fromordinal(ordinal)`, or `None` outside the years Python admits.
    #[must_use]
    pub fn from_ordinal(ordinal: i32) -> Option<Self> {
        let last = ymd_to_ord(MAX_YEAR, 12, 31);
        if !(1..=last).contains(&ordinal) {
            return None;
        }
        let (year, month, day) = ord_to_ymd(ordinal);
        Some(Self { year, month, day })
    }

    /// `date + timedelta(days=days)`, or `None` where Python raises `OverflowError`.
    #[must_use]
    pub fn add_days(self, days: i64) -> Option<Self> {
        let ordinal = i64::from(self.ordinal()).checked_add(days)?;
        Self::from_ordinal(i32::try_from(ordinal).ok()?)
    }

    /// `(self - other).days`.
    #[must_use]
    pub const fn days_since(self, other: Self) -> i32 {
        self.ordinal() - other.ordinal()
    }

    /// `date.weekday()`: 0 for Monday.
    #[must_use]
    pub const fn weekday(self) -> i32 {
        (self.ordinal() + 6) % 7
    }

    /// `date.isoformat()`.
    #[must_use]
    pub fn isoformat(self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    /// The date of an epoch instant in a zone: `datetime.fromtimestamp(instant, zone).date()`.
    #[must_use]
    pub fn at(instant: i64, zone: &TimeZone) -> Option<Self> {
        let civil = Timestamp::from_second(instant)
            .ok()?
            .to_zoned(zone.clone())
            .date();
        Self::new(
            i32::from(civil.year()),
            i32::from(civil.month()),
            i32::from(civil.day()),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::Date;

    #[test]
    fn dates_count_as_python_counts_them() {
        let day = Date::new(2026, 10, 15).expect("a date");
        assert_eq!(day.weekday(), 3);
        assert_eq!(day.add_days(17), Date::new(2026, 11, 1));
        assert_eq!(day.add_days(-288), Date::new(2025, 12, 31));
        assert_eq!(day.isoformat(), "2026-10-15");
        assert_eq!(Date::new(2026, 2, 29), None);
        assert_eq!(Date::new(0, 1, 1), None);
        assert_eq!(
            Date::new(9999, 12, 31).and_then(|last| last.add_days(1)),
            None
        );
        assert_eq!(Date::new(1, 1, 1).map(Date::ordinal), Some(1));
        assert_eq!(
            Date::new(2026, 10, 16).map(|later| later.days_since(day)),
            Some(1)
        );
    }
}
