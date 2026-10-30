//! Every golden vector of the Assistant-install contract through Team's own admission, with its exact code.

use std::path::PathBuf;

use strict_json::Value;

fn read(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../protocol/install/v1")
        .join(name);
    strict_json::parse(&std::fs::read(path).expect("readable")).expect("strict JSON")
}

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
        let (Value::String(key), Value::Object(members)) = (key, parent) else {
            panic!("object path")
        };
        parent = &mut members
            .iter_mut()
            .find(|(name, _)| name == key)
            .expect("present")
            .1;
    }
    let (Some(Value::String(last)), Value::Object(members)) = (path.last(), parent) else {
        panic!("object")
    };
    let position = members.iter().position(|(name, _)| name == last);
    if mutation.get("op") == Some(&Value::String("remove".to_owned())) {
        members.remove(position.expect("present"));
    } else {
        let item = mutation.get("value").expect("value").clone();
        match position {
            Some(index) => members[index].1 = item,
            None => members.push((last.clone(), item)),
        }
    }
    value
}

#[test]
fn every_golden_vector_answers_its_code_through_team_admission() {
    let vectors = read("vectors.json");
    let Some(Value::Array(cases)) = vectors.get("cases") else {
        panic!("cases")
    };
    for case in cases {
        let fixture = vectors
            .get("fixtures")
            .and_then(|fixtures| fixtures.get("resolve_response"))
            .expect("fixture");
        let value = mutated(fixture.get("value").expect("value"), case.get("mutation"));
        let actual = team_install::contract::validate(&value)
            .err()
            .map(assistant_install::Violation::code);
        let expected = match case.get("error") {
            Some(Value::String(code)) => Some(code.as_str()),
            _ => None,
        };
        assert_eq!(actual, expected, "{:?}", case.get("name"));
    }
}
