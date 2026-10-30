//! The vector groups of `content`, `preview`, `browser_view`, `challenge`, and `supervisor`, checked as `verify.py`
//! checks them with typed equality, and the edges of each validator the vectors leave open.

use crate::support::registry::Outcome;
use crate::support::vectors::{admits, admits_group, call, group, items, same, text};
use strict_json::{Number, Value};
use team_protocol::supervisor::{
    self, ASSERTION_AUDIENCE, ROUTINE_AUDIENCE, SupervisorAssertionError,
};
use team_protocol::{browser_view, challenge, preview};

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

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

#[test]
fn framing_view_and_projection_vectors() {
    for (path, name) in [
        ("file_content", "content.canonical_framing"),
        ("file_preview", "preview.canonical_framing"),
        ("file_page_preview", "preview.canonical_page_framing"),
        ("browser_view", "browser_view.canonical_view"),
        ("browser_view_key", "browser_view.canonical_key"),
        ("file_disclosure", "challenge.canonical_file_disclosure"),
        ("input_projection", "challenge.canonical_input_projection"),
        (
            "supervisor_key_rotation",
            "supervisor.canonical_key_rotation",
        ),
    ] {
        admits_group(name, path);
    }
}

#[test]
fn page_vectors_name_their_page() {
    for case in items(group("file_page.valid")) {
        let page = preview::canonical_page(case).expect("a valid page");
        assert_eq!(page.to_string(), text(case));
    }
    for case in items(group("file_page.invalid")) {
        assert_eq!(preview::canonical_page(case), None, "{case:?}");
    }
}

#[test]
fn accept_and_header_vectors() {
    for case in items(group("browser_view_accept")) {
        let key = text(case.get("key").expect("a key"));
        assert_eq!(
            browser_view::accept(key),
            text(case.get("accept").expect("an accept"))
        );
    }
    assert_eq!(
        group("headers"),
        &object(vec![(
            "local_supervisor",
            string(supervisor::ASSERTION_HEADER)
        )])
    );
}

#[test]
fn rendered_copy_vectors() {
    let pairs = |key: &str| {
        Value::Array(
            items(group(key))
                .iter()
                .map(|case| {
                    object(vec![
                        ("value", case.get("rendered").expect("rendered").clone()),
                        ("request", case.get("request").expect("request").clone()),
                    ])
                })
                .collect(),
        )
    };
    let valid = pairs("rendered_copy.valid");
    let invalid = pairs("rendered_copy.invalid");
    for case in items(&valid) {
        let rendered = case.get("value").expect("value");
        let admitted =
            challenge::canonical_rendered(rendered, case.get("request").expect("request"));
        assert!(admitted.is_some_and(|admitted| same(admitted, rendered)));
    }
    for case in items(&invalid) {
        assert_eq!(call("challenge.canonical_rendered", case), Outcome::Refuse);
    }
}

#[test]
fn supervisor_and_routine_claim_vectors() {
    for (path, audience) in [
        ("local_supervisor", ASSERTION_AUDIENCE),
        ("local_routine", ROUTINE_AUDIENCE),
    ] {
        let cases = group(path);
        for case in items(cases.get("valid").expect("valid")) {
            let admitted = supervisor::canonical_claims(case, audience).expect("admitted claims");
            assert!(same(&admitted, case), "{case:?}");
        }
        for case in items(cases.get("invalid").expect("invalid")) {
            assert!(
                supervisor::canonical_claims(case, audience).is_err(),
                "{case:?}"
            );
        }
    }
}

#[test]
fn claims_encoding_vectors() {
    let cases = items(group("canonical_json.claims_json"));
    assert!(
        cases
            .iter()
            .any(|case| case.get("utf8") == Some(&Value::Null))
    );
    for case in cases {
        let encoded = supervisor::claims_json(
            case.get("claims").expect("claims"),
            text(case.get("audience").expect("audience")),
        );
        match case.get("utf8").expect("utf8") {
            Value::Null => assert!(encoded.is_err()),
            expected => assert_eq!(
                String::from_utf8(encoded.expect("encoded")).expect("UTF-8"),
                text(expected)
            ),
        }
    }
}

/// The first valid Supervisor claim set with a JSON body and no binding, as a member list.
fn claims() -> Vec<(String, Value)> {
    let case = items(group("local_supervisor.valid"))
        .iter()
        .find(|case| {
            case.get("body").and_then(|body| body.get("kind")) == Some(&string("json"))
                && case.get("model").is_none()
        })
        .expect("a JSON body claim set");
    members(case)
}

fn members(value: &Value) -> Vec<(String, Value)> {
    match value {
        Value::Object(members) => members.clone(),
        _ => unreachable!("claims are objects"),
    }
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a JSON test value")
}

/// `members` with `key` removed, then set to the JSON `value` unless it is empty.
fn with(members: &[(String, Value)], key: &str, value: &str) -> Value {
    let mut changed: Vec<(String, Value)> = members
        .iter()
        .filter(|(name, _)| name != key)
        .cloned()
        .collect();
    if !value.is_empty() {
        changed.push((key.to_owned(), json(value)));
    }
    Value::Object(changed)
}

fn refused(value: &Value, audience: &str) -> SupervisorAssertionError {
    supervisor::canonical_claims(value, audience).expect_err("refused claims")
}

use SupervisorAssertionError as E;

/// Each claim member replaced (or removed, for an empty replacement) and the error Python raises for it.
const MEMBER_CASES: &[(&str, &str, SupervisorAssertionError)] = &[
    ("extra", "1", E::Claims),
    ("jti", "", E::Claims),
    ("v", "true", E::Unsupported),
    ("aud", r#""team-local-routine""#, E::Unsupported),
    ("sub", r#""A""#, E::Subject),
    ("jti", "1", E::Nonce),
    ("iat", "0", E::Instant("assertion issued time")),
    ("exp", r#""1""#, E::Instant("assertion expiry")),
    ("exp", "2200000016", E::Lifetime),
    ("method", r#""HEAD""#, E::Method),
    ("path", r#""/v1/teams/""#, E::Path),
    ("path", r#""/v1//teams""#, E::Path),
    ("path", r#""/V1/teams""#, E::Path),
    ("path", r#""/v1/tëams""#, E::Path),
    ("path", "1", E::Path),
    ("authority_sha256", r#""b""#, E::Digest("authority digest")),
    ("authority", r#""routine""#, E::Authority),
    ("authority", r#""admin""#, E::Authority),
    ("authority", "1", E::Authority),
    ("body", "1", E::Body),
    ("body", r#"{"kind": "raw"}"#, E::Body),
    (
        "body",
        r#"{"kind": "none", "length": false, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}"#,
        E::EmptyBody,
    ),
    (
        "body",
        r#"{"kind": "none", "length": 0.0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}"#,
        E::EmptyBody,
    ),
    (
        "body",
        r#"{"kind": "none", "length": 0, "sha256": "x"}"#,
        E::Digest("empty body digest"),
    ),
    (
        "body",
        r#"{"kind": "none", "length": 0, "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
        E::EmptyDigest,
    ),
    (
        "body",
        r#"{"kind": "json", "length": 1, "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
        E::JsonBody,
    ),
    (
        "body",
        r#"{"kind": "json", "length": 262145, "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
        E::JsonBody,
    ),
    (
        "body",
        r#"{"kind": "json", "length": 2, "sha256": "x"}"#,
        E::Digest("JSON body digest"),
    ),
    (
        "body",
        r#"{"kind": "file", "length": 0, "filename": "a", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": " a", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "..", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": ".", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "a/b", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "a\\b", "media_type": "text/plain"}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "a", "media_type": ""}"#,
        E::FileBody,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "a", "media_type": "Text/plain"}"#,
        E::FileBody,
    ),
    ("model", "1", E::Model),
    (
        "model",
        r#"{"provider": "Open", "key_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
        E::ModelProvider,
    ),
    (
        "model",
        r#"{"provider": "openai", "key_sha256": "x"}"#,
        E::Digest("model credential digest"),
    ),
    (
        "decision",
        r#"{"provider": "openai", "key_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
        E::Decision,
    ),
    (
        "decision",
        r#"{"provider": "typesafe", "key_sha256": "x"}"#,
        E::Digest("decision credential digest"),
    ),
    (
        "assurance",
        r#"{"kind": "auth:sms", "challenge_id": "0123456789abcdef0123456789abcdef"}"#,
        E::Assurance,
    ),
    (
        "assurance",
        r#"{"kind": "auth:totp", "challenge_id": "x"}"#,
        E::Assurance,
    ),
    ("assurance", "1", E::Assurance),
];

/// Members each admitted on their own beside the base claims.
const ADMITTED_MEMBERS: &[(&str, &str)] = &[
    (
        "body",
        r#"{"kind": "none", "length": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}"#,
    ),
    (
        "body",
        r#"{"kind": "file", "length": 1, "filename": "a b.pdf", "media_type": "application/pdf"}"#,
    ),
    (
        "decision",
        r#"{"provider": "typesafe", "key_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#,
    ),
    (
        "assurance",
        r#"{"kind": "auth:totp", "challenge_id": "0123456789abcdef0123456789abcdef"}"#,
    ),
];

#[test]
fn claims_refuse_each_member_outside_its_contract() {
    let base = claims();
    for (key, value, error) in MEMBER_CASES {
        assert_eq!(
            refused(&with(&base, key, value), ASSERTION_AUDIENCE),
            *error,
            "{key} {value}"
        );
    }
    for (key, value) in ADMITTED_MEMBERS {
        let changed = with(&base, key, value);
        let admitted =
            supervisor::canonical_claims(&changed, ASSERTION_AUDIENCE).expect("admitted");
        assert_eq!(admitted.get(key), changed.get(key));
    }
    let long_path = format!(r#""/{}""#, "a".repeat(512));
    assert_eq!(
        refused(&with(&base, "path", &long_path), ASSERTION_AUDIENCE),
        E::Path
    );
    let long_name = format!(
        r#"{{"kind": "file", "length": 1, "filename": "{}", "media_type": "text/plain"}}"#,
        "é".repeat(128)
    );
    assert_eq!(
        refused(&with(&base, "body", &long_name), ASSERTION_AUDIENCE),
        E::FileBody
    );
    let long_type = format!(
        r#"{{"kind": "file", "length": 1, "filename": "a", "media_type": "a/{}"}}"#,
        "b".repeat(126)
    );
    assert_eq!(
        refused(&with(&base, "body", &long_type), ASSERTION_AUDIENCE),
        E::FileBody
    );
    assert_eq!(refused(&int(1), ASSERTION_AUDIENCE), E::Claims);
    assert_eq!(refused(&Value::Object(base), "team-other"), E::Unsupported);
}

#[test]
fn routine_claims_bind_the_routine_authority_alone() {
    let base = claims();
    let routine = members(&with(
        &members(&with(&base, "aud", r#""team-local-routine""#)),
        "authority",
        r#""routine""#,
    ));
    assert!(
        supervisor::canonical_claims(&Value::Object(routine.clone()), ROUTINE_AUDIENCE).is_ok()
    );
    assert_eq!(
        refused(
            &with(&routine, "authority", r#""session""#),
            ROUTINE_AUDIENCE
        ),
        E::Authority
    );
    let model = r#"{"provider": "openai", "key_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#;
    assert_eq!(
        refused(&with(&routine, "model", model), ROUTINE_AUDIENCE),
        E::RoutineBinding
    );
}

#[test]
fn canonical_json_is_bounded() {
    let at = |length: usize| Value::String("a".repeat(length));
    assert_eq!(
        supervisor::canonical_json(&at(4094))
            .expect("at the bound")
            .len(),
        4096
    );
    assert_eq!(supervisor::canonical_json(&at(4095)), Err(E::TooLarge));
}

const REQUEST: &str = r#"{"title": "t", "placeholder": null, "options": [{"label": "x"}]}"#;
const RENDERED_ADMITTED: &str =
    r#"{"title": "T", "placeholder": null, "options": [{"label": "X", "description": null}]}"#;
const RENDERED_REFUSED: &[&str] = &[
    r#"{"title": " T", "placeholder": null, "options": [{"label": "X", "description": null}]}"#,
    r#"{"title": "T", "placeholder": "P", "options": [{"label": "X", "description": null}]}"#,
    r#"{"title": "T", "placeholder": null, "options": [{"label": "X", "description": "D"}]}"#,
    r#"{"title": "T", "placeholder": null, "options": [{"label": "", "description": null}]}"#,
    r#"{"title": "T", "placeholder": null, "options": []}"#,
    r#"{"title": "T", "placeholder": null, "options": 1}"#,
    r#"{"title": "T", "placeholder": null, "options": [1]}"#,
    r#"{"title": "T", "placeholder": null}"#,
    r#"{"title": "T", "placeholder": null, "label": "L"}"#,
    r#"{"title": "T", "label": null, "options": [{"label": "X", "description": null}]}"#,
    "1",
];

#[test]
fn rendered_copy_refuses_each_shape_outside_its_request() {
    let request = json(REQUEST);
    assert!(challenge::canonical_rendered(&json(RENDERED_ADMITTED), &request).is_some());
    for case in RENDERED_REFUSED {
        assert!(
            challenge::canonical_rendered(&json(case), &request).is_none(),
            "{case}"
        );
    }
    assert!(challenge::canonical_rendered(&json(RENDERED_ADMITTED), &int(1)).is_none());
    let rendered = json(r#"{"options": [{"label": "X", "description": null}]}"#);
    for request in [r#"{"options": [1]}"#, r#"{"options": 1}"#] {
        assert!(challenge::canonical_rendered(&rendered, &json(request)).is_none());
    }
}

fn row(name: &str, value: &str, truncated: &str) -> String {
    format!(r#"{{"name": "{name}", "value": "{value}", "truncated": {truncated}}}"#)
}

fn projection(rows: &[String], omitted: &str) -> Value {
    json(&format!(
        r#"{{"fields": [{}], "omitted": {omitted}}}"#,
        rows.join(", ")
    ))
}

#[test]
fn projections_refuse_each_row_outside_its_contract() {
    let full: Vec<String> = (0..16)
        .map(|index| row(&format!("a{index:02}"), "1", "false"))
        .collect();
    let pair = |first: &str, second: &str| [row(first, "1", "false"), row(second, "1", "false")];
    let invalid = [
        projection(&full, "4097"),
        projection(&full[..15], "1"),
        projection(&[full.clone(), vec![row("z", "1", "false")]].concat(), "0"),
        projection(&full, "false"),
        projection(&pair("b", "a"), "0"),
        projection(&pair("a", "a"), "0"),
        projection(&[row("a", "1", "0")], "0"),
        projection(&[row("a", "", "false")], "0"),
        projection(&[row("a\\u0007", "1", "false")], "0"),
        projection(&[row(&"a".repeat(129), "1", "false")], "0"),
        projection(&[row("a", &"1".repeat(401), "false")], "0"),
        json(r#"{"fields": 1, "omitted": 0}"#),
    ];
    let valid = [
        projection(&full, "4096"),
        projection(&[row("a", "1", "true")], "0"),
    ];
    admits(
        "challenge.canonical_input_projection",
        &Value::Array(valid.to_vec()),
        &Value::Array(invalid.to_vec()),
    );
}
