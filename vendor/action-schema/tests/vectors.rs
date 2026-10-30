//! The published Assistant protocol vectors this crate implements: pattern semantics and programs, schema admission,
//! and payload validation with its matching budget.

use action_schema::{
    Budget, MAX_PATTERN_WORK, Number, PatternError, PayloadError, Value, admit_schema,
    pattern_matches, pattern_preparation, pattern_program, validate_payload, validate_payload_in,
};

const PATTERN: &str = include_str!("../../../protocol/assistant/v1/vectors/pattern.json");
const PROGRAM: &str = include_str!("../../../protocol/assistant/v1/vectors/pattern-program.json");
const SCHEMA: &str = include_str!("../../../protocol/assistant/v1/vectors/action-schema.json");
const PAYLOAD: &str = include_str!("../../../protocol/assistant/v1/vectors/payload.json");

fn cases(text: &str) -> Vec<Value> {
    let document = strict_json::parse(text.as_bytes()).expect("vectors are strict JSON");
    assert_eq!(
        document.get("version"),
        Some(&Value::Number(Number::Int(1)))
    );
    match document.get("cases") {
        Some(Value::Array(cases)) if !cases.is_empty() => cases.clone(),
        _ => panic!("vectors hold cases"),
    }
}

fn text<'a>(case: &'a Value, field: &str) -> &'a str {
    match case.get(field) {
        Some(Value::String(text)) => text,
        _ => panic!("{field} is a string"),
    }
}

fn flag(case: &Value, field: &str) -> bool {
    match case.get(field) {
        Some(Value::Bool(flag)) => *flag,
        _ => panic!("{field} is a boolean"),
    }
}

fn integer(case: &Value, field: &str) -> Option<u64> {
    match case.get(field) {
        Some(Value::Number(Number::Int(value))) => Some(u64::try_from(*value).expect("unsigned")),
        Some(Value::Null) => None,
        _ => panic!("{field} is an integer or null"),
    }
}

#[test]
fn every_pattern_vector_matches_as_published() {
    for case in cases(PATTERN) {
        let name = text(&case, "name");
        let matched = pattern_matches(
            text(&case, "pattern"),
            text(&case, "subject"),
            &mut Budget::default(),
        );
        assert_eq!(matched, Ok(flag(&case, "matches")), "{name}");
    }
}

#[test]
fn every_pattern_program_is_as_published() {
    for case in cases(PROGRAM) {
        let name = text(&case, "name");
        assert_eq!(
            pattern_program(text(&case, "pattern")),
            integer(&case, "program"),
            "{name}"
        );
        assert_eq!(
            pattern_preparation(text(&case, "pattern")),
            integer(&case, "preparation"),
            "{name}"
        );
    }
}

#[test]
fn every_schema_vector_is_admitted_as_published() {
    for case in cases(SCHEMA) {
        let name = text(&case, "name");
        let schema = case.get("schema").expect("schema");
        assert_eq!(
            admit_schema(schema).is_ok(),
            flag(&case, "valid"),
            "{name}: {:?}",
            admit_schema(schema)
        );
    }
}

fn outcome(result: Result<(), PayloadError>) -> &'static str {
    match result {
        Ok(()) => "valid",
        Err(PayloadError::NotAnObject | PayloadError::TooDeep) => "shape",
        Err(PayloadError::Invalid) => "invalid",
        Err(PayloadError::Arithmetic) => "arithmetic",
        Err(PayloadError::Pattern(PatternError::Exhausted)) => "exhausted",
        Err(PayloadError::Pattern(PatternError::Refused) | PayloadError::Unsupported) => "refused",
    }
}

#[test]
fn every_payload_vector_validates_and_charges_as_published() {
    for case in cases(PAYLOAD) {
        let name = text(&case, "name");
        let schema = case.get("schema").expect("schema");
        let payload = case.get("payload").expect("payload");
        assert!(admit_schema(schema).is_ok(), "{name}");
        let mut budget = Budget::default();
        let result = validate_payload_in(schema, payload, &mut budget);
        assert_eq!(outcome(result), text(&case, "outcome"), "{name}");
        assert_eq!(
            Some(MAX_PATTERN_WORK - budget.remaining()),
            integer(&case, "charged"),
            "{name}"
        );
        assert_eq!(validate_payload(schema, payload), result, "{name}");
    }
}
