//! `datetime.datetime.fromisoformat` exactly as the C implementation of Python 3.14 (`_datetimemodule.c`) parses it.
//!
//! The C parser reads a NUL-terminated UTF-8 buffer and sometimes looks one byte past the part it parses; this port
//! reads the same bytes, treating every position at or past the end as that NUL. The date takes one of the forms
//! `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www[-D]`, or `YYYYWww[D]`; one character of any kind separates the time,
//! `HH[:MM[:SS[.f]]]` or `HH[MM[SS[.f]]]` with up to six fraction digits read (more are skipped), and an optional
//! `Z` or `±HH[:MM[:SS[.f]]]` offset strictly inside one day. Hour 24 with zero minutes and seconds is midnight of the
//! next day. Every range check of `datetime(...)` applies.

/// A parsed datetime: its fields and its UTC offset in microseconds, `None` when naive.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DateTime {
    /// 1..=9999.
    pub year: i32,
    /// 1..=12.
    pub month: i32,
    /// 1..=the month's days.
    pub day: i32,
    /// 0..=23.
    pub hour: i32,
    /// 0..=59.
    pub minute: i32,
    /// 0..=59.
    pub second: i32,
    /// 0..=999,999.
    pub microsecond: i32,
    /// `utcoffset()` in microseconds, or `None` for a naive datetime.
    pub offset: Option<i64>,
}

/// The `ValueError` (or `TypeError`) `fromisoformat` raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidIsoformat;

const MIN_YEAR: i32 = 1;
const MAX_YEAR: i32 = 9999;
const DAY_MICROSECONDS: i64 = 86_400_000_000;
const DAYS_IN_400_YEARS: i32 = 146_097;

/// `year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)`.
#[must_use]
pub const fn is_leap(year: i32) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

/// The days of `month` (1..=12) in `year`.
#[must_use]
pub const fn days_in_month(year: i32, month: i32) -> i32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Days before January 1 of `year` (`days_before_year` of the C implementation).
const fn days_before_year(year: i32) -> i32 {
    let previous = year - 1;
    previous * 365 + previous / 4 - previous / 100 + previous / 400
}

const fn days_before_month(year: i32, month: i32) -> i32 {
    let mut days = 0;
    let mut current = 1;
    while current < month {
        days += days_in_month(year, current);
        current += 1;
    }
    days
}

/// The proleptic Gregorian ordinal of a date, January 1 of year 1 being 1.
#[must_use]
pub const fn ymd_to_ord(year: i32, month: i32, day: i32) -> i32 {
    days_before_year(year) + days_before_month(year, month) + day
}

/// The date of a proleptic Gregorian ordinal (at least 1).
#[must_use]
pub fn ord_to_ymd(ordinal: i32) -> (i32, i32, i32) {
    let offset = ordinal - 1;
    let mut year = offset / DAYS_IN_400_YEARS * 400 + 1;
    let mut rest = offset % DAYS_IN_400_YEARS;
    while rest >= days_in_year(year) {
        rest -= days_in_year(year);
        year += 1;
    }
    let mut month = 1;
    while rest >= days_in_month(year, month) {
        rest -= days_in_month(year, month);
        month += 1;
    }
    (year, month, rest + 1)
}

const fn days_in_year(year: i32) -> i32 {
    if is_leap(year) { 366 } else { 365 }
}

/// Monday 0 .. Sunday 6.
const fn weekday(year: i32, month: i32, day: i32) -> i32 {
    (ymd_to_ord(year, month, day) + 6) % 7
}

fn iso_to_ymd(year: i32, week: i32, day: i32) -> Option<(i32, i32, i32)> {
    if !(MIN_YEAR..=MAX_YEAR).contains(&year) {
        return None;
    }
    if week <= 0 || week >= 53 {
        let first = weekday(year, 1, 1);
        if !(week == 53 && (first == 3 || (first == 2 && is_leap(year)))) {
            return None;
        }
    }
    if !(1..=7).contains(&day) {
        return None;
    }
    let first_day = ymd_to_ord(year, 1, 1);
    let first_weekday = (first_day + 6) % 7;
    let mut week1_monday = first_day - first_weekday;
    if first_weekday > 3 {
        week1_monday += 7;
    }
    Some(ord_to_ymd(week1_monday + (week - 1) * 7 + day - 1))
}

/// A parsed time: hour, minute, second, and microsecond, and the offset's seconds and microseconds when it has one.
type Time = ([i32; 4], Option<(i32, i32)>);

/// The C buffer: the bytes, then NUL forever.
struct Buffer<'a>(&'a [u8]);

impl Buffer<'_> {
    fn at(&self, index: usize) -> u8 {
        self.0.get(index).copied().unwrap_or(0)
    }

    /// `parse_digits`: `count` ASCII digits from `index`, accumulated onto `value`.
    fn digits(&self, index: usize, count: usize, value: &mut i32) -> Option<usize> {
        for offset in 0..count {
            let digit = self.at(index + offset).wrapping_sub(b'0');
            if digit > 9 {
                return None;
            }
            *value = *value * 10 + i32::from(digit);
        }
        Some(index + count)
    }
}

/// `_find_isoformat_datetime_separator` for a string of at least 7 bytes.
fn separator(buffer: &Buffer<'_>, length: usize) -> Option<usize> {
    if length == 7 {
        return Some(7);
    }
    if buffer.at(4) == b'-' {
        if buffer.at(5) != b'W' {
            return Some(10);
        }
        // A string of 7 bytes returned above, and fewer than 7 never reach here, so the C `len < 8` check never
        // refuses.
        // Every byte at or past the end reads as NUL, so the C length checks before reading bytes 8 and 10 and the
        // digits after `W` change nothing and are left out.
        if buffer.at(8) == b'-' {
            if length == 9 {
                return None;
            }
            return Some(if buffer.at(10).is_ascii_digit() {
                8
            } else {
                10
            });
        }
        return Some(8);
    }
    if buffer.at(4) != b'W' {
        return Some(8);
    }
    let mut index = 7;
    while buffer.at(index).is_ascii_digit() {
        index += 1;
    }
    if index < 9 {
        Some(index)
    } else if index % 2 == 0 {
        Some(7)
    } else {
        Some(8)
    }
}

/// `parse_isoformat_date`: the date of the first `length` bytes (the C code reads past it when it may).
fn parse_date(buffer: &Buffer<'_>, length: usize) -> Option<(i32, i32, i32)> {
    let mut year = 0;
    let mut position = buffer.digits(0, 4, &mut year)?;
    let separated = buffer.at(position) == b'-';
    position += usize::from(separated);
    if buffer.at(position) == b'W' {
        let mut week = 0;
        position = buffer.digits(position + 1, 2, &mut week)?;
        let mut day = 1;
        if position < length {
            // A separated week date with a day was split after `YYYY-Www-D`, whose byte 8 is the `-` the C code
            // checks again, so only the position moves.
            position += usize::from(separated);
            day = 0;
            buffer.digits(position, 1, &mut day)?;
        }
        return iso_to_ymd(year, week, day);
    }
    let (mut month, mut day) = (0, 0);
    position = buffer.digits(position, 2, &mut month)?;
    if separated {
        if buffer.at(position) != b'-' {
            return None;
        }
        position += 1;
    }
    buffer.digits(position, 2, &mut day)?;
    Some((year, month, day))
}

/// The result of `parse_hh_mm_ss_ff`: the fields and whether text remains (the C return value 1).
struct Clock {
    fields: [i32; 4],
    trailing: bool,
}

/// `parse_hh_mm_ss_ff` over `[start, end)` of the buffer.
fn parse_clock(buffer: &Buffer<'_>, start: usize, end: usize) -> Option<Clock> {
    let mut fields = [0_i32; 4];
    let mut position = start;
    let mut separated = true;
    for index in 0..3 {
        position = buffer.digits(position, 2, &mut fields[index])?;
        let character = buffer.at(position);
        position += 1;
        if index == 0 {
            separated = character == b':';
        }
        if position >= end {
            return Some(Clock {
                fields,
                trailing: character != 0,
            });
        }
        if separated && character == b':' {
            if index == 2 {
                return None;
            }
        } else if character == b'.' || character == b',' {
            if index < 2 {
                return None;
            }
            break;
        } else if separated {
            return None;
        } else {
            position -= 1;
        }
    }
    let remaining = end.saturating_sub(position);
    let count = remaining.min(6);
    position = buffer.digits(position, count, &mut fields[3])?;
    if count < 6 {
        fields[3] *= [1, 100_000, 10_000, 1000, 100, 10][count];
    }
    while buffer.at(position).is_ascii_digit() {
        position += 1;
    }
    Some(Clock {
        fields,
        trailing: buffer.at(position) != 0,
    })
}

/// `parse_isoformat_time` from `start`: the clock and, with an offset, its seconds and microseconds.
fn parse_time(buffer: &Buffer<'_>, start: usize, end: usize) -> Option<Time> {
    let zone = (start..end)
        .find(|index| matches!(buffer.at(*index), b'Z' | b'+' | b'-'))
        .unwrap_or(end);
    let clock = parse_clock(buffer, start, zone)?;
    if zone == end {
        return (!clock.trailing).then_some((clock.fields, None));
    }
    if buffer.at(zone) == b'Z' {
        return (buffer.at(zone + 1) == 0).then_some((clock.fields, Some((0, 0))));
    }
    let sign = if buffer.at(zone) == b'-' { -1 } else { 1 };
    let offset = parse_clock(buffer, zone + 1, end);
    let offset = offset.filter(|offset| !offset.trailing)?;
    let [hours, minutes, seconds, microseconds] = offset.fields;
    Some((
        clock.fields,
        Some((
            sign * (hours * 3600 + minutes * 60 + seconds),
            sign * microseconds,
        )),
    ))
}

/// `datetime.datetime.fromisoformat(text)`.
///
/// # Errors
///
/// Returns [`InvalidIsoformat`] wherever Python raises.
pub fn fromisoformat(text: &str) -> Result<DateTime, InvalidIsoformat> {
    let ((year, month, day), (clock, zone)) = parse(text).ok_or(InvalidIsoformat)?;
    let offset = zone.map(|(seconds, microseconds)| {
        if seconds == 0 {
            0
        } else {
            i64::from(seconds) * 1_000_000 + i64::from(microseconds)
        }
    });
    if offset.is_some_and(|offset| offset <= -DAY_MICROSECONDS || offset >= DAY_MICROSECONDS) {
        return Err(InvalidIsoformat);
    }
    let [_, minute, second, microsecond] = clock;
    let (year, month, day, hour) = next_midnight(year, month, day, clock)?;
    let valid = (MIN_YEAR..=MAX_YEAR).contains(&year)
        && (1..=12).contains(&month)
        && (1..=days_in_month(year, month)).contains(&day)
        && (0..=23).contains(&hour)
        && (0..=59).contains(&minute)
        && (0..=59).contains(&second)
        && (0..=999_999).contains(&microsecond);
    if !valid {
        return Err(InvalidIsoformat);
    }
    Ok(DateTime {
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        offset,
    })
}

/// The date and time parts of `text`, before any range check.
fn parse(text: &str) -> Option<((i32, i32, i32), Time)> {
    let bytes = text.as_bytes();
    if text.chars().count() < 7 {
        return None;
    }
    let buffer = Buffer(bytes);
    let length = bytes.len();
    let split = separator(&buffer, length)?;
    let date = parse_date(&buffer, split)?;
    if length <= split {
        return Some((date, ([0; 4], None)));
    }
    let width = match buffer.at(split) {
        byte if byte & 0x80 == 0 => 1,
        byte if byte & 0xf0 == 0xe0 => 3,
        byte if byte & 0xf0 == 0xf0 => 4,
        _ => 2,
    };
    Some((date, parse_time(&buffer, split + width, length)?))
}

/// Hour 24 of a valid day is midnight of the next day when its minutes, seconds, and microseconds are zero.
fn next_midnight(
    year: i32,
    month: i32,
    day: i32,
    clock: [i32; 4],
) -> Result<(i32, i32, i32, i32), InvalidIsoformat> {
    let [hour, minute, second, microsecond] = clock;
    if hour != 24 || month > 12 || day > days_in_month(year, month) {
        return Ok((year, month, day, hour));
    }
    if minute != 0 || second != 0 || microsecond != 0 {
        return Err(InvalidIsoformat);
    }
    if day < days_in_month(year, month) {
        Ok((year, month, day + 1, 0))
    } else if month < 12 {
        Ok((year, month + 1, 1, 0))
    } else {
        Ok((year + 1, 1, 1, 0))
    }
}
