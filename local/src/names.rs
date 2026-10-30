//! Local Team display names (ADR-0088): a mutable name over an immutable id.
//!
//! A Team network's label keeps its creation name and proves ownership. A rename writes a Team-owned record, bound to
//! the network incarnation, under the inference volume (`<sha256(team_id)>.name.json`); the current name is that
//! record, or the label when no record exists. Names are unique per Space ignoring case, under the names lock.

use std::io::ErrorKind;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_protocol::payload::{canonical_local_team_name, canonical_team_name};
use team_protocol::python::text::{casefold, nfc};
use team_storage::json_record;

use crate::http::service::Problem;

/// The record's schema.
pub const SCHEMA: i64 = 1;
/// The largest record.
pub const MAX_RECORD_BYTES: usize = 4096;

/// The names store failed its safety checks.
pub const NAMES_UNAVAILABLE: Problem = Problem::new(
    503,
    "Team names failed their safety checks",
    "team-names-unavailable",
);
/// A name outside the Local name contract.
pub const INVALID_NAME: Problem = Problem::new(
    422,
    "Team name must contain 1 to 80 trimmed characters",
    "invalid-team-name",
);

/// A Local display name as sent: 1 to 80 trimmed characters without controls, already NFC.
///
/// # Errors
/// `invalid-team-name`.
pub fn canonical_name(value: &Value) -> Result<String, Problem> {
    canonical_local_team_name(value)
        .map(str::to_owned)
        .ok_or(INVALID_NAME)
}

/// A Team network's creation-name label, when it is a Team name (the shared bounds; a label is never renamed).
#[must_use]
pub fn label_name(label: Option<&String>) -> Option<String> {
    label
        .map(|label| Value::String(label.clone()))
        .as_ref()
        .and_then(canonical_team_name)
        .map(str::to_owned)
}

/// The form two names are compared in: case-folded, then NFC.
#[must_use]
pub fn folded(name: &str) -> String {
    nfc(&casefold(name))
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        // Writing to a String cannot fail.
        let _ = write!(text, "{byte:02x}");
    }
    text
}

/// Whether `name` is a file this store writes: a record, or the temporary an interrupted write left.
fn owned(name: &str) -> bool {
    let is_hex = |text: &str, length: usize| {
        text.len() == length
            && text
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    };
    if let Some(digest) = name.strip_suffix(".name.json") {
        return is_hex(digest, 64);
    }
    name.strip_prefix('.')
        .and_then(|rest| rest.strip_suffix(".tmp"))
        .and_then(|rest| rest.split_once(".name.json."))
        .is_some_and(|(digest, suffix)| is_hex(digest, 64) && is_hex(suffix, 16))
}

/// One record per renamed Team; an absent record means the Team still has its creation name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TeamNames {
    root: PathBuf,
}

impl TeamNames {
    /// The store in `root` (the inference volume).
    #[must_use]
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn file_name(team_id: &str) -> String {
        format!("{}.name.json", hex(&Sha256::digest(team_id.as_bytes())))
    }

    fn path(&self, team_id: &str) -> PathBuf {
        self.root.join(Self::file_name(team_id))
    }

    /// The recorded name of this exact incarnation (`network_id`), or `None` when it has none.
    ///
    /// # Errors
    /// `team-names-unavailable` for a record that is unsafe, malformed, or another incarnation's.
    pub fn load(&self, team_id: &str, network_id: &str) -> Result<Option<String>, Problem> {
        let Some(value) = json_record::read(&self.path(team_id), MAX_RECORD_BYTES)
            .map_err(|_| NAMES_UNAVAILABLE)?
        else {
            return Ok(None);
        };
        let Value::Object(members) = &value else {
            return Err(NAMES_UNAVAILABLE);
        };
        let field = |name: &str| {
            members
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value)
        };
        let exact = members.len() == 4
            && field("schema") == Some(&Value::Number(Number::Int(SCHEMA)))
            && field("team_id") == Some(&Value::String(team_id.to_owned()))
            && field("network_id") == Some(&Value::String(network_id.to_owned()));
        let name = field("team_name").and_then(canonical_local_team_name);
        match name {
            Some(name) if exact => Ok(Some(name.to_owned())),
            _ => Err(NAMES_UNAVAILABLE),
        }
    }

    /// Records `team_name` as the name of `team_id`'s incarnation `network_id`.
    ///
    /// # Errors
    /// `team-names-unavailable` when it cannot be written.
    pub fn save(&self, team_id: &str, network_id: &str, team_name: &str) -> Result<(), Problem> {
        let record = Value::Object(vec![
            ("schema".to_owned(), Value::Number(Number::Int(SCHEMA))),
            ("team_id".to_owned(), Value::String(team_id.to_owned())),
            (
                "network_id".to_owned(),
                Value::String(network_id.to_owned()),
            ),
            ("team_name".to_owned(), Value::String(team_name.to_owned())),
        ]);
        std::fs::create_dir_all(&self.root)
            .and_then(|()| {
                std::fs::set_permissions(&self.root, std::fs::Permissions::from_mode(0o700))
            })
            .map_err(|_| NAMES_UNAVAILABLE)?;
        json_record::write(&self.path(team_id), &record, MAX_RECORD_BYTES)
            .map_err(|_| NAMES_UNAVAILABLE)
    }

    fn owned_files(&self) -> Result<Vec<String>, Problem> {
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
            Err(_) => return Err(NAMES_UNAVAILABLE),
        };
        let mut names = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|_| NAMES_UNAVAILABLE)?;
            if let Some(name) = entry.file_name().to_str()
                && owned(name)
            {
                names.push(name.to_owned());
            }
        }
        Ok(names)
    }

    fn unlink(root: &Path, names: &[String]) -> Result<(), Problem> {
        for name in names {
            match std::fs::remove_file(root.join(name)) {
                Ok(()) => {}
                Err(error) if error.kind() == ErrorKind::NotFound => {}
                Err(_) => return Err(NAMES_UNAVAILABLE),
            }
        }
        Ok(())
    }

    /// Removes the Team's record and any temporary an interrupted rename left; absence is success.
    ///
    /// # Errors
    /// `team-names-unavailable`.
    pub fn delete(&self, team_id: &str) -> Result<(), Problem> {
        let record = Self::file_name(team_id);
        let temporary = format!(".{record}.");
        let mut names = vec![record];
        names.extend(
            self.owned_files()?
                .into_iter()
                .filter(|name| name.starts_with(&temporary)),
        );
        Self::unlink(&self.root, &names)
    }

    /// Removes every record, including one whose Team network a crash already removed.
    ///
    /// # Errors
    /// `team-names-unavailable`.
    pub fn delete_all(&self) -> Result<(), Problem> {
        Self::unlink(&self.root, &self.owned_files()?)
    }
}

/// Docker's RFC 3339 instant (whole seconds, an optional fraction of up to nanoseconds, and `Z` or an offset up to
/// `±23:59`) in nanoseconds since the epoch; `None` when it is not one.
#[must_use]
pub fn docker_instant_ns(value: &str) -> Option<i128> {
    let bytes = value.as_bytes();
    let digits = |range: std::ops::Range<usize>| bytes.get(range).and_then(digits_of);
    let separators = [(4, b'-'), (7, b'-'), (10, b'T'), (13, b':'), (16, b':')];
    if bytes.len() < 20 || separators.iter().any(|(at, byte)| bytes[*at] != *byte) {
        return None;
    }
    let (year, month, day) = (digits(0..4)?, digits(5..7)?, digits(8..10)?);
    let (hour, minute, second) = (digits(11..13)?, digits(14..16)?, digits(17..19)?);
    let (nanos, zone) = fraction(&value[19..])?;
    let offset = zone_offset(zone)?;
    let valid = (1..=12).contains(&month)
        && (1..=days_in_month(year, month)).contains(&day)
        && hour <= 23
        && minute <= 59
        && second <= 59
        && year >= 1;
    if !valid {
        return None;
    }
    let seconds =
        days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second - offset;
    Some(i128::from(seconds) * 1_000_000_000 + i128::from(nanos))
}

/// The nanoseconds of an optional `.` fraction of 1 to 9 digits, and what follows it.
fn fraction(rest: &str) -> Option<(i64, &str)> {
    let Some(fraction) = rest.strip_prefix('.') else {
        return Some((0, rest));
    };
    let length = fraction.bytes().take_while(u8::is_ascii_digit).count();
    if !(1..=9).contains(&length) {
        return None;
    }
    let nanos = format!("{:0<9}", &fraction[..length]).parse().ok()?;
    Some((nanos, &fraction[length..]))
}

/// The seconds east of UTC of `Z` or `±hh:mm` up to `±23:59`.
fn zone_offset(zone: &str) -> Option<i64> {
    if zone == "Z" {
        return Some(0);
    }
    let bytes = zone.as_bytes();
    let sign = match bytes.first() {
        Some(b'+') => 1,
        Some(b'-') => -1,
        _ => return None,
    };
    if bytes.len() != 6 || bytes[3] != b':' {
        return None;
    }
    let hours = digits_of(&bytes[1..3])?;
    let minutes = digits_of(&bytes[4..6])?;
    (hours <= 23 && minutes <= 59).then_some(sign * (hours * 3600 + minutes * 60))
}

fn digits_of(part: &[u8]) -> Option<i64> {
    part.iter().all(u8::is_ascii_digit).then(|| {
        part.iter()
            .fold(0_i64, |total, digit| total * 10 + i64::from(digit - b'0'))
    })
}

fn days_in_month(year: i64, month: i64) -> i64 {
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Days since 1970-01-01 of a proleptic Gregorian date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year - era * 400;
    let month_index = (month + 9) % 12;
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

#[cfg(test)]
mod tests;
