//! Admission and payload validation of Action input and output schemas.
//!
//! One predicate, [`admit_schema`], decides which schemas an Action may declare: Developers applies it at publication
//! (behind its own encoded-size guard), and Team and the Brain apply it to every schema they receive. An admitted
//! schema describes a closed object and is a valid Draft 2020-12 schema, fits [`MAX_NODES`] JSON values and
//! [`MAX_BYTES`] in Team's encoding, references only its root or a named definition, resolves every reference without
//! a cycle within [`MAX_EXPANDED_SUBSCHEMAS`] expanded subschemas and [`MAX_SUBSCHEMA_DEPTH`] levels, uses only the
//! Draft 2020-12 dialect, has no boolean subschema at a closed position, leaves `unevaluatedProperties` out, and
//! admits each of its patterns in the dialect of [`pattern_program`], whose [`pattern_preparation`]s sum to at most
//! [`MAX_PREPARATION_WORK`] over its distinct patterns.
//!
//! [`validate_payload`] evaluates one payload against an admitted schema with the semantics of Python `jsonschema`
//! 4.25.1's Draft 2020-12 validator on that subset, keyword by keyword in schema order, and charges every pattern search
//! `max(subject bytes, 1) × pattern_program(pattern)` against one [`MAX_PATTERN_WORK`] budget per payload, in the order
//! and multiplicity that validator searches, after charging each distinct pattern's preparation once
//! ([`pattern_preparation`]). A preparation or search whose charge exceeds what remains is refused before it runs.

mod admission;
mod matcher;
mod meta;
mod numbers;
mod pattern;
mod size;
mod validate;
mod walk;

pub use admission::{Refusal, admit_schema};
pub use matcher::{Budget, PatternError, pattern_matches};
pub use pattern::{
    MAX_PATTERN_NESTING, MAX_PATTERN_PROGRAM, MAX_REPEAT, pattern_admitted, pattern_preparation,
    pattern_program,
};
pub use size::encoded_len;
pub use strict_json::{Number, Value};
pub use validate::{MAX_PAYLOAD_DEPTH, PayloadError, validate_payload, validate_payload_in};
pub use walk::nodes_within;

/// The most JSON values an admitted schema holds: every value counts, whether a subschema, an annotation such as
/// `default` or `examples`, or a literal such as `const` or `enum`.
pub const MAX_NODES: usize = 4096;
/// The longest an admitted schema's compact, ASCII-escaped JSON encoding may be, in bytes.
pub const MAX_BYTES: usize = 128 * 1024;
/// The most subschemas validation may visit once every reference is expanded at each subschema that holds it.
pub const MAX_EXPANDED_SUBSCHEMAS: usize = 4096;
/// The deepest a subschema may lie below the root, where a reference's target counts one level below the subschema
/// that holds the reference.
pub const MAX_SUBSCHEMA_DEPTH: usize = 32;
/// The matching work one payload validation may charge: the [`pattern_preparation`] of each distinct pattern it
/// prepares, plus, over its searches, the subject's UTF-8 length (at least 1) times the [`pattern_program`] bound.
pub const MAX_PATTERN_WORK: u64 = 1 << 27;
/// The most the preparations of an admitted schema's distinct patterns may sum to: half the budget, so one validation
/// can prepare every pattern of its schema and keeps the other half for its searches.
pub const MAX_PREPARATION_WORK: u64 = MAX_PATTERN_WORK / 2;
/// What preparing any pattern charges for building its matcher, once per payload validation.
pub const PREPARATION_BASE: u64 = 1 << 16;
/// What preparing one pattern (reading its syntax and compiling its program) charges per unit of its program bound,
/// once per payload validation: compiling costs far more per instruction than a search does per subject byte.
pub const PREPARATION_WEIGHT: u64 = 256;
/// The one dialect an admitted schema may declare.
pub const DRAFT_2020_12: &str = "https://json-schema.org/draft/2020-12/schema";
