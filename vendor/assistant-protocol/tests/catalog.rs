//! The published message catalog, display, render, and language pack vectors.

mod common;

use std::fmt::Write as _;

use assistant_protocol::catalog::{
    canonical_json, catalog_by_id, catalog_digest, catalog_error, display_error, display_uses,
    message_id, pack_digest, pack_error, public_text, render,
};
use assistant_protocol::human_request::reference_error;
use assistant_protocol::python::{array, small_int, string};
use assistant_protocol::{Number, Value};

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn message(msgid: &str, max_length: i64, params: Vec<Value>) -> Value {
    Value::Object(vec![
        ("id".to_owned(), text(&message_id(msgid))),
        ("msgid".to_owned(), text(msgid)),
        ("max_length".to_owned(), int(max_length)),
        ("params".to_owned(), Value::Array(params)),
    ])
}

fn number(value: &Value, field: &str) -> usize {
    value
        .get(field)
        .and_then(small_int)
        .and_then(|n| usize::try_from(n).ok())
        .expect("count")
}

/// The summary plus `count - 1` padded parameterized messages, sorted by id.
fn generated(summary: &str, spec: &Value) -> Value {
    let (count, padding, params) = (
        number(spec, "count"),
        number(spec, "padding"),
        number(spec, "params"),
    );
    let declared: Vec<Value> = (0..params)
        .map(|index| {
            Value::Object(vec![
                ("name".to_owned(), text(&format!("p{index}"))),
                ("kind".to_owned(), text("integer")),
                ("max_length".to_owned(), int(1)),
            ])
        })
        .collect();
    let fields = (0..params).fold(String::new(), |mut fields, index| {
        let _ = write!(fields, " {{p{index}}}");
        fields
    });
    let padding = if padding > 0 {
        format!(" {}", "x".repeat(padding))
    } else {
        String::new()
    };
    let mut messages = vec![message(summary, 80, Vec::new())];
    messages.extend((0..count - 1).map(|index| {
        message(
            &format!("{index:04}{fields}{padding}"),
            500,
            declared.clone(),
        )
    }));
    messages.sort_by(|left, right| {
        left.get("id")
            .and_then(string)
            .cmp(&right.get("id").and_then(string))
    });
    Value::Array(messages)
}

/// The summary message plus one entry made of `depth` nested arrays.
fn nested(summary: &str, depth: usize) -> Value {
    let mut entry = Value::Array(Vec::new());
    for _ in 1..depth {
        entry = Value::Array(vec![entry]);
    }
    Value::Array(vec![message(summary, 80, Vec::new()), entry])
}

fn case_messages(case: &Value) -> Value {
    let summary = common::text(case, "summary");
    if let Some(messages) = case.get("messages") {
        messages.clone()
    } else if let Some(depth) = case.get("nested") {
        nested(
            summary,
            usize::try_from(small_int(depth).expect("depth")).expect("depth"),
        )
    } else {
        generated(summary, common::field(case, "generated"))
    }
}

fn texts(case: &Value, field: &str) -> Vec<Value> {
    array(common::field(case, field)).expect("list").to_vec()
}

fn check(file_section: &str, evaluate: impl Fn(&Value) -> Option<&'static str>) {
    let document = common::vectors("catalog.json");
    let cases = array(common::field(&document, file_section)).expect("cases");
    let mut outcomes = (false, false);
    for case in cases {
        let valid = common::flag(case, "valid");
        assert_eq!(
            evaluate(case),
            common::expected(case),
            "{file_section}: {}",
            common::name(case)
        );
        if valid {
            outcomes.0 = true;
        } else {
            outcomes.1 = true;
        }
    }
    assert_eq!(outcomes, (true, true));
}

fn reference_catalog() -> Value {
    common::field(
        common::field(&common::vectors("catalog.json"), "catalog"),
        "messages",
    )
    .clone()
}

#[test]
fn the_reference_catalog_and_pack_are_admitted_with_their_digests() {
    let document = common::vectors("catalog.json");
    let section = common::field(&document, "catalog");
    let messages = common::field(section, "messages");
    assert_eq!(
        catalog_error(messages, common::field(section, "summary")),
        None
    );
    assert_eq!(catalog_digest(messages), common::text(section, "digest"));
    let pack = common::field(&document, "pack");
    let raw = canonical_json(common::field(pack, "value"));
    assert_eq!(pack_error(&raw, messages), None);
    assert_eq!(pack_digest(&raw), common::text(pack, "digest"));
}

#[test]
fn every_catalog_vector_agrees() {
    check("catalog_cases", |case| {
        catalog_error(&case_messages(case), common::field(case, "summary"))
    });
}

#[test]
fn every_display_vector_agrees() {
    check("display_cases", |case| {
        let (actions, labels, help) = (
            texts(case, "action_descriptions"),
            texts(case, "labels"),
            texts(case, "help_texts"),
        );
        let uses = display_uses(
            string(common::field(case, "description")),
            actions.iter().map(string),
            labels.iter().map(string),
            help.iter().map(string),
        );
        display_error(common::field(case, "messages"), uses)
    });
}

#[test]
fn every_pack_vector_agrees() {
    let messages = reference_catalog();
    check("pack_cases", |case| {
        let raw = case.get("pack").map_or_else(
            || common::text(case, "text").as_bytes().to_vec(),
            canonical_json,
        );
        pack_error(&raw, &messages)
    });
}

#[test]
fn every_render_vector_renders_its_admitted_reference() {
    let document = common::vectors("catalog.json");
    let messages = reference_catalog();
    let catalog = catalog_by_id(&messages);
    let locales = common::field(common::field(&document, "pack"), "value")
        .get("locales")
        .expect("locales");
    for case in array(common::field(&document, "render_cases")).expect("cases") {
        let reference = common::field(case, "reference");
        let bound = usize::try_from(small_int(common::field(case, "bound")).expect("bound"))
            .expect("bound");
        assert_eq!(
            reference_error(reference, &catalog, bound),
            None,
            "{}",
            common::name(case)
        );
        let identifier = common::text(reference, "message");
        let template = match common::text(case, "locale") {
            "en" => common::text(&Value::Object(catalog[identifier].to_vec()), "msgid").to_owned(),
            locale => common::text(common::field(locales, locale), identifier).to_owned(),
        };
        let rendered = render(reference, &template);
        assert_eq!(
            rendered,
            common::text(case, "rendered"),
            "{}",
            common::name(case)
        );
        assert!(
            public_text(&text(&rendered), bound),
            "{}",
            common::name(case)
        );
    }
}
