//! Every `vectors.json` group of `identifiers`, `purpose`, `payload`, `progress`, and `routine`, and the canonical JSON
//! v1 vectors, checked with typed equality.

use crate::support::registry::Outcome;
use crate::support::vectors::{admits, admits_group, call, group, items, same, text};
use strict_json::{Number, Value};
use team_protocol::python::json::{self, Options};
use team_protocol::{payload, progress, routine};

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

#[test]
fn identifier_vectors() {
    for (kind, name) in [
        ("team", "identifiers.canonical_team_id"),
        ("assistant", "identifiers.canonical_assistant_id"),
        ("action", "identifiers.canonical_action_id"),
        ("identifier", "identifiers.canonical_identifier"),
        ("source_digest", "payload.canonical_source_digest"),
        ("local_team_name", "payload.canonical_local_team_name"),
    ] {
        admits_group(name, &format!("identifiers.{kind}"));
    }
}

#[test]
fn payload_vectors() {
    for (path, name) in [
        ("chat_limit", "payload.canonical_chat_limit"),
        ("clarification", "payload.canonical_clarification"),
        ("memory", "payload.canonical_memory"),
        ("memory_changes", "payload.canonical_memory_changes"),
        ("chat_locale", "payload.canonical_locale"),
        (
            "chat_request_identity",
            "payload.canonical_request_identity",
        ),
        ("help_url", "payload.canonical_help_url"),
        ("stored_input_help", "payload.canonical_stored_input_help"),
        ("purpose", "purpose.canonical_purpose"),
        ("pack_digest", "payload.canonical_pack_digest"),
        ("snapshot_summary", "payload.canonical_snapshot_summary"),
        ("local_assistant", "payload.canonical_local_assistant"),
        ("assistant_details", "payload.canonical_assistant_details"),
        ("creator_links", "payload.canonical_creator_links"),
        ("turn_usage", "payload.canonical_turn_usage"),
        ("routine_run_usage", "routine.canonical_run_usage"),
        ("routine_timezone", "routine.canonical_timezone"),
        ("routine_views.output", "routine.canonical_output"),
        ("routine_views.page", "routine.canonical_page"),
        ("routine_views.summary", "routine.canonical_summary"),
    ] {
        admits_group(name, path);
    }
}

/// A generated window at or past its bounds.
fn conversation_case(case: &Value) -> Value {
    let entry = |text: String| {
        object(vec![
            ("role", Value::String("user".into())),
            ("truncated", Value::Bool(false)),
            ("text", Value::String(text)),
        ])
    };
    match case.get("generated").map(text) {
        Some("eight-maximal-entries") => {
            Value::Array(vec![
                entry("x".repeat(payload::MAX_CONVERSATION_TEXT_CHARS));
                payload::MAX_CONVERSATION_ENTRIES
            ])
        }
        Some("nine-entries") => Value::Array(vec![
            entry("x".into());
            payload::MAX_CONVERSATION_ENTRIES + 1
        ]),
        Some("overlong-entry") => Value::Array(vec![entry(
            "x".repeat(payload::MAX_CONVERSATION_TEXT_CHARS + 1),
        )]),
        _ => case.clone(),
    }
}

#[test]
fn conversation_vectors() {
    let valid = Value::Array(
        items(group("chat_conversation.valid"))
            .iter()
            .map(conversation_case)
            .collect(),
    );
    let invalid = Value::Array(
        items(group("chat_conversation.invalid"))
            .iter()
            .map(conversation_case)
            .collect(),
    );
    admits("payload.canonical_conversation", &valid, &invalid);
}

#[test]
fn clarification_renderings_and_labels() {
    let rendered: Vec<&str> = items(group("clarification.rendered"))
        .iter()
        .map(text)
        .collect();
    let actual: Vec<String> = items(group("clarification.valid"))
        .iter()
        .map(payload::render_clarification)
        .collect();
    assert_eq!(actual, rendered);
    for case in items(group("clarification_labels.composed")) {
        let field = |key: &str| text(case.get(key).expect("a composed field"));
        let composed = payload::compose_clarified(
            field("original"),
            field("question"),
            field("answer"),
            field("locale"),
        );
        assert_eq!(composed.as_deref(), Some(field("message")));
    }
    for case in items(group("clarification_labels.authored_segments")) {
        let segments: Vec<&str> = items(case.get("segments").expect("segments"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(
            payload::authored_segments(text(case.get("message").expect("message"))),
            segments
        );
    }
    let mut locales: Vec<&str> = payload::CLARIFICATION_LABELS
        .iter()
        .map(|(locale, _, _)| *locale)
        .collect();
    locales.sort_unstable();
    assert_eq!(locales, routine::LOCALES);
}

#[test]
fn memory_application_vectors() {
    let cases = items(group("memory_apply"));
    assert_ne!(cases.len(), 0);
    for case in cases {
        let field = |key: &str| case.get(key).expect("a memory application field");
        assert!(same(
            &payload::apply_memory_changes(field("memory"), field("changes")),
            field("result")
        ));
    }
}

#[test]
fn chat_stream_vectors() {
    for case in items(group("chat_stream")) {
        let record = case.get("record").expect("record");
        let outcome = progress::canonical_record(record);
        match case.get("valid") {
            Some(Value::Bool(true)) => {
                assert!(same(&outcome.expect("admitted"), record), "{case:?}");
            }
            _ => assert_eq!(outcome, Err(progress::ProgressContractError), "{case:?}"),
        }
    }
    for case in items(group("chat_stream_lines")) {
        let raw = if case.get("generated").map(text) == Some("over-max-line") {
            [
                b"{}".as_slice(),
                &vec![b' '; progress::MAX_LINE_BYTES],
                b"\n",
            ]
            .concat()
        } else {
            text(case.get("line").expect("line")).as_bytes().to_vec()
        };
        let outcome = progress::decode_line(&raw);
        match case.get("valid") {
            Some(Value::Bool(true)) => assert!(same(
                &outcome.expect("admitted"),
                case.get("record").expect("record")
            )),
            _ => assert!(outcome.is_err(), "{case:?}"),
        }
    }
}

#[test]
fn routine_schedule_vectors() {
    for case in items(group("routine_schedule.valid")) {
        assert!(routine::exact_schedule(case), "{case:?}");
    }
    for case in items(group("routine_schedule.invalid")) {
        assert_eq!(routine::canonical_schedule(case), None, "{case:?}");
    }
}

#[test]
fn routine_daily_cap_and_position_vectors() {
    let caps = items(group("routine_schedule.daily_cap"));
    assert_ne!(caps.len(), 0);
    for case in caps {
        let cap = routine::daily_cap(case.get("schedule").expect("schedule"));
        assert_eq!(
            Value::Number(Number::Int(cap)),
            *case.get("cap").expect("cap")
        );
    }
    for case in items(group("routine_position.valid")) {
        assert!(
            matches!(call("routine.canonical_position", case), Outcome::Value(v) if same(&v, case.get("value").expect("value")))
        );
    }
    for case in items(group("routine_position.invalid")) {
        assert_eq!(
            call("routine.canonical_position", case),
            Outcome::Refuse,
            "{case:?}"
        );
    }
}

#[test]
fn canonical_json_vectors() {
    for case in items(group("canonical_json.canonical")) {
        let encoded = json::canonical(case.get("value").expect("value"));
        assert_eq!(
            encoded,
            text(case.get("utf8").expect("utf8")).as_bytes(),
            "{case:?}"
        );
    }
    for case in items(group("canonical_json.dumps")) {
        let flag = |key: &str| case.get(key) == Some(&Value::Bool(true));
        let options = Options {
            ensure_ascii: flag("ensure_ascii"),
            sort_keys: flag("sort_keys"),
            compact: flag("compact"),
        };
        assert_eq!(
            json::dumps(case.get("value").expect("value"), options),
            text(case.get("text").expect("text"))
        );
    }
    for case in items(group("canonical_json.encoded_bytes")) {
        let size = routine::encoded_bytes(case.get("value").expect("value"));
        assert_eq!(
            Value::Number(Number::Int(i64::try_from(size).expect("size"))),
            *case.get("bytes").expect("bytes")
        );
    }
    for case in items(group("canonical_json.encode_record")) {
        let line = progress::encode_record(case.get("record").expect("record")).ok();
        let expected = match case.get("line") {
            Some(Value::String(line)) => Some(line.as_bytes().to_vec()),
            _ => None,
        };
        assert_eq!(line, expected, "{case:?}");
    }
}
