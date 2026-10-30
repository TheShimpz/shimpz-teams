//! Verified language packs that travel with one reviewed Assistant artifact (`assistant/language.py`, ADR-0091).

use assistant_protocol::catalog::{
    catalog_by_id, catalog_digest as digest_of_catalog, pack_digest, pack_error,
};
use strict_json::Value;
use team_protocol::payload::canonical_pack_digest;

use crate::ManifestError;

/// The fixed path of the pack in every Assistant image.
pub const PACK_PATH: &str = "/opt/shimpz/shimpz.pack.json";
/// The largest pack.
pub const MAX_PACK_BYTES: usize = assistant_protocol::catalog::MAX_PACK_BYTES;
/// English is the catalog itself; every other interface language comes from the pack.
pub const ENGLISH: &str = "en";

/// The English catalog and every admitted translation of one reviewed binding.
#[derive(Clone, Debug, PartialEq)]
pub struct LanguagePack {
    /// The digest of the catalog it translates.
    pub catalog_digest: String,
    /// The digest of its exact bytes.
    pub pack_digest: String,
    /// The catalog messages.
    pub messages: Value,
    /// The locale tables.
    pub translations: Value,
    /// Its size in bytes.
    pub size: usize,
}

impl LanguagePack {
    /// The English template or its admitted translation of one declared message.
    #[must_use]
    pub fn template(&self, identifier: &str, locale: &str) -> Option<String> {
        if locale == ENGLISH {
            let catalog = catalog_by_id(&self.messages);
            return catalog.get(identifier).and_then(|message| {
                message
                    .iter()
                    .find(|(name, _)| name == "msgid")
                    .and_then(|(_, msgid)| text(msgid))
            });
        }
        self.translations
            .get(locale)
            .and_then(|entries| entries.get(identifier))
            .and_then(text)
    }
}

fn text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        _ => None,
    }
}

/// The digest of a reviewed machine contract's canonical catalog.
#[must_use]
pub fn catalog_digest(machine_contract: &Value) -> String {
    digest_of_catalog(machine_contract.get("messages").unwrap_or(&Value::Null))
}

/// Admits exact pack bytes for an already admitted catalog and its reviewed pack digest.
///
/// # Errors
/// [`ManifestError`] for an invalid digest, a pack the catalog refuses, or bytes of another digest.
pub fn admit_pack(
    raw: &[u8],
    messages: &Value,
    expected_digest: &str,
) -> Result<LanguagePack, ManifestError> {
    if canonical_pack_digest(&Value::String(expected_digest.to_owned())).is_none() {
        return Err(ManifestError::Invalid("language pack digest"));
    }
    if pack_error(raw, messages).is_some() {
        return Err(ManifestError::Invalid("language pack"));
    }
    if pack_digest(raw) != expected_digest {
        return Err(ManifestError::Invalid(
            "language pack does not match its reviewed digest",
        ));
    }
    let pack = strict_json::parse(raw).map_err(|_| ManifestError::Invalid("language pack"))?;
    Ok(LanguagePack {
        catalog_digest: digest_of_catalog(messages),
        pack_digest: expected_digest.to_owned(),
        messages: messages.clone(),
        translations: pack.get("locales").cloned().unwrap_or(Value::Null),
        size: raw.len(),
    })
}

/// The pack is valid for exactly this reviewed catalog and digest.
///
/// # Errors
/// [`ManifestError`] when it was admitted for another binding.
pub fn require_binding(
    pack: &LanguagePack,
    machine_contract: &Value,
    expected_digest: &str,
) -> Result<(), ManifestError> {
    if pack.catalog_digest == catalog_digest(machine_contract)
        && pack.pack_digest == expected_digest
    {
        Ok(())
    } else {
        Err(ManifestError::Invalid(
            "language pack does not match its reviewed binding",
        ))
    }
}
