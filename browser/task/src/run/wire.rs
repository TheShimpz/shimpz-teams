//! How the run's values cross the ledger: Team's strict JSON travels as its own compact text inside Holdpoint's
//! values, so its member order and bytes survive replay exactly, and an observation crosses without its screenshot,
//! which travels as a transient blob.

use std::collections::BTreeSet;

use strict_json::{Number, Value};
use team_protocol::python::json::{Options, dumps};

use crate::cua::Observation;

const COMPACT: Options = Options {
    ensure_ascii: false,
    sort_keys: false,
    compact: true,
};

/// The actions a ref may be offered for.
const ACTIONS: [&str; 4] = ["click", "type", "scroll", "pointer"];

/// `value` as compact JSON text.
#[must_use]
pub fn text(value: &Value) -> String {
    dumps(value, COMPACT)
}

/// The strict JSON value of `text`, or `None`.
#[must_use]
pub fn parse(text: &str) -> Option<Value> {
    strict_json::parse(text.as_bytes()).ok()
}

/// The strict JSON value a Holdpoint string member holds as text.
#[must_use]
pub fn member(value: &serde_json::Value, name: &str) -> Option<Value> {
    value
        .get(name)
        .and_then(serde_json::Value::as_str)
        .and_then(parse)
}

/// A Holdpoint string member.
#[must_use]
pub fn string<'v>(value: &'v serde_json::Value, name: &str) -> Option<&'v str> {
    value.get(name).and_then(serde_json::Value::as_str)
}

/// A Holdpoint integer member.
#[must_use]
pub fn int(value: &serde_json::Value, name: &str) -> Option<i64> {
    value.get(name).and_then(serde_json::Value::as_i64)
}

fn strings(values: &[&str]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|value| Value::String((*value).to_owned()))
            .collect(),
    )
}

fn pair(first: &str, second: Value) -> Value {
    Value::Array(vec![Value::String(first.to_owned()), second])
}

/// The observation without its screenshot.
#[must_use]
pub fn observation(observation: &Observation) -> Value {
    let refs = observation
        .refs
        .iter()
        .map(|(reference, actions)| {
            pair(
                reference,
                strings(&actions.iter().copied().collect::<Vec<_>>()),
            )
        })
        .collect();
    let links = observation
        .links
        .iter()
        .map(|(reference, name)| pair(reference, Value::String(name.clone())))
        .collect();
    Value::Object(vec![
        ("url".to_owned(), Value::String(observation.url.clone())),
        ("title".to_owned(), Value::String(observation.title.clone())),
        ("text".to_owned(), Value::String(observation.text.clone())),
        ("refs".to_owned(), Value::Array(refs)),
        ("links".to_owned(), Value::Array(links)),
    ])
}

fn as_str(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

fn two(value: &Value) -> Option<(&str, &Value)> {
    match value {
        Value::Array(items) if items.len() == 2 => Some((as_str(&items[0])?, &items[1])),
        _ => None,
    }
}

fn actions(value: &Value) -> Option<BTreeSet<&'static str>> {
    let Value::Array(items) = value else {
        return None;
    };
    items
        .iter()
        .map(|item| {
            let name = as_str(item)?;
            ACTIONS.iter().copied().find(|known| *known == name)
        })
        .collect()
}

/// The observation [`observation`] wrote, without a screenshot; `None` for any other value.
#[must_use]
pub fn read_observation(value: &Value) -> Option<Observation> {
    let field = |name: &str| value.get(name).and_then(as_str).map(str::to_owned);
    let Some(Value::Array(refs)) = value.get("refs") else {
        return None;
    };
    let Some(Value::Array(links)) = value.get("links") else {
        return None;
    };
    let refs = refs
        .iter()
        .map(|item| {
            let (reference, offered) = two(item)?;
            Some((reference.to_owned(), actions(offered)?))
        })
        .collect::<Option<Vec<_>>>()?;
    let links = links
        .iter()
        .map(|item| {
            let (reference, name) = two(item)?;
            Some((reference.to_owned(), as_str(name)?.to_owned()))
        })
        .collect::<Option<Vec<_>>>()?;
    Some(Observation {
        url: field("url")?,
        title: field("title")?,
        text: field("text")?,
        refs,
        screenshot: None,
        links,
    })
}

/// A count as a strict JSON integer.
#[must_use]
pub fn count(value: u64) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::{observation, parse, read_observation, text};
    use crate::cua::Observation;

    #[test]
    fn an_observation_crosses_exactly_without_its_screenshot() {
        let seen = Observation {
            url: "https://example.test/a".to_owned(),
            title: "A \"quoted\" title".to_owned(),
            text: "page:\nline é".to_owned(),
            refs: vec![
                ("e1".to_owned(), BTreeSet::from(["click"])),
                ("e2".to_owned(), BTreeSet::from(["scroll", "type"])),
            ],
            screenshot: Some(vec![1, 2, 3]),
            links: vec![("e1".to_owned(), "Next".to_owned())],
        };
        let back =
            read_observation(&parse(&text(&observation(&seen))).expect("parsed")).expect("read");
        assert_eq!(
            back,
            Observation {
                screenshot: None,
                ..seen
            }
        );
    }

    #[test]
    fn an_unknown_action_or_shape_is_refused() {
        let value =
            parse(r#"{"url":"u","title":"t","text":"x","refs":[["e1",["hover"]]],"links":[]}"#)
                .expect("parsed");
        assert_eq!(read_observation(&value), None);
        assert_eq!(read_observation(&parse("[]").expect("parsed")), None);
    }
}
