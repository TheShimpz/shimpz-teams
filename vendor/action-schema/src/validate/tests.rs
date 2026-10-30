use std::ops::ControlFlow;

use strict_json::Value;

use super::{Evaluator, PayloadError, validate_payload, validate_payload_in};
use crate::matcher::{Budget, PatternError};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("strict JSON")
}

fn outcome(result: Result<(), PayloadError>) -> &'static str {
    match result {
        Ok(()) => "valid",
        Err(PayloadError::Invalid) => "invalid",
        Err(PayloadError::Pattern(PatternError::Exhausted)) => "exhausted",
        Err(PayloadError::Pattern(PatternError::Refused)) => "refused",
        Err(PayloadError::Arithmetic) => "arithmetic",
        Err(PayloadError::NotAnObject | PayloadError::TooDeep) => "shape",
        Err(PayloadError::Unsupported) => "unsupported",
    }
}

/// Each schema, payload, and the outcome Team's Python validation reaches, keyword by keyword.
const DECIDED: &[(&str, &str, &str)] = &[
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"allOf":[{"type":"string"},{"minLength":2}]}}}"#,
        r#"{"v":"a"}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"allOf":[{"type":"string"},{"minLength":1}]}}}"#,
        r#"{"v":"a"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"anyOf":[{"type":"string"},{"type":"integer"}]}}}"#,
        r#"{"v":null}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"oneOf":[{"type":"string"},{"type":"integer"}]}}}"#,
        r#"{"v":null}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"oneOf":[{"type":"string"},{"type":"integer"}]}}}"#,
        r#"{"v":1}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"oneOf":[{"type":"integer"},{"minimum":0},{"type":"string"}]}}}"#,
        r#"{"v":1}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"not":{"type":"integer"}}}}"#,
        r#"{"v":1}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"not":{"type":"integer"}}}}"#,
        r#"{"v":"a"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"if":{"type":"integer"},"then":{"minimum":3}}}}"#,
        r#"{"v":2}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"if":{"type":"integer"}}}}"#,
        r#"{"v":2}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"if":{"type":"integer"},"else":{"type":"string"}}}}"#,
        r#"{"v":null}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"if":{"type":"integer"}}}}"#,
        r#"{"v":null}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"properties":{"a":{"type":"integer"}}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}}}}}"#,
        r#"{"v":{"a":"x","b":"y"}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"pattern":"a"}}}"#,
        r#"{"v":1}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"patternProperties":{"^a":{"type":"integer"}}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","patternProperties":{"^a":{"type":"integer"},"^b":{}}}}}"#,
        r#"{"v":{"a1":"x","b":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"array","additionalProperties":{"type":"integer"}}}}"#,
        r#"{"v":{"a":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"array","additionalProperties":{"type":"integer"}}}}"#,
        r#"{"v":3}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"array","additionalProperties":{"type":"integer"},"properties":{"a":{}}}}}"#,
        r#"{"v":{"a":"x","b":"y","c":"z"}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"array","additionalProperties":true}}}"#,
        r#"{"v":{"a":"x"}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"patternProperties":{"^a":{},"^b":{}}}"#,
        r#"{"a":1,"b":2}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"patternProperties":{"^a":{},"^b":{}}}"#,
        r#"{"c":1}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","propertyNames":{"maxLength":1},"additionalProperties":false,"patternProperties":{"":{}}}}}"#,
        r#"{"v":{"aa":1,"bb":2}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"dependentSchemas":{"a":{"required":["b"]}}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentSchemas":{"a":{"required":["b","c"]},"x":{"required":["y"]}}}}}"#,
        r#"{"v":{"a":1,"x":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"required":["a"]}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"required":["a","b"]}}}"#,
        r#"{"v":{}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"dependentRequired":{"a":["b"]}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentRequired":{"a":["b","c"],"d":["e"]}}}}"#,
        r#"{"v":{"a":1,"d":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"minProperties":2}}}"#,
        r#"{"v":{"a":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minProperties":2}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"prefixItems":[{"type":"integer"}]}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"prefixItems":[{"type":"integer"},{"type":"integer"}]}}}"#,
        r#"{"v":["a","b"]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"items":{"type":"integer"}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"prefixItems":[{},{}],"items":{"type":"integer"}}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"items":{"type":"integer"}}}}"#,
        r#"{"v":["a","b"]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minItems":2}}}"#,
        r#"{"v":[1]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"maxItems":1}}}"#,
        r#"{"v":[1,2]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minItems":2}}}"#,
        r#"{"v":"ab"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"}}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"},"minContains":2}}}"#,
        r#"{"v":[1,"a"]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"},"maxContains":1.0}}}"#,
        r#"{"v":[1,2]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"},"maxContains":2}}}"#,
        r#"{"v":[1,2]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false}}}"#,
        r#"{"v":[]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"items":{}}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"oneOf":[{"prefixItems":[{}]},{"type":"string"}]}}}"#,
        r#"{"v":[1,2]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"allOf":[{"type":"string"}]}}}"#,
        r#"{"v":[1]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":true}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"if":{"prefixItems":[{"type":"integer"}]}}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"if":{"prefixItems":[{"type":"integer"}]},"else":{"prefixItems":[{}]}}}}"#,
        r#"{"v":["a"]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"if":{"prefixItems":[{"type":"integer"}]}}}}"#,
        r#"{"v":["a"]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"allOf":[{"unevaluatedItems":{"type":"integer"}}]}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"uniqueItems":false}}}"#,
        r#"{"v":[1,1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"uniqueItems":true}}}"#,
        r#"{"v":"aa"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":["integer","string"]}}}"#,
        r#"{"v":null}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minimum":1}}}"#,
        r#"{"v":"a"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"multipleOf":2}}}"#,
        r#"{"v":"a"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minLength":2}}}"#,
        r#"{"v":1}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"exclusiveMaximum":2}}}"#,
        r#"{"v":2}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"exclusiveMinimum":2}}}"#,
        r#"{"v":2.5}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"maximum":2}}}"#,
        r#"{"v":2.0}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"enum":[1,"a"]}}}"#,
        r#"{"v":true}"#,
        "invalid",
    ),
    (
        r##"{"type":"object","additionalProperties":false,"$defs":{"p":{"prefixItems":[{}]}},"properties":{"v":{"$ref":"#/$defs/p","unevaluatedItems":false}}}"##,
        r#"{"v":[1,2]}"#,
        "invalid",
    ),
    (
        r##"{"type":"object","additionalProperties":false,"definitions":{"s":{"type":"string"}},"properties":{"v":{"$ref":"#/definitions/s"}}}"##,
        r#"{"v":1}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"additionalProperties":false}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"additionalProperties":{"type":"integer"}}}}"#,
        r#"{"v":{"b":1,"c":"z"}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"additionalProperties":{"type":"integer"}}}}"#,
        r#"{"v":{"b":1,"c":2}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"propertyNames":{"maxLength":3}}}}"#,
        r#"{"v":{"a":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentSchemas":{"a":{"required":["b"]}}}}}"#,
        r#"{"v":{"c":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentSchemas":{"a":{"required":["b"]},"c":{"required":["d"]}}}}}"#,
        r#"{"v":{"a":1,"b":1,"c":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"items":false}}}"#,
        r#"{"v":[1]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"items":false}}}"#,
        r#"{"v":[]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"allOf":[true]}}}"#,
        r#"{"v":[1]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"unevaluatedItems":false,"if":{"prefixItems":[{}]},"then":{"prefixItems":[{},{}]}}}}"#,
        r#"{"v":[1,2]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"required":["a"]}}}"#,
        r#"{"v":{"a":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"required":[1]}}}"#,
        r#"{"v":{"a":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentRequired":{"a":["b"]}}}}"#,
        r#"{"v":{"c":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentRequired":{"a":["b"]}}}}"#,
        r#"{"v":{"a":1,"b":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"dependentRequired":{"a":[2]}}}}"#,
        r#"{"v":{"a":1}}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"type":"object","additionalProperties":false,"patternProperties":{"":{}},"anyOf":[{"required":["a","b"]},{"required":["c"]}]}}}"#,
        r#"{"v":{"c":1}}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"maximum":2}}}"#,
        r#"{"v":3}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"maximum":2}}}"#,
        r#"{"v":2}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"exclusiveMinimum":2}}}"#,
        r#"{"v":2}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"exclusiveMinimum":2}}}"#,
        r#"{"v":3}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"minimum":2}}}"#,
        r#"{"v":2}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"exclusiveMaximum":2}}}"#,
        r#"{"v":1.5}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"},"minContains":2}}}"#,
        r#"{"v":[1,2]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"}}}}"#,
        r#"{"v":[1]}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"contains":{"type":"integer"},"maxContains":2}}}"#,
        r#"{"v":[1,2,3]}"#,
        "invalid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"const":"a"}}}"#,
        r#"{"v":"a"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"enum":["a","b"]}}}"#,
        r#"{"v":"b"}"#,
        "valid",
    ),
    (
        r#"{"type":"object","additionalProperties":false,"properties":{"v":{"const":"a"}}}"#,
        r#"{"v":"b"}"#,
        "invalid",
    ),
];

#[test]
fn every_keyword_decides_as_python_jsonschema() {
    for (schema, payload, expected) in DECIDED {
        let result = validate_payload(&json(schema), &json(payload));
        assert_eq!(outcome(result), *expected, "{schema} {payload}");
    }
}

#[test]
fn what_admission_refuses_cannot_be_evaluated() {
    let cases = [
        (r#"{"properties":{"v":{"$ref":5}}}"#, "unsupported"),
        (
            r##"{"properties":{"v":{"$ref":"#/$defs/missing"}}}"##,
            "unsupported",
        ),
        (
            r##"{"properties":{"v":{"$dynamicRef":"#meta"}}}"##,
            "unsupported",
        ),
        (
            r#"{"properties":{"v":{"unevaluatedProperties":false}}}"#,
            "refused",
        ),
        (r#"{"properties":{"v":7}}"#, "unsupported"),
        (
            r#"{"properties":{"v":{"items":{"pattern":"\\p{L}"}}}}"#,
            "refused",
        ),
        (
            r##"{"properties":{"v":{"$ref":"#/$defs/missing","unevaluatedItems":false}}}"##,
            "unsupported",
        ),
    ];
    for (schema, expected) in cases {
        let result = validate_payload(&json(schema), &json(r#"{"v":["a"]}"#));
        assert_eq!(outcome(result), expected, "{schema}");
    }
}

#[test]
fn boolean_subschemas_pass_or_fail_everything() {
    let schema = json(r#"{"properties":{"v":true,"w":false}}"#);
    assert_eq!(validate_payload(&schema, &json(r#"{"v":1}"#)), Ok(()));
    assert_eq!(
        validate_payload(&schema, &json(r#"{"w":1}"#)),
        Err(PayloadError::Invalid)
    );
}

#[test]
fn the_payload_is_one_object_within_the_depth_bound() {
    let schema = json("{}");
    assert_eq!(
        validate_payload(&schema, &json("[]")),
        Err(PayloadError::NotAnObject)
    );
    let deep = format!("{{\"a\":{}1{}}}", "[".repeat(32), "]".repeat(32));
    assert_eq!(
        validate_payload(&schema, &json(&deep)),
        Err(PayloadError::TooDeep)
    );
    let deepest = format!("{{\"a\":{}1{}}}", "[".repeat(31), "]".repeat(31));
    assert_eq!(validate_payload(&schema, &json(&deepest)), Ok(()));
}

#[test]
fn a_shared_budget_carries_earlier_charges() {
    let schema = json(r#"{"properties":{"v":{"pattern":"a"}}}"#);
    let payload = json(r#"{"v":"aaaa"}"#);
    let mut budget = Budget::default();
    assert_eq!(validate_payload_in(&schema, &payload, &mut budget), Ok(()));
    let prepared = crate::pattern_preparation("a").expect("an admitted pattern");
    assert_eq!(
        crate::MAX_PATTERN_WORK - budget.remaining(),
        prepared + 4 * 24
    );
    // The budget keeps the prepared pattern, so a second validation charges only its search.
    assert_eq!(validate_payload_in(&schema, &payload, &mut budget), Ok(()));
    assert_eq!(
        crate::MAX_PATTERN_WORK - budget.remaining(),
        prepared + 8 * 24
    );
}

#[test]
fn a_consumer_that_stops_stops_the_evaluation() {
    let root = json(r#"{"allOf":[{"type":"string"},{"pattern":"a"}]}"#);
    let mut budget = Budget::default();
    let mut evaluator = Evaluator {
        root: &root,
        budget: &mut budget,
    };
    let mut errors = 0;
    let flow = evaluator.descend(&json("1"), &root, &mut || {
        errors += 1;
        ControlFlow::Break(())
    });
    assert_eq!(flow, Ok(ControlFlow::Break(())));
    assert_eq!(errors, 1);
    assert_eq!(budget.remaining(), crate::MAX_PATTERN_WORK);
}
