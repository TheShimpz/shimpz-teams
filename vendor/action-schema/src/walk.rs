//! The subschema positions of a schema and the walks over them: the value count, the reference target, and the
//! expansion of every reference.

use std::collections::{HashMap, HashSet};

use strict_json::Value;

/// Positions whose values are subschemas; any other value is data.
pub(crate) const APPLICATORS: [&str; 11] = [
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
/// Positions whose array members are subschemas.
pub(crate) const LIST_APPLICATORS: [&str; 4] = ["allOf", "anyOf", "oneOf", "prefixItems"];
/// Positions whose object member values are subschemas.
pub(crate) const MAP_APPLICATORS: [&str; 5] = [
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
];

/// The members of an object value, or an empty slice for any other value.
pub(crate) fn members(value: &Value) -> &[(String, Value)] {
    match value {
        Value::Object(members) => members,
        _ => &[],
    }
}

/// The elements of an array value, or an empty slice for any other value.
pub(crate) fn elements(value: &Value) -> &[Value] {
    match value {
        Value::Array(elements) => elements,
        _ => &[],
    }
}

/// Every subschema a node applies directly, through each Draft 2020-12 applicator except `$ref`.
pub(crate) fn applied(node: &Value) -> impl Iterator<Item = &Value> {
    let single = APPLICATORS.iter().filter_map(|keyword| node.get(keyword));
    let lists = LIST_APPLICATORS
        .iter()
        .filter_map(|keyword| node.get(keyword))
        .flat_map(elements);
    let maps = MAP_APPLICATORS
        .iter()
        .filter_map(|keyword| node.get(keyword))
        .flat_map(|map| members(map).iter().map(|(_, child)| child));
    single.chain(lists).chain(maps)
}

/// Returns whether `value` holds at most `limit` JSON values, counting the value itself, every array element, and
/// every object member value at any depth; member names do not count. The walk stops at the first excess.
#[must_use]
pub fn nodes_within(value: &Value, limit: usize) -> bool {
    let mut pending = vec![value];
    let mut count = 0_usize;
    while let Some(node) = pending.pop() {
        count += 1;
        if count > limit {
            return false;
        }
        pending.extend(elements(node));
        pending.extend(members(node).iter().map(|(_, child)| child));
    }
    true
}

/// The subschema an admitted reference names: `#`, or `#/$defs/<name>` and `#/definitions/<name>` with `~1` and `~0`
/// in the name decoded as the JSON Pointer resolver decodes them.
pub(crate) fn reference_target<'a>(root: &'a Value, reference: &str) -> Option<&'a Value> {
    if reference == "#" {
        return Some(root);
    }
    ["$defs", "definitions"].into_iter().find_map(|container| {
        let name = reference
            .strip_prefix("#/")?
            .strip_prefix(container)?
            .strip_prefix('/')?;
        root.get(container)?
            .get(&name.replace("~1", "/").replace("~0", "~"))
    })
}

/// How far the references of a schema expand: the subschemas validation can visit and the deepest object subschema
/// below the root.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Expansion {
    /// Every value at a subschema position, counted once per path from the root that reaches it.
    pub(crate) subschemas: usize,
    /// The most levels an object subschema lies below the root, a reference's target one level below its holder.
    pub(crate) depth: usize,
}

/// One open subschema of the expansion walk: its node, its remaining edges, and what it has summed so far.
struct Frame<'a> {
    node: &'a Value,
    edges: Vec<&'a Value>,
    expansion: Expansion,
}

/// Expands every reference of `root`: each value at a subschema position counts once per path that reaches it, and a
/// `$ref` target counts again at every subschema holding the reference. Returns `None` when a reference names a
/// missing definition or leads back to a subschema that contains it, which makes every `#` reference a cycle. Each
/// subschema's expansion is computed once, so the walk is linear in the document.
pub(crate) fn expand(root: &Value) -> Option<Expansion> {
    let mut known: HashMap<*const Value, Expansion> = HashMap::new();
    let mut open: HashSet<*const Value> = HashSet::from([std::ptr::from_ref(root)]);
    let mut parents: Vec<Frame> = Vec::new();
    let mut current = frame(root, root)?;
    loop {
        if let Some(edge) = current.edges.pop() {
            let key = std::ptr::from_ref(edge);
            if let Some(child) = known.get(&key) {
                absorb(&mut current.expansion, *child, edge);
            } else if !matches!(edge, Value::Object(_)) {
                absorb(&mut current.expansion, LEAF, edge);
            } else if open.insert(key) {
                parents.push(std::mem::replace(&mut current, frame(root, edge)?));
            } else {
                return None;
            }
            continue;
        }
        let key = std::ptr::from_ref(current.node);
        open.remove(&key);
        known.insert(key, current.expansion);
        let Some(mut parent) = parents.pop() else {
            return Some(current.expansion);
        };
        absorb(&mut parent.expansion, current.expansion, current.node);
        current = parent;
    }
}

/// A subschema that applies nothing further.
const LEAF: Expansion = Expansion {
    subschemas: 1,
    depth: 0,
};

fn frame<'a>(root: &'a Value, node: &'a Value) -> Option<Frame<'a>> {
    let mut edges: Vec<&Value> = applied(node).collect();
    if let Some(Value::String(reference)) = node.get("$ref") {
        edges.push(reference_target(root, reference)?);
    }
    Some(Frame {
        node,
        edges,
        expansion: LEAF,
    })
}

/// Adds one child's expansion to its parent's: its subschemas, and its depth one level down when it is an object.
fn absorb(parent: &mut Expansion, child: Expansion, node: &Value) {
    parent.subschemas = parent.subschemas.saturating_add(child.subschemas);
    if matches!(node, Value::Object(_)) {
        parent.depth = parent.depth.max(child.depth.saturating_add(1));
    }
}

#[cfg(test)]
mod tests;
