//! Every case of `vectors.json` decides as the vector records.

mod support;

use strict_json::Value;

fn text<'a>(case: &'a Value, field: &str) -> &'a str {
    match case.get(field) {
        Some(Value::String(text)) => text,
        other => panic!("{field} is not a string: {other:?}"),
    }
}

#[test]
fn every_vector_decides_as_recorded() {
    let Ok(Value::Array(cases)) = strict_json::parse(include_bytes!("../vectors.json")) else {
        panic!("vectors.json is a strict JSON array");
    };
    assert!(cases.len() > 500, "the vector set is complete");
    let mut accepted = 0;
    for (index, case) in cases.iter().enumerate() {
        let input = support::base64::decode(text(case, "input_b64"));
        let actual = strict_json::parse(&input)
            .ok()
            .map(|value| support::repr::canonical(&value));
        let expected = match case.get("accept") {
            Some(Value::Bool(true)) => Some(text(case, "value")),
            Some(Value::Bool(false)) => None,
            other => panic!("accept is not a boolean: {other:?}"),
        };
        assert_eq!(actual.as_deref(), expected, "vector {index}");
        accepted += usize::from(expected.is_some());
    }
    assert!(
        accepted > 100 && cases.len() - accepted > 100,
        "both outcomes are covered"
    );
}
