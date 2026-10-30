//! The memory-only occurrences of a recording span's Action calls, and the values it protects (ADR-0101).
//!
//! A Routine is recorded from work the ordinary chat agent actually did. While a Local send may record one, Team keeps
//! each successful Action call in dispatch order: its Assistant Action, complete pin, whether the reviewed effect
//! proves it read-only, and its model-given input and validated result as kept values. A kept value is the exact JSON
//! with every position that may be secret withheld whole and recorded out of band as an RFC 6901 pointer: a
//! schema-secret position, a credential-shaped string, a string holding a value Team injected, and an object holding a
//! credential-shaped or injected key. Private envelopes and human answers are never kept.
//!
//! The span also keeps a protection set: every value an invocation was given or returned in secret. It only grows, and
//! a span that would exceed its bound loses it, irreversibly, so nothing it wrote may then enter a Routine. Nothing
//! here is ever persisted: a Team restart drops every occurrence and set, and recording is then unavailable.

use std::collections::BTreeSet;

use strict_json::Value;

use crate::credential::resembles_credential;
use crate::json::{EMPTY, canonical, object, text};
use crate::plan::{
    Held, MAX_SAFE_OUTPUT_DEPTH, MAX_STEPS, PlanError, Sub, applicable, item_schemas,
    member_schemas, secret_position,
};

/// The calls one recording span may keep: the plan bound.
pub const MAX_OCCURRENCES: usize = MAX_STEPS;
/// A kept value larger than this is withheld whole, so nothing can be copied from it.
pub const MAX_KEPT_BYTES: usize = 256 * 1024;
/// Every kept input and result of one recording span, together.
pub const MAX_TRACE_BYTES: usize = 1024 * 1024;
/// The protected values of one turn or run at most: past either bound the set is lost, never evicted (ADR-0101).
pub const MAX_PROTECTED_VALUES: usize = 128;
/// The protected values' UTF-8 bytes of one turn or run at most.
pub const MAX_PROTECTED_BYTES: usize = 64 * 1024;

/// One kept value: exact JSON with every withheld subtree emptied to null, and the pointers withheld.
#[derive(Clone, Debug, PartialEq)]
pub struct Kept {
    /// The value, every withheld position `null`.
    pub value: Value,
    /// The pointer of every withheld position.
    pub withheld: BTreeSet<String>,
    /// The value was larger than its bound and is withheld whole.
    pub oversize: bool,
}

impl Kept {
    /// A value kept whole, nothing withheld.
    #[must_use]
    pub const fn whole(value: Value) -> Self {
        Self {
            value,
            withheld: BTreeSet::new(),
            oversize: false,
        }
    }

    /// Whether nothing on the way to the pointer, at it, or under it was withheld.
    #[must_use]
    pub fn available(&self, pointer: &str) -> bool {
        !self.withheld.iter().any(|item| {
            item.is_empty()
                || item == pointer
                || pointer.starts_with(&format!("{item}/"))
                || item.starts_with(&format!("{pointer}/"))
        })
    }
}

/// One RFC 6901 reference token.
#[must_use]
pub fn escape(token: &str) -> String {
    token.replace('~', "~0").replace('/', "~1")
}

/// What one keep walk shares: the schema root, the protected values, and the pointers it withholds.
struct Keeper<'a> {
    root: &'a Value,
    protected: Vec<&'a str>,
    withheld: BTreeSet<String>,
}

/// The kept form of one value under its reviewed schema: every position that may be secret withheld whole.
///
/// # Errors
///
/// `plan-secret-literal` when a pattern of the schema cannot be matched within its work budget.
pub fn keep(value: &Value, schema: &Value, protected: &[String]) -> Result<Kept, PlanError> {
    let root = if matches!(schema, Value::Object(_)) {
        schema
    } else {
        &EMPTY
    };
    let mut keeper = Keeper {
        root,
        protected: protected
            .iter()
            .map(String::as_str)
            .filter(|item| !item.is_empty())
            .collect(),
        withheld: BTreeSet::new(),
    };
    let kept = keeper.kept(value, &Sub::Node(root), ("", ""), 0)?;
    if canonical(&kept).len() > MAX_KEPT_BYTES {
        return Ok(Kept {
            value: Value::Null,
            withheld: BTreeSet::from([String::new()]),
            oversize: true,
        });
    }
    Ok(Kept {
        value: kept,
        withheld: keeper.withheld,
        oversize: false,
    })
}

/// The subschemas that apply at one position, and whether it may be secret; an unwalkable schema may be.
fn secret_at<'a>(
    root: &'a Value,
    subschema: &Sub<'a>,
    name: &str,
    value: &Value,
    depth: usize,
) -> (Vec<&'a Value>, bool) {
    match applicable(root, subschema, 0, Held::Value(value)) {
        Ok(candidates) => {
            let secret = depth > MAX_SAFE_OUTPUT_DEPTH || secret_position(root, name, &candidates);
            (candidates, secret)
        }
        Err(_) => (Vec::new(), true),
    }
}

impl<'a> Keeper<'a> {
    fn kept(
        &mut self,
        value: &Value,
        subschema: &Sub<'a>,
        place: (&str, &str),
        depth: usize,
    ) -> Result<Value, PlanError> {
        let (name, at) = place;
        let (candidates, unsafe_position) = secret_at(self.root, subschema, name, value, depth);
        if unsafe_position || unsafe_value(value, &self.protected) {
            self.withheld.insert(at.to_owned());
            return Ok(Value::Null);
        }
        match value {
            Value::Object(found) => {
                let mut kept = Vec::with_capacity(found.len());
                for (key, item) in found {
                    let member = member_schemas(&candidates, key)?;
                    let pointer = format!("{at}/{}", escape(key));
                    kept.push((
                        key.clone(),
                        self.kept(item, &member, (key, &pointer), depth + 1)?,
                    ));
                }
                Ok(Value::Object(kept))
            }
            Value::Array(items) => {
                let mut kept = Vec::with_capacity(items.len());
                for (index, item) in items.iter().enumerate() {
                    let pointer = format!("{at}/{index}");
                    kept.push(self.kept(
                        item,
                        &item_schemas(&candidates, index),
                        (name, &pointer),
                        depth + 1,
                    )?);
                }
                Ok(Value::Array(kept))
            }
            _ => Ok(value.clone()),
        }
    }
}

/// A credential-shaped or injected string, or an object holding such a key: withheld whole.
fn unsafe_value(value: &Value, protected: &[&str]) -> bool {
    let unsafe_text = |text: &str| {
        resembles_credential(text) || protected.iter().any(|secret| text.contains(secret))
    };
    match value {
        Value::Object(found) => found.iter().any(|(key, _)| unsafe_text(key)),
        Value::String(text) => unsafe_text(text),
        _ => false,
    }
}

/// Every string a result holds at a position its reviewed schema may make secret, which the turn then protects.
///
/// # Errors
///
/// `plan-secret-literal` when a pattern of the schema cannot be matched within its work budget.
pub fn secret_values(value: &Value, schema: &Value) -> Result<Vec<String>, PlanError> {
    let root = if matches!(schema, Value::Object(_)) {
        schema
    } else {
        &EMPTY
    };
    let mut found = Vec::new();
    collect(root, value, &Sub::Node(root), "", 0, &mut found)?;
    Ok(found)
}

fn collect<'a>(
    root: &'a Value,
    value: &Value,
    subschema: &Sub<'a>,
    name: &str,
    depth: usize,
    found: &mut Vec<String>,
) -> Result<(), PlanError> {
    let (candidates, secret) = secret_at(root, subschema, name, value, depth);
    if secret {
        strings(value, found);
        return Ok(());
    }
    match value {
        Value::Object(members) => {
            for (key, item) in members {
                collect(
                    root,
                    item,
                    &member_schemas(&candidates, key)?,
                    key,
                    depth + 1,
                    found,
                )?;
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                collect(
                    root,
                    item,
                    &item_schemas(&candidates, index),
                    name,
                    depth + 1,
                    found,
                )?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn strings(value: &Value, found: &mut Vec<String>) {
    match value {
        Value::String(text) => found.push(text.clone()),
        Value::Object(members) => members.iter().for_each(|(_, item)| strings(item, found)),
        Value::Array(items) => items.iter().for_each(|item| strings(item, found)),
        _ => {}
    }
}

/// Whether any string or key inside a value holds a protected value.
#[must_use]
pub fn exposes(value: &Value, protected: &BTreeSet<String>) -> bool {
    match value {
        Value::String(text) => protected
            .iter()
            .any(|secret| text.contains(secret.as_str())),
        Value::Array(items) => items.iter().any(|item| exposes(item, protected)),
        Value::Object(members) => members.iter().any(|(key, item)| {
            protected.iter().any(|secret| key.contains(secret.as_str())) || exposes(item, protected)
        }),
        _ => false,
    }
}

/// Every value a turn or run was given or returned in secret; it only grows, and once lost it stays lost.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Protection {
    /// The protected values.
    pub values: BTreeSet<String>,
    /// The set outgrew its bound, for good.
    pub lost: bool,
}

impl Protection {
    /// The set grown by `values`, or lost when it would outgrow either bound.
    #[must_use]
    pub fn grow<'v>(&self, values: impl IntoIterator<Item = &'v str>) -> Self {
        let mut grown = self.values.clone();
        grown.extend(
            values
                .into_iter()
                .filter(|item| !item.is_empty())
                .map(str::to_owned),
        );
        let bytes: usize = grown.iter().map(String::len).sum();
        if self.lost || grown.len() > MAX_PROTECTED_VALUES || bytes > MAX_PROTECTED_BYTES {
            return Self {
                values: self.values.clone(),
                lost: true,
            };
        }
        Self {
            values: grown,
            lost: false,
        }
    }
}

/// The encoded size of a kept value with every pointer it withholds.
#[must_use]
pub fn kept_bytes(kept: &Kept) -> usize {
    let withheld = Value::Array(kept.withheld.iter().map(|pointer| text(pointer)).collect());
    canonical(&object(vec![
        ("value", kept.value.clone()),
        ("withheld", withheld),
    ]))
    .len()
}

/// One successful Action call of a recording turn, in the order Team dispatched it.
#[derive(Clone, Debug)]
pub struct Occurrence {
    /// Its Assistant.
    pub assistant: String,
    /// Its Action.
    pub action: String,
    /// The Action's complete pin.
    pub pin: String,
    /// Whether the reviewed contract proves the Action read-only; anything not proven is a change.
    pub read_only: bool,
    /// Its kept input.
    pub input: Kept,
    /// Its kept result.
    pub result: Kept,
    /// The interrupt the call answered, by which Team names it to the Brain with its result (ADR-0122); never compared.
    pub interrupt: String,
    /// The encoded size of its kept input and result.
    pub size: usize,
}

impl Occurrence {
    /// One occurrence, its size derived from its kept values.
    #[must_use]
    pub fn new(
        assistant: &str,
        action: &str,
        pin: &str,
        read_only: bool,
        input: Kept,
        result: Kept,
    ) -> Self {
        let size = kept_bytes(&input) + kept_bytes(&result);
        Self {
            assistant: assistant.to_owned(),
            action: action.to_owned(),
            pin: pin.to_owned(),
            read_only,
            input,
            result,
            interrupt: String::new(),
            size,
        }
    }
}

impl PartialEq for Occurrence {
    fn eq(&self, other: &Self) -> bool {
        (
            &self.assistant,
            &self.action,
            &self.pin,
            self.read_only,
            &self.input,
            &self.result,
            self.size,
        ) == (
            &other.assistant,
            &other.action,
            &other.pin,
            other.read_only,
            &other.input,
            &other.result,
            other.size,
        )
    }
}

#[cfg(test)]
mod tests;
