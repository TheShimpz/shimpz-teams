# team-protocol

The Team HTTP protocol v1 in Rust (ADR-0123, plan phase R2). Each module reproduces the Python module of the same name
in [`../v1`](../v1), which stays the authority while Team is Python: the same public constants by name, and validators
that take a strict JSON value (the vendored `strict-json` crate at `teams/vendor/strict-json`) and return exactly the
value Python returns, with its members in Python's order, or refuse it the way Python does (`None`, or a closed error
where Python raises by contract). It depends on `fancy-regex` and `unicode-normalization` (Unicode 16, as Python 3.14).

## Python semantics (`src/python/`)

- `text`: code-point `len`; `isspace`, `strip`, and `rstrip` with Python's whitespace (U+001C..U+001F and U+0085
  included); `category`; `isprintable`; the `re` word character; NFC; full `casefold`; `lower` with Final_Sigma.
  General_Category, case folding, lowercase mapping, the Final_Sigma context sets, and whitespace are tables
  `tables.py` generates from Python 3.14 itself (`python3 -I tables.py`; `src/python/tables/*.rs` are never edited);
  `tests/fixtures/unicode.txt` proves `unicode-normalization` 0.1.24 equal to Python on every code point.
- `json`: `json.dumps` byte for byte under `ensure_ascii`, `sort_keys`, and compact or default separators, integers
  exactly and floats as Python `repr`; `canonical` is the Shimpz canonical JSON v1 writer (sorted keys, compact,
  UTF-8 without escapes for non-ASCII), `teams/core/canonical_json.py`'s `encode`.
- `regex`: Python `re` patterns translated to `fancy-regex` with Python's `\w`, `\d`, `\s`, `\b`, `\B`, `$`, and `\Z`,
  run under a pinned backtracking limit (`BACKTRACK_LIMIT`, ten million steps); a search that cannot complete refuses
  the operation, never "no match". `fancy-regex` counts one search's steps across every start position it tries, so
  the limit also caps how much text a lookaround pattern scans: several hundred thousand characters for the phrase
  tables, far past the 16,000-character chat message, the longest text a reader is given (a 16,000-character
  adversarial sentence reads in under a second).
- `value`: Python's type tests and `==` over strict JSON values, and exact non-negative integer arithmetic.
- `datetime`: `datetime.datetime.fromisoformat` exactly as the C implementation of Python 3.14.

## Phrase readers (`src/phrase/`)

`phrase` reads its tables in Python `re` syntax through the `regex` translator; a captured count, hour, or day reads
its Unicode decimal digits of any script as Python's `int()`, and a zone is stated only as an exact member of the
pinned zone names. A monthly reading's `P.*?S` is searched as `P`, then the leftmost `S` after it, which is the match
Python finds, in time linear in the sentence where the whole pattern would be quadratic; each sentence's negations are
searched once per language. `zones.py` generates `src/phrase/generated.rs` (never edited) from this host's Python: every zone
name `routine.canonical_timezone` admits that `zoneinfo` loads, with the zone database version (`2026c`), which a Team
or Brain image must hold too, and the zero of every run of Unicode decimal digits.

## Web URLs and origins (`websocket`, `browser`, `sign_in`)

An Origin is printable ASCII without spaces, brackets, `?`, or `#`, and a Browser or sign-in URL's authority (after
`http://` or `https://` in any ASCII case, up to the first `/`, `?`, or `#`) is ASCII without brackets: Python refuses
the rest before `urllib.parse` reads it, so `websocket::authority`, `port`, and `scheme` split exactly as `urlsplit`
and `SplitResult` do without its Unicode lowering, NFKC check, whitespace stripping, or IPv6 validation.
`oracle/urls.py` generates the seeded URL corpus the URL validators' differential and `vectors.json`'s
`url_canonicalization` group draw from.

## Proof against the Python modules

- `tests/protocol/` is one test binary: every `vectors.json` group of the ported modules, checked as `verify.py`
  checks it with typed equality, the replayed corpus, and the edge cases no adapter reaches.
- `tests/protocol/support/registry/` names every validator and encoder by its Python path (`module.function`);
  `oracle/` holds the same names over the Python modules. `differential.py` draws seeded cases per entry (seeds from
  the vectors, changed by a generic mutator), decides them with Python after a strict JSON round trip and with the
  release `decide` example, and fails on any difference:
  `teams/.venv/bin/python -I teams/protocol/http/rust/differential.py --count 1000000`.
- `differential.py --corpus DRAWS` rewrites `tests/protocol/corpus/<module>.json`: each entry's seeds (only those
  adding an arc when an entry has more than 400) and every drawn case that executes a new line arc of the Python
  modules, with the Python line; `tests/protocol/corpus.rs` replays them. `tests/protocol/constants.rs` pins every
  integer constant to its Python value, and `tests/protocol/unicode.rs` checks `unicode-normalization` against
  `tests/fixtures/unicode.txt` on every code point.
- The tests read the Python protocol at `../v1` beside the crate; a copy of the crate, such as a mutation scratch
  tree, holds it at the same relative path.
- `fuzz/` runs every registry entry over arbitrary bytes decoded by `strict_json::parse`.

The Python files (`tables.py`, `zones.py`, `differential.py`, `oracle/`) are tooling, deleted with the Python
modules (plan phase R9).
