//! A recursive-descent reader whose recursion is bounded by [`MAX_DEPTH`].

mod number;
mod string;

use crate::{Error, MAX_DEPTH, Value};

pub(crate) struct Parser<'a> {
    text: &'a str,
    bytes: &'a [u8],
    pos: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    pub(crate) fn new(text: &'a str) -> Self {
        Self {
            text,
            bytes: text.as_bytes(),
            pos: 0,
            depth: 0,
        }
    }

    pub(crate) fn document(mut self) -> Result<Value, Error> {
        self.skip_whitespace();
        let value = self.value()?;
        self.skip_whitespace();
        if self.pos == self.bytes.len() {
            Ok(value)
        } else {
            Err(Error::TrailingData { offset: self.pos })
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn syntax(&self) -> Error {
        Error::Syntax { offset: self.pos }
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    /// Consumes `byte` when it is next.
    fn eat(&mut self, byte: u8) -> bool {
        let found = self.peek() == Some(byte);
        if found {
            self.pos += 1;
        }
        found
    }

    fn value(&mut self) -> Result<Value, Error> {
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string().map(Value::String),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(b'-' | b'0'..=b'9') => self.number().map(Value::Number),
            _ => Err(self.syntax()),
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, Error> {
        if self.text[self.pos..].starts_with(word) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(self.syntax())
        }
    }

    /// Opens an array or object at the current bracket, one level deeper.
    fn enter(&mut self) -> Result<(), Error> {
        if self.depth == MAX_DEPTH {
            return Err(Error::TooDeep { offset: self.pos });
        }
        self.depth += 1;
        self.pos += 1;
        self.skip_whitespace();
        Ok(())
    }

    /// Reads the separator after a member: `true` when `close` ended the container, `false` after a comma.
    fn separator(&mut self, close: u8) -> Result<bool, Error> {
        self.skip_whitespace();
        if self.eat(close) {
            self.depth -= 1;
            return Ok(true);
        }
        if !self.eat(b',') {
            return Err(self.syntax());
        }
        self.skip_whitespace();
        Ok(false)
    }

    fn array(&mut self) -> Result<Value, Error> {
        self.enter()?;
        let mut items = Vec::new();
        if self.eat(b']') {
            self.depth -= 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value()?);
            if self.separator(b']')? {
                return Ok(Value::Array(items));
            }
        }
    }

    fn object(&mut self) -> Result<Value, Error> {
        let start = self.pos;
        self.enter()?;
        let mut members = Vec::new();
        if self.eat(b'}') {
            self.depth -= 1;
            return Ok(Value::Object(members));
        }
        loop {
            members.push(self.member()?);
            if self.separator(b'}')? {
                return unique(members, start);
            }
        }
    }

    fn member(&mut self) -> Result<(String, Value), Error> {
        if self.peek() != Some(b'"') {
            return Err(self.syntax());
        }
        let key = self.string()?;
        self.skip_whitespace();
        if !self.eat(b':') {
            return Err(self.syntax());
        }
        self.skip_whitespace();
        Ok((key, self.value()?))
    }
}

/// Refuses an object that names a field twice; sorting keeps a large object at O(n log n).
fn unique(members: Vec<(String, Value)>, offset: usize) -> Result<Value, Error> {
    let mut keys: Vec<&str> = members.iter().map(|(key, _)| key.as_str()).collect();
    keys.sort_unstable();
    if keys.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(Error::DuplicateKey { offset });
    }
    Ok(Value::Object(members))
}

#[cfg(test)]
mod tests;
