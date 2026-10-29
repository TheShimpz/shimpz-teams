//! Canonical recorded-Routine card forms (`routine_proposal.py`, ADR-0101): the card, Team's questions, refusals, and
//! answers.

use std::collections::HashSet;

use strict_json::{Number, Value};

use crate::python::json::{Options, dumps};
use crate::python::text;
use crate::python::value::{array, exact_object, field, object_of, small_int, string, whole};
use crate::routine::{
    self, ERROR_CODE_RE, MAX_CONTINUOUS_GAP_SECONDS, MAX_MEMBER_CHARS, MAX_ROUTINE_STEPS,
    MIN_CONTINUOUS_GAP_SECONDS, OUTPUT_MODES, ROUTINE_ID_RE, plan_unsafe,
};
use crate::routine_notice::{instant, optional};

/// The encoded confirmation card of a recorded Routine at most.
pub const MAX_PROPOSAL_BYTES: usize = 160 * 1024;
/// Where a card input's value comes from.
pub const INPUT_ORIGINS: [&str; 5] = ["request", "assistant", "clock", "step", "selector"];
/// A refined step's change.
pub const STEP_CHANGES: [&str; 3] = ["kept", "added", "replaced"];
/// A removed base step's change.
pub const REMOVED_CHANGES: [&str; 2] = ["removed", "replaced"];
/// The questions Team asks before a card.
pub const QUESTION_CODES: [&str; 5] = [
    "routine-schedule-unstated",
    "routine-output-unstated",
    "routine-interval-over-budget",
    "routine-binding-ambiguous",
    "routine-work-rerun",
];
/// The most targets of one question.
pub const MAX_QUESTION_OPTIONS: usize = 8;
/// The longest target name or string target.
pub const MAX_QUESTION_OPTION_CHARS: usize = 120;
/// A target's exact JSON text at most: a string escapes at most its quotes and backslashes.
pub const MAX_QUESTION_VALUE_CHARS: usize = 2 * MAX_QUESTION_OPTION_CHARS + 2;
/// What each run does with its result, as each interface language labels the choices, in output-mode order.
pub const OUTPUT_CHOICES: [(&str, [(&str, &str); 3]); 8] = [
    (
        "ar",
        [
            ("show", "اعرض في كل تشغيل"),
            ("changes", "اعرض فقط عند التغيير"),
            ("none", "لا تعرض"),
        ],
    ),
    (
        "de",
        [
            ("show", "Bei jedem Lauf anzeigen"),
            ("changes", "Nur bei Änderung anzeigen"),
            ("none", "Nicht anzeigen"),
        ],
    ),
    (
        "en",
        [
            ("show", "Show every run"),
            ("changes", "Show only when it changes"),
            ("none", "Don't show"),
        ],
    ),
    (
        "es",
        [
            ("show", "Mostrar en cada ejecución"),
            ("changes", "Mostrar solo cuando cambie"),
            ("none", "No mostrar"),
        ],
    ),
    (
        "fr",
        [
            ("show", "Afficher à chaque exécution"),
            ("changes", "Afficher seulement en cas de changement"),
            ("none", "Ne pas afficher"),
        ],
    ),
    (
        "ja",
        [
            ("show", "毎回表示する"),
            ("changes", "変更時のみ表示する"),
            ("none", "表示しない"),
        ],
    ),
    (
        "pt",
        [
            ("show", "Mostrar em todas as execuções"),
            ("changes", "Mostrar somente quando mudar"),
            ("none", "Não mostrar"),
        ],
    ),
    (
        "zh",
        [
            ("show", "每次运行都显示"),
            ("changes", "仅在变化时显示"),
            ("none", "不显示"),
        ],
    ),
];
/// The schedules Admin offers when Team asks how often a Routine runs, in each interface language.
pub const SCHEDULE_CHOICES: [(&str, [&str; 3]); 8] = [
    ("ar", ["كل 30 ثانية", "كل ساعة", "كل يوم الساعة 09:00"]),
    (
        "de",
        ["Alle 30 Sekunden", "Jede Stunde", "Jeden Tag um 09:00"],
    ),
    (
        "en",
        ["Every 30 seconds", "Every hour", "Every day at 09:00"],
    ),
    (
        "es",
        [
            "Cada 30 segundos",
            "Cada hora",
            "Todos los días a las 09:00",
        ],
    ),
    (
        "fr",
        [
            "Toutes les 30 secondes",
            "Toutes les heures",
            "Tous les jours à 09:00",
        ],
    ),
    ("ja", ["30 秒ごと", "1 時間ごと", "毎日 09:00"]),
    (
        "pt",
        ["A cada 30 segundos", "A cada hora", "Todo dia às 09:00"],
    ),
    ("zh", ["每 30 秒", "每小时", "每天 09:00"]),
];
/// The interval Admin offers when the stated one outgrows the Team's budget; `{seconds}` is the question's value.
pub const OVER_BUDGET_ANSWERS: [(&str, &str); 8] = [
    ("ar", "كل {seconds} ثانية"),
    ("de", "Alle {seconds} Sekunden"),
    ("en", "Every {seconds} seconds"),
    ("es", "Cada {seconds} segundos"),
    ("fr", "Toutes les {seconds} secondes"),
    ("ja", "{seconds} 秒ごと"),
    ("pt", "A cada {seconds} segundos"),
    ("zh", "每 {seconds} 秒"),
];
/// The questions about how often the Routine runs and what each run does with its result, sorted.
pub const SETTINGS_QUESTION_CODES: [&str; 3] = [
    "routine-interval-over-budget",
    "routine-output-unstated",
    "routine-schedule-unstated",
];
/// A person's answer to a card: confirming it creates or changes the Routine; revoking it drops the card.
pub const PROPOSAL_STATUSES: [&str; 3] = ["created", "changed", "revoked"];

/// `SETTINGS_ANSWERS[code]`: every interface language's fixed answers Admin offers to one settings question, or `None`
/// for any other question.
#[must_use]
pub fn settings_answers(code: &str) -> Option<Vec<&'static str>> {
    match code {
        "routine-schedule-unstated" => Some(
            SCHEDULE_CHOICES
                .iter()
                .flat_map(|(_, labels)| *labels)
                .collect(),
        ),
        "routine-interval-over-budget" => Some(
            OVER_BUDGET_ANSWERS
                .iter()
                .map(|(_, answer)| *answer)
                .collect(),
        ),
        "routine-output-unstated" => Some(
            OUTPUT_CHOICES
                .iter()
                .flat_map(|(_, labels)| labels.map(|(_, label)| label))
                .collect(),
        ),
        _ => None,
    }
}

fn among(value: &Value, options: &[&str]) -> bool {
    string(value).is_some_and(|text| options.contains(&text))
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// A literal or template text: a string without a control or invisible character.
fn safe(value: &Value) -> Option<&str> {
    string(value).filter(|text| {
        !text
            .chars()
            .any(|character| plan_unsafe(u32::from(character)))
    })
}

fn nulls(members: &[(String, Value)], keys: &[&str]) -> bool {
    keys.iter().all(|key| *field(members, key) == Value::Null)
}

/// `_card_input`: one input as the card shows it: a literal's text, the run date or a dated text, or its source.
fn card_input(value: &Value, position: i64) -> bool {
    let fields = [
        "member", "origin", "value", "step", "pointer", "where", "item",
    ];
    let Some(members) = exact_object(value, &fields) else {
        return false;
    };
    let origin = string(field(members, "origin")).filter(|origin| INPUT_ORIGINS.contains(origin));
    let Some(origin) = origin else {
        return false;
    };
    if !routine::plain(field(members, "member"), MAX_MEMBER_CHARS) {
        return false;
    }
    let shown = field(members, "value");
    let unreferenced = nulls(members, &["step", "pointer", "where", "item"]);
    match origin {
        "request" | "assistant" => safe(shown).is_some() && unreferenced,
        "clock" => {
            (*shown == Value::Null || safe(shown).is_some_and(|text| text.contains('{')))
                && unreferenced
        }
        _ => {
            *shown == Value::Null
                && (*field(members, "where") != Value::Null) == (origin == "selector")
                && routine::reference(members, position)
        }
    }
}

/// The checks of `_card_step` over a step's card members, whatever other members it has.
fn card_members(members: &[(String, Value)], position: i64) -> bool {
    small_int(field(members, "position")) == Some(position)
        && routine::valid_assistant(field(members, "assistant"))
        && routine::valid_action(field(members, "action"))
        && matches!(field(members, "read_only"), Value::Bool(_))
        && routine::members(field(members, "inputs"), |item| card_input(item, position))
}

const CARD_STEP_FIELDS: [&str; 5] = ["position", "assistant", "action", "read_only", "inputs"];

fn card_step(value: &Value, position: i64) -> bool {
    exact_object(value, &CARD_STEP_FIELDS).is_some_and(|members| card_members(members, position))
}

/// The steps of a card: 1 to 256, each admitted at its position from 1.
fn steps_of(value: &Value, step: impl Fn(&Value, i64) -> bool) -> bool {
    array(value).is_some_and(|steps| {
        !steps.is_empty()
            && i64::try_from(steps.len()).is_ok_and(|count| count <= MAX_ROUTINE_STEPS)
            && steps
                .iter()
                .zip(1..)
                .all(|(item, position)| step(item, position))
    })
}

/// The members a card and a refinement card share: name, description, schedule, zone, and output.
fn settings(members: &[(String, Value)]) -> bool {
    routine::exact_name(field(members, "name"))
        && routine::exact_description(field(members, "description"))
        && routine::exact_schedule(field(members, "schedule"))
        && routine::zoned(
            field(members, "timezone"),
            field(members, "timezone_source"),
        )
        && among(field(members, "output"), &OUTPUT_MODES)
}

/// `canonical_proposal`: one recorded Routine's confirmation card, within its byte bound.
#[must_use]
pub fn canonical_proposal(value: &Value) -> Option<&Value> {
    let fields = [
        "proposal_id",
        "expires_at",
        "replaces",
        "name",
        "description",
        "schedule",
        "timezone",
        "timezone_source",
        "output",
        "steps",
    ];
    let members = exact_object(value, &fields)?;
    let valid = routine::matches(field(members, "proposal_id"), &ROUTINE_ID_RE)
        && instant(field(members, "expires_at"))
        && optional(field(members, "replaces"), &ROUTINE_ID_RE)
        && settings(members)
        && steps_of(field(members, "steps"), card_step)
        && routine::encoded_bytes(value) <= MAX_PROPOSAL_BYTES;
    valid.then_some(value)
}

/// `_refined_step`: a step of the resulting plan with its change and the position it had in the base plan.
fn refined_step(value: &Value, position: i64) -> bool {
    let fields = [
        "position",
        "assistant",
        "action",
        "read_only",
        "inputs",
        "change",
        "base",
    ];
    exact_object(value, &fields).is_some_and(|members| {
        let (change, base) = (field(members, "change"), field(members, "base"));
        among(change, &STEP_CHANGES)
            && if string(change) == Some("added") {
                *base == Value::Null
            } else {
                whole(base, 1, MAX_ROUTINE_STEPS)
            }
            && card_members(members, position)
    })
}

/// `_removed_step`: a base step that does not continue, with its effect.
fn removed_step(value: &Value) -> bool {
    let fields = ["position", "assistant", "action", "read_only", "change"];
    exact_object(value, &fields).is_some_and(|members| {
        whole(field(members, "position"), 1, MAX_ROUTINE_STEPS)
            && routine::valid_assistant(field(members, "assistant"))
            && routine::valid_action(field(members, "action"))
            && matches!(field(members, "read_only"), Value::Bool(_))
            && among(field(members, "change"), &REMOVED_CHANGES)
    })
}

/// The integer members `key` of the items whose `change` member is `change`, in order.
fn positions(items: &[Value], change: &str, key: &str) -> Vec<i64> {
    items
        .iter()
        .filter(|item| string(field_of(item, "change")) == Some(change))
        .filter_map(|item| small_int(field_of(item, key)))
        .collect()
}

/// `_base_positions`: every base step once: a kept one where it was, in order, a replaced one beside its replacement,
/// else removed.
fn base_positions(steps: &[Value], removed: &[Value]) -> bool {
    let kept = positions(steps, "kept", "base");
    let mut replaced = positions(steps, "replaced", "base");
    replaced.sort_unstable();
    let gone: Vec<i64> = removed
        .iter()
        .filter_map(|item| small_int(field_of(item, "position")))
        .collect();
    let mut all: Vec<i64> = kept.iter().chain(&gone).copied().collect();
    all.sort_unstable();
    kept.is_sorted()
        && gone.is_sorted()
        && all
            .iter()
            .copied()
            .eq(1..=i64::try_from(all.len()).unwrap_or(i64::MAX))
        && replaced == positions(removed, "replaced", "position")
}

const REFINEMENT_FIELDS: [&str; 13] = [
    "proposal_id",
    "expires_at",
    "routine_id",
    "base_revision",
    "name",
    "description",
    "schedule",
    "timezone",
    "timezone_source",
    "output",
    "last_changed",
    "steps",
    "removed",
];

/// `canonical_refinement`: one refinement's confirmation card, within the card's byte bound.
#[must_use]
pub fn canonical_refinement(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &REFINEMENT_FIELDS)?;
    let (steps, removed) = (field(members, "steps"), field(members, "removed"));
    let (Some(step_items), Some(removed_items)) = (array(steps), array(removed)) else {
        return None;
    };
    let valid = routine::matches(field(members, "proposal_id"), &ROUTINE_ID_RE)
        && instant(field(members, "expires_at"))
        && routine::matches(field(members, "routine_id"), &ROUTINE_ID_RE)
        && routine::valid_revision(field(members, "base_revision"))
        && settings(members)
        && matches!(field(members, "last_changed"), Value::Bool(_))
        && steps_of(steps, refined_step)
        && removed_items.iter().all(removed_step)
        && base_positions(step_items, removed_items)
        && routine::encoded_bytes(value) <= MAX_PROPOSAL_BYTES;
    valid.then_some(value)
}

/// `_target`: a target's exact JSON text: one string or integer, as compact JSON writes it, so no client rounds it.
fn target(value: &Value) -> bool {
    let Some(text) =
        string(value).filter(|text| (1..=MAX_QUESTION_VALUE_CHARS).contains(&text::len(text)))
    else {
        return false;
    };
    let Ok(decoded) = strict_json::parse(text.as_bytes()) else {
        return false;
    };
    let scalar = match &decoded {
        Value::String(_) => routine::plain(&decoded, MAX_QUESTION_OPTION_CHARS),
        Value::Number(Number::Int(_) | Number::BigInt(_)) => {
            text::len(text) <= MAX_QUESTION_OPTION_CHARS
        }
        _ => false,
    };
    let options = Options {
        ensure_ascii: false,
        sort_keys: false,
        compact: false,
    };
    scalar && dumps(&decoded, options) == text
}

/// `_question_option`: one target a person may choose: the JSON text of the value its input would take, and its name.
fn question_option(value: &Value) -> bool {
    exact_object(value, &["value", "label"]).is_some_and(|members| {
        let label = field(members, "label");
        target(field(members, "value"))
            && (*label == Value::Null || routine::plain(label, MAX_QUESTION_OPTION_CHARS))
    })
}

fn question_options(code: &str, options: &Value) -> bool {
    array(options).is_some_and(|options| {
        (code == "routine-binding-ambiguous" || options.is_empty())
            && options.len() <= MAX_QUESTION_OPTIONS
            && options.iter().all(question_option)
            && options
                .iter()
                .filter_map(|option| string(field_of(option, "value")))
                .collect::<HashSet<_>>()
                .len()
                == options.len()
    })
}

/// `canonical_question`: one question Team asks before a card: its code, its targets for an ambiguous binding, and a
/// fitting interval.
#[must_use]
pub fn canonical_question(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["code", "options", "value"])?;
    let code = string(field(members, "code")).filter(|code| QUESTION_CODES.contains(code))?;
    let interval = field(members, "value");
    let valid = question_options(code, field(members, "options"))
        && if code == "routine-interval-over-budget" {
            whole(
                interval,
                MIN_CONTINUOUS_GAP_SECONDS,
                MAX_CONTINUOUS_GAP_SECONDS,
            )
        } else {
            *interval == Value::Null
        };
    valid.then_some(value)
}

/// `canonical_refusal`: why a recording made no card: one code, which Admin words in the interface language.
#[must_use]
pub fn canonical_refusal(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["code"])?;
    let code = field(members, "code");
    routine::matches(code, &ERROR_CODE_RE).then(|| object_of(vec![("code", code.clone())]))
}

/// `canonical_proposal_answer`: what an answer did: the Routine it created or changed, or the card it revoked.
#[must_use]
pub fn canonical_proposal_answer(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["team_id", "proposal_id", "routine_id", "status"])?;
    let (status, routine_id) = (field(members, "status"), field(members, "routine_id"));
    let valid = routine::valid_team(field(members, "team_id"))
        && routine::matches(field(members, "proposal_id"), &ROUTINE_ID_RE)
        && among(status, &PROPOSAL_STATUSES)
        && if string(status) == Some("revoked") {
            *routine_id == Value::Null
        } else {
            routine::matches(routine_id, &ROUTINE_ID_RE)
        };
    valid.then_some(value)
}
