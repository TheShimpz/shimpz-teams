//! `routine_proposal`: cards, refinements, questions, and answers.

use strict_json::Value;
use team_protocol::python::text;
use team_protocol::{routine, routine_proposal};

use super::{first, int, member, object, refused, string, verdicts, with};
use crate::support::registry::Outcome;
use crate::support::vectors::{call, group, items};

const WIDE: &str = "\u{754c}";

/// A card at its byte bound, one byte past it, or with a step more than a plan admits.
fn proposal_case(name: &str) -> Value {
    let card = first("routine_proposal.valid");
    let steps = items(member(&card, "steps"));
    if name == "over-steps" {
        let assistant = member(&steps[0], "assistant").clone();
        let all = (1..=routine::MAX_ROUTINE_STEPS + 1)
            .map(|position| {
                object(vec![
                    ("position", int(position)),
                    ("assistant", assistant.clone()),
                    ("action", string("list-zones")),
                    ("read_only", Value::Bool(true)),
                    ("inputs", Value::Array(vec![])),
                ])
            })
            .collect();
        return with(&card, "steps", Value::Array(all));
    }
    let note = |value: &str| {
        object(vec![
            ("member", string("note")),
            ("origin", string("assistant")),
            ("value", string(value)),
            ("step", Value::Null),
            ("pointer", Value::Null),
            ("where", Value::Null),
            ("item", Value::Null),
        ])
    };
    let card_with = |value: &str| {
        let step = with(&steps[0], "inputs", Value::Array(vec![note(value)]));
        let mut all = steps.to_vec();
        all[0] = step;
        with(&card, "steps", Value::Array(all))
    };
    let room = routine_proposal::MAX_PROPOSAL_BYTES - routine::encoded_bytes(&card_with(""));
    let tail = room % 3 + usize::from(name == "one-byte-over");
    card_with(&format!("{}{}", WIDE.repeat(room / 3), "a".repeat(tail)))
}

#[test]
fn generated_proposals_hold_their_bounds() {
    let generated = items(member(group("routine_proposal"), "generated"));
    let names = ["largest-unicode", "one-byte-over", "over-steps"].map(string);
    assert_eq!(generated, names);
    let largest = proposal_case("largest-unicode");
    assert_eq!(
        routine::encoded_bytes(&largest),
        routine_proposal::MAX_PROPOSAL_BYTES
    );
    assert_eq!(
        routine_proposal::canonical_proposal(&largest),
        Some(&largest)
    );
    let over = ["one-byte-over", "over-steps"].map(proposal_case);
    assert!(
        over.iter()
            .all(|case| routine_proposal::canonical_proposal(case).is_none())
    );
}

#[test]
fn output_choices_name_each_mode_once_in_every_language() {
    let locales = routine_proposal::OUTPUT_CHOICES.map(|(locale, _)| locale);
    assert_eq!(locales, routine::LOCALES);
    for (_, labels) in routine_proposal::OUTPUT_CHOICES {
        assert_eq!(labels.map(|(mode, _)| mode), routine::OUTPUT_MODES);
        let mut folded = labels.map(|(_, label)| text::casefold(label)).to_vec();
        folded.sort();
        folded.dedup();
        let named = labels
            .iter()
            .all(|(_, label)| routine::exact_name(&string(label)));
        assert!(named && folded.len() == labels.len(), "{labels:?}");
    }
}

#[test]
fn schedule_choices_and_interval_answers_cover_every_language() {
    let schedules = routine_proposal::SCHEDULE_CHOICES.map(|(locale, _)| locale);
    let answers = routine_proposal::OVER_BUDGET_ANSWERS.map(|(locale, _)| locale);
    assert_eq!((schedules, answers), (routine::LOCALES, routine::LOCALES));
    let distinct = routine_proposal::SCHEDULE_CHOICES
        .iter()
        .all(|(_, [a, b, c])| a != b && b != c && a != c);
    let templated = routine_proposal::OVER_BUDGET_ANSWERS
        .iter()
        .all(|(_, answer)| answer.contains("{seconds}"));
    assert!(distinct && templated);
}

#[test]
fn settings_answers_are_the_fixed_answers_of_each_settings_question() {
    let answers = |code: &str| routine_proposal::settings_answers(code).unwrap_or_default();
    let counts = routine_proposal::SETTINGS_QUESTION_CODES.map(|code| answers(code).len());
    assert_eq!(counts, [8, 24, 24]);
    assert!(answers("routine-schedule-unstated").contains(&"Every hour"));
    assert!(answers("routine-interval-over-budget").contains(&"Every {seconds} seconds"));
    assert!(answers("routine-output-unstated").contains(&"Não mostrar"));
    assert_eq!(
        routine_proposal::settings_answers("routine-binding-ambiguous"),
        None
    );
    let asked = routine_proposal::SETTINGS_QUESTION_CODES
        .iter()
        .all(|code| routine_proposal::QUESTION_CODES.contains(code));
    assert!(asked && routine_proposal::MAX_QUESTION_VALUE_CHARS == 242);
}

fn card_input(origin: &str) -> Value {
    object(vec![
        ("member", string("zone")),
        ("origin", string(origin)),
        ("value", Value::Null),
        ("step", Value::Null),
        ("pointer", Value::Null),
        ("where", Value::Null),
        ("item", Value::Null),
    ])
}

fn card_with_step(key: &str, member_value: Value) -> Value {
    let card = first("routine_proposal.valid");
    let steps = items(member(&card, "steps"));
    let mut all = steps.to_vec();
    all[0] = with(&steps[0], key, member_value);
    with(&card, "steps", Value::Array(all))
}

fn shown(origin: &str, value: Value) -> Value {
    with(&card_input(origin), "value", value)
}

#[test]
fn card_inputs_show_a_literal_a_date_or_a_source() {
    let cases = [
        (shown("request", string("a")), true),
        (shown("assistant", string("")), true),
        (card_input("request"), false),
        (shown("request", string("\u{200b}")), false),
        (with(&shown("request", string("a")), "step", int(1)), false),
        (card_input("clock"), true),
        (shown("clock", string("{date}")), true),
        (shown("clock", string("date")), false),
        (shown("clock", int(1)), false),
        (with(&card_input("clock"), "pointer", string("")), false),
        (card_input("step"), false),
        (shown("step", string("a")), false),
        (card_input("selector"), false),
        (card_input("other"), false),
        (with(&card_input("clock"), "member", string("")), false),
        (Value::Null, false),
    ]
    .map(|(input, admitted)| {
        (
            card_with_step("inputs", Value::Array(vec![input])),
            admitted,
        )
    });
    verdicts(&cases, |value| {
        routine_proposal::canonical_proposal(value).is_some()
    });
}

#[test]
fn card_steps_are_numbered_from_one() {
    let card = first("routine_proposal.valid");
    let mut cases = refused(
        &card,
        vec![
            ("steps", Value::Array(vec![])),
            ("steps", Value::Null),
            ("proposal_id", Value::Null),
            ("expires_at", Value::Null),
            ("replaces", string("x")),
            ("output", string("other")),
        ],
    );
    cases.extend(
        [
            ("position", int(2)),
            ("position", Value::Bool(true)),
            ("assistant", Value::Null),
            ("action", Value::Null),
            ("read_only", int(1)),
            ("inputs", Value::Null),
        ]
        .into_iter()
        .map(|(key, bad)| (card_with_step(key, bad), false)),
    );
    verdicts(&cases, |value| {
        routine_proposal::canonical_proposal(value).is_some()
    });
}

#[test]
fn refinements_check_every_member() {
    let refinement = first("routine_refinement.valid");
    let steps = items(member(&refinement, "steps"));
    let set_step = |key: &str, bad: Value| {
        let mut all = steps.to_vec();
        all[0] = with(&steps[0], key, bad);
        (with(&refinement, "steps", Value::Array(all)), false)
    };
    let mut cases = refused(
        &refinement,
        vec![
            ("removed", Value::Null),
            ("removed", Value::Array(vec![Value::Null])),
            ("last_changed", Value::Null),
            ("base_revision", int(0)),
            ("routine_id", Value::Null),
            ("steps", Value::Null),
            ("proposal_id", Value::Null),
            ("expires_at", Value::Null),
            ("name", Value::Null),
        ],
    );
    cases.push(set_step("change", string("other")));
    cases.push(set_step("base", int(300)));
    cases.push(set_step("change", string("added")));
    cases.push(set_step("position", int(9)));
    verdicts(&cases, |value| {
        routine_proposal::canonical_refinement(value).is_some()
    });
}

fn base_step(position: i64, change: &str, base: Value) -> Value {
    object(vec![
        ("position", int(position)),
        ("assistant", string("shimpz-cloudflare")),
        ("action", string("list-zones")),
        ("read_only", Value::Bool(true)),
        ("inputs", Value::Array(vec![])),
        ("change", string(change)),
        ("base", base),
    ])
}

fn removed(position: i64, change: &str) -> Value {
    object(vec![
        ("position", int(position)),
        ("assistant", string("shimpz-cloudflare")),
        ("action", string("list-zones")),
        ("read_only", Value::Bool(false)),
        ("change", string(change)),
    ])
}

fn kept(position: i64, base: i64) -> Value {
    base_step(position, "kept", int(base))
}

fn plan(steps: Vec<Value>, gone: Vec<Value>) -> Value {
    let refinement = first("routine_refinement.valid");
    with(
        &with(&refinement, "steps", Value::Array(steps)),
        "removed",
        Value::Array(gone),
    )
}

#[test]
fn base_positions_are_kept_replaced_or_removed_once() {
    let gone = |key: &str, bad: Value| vec![with(&removed(2, "removed"), key, bad)];
    verdicts(
        &[
            (plan(vec![kept(1, 1), kept(2, 2)], vec![]), true),
            (plan(vec![kept(1, 2)], vec![removed(1, "removed")]), true),
            (
                plan(
                    vec![base_step(1, "replaced", int(1))],
                    vec![removed(1, "replaced")],
                ),
                true,
            ),
            (plan(vec![base_step(1, "added", Value::Null)], vec![]), true),
            (plan(vec![kept(1, 2), kept(2, 1)], vec![]), false),
            (
                plan(
                    vec![kept(1, 1)],
                    vec![removed(3, "removed"), removed(2, "removed")],
                ),
                false,
            ),
            (plan(vec![kept(1, 2)], vec![]), false),
            (
                plan(
                    vec![base_step(1, "replaced", int(1))],
                    vec![removed(1, "removed")],
                ),
                false,
            ),
            (plan(vec![kept(1, 1)], vec![removed(2, "other")]), false),
            (plan(vec![kept(1, 1)], vec![removed(0, "removed")]), false),
            (
                plan(vec![kept(1, 1)], gone("read_only", Value::Null)),
                false,
            ),
            (
                plan(vec![kept(1, 1)], gone("assistant", Value::Null)),
                false,
            ),
            (plan(vec![kept(1, 1)], gone("action", Value::Null)), false),
        ],
        |value| routine_proposal::canonical_refinement(value).is_some(),
    );
}

fn question(code: &str, options: Vec<Value>, value: Value) -> Value {
    object(vec![
        ("code", string(code)),
        ("options", Value::Array(options)),
        ("value", value),
    ])
}

fn option(value: &str, label: Value) -> Value {
    object(vec![("value", string(value)), ("label", label)])
}

fn ambiguous(options: Vec<Value>) -> Value {
    question("routine-binding-ambiguous", options, Value::Null)
}

#[test]
fn questions_offer_targets_only_for_an_ambiguous_binding() {
    let twelve = option("12", string("Twelve"));
    verdicts(
        &[
            (ambiguous(vec![option("\"a\"", Value::Null), twelve]), true),
            (
                ambiguous(vec![
                    option("\"a\"", Value::Null),
                    option("\"a\"", Value::Null),
                ]),
                false,
            ),
            (ambiguous(vec![option("1", string(""))]), false),
            (ambiguous(vec![Value::Null]), false),
            (ambiguous(vec![option("1", Value::Null); 9]), false),
            (
                question(
                    "routine-work-rerun",
                    vec![option("1", Value::Null)],
                    Value::Null,
                ),
                false,
            ),
            (
                question("routine-interval-over-budget", vec![], int(5)),
                true,
            ),
            (
                question("routine-interval-over-budget", vec![], int(4)),
                false,
            ),
            (question("routine-output-unstated", vec![], int(5)), false),
            (question("unknown", vec![], Value::Null), false),
            (
                with(
                    &question("routine-work-rerun", vec![], Value::Null),
                    "options",
                    Value::Null,
                ),
                false,
            ),
        ],
        |value| routine_proposal::canonical_question(value).is_some(),
    );
}

#[test]
fn targets_are_exact_json_scalars() {
    let mut texts: Vec<(String, bool)> = [
        ("\"a\"", true),
        ("\"é\"", true),
        ("\"\\u00e9\"", false),
        ("\"\\\"\"", true),
        ("\"\\u2028\"", false),
        ("12", true),
        ("-3", true),
        ("-0", false),
        ("012", false),
        (" 12", false),
        ("12 ", false),
        ("1.0", false),
        ("1e2", false),
        ("true", false),
        ("null", false),
        ("[1]", false),
        ("{\"a\": 1}", false),
        ("\"\"", false),
        ("", false),
        ("\"\\ud800\"", false),
        ("{", false),
    ]
    .map(|(text, admitted)| (text.to_owned(), admitted))
    .to_vec();
    texts.extend([
        ("1".repeat(120), true),
        ("1".repeat(121), false),
        (format!("\"{}\"", "a".repeat(120)), true),
        (format!("\"{}\"", "a".repeat(121)), false),
        (format!("\"{}\"", "\\\"".repeat(120)), true),
        (format!("\"{}\"", "\\\"".repeat(121)), false),
    ]);
    let cases: Vec<(Value, bool)> = texts
        .into_iter()
        .map(|(text, admitted)| (ambiguous(vec![option(&text, Value::Null)]), admitted))
        .collect();
    verdicts(&cases, |value| {
        routine_proposal::canonical_question(value).is_some()
    });
}

fn answer(status: &str, routine: Value) -> Value {
    object(vec![
        ("team_id", string("team_1")),
        ("proposal_id", string(&"a".repeat(32))),
        ("routine_id", routine),
        ("status", string(status)),
    ])
}

#[test]
fn proposal_answers_name_a_routine_unless_revoked() {
    let routine = string(&"b".repeat(32));
    verdicts(
        &[
            (answer("created", routine.clone()), true),
            (answer("revoked", Value::Null), true),
            (answer("revoked", routine), false),
            (answer("changed", Value::Null), false),
            (answer("other", Value::Null), false),
            (
                with(&answer("revoked", Value::Null), "team_id", Value::Null),
                false,
            ),
            (
                with(&answer("revoked", Value::Null), "proposal_id", Value::Null),
                false,
            ),
        ],
        |value| routine_proposal::canonical_proposal_answer(value).is_some(),
    );
}

#[test]
fn every_entry_refuses_a_non_object() {
    let names = [
        "routine_notice.canonical_routine_view",
        "routine_notice.canonical_incident_view",
        "routine_run.canonical_card",
        "routine_run.canonical_run_steps",
        "routine_proposal.canonical_refinement",
    ];
    assert!(
        names
            .iter()
            .all(|name| call(name, &Value::Null) == Outcome::Refuse)
    );
}
