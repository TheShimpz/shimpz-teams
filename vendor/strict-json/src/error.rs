use std::fmt;

/// Why a document was refused. Offsets count bytes from the start of the input; no variant carries input text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The input starts with the UTF-8 byte-order mark.
    ByteOrderMark,
    /// The input is not UTF-8; `offset` is the length of its longest valid prefix.
    InvalidUtf8 {
        /// First byte that is not part of a valid UTF-8 sequence.
        offset: usize,
    },
    /// The bytes at `offset` do not continue the JSON grammar; `offset` equals the input length when it ended early.
    Syntax {
        /// Position of the unexpected byte or the end of the input.
        offset: usize,
    },
    /// A string holds a raw character below U+0020.
    ControlCharacter {
        /// Position of the control character.
        offset: usize,
    },
    /// A backslash starts an escape that is not one of `\" \\ \/ \b \f \n \r \t \uXXXX`.
    InvalidEscape {
        /// Position of the backslash.
        offset: usize,
    },
    /// A `\u` escape names a surrogate that is not half of a high-low escape pair.
    LoneSurrogate {
        /// Position of the backslash of the unpaired escape.
        offset: usize,
    },
    /// An object names the same field twice after decoding.
    DuplicateKey {
        /// Position of the object's opening brace.
        offset: usize,
    },
    /// A number with a fraction or exponent overflows to infinity.
    NonFiniteNumber {
        /// Position of the number.
        offset: usize,
    },
    /// An integer literal has more than [`crate::MAX_INTEGER_DIGITS`] digits.
    IntegerTooLong {
        /// Position of the number.
        offset: usize,
    },
    /// Arrays and objects nest deeper than [`crate::MAX_DEPTH`].
    TooDeep {
        /// Position of the opening bracket or brace that is one level too deep.
        offset: usize,
    },
    /// Something other than whitespace follows the document's value.
    TrailingData {
        /// Position of the first byte after the value and its whitespace.
        offset: usize,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            Self::ByteOrderMark => {
                formatter.write_str("JSON must not start with a byte-order mark")
            }
            Self::InvalidUtf8 { offset } => write!(formatter, "JSON is not UTF-8 at byte {offset}"),
            Self::Syntax { offset } => write!(formatter, "invalid JSON syntax at byte {offset}"),
            Self::ControlCharacter { offset } => {
                write!(
                    formatter,
                    "control character in JSON string at byte {offset}"
                )
            }
            Self::InvalidEscape { offset } => {
                write!(formatter, "invalid JSON escape at byte {offset}")
            }
            Self::LoneSurrogate { offset } => {
                write!(formatter, "lone surrogate in JSON string at byte {offset}")
            }
            Self::DuplicateKey { offset } => write!(
                formatter,
                "duplicate JSON field in the object at byte {offset}"
            ),
            Self::NonFiniteNumber { offset } => {
                write!(formatter, "non-finite JSON number at byte {offset}")
            }
            Self::IntegerTooLong { offset } => {
                write!(formatter, "JSON integer is too long at byte {offset}")
            }
            Self::TooDeep { offset } => {
                write!(formatter, "JSON nesting is too deep at byte {offset}")
            }
            Self::TrailingData { offset } => {
                write!(formatter, "data after the JSON value at byte {offset}")
            }
        }
    }
}

impl std::error::Error for Error {}
