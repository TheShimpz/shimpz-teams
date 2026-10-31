//! Canonical Routine forms a recording chat turn's Brain context carries (ADR-0101, ADR-0122):
//! the listing, the rerun work, the selected Routine, and the agent's refinement.

use std::sync::LazyLock;

use strict_json::Value;

use crate::python::datetime::{days_in_month, ymd_to_ord};
use crate::python::json::{Options, dumps, encoded_len};
use crate::python::regex::{MatchError, Pattern, declared};
use crate::python::value::{array, exact_object, field, small_int, string, whole};
use crate::routine::{
    self, MAX_CONTINUOUS_CAP, MAX_MEMBER_CHARS, MAX_ROUTINE_STEPS, MAX_ROUTINES, MAX_STEP_INPUTS,
    OUTPUT_MODES, ROUTINE_ID_RE, SHOWN_MODES,
};

/// A step's plan id.
pub static ROUTINE_STEP_ID_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[a-z][a-z0-9_-]{0,31}\Z"));
const LISTING_FIELDS: [&str; 9] = [
    "routine_id",
    "name",
    "schedule",
    "timezone",
    "timezone_source",
    "revision",
    "daily_steps",
    "output",
    "steps",
];
/// A Routine's Action units in any rolling 24 hours: every start of its whole cap may use every unit.
pub const MAX_LISTED_DAILY_STEPS: i64 = MAX_CONTINUOUS_CAP * MAX_ROUTINE_STEPS;
/// The listed steps' compact UTF-8 JSON of one Routine at most.
pub const MAX_LISTED_STEPS_BYTES: usize = 256 * 1024;
/// The listed steps' compact UTF-8 JSON of the whole listing at most.
pub const MAX_LISTING_STEPS_BYTES: usize = 1024 * 1024;
/// The entries of one rerun at most.
pub const MAX_RERUN_ENTRIES: usize = 256;
/// The calls of one rerun at most.
pub const MAX_RERUN_CALLS: i64 = MAX_ROUTINE_STEPS;
/// The inputs of one rerun entry at most.
pub const MAX_RERUN_INPUTS: usize = MAX_STEP_INPUTS;
/// The characters of one rerun input member name at most.
pub const MAX_RERUN_MEMBER_CHARS: usize = MAX_MEMBER_CHARS;
/// The characters of one rerun value's JSON text at most.
pub const MAX_RERUN_LITERAL_CHARS: usize = 1024;
/// The shortest string Team's recorder takes as an identifier only a result can source.
pub const MIN_IDENTIFIER_CHARS: usize = 6;
/// The fewest digits of a whole number Team's recorder takes as an identifier only a result can source.
pub const MIN_IDENTIFIER_DIGITS: usize = 6;
/// A complete number as written, its sign and exponent included, never a fragment of a longer number or word.
pub static NUMBER_TOKEN_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"(?<![\w.,+-])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?(?!\w|[.,]\d)"));
/// How far from the send's date a written date still counts as each run's own.
pub const MAX_DATE_OFFSET_DAYS: i32 = 31;
/// The unspaced date forms Team's recorder writes again for each run, in Python `re` syntax. The numeric form's
/// separator repeats (`\2` in Python) as one alternative per separator, which reads the same matches with the year as
/// the last group.
pub const UNSPACED_DATE_FORMS: [(&str, &str); 4] = [
    ("iso", r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])"),
    (
        "numeric",
        r"(?<![\d/.])(\d{1,2})(?:/(\d{1,2})/|\.(\d{1,2})\.)(\d{4})(?![\d/]|\.\d)",
    ),
    ("short", r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/]|\.\d)"),
    ("cjk", r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"),
];
static DATE_FORMS: LazyLock<Vec<(&str, Pattern)>> = LazyLock::new(|| {
    UNSPACED_DATE_FORMS
        .iter()
        .map(|(kind, pattern)| (*kind, declared(pattern)))
        .collect()
});
/// A step's outcome in the latest run: its own record's status, or a gap the run's records prove or cannot show.
pub const LAST_RUN_STATUSES: [&str; 7] = [
    "done",
    "recovered",
    "failed",
    "stopped",
    "waiting",
    "not_run",
    "unavailable",
];
/// The selected definition's compact UTF-8 JSON at most.
pub const MAX_SELECTED_BYTES: usize = 256 * 1024;
const SELECTED_FIELDS: [&str; 7] = [
    "routine_id",
    "name",
    "description",
    "revision",
    "output",
    "steps",
    "last_run",
];
/// The kinds of one refinement edit.
pub const REFINE_KINDS: [&str; 3] = ["insert", "replace", "remove"];
/// A traced call of the current send, by its 1-based place among the send's traced calls.
pub static CALL_HANDLE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"c[1-9][0-9]{0,2}\Z"));
/// The edits of one refinement at most.
pub const MAX_REFINE_EDITS: usize = 256;
const REFINE_FIELDS: [&str; 4] = ["routine_id", "base_revision", "description", "edits"];
/// The compact, insertion-ordered, UTF-8 encoding the byte bounds measure.
const COMPACT: Options = Options {
    ensure_ascii: false,
    sort_keys: false,
    compact: true,
};

/// `_members_listed`: at most `limit` input member names, each plain and at most `maximum` characters, sorted, none
/// twice.
fn members_listed(names: &[&Value], limit: usize, maximum: usize) -> bool {
    names.len() <= limit
        && names.iter().all(|name| routine::plain(name, maximum))
        && names
            .windows(2)
            .all(|pair| string(pair[0]) < string(pair[1]))
}

fn member_values(value: &Value) -> Option<Vec<&Value>> {
    array(value).map(|items| items.iter().collect())
}

fn one_of(value: &Value, options: &[&str]) -> bool {
    string(value).is_some_and(|text| options.contains(&text))
}

fn listed_output(value: &Value, total: usize) -> bool {
    exact_object(value, &["mode"]).is_some_and(|members| {
        one_of(field(members, "mode"), &OUTPUT_MODES)
            && (1..=MAX_ROUTINE_STEPS).contains(&i64::try_from(total).unwrap_or(i64::MAX))
    })
}

fn listed_step(value: &Value) -> bool {
    exact_object(value, &["id", "assistant", "action", "inputs"]).is_some_and(|members| {
        routine::matches(field(members, "id"), &ROUTINE_STEP_ID_RE)
            && routine::valid_assistant(field(members, "assistant"))
            && routine::valid_action(field(members, "action"))
            && member_values(field(members, "inputs"))
                .is_some_and(|names| members_listed(&names, MAX_STEP_INPUTS, MAX_MEMBER_CHARS))
    })
}

/// `canonical_routine_listing`: one Routine as the Brain sees it.
#[must_use]
pub fn canonical_routine_listing(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &LISTING_FIELDS)?;
    let steps = array(field(members, "steps"))?;
    let valid = routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::exact_name(field(members, "name"))
        && routine::exact_schedule(field(members, "schedule"))
        && routine::zoned(
            field(members, "timezone"),
            field(members, "timezone_source"),
        )
        && routine::valid_revision(field(members, "revision"))
        && whole(field(members, "daily_steps"), 1, MAX_LISTED_DAILY_STEPS)
        && i64::try_from(steps.len()).unwrap_or(i64::MAX) <= MAX_ROUTINE_STEPS
        && listed_output(field(members, "output"), steps.len())
        && steps.iter().all(listed_step);
    valid.then_some(value)
}

fn steps_bytes(listed: &Value) -> usize {
    listed
        .get("steps")
        .map_or(0, |steps| encoded_len(steps, COMPACT))
}

/// `canonical_routine_listings`: the Team's whole listing, at most `MAX_ROUTINES`, each admitted, none twice, within
/// its byte bounds.
#[must_use]
pub fn canonical_routine_listings(value: &Value) -> Option<&Value> {
    let items = array(value).filter(|items| items.len() <= MAX_ROUTINES)?;
    if !items
        .iter()
        .all(|item| canonical_routine_listing(item).is_some())
    {
        return None;
    }
    let ids: Vec<&str> = items
        .iter()
        .filter_map(|item| item.get("routine_id").and_then(string))
        .collect();
    if (1..ids.len()).any(|index| ids[..index].contains(&ids[index])) {
        return None;
    }
    let sizes: Vec<usize> = items.iter().map(steps_bytes).collect();
    let within = sizes.iter().all(|size| *size <= MAX_LISTED_STEPS_BYTES)
        && sizes.iter().sum::<usize>() <= MAX_LISTING_STEPS_BYTES;
    within.then_some(value)
}

/// One match's groups as (year, month, day) readings: the ISO and CJK forms as written, a numeric form day first or
/// month first, a missing year `None`.
fn dates_written<'a>(
    kind: &str,
    groups: &[Option<&'a str>],
) -> Vec<(Option<&'a str>, &'a str, &'a str)> {
    let group = |index: usize| groups[index].unwrap_or_default();
    match kind {
        "iso" | "cjk" => vec![(groups[0], group(1), group(2))],
        "numeric" => {
            let second = groups[1].or(groups[2]).unwrap_or_default();
            vec![(groups[3], second, group(0)), (groups[3], group(0), second)]
        }
        _ => vec![(None, group(1), group(0)), (None, group(0), group(1))],
    }
}

/// `_near`: whether a written date is a real date within the offset of `today`, a missing year read as the nearest.
fn near(year: Option<&str>, month: &str, day: &str, today: (i32, i32, i32)) -> bool {
    let number = |digits: &str| digits.parse::<i32>().unwrap_or(0);
    let years: Vec<i32> = match year.filter(|year| !year.is_empty()) {
        Some(year) => vec![number(year)],
        None => vec![today.0 - 1, today.0, today.0 + 1],
    };
    let (month, day) = (number(month), number(day));
    let today = ymd_to_ord(today.0, today.1, today.2);
    years.into_iter().any(|year| {
        (1..=9999).contains(&year)
            && (1..=12).contains(&month)
            && (1..=days_in_month(year, month)).contains(&day)
            && (ymd_to_ord(year, month, day) - today).abs() <= MAX_DATE_OFFSET_DAYS + 1
    })
}

/// `may_write_date`: whether a text writes, in an unspaced form of ASCII digits, a date near `today` (a valid
/// `(year, month, day)`) that Team's recorder may read as each run's own.
///
/// # Errors
///
/// [`MatchError`] when a search cannot complete.
pub fn may_write_date(text: &str, today: (i32, i32, i32)) -> Result<bool, MatchError> {
    writes_date(&DATE_FORMS, text, today)
}

/// [`may_write_date`] over these date forms.
fn writes_date(
    forms: &[(&str, Pattern)],
    text: &str,
    today: (i32, i32, i32),
) -> Result<bool, MatchError> {
    for (kind, pattern) in forms {
        for found in pattern.finditer(text)? {
            let groups: Vec<Option<&str>> = (1..=found.groups.len())
                .map(|index| found.group(text, index))
                .collect();
            if groups.iter().flatten().all(|group| group.is_ascii())
                && dates_written(kind, &groups)
                    .into_iter()
                    .any(|(year, month, day)| near(year, month, day, today))
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// `_json_literal`: one value's exact JSON text as Python's default `json.dumps` writes it (spaced separators,
/// insertion order, UTF-8), within its bound. Every text Python's decoder reads that re-encodes to itself is strict
/// JSON, so the strict decoder reads exactly those.
#[must_use]
pub fn json_literal(text: &Value) -> bool {
    string(text).is_some_and(|text| {
        (1..=MAX_RERUN_LITERAL_CHARS).contains(&crate::python::text::len(text))
            && strict_json::parse(text.as_bytes()).is_ok_and(|decoded| {
                dumps(
                    &decoded,
                    Options {
                        ensure_ascii: false,
                        sort_keys: false,
                        compact: false,
                    },
                ) == text
            })
    })
}

fn rerun_source(value: &Value) -> bool {
    exact_object(value, &["assistant", "action"]).is_some_and(|members| {
        routine::valid_assistant(field(members, "assistant"))
            && routine::valid_action(field(members, "action"))
    })
}

fn rerun_input(value: &Value) -> bool {
    let Some(members) = exact_object(value, &["member", "kind", "value", "chosen", "source"])
    else {
        return false;
    };
    let (shown, chosen, source) = (
        field(members, "value"),
        field(members, "chosen"),
        field(members, "source"),
    );
    let valid = match string(field(members, "kind")) {
        Some("value") => {
            (*shown == Value::Null || json_literal(shown))
                && matches!(chosen, Value::Bool(_))
                && *source == Value::Null
        }
        Some("fresh") => {
            *shown == Value::Null
                && *chosen == Value::Bool(false)
                && (*source == Value::Null || rerun_source(source))
        }
        kind => {
            kind == Some("clock")
                && *shown == Value::Null
                && *chosen == Value::Bool(false)
                && *source == Value::Null
        }
    };
    valid && routine::plain(field(members, "member"), MAX_RERUN_MEMBER_CHARS)
}

fn rerun_entry(value: &Value) -> bool {
    let Some(members) = exact_object(value, &["assistant", "action", "count", "inputs"]) else {
        return false;
    };
    routine::valid_assistant(field(members, "assistant"))
        && routine::valid_action(field(members, "action"))
        && whole(field(members, "count"), 1, MAX_RERUN_CALLS)
        && array(field(members, "inputs")).is_some_and(|inputs| {
            let names: Vec<&Value> = inputs.iter().map(|item| field_of(item, "member")).collect();
            inputs.len() <= MAX_RERUN_INPUTS
                && inputs.iter().all(rerun_input)
                && members_listed(&names, MAX_RERUN_INPUTS, MAX_RERUN_MEMBER_CHARS)
        })
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// `canonical_rerun`: the work a rerun question asks the Brain to repeat, in order, within its bounds.
#[must_use]
pub fn canonical_rerun(value: &Value) -> Option<&Value> {
    let entries =
        array(value).filter(|entries| (1..=MAX_RERUN_ENTRIES).contains(&entries.len()))?;
    let calls: i64 = entries
        .iter()
        .map(|entry| small_int(field_of(entry, "count")).unwrap_or(0))
        .sum();
    (entries.iter().all(rerun_entry) && calls <= MAX_RERUN_CALLS).then_some(value)
}

/// `_selected_step`: one step at `position`, its id not an earlier step's, beside its plan projection.
fn selected_step(value: &Value, position: i64, earlier: &[&str]) -> bool {
    let Some(members) = exact_object_any(value) else {
        return false;
    };
    let id = field_of(value, "id");
    let projection = Value::Object(
        members
            .iter()
            .filter(|(key, _)| key != "id")
            .cloned()
            .collect(),
    );
    routine::matches(id, &ROUTINE_STEP_ID_RE)
        && string(id).is_some_and(|id| !earlier.contains(&id))
        && routine::canonical_step(&projection, position).is_some()
}

fn exact_object_any(value: &Value) -> Option<&[(String, Value)]> {
    match value {
        Value::Object(members) => Some(members),
        _ => None,
    }
}

/// `_last_run`: the latest run's outcome at each position of the revision it executed, from 1, or null.
fn last_run(value: &Value) -> bool {
    if *value == Value::Null {
        return true;
    }
    let Some(members) = exact_object(value, &["revision", "steps"]) else {
        return false;
    };
    routine::valid_revision(field(members, "revision"))
        && array(field(members, "steps")).is_some_and(|steps| {
            (1..=usize::try_from(MAX_ROUTINE_STEPS).unwrap_or(usize::MAX)).contains(&steps.len())
                && steps.iter().zip(1_i64..).all(|(item, position)| {
                    exact_object(item, &["position", "status"]).is_some_and(|members| {
                        small_int(field(members, "position")) == Some(position)
                            && one_of(field(members, "status"), &LAST_RUN_STATUSES)
                    })
                })
        })
}

/// The ids of a selected Routine's steps, each admitted at its position, or `None` at the first that is not.
fn selected_ids(steps: &Value) -> Option<Vec<&str>> {
    let mut ids: Vec<&str> = Vec::new();
    for (item, position) in array(steps).unwrap_or_default().iter().zip(1_i64..) {
        if !selected_step(item, position, &ids) {
            return None;
        }
        ids.push(string(field_of(item, "id")).unwrap_or_default());
    }
    Some(ids)
}

fn selected_output(output: &Value, ids: &[&str]) -> bool {
    exact_object(output, &["mode", "step"]).is_some_and(|members| {
        let mode = field(members, "mode");
        one_of(mode, &OUTPUT_MODES)
            && *field(members, "step")
                == if one_of(mode, &SHOWN_MODES) {
                    ids.last()
                        .map_or(Value::Null, |id| Value::String((*id).to_owned()))
                } else {
                    Value::Null
                }
    })
}

/// `canonical_selected_routine`: the selected Routine's definition as the Brain sees it, within its byte bound.
#[must_use]
pub fn canonical_selected_routine(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &SELECTED_FIELDS)?;
    let steps = field(members, "steps");
    let ids = selected_ids(steps)?;
    let count = array(steps).map_or(0, <[Value]>::len);
    let valid = routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::exact_name(field(members, "name"))
        && routine::exact_description(field(members, "description"))
        && routine::valid_revision(field(members, "revision"))
        && (1..=usize::try_from(MAX_ROUTINE_STEPS).unwrap_or(usize::MAX)).contains(&count)
        && selected_output(field(members, "output"), &ids)
        && last_run(field(members, "last_run"))
        && encoded_len(value, COMPACT) <= MAX_SELECTED_BYTES;
    valid.then_some(value)
}

fn edit(value: &Value) -> bool {
    let Some(members) = exact_object(value, &["kind", "step", "calls"]) else {
        return false;
    };
    let kind = string(field(members, "kind")).filter(|kind| REFINE_KINDS.contains(kind));
    let (Some(kind), Some(calls)) = (kind, array(field(members, "calls"))) else {
        return false;
    };
    let counted = match kind {
        "insert" => {
            (1..=usize::try_from(MAX_ROUTINE_STEPS).unwrap_or(usize::MAX)).contains(&calls.len())
        }
        "replace" => calls.len() == 1,
        _ => calls.is_empty(),
    };
    routine::matches(field(members, "step"), &ROUTINE_STEP_ID_RE)
        && calls
            .iter()
            .all(|call| routine::matches(call, &CALL_HANDLE_RE))
        && counted
}

/// `canonical_edits`: a refinement's edits, at most one per step and each call named once, in their given order.
#[must_use]
pub fn canonical_edits(value: &Value) -> Option<&Value> {
    let edits =
        array(value).filter(|edits| edits.len() <= MAX_REFINE_EDITS && edits.iter().all(edit))?;
    let steps: Vec<&str> = edits
        .iter()
        .filter_map(|item| string(field_of(item, "step")))
        .collect();
    let calls: Vec<&str> = edits
        .iter()
        .flat_map(|item| array(field_of(item, "calls")).unwrap_or_default())
        .filter_map(string)
        .collect();
    (distinct(&steps) && distinct(&calls)).then_some(value)
}

fn distinct(names: &[&str]) -> bool {
    let mut sorted = names.to_vec();
    sorted.sort_unstable();
    sorted.windows(2).all(|pair| pair[0] != pair[1])
}

/// `canonical_refine`: the agent's refinement: the Routine, the revision it edits, the description, and edits.
#[must_use]
pub fn canonical_refine(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &REFINE_FIELDS)?;
    let valid = routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::valid_revision(field(members, "base_revision"))
        && routine::exact_description(field(members, "description"))
        && canonical_edits(field(members, "edits")).is_some();
    valid.then_some(value)
}

#[cfg(test)]
mod tests;
