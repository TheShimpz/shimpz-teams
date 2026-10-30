//! The keywords that assert something of the instance itself and apply no subschema.

use std::cmp::Ordering;

use strict_json::Value;

use super::equality::{equal, unique};
use super::{GO_ON, PayloadError, Sink, Step};
use crate::numbers;
use crate::walk::{elements, members};

/// Runs one assertion keyword; any other keyword asserts nothing.
pub(super) fn assert(keyword: &str, value: &Value, instance: &Value, sink: Sink<'_>) -> Step {
    let failed = match keyword {
        "type" => !typed(value, instance),
        "const" => !equal(instance, value),
        "enum" => !elements(value).iter().any(|each| equal(each, instance)),
        "minimum" => numeric(instance, value, Ordering::is_lt),
        "maximum" => numeric(instance, value, Ordering::is_gt),
        "exclusiveMinimum" => numeric(instance, value, Ordering::is_le),
        "exclusiveMaximum" => numeric(instance, value, Ordering::is_ge),
        "multipleOf" => not_multiple(instance, value)?,
        "minLength" | "maxLength" => match instance {
            Value::String(text) => bounded(keyword, text.chars().count(), value),
            _ => false,
        },
        "minItems" | "maxItems" => match instance {
            Value::Array(items) => bounded(keyword, items.len(), value),
            _ => false,
        },
        "minProperties" | "maxProperties" => match instance {
            Value::Object(entries) => bounded(keyword, entries.len(), value),
            _ => false,
        },
        "uniqueItems" => *value == Value::Bool(true) && !unique(elements(instance)),
        "required" => return required(value, instance, sink),
        "dependentRequired" => return dependent_required(value, instance, sink),
        _ => false,
    };
    if failed { Ok(sink()) } else { GO_ON }
}

/// Whether `instance` has one of the types `value` names, as `jsonschema`'s Draft 2020-12 type checker decides.
fn typed(value: &Value, instance: &Value) -> bool {
    match value {
        Value::String(kind) => is_type(instance, kind),
        other => elements(other).iter().any(|kind| match kind {
            Value::String(kind) => is_type(instance, kind),
            _ => false,
        }),
    }
}

fn is_type(instance: &Value, kind: &str) -> bool {
    match (kind, instance) {
        ("array", Value::Array(_))
        | ("boolean", Value::Bool(_))
        | ("null", Value::Null)
        | ("number", Value::Number(_))
        | ("object", Value::Object(_))
        | ("string", Value::String(_)) => true,
        ("integer", Value::Number(number)) => numbers::is_integer(number),
        _ => false,
    }
}

/// Whether a numeric bound refuses `instance`: `refuses` holds of the instance compared with the bound.
fn numeric(instance: &Value, bound: &Value, refuses: fn(Ordering) -> bool) -> bool {
    match (instance, bound) {
        (Value::Number(instance), Value::Number(bound)) => {
            refuses(numbers::compare(instance, bound))
        }
        _ => false,
    }
}

fn not_multiple(instance: &Value, divisor: &Value) -> Result<bool, PayloadError> {
    match (instance, divisor) {
        (Value::Number(instance), Value::Number(divisor)) => {
            numbers::not_multiple(instance, divisor).map_err(|_| PayloadError::Arithmetic)
        }
        _ => Ok(false),
    }
}

/// Whether a `min*` bound is above `count` or a `max*` bound below it.
fn bounded(keyword: &str, count: usize, bound: &Value) -> bool {
    let Value::Number(bound) = bound else {
        return false;
    };
    let ordering = numbers::compare_count(count, bound);
    if keyword.starts_with("min") {
        ordering.is_lt()
    } else {
        ordering.is_gt()
    }
}

/// One error for each required member the object lacks, in the order `required` lists them.
fn required(names: &Value, instance: &Value, sink: Sink<'_>) -> Step {
    if !matches!(instance, Value::Object(_)) {
        return GO_ON;
    }
    missing(elements(names), instance, sink)
}

/// One error for each name the object lacks; Python finds no member under a name that is not a string.
fn missing(names: &[Value], instance: &Value, sink: Sink<'_>) -> Step {
    for name in names {
        let present = matches!(name, Value::String(name) if instance.get(name).is_some());
        if !present && sink().is_break() {
            return Ok(std::ops::ControlFlow::Break(()));
        }
    }
    GO_ON
}

/// For each present member with dependencies, in schema order, one error for each dependency the object lacks.
fn dependent_required(map: &Value, instance: &Value, sink: Sink<'_>) -> Step {
    if !matches!(instance, Value::Object(_)) {
        return GO_ON;
    }
    for (name, dependencies) in members(map) {
        if instance.get(name).is_some()
            && missing(elements(dependencies), instance, sink)?.is_break()
        {
            return Ok(std::ops::ControlFlow::Break(()));
        }
    }
    GO_ON
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{bounded, typed};

    #[test]
    fn only_named_types_match() {
        let value = Value::Number(Number::Int(1));
        assert!(!typed(
            &Value::Array(vec![Value::Number(Number::Int(3))]),
            &value
        ));
        assert!(typed(
            &Value::Array(vec![Value::String("integer".to_owned())]),
            &value
        ));
        assert!(!typed(&Value::Null, &value));
    }

    #[test]
    fn a_bound_that_is_no_number_bounds_nothing() {
        assert!(!bounded("minLength", 0, &Value::String("a".to_owned())));
        assert!(bounded("minLength", 0, &Value::Number(Number::Int(1))));
        assert!(bounded("maxItems", 2, &Value::Number(Number::Float(1.5))));
    }
}
