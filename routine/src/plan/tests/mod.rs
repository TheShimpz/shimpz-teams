//! `test_routine_plan.py` and `test_routine_select.py`: admission, same-run references and selectors, the run date,
//! secret literals, bounds, and what a step record shows of an attempt's inputs.

use std::collections::BTreeMap;

use strict_json::Value;

use super::walk::{Sub, applicable, could_hold_secret, member_schemas};
use super::{
    ActionContract, Contracts, Held, Key, MAX_RESOLVED_INPUT_BYTES, MAX_RETAINED_BYTES,
    MAX_SAFE_OUTPUT_DEPTH, MAX_SECRET_DEPTH, MAX_STEPS, Plan, PlanError, VERSION, admit,
    commitment, holds_credential, input_preview, output_redacted, output_safe, resolve,
    retained_within, same, secret_literal, secret_literal_at, select, select_where, selections,
    well_formed,
};
use crate::dates::clock_value;
use crate::json::{as_str, at, elements, int, set, text};
use crate::schedule::zone_named;
use crate::testing::{epoch, json};

pub(crate) const PIN: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub(crate) const OTHER_PIN: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const PUBLISH: &str = r#"{
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "day": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "count": {"type": "integer"},
        "note": {"type": ["string", "null"]}
    },
    "required": ["title"],
    "additionalProperties": false
}"#;
const SHARE: &str = r##"{
    "type": "object",
    "properties": {
        "post_id": {"type": "string"},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}},
        "at": {"type": "integer"},
        "api_token": {"type": "string"},
        "secret_note": {"type": "string", "writeOnly": true},
        "code": {"type": "string", "format": "password"}
    },
    "required": ["post_id"],
    "additionalProperties": false,
    "$defs": {"tag": {"type": "string", "minLength": 1}}
}"##;
const DATED: &str = r#"{"days": 0, "format": "{dd}/{MM}/{yyyy}", "lang": null}"#;

/// One contract.
pub(crate) fn contract(pin: &str, schema: &str) -> ActionContract {
    ActionContract::new(pin.to_owned(), json(schema))
}

/// The contracts of the blog Assistant's two Actions.
pub(crate) fn contracts() -> Contracts {
    Contracts::from([
        (
            ("shimpz-blog".to_owned(), "publish-post".to_owned()),
            contract(PIN, PUBLISH),
        ),
        (
            ("shimpz-blog".to_owned(), "share-post".to_owned()),
            contract(OTHER_PIN, SHARE),
        ),
    ])
}

/// The two-step blog plan: publish a post, then share it.
pub(crate) fn document() -> Value {
    json(&format!(
        r#"{{
        "version": 4,
        "timezone": "America/Sao_Paulo",
        "steps": [
            {{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}",
              "input": {{"title": {{"kind": "literal", "value": "Weekly report"}},
                         "day": {{"kind": "run_clock", "format": "date"}}}}}},
            {{"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": "{OTHER_PIN}",
              "input": {{"post_id": {{"kind": "step_output", "step": "publish", "pointer": "/id"}},
                         "tags": {{"kind": "step_output", "step": "publish", "pointer": "/meta/a~1b/0"}}}}}}
        ],
        "output": {{"mode": "show", "step": "share"}}
    }}"#
    ))
}

/// The blog plan with one top-level member replaced.
pub(crate) fn document_with(key: &str, value: Value) -> Value {
    let mut changed = document();
    set(&mut changed, key, value);
    changed
}

fn step_mut(document: &mut Value, index: usize) -> &mut Value {
    match document {
        Value::Object(found) => match &mut found
            .iter_mut()
            .find(|(name, _)| name == "steps")
            .expect("steps")
            .1
        {
            Value::Array(steps) => &mut steps[index],
            _ => panic!("steps"),
        },
        _ => panic!("a document"),
    }
}

fn input_mut(document: &mut Value, index: usize) -> &mut Value {
    match step_mut(document, index) {
        Value::Object(found) => {
            &mut found
                .iter_mut()
                .find(|(name, _)| name == "input")
                .expect("input")
                .1
        }
        _ => panic!("a step"),
    }
}

fn refused(document: &Value, code: &'static str) {
    assert_eq!(
        admit(document, &contracts()).map(|_| ()),
        Err(PlanError::new(code)),
        "{document:?}"
    );
}

fn validator(schema: &str) -> impl Fn(&Value) -> Result<(), ()> {
    let schema = json(schema);
    move |value| action_schema::validate_payload(&schema, value).map_err(|_| ())
}

fn key(step: &str, pointer: &str, where_text: &str, item: &str) -> Key {
    (
        step.to_owned(),
        pointer.to_owned(),
        where_text.to_owned(),
        item.to_owned(),
    )
}

fn at_mut<'v>(value: &'v mut Value, key: &str) -> &'v mut Value {
    match value {
        Value::Object(found) => {
            &mut found
                .iter_mut()
                .find(|(name, _)| name == key)
                .expect("a member")
                .1
        }
        _ => panic!("an object"),
    }
}

fn with_source(name: &str, value: &str, step: usize) -> Value {
    let mut changed = document();
    set(input_mut(&mut changed, step), name, json(value));
    changed
}

fn shown_value(previews: &Value, index: usize) -> String {
    as_str(at(&elements(previews)[index], "value"))
        .unwrap_or("<null>")
        .to_owned()
}

mod admission;
mod preview;
mod resolve;
mod secret;
mod select;
