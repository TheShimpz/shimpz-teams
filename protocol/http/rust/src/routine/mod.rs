//! Canonical Team Routine wire forms (ADR-0086, ADR-0101): schedules, timezones, bounds, plans, and
//! outputs.

mod output;
mod plan;

use std::sync::LazyLock;

use strict_json::Value;

pub use self::output::{
    MAX_OUTPUT_BYTES, MAX_OUTPUT_DEPTH, MAX_OUTPUT_FIELDS, MAX_OUTPUT_ITEMS, MAX_OUTPUT_KEY_CHARS,
    MAX_OUTPUT_NUMBER_CHARS, MAX_OUTPUT_TEXT_CHARS, OUTPUT_MODES, OUTPUT_NUMBER_RE, OUTPUT_STATES,
    SHOWN_MODES, canonical_disposition, canonical_output,
};
pub use self::plan::{
    MAX_MEMBER_CHARS, MAX_PAGE_BYTES, MAX_PAGE_STEPS, MAX_POINTER_CHARS, MAX_PREVIEW_CHARS,
    MAX_STEP_INPUTS, MAX_STEP_STORED_INPUTS, MAX_STEP_VIEW_BYTES, MAX_SUMMARY_RUNS,
    MAX_WHERE_CHARS, PLAN_DIGEST_RE, PLAN_UNSAFE_RE, POINTER_RE, canonical_page, canonical_step,
    canonical_summary, escaped, literal_preview, members, paged, plain, plan_unsafe, preview,
    reference, where_text,
};
use crate::identifiers::{canonical_action_id, canonical_assistant_id, canonical_team_id};
pub use crate::payload::{canonical_run_usage, canonical_turn_usage};
use crate::python::json::{CANONICAL, encoded_len};
use crate::python::regex::{Pattern, declared};
use crate::python::text;
pub use crate::python::value::whole;
use crate::python::value::{exact_object, field, small_int, string};

/// The most Routines of one Team.
pub const MAX_ROUTINES: usize = 8;
/// The longest Routine name.
pub const MAX_ROUTINE_NAME_CHARS: usize = 80;
/// The longest Routine card description.
pub const MAX_ROUTINE_DESCRIPTION_CHARS: usize = 600;
/// The ordered Actions of a recorded plan.
pub const MAX_ROUTINE_STEPS: i64 = 256;
/// A continuous Routine starts its runs at least this far apart.
pub const MIN_CONTINUOUS_GAP_SECONDS: i64 = 5;
/// A continuous Routine starts its runs at most this far apart.
pub const MAX_CONTINUOUS_GAP_SECONDS: i64 = 366 * 86_400;
/// One day.
pub const DAY_SECONDS: i64 = 86_400;
/// The largest daily cap of a continuous Routine: its shortest gap's whole day.
pub const MAX_CONTINUOUS_CAP: i64 =
    (DAY_SECONDS + MIN_CONTINUOUS_GAP_SECONDS - 1) / MIN_CONTINUOUS_GAP_SECONDS;
/// The healthy runs one minute bucket rolls up.
pub const MAX_ROLLUP_RUNS: usize = 60;
/// The Assistants one Routine pins at most.
pub const MAX_ROUTINE_ASSISTANTS: usize = 16;
/// A Routine's permitted Actions: every Action its replay steps call, each once.
pub const MAX_PERMITTED: i64 = MAX_ROUTINE_STEPS;
/// The notice outcomes, sorted.
pub const OUTCOMES: [&str; 16] = [
    "changed",
    "created",
    "deleted",
    "denied",
    "done",
    "failed",
    "frozen",
    "healthy",
    "held",
    "paused",
    "recovered",
    "scope-changed",
    "skipped",
    "stopped",
    "streak-paused",
    "user-skipped",
];
/// Outcomes of the Routine itself, never of a run.
pub const ROUTINE_OUTCOMES: [&str; 7] = [
    "skipped",
    "scope-changed",
    "created",
    "changed",
    "healthy",
    "deleted",
    "streak-paused",
];
/// Why a held run's Routine was paused.
pub const PAUSE_REASONS: [&str; 3] = ["exhausted", "policy", "evidence"];
/// A closed error code.
pub static ERROR_CODE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[a-z][a-z0-9-]{0,63}\Z"));
/// Consecutive no-effect failed runs that pause a Routine.
pub const MAX_FAILURE_STREAK: i64 = 3;
/// The interface languages, sorted.
pub const LOCALES: [&str; 8] = ["ar", "de", "en", "es", "fr", "ja", "pt", "zh"];
/// The schedule kinds, sorted.
pub const SCHEDULE_KINDS: [&str; 4] = ["continuous", "daily", "monthly", "weekly"];
/// A Routine id.
pub static ROUTINE_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));
/// An IANA zone name such as `UTC` or `America/Argentina/Buenos_Aires`.
pub static TIMEZONE_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}\Z")
});
static TIME_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z"));
/// Where a Routine's timezone came from.
pub const TIMEZONE_SOURCES: [&str; 3] = ["browser", "person", "none"];
/// The zone a Routine without a known zone runs on by convention.
pub const CONVENTIONAL_TIMEZONE: &str = "UTC";

fn schedule_fields(kind: &str) -> &'static [&'static str] {
    match kind {
        "daily" => &["kind", "time"],
        "weekly" => &["kind", "weekday", "time"],
        "monthly" => &["kind", "day", "time"],
        _ => &["kind", "gap"],
    }
}

/// `canonical_schedule`: daily, weekly (0 = Monday), or monthly on day 1..28 at a wall-clock time, or continuous with
/// a gap of 5 seconds to 366 days.
#[must_use]
pub fn canonical_schedule(value: &Value) -> Option<&Value> {
    let kind = value
        .get("kind")
        .and_then(string)
        .filter(|kind| SCHEDULE_KINDS.contains(kind))?;
    let members = exact_object(value, schedule_fields(kind))?;
    let valid = match kind {
        "continuous" => whole(
            field(members, "gap"),
            MIN_CONTINUOUS_GAP_SECONDS,
            MAX_CONTINUOUS_GAP_SECONDS,
        ),
        _ => {
            TIME_RE.is_full_value(field(members, "time"))
                && (kind != "weekly" || whole(field(members, "weekday"), 0, 6))
                && (kind != "monthly" || whole(field(members, "day"), 1, 28))
        }
    };
    valid.then_some(value)
}

/// `exact_schedule`: whether a value is a schedule exactly in its canonical form.
#[must_use]
pub fn exact_schedule(value: &Value) -> bool {
    canonical_schedule(value).is_some()
}

/// `continuous_cap`: a continuous Routine's starts in any rolling 24 hours, `ceil(86400 / gap)`.
#[must_use]
pub const fn continuous_cap(gap: i64) -> i64 {
    (DAY_SECONDS - 1) / gap + 1
}

/// `daily_cap` of a canonical schedule: a continuous one's cap, else one.
#[must_use]
pub fn daily_cap(schedule: &Value) -> i64 {
    match schedule.get("gap").and_then(small_int) {
        Some(gap) if string(field_of(schedule, "kind")) == Some("continuous") => {
            continuous_cap(gap)
        }
        _ => 1,
    }
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

fn line(value: &Value, maximum: usize) -> Option<&str> {
    string(value).filter(|line| {
        (1..=maximum).contains(&text::len(line))
            && text::is_nfc(line)
            && text::strip(line) == *line
            && !line.chars().any(|character| {
                let category = text::category(character);
                category.starts_with('C') || category == "Zl" || category == "Zp"
            })
    })
}

/// `canonical_name`: a Routine's short name: NFC, one line, no control characters, trimmed, 1 to 80 characters.
#[must_use]
pub fn canonical_name(value: &Value) -> Option<&str> {
    line(value, MAX_ROUTINE_NAME_CHARS)
}

/// `canonical_description`: a Routine card's description, 1 to 600 characters on the same rules.
#[must_use]
pub fn canonical_description(value: &Value) -> Option<&str> {
    line(value, MAX_ROUTINE_DESCRIPTION_CHARS)
}

/// `exact_name`.
#[must_use]
pub fn exact_name(value: &Value) -> bool {
    canonical_name(value).is_some()
}

/// `exact_description`.
#[must_use]
pub fn exact_description(value: &Value) -> bool {
    canonical_description(value).is_some()
}

/// `canonical_timezone`: an IANA zone name by its grammar (Team also requires that it loads).
#[must_use]
pub fn canonical_timezone(value: &Value) -> Option<&str> {
    string(value).filter(|zone| TIMEZONE_RE.is_full(zone))
}

/// `zoned`: whether a timezone and its source fit together: a known source names a zone, and none means UTC.
#[must_use]
pub fn zoned(timezone: &Value, source: &Value) -> bool {
    let Some(source) = string(source).filter(|source| TIMEZONE_SOURCES.contains(source)) else {
        return false;
    };
    canonical_timezone(timezone)
        .is_some_and(|zone| source != "none" || zone == CONVENTIONAL_TIMEZONE)
}

/// `valid_assistant`.
#[must_use]
pub fn valid_assistant(value: &Value) -> bool {
    canonical_assistant_id(value).is_some()
}

/// `valid_action`.
#[must_use]
pub fn valid_action(value: &Value) -> bool {
    canonical_action_id(value).is_some()
}

/// `valid_team`.
#[must_use]
pub fn valid_team(value: &Value) -> bool {
    canonical_team_id(value).is_some()
}

/// `matches`: a string that fully matches `pattern`.
#[must_use]
pub fn matches(value: &Value, pattern: &Pattern) -> bool {
    pattern.is_full_value(value)
}

/// `_position`: a step's 1-based position among at most `maximum` steps.
#[must_use]
pub fn position(value: &Value, maximum: i64) -> bool {
    whole(value, 1, maximum)
}

/// `canonical_position`: a call's position among a plan's `steps`.
#[must_use]
pub fn canonical_position<'a>(value: &'a Value, steps: &Value) -> Option<&'a Value> {
    small_int(steps)
        .filter(|steps| (0..=MAX_ROUTINE_STEPS).contains(steps) && position(value, *steps))
        .map(|_| value)
}

/// `encoded_bytes`: the UTF-8 size of a value in the Local API's JSON encoding (sorted, compact, UTF-8).
#[must_use]
pub fn encoded_bytes(value: &Value) -> usize {
    encoded_len(value, CANONICAL)
}

/// `valid_revision`: an integer from 1 below 2^31.
#[must_use]
pub fn valid_revision(value: &Value) -> bool {
    whole(value, 1, (1 << 31) - 1)
}
