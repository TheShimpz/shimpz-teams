//! Reading Docker inspections and controller answers.

use strict_json::{Number, Value};

/// The value at `path` of nested object members.
pub fn at<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    path.iter().try_fold(value, |value, key| value.get(key))
}

/// The string at `path`.
pub fn text<'a>(value: &'a Value, path: &[&str]) -> Option<&'a str> {
    match at(value, path) {
        Some(Value::String(text)) => Some(text),
        _ => None,
    }
}

/// The integer at `path`.
pub fn integer(value: &Value, path: &[&str]) -> Option<i64> {
    match at(value, path) {
        Some(Value::Number(Number::Int(number))) => Some(*number),
        _ => None,
    }
}

/// The boolean at `path`.
pub fn boolean(value: &Value, path: &[&str]) -> Option<bool> {
    match at(value, path) {
        Some(Value::Bool(flag)) => Some(*flag),
        _ => None,
    }
}

/// The items of the array at `path`; absent, `null`, or another type reads as no items.
pub fn items<'a>(value: &'a Value, path: &[&str]) -> &'a [Value] {
    match at(value, path) {
        Some(Value::Array(items)) => items,
        _ => &[],
    }
}

/// The member names of the object at `path`; absent, `null`, or another type reads as none.
pub fn keys<'a>(value: &'a Value, path: &[&str]) -> Vec<&'a str> {
    match at(value, path) {
        Some(Value::Object(members)) => members.iter().map(|(name, _)| name.as_str()).collect(),
        _ => Vec::new(),
    }
}

/// The strings of the array at `path`.
pub fn strings<'a>(value: &'a Value, path: &[&str]) -> Vec<&'a str> {
    items(value, path)
        .iter()
        .filter_map(|item| match item {
            Value::String(text) => Some(text.as_str()),
            _ => None,
        })
        .collect()
}

/// Whether the value at `path` is absent, `null`, or an empty array or object (how Docker reports "none").
pub fn empty(value: &Value, path: &[&str]) -> bool {
    match at(value, path) {
        None | Some(Value::Null) => true,
        Some(Value::Array(items)) => items.is_empty(),
        Some(Value::Object(members)) => members.is_empty(),
        Some(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::{at, empty, integer, keys, strings, text};

    #[test]
    fn nested_members_read_by_path() {
        let value = strict_json::parse(
            br#"{"HostConfig":{"Memory":134217728,"CapDrop":["ALL"],"CapAdd":null,"Tmpfs":{}},"Name":"/a"}"#,
        )
        .expect("json");
        assert_eq!(
            integer(&value, &["HostConfig", "Memory"]),
            Some(134_217_728)
        );
        assert_eq!(strings(&value, &["HostConfig", "CapDrop"]), ["ALL"]);
        assert!(empty(&value, &["HostConfig", "CapAdd"]));
        assert!(empty(&value, &["HostConfig", "Tmpfs"]));
        assert!(empty(&value, &["HostConfig", "Missing"]));
        assert!(!empty(&value, &["HostConfig", "CapDrop"]));
        assert_eq!(text(&value, &["Name"]), Some("/a"));
        assert_eq!(keys(&value, &["HostConfig"]).len(), 4);
        assert!(at(&value, &["Name", "x"]).is_none());
    }
}
