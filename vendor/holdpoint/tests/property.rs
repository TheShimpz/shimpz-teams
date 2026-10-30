//! Property tests: the codec, the schema subset, and a stateful model of submit, drive, resolve, cancel, and
//! restart that checks the run invariants after every operation.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::{Setup, memory_store, plan};
use holdpoint::json::{digest, parse, to_canonical};
use holdpoint::{Driven, Error, Outcome, RunOptions, Schema, Store};
use proptest::prelude::*;
use serde_json::{Map, Number, Value, json};

#[cfg(test)]
fn json_value() -> impl Strategy<Value = Value> {
    let leaf = prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Bool),
        any::<i64>().prop_map(Value::from),
        any::<u64>().prop_map(Value::from),
        any::<f64>().prop_filter_map("finite", |float| Number::from_f64(float).map(Value::Number)),
        any::<String>().prop_map(Value::String),
    ];
    leaf.prop_recursive(6, 64, 8, |inner| {
        prop_oneof![
            prop::collection::vec(inner.clone(), 0..8).prop_map(Value::Array),
            prop::collection::btree_map(any::<String>(), inner, 0..8)
                .prop_map(|map| Value::Object(Map::from_iter(map))),
        ]
    })
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 2_000, ..ProptestConfig::default() })]

    #[test]
    fn canonical_encoding_round_trips(value in json_value()) {
        let encoded = to_canonical(&value);
        prop_assert_eq!(parse(encoded.as_bytes(), usize::MAX).unwrap(), value.clone());
        prop_assert_eq!(to_canonical(&parse(encoded.as_bytes(), usize::MAX).unwrap()), encoded);
    }

    #[test]
    fn the_digest_ignores_formatting(value in json_value()) {
        let pretty = serde_json::to_string_pretty(&value).unwrap();
        prop_assert_eq!(digest(&parse(pretty.as_bytes(), usize::MAX).unwrap()), digest(&value));
    }

    #[test]
    fn the_decoder_never_panics(bytes in prop::collection::vec(any::<u8>(), 0..256)) {
        let _ = parse(&bytes, 1 << 16);
    }

    #[test]
    fn the_decoder_reads_exactly_what_any_json_writer_wrote(value in json_value()) {
        // Floats parse correctly rounded, so even serde_json's own output of a double reads back as that double.
        let text = serde_json::to_string(&value).unwrap();
        prop_assert_eq!(parse(text.as_bytes(), usize::MAX).unwrap(), value);
    }

    #[test]
    fn bounded_strings_accept_exactly_their_lengths(text in "[a-z]{0,12}", min in 0_u64..6, extra in 0_u64..6) {
        let schema = Schema::compile(&json!({"type": "string", "minLength": min, "maxLength": min + extra})).unwrap();
        let length = text.chars().count() as u64;
        prop_assert_eq!(schema.validate(&json!(text)).is_ok(), length >= min && length <= min + extra);
    }

    #[test]
    fn integer_bounds_are_inclusive(number in -50_i64..50, low in -20_i64..0, high in 0_i64..20) {
        let schema = Schema::compile(&json!({"type": "integer", "minimum": low, "maximum": high})).unwrap();
        prop_assert_eq!(schema.validate(&json!(number)).is_ok(), number >= low && number <= high);
    }
}

#[derive(Debug, Clone)]
enum Operation {
    Drive(usize),
    Resolve(usize, u32),
    Cancel(usize),
    Restart,
}

#[cfg(test)]
fn operation() -> impl Strategy<Value = Operation> {
    prop_oneof![
        4 => (0_usize..3).prop_map(Operation::Drive),
        3 => (0_usize..3, 0_u32..4).prop_map(|(run, step)| Operation::Resolve(run, step)),
        1 => (0_usize..3).prop_map(Operation::Cancel),
        1 => Just(Operation::Restart),
    ]
}

#[cfg(test)]
fn steps() -> Value {
    plan(json!([
        { "kind": "activity", "name": "act", "input": 1 },
        { "kind": "deferred", "name": "ask", "requests": [1, 2] },
        { "kind": "local", "name": "double", "input": 3 },
        { "kind": "deferred", "name": "again", "requests": [3] }
    ]))
}

/// What the model remembers between operations.
#[cfg(test)]
#[derive(Default)]
struct Model {
    outcomes: HashMap<usize, Outcome>,
}

#[cfg(test)]
fn check_invariants(
    store: &Store,
    model: &mut Model,
    executions: &HashMap<String, usize>,
) -> Result<(), TestCaseError> {
    for run in 0..3 {
        let id = format!("run-{run}");
        let history = store.history(&id);
        prop_assert!(history.is_ok(), "{id}: {history:?}");
        if let Some(outcome) = store.result(&id).unwrap() {
            let previous = model.outcomes.entry(run).or_insert_with(|| outcome.clone());
            prop_assert_eq!(&*previous, &outcome, "an outcome never changes");
        }
    }
    for (key, count) in executions {
        prop_assert!(*count <= 1, "{key} ran {count} times");
    }
    Ok(())
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 200, ..ProptestConfig::default() })]

    #[test]
    fn runs_keep_their_invariants_under_any_interleaving(operations in prop::collection::vec(operation(), 1..40)) {
        let store = memory_store();
        let mut fixture = Setup::new(Arc::clone(&store)).build();
        let mut activity_calls = Vec::new();
        for run in 0..3 {
            fixture.worker.submit("plan", &format!("run-{run}"), &steps(), &RunOptions::default()).unwrap();
        }
        let mut model = Model::default();
        for operation in operations {
            match operation {
                Operation::Drive(run) => {
                    let driven = fixture.worker.drive(&format!("run-{run}")).unwrap();
                    if let Driven::Ended(Outcome::Done { result }) = &driven {
                        prop_assert_eq!(result, &json!([1, ["a", "a"], 6, ["a"]]));
                    }
                }
                Operation::Resolve(run, step) => match fixture.worker.resolve(&format!("run-{run}"), step, &json!("a")) {
                    Ok(_) | Err(Error::State(_)) => {}
                    Err(error) => prop_assert!(false, "{error}"),
                },
                Operation::Cancel(run) => {
                    fixture.worker.cancel(&format!("run-{run}")).unwrap();
                }
                Operation::Restart => {
                    activity_calls.extend(fixture.activity.calls());
                    fixture = Setup::new(Arc::clone(&store)).build();
                }
            }
            let mut executions: HashMap<String, usize> = HashMap::new();
            for call in activity_calls.iter().chain(fixture.activity.calls().iter()) {
                *executions.entry(call.effect_key.clone()).or_default() += 1;
            }
            check_invariants(&store, &mut model, &executions)?;
        }
    }
}
