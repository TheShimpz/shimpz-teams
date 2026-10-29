//! Strings: raw runs, the closed escape set, and surrogate pairs.

use super::Parser;
use crate::Error;

const HIGH_SURROGATES: std::ops::Range<u32> = 0xD800..0xDC00;
const LOW_SURROGATES: std::ops::Range<u32> = 0xDC00..0xE000;

impl Parser<'_> {
    /// Reads the string whose opening quote is the current byte.
    pub(super) fn string(&mut self) -> Result<String, Error> {
        self.pos += 1;
        let mut decoded = String::new();
        loop {
            let start = self.pos;
            while let Some(byte) = self.peek()
                && byte != b'"'
                && byte != b'\\'
                && byte >= 0x20
            {
                self.pos += 1;
            }
            // The run stops only at ASCII bytes, so both ends are character boundaries.
            decoded.push_str(&self.text[start..self.pos]);
            match self.peek() {
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(decoded);
                }
                Some(b'\\') => decoded.push(self.escape()?),
                Some(_) => return Err(Error::ControlCharacter { offset: self.pos }),
                None => return Err(self.syntax()),
            }
        }
    }

    /// Decodes the escape whose backslash is the current byte.
    fn escape(&mut self) -> Result<char, Error> {
        let at = self.pos;
        self.pos += 2;
        match self.bytes.get(at + 1) {
            Some(b'"') => Ok('"'),
            Some(b'\\') => Ok('\\'),
            Some(b'/') => Ok('/'),
            Some(b'b') => Ok('\u{8}'),
            Some(b'f') => Ok('\u{c}'),
            Some(b'n') => Ok('\n'),
            Some(b'r') => Ok('\r'),
            Some(b't') => Ok('\t'),
            Some(b'u') => self.unicode(at),
            _ => Err(Error::InvalidEscape { offset: at }),
        }
    }

    /// Decodes the `\uXXXX` escape at `at`, combining a high surrogate with the low surrogate escape that follows.
    fn unicode(&mut self, at: usize) -> Result<char, Error> {
        let unit = self.hex4(at)?;
        let code = if HIGH_SURROGATES.contains(&unit) {
            0x1_0000
                + ((unit - HIGH_SURROGATES.start) << 10)
                + (self.low_surrogate(at)? - LOW_SURROGATES.start)
        } else {
            unit
        };
        // Only a lone low surrogate is left without a character here.
        char::from_u32(code).ok_or(Error::LoneSurrogate { offset: at })
    }

    fn low_surrogate(&mut self, at: usize) -> Result<u32, Error> {
        if self.bytes[self.pos..].starts_with(b"\\u") {
            self.pos += 2;
            let unit = self.hex4(self.pos - 2)?;
            if LOW_SURROGATES.contains(&unit) {
                return Ok(unit);
            }
        }
        Err(Error::LoneSurrogate { offset: at })
    }

    /// Reads four hexadecimal digits of either case for the escape at `at`.
    fn hex4(&mut self, at: usize) -> Result<u32, Error> {
        let digits = self.bytes.get(self.pos..self.pos + 4);
        let unit = digits.and_then(|digits| {
            digits.iter().try_fold(0, |unit, &digit| {
                char::from(digit)
                    .to_digit(16)
                    .map(|value| unit * 16 + value)
            })
        });
        self.pos += 4;
        unit.ok_or(Error::InvalidEscape { offset: at })
    }
}
