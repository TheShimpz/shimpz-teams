# strict-json

The neutral strict JSON admission every Shimpz backend decodes hostile input with (ADR-0123).
`strict_json::parse(&[u8])` admits only the documents described below and decodes them to their values; it has no
dependencies.

Admission: UTF-8 without a leading byte-order mark; whitespace only space, tab, LF, and CR; one value of any type and
nothing after it; no `NaN` or `Infinity`; numbers `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?` over ASCII digits;
no raw control characters in strings; only the escapes `\" \\ \/ \b \f \n \r \t \uXXXX`; surrogate escapes only as
high-low pairs; no field named twice in one object after decoding; arrays and objects nested at most 512 deep.

Values: objects keep document order. An integer literal is `Number::Int` when it fits `i64` (`-0` is `0`) and
`Number::BigInt` with its decimal text otherwise; more than 4,300 digits (Python's `sys.int_max_str_digits`, sign not
counted) is refused. A literal with a fraction or exponent is a correctly rounded `Number::Float`: overflow to
infinity is refused, underflow gives a zero that keeps its sign.

## Proof

- `vectors.json` holds every rule and boundary above plus 600 seeded corpus documents, each with its decision and the
  canonical representation of its value (`{"int": "..."}`, `{"float_bits": "<16 hex>"}`, `{"object": [[key, value],
  ...]}`, other values as themselves) as compact JSON text, so a case nested to the limit stays readable by this
  crate. `tests/vectors.rs` checks each case. The vectors are fixed data: a rule change edits them with the code.
- `fuzz/` holds the cargo-fuzz `parse` target: `cargo +nightly fuzz run parse`. Seed it from the vectors when its
  ignored corpus is empty.
