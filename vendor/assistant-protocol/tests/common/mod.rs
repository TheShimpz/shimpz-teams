//! Reading the published vectors beside this crate (`../../protocol/assistant/v1/vectors`).

#![allow(dead_code)]

use std::path::PathBuf;

use assistant_protocol::{Number, Value};

/// The decoded vector file `name`.
pub fn vectors(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../protocol/assistant/v1/vectors")
        .join(name);
    let raw = std::fs::read(&path).unwrap_or_else(|_| panic!("{} is readable", path.display()));
    let document = strict_json::parse(&raw).expect("vectors are strict JSON");
    assert_eq!(
        document.get("version"),
        Some(&Value::Number(Number::Int(1)))
    );
    document
}

/// The non-empty `cases` list of a vector file.
pub fn cases(name: &str) -> Vec<Value> {
    match vectors(name).get("cases") {
        Some(Value::Array(cases)) if !cases.is_empty() => cases.clone(),
        _ => panic!("{name} holds cases"),
    }
}

/// A string member.
pub fn text<'a>(case: &'a Value, field: &str) -> &'a str {
    match case.get(field) {
        Some(Value::String(text)) => text,
        _ => panic!("{field} is a string"),
    }
}

/// The case's name.
pub fn name(case: &Value) -> &str {
    text(case, "name")
}

/// A boolean member.
pub fn flag(case: &Value, field: &str) -> bool {
    match case.get(field) {
        Some(Value::Bool(flag)) => *flag,
        _ => panic!("{field} is a boolean"),
    }
}

/// A member that must be present.
pub fn field<'a>(case: &'a Value, field: &str) -> &'a Value {
    case.get(field)
        .unwrap_or_else(|| panic!("{field} is present"))
}

/// The expected reason: `None` for a valid case, else its `error` (or `Some("")` when the family names none).
pub fn expected(case: &Value) -> Option<&str> {
    if flag(case, "valid") {
        None
    } else {
        Some(
            case.get("error")
                .and_then(|error| match error {
                    Value::String(text) => Some(text.as_str()),
                    _ => None,
                })
                .unwrap_or(""),
        )
    }
}

/// Checks that a family holds both outcomes and that `verdict` agrees with every case's validity.
pub fn agree(file: &str, verdict: impl Fn(&Value) -> Option<&'static str>) {
    let mut outcomes = (false, false);
    for case in cases(file) {
        let valid = flag(&case, "valid");
        assert_eq!(verdict(&case).is_none(), valid, "{file}: {}", name(&case));
        if valid {
            outcomes.0 = true;
        } else {
            outcomes.1 = true;
        }
    }
    assert_eq!(outcomes, (true, true), "{file} pins both outcomes");
}
