//! What a run's step record shows of the inputs one attempt was given (ADR-0092 amendment, 2026-10-05, scale).
//!
//! Each member is the escaped preview of a redacted copy of its value. A value copied from an earlier step is first
//! walked under that step's reviewed output schema along its pointer, so a secret position on the way withholds the
//! whole preview and one inside it is redacted; every member is then redacted under the Action's own input schema,
//! every credential-shaped string or key is redacted, and every string holding a value Team injected into the attempt
//! is redacted, all before any cut. A value selected through an array item is walked along the array's pointer, then
//! under every schema any of its items may have, then along the item's own pointer.

use std::collections::{BTreeMap, BTreeSet};

use strict_json::Value;
use team_protocol::routine::literal_preview;

use super::walk::{
    Held, MAX_SECRET_DEPTH, Sub, any_item, applicable, item_schemas, member_schemas,
    secret_position,
};
use super::{Key, Plan, PlanError, Step, index_token, pointer_tokens, selector};
use crate::credential::resembles_credential;
use crate::json::{as_str, at, object, text as string};

/// What a redacted value or key reads as.
pub const INPUT_REDACTED: &str = "[redacted]";

/// Each input member of one attempt, sorted, as `{member, value}` with a redacted preview or `null`.
#[must_use]
pub fn input_preview(
    plan: &Plan,
    step: &Step,
    resolved: &Value,
    selected: &BTreeMap<Key, Value>,
    protected: &[String],
) -> Value {
    let mut names: Vec<&str> = step.inputs.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    let previews = names
        .into_iter()
        .map(|member| {
            let preview = shown_input(plan, step, member, resolved, selected, protected)
                .ok()
                .flatten()
                .map_or(Value::Null, |value| Value::String(literal_preview(&value)));
            object(vec![("member", string(member)), ("value", preview)])
        })
        .collect();
    Value::Array(previews)
}

/// One member's redacted copy, `None` when it is withheld; an error withholds it too.
fn shown_input(
    plan: &Plan,
    step: &Step,
    member: &str,
    resolved: &Value,
    selected: &BTreeMap<Key, Value>,
    protected: &[String],
) -> Result<Option<Value>, PlanError> {
    let missing = PlanError::new("plan-reference-missing");
    let source = step.input(member).ok_or(missing)?;
    let mut value = resolved.get(member).ok_or(missing)?.clone();
    if as_str(at(source, "kind")) == Some("step_output") {
        let origin = plan
            .step(as_str(at(source, "step")).unwrap_or_default())
            .ok_or(missing)?;
        let root = &origin.output_schema;
        let mut path: Vec<Option<String>> = pointer_tokens(at(source, "pointer"))
            .ok_or(missing)?
            .into_iter()
            .map(Some)
            .collect();
        if source.get("where").is_some() {
            path.push(None);
            path.extend(
                pointer_tokens(at(source, "item"))
                    .ok_or(missing)?
                    .into_iter()
                    .map(Some),
            );
        }
        let Some(subschema) = pointed(root, &path)? else {
            return Ok(None);
        };
        let name = path
            .iter()
            .rev()
            .find_map(Option::as_deref)
            .unwrap_or_default();
        let copied = selected.get(&selector(source)).ok_or(missing)?;
        value = redacted(copied, root, &subschema, name, 0)?;
    }
    let root = &step.input_schema;
    let candidates = applicable(root, &Sub::Node(root), 0, Held::Value(resolved))?;
    let member_schema = member_schemas(&candidates, member)?;
    // Every schema is walked against the original keys first; injected values and keys are scrubbed only after.
    let walked = redacted(&value, root, &member_schema, member, 0)?;
    Ok(Some(scrubbed(&walked, protected)))
}

/// The subschemas a path reaches in a source output schema, or `None` when a position on its way may be secret.
///
/// The root and every position on the way count, each with every dependent schema it might apply. A `None` token is
/// any one item of an array: every schema any item may have applies there.
fn pointed<'a>(root: &'a Value, path: &[Option<String>]) -> Result<Option<Sub<'a>>, PlanError> {
    let mut subschema = Sub::Node(root);
    if secret_position(root, "", &applicable(root, &subschema, 0, Held::Unknown)?) {
        return Ok(None);
    }
    for token in path {
        let candidates = applicable(root, &subschema, 0, Held::Unknown)?;
        let (mut reached, token) = match token {
            None => (any_item(&candidates), ""),
            Some(token) => match member_schemas(&candidates, token)? {
                Sub::AllOf(nodes) => (nodes, token.as_str()),
                Sub::Node(node) => (vec![node], token.as_str()),
            },
        };
        if let Some(index) = index_token(token)
            && let Sub::AllOf(nodes) = item_schemas(&candidates, index)
        {
            reached.extend(nodes);
        }
        let reached = Sub::AllOf(reached);
        if secret_position(root, token, &applicable(root, &reached, 0, Held::Unknown)?) {
            return Ok(None);
        }
        subschema = reached;
    }
    Ok(Some(subschema))
}

/// The value with injected values and their keys, and credential-shaped keys, hidden; after every schema walk.
///
/// It works structurally, before any encoding, so no escaping can carry an injected value past it.
fn scrubbed(value: &Value, protected: &[String]) -> Value {
    let injected = |text: &str| {
        protected
            .iter()
            .any(|secret| !secret.is_empty() && text.contains(secret.as_str()))
    };
    match value {
        Value::String(text) if injected(text) => string(INPUT_REDACTED),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| scrubbed(item, protected)).collect())
        }
        Value::Object(found) => {
            let names = hidden(found, |key| resembles_credential(key) || injected(key));
            Value::Object(
                found
                    .iter()
                    .map(|(key, item)| (names[key].clone(), scrubbed(item, protected)))
                    .collect(),
            )
        }
        _ => value.clone(),
    }
}

/// Each key mapped to itself or, when hidden, to a redacted name no other key of the value holds.
fn hidden(found: &[(String, Value)], hide: impl Fn(&str) -> bool) -> BTreeMap<String, String> {
    let mut taken: BTreeSet<String> = found.iter().map(|(key, _)| key.clone()).collect();
    let mut names = BTreeMap::new();
    let mut count = 0;
    let mut keys: Vec<&str> = found.iter().map(|(key, _)| key.as_str()).collect();
    keys.sort_unstable();
    for key in keys {
        if !hide(key) {
            names.insert(key.to_owned(), key.to_owned());
            continue;
        }
        while taken.contains(&format!("{INPUT_REDACTED} {count}")) {
            count += 1;
        }
        let name = format!("{INPUT_REDACTED} {count}");
        taken.insert(name.clone());
        names.insert(key.to_owned(), name);
    }
    names
}

/// A copy with every secret position, credential-shaped string, and credential-shaped key redacted.
fn redacted<'a>(
    value: &Value,
    root: &'a Value,
    subschema: &Sub<'a>,
    name: &str,
    depth: usize,
) -> Result<Value, PlanError> {
    if depth > MAX_SECRET_DEPTH {
        return Ok(string(INPUT_REDACTED));
    }
    let candidates = applicable(root, subschema, 0, Held::Value(value))?;
    if secret_position(root, name, &candidates) {
        return Ok(string(INPUT_REDACTED));
    }
    match value {
        Value::Object(found) => {
            // Keys stay as they are, so a later schema walk still sees every member; only the final scrub renames them.
            let mut sorted: Vec<&(String, Value)> = found.iter().collect();
            sorted.sort_by(|left, right| left.0.cmp(&right.0));
            let mut copied = Vec::with_capacity(sorted.len());
            for (key, item) in sorted {
                let member = if resembles_credential(key) {
                    string(INPUT_REDACTED)
                } else {
                    redacted(
                        item,
                        root,
                        &member_schemas(&candidates, key)?,
                        key,
                        depth + 1,
                    )?
                };
                copied.push((key.clone(), member));
            }
            Ok(Value::Object(copied))
        }
        Value::Array(items) => {
            let mut copied = Vec::with_capacity(items.len());
            for (index, item) in items.iter().enumerate() {
                copied.push(redacted(
                    item,
                    root,
                    &item_schemas(&candidates, index),
                    name,
                    depth + 1,
                )?);
            }
            Ok(Value::Array(copied))
        }
        Value::String(text) if resembles_credential(text) => Ok(string(INPUT_REDACTED)),
        _ => Ok(value.clone()),
    }
}
