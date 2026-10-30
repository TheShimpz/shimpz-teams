//! Encrypted, controller-owned OAuth tokens for Assistant Integrations (`integrations/store.py`).
//!
//! Tokens never enter an Assistant manifest, a Brain prompt, a process environment or argument, a public answer, or a
//! log. Each record seals its tokens in an identified envelope whose associated data binds the exact Team, Assistant,
//! Integration, provider, scopes, expiry, status, and generation:
//! `["shimpz-oauth-integration-v1", team, assistant, integration, provider, [scopes], expires_at, status, generation]`
//! in canonical JSON. The state file is `assistant-integrations/state/integrations.json`, its keyring
//! `assistant-integrations/key/aes256.key`. A refresh or replacement of one Integration runs single-flight; another
//! Integration never waits for it.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use strict_json::{Number, Value};
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_core::secret::Secret;
use team_protocol::python::json;
use team_storage::envelope::{Bounds, Envelope};
use team_storage::private::{self, STATE_JSON, StateError};
use team_storage::record_store::{Outcome, RecordPolicy, RecordStore, State};

use crate::challenge::team_id;
use crate::providers::{self, identifier};
use crate::token::{MAX_TOKEN_BYTES, TokenSet};

mod flight;
mod record;

pub use record::{Grant, Identity};

/// The largest state file.
pub const MAX_STATE_BYTES: usize = 4 * 1024 * 1024;
/// The largest sealed plaintext.
pub const MAX_PLAINTEXT_BYTES: usize = MAX_TOKEN_BYTES * 3 + 2048;
/// The most Integrations one Assistant of one Team holds.
pub const MAX_INTEGRATIONS_PER_ASSISTANT: usize = 16;
/// The most records the whole store holds.
pub const MAX_TOTAL_RECORDS: usize = 4096;
/// The longest provider account id.
pub const MAX_INTEGRATION_ID_BYTES: usize = 256;
/// The longest provider account name or username.
pub const MAX_INTEGRATION_TEXT_BYTES: usize = 512;
/// A token expiring within this many seconds is refreshed before it is used.
pub const REFRESH_WINDOW_SECONDS: i64 = 60;
/// The latest expiry a record admits (JavaScript's largest safe integer).
pub const MAX_EXPIRY: i64 = (1 << 53) - 1;
const AAD_LABEL: &str = "shimpz-oauth-integration-v1";
const BOUNDS: Bounds = Bounds {
    maximum_encoded_part: MAX_PLAINTEXT_BYTES * 2 + 128,
    maximum_plaintext: MAX_PLAINTEXT_BYTES,
};

/// Why an Integration operation was refused; never carries a token, a path, or file content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreError {
    /// The caller supplied invalid data (an id, a declaration, a token set, or an identity).
    Invalid,
    /// The Integration was never connected.
    Missing,
    /// The person must authorize the Integration again: it was demoted, its declaration changed, it has no refresh
    /// token, or it changed while it was refreshed.
    Reauthorization,
    /// The Assistant already holds its most Integrations.
    Capacity,
    /// The provider boundary could not revoke a grant.
    Revocation,
    /// The broker exchange or refresh failed; nothing was sealed.
    Exchange,
    /// The state, keyring, or an envelope is unavailable, unsafe, malformed, or unauthentic.
    State(StateError),
}

impl From<StateError> for StoreError {
    fn from(error: StateError) -> Self {
        Self::State(error)
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid => formatter.write_str("the OAuth Integration data is invalid"),
            Self::Missing => formatter.write_str("the OAuth Integration is not configured"),
            Self::Reauthorization => {
                formatter.write_str("the OAuth Integration requires reauthorization")
            }
            Self::Capacity => formatter.write_str("the OAuth Integration capacity is reached"),
            Self::Revocation => formatter.write_str("the OAuth grant could not be revoked"),
            Self::Exchange => formatter.write_str("the OAuth exchange failed"),
            Self::State(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for StoreError {}

/// One declared Integration: its reviewed provider and requested scopes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Declaration {
    /// The provider id.
    pub provider: String,
    /// The requested scopes, in any order.
    pub scopes: Vec<String>,
}

/// A record's stored status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stored {
    /// Usable.
    Connected,
    /// Demoted: a replacement is underway or failed.
    ReauthorizationRequired,
}

impl Stored {
    fn text(self) -> &'static str {
        match self {
            Self::Connected => "connected",
            Self::ReauthorizationRequired => "reauthorization-required",
        }
    }

    fn parse(text: &str) -> Option<Self> {
        match text {
            "connected" => Some(Self::Connected),
            "reauthorization-required" => Some(Self::ReauthorizationRequired),
            _ => None,
        }
    }
}

/// One declared Integration's public status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    /// Never connected.
    Missing,
    /// Usable now.
    Connected,
    /// Expired, but a refresh token can renew it without a person.
    RefreshRequired,
    /// A person must authorize it again.
    ReauthorizationRequired,
}

impl Status {
    /// Its wire text.
    #[must_use]
    pub fn text(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Connected => "connected",
            Self::RefreshRequired => "refresh-required",
            Self::ReauthorizationRequired => "reauthorization-required",
        }
    }
}

/// One declared Integration's bounded public inventory row; it never holds a token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Metadata {
    /// The Integration id.
    pub id: String,
    /// The declared provider.
    pub provider: String,
    /// The declared scopes, sorted.
    pub scopes: Vec<String>,
    /// Its status.
    pub status: Status,
    /// The provider account it is connected to, when known.
    pub integration: Option<Identity>,
    /// When its access token expires (epoch seconds), once connected.
    pub expires_at: Option<i64>,
    /// Its store-wide generation, 0 when missing.
    pub generation: u64,
}

/// The grant a replacement supersedes or a failed seal leaves unsealed, handed to the revocation boundary.
pub struct Revocation<'a> {
    /// The provider.
    pub provider: &'a str,
    /// The access token.
    pub access_token: &'a Secret,
    /// The refresh token, when the grant has one.
    pub refresh_token: Option<&'a Secret>,
    /// The broker lease, when the grant has one.
    pub broker_lease: Option<&'a Secret>,
}

/// The reviewed provider boundary that revokes a grant; it treats an unknown or already-revoked token as revoked.
pub trait Revoke: Send + Sync {
    /// Revokes the grant.
    ///
    /// # Errors
    /// [`StoreError::Revocation`] when the boundary could not revoke it.
    fn revoke<'a>(
        &'a self,
        grant: Revocation<'a>,
    ) -> team_core::future::BoxFuture<'a, Result<(), StoreError>>;
}

/// The record policy of the Integration store.
#[derive(Clone, Copy, Debug, Default)]
pub struct Policy;

impl RecordPolicy for Policy {
    fn maximum_state_bytes(&self) -> usize {
        MAX_STATE_BYTES
    }

    fn records_per_assistant(&self) -> usize {
        MAX_INTEGRATIONS_PER_ASSISTANT
    }

    fn maximum_records(&self) -> usize {
        MAX_TOTAL_RECORDS
    }

    fn team_id(&self, id: &str) -> bool {
        team_id(id)
    }

    fn assistant_id(&self, id: &str) -> bool {
        assistant_id(id)
    }

    fn record_id(&self, id: &str) -> bool {
        identifier(id)
    }

    fn record_generation(&self, record: &Value) -> Option<u64> {
        record::Header::admit(record).map(|header| header.generation)
    }
}

pub(crate) fn assistant_id(text: &str) -> bool {
    team_protocol::identifiers::canonical_assistant_id(&Value::String(text.to_owned())).is_some()
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

/// The admitted identity of one Integration: Team, Assistant, Integration.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Reference {
    team: String,
    assistant: String,
    integration: String,
}

impl Reference {
    fn admit(team: &str, assistant: &str, integration: &str) -> Result<Self, StoreError> {
        if !team_id(team) || !assistant_id(assistant) || !identifier(integration) {
            return Err(StoreError::Invalid);
        }
        Ok(Self {
            team: team.to_owned(),
            assistant: assistant.to_owned(),
            integration: integration.to_owned(),
        })
    }

    fn aad(&self, header: &record::Header) -> Vec<u8> {
        json::canonical(&Value::Array(vec![
            Value::String(AAD_LABEL.to_owned()),
            Value::String(self.team.clone()),
            Value::String(self.assistant.clone()),
            Value::String(self.integration.clone()),
            Value::String(header.provider.clone()),
            Value::Array(header.scopes.iter().cloned().map(Value::String).collect()),
            int(header.expires_at),
            Value::String(header.status.text().to_owned()),
            int(i64::try_from(header.generation).unwrap_or(i64::MAX)),
        ]))
    }
}

/// The reviewed intent of one declaration.
///
/// # Errors
/// [`StoreError::Invalid`] for an unreviewed provider or scope set.
pub fn intent(
    provider: &str,
    scopes: &[String],
) -> Result<(&'static str, Vec<String>), StoreError> {
    let intent = providers::intent(provider, scopes).map_err(|_| StoreError::Invalid)?;
    Ok((intent.provider.id, intent.scopes))
}

/// The encrypted Integration store.
pub struct IntegrationStore {
    records: RecordStore<Policy>,
    clock: Arc<dyn Clock>,
    flights: Mutex<HashMap<Reference, Arc<tokio::sync::Mutex<()>>>>,
}

impl std::fmt::Debug for IntegrationStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("IntegrationStore")
            .field("records", &self.records)
            .finish_non_exhaustive()
    }
}

impl IntegrationStore {
    /// The store whose state lives at `state_path` and keyring at `key_path`.
    ///
    /// # Errors
    /// [`StoreError::State`] unless both paths are absolute and their directories differ.
    pub fn new(
        state_path: PathBuf,
        key_path: PathBuf,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, StoreError> {
        Ok(Self {
            records: RecordStore::new(state_path, key_path, Policy, entropy)?,
            clock,
            flights: Mutex::new(HashMap::new()),
        })
    }

    fn now(&self) -> i64 {
        private::epoch_seconds(self.clock.as_ref())
    }

    /// Seals one exchanged token set and advances its generation, atomically.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] for invalid input, [`StoreError::Capacity`] for a full Assistant, or a state failure.
    pub fn put(
        &self,
        reference: (&str, &str, &str),
        declaration: (&str, &[String]),
        tokens: &TokenSet,
        identity: Option<&Identity>,
    ) -> Result<Metadata, StoreError> {
        let reference = Reference::admit(reference.0, reference.1, reference.2)?;
        let (provider, scopes) = intent(declaration.0, declaration.1)?;
        let grant = Grant::from_tokens(tokens, &scopes, self.now(), identity)?;
        self.seal_new(&reference, provider, &grant)
    }

    fn seal_new(
        &self,
        reference: &Reference,
        provider: &'static str,
        grant: &Grant,
    ) -> Result<Metadata, StoreError> {
        let plaintext = grant.plaintext()?;
        self.records.update(|state| {
            let key = self.records.key(!state.has_records())?;
            let full = state
                .records(&reference.team, &reference.assistant)
                .is_some_and(|records| {
                    records.len() >= MAX_INTEGRATIONS_PER_ASSISTANT
                        && !records.iter().any(|(id, _)| *id == reference.integration)
                });
            if full {
                return Err(StoreError::Capacity);
            }
            let generation = state.advance_generation();
            let header = record::Header {
                provider: provider.to_owned(),
                scopes: grant.scopes.clone(),
                expires_at: grant.expires_at,
                status: Stored::Connected,
                generation,
            };
            let sealed = self.seal(reference, &header, &plaintext, &key)?;
            set_record(state, reference, sealed);
            Ok(Outcome::Changed(Metadata {
                id: reference.integration.clone(),
                provider: provider.to_owned(),
                scopes: grant.scopes.clone(),
                status: Status::Connected,
                integration: grant.integration.clone(),
                expires_at: Some(grant.expires_at),
                generation,
            }))
        })
    }

    fn seal(
        &self,
        reference: &Reference,
        header: &record::Header,
        plaintext: &[u8],
        key: &team_storage::envelope::Key,
    ) -> Result<Value, StoreError> {
        let envelope = Envelope::seal_identified(
            key,
            plaintext,
            &reference.aad(header),
            self.records.entropy(),
        )
        .map_err(|_| StoreError::State(StateError::Unavailable))?;
        Ok(header.to_value(
            &private::timestamp(self.clock.as_ref()),
            envelope.to_value(),
        ))
    }

    fn open(
        &self,
        reference: &Reference,
        record: &Value,
    ) -> Result<(record::Header, Grant), StoreError> {
        let header =
            record::Header::admit(record).ok_or(StoreError::State(StateError::Malformed))?;
        let envelope = record
            .get("envelope")
            .and_then(|value| Envelope::admit_identified(value, BOUNDS).ok())
            .ok_or(StoreError::State(StateError::Malformed))?;
        let key = self.records.key(false)?;
        let plaintext = envelope
            .open(&key, &reference.aad(&header))
            .map_err(|_| StoreError::State(StateError::Unauthentic))?;
        let grant = Grant::decrypted(&plaintext, &header)
            .ok_or(StoreError::State(StateError::Malformed))?;
        Ok((header, grant))
    }

    /// Every declared Integration's inventory row, missing ones included, in declaration order.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] for invalid declarations; a state failure for anything stored that does not open.
    pub fn metadata(
        &self,
        team: &str,
        assistant: &str,
        declarations: &[(String, Declaration)],
    ) -> Result<Vec<Metadata>, StoreError> {
        let declared = admit_declarations(declarations)?;
        if !team_id(team) || !assistant_id(assistant) {
            return Err(StoreError::Invalid);
        }
        let now = self.now();
        self.records.read(|state| {
            declared
                .iter()
                .map(|(integration, provider, scopes)| {
                    let reference = Reference::admit(team, assistant, integration)?;
                    let stored = state
                        .records(team, assistant)
                        .and_then(|records| records.iter().find(|(id, _)| id == integration));
                    match stored {
                        None => Ok(missing(integration, provider, scopes)),
                        Some((_, record)) => self.row(&reference, record, (provider, scopes), now),
                    }
                })
                .collect()
        })
    }

    fn row(
        &self,
        reference: &Reference,
        record: &Value,
        (provider, scopes): (&str, &[String]),
        now: i64,
    ) -> Result<Metadata, StoreError> {
        let (header, grant) = self.open(reference, record)?;
        let drifted = header.provider != provider || header.scopes != scopes;
        let usable = !drifted && header.status == Stored::Connected;
        let status = if usable && header.expires_at > now {
            Status::Connected
        } else if usable && grant.refresh_token.is_some() {
            Status::RefreshRequired
        } else {
            Status::ReauthorizationRequired
        };
        Ok(Metadata {
            id: reference.integration.clone(),
            provider: provider.to_owned(),
            scopes: scopes.to_vec(),
            status,
            integration: if drifted { None } else { grant.integration },
            expires_at: Some(header.expires_at),
            generation: header.generation,
        })
    }

    /// Demotes the Integration to `reauthorization-required` (same generation) and returns its provider and grant for
    /// revocation; `None` when it was never connected.
    ///
    /// # Errors
    /// A state failure.
    fn demote(&self, reference: &Reference) -> Result<Option<(String, Grant)>, StoreError> {
        self.records.update(|state| {
            let Some(record) = stored(state, reference) else {
                return Ok(Outcome::Unchanged(None));
            };
            let (mut header, grant) = self.open(reference, record)?;
            header.status = Stored::ReauthorizationRequired;
            let key = self.records.key(false)?;
            let sealed = self.seal(reference, &header, &grant.plaintext()?, &key)?;
            set_record(state, reference, sealed);
            Ok(Outcome::Changed(Some((header.provider, grant))))
        })
    }

    /// Discards the Integrations the Assistant's current release no longer declares; whether any were.
    ///
    /// # Errors
    /// A state failure.
    pub fn retain_declared(
        &self,
        team: &str,
        assistant: &str,
        declared: &[&str],
    ) -> Result<bool, StoreError> {
        Ok(self.records.retain_declared(team, assistant, declared)?)
    }

    /// Deletes one Assistant's Integrations of one Team; whether it had any.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_assistant(&self, team: &str, assistant: &str) -> Result<bool, StoreError> {
        Ok(self.records.delete_assistant(team, assistant)?)
    }

    /// Deletes one Team's Integrations; whether it had any.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_team(&self, team: &str) -> Result<bool, StoreError> {
        Ok(self.records.delete_team(team)?)
    }

    /// Deletes every Integration during an owned Space reset; whether any existed.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_all(&self) -> Result<bool, StoreError> {
        Ok(self.records.delete_all()?)
    }
}

fn stored<'a>(state: &'a State, reference: &Reference) -> Option<&'a Value> {
    state
        .records(&reference.team, &reference.assistant)?
        .iter()
        .find(|(id, _)| *id == reference.integration)
        .map(|(_, record)| record)
}

fn set_record(state: &mut State, reference: &Reference, sealed: Value) {
    let records = state.records_mut(&reference.team, &reference.assistant);
    match records
        .iter_mut()
        .find(|(id, _)| *id == reference.integration)
    {
        Some((_, record)) => *record = sealed,
        None => records.push((reference.integration.clone(), sealed)),
    }
}

fn missing(integration: &str, provider: &str, scopes: &[String]) -> Metadata {
    Metadata {
        id: integration.to_owned(),
        provider: provider.to_owned(),
        scopes: scopes.to_vec(),
        status: Status::Missing,
        integration: None,
        expires_at: None,
        generation: 0,
    }
}

type Declared = (String, &'static str, Vec<String>);

fn admit_declarations(declarations: &[(String, Declaration)]) -> Result<Vec<Declared>, StoreError> {
    if declarations.len() > MAX_INTEGRATIONS_PER_ASSISTANT {
        return Err(StoreError::Invalid);
    }
    let mut admitted: Vec<Declared> = Vec::with_capacity(declarations.len());
    for (id, declaration) in declarations {
        if !identifier(id) || admitted.iter().any(|(seen, _, _)| seen == id) {
            return Err(StoreError::Invalid);
        }
        let (provider, scopes) = intent(&declaration.provider, &declaration.scopes)?;
        admitted.push((id.clone(), provider, scopes));
    }
    Ok(admitted)
}

/// The Python writer's plaintext options: `ensure_ascii`, `sort_keys`, compact.
fn plaintext_json(value: &Value) -> Vec<u8> {
    json::dumps(value, STATE_JSON).into_bytes()
}

#[cfg(test)]
pub(crate) mod tests;
