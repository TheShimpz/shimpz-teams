//! Team's admission of one Developers resolution (`install/contract.py`): the neutral Assistant-install v1 schema and
//! rules, with Team's own stricter file-input and Stored Input placement admission in their places.

use std::sync::LazyLock;

use assistant_install::resolve::{
    catalog_bound, image_bound, integrations_bound, stored_inputs_bound,
};
use assistant_install::{Contract, Violation};
use assistant_protocol::input_file::declaration_error;
use strict_json::Value;
use team_assistant::manifest::{proofs_signed, stored_input_declarations_from_documents};

const DEFINITIONS: &[u8] = include_bytes!("../../protocol/install/v1/definitions.schema.json");
const RESOLVE_RESPONSE: &[u8] =
    include_bytes!("../../protocol/install/v1/resolve-response.schema.json");

static CONTRACT: LazyLock<Option<Contract>> =
    LazyLock::new(|| Contract::load(DEFINITIONS, RESOLVE_RESPONSE).ok());

/// Admits one decoded resolve response.
///
/// # Errors
/// The first [`Violation`]; a contract whose pinned schemas cannot load refuses every value as a schema violation.
pub fn validate(value: &Value) -> Result<(), Violation> {
    let contract = CONTRACT.as_ref().ok_or(Violation::Schema)?;
    if !contract.schema_admits(value) {
        return Err(Violation::Schema);
    }
    image_bound(value)?;
    catalog_bound(value)?;
    let (Some(Value::Array(intents)), Some(machine_contract @ Value::Object(_))) =
        (value.get("integrations"), value.get("machine_contract"))
    else {
        return Ok(());
    };
    let actions = match machine_contract.get("actions") {
        Some(Value::Array(actions)) => actions.as_slice(),
        _ => &[],
    };
    if actions.iter().any(|action| match action {
        Value::Object(members) => {
            action.get("input_files").is_some_and(truthy) && declaration_error(members).is_some()
        }
        _ => false,
    }) {
        return Err(Violation::InputFileMismatch);
    }
    integrations_bound(intents, actions)?;
    let stored_inputs = match value.get("stored_inputs") {
        Some(Value::Array(items)) => items.as_slice(),
        _ => &[],
    };
    stored_inputs_bound(stored_inputs, actions)?;
    placements_admitted(value, stored_inputs, actions)
}

/// Team's own placement admission of the declarations, and every proof beside the input it signs.
fn placements_admitted(
    value: &Value,
    stored_inputs: &[Value],
    actions: &[Value],
) -> Result<(), Violation> {
    let allowed_hosts: Vec<String> = match value.get("allowed_hosts") {
        Some(Value::Array(hosts)) => hosts
            .iter()
            .filter_map(|host| match host {
                Value::String(host) => Some(host.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let declarations = stored_input_declarations_from_documents(stored_inputs, &allowed_hosts)
        .map_err(|_| Violation::StoredInputPlacement)?;
    if proofs_signed(actions, &declarations) {
        Ok(())
    } else {
        Err(Violation::StoredInputPlacement)
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::Array(items) => !items.is_empty(),
        Value::String(text) => !text.is_empty(),
        Value::Object(members) => !members.is_empty(),
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::CONTRACT;

    #[test]
    fn the_pinned_schemas_load() {
        assert!(CONTRACT.is_some());
    }
}
