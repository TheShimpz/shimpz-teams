# strict-json

The Rust form of the neutral strict JSON admission (ADR-0123). `strict_json::parse(&[u8])` admits exactly the
documents that the Python oracle [`../strict_json.py`](../strict_json.py) `loads` admits and decodes them to the same
values; it has no dependencies.

Admission: UTF-8 without a leading byte-order mark; whitespace only space, tab, LF, and CR; one value of any type and
nothing after it; no `NaN` or `Infinity`; numbers `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?` over ASCII digits;
no raw control characters in strings; only the escapes `\" \\ \/ \b \f \n \r \t \uXXXX`; surrogate escapes only as
high-low pairs; no field named twice in one object after decoding; arrays and objects nested at most 512 deep.

Values: objects keep document order. An integer literal is `Number::Int` when it fits `i64` (`-0` is `0`) and
`Number::BigInt` with its decimal text otherwise; more than 4,300 digits (Python's `sys.int_max_str_digits`, sign not
counted) is refused. A literal with a fraction or exponent is a correctly rounded `Number::Float`: overflow to
infinity is refused, underflow gives a zero that keeps its sign.

## Proof against the oracle

- `vectors.json` holds every rule and boundary above plus 600 seeded corpus documents, each with the oracle's
  decision and the canonical representation of its value (`{"int": "..."}`, `{"float_bits": "<16 hex>"}`,
  `{"object": [[key, value], ...]}`, other values as themselves) as compact JSON text, so a case nested to the limit
  stays readable by this crate. `tests/vectors.rs` checks each case. Regenerate after changing the oracle or the
  corpus with `python3 -I vectors.py`.
- `differential.py` draws `--count` documents (default 20,000) from `--seed` with the same generator, decides them
  with the oracle, runs the release `decide` example once over all of them, and exits non-zero on any disagreement:
  `python3 -I differential.py --count 1000000`.
- `fuzz/` holds the cargo-fuzz `parse` target: `cargo +nightly fuzz run parse`. Seed it from the vectors when its
  ignored corpus is empty.

The generator is a seeded SplitMix64, not Python's `random`, so a seed names the same corpus on every Python version.
The Python files are tooling and are deleted with the oracle (plan phase R9).
