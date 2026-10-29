# Team Action protocol v1

Team owns the validation semantics of an admitted Action input or output schema. `schema.py` defines them once: the JSON
value and byte bounds Team admits, the reference walk (only the root or a named definition, no cycle, at most 4,096
subschemas once every reference is expanded, and no subschema more than 32 levels below the root, a reference's target
one level below the subschema holding it), the Draft 2020-12 dialect rule, and payload validation that evaluates every
`pattern` and `patternProperties` with bounded RE2 under one work budget per payload. `pattern.py` defines the pattern
dialect, each pattern's program bound, and its preparation: 65,536, plus 256 times the bound, plus each bracketed
class's case-folding work, 64 per code point its members cover below U+20000 and 4 per code point above. Preparing a
pattern charges its preparation once per validation, which then reuses it; each search charges its subject's UTF-8
length, at least 1, times the bound; and one payload validation may charge at most 134,217,728 (2^27): the preparation
or search that would exceed it is refused before it runs. An admitted schema's distinct patterns prepare at most half
that budget together, so one validation can always prepare every pattern of its schema. `uniqueItems` is exact JSON
Schema equality, and a `multipleOf` that Python cannot evaluate refuses the payload. These are the rules of the
Developers Assistant protocol, which its `action-schema` crate implements for publication and its vectors pin, so
Developers, Team, and the Brain admit and validate Action schemas with one predicate. `admission_problem` is that whole
predicate, the Draft 2020-12 metaschema, closed objects, and no boolean subschema included, and Team admits every
Assistant's schemas with it.

The Brain re-checks every input and output schema Team sends it with the same `admission_problem` and validates its
model's proposed Action arguments with these same semantics. It consumes this directory as a generated mirror at
`brain/protocol/team/action/v1/`, pinned by `brain/protocol/team/action/upstream.json` to the Team commit, the protocol
tree, and the SHA-256 of `contract-files.sha256`; the mirror is never edited locally. Its pattern semantics are pinned
by the Developers Assistant protocol's pattern, pattern-program, and payload vectors, which Team executes.
