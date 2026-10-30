//! The work a Routine question asks the Brain to run again, as Team names it, without a model (ADR-0101).

use std::collections::BTreeSet;

use strict_json::Value;

use crate::json::sorted_members;
use crate::provenance::{self, Origin, binding_for, holders};
use crate::recording::{Call, Context, Manifest, Pending, RecordingError, Slot};

/// The work these calls did, as a rerun must repeat it, with a chosen target in place of the one they sent.
///
/// # Errors
///
/// The first refusal of the recorder's own classification: an oversize input, or a withheld one.
pub fn manifest(
    context: &Context<'_>,
    calls: &[Call],
    chosen: &[Pending],
) -> Result<Manifest, RecordingError> {
    let mut slots = Vec::new();
    let mut seen = BTreeSet::new();
    for call in calls {
        let given = &call.occurrence.input;
        // The first refusals of the recorder's own classification, in its order.
        if given.oversize {
            return Err(RecordingError::new("routine-recording-too-large"));
        }
        if !given.withheld.is_empty() || !matches!(given.value, Value::Object(_)) {
            return Err(RecordingError::new("routine-secret-literal"));
        }
        if !seen.insert(context.class(call.index)) {
            continue;
        }
        let inputs: Vec<(String, String, Value)> = sorted_members(&given.value)
            .into_iter()
            .map(|(member, value)| slot_input(context, call, member, value, chosen))
            .collect();
        let sources = inputs
            .iter()
            .filter(|(_, kind, _)| kind == "fresh")
            .filter_map(|(member, _, value)| {
                let found = holders(context, call, value);
                let first = *found.keys().next()?;
                Some((member.clone(), context.calls[first].action()))
            })
            .collect();
        slots.push(Slot {
            action: call.action(),
            inputs,
            sources,
        });
    }
    Ok(Manifest { slots })
}

/// What a rerun must send for one input member: the chosen target, a date, a fresh value, or this exact one.
///
/// It names each value by the recorder's own classifier: a value the recorder copies from a result, or asks about as
/// an identifier no result holds, needs fresh provenance; the run date stays the run date; anything else, a dated text
/// included, is kept exactly as the recorder keeps it.
fn slot_input(
    context: &Context<'_>,
    call: &Call,
    member: &str,
    value: &Value,
    chosen: &[Pending],
) -> (String, String, Value) {
    if let Some(target) =
        binding_for(chosen, (call, member), value).and_then(|binding| binding.chosen.clone())
    {
        return (member.to_owned(), "value".to_owned(), target);
    }
    match provenance::origin(context, (call, member), value, None) {
        Origin::Clock => (member.to_owned(), "clock".to_owned(), Value::Null),
        Origin::Sourced | Origin::Unsourced => {
            (member.to_owned(), "fresh".to_owned(), value.clone())
        }
        _ => (member.to_owned(), "value".to_owned(), value.clone()),
    }
}
