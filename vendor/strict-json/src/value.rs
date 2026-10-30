/// One decoded JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// A finite number.
    Number(Number),
    /// A string without unpaired surrogates.
    String(String),
    /// An array in document order.
    Array(Vec<Value>),
    /// An object's members in document order; field names are unique.
    Object(Vec<(String, Value)>),
}

/// A decoded JSON number, typed as Python's decoder types it.
#[derive(Clone, Debug, PartialEq)]
pub enum Number {
    /// An integer literal (no fraction or exponent) that fits in `i64`; `-0` is `Int(0)`.
    Int(i64),
    /// An integer literal outside `i64`, as its canonical decimal text with the sign of a negative value.
    BigInt(String),
    /// A literal with a fraction or exponent, correctly rounded; underflow gives a signed zero.
    Float(f64),
}

impl Value {
    /// Returns the value of the object member named `key`, or `None` for another key or a value that is not an object.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Self::Object(members) => members
                .iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value),
            _ => None,
        }
    }
}
