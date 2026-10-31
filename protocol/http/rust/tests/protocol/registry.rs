//! The registry the corpus and the fuzz target call: unique names, each answering every kind of value.

use strict_json::{Number, Value};

use crate::support::registry::{Outcome, all, find, line};

#[test]
fn names_are_unique_python_paths() {
    let entries = all();
    for (index, (name, _)) in entries.iter().enumerate() {
        assert!(name.contains('.'), "{name}");
        assert!(
            entries[..index].iter().all(|(other, _)| other != name),
            "{name} twice"
        );
        assert!(find(name).is_some());
    }
    assert!(find("payload.unknown").is_none());
}

#[test]
fn every_entry_answers_every_kind_of_value() {
    let samples = [
        Value::Null,
        Value::Bool(true),
        Value::Number(Number::Int(3)),
        Value::Number(Number::Float(1.5)),
        Value::String("a".into()),
        Value::Array(vec![]),
        Value::Object(vec![]),
    ];
    for (name, call) in all() {
        for sample in &samples {
            let outcome = call(sample);
            let text = line(&outcome);
            match outcome {
                Outcome::Value(_) => assert!(
                    text.starts_with(['{', '[', '"', 't', 'f', 'n']),
                    "{name}: {text}"
                ),
                Outcome::Refuse => assert_eq!(text, "refuse"),
                Outcome::Error(kind) => assert_eq!(text, format!("error:{kind}")),
            }
        }
    }
}
