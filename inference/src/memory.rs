//! A Team's private memory entries: each memory with its provenance (ADR-0084, ADR-0121).
//!
//! Team keeps, beside each memory's topic and preference, an opaque id, the UTC time it committed the memory, and its
//! source: the signed request nonce of the turn that taught it, the verified person who sent that message, and the
//! Team incarnation. Brain never sees any of it; the turn context carries only topic and preference.

use strict_json::Value;
use team_core::entropy::{Entropy, EntropyUnavailable, token_hex};
use team_protocol::payload::{REQUEST_NONCE_RE, canonical_memory};
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::value::{object_of, string};
use team_protocol::sign_in::INCARNATION_RE;
use team_protocol::turn::MAX_MEMORIES;

use std::sync::LazyLock;

/// The activity operation of a remembered memory (ADR-0121).
pub const REMEMBER: &str = "memory-remember";
/// The activity operation of a forgotten memory.
pub const FORGET: &str = "memory-forget";
/// The activity operation of a memory evicted by the bound.
pub const EVICTED: &str = "memory-evicted";

const ENTRY_FIELDS: [&str; 5] = ["memory_id", "topic", "preference", "since", "source"];
const SOURCE_FIELDS: [&str; 3] = ["nonce", "actor_id", "incarnation"];
static OPAQUE_ID: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));
static SINCE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"));

/// Who taught a memory: the turn's signed request nonce, its verified author, and the Team incarnation.
///
/// Team captures it when a turn starts and carries it unchanged through every continuation, so a person who only
/// resumes a turn never becomes its author.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Source {
    /// The turn's signed request nonce.
    pub nonce: String,
    /// The verified person who sent the turn's message.
    pub actor_id: String,
    /// The Team incarnation.
    pub incarnation: String,
}

impl Source {
    /// The stored form.
    #[must_use]
    pub fn record(&self) -> Value {
        object_of(vec![
            ("nonce", Value::String(self.nonce.clone())),
            ("actor_id", Value::String(self.actor_id.clone())),
            ("incarnation", Value::String(self.incarnation.clone())),
        ])
    }

    /// Whether every part has its exact form.
    #[must_use]
    pub fn valid(&self) -> bool {
        REQUEST_NONCE_RE.is_full(&self.nonce)
            && OPAQUE_ID.is_full(&self.actor_id)
            && INCARNATION_RE.is_full(&self.incarnation)
    }
}

fn exactly<'v>(value: &'v Value, fields: &[&str]) -> Option<&'v [(String, Value)]> {
    let Value::Object(members) = value else {
        return None;
    };
    (members.len() == fields.len()
        && members
            .iter()
            .all(|(name, _)| fields.contains(&name.as_str())))
    .then_some(members.as_slice())
}

fn text(value: &Value, name: &str) -> Option<String> {
    value.get(name).and_then(string).map(str::to_owned)
}

/// One exact source from its stored form, or `None`.
#[must_use]
pub fn canonical_source(value: &Value) -> Option<Source> {
    exactly(value, &SOURCE_FIELDS)?;
    let source = Source {
        nonce: text(value, "nonce")?,
        actor_id: text(value, "actor_id")?,
        incarnation: text(value, "incarnation")?,
    };
    source.valid().then_some(source)
}

/// One private memory entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// Its opaque id.
    pub memory_id: String,
    /// Its topic.
    pub topic: String,
    /// Its preference line.
    pub preference: String,
    /// The UTC time Team committed it.
    pub since: String,
    /// Who taught it.
    pub source: Source,
}

impl Entry {
    /// The stored form.
    #[must_use]
    pub fn record(&self) -> Value {
        object_of(vec![
            ("memory_id", Value::String(self.memory_id.clone())),
            ("topic", Value::String(self.topic.clone())),
            ("preference", Value::String(self.preference.clone())),
            ("since", Value::String(self.since.clone())),
            ("source", self.source.record()),
        ])
    }
}

fn entry(value: &Value) -> Option<Entry> {
    exactly(value, &ENTRY_FIELDS)?;
    let memory_id = text(value, "memory_id").filter(|id| OPAQUE_ID.is_full(id))?;
    let since = text(value, "since").filter(|since| SINCE.is_full(since))?;
    Some(Entry {
        memory_id,
        topic: text(value, "topic")?,
        preference: text(value, "preference")?,
        since,
        source: canonical_source(value.get("source")?)?,
    })
}

/// The exact private entries of a knowledge record, or `None`: ADR-0084's memory, each with distinct provenance.
#[must_use]
pub fn canonical_entries(value: &Value) -> Option<Vec<Entry>> {
    let Value::Array(items) = value else {
        return None;
    };
    let entries = items.iter().map(entry).collect::<Option<Vec<_>>>()?;
    canonical_memory(&Value::Array(projection(&entries)))?;
    let distinct = entries.iter().enumerate().all(|(index, entry)| {
        entries[..index]
            .iter()
            .all(|earlier| earlier.memory_id != entry.memory_id)
    });
    distinct.then_some(entries)
}

/// The memory as a turn carries it to the Brain: topic and preference only, never provenance.
#[must_use]
pub fn projection(entries: &[Entry]) -> Vec<Value> {
    entries
        .iter()
        .map(|entry| {
            object_of(vec![
                ("topic", Value::String(entry.topic.clone())),
                ("preference", Value::String(entry.preference.clone())),
            ])
        })
        .collect()
}

/// One memory change's activity operation and the memory it concerns, never its topic or preference.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Change {
    /// [`REMEMBER`], [`FORGET`], or [`EVICTED`].
    pub operation: &'static str,
    /// The memory's id.
    pub memory_id: String,
}

/// The private entries one turn's changes leave, oldest first, and the change each one made.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Plan {
    /// The entries left.
    pub memory: Vec<Entry>,
    /// The changes made.
    pub changes: Vec<Change>,
}

/// Applies canonical `changes` (as `canonical_memory_changes` admits them) to `entries`, stamping each remembered
/// entry with a new id, `since`, and `source`, and naming each change.
///
/// A `remember` replaces any entry of its topic; a `forget` of a present topic removes it, one of an absent topic
/// changes nothing; the oldest entries beyond the bound are evicted.
///
/// # Errors
/// [`EntropyUnavailable`] when no id could be drawn.
pub fn plan(
    entries: &[Entry],
    changes: &[Value],
    source: &Source,
    since: &str,
    entropy: &dyn Entropy,
) -> Result<Plan, EntropyUnavailable> {
    let mut kept: Vec<Entry> = entries.to_vec();
    let mut made = Vec::new();
    for change in changes {
        let topic = text(change, "topic").unwrap_or_default();
        let removed = kept
            .iter()
            .position(|entry| entry.topic == topic)
            .map(|index| kept.remove(index));
        if change.get("op").and_then(string) == Some("remember") {
            let memory_id = token_hex(entropy, 16)?;
            made.push(Change {
                operation: REMEMBER,
                memory_id: memory_id.clone(),
            });
            kept.push(Entry {
                memory_id,
                topic,
                preference: text(change, "preference").unwrap_or_default(),
                since: since.to_owned(),
                source: source.clone(),
            });
        } else if let Some(removed) = removed {
            made.push(Change {
                operation: FORGET,
                memory_id: removed.memory_id,
            });
        }
    }
    let evicted = kept.len().saturating_sub(MAX_MEMORIES);
    made.extend(kept.drain(..evicted).map(|entry| Change {
        operation: EVICTED,
        memory_id: entry.memory_id,
    }));
    Ok(Plan {
        memory: kept,
        changes: made,
    })
}

#[cfg(test)]
mod tests;
