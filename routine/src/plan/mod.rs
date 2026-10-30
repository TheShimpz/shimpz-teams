//! The recorded plan of a Routine and the resolution of each step's inputs, without I/O (ADR-0092 §3, ADR-0101).
//!
//! A plan is versioned declarative JSON: at most 256 ordered steps, each naming one exact Assistant Action by its
//! complete pin, the same Action as often as the work needs with its own inputs, and giving each top-level input
//! member it holds exactly one value: a `literal`, the `run_clock` date of the run's one immutable start instant in the
//! plan's timezone or a text template whose dates are written again for that date ([`crate::dates`]), or a
//! `step_output` that copies one JSON value from a completed earlier step of the same run, selected by an RFC 6901
//! pointer, or through the one array item whose `where` member equals a constant and then the item's own `item`
//! pointer. Every required member is present. There is no coercion, interpolation, expression, branch, loop, or
//! cross-run lookup. Outputs can only fill inputs: they never choose an Assistant, an Action, a schedule, or a step.
//! A missing path, an invalid index or escape, a selector matching no item or several, and a value its destination
//! schema refuses (null included) fail closed before dispatch. Only the selected values are retained, never a complete
//! output, and a literal that a secret belongs in is refused: such a value must be the Action's declared Stored Input.
//! An Action that declares a file input is refused: a Routine holds no file grant, so no literal id or copied output
//! may stand for an attached file (ADR-0093).
//!
//! A plan also states what a completed run does with its result: `show` its last step's result to the person after
//! every run, show it only when it `changes`, or show `none` of it (ADR-0101). A plan has at least one step.

mod output;
mod preview;
mod walk;

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::LazyLock;

use jiff::tz::TimeZone;
use strict_json::{Number, Value};
use team_protocol::identifiers::{canonical_action_id, canonical_assistant_id};
use team_protocol::python::regex::Pattern;
use team_protocol::python::text;
use team_protocol::python::value::{py_eq, small_int};
use team_protocol::routine::{
    MAX_POINTER_CHARS, OUTPUT_MODES, PLAN_DIGEST_RE, POINTER_RE, SHOWN_MODES,
};
use team_protocol::routine_context::{
    MAX_LISTED_STEPS_BYTES, MAX_LISTING_STEPS_BYTES, ROUTINE_STEP_ID_RE,
};
use team_protocol::routine_run;

pub use self::output::{
    MAX_COMPARED_OUTPUT_BYTES, MAX_SAFE_OUTPUT_DEPTH, OUTPUT_LEVELS, OUTPUT_REDACTED_KEY,
    OutputError, output_compared, output_elided, output_label, output_redacted, output_safe,
    output_shown, output_state,
};
pub use self::preview::{INPUT_REDACTED, input_preview};
pub use self::walk::{
    Held, MAX_SECRET_DEPTH, Sub, any_item, applicable, could_hold_secret, item_schemas,
    member_schemas, reference_target, secret_position,
};
use crate::credential::resembles_credential;
use crate::dates;
use crate::json::{
    as_str, at, canonical, has_fields, is_integer, is_object, members, object, sha256_hex,
    text as string,
};
use crate::schedule;

/// The plan document version.
pub const VERSION: i64 = 4;
/// The steps of one plan at most: the protocol's `MAX_ROUTINE_STEPS`.
pub const MAX_STEPS: usize = 256;
/// The canonical plan document of one revision; the steps the Brain's listing shows of it fit the protocol's bound.
pub const MAX_PLAN_BYTES: usize = MAX_LISTED_STEPS_BYTES;
/// One Routine's canonical plan and grant together; the grant repeats per input what granted it.
pub const MAX_DEFINITION_BYTES: usize = 512 * 1024;
/// Every definition a Team holds, together: at most two Routines at their own bound, or many smaller ones.
pub const TEAM_DEFINITION_BYTES: usize = MAX_LISTING_STEPS_BYTES;
/// One step's fully resolved input, canonical; a larger one is refused before its dispatch.
pub const MAX_RESOLVED_INPUT_BYTES: usize = 128 * 1024;
/// The business steps a Team's runs may start in any rolling 24 hours.
pub const MAX_DAILY_STEPS: i64 = 20_000;
/// A claimed run must start its segment within this window; the segment then extends its lease over its active time.
pub const START_LEASE_SECONDS: i64 = 900;
/// What a running segment's lease outlasts its active time by.
pub const LEASE_MARGIN_SECONDS: i64 = 300;
/// The active time of a short run.
pub const SHORT_ACTIVE_SECONDS: i64 = routine_run::SHORT_ACTIVE_SECONDS;
/// The most active time any run may spend.
pub const MAX_ACTIVE_SECONDS: i64 = routine_run::MAX_ACTIVE_SECONDS;
/// The selections one run retains, canonical, at most.
pub const MAX_RETAINED_BYTES: usize = 128 * 1024;
/// The characters of one pointer at most.
pub const MAX_POINTER: usize = MAX_POINTER_CHARS;
/// The instant a template's text is checked at when a plan is admitted.
const SAMPLE_INSTANT: i64 = 1_767_225_600;

/// A step's plan id.
pub static STEP_ID_RE: &LazyLock<Pattern> = &ROUTINE_STEP_ID_RE;
/// A complete pin.
pub static PIN_RE: &LazyLock<Pattern> = &PLAN_DIGEST_RE;

/// The active time one run of a revision with `steps` steps may spend, which a hold never refills.
#[must_use]
pub fn active_seconds(steps: i64) -> i64 {
    routine_run::active_seconds(steps)
}

/// Whether a run of this many steps is long: Admin's workers hold at most one long run at a time.
#[must_use]
pub fn long_run(steps: i64) -> bool {
    active_seconds(steps) > SHORT_ACTIVE_SECONDS
}

/// A plan or one of its resolutions was refused; `code` is the stable reason.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PlanError {
    /// The stable reason.
    pub code: &'static str,
}

impl PlanError {
    /// A refusal for this reason.
    #[must_use]
    pub const fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl fmt::Display for PlanError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for PlanError {}

/// One admitted step.
#[derive(Clone, Debug, PartialEq)]
pub struct Step {
    /// Its id within the plan.
    pub step_id: String,
    /// The Assistant it calls.
    pub assistant_id: String,
    /// The Action it calls.
    pub action: String,
    /// The Action's complete pin.
    pub pin: String,
    /// Each input member's source, in the document's order.
    pub inputs: Vec<(String, Value)>,
    /// The Action's reviewed output schema, which orders and redacts a shown result; never part of the document.
    pub output_schema: Value,
    /// The Action's reviewed input schema, which redacts the inputs a run's step record shows; never in the document.
    pub input_schema: Value,
}

impl Step {
    /// The source of one input member.
    #[must_use]
    pub fn input(&self, member: &str) -> Option<&Value> {
        self.inputs
            .iter()
            .find(|(name, _)| name == member)
            .map(|(_, source)| source)
    }
}

/// One selection from a step's output: its pointer, its canonical `where` text ("" when it selects by pointer alone),
/// and its item pointer; two steps selecting different items of one array never share a key.
pub type Selector = (String, String, String);
/// A selection's complete key in a run: the step it selects from, then its selector.
pub type Key = (String, String, String, String);

/// An admitted plan.
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    /// The zone each run reads its dates in.
    pub timezone: String,
    /// Its steps, in order.
    pub steps: Vec<Step>,
    /// The `sha256:` digest of its canonical document.
    pub digest: String,
    /// What a completed run does with its result: `{"mode": a protocol output mode, "step": the shown step id or null}`.
    pub output: Value,
}

impl Plan {
    /// The step whose result a completed run shows, or `None` when the plan shows none.
    #[must_use]
    pub fn shown(&self) -> Option<&Step> {
        let mode = as_str(at(&self.output, "mode"))?;
        if !SHOWN_MODES.contains(&mode) {
            return None;
        }
        self.steps.last()
    }

    /// The complete selectors later steps apply to `step_id`'s output, sorted, each once.
    #[must_use]
    pub fn references(&self, step_id: &str) -> Vec<Selector> {
        let found: BTreeSet<Selector> = self
            .steps
            .iter()
            .flat_map(|step| step.inputs.iter().map(|(_, source)| source))
            .filter(|source| {
                as_str(at(source, "kind")) == Some("step_output")
                    && as_str(at(source, "step")) == Some(step_id)
            })
            .map(|source| {
                let (_, pointer, where_text, item) = selector(source);
                (pointer, where_text, item)
            })
            .collect();
        found.into_iter().collect()
    }

    /// The step of this id.
    #[must_use]
    pub fn step(&self, step_id: &str) -> Option<&Step> {
        self.steps.iter().find(|step| step.step_id == step_id)
    }
}

/// A `step_output` source's complete selection key.
#[must_use]
pub fn selector(source: &Value) -> Key {
    let where_text = match source.get("where") {
        None | Some(Value::Null) => String::new(),
        Some(constant) => String::from_utf8(canonical(constant)).unwrap_or_default(),
    };
    (
        as_str(at(source, "step")).unwrap_or_default().to_owned(),
        as_str(at(source, "pointer")).unwrap_or_default().to_owned(),
        where_text,
        source
            .get("item")
            .and_then(as_str)
            .unwrap_or_default()
            .to_owned(),
    )
}

/// What a plan needs of one Action: its current complete pin, its reviewed input schema, and its file inputs.
#[derive(Clone, Debug, PartialEq)]
pub struct ActionContract {
    /// The Action's current complete pin.
    pub pin: String,
    /// Its reviewed input schema.
    pub input_schema: Value,
    /// Its file inputs, which a plan never fills.
    pub input_files: Vec<String>,
    /// Its reviewed output schema.
    pub output_schema: Value,
    /// Whether the reviewed effect proves the Action read-only; anything not proven counts as a change.
    pub read_only: bool,
    /// The Stored Inputs the Action uses, by name only, sorted.
    pub stored_inputs: Vec<String>,
}

impl ActionContract {
    /// A contract of this pin and input schema, with no file input, an empty output schema, a change effect, and no
    /// Stored Input.
    #[must_use]
    pub const fn new(pin: String, input_schema: Value) -> Self {
        Self {
            pin,
            input_schema,
            input_files: Vec::new(),
            output_schema: Value::Object(Vec::new()),
            read_only: false,
            stored_inputs: Vec::new(),
        }
    }
}

/// The exact current contract of every Action, by Assistant and Action.
pub type Contracts = BTreeMap<(String, String), ActionContract>;

fn contract<'c>(
    contracts: &'c Contracts,
    assistant: &str,
    action: &str,
) -> Option<&'c ActionContract> {
    contracts.get(&(assistant.to_owned(), action.to_owned()))
}

/// Admit one plan at creation against the exact current contracts of the Actions it names.
///
/// # Errors
///
/// The [`PlanError`] of the first refusal: the document's shape, a step's shape or contract, a source, or the output.
pub fn admit(document: &Value, contracts: &Contracts) -> Result<Plan, PlanError> {
    let (timezone, raw_steps, output, encoded) = document_parts(document)?;
    let mut steps: Vec<Step> = Vec::new();
    for raw in raw_steps {
        let earlier: Vec<&str> = steps.iter().map(|step| step.step_id.as_str()).collect();
        let step = admitted_step(raw, contracts, &earlier)?;
        steps.push(step);
    }
    let ids: Vec<&str> = steps.iter().map(|step| step.step_id.as_str()).collect();
    check_output(output, &ids)?;
    Ok(Plan {
        timezone,
        steps,
        digest: format!("sha256:{}", sha256_hex(&encoded)),
        output: output.clone(),
    })
}

/// The `sha256:` digest of a document's canonical bytes; a plan's binds every run, cursor, and claim to it.
#[must_use]
pub fn digest(document: &Value) -> String {
    format!("sha256:{}", sha256_hex(&canonical(document)))
}

/// Whether a kept plan still has the closed shape and bounds of an admitted one; its contracts are checked apart.
#[must_use]
pub fn well_formed(document: &Value) -> bool {
    let Ok((_timezone, raw_steps, output, _encoded)) = document_parts(document) else {
        return false;
    };
    let mut ids: Vec<&str> = Vec::new();
    for raw in raw_steps {
        let Ok(shape) = step_shape(raw, &ids) else {
            return false;
        };
        ids.push(shape.0);
    }
    check_output(output, &ids).is_ok()
}

/// A plan's timezone, raw steps, raw output, and canonical bytes, within its closed top-level shape and bounds.
/// A document's timezone, raw steps, raw output, and canonical bytes.
type Parts<'d> = (String, &'d [Value], &'d Value, Vec<u8>);

fn document_parts(document: &Value) -> Result<Parts<'_>, PlanError> {
    if !has_fields(document, &["version", "timezone", "steps", "output"]) {
        return Err(PlanError::new("plan-invalid"));
    }
    let encoded = canonical(document);
    if !matches!(
        document.get("version"),
        Some(Value::Number(Number::Int(VERSION)))
    ) || encoded.len() > MAX_PLAN_BYTES
    {
        return Err(PlanError::new("plan-invalid"));
    }
    let timezone = at(document, "timezone");
    schedule::zone(timezone).map_err(|_| PlanError::new("plan-timezone-invalid"))?;
    let Value::Array(raw_steps) = at(document, "steps") else {
        return Err(PlanError::new("plan-invalid"));
    };
    if raw_steps.len() > MAX_STEPS {
        return Err(PlanError::new("plan-invalid"));
    }
    Ok((
        as_str(timezone).unwrap_or_default().to_owned(),
        raw_steps,
        at(document, "output"),
        encoded,
    ))
}

/// The run's output disposition: a shown step is the plan's last, and the plan has at least one step to run.
fn check_output(value: &Value, ids: &[&str]) -> Result<(), PlanError> {
    let invalid = PlanError::new("plan-output-invalid");
    if !has_fields(value, &["mode", "step"]) {
        return Err(invalid);
    }
    let Some(mode) = as_str(at(value, "mode")).filter(|mode| OUTPUT_MODES.contains(mode)) else {
        return Err(invalid);
    };
    let Some(last) = ids.last() else {
        return Err(invalid);
    };
    let shown = at(value, "step");
    let valid = if SHOWN_MODES.contains(&mode) {
        as_str(shown) == Some(*last)
    } else {
        matches!(shown, Value::Null)
    };
    if valid { Ok(()) } else { Err(invalid) }
}

/// One step's closed shape: its id, Assistant, Action, pin, and value sources, before its contract is known.
type Shape<'d> = (&'d str, &'d str, &'d str, &'d str, &'d [(String, Value)]);

fn step_shape<'d>(raw: &'d Value, earlier: &[&str]) -> Result<Shape<'d>, PlanError> {
    let invalid = PlanError::new("plan-step-invalid");
    if !has_fields(raw, &["id", "assistant", "action", "pin", "input"]) {
        return Err(invalid);
    }
    let step_id = at(raw, "id");
    let inputs = at(raw, "input");
    if !STEP_ID_RE.is_full_value(step_id)
        || earlier.iter().any(|known| as_str(step_id) == Some(*known))
        || canonical_assistant_id(at(raw, "assistant")).is_none()
        || canonical_action_id(at(raw, "action")).is_none()
        || !PIN_RE.is_full_value(at(raw, "pin"))
        || !is_object(inputs)
    {
        return Err(invalid);
    }
    for (_, source) in members(inputs) {
        check_source(source, earlier)?;
    }
    Ok((
        as_str(step_id).unwrap_or_default(),
        as_str(at(raw, "assistant")).unwrap_or_default(),
        as_str(at(raw, "action")).unwrap_or_default(),
        as_str(at(raw, "pin")).unwrap_or_default(),
        members(inputs),
    ))
}

fn admitted_step(raw: &Value, contracts: &Contracts, earlier: &[&str]) -> Result<Step, PlanError> {
    let (step_id, assistant_id, action, pin, inputs) = step_shape(raw, earlier)?;
    let contract = contract(contracts, assistant_id, action)
        .filter(|contract| contract.pin == pin)
        .ok_or(PlanError::new("plan-pin-drift"))?;
    if !contract.input_files.is_empty() {
        return Err(PlanError::new("plan-file-input"));
    }
    let schema = &contract.input_schema;
    let properties = members(at(schema, "properties"));
    let required = crate::json::elements(at(schema, "required"));
    let named = |name: &str| properties.iter().any(|(property, _)| property == name);
    if !inputs.iter().all(|(name, _)| named(name))
        || !required.iter().all(|member| {
            inputs
                .iter()
                .any(|(name, _)| as_str(member) == Some(name.as_str()))
        })
    {
        return Err(PlanError::new("plan-input-mismatch"));
    }
    // The whole input is the first position, so the root's own applicators and annotations derive every member's
    // destinations; a template is checked as the text it writes, and any other member that is not a literal counts
    // only for presence and is never itself checked.
    let supplied: Vec<(String, Option<Value>)> = inputs
        .iter()
        .map(|(name, source)| Ok((name.clone(), supplied_value(source)?)))
        .collect::<Result<_, PlanError>>()?;
    if step_secret_literal(schema, &supplied)? {
        return Err(PlanError::new("plan-secret-literal"));
    }
    for (name, source) in inputs {
        typed(name, source, schema)?;
    }
    Ok(Step {
        step_id: step_id.to_owned(),
        assistant_id: assistant_id.to_owned(),
        action: action.to_owned(),
        pin: pin.to_owned(),
        inputs: inputs.to_vec(),
        output_schema: contract.output_schema.clone(),
        input_schema: schema.clone(),
    })
}

/// The text a run-clock source writes at the sample instant in UTC, which admission checks.
fn sample_clock(source: &Value) -> Result<Value, PlanError> {
    dates::clock_value(source, SAMPLE_INSTANT, &TimeZone::UTC)
        .ok_or(PlanError::new("plan-input-invalid"))
}

/// What a step input writes that its plan holds: a literal, or a template's text, which a secret never takes.
fn supplied_value(source: &Value) -> Result<Option<Value>, PlanError> {
    if as_str(at(source, "kind")) == Some("run_clock") && source.get("parts").is_some() {
        return sample_clock(source).map(Some);
    }
    Ok(source.get("value").cloned())
}

const SOURCE_FIELDS: [(&str, &[&str]); 5] = [
    ("literal", &["kind", "value"]),
    ("run_clock", &["kind", "format"]),
    ("run_clock", &["kind", "format", "parts"]),
    ("step_output", &["kind", "step", "pointer"]),
    ("step_output", &["kind", "step", "pointer", "where", "item"]),
];

/// One value source's closed shape: a literal, the run date, or a reference to an earlier step's output.
fn check_source(source: &Value, earlier: &[&str]) -> Result<(), PlanError> {
    let kind = as_str(source.get("kind").unwrap_or(&Value::Null));
    let Some(kind) = kind.filter(|kind| {
        SOURCE_FIELDS
            .iter()
            .any(|(name, fields)| kind == name && has_fields(source, fields))
    }) else {
        return Err(PlanError::new("plan-input-invalid"));
    };
    if kind == "literal" && holds_credential(at(source, "value")) {
        return Err(PlanError::new("plan-secret-literal"));
    }
    if kind == "run_clock" && !dates::admitted_clock(source) {
        return Err(PlanError::new("plan-input-invalid"));
    }
    if kind == "run_clock"
        && source.get("parts").is_some()
        && holds_credential(&sample_clock(source)?)
    {
        return Err(PlanError::new("plan-secret-literal"));
    }
    if kind == "step_output"
        && (!earlier
            .iter()
            .any(|known| as_str(at(source, "step")) == Some(*known))
            || pointer_tokens(at(source, "pointer")).is_none()
            || (source.get("where").is_some()
                && !(where_shape(at(source, "where"))
                    && pointer_tokens(at(source, "item")).is_some())))
    {
        return Err(PlanError::new("plan-reference-invalid"));
    }
    Ok(())
}

/// A selector's one member, whose value is a string or an integer, never a boolean.
fn where_shape(value: &Value) -> bool {
    match value {
        Value::Object(found) => {
            found.len() == 1
                && found.iter().all(|(_, constant)| {
                    matches!(constant, Value::String(_)) || is_integer(constant)
                })
        }
        _ => false,
    }
}

/// A literal or run-clock value must fit its destination; an output reference is typed when it resolves.
fn typed(name: &str, source: &Value, schema: &Value) -> Result<(), PlanError> {
    match as_str(at(source, "kind")) {
        Some("literal") => admit_member(schema, name, at(source, "value")),
        Some("run_clock") => admit_member(schema, name, &sample_clock(source)?),
        _ => Ok(()),
    }
}

/// Whether a literal reaches a secret destination anywhere inside it (`value` `None` stands for a member a run fills).
fn secret_literal_at<'a>(
    root: &'a Value,
    name: &str,
    value: Option<&Value>,
    subschema: &Sub<'a>,
    depth: usize,
) -> Result<bool, PlanError> {
    let Some(value) = value else {
        return Ok(false);
    };
    if depth > MAX_SECRET_DEPTH {
        return Ok(true);
    }
    let candidates = applicable(root, subschema, 0, Held::Value(value))?;
    if secret_position(root, name, &candidates) {
        return Ok(true);
    }
    match value {
        Value::Object(found) => {
            for (key, item) in found {
                let member = member_schemas(&candidates, key)?;
                if secret_literal_at(root, key, Some(item), &member, depth + 1)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                if secret_literal_at(
                    root,
                    name,
                    Some(item),
                    &item_schemas(&candidates, index),
                    depth + 1,
                )? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        _ => Ok(false),
    }
}

/// Whether a step's input, its run-filled members counting for presence only, reaches a secret destination.
fn step_secret_literal(
    schema: &Value,
    supplied: &[(String, Option<Value>)],
) -> Result<bool, PlanError> {
    let candidates = applicable(schema, &Sub::Node(schema), 0, Held::Supplied(supplied))?;
    if secret_position(schema, "", &candidates) {
        return Ok(true);
    }
    for (key, item) in supplied {
        let member = member_schemas(&candidates, key)?;
        if secret_literal_at(schema, key, item.as_ref(), &member, 1)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Whether a step's whole input, every member taken as a literal, reaches a secret destination anywhere.
///
/// The complete input is checked at once, so a dependent schema that a sibling member activates still applies.
///
/// # Errors
///
/// `plan-secret-literal` when the schema cannot be walked.
pub fn secret_literal(schema: &Value, supplied: &Value) -> Result<bool, PlanError> {
    secret_literal_at(schema, "", Some(supplied), &Sub::Node(schema), 0)
}

/// Whether a value holds credential-shaped text in any string or key.
#[must_use]
pub fn holds_credential(value: &Value) -> bool {
    match value {
        Value::String(text) => resembles_credential(text),
        Value::Array(items) => items.iter().any(holds_credential),
        Value::Object(found) => found
            .iter()
            .any(|(key, item)| resembles_credential(key) || holds_credential(item)),
        _ => false,
    }
}

/// Validate one value against exactly its destination member, with the schema's own definitions.
fn admit_member(schema: &Value, name: &str, value: &Value) -> Result<(), PlanError> {
    let property = members(at(schema, "properties"))
        .iter()
        .find(|(property, _)| property == name)
        .map_or(Value::Null, |(_, property)| property.clone());
    let mut member = object(vec![
        ("type", string("object")),
        (
            "properties",
            Value::Object(vec![(name.to_owned(), property)]),
        ),
        ("required", Value::Array(vec![string(name)])),
        ("additionalProperties", Value::Bool(false)),
    ]);
    for definitions in ["$defs", "definitions"] {
        if let Some(found) = schema.get(definitions) {
            crate::json::set(&mut member, definitions, found.clone());
        }
    }
    let payload = Value::Object(vec![(name.to_owned(), value.clone())]);
    action_schema::validate_payload(&member, &payload)
        .map_err(|_| PlanError::new("plan-input-type"))
}

/// Whether a token indexes an array: `0` or a number of at most nine digits without a leading zero.
fn index_token(token: &str) -> Option<usize> {
    let valid = !token.is_empty()
        && token.len() <= 9
        && token.bytes().all(|byte| byte.is_ascii_digit())
        && (token == "0" || !token.starts_with('0'));
    if valid { token.parse().ok() } else { None }
}

/// Decode one RFC 6901 pointer string; the empty pointer selects the whole value.
#[must_use]
pub fn pointer_tokens(pointer: &Value) -> Option<Vec<String>> {
    pointer_tokens_of(as_str(pointer)?)
}

/// [`pointer_tokens`] of a pointer already read as text.
#[must_use]
pub fn pointer_tokens_of(pointer: &str) -> Option<Vec<String>> {
    if text::len(pointer) > MAX_POINTER || !POINTER_RE.is_full(pointer) {
        return None;
    }
    if pointer.is_empty() {
        return Some(Vec::new());
    }
    Some(
        pointer[1..]
            .split('/')
            .map(|token| token.replace("~1", "/").replace("~0", "~"))
            .collect(),
    )
}

/// The one value a pointer selects; a missing member or an invalid index fails closed.
///
/// # Errors
///
/// `plan-reference-invalid` for a pointer outside RFC 6901 or its bound, `plan-reference-missing` when nothing is there.
pub fn select(value: &Value, pointer: &Value) -> Result<Value, PlanError> {
    let tokens = pointer_tokens(pointer).ok_or(PlanError::new("plan-reference-invalid"))?;
    let mut current = value;
    for token in &tokens {
        current = match current {
            Value::Object(found) => found
                .iter()
                .find(|(name, _)| name == token)
                .map(|(_, member)| member),
            Value::Array(items) => index_token(token).and_then(|index| items.get(index)),
            _ => None,
        }
        .ok_or(PlanError::new("plan-reference-missing"))?;
    }
    Ok(current.clone())
}

/// Exact, type-sensitive JSON equality, with no lossy conversion (ADR-0101).
///
/// A boolean never equals a number, two integers are equal only as integers, and a float equals an integer only when
/// it is integral and converts to exactly that integer.
#[must_use]
pub fn same(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Bool(left), Value::Bool(right)) => left == right,
        (Value::Number(_), Value::Number(_)) => py_eq(left, right),
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(a, b)| same(a, b))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, item)| {
                    right
                        .iter()
                        .find(|(name, _)| name == key)
                        .is_some_and(|(_, other)| same(item, other))
                })
        }
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Null, Value::Null) => true,
        _ => false,
    }
}

/// The value `item` selects inside the one array item whose `where` member is the given constant (ADR-0101).
///
/// # Errors
///
/// `plan-reference-invalid` for a selector outside its closed shape, `plan-reference-missing` when no item matches
/// or the pointer selects no array, and `plan-reference-ambiguous` when several do.
pub fn select_where(
    value: &Value,
    pointer: &Value,
    where_: &Value,
    item: &Value,
) -> Result<Value, PlanError> {
    if pointer_tokens(pointer).is_none() || pointer_tokens(item).is_none() || !where_shape(where_) {
        return Err(PlanError::new("plan-reference-invalid"));
    }
    let Value::Array(items) = select(value, pointer)? else {
        return Err(PlanError::new("plan-reference-missing"));
    };
    let (key, constant) = &members(where_)[0];
    let found: Vec<&Value> = items
        .iter()
        .filter(|entry| {
            entry
                .get(key)
                .is_some_and(|member| is_object(entry) && same(member, constant))
        })
        .collect();
    match found.as_slice() {
        [] => Err(PlanError::new("plan-reference-missing")),
        [one] => select(one, item),
        _ => Err(PlanError::new("plan-reference-ambiguous")),
    }
}

/// Every value later steps select from one completed step's result, keyed by selector.
///
/// # Errors
///
/// The [`PlanError`] of the first selection that fails.
pub fn selections(
    plan: &Plan,
    step_id: &str,
    result: &Value,
) -> Result<BTreeMap<Selector, Value>, PlanError> {
    let mut chosen = BTreeMap::new();
    for key in plan.references(step_id) {
        let value = selected(result, &key)?;
        chosen.insert(key, value);
    }
    Ok(chosen)
}

fn selected(result: &Value, key: &Selector) -> Result<Value, PlanError> {
    let (pointer, where_text, item) = key;
    if where_text.is_empty() {
        return select(result, &string(pointer));
    }
    let where_ = strict_json::parse(where_text.as_bytes())
        .map_err(|_| PlanError::new("plan-reference-invalid"))?;
    select_where(result, &string(pointer), &where_, &string(item))
}

/// Whether a run's retained selections fit their byte bound.
#[must_use]
pub fn retained_within(selected: &BTreeMap<Key, Value>) -> bool {
    retained_bytes(selected) <= MAX_RETAINED_BYTES
}

/// The canonical bytes of a run's retained selections.
#[must_use]
pub fn retained_bytes(selected: &BTreeMap<Key, Value>) -> usize {
    let entries = selected
        .iter()
        .map(|((step, pointer, where_text, item), value)| {
            Value::Array(vec![
                string(step),
                string(pointer),
                string(where_text),
                string(item),
                value.clone(),
            ])
        })
        .collect();
    canonical(&Value::Array(entries)).len()
}

/// One step's complete input, validated against its Action's reviewed input schema before any dispatch.
///
/// # Errors
///
/// `plan-reference-missing` for a selection the run does not hold, `plan-input-too-large` past the dispatch bound,
/// `plan-input-type` when `validate` refuses it, and `plan-timezone-invalid` when the plan's zone does not load.
pub fn resolve(
    plan: &Plan,
    step: &Step,
    selected: &BTreeMap<Key, Value>,
    started_at: i64,
    validate: impl Fn(&Value) -> Result<(), ()>,
) -> Result<Value, PlanError> {
    let mut resolved: Vec<(String, Value)> = Vec::with_capacity(step.inputs.len());
    for (name, source) in &step.inputs {
        let value = match as_str(at(source, "kind")) {
            Some("literal") => at(source, "value").clone(),
            Some("run_clock") => {
                let zone = schedule::zone_named(&plan.timezone)
                    .map_err(|_| PlanError::new("plan-timezone-invalid"))?;
                dates::clock_value(source, started_at, &zone)
                    .ok_or(PlanError::new("plan-input-invalid"))?
            }
            _ => selected
                .get(&selector(source))
                .cloned()
                .ok_or(PlanError::new("plan-reference-missing"))?,
        };
        resolved.push((name.clone(), value));
    }
    let resolved = Value::Object(resolved);
    if canonical(&resolved).len() > MAX_RESOLVED_INPUT_BYTES {
        // A frozen run's continuation keeps its one pending request whole, so a larger input never dispatches.
        return Err(PlanError::new("plan-input-too-large"));
    }
    validate(&resolved).map_err(|()| PlanError::new("plan-input-type"))?;
    Ok(resolved)
}

/// The digest binding one dispatch to its exact resolved input.
#[must_use]
pub fn commitment(resolved: &Value) -> String {
    sha256_hex(&canonical(resolved))
}

/// The number a JSON value holds when it is a small integer (a test and caller convenience).
#[must_use]
pub const fn small(value: &Value) -> Option<i64> {
    small_int(value)
}

#[cfg(test)]
pub(crate) mod tests;
