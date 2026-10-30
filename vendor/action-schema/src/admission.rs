//! The one admission predicate of an Action input or output schema.

use std::collections::HashMap;
use std::fmt;

use strict_json::Value;

use crate::walk::{self, LIST_APPLICATORS, elements, members};
use crate::{
    DRAFT_2020_12, MAX_BYTES, MAX_EXPANDED_SUBSCHEMAS, MAX_NODES, MAX_PREPARATION_WORK,
    MAX_SUBSCHEMA_DEPTH, meta, pattern, size,
};

/// Why a schema is not admitted; `Display` completes "Assistant Action `<kind>` schema ...".
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The root is not an object whose `type` is `"object"`.
    NotAnObject,
    /// The schema exceeds its JSON value or encoded byte bound.
    TooLarge,
    /// The schema is not a valid Draft 2020-12 schema.
    Invalid,
    /// A reference names something other than the root or one direct definition.
    Reference,
    /// A subschema declares a dialect other than Draft 2020-12.
    Dialect,
    /// A subschema below the root declares `$id`.
    NestedIdentifier,
    /// A subschema uses `unevaluatedProperties`.
    UnevaluatedProperties,
    /// A pattern is outside the admitted dialect or its program bound.
    Pattern,
    /// The preparations of the distinct patterns sum beyond [`MAX_PREPARATION_WORK`].
    Preparation,
    /// A subschema lies more than 32 levels below the root.
    TooDeep,
    /// A reference is missing or leads back into a subschema that contains it.
    Cycle,
    /// The references expand to more than 4,096 subschemas.
    Expanded,
    /// A subschema that may describe an object leaves it open.
    Open,
    /// A closed position holds a boolean subschema.
    BooleanSubschema,
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::NotAnObject => "must describe an object",
            Self::TooLarge => "is too large",
            Self::Invalid => "is invalid",
            Self::Reference => "must reference only its root or a named definition",
            Self::Dialect => "must use only the Draft 2020-12 dialect",
            Self::NestedIdentifier => "must not declare a nested identifier",
            Self::UnevaluatedProperties => "must not use unevaluatedProperties",
            Self::Pattern => "must use only patterns the linear-time matcher admits",
            Self::Preparation => "must prepare its patterns within half the matching budget",
            Self::TooDeep => "nests subschemas too deeply",
            Self::Cycle => "must resolve every reference without a cycle",
            Self::Expanded => "is too large once its references are expanded",
            Self::Open => "must close every object",
            Self::BooleanSubschema => "must not use a boolean subschema",
        })
    }
}

impl std::error::Error for Refusal {}

/// Admits one Action input or output schema, or names the first rule it breaks.
///
/// # Errors
///
/// Returns the [`Refusal`] of the first rule the schema breaks, checked in this order: the root, the JSON value bound,
/// the meta-schema, the byte bound, each walked subschema, the patterns' preparation, the reference expansion, and the
/// closed objects.
pub fn admit_schema(schema: &Value) -> Result<(), Refusal> {
    if schema.get("type") != Some(&Value::String("object".to_owned())) {
        return Err(Refusal::NotAnObject);
    }
    if !walk::nodes_within(schema, MAX_NODES) {
        return Err(Refusal::TooLarge);
    }
    if !meta::valid(schema) {
        return Err(Refusal::Invalid);
    }
    if size::encoded_len(schema) > MAX_BYTES {
        return Err(Refusal::TooLarge);
    }
    let preparations = walked(schema)?;
    if preparations.values().fold(0_u64, |total, preparation| {
        total.saturating_add(*preparation)
    }) > MAX_PREPARATION_WORK
    {
        return Err(Refusal::Preparation);
    }
    let expansion = walk::expand(schema).ok_or(Refusal::Cycle)?;
    if expansion.subschemas > MAX_EXPANDED_SUBSCHEMAS {
        return Err(Refusal::Expanded);
    }
    if expansion.depth > MAX_SUBSCHEMA_DEPTH {
        return Err(Refusal::TooDeep);
    }
    closed(schema)
}

/// Checks every subschema position the meta-schema validates as a schema, and returns the preparation of each distinct
/// pattern. The legacy `dependencies` keyword applies nothing in Draft 2020-12, so only its patterns and depth are
/// checked there.
fn walked(root: &Value) -> Result<HashMap<&str, u64>, Refusal> {
    let mut preparations = HashMap::new();
    let mut pending = vec![(root, 0_usize, true)];
    while let Some((node, depth, applied)) = pending.pop() {
        if !matches!(node, Value::Object(_)) {
            continue;
        }
        if depth > MAX_SUBSCHEMA_DEPTH {
            return Err(Refusal::TooDeep);
        }
        if applied {
            node_admitted(node, depth > 0)?;
        }
        patterns_admitted(node, &mut preparations)?;
        pending.extend(walk::applied(node).map(|child| (child, depth + 1, applied)));
        let legacy = node.get("dependencies").map_or(&[][..], members);
        pending.extend(legacy.iter().map(|(_, child)| (child, depth + 1, false)));
    }
    Ok(preparations)
}

fn node_admitted(node: &Value, nested: bool) -> Result<(), Refusal> {
    let reference_admitted = node.get("$ref").is_none_or(|reference| match reference {
        Value::String(reference) => local_reference(reference),
        _ => false,
    });
    if !reference_admitted || node.get("$dynamicRef").is_some() {
        return Err(Refusal::Reference);
    }
    if node
        .get("$schema")
        .is_some_and(|dialect| *dialect != Value::String(DRAFT_2020_12.to_owned()))
    {
        return Err(Refusal::Dialect);
    }
    if nested && node.get("$id").is_some() {
        return Err(Refusal::NestedIdentifier);
    }
    if node.get("unevaluatedProperties").is_some() {
        return Err(Refusal::UnevaluatedProperties);
    }
    Ok(())
}

/// A reference may name only the root or one direct definition. A percent escape is refused because the resolver
/// decodes it before it splits the pointer.
fn local_reference(reference: &str) -> bool {
    reference == "#"
        || ["#/$defs/", "#/definitions/"].iter().any(|prefix| {
            reference
                .strip_prefix(prefix)
                .is_some_and(|name| !name.is_empty() && !name.contains(['/', '%']))
        })
}

/// Admits every pattern of one node, recording each one's preparation.
fn patterns_admitted<'a>(
    node: &'a Value,
    preparations: &mut HashMap<&'a str, u64>,
) -> Result<(), Refusal> {
    let value = match node.get("pattern") {
        Some(Value::String(pattern)) => Some(pattern.as_str()),
        _ => None,
    };
    let names = node
        .get("patternProperties")
        .map_or(&[][..], members)
        .iter()
        .map(|(name, _)| name.as_str());
    for pattern in value.into_iter().chain(names) {
        let preparation = pattern::pattern_preparation(pattern).ok_or(Refusal::Pattern)?;
        preparations.insert(pattern, preparation);
    }
    Ok(())
}

/// The positions the closed-object rule descends into, besides the list applicators.
const CLOSED_APPLICATORS: [&str; 8] = [
    "additionalProperties",
    "propertyNames",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
];
const CLOSED_MAP_APPLICATORS: [&str; 5] = [
    "properties",
    "patternProperties",
    "dependentSchemas",
    "$defs",
    "definitions",
];
/// The keywords that make an untyped subschema describe an object.
const OBJECT_KEYWORDS: [&str; 9] = [
    "properties",
    "patternProperties",
    "additionalProperties",
    "required",
    "minProperties",
    "maxProperties",
    "dependentRequired",
    "dependentSchemas",
    "propertyNames",
];

/// Every subschema that may describe an object declares `additionalProperties: false`, which is the closing marker and
/// not a boolean subschema; every other subschema at a closed position is an object.
fn closed(root: &Value) -> Result<(), Refusal> {
    let mut pending = vec![root];
    while let Some(node) = pending.pop() {
        match node {
            Value::Bool(_) => return Err(Refusal::BooleanSubschema),
            Value::Object(_) => {}
            _ => return Err(Refusal::Invalid),
        }
        if permits_object(node) && node.get("additionalProperties") != Some(&Value::Bool(false)) {
            return Err(Refusal::Open);
        }
        pending.extend(CLOSED_APPLICATORS.iter().filter_map(|keyword| {
            node.get(keyword).filter(|child| {
                !(**child == Value::Null
                    || (*keyword == "additionalProperties" && **child == Value::Bool(false)))
            })
        }));
        pending.extend(
            LIST_APPLICATORS
                .iter()
                .filter_map(|keyword| node.get(keyword))
                .flat_map(elements),
        );
        pending.extend(
            CLOSED_MAP_APPLICATORS
                .iter()
                .filter_map(|keyword| node.get(keyword))
                .flat_map(|map| members(map).iter().map(|(_, child)| child)),
        );
    }
    Ok(())
}

fn permits_object(node: &Value) -> bool {
    match node.get("type") {
        Some(Value::String(kind)) => kind == "object",
        Some(Value::Array(kinds)) => kinds
            .iter()
            .any(|kind| *kind == Value::String("object".to_owned())),
        None | Some(Value::Null) => OBJECT_KEYWORDS
            .iter()
            .any(|keyword| node.get(keyword).is_some()),
        Some(_) => false,
    }
}

#[cfg(test)]
mod tests;
