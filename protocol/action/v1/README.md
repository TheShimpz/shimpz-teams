# Team Action protocol v1

Team owns the validation semantics of an admitted Action input or output schema. `schema.py` defines them once: the
JSON value and byte bounds Team admits, the reference walk (only the root or a named definition, no cycle, at most
4,096 subschemas once every reference is expanded), the Draft 2020-12 dialect rule, and payload validation that
evaluates every `pattern` and `patternProperties` with bounded RE2 under one work budget per payload. Team admission
adds its own rules, such as closed objects and no boolean subschema, and stays authoritative.

The Brain re-checks the schemas Team sends it and validates its model's proposed Action arguments with these same
semantics. It consumes this directory as a generated mirror at `brain/protocol/team/action/v1/`, pinned by
`brain/protocol/team/action/upstream.json` to the Team commit, the protocol tree, and the SHA-256 of
`contract-files.sha256`; the mirror is never edited locally. Its pattern semantics are pinned by the Developers
Assistant protocol's pattern vectors, which Team executes.
