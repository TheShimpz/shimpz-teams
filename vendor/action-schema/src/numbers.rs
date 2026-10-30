//! Python's numeric semantics over decoded JSON numbers: exact comparison between integers of any size and floats,
//! integer conversion to float with its overflow, and `jsonschema`'s `multipleOf` arithmetic.

use std::cmp::Ordering;

use num_bigint::BigInt;
use num_traits::{FromPrimitive, Zero};
use strict_json::Number;

/// Python's arithmetic raised: an integer too large for a float met a float (`OverflowError`), or a divisor was zero.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Overflow;

/// A number as Python holds it: an integer of any size, or a float.
enum Exact {
    Integer(BigInt),
    Float(f64),
}

fn exact(number: &Number) -> Exact {
    match number {
        Number::Int(value) => Exact::Integer(BigInt::from(*value)),
        Number::BigInt(digits) => Exact::Integer(digits.parse().unwrap_or_default()),
        Number::Float(float) => Exact::Float(*float),
    }
}

/// The exact integer an integral float holds.
fn integral(float: f64) -> BigInt {
    BigInt::from_f64(float).unwrap_or_default()
}

/// Python's exact comparison of two numbers, `bool` aside.
pub(crate) fn compare(left: &Number, right: &Number) -> Ordering {
    match (exact(left), exact(right)) {
        (Exact::Float(left), Exact::Float(right)) => {
            left.partial_cmp(&right).unwrap_or(Ordering::Equal)
        }
        (Exact::Float(float), Exact::Integer(integer)) => compare_float(&integer, float).reverse(),
        (Exact::Integer(integer), Exact::Float(float)) => compare_float(&integer, float),
        (Exact::Integer(left), Exact::Integer(right)) => left.cmp(&right),
    }
}

/// Compares an integer with a finite float exactly.
fn compare_float(integer: &BigInt, float: f64) -> Ordering {
    let floor = integral(float.floor());
    match integer.cmp(&floor) {
        Ordering::Equal if float.fract() != 0.0 => Ordering::Less,
        ordering => ordering,
    }
}

/// Compares a count, such as a length, with a schema bound exactly.
pub(crate) fn compare_count(count: usize, bound: &Number) -> Ordering {
    let count = Number::BigInt(count.to_string());
    compare(&count, bound)
}

/// Whether two numbers are equal as Python compares them: `1 == 1.0`.
pub(crate) fn equal(left: &Number, right: &Number) -> bool {
    compare(left, right) == Ordering::Equal
}

/// Whether a number is an integer as `jsonschema` types one: any integer, or a float without a fraction.
pub(crate) fn is_integer(number: &Number) -> bool {
    match number {
        Number::Int(_) | Number::BigInt(_) => true,
        Number::Float(float) => float.fract() == 0.0,
    }
}

/// Python's conversion of a number to float: an integer correctly rounded, or `OverflowError` beyond the float range.
fn to_float(number: &Number) -> Result<f64, Overflow> {
    let float = match number {
        Number::Float(float) => *float,
        // Decimal parsing rounds to the nearest float, ties to even, as Python's conversion does.
        Number::Int(value) => value.to_string().parse().unwrap_or(f64::INFINITY),
        Number::BigInt(digits) => digits.parse().unwrap_or(f64::INFINITY),
    };
    if float.is_finite() {
        Ok(float)
    } else {
        Err(Overflow)
    }
}

/// Whether `jsonschema`'s `multipleOf` refuses `instance` for `divisor`: a float divisor divides the float quotient
/// (exactly, with fractions, once that quotient overflows), and an integer divisor takes the remainder.
pub(crate) fn not_multiple(instance: &Number, divisor: &Number) -> Result<bool, Overflow> {
    match exact(divisor) {
        Exact::Float(0.0) => Err(Overflow),
        Exact::Float(divisor) => {
            let quotient = to_float(instance)? / divisor;
            Ok(if quotient.is_finite() {
                quotient.fract() != 0.0
            } else {
                !divides_exactly(instance, divisor)
            })
        }
        Exact::Integer(divisor) if divisor.is_zero() => Err(Overflow),
        Exact::Integer(divisor) => match exact(instance) {
            Exact::Float(float) => {
                Ok(float % to_float(&Number::BigInt(divisor.to_string()))? != 0.0)
            }
            Exact::Integer(integer) => Ok(!(integer % divisor).is_zero()),
        },
    }
}

/// A finite float's magnitude as an integer times a power of two; divisibility never depends on the sign.
fn binary(float: f64) -> (BigInt, i64) {
    let bits = float.to_bits();
    let exponent = i64::try_from((bits >> 52) & 0x7ff).unwrap_or_default();
    let fraction = bits & ((1 << 52) - 1);
    if exponent == 0 {
        (BigInt::from(fraction), -1074)
    } else {
        (BigInt::from(fraction + (1 << 52)), exponent - 1075)
    }
}

/// Whether `instance / divisor` is an integer, computed with exact fractions.
fn divides_exactly(instance: &Number, divisor: f64) -> bool {
    let (numerator, power) = match exact(instance) {
        Exact::Float(float) => binary(float),
        Exact::Integer(integer) => (integer, 0),
    };
    let (denominator, divisor_power) = binary(divisor);
    let shift = power - divisor_power;
    let magnitude = usize::try_from(shift.unsigned_abs()).unwrap_or(usize::MAX);
    let (numerator, denominator) = if shift >= 0 {
        (numerator << magnitude, denominator)
    } else {
        (numerator, denominator << magnitude)
    };
    (numerator % denominator).is_zero()
}

#[cfg(test)]
mod tests;
