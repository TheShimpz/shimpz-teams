use std::time::Instant;

use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use strict_json::Value;

use super::{ActionFailure, MAX_ERROR_TYPE, MAX_TEXT_BYTES, REDACTED, admit, is_failure};

const TOKEN: &str = "oauth-Access-Token-7f3a9c";
const STORED: &str = "stored/input+value=42";
const REGISTERED: &str = "registered-derived-answer";
const DERIVED: &str = "derivedsecret0123456789abcdef";

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn envelope(changes: &[(&str, Value)]) -> Value {
    let mut failure = vec![
        ("error_type".to_owned(), text("httpx.HTTPStatusError")),
        ("message".to_owned(), text("Client error '404 Not Found'")),
        ("provider".to_owned(), text("api.example.com")),
        (
            "http_status".to_owned(),
            Value::Number(strict_json::Number::Int(404)),
        ),
        ("response_excerpt".to_owned(), text(r#"{"errors":[]}"#)),
        ("redacted".to_owned(), Value::Bool(false)),
        ("truncated".to_owned(), Value::Bool(false)),
    ];
    for (name, value) in changes {
        let slot = failure
            .iter_mut()
            .find(|(key, _)| key == name)
            .expect("a failure member");
        slot.1 = value.clone();
    }
    Value::Object(vec![
        ("type".to_owned(), text("failure")),
        ("failure".to_owned(), Value::Object(failure)),
    ])
}

fn hex(raw: &[u8]) -> String {
    use std::fmt::Write as _;
    raw.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

fn message(value: &str, secrets: &[&str]) -> ActionFailure {
    admit(&envelope(&[("message", text(value))]), secrets).expect("admitted")
}

#[test]
fn team_admission_matches_every_published_failure_vector() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../protocol/assistant/v1/vectors/failure.json"
    );
    let vectors = strict_json::parse(&std::fs::read(path).expect("vectors")).expect("json");
    let Some(Value::Array(cases)) = vectors.get("cases") else {
        panic!("cases")
    };
    for case in cases {
        let admitted = admit::<&str>(case.get("response").expect("response"), &[]);
        assert_eq!(
            Some(&Value::Bool(admitted.is_ok())),
            case.get("valid"),
            "{:?}",
            case.get("name")
        );
    }
    assert_eq!(cases.len(), 37);
}

#[test]
fn a_clean_diagnostic_is_kept_exactly() {
    let clean = envelope(&[
        ("redacted", Value::Bool(true)),
        ("truncated", Value::Bool(true)),
    ]);
    assert_eq!(
        Some(&admit(&clean, &[TOKEN]).expect("admitted").document()),
        clean.get("failure")
    );
    assert!(is_failure(&clean));
    assert!(!is_failure(
        &strict_json::parse(br#"{"type":"result","result":{}}"#).expect("json")
    ));
    assert!(!is_failure(&Value::Array(vec![text("failure")])));
}

#[test]
fn every_member_is_re_redacted_with_each_injected_value() {
    let leaky = envelope(&[
        ("error_type", text(&format!("Leak{TOKEN}Error"))),
        (
            "message",
            text(&format!(
                "stored value {STORED} and registered {} were refused",
                REGISTERED.to_uppercase()
            )),
        ),
        ("provider", text(&format!("{DERIVED}.example.com"))),
        (
            "response_excerpt",
            text(&format!(r#"{{"token":"{TOKEN}"}}"#)),
        ),
    ]);
    let failure = admit(&leaky, &[TOKEN, STORED, REGISTERED, DERIVED]).expect("admitted");
    let rendered = format!("{:?}", failure.document());
    assert!(
        ![
            TOKEN,
            STORED,
            REGISTERED,
            &REGISTERED.to_uppercase(),
            DERIVED
        ]
        .iter()
        .any(|secret| rendered.contains(*secret))
    );
    assert_eq!(
        (failure.error_type.as_str(), failure.provider.as_deref()),
        ("Leak[REDACTED]Error", None)
    );
    assert_eq!(
        (failure.http_status, failure.redacted, failure.truncated),
        (Some(404), true, false)
    );
}

#[test]
fn failure_redaction_every_encoding_and_clipped_prefix() {
    let raw = STORED.as_bytes();
    let quoted = super::percent(STORED, false);
    let encoded = [
        "stored\\/input+value=42".to_owned(),
        quoted.clone(),
        quoted.to_lowercase(),
        hex(raw),
        hex(raw).to_uppercase(),
        STANDARD.encode(raw),
        URL_SAFE_NO_PAD.encode(raw),
        format!(
            "Authorization {}",
            STANDARD.encode([b"user:".as_slice(), raw].concat())
        ),
        STANDARD.encode([b"ab".as_slice(), raw, b"tail"].concat()),
    ];
    for form in &encoded {
        let failure = message(&format!("rejected {form} here"), &[STORED]);
        assert!(
            failure.redacted
                && failure.message.contains(REDACTED)
                && !failure.message.contains(form.as_str()),
            "{form}"
        );
    }
    let unicode = "pässwörd-ünïcode";
    assert!(
        !message(
            &format!(r#"{{"v": "{}"}}"#, super::json_body(unicode, true)),
            &[unicode]
        )
        .message
        .contains("u00e4")
    );
}

#[test]
fn secret_shaped_text_is_consumed_whole_through_the_end_of_its_value() {
    let cases = [
        (
            "Authorization: Bearer abc.def-ghi/jkl",
            "Authorization: [REDACTED]",
        ),
        ("token=abc def", "token=[REDACTED] def"),
        (
            r#"{"password": "a b \" c"} after"#,
            r#"{"password": [REDACTED]} after"#,
        ),
        (
            "client_secret='unterminated value",
            "client_secret=[REDACTED]",
        ),
        (
            "api_key: sk-proj-ABCDEFGHIJKLmnop_qrst-uvwx and more",
            "api_key: [REDACTED] and more",
        ),
        (
            "key sk-proj-ABCDEFGHIJKLmnop_qrst-uvwx.extra",
            "key [REDACTED]",
        ),
        (
            "jwt eyJhbGciOi.eyJzdWIiOjF9.c2lnbmF0dXJl end",
            "jwt [REDACTED] end",
        ),
        (
            "pem -----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----- ok -----BEGIN RSA PRIVATE KEY-----\nMIIabc\n",
            "pem [REDACTED] ok [REDACTED]",
        ),
        (
            "proxy https://user:pass@proxy.local:3128 failed",
            "proxy https://[REDACTED]@proxy.local:3128 failed",
        ),
        ("token=[REDACTED] already", "token=[REDACTED] already"),
        ("PASSWORD=ı-dotless", "PASSWORD=[REDACTED]"),
    ];
    let wrong: Vec<_> = cases
        .iter()
        .filter(|(text, expected)| message(text, &[]).message != *expected)
        .collect();
    assert!(wrong.is_empty(), "{wrong:?}");
    assert!(!message("token=[REDACTED] already", &[]).redacted);
}

#[test]
fn a_clipped_trailing_secret_is_withheld_rather_than_shown_cut() {
    let clips = [
        TOKEN[..4].to_owned(),
        TOKEN[..TOKEN.len() - 1].to_owned(),
        TOKEN[..12].to_uppercase(),
        super::percent(STORED, false)[..9].to_owned(),
    ];
    for clipped in clips {
        let failure = admit(
            &envelope(&[
                ("message", text(&format!("request failed for {clipped}"))),
                ("truncated", Value::Bool(true)),
            ]),
            &[TOKEN, STORED],
        )
        .expect("admitted");
        assert_eq!(
            failure.message, "request failed for [REDACTED]",
            "{clipped}"
        );
        assert!(failure.redacted && failure.truncated);
    }
    // Three characters are too short to identify a value; a value elsewhere in the text is not a clipped tail.
    assert_eq!(message("ends oau", &[TOKEN]).message, "ends oau");
    assert_eq!(
        message(&format!("{} first", &TOKEN[..8]), &[TOKEN]).message,
        format!("{} first", &TOKEN[..8])
    );
}

#[test]
fn redaction_runs_before_the_bound_and_marks_the_cut() {
    let failure = admit(
        &envelope(&[
            ("error_type", text(&format!("E{}", "s3".repeat(63)))),
            ("message", text(&format!("{}s3", "é".repeat(1023)))),
        ]),
        &["s3"],
    )
    .expect("admitted");
    assert_eq!(failure.error_type.chars().count(), MAX_ERROR_TYPE);
    assert!(
        failure.error_type.starts_with("E[REDACTED]") && failure.message.len() <= MAX_TEXT_BYTES
    );
    assert!(!format!("{}{}", failure.error_type, failure.message).contains("s3"));
    assert!(failure.redacted && failure.truncated);
    let wrapped = Value::Object(vec![
        ("type".to_owned(), text("failure")),
        ("failure".to_owned(), failure.document()),
    ]);
    assert_eq!(assistant_protocol::failure::failure_error(&wrapped), None);
}

#[test]
fn an_empty_value_or_a_lone_surrogate_never_passes() {
    assert_eq!(message("nothing to hide", &[""]).message, "nothing to hide");
    let lone = br#"{"type":"failure","failure":{"error_type":"E","message":"lone \ud800 surrogate","provider":null,"http_status":null,"response_excerpt":null,"redacted":false,"truncated":false}}"#;
    assert!(strict_json::parse(lone).map_or(true, |value| admit::<&str>(&value, &[]).is_err()));
}

#[test]
fn short_derived_encodings_never_erase_ordinary_text() {
    let failure = admit(
        &envelope(&[
            ("message", text("QQ is 51 in hex")),
            ("response_excerpt", Value::Null),
        ]),
        &["Q"],
    )
    .expect("admitted");
    assert_eq!(
        (failure.message.as_str(), failure.response_excerpt),
        ("[REDACTED][REDACTED] is 51 in hex", None)
    );
}

#[test]
fn adversarial_text_stays_cheap_to_sanitize() {
    let secrets: Vec<String> = (0..14)
        .map(|index| format!("{}b", "a".repeat(16_000 - index)))
        .collect();
    let started = Instant::now();
    let failure = admit(
        &envelope(&[
            ("message", text(&format!("{}c", "a".repeat(2047)))),
            ("response_excerpt", text(&"a".repeat(2048))),
        ]),
        &secrets,
    )
    .expect("admitted");
    assert!(started.elapsed().as_millis() < 500);
    assert_eq!(
        (failure.message, failure.response_excerpt.as_deref()),
        (format!("{}c", "a".repeat(2047)), Some(REDACTED))
    );
}

#[test]
fn a_kept_failure_is_re_redacted_or_withheld_for_a_run() {
    let failure = message(&format!("later {REGISTERED}"), &[]);
    assert_eq!(
        failure.redacted_with(&[REGISTERED]).message,
        "later [REDACTED]"
    );
    let withheld = failure.withheld();
    assert_eq!(
        (
            withheld.error_type.as_str(),
            withheld.message.as_str(),
            withheld.http_status,
            withheld.redacted
        ),
        ("withheld", "", Some(404), true)
    );
}
