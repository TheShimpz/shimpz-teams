use std::cmp::Ordering;

use strict_json::{Number, Value};

use super::{
    array, eq_int, eq_str, exact_object, field, has_exactly, int, is_bool, is_int, is_int_like,
    member, natural_cmp, natural_digits, natural_sub, object, object_of, py_eq, small_int,
    sorted_unique, strictly_sorted, string, text, whole,
};

fn big(digits: &str) -> Value {
    Value::Number(Number::BigInt(digits.to_owned()))
}

fn float(value: f64) -> Value {
    Value::Number(Number::Float(value))
}

#[test]
fn type_tests_follow_python() {
    let checks = [
        is_int(&int(1)) && is_int(&big("18446744073709551616")),
        !is_int(&Value::Bool(true)) && !is_int(&float(1.0)),
        is_int_like(&Value::Bool(false)) && is_int_like(&int(0)) && !is_int_like(&Value::Null),
        is_bool(&Value::Bool(true)) && !is_bool(&int(1)),
        small_int(&int(-3)) == Some(-3) && small_int(&big("18446744073709551616")).is_none(),
        whole(&int(2), 1, 2) && !whole(&int(3), 1, 2) && !whole(&Value::Bool(true), 0, 1),
        string(&text("a")) == Some("a")
            && array(&Value::Array(vec![])).map(<[Value]>::len) == Some(0),
        object(&Value::Null).is_none()
            && array(&Value::Null).is_none()
            && string(&int(1)).is_none(),
    ];
    for (index, check) in checks.into_iter().enumerate() {
        assert!(check, "check {index}");
    }
}

#[test]
fn members_follow_python_dicts() {
    let value = object_of(vec![("b", int(1)), ("a", Value::Null)]);
    let members = object(&value).expect("an object");
    assert!(
        has_exactly(members, &["a", "b"])
            && !has_exactly(members, &["a"])
            && !has_exactly(members, &["a", "c"])
    );
    assert!(exact_object(&value, &["b", "a"]).is_some() && exact_object(&int(1), &[]).is_none());
    assert_eq!(field(members, "b"), &int(1));
    assert_eq!(field(members, "z"), &Value::Null);
    assert_eq!(member(&value, "b"), Some(&int(1)));
    assert!(eq_str(&text("x"), "x") && !eq_str(&int(1), "1"));
}

#[test]
fn equality_follows_python_across_numbers() {
    for (left, right) in &equal_pairs() {
        assert!(py_eq(left, right), "{left:?} == {right:?}");
    }
    for (left, right) in &different_pairs() {
        assert!(!py_eq(left, right), "{left:?} != {right:?}");
    }
    assert!(eq_int(&float(3.0), 3) && !eq_int(&Value::Null, 0));
}

fn equal_pairs() -> [(Value, Value); 10] {
    [
        (Value::Bool(true), int(1)),
        (int(1), float(1.0)),
        (float(-0.0), int(0)),
        (
            big("18446744073709551616"),
            float(18_446_744_073_709_551_616.0),
        ),
        (
            float(18_446_744_073_709_551_616.0),
            big("18446744073709551616"),
        ),
        (big("-18446744073709551617"), big("-18446744073709551617")),
        (float(0.5), float(0.5)),
        (Value::Null, Value::Null),
        (
            Value::Array(vec![int(1), text("a")]),
            Value::Array(vec![float(1.0), text("a")]),
        ),
        (
            object_of(vec![("a", int(1)), ("b", int(2))]),
            object_of(vec![("b", float(2.0)), ("a", Value::Bool(true))]),
        ),
    ]
}

fn different_pairs() -> [(Value, Value); 13] {
    [
        (int(1), big("18446744073709551616")),
        (big("18446744073709551616"), int(1)),
        (big("1"), big("2")),
        (float(0.5), int(0)),
        (
            big("18446744073709551617"),
            float(18_446_744_073_709_551_616.0),
        ),
        (float(1.0), float(2.0)),
        (text("1"), int(1)),
        (Value::Null, Value::Bool(false)),
        (Value::Array(vec![int(1)]), Value::Array(vec![])),
        (Value::Array(vec![int(1)]), Value::Array(vec![int(2)])),
        (
            object_of(vec![("a", int(1))]),
            object_of(vec![("b", int(1))]),
        ),
        (
            object_of(vec![("a", int(1))]),
            object_of(vec![("a", int(2))]),
        ),
        (object_of(vec![("a", int(1))]), object_of(vec![])),
    ]
}

#[test]
fn natural_digits_are_non_negative_integers() {
    let values = [
        int(12),
        int(-1),
        big("18446744073709551616"),
        big("-18446744073709551616"),
        Value::Bool(true),
    ];
    let digits: Vec<Option<String>> = values.iter().map(natural_digits).collect();
    let expected = [Some("12"), None, Some("18446744073709551616"), None, None];
    assert_eq!(digits, expected.map(|found| found.map(str::to_owned)));
}

#[test]
fn natural_arithmetic_is_exact() {
    let comparisons = [
        natural_cmp("9", "10"),
        natural_cmp("19", "10"),
        natural_cmp("10", "10"),
    ];
    assert_eq!(
        comparisons,
        [Ordering::Less, Ordering::Greater, Ordering::Equal]
    );
    let differences = [
        ("100", "1", "99"),
        ("18446744073709551616", "18446744073709551616", "0"),
        ("1000000000000000000000", "1", "999999999999999999999"),
    ];
    for (left, right, difference) in differences {
        assert_eq!(natural_sub(left, right), difference);
    }
}

#[test]
fn string_order_is_code_point_order() {
    assert_eq!(
        sorted_unique(&["b", "a", "b", "\u{ffff}", "\u{10000}"]),
        ["a", "b", "\u{ffff}", "\u{10000}"]
    );
    assert!(strictly_sorted(&["a", "b"]) && !strictly_sorted(&["a", "a"]) && strictly_sorted(&[]));
}
