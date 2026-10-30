//! Where each recorded input value comes from, without a model (ADR-0101).
//!
//! A value is a literal, the run date, a copy of an earlier result, or a question to the person.

use std::collections::{BTreeMap, BTreeSet};

use strict_json::Value;
use team_protocol::python::text;
use team_protocol::python::value::py_eq;
use team_protocol::routine_context::{MIN_IDENTIFIER_CHARS, MIN_IDENTIFIER_DIGITS};
use team_protocol::routine_proposal::{
    MAX_QUESTION_OPTION_CHARS, MAX_QUESTION_OPTIONS, canonical_question,
};

use crate::dates;
use crate::dates::date::Date;
use crate::json::{as_str, at, is_integer, json_text, members, object, text as string};
use crate::plan::{self, pointer_tokens_of, same};
use crate::recording::{
    Call, Context, Known, Pending, Question, RERUN, RecordingError, Stop, Target,
};
use crate::schedule;
use crate::trace::{Kept, escape, exposes};

/// The members whose text names an item to a person, such as a zone's domain: a target's label when it has one only,
/// and the selector when the person named several members that each single out the item.
const NAME_MEMBERS: [&str; 6] = [
    "name",
    "title",
    "label",
    "display_name",
    "hostname",
    "domain",
];

/// One input member of one call: the call, and the member.
pub type Input<'c> = (&'c Call, &'c str);

/// Where one recorded input value comes from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin {
    /// The person named it.
    Request,
    /// It is the run date in the Routine's zone.
    Clock,
    /// It is the UTC date of a send on another local date: a fixed literal.
    UtcLiteral,
    /// The assistant chose it.
    Assistant,
    /// A text whose dates each run writes.
    Template,
    /// A value one earlier result holds.
    Sourced,
    /// An identifier no result holds, which only work run again can source.
    Unsourced,
}

/// Classify every top-level input member of one plan call; refuses a secret.
///
/// # Errors
///
/// [`Stop`] with the question to ask about one input, or the refusal of a secret, an oversize input, or a reference no
/// plan can hold.
pub fn classify_call(context: &mut Context<'_>, index: usize) -> Result<(), Stop> {
    let call = context.calls[index].clone();
    let given = &call.occurrence.input;
    if given.oversize {
        return Err(Stop::refuse("routine-recording-too-large"));
    }
    let schema = context
        .contracts
        .get(&call.action())
        .map_or(&Value::Null, |contract| &contract.input_schema);
    if !given.withheld.is_empty()
        || !matches!(given.value, Value::Object(_))
        || exposes(&given.value, &context.known.protected)
    {
        return Err(Stop::refuse("routine-secret-literal"));
    }
    if plan::secret_literal(schema, &given.value).unwrap_or(true) {
        return Err(Stop::refuse("routine-secret-literal"));
    }
    let mut inputs = Vec::new();
    let mut origins = Vec::new();
    for (member, value) in members(&given.value) {
        let (source, origin) = classified(context, (&call, member), value)?;
        inputs.push((member.clone(), source));
        origins.push((member.clone(), origin.to_owned()));
    }
    context.inputs.insert(call.index, (inputs, origins));
    Ok(())
}

/// Where one input value comes from, by the first of the recording's rules that applies (ADR-0101).
///
/// It is the one classifier the recorder plans from and a rerun names its work by, so the two never disagree.
/// `pending` is the target choice this value belongs to, if any, which only the person's exact answer names.
#[must_use]
pub fn origin(
    context: &Context<'_>,
    input: Input<'_>,
    value: &Value,
    pending: Option<&Pending>,
) -> Origin {
    let (call, _member) = input;
    if person_named(&context.known, pending, value) {
        return Origin::Request;
    }
    if let Some(dated) = date_origin(context, call, value) {
        return dated;
    }
    if !referable(value) {
        return Origin::Assistant;
    }
    if !holders(context, call, value).is_empty() {
        return Origin::Sourced;
    }
    if matches!(value, Value::String(written) if dated(context, call, written).is_some()) {
        return Origin::Template;
    }
    if identifier(value) {
        Origin::Unsourced
    } else {
        Origin::Assistant
    }
}

fn literal(value: &Value) -> Value {
    object(vec![("kind", string("literal")), ("value", value.clone())])
}

fn classified(
    context: &Context<'_>,
    input: Input<'_>,
    value: &Value,
) -> Result<(Value, &'static str), Stop> {
    let choices = target_choices(context);
    let pending = binding_for(&choices, input, value);
    match origin(context, input, value, pending) {
        Origin::Request => Ok((literal(value), "request")),
        Origin::Clock => Ok((
            object(vec![
                ("kind", string("run_clock")),
                ("format", string("date")),
            ]),
            "clock",
        )),
        Origin::Template => {
            let written = as_str(value).unwrap_or_default();
            dated(context, input.0, written)
                .ok_or_else(|| Stop::refuse("routine-recording-unverified"))
        }
        Origin::Sourced | Origin::Unsourced => bound(context, input, value, pending),
        Origin::UtcLiteral | Origin::Assistant => Ok((literal(value), "assistant")),
    }
}

/// Whether the person named the value: the target they chose, or, never for a target, a value their text holds.
///
/// A target of the pending choice is confirmed only by the person's exact answer, never by a substring.
fn person_named(known: &Known, pending: Option<&Pending>, value: &Value) -> bool {
    let written = json_text(value);
    if let Some(pending) = pending.filter(|pending| pending.offers(&written)) {
        return pending
            .chosen
            .as_ref()
            .is_some_and(|chosen| json_text(chosen) == written);
    }
    known.names(value)
}

/// A referable value's source, or the question about it again until the person answers it.
///
/// Once the person chose another of the targets of the choice this value belongs to, the work must run again with it.
fn bound(
    context: &Context<'_>,
    input: Input<'_>,
    value: &Value,
    pending: Option<&Pending>,
) -> Result<(Value, &'static str), Stop> {
    let source = match sourced(context, input, value) {
        Ok(source) => source,
        Err(Stop::Ask(question)) => {
            let again = pending.filter(|pending| {
                pending.chosen.is_some()
                    && question
                        .pending
                        .as_ref()
                        .is_some_and(|asked| asked.same_choice(pending))
            });
            return Err(match again {
                Some(pending) => {
                    let mut rerun = Question::new(RERUN, Vec::new());
                    rerun.pending = Some(pending.clone());
                    Stop::Ask(Box::new(rerun))
                }
                None => Stop::Ask(question),
            });
        }
        Err(refused) => return Err(refused),
    };
    unexposed(&source, &context.known)?;
    Ok((
        source.clone(),
        if source.get("where").is_some() {
            "selector"
        } else {
            "step"
        },
    ))
}

/// `Clock` when the value is the date its own send started on in the Routine's zone, which each run reads there.
///
/// The UTC date of a send that started on another date in that zone is the assistant's fixed literal; anything else is
/// no date of the send.
fn date_origin(context: &Context<'_>, call: &Call, value: &Value) -> Option<Origin> {
    let written = as_str(value)?;
    let started = context.sends[call.send].started_at;
    if date_at(started, &context.zone.0).is_some_and(|day| day.isoformat() == written) {
        return Some(Origin::Clock);
    }
    date_at(started, "UTC")
        .filter(|day| day.isoformat() == written)
        .map(|_| Origin::UtcLiteral)
}

/// A text no result holds whose dates each run writes for its own date, as a template; `None` when it has none.
///
/// The dates are read near the date its send started on in the Routine's zone, in the send's interface language; a
/// date the person's own words wrote stays as written.
fn dated(context: &Context<'_>, call: &Call, value: &str) -> Option<(Value, &'static str)> {
    let send = &context.sends[call.send];
    let day = date_at(send.started_at, &context.zone.0)?;
    let mut named = BTreeSet::new();
    for line in &context.known.texts {
        named.extend(dates::dates(line, day).ok()?);
    }
    let parts = dates::template(value, day, send.locale.as_deref(), |date| {
        named.contains(&date)
    })?;
    let source = object(vec![
        ("kind", string("run_clock")),
        ("format", string("text")),
        ("parts", Value::Array(parts)),
    ]);
    Some((source, "clock"))
}

/// The date of an instant in a zone, `None` when the zone does not load.
#[must_use]
pub fn date_at(instant: i64, timezone: &str) -> Option<Date> {
    Date::at(instant, &schedule::zone_named(timezone).ok()?)
}

fn referable(value: &Value) -> bool {
    match value {
        Value::String(written) => text::len(written) >= MIN_IDENTIFIER_CHARS,
        Value::Number(_) if is_integer(value) => {
            let digits = crate::json::json_text(value);
            digits.trim_start_matches('-').len() >= MIN_IDENTIFIER_DIGITS
        }
        Value::Object(found) => !found.is_empty(),
        Value::Array(items) => !items.is_empty(),
        _ => false,
    }
}

/// A position inside a kept result: its tokens, and which of them index an array.
pub type Position = (Vec<String>, Vec<bool>);

fn positions(node: &Value, value: &Value, at: &mut Position, found: &mut Vec<Position>) {
    if same(node, value) {
        found.push(at.clone());
    }
    match node {
        Value::Object(members) => {
            for (key, item) in members {
                at.0.push(key.clone());
                at.1.push(false);
                positions(item, value, at, found);
                at.0.pop();
                at.1.pop();
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                at.0.push(index.to_string());
                at.1.push(true);
                positions(item, value, at, found);
                at.0.pop();
                at.1.pop();
            }
        }
        _ => {}
    }
}

/// The RFC 6901 pointer of these tokens.
#[must_use]
pub fn pointer(tokens: &[String]) -> String {
    let mut written = String::new();
    for token in tokens {
        written.push('/');
        written.push_str(&escape(token));
    }
    written
}

/// Each source representative whose replayed result holds `value`, and the available positions it is at.
#[must_use]
pub fn holders(
    context: &Context<'_>,
    consumer: &Call,
    value: &Value,
) -> BTreeMap<usize, Vec<Position>> {
    let mut found: BTreeMap<usize, Vec<Position>> = BTreeMap::new();
    let consumer_class = context.class(consumer.index);
    for call in &context.calls {
        let representative = context.class(call.index);
        // Neither the consumer nor any exact twin of it is its own source.
        if representative == consumer_class || found.contains_key(&representative) {
            continue;
        }
        let result = &call.occurrence.result;
        let mut all = Vec::new();
        positions(
            &result.value,
            value,
            &mut (Vec::new(), Vec::new()),
            &mut all,
        );
        let available: Vec<Position> = all
            .into_iter()
            .filter(|(tokens, _)| result.available(&pointer(tokens)))
            .collect();
        if !available.is_empty() {
            found.insert(representative, available);
        }
    }
    found
}

/// Of several sources holding a work call's value, the one the work itself ran: it replays every run.
///
/// Only a consumer the work stands for prefers it, and only a source whose class holds a call of the narrowed work
/// counts; two such sources, or none, keep them all.
fn from_work(
    context: &Context<'_>,
    consumer: &Call,
    found: BTreeMap<usize, Vec<Position>>,
) -> BTreeMap<usize, Vec<Position>> {
    let ran: BTreeSet<usize> = context
        .work
        .iter()
        .map(|index| context.class(*index))
        .collect();
    if !ran.contains(&context.class(consumer.index)) {
        return found;
    }
    let kept: BTreeMap<usize, Vec<Position>> = found
        .iter()
        .filter(|(node, _)| ran.contains(node))
        .map(|(node, at)| (*node, at.clone()))
        .collect();
    if kept.len() == 1 { kept } else { found }
}

/// Whether a referable value is shaped like an identifier: one word of text, or a whole number.
fn identifier(value: &Value) -> bool {
    is_integer(value)
        || matches!(value, Value::String(written) if !written.chars().any(text::isspace))
}

/// Every target choice the span holds, as the person's answers settle them.
///
/// Each one already answered, and the open one, chosen as soon as a later send answers it exactly, so that answer
/// decides everything at once: what the work sends and what a rerun must repeat.
#[must_use]
pub fn target_choices(context: &Context<'_>) -> Vec<Pending> {
    let Some(asked) = context.asked else {
        return Vec::new();
    };
    let Some(pending) = &asked.pending else {
        return asked.chosen.clone();
    };
    let mut current = pending.clone();
    if let Some(selected) = selection(context, pending) {
        current.chosen = Some(selected);
    }
    let mut choices = asked.chosen.clone();
    choices.push(current);
    choices
}

/// The target choice binding this Action input's value: one about this input with the value among its targets.
#[must_use]
pub fn binding_for<'b>(
    bindings: &'b [Pending],
    input: Input<'_>,
    value: &Value,
) -> Option<&'b Pending> {
    let (call, member) = input;
    let written = json_text(value);
    let action = call.action();
    bindings.iter().find(|binding| {
        binding.action == action && binding.member == member && binding.offers(&written)
    })
}

/// The work without its calls that sent a target the person did not choose, when one of its calls sent theirs.
///
/// Those calls were the choice not taken, so the work keeps the chosen call and runs nothing again. A changing call
/// never drops: work that changed something with another target must run again with the chosen one.
#[must_use]
pub fn narrowed(context: &Context<'_>, work: Vec<Call>) -> Vec<Call> {
    let mut work = work;
    for binding in target_choices(context) {
        let Some(chosen) = &binding.chosen else {
            continue;
        };
        let chosen = json_text(chosen);
        let sent: Vec<(usize, Option<String>)> = work
            .iter()
            .map(|call| (call.index, sent_target(call, &binding)))
            .collect();
        let others: BTreeSet<usize> = sent
            .iter()
            .filter(|(_, written)| written.as_ref().is_some_and(|written| *written != chosen))
            .map(|(index, _)| *index)
            .collect();
        let chose = sent
            .iter()
            .any(|(_, written)| written.as_deref() == Some(chosen.as_str()));
        if chose
            && work
                .iter()
                .filter(|call| others.contains(&call.index))
                .all(Call::read_only)
        {
            work.retain(|call| !others.contains(&call.index));
        }
    }
    work
}

/// The JSON text of the target of `binding` this call sent, or `None` when it sent none of them.
fn sent_target(call: &Call, binding: &Pending) -> Option<String> {
    if call.action() != binding.action {
        return None;
    }
    sent_value(call, &binding.member).filter(|written| binding.offers(written))
}

/// The JSON text of what a call sent for one input member, or `None` when it sent none or it is withheld.
#[must_use]
pub fn sent_value(call: &Call, member: &str) -> Option<String> {
    let given = &call.occurrence.input;
    if !given.withheld.is_empty() {
        return None;
    }
    given.value.get(member).map(json_text)
}

/// The target whose exact JSON text the person's latest answer is, or `None`; "123" and 123 never match each other.
fn selection(context: &Context<'_>, pending: &Pending) -> Option<Value> {
    let after = context.asked.map_or(0, |asked| asked.after);
    let segments: Vec<&String> = context
        .sends
        .iter()
        .skip(after)
        .flat_map(|send| &send.person)
        .collect();
    segments.into_iter().rev().find_map(|segment| {
        let answer = text::strip(segment);
        pending
            .targets
            .iter()
            .find(|target| json_text(&target.value) == answer)
            .map(|target| target.value.clone())
    })
}

/// How one position of a source's result is read.
enum Binding {
    /// By its pointer, outside arrays.
    Pointer(usize, String),
    /// Through the one array item whose named member the person wrote.
    Selector(usize, String, (String, Value), String),
    /// Not by itself: these targets are offered instead.
    Options(Vec<Target>),
}

impl Binding {
    fn source(&self) -> Option<Value> {
        match self {
            Self::Pointer(node, pointer) => Some(node_source(*node, pointer, None)),
            Self::Selector(node, pointer, where_, item) => {
                Some(node_source(*node, pointer, Some((where_, item))))
            }
            Self::Options(_) => None,
        }
    }
}

/// A copied value's source before its plan is numbered: its source call as `node`, then its selection.
fn node_source(node: usize, pointer: &str, selector: Option<(&(String, Value), &String)>) -> Value {
    let mut fields = vec![
        (
            "node".to_owned(),
            crate::json::int(i64::try_from(node).unwrap_or(i64::MAX)),
        ),
        ("pointer".to_owned(), string(pointer)),
    ];
    if let Some(((key, constant), item)) = selector {
        fields.push((
            "where".to_owned(),
            Value::Object(vec![(key.clone(), constant.clone())]),
        ));
        fields.push(("item".to_owned(), string(item)));
    }
    Value::Object(fields)
}

/// The one source occurrence and position holding `value`; else it asks.
///
/// An identifier nothing returned is asked about, since freezing one would replay a remembered id.
fn sourced(context: &Context<'_>, input: Input<'_>, value: &Value) -> Result<Value, Stop> {
    let (consumer, _member) = input;
    let mut found = holders(context, consumer, value);
    if found.is_empty() {
        // An identifier no result holds: only work run again finds where it comes from.
        return Err(Stop::ask(RERUN));
    }
    if found.len() > 1 {
        found = from_work(context, consumer, found);
    }
    if found.len() > 1 {
        return Err(ambiguous(
            context,
            input,
            value,
            vec![Target::new(value.clone(), None)],
        ));
    }
    let Some((node, positions)) = found.into_iter().next() else {
        return Err(Stop::ask(RERUN));
    };
    let bindings: Vec<Binding> = positions
        .iter()
        .map(|position| binding(context, node, position))
        .collect();
    if let [single] = bindings.as_slice()
        && let Some(source) = single.source()
    {
        return Ok(source);
    }
    let selected: Vec<&Binding> = bindings
        .iter()
        .filter(|item| matches!(item, Binding::Selector(..)))
        .collect();
    if let [one] = selected.as_slice()
        && let Some(source) = one.source()
    {
        return Ok(source);
    }
    let options = bindings
        .iter()
        .flat_map(|item| match item {
            Binding::Options(targets) if !targets.is_empty() => targets.clone(),
            _ => vec![Target::new(value.clone(), None)],
        })
        .collect();
    Err(ambiguous(context, input, value, options))
}

/// The question which target an input means: a scalar's choices; a container is never chosen and refuses.
///
/// A target the span protects is never offered, and a protected label is never shown.
fn ambiguous(context: &Context<'_>, input: Input<'_>, value: &Value, options: Vec<Target>) -> Stop {
    if matches!(value, Value::Object(_) | Value::Array(_)) {
        return Stop::refuse("routine-recording-ambiguous");
    }
    let protected = &context.known.protected;
    let mut choices: Vec<Target> = Vec::new();
    for option in options {
        if exposes(&string(&json_text(&option.value)), protected) {
            continue;
        }
        let mut option = option;
        if option
            .label
            .as_deref()
            .is_some_and(|label| exposes(&string(label), protected))
        {
            option.label = None;
        }
        if choices
            .iter()
            .all(|item| !py_eq(&option.value, &item.value))
            && offered(&option)
        {
            choices.push(option);
        }
    }
    let (call, member) = input;
    let pending = Pending {
        action: call.action(),
        member: member.to_owned(),
        targets: choices,
        chosen: None,
    };
    let mut question = Question::new("routine-binding-ambiguous", shown(&pending));
    question.pending = Some(pending);
    Stop::Ask(Box::new(question))
}

/// The targets a question shows: every one, or none when there are more than it may show.
fn shown(pending: &Pending) -> Vec<Target> {
    if pending.targets.len() > MAX_QUESTION_OPTIONS {
        return Vec::new();
    }
    pending.targets.clone()
}

/// Whether a target can be shown as one choice of the question.
fn offered(option: &Target) -> bool {
    let question = Question::new("routine-binding-ambiguous", vec![option.clone()]).wire();
    canonical_question(&question).is_some()
}

/// How one position is read: its pointer outside arrays, or through one array by the member the person named.
///
/// Never by an index, which a reordered result would point at another item; anything else offers its targets.
fn binding(context: &Context<'_>, node: usize, position: &Position) -> Binding {
    let (tokens, arrays) = position;
    let crossed: Vec<usize> = arrays
        .iter()
        .enumerate()
        .filter(|(_, array)| **array)
        .map(|(place, _)| place)
        .collect();
    let place = match crossed.as_slice() {
        [] => return Binding::Pointer(node, pointer(tokens)),
        [one] => *one,
        _ => return Binding::Options(Vec::new()),
    };
    let result = &context.calls[node].occurrence.result;
    let (array_tokens, rest) = (&tokens[..place], &tokens[place + 1..]);
    let items = crate::json::elements(reach(&result.value, array_tokens));
    let chosen: usize = tokens[place].parse().unwrap_or(usize::MAX);
    let item = items.get(chosen).unwrap_or(&Value::Null);
    let named: Vec<(String, Value)> = members(item)
        .iter()
        .filter(|(key, constant)| rest.first() != Some(key) && selectable(constant, &context.known))
        .cloned()
        .collect();
    let mut unique: Vec<&(String, Value)> = named
        .iter()
        .filter(|(key, constant)| distinct(result, array_tokens, items, chosen, key, constant))
        .collect();
    if unique.len() > 1 {
        // Several named members each single out this one item, such as a record's type and name: the one that names
        // it to a person selects it.
        let by_name: Vec<&(String, Value)> = unique
            .iter()
            .copied()
            .filter(|(key, _)| NAME_MEMBERS.contains(&key.as_str()))
            .collect();
        if !by_name.is_empty() {
            unique = by_name;
        }
    }
    if let [(key, constant)] = unique.as_slice() {
        return Binding::Selector(
            node,
            pointer(array_tokens),
            (key.clone(), constant.clone()),
            pointer(rest),
        );
    }
    if named.is_empty() {
        return Binding::Options(listed(items, rest));
    }
    Binding::Options(targets(items, &named, rest))
}

/// A target value: a string or an integer, never a boolean.
fn target_value(value: Option<&Value>) -> Option<&Value> {
    value.filter(|value| matches!(value, Value::String(_)) || is_integer(value))
}

/// Each array item sharing a named member, as a target: the value its input would take and that member.
fn targets(items: &[Value], named: &[(String, Value)], rest: &[String]) -> Vec<Target> {
    let mut found = Vec::new();
    for (key, constant) in named {
        for item in items {
            if item.get(key).is_some_and(|member| same(member, constant))
                && let Some(chosen) = target_value(reached(item, rest))
            {
                found.push(Target::new(chosen.clone(), as_str(constant)));
            }
        }
    }
    found
}

/// Every array item as a target: the value its input would take, labelled by its one name-like member, if any.
fn listed(items: &[Value], rest: &[String]) -> Vec<Target> {
    items
        .iter()
        .filter_map(|item| {
            target_value(reached(item, rest)).map(|chosen| Target::new(chosen.clone(), label(item)))
        })
        .collect()
}

fn label(item: &Value) -> Option<&str> {
    let names: Vec<&str> = members(item)
        .iter()
        .filter(|(key, _)| NAME_MEMBERS.contains(&key.as_str()))
        .filter_map(|(_, value)| as_str(value))
        .filter(|name| (1..=MAX_QUESTION_OPTION_CHARS).contains(&text::len(name)))
        .collect();
    match names.as_slice() {
        [one] => Some(one),
        _ => None,
    }
}

/// The node an item's member tokens reach, or `None` when one names nothing there; they cross no array.
fn reached<'v>(value: &'v Value, tokens: &[String]) -> Option<&'v Value> {
    let mut value = value;
    for token in tokens {
        value = match value {
            Value::Object(_) => value.get(token)?,
            _ => return None,
        };
    }
    Some(value)
}

/// The node a position's tokens reach inside a kept value; every token names an existing member or index.
fn reach<'v>(value: &'v Value, tokens: &[String]) -> &'v Value {
    let mut value = value;
    for token in tokens {
        value = match value {
            Value::Array(items) => token
                .parse::<usize>()
                .ok()
                .and_then(|index| items.get(index)),
            _ => value.get(token),
        }
        .unwrap_or(&Value::Null);
    }
    value
}

fn selectable(constant: &Value, known: &Known) -> bool {
    match constant {
        Value::String(written) => {
            text::len(written) >= crate::recording::MIN_SELECTOR_CHARS && known.names(constant)
        }
        _ => is_integer(constant) && known.names(constant),
    }
}

/// Whether no other item holds the same member value, and none hides that member where it could.
fn distinct(
    result: &Kept,
    array_tokens: &[String],
    items: &[Value],
    chosen: usize,
    key: &str,
    constant: &Value,
) -> bool {
    items
        .iter()
        .enumerate()
        .filter(|(index, _)| *index != chosen)
        .all(|(index, other)| {
            let mut at = array_tokens.to_vec();
            at.push(index.to_string());
            at.push(key.to_owned());
            result.available(&pointer(&at))
                && !other.get(key).is_some_and(|member| same(member, constant))
        })
}

/// Refuse a reference no plan pointer can hold, or whose path or selector holds a value the span protects.
fn unexposed(source: &Value, known: &Known) -> Result<(), RecordingError> {
    let pointers = [
        as_str(at(source, "pointer")).unwrap_or_default(),
        source.get("item").and_then(as_str).unwrap_or_default(),
    ];
    let mut flat = Vec::new();
    for written in pointers {
        flat.extend(
            pointer_tokens_of(written).ok_or(RecordingError::new("routine-recording-too-large"))?,
        );
    }
    let mut exposed: Vec<Value> = pointers.iter().map(|written| string(written)).collect();
    exposed.extend(flat.iter().map(|token| string(token)));
    exposed.push(
        source
            .get("where")
            .cloned()
            .unwrap_or_else(|| object(vec![])),
    );
    if exposes(&Value::Array(exposed), &known.protected) {
        return Err(RecordingError::new("routine-secret-literal"));
    }
    Ok(())
}
