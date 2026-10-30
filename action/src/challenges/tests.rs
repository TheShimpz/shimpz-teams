use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_assistant::language::LanguagePack;
use team_core::testing::CountingEntropy;
use team_integrations::challenge::ChallengeError;

use super::{HumanRequirement, challenge_payload, input_projection, render_copy, store};
use crate::human::{self, HumanRequest};
use crate::journal::{canonical, sha256_hex};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn vectors() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../protocol/assistant/v1/vectors/human-request.json"
    );
    strict_json::parse(&std::fs::read(path).expect("vectors")).expect("json")
}

fn messages() -> Value {
    vectors()
        .get("catalog")
        .and_then(|catalog| catalog.get("messages"))
        .expect("messages")
        .clone()
}

pub(crate) fn approval() -> HumanRequest {
    let Some(Value::Array(cases)) = vectors().get("request_cases").cloned() else {
        panic!("cases")
    };
    let request = cases
        .into_iter()
        .find(|case| case.get("name") == Some(&Value::String("valid_approval".to_owned())))
        .expect("a case");
    let request = request.get("request").expect("request").clone();
    let Value::Object(mut members) = request.clone() else {
        panic!("an object")
    };
    members.push((
        "fingerprint".to_owned(),
        Value::String(sha256_hex(&canonical(&request))),
    ));
    human::validate_request(
        &Value::Object(members),
        &["approval".to_owned()],
        &[],
        &messages(),
    )
    .expect("admitted")
}

fn pack() -> LanguagePack {
    LanguagePack {
        catalog_digest: format!("sha256:{}", "a".repeat(64)),
        pack_digest: format!("sha256:{}", "b".repeat(64)),
        messages: messages(),
        translations: Value::Object(Vec::new()),
        size: 0,
    }
}

fn requirement(request: HumanRequest) -> HumanRequirement {
    let copy = render_copy(&request, &pack(), "en", None).expect("rendered");
    HumanRequirement {
        assistant_id: "cloudflare".to_owned(),
        assistant_name: "Cloudflare".to_owned(),
        action_id: "publish-record".to_owned(),
        action_summary: "Publish one DNS record.".to_owned(),
        interrupt_id: "interrupt-1".to_owned(),
        request,
        assistant_version: "0.4.1".to_owned(),
        copy,
        help_url: None,
        help_text: None,
        purpose: None,
        purpose_locale: None,
        file: None,
        input: None,
    }
}

#[tokio::test(start_paused = true)]
async fn the_projection_contains_only_public_reviewed_context_and_expires() {
    let store = store::<&str>(Arc::new(CountingEntropy::default())).expect("store");
    let pending = store
        .create("team_1", requirement(approval()), "must-not-project")
        .expect("created");
    tokio::time::advance(Duration::from_secs(1)).await;
    let payload = challenge_payload(&pending).expect("projected");
    assert_eq!(
        payload.get("status"),
        Some(&Value::String("human-required".to_owned()))
    );
    assert_eq!(
        payload.get("expires_in"),
        Some(&Value::Number(strict_json::Number::Int(299)))
    );
    assert_eq!(
        payload
            .get("request")
            .and_then(|request| request.get("kind")),
        Some(&Value::String("approval".to_owned()))
    );
    assert!(!format!("{payload:?}").contains("must-not-project"));
    tokio::time::advance(Duration::from_secs(299)).await;
    assert!(challenge_payload(&pending).is_err());
}

#[tokio::test(start_paused = true)]
async fn a_challenge_is_team_bound_one_use_and_its_expiry_is_drained_once() {
    let store = store::<&str>(Arc::new(CountingEntropy::default())).expect("store");
    let pending = store
        .create("team_1", requirement(approval()), "opaque")
        .expect("created");
    assert_eq!(
        store.get("team_2", &pending.id).err(),
        Some(ChallengeError::NotFound)
    );
    assert!(store.claim("team_1", &pending.id).is_ok());
    let expiring = store
        .create("team_1", requirement(approval()), "continuation")
        .expect("created");
    tokio::time::advance(super::DEFAULT_TTL).await;
    let drained = store.drain_expired(Some("team_1")).expect("retained");
    assert!(drained.len() == 1 && Arc::ptr_eq(&drained[0], &expiring));
    assert_eq!(
        store.drain_expired(None).map(|drained| drained.len()),
        Ok(0)
    );
}

#[test]
fn only_an_authorization_card_carries_the_input_projection() {
    let store = store::<()>(Arc::new(CountingEntropy::default())).expect("store");
    let mut card = requirement(approval());
    card.input = Some(input_projection(&json(r#"{"zone":"example.com"}"#)).expect("projected"));
    assert!(store.create("team_1", card, ()).is_ok());
    let mut purpose = requirement(approval());
    purpose.purpose = Some("To publish the record.".to_owned());
    assert_eq!(
        store.create("team_2", purpose, ()).err(),
        Some(ChallengeError::Invalid)
    );
}

#[test]
fn rendering_refuses_an_unknown_locale_and_a_foreign_pack() {
    let request = approval();
    assert!(render_copy(&request, &pack(), "xx", None).is_err());
    let foreign = LanguagePack {
        messages: Value::Array(Vec::new()),
        ..pack()
    };
    assert!(render_copy(&request, &foreign, "en", None).is_err());
    assert!(render_copy(&request, &pack(), "pt", None).is_err());
}

fn rows(projection: &Value) -> Vec<(String, String, bool)> {
    let Some(Value::Array(fields)) = projection.get("fields") else {
        panic!("fields")
    };
    fields
        .iter()
        .map(|field| {
            let text = |key| match field.get(key) {
                Some(Value::String(text)) => text.clone(),
                _ => String::new(),
            };
            (
                text("name"),
                text("value"),
                field.get("truncated") == Some(&Value::Bool(true)),
            )
        })
        .collect()
}

#[test]
fn every_argument_is_shown_as_escaped_quoted_json() {
    let arguments = json(
        r#"{"name":"www","content":"v=spf1 \u202eevil\u2028next\nline \udb40\udc01","ttl":300,"proxied":true,"tags":["a","b"],"nested":{"z":1,"a":null},"flag":"true","na\u200bme":1}"#,
    );
    let projection = input_projection(&arguments).expect("projected");
    let rows = rows(&projection);
    let value = |name: &str| {
        rows.iter()
            .find(|row| row.0 == name)
            .map(|row| row.1.clone())
            .expect("a row")
    };
    assert_eq!(
        projection.get("omitted"),
        Some(&Value::Number(strict_json::Number::Int(0)))
    );
    assert_eq!(
        (value("name"), value("flag"), value("proxied")),
        (
            r#""www""#.to_owned(),
            r#""true""#.to_owned(),
            "true".to_owned()
        )
    );
    assert_eq!(
        (value("tags"), value("nested")),
        (
            r#"["a", "b"]"#.to_owned(),
            r#"{"a": null, "z": 1}"#.to_owned()
        )
    );
    assert_eq!(
        value("content"),
        r#""v=spf1 \u202eevil\u2028next\nline \udb40\udc01""#
    );
    assert!(rows.iter().any(|row| row.0 == r"na\u200bme"));
}

#[test]
fn bounds_are_explicit_and_never_split_an_escape() {
    let many = Value::Object(
        (0..20)
            .map(|index| {
                (
                    format!("field{index:02}"),
                    Value::Number(strict_json::Number::Int(index)),
                )
            })
            .collect(),
    );
    let projected = input_projection(&many).expect("projected");
    assert_eq!(
        (rows(&projected).len(), projected.get("omitted")),
        (16, Some(&Value::Number(strict_json::Number::Int(4))))
    );
    let long = json(&format!(
        r#"{{"a":"{}","b{}":1,"c":"{}\u202e"}}"#,
        "x".repeat(500),
        "y".repeat(200),
        "x".repeat(396)
    ));
    let rows = rows(&input_projection(&long).expect("projected"));
    assert!(rows[0].2 && rows[0].1.chars().count() == 400);
    assert!(rows[1].2 && rows[1].0.chars().count() == 128);
    assert_eq!(
        (rows[2].1.clone(), rows[2].2),
        (format!("\"{}", "x".repeat(396)), true)
    );
    let backslash = json(&format!(r#"{{"a":"{}\\u"}}"#, "x".repeat(396)));
    let kept = &self::rows(&input_projection(&backslash).expect("projected"))[0];
    assert_eq!(kept.1, format!(r#""{}\\u"#, "x".repeat(396)));
    let colliding = json(&format!(r#"{{"{0}1":1,"{0}2":2}}"#, "a".repeat(128)));
    assert!(input_projection(&colliding).is_err());
}
