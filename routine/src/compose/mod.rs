//! Composing a confirmable Routine plan from a recording span, without a model (ADR-0101).

pub mod rerun;

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

use strict_json::Value;
use team_protocol::routine::SHOWN_MODES;

use crate::json::{as_str, at, elements, int, members, object, text};
use crate::plan::{self, Contracts, Key, Plan, PlanError, same};
use crate::provenance::{classify_call, narrowed, sent_value, target_choices};
use crate::recording::{
    self, ActionKey, Call, Context, Existing, Known, Origins, Pending, Question, RERUN, Recorded,
    Recording, RecordingError, Send, Stop, known_texts, segment_lines, span_calls, stated_outputs,
    stated_schedules,
};
use crate::trace::{Occurrence, Protection};

/// What the person asked a recording to be: the span, its protection, the exact contracts, the send whose work the
/// record intent selected, the question the span last asked, and the Routine it replaces.
#[derive(Clone, Copy, Debug)]
pub struct Request<'a> {
    /// The span's sends.
    pub sends: &'a [Send],
    /// The span's protection set.
    pub protection: &'a Protection,
    /// The exact current contracts.
    pub contracts: &'a Contracts,
    /// The position of the send whose calls are the work.
    pub work: Option<usize>,
    /// The question the span last asked.
    pub asked: Option<&'a recording::Asked>,
    /// The Routine a replacement replaces.
    pub existing: Option<&'a Existing>,
}

/// The plan a recording span defines, or the question to ask first.
///
/// The work is the calls of the send at position `work`, the one its record intent selected; with none, a
/// replacement keeps its steps. A span gets one repair: once it asked for work to run again and a later send ran
/// Actions, work that needs another repair refuses `routine-record-again` instead of asking again, whatever was asked
/// between. No card ever records work that calls an Action a person's choice is about without sending the target they
/// chose.
///
/// # Errors
///
/// [`RecordingError`] when the span cannot be one plan.
pub fn record(request: &Request<'_>) -> Result<Recording, RecordingError> {
    record_stated(request, output(request.sends, request.existing).as_deref())
}

/// [`record`] with the output the person stated (`None` when they stated none, or two).
pub(crate) fn record_stated(
    request: &Request<'_>,
    stated: Option<&str>,
) -> Result<Recording, RecordingError> {
    if request.protection.lost {
        return Err(RecordingError::new("routine-recording-unavailable"));
    }
    let sends = request.sends;
    let calls = span_calls(sends, request.contracts)?;
    let texts: Vec<String> = sends
        .iter()
        .flat_map(|send| {
            segment_lines(&send.person)
                .into_iter()
                .chain(send.window.iter().cloned())
        })
        .collect();
    let latest: Vec<Call> = calls
        .iter()
        .filter(|call| Some(call.send) == request.work)
        .cloned()
        .collect();
    let known = known_texts(&texts, request.protection);
    let mut context = Context::new(
        sends,
        calls,
        known,
        recording::zone(sends),
        request.asked,
        request.contracts,
    );
    let work = narrowed(&context, latest.clone());
    context.work = work.iter().map(|call| call.index).collect();
    // The person's output, or show while the work is planned and its own questions come first; unstated, it is asked.
    let mode = stated.unwrap_or("show");
    match plan_span(
        &mut context,
        request,
        mode,
        &work,
        &latest,
        stated.is_none(),
    ) {
        Ok(planned) => recorded(&context, request, &work, planned),
        Err(Stop::Refuse(error)) => Err(error),
        Err(Stop::Ask(question)) => asked(&context, request, &work, *question),
    }
}

/// The send count when the span first asked for a repair, which it keeps across later questions until a card.
fn repair_from(request: &Request<'_>) -> Option<usize> {
    request.asked.and_then(|asked| asked.repair_from)
}

/// The question a span asks, kept with its work and choices; a second repair ends the span instead.
fn asked(
    context: &Context<'_>,
    request: &Request<'_>,
    work: &[Call],
    question: Question,
) -> Result<Recording, RecordingError> {
    let mut repair = repair_from(request);
    let repaired = repair.is_some_and(|from| {
        request
            .sends
            .iter()
            .skip(from)
            .any(|send| !send.occurrences.is_empty())
    });
    let repairing = question.code == RERUN;
    if repaired && repairing {
        return Err(RecordingError::new("routine-record-again"));
    }
    if repairing && repair.is_none() {
        repair = Some(request.sends.len());
    }
    let mut asked = asked_question(context, work, question)?;
    asked.repair_from = repair;
    Ok(Recording::Asked(Box::new(asked)))
}

/// The recorded plan, with what the span settled; work that never sends a chosen target refuses.
fn recorded(
    context: &Context<'_>,
    request: &Request<'_>,
    work: &[Call],
    planned: Planned,
) -> Result<Recording, RecordingError> {
    let mut kept = match planned {
        Planned::Kept(kept) => kept,
        Planned::Recorded(document, origins, when) => {
            if !chosen_used(context, work) {
                return Err(RecordingError::new("routine-record-again"));
            }
            let actions: Vec<ActionKey> = elements(at(&document, "steps"))
                .iter()
                .map(step_action)
                .collect();
            Recorded {
                document,
                origins,
                permitted: permitted(&actions, request.contracts),
                schedule: when,
                timezone_source: context.zone.1.clone(),
                chosen: Vec::new(),
                repair_from: None,
            }
        }
    };
    // A question Team still asks about the plan, such as its interval over the budget, keeps what the span settled.
    kept.chosen = settled(context, &[]);
    kept.repair_from = repair_from(request);
    Ok(Recording::Recorded(Box::new(kept)))
}

/// What a span plans before its settled choices are attached: a kept replacement, or a recorded plan.
enum Planned {
    Kept(Recorded),
    Recorded(Value, Origins, Value),
}

fn plan_span(
    context: &mut Context<'_>,
    request: &Request<'_>,
    mode: &str,
    work: &[Call],
    latest: &[Call],
    unstated: bool,
) -> Result<Planned, Stop> {
    let planned = match request.existing {
        Some(existing) if latest.is_empty() => Planned::Kept(kept_plan(context, mode, existing)?),
        _ => {
            let (document, origins) = plan_document(context, mode, work)?;
            let when = schedule(request.sends, request.existing)?;
            Planned::Recorded(document, origins, when)
        }
    };
    if unstated {
        return Err(Stop::ask("routine-output-unstated"));
    }
    Ok(planned)
}

fn step_action(step: &Value) -> ActionKey {
    (
        as_str(at(step, "assistant")).unwrap_or_default().to_owned(),
        as_str(at(step, "action")).unwrap_or_default().to_owned(),
    )
}

/// Whether the work sends every target the person chose wherever it calls the Action that choice is about.
fn chosen_used(context: &Context<'_>, work: &[Call]) -> bool {
    target_choices(context).iter().all(|binding| {
        let Some(chosen) = &binding.chosen else {
            return true;
        };
        let chosen = crate::json::json_text(chosen);
        let calls: Vec<&Call> = work
            .iter()
            .filter(|call| call.action() == binding.action)
            .collect();
        calls.is_empty()
            || calls
                .iter()
                .any(|call| sent_value(call, &binding.member).as_deref() == Some(chosen.as_str()))
    })
}

/// A question as Team keeps it: with the work it asks to repeat, and every choice already answered.
///
/// A choice the person answered exactly stays bound, whatever Team asks next.
fn asked_question(
    context: &Context<'_>,
    work: &[Call],
    question: Question,
) -> Result<Question, RecordingError> {
    let mut question = question;
    let mut now = Vec::new();
    if question
        .pending
        .as_ref()
        .is_some_and(|pending| pending.chosen.is_some())
        && let Some(answered) = question.pending.take()
    {
        now.push(answered);
    }
    let chosen = settled(context, &now);
    if question.manifest.is_none() && question.code == RERUN {
        question.manifest = Some(rerun::manifest(context, work, &chosen)?);
    }
    question.chosen = chosen;
    Ok(question)
}

/// Every target choice the person answered exactly, and any chosen now, the latest one for each choice.
fn settled(context: &Context<'_>, chosen: &[Pending]) -> Vec<Pending> {
    let mut kept: Vec<Pending> = Vec::new();
    let answered = target_choices(context)
        .into_iter()
        .filter(|item| item.chosen.is_some());
    for item in answered.chain(chosen.iter().cloned()) {
        match kept.iter_mut().find(|known| known.same_choice(&item)) {
            Some(slot) => *slot = item,
            None => kept.push(item),
        }
    }
    kept
}

/// Every Action of these steps once, sorted, at its current pin, with its effect and Stored Inputs.
#[must_use]
pub fn permitted(actions: &[ActionKey], contracts: &Contracts) -> Vec<Value> {
    let unique: BTreeSet<&ActionKey> = actions.iter().collect();
    unique
        .into_iter()
        .filter_map(|key| {
            let contract = contracts.get(key)?;
            let mut stored = contract.stored_inputs.clone();
            stored.sort();
            Some(object(vec![
                ("assistant", text(&key.0)),
                ("action", text(&key.1)),
                ("pin", text(&contract.pin)),
                ("read_only", Value::Bool(contract.read_only)),
                (
                    "stored_inputs",
                    Value::Array(stored.iter().map(|name| text(name)).collect()),
                ),
            ]))
        })
        .collect()
}

// --- When it runs ----------------------------------------------------------------------------------------------------

/// The schedule the latest authored segment stating one states; a replacement keeps its own when none is stated.
///
/// Every reading within one segment counts, so different schedules in one text are asked about.
fn schedule(sends: &[Send], existing: Option<&Existing>) -> Result<Value, Stop> {
    let latest = stated_schedules(sends);
    match (latest.as_slice(), existing) {
        ([one], _) => Ok(one.clone()),
        ([], Some(existing)) => Ok(existing.schedule.clone()),
        _ => Err(Stop::ask("routine-schedule-unstated")),
    }
}

/// The output choice the person states; a replacement keeps its mode when none is.
///
/// `None` when no segment states one, or the latest that does states two: the person is then asked.
#[must_use]
pub fn output(sends: &[Send], existing: Option<&Existing>) -> Option<String> {
    let latest = stated_outputs(sends);
    match (latest.as_slice(), existing) {
        ([one], _) => Some((*one).to_owned()),
        ([], Some(existing)) => as_str(at(at(&existing.plan, "output"), "mode")).map(str::to_owned),
        _ => None,
    }
}

// --- What it runs ----------------------------------------------------------------------------------------------------

/// A replacement that ran no Action: the replaced plan's steps exactly, on a new schedule, zone, or output.
///
/// It is admitted again against the Team's exact current contracts, so a pin that drifted refuses.
fn kept_plan(context: &Context<'_>, mode: &str, existing: &Existing) -> Result<Recorded, Stop> {
    let steps = elements(at(&existing.plan, "steps")).to_vec();
    let last = steps
        .last()
        .and_then(|step| as_str(at(step, "id")))
        .ok_or(Stop::refuse("routine-recording-empty"))?
        .to_owned();
    let origins = steps
        .iter()
        .map(|step| {
            let id = as_str(at(step, "id")).unwrap_or_default().to_owned();
            let inputs = members(at(step, "input"))
                .iter()
                .map(|(member, source)| {
                    (
                        member.clone(),
                        kept_origin(source, &context.known).to_owned(),
                    )
                })
                .collect();
            (id, inputs)
        })
        .collect();
    let actions: Vec<ActionKey> = steps.iter().map(step_action).collect();
    let document = document(steps, &last, mode, &context.zone.0);
    admitted(&document, context.contracts)?;
    let when = schedule(context.sends, Some(existing))?;
    Ok(Recorded {
        document,
        origins,
        permitted: permitted(&actions, context.contracts),
        schedule: when,
        timezone_source: context.zone.1.clone(),
        chosen: Vec::new(),
        repair_from: None,
    })
}

/// The plan document of these steps, in the zone each run reads its dates in.
#[must_use]
pub fn document(steps: Vec<Value>, last: &str, mode: &str, timezone: &str) -> Value {
    let shown = if SHOWN_MODES.contains(&mode) {
        text(last)
    } else {
        Value::Null
    };
    object(vec![
        ("version", int(plan::VERSION)),
        ("timezone", text(timezone)),
        ("steps", Value::Array(steps)),
        (
            "output",
            object(vec![("mode", text(mode)), ("step", shown)]),
        ),
    ])
}

/// The plan as every run admits it, against the exact contracts; a refusal keeps the plan's own code.
///
/// # Errors
///
/// The plan's own refusal code.
pub fn admitted(document: &Value, contracts: &Contracts) -> Result<Plan, RecordingError> {
    plan::admit(document, contracts).map_err(|error| RecordingError::new(error.code))
}

/// How the card names a kept input's origin, as a recording would now.
#[must_use]
pub fn kept_origin(source: &Value, known: &Known) -> &'static str {
    match as_str(at(source, "kind")) {
        Some("literal") if known.names(at(source, "value")) => "request",
        Some("literal") => "assistant",
        Some("run_clock") => "clock",
        _ if source.get("where").is_some() => "selector",
        _ => "step",
    }
}

/// The plan document and each step's input origins, from the work and every source it needs.
fn plan_document(
    context: &mut Context<'_>,
    mode: &str,
    work: &[Call],
) -> Result<(Value, Origins), Stop> {
    if work.is_empty() {
        return Err(Stop::refuse("routine-recording-empty"));
    }
    source_classes(context);
    let nodes = closure(context, work)?;
    let order = ordered(context, &nodes)?;
    let names: BTreeMap<usize, String> = order
        .iter()
        .enumerate()
        .map(|(position, node)| (*node, format!("s{}", position + 1)))
        .collect();
    let steps: Vec<Value> = order
        .iter()
        .map(|node| step(context, &nodes[node], &names))
        .collect();
    let origins = order
        .iter()
        .map(|node| {
            (
                names[node].clone(),
                context
                    .inputs
                    .get(node)
                    .map(|(_, origins)| origins.clone())
                    .unwrap_or_default(),
            )
        })
        .collect();
    // Every other plan call is a source a later work call reads, so the last call in order is work, the one shown.
    let last = as_str(at(steps.last().unwrap_or(&Value::Null), "id"))
        .unwrap_or_default()
        .to_owned();
    let document = document(steps, &last, mode, &context.zone.0);
    verify(context, &nodes, &document, &names)?;
    Ok((document, origins))
}

/// The plan calls: each work call's source representative, and every source their inputs read, transitively.
///
/// # Errors
///
/// [`Stop`] with the first question or refusal of a call's classification.
pub fn closure(context: &mut Context<'_>, work: &[Call]) -> Result<BTreeMap<usize, Call>, Stop> {
    let mut nodes: BTreeMap<usize, Call> = BTreeMap::new();
    let mut pending: Vec<usize> = work.iter().map(|call| context.class(call.index)).collect();
    while let Some(node) = pending.pop() {
        if nodes.contains_key(&node) {
            continue;
        }
        nodes.insert(node, context.calls[node].clone());
        classify_call(context, node)?;
        if let Some((inputs, _)) = context.inputs.get(&node) {
            pending.extend(inputs.iter().filter_map(|(_, source)| source_node(source)));
        }
    }
    Ok(nodes)
}

/// The call a copied value's source names before its plan is numbered.
fn source_node(source: &Value) -> Option<usize> {
    source
        .get("node")
        .and_then(plan::small)
        .and_then(|node| usize::try_from(node).ok())
}

fn step(context: &Context<'_>, call: &Call, names: &BTreeMap<usize, String>) -> Value {
    let inputs = context
        .inputs
        .get(&call.index)
        .map(|(inputs, _)| {
            inputs
                .iter()
                .map(|(member, source)| {
                    (
                        member.clone(),
                        named(source, |node| names.get(&node).cloned()),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    object(vec![
        ("id", text(&names[&call.index])),
        ("assistant", text(&call.occurrence.assistant)),
        ("action", text(&call.occurrence.action)),
        ("pin", text(&call.occurrence.pin)),
        ("input", Value::Object(inputs)),
    ])
}

/// A source with the call it copies from named by its step id.
pub(crate) fn named(source: &Value, name: impl Fn(usize) -> Option<String>) -> Value {
    let Some(node) = source_node(source) else {
        return source.clone();
    };
    let mut fields = vec![
        ("kind".to_owned(), text("step_output")),
        (
            "step".to_owned(),
            name(node).map_or(Value::Null, |step| text(&step)),
        ),
    ];
    fields.extend(
        members(source)
            .iter()
            .filter(|(key, _)| key != "node")
            .cloned(),
    );
    Value::Object(fields)
}

/// Each call's source: its earliest read-only twin (same Action, input, and result) with no change between.
pub fn source_classes(context: &mut Context<'_>) {
    let mut open: Vec<usize> = Vec::new();
    for call in &context.calls {
        if !call.read_only() {
            // Nothing read before a change is the same source as anything read after it.
            open.clear();
            context.classes.insert(call.index, call.index);
            continue;
        }
        let twin = open
            .iter()
            .copied()
            .find(|candidate| twins(&context.calls[*candidate].occurrence, &call.occurrence));
        if twin.is_none() {
            open.push(call.index);
        }
        context
            .classes
            .insert(call.index, twin.unwrap_or(call.index));
    }
}

/// Whether two read-only calls are one source: the same Action, input, and result as JSON values compare them.
///
/// A call holding anything withheld is only itself.
fn twins(left: &Occurrence, right: &Occurrence) -> bool {
    let kept = [&left.input, &left.result, &right.input, &right.result];
    !kept
        .iter()
        .any(|item| !item.withheld.is_empty() || item.oversize)
        && (&left.assistant, &left.action) == (&right.assistant, &right.action)
        && same(&left.input.value, &right.input.value)
        && same(&left.result.value, &right.result.value)
}

/// Each plan call's successors: what reads it, and every call on the far side of a changing call.
#[must_use]
pub fn edges(
    context: &Context<'_>,
    nodes: &BTreeMap<usize, Call>,
) -> BTreeMap<usize, BTreeSet<usize>> {
    let mut successors: BTreeMap<usize, BTreeSet<usize>> =
        nodes.keys().map(|node| (*node, BTreeSet::new())).collect();
    for node in nodes.keys() {
        if let Some((inputs, _)) = context.inputs.get(node) {
            for source in inputs.iter().filter_map(|(_, source)| source_node(source)) {
                successors.entry(source).or_default().insert(*node);
            }
        }
    }
    for (changing, call) in nodes {
        if call.read_only() {
            continue;
        }
        for other in nodes.keys() {
            if other < changing {
                successors.entry(*other).or_default().insert(*changing);
            } else if other > changing {
                successors.entry(*changing).or_default().insert(*other);
            }
        }
    }
    successors
}

/// The plan calls in data-dependency order, earliest first, every changing call where it ran; refuses a cycle.
///
/// # Errors
///
/// `routine-recording-conflict` when an order would move a read across a change, `routine-recording-cyclic` otherwise.
pub fn ordered(
    context: &Context<'_>,
    nodes: &BTreeMap<usize, Call>,
) -> Result<Vec<usize>, RecordingError> {
    let successors = edges(context, nodes);
    let mut incoming: BTreeMap<usize, usize> = nodes.keys().map(|node| (*node, 0)).collect();
    for targets in successors.values() {
        for target in targets {
            *incoming.entry(*target).or_default() += 1;
        }
    }
    let mut ready: BinaryHeap<Reverse<usize>> = incoming
        .iter()
        .filter(|(_, count)| **count == 0)
        .map(|(node, _)| Reverse(*node))
        .collect();
    let mut order = Vec::new();
    while let Some(Reverse(node)) = ready.pop() {
        order.push(node);
        for target in successors.get(&node).into_iter().flatten() {
            let count = incoming.entry(*target).or_default();
            *count -= 1;
            if *count == 0 {
                ready.push(Reverse(*target));
            }
        }
    }
    if order.len() != nodes.len() {
        let changed = nodes
            .iter()
            .any(|(node, call)| !order.contains(node) && !call.read_only());
        return Err(RecordingError::new(if changed {
            "routine-recording-conflict"
        } else {
            "routine-recording-cyclic"
        }));
    }
    Ok(order)
}

// --- Verification ----------------------------------------------------------------------------------------------------

/// Every call a step stands for, replayed through a run's own admission and resolver, sends exactly what it sent.
///
/// The plan is admitted against the exact contracts, each step's selections from its recorded result are kept as a
/// run keeps them and within their bound, and each call's input is resolved as a run resolves it on that call's own
/// start, so a card never shows a plan that fails every run, such as an input past the bound a run dispatches.
pub(crate) fn verify(
    context: &Context<'_>,
    nodes: &BTreeMap<usize, Call>,
    document: &Value,
    names: &BTreeMap<usize, String>,
) -> Result<(), RecordingError> {
    let plan = admitted(document, context.contracts)?;
    let recorded: BTreeMap<&str, &Value> = nodes
        .iter()
        .map(|(node, call)| (names[node].as_str(), &call.occurrence.result.value))
        .collect();
    let resolved_calls = context
        .calls
        .iter()
        .filter(|call| nodes.contains_key(&context.class(call.index)))
        .map(|call| (call, names[&context.class(call.index)].clone()));
    replayed(
        &plan,
        |step_id| recorded.get(step_id).copied(),
        resolved_calls,
        context.sends,
    )
}

/// Whether every call resolves, through the plan's selections from the recorded results, to exactly what it sent.
pub(crate) fn replayed<'c>(
    plan: &Plan,
    recorded: impl Fn(&str) -> Option<&'c Value>,
    calls: impl Iterator<Item = (&'c Call, String)>,
    sends: &[Send],
) -> Result<(), RecordingError> {
    let replay = || -> Result<Result<(), RecordingError>, PlanError> {
        let mut selected: BTreeMap<Key, Value> = BTreeMap::new();
        for step in &plan.steps {
            let Some(result) = recorded(&step.step_id) else {
                continue;
            };
            for ((pointer, where_text, item), value) in
                plan::selections(plan, &step.step_id, result)?
            {
                selected.insert((step.step_id.clone(), pointer, where_text, item), value);
            }
        }
        if !plan::retained_within(&selected) {
            return Ok(Err(RecordingError::new("routine-recording-too-large")));
        }
        for (call, step_id) in calls {
            let Some(step) = plan.step(&step_id) else {
                continue;
            };
            let started = sends[call.send].started_at;
            let resolved = plan::resolve(plan, step, &selected, started, |_| Ok(()))?;
            if !same(&resolved, &call.occurrence.input.value) {
                return Ok(Err(RecordingError::new("routine-recording-unverified")));
            }
        }
        Ok(Ok(()))
    };
    replay().unwrap_or_else(|error| {
        Err(RecordingError::new(
            if error.code == "plan-input-too-large" {
                "routine-recording-too-large"
            } else {
                "routine-recording-unverified"
            },
        ))
    })
}

#[cfg(test)]
pub(crate) mod tests;
