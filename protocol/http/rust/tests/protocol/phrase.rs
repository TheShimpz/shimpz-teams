//! Every `vectors.json` group of `phrase` and `routine_context`, checked with typed equality.

use strict_json::Value;
use team_protocol::python::json::{Options, encoded_len};
use team_protocol::{phrase, routine, routine_context, routine_proposal, routine_run};

use crate::support::registry::Outcome;
use crate::support::vectors::{admits_group, call, group, items, same};

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn strings(values: &[impl AsRef<str>]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|value| Value::String(value.as_ref().to_owned()))
            .collect(),
    )
}

/// The reading `name` of one case's text, with its `language` member when the case has one.
fn read(name: &str, case: &Value) -> Outcome {
    let text = case.get("text").cloned().unwrap_or(Value::Null);
    if matches!(
        name,
        "phrase.stated(language)" | "phrase.team_asks(language)"
    ) {
        let language = case.get("language").cloned().unwrap_or(Value::Null);
        call(name, &object(vec![("text", text), ("language", language)]))
    } else {
        call(name, &text)
    }
}

#[test]
fn phrase_reading_vectors() {
    for (path, name, key) in [
        ("stated", "phrase.stated(language)", "schedules"),
        ("outputs", "phrase.outputs", "outputs"),
        ("zones", "phrase.zones", "zones"),
        ("team_asks", "phrase.team_asks(language)", "asks"),
        ("requests_routine", "phrase.requests_routine", "requests"),
    ] {
        let cases = items(group(&format!("routine_phrase.{path}")));
        assert!(!cases.is_empty(), "{path}");
        for case in cases {
            let expected = case.get(key).expect("an expected reading");
            match read(name, case) {
                Outcome::Value(found) => assert!(same(&found, expected), "{case:?}: {found:?}"),
                other => panic!("{name} refused {case:?}: {other:?}"),
            }
        }
    }
}

#[test]
fn schedule_answers_each_state_one_schedule_in_every_interface_language() {
    let shortest = object(vec![
        ("kind", Value::String("continuous".into())),
        (
            "gap",
            team_protocol::python::value::int(routine::MIN_CONTINUOUS_GAP_SECONDS),
        ),
    ]);
    for (_, labels) in routine_proposal::SCHEDULE_CHOICES {
        for label in labels {
            assert_eq!(
                phrase::stated(label, None).map(|found| found.len()),
                Ok(1),
                "{label}"
            );
        }
    }
    for (_, answer) in routine_proposal::OVER_BUDGET_ANSWERS {
        let answer = answer.replace(
            "{seconds}",
            &routine::MIN_CONTINUOUS_GAP_SECONDS.to_string(),
        );
        assert_eq!(
            phrase::stated(&answer, None),
            Ok(vec![shortest.clone()]),
            "{answer}"
        );
    }
}

#[test]
fn routine_context_vectors() {
    for (path, name) in [
        (
            "routine_listing",
            "routine_context.canonical_routine_listings",
        ),
        ("routine_rerun", "routine_context.canonical_rerun"),
        (
            "routine_selected",
            "routine_context.canonical_selected_routine",
        ),
        ("routine_refine", "routine_context.canonical_refine"),
    ] {
        admits_group(name, path);
    }
    let statuses: Vec<&str> = routine_run::RUN_STEP_STATUSES
        .iter()
        .chain(&routine_run::RUN_STEP_GAPS)
        .copied()
        .collect();
    assert_eq!(statuses, routine_context::LAST_RUN_STATUSES);
}

/// A rerun of one call whose one value input shows `literal`.
fn literal_rerun(literal: &Value) -> Value {
    let shown = object(vec![
        ("member", Value::String("page".into())),
        ("kind", Value::String("value".into())),
        ("value", literal.clone()),
        ("chosen", Value::Bool(false)),
        ("source", Value::Null),
    ]);
    Value::Array(vec![object(vec![
        ("assistant", Value::String("shimpz-cloudflare".into())),
        ("action", Value::String("list-dns-records".into())),
        ("count", team_protocol::python::value::int(1)),
        ("inputs", Value::Array(vec![shown])),
    ])])
}

#[test]
fn rerun_literal_vectors() {
    let literals = group("routine_rerun_literal");
    for literal in items(literals.get("valid").expect("valid")) {
        let rerun = literal_rerun(literal);
        assert_eq!(
            routine_context::canonical_rerun(&rerun),
            Some(&rerun),
            "{literal:?}"
        );
    }
    for literal in items(literals.get("invalid").expect("invalid")) {
        assert_eq!(
            routine_context::canonical_rerun(&literal_rerun(literal)),
            None,
            "{literal:?}"
        );
    }
}

#[test]
fn date_word_and_number_token_vectors() {
    let words = items(group("routine_date_words"));
    let dated: Vec<bool> = words
        .iter()
        .map(|case| case.get("dated") == Some(&Value::Bool(true)))
        .collect();
    assert!(dated.contains(&true) && dated.contains(&false));
    for case in words {
        let arguments = object(vec![
            ("text", case.get("text").cloned().expect("text")),
            ("today", case.get("today").cloned().expect("today")),
        ]);
        let expected = case.get("dated").cloned().expect("dated");
        assert_eq!(
            call("routine_context.may_write_date", &arguments),
            Outcome::Value(expected),
            "{case:?}"
        );
    }
    let tokens = items(group("routine_number_tokens"));
    assert_ne!(tokens.len(), 0);
    for case in tokens {
        let found = call(
            "routine_context.number_tokens",
            case.get("text").expect("text"),
        );
        assert_eq!(
            found,
            Outcome::Value(case.get("tokens").cloned().expect("tokens")),
            "{case:?}"
        );
    }
}

/// A listed Routine of `steps` steps, each with every input name at its widest.
fn listing(index: usize, steps: usize) -> Value {
    let names: Vec<String> = (0..routine::MAX_STEP_INPUTS)
        .map(|name| format!("{name:03}{}", "a".repeat(routine::MAX_MEMBER_CHARS - 3)))
        .collect();
    let step = |number: usize| {
        object(vec![
            ("id", Value::String(format!("s{number}"))),
            ("assistant", Value::String("shimpz-cloudflare".into())),
            ("action", Value::String("list-dns-records".into())),
            ("inputs", strings(&names)),
        ])
    };
    object(vec![
        ("routine_id", Value::String(format!("{index:032x}"))),
        ("name", Value::String("DNS".into())),
        (
            "schedule",
            object(vec![
                ("kind", Value::String("daily".into())),
                ("time", Value::String("08:00".into())),
            ]),
        ),
        ("timezone", Value::String("UTC".into())),
        ("timezone_source", Value::String("none".into())),
        ("revision", team_protocol::python::value::int(1)),
        ("daily_steps", team_protocol::python::value::int(1)),
        (
            "output",
            object(vec![("mode", Value::String("show".into()))]),
        ),
        ("steps", Value::Array((0..steps).map(step).collect())),
    ])
}

fn steps_bytes(listed: &Value) -> usize {
    let options = Options {
        ensure_ascii: false,
        sort_keys: false,
        compact: true,
    };
    encoded_len(listed.get("steps").expect("steps"), options)
}

#[test]
fn generated_listings_hold_at_their_byte_bounds() {
    // The steps' size grows with their count, so the widest fitting count is the last before the first that does not.
    let fit = (1..=usize::try_from(routine::MAX_ROUTINE_STEPS).expect("steps"))
        .take_while(|count| {
            steps_bytes(&listing(0, *count)) <= routine_context::MAX_LISTED_STEPS_BYTES
        })
        .last()
        .expect("one fits");
    let held = routine_context::MAX_LISTING_STEPS_BYTES / steps_bytes(&listing(0, fit));
    assert!(held < routine::MAX_ROUTINES);
    let fitting = Value::Array((0..held).map(|index| listing(index, fit)).collect());
    assert_eq!(
        routine_context::canonical_routine_listings(&fitting),
        Some(&fitting)
    );
    let wide = Value::Array(vec![listing(0, fit + 1)]);
    assert_eq!(routine_context::canonical_routine_listings(&wide), None);
    let many = Value::Array((0..=held).map(|index| listing(index, fit)).collect());
    assert_eq!(routine_context::canonical_routine_listings(&many), None);
}
