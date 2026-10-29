use strict_json::{Number, Value};

use super::{
    MAX_ORIGIN_CHARS, MAX_SITES, MAX_URL_CHARS, canonical_cancelled, canonical_current,
    canonical_delete, canonical_deleted, canonical_origin, canonical_saved, canonical_session,
    canonical_sign_in, canonical_site, canonical_start, canonical_started, start_site,
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
fn start_sites_are_the_ascii_host_of_an_https_url_without_credentials_or_other_ports() {
    for (url, site) in [
        ("https://www.example.com/login", "www.example.com"),
        ("HTTPS://Example.com:0443/", "example.com"),
        ("https://:@example.com", "example.com"),
        ("https://example.com:", "example.com"),
    ] {
        assert_eq!(start_site(&text(url)).as_deref(), Some(site), "{url}");
    }
    let long = format!("https://example.com/{}", "a".repeat(MAX_URL_CHARS));
    for url in [
        text("http://example.com/"),
        text("https://user@example.com/"),
        text("https://:pw@example.com/"),
        text("https://example.com:8443/"),
        text("https://example.com:99999/"),
        text("https://example.com/a b"),
        text("https://example.com/\u{7}"),
        text("https://\u{212a}eep.com/"),
        text("https://[::1]/"),
        text("https://localhost/"),
        text(&long),
        int(1),
    ] {
        assert_eq!(start_site(&url), None, "{url:?}");
    }
}

#[test]
fn sites_and_origins_have_one_exact_form() {
    assert_eq!(canonical_site(&text("example.com")), Some("example.com"));
    for site in ["Example.com", "localhost", "a.c", "a-.com"] {
        assert_eq!(canonical_site(&text(site)), None, "{site}");
    }
    assert_eq!(
        canonical_origin(&text("https://admin.example")),
        Some("https://admin.example")
    );
    let long = format!("https://{}.example", "a".repeat(MAX_ORIGIN_CHARS));
    for origin in [
        text("HTTPS://admin.example"),
        text("null"),
        text(&long),
        Value::Null,
    ] {
        assert_eq!(canonical_origin(&origin), None, "{origin:?}");
    }
}

fn id() -> Value {
    text(&"a".repeat(32))
}

#[test]
fn requests_name_their_exact_members() {
    let origin = text("https://admin.example");
    let start = object(&[
        ("url", text("https://example.com/")),
        ("locale", text("en")),
        ("origin", origin.clone()),
    ]);
    assert_eq!(canonical_start(&start), Some(&start));
    for refused in [
        with(&start, "url", text("http://example.com/")),
        with(&start, "locale", text("pt-BR")),
        with(&start, "origin", text("null")),
        object(&[("url", text("https://example.com/"))]),
    ] {
        assert_eq!(canonical_start(&refused), None, "{refused:?}");
    }
    let session = object(&[("sign_in_session_id", id()), ("origin", origin.clone())]);
    assert_eq!(canonical_session(&session), Some(&session));
    assert_eq!(
        canonical_session(&with(&session, "sign_in_session_id", int(1))),
        None
    );
    assert_eq!(canonical_session(&with(&session, "origin", int(1))), None);
    let started = object(&[
        ("sign_in_session_id", id()),
        ("view_id", id()),
        ("site", text("example.com")),
    ]);
    assert_eq!(canonical_started(&started), Some(&started));
    for key in ["sign_in_session_id", "view_id", "site"] {
        assert_eq!(
            canonical_started(&with(&started, key, text("X"))),
            None,
            "{key}"
        );
    }
}

#[test]
fn deletions_bind_the_sign_in_its_generation_and_incarnation() {
    let delete = object(&[
        ("sign_in_id", id()),
        ("generation", int(1)),
        ("team_incarnation", text(&"c".repeat(64))),
        ("factor", text("totp")),
        ("origin", text("https://admin.example")),
    ]);
    assert_eq!(canonical_delete(&delete), Some(&delete));
    assert!(canonical_delete(&with(&delete, "generation", int((1 << 31) - 1))).is_some());
    for (key, value) in [
        ("sign_in_id", text("x")),
        ("generation", int(0)),
        ("generation", int(1 << 31)),
        ("generation", Value::Bool(true)),
        ("team_incarnation", text(&"c".repeat(63))),
        ("team_incarnation", int(1)),
        ("factor", text("password")),
        ("factor", Value::Array(vec![text("totp")])),
        ("origin", text("ftp://admin.example")),
    ] {
        assert_eq!(
            canonical_delete(&with(&delete, key, value.clone())),
            None,
            "{key} {value:?}"
        );
    }
    let deleted = object(&[("deletion", text("pending"))]);
    assert_eq!(canonical_deleted(&deleted), Some(&deleted));
    for value in [text("forgotten"), Value::Array(vec![text("deleted")])] {
        assert_eq!(canonical_deleted(&object(&[("deletion", value)])), None);
    }
    assert_eq!(canonical_deleted(&object(&[])), None);
}

fn sign_in() -> Value {
    object(&[
        ("sign_in_id", id()),
        ("state", text("ready")),
        ("generation", int(1)),
        ("team_incarnation", text(&"c".repeat(64))),
        (
            "sites",
            Value::Array(vec![text("example.com"), text("linkedin.com")]),
        ),
        ("sites_truncated", Value::Bool(false)),
        ("created_at", text("2026-10-20T12:00:00Z")),
        ("saved_at", text("2026-10-20T12:00:00Z")),
        ("last_used_at", text("2026-10-20T12:00:00Z")),
        ("expires_at", text("2026-11-19T12:00:00Z")),
    ])
}

#[test]
fn a_sign_in_shows_its_state_sorted_sites_and_ordered_times() {
    let record = sign_in();
    assert_eq!(canonical_sign_in(&record), Some(&record));
    let many = (0..=MAX_SITES)
        .map(|index| text(&format!("s{index:03}.example.com")))
        .collect();
    let refused = [
        ("sign_in_id", text("x")),
        ("state", text("checked-out")),
        ("state", Value::Array(vec![text("ready")])),
        ("generation", int(0)),
        ("team_incarnation", text("c")),
        ("sites", text("example.com")),
        (
            "sites",
            Value::Array(vec![text("linkedin.com"), text("example.com")]),
        ),
        (
            "sites",
            Value::Array(vec![text("example.com"), text("example.com")]),
        ),
        ("sites", Value::Array(vec![text("Example.com")])),
        ("sites", Value::Array(many)),
        ("sites_truncated", int(0)),
        ("created_at", text("2026-10-20")),
        ("saved_at", text("2026-10-19T12:00:00Z")),
        ("expires_at", text("2026-10-20T12:00:00Z")),
        ("last_used_at", text("2026-10-19T12:00:00Z")),
        ("saved_at", int(1)),
        ("last_used_at", Value::Null),
        ("expires_at", Value::Null),
    ];
    for (key, value) in refused {
        assert_eq!(
            canonical_sign_in(&with(&record, key, value.clone())),
            None,
            "{key} {value:?}"
        );
    }
    let current = object(&[("sign_in", record.clone())]);
    assert_eq!(canonical_current(&current), Some(&current));
    let none = object(&[("sign_in", Value::Null)]);
    assert_eq!(canonical_current(&none), Some(&none));
    assert_eq!(canonical_current(&object(&[("sign_in", int(1))])), None);
    let saved = object(&[("sign_in", record), ("recorded", Value::Bool(true))]);
    assert_eq!(canonical_saved(&saved), Some(&saved));
    assert_eq!(canonical_saved(&with(&saved, "recorded", int(1))), None);
    assert_eq!(canonical_saved(&with(&saved, "sign_in", Value::Null)), None);
}

#[test]
fn a_cancellation_equals_the_one_cancelled_object_as_python_compares() {
    for value in [Value::Bool(true), int(1), Value::Number(Number::Float(1.0))] {
        let cancelled = object(&[("cancelled", value)]);
        assert_eq!(canonical_cancelled(&cancelled), Some(&cancelled));
    }
    for value in [
        object(&[("cancelled", Value::Bool(false))]),
        object(&[]),
        int(1),
    ] {
        assert_eq!(canonical_cancelled(&value), None);
    }
}
