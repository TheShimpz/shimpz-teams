//! Shared fixtures: the reference Cloudflare Assistant, a fixture manifest, and machine-contract builders whose
//! displayed copy is cataloged (ADR-0091).

#![allow(dead_code)]

use std::path::PathBuf;

use assistant_protocol::catalog::message_id;
use strict_json::{Number, Value};

pub const SUMMARY: &str = "Exercise one reviewed Assistant.";
pub const ASSISTANT_DESCRIPTION: &str =
    "Exercise one reviewed Assistant through the Team test suite.";
pub const ACTION_DESCRIPTION: &str = "Run one reviewed test Action.";
pub const STORED_INPUT_HELP: &str = "Create a test key in the provider dashboard and copy it.";
pub const HELP_URL: &str = "https://dashboard.exa.ai/api-keys";
pub const REFERENCE_SUMMARY: &str =
    "List Cloudflare zones and inspect their DNS records through OAuth.";
pub const REFERENCE_DESCRIPTION: &str = "Browse the zones of your Cloudflare account and read the DNS records of each zone. Every Action only reads; nothing in your account changes.";

pub fn fixture(name: &str) -> Vec<u8> {
    std::fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/reference")
            .join(name),
    )
    .expect("fixture")
}

pub fn protocol(path: &str) -> Value {
    let raw = std::fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../protocol/assistant/v1")
            .join(path),
    )
    .expect("vectors");
    strict_json::parse(&raw).expect("strict JSON")
}

pub fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("test JSON")
}

pub fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

pub fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

pub fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

pub fn set(value: &mut Value, key: &str, item: Value) {
    let Value::Object(members) = value else {
        panic!("object")
    };
    match members.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = item,
        None => members.push((key.to_owned(), item)),
    }
}

/// A fixture manifest; `extra` is appended after the network section.
pub fn manifest(allowed_hosts: &[&str], summary: &str, extra: &str) -> Vec<u8> {
    let hosts: Vec<String> = allowed_hosts
        .iter()
        .map(|host| format!("\"{host}\""))
        .collect();
    format!(
        "[shimpz]\nspec = 1\nid = \"fixture-assistant\"\nversion = \"0.1.0\"\nname = \"Fixture Assistant\"\nsummary = \"{summary}\"\ndescription = \"{ASSISTANT_DESCRIPTION}\"\ncreators = [\"@fixture\"]\ngithub = \"https://github.com/TheShimpz/fixture-assistant\"\ngenesis = \"Use the available Actions.\"\n\n[network]\nallowed_hosts = [{}]\n\n{extra}",
        hosts.join(", ")
    )
    .into_bytes()
}

pub fn closed_schema() -> Value {
    json(r#"{"type": "object", "additionalProperties": false}"#)
}

/// One closed machine-contract Action with empty capabilities.
pub fn action(id: &str, effect: &str) -> Value {
    object(vec![
        ("id", text(id)),
        ("description", text(ACTION_DESCRIPTION)),
        ("input_schema", closed_schema()),
        ("output_schema", closed_schema()),
        ("integrations", Value::Array(Vec::new())),
        ("stored_inputs", Value::Array(Vec::new())),
        ("input_files", Value::Array(Vec::new())),
        ("human_requests", Value::Array(Vec::new())),
        ("effect", text(effect)),
    ])
}

pub fn message(msgid: &str, max_length: i64) -> Value {
    object(vec![
        ("id", text(&message_id(msgid))),
        ("msgid", text(msgid)),
        ("max_length", int(max_length)),
        ("params", Value::Array(Vec::new())),
    ])
}

/// The summary, description, Action description, and Stored Input help messages, plus one line message per label.
pub fn messages(labels: &[&str]) -> Value {
    let mut entries = vec![
        message(SUMMARY, 80),
        message(ASSISTANT_DESCRIPTION, 500),
        message(ACTION_DESCRIPTION, 120),
        message(STORED_INPUT_HELP, 500),
    ];
    entries.extend(labels.iter().map(|label| message(label, 120)));
    entries.sort_by_key(|entry| match entry.get("id") {
        Some(Value::String(id)) => id.clone(),
        _ => String::new(),
    });
    entries.dedup_by(|left, right| left.get("id") == right.get("id"));
    Value::Array(entries)
}

pub fn contract(actions: Vec<Value>, labels: &[&str]) -> Value {
    object(vec![
        ("version", int(1)),
        ("actions", Value::Array(actions)),
        ("messages", messages(labels)),
    ])
}

/// One Stored Input declaration: a header on the fixture host unless the placement says otherwise.
pub fn stored(label: &str, placement: &[(&str, Value)]) -> Value {
    let mut fields = object(vec![
        ("kind", text("password")),
        ("label", text(label)),
        ("description", text(STORED_INPUT_HELP)),
        ("help_url", text(HELP_URL)),
        ("host", text("api.example.com")),
        (
            "routes",
            json(r#"[{"method": "POST", "path": "/v23.0/*/messages"}]"#),
        ),
    ]);
    for (key, value) in placement {
        set(&mut fields, key, value.clone());
    }
    if fields.get("query").is_none() && fields.get("header").is_none() {
        set(&mut fields, "header", text("X-Api-Key"));
    }
    fields
}

/// [`messages`] plus one line message per string Action description of `actions`.
pub fn display_messages(actions: &[Value], labels: &[&str]) -> Value {
    let descriptions: Vec<&str> = actions
        .iter()
        .filter_map(|action| match action.get("description") {
            Some(Value::String(description)) => Some(description.as_str()),
            _ => None,
        })
        .collect();
    let mut all: Vec<&str> = labels.to_vec();
    all.extend(descriptions);
    let Value::Array(mut entries) = messages(&all) else {
        panic!("messages")
    };
    entries.sort_by_key(|entry| match entry.get("id") {
        Some(Value::String(id)) => id.clone(),
        _ => String::new(),
    });
    entries.dedup_by(|left, right| left.get("id") == right.get("id"));
    Value::Array(entries)
}
