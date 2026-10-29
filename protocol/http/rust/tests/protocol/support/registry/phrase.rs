//! `phrase`.

use strict_json::Value;
use team_protocol::phrase::{self, PhraseError};
use team_protocol::routine::LOCALES;

use super::{Entry, Outcome, arguments, flag, string};

/// A `language` argument: null, an empty string (which Python reads as none), or one interface language; `Err` for
/// anything else, which the adapter refuses.
fn language(value: &Value) -> Result<Option<&str>, ()> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) if text.is_empty() || LOCALES.contains(&text.as_str()) => {
            Ok(Some(text))
        }
        _ => Err(()),
    }
}

fn reading<T>(result: Result<T, PhraseError>, answer: impl Fn(T) -> Outcome) -> Outcome {
    result.map_or(Outcome::Error("phrase"), answer)
}

fn strings(items: Vec<impl Into<String>>) -> Outcome {
    Outcome::Value(Value::Array(
        items
            .into_iter()
            .map(|item| Value::String(item.into()))
            .collect(),
    ))
}

fn stated(value: &Value) -> Outcome {
    match arguments(value, ["text", "language"]) {
        Some([text, known]) => match (string(text), language(known)) {
            (Some(text), Ok(known)) => reading(phrase::stated(text, known), |found| {
                Outcome::Value(Value::Array(found))
            }),
            _ => Outcome::Refuse,
        },
        None => Outcome::Refuse,
    }
}

fn team_asks(value: &Value) -> Outcome {
    match arguments(value, ["text", "language"]) {
        Some([text, known]) => match (string(text), language(known)) {
            (Some(text), Ok(known)) => reading(phrase::team_asks(text, known), flag),
            _ => Outcome::Refuse,
        },
        None => Outcome::Refuse,
    }
}

/// The entries of `phrase`.
pub const ENTRIES: &[Entry] = &[
    ("phrase.stated", |v| {
        string(v).map_or(Outcome::Refuse, |text| {
            reading(phrase::stated(text, None), |found| {
                Outcome::Value(Value::Array(found))
            })
        })
    }),
    ("phrase.stated(language)", stated),
    ("phrase.outputs", |v| {
        string(v).map_or(Outcome::Refuse, |text| {
            reading(phrase::outputs(text), strings)
        })
    }),
    ("phrase.zones", |v| {
        string(v).map_or(Outcome::Refuse, |text| {
            reading(phrase::zones(text), strings)
        })
    }),
    ("phrase.team_asks", |v| {
        string(v).map_or(Outcome::Refuse, |text| {
            reading(phrase::team_asks(text, None), flag)
        })
    }),
    ("phrase.team_asks(language)", team_asks),
    ("phrase.requests_routine", |v| {
        string(v).map_or(Outcome::Refuse, |text| {
            reading(phrase::requests_routine(text), flag)
        })
    }),
];
