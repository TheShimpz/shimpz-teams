use strict_json::{Number, Value};

use super::{
    ENTRY_OVERHEAD_BYTES, MAX_MEMORY_BYTES, MAX_NAME_BYTES, MAX_OBSERVATION_BYTES, MAX_STEPS,
    STEP_SCHEMA, canonical_check, canonical_downloads, canonical_file_reference, canonical_memory,
    canonical_objective, canonical_observation, canonical_record, canonical_step,
    canonical_terminal, canonical_thread, canonical_url, entry_bytes, field_check, history_bytes,
    record_bytes, request_text_bytes, step_schema, web_authority,
};

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn object(members: &[(&str, Value)]) -> Value {
    Value::Object(
        members
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect(),
    )
}

fn with(value: &Value, key: &str, replacement: Value) -> Value {
    let Value::Object(members) = value else {
        panic!("an object");
    };
    let mut members = members.clone();
    let slot = members
        .iter_mut()
        .find(|(name, _)| name == key)
        .expect("the member to replace");
    slot.1 = replacement;
    Value::Object(members)
}

#[test]
fn web_authorities_are_ascii_and_bracket_free() {
    assert_eq!(
        web_authority("HTTPS://user@Example.com:8443/a?b#c"),
        Some(("https", "user@Example.com:8443"))
    );
    assert_eq!(web_authority("hTTp://a?b"), Some(("http", "a")));
    assert_eq!(web_authority("http://a#b"), Some(("http", "a")));
    assert_eq!(web_authority("http://"), Some(("http", "")));
    for url in [
        "ftp://example.com",
        "https:/example.com",
        "http\u{17f}://x",
        "https://\u{212a}eep.com/",
        "https://[::1]/",
        "https://a]/",
        "h\u{e9}",
        "",
    ] {
        assert_eq!(web_authority(url), None, "{url}");
    }
}

#[test]
fn urls_are_absolute_web_urls_with_a_host_and_no_credentials() {
    for url in [
        "https://example.com/a?q=1",
        "http://example.com",
        "https://@example.com/",
        "https://:@example.com/",
        "https://example.com/\u{e9}",
        "https://example.com:bad/",
    ] {
        assert_eq!(canonical_url(&text(url)), Some(url), "{url}");
    }
    let long = format!("https://example.com/{}", "a".repeat(2_048));
    for url in [
        text(""),
        text("https:///path"),
        text("https://user@example.com"),
        text("https://:pw@example.com"),
        text("https://example.com/a b"),
        text("https://example.com/\u{7}"),
        text("https://m\u{fc}nchen.de/"),
        text(&long),
        int(7),
    ] {
        assert_eq!(canonical_url(&url), None, "{url:?}");
    }
}

#[test]
fn objectives_are_plain_bounded_text() {
    assert_eq!(
        canonical_objective(&text("\n find it\t\n")),
        Some("find it")
    );
    for value in [
        text(" \n"),
        text("a\u{7}"),
        text(&"a".repeat(2_001)),
        Value::Null,
    ] {
        assert_eq!(canonical_objective(&value), None, "{value:?}");
    }
}

/// `(field, value, what field_check answers)` with Python's answers.
fn field_cases() -> Vec<(&'static str, Value, Option<Value>)> {
    let refs = Value::Array(vec![text("p1:2"), text("p1:3")]);
    vec![
        ("ref", text("p1:2"), Some(text("p1:2"))),
        ("ref", text("p1:"), None),
        ("refs", text("p1:2"), None),
        ("refs", Value::Array(vec![]), None),
        ("refs", refs.clone(), Some(refs)),
        ("refs", Value::Array(vec![text("p1:2"), text("p1:2")]), None),
        ("refs", Value::Array(vec![text("p1:2"), int(1)]), None),
        ("text", text(""), Some(text(""))),
        ("text", text("a\nb"), None),
        ("direction", text("up"), Some(text("up"))),
        ("direction", text("left"), None),
        ("reason", text("captcha"), Some(text("captcha"))),
        ("summary", text("a\nb"), Some(text("a\nb"))),
        ("summary", text(&"\u{e9}".repeat(2_049)), None),
        ("sentence", text("one line"), Some(text("one line"))),
        ("sentence", text("two\nlines"), None),
        (
            "url",
            text("https://example.com"),
            Some(text("https://example.com")),
        ),
        ("note", text("x"), None),
    ]
}

#[test]
fn fields_are_plain_bounded_text() {
    for (name, value, expected) in field_cases() {
        assert_eq!(field_check(name, &value), expected, "{name} {value:?}");
    }
}

#[test]
fn steps_are_compact_and_closed() {
    let flat = object(&[
        ("kind", text("type")),
        ("url", Value::Null),
        ("ref", text("p1:0")),
        ("text", text("hi")),
        ("note", text("  ")),
    ]);
    let compact = object(&[
        ("kind", text("type")),
        ("ref", text("p1:0")),
        ("text", text("hi")),
    ]);
    assert_eq!(canonical_step(&flat), Some(compact));
    let noted = object(&[("kind", text("wait")), ("note", text("saw it"))]);
    assert_eq!(canonical_step(&noted), Some(noted.clone()));
    for step in [
        text("wait"),
        object(&[("kind", int(1))]),
        object(&[("kind", text("fly"))]),
        object(&[("kind", text("wait")), ("extra", Value::Null)]),
        object(&[("kind", text("wait")), ("url", text("https://example.com"))]),
        object(&[("kind", text("navigate"))]),
        object(&[("kind", text("wait")), ("note", int(1))]),
        object(&[("kind", text("wait")), ("note", text("a\nb"))]),
    ] {
        assert_eq!(canonical_step(&step), None, "{step:?}");
    }
}

fn page() -> Value {
    object(&[("url", text("about:blank")), ("title", text(""))])
}

fn wait() -> Value {
    object(&[
        ("step", object(&[("kind", text("wait"))])),
        ("outcome", text("ok")),
        ("page", page()),
    ])
}

#[test]
fn records_are_one_earlier_step_its_outcome_and_page() {
    assert_eq!(canonical_record(&wait()), Some(wait()));
    let done = object(&[("kind", text("done")), ("summary", text("ok"))]);
    let flat = object(&[("kind", text("wait")), ("url", Value::Null)]);
    for record in [
        text("x"),
        with(&wait(), "outcome", text("lost")),
        with(&wait(), "outcome", int(1)),
        with(&wait(), "step", done),
        with(&wait(), "step", flat),
        with(&wait(), "step", text("wait")),
        with(&wait(), "page", text("x")),
        with(
            &wait(),
            "page",
            object(&[("url", text("a\u{7}")), ("title", text(""))]),
        ),
        with(
            &wait(),
            "page",
            object(&[("url", text("a")), ("title", text(&"a".repeat(301)))]),
        ),
        with(
            &wait(),
            "page",
            object(&[("url", int(1)), ("title", text(""))]),
        ),
    ] {
        assert_eq!(canonical_record(&record), None, "{record:?}");
    }
    assert_eq!(record_bytes(&wait()), 80);
}

fn entry(text_value: &str, downloads: Value) -> Value {
    object(&[
        ("step", object(&[("kind", text("wait"))])),
        ("outcome", text("ok")),
        ("page", page()),
        ("text", text(text_value)),
        ("downloads", downloads),
        ("screenshot", Value::Bool(false)),
    ])
}

fn thread(changes: &[(&str, Value)]) -> Value {
    let mut value = object(&[
        ("folded", Value::Array(vec![])),
        ("folded_omitted", int(0)),
        ("memory", text("")),
        ("unnoted", int(0)),
        ("steps", Value::Array(vec![])),
    ]);
    for (key, replacement) in changes {
        value = with(&value, key, replacement.clone());
    }
    value
}

fn downloads(files: &[&str], pending: i64, truncated: bool) -> Value {
    object(&[
        (
            "files",
            Value::Array(files.iter().map(|name| text(name)).collect()),
        ),
        ("pending", int(pending)),
        ("truncated", Value::Bool(truncated)),
    ])
}

#[test]
fn threads_hold_canonical_records_and_entries_within_their_bounds() {
    let listed = downloads(&["a.pdf"], 1, false);
    let steps = Value::Array(vec![
        entry("page", Value::Null),
        entry("page", listed.clone()),
    ]);
    let admitted = thread(&[
        ("steps", steps.clone()),
        ("memory", text("noted")),
        ("folded", Value::Array(vec![wait()])),
    ]);
    assert_eq!(canonical_thread(&admitted), Some(admitted.clone()));
    // Python's measures of the same values.
    assert_eq!(entry_bytes(&entry("page", Value::Null)), 546);
    assert_eq!(history_bytes(&admitted), 1_734);
    assert_eq!(
        request_text_bytes("find", &admitted, "page", &listed),
        2_303
    );
    assert_eq!(history_bytes(&Value::Null), ENTRY_OVERHEAD_BYTES);
    let reordered = object(&[
        ("truncated", Value::Bool(false)),
        ("pending", int(1)),
        ("files", Value::Array(vec![text("a.pdf")])),
    ]);
    assert!(
        canonical_thread(&thread(&[(
            "steps",
            Value::Array(vec![entry("p", reordered)])
        )]))
        .is_some()
    );
}

#[test]
fn threads_past_their_bounds_are_refused() {
    let too_many = Value::Array(vec![wait(); MAX_STEPS + 1]);
    let long_text = "a".repeat(MAX_OBSERVATION_BYTES + 1);
    for refused in [
        text("x"),
        thread(&[("folded", text("x"))]),
        thread(&[("steps", text("x"))]),
        thread(&[("folded", too_many)]),
        thread(&[("folded_omitted", int(-1))]),
        thread(&[("unnoted", int(90_001))]),
        thread(&[("memory", int(1))]),
        thread(&[("memory", text(" "))]),
        thread(&[("folded", Value::Array(vec![text("x")]))]),
        thread(&[("steps", Value::Array(vec![wait()]))]),
        thread(&[("steps", Value::Array(vec![entry(&long_text, Value::Null)]))]),
        thread(&[(
            "steps",
            Value::Array(vec![entry("p", downloads(&["b", "a"], 0, false))]),
        )]),
        thread(&[(
            "steps",
            Value::Array(vec![with(&entry("p", Value::Null), "screenshot", int(0))]),
        )]),
        thread(&[(
            "steps",
            Value::Array(vec![with(
                &entry("p", Value::Null),
                "outcome",
                text("lost"),
            )]),
        )]),
        thread(&[("folded", Value::Array(vec![wait(); 107]))]),
        thread(&[(
            "steps",
            Value::Array(vec![
                entry(&"a".repeat(MAX_OBSERVATION_BYTES), Value::Null);
                6
            ]),
        )]),
    ] {
        assert_eq!(canonical_thread(&refused), None, "{refused:?}");
    }
}

#[test]
fn memories_and_observations_are_bounded_text() {
    assert_eq!(canonical_memory(&text("a\nb\tc")), Some("a\nb\tc"));
    assert_eq!(
        canonical_memory(&text(&"a".repeat(MAX_MEMORY_BYTES))),
        Some("a".repeat(MAX_MEMORY_BYTES).as_str())
    );
    assert_eq!(
        canonical_memory(&text(&"\u{e9}".repeat(MAX_MEMORY_BYTES / 2 + 1))),
        None
    );
    assert_eq!(canonical_memory(&text("")), None);
    assert_eq!(canonical_observation(&text("")), Some(""));
    assert_eq!(canonical_observation(&int(1)), None);
}

#[test]
fn checks_are_complete_incomplete_with_a_sentence_or_unchecked() {
    let check =
        |complete: Value, missing: Value| object(&[("complete", complete), ("missing", missing)]);
    for value in [
        check(Value::Bool(true), Value::Null),
        check(Value::Null, Value::Null),
        check(Value::Bool(false), text("the date")),
    ] {
        assert_eq!(canonical_check(&value), Some(value.clone()));
    }
    for value in [
        check(Value::Bool(true), text("x")),
        check(Value::Bool(false), Value::Null),
        check(Value::Bool(false), text("a\nb")),
        check(int(1), Value::Null),
        object(&[("complete", Value::Null)]),
    ] {
        assert_eq!(canonical_check(&value), None, "{value:?}");
    }
}

#[test]
fn downloads_are_sorted_distinct_plain_names_and_bounded_counts() {
    let listed = downloads(&["a", "b"], 1_000, true);
    assert_eq!(canonical_downloads(&listed), Some(listed));
    let long = "a".repeat(MAX_NAME_BYTES + 1);
    for value in [
        downloads(&["b", "a"], 0, false),
        downloads(&["a", "a"], 0, false),
        downloads(&[" "], 0, false),
        downloads(&["a\nb"], 0, false),
        downloads(&[&long], 0, false),
        downloads(&[], 1_001, false),
        downloads(&[], -1, false),
        with(&downloads(&[], 0, false), "truncated", int(0)),
        with(&downloads(&[], 0, false), "files", text("a")),
        with(&downloads(&[], 0, false), "pending", Value::Bool(false)),
    ] {
        assert_eq!(canonical_downloads(&value), None, "{value:?}");
    }
}

fn reference() -> Value {
    object(&[
        ("id", text(&"f".repeat(32))),
        ("name", text("edital.pdf")),
        ("media_type", text("application/pdf")),
        ("size", int(2_048)),
    ])
}

#[test]
fn terminals_list_kept_references_named_losses_and_an_unnamed_count() {
    assert_eq!(canonical_file_reference(&reference()), Some(reference()));
    for (key, value) in [
        ("id", text("x")),
        ("name", Value::Null),
        ("name", text("a/b")),
        ("media_type", text("TEXT/PLAIN")),
        ("media_type", Value::Null),
        ("size", int(0)),
        ("size", int(25 * 1024 * 1024 + 1)),
    ] {
        assert_eq!(
            canonical_file_reference(&with(&reference(), key, value.clone())),
            None,
            "{key} {value:?}"
        );
    }
    let loss = |name: &str, reason: Value| object(&[("name", text(name)), ("reason", reason)]);
    let terminal = |files: Vec<Value>, lost: Vec<Value>, skipped: i64| {
        object(&[
            ("files", Value::Array(files)),
            ("lost", Value::Array(lost)),
            ("skipped", int(skipped)),
        ])
    };
    let kept = terminal(vec![reference()], vec![loss("a.pdf", text("empty"))], 1_000);
    assert_eq!(canonical_terminal(&kept), Some(kept));
    for value in [
        terminal(vec![], vec![], 1_001),
        terminal(vec![], vec![], -1),
        terminal(vec![reference(); 9], vec![], 0),
        terminal(vec![], vec![loss("a", text("empty")); 129], 0),
        terminal(vec![text("x")], vec![], 0),
        terminal(vec![], vec![loss("a", text("gone"))], 0),
        terminal(
            vec![],
            vec![loss("a", Value::Array(vec![text("empty")]))],
            0,
        ),
        terminal(vec![], vec![loss("", text("empty"))], 0),
        terminal(vec![], vec![loss("a\u{7}", text("empty"))], 0),
        terminal(vec![], vec![text("x")], 0),
        object(&[
            ("files", text("x")),
            ("lost", Value::Array(vec![])),
            ("skipped", int(0)),
        ]),
    ] {
        assert_eq!(canonical_terminal(&value), None, "{value:?}");
    }
}

#[test]
fn the_step_schema_is_the_flat_closed_object_of_every_field() {
    let schema = step_schema();
    assert_eq!(schema, &*STEP_SCHEMA);
    let Some(Value::Array(required)) = schema.get("required") else {
        panic!("required fields");
    };
    assert_eq!(required.len(), 10);
    let url = schema
        .get("properties")
        .and_then(|properties| properties.get("url"));
    let expected = object(&[(
        "anyOf",
        Value::Array(vec![
            object(&[("type", text("string")), ("maxLength", int(2_048))]),
            object(&[("type", text("null"))]),
        ]),
    )]);
    assert_eq!(url, Some(&expected));
}

#[test]
fn threads_hold_exactly_at_their_count_bounds() {
    // Python admits 100 folded lines with 200 steps (300 records in all) and refuses one step more.
    let folded = Value::Array(vec![wait(); 100]);
    let steps = |count| Value::Array(vec![entry("page", Value::Null); count]);
    let full = thread(&[("folded", folded.clone()), ("steps", steps(200))]);
    assert_eq!(canonical_thread(&full), Some(full.clone()));
    let over = thread(&[("folded", folded), ("steps", steps(201))]);
    assert_eq!(canonical_thread(&over), None);
    let unnoted = thread(&[("unnoted", int(90_000))]);
    assert_eq!(canonical_thread(&unnoted), Some(unnoted.clone()));
}

#[test]
fn terminals_hold_exactly_at_their_count_bounds() {
    let loss = object(&[("name", text("a")), ("reason", text("empty"))]);
    let full = object(&[
        ("files", Value::Array(vec![reference(); 8])),
        ("lost", Value::Array(vec![loss; 128])),
        ("skipped", int(0)),
    ]);
    assert_eq!(canonical_terminal(&full), Some(full.clone()));
}
