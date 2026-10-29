use crate::{Error, MAX_DEPTH, Number, Value, parse};

fn string(text: &str) -> Value {
    Value::String(text.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

#[test]
fn decodes_every_value_kind() {
    let value =
        parse(b" {\"a\" : [true, false, null, -0, 1.5e1, \"x\"], \"b\":{}, \"c\":[]}\r\n\t")
            .unwrap();
    let expected = Value::Object(vec![
        (
            "a".to_owned(),
            Value::Array(vec![
                Value::Bool(true),
                Value::Bool(false),
                Value::Null,
                int(0),
                Value::Number(Number::Float(15.0)),
                string("x"),
            ]),
        ),
        ("b".to_owned(), Value::Object(vec![])),
        ("c".to_owned(), Value::Array(vec![])),
    ]);
    assert_eq!(value, expected);
}

#[test]
fn types_numbers_as_python_does() {
    let cases: [(&[u8], Number); 8] = [
        (b"-9223372036854775808", Number::Int(i64::MIN)),
        (b"9223372036854775807", Number::Int(i64::MAX)),
        (
            b"9223372036854775808",
            Number::BigInt("9223372036854775808".to_owned()),
        ),
        (
            b"-9223372036854775809",
            Number::BigInt("-9223372036854775809".to_owned()),
        ),
        (b"1E2", Number::Float(100.0)),
        (b"1e+2", Number::Float(100.0)),
        (b"1e-2", Number::Float(0.01)),
        (b"1e-400", Number::Float(0.0)),
    ];
    for (input, number) in cases {
        assert_eq!(parse(input), Ok(Value::Number(number)));
    }
    let Ok(Value::Number(Number::Float(negative_zero))) = parse(b"-0.0") else {
        panic!("-0.0 is a float");
    };
    assert!(negative_zero.is_sign_negative());
}

#[test]
fn decodes_escapes_and_surrogate_pairs() {
    let value =
        parse(b"\"\\\"\\\\\\/\\b\\f\\n\\r\\t\\u00e9\\uD83D\\ude00\\uFFFF\\u0000\"").unwrap();
    assert_eq!(
        value,
        string("\"\\/\u{8}\u{c}\n\r\t\u{e9}\u{1f600}\u{ffff}\u{0}")
    );
    assert_eq!(
        parse(b"\"\\ud800\\udc00\\udbff\\udfff\""),
        Ok(string("\u{10000}\u{10ffff}"))
    );
    assert_eq!(
        parse("\"\u{7f}\u{e9}\"".as_bytes()),
        Ok(string("\u{7f}\u{e9}"))
    );
}

#[test]
fn refuses_the_encoding_with_its_offset() {
    assert_eq!(parse(b"\xef\xbb\xbf1"), Err(Error::ByteOrderMark));
    assert_eq!(parse(b" \xef\xbb\xbf"), Err(Error::Syntax { offset: 1 }));
    assert_eq!(parse(b"\"ab\xff\""), Err(Error::InvalidUtf8 { offset: 3 }));
}

#[test]
fn refuses_syntax_with_its_offset() {
    let cases: [(&[u8], usize); 16] = [
        (b"", 0),
        (b"   ", 3),
        (b"tru", 0),
        (b"[1 2]", 3),
        (b"[1,]", 3),
        (b"{\"a\" 1}", 5),
        (b"{1:1}", 1),
        (b"{\"a\":1,}", 7),
        (b"\"abc", 4),
        (b"-", 1),
        (b"-a", 1),
        (b"1.", 2),
        (b"1.e5", 2),
        (b"1e+", 3),
        (b"1E", 2),
        (b"[", 1),
    ];
    for (input, offset) in cases {
        assert_eq!(parse(input), Err(Error::Syntax { offset }), "{input:?}");
    }
}

#[test]
fn refuses_string_content_with_its_offset() {
    let cases: [(&[u8], Error); 10] = [
        (b"\"a\x1f\"", Error::ControlCharacter { offset: 2 }),
        (b" \"a\\x\"", Error::InvalidEscape { offset: 3 }),
        (b"\"\\", Error::InvalidEscape { offset: 1 }),
        (b"\"\\u12g4\"", Error::InvalidEscape { offset: 1 }),
        (b"\"\\u12\"", Error::InvalidEscape { offset: 1 }),
        (b"\"\\ud800\\u12\"", Error::InvalidEscape { offset: 7 }),
        (b"\"a\\udc00\"", Error::LoneSurrogate { offset: 2 }),
        (b"\"\\ud800\"", Error::LoneSurrogate { offset: 1 }),
        (b"\"\\ud800\\ud800\"", Error::LoneSurrogate { offset: 1 }),
        (b"\"\\udbff\\ue000\"", Error::LoneSurrogate { offset: 1 }),
    ];
    for (input, error) in cases {
        assert_eq!(parse(input), Err(error), "{input:?}");
    }
}

#[test]
fn refuses_structure_and_numbers_with_their_offset() {
    assert_eq!(
        parse(br#"[{"a":1,"b":2,"a":3}]"#),
        Err(Error::DuplicateKey { offset: 1 })
    );
    assert_eq!(
        parse(b" [1e999]"),
        Err(Error::NonFiniteNumber { offset: 2 })
    );
    assert_eq!(parse(b"1 2"), Err(Error::TrailingData { offset: 2 }));
    let too_long = format!("[-{}]", "1".repeat(4301));
    assert_eq!(
        parse(too_long.as_bytes()),
        Err(Error::IntegerTooLong { offset: 1 })
    );
}

#[test]
fn nesting_stops_at_the_limit_whatever_the_siblings() {
    for (open, close) in [("[", "]"), ("{\"k\":", "}")] {
        let deepest = format!("{}0{}", open.repeat(MAX_DEPTH), close.repeat(MAX_DEPTH));
        assert!(parse(deepest.as_bytes()).is_ok());
        let deeper = format!(
            "{}0{}",
            open.repeat(MAX_DEPTH + 1),
            close.repeat(MAX_DEPTH + 1)
        );
        let offset = (MAX_DEPTH) * open.len();
        assert_eq!(parse(deeper.as_bytes()), Err(Error::TooDeep { offset }));
    }
    let deep = format!("{}{}", "[".repeat(MAX_DEPTH - 1), "]".repeat(MAX_DEPTH - 1));
    for sibling in ["[]", "{}", "[0]", "{\"k\":0}"] {
        let document = format!("[{sibling},{sibling},{deep}]");
        assert!(parse(document.as_bytes()).is_ok(), "{sibling}");
    }
}
