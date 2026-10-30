//! The positions of an Action schema a value's members and items reach, and which of them may hold a secret.
//!
//! The whole value is the first position, checked against the schema root, so the root's own applicators derive each
//! member's destinations exactly as nested ones do. Applicators whose effect is exact are followed position by
//! position: local `$ref`, `allOf`, `anyOf`, `oneOf`, `dependentSchemas` of a present member, object `properties`,
//! `patternProperties` (matched by the linear-time matcher), and `additionalProperties`, and array `prefixItems` and
//! `items`. A member whose name marks a secret, or a destination annotated `writeOnly` or `format: password`, is a
//! secret position. Every other applicator (`if`, `then`, `else`, `not`, `contains`, `unevaluatedItems`,
//! `contentSchema`, `propertyNames`) is not modelled, so a position is secret when anything that one reaches could
//! hold a secret. Indirection deeper than [`MAX_SECRET_DEPTH`] is refused rather than followed.

use action_schema::{Budget, pattern_matches};
use strict_json::Value;
use team_protocol::python::text::lower;

use super::PlanError;
use crate::json::{as_str, elements, members};

/// Literal nesting and schema indirection checked for secret destinations; anything deeper is refused.
pub const MAX_SECRET_DEPTH: usize = 64;
/// A secret is never a literal: one of these in a destination name marks it.
const SECRET_MARKERS: [&str; 9] = [
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "credential",
];
/// Applicators whose effect at a position is not modelled; anything they reach that could hold a secret refuses.
const UNMODELLED: [&str; 8] = [
    "if",
    "then",
    "else",
    "not",
    "contains",
    "unevaluatedItems",
    "contentSchema",
    "propertyNames",
];
/// The Draft 2020-12 positions that hold one subschema.
const APPLICATORS: [&str; 11] = [
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
];
/// The positions that hold a list of subschemas.
const LIST_APPLICATORS: [&str; 4] = ["allOf", "anyOf", "oneOf", "prefixItems"];
/// The positions that hold a map of subschemas.
const MAP_APPLICATORS: [&str; 5] = [
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
];

/// A subschema at one position: a schema node, or every subschema the position's parent applies there together (the
/// `{"allOf": [...]}` Python builds, which holds nothing of its own).
#[derive(Clone, Debug, PartialEq)]
pub enum Sub<'a> {
    /// One node of the schema.
    Node(&'a Value),
    /// Every one of these applies.
    AllOf(Vec<&'a Value>),
}

/// What a position holds, as far as a dependent schema needs it.
#[derive(Clone, Copy, Debug)]
pub enum Held<'v> {
    /// A value not at hand, which may hold any member: every dependent schema applies.
    Unknown,
    /// This value.
    Value(&'v Value),
    /// A step input whose members a run fills or a plan holds: each member counts for presence.
    Supplied(&'v [(String, Option<Value>)]),
}

impl Held<'_> {
    /// Whether the dependent schema of member `key` applies, or `None` when the position holds no object.
    fn depends(&self, key: &str) -> Option<bool> {
        match self {
            Self::Unknown => Some(true),
            Self::Value(Value::Object(found)) => Some(found.iter().any(|(name, _)| name == key)),
            Self::Supplied(found) => Some(found.iter().any(|(name, _)| name == key)),
            Self::Value(_) => None,
        }
    }
}

/// The subschema an admitted schema's `$ref` names: its root or one direct definition, whose name may escape.
#[must_use]
pub fn reference_target<'a>(root: &'a Value, reference: &str) -> Option<&'a Value> {
    if reference == "#" {
        return Some(root);
    }
    let rest: String = reference.chars().skip(2).collect();
    let (container, name) = rest.split_once('/').unwrap_or((rest.as_str(), ""));
    let container = root.get(container)?;
    let name = name.replace("~1", "/").replace("~0", "~");
    container.get(&name)
}

/// Every subschema a node applies directly, through each Draft 2020-12 applicator except `$ref`.
fn applied_subschemas(node: &Value) -> Vec<&Value> {
    let mut reached = Vec::new();
    for keyword in APPLICATORS {
        if let Some(child) = node.get(keyword) {
            reached.push(child);
        }
    }
    for keyword in LIST_APPLICATORS {
        reached.extend(node.get(keyword).map(elements).unwrap_or_default());
    }
    for keyword in MAP_APPLICATORS {
        reached.extend(
            node.get(keyword)
                .map(members)
                .unwrap_or_default()
                .iter()
                .map(|(_, child)| child),
        );
    }
    reached
}

/// The subschemas that apply at one position.
///
/// That is the node, its local reference, its combinator members, and the `dependentSchemas` of each member the
/// position holds.
///
/// # Errors
///
/// `plan-secret-literal` when indirection goes deeper than [`MAX_SECRET_DEPTH`].
pub fn applicable<'a>(
    root: &'a Value,
    sub: &Sub<'a>,
    depth: usize,
    held: Held<'_>,
) -> Result<Vec<&'a Value>, PlanError> {
    let mut found = Vec::new();
    match sub {
        Sub::Node(node) => collect(root, Some(node), depth, held, &mut found)?,
        Sub::AllOf(nodes) => {
            if depth > MAX_SECRET_DEPTH {
                return Err(PlanError::new("plan-secret-literal"));
            }
            for node in nodes {
                collect(root, Some(node), depth + 1, held, &mut found)?;
            }
        }
    }
    Ok(found)
}

fn collect<'a>(
    root: &'a Value,
    node: Option<&'a Value>,
    depth: usize,
    held: Held<'_>,
    found: &mut Vec<&'a Value>,
) -> Result<(), PlanError> {
    if depth > MAX_SECRET_DEPTH {
        return Err(PlanError::new("plan-secret-literal"));
    }
    let Some(node @ Value::Object(_)) = node else {
        return Ok(());
    };
    let mut reached: Vec<Option<&'a Value>> = Vec::new();
    if let Some(reference) = node.get("$ref").and_then(as_str) {
        reached.push(reference_target(root, reference));
    }
    for combinator in ["allOf", "anyOf", "oneOf"] {
        if let Some(Value::Array(items)) = node.get(combinator) {
            reached.extend(items.iter().map(Some));
        }
    }
    if let Some(Value::Object(dependent)) = node.get("dependentSchemas") {
        for (key, item) in dependent {
            if held.depends(key) == Some(true) {
                reached.push(Some(item));
            }
        }
    }
    found.push(node);
    for member in reached {
        collect(root, member, depth + 1, held, found)?;
    }
    Ok(())
}

/// Every subschema the candidates apply to one object member.
///
/// That is its property, each matching pattern, or else the candidate's additional-properties schema.
///
/// # Errors
///
/// `plan-secret-literal` when a pattern cannot be matched within its work budget.
pub fn member_schemas<'a>(candidates: &[&'a Value], key: &str) -> Result<Sub<'a>, PlanError> {
    let mut budget = Budget::default();
    let mut reached = Vec::new();
    for item in candidates {
        let properties = match item.get("properties") {
            Some(properties @ Value::Object(_)) => members(properties),
            _ => &[],
        };
        let patterns = match item.get("patternProperties") {
            Some(patterns @ Value::Object(_)) => members(patterns),
            _ => &[],
        };
        let mut matched = Vec::new();
        for (pattern, schema) in patterns {
            if pattern_matches(pattern, key, &mut budget)
                .map_err(|_| PlanError::new("plan-secret-literal"))?
            {
                matched.push(schema);
            }
        }
        let property = properties
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, schema)| schema);
        if let Some(property) = property {
            reached.push(property);
        }
        let none_matched = matched.is_empty();
        reached.extend(matched);
        if property.is_none()
            && none_matched
            && let Some(additional @ Value::Object(_)) = item.get("additionalProperties")
        {
            reached.push(additional);
        }
    }
    Ok(Sub::AllOf(reached))
}

/// Every subschema the candidates apply to one array item.
#[must_use]
pub fn item_schemas<'a>(candidates: &[&'a Value], index: usize) -> Sub<'a> {
    let mut reached = Vec::new();
    for item in candidates {
        match item.get("prefixItems") {
            Some(Value::Array(prefix)) if index < prefix.len() => reached.push(&prefix[index]),
            _ => {
                if let Some(items) = item.get("items") {
                    reached.push(items);
                }
            }
        }
    }
    Sub::AllOf(reached)
}

/// Every schema any item of an array may have: each of its prefix items and its items.
#[must_use]
pub fn any_item<'a>(candidates: &[&'a Value]) -> Vec<&'a Value> {
    let mut found = Vec::new();
    for item in candidates {
        if let Some(Value::Array(prefix)) = item.get("prefixItems") {
            found.extend(prefix.iter());
        }
        if let Some(items) = item.get("items") {
            found.push(items);
        }
    }
    found
}

fn secret_name(name: &str) -> bool {
    let lowered = lower(name).replace('-', "_");
    SECRET_MARKERS.iter().any(|marker| lowered.contains(marker))
}

fn marked(subschema: &Value) -> bool {
    matches!(subschema.get("writeOnly"), Some(Value::Bool(true)))
        || matches!(subschema.get("format"), Some(Value::String(format)) if format == "password")
}

/// Whether one position itself is a secret destination, or an unmodelled applicator there could reach one.
#[must_use]
pub fn secret_position(root: &Value, name: &str, candidates: &[&Value]) -> bool {
    secret_name(name)
        || candidates.iter().any(|item| marked(item))
        || candidates.iter().any(|item| {
            UNMODELLED
                .iter()
                .filter_map(|keyword| item.get(keyword))
                .any(|reached| could_hold_secret(root, Some(reached), 0))
        })
}

/// Whether anything a subschema reaches, through every applicator and local reference, could hold a secret.
#[must_use]
pub fn could_hold_secret(root: &Value, subschema: Option<&Value>, depth: usize) -> bool {
    if depth > MAX_SECRET_DEPTH {
        return true;
    }
    let Some(subschema @ Value::Object(_)) = subschema else {
        return false;
    };
    let properties = match subschema.get("properties") {
        Some(properties @ Value::Object(_)) => members(properties),
        _ => &[],
    };
    if marked(subschema) || properties.iter().any(|(key, _)| secret_name(key)) {
        return true;
    }
    let mut reached: Vec<Option<&Value>> = applied_subschemas(subschema)
        .into_iter()
        .map(Some)
        .collect();
    if let Some(reference) = subschema.get("$ref").and_then(as_str) {
        reached.push(reference_target(root, reference));
    }
    reached
        .into_iter()
        .any(|item| could_hold_secret(root, item, depth + 1))
}
