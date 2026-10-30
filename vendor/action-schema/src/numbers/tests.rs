use std::cmp::Ordering;

use strict_json::Number;

use super::{Overflow, compare, compare_count, divides_exactly, equal, is_integer, not_multiple};

fn big(digits: &str) -> Number {
    Number::BigInt(digits.to_owned())
}

#[test]
fn integers_and_floats_compare_exactly() {
    let cases = [
        (Number::Int(1), Number::Float(1.0), Ordering::Equal),
        (Number::Float(-0.0), Number::Int(0), Ordering::Equal),
        (Number::Float(0.5), Number::Float(0.25), Ordering::Greater),
        (Number::Int(2), Number::Float(2.5), Ordering::Less),
        (Number::Int(3), Number::Float(2.5), Ordering::Greater),
        (Number::Float(2.5), Number::Int(2), Ordering::Greater),
        (Number::Int(-3), Number::Float(-2.5), Ordering::Less),
        (Number::Int(-2), Number::Float(-2.5), Ordering::Greater),
        (
            big("9007199254740993"),
            Number::Float(9_007_199_254_740_992.0),
            Ordering::Greater,
        ),
        (
            big("-18446744073709551617"),
            Number::Int(i64::MIN),
            Ordering::Less,
        ),
        (Number::Int(7), Number::Int(7), Ordering::Equal),
    ];
    for (left, right, expected) in cases {
        assert_eq!(compare(&left, &right), expected, "{left:?} {right:?}");
    }
    assert!(equal(&Number::Int(1), &Number::Float(1.0)));
    assert!(!equal(&Number::Int(1), &Number::Float(1.5)));
}

#[test]
fn counts_compare_with_any_bound() {
    assert_eq!(compare_count(2, &Number::Float(1.5)), Ordering::Greater);
    assert_eq!(compare_count(1, &Number::Float(1.0)), Ordering::Equal);
    assert_eq!(
        compare_count(3, &big("100000000000000000000")),
        Ordering::Less
    );
    assert_eq!(compare_count(0, &Number::Int(-1)), Ordering::Greater);
}

#[test]
fn an_integral_float_is_an_integer() {
    assert!(is_integer(&Number::Int(5)));
    assert!(is_integer(&big("100000000000000000000")));
    assert!(is_integer(&Number::Float(1e300)));
    assert!(!is_integer(&Number::Float(0.5)));
}

#[test]
fn a_float_divisor_divides_the_float_quotient() {
    assert_eq!(
        not_multiple(&Number::Float(1.5), &Number::Float(0.5)),
        Ok(false)
    );
    assert_eq!(
        not_multiple(&Number::Float(0.3), &Number::Float(0.1)),
        Ok(true)
    );
    assert_eq!(
        not_multiple(&Number::Int(7), &Number::Float(0.5)),
        Ok(false)
    );
    assert_eq!(
        not_multiple(&Number::Int(-7), &Number::Float(2.0)),
        Ok(true)
    );
    assert_eq!(
        not_multiple(&big("12345678901234567890"), &Number::Float(1.0)),
        Ok(false)
    );
}

#[test]
fn an_overflowing_quotient_divides_with_exact_fractions() {
    // 1e308 / 5e-324 overflows; 1e308 is an integer and 5e-324 is 2^-1074, so it divides.
    assert_eq!(
        not_multiple(&Number::Float(1e308), &Number::Float(5e-324)),
        Ok(false)
    );
    assert_eq!(
        not_multiple(&Number::Float(-1e308), &Number::Float(5e-324)),
        Ok(false)
    );
    // 1e-300 is no power of two, so 1e10 is no multiple of it.
    assert_eq!(
        not_multiple(&Number::Float(1e10), &Number::Float(1e-300)),
        Ok(true)
    );
    // An integer instance shifts the divisor's denominator instead.
    assert_eq!(
        not_multiple(&Number::Int(3), &Number::Float(5e-324)),
        Ok(false)
    );
    assert_eq!(not_multiple(&big("1"), &Number::Float(1e-310)), Ok(true));
    // A subnormal instance against a subnormal divisor.
    assert_eq!(
        not_multiple(&Number::Float(1e-310), &Number::Float(5e-324)),
        Ok(false)
    );
}

#[test]
fn an_integer_divisor_takes_the_remainder() {
    assert_eq!(not_multiple(&Number::Int(9), &Number::Int(3)), Ok(false));
    assert_eq!(
        not_multiple(&big("1000000000000000000000000000000"), &Number::Int(3)),
        Ok(true)
    );
    assert_eq!(
        not_multiple(&Number::Float(4.0), &Number::Int(2)),
        Ok(false)
    );
    assert_eq!(not_multiple(&Number::Float(4.5), &Number::Int(2)), Ok(true));
}

#[test]
fn arithmetic_beyond_the_float_range_raises() {
    let huge = format!("1{}", "0".repeat(400));
    assert_eq!(
        not_multiple(&big(&huge), &Number::Float(0.5)),
        Err(Overflow)
    );
    assert_eq!(
        not_multiple(&Number::Float(1.5), &big(&huge)),
        Err(Overflow)
    );
    assert_eq!(
        not_multiple(&Number::Int(1), &Number::Float(0.0)),
        Err(Overflow)
    );
    assert_eq!(
        not_multiple(&Number::Int(1), &Number::Int(0)),
        Err(Overflow)
    );
    assert_eq!(
        not_multiple(&Number::Float(1.5), &Number::Int(i64::MAX)),
        Ok(true)
    );
}

#[test]
fn exact_division_shifts_whichever_side_has_the_smaller_power() {
    assert!(!divides_exactly(&Number::Float(1.0), 4.0));
    assert!(divides_exactly(&Number::Float(8.0), 4.0));
    assert!(divides_exactly(&Number::Int(6), 1.5));
    assert!(!divides_exactly(&Number::Int(5), 1.5));
    assert!(!divides_exactly(&Number::Float(-0.75), 0.5));
    assert!(divides_exactly(&Number::Float(-1.0), 0.5));
    assert!(divides_exactly(&Number::Int(-6), 1.5));
}
