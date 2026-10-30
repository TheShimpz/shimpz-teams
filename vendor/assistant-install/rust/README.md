# assistant-install

The Rust form of the neutral Assistant-install contract v1 (ADR-0123), equal to the reference verifier
[`../v1/verify.py`](../v1/verify.py) and its JSON Schema subset [`../v1/schema_validator.py`](../v1/schema_validator.py).
Dependencies: [`strict-json`](../../strict-json), `fancy-regex` (schema patterns with lookarounds), and `sha2`.

- `Contract::load(definitions, resolve_response)` takes the exact bytes of the two pinned schema documents, requires
  Draft 2020-12 under each canonical `$id`, and checks every node against the subset: supported keywords only, every
  reference resolved, every pattern compiled. A consumer passes the schemas of its own pinned mirror.
- `Contract::validate(value)` decides one decoded resolve response: the schema, then `resolve::validate`, the image,
  catalog, file-input, Integration, and Stored Input rules, answering the reference's stable code
  (`schema_violation`, `resolve_*_mismatch`, `resolve_stored_input_placement`). Each rule is public, so a consumer that
  keeps stricter rules of its own for one step composes the others unchanged.

The one deliberate difference from the Python reference: a pattern's `$` matches only at the end of the string, as
JSON Schema's ECMA-262 dialect and the Developers producer read it, never before a final line feed.

`tests/vectors.rs` decides every case of `../v1/vectors.json` and requires its exact code.
