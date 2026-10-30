//! The reviewed runtime contract of one Assistant binding (`assistant/spec.py`).

use action_schema::{PayloadError, validate_payload};
use strict_json::Value;

use crate::ManifestError;
use crate::manifest::{
    IntegrationDeclaration, ManifestContract, ManifestPresentation, StoredInputDeclaration,
    canonical_allowed_hosts, canonical_machine_contract, canonical_manifest_presentation,
    reviewed_manifest_contract, stored_input_declarations_from_documents,
};

/// The digest no published image ever has.
pub const ALL_ZERO_SHA256: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// One admitted machine-contract Action as the Team holds it.
#[derive(Clone, Debug, PartialEq)]
pub struct ActionSpec {
    /// The Action id.
    pub id: String,
    /// The id as a sentence.
    pub summary: String,
    /// The admitted input schema.
    pub input_schema: Value,
    /// The admitted output schema.
    pub output_schema: Value,
    /// The Integrations it uses.
    pub integrations: Vec<String>,
    /// The Stored Inputs it uses.
    pub stored_inputs: Vec<String>,
    /// The human requests it may make.
    pub human_requests: Vec<String>,
    /// The input properties that carry one Team file each (ADR-0093).
    pub input_files: Vec<String>,
    /// `read_only` or `mutating` (ADR-0092).
    pub effect: String,
    /// The optional verifier declaration.
    pub verifier: Option<Value>,
    /// The optional idempotency declaration.
    pub idempotency: Option<Value>,
}

fn strings(action: &Value, key: &str) -> Vec<String> {
    match action.get(key) {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| match item {
                Value::String(text) => Some(text.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

impl ActionSpec {
    /// One admitted machine-contract Action.
    #[must_use]
    pub fn of(action: &Value) -> Self {
        let id = match action.get("id") {
            Some(Value::String(id)) => id.clone(),
            _ => String::new(),
        };
        let field = |key: &str| action.get(key).cloned().unwrap_or(Value::Null);
        Self {
            summary: action_summary(&id),
            id,
            input_schema: field("input_schema"),
            output_schema: field("output_schema"),
            integrations: strings(action, "integrations"),
            stored_inputs: strings(action, "stored_inputs"),
            human_requests: strings(action, "human_requests"),
            input_files: strings(action, "input_files"),
            effect: match action.get("effect") {
                Some(Value::String(effect)) => effect.clone(),
                _ => "mutating".to_owned(),
            },
            verifier: action.get("verifier").cloned(),
            idempotency: action.get("idempotency").cloned(),
        }
    }

    /// Validates one untrusted input or output payload against its reviewed schema.
    ///
    /// # Errors
    /// [`PayloadError`] when it is not an object, nests too deeply, or does not match.
    pub fn validate(&self, direction: Direction, payload: &Value) -> Result<(), PayloadError> {
        let schema = match direction {
            Direction::Input => &self.input_schema,
            Direction::Output => &self.output_schema,
        };
        validate_payload(schema, payload)
    }
}

/// Which schema of an Action a payload is checked against.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    /// The input schema.
    Input,
    /// The output schema.
    Output,
}

/// The id of an Action as a sentence: hyphens become spaces and the first letter is capitalized.
#[must_use]
pub fn action_summary(action_id: &str) -> String {
    let spaced = action_id.replace('-', " ");
    let mut characters = spaced.chars();
    characters.next().map_or_else(String::new, |first| {
        first
            .to_uppercase()
            .chain(characters.flat_map(char::to_lowercase))
            .collect()
    })
}

/// Whether an image reference names a digest other than the all-zero one.
#[must_use]
pub fn digest_is_bound(reference: &str) -> bool {
    !reference.ends_with(&format!("sha256:{ALL_ZERO_SHA256}"))
}

/// The admitted runtime contract of one Assistant binding document.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeContract {
    /// The Actions by id, in the contract's sorted order.
    pub actions: Vec<ActionSpec>,
    /// The exact outbound hosts.
    pub allowed_hosts: Vec<String>,
    /// The Integrations.
    pub integrations: Vec<IntegrationDeclaration>,
    /// The Stored Inputs.
    pub stored_inputs: Vec<StoredInputDeclaration>,
    /// The canonical machine contract.
    pub machine_contract: Value,
    /// The Assistant page copy.
    pub presentation: ManifestPresentation,
}

impl RuntimeContract {
    /// The Action `id`.
    #[must_use]
    pub fn action(&self, id: &str) -> Option<&ActionSpec> {
        self.actions.iter().find(|action| action.id == id)
    }
}

fn integration(document: &Value) -> Result<IntegrationDeclaration, ManifestError> {
    let refused = || ManifestError::Invalid("binding Integration");
    let text = |key: &str| match document.get(key) {
        Some(Value::String(text)) => Ok(text.clone()),
        _ => Err(refused()),
    };
    let Some(Value::Array(scopes)) = document.get("scopes") else {
        return Err(refused());
    };
    let scopes = scopes
        .iter()
        .map(|scope| match scope {
            Value::String(scope) => Ok(scope.clone()),
            _ => Err(refused()),
        })
        .collect::<Result<_, _>>()?;
    Ok(IntegrationDeclaration {
        id: text("id")?,
        provider: text("provider")?,
        scopes,
    })
}

/// A binding document's outbound hosts, Integrations, and Stored Inputs.
fn declarations(document: &Value) -> Result<ManifestContract, ManifestError> {
    let refused = || ManifestError::Invalid("binding document");
    let (Some(Value::Array(integrations)), Some(hosts), Some(Value::Array(stored_inputs))) = (
        document.get("integrations"),
        document.get("allowed_hosts"),
        document.get("stored_inputs"),
    ) else {
        return Err(refused());
    };
    let integrations = integrations
        .iter()
        .map(integration)
        .collect::<Result<Vec<_>, _>>()?;
    let allowed_hosts = canonical_allowed_hosts(hosts)?;
    let stored_inputs = stored_input_declarations_from_documents(stored_inputs, &allowed_hosts)?;
    Ok(ManifestContract {
        allowed_hosts,
        integrations,
        stored_inputs,
    })
}

/// Admits a binding document's declarations and its exactly canonical machine contract.
///
/// # Errors
/// [`ManifestError`] for any refused part, or a machine contract that is not already canonical.
pub fn runtime_contract(document: &Value) -> Result<RuntimeContract, ManifestError> {
    let field = |key: &str| {
        document
            .get(key)
            .ok_or(ManifestError::Invalid("binding document"))
    };
    let declared = declarations(document)?;
    let (integrations, stored_inputs) = (
        declared.integrations.clone(),
        declared.stored_inputs.clone(),
    );
    let presentation = canonical_manifest_presentation(field("description")?, field("links")?)?;
    let Value::String(summary) = field("summary")? else {
        return Err(ManifestError::Invalid("binding document"));
    };
    let machine_contract = canonical_machine_contract(
        field("machine_contract")?,
        &declared,
        summary,
        &presentation.description,
    )?;
    if !team_protocol::python::value::py_eq(&machine_contract, field("machine_contract")?) {
        return Err(ManifestError::Invalid("machine contract is not canonical"));
    }
    let reviewed =
        reviewed_manifest_contract(field("allowed_hosts")?, &integrations, &stored_inputs)?;
    let actions = match machine_contract.get("actions") {
        Some(Value::Array(actions)) => actions.iter().map(ActionSpec::of).collect(),
        _ => Vec::new(),
    };
    Ok(RuntimeContract {
        actions,
        allowed_hosts: reviewed.allowed_hosts,
        integrations,
        stored_inputs,
        machine_contract,
        presentation,
    })
}
