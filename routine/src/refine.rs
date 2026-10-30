//! A recorded Routine's next revision built from its plan and the agent's explicit edits, without a model (ADR-0122).
//!
//! The agent names each edit with the selected revision's step ids and the handles Team issued for the traced calls of
//! the current send (`c1` for its first traced call, and so on): `insert` adds calls after a step in their traced
//! order, `replace` puts one call in a step's place, and `remove` drops a step. Nothing is matched by its Assistant and
//! Action alone. The new plan is deterministic: the base steps in their order, each kept with its own inputs, replaced,
//! or dropped, with every insertion after its step; then every step is numbered again (`s1`, `s2`, ...), since a step
//! id names a step only within its revision, and every reference follows its step.
//!
//! A new step's inputs are classified from the span exactly as a recording classifies them ([`crate::provenance`]): a
//! literal, a run date, or a value copied from another traced call of the span, kept as a name-based selection. Every
//! call a new step copies from joins the plan as an added step, once, in the earliest edit that needs it and before
//! every step that reads it, in the recorder's data-dependency order; a named call that an earlier edit would read is
//! refused, since its edit would place it after its reader. A new step never reads a kept base step, whose recorded
//! result is not in the span. An input the recorder would ask about is refused. Removing or replacing a step that a
//! kept step reads is refused too: the person records that group again. The plan keeps its zone and output mode, its
//! shown step being its new last one, and is admitted against the Team's exact current contracts, so every pin is
//! revalidated; each new step's input is resolved as a run resolves it on its call's own start and must be exactly what
//! that call sent.

use std::collections::{BTreeMap, BTreeSet};

use strict_json::Value;
use team_protocol::routine::SHOWN_MODES;

use crate::compose::{self, closure, edges, kept_origin, named, ordered, source_classes};
use crate::json::{as_str, at, elements, int, members, object, text};
use crate::plan;
use crate::recording::{Call, Context, Origins, RecordingError, Stop};

/// A refinement that names a step or call that does not exist, or leaves no step.
pub const INVALID: &str = "routine-refine-invalid";
/// A removed or replaced step that a kept step reads.
pub const DEPENDENCY: &str = "routine-refine-dependency";
/// A new step input the recorder would have to ask about.
pub const INPUT: &str = "routine-refine-input";

/// The refined plan document, each step's input origins by its new id, and how it differs from its base.
///
/// `changes` holds each new step's change and base position, in plan order; `removed` each base step that does not
/// continue, with whether it was removed or replaced; `last_changed` whether the shown result now comes from another
/// step than the base's last.
#[derive(Clone, Debug, PartialEq)]
pub struct Refined {
    /// The refined plan document.
    pub document: Value,
    /// Each step's input origins, by its new id.
    pub origins: Origins,
    /// Each new step's change and base position, in plan order.
    pub changes: Vec<(String, Option<usize>)>,
    /// Each base step that does not continue, with whether it was removed or replaced.
    pub removed: Vec<(usize, String)>,
    /// Whether the shown result now comes from another step than the base's last.
    pub last_changed: bool,
}

/// One step of the refined plan before it is numbered: what it is, its change, and where it came from.
struct Entry {
    step: Value,
    change: &'static str,
    base: Option<usize>,
    origins: Vec<(String, String)>,
    /// The source class of the traced call a new step stands for; `None` for a kept step.
    node: Option<usize>,
}

/// One edit as the refinement names it.
struct Edit<'e> {
    kind: &'e str,
    step: &'e str,
    calls: Vec<&'e str>,
}

fn edit_of(value: &Value) -> Edit<'_> {
    Edit {
        kind: as_str(at(value, "kind")).unwrap_or_default(),
        step: as_str(at(value, "step")).unwrap_or_default(),
        calls: elements(at(value, "calls"))
            .iter()
            .filter_map(as_str)
            .collect(),
    }
}

/// The plan `edits` make of `base`.
///
/// `edits` are canonical (no step or call twice); `context` covers the recording span, its zone the Routine's own;
/// `calls` are the current send's traced calls, the first named `c1`.
///
/// # Errors
///
/// [`INVALID`], [`DEPENDENCY`], [`INPUT`], or a recording or plan refusal with its own code.
pub fn refine(
    base: &Value,
    edits: &[Value],
    context: &mut Context<'_>,
    calls: &[Call],
) -> Result<Refined, RecordingError> {
    let steps = elements(at(base, "steps")).to_vec();
    let edits: Vec<Edit<'_>> = edits.iter().map(edit_of).collect();
    let ids: BTreeSet<&str> = steps
        .iter()
        .filter_map(|step| as_str(at(step, "id")))
        .collect();
    if !edits.iter().all(|edit| ids.contains(edit.step)) {
        return Err(RecordingError::new(INVALID));
    }
    independent(&steps, &edits)?;
    let mut named_calls: BTreeMap<&str, Vec<Call>> = BTreeMap::new();
    for edit in &edits {
        let group = edit
            .calls
            .iter()
            .map(|handle| call(calls, handle))
            .collect::<Result<Vec<_>, _>>()?;
        named_calls.insert(edit.step, group);
    }
    context.work = edits
        .iter()
        .flat_map(|edit| named_calls[edit.step].iter().map(|call| call.index))
        .collect();
    source_classes(context);
    let groups = placed(context, &steps, &edits, &named_calls)?;
    let (entries, removed) = entries(context, &steps, &edits, &named_calls, &groups);
    if entries.is_empty() {
        return Err(RecordingError::new(INVALID));
    }
    let document = numbered(base, &entries);
    verified(&document, context, &entries)?;
    let last = entries
        .last()
        .map_or(("", None), |entry| (entry.change, entry.base));
    Ok(Refined {
        document,
        origins: entries
            .iter()
            .enumerate()
            .map(|(place, entry)| (format!("s{}", place + 1), entry.origins.clone()))
            .collect(),
        changes: entries
            .iter()
            .map(|entry| (entry.change.to_owned(), entry.base))
            .collect(),
        removed,
        last_changed: !(last.0 == "kept" && last.1 == Some(steps.len())),
    })
}

/// A base step kept with its own inputs, their origins named as a recording would now.
fn kept_entry(context: &Context<'_>, step: &Value, position: usize) -> Entry {
    let origins = members(at(step, "input"))
        .iter()
        .map(|(member, source)| {
            (
                member.clone(),
                kept_origin(source, &context.known).to_owned(),
            )
        })
        .collect();
    Entry {
        step: step.clone(),
        change: "kept",
        base: Some(position),
        origins,
        node: None,
    }
}

/// The refined plan's steps before numbering: each base step kept, removed, or replaced, with every insertion after
/// its step; and each base step that does not continue.
fn entries(
    context: &Context<'_>,
    steps: &[Value],
    edits: &[Edit<'_>],
    named_calls: &BTreeMap<&str, Vec<Call>>,
    groups: &BTreeMap<String, Vec<usize>>,
) -> (Vec<Entry>, Vec<(usize, String)>) {
    let mut entries: Vec<Entry> = Vec::new();
    let mut removed = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        let position = index + 1;
        let id = as_str(at(step, "id")).unwrap_or_default();
        let kind = edits
            .iter()
            .find(|edit| edit.step == id)
            .map(|edit| edit.kind);
        if matches!(kind, None | Some("insert")) {
            entries.push(kept_entry(context, step, position));
        } else {
            removed.push((
                position,
                if kind == Some("remove") {
                    "removed"
                } else {
                    "replaced"
                }
                .to_owned(),
            ));
        }
        if !matches!(kind, Some("insert" | "replace")) {
            continue;
        }
        let replaced: BTreeSet<usize> = if kind == Some("replace") {
            named_calls[id]
                .iter()
                .map(|call| context.class(call.index))
                .collect()
        } else {
            BTreeSet::new()
        };
        for node in groups.get(id).into_iter().flatten() {
            let (change, from) = if replaced.contains(node) {
                ("replaced", Some(position))
            } else {
                ("added", None)
            };
            entries.push(new_entry(context, *node, change, from));
        }
    }
    (entries, removed)
}

/// The traced call a Team handle names: `c{k}` is the send's k-th traced call.
fn call(calls: &[Call], handle: &str) -> Result<Call, RecordingError> {
    let place: usize = handle
        .get(1..)
        .and_then(|digits| digits.parse().ok())
        .unwrap_or(0);
    place
        .checked_sub(1)
        .and_then(|index| calls.get(index))
        .cloned()
        .ok_or(RecordingError::new(INVALID))
}

/// Refuse removing or replacing a step that a step kept as it is reads: that group is recorded again.
fn independent(steps: &[Value], edits: &[Edit<'_>]) -> Result<(), RecordingError> {
    let gone: BTreeSet<&str> = edits
        .iter()
        .filter(|edit| matches!(edit.kind, "remove" | "replace"))
        .map(|edit| edit.step)
        .collect();
    for step in steps {
        if gone.contains(as_str(at(step, "id")).unwrap_or_default()) {
            continue;
        }
        let reads_gone = members(at(step, "input")).iter().any(|(_, source)| {
            as_str(at(source, "kind")) == Some("step_output")
                && gone.contains(as_str(at(source, "step")).unwrap_or_default())
        });
        if reads_gone {
            return Err(RecordingError::new(DEPENDENCY));
        }
    }
    Ok(())
}

/// Each edit's new steps as source classes in their order: its named calls and every call they copy from, once.
///
/// A copied-from call that no edit names goes to the earliest edit that needs it; a named call stays with its edit,
/// which is refused when an earlier edit reads it. Within an edit the recorder's data-dependency order holds.
/// Where each edit's new steps go: a replacement where its step was, an insertion just after its step.
fn anchors<'e>(steps: &[Value], edits: &[Edit<'e>]) -> BTreeMap<&'e str, (usize, usize)> {
    let place: BTreeMap<&str, usize> = steps
        .iter()
        .enumerate()
        .filter_map(|(index, step)| as_str(at(step, "id")).map(|id| (id, index)))
        .collect();
    edits
        .iter()
        .map(|edit| {
            (
                edit.step,
                (
                    place.get(edit.step).copied().unwrap_or(0),
                    usize::from(edit.kind != "replace"),
                ),
            )
        })
        .collect()
}

fn placed(
    context: &mut Context<'_>,
    steps: &[Value],
    edits: &[Edit<'_>],
    named_calls: &BTreeMap<&str, Vec<Call>>,
) -> Result<BTreeMap<String, Vec<usize>>, RecordingError> {
    let anchor = anchors(steps, edits);
    let closures = closures(context, edits, named_calls)?;
    let mut by_anchor: Vec<&str> = closures.keys().copied().collect();
    by_anchor.sort_by_key(|step| anchor[step]);
    let owner = owners(context, edits, named_calls, &closures, &by_anchor);
    if closures
        .iter()
        .any(|(step, nodes)| nodes.keys().any(|node| anchor[owner[node]] > anchor[step]))
    {
        return Err(RecordingError::new(INVALID));
    }
    // One data-dependency order over every new step, which each edit keeps for its own.
    let nodes: BTreeMap<usize, Call> = owner
        .keys()
        .map(|node| (*node, context.calls[*node].clone()))
        .collect();
    let order = ordered(context, &nodes)?;
    let groups: BTreeMap<String, Vec<usize>> = closures
        .keys()
        .map(|step| {
            (
                (*step).to_owned(),
                order
                    .iter()
                    .copied()
                    .filter(|node| owner[node] == *step)
                    .collect(),
            )
        })
        .collect();
    // The plan runs the groups in their anchors' order: every read and every change barrier must still hold there.
    let ranked: BTreeMap<usize, usize> = by_anchor
        .iter()
        .flat_map(|step| groups[*step].iter().copied())
        .enumerate()
        .map(|(rank, node)| (node, rank))
        .collect();
    if edges(context, &nodes).iter().any(|(source, targets)| {
        targets
            .iter()
            .any(|target| ranked.get(source) > ranked.get(target))
    }) {
        return Err(RecordingError::new(INVALID));
    }
    Ok(groups)
}

/// Each edit's named calls and every call they copy from, by source class; a call the recorder would ask about
/// refuses.
fn closures<'e>(
    context: &mut Context<'_>,
    edits: &[Edit<'e>],
    named_calls: &BTreeMap<&str, Vec<Call>>,
) -> Result<BTreeMap<&'e str, BTreeMap<usize, Call>>, RecordingError> {
    let mut closures = BTreeMap::new();
    for edit in edits {
        let group = &named_calls[edit.step];
        if !group.is_empty() {
            let nodes = closure(context, group).map_err(|stop| match stop {
                Stop::Ask(_) => RecordingError::new(INPUT),
                Stop::Refuse(error) => error,
            })?;
            closures.insert(edit.step, nodes);
        }
    }
    Ok(closures)
}

/// The edit each new step's source class joins: a named call its own edit, any other call the earliest that needs it.
fn owners<'e>(
    context: &Context<'_>,
    edits: &[Edit<'e>],
    named_calls: &BTreeMap<&str, Vec<Call>>,
    closures: &BTreeMap<&'e str, BTreeMap<usize, Call>>,
    by_anchor: &[&'e str],
) -> BTreeMap<usize, &'e str> {
    let mut owner: BTreeMap<usize, &str> = BTreeMap::new();
    for edit in edits {
        for call in &named_calls[edit.step] {
            owner.insert(context.class(call.index), edit.step);
        }
    }
    for step in by_anchor {
        for node in closures[step].keys() {
            owner.entry(*node).or_insert(step);
        }
    }
    owner
}

/// A new step's id until the plan is numbered: no base step id (`s{k}`) can be one.
fn placeholder(node: usize) -> String {
    format!("n{node}")
}

/// A new step for one source class, its inputs as the recorder classified them and its copies naming new steps.
fn new_entry(
    context: &Context<'_>,
    node: usize,
    change: &'static str,
    base: Option<usize>,
) -> Entry {
    let occurrence = &context.calls[node].occurrence;
    let (inputs, origins) = context.inputs.get(&node).cloned().unwrap_or_default();
    let inputs = inputs
        .iter()
        .map(|(member, source)| {
            (
                member.clone(),
                named(source, |source_node| Some(placeholder(source_node))),
            )
        })
        .collect();
    let step = object(vec![
        ("id", text(&placeholder(node))),
        ("assistant", text(&occurrence.assistant)),
        ("action", text(&occurrence.action)),
        ("pin", text(&occurrence.pin)),
        ("input", Value::Object(inputs)),
    ]);
    Entry {
        step,
        change,
        base,
        origins,
        node: Some(node),
    }
}

/// The plan document with every step numbered again in order and every reference following its step.
fn numbered(base: &Value, entries: &[Entry]) -> Value {
    let renamed: BTreeMap<String, String> = entries
        .iter()
        .enumerate()
        .map(|(place, entry)| {
            (
                as_str(at(&entry.step, "id")).unwrap_or_default().to_owned(),
                format!("s{}", place + 1),
            )
        })
        .collect();
    let steps: Vec<Value> = entries
        .iter()
        .enumerate()
        .map(|(place, entry)| {
            let inputs = members(at(&entry.step, "input"))
                .iter()
                .map(|(member, source)| (member.clone(), renumbered(source, &renamed)))
                .collect();
            let mut step = entry.step.clone();
            crate::json::set(&mut step, "id", text(&format!("s{}", place + 1)));
            crate::json::set(&mut step, "input", Value::Object(inputs));
            step
        })
        .collect();
    let mode = as_str(at(at(base, "output"), "mode")).unwrap_or_default();
    let last = steps
        .last()
        .map_or(Value::Null, |step| at(step, "id").clone());
    let shown = if SHOWN_MODES.contains(&mode) {
        last
    } else {
        Value::Null
    };
    object(vec![
        ("version", int(plan::VERSION)),
        ("timezone", at(base, "timezone").clone()),
        ("steps", Value::Array(steps)),
        (
            "output",
            object(vec![("mode", text(mode)), ("step", shown)]),
        ),
    ])
}

/// A source with the step it copies from named by its new id.
fn renumbered(source: &Value, renamed: &BTreeMap<String, String>) -> Value {
    let mut source = source.clone();
    if as_str(at(&source, "kind")) == Some("step_output") {
        let target = as_str(at(&source, "step"))
            .and_then(|step| renamed.get(step))
            .cloned();
        crate::json::set(
            &mut source,
            "step",
            target.map_or(Value::Null, |step| text(&step)),
        );
    }
    source
}

/// Admit the plan against the exact contracts and prove every new step sends what its calls sent.
///
/// Every call a new step stands for is resolved on its own start from the new steps' recorded selections, as a
/// recording's verification proves its plan.
fn verified(
    document: &Value,
    context: &Context<'_>,
    entries: &[Entry],
) -> Result<(), RecordingError> {
    let plan = compose::admitted(document, context.contracts)?;
    // The new steps in plan order, each with the source class its traced calls share.
    let new: Vec<(String, usize)> = plan
        .steps
        .iter()
        .zip(entries)
        .filter_map(|(step, entry)| entry.node.map(|node| (step.step_id.clone(), node)))
        .collect();
    let recorded = |step_id: &str| {
        new.iter()
            .find(|(id, _)| id == step_id)
            .map(|(_, node)| &context.calls[*node].occurrence.result.value)
    };
    let calls = new.iter().flat_map(|(step_id, node)| {
        context
            .calls
            .iter()
            .filter(move |call| context.class(call.index) == *node)
            .map(move |call| (call, step_id.clone()))
    });
    compose::replayed(&plan, recorded, calls, context.sends)
}

#[cfg(test)]
mod tests;
