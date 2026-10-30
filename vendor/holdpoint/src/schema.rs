//! A strict JSON Schema subset for the arguments of runtime-owned tools.
//!
//! Accepted keywords: `type` (`object`, `array`, `string`, `integer`, `number`, `boolean`, `null`), `properties`,
//! `required`, `additionalProperties` (mandatory and `false` on every object), `items`, `minItems`, `maxItems`,
//! `minLength`, `maxLength`, `pattern` (linear-time `regex` syntax, at most 256 bytes), `minimum`, `maximum`,
//! `enum`, `const`, `anyOf` (of non-container schemas), and `description`. Any other keyword is refused when the
//! schema is compiled, so a schema never means more than Holdpoint checks.

use std::collections::BTreeMap;

use regex::Regex;
use std::cmp::Ordering;

use serde_json::{Map, Number, Value};

use crate::error::Error;

const MAX_PATTERN_BYTES: usize = 256;
const MAX_SCHEMA_DEPTH: usize = 16;

/// A compiled schema.
#[derive(Debug, Clone)]
pub struct Schema {
    root: Node,
}

#[derive(Debug, Clone)]
enum Node {
    Object { properties: BTreeMap<String, Node>, required: Vec<String> },
    Array { items: Box<Node>, min: Option<u64>, max: Option<u64> },
    String { min: Option<u64>, max: Option<u64>, pattern: Option<Regex> },
    Integer { min: Option<Number>, max: Option<Number> },
    Number { min: Option<Number>, max: Option<Number> },
    Boolean,
    Null,
    Enum(Vec<Value>),
    AnyOf(Vec<Node>),
}

impl Schema {
    /// Compiles `schema`, refusing anything outside the subset.
    ///
    /// # Errors
    ///
    /// [`Error::Contract`] naming the offending keyword and its location.
    pub fn compile(schema: &Value) -> Result<Self, Error> {
        Ok(Self { root: compile(schema, "$", 0)? })
    }

    /// Checks `value` against the schema.
    ///
    /// # Errors
    ///
    /// [`Error::Contract`] naming the first location that does not match.
    pub fn validate(&self, value: &Value) -> Result<(), Error> {
        check(&self.root, value, "$")
    }
}

fn refuse(at: &str, what: &str) -> Error {
    Error::Contract(format!("schema at {at}: {what}"))
}

fn compile(schema: &Value, at: &str, depth: usize) -> Result<Node, Error> {
    if depth > MAX_SCHEMA_DEPTH {
        return Err(refuse(at, "nested too deep"));
    }
    let map = schema.as_object().ok_or_else(|| refuse(at, "must be an object"))?;
    if let Some(constant) = map.get("const") {
        only(map, at, &["const", "description"])?;
        return Ok(Node::Enum(vec![constant.clone()]));
    }
    if let Some(options) = map.get("enum") {
        only(map, at, &["enum", "type", "description"])?;
        let options = options.as_array().filter(|options| !options.is_empty());
        let options = options.ok_or_else(|| refuse(at, "enum must be a non-empty array"))?.clone();
        if let Some(kind) = map.get("type") {
            let typed = compile(&serde_json::json!({ "type": kind }), at, depth + 1)?;
            if options.iter().any(|option| check(&typed, option, at).is_err()) {
                return Err(refuse(at, "an enum option does not match its type"));
            }
        }
        return Ok(Node::Enum(options));
    }
    if let Some(options) = map.get("anyOf") {
        only(map, at, &["anyOf", "description"])?;
        return compile_any_of(options, at, depth);
    }
    match map.get("type").and_then(Value::as_str) {
        Some("object") => compile_object(map, at, depth),
        Some("array") => compile_array(map, at, depth),
        Some("string") => compile_string(map, at),
        Some("integer") => compile_bounds(map, at).map(|(min, max)| Node::Integer { min, max }),
        Some("number") => compile_bounds(map, at).map(|(min, max)| Node::Number { min, max }),
        Some("boolean") => only(map, at, &["type", "description"]).map(|()| Node::Boolean),
        Some("null") => only(map, at, &["type", "description"]).map(|()| Node::Null),
        _ => Err(refuse(at, "needs a supported type, const, enum, or anyOf")),
    }
}

fn only(map: &Map<String, Value>, at: &str, allowed: &[&str]) -> Result<(), Error> {
    match map.keys().find(|key| !allowed.contains(&key.as_str())) {
        Some(key) => Err(refuse(at, &format!("unsupported keyword {key}"))),
        None => Ok(()),
    }
}

fn compile_any_of(options: &Value, at: &str, depth: usize) -> Result<Node, Error> {
    let options = options.as_array().filter(|options| !options.is_empty());
    let options = options.ok_or_else(|| refuse(at, "anyOf must be a non-empty array"))?;
    let mut nodes = Vec::with_capacity(options.len());
    for (index, option) in options.iter().enumerate() {
        let node = compile(option, &format!("{at}.anyOf[{index}]"), depth + 1)?;
        if matches!(node, Node::Object { .. } | Node::Array { .. } | Node::AnyOf(_)) {
            return Err(refuse(at, "anyOf members must be scalar schemas"));
        }
        nodes.push(node);
    }
    Ok(Node::AnyOf(nodes))
}

fn compile_object(map: &Map<String, Value>, at: &str, depth: usize) -> Result<Node, Error> {
    only(map, at, &["type", "properties", "required", "additionalProperties", "description"])?;
    if map.get("additionalProperties") != Some(&Value::Bool(false)) {
        return Err(refuse(at, "additionalProperties must be false"));
    }
    let mut properties = BTreeMap::new();
    if let Some(declared) = map.get("properties") {
        let declared = declared.as_object().ok_or_else(|| refuse(at, "properties must be an object"))?;
        for (name, schema) in declared {
            properties.insert(name.clone(), compile(schema, &format!("{at}.{name}"), depth + 1)?);
        }
    }
    let mut required = Vec::new();
    if let Some(names) = map.get("required") {
        let names = names.as_array().ok_or_else(|| refuse(at, "required must be an array"))?;
        for name in names {
            let name = name.as_str().ok_or_else(|| refuse(at, "required names must be strings"))?;
            if !properties.contains_key(name) || required.iter().any(|known: &String| known == name) {
                return Err(refuse(at, &format!("required {name} is undeclared or repeated")));
            }
            required.push(name.to_owned());
        }
    }
    Ok(Node::Object { properties, required })
}

fn compile_array(map: &Map<String, Value>, at: &str, depth: usize) -> Result<Node, Error> {
    only(map, at, &["type", "items", "minItems", "maxItems", "description"])?;
    let items = map.get("items").ok_or_else(|| refuse(at, "an array needs items"))?;
    let items = Box::new(compile(items, &format!("{at}[]"), depth + 1)?);
    let (min, max) = (count(map, "minItems", at)?, count(map, "maxItems", at)?);
    ordered(min, max, at)?;
    Ok(Node::Array { items, min, max })
}

fn compile_string(map: &Map<String, Value>, at: &str) -> Result<Node, Error> {
    only(map, at, &["type", "minLength", "maxLength", "pattern", "description"])?;
    let (min, max) = (count(map, "minLength", at)?, count(map, "maxLength", at)?);
    ordered(min, max, at)?;
    let pattern = match map.get("pattern") {
        None => None,
        Some(Value::String(pattern)) if pattern.len() <= MAX_PATTERN_BYTES => Some(
            regex::RegexBuilder::new(pattern)
                .size_limit(1 << 20)
                .build()
                .map_err(|_| refuse(at, "pattern does not compile"))?,
        ),
        Some(_) => return Err(refuse(at, "pattern must be a string of at most 256 bytes")),
    };
    Ok(Node::String { min, max, pattern })
}

type Bounds = (Option<Number>, Option<Number>);

fn compile_bounds(map: &Map<String, Value>, at: &str) -> Result<Bounds, Error> {
    only(map, at, &["type", "minimum", "maximum", "description"])?;
    let bound = |name: &str| match map.get(name) {
        None => Ok(None),
        Some(Value::Number(number)) => Ok(Some(number.clone())),
        Some(_) => Err(refuse(at, &format!("{name} must be a number"))),
    };
    let (min, max) = (bound("minimum")?, bound("maximum")?);
    if let (Some(min), Some(max)) = (&min, &max)
        && compare(min, max) == Ordering::Greater
    {
        return Err(refuse(at, "minimum is above maximum"));
    }
    Ok((min, max))
}

/// Compares two JSON numbers exactly: integers as integers, an integer against a double without rounding the
/// integer, and two doubles as doubles (JSON numbers are always finite).
fn compare(left: &Number, right: &Number) -> Ordering {
    let exact = |number: &Number| number.as_i64().map(i128::from).or_else(|| number.as_u64().map(i128::from));
    match (exact(left), exact(right)) {
        (Some(left), Some(right)) => left.cmp(&right),
        (Some(left), None) => against_double(left, right.as_f64().unwrap_or(0.0)),
        (None, Some(right)) => against_double(right, left.as_f64().unwrap_or(0.0)).reverse(),
        (None, None) => left.as_f64().partial_cmp(&right.as_f64()).unwrap_or(Ordering::Equal),
    }
}

/// `integer` compared with `double`, exactly.
fn against_double(integer: i128, double: f64) -> Ordering {
    // Every i64/u64 lies strictly inside ±2^127; doubles beyond that bound compare by sign alone.
    const BOUND: f64 = 1.701_411_834_604_692_3e38;
    if double >= BOUND {
        return Ordering::Less;
    }
    if double <= -BOUND {
        return Ordering::Greater;
    }
    let whole = double.trunc();
    #[expect(clippy::cast_possible_truncation, reason = "an integral double strictly inside ±2^127 converts exactly")]
    let whole_integer = whole as i128;
    integer.cmp(&whole_integer).then_with(|| 0.0_f64.partial_cmp(&(double - whole)).unwrap_or(Ordering::Equal))
}

fn count(map: &Map<String, Value>, name: &str, at: &str) -> Result<Option<u64>, Error> {
    match map.get(name) {
        None => Ok(None),
        Some(value) => value.as_u64().map(Some).ok_or_else(|| refuse(at, &format!("{name} must be a count"))),
    }
}

fn ordered(min: Option<u64>, max: Option<u64>, at: &str) -> Result<(), Error> {
    match (min, max) {
        (Some(min), Some(max)) if min > max => Err(refuse(at, "minimum count is above maximum")),
        _ => Ok(()),
    }
}

fn mismatch(at: &str, what: &str) -> Error {
    Error::Contract(format!("argument at {at}: {what}"))
}

fn check(node: &Node, value: &Value, at: &str) -> Result<(), Error> {
    match node {
        Node::Object { properties, required } => check_object(properties, required, value, at),
        Node::Array { items, min, max } => {
            let values = value.as_array().ok_or_else(|| mismatch(at, "expected an array"))?;
            within(values.len(), *min, *max).then_some(()).ok_or_else(|| mismatch(at, "wrong number of items"))?;
            values.iter().enumerate().try_for_each(|(index, item)| check(items, item, &format!("{at}[{index}]")))
        }
        Node::String { min, max, pattern } => {
            let text = value.as_str().ok_or_else(|| mismatch(at, "expected a string"))?;
            within(text.chars().count(), *min, *max).then_some(()).ok_or_else(|| mismatch(at, "wrong length"))?;
            match pattern {
                Some(pattern) if !pattern.is_match(text) => Err(mismatch(at, "does not match the pattern")),
                _ => Ok(()),
            }
        }
        Node::Integer { min, max } => match value.as_number() {
            Some(number) if number.is_i64() || number.is_u64() => bounded(number, min.as_ref(), max.as_ref(), at),
            _ => Err(mismatch(at, "expected an integer")),
        },
        Node::Number { min, max } => match value.as_number() {
            Some(number) => bounded(number, min.as_ref(), max.as_ref(), at),
            None => Err(mismatch(at, "expected a number")),
        },
        Node::Boolean => value.is_boolean().then_some(()).ok_or_else(|| mismatch(at, "expected a boolean")),
        Node::Null => value.is_null().then_some(()).ok_or_else(|| mismatch(at, "expected null")),
        Node::Enum(options) => {
            options.contains(value).then_some(()).ok_or_else(|| mismatch(at, "not an allowed value"))
        }
        Node::AnyOf(options) => {
            let matched = options.iter().any(|option| check(option, value, at).is_ok());
            matched.then_some(()).ok_or_else(|| mismatch(at, "matches no allowed schema"))
        }
    }
}

fn check_object(
    properties: &BTreeMap<String, Node>,
    required: &[String],
    value: &Value,
    at: &str,
) -> Result<(), Error> {
    let map = value.as_object().ok_or_else(|| mismatch(at, "expected an object"))?;
    if let Some(name) = required.iter().find(|name| !map.contains_key(*name)) {
        return Err(mismatch(at, &format!("missing {name}")));
    }
    for (name, item) in map {
        let node = properties.get(name).ok_or_else(|| mismatch(at, &format!("unexpected {name}")))?;
        check(node, item, &format!("{at}.{name}"))?;
    }
    Ok(())
}

fn within(length: usize, min: Option<u64>, max: Option<u64>) -> bool {
    let length = u64::try_from(length).unwrap_or(u64::MAX);
    min.is_none_or(|min| length >= min) && max.is_none_or(|max| length <= max)
}

fn bounded(number: &Number, min: Option<&Number>, max: Option<&Number>, at: &str) -> Result<(), Error> {
    let below = min.is_some_and(|min| compare(number, min) == Ordering::Less);
    let above = max.is_some_and(|max| compare(number, max) == Ordering::Greater);
    if below || above { Err(mismatch(at, "out of range")) } else { Ok(()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn search() -> Schema {
        Schema::compile(&json!({
            "type": "object",
            "description": "search",
            "additionalProperties": false,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 20, "pattern": "^[a-z ]+$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
                "mode": {"enum": ["fast", "slow"]},
                "kind": {"const": "web"},
                "strict": {"type": "boolean"},
                "none": {"type": "null"},
                "either": {"anyOf": [{"type": "string"}, {"type": "null"}]}
            }
        }))
        .unwrap()
    }

    /// The refusal's message; a success yields a message no assertion expects.
    fn contract(result: Result<(), Error>) -> String {
        result.map_or_else(|error| error.to_string(), |()| "accepted".to_owned())
    }

    #[test]
    fn accepts_a_matching_value() {
        let value = json!({"query": "jobs in curitiba", "limit": 3, "ratio": 0.5, "tags": ["a"], "mode": "fast",
            "kind": "web", "strict": true, "none": null, "either": null});
        assert_eq!(search().validate(&value), Ok(()));
        assert_eq!(search().validate(&json!({"query": "x", "either": "y"})), Ok(()));
    }

    #[test]
    fn refuses_every_kind_of_mismatch() {
        let schema = search();
        for (value, expected) in [
            (json!([]), "expected an object"),
            (json!({}), "missing query"),
            (json!({"query": "x", "other": 1}), "unexpected other"),
            (json!({"query": 1}), "expected a string"),
            (json!({"query": ""}), "wrong length"),
            (json!({"query": "UPPER"}), "does not match"),
            (json!({"query": "x", "limit": 1.5}), "expected an integer"),
            (json!({"query": "x", "limit": 11}), "out of range"),
            (json!({"query": "x", "limit": 0}), "out of range"),
            (json!({"query": "x", "ratio": "a"}), "expected a number"),
            (json!({"query": "x", "tags": "a"}), "expected an array"),
            (json!({"query": "x", "tags": []}), "wrong number of items"),
            (json!({"query": "x", "tags": [1]}), "$.tags[0]: expected a string"),
            (json!({"query": "x", "mode": "medium"}), "not an allowed value"),
            (json!({"query": "x", "kind": "local"}), "not an allowed value"),
            (json!({"query": "x", "strict": 1}), "expected a boolean"),
            (json!({"query": "x", "none": 0}), "expected null"),
            (json!({"query": "x", "either": 1}), "matches no allowed schema"),
        ] {
            let message = contract(schema.validate(&value));
            assert!(message.contains(expected), "{value}: {message}");
        }
    }

    #[test]
    fn refuses_schemas_outside_the_subset() {
        for (schema, expected) in [
            (json!(true), "must be an object"),
            (json!({"type": "object"}), "additionalProperties must be false"),
            (json!({"type": "object", "additionalProperties": true}), "additionalProperties must be false"),
            (json!({"type": "object", "additionalProperties": false, "patternProperties": {}}), "unsupported keyword"),
            (json!({"type": "object", "additionalProperties": false, "properties": []}), "properties must be"),
            (json!({"type": "object", "additionalProperties": false, "required": "a"}), "required must be"),
            (json!({"type": "object", "additionalProperties": false, "required": [1]}), "must be strings"),
            (json!({"type": "object", "additionalProperties": false, "required": ["a"]}), "undeclared"),
            (
                json!({"type": "object", "additionalProperties": false, "properties": {"a": {"type": "null"}},
                    "required": ["a", "a"]}),
                "repeated",
            ),
            (json!({"type": "array"}), "needs items"),
            (json!({"type": "array", "items": {"type": "null"}, "minItems": 2, "maxItems": 1}), "above maximum"),
            (json!({"type": "array", "items": {"type": "null"}, "minItems": -1}), "must be a count"),
            (json!({"type": "string", "minLength": 3, "maxLength": 2}), "above maximum"),
            (json!({"type": "string", "pattern": "("}), "does not compile"),
            (json!({"type": "string", "pattern": "a".repeat(257)}), "at most 256 bytes"),
            (json!({"type": "string", "format": "email"}), "unsupported keyword"),
            (json!({"type": "integer", "minimum": "1"}), "must be a number"),
            (json!({"type": "number", "minimum": 2, "maximum": 1}), "minimum is above maximum"),
            (json!({"type": "boolean", "default": true}), "unsupported keyword"),
            (json!({"type": "null", "nullable": true}), "unsupported keyword"),
            (json!({"type": "tuple"}), "needs a supported type"),
            (json!({}), "needs a supported type"),
            (json!({"const": 1, "type": "integer"}), "unsupported keyword"),
            (json!({"enum": []}), "non-empty array"),
            (json!({"enum": [1], "minimum": 0}), "unsupported keyword"),
            (json!({"anyOf": []}), "non-empty array"),
            (json!({"anyOf": [{"type": "null"}], "type": "null"}), "unsupported keyword"),
            (json!({"anyOf": [{"type": "array", "items": {"type": "null"}}]}), "scalar schemas"),
            (json!({"anyOf": [{"type": "bogus"}]}), "needs a supported type"),
        ] {
            let message = contract(Schema::compile(&schema).map(|_| ()));
            assert!(message.contains(expected), "{schema}: {message}");
        }
    }

    #[test]
    fn refuses_schemas_nested_too_deep() {
        let mut schema = json!({"type": "null"});
        for _ in 0..=MAX_SCHEMA_DEPTH {
            schema = json!({"type": "array", "items": schema});
        }
        assert!(contract(Schema::compile(&schema).map(|_| ())).contains("nested too deep"));
    }

    #[test]
    fn integer_bounds_compare_exactly_beyond_double_precision() {
        let schema = Schema::compile(&json!({"type": "integer", "maximum": 9_007_199_254_740_992_u64})).unwrap();
        assert_eq!(schema.validate(&json!(9_007_199_254_740_992_u64)), Ok(()));
        assert!(schema.validate(&json!(9_007_199_254_740_993_u64)).is_err());
        let fractional = Schema::compile(&json!({"type": "number", "minimum": 0.5})).unwrap();
        assert!(fractional.validate(&json!(0)).is_err());
        assert_eq!(fractional.validate(&json!(1)), Ok(()));
    }

    #[test]
    fn integers_compare_exactly_against_double_bounds() {
        let schema = Schema::compile(&json!({"type": "integer", "maximum": 9_007_199_254_740_992.0})).unwrap();
        assert_eq!(schema.validate(&json!(9_007_199_254_740_992_u64)), Ok(()));
        assert!(schema.validate(&json!(9_007_199_254_740_993_u64)).is_err());
        let fractional = Schema::compile(&json!({"type": "integer", "minimum": -2.5, "maximum": 1e300})).unwrap();
        assert!(schema_ok(&fractional, -2));
        assert!(!schema_ok(&fractional, -3));
        assert!(schema_ok(&fractional, i64::MAX));
        let tiny = Schema::compile(&json!({"type": "number", "minimum": -1e300, "maximum": 0.5})).unwrap();
        assert!(schema_ok(&tiny, i64::MIN));
        assert!(tiny.validate(&json!(0.25)).is_ok() && tiny.validate(&json!(0.75)).is_err());
        assert!(Schema::compile(&json!({"type": "number", "minimum": 1.5, "maximum": 1})).is_err());
        let zero = Schema::compile(&json!({"type": "number", "minimum": 0.0, "maximum": -0.0})).unwrap();
        assert!(
            zero.validate(&json!(-0.0)).is_ok()
                && zero.validate(&json!(0.0)).is_ok()
                && zero.validate(&json!(0)).is_ok()
        );
    }

    fn schema_ok(schema: &Schema, value: i64) -> bool {
        schema.validate(&json!(value)).is_ok()
    }

    #[test]
    fn an_enum_must_match_its_declared_type() {
        assert!(
            contract(Schema::compile(&json!({"type": "integer", "enum": ["wrong"]})).map(|_| ()))
                .contains("does not match")
        );
        let schema = Schema::compile(&json!({"type": "integer", "enum": [1, 2]})).unwrap();
        assert_eq!(schema.validate(&json!(2)), Ok(()));
        assert!(schema.validate(&json!("2")).is_err());
    }

    #[test]
    fn an_object_without_required_names_accepts_any_subset() {
        let schema = Schema::compile(
            &json!({"type": "object", "additionalProperties": false, "properties": {"a": {"type": "null"}}}),
        )
        .unwrap();
        assert_eq!(schema.validate(&json!({})), Ok(()));
        assert_eq!(schema.validate(&json!({"a": null})), Ok(()));
    }

    #[test]
    fn nesting_counts_through_enums_and_any_of() {
        let wrap = |mut schema: Value, levels: usize| {
            for _ in 0..levels {
                schema = json!({"type": "array", "items": schema});
            }
            schema
        };
        let any_of = json!({"anyOf": [{"type": "null"}]});
        assert!(Schema::compile(&wrap(any_of.clone(), MAX_SCHEMA_DEPTH - 1)).is_ok());
        assert!(Schema::compile(&wrap(any_of, MAX_SCHEMA_DEPTH)).is_err());
        let typed_enum = json!({"type": "integer", "enum": [1]});
        assert!(Schema::compile(&wrap(typed_enum.clone(), MAX_SCHEMA_DEPTH - 1)).is_ok());
        assert!(Schema::compile(&wrap(typed_enum, MAX_SCHEMA_DEPTH)).is_err());
    }

    #[test]
    fn integer_bounds_accept_unsigned_and_signed_values() {
        let schema = Schema::compile(&json!({"type": "integer"})).unwrap();
        assert_eq!(schema.validate(&json!(u64::MAX)), Ok(()));
        assert_eq!(schema.validate(&json!(i64::MIN)), Ok(()));
        assert!(schema.validate(&json!("1")).is_err());
    }
}
