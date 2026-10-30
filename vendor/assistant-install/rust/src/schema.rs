//! The dependency-free JSON Schema subset the contract's schemas use (`../v1/schema_validator.py`).
//!
//! A schema node is a boolean or an object of [`SUPPORTED_KEYWORDS`]. References resolve by URL against the loaded
//! documents with an RFC 6901 fragment. `type` is one of `array`, `integer`, `object`, or `string`; a float is never an
//! integer. `const`, `enum`, and `uniqueItems` compare values by their sorted-key JSON text, so `1`, `1.0`, and `true`
//! differ. Lengths count code points. A `pattern` is searched anywhere in the string, and `$` matches only at its end,
//! as JSON Schema's ECMA-262 dialect and the producer read it.

use std::collections::HashMap;

use fancy_regex::Regex;
use strict_json::{Number, Value};

use crate::json::{Members, array, canonical, object, string};

/// The keywords a contract schema may use.
pub const SUPPORTED_KEYWORDS: [&str; 27] = [
    "$defs",
    "$id",
    "$ref",
    "$schema",
    "additionalProperties",
    "allOf",
    "const",
    "contains",
    "enum",
    "items",
    "maxItems",
    "maxContains",
    "maxLength",
    "maximum",
    "minItems",
    "minContains",
    "minLength",
    "minimum",
    "not",
    "oneOf",
    "pattern",
    "prefixItems",
    "properties",
    "required",
    "title",
    "type",
    "uniqueItems",
];
const SCHEMA_LISTS: [&str; 3] = ["allOf", "oneOf", "prefixItems"];
const SCHEMA_MAPS: [&str; 2] = ["$defs", "properties"];
const SCHEMA_CHILDREN: [&str; 4] = ["additionalProperties", "contains", "items", "not"];

/// The loaded schema documents by their `$id`, with every pattern compiled once.
#[derive(Debug)]
pub struct Documents {
    documents: HashMap<String, Value>,
    patterns: HashMap<String, Regex>,
}

/// A schema node that is not a boolean, or an object of supported keywords with resolvable references and
/// compilable patterns.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidSchema;

impl Documents {
    /// Loads `documents` (`$id`, document) and checks every node of each.
    ///
    /// # Errors
    /// [`InvalidSchema`] for any node the subset does not admit.
    pub fn load(documents: Vec<(String, Value)>) -> Result<Self, InvalidSchema> {
        let mut loaded = Self {
            documents: documents.into_iter().collect(),
            patterns: HashMap::new(),
        };
        let ids: Vec<String> = loaded.documents.keys().cloned().collect();
        for id in ids {
            let document = loaded.documents[&id].clone();
            loaded.check(&document, &id)?;
        }
        Ok(loaded)
    }

    fn check(&mut self, schema: &Value, current: &str) -> Result<(), InvalidSchema> {
        if matches!(schema, Value::Bool(_)) {
            return Ok(());
        }
        let members = object(schema).ok_or(InvalidSchema)?;
        if members
            .iter()
            .any(|(key, _)| !SUPPORTED_KEYWORDS.contains(&key.as_str()))
        {
            return Err(InvalidSchema);
        }
        if let Some(Value::String(reference)) = schema.get("$ref") {
            self.resolve(reference, current).ok_or(InvalidSchema)?;
        }
        if let Some(Value::String(pattern)) = schema.get("pattern") {
            let compiled = Regex::new(pattern).map_err(|_| InvalidSchema)?;
            self.patterns.insert(pattern.clone(), compiled);
        }
        for keyword in SCHEMA_LISTS {
            let list = present(members, keyword).map(|list| array(list).ok_or(InvalidSchema));
            for child in list.transpose()?.unwrap_or_default() {
                self.check(child, current)?;
            }
        }
        for keyword in SCHEMA_MAPS {
            let map = present(members, keyword).map(|map| object(map).ok_or(InvalidSchema));
            for (_, child) in map.transpose()?.unwrap_or_default() {
                self.check(child, current)?;
            }
        }
        for keyword in SCHEMA_CHILDREN {
            if let Some(child) = present(members, keyword) {
                self.check(child, current)?;
            }
        }
        Ok(())
    }

    /// The node `reference` names from the document `current`, with that node's document id.
    fn resolve(&self, reference: &str, current: &str) -> Option<(&Value, String)> {
        let (location, fragment) = reference.split_once('#').unwrap_or((reference, ""));
        let document_id = if location.is_empty() {
            current.to_owned()
        } else if location.contains("://") {
            location.to_owned()
        } else {
            let base = &current[..=current.rfind('/')?];
            format!("{base}{location}")
        };
        let mut target = self.documents.get(&document_id)?;
        if !fragment.is_empty() {
            for encoded in fragment.strip_prefix('/')?.split('/') {
                target = target.get(&encoded.replace("~1", "/").replace("~0", "~"))?;
            }
        }
        Some((target, document_id))
    }

    /// Whether `value` satisfies the node `schema` of the document `current`.
    #[must_use]
    pub fn valid(&self, schema: &Value, value: &Value, current: &str) -> bool {
        let Some(members) = object(schema) else {
            return matches!(schema, Value::Bool(true));
        };
        if let Some(Value::String(reference)) = schema.get("$ref") {
            let Some((target, id)) = self.resolve(reference, current) else {
                return false;
            };
            if !self.valid(target, value, &id) {
                return false;
            }
        }
        self.combinators_hold(members, value, current)
            && literals_hold(members, value)
            && type_holds(present(members, "type"), value)
            && match value {
                Value::Object(items) => self.object_holds(members, items, current),
                Value::Array(items) => self.array_holds(members, items, current),
                Value::String(text) => self.string_holds(members, text),
                Value::Number(number) => integer_holds(members, number),
                _ => true,
            }
    }

    fn combinators_hold(&self, schema: &Members, value: &Value, current: &str) -> bool {
        let all = find(schema, "allOf")
            .and_then(array)
            .unwrap_or_default()
            .iter()
            .all(|candidate| self.valid(candidate, value, current));
        let one = find(schema, "oneOf")
            .and_then(array)
            .is_none_or(|candidates| {
                candidates
                    .iter()
                    .filter(|candidate| self.valid(candidate, value, current))
                    .count()
                    == 1
            });
        let not =
            present(schema, "not").is_none_or(|rejected| !self.valid(rejected, value, current));
        all && one && not
    }

    fn object_holds(&self, schema: &Members, value: &Members, current: &str) -> bool {
        let required = find(schema, "required").and_then(array).unwrap_or_default();
        if required
            .iter()
            .any(|key| !string(key).is_some_and(|key| value.iter().any(|(name, _)| name == key)))
        {
            return false;
        }
        let Some(properties) = find(schema, "properties").map_or(Some(&[][..]), object) else {
            return false;
        };
        let additional = find(schema, "additionalProperties");
        value.iter().all(
            |(key, item)| match properties.iter().find(|(name, _)| name == key) {
                Some((_, child)) => self.valid(child, item, current),
                None => match additional {
                    Some(Value::Bool(false)) => false,
                    Some(child @ Value::Object(_)) => self.valid(child, item, current),
                    _ => true,
                },
            },
        )
    }

    fn array_holds(&self, schema: &Members, items: &[Value], current: &str) -> bool {
        let count = items.len();
        let bound = |keyword| find(schema, keyword).and_then(small_int);
        if bound("minItems").is_some_and(|minimum| count < minimum)
            || bound("maxItems").is_some_and(|maximum| count > maximum)
        {
            return false;
        }
        if find(schema, "uniqueItems") == Some(&Value::Bool(true)) {
            let texts: Vec<String> = items.iter().map(canonical).collect();
            if texts
                .iter()
                .enumerate()
                .any(|(index, text)| texts[..index].contains(text))
            {
                return false;
            }
        }
        if let Some(contained) = present(schema, "contains") {
            let matches = items
                .iter()
                .filter(|item| self.valid(contained, item, current))
                .count();
            let minimum = find(schema, "minContains").map_or(Some(1), small_int);
            if minimum.is_some_and(|minimum| matches < minimum)
                || bound("maxContains").is_some_and(|maximum| matches > maximum)
            {
                return false;
            }
        }
        let prefix = find(schema, "prefixItems")
            .and_then(array)
            .unwrap_or_default();
        let empty = Value::Object(Vec::new());
        let rest = find(schema, "items").unwrap_or(&empty);
        items
            .iter()
            .enumerate()
            .all(|(index, item)| self.valid(prefix.get(index).unwrap_or(rest), item, current))
    }

    fn string_holds(&self, schema: &Members, text: &str) -> bool {
        let length = text.chars().count();
        let bound = |keyword| find(schema, keyword).and_then(small_int);
        !(bound("minLength").is_some_and(|minimum| length < minimum)
            || bound("maxLength").is_some_and(|maximum| length > maximum))
            && find(schema, "pattern")
                .and_then(string)
                .is_none_or(|pattern| {
                    self.patterns
                        .get(pattern)
                        .is_some_and(|compiled| compiled.is_match(text).unwrap_or(false))
                })
    }
}

fn find<'a>(schema: &'a Members, key: &str) -> Option<&'a Value> {
    schema
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

/// A keyword whose JSON `null` value the reference reads as absent (`schema.get(key) is not None`).
fn present<'a>(schema: &'a Members, key: &str) -> Option<&'a Value> {
    find(schema, key).filter(|value| **value != Value::Null)
}

fn small_int(value: &Value) -> Option<usize> {
    match value {
        Value::Number(Number::Int(integer)) => usize::try_from(*integer).ok(),
        _ => None,
    }
}

fn literals_hold(schema: &Members, value: &Value) -> bool {
    let text = canonical(value);
    find(schema, "const").is_none_or(|constant| canonical(constant) == text)
        && find(schema, "enum")
            .and_then(array)
            .is_none_or(|choices| choices.iter().any(|choice| canonical(choice) == text))
}

fn type_holds(expected: Option<&Value>, value: &Value) -> bool {
    let Some(expected) = expected else {
        return true;
    };
    match string(expected) {
        Some("array") => matches!(value, Value::Array(_)),
        Some("integer") => matches!(value, Value::Number(Number::Int(_) | Number::BigInt(_))),
        Some("object") => matches!(value, Value::Object(_)),
        Some("string") => matches!(value, Value::String(_)),
        _ => false,
    }
}

/// `minimum` and `maximum` of an integer value; a float is never checked against them, as the reference skips it.
fn integer_holds(schema: &Members, number: &Number) -> bool {
    let bound = |keyword| match find(schema, keyword) {
        Some(Value::Number(Number::Int(limit))) => Some(*limit),
        _ => None,
    };
    match number {
        Number::Int(integer) => {
            !(bound("minimum").is_some_and(|minimum| *integer < minimum)
                || bound("maximum").is_some_and(|maximum| *integer > maximum))
        }
        Number::BigInt(digits) => {
            let negative = digits.starts_with('-');
            !(negative && bound("minimum").is_some() || !negative && bound("maximum").is_some())
        }
        Number::Float(_) => true,
    }
}
