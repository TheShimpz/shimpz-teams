//! Every `vectors.json` group of `websocket`, `sign_in`, and `browser`, with the bound checks `verify.py` makes, and the
//! URL canonicalization vectors, checked with typed equality.

use crate::support::vectors::{admits_group, group, items, same, text};
use strict_json::{Number, Value};
use team_protocol::{browser, sign_in, websocket};

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn string(text: &str) -> Value {
    Value::String(text.to_owned())
}

fn int(value: usize) -> Value {
    Value::Number(Number::Int(i64::try_from(value).expect("fits")))
}

fn status(case: &Value, key: &str) -> u16 {
    match case.get(key) {
        Some(Value::Number(Number::Int(code))) => u16::try_from(*code).expect("a status"),
        other => panic!("no {key}: {other:?}"),
    }
}

#[test]
fn frame_vectors() {
    for case in items(group("frames")) {
        let Some(Value::Object(message)) = case.get("message") else {
            panic!("a frame case has a message");
        };
        let message: Vec<(String, Value)> = message
            .iter()
            .map(|(key, value)| match key.as_str() {
                "bytes_hex" => ("bytes".to_owned(), value.clone()),
                _ => (key.clone(), value.clone()),
            })
            .collect();
        let Some(Value::Number(Number::Int(maximum))) = case.get("max_bytes") else {
            panic!("a frame case has a bound");
        };
        let decoded = websocket::decode_bounded_json_frame(&message, *maximum);
        match (decoded, case.get("valid")) {
            (Ok(value), Some(Value::Bool(true))) => {
                assert!(
                    same(&value, case.get("value").expect("a value")),
                    "{case:?}"
                );
            }
            (Err(error), Some(Value::Bool(false))) => {
                assert_eq!(error.status, status(case, "status"), "{case:?}");
                assert_eq!(error.close_code, status(case, "close_code"), "{case:?}");
            }
            (other, _) => panic!("{case:?} decoded to {other:?}"),
        }
    }
}

#[test]
fn human_response_vectors() {
    for case in items(group("human_response_frames")) {
        let frame = case.get("frame").expect("a frame");
        match (
            websocket::canonical_human_response(frame),
            case.get("valid"),
        ) {
            (Ok(value), Some(Value::Bool(true))) => assert!(same(&value, frame), "{case:?}"),
            (Err(error), Some(Value::Bool(false))) => assert_eq!(error.status, 400),
            (other, _) => panic!("{case:?} answered {other:?}"),
        }
    }
}

#[test]
fn sign_in_vectors() {
    for (path, name) in [
        ("sign_in_start", "sign_in.canonical_start"),
        ("sign_in_session", "sign_in.canonical_session"),
        ("sign_in_delete", "sign_in.canonical_delete"),
        ("sign_in_started", "sign_in.canonical_started"),
        ("sign_in_current", "sign_in.canonical_current"),
        ("sign_in_saved", "sign_in.canonical_saved"),
        ("sign_in_deleted", "sign_in.canonical_deleted"),
        ("sign_in_cancelled", "sign_in.canonical_cancelled"),
    ] {
        admits_group(name, path);
    }
}

#[test]
fn browser_vectors() {
    for kind in [
        "url",
        "objective",
        "step",
        "thread",
        "check",
        "observation",
        "terminal",
        "downloads",
    ] {
        admits_group(
            &format!("browser.canonical_{kind}"),
            &format!("browser.{kind}"),
        );
    }
    for case in items(group("browser_blank_note")) {
        let canonical =
            browser::canonical_step(case.get("step").expect("a step")).expect("admitted");
        assert!(same(&canonical, case.get("canonical").expect("canonical")));
    }
}

fn wait() -> Value {
    object(vec![
        ("step", object(vec![("kind", string("wait"))])),
        ("outcome", string("ok")),
        (
            "page",
            object(vec![("url", string("about:blank")), ("title", string(""))]),
        ),
    ])
}

fn empty_thread(changes: Vec<(&str, Value)>) -> Value {
    let mut thread = vec![
        ("folded", Value::Array(vec![])),
        ("folded_omitted", int(0)),
        ("memory", string("")),
        ("unnoted", int(0)),
        ("steps", Value::Array(vec![])),
    ];
    for (key, value) in changes {
        let slot = thread
            .iter_mut()
            .find(|(name, _)| *name == key)
            .expect("a thread member");
        slot.1 = value;
    }
    object(thread)
}

fn with(value: &Value, key: &str, replacement: &Value) -> Value {
    let Value::Object(members) = value else {
        panic!("an object");
    };
    Value::Object(
        members
            .iter()
            .map(|(name, member)| {
                (
                    name.clone(),
                    if name == key {
                        replacement.clone()
                    } else {
                        member.clone()
                    },
                )
            })
            .collect(),
    )
}

fn over_threads() -> Vec<Value> {
    let lines = browser::MAX_FOLDED_BYTES / browser::record_bytes(&wait());
    let page_step = object(vec![
        ("step", object(vec![("kind", string("wait"))])),
        ("outcome", string("ok")),
        (
            "page",
            object(vec![("url", string("about:blank")), ("title", string(""))]),
        ),
        ("text", string(&"a".repeat(browser::MAX_OBSERVATION_BYTES))),
        ("downloads", Value::Null),
        ("screenshot", Value::Bool(false)),
    ]);
    vec![
        empty_thread(vec![("folded", Value::Array(vec![wait(); lines + 1]))]),
        empty_thread(vec![("memory", Value::Null)]),
        empty_thread(vec![(
            "unnoted",
            int(browser::MAX_STEPS * browser::MAX_STEPS + 1),
        )]),
        empty_thread(vec![(
            "memory",
            string(&"a".repeat(browser::MAX_MEMORY_BYTES + 1)),
        )]),
        empty_thread(vec![("steps", Value::Array(vec![page_step; 6]))]),
    ]
}

fn over_steps() -> Vec<Value> {
    let refs: Vec<Value> = (0..=browser::MAX_READ_REFS)
        .map(|n| string(&format!("p1:{n}")))
        .collect();
    vec![
        object(vec![
            ("kind", string("done")),
            (
                "summary",
                string(&"\u{e9}".repeat(browser::MAX_SUMMARY_BYTES / 2 + 1)),
            ),
        ]),
        object(vec![
            ("kind", string("type")),
            ("ref", string("p1:0")),
            ("text", string(&"a".repeat(browser::MAX_TEXT_CHARS + 1))),
        ]),
        object(vec![("kind", string("read")), ("refs", Value::Array(refs))]),
        object(vec![
            ("kind", string("wait")),
            ("note", string(&"a".repeat(browser::MAX_NOTE_CHARS + 1))),
        ]),
    ]
}

#[test]
fn browser_values_past_their_bounds_are_refused() {
    let long_url = format!("https://example.com/{}", "a".repeat(browser::MAX_URL_CHARS));
    assert!(browser::canonical_url(&string(&long_url)).is_none());
    let objective = "a".repeat(browser::MAX_OBJECTIVE_CHARS + 1);
    assert!(browser::canonical_objective(&string(&objective)).is_none());
    for step in over_steps() {
        assert!(browser::canonical_step(&step).is_none(), "{step:?}");
    }
    for thread in over_threads() {
        assert!(browser::canonical_thread(&thread).is_none());
    }
    let long_title = object(vec![
        ("url", string("about:blank")),
        ("title", string(&"a".repeat(browser::MAX_TITLE_CHARS + 1))),
    ]);
    let long_page = object(vec![("url", string(&long_url)), ("title", string(""))]);
    for page in [long_title, long_page] {
        assert!(browser::canonical_record(&with(&wait(), "page", &page)).is_none());
    }
    let observation = "a".repeat(browser::MAX_OBSERVATION_BYTES + 1);
    assert!(browser::canonical_observation(&string(&observation)).is_none());
}

#[test]
fn browser_terminals_and_downloads_past_their_bounds_are_refused() {
    let kept = items(group("browser.terminal.valid"))[0].clone();
    let repeat = |key: &str, times: usize| {
        let Some(Value::Array(members)) = kept.get(key) else {
            panic!("a list");
        };
        Value::Array(
            members
                .iter()
                .cycle()
                .take(members.len() * times)
                .cloned()
                .collect(),
        )
    };
    let long_name = object(vec![
        ("name", string(&"a".repeat(browser::MAX_NAME_BYTES + 1))),
        ("reason", string("empty")),
    ]);
    for terminal in [
        with(
            &kept,
            "files",
            &repeat("files", browser::MAX_TERMINAL_FILES + 1),
        ),
        with(
            &kept,
            "lost",
            &repeat("lost", browser::MAX_TERMINAL_LOSSES + 1),
        ),
        with(&kept, "lost", &Value::Array(vec![long_name])),
    ] {
        assert!(browser::canonical_terminal(&terminal).is_none());
    }
}

#[test]
fn browser_downloads_past_their_bounds_are_refused() {
    let names: Vec<Value> = (0..=browser::MAX_DOWNLOAD_NAMES)
        .map(|n| string(&format!("f{n:02}")))
        .collect();
    let downloads = |files: Vec<Value>, pending: usize, truncated: bool| {
        object(vec![
            ("files", Value::Array(files)),
            ("pending", int(pending)),
            ("truncated", Value::Bool(truncated)),
        ])
    };
    for value in [
        downloads(names, 0, true),
        downloads(vec![string(&"\u{e9}".repeat(128))], 0, false),
        downloads(
            vec![],
            usize::try_from(browser::MAX_PENDING_DOWNLOADS).expect("fits") + 1,
            false,
        ),
    ] {
        assert!(browser::canonical_downloads(&value).is_none());
    }
}

#[test]
fn a_thread_at_its_bounds_is_admitted_and_measured_whole() {
    let lines = browser::MAX_FOLDED_BYTES / browser::record_bytes(&wait());
    let at_bound = empty_thread(vec![
        ("folded", Value::Array(vec![wait(); lines])),
        ("memory", string(&"a".repeat(browser::MAX_MEMORY_BYTES))),
    ]);
    let admitted = browser::canonical_thread(&at_bound).expect("admitted at its bounds");
    assert!(same(&admitted, &at_bound));
    let measured = browser::request_text_bytes("objective", &at_bound, "page", &Value::Null);
    assert!(measured >= browser::history_bytes(&at_bound) + "objective".len() + "page".len());
}

#[test]
fn url_canonicalization_vectors() {
    let cases = group("url_canonicalization");
    let named = items(cases.get("named").expect("named"));
    for case in named
        .iter()
        .chain(items(cases.get("corpus").expect("corpus")))
    {
        let url = text(case.get("url").expect("a url"));
        let optional = |answer: Option<&str>| answer.map_or(Value::Null, string);
        let origin = websocket::canonical_origin(Some(url));
        assert!(
            same(
                &optional(origin.as_deref()),
                case.get("origin").expect("origin")
            ),
            "{url:?}"
        );
        let candidate = string(url);
        let browser_url = browser::canonical_url(&candidate);
        assert!(
            same(
                &optional(browser_url),
                case.get("browser_url").expect("browser url")
            ),
            "{url:?}"
        );
        let site = sign_in::start_site(&candidate);
        assert!(
            same(
                &optional(site.as_deref()),
                case.get("start_site").expect("site")
            ),
            "{url:?}"
        );
    }
}

#[test]
fn kept_files_and_losses_are_exact() {
    let kept = items(group("browser.terminal.valid"))[0].clone();
    let Some(Value::Array(files)) = kept.get("files") else {
        panic!("kept files");
    };
    let reference = files[0].clone();
    for (key, value) in [
        ("name", Value::Null),
        ("name", string("a/b")),
        ("media_type", string("TEXT/PLAIN")),
        ("size", int(0)),
    ] {
        assert!(browser::canonical_file_reference(&with(&reference, key, &value)).is_none());
    }
    for name in ["", "a\u{7}"] {
        let loss = object(vec![("name", string(name)), ("reason", string("empty"))]);
        let terminal = with(&kept, "lost", &Value::Array(vec![loss]));
        assert!(browser::canonical_terminal(&terminal).is_none());
    }
    let downloads = object(vec![
        ("files", string("a")),
        ("pending", int(0)),
        ("truncated", Value::Bool(false)),
    ]);
    assert!(browser::canonical_downloads(&downloads).is_none());
}
