//! Every validator and encoder of the crate by its Python name (`module.function`), taking one JSON value, so the
//! vector tests, the differential `decide` example, and the fuzz target call each one alike.
//!
//! A function of several Python arguments takes an object of them by their Python names. An adapter that needs a
//! canonical input (such as `routine.daily_cap`, which Python calls only on an admitted schedule) refuses any other.
//! `differential.py` holds the same names over the Python modules. Each module's entries live in their own file; add a
//! module's file and its line in [`all`].

mod browser;
mod payload;
mod phrase;
mod presentation;
mod progress;
mod routine;
mod routine_context;
mod routine_notice;
mod routine_proposal;
mod routine_run;
mod sign_in;
mod websocket;

use strict_json::{Number, Value};

/// What one call answers: the value Python returns, a refusal (`None`), or a closed error Python raises.
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// The returned value.
    Value(Value),
    /// `None`.
    Refuse,
    /// A closed error, by the name `differential.py` gives its Python exception.
    Error(&'static str),
}

/// One entry: the Python name and the call.
pub type Entry = (&'static str, fn(&Value) -> Outcome);

/// Every entry.
#[must_use]
pub fn all() -> Vec<Entry> {
    [
        payload::ENTRIES,
        phrase::ENTRIES,
        presentation::ENTRIES,
        progress::ENTRIES,
        routine::ENTRIES,
        routine_context::ENTRIES,
        routine_notice::ENTRIES,
        routine_proposal::ENTRIES,
        routine_run::ENTRIES,
        browser::ENTRIES,
        sign_in::ENTRIES,
        websocket::ENTRIES,
    ]
    .concat()
}

/// The call named `name`.
#[must_use]
pub fn find(name: &str) -> Option<fn(&Value) -> Outcome> {
    all()
        .into_iter()
        .find(|(known, _)| *known == name)
        .map(|(_, call)| call)
}

/// One line of the differential output: the canonical representation, `refuse`, or `error:<name>`.
#[must_use]
pub fn line(outcome: &Outcome) -> String {
    match outcome {
        Outcome::Value(value) => super::repr::canonical(value),
        Outcome::Refuse => "refuse".to_owned(),
        Outcome::Error(name) => format!("error:{name}"),
    }
}

/// An optional value.
pub fn value(value: Option<Value>) -> Outcome {
    value.map_or(Outcome::Refuse, Outcome::Value)
}

/// An optional borrowed value.
pub fn borrowed(value: Option<&Value>) -> Outcome {
    value.cloned().map_or(Outcome::Refuse, Outcome::Value)
}

/// An optional string.
pub fn text(text: Option<&str>) -> Outcome {
    text.map_or(Outcome::Refuse, |text| {
        Outcome::Value(Value::String(text.to_owned()))
    })
}

/// A boolean.
pub fn flag(flag: bool) -> Outcome {
    Outcome::Value(Value::Bool(flag))
}

/// An integer.
pub fn integer(integer: i64) -> Outcome {
    Outcome::Value(Value::Number(Number::Int(integer)))
}

/// A count.
pub fn count(count: usize) -> Outcome {
    integer(i64::try_from(count).expect("a count fits i64"))
}

/// The members of an argument object with exactly these names, in this order; `None` for anything else.
pub fn arguments<'a, const N: usize>(value: &'a Value, names: [&str; N]) -> Option<[&'a Value; N]> {
    let Value::Object(members) = value else {
        return None;
    };
    if members.len() != N {
        return None;
    }
    let mut found = [&Value::Null; N];
    for (slot, name) in found.iter_mut().zip(names) {
        *slot = members
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, member)| member)?;
    }
    Some(found)
}

/// A string argument.
pub fn string(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

/// An `i64` integer argument (never a boolean).
pub fn small(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(integer)) => Some(*integer),
        _ => None,
    }
}
