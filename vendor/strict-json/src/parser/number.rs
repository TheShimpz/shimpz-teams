//! Numbers: the JSON grammar over ASCII digits, typed as Python types them.

use super::Parser;
use crate::{Error, MAX_INTEGER_DIGITS, Number};

impl Parser<'_> {
    /// Reads `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?` at the current byte.
    pub(super) fn number(&mut self) -> Result<Number, Error> {
        let start = self.pos;
        self.eat(b'-');
        if !self.eat(b'0') {
            self.required_digits()?;
        }
        let integral = self.pos;
        if self.eat(b'.') {
            self.required_digits()?;
        }
        if self.eat(b'e') || self.eat(b'E') {
            if !self.eat(b'+') {
                self.eat(b'-');
            }
            self.required_digits()?;
        }
        let literal = &self.text[start..self.pos];
        if self.pos == integral {
            integer(literal, start)
        } else {
            float(literal, start)
        }
    }

    fn required_digits(&mut self) -> Result<(), Error> {
        let start = self.pos;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        if self.pos == start {
            Err(self.syntax())
        } else {
            Ok(())
        }
    }
}

fn integer(literal: &str, offset: usize) -> Result<Number, Error> {
    let digits = literal.strip_prefix('-').unwrap_or(literal);
    if digits.len() > MAX_INTEGER_DIGITS {
        return Err(Error::IntegerTooLong { offset });
    }
    // The grammar admits only digits, so a failed `i64` parse is an overflow: the literal is already canonical.
    Ok(literal
        .parse()
        .map_or_else(|_| Number::BigInt(literal.to_owned()), Number::Int))
}

fn float(literal: &str, offset: usize) -> Result<Number, Error> {
    literal
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite())
        .map(Number::Float)
        .ok_or(Error::NonFiniteNumber { offset })
}
