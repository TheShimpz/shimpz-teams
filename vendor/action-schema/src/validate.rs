//! Payload validation against an admitted schema, as Python `jsonschema` 4.25.1's Draft 2020-12 validator evaluates it.
//!
//! Each subschema's keywords run in schema order, and each keyword reports its errors in the order and at the moments
//! that validator yields them. A consumer that needs one error stops the evaluation there, as Python stops pulling from
//! the error generator: `validate`, `is_valid`, and the first-error probes of `not`, `if`, `contains`, and
//! `unevaluatedItems` stop at the first error, while `anyOf` and `oneOf` evaluate a subschema completely. So the pattern
//! searches that run, and what each charges, are exactly the searches Python runs. Team's pattern keywords
//! (`pattern`, `patternProperties`, `additionalProperties`) match with the bounded RE2-equivalent matcher; `uniqueItems`
//! is exact JSON Schema equality, without `jsonschema`'s sort shortcut; and `multipleOf` raises where Python raises.

mod applicators;
mod assertions;
mod equality;

use std::fmt;
use std::ops::ControlFlow;

use strict_json::Value;

use crate::matcher::{Budget, PatternError};
use crate::walk::{elements, members};

/// The deepest an Action payload nests below its root object.
pub const MAX_PAYLOAD_DEPTH: usize = 32;

/// Why a payload is refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PayloadError {
    /// The payload is not a JSON object.
    NotAnObject,
    /// A value nests more than [`MAX_PAYLOAD_DEPTH`] levels below the root.
    TooDeep,
    /// The payload does not match the schema.
    Invalid,
    /// A pattern search could not run: the pattern is outside the dialect, or the matching work is spent.
    Pattern(PatternError),
    /// A number cannot be compared as the schema requires: Python's arithmetic raises.
    Arithmetic,
    /// The schema uses something admission refuses: a missing reference target, `$dynamicRef`,
    /// `unevaluatedProperties`, or a value that is no schema at a subschema position.
    Unsupported,
}

impl fmt::Display for PayloadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAnObject => formatter.write_str("Action payload must be an object"),
            Self::TooDeep => formatter.write_str("Action payload nests too deeply"),
            Self::Invalid => {
                formatter.write_str("Action payload does not match its reviewed schema")
            }
            Self::Pattern(error) => error.fmt(formatter),
            Self::Arithmetic => formatter.write_str("Action payload number cannot be evaluated"),
            Self::Unsupported => formatter.write_str("Action schema is not admitted"),
        }
    }
}

impl std::error::Error for PayloadError {}

/// Validates one Action payload against an admitted schema under a fresh matching budget.
///
/// # Errors
///
/// Returns the [`PayloadError`] that refuses the payload: its shape first, then the first error the evaluation meets.
pub fn validate_payload(schema: &Value, payload: &Value) -> Result<(), PayloadError> {
    validate_payload_in(schema, payload, &mut Budget::default())
}

/// Validates one Action payload against an admitted schema, charging every search to `budget`, which may already
/// carry earlier charges.
///
/// # Errors
///
/// Returns the [`PayloadError`] that refuses the payload: its shape first, then the first error the evaluation meets.
pub fn validate_payload_in(
    schema: &Value,
    payload: &Value,
    budget: &mut Budget,
) -> Result<(), PayloadError> {
    if !matches!(payload, Value::Object(_)) {
        return Err(PayloadError::NotAnObject);
    }
    if !depth_within(payload, MAX_PAYLOAD_DEPTH) {
        return Err(PayloadError::TooDeep);
    }
    let mut evaluator = Evaluator {
        root: schema,
        budget,
    };
    if evaluator.is_valid(payload, schema)? {
        Ok(())
    } else {
        Err(PayloadError::Invalid)
    }
}

/// Whether no array element or member value nests more than `limit` levels below `value`.
fn depth_within(value: &Value, limit: usize) -> bool {
    let mut pending = vec![(value, 0_usize)];
    while let Some((node, depth)) = pending.pop() {
        if depth > limit {
            return false;
        }
        pending.extend(elements(node).iter().map(|child| (child, depth + 1)));
        pending.extend(members(node).iter().map(|(_, child)| (child, depth + 1)));
    }
    true
}

/// Whether the error consumer wants more errors.
type Flow = ControlFlow<()>;
/// One evaluation step: whether to go on, or why evaluation cannot complete.
type Step = Result<Flow, PayloadError>;
/// Receives each error as it is found and answers whether to go on.
type Sink<'s> = &'s mut dyn FnMut() -> Flow;

const GO_ON: Step = Ok(ControlFlow::Continue(()));

/// One payload's evaluation: the root that references resolve in, and the payload's matching budget.
struct Evaluator<'a, 'b> {
    root: &'a Value,
    budget: &'b mut Budget,
}

impl<'a> Evaluator<'a, '_> {
    /// Evaluates every keyword of `schema` on `instance`, reporting each error to `sink` until it stops.
    fn descend(&mut self, instance: &Value, schema: &'a Value, sink: Sink<'_>) -> Step {
        match schema {
            Value::Bool(true) => GO_ON,
            Value::Bool(false) => Ok(sink()),
            Value::Object(keywords) => {
                for (keyword, value) in keywords {
                    if self
                        .keyword(keyword, value, instance, schema, sink)?
                        .is_break()
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                }
                GO_ON
            }
            _ => Err(PayloadError::Unsupported),
        }
    }

    /// Whether `instance` has no error under `schema`, evaluated up to its first error.
    fn is_valid(&mut self, instance: &Value, schema: &'a Value) -> Result<bool, PayloadError> {
        let mut valid = true;
        let _ = self.descend(instance, schema, &mut || {
            valid = false;
            ControlFlow::Break(())
        })?;
        Ok(valid)
    }

    /// Whether `instance` has any error under `schema`, evaluated completely, as `list(descend(...))` collects them.
    fn has_errors(&mut self, instance: &Value, schema: &'a Value) -> Result<bool, PayloadError> {
        let mut found = false;
        let _ = self.descend(instance, schema, &mut || {
            found = true;
            ControlFlow::Continue(())
        })?;
        Ok(found)
    }

    /// Whether `pattern` matches `subject`, charged against the payload's budget.
    fn matches(&mut self, pattern: &str, subject: &str) -> Result<bool, PayloadError> {
        crate::matcher::pattern_matches(pattern, subject, self.budget)
            .map_err(PayloadError::Pattern)
    }

    /// Runs the validator function of one Draft 2020-12 keyword; a keyword without one, such as an annotation or
    /// `format`, which is never asserted, does nothing.
    fn keyword(
        &mut self,
        keyword: &str,
        value: &'a Value,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        match keyword {
            "$ref" => self.reference(value, instance, sink),
            "additionalProperties" => self.additional_properties(value, instance, schema, sink),
            "allOf" => self.all_of(value, instance, sink),
            "anyOf" => self.any_of(value, instance, sink),
            "contains" => self.contains(value, instance, schema, sink),
            "dependentSchemas" => self.dependent_schemas(value, instance, sink),
            "if" => self.if_then_else(value, instance, schema, sink),
            "items" => self.items(value, instance, schema, sink),
            "not" => self.not(value, instance, sink),
            "oneOf" => self.one_of(value, instance, sink),
            "pattern" => self.pattern(value, instance, sink),
            "patternProperties" => self.pattern_properties(value, instance, sink),
            "prefixItems" => self.prefix_items(value, instance, sink),
            "properties" => self.properties(value, instance, sink),
            "propertyNames" => self.property_names(value, instance, sink),
            "unevaluatedItems" => self.unevaluated_items(instance, schema, sink),
            "$dynamicRef" => Err(PayloadError::Unsupported),
            "unevaluatedProperties" => Err(PayloadError::Pattern(PatternError::Refused)),
            _ => assertions::assert(keyword, value, instance, sink),
        }
    }
}

#[cfg(test)]
mod tests;
