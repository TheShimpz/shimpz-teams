# team-protocol

The Team HTTP protocol v1 (ADR-0123): the closed identifiers, payload projections, Routine forms, Supervisor assertion
contract, and WebSocket frame boundary Team, Admin, the Brain, and the Store share. Each module keeps the public
constants by name, and validators take a strict JSON value (the vendored `strict-json` crate at
`teams/vendor/strict-json`) and return the canonical value, with its members in order, or refuse it (`None`, or a
closed error by contract). The protocol was first written in Python; the Rust crate reproduces those semantics exactly
and is now the only implementation. It depends on `fancy-regex` and `unicode-normalization` (Unicode 16).

## Python semantics (`src/python/`)

- `text`: code-point `len`; `isspace`, `strip`, and `rstrip` with Python's whitespace (U+001C..U+001F and U+0085
  included); `category`; `isprintable`; the `re` word character; NFC; full `casefold`; `lower` with Final_Sigma.
  General_Category, case folding, lowercase mapping, the Final_Sigma context sets, and whitespace are tables generated
  from Python 3.14 (`src/python/tables/*.rs`, fixed data); `tests/fixtures/unicode.txt` pins `unicode-normalization`
  0.1.24 to Python 3.14 on every code point.
- `json`: `json.dumps` byte for byte under `ensure_ascii`, `sort_keys`, and compact or default separators, integers
  exactly and floats as Python `repr`; `canonical` is the Shimpz canonical JSON v1 writer (sorted keys, compact,
  UTF-8 without escapes for non-ASCII).
- `regex`: Python `re` patterns translated to `fancy-regex` with Python's `\w`, `\d`, `\s`, `\b`, `\B`, `$`, and `\Z`,
  run under a pinned backtracking limit (`BACKTRACK_LIMIT`, ten million steps); a search that cannot complete refuses
  the operation, never "no match". `fancy-regex` counts one search's steps across every start position it tries, so
  the limit also caps how much text a lookaround pattern scans: several hundred thousand characters for the phrase
  tables, far past the 16,000-character chat message, the longest text a reader is given (a 16,000-character
  adversarial sentence reads in under a second).
- `value`: Python's type tests and `==` over strict JSON values, and exact non-negative integer arithmetic.
- `datetime`: `datetime.datetime.fromisoformat` exactly as the C implementation of Python 3.14.
- `base64`: the padded encoding `base64` writes in the standard and URL-safe alphabets, and decoding of text a
  grammar already admitted.

## Phrase readers (`src/phrase/`)

`phrase` reads its tables in Python `re` syntax through the `regex` translator; a captured count, hour, or day reads
its Unicode decimal digits of any script as Python's `int()`, and a zone is stated only as an exact member of the
pinned zone names. A monthly reading's `P.*?S` is searched as `P`, then the leftmost `S` after it, which is the match
Python finds, in time linear in the sentence where the whole pattern would be quadratic; each sentence's negations are
searched once per language. `src/phrase/generated.rs` (fixed data, generated from Python's `zoneinfo`) holds every zone
name `routine.canonical_timezone` admits, with the zone database version (`2026c`), which a Team or Brain image must
hold too, and the zero of every run of Unicode decimal digits.

## Web URLs and origins (`websocket`, `browser`, `sign_in`)

An Origin is printable ASCII without spaces, brackets, `?`, or `#`, and a Browser or sign-in URL's authority (after
`http://` or `https://` in any ASCII case, up to the first `/`, `?`, or `#`) is ASCII without brackets: the rest is
refused before it is split, so `websocket::authority`, `port`, and `scheme` split exactly as `urlsplit`
and `SplitResult` do without its Unicode lowering, NFKC check, whitespace stripping, or IPv6 validation.
`vectors.json`'s `url_canonicalization` group pins them over a seeded corpus.

## Proof

- `tests/protocol/` is one test binary: every `vectors.json` group of [`../v1`](../v1) with typed equality, the
  replayed corpus, and the edge cases no adapter reaches.
- `tests/protocol/support/registry/` names every validator and encoder by its module path (`module.function`).
  `tests/protocol/corpus/<module>.json` holds the cases recorded from the Python modules before they were retired, each
  with the answer they gave; `tests/protocol/corpus.rs` replays them. `tests/protocol/constants.rs` pins every integer
  constant, and `tests/protocol/unicode.rs` checks `unicode-normalization` against `tests/fixtures/unicode.txt` on every
  code point. The vectors, the corpus, and the generated tables are fixed data: a rule change edits them with the code.
- The tests read `../v1/vectors.json` beside the crate; a copy of the crate, such as a mutation scratch tree, holds it at
  the same relative path.
- `fuzz/` runs every registry entry over arbitrary bytes decoded by `strict_json::parse`.
