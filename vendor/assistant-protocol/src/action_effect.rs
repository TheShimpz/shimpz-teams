//! Action effect classes, verifiers, and provider idempotency (`validators/action_effect.py`, Assistant Spec v1).
//!
//! Every Action declares `effect`. A `mutating` Action may declare how its provider honors the logical
//! `operation_id` as an idempotency key, and may name one `read_only` Action of the same contract as its verifier,
//! with fixed typed input bindings correlated to the exact operation and the output positions of its outcome and
//! recovered result. A binding never names a declared file input property (ADR-0093).

use strict_json::Value;

use crate::failure::dns_labels;
use crate::python::{
    array, char_len, contains, has_exactly, int_within, member, object, py_eq, same, string,
};

/// The effect classes.
pub const EFFECTS: [&str; 2] = ["read_only", "mutating"];
/// The outcomes a verifier reports.
pub const OUTCOMES: [&str; 3] = ["occurred", "not_occurred", "inconclusive"];
/// The most bindings of one verifier.
pub const MAX_BINDINGS: usize = 16;
/// The longest binding name.
pub const MAX_BINDING_NAME: usize = 128;
/// The longest pointer.
pub const MAX_POINTER: usize = 256;
/// Where a provider reads the idempotency key.
pub const KEY_LOCATIONS: [&str; 3] = ["header", "query", "body"];
/// The scope a provider keeps keys in.
pub const KEY_SCOPES: [&str; 2] = ["account", "endpoint"];
/// The longest provider host.
pub const MAX_PROVIDER_HOST: usize = 253;
/// The shortest provider retention.
pub const MIN_RETENTION_SECONDS: i64 = 60;
/// The longest provider retention.
pub const MAX_RETENTION_SECONDS: i64 = 31_536_000;

type Members = [(String, Value)];

fn operation_id_binding() -> Value {
    Value::Object(vec![(
        "from".to_owned(),
        Value::String("operation_id".to_owned()),
    )])
}

fn operation_id_schema() -> Value {
    Value::Object(vec![(
        "type".to_owned(),
        Value::String("string".to_owned()),
    )])
}

/// The stable reason the Actions' effect or verifier declarations are refused, or `None`.
#[must_use]
pub fn effect_error(actions: &Value) -> Option<&'static str> {
    let Some(actions) = array(actions) else {
        return Some("actions_invalid");
    };
    let objects: Vec<&Members> = actions.iter().filter_map(object).collect();
    if objects.len() != actions.len() {
        return Some("actions_invalid");
    }
    objects
        .iter()
        .find_map(|action| action_error(action, &objects))
}

/// The tokens of one non-root RFC 6901 pointer whose reference tokens are all non-empty.
#[must_use]
pub fn pointer_tokens(pointer: &Value) -> Option<Vec<String>> {
    let text = string(pointer).filter(|text| char_len(text) <= MAX_POINTER)?;
    let rest = text.strip_prefix('/')?;
    let tokens: Vec<&str> = rest.split('/').collect();
    let valid = tokens
        .iter()
        .all(|token| !token.is_empty() && escapes_valid(token));
    valid.then(|| {
        tokens
            .iter()
            .map(|token| token.replace("~1", "/").replace("~0", "~"))
            .collect()
    })
}

fn escapes_valid(token: &str) -> bool {
    let mut characters = token.chars();
    while let Some(character) = characters.next() {
        if character == '~' && !matches!(characters.next(), Some('0' | '1')) {
            return false;
        }
    }
    true
}

/// The last action whose `id` is the string `identifier`, as a Python dict keyed by id finds it.
fn action_by_id<'a>(actions: &[&'a Members], identifier: &str) -> Option<&'a Members> {
    actions
        .iter()
        .rev()
        .find(|action| member(action, "id").and_then(string) == Some(identifier))
        .copied()
}

fn action_error(action: &Members, actions: &[&Members]) -> Option<&'static str> {
    let effect = member(action, "effect").and_then(string);
    let Some(effect) = effect.filter(|effect| EFFECTS.contains(effect)) else {
        return Some("effect_invalid");
    };
    if let Some(declaration) = member(action, "idempotency") {
        let error = if effect == "mutating" {
            idempotency_error(declaration)
        } else {
            Some("idempotency_on_read_only")
        };
        if error.is_some() {
            return error;
        }
    }
    let verifier = member(action, "verifier")?;
    if effect != "mutating" {
        return Some("verifier_on_read_only");
    }
    verifier_error(verifier, action, actions)
}

fn verifier_error(
    verifier: &Value,
    action: &Members,
    actions: &[&Members],
) -> Option<&'static str> {
    let Some(members) = object(verifier)
        .filter(|members| has_exactly(members, &["action", "input", "outcome", "result"]))
    else {
        return Some("verifier_invalid");
    };
    let Some(identifier) = member(members, "action").and_then(string) else {
        return Some("verifier_invalid");
    };
    let target = action_by_id(actions, identifier);
    if let Some(error) = target_error(target, action) {
        return Some(error);
    }
    let target = target.unwrap_or_default();
    let input = member(members, "input").unwrap_or(&Value::Null);
    bindings_error(input, action, member(target, "input_schema"))
        .or_else(|| outcome_error(member(members, "outcome"), member(target, "output_schema")))
        .or_else(|| {
            result_error(
                members,
                member(target, "output_schema"),
                member(action, "output_schema"),
            )
        })
}

fn target_error(target: Option<&Members>, action: &Members) -> Option<&'static str> {
    let Some(target) = target.filter(|target| !std::ptr::eq(*target, action)) else {
        return Some("verifier_unknown");
    };
    if member(target, "effect").and_then(string) != Some("read_only") {
        return Some("verifier_not_read_only");
    }
    (!non_interactive(target)).then_some("verifier_interactive")
}

/// A verifier runs without a person: no human request, or only a password request with a declared Stored Input.
#[must_use]
pub fn non_interactive(target: &Members) -> bool {
    let requests = member(target, "human_requests");
    let password_only = Value::Array(vec![Value::String("input:password".to_owned())]);
    requests.is_some_and(|requests| py_eq(requests, &Value::Array(Vec::new())))
        || (requests.is_some_and(|requests| py_eq(requests, &password_only))
            && member(target, "stored_inputs").is_some_and(truthy))
}

/// Python truthiness of a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(_) => !py_eq(value, &Value::Number(strict_json::Number::Int(0))),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(members) => !members.is_empty(),
    }
}

fn listed_text(value: Option<&Value>, choices: &[&str]) -> bool {
    value
        .and_then(string)
        .is_some_and(|text| choices.contains(&text))
}

/// `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.
fn key_name(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=128).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn idempotency_error(declaration: &Value) -> Option<&'static str> {
    let admitted = object(declaration).is_some_and(|members| {
        has_exactly(
            members,
            &[
                "provider",
                "key",
                "scope",
                "retention_seconds",
                "same_payload_required",
            ],
        ) && member(members, "provider")
            .and_then(string)
            .is_some_and(|host| host.len() <= MAX_PROVIDER_HOST && dns_labels(host, 2))
            && member(members, "key").and_then(object).is_some_and(|key| {
                has_exactly(key, &["location", "name"])
                    && listed_text(member(key, "location"), &KEY_LOCATIONS)
                    && member(key, "name").and_then(string).is_some_and(key_name)
            })
            && listed_text(member(members, "scope"), &KEY_SCOPES)
            && member(members, "retention_seconds").is_some_and(|seconds| {
                int_within(seconds, MIN_RETENTION_SECONDS, MAX_RETENTION_SECONDS)
            })
            && matches!(
                member(members, "same_payload_required"),
                Some(Value::Bool(_))
            )
    });
    (!admitted).then_some("idempotency_invalid")
}

fn bindings_error(
    bindings: &Value,
    action: &Members,
    destination: Option<&Value>,
) -> Option<&'static str> {
    let Some(bindings) = object(bindings).filter(|members| {
        (1..=MAX_BINDINGS).contains(&members.len())
            && members
                .iter()
                .all(|(name, _)| (1..=MAX_BINDING_NAME).contains(&char_len(name)))
    }) else {
        return Some("verifier_invalid");
    };
    let destination = destination.and_then(object);
    let properties = destination
        .and_then(|schema| member(schema, "properties"))
        .and_then(object);
    let required = destination.map(|schema| member(schema, "required"));
    let (Some(properties), Some(Some(Value::Array(required)))) =
        (properties, required.map(|value| value.or(Some(&EMPTY))))
    else {
        return Some("verifier_input_mismatch");
    };
    let bound = |name: &Value| string(name).is_some_and(|name| member(bindings, name).is_some());
    if !bindings
        .iter()
        .all(|(name, _)| member(properties, name).is_some())
        || !required.iter().all(bound)
    {
        return Some("verifier_input_mismatch");
    }
    let source = member(action, "input_schema");
    let files = member(action, "input_files").and_then(array);
    for (name, binding) in bindings {
        let destination = member(properties, name).unwrap_or(&Value::Null);
        if let Some(error) = binding_error(binding, source, destination) {
            return Some(error);
        }
        if files.is_some_and(|files| {
            !py_eq(binding, &operation_id_binding()) && names_file(binding, files)
        }) {
            return Some("verifier_binding_file");
        }
    }
    (!correlated(bindings, source)).then_some("verifier_uncorrelated")
}

static EMPTY: Value = Value::Array(Vec::new());

/// A file id reaches an Action only through that Action's own declared file input.
fn names_file(binding: &Value, files: &[Value]) -> bool {
    binding
        .get("pointer")
        .and_then(pointer_tokens)
        .and_then(|tokens| tokens.into_iter().next())
        .is_some_and(|first| contains(files, &Value::String(first)))
}

/// Binds the original `operation_id`, or every required top-level member of the original input whole.
fn correlated(bindings: &Members, source: Option<&Value>) -> bool {
    if bindings
        .iter()
        .any(|(_, binding)| py_eq(binding, &operation_id_binding()))
    {
        return true;
    }
    let whole: Vec<String> = bindings
        .iter()
        .filter_map(|(_, binding)| binding.get("pointer").and_then(string))
        .filter(|pointer| pointer.matches('/').count() == 1)
        .map(|pointer| pointer[1..].replace("~1", "/").replace("~0", "~"))
        .collect();
    let required = source.and_then(object).map_or(Some(Vec::new()), |schema| {
        required_set(member(schema, "required"))
    });
    required.is_some_and(|required| {
        !required.is_empty() && required.iter().all(|name| whole.contains(name))
    })
}

/// `set(required)` as names: a list's string items, a string's characters, or an object's keys.
fn required_set(value: Option<&Value>) -> Option<Vec<String>> {
    match value {
        None => Some(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| string(item).map(str::to_owned))
            .collect(),
        Some(Value::String(text)) => Some(text.chars().map(String::from).collect()),
        Some(Value::Object(members)) => Some(members.iter().map(|(key, _)| key.clone()).collect()),
        Some(_) => None,
    }
}

fn binding_error(
    binding: &Value,
    source: Option<&Value>,
    destination: &Value,
) -> Option<&'static str> {
    if py_eq(binding, &operation_id_binding()) {
        return (!same(destination, &operation_id_schema())).then_some("verifier_binding_type");
    }
    let shaped = object(binding).is_some_and(|members| {
        has_exactly(members, &["from", "pointer"])
            && member(members, "from").and_then(string) == Some("input")
    });
    if !shaped {
        return Some("verifier_invalid");
    }
    let Some(tokens) = binding.get("pointer").and_then(pointer_tokens) else {
        return Some("verifier_invalid");
    };
    match resolve(source, &tokens, true) {
        None => Some("verifier_binding_unresolved"),
        Some(resolved) => (!same(resolved, destination)).then_some("verifier_binding_type"),
    }
}

fn outcome_error(pointer: Option<&Value>, output: Option<&Value>) -> Option<&'static str> {
    let Some(tokens) = pointer.and_then(pointer_tokens) else {
        return Some("verifier_invalid");
    };
    let admitted = resolve(output, &tokens, true)
        .and_then(object)
        .is_some_and(|schema| {
            let values = member(schema, "enum").and_then(array).unwrap_or_default();
            let texts: Vec<&str> = values.iter().filter_map(string).collect();
            has_exactly(schema, &["type", "enum"])
                && member(schema, "type").and_then(string) == Some("string")
                && member(schema, "enum").and_then(array).is_some()
                && texts.len() == values.len()
                && texts.len() == OUTCOMES.len()
                && OUTCOMES.iter().all(|outcome| texts.contains(outcome))
        });
    (!admitted).then_some("verifier_outcome_invalid")
}

fn result_error(
    verifier: &Members,
    output: Option<&Value>,
    original: Option<&Value>,
) -> Option<&'static str> {
    let tokens = member(verifier, "result").and_then(pointer_tokens);
    let outcome = member(verifier, "outcome").and_then(pointer_tokens);
    let (Some(tokens), Some(outcome)) = (tokens, outcome) else {
        return Some("verifier_invalid");
    };
    let shorter = tokens.len().min(outcome.len());
    if tokens[..shorter] == outcome[..shorter] {
        return Some("verifier_result_invalid");
    }
    let original = original.unwrap_or(&Value::Null);
    let admitted = resolve(output, &tokens, false).is_some_and(|schema| same(schema, original));
    (!admitted).then_some("verifier_result_invalid")
}

/// Follows literal `properties` only, through required members; the last may be optional when allowed.
fn resolve<'a>(
    schema: Option<&'a Value>,
    tokens: &[String],
    final_required: bool,
) -> Option<&'a Value> {
    let mut current = schema?;
    for (index, token) in tokens.iter().enumerate() {
        let members = object(current)?;
        let properties = member(members, "properties").and_then(object)?;
        let required = match member(members, "required") {
            None => &[][..],
            Some(required) => array(required)?,
        };
        let next = member(properties, token)?;
        let last = index + 1 == tokens.len();
        if !contains(required, &Value::String(token.clone())) && (final_required || !last) {
            return None;
        }
        current = next;
    }
    Some(current)
}
