//! A Team's model selection and learned memory, in Team's private inference volume; it never stores a secret.
//!
//! The provider and model registry is the reviewed `model_catalog.json`: a person may choose only a catalog model
//! marked for chat; a non-chat catalog model is one Brain runs itself (the Browser's). Each Team's records are named by
//! its id's SHA-256, so no id ever becomes a path: `<sha>.json` (schema 3) and `<sha>.knowledge.json` (schema 2), both
//! canonical JSON written atomically and durably.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_core::entropy::Entropy;
use team_protocol::identifiers::TEAM_ID_RE;
use team_protocol::payload::canonical_memory_changes;
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::value::{int, object_of, string};
use team_storage::json_record::{self, RecordError};
use team_storage::{durable, private};

use crate::memory::{self, Entry, Plan, Source};

/// The configuration record's schema.
pub const SCHEMA: i64 = 3;
/// The knowledge record's schema.
pub const KNOWLEDGE_SCHEMA: i64 = 2;
/// The configuration record's bound.
pub const MAX_CONFIG_BYTES: usize = 4096;
/// The knowledge record's bound: well above 32 memories and their provenance at their field bounds.
pub const MAX_KNOWLEDGE_BYTES: usize = 1024 * 1024;

const CATALOG: &str = include_str!("../model_catalog.json");

/// One catalog model: its id, its input and output prices in cents per million tokens, and whether a Team may chat
/// with it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Model {
    /// The model id.
    pub id: String,
    /// Input price, cents per million tokens.
    pub input_cents: u64,
    /// Output price, cents per million tokens.
    pub output_cents: u64,
    /// Whether a Team may choose it for chat.
    pub chat: bool,
}

fn parse_model(value: &Value) -> Option<Model> {
    let cents = |name: &str| match value.get(name) {
        Some(Value::Number(Number::Int(cents))) => u64::try_from(*cents).ok(),
        _ => None,
    };
    Some(Model {
        id: string(value.get("id")?)?.to_owned(),
        input_cents: cents("input_usd_per_million_cents")?,
        output_cents: cents("output_usd_per_million_cents")?,
        chat: matches!(value.get("chat")?, Value::Bool(true)),
    })
}

fn parse_catalog() -> Option<BTreeMap<String, Vec<Model>>> {
    let catalog = strict_json::parse(CATALOG.as_bytes()).ok()?;
    let Value::Array(providers) = catalog.get("providers")? else {
        return None;
    };
    providers
        .iter()
        .map(|provider| {
            let Value::Array(models) = provider.get("models")? else {
                return None;
            };
            let models = models.iter().map(parse_model).collect::<Option<Vec<_>>>()?;
            Some((string(provider.get("id")?)?.to_owned(), models))
        })
        .collect()
}

/// Every catalog provider and its models, in provider order.
pub static CATALOG_MODELS: LazyLock<BTreeMap<String, Vec<Model>>> =
    LazyLock::new(|| parse_catalog().expect("the reviewed model catalog parses"));

/// The model ids a person may choose for `provider`'s chat, or `None` for a provider outside the catalog.
#[must_use]
pub fn chat_models(provider: &str) -> Option<Vec<&'static str>> {
    CATALOG_MODELS.get(provider).map(|models| {
        models
            .iter()
            .filter(|model| model.chat)
            .map(|model| model.id.as_str())
            .collect()
    })
}

/// A provider model's catalog price, `(input, output)` cents per million tokens; an unknown or unnamed model is priced
/// as the provider's dearest. `None` for a provider outside the catalog.
#[must_use]
pub fn price(provider: &str, model: Option<&str>) -> Option<(u64, u64)> {
    let models = CATALOG_MODELS.get(provider)?;
    let known = model.and_then(|model| models.iter().find(|entry| entry.id == model));
    let chosen = known.or_else(|| {
        models.iter().reduce(|dearest, entry| {
            if entry.input_cents + entry.output_cents > dearest.input_cents + dearest.output_cents {
                entry
            } else {
                dearest
            }
        })
    })?;
    Some((chosen.input_cents, chosen.output_cents))
}

/// Why inference metadata was refused or its private store failed closed; it never carries a path or content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// The Team id is outside the Team protocol grammar.
    InvalidTeam,
    /// The provider is not a catalog provider.
    UnsupportedProvider,
    /// The model is not one of the provider's chat models.
    UnsupportedModel,
    /// The Team has no inference configuration yet.
    Missing,
    /// The configuration could not be read.
    Unavailable,
    /// The stored configuration is invalid.
    Invalid,
    /// The configuration could not be saved.
    NotSaved,
    /// The Team's knowledge could not be read.
    KnowledgeUnavailable,
    /// The stored knowledge is invalid.
    KnowledgeInvalid,
    /// A turn's memory changes or their source are invalid.
    ChangesInvalid,
    /// The Team's knowledge could not be saved.
    KnowledgeNotSaved,
    /// A record could not be removed.
    NotRemoved,
    /// The store could not be listed.
    NotListed,
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::InvalidTeam => "invalid Team id",
            Self::UnsupportedProvider => "provider must be one of ['anthropic', 'openai']",
            Self::UnsupportedModel => "model is not supported by the selected provider",
            Self::Missing => "Team inference configuration is not set",
            Self::Unavailable => "Team inference configuration is unavailable",
            Self::Invalid => "Team inference configuration is invalid",
            Self::NotSaved => "Team inference configuration could not be saved",
            Self::KnowledgeUnavailable => "Team knowledge is unavailable",
            Self::KnowledgeInvalid => "Team knowledge is invalid",
            Self::ChangesInvalid => "Team knowledge changes are invalid",
            Self::KnowledgeNotSaved => "Team knowledge could not be saved",
            Self::NotRemoved => "Team inference configuration could not be removed",
            Self::NotListed => "Team inference configuration could not be listed",
        })
    }
}

impl std::error::Error for ConfigError {}

/// A Team's model selection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InferenceConfig {
    /// The catalog provider.
    pub provider: String,
    /// One of its chat models.
    pub model: String,
}

/// Admits exactly one catalog provider and one of its chat models; nothing is defaulted or coerced.
///
/// # Errors
/// [`ConfigError::UnsupportedProvider`] or [`ConfigError::UnsupportedModel`].
pub fn normalize(provider: &Value, model: &Value) -> Result<InferenceConfig, ConfigError> {
    let provider = string(provider).ok_or(ConfigError::UnsupportedProvider)?;
    let models = chat_models(provider).ok_or(ConfigError::UnsupportedProvider)?;
    let model = string(model)
        .filter(|model| models.contains(model))
        .ok_or(ConfigError::UnsupportedModel)?;
    Ok(InferenceConfig {
        provider: provider.to_owned(),
        model: model.to_owned(),
    })
}

/// Every name this store writes: a Team's configuration, its learned knowledge, and their interrupted temporaries.
static OWNED_NAME_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(
        r"(?:[0-9a-f]{64}(?:\.knowledge)?\.json|\.[0-9a-f]{64}(?:\.knowledge)?\.json\.[0-9a-f]{16}\.tmp)\Z",
    )
});

fn team_id(value: &str) -> Result<&str, ConfigError> {
    if TEAM_ID_RE.is_full(value) {
        Ok(value)
    } else {
        Err(ConfigError::InvalidTeam)
    }
}

fn exactly(value: &Value, fields: &[&str]) -> bool {
    matches!(value, Value::Object(members)
        if members.len() == fields.len() && members.iter().all(|(name, _)| fields.contains(&name.as_str())))
}

/// The private inference store of every Team.
#[derive(Clone, Debug)]
pub struct InferenceConfigStore {
    root: PathBuf,
}

impl InferenceConfigStore {
    /// The store under `root`.
    #[must_use]
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    /// A Team's record, named by its id's SHA-256.
    #[must_use]
    pub fn path(&self, team_id: &str, suffix: &str) -> PathBuf {
        let mut name = String::with_capacity(64 + suffix.len());
        for byte in Sha256::digest(team_id.as_bytes()) {
            let _ = write!(name, "{byte:02x}");
        }
        name.push_str(suffix);
        self.root.join(name)
    }

    fn config_path(&self, team_id: &str) -> PathBuf {
        self.path(team_id, ".json")
    }

    fn knowledge_path(&self, team_id: &str) -> PathBuf {
        self.path(team_id, ".knowledge.json")
    }

    fn write(&self, target: &Path, value: &Value, maximum: usize) -> Result<(), RecordError> {
        durable::create_private_directory(&self.root).map_err(|_| RecordError::Unavailable)?;
        std::fs::set_permissions(
            &self.root,
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .map_err(|_| RecordError::Unavailable)?;
        json_record::write(target, value, maximum)
    }

    /// Saves the Team's model selection, re-admitting it first.
    ///
    /// # Errors
    /// [`ConfigError`] for an invalid Team or selection, or [`ConfigError::NotSaved`].
    pub fn save(
        &self,
        team: &str,
        config: &InferenceConfig,
    ) -> Result<InferenceConfig, ConfigError> {
        let team = team_id(team)?;
        let validated = normalize(
            &Value::String(config.provider.clone()),
            &Value::String(config.model.clone()),
        )?;
        let record = object_of(vec![
            ("schema", int(SCHEMA)),
            ("team_id", Value::String(team.to_owned())),
            ("provider", Value::String(validated.provider.clone())),
            ("model", Value::String(validated.model.clone())),
        ]);
        self.write(&self.config_path(team), &record, MAX_CONFIG_BYTES)
            .map_err(|_| ConfigError::NotSaved)?;
        Ok(validated)
    }

    /// The Team's model selection.
    ///
    /// # Errors
    /// [`ConfigError::Missing`] when none is saved; any other [`ConfigError`] when it cannot be trusted.
    pub fn load(&self, team: &str) -> Result<InferenceConfig, ConfigError> {
        let team = team_id(team)?;
        let value = json_record::read(&self.config_path(team), MAX_CONFIG_BYTES)
            .map_err(|error| match error {
                RecordError::Unavailable => ConfigError::Unavailable,
                RecordError::Invalid => ConfigError::Invalid,
            })?
            .ok_or(ConfigError::Missing)?;
        let schema = matches!(
            value.get("schema"),
            Some(Value::Number(Number::Int(SCHEMA)))
        );
        let owned = value.get("team_id").and_then(string) == Some(team);
        if !exactly(&value, &["schema", "team_id", "provider", "model"]) || !schema || !owned {
            return Err(ConfigError::Invalid);
        }
        normalize(
            &value.get("provider").cloned().unwrap_or(Value::Null),
            &value.get("model").cloned().unwrap_or(Value::Null),
        )
        .map_err(|_| ConfigError::Invalid)
    }

    fn memory(&self, team: &str) -> Result<Vec<Entry>, ConfigError> {
        let Some(value) = json_record::read(&self.knowledge_path(team), MAX_KNOWLEDGE_BYTES)
            .map_err(|_| ConfigError::KnowledgeUnavailable)?
        else {
            return Ok(Vec::new());
        };
        let schema = matches!(
            value.get("schema"),
            Some(Value::Number(Number::Int(KNOWLEDGE_SCHEMA)))
        );
        let owned = value.get("team_id").and_then(string) == Some(team);
        if !exactly(&value, &["schema", "team_id", "memory"]) || !schema || !owned {
            return Err(ConfigError::KnowledgeInvalid);
        }
        value
            .get("memory")
            .and_then(memory::canonical_entries)
            .filter(|entries| !entries.is_empty())
            .ok_or(ConfigError::KnowledgeInvalid)
    }

    /// The Team's learned memory as a turn carries it: topic and preference, never provenance (ADR-0121).
    ///
    /// # Errors
    /// [`ConfigError`] when the Team is invalid or its knowledge cannot be trusted.
    pub fn load_knowledge(&self, team: &str) -> Result<Vec<Value>, ConfigError> {
        Ok(memory::projection(&self.memory(team_id(team)?)?))
    }

    /// What one committed turn's memory changes leave, stamped with the turn's source at `now` (Unix seconds);
    /// nothing is written yet.
    ///
    /// # Errors
    /// [`ConfigError`] for an invalid Team, changes, or source, unreadable knowledge, or no entropy.
    pub fn plan_knowledge(
        &self,
        team: &str,
        changes: &Value,
        source: &Source,
        now: i64,
        entropy: &dyn Entropy,
    ) -> Result<Plan, ConfigError> {
        let team = team_id(team)?;
        let Some(Value::Array(admitted)) = canonical_memory_changes(changes) else {
            return Err(ConfigError::ChangesInvalid);
        };
        if !source.valid() {
            return Err(ConfigError::ChangesInvalid);
        }
        let entries = self.memory(team)?;
        memory::plan(
            &entries,
            &admitted,
            source,
            &private::instant_text(now),
            entropy,
        )
        .map_err(|_| ConfigError::KnowledgeNotSaved)
    }

    /// Keeps a planned memory in one atomic write; nothing learned, no file.
    ///
    /// # Errors
    /// [`ConfigError::KnowledgeNotSaved`] or [`ConfigError::NotRemoved`].
    pub fn save_knowledge(&self, team: &str, plan: &Plan) -> Result<(), ConfigError> {
        let team = team_id(team)?;
        let target = self.knowledge_path(team);
        if plan.memory.is_empty() {
            return unlink(&target);
        }
        let record = object_of(vec![
            ("schema", int(KNOWLEDGE_SCHEMA)),
            ("team_id", Value::String(team.to_owned())),
            (
                "memory",
                Value::Array(plan.memory.iter().map(Entry::record).collect()),
            ),
        ]);
        self.write(&target, &record, MAX_KNOWLEDGE_BYTES)
            .map_err(|_| ConfigError::KnowledgeNotSaved)
    }

    /// Removes the Team's inference configuration and its learned knowledge.
    ///
    /// # Errors
    /// [`ConfigError::InvalidTeam`] or [`ConfigError::NotRemoved`].
    pub fn delete(&self, team: &str) -> Result<(), ConfigError> {
        let team = team_id(team)?;
        unlink(&self.config_path(team))?;
        unlink(&self.knowledge_path(team))
    }

    /// Removes every Team's configuration and knowledge, including ones no Team network names now.
    ///
    /// # Errors
    /// [`ConfigError::NotListed`] or [`ConfigError::NotRemoved`].
    pub fn delete_all(&self) -> Result<(), ConfigError> {
        let listed = match std::fs::read_dir(&self.root) {
            Ok(listed) => listed,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err(ConfigError::NotListed),
        };
        let mut owned = Vec::new();
        for entry in listed {
            let entry = entry.map_err(|_| ConfigError::NotListed)?;
            if entry
                .file_name()
                .to_str()
                .is_some_and(|name| OWNED_NAME_RE.is_full(name))
            {
                owned.push(entry.path());
            }
        }
        owned.iter().try_for_each(|path| unlink(path))
    }
}

/// Durably removes one owned record: a returned removal survives power loss; an absent one already succeeded.
///
/// The parent directory is fsynced even when the record was already absent, so a retry after a failed sync still
/// commits the earlier removal; an absent store has nothing to commit.
fn unlink(path: &Path) -> Result<(), ConfigError> {
    match std::fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(ConfigError::NotRemoved),
    }
    let parent = path.parent().ok_or(ConfigError::NotRemoved)?;
    match durable::fsync_directory(parent) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(ConfigError::NotRemoved),
    }
}

#[cfg(test)]
mod tests;
