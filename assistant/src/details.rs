//! One Assistant page in one interface language, projected from an admitted binding or staged snapshot
//! (`assistant/details.py`).

use assistant_protocol::catalog::message_id;
use strict_json::Value;
use team_protocol::payload::canonical_assistant_details;

use crate::ManifestError;
use crate::language::{ENGLISH, LanguagePack};

/// A declared Stored Input's English label and help text, and its help link.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredInputCopy {
    /// Its id.
    pub id: String,
    /// The label.
    pub label: String,
    /// The help text.
    pub description: String,
    /// The help link.
    pub help_url: String,
}

/// The admitted identity, displayed English copy, and declared capabilities an Assistant page shows.
#[derive(Clone, Debug, PartialEq)]
pub struct AssistantPage {
    /// The Assistant id.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its name.
    pub name: String,
    /// Self-declared handles: a resolution's creators or a staged snapshot's first four, never authority.
    pub creators: Vec<String>,
    /// The summary.
    pub summary: String,
    /// The description.
    pub description: String,
    /// The Creator links object.
    pub links: Value,
    /// The canonical machine contract.
    pub machine_contract: Value,
    /// Integration id and its provider.
    pub integrations: Vec<(String, String)>,
    /// The declared Stored Inputs' copy.
    pub stored_inputs: Vec<StoredInputCopy>,
}

fn text(value: String) -> Value {
    Value::String(value)
}

/// Renders one English catalog text in the page's language.
struct Translator<'a> {
    locale: &'a str,
    pack: Option<&'a LanguagePack>,
}

impl Translator<'_> {
    fn text(&self, english: &str) -> Result<Value, ManifestError> {
        if self.locale == ENGLISH {
            return Ok(text(english.to_owned()));
        }
        let pack = self.pack.ok_or(ManifestError::Invalid(
            "details need the binding's language pack",
        ))?;
        pack.template(&message_id(english), self.locale)
            .map(text)
            .ok_or(ManifestError::Invalid("details translation"))
    }

    fn actions(&self, machine_contract: &Value) -> Result<Value, ManifestError> {
        let actions = match machine_contract.get("actions") {
            Some(Value::Array(actions)) => actions.as_slice(),
            _ => &[],
        };
        let projected = actions
            .iter()
            .map(|action| {
                let field = |key: &str| action.get(key).cloned().unwrap_or(Value::Null);
                let description = match action.get("description") {
                    Some(Value::String(description)) => self.text(description)?,
                    _ => Value::Null,
                };
                Ok(object(vec![
                    ("id", field("id")),
                    ("effect", field("effect")),
                    ("description", description),
                ]))
            })
            .collect::<Result<_, ManifestError>>()?;
        Ok(Value::Array(projected))
    }

    fn stored_inputs(&self, stored_inputs: &[StoredInputCopy]) -> Result<Value, ManifestError> {
        let mut sorted = stored_inputs.to_vec();
        sorted.sort_by(|left, right| left.id.cmp(&right.id));
        let projected = sorted
            .into_iter()
            .map(|item| {
                Ok(object(vec![
                    ("id", text(item.id)),
                    ("label", self.text(&item.label)?),
                    ("description", self.text(&item.description)?),
                    ("help_url", text(item.help_url)),
                ]))
            })
            .collect::<Result<_, ManifestError>>()?;
        Ok(Value::Array(projected))
    }
}

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

impl AssistantPage {
    /// The closed details object in one interface language; English needs no pack, any other its admitted one.
    ///
    /// Every displayed text is one catalog message, so a translation is that message's template from the pack.
    ///
    /// # Errors
    /// [`ManifestError`] without the pack a translation needs, or when the projection is not the exact protocol object.
    pub fn localized(
        &self,
        locale: &str,
        pack: Option<&LanguagePack>,
    ) -> Result<Value, ManifestError> {
        let translator = Translator { locale, pack };
        let mut integrations = self.integrations.clone();
        integrations.sort();
        let integrations = integrations
            .into_iter()
            .map(|(id, provider)| object(vec![("id", text(id)), ("provider", text(provider))]))
            .collect();
        let details = object(vec![
            ("locale", text(locale.to_owned())),
            ("assistant_id", text(self.assistant_id.clone())),
            ("assistant_version", text(self.version.clone())),
            ("name", text(self.name.clone())),
            (
                "creators",
                Value::Array(self.creators.iter().cloned().map(text).collect()),
            ),
            ("summary", translator.text(&self.summary)?),
            ("description", translator.text(&self.description)?),
            ("links", self.links.clone()),
            ("actions", translator.actions(&self.machine_contract)?),
            ("integrations", Value::Array(integrations)),
            (
                "stored_inputs",
                translator.stored_inputs(&self.stored_inputs)?,
            ),
        ]);
        if canonical_assistant_details(&details).is_none() {
            return Err(ManifestError::Invalid("Assistant details"));
        }
        Ok(details)
    }
}
