//! One stored Integration record: its public header and the grant its envelope seals.

use strict_json::{Number, Value};
use team_core::secret::Secret;
use team_storage::envelope::Envelope;
use team_storage::private::is_timestamp;

use super::{
    BOUNDS, MAX_EXPIRY, MAX_INTEGRATION_ID_BYTES, MAX_INTEGRATION_TEXT_BYTES, MAX_PLAINTEXT_BYTES,
    StoreError, Stored, int, intent, plaintext_json,
};
use crate::text::public_bytes;
use crate::token::{MAX_TOKEN_BYTES, TokenSet};

const RECORD_FIELDS: [&str; 7] = [
    "envelope",
    "expires_at",
    "generation",
    "provider",
    "scopes",
    "status",
    "updated_at",
];
const GRANT_FIELDS: [&str; 4] = [
    "access_token",
    "broker_lease",
    "integration",
    "refresh_token",
];

/// The provider account an Integration is connected to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    /// The account id (at most 256 bytes of public text).
    pub id: String,
    /// The username, when known (at most 512 bytes).
    pub username: Option<String>,
    /// The display name, when known (at most 512 bytes).
    pub name: Option<String>,
}

impl Identity {
    /// Whether every field is bounded public text.
    #[must_use]
    pub fn valid(&self) -> bool {
        public_bytes(&self.id, MAX_INTEGRATION_ID_BYTES)
            && [&self.username, &self.name].iter().all(|field| {
                field
                    .as_ref()
                    .is_none_or(|text| public_bytes(text, MAX_INTEGRATION_TEXT_BYTES))
            })
    }

    fn to_value(&self) -> Value {
        let optional = |field: &Option<String>| field.clone().map_or(Value::Null, Value::String);
        Value::Object(vec![
            ("id".to_owned(), Value::String(self.id.clone())),
            ("username".to_owned(), optional(&self.username)),
            ("name".to_owned(), optional(&self.name)),
        ])
    }

    /// The identity a decrypted grant holds: `null`, or an object of `id` and optional `username` and `name`.
    fn admit(value: &Value) -> Result<Option<Self>, ()> {
        let Value::Object(members) = value else {
            return if *value == Value::Null {
                Ok(None)
            } else {
                Err(())
            };
        };
        if members
            .iter()
            .any(|(key, _)| !matches!(key.as_str(), "id" | "username" | "name"))
        {
            return Err(());
        }
        let text = |key: &str| match value.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(text)) => Ok(Some(text.clone())),
            Some(_) => Err(()),
        };
        let identity = Self {
            id: text("id")?.ok_or(())?,
            username: text("username")?,
            name: text("name")?,
        };
        if identity.valid() {
            Ok(Some(identity))
        } else {
            Err(())
        }
    }
}

/// A record's public, authenticated header.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Header {
    pub provider: String,
    pub scopes: Vec<String>,
    pub expires_at: i64,
    pub status: Stored,
    pub generation: u64,
}

impl Header {
    /// Admits a whole stored record: exactly its fields, a reviewed provider and canonical scopes, a bounded expiry, a
    /// stored status, a positive generation, a record time, and a well-formed identified envelope.
    pub fn admit(record: &Value) -> Option<Self> {
        let Value::Object(members) = record else {
            return None;
        };
        let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
        names.sort_unstable();
        if names != RECORD_FIELDS {
            return None;
        }
        let Some(Value::String(provider)) = record.get("provider") else {
            return None;
        };
        let Some(Value::Array(stored)) = record.get("scopes") else {
            return None;
        };
        let scopes: Vec<String> = stored
            .iter()
            .map(|scope| match scope {
                Value::String(scope) => Some(scope.clone()),
                _ => None,
            })
            .collect::<Option<_>>()?;
        let (canonical, sorted) = intent(provider, &scopes).ok()?;
        let integer = |key| match record.get(key) {
            Some(Value::Number(Number::Int(value))) => Some(*value),
            _ => None,
        };
        let expires_at = integer("expires_at").filter(|value| (1..=MAX_EXPIRY).contains(value))?;
        let generation = integer("generation")
            .and_then(|value| u64::try_from(value).ok())
            .filter(|value| *value >= 1)?;
        let status = match record.get("status") {
            Some(Value::String(status)) => Stored::parse(status)?,
            _ => return None,
        };
        let timestamped =
            matches!(record.get("updated_at"), Some(Value::String(text)) if is_timestamp(text));
        let sealed = record
            .get("envelope")
            .is_some_and(|envelope| Envelope::admit_identified(envelope, BOUNDS).is_ok());
        (sorted == scopes && timestamped && sealed).then(|| Self {
            provider: canonical.to_owned(),
            scopes,
            expires_at,
            status,
            generation,
        })
    }

    /// The stored record with `updated_at` and its `envelope`.
    pub fn to_value(&self, updated_at: &str, envelope: Value) -> Value {
        Value::Object(vec![
            ("provider".to_owned(), Value::String(self.provider.clone())),
            (
                "scopes".to_owned(),
                Value::Array(self.scopes.iter().cloned().map(Value::String).collect()),
            ),
            ("expires_at".to_owned(), int(self.expires_at)),
            (
                "status".to_owned(),
                Value::String(self.status.text().to_owned()),
            ),
            (
                "generation".to_owned(),
                int(i64::try_from(self.generation).unwrap_or(i64::MAX)),
            ),
            (
                "updated_at".to_owned(),
                Value::String(updated_at.to_owned()),
            ),
            ("envelope".to_owned(), envelope),
        ])
    }
}

/// One sealed grant: its tokens and the account they belong to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Grant {
    /// The access token.
    pub access_token: Secret,
    /// The refresh token, when the grant has one.
    pub refresh_token: Option<Secret>,
    /// The broker lease, when the grant has one.
    pub broker_lease: Option<Secret>,
    /// The granted scopes, sorted.
    pub scopes: Vec<String>,
    /// When the access token expires (epoch seconds).
    pub expires_at: i64,
    /// The provider account, when known.
    pub integration: Option<Identity>,
}

fn token(text: &str) -> bool {
    public_bytes(text, MAX_TOKEN_BYTES)
}

impl Grant {
    /// Admits a token set for the declared `scopes` delivered at `now`: its scopes must equal them exactly, its
    /// lifetime must lie in 30 seconds to one year, and every token must be bounded public text.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] for anything else.
    pub fn from_tokens(
        tokens: &TokenSet,
        scopes: &[String],
        now: i64,
        integration: Option<&Identity>,
    ) -> Result<Self, StoreError> {
        if tokens.scopes != scopes || !(30..=31_536_000).contains(&tokens.expires_in) {
            return Err(StoreError::Invalid);
        }
        let expires_at = now
            .checked_add(tokens.expires_in)
            .ok_or(StoreError::Invalid)?;
        let optional =
            |value: &Option<Secret>| value.as_ref().is_none_or(|secret| token(secret.expose()));
        if !(1..=MAX_EXPIRY).contains(&expires_at)
            || !token(tokens.access_token.expose())
            || !optional(&tokens.refresh_token)
            || !optional(&tokens.broker_lease)
            || integration.is_some_and(|identity| !identity.valid())
        {
            return Err(StoreError::Invalid);
        }
        Ok(Self {
            access_token: tokens.access_token.clone(),
            refresh_token: tokens.refresh_token.clone(),
            broker_lease: tokens.broker_lease.clone(),
            scopes: scopes.to_vec(),
            expires_at,
            integration: integration.cloned(),
        })
    }

    /// The sealed plaintext: `{"access_token","broker_lease","integration","refresh_token"}`, ASCII, sorted, compact.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] beyond [`MAX_PLAINTEXT_BYTES`].
    pub fn plaintext(&self) -> Result<zeroize::Zeroizing<Vec<u8>>, StoreError> {
        let secret = |value: &Option<Secret>| {
            value.as_ref().map_or(Value::Null, |secret| {
                Value::String(secret.expose().to_owned())
            })
        };
        let document = zeroize::Zeroizing::new(plaintext_json(&Value::Object(vec![
            (
                "access_token".to_owned(),
                Value::String(self.access_token.expose().to_owned()),
            ),
            ("refresh_token".to_owned(), secret(&self.refresh_token)),
            ("broker_lease".to_owned(), secret(&self.broker_lease)),
            (
                "integration".to_owned(),
                self.integration
                    .as_ref()
                    .map_or(Value::Null, Identity::to_value),
            ),
        ])));
        if document.len() > MAX_PLAINTEXT_BYTES {
            return Err(StoreError::Invalid);
        }
        Ok(document)
    }

    /// The grant a decrypted plaintext holds under `header`, or `None` for anything malformed.
    pub(super) fn decrypted(plaintext: &[u8], header: &Header) -> Option<Self> {
        if plaintext.len() > MAX_PLAINTEXT_BYTES {
            return None;
        }
        let value = strict_json::parse(plaintext).ok()?;
        let Value::Object(members) = &value else {
            return None;
        };
        let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
        names.sort_unstable();
        if names != GRANT_FIELDS {
            return None;
        }
        let secret = |key: &str, optional: bool| match value.get(key) {
            Some(Value::Null) if optional => Some(None),
            Some(Value::String(text)) if token(text) => Some(Some(Secret::new(text.clone()))),
            _ => None,
        };
        Some(Self {
            access_token: secret("access_token", false)??,
            refresh_token: secret("refresh_token", true)?,
            broker_lease: secret("broker_lease", true)?,
            scopes: header.scopes.clone(),
            expires_at: header.expires_at,
            integration: Identity::admit(value.get("integration")?).ok()?,
        })
    }
}
