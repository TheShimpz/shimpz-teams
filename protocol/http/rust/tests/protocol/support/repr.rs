//! The canonical value representation the vectors and the differential harness compare.
//!
//! Integers are `{"int":"<decimal>"}`, floats `{"float_bits":"<16 hex digits of the f64 bits>"}`, objects
//! `{"object":[[key,value],...]}` in document order, and strings, arrays, booleans, and null are themselves. The text
//! is exactly what Python writes with `json.dumps(tree, ensure_ascii=False, separators=(",", ":"))`.

use std::fmt::Write as _;

use strict_json::{Number, Value};

/// Writes the canonical representation of `value`.
pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(Number::Int(integer)) => write!(out, "{{\"int\":\"{integer}\"}}").unwrap(),
        Value::Number(Number::BigInt(digits)) => write!(out, "{{\"int\":\"{digits}\"}}").unwrap(),
        Value::Number(Number::Float(float)) => {
            write!(out, "{{\"float_bits\":\"{:016x}\"}}", float.to_bits()).unwrap();
        }
        Value::String(text) => write_string(text, out),
        Value::Array(items) => write_items(items.iter(), out, write_value),
        Value::Object(members) => {
            out.push_str("{\"object\":");
            write_items(members.iter(), out, |(key, member), out| {
                out.push('[');
                write_string(key, out);
                out.push(',');
                write_value(member, out);
                out.push(']');
            });
            out.push('}');
        }
    }
}

fn write_items<T>(
    items: impl Iterator<Item = T>,
    out: &mut String,
    write_item: impl Fn(T, &mut String),
) {
    out.push('[');
    for (index, item) in items.enumerate() {
        if index > 0 {
            out.push(',');
        }
        write_item(item, out);
    }
    out.push(']');
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\0'..='\u{1f}' => write!(out, "\\u{:04x}", u32::from(character)).unwrap(),
            _ => out.push(character),
        }
    }
    out.push('"');
}
