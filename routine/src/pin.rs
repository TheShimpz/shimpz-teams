//! The complete pin of one Action a replayed Routine step runs (ADR-0092 section 3).
//!
//! A step pins everything that decides what its Action may do and how Team handles it: the immutable image and source
//! identity, the outbound hosts, the Action's whole reviewed contract (input and output schemas, capabilities, effect,
//! verifier, and idempotency), the verifier Action recovery would run, the Integration and Stored Input declarations
//! either uses, the English catalog, and the language pack with the one fixed locale its document names. Team
//! recomputes the pin before every dispatch and resume; any difference is drift, which holds the Routine until an
//! authenticated update.

use std::collections::{BTreeMap, BTreeSet};

use strict_json::Value;
use team_protocol::python::json::{Options, dumps};

use crate::json::{as_str, at, elements, object, text};
use crate::plan;

/// The format of one Action's pin document.
pub const FORMAT: &str = "shimpz-routine-action-pin-v1";
/// The format of one Assistant's scope pin document.
pub const SCOPE_FORMAT: &str = "shimpz-routine-assistant-pin-v1";
/// Every locale's copy is in the pinned pack, so every pin names this one fixed locale.
pub const PIN_LOCALE: &str = "en";

/// One Integration an Action declares: its provider and scopes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Integration {
    /// The provider.
    pub provider: String,
    /// The scopes.
    pub scopes: Vec<String>,
}

/// One Stored Input an Action declares, as a person reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredInput {
    /// Its kind.
    pub kind: String,
    /// Its label.
    pub label: String,
    /// Its English help text.
    pub description: String,
    /// The official page where a person creates or finds it, if any.
    pub help_url: Option<String>,
}

/// What a pin covers of one admitted Assistant.
#[derive(Clone, Debug, PartialEq)]
pub struct PinnedAssistant {
    /// Its id.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its image reference.
    pub image: String,
    /// The image labels it requires, in order.
    pub image_labels: Vec<(String, String)>,
    /// Its outbound hosts, in order.
    pub allowed_hosts: Vec<String>,
    /// Its machine contract's Actions.
    pub actions: Vec<Value>,
    /// Its Integration declarations, by name.
    pub integrations: BTreeMap<String, Integration>,
    /// Its Stored Input declarations, by name.
    pub stored_inputs: BTreeMap<String, StoredInput>,
    /// The digest of its machine contract's English catalog.
    pub catalog: String,
    /// The reviewed binding's language-pack digest.
    pub pack_digest: String,
}

impl PinnedAssistant {
    fn action(&self, action_id: &str) -> Option<&Value> {
        self.actions
            .iter()
            .find(|action| as_str(at(action, "id")) == Some(action_id))
    }
}

/// Each named Action's `sha256:` pin, or `None` when one names an Action, a verifier, or a declaration the Assistant
/// does not hold; one catalog digest serves every pin.
#[must_use]
pub fn action_pins<'a>(
    spec: &PinnedAssistant,
    action_ids: impl IntoIterator<Item = &'a str>,
) -> Option<BTreeMap<String, String>> {
    action_ids
        .into_iter()
        .map(|action_id| Some((action_id.to_owned(), action_pin(spec, action_id)?)))
        .collect()
}

/// The `sha256:` scope pin of one Assistant: its Brain-visible contract and every Action's complete pin.
///
/// A Routine pins the Assistants it may use with it, so a changed image, output schema, effect, verifier,
/// idempotency, capability, catalog, or pack is drift exactly as a changed input schema is.
#[must_use]
pub fn assistant_pin(spec: &PinnedAssistant, brain_digest: &str) -> Option<String> {
    let mut declared: Vec<&str> = spec
        .actions
        .iter()
        .filter_map(|action| as_str(at(action, "id")))
        .collect();
    declared.sort_unstable();
    let pins = action_pins(spec, declared)?;
    let document = object(vec![
        ("format", text(SCOPE_FORMAT)),
        ("brain", text(brain_digest)),
        (
            "actions",
            Value::Object(
                pins.into_iter()
                    .map(|(id, pin)| (id, Value::String(pin)))
                    .collect(),
            ),
        ),
    ]);
    let encoded = dumps(
        &document,
        Options {
            ensure_ascii: true,
            sort_keys: true,
            compact: true,
        },
    );
    Some(format!(
        "sha256:{}",
        crate::json::sha256_hex(encoded.as_bytes())
    ))
}

fn names(pinned: &[&Value], key: &str) -> BTreeSet<String> {
    pinned
        .iter()
        .flat_map(|item| elements(at(item, key)))
        .filter_map(as_str)
        .map(str::to_owned)
        .collect()
}

/// Declarations by name, as a pin document holds them.
type Declarations = Vec<(String, Value)>;

/// The Integration and Stored Input declarations an Action and its verifier use, by name.
fn declarations(spec: &PinnedAssistant, pinned: &[&Value]) -> Option<(Declarations, Declarations)> {
    let mut integrations = Vec::new();
    for name in names(pinned, "integrations") {
        let declaration = spec.integrations.get(&name)?;
        let scopes = Value::Array(declaration.scopes.iter().map(|scope| text(scope)).collect());
        integrations.push((
            name,
            object(vec![
                ("provider", text(&declaration.provider)),
                ("scopes", scopes),
            ]),
        ));
    }
    let mut stored_inputs = Vec::new();
    for name in names(pinned, "stored_inputs") {
        let declaration = spec.stored_inputs.get(&name)?;
        let help_url = declaration.help_url.as_deref().map_or(Value::Null, text);
        stored_inputs.push((
            name,
            object(vec![
                ("kind", text(&declaration.kind)),
                ("label", text(&declaration.label)),
                ("description", text(&declaration.description)),
                ("help_url", help_url),
            ]),
        ));
    }
    Some((integrations, stored_inputs))
}

fn action_pin(spec: &PinnedAssistant, action_id: &str) -> Option<String> {
    let action = spec.action(action_id)?;
    let verifier = match action.get("verifier") {
        None | Some(Value::Null) => None,
        Some(verifier) => Some(spec.action(as_str(at(verifier, "action"))?)?),
    };
    let pinned: Vec<&Value> = std::iter::once(action).chain(verifier).collect();
    let (integrations, stored_inputs) = declarations(spec, &pinned)?;
    let labels = spec
        .image_labels
        .iter()
        .map(|(name, value)| Value::Array(vec![text(name), text(value)]))
        .collect();
    let document = object(vec![
        ("format", text(FORMAT)),
        ("assistant", text(&spec.assistant_id)),
        ("version", text(&spec.version)),
        ("image", text(&spec.image)),
        ("image_labels", Value::Array(labels)),
        (
            "allowed_hosts",
            Value::Array(spec.allowed_hosts.iter().map(|host| text(host)).collect()),
        ),
        ("action", action.clone()),
        ("verifier", verifier.cloned().unwrap_or(Value::Null)),
        ("integrations", Value::Object(integrations)),
        ("stored_inputs", Value::Object(stored_inputs)),
        ("catalog", text(&spec.catalog)),
        ("pack", text(&spec.pack_digest)),
        ("locale", text(PIN_LOCALE)),
    ]);
    Some(plan::digest(&document))
}

#[cfg(test)]
mod tests;
