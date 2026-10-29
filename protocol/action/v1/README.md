# Team Action protocol v1

Team owns the validation semantics of an admitted Action input or output schema. `schema.py` defines them once: the
JSON value and byte bounds Team admits, the reference walk (only the root or a named definition, no cycle, at most
4,096 subschemas once every reference is expanded, and no subschema more than 32 levels below the root, a reference's
target one level below the subschema holding it), the Draft 2020-12 dialect rule, and payload validation that
evaluates every `pattern` and `patternProperties` with bounded RE2 under one work budget per payload. `pattern.py`
defines the pattern dialect and each pattern's program bound. Preparing a pattern charges 256 times its program bound
once per validation, which then reuses it; each search charges its subject's UTF-8 length, at least 1, times the bound;
and one payload validation may charge at most 134,217,728 (2^27): the preparation or search that would exceed it is
refused before it runs. `uniqueItems` is exact JSON Schema equality, and a `multipleOf` that Python cannot
evaluate refuses the payload. These are the rules of the Developers Assistant protocol, which its `action-schema`
crate implements for publication and its vectors pin, so Developers, Team, and the Brain admit and validate Action
schemas with one predicate. Team admission adds the rules a schema position needs, such as closed objects and no
boolean subschema, and stays authoritative.

The Brain re-checks the schemas Team sends it and validates its model's proposed Action arguments with these same
semantics. It consumes this directory as a generated mirror at `brain/protocol/team/action/v1/`, pinned by
`brain/protocol/team/action/upstream.json` to the Team commit, the protocol tree, and the SHA-256 of
`contract-files.sha256`; the mirror is never edited locally. Its pattern semantics are pinned by the Developers
Assistant protocol's pattern, pattern-program, and payload vectors, which Team executes.
