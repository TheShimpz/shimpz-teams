//! When a Routine fires: the next occurrence of its canonical schedule, strictly after an instant (ADR-0086).
//!
//! Instants are whole UTC epoch seconds. A wall-clock time runs at its first occurrence when it is ambiguous and
//! shifted past the gap when it does not exist, as Python's `fold=0` does.

use std::fmt;

use jiff::civil::DateTime;
use jiff::tz::{Disambiguation, TimeZone};
use strict_json::Value;
use team_protocol::routine::{canonical_schedule, canonical_timezone};

use crate::dates::date::Date;
use crate::json::{as_str, at};

/// A schedule or timezone is not in the closed Routine contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ScheduleError(pub &'static str);

impl fmt::Display for ScheduleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for ScheduleError {}

/// The IANA zone a canonical name loads, by its exact name; never a path or the host's local zone.
///
/// # Errors
///
/// [`ScheduleError`] for a name outside the grammar or one the zone database does not hold.
pub fn zone(name: &Value) -> Result<TimeZone, ScheduleError> {
    let name = canonical_timezone(name).ok_or(ScheduleError("invalid timezone"))?;
    zone_named(name)
}

/// [`zone`] of a name already read as text.
///
/// # Errors
///
/// [`ScheduleError`] for a name outside the grammar or one the zone database does not hold.
pub fn zone_named(name: &str) -> Result<TimeZone, ScheduleError> {
    if canonical_timezone(&Value::String(name.to_owned())).is_none() {
        return Err(ScheduleError("invalid timezone"));
    }
    // The database is read without case, so only a zone it names exactly as written is the zone Python loads.
    TimeZone::get(name)
        .ok()
        .filter(|zone| zone.iana_name() == Some(name))
        .ok_or(ScheduleError("unknown timezone"))
}

/// The instant a wall-clock time on a local date names in a zone.
fn wall_clock(day: Date, time: &str, zone: &TimeZone) -> Option<i64> {
    let (hour, minute) = time.split_once(':')?;
    let civil = DateTime::new(
        i16::try_from(day.year).ok()?,
        i8::try_from(day.month).ok()?,
        i8::try_from(day.day).ok()?,
        hour.parse().ok()?,
        minute.parse().ok()?,
        0,
        0,
    )
    .ok()?;
    let zoned = zone
        .to_ambiguous_zoned(civil)
        .disambiguate(Disambiguation::Compatible)
        .ok()?;
    Some(zoned.timestamp().as_second())
}

fn matches(schedule: &Value, day: Date) -> bool {
    match as_str(at(schedule, "kind")) {
        Some("weekly") => at(schedule, "weekday").eq(&crate::json::int(i64::from(day.weekday()))),
        Some("monthly") => at(schedule, "day").eq(&crate::json::int(i64::from(day.day))),
        _ => true,
    }
}

/// The first firing strictly after `after` and the Routine's `anchor`.
///
/// # Errors
///
/// [`ScheduleError`] for a schedule or timezone outside the contract, or a firing beyond the calendar.
pub fn next_run(
    schedule: &Value,
    timezone: &Value,
    anchor: i64,
    after: i64,
) -> Result<i64, ScheduleError> {
    let canonical = canonical_schedule(schedule).ok_or(ScheduleError("invalid schedule"))?;
    let zone = zone(timezone)?;
    // Nothing fires at or before the anchor: the first firing is strictly after it.
    let after = after.max(anchor);
    if let Some(gap) = canonical
        .get("gap")
        .and_then(team_protocol::python::value::small_int)
    {
        // A continuous Routine's next run is due its gap after `after`: the start a claim records, never a backlog.
        return after
            .checked_add(gap)
            .ok_or(ScheduleError("invalid schedule"));
    }
    let time = as_str(at(canonical, "time")).ok_or(ScheduleError("invalid schedule"))?;
    // Start a day early: a firing on the previous local date can still lie after `after` in UTC.
    let mut day = Date::at(after, &zone)
        .and_then(|day| day.add_days(-1))
        .ok_or(ScheduleError("invalid schedule"))?;
    loop {
        if matches(canonical, day) {
            let candidate =
                wall_clock(day, time, &zone).ok_or(ScheduleError("invalid schedule"))?;
            if candidate > after {
                return Ok(candidate);
            }
        }
        day = day.add_days(1).ok_or(ScheduleError("invalid schedule"))?;
    }
}

#[cfg(test)]
mod tests;
