//! Canonical Team Routine run forms (`routine_run.py`, ADR-0092, ADR-0101): recovery cards, claims, diagnostics, and
//! run steps.

use std::collections::HashSet;
use std::sync::LazyLock;

use strict_json::Value;

use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{
    array, exact_object, field, int, natural_digits, object_of, py_eq, small_int, string,
    text as text_value,
};
use crate::routine::{
    self, LOCALES, MAX_MEMBER_CHARS, MAX_PREVIEW_CHARS, MAX_ROUTINE_STEPS, MAX_STEP_VIEW_BYTES,
    PLAN_DIGEST_RE, ROUTINE_ID_RE,
};
use crate::routine_notice::{LEASE_TOKEN_RE, instant};

/// A recovery card's entity tag.
pub static TAG_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{64}\Z"));
/// Whether the held call's failure has a diagnostic: recorded, none kept, or unreadable.
pub const CARD_EVIDENCE: [&str; 3] = ["recorded", "absent", "unavailable"];
/// A run is long past this active time.
pub const SHORT_ACTIVE_SECONDS: i64 = 600;
/// The most active time any run may spend.
pub const MAX_ACTIVE_SECONDS: i64 = 7200;
/// How a run stands once a segment or a resuming answer returns, sorted.
pub const SEGMENT_STATUSES: [&str; 7] = [
    "denied",
    "done",
    "failed",
    "frozen",
    "held",
    "recovered",
    "stopped",
];
/// One notice acknowledgment admits at most this many deliveries.
pub const MAX_ACK_DELIVERIES: usize = 256;
/// The most diagnostics of one run.
pub const MAX_RUN_DIAGNOSTICS: usize = 32;
/// The most attempts of one operation.
pub const MAX_DIAGNOSTIC_ATTEMPTS: i64 = 64;
/// The longest diagnostic text, in UTF-8 bytes.
pub const MAX_DIAGNOSTIC_TEXT_BYTES: usize = 2048;
/// A logical operation's id: a canonical lowercase UUID version 4.
pub static OPERATION_ID_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
});
/// A failure's real type.
pub static ERROR_TYPE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[!-~]{1,128}\Z"));
/// A provider host.
pub static PROVIDER_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
});
/// The closed safe transport conditions.
pub static CONDITION_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(
        r"(?:exit-status:-?[0-9]{1,10}|stderr-output|timeout|frame-invalid|exit-unavailable|transport-failed)\Z",
    )
});
/// The statuses of a run step that ran.
pub const RUN_STEP_STATUSES: [&str; 5] = ["done", "recovered", "failed", "stopped", "waiting"];
/// The statuses of a run step that did not run or cannot be shown.
pub const RUN_STEP_GAPS: [&str; 2] = ["not_run", "unavailable"];
/// A run's records snapshot.
pub static SNAPSHOT_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));
/// A run step's fields, sorted.
pub const RUN_STEP_FIELDS: [&str; 7] = [
    "action",
    "assistant_id",
    "attempt",
    "duration_ms",
    "inputs",
    "position",
    "status",
];

fn among(value: &Value, options: &[&str]) -> bool {
    string(value).is_some_and(|text| options.contains(&text))
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// `_card_evidence`: the held operation's latest diagnostic, exactly when one is recorded, and only of the card's call.
fn card_evidence(members: &[(String, Value)]) -> bool {
    let (evidence, diagnostic) = (field(members, "evidence"), field(members, "diagnostic"));
    if !among(evidence, &CARD_EVIDENCE)
        || (*diagnostic == Value::Null) == (string(evidence) == Some("recorded"))
    {
        return false;
    }
    if *diagnostic == Value::Null {
        return true;
    }
    canonical_diagnostic(diagnostic).is_some_and(|admitted| {
        ["assistant_id", "action", "position"]
            .iter()
            .all(|key| py_eq(field_of(admitted, key), field(members, key)))
    })
}

/// `canonical_card`: an opened recovery card: the call it stopped at, its failure as recorded, and its entity tag.
#[must_use]
pub fn canonical_card(value: &Value) -> Option<&Value> {
    let fields = [
        "team_id",
        "run_id",
        "assistant_id",
        "action",
        "tag",
        "position",
        "steps",
        "evidence",
        "diagnostic",
    ];
    let members = exact_object(value, &fields)?;
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && routine::valid_assistant(field(members, "assistant_id"))
        && routine::valid_action(field(members, "action"))
        && routine::canonical_position(field(members, "position"), field(members, "steps"))
            .is_some()
        && card_evidence(members)
        && routine::matches(field(members, "tag"), &TAG_RE);
    valid.then_some(value)
}

/// `canonical_card_answer_request`: a person's Rodar on one card: the tag it was opened with.
#[must_use]
pub fn canonical_card_answer_request(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["tag"])?;
    let tag = field(members, "tag");
    routine::matches(tag, &TAG_RE).then(|| object_of(vec![("tag", tag.clone())]))
}

/// `canonical_card_answer`: what an answer did: the Team and the run it set aside.
#[must_use]
pub fn canonical_card_answer(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["team_id", "run_id"])?;
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE);
    valid.then_some(value)
}

/// `canonical_challenge_open`: opening a frozen run's challenge names the Admin interface language.
#[must_use]
pub fn canonical_challenge_open(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["locale"])?;
    let locale = string(field(members, "locale")).filter(|locale| LOCALES.contains(locale))?;
    Some(object_of(vec![("locale", text_value(locale))]))
}

/// `canonical_claim_request`: Admin's claim says only whether it can take a long run now.
#[must_use]
pub fn canonical_claim_request(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["long"])?;
    let long = field(members, "long");
    matches!(long, Value::Bool(_)).then(|| object_of(vec![("long", long.clone())]))
}

/// `active_seconds`: the active execution time a run of `units` units may spend, `min(360 + 30 * units, 7200)`.
#[must_use]
pub fn active_seconds(units: i64) -> i64 {
    let seconds = units.saturating_mul(30).saturating_add(360);
    seconds.min(MAX_ACTIVE_SECONDS)
}

fn positive(value: &Value) -> bool {
    natural_digits(value).is_some_and(|digits| digits != "0")
}

fn claimed_run(run: &Value) -> bool {
    let fields = [
        "team_id",
        "run_id",
        "lease_token",
        "active_seconds",
        "revision",
        "plan_digest",
    ];
    exact_object(run, &fields).is_some_and(|members| {
        small_int(field(members, "active_seconds"))
            .is_some_and(|seconds| 0 < seconds && seconds <= MAX_ACTIVE_SECONDS)
            && routine::valid_team(field(members, "team_id"))
            && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
            && routine::matches(field(members, "lease_token"), &LEASE_TOKEN_RE)
            && routine::valid_revision(field(members, "revision"))
            && routine::matches(field(members, "plan_digest"), &PLAN_DIGEST_RE)
    })
}

/// `canonical_claim`: one claimed run with its lease token, or none and a wake hint.
#[must_use]
pub fn canonical_claim(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["run", "next_due_at"])?;
    let (run, hint) = (field(members, "run"), field(members, "next_due_at"));
    if *run == Value::Null {
        return (*hint == Value::Null || positive(hint))
            .then(|| object_of(vec![("run", Value::Null), ("next_due_at", hint.clone())]));
    }
    (*hint == Value::Null && claimed_run(run)).then(|| value.clone())
}

/// `canonical_segment_request`: exactly the revision and plan digest its claim named.
#[must_use]
pub fn canonical_segment_request(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["revision", "plan_digest"])?;
    let (revision, digest) = (field(members, "revision"), field(members, "plan_digest"));
    (routine::valid_revision(revision) && PLAN_DIGEST_RE.is_full_value(digest)).then(|| {
        object_of(vec![
            ("revision", revision.clone()),
            ("plan_digest", digest.clone()),
        ])
    })
}

/// `canonical_segment_answer`: a segment's Team, run, and how the run stands.
#[must_use]
pub fn canonical_segment_answer(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["team_id", "run_id", "status"])?;
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && among(field(members, "status"), &SEGMENT_STATUSES);
    valid.then_some(value)
}

/// `_UNSAFE_TEXT_RE`: every control but tab and line feed, bidi overrides and isolates, and zero-width formatting.
const fn unsafe_text(point: u32) -> bool {
    matches!(
        point,
        0x00..=0x08 | 0x0b..=0x1f | 0x7f..=0x9f | 0x200b..=0x200f | 0x202a..=0x202e | 0x2060..=0x206f | 0xfeff
    )
}

fn diagnostic_text(value: &Value) -> bool {
    string(value).is_some_and(|text| {
        !text
            .chars()
            .any(|character| unsafe_text(u32::from(character)))
            && text.len() <= MAX_DIAGNOSTIC_TEXT_BYTES
    })
}

/// `canonical_failure`: one sanitized handled failure: type, message, provider, status, excerpt, and both flags.
#[must_use]
pub fn canonical_failure(value: &Value) -> Option<&Value> {
    let fields = [
        "error_type",
        "message",
        "provider",
        "http_status",
        "response_excerpt",
        "redacted",
        "truncated",
    ];
    let members = exact_object(value, &fields)?;
    let (provider, status, excerpt) = (
        field(members, "provider"),
        field(members, "http_status"),
        field(members, "response_excerpt"),
    );
    let valid = routine::matches(field(members, "error_type"), &ERROR_TYPE_RE)
        && diagnostic_text(field(members, "message"))
        && (*provider == Value::Null
            || (routine::matches(provider, &PROVIDER_RE)
                && string(provider).is_some_and(|host| text::len(host) <= 253)))
        && (*status == Value::Null
            || small_int(status).is_some_and(|code| (100..=599).contains(&code)))
        && (*excerpt == Value::Null || diagnostic_text(excerpt))
        && matches!(field(members, "redacted"), Value::Bool(_))
        && matches!(field(members, "truncated"), Value::Bool(_));
    valid.then_some(value)
}

/// `canonical_diagnostic`: one attempt's diagnostic: exactly one of a sanitized failure or a safe transport condition.
#[must_use]
pub fn canonical_diagnostic(value: &Value) -> Option<&Value> {
    let fields = [
        "operation_id",
        "attempt",
        "assistant_id",
        "action",
        "position",
        "recorded_at",
        "failure",
        "condition",
    ];
    let members = exact_object(value, &fields)?;
    let (failure, condition) = (field(members, "failure"), field(members, "condition"));
    let valid = routine::matches(field(members, "operation_id"), &OPERATION_ID_RE)
        && small_int(field(members, "attempt"))
            .is_some_and(|attempt| (1..=MAX_DIAGNOSTIC_ATTEMPTS).contains(&attempt))
        && routine::valid_assistant(field(members, "assistant_id"))
        && routine::valid_action(field(members, "action"))
        && routine::canonical_position(field(members, "position"), &int(MAX_ROUTINE_STEPS))
            .is_some()
        && instant(field(members, "recorded_at"))
        && (*failure == Value::Null) != (*condition == Value::Null)
        && (*failure == Value::Null || canonical_failure(failure).is_some())
        && (*condition == Value::Null || routine::matches(condition, &CONDITION_RE));
    valid.then_some(value)
}

/// One admitted diagnostic's ordering key: when it was recorded, its operation, and its attempt.
fn diagnostic_key(diagnostic: &Value) -> (&str, &str, Option<i64>) {
    (
        string(field_of(diagnostic, "recorded_at")).unwrap_or_default(),
        string(field_of(diagnostic, "operation_id")).unwrap_or_default(),
        small_int(field_of(diagnostic, "attempt")),
    )
}

/// `canonical_diagnostics`: a run's diagnostics, oldest first, each attempt of each operation at most once.
#[must_use]
pub fn canonical_diagnostics(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["team_id", "run_id", "diagnostics"])?;
    let entries = array(field(members, "diagnostics"))?;
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && entries.len() <= MAX_RUN_DIAGNOSTICS
        && entries
            .iter()
            .all(|entry| canonical_diagnostic(entry).is_some());
    if !valid {
        return None;
    }
    let keys: Vec<(&str, &str, Option<i64>)> = entries.iter().map(diagnostic_key).collect();
    let unique: HashSet<(&str, Option<i64>)> = keys.iter().map(|key| (key.1, key.2)).collect();
    (unique.len() == entries.len() && keys.is_sorted()).then_some(value)
}

/// `_run_input`: one input as a run shows it: its member and its redacted preview, or null.
fn run_input(value: &Value) -> bool {
    exact_object(value, &["member", "value"]).is_some_and(|members| {
        let preview = field(members, "value");
        routine::plain(field(members, "member"), MAX_MEMBER_CHARS)
            && (*preview == Value::Null || routine::plain(preview, MAX_PREVIEW_CHARS))
    })
}

fn ran(members: &[(String, Value)]) -> bool {
    let (status, duration, inputs) = (
        field(members, "status"),
        field(members, "duration_ms"),
        field(members, "inputs"),
    );
    among(status, &RUN_STEP_STATUSES)
        && routine::valid_assistant(field(members, "assistant_id"))
        && routine::valid_action(field(members, "action"))
        && small_int(field(members, "attempt"))
            .is_some_and(|attempt| (1..=MAX_DIAGNOSTIC_ATTEMPTS).contains(&attempt))
        && (*duration == Value::Null
            || small_int(duration).is_some_and(|duration| (0..1 << 53).contains(&duration)))
        && (string(status) != Some("recovered") || *duration == Value::Null)
        && (*inputs == Value::Null || routine::members(inputs, run_input))
}

/// `canonical_run_step`: one entry at `position`: what its attempt did, or only that it never ran or cannot be shown.
#[must_use]
pub fn canonical_run_step<'a>(value: &'a Value, position: i64, steps: &Value) -> Option<&'a Value> {
    let members = exact_object(value, &RUN_STEP_FIELDS)?;
    if !py_eq(field(members, "position"), &int(position)) {
        return None;
    }
    let valid = if among(field(members, "status"), &RUN_STEP_GAPS) {
        ["assistant_id", "action", "attempt", "duration_ms", "inputs"]
            .iter()
            .all(|key| *field(members, key) == Value::Null)
    } else {
        ran(members)
    };
    let valid = valid
        && routine::canonical_position(field(members, "position"), steps).is_some()
        && routine::encoded_bytes(value) <= MAX_STEP_VIEW_BYTES;
    valid.then_some(value)
}

/// `canonical_run_steps`: one page of a run's entries from `offset`: whole consecutive steps of its revision's run.
#[must_use]
pub fn canonical_run_steps(value: &Value) -> Option<&Value> {
    let fields = [
        "team_id",
        "run_id",
        "routine_id",
        "revision",
        "plan_digest",
        "total",
        "snapshot",
        "offset",
        "steps",
        "next",
    ];
    let members = exact_object(value, &fields)?;
    let total = field(members, "total");
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "run_id"), &ROUTINE_ID_RE)
        && routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::valid_revision(field(members, "revision"))
        && routine::matches(field(members, "plan_digest"), &PLAN_DIGEST_RE)
        && routine::matches(field(members, "snapshot"), &SNAPSHOT_RE)
        && routine::paged(members, |item, at| {
            canonical_run_step(item, at, total).is_some()
        });
    valid.then_some(value)
}
