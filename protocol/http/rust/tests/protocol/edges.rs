//! Branches of `payload`, `progress`, and `routine` no registry adapter reaches, each checked against what the retired
//! Python module answered for the same input.

use crate::support::vectors::{group, items};
use strict_json::Value;
use team_protocol::python::regex::Pattern;
use team_protocol::{
    browser, payload, progress, routine, routine_context, routine_notice, sign_in, turn,
};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("test JSON")
}

#[test]
fn memory_application_keeps_python_dict_order_for_repeated_topics() {
    // {"a": "1", "b": "2", "a": "3"} keeps "a" first with its last preference, as a Python dict does.
    let memory = json(
        r#"[{"topic": "a", "preference": "1"}, {"topic": "b", "preference": "2"}, {"topic": "a", "preference": "3"}]"#,
    );
    let changes = json(r#"[{"op": "remember", "topic": "c", "preference": "4"}]"#);
    let expected = json(
        r#"[{"topic": "a", "preference": "3"}, {"topic": "b", "preference": "2"}, {"topic": "c", "preference": "4"}]"#,
    );
    assert_eq!(payload::apply_memory_changes(&memory, &changes), expected);
}

#[test]
fn daily_cap_of_any_other_schedule_is_one() {
    assert_eq!(
        routine::daily_cap(&json(r#"{"kind": "daily", "gap": 5}"#)),
        1
    );
    assert_eq!(
        routine::daily_cap(&json(r#"{"kind": "continuous", "gap": 5}"#)),
        17_280
    );
}

#[test]
fn a_terminal_record_past_its_line_bound_is_refused() {
    let reply = "x".repeat(progress::MAX_LINE_BYTES);
    let record = json(&format!(
        r#"{{"type": "terminal", "status": 200, "body": {{"reply": "{reply}"}}}}"#
    ));
    assert_eq!(
        progress::encode_record(&record),
        Err(progress::ProgressContractError)
    );
    assert_eq!(
        progress::ProgressContractError.to_string(),
        "the chat stream record violates the closed protocol"
    );
}

#[test]
fn routine_identity_helpers_follow_their_grammars() {
    assert!(routine::valid_team(&json(r#""team_1""#)) && !routine::valid_team(&json(r#""Team""#)));
    assert!(routine::matches(&json(r#""a-b""#), &routine::ERROR_CODE_RE));
    assert!(!routine::matches(&json("1"), &routine::ERROR_CODE_RE));
}

#[test]
fn every_declared_pattern_compiles_with_its_python_meaning() {
    assert!(
        turn::REPLY_CONTROL_RE
            .search("a\u{7}b")
            .expect("a search")
            .is_some()
    );
    assert!(turn::REPLY_CONTROL_RE.is_absent("a\tb\n"));
    assert!(turn::SAFE_ID_RE.is_full("thread:1/a.b") && !turn::SAFE_ID_RE.is_full("thread\n"));
    assert!(payload::GITHUB_REPOSITORY_RE.is_full("https://github.com/TheShimpz/shimpz"));
    assert!(!payload::GITHUB_REPOSITORY_RE.is_full("https://github.com/-a/b"));
    let unsafe_class: &Pattern = &routine::PLAN_UNSAFE_RE;
    for point in 0..=0x10_ffff_u32 {
        if let Some(character) = char::from_u32(point) {
            let text = character.to_string();
            assert_eq!(
                !unsafe_class.is_absent(&text),
                routine::plan_unsafe(point),
                "{point:x}"
            );
        }
    }
}

#[test]
fn progress_lines_hold_exactly_at_their_bounds() {
    // The largest finished Action progress record is exactly its 311-byte bound, newline included.
    let largest = json(&format!(
        r#"{{"type": "progress", "seq": 2048, "phase": "action", "state": "finished", "elapsed_ms": 86400000,
           "assistant_id": "{}", "index": 512, "action": "{}", "total": 512}}"#,
        "a".repeat(40),
        "p".repeat(128)
    ));
    let line = progress::encode_record(&largest).expect("the largest progress line");
    assert_eq!(line.len(), progress::MAX_PROGRESS_LINE_BYTES);
    // A terminal past a progress line's bound is still a terminal.
    let terminal = json(&format!(
        r#"{{"type": "terminal", "status": 200, "body": {{"reply": "{}"}}}}"#,
        "x".repeat(400)
    ));
    assert!(progress::encode_record(&terminal).is_ok());
    // A terminal line of exactly the line bound decodes; one byte more does not.
    let frame = |reply: usize| {
        format!(
            "{{\"body\":{{\"reply\":\"{}\"}},\"status\":200,\"type\":\"terminal\"}}\n",
            "x".repeat(reply)
        )
    };
    let fill = progress::MAX_LINE_BYTES - frame(0).len();
    assert!(progress::decode_line(frame(fill).as_bytes()).is_ok());
    assert_eq!(
        progress::decode_line(frame(fill + 1).as_bytes()),
        Err(progress::ProgressContractError)
    );
}

#[test]
fn urls_hold_exactly_at_their_length_bounds() {
    let url = |length: usize| format!("https://example.com/{}", "a".repeat(length - 20));
    let at = Value::String(url(browser::MAX_URL_CHARS));
    assert_eq!(
        browser::canonical_url(&at),
        Some(url(browser::MAX_URL_CHARS).as_str())
    );
    let site = Value::String(url(sign_in::MAX_URL_CHARS));
    assert_eq!(sign_in::start_site(&site).as_deref(), Some("example.com"));
}

#[test]
fn shown_fields_nest_at_most_four_levels() {
    let shown = |levels: usize| {
        let mut node = json(r#"{"kind": "text", "value": "a", "cut": false}"#);
        for _ in 0..levels {
            node = Value::Object(vec![
                ("kind".to_owned(), Value::String("fields".to_owned())),
                (
                    "fields".to_owned(),
                    Value::Array(vec![Value::Array(vec![
                        Value::String("k".to_owned()),
                        node,
                    ])]),
                ),
                (
                    "omitted".to_owned(),
                    Value::Number(strict_json::Number::Int(0)),
                ),
            ]);
        }
        let mut output = json(r#"{"step": 1, "state": "shown", "truncated": false}"#);
        if let Value::Object(members) = &mut output {
            members.push(("value".to_owned(), node));
        }
        output
    };
    assert!(routine::canonical_output(&shown(4)).is_some());
    assert!(routine::canonical_output(&shown(5)).is_none());
}

#[test]
fn dates_a_day_past_the_reach_still_count() {
    let today = (2026, 1, 1);
    assert!(routine_context::may_write_date("2026-02-02", today).expect("a completed search"));
    assert!(!routine_context::may_write_date("2026-02-03", today).expect("a completed search"));
}

#[test]
fn a_notice_batch_holds_exactly_at_its_byte_bound() {
    // Held notices with distinct ids and names trimmed so the batch encodes to exactly its bound, as Python admits;
    // one more byte is refused.
    let batches = items(group("routine_views.notice_batch.valid"));
    let held = batches
        .iter()
        .flat_map(|batch| items(batch.get("notices").expect("notices")).iter())
        .find(|notice| notice.get("outcome") == Some(&Value::String("held".to_owned())))
        .expect("a held notice");
    let notices = |lengths: &[usize]| {
        Value::Array(
            lengths
                .iter()
                .enumerate()
                .map(|(index, length)| notice(held, index, *length))
                .collect(),
        )
    };
    let bound = routine_notice::MAX_NOTICE_BATCH_BYTES;
    let mut lengths = vec![80];
    while routine::encoded_bytes(&notices(&lengths)) < bound {
        lengths.push(80);
    }
    let mut excess = routine::encoded_bytes(&notices(&lengths)) - bound;
    for length in &mut lengths {
        let cut = excess.min(79);
        *length -= cut;
        excess -= cut;
    }
    let batch = |lengths: &[usize]| {
        Value::Object(vec![
            ("notices".to_owned(), notices(lengths)),
            ("more".to_owned(), Value::Bool(false)),
        ])
    };
    assert_eq!(routine::encoded_bytes(&notices(&lengths)), bound);
    assert!(routine_notice::canonical_notice_batch(&batch(&lengths)).is_some());
    lengths[0] += 1;
    assert!(routine_notice::canonical_notice_batch(&batch(&lengths)).is_none());
}

/// A copy of `held` with ids made of `index` and a name of `length` characters.
fn notice(held: &Value, index: usize, length: usize) -> Value {
    let Value::Object(members) = held else {
        panic!("a notice object");
    };
    let replaced = |key: &str, value: &Value| match key {
        "notice_id" | "run_id" => Value::String(format!("{index:032x}")),
        "name" => Value::String("a".repeat(length)),
        _ => value.clone(),
    };
    Value::Object(
        members
            .iter()
            .map(|(key, value)| (key.clone(), replaced(key, value)))
            .collect(),
    )
}

#[test]
fn selector_constants_round_trip_only_as_python_writes_them() {
    // Python admits a lone surrogate escape in any company and refuses a surrogate pair, which it reads as one
    // supplementary character that its writer spells bare.
    let step = |value_json: &str| {
        json(&format!(
            r#"{{"position": 2, "assistant": "shimpz-cloudflare", "action": "list-dns-records", "read_only": true,
               "inputs": [{{"member": "zone_id", "source": "step_output", "step": 1, "pointer": "/result",
                 "where": {{"member": "name", "value_json": {}}}, "item": "/id"}}],
               "stored_inputs": ["api-token"]}}"#,
            team_protocol::python::json::dumps(
                &Value::String(value_json.to_owned()),
                team_protocol::python::json::CANONICAL
            )
        ))
    };
    let backslash = '\\';
    let cases = [
        (format!("\"{backslash}ud800xxxxxxxxx\""), true),
        (format!("\"{backslash}u0085{backslash}udc00\""), true),
        (format!("\"{backslash}ud800{backslash}ud800\""), true),
        (format!("\"{backslash}ud800{backslash}udc00\""), false),
        (format!("\"{backslash}ud800{backslash}ndc00\""), true),
        (format!("\"{backslash}ud800xudc00\""), true),
    ];
    for (value_json, admitted) in cases {
        assert_eq!(
            routine::canonical_step(&step(&value_json), 2).is_some(),
            admitted,
            "{value_json}"
        );
    }
}
