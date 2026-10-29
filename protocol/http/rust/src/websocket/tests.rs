use strict_json::{Number, Value};

use super::{
    FrameError, InvalidText, MAX_HUMAN_CHOICE_CHARS, MAX_HUMAN_CHOICES, MAX_HUMAN_TEXT_CHARS,
    authority, canonical_human_response, canonical_origin, challenge_identity,
    decode_bounded_json_frame, error_terminal, human_value, port, public_text, safe_status, scheme,
    valid_challenge_id,
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

#[test]
fn origins_keep_a_lowercase_scheme_and_authority_and_an_explicit_port() {
    for (origin, expected) in [
        ("HTTPS://Example.COM:443", "https://example.com:443"),
        ("http://a", "http://a"),
        ("http://a:", "http://a:"),
        ("https://a:00000000080", "https://a:00000000080"),
        ("http://a;b", "http://a;b"),
    ] {
        assert_eq!(canonical_origin(Some(origin)).as_deref(), Some(expected));
    }
    for origin in [
        None,
        Some(""),
        Some("null"),
        Some(" http://a"),
        Some("http://a\u{7f}"),
        Some("http://\u{e9}"),
        Some("http://[::1]"),
        Some("http://a]"),
        Some("http://a?"),
        Some("http://a#"),
        Some("http://a/"),
        Some("ftp://a"),
        Some("http:a"),
        Some("1http://a"),
        Some("http://:80"),
        Some("http://u@a"),
        Some("http://a:x"),
        Some("http://a:70000"),
        Some("http://a:1234567"),
    ] {
        assert_eq!(canonical_origin(origin), None, "{origin:?}");
    }
}

#[test]
fn authorities_split_as_urllib_splits_them() {
    let split = |text| {
        let parts = authority(text);
        (parts.userinfo, parts.host, parts.port)
    };
    assert_eq!(
        [
            split("user:pw:x@host:80:1"),
            split("a@b@host"),
            split("host:")
        ],
        [
            (Some(("user", Some("pw:x"))), "host", Some("80:1")),
            (Some(("a@b", None)), "host", None),
            (None, "host", None),
        ]
    );
    let ports = [
        None,
        Some("0"),
        Some("65535"),
        Some("00065536"),
        Some("100000"),
        Some("4 3"),
    ]
    .map(port);
    assert_eq!(
        ports,
        [
            Ok(None),
            Ok(Some(0)),
            Ok(Some(65_535)),
            Err(()),
            Err(()),
            Err(())
        ]
    );
    let schemes = ["HTTP+x.1-:rest", ":rest", "a_b:rest", "rest"].map(scheme);
    assert_eq!(
        schemes,
        [
            ("http+x.1-".to_owned(), "rest"),
            (String::new(), ":rest"),
            (String::new(), "a_b:rest"),
            (String::new(), "rest"),
        ]
    );
}

#[test]
fn public_text_is_trimmed_bounded_and_free_of_other_and_separators() {
    assert_eq!(public_text(&text("Ready"), 5), Ok("Ready"));
    assert_eq!(
        public_text(&text("fran\u{e7}ais\u{a0}?"), 10),
        Ok("fran\u{e7}ais\u{a0}?")
    );
    for value in [
        Value::Null,
        text(""),
        text(" bad"),
        text("bad\n"),
        text("toolong"),
        text("a\u{2028}b"),
        text("a\u{2029}b"),
        text("a\u{200d}b"),
    ] {
        assert_eq!(public_text(&value, 6), Err(InvalidText), "{value:?}");
    }
    assert_eq!(InvalidText.to_string(), "invalid public text");
}

fn receive(text: &str) -> Vec<(String, Value)> {
    vec![
        (
            "type".to_owned(),
            Value::String("websocket.receive".to_owned()),
        ),
        ("text".to_owned(), Value::String(text.to_owned())),
    ]
}

#[test]
fn frames_decode_to_bounded_objects_or_their_exact_error() {
    let value = decode_bounded_json_frame(&receive(r#"{"a":1}"#), 7).expect("a frame");
    assert_eq!(value, object(&[("a", int(1))]));
    let error = |status, detail, close_code| FrameError {
        status,
        detail,
        close_code,
    };
    let mut other = receive("{}");
    other[0].1 = Value::String("websocket.send".to_owned());
    assert_eq!(
        decode_bounded_json_frame(&other, 9),
        Err(error(400, "invalid WebSocket frame", 1007))
    );
    let mut binary = receive("{}");
    binary.push(("bytes".to_owned(), Value::String("00".to_owned())));
    let binary_error = error(415, "WebSocket frame must be text JSON", 1003);
    assert_eq!(decode_bounded_json_frame(&binary, 9), Err(binary_error));
    let mut nulled = receive("{}");
    nulled.push(("bytes".to_owned(), Value::Null));
    assert!(decode_bounded_json_frame(&nulled, 9).is_ok());
    assert_eq!(
        decode_bounded_json_frame(&receive("{}")[..1], 9),
        Err(binary_error)
    );
    assert_eq!(
        decode_bounded_json_frame(&receive(r#"{"a":1}"#), 6),
        Err(error(413, "WebSocket frame too large", 1009))
    );
    assert_eq!(
        decode_bounded_json_frame(&receive(r#"{"a":1,"a":2}"#), 99),
        Err(error(
            400,
            "WebSocket frame must be valid unique-key JSON",
            1007
        ))
    );
    assert_eq!(
        decode_bounded_json_frame(&receive("[]"), 99),
        Err(error(400, "WebSocket JSON must be an object", 1007))
    );
    assert_eq!(
        binary_error.to_string(),
        "WebSocket frame must be text JSON"
    );
}

#[test]
fn error_terminals_carry_a_safe_status_and_a_safe_detail() {
    let terminal =
        |status: Value, detail: &str, maximum| error_terminal(&status, detail, "fallback", maximum);
    let expected = |status, detail: &str| {
        object(&[
            ("type", text("error")),
            ("status", int(status)),
            ("detail", text(detail)),
        ])
    };
    assert_eq!(terminal(int(404), "missing", 7), expected(404, "missing"));
    assert_eq!(terminal(int(400), "missing", 6), expected(400, "fallback"));
    assert_eq!(terminal(int(599), "", 6), expected(599, "fallback"));
    assert_eq!(terminal(int(600), "x\u{1f}", 6), expected(502, "fallback"));
    assert_eq!(terminal(int(399), "x\u{7f}", 6), expected(502, "fallback"));
    assert_eq!(
        terminal(Value::Bool(true), "\u{80}", 6),
        expected(502, "\u{80}")
    );
    assert_eq!(safe_status(&Value::Number(Number::Float(404.0))), 502);
    assert_eq!(safe_status(&text("404")), 502);
}

#[test]
fn challenge_identities_bind_one_id_of_the_expected_team() {
    let id = "a".repeat(32);
    let value = |team: &str, challenge: Value, turn: Value| {
        object(&[
            ("team_id", text(team)),
            ("challenge_id", challenge),
            ("turn_id", turn),
        ])
    };
    let good = value("team_1", text(&id), text(&id));
    assert_eq!(
        challenge_identity(&good, "team_1"),
        Some((id.as_str(), id.as_str()))
    );
    let refused = [
        (good.clone(), "team_2"),
        (value("team_1", text(&id), text(&"b".repeat(32))), "team_1"),
        (value("team_1", text("x"), text("x")), "team_1"),
        (text("x"), "team_1"),
        (object(&[("challenge_id", text(&id))]), "team_1"),
        (object(&[("turn_id", text(&id))]), "team_1"),
    ];
    for (value, team) in &refused {
        assert_eq!(challenge_identity(value, team), None, "{value:?}");
    }
    let ids = [text(&id), text(&"A".repeat(32)), int(1)].map(|value| valid_challenge_id(&value));
    assert_eq!(ids, [true, false, false]);
}

#[test]
fn human_responses_are_exact_closed_frames() {
    let id = text(&"a".repeat(32));
    let submit = object(&[
        ("type", text("human-response")),
        ("challenge_id", id.clone()),
        ("decision", text("submit")),
        ("value", Value::Bool(true)),
    ]);
    assert_eq!(canonical_human_response(&submit), Ok(submit.clone()));
    let deny = object(&[
        ("type", text("human-response")),
        ("challenge_id", id.clone()),
        ("decision", text("deny")),
    ]);
    assert_eq!(canonical_human_response(&deny), Ok(deny.clone()));
}

#[test]
fn other_human_response_frames_are_refused() {
    let id = text(&"a".repeat(32));
    let refused = Err(FrameError {
        status: 400,
        detail: "invalid human response frame",
        close_code: 1007,
    });
    for value in [
        text("x"),
        object(&[
            ("type", text("human-response")),
            ("challenge_id", id.clone()),
            ("decision", text("maybe")),
        ]),
        object(&[
            ("type", text("other")),
            ("challenge_id", id.clone()),
            ("decision", text("deny")),
        ]),
        object(&[
            ("type", text("human-response")),
            ("challenge_id", text("x")),
            ("decision", text("deny")),
        ]),
        object(&[("type", text("human-response")), ("decision", text("deny"))]),
        object(&[
            ("type", text("human-response")),
            ("challenge_id", id.clone()),
            ("decision", text("submit")),
        ]),
        object(&[
            ("type", text("human-response")),
            ("challenge_id", id.clone()),
            ("decision", text("submit")),
            ("value", Value::Bool(false)),
        ]),
        object(&[
            ("type", text("human-response")),
            ("challenge_id", id),
            ("decision", text("deny")),
            ("value", Value::Bool(true)),
        ]),
    ] {
        assert_eq!(canonical_human_response(&value), refused, "{value:?}");
    }
}

#[test]
fn human_values_are_an_approval_bounded_text_or_distinct_bounded_choices() {
    let choices = |count: usize, length: usize| {
        Value::Array(
            (0..count)
                .map(|index| text(&format!("{index:0length$}")))
                .collect(),
        )
    };
    let values = [
        Value::Bool(true),
        Value::Bool(false),
        int(1),
        text(&"a".repeat(MAX_HUMAN_TEXT_CHARS)),
        text(&"a".repeat(MAX_HUMAN_TEXT_CHARS + 1)),
        choices(MAX_HUMAN_CHOICES, MAX_HUMAN_CHOICE_CHARS),
        choices(MAX_HUMAN_CHOICES + 1, 3),
        choices(1, MAX_HUMAN_CHOICE_CHARS + 1),
        Value::Array(vec![text("a"), text("a")]),
        Value::Array(vec![text("a"), int(1)]),
        Value::Array(vec![]),
    ];
    let admitted = values.map(|value| human_value(&value));
    assert_eq!(
        admitted,
        [
            true, false, false, true, false, true, false, false, false, false, true
        ]
    );
}
