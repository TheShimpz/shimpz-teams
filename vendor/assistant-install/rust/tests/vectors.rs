//! Every golden vector of `../v1/vectors.json`, decided with the contract's own pinned schemas.

use std::path::PathBuf;

use assistant_install::{Contract, LoadError, Violation};
use strict_json::Value;

fn read(name: &str) -> Vec<u8> {
    std::fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../v1")
            .join(name),
    )
    .expect("readable")
}

fn contract() -> Contract {
    Contract::load(
        &read("definitions.schema.json"),
        &read("resolve-response.schema.json"),
    )
    .expect("the pinned schemas load")
}

/// The fixture with the case's one `set` or `remove` mutation applied.
fn mutated(fixture: &Value, mutation: Option<&Value>) -> Value {
    let mut value = fixture.clone();
    let Some(mutation) = mutation else {
        return value;
    };
    let Some(Value::Array(path)) = mutation.get("path") else {
        panic!("path")
    };
    let mut parent = &mut value;
    for key in &path[..path.len() - 1] {
        let Value::String(key) = key else {
            panic!("key")
        };
        let Value::Object(members) = parent else {
            panic!("object")
        };
        parent = &mut members
            .iter_mut()
            .find(|(name, _)| name == key)
            .expect("present")
            .1;
    }
    let Some(Value::String(last)) = path.last() else {
        panic!("last")
    };
    let Value::Object(members) = parent else {
        panic!("object")
    };
    let position = members.iter().position(|(name, _)| name == last);
    match mutation.get("op") {
        Some(Value::String(op)) if op == "remove" => {
            members.remove(position.expect("present"));
        }
        _ => {
            let item = mutation.get("value").expect("value").clone();
            match position {
                Some(index) => members[index].1 = item,
                None => members.push((last.clone(), item)),
            }
        }
    }
    value
}

#[test]
fn every_golden_vector_answers_its_code() {
    let contract = contract();
    let vectors = strict_json::parse(&read("vectors.json")).expect("strict JSON");
    let Some(Value::Array(cases)) = vectors.get("cases") else {
        panic!("cases")
    };
    let mut codes = Vec::new();
    for case in cases {
        let Some(Value::String(name)) = case.get("name") else {
            panic!("name")
        };
        let Some(Value::String(fixture)) = case.get("fixture") else {
            panic!("fixture")
        };
        let fixture = vectors
            .get("fixtures")
            .and_then(|fixtures| fixtures.get(fixture))
            .expect("fixture");
        assert_eq!(
            fixture.get("schema"),
            Some(&Value::String("resolve-response.schema.json".to_owned()))
        );
        let value = mutated(fixture.get("value").expect("value"), case.get("mutation"));
        let actual = contract.validate(&value).err().map(Violation::code);
        let expected = match case.get("error") {
            Some(Value::String(code)) => Some(code.as_str()),
            _ => None,
        };
        assert_eq!(actual, expected, "{name}");
        assert_eq!(
            case.get("valid") == Some(&Value::Bool(true)),
            expected.is_none(),
            "{name}"
        );
        codes.push(actual);
    }
    codes.sort_unstable();
    codes.dedup();
    assert_eq!(codes.len(), 10, "every code and the valid outcome occur");
}

#[test]
fn schemas_outside_the_contract_never_load() {
    let definitions = read("definitions.schema.json");
    let entry = read("resolve-response.schema.json");
    let replace = |raw: &[u8], from: &str, to: &str| {
        String::from_utf8(raw.to_vec())
            .expect("utf-8")
            .replacen(from, to, 1)
            .into_bytes()
    };
    let cases = [
        (
            replace(&definitions, "\"title\"", "\"description\""),
            entry.clone(),
        ),
        (
            definitions.clone(),
            replace(&entry, "#/$defs/resolveResponse", "#/$defs/missing"),
        ),
        (
            definitions.clone(),
            replace(
                &entry,
                "assistant-install/v1/resolve",
                "assistant-install/v2/resolve",
            ),
        ),
        (replace(&definitions, "2020-12", "2019-09"), entry.clone()),
        (b"{".to_vec(), entry.clone()),
    ];
    for (definitions, entry) in cases {
        assert_eq!(Contract::load(&definitions, &entry).err(), Some(LoadError));
    }
}

#[test]
fn a_pattern_end_anchor_never_admits_a_final_line_feed() {
    let contract = contract();
    let vectors = strict_json::parse(&read("vectors.json")).expect("strict JSON");
    let fixture = vectors
        .get("fixtures")
        .and_then(|fixtures| fixtures.get("resolve_response"))
        .and_then(|fixture| fixture.get("value"))
        .expect("fixture");
    let Some(Value::String(digest)) = fixture.get("source_digest") else {
        panic!("digest")
    };
    let mutation = strict_json::parse(
        format!(r#"{{"op": "set", "path": ["source_digest"], "value": "{digest}\n"}}"#).as_bytes(),
    )
    .expect("JSON");
    assert_eq!(
        contract.validate(&mutated(fixture, Some(&mutation))),
        Err(Violation::Schema)
    );
}
