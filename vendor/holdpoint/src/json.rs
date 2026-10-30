//! Canonical JSON: the only encoding Holdpoint persists or hashes.
//!
//! The decoder is strict RFC 8259 with three extra refusals: a duplicate object key, nesting deeper than
//! [`MAX_DEPTH`], and an integer literal outside `i64::MIN..=u64::MAX` (two distinct integers must never decode to the
//! same value). A number with a fraction or exponent is an IEEE-754 double, as for every JSON consumer; a literal
//! that overflows to infinity is refused. The writer sorts object keys itself, so its output never depends on how
//! `serde_json::Map` happens to be compiled, and every digest is SHA-256 over those canonical bytes.

use serde_json::{Map, Number, Value};
use sha2::{Digest as _, Sha256};

use crate::error::Error;

/// The deepest nesting of arrays and objects accepted.
pub const MAX_DEPTH: usize = 64;

/// Decodes `bytes` as one strict JSON document of at most `max_bytes` bytes.
///
/// # Errors
///
/// [`Error::Contract`] when the input is larger than `max_bytes`, is not valid JSON, repeats an object key, nests
/// deeper than [`MAX_DEPTH`], or holds a number that cannot be represented exactly as described in the module docs.
pub fn parse(bytes: &[u8], max_bytes: usize) -> Result<Value, Error> {
    if bytes.len() > max_bytes {
        return Err(Error::Contract(format!("json document over {max_bytes} bytes")));
    }
    let mut parser = Parser { bytes, at: 0 };
    let value = parser.value(0)?;
    parser.skip_space();
    if parser.at != bytes.len() {
        return Err(parser.fail("trailing content"));
    }
    Ok(value)
}

/// Encodes `value` canonically: compact, object keys sorted by their UTF-8 bytes.
#[must_use]
pub fn to_canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

/// The SHA-256 of the canonical encoding of `value`, as 64 lowercase hex digits.
#[must_use]
pub fn digest(value: &Value) -> String {
    digest_bytes(to_canonical(value).as_bytes())
}

/// The SHA-256 of `bytes`, as 64 lowercase hex digits.
#[must_use]
pub fn digest_bytes(bytes: &[u8]) -> String {
    let hash = Sha256::digest(bytes);
    let mut hex = String::with_capacity(64);
    for byte in hash {
        hex.push(char::from(HEX[usize::from(byte >> 4)]));
        hex.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    hex
}

/// The depth of the deepest array or object in `value` (a scalar has depth 0).
#[must_use]
pub fn depth(value: &Value) -> usize {
    match value {
        Value::Array(items) => 1 + items.iter().map(depth).max().unwrap_or(0),
        Value::Object(map) => 1 + map.values().map(depth).max().unwrap_or(0),
        _ => 0,
    }
}

const HEX: &[u8; 16] = b"0123456789abcdef";

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => out.push_str(&number.to_string()),
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => write_object(out, map),
    }
}

fn write_object(out: &mut String, map: &Map<String, Value>) {
    let mut entries: Vec<(&String, &Value)> = map.iter().collect();
    entries.sort_unstable_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
    out.push('{');
    for (index, (key, item)) in entries.into_iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        write_string(out, key);
        out.push(':');
        write_value(out, item);
    }
    out.push('}');
}

fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            control if u32::from(control) < 0x20 => {
                let code = u32::from(control);
                out.push_str("\\u00");
                out.push(char::from(HEX[(code >> 4) as usize]));
                out.push(char::from(HEX[(code & 0x0f) as usize]));
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

struct Parser<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Parser<'_> {
    fn fail(&self, what: &str) -> Error {
        Error::Contract(format!("invalid json at byte {}: {what}", self.at))
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\n' | b'\r' | b'\t')) {
            self.at += 1;
        }
    }

    fn expect(&mut self, byte: u8) -> Result<(), Error> {
        if self.peek() == Some(byte) {
            self.at += 1;
            Ok(())
        } else {
            Err(self.fail("unexpected byte"))
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, Error> {
        self.skip_space();
        match self.peek() {
            Some(b'{') => self.object(depth + 1),
            Some(b'[') => self.array(depth + 1),
            Some(b'"') => self.string().map(Value::String),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.fail("expected a value")),
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, Error> {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            Ok(value)
        } else {
            Err(self.fail("invalid literal"))
        }
    }

    fn enter(&self, depth: usize) -> Result<(), Error> {
        if depth > MAX_DEPTH { Err(self.fail("nesting too deep")) } else { Ok(()) }
    }

    fn array(&mut self, depth: usize) -> Result<Value, Error> {
        self.enter(depth)?;
        self.at += 1;
        let mut items = Vec::new();
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value(depth)?);
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(Value::Array(items));
                }
                _ => return Err(self.fail("expected ',' or ']'")),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<Value, Error> {
        self.enter(depth)?;
        self.at += 1;
        let mut map = Map::new();
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.skip_space();
            if self.peek() != Some(b'"') {
                return Err(self.fail("expected a key"));
            }
            let key = self.string()?;
            self.skip_space();
            self.expect(b':')?;
            let item = self.value(depth)?;
            if map.insert(key, item).is_some() {
                return Err(self.fail("duplicate key"));
            }
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Value::Object(map));
                }
                _ => return Err(self.fail("expected ',' or '}'")),
            }
        }
    }

    fn string(&mut self) -> Result<String, Error> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            match self.peek() {
                None => return Err(self.fail("unterminated string")),
                Some(b'"') => {
                    self.at += 1;
                    return String::from_utf8(out).map_err(|_| self.fail("invalid utf-8"));
                }
                Some(b'\\') => {
                    self.at += 1;
                    let character = self.escape()?;
                    let mut buffer = [0_u8; 4];
                    out.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
                }
                Some(byte) if byte < 0x20 => return Err(self.fail("control character in string")),
                Some(byte) => {
                    out.push(byte);
                    self.at += 1;
                }
            }
        }
    }

    fn escape(&mut self) -> Result<char, Error> {
        let byte = self.peek().ok_or_else(|| self.fail("unterminated escape"))?;
        self.at += 1;
        Ok(match byte {
            b'"' => '"',
            b'\\' => '\\',
            b'/' => '/',
            b'b' => '\u{08}',
            b'f' => '\u{0c}',
            b'n' => '\n',
            b'r' => '\r',
            b't' => '\t',
            b'u' => return self.unicode(),
            _ => return Err(self.fail("invalid escape")),
        })
    }

    fn unicode(&mut self) -> Result<char, Error> {
        let first = self.hex4()?;
        if (0xdc00..0xe000).contains(&first) {
            return Err(self.fail("lone low surrogate"));
        }
        if !(0xd800..0xdc00).contains(&first) {
            return char::from_u32(first).ok_or_else(|| self.fail("invalid code point"));
        }
        if !self.bytes[self.at..].starts_with(b"\\u") {
            return Err(self.fail("lone high surrogate"));
        }
        self.at += 2;
        let second = self.hex4()?;
        if !(0xdc00..0xe000).contains(&second) {
            return Err(self.fail("invalid low surrogate"));
        }
        let code = 0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00);
        char::from_u32(code).ok_or_else(|| self.fail("invalid code point"))
    }

    fn hex4(&mut self) -> Result<u32, Error> {
        let digits = self.bytes.get(self.at..self.at + 4).ok_or_else(|| self.fail("short unicode escape"))?;
        let mut code = 0_u32;
        for digit in digits {
            let nibble = char::from(*digit).to_digit(16).ok_or_else(|| self.fail("invalid hex digit"))?;
            code = code * 16 + nibble;
        }
        self.at += 4;
        Ok(code)
    }

    fn digits(&mut self) -> usize {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.at += 1;
        }
        self.at - start
    }

    fn number(&mut self) -> Result<Value, Error> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        let integer_start = self.at;
        let integer_digits = self.digits();
        if integer_digits == 0 || (integer_digits > 1 && self.bytes[integer_start] == b'0') {
            return Err(self.fail("invalid number"));
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            self.at += 1;
            is_float = true;
            if self.digits() == 0 {
                return Err(self.fail("invalid fraction"));
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.at += 1;
            is_float = true;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if self.digits() == 0 {
                return Err(self.fail("invalid exponent"));
            }
        }
        // Every byte from `start` is an ASCII digit, sign, dot, or exponent marker.
        let text = String::from_utf8_lossy(&self.bytes[start..self.at]);
        if is_float {
            let float: f64 = text.parse().map_err(|_| self.fail("invalid number"))?;
            return Number::from_f64(float).map(Value::Number).ok_or_else(|| self.fail("number out of range"));
        }
        let number = if text.starts_with('-') {
            text.parse::<i64>().map(Number::from)
        } else {
            text.parse::<u64>().map(Number::from)
        };
        number.map(Value::Number).map_err(|_| self.fail("integer out of range"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ok(text: &str) -> Value {
        parse(text.as_bytes(), 1 << 20).unwrap()
    }

    /// The refusal's message; a document that decodes yields a message no assertion expects.
    fn refused(text: &str) -> String {
        parse(text.as_bytes(), 1 << 20).map_or_else(|error| error.to_string(), |value| format!("decoded {value}"))
    }

    #[test]
    fn decodes_every_kind_of_value() {
        let value = ok(r#" {"a": [1, -2, 3.5, true, false, null, "x"], "b": {}, "c": [], "d": 1e2} "#);
        assert_eq!(value, json!({"a": [1, -2, 3.5, true, false, null, "x"], "b": {}, "c": [], "d": 100.0}));
    }

    #[test]
    fn decodes_escapes_and_surrogate_pairs() {
        let value = ok(r#""\"\\\/\b\f\n\r\t\u00e9\ud83d\ude00""#);
        assert_eq!(value, json!("\"\\/\u{08}\u{0c}\n\r\t\u{e9}\u{1f600}"));
    }

    #[test]
    fn refuses_duplicate_keys() {
        assert!(refused(r#"{"a": 1, "a": 1}"#).contains("duplicate key"));
    }

    #[test]
    fn refuses_integers_that_would_collapse() {
        assert!(refused("18446744073709551616").contains("integer out of range"));
        assert!(refused("-9223372036854775809").contains("integer out of range"));
        assert_eq!(ok("18446744073709551615"), json!(u64::MAX));
        assert_eq!(ok("-9223372036854775808"), json!(i64::MIN));
    }

    #[test]
    fn refuses_non_finite_numbers() {
        assert!(refused("1e400").contains("number out of range"));
    }

    #[test]
    fn refuses_deep_nesting_but_accepts_the_limit() {
        let deep = "[".repeat(MAX_DEPTH + 1) + &"]".repeat(MAX_DEPTH + 1);
        assert!(refused(&deep).contains("nesting too deep"));
        let limit = "[".repeat(MAX_DEPTH) + &"]".repeat(MAX_DEPTH);
        assert_eq!(depth(&ok(&limit)), MAX_DEPTH);
        let objects = "{\"a\":".repeat(MAX_DEPTH + 1) + "1" + &"}".repeat(MAX_DEPTH + 1);
        assert!(refused(&objects).contains("nesting too deep"));
    }

    #[test]
    fn refuses_oversized_documents() {
        assert!(matches!(parse(b"[1,2]", 4), Err(Error::Contract(_))));
        assert!(parse(b"[1,2]", 5).is_ok());
    }

    #[test]
    fn refuses_malformed_documents() {
        for text in [
            "",
            " ",
            "{",
            "[1,]",
            "[1 2]",
            "{\"a\" 1}",
            "{\"a\":1,}",
            "{1:1}",
            "{\"a\":1 \"b\":2}",
            "tru",
            "nul",
            "fals",
            "01",
            "-",
            "1.",
            "1e",
            "1e+",
            ".5",
            "+1",
            "\"abc",
            "\"\\x\"",
            "\"\\u12\"",
            "\"\\uzzzz\"",
            "\"\\udc00\"",
            "\"\\ud800\"",
            "\"\\ud800\\u0041\"",
            "\"\u{01}\"",
            "1 2",
            "[",
            "\"\\",
            "@",
        ] {
            assert!(refused(text).starts_with("contract: invalid json"), "{text}");
        }
    }

    #[test]
    fn refuses_invalid_utf8() {
        assert!(matches!(parse(b"\"\xff\"", 16), Err(Error::Contract(_))));
    }

    #[test]
    fn canonical_form_sorts_keys_and_escapes_controls() {
        let value =
            json!({"b": 1, "a": [true, null, "\u{01}\u{1f}\"\\\n\r\t\u{08}\u{0c}é"], "aa": {"z": 0, "y": -1.5}});
        assert_eq!(
            to_canonical(&value),
            r#"{"a":[true,null,"\u0001\u001f\"\\\n\r\t\b\fé"],"aa":{"y":-1.5,"z":0},"b":1}"#
        );
    }

    #[test]
    fn canonical_form_round_trips() {
        let value = json!({"k": [1, 2.25, "s", {"x": false}], "e": ""});
        assert_eq!(ok(&to_canonical(&value)), value);
    }

    #[test]
    fn digest_depends_only_on_the_value() {
        assert_eq!(digest(&ok(r#"{"a":1,"b":2}"#)), digest(&ok(r#"{ "b" : 2, "a" : 1 }"#)));
        assert_ne!(digest(&json!(1)), digest(&json!(1.0)));
        assert_eq!(digest(&json!(null)), "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b");
    }
}
