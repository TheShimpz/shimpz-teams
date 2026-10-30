//! Bring a finished Browser task's downloads into the Team's files (ADR-0117 §7).
//!
//! The listing `browser-host files list` answered is parsed strictly. Each complete file within the bounds is read
//! exactly, typed from its bytes, and stored as an untrusted Team file marked as from the web in the same write; every
//! other entry gets a closed reason, so the reply can say what was not kept. A Stop ends the ingest before the next
//! file.

use std::collections::BTreeSet;

use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser::{MAX_DOWNLOAD_NAMES, MAX_PENDING_DOWNLOADS, canonical_downloads};

/// The most files one task keeps.
pub const MAX_FILES: usize = 8;
/// The largest file kept.
pub const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;
/// The most entries of each list a listing may hold.
pub const MAX_LISTED: usize = 64;

/// The downloads listing broke its closed shape; nothing of it is trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ListingError;

impl std::fmt::Display for ListingError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the downloads listing is malformed")
    }
}

impl std::error::Error for ListingError {}

/// A download could not be read exactly; the Browser cannot be trusted for any later file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReadLost;

/// Why a download was not stored.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PutRefused {
    /// The turn was stopped: no further file is stored.
    Stopped,
    /// The task's time ran out: this file and every later one are lost.
    ReadLost,
    /// The Team's storage quota is full.
    QuotaExceeded,
    /// The file's name is not one Team stores.
    NameRefused,
    /// The store failed.
    Unavailable,
}

/// Where the downloads come from and go to.
pub trait DownloadSink: Send + Sync {
    /// The listed download `name`'s exact `size` bytes.
    ///
    /// # Errors
    /// [`ReadLost`] when they could not be read exactly.
    fn read<'a>(&'a self, name: &'a str, size: u64) -> BoxFuture<'a, Result<Vec<u8>, ReadLost>>;

    /// Stores `data` as a Team file from the web, its media type detected from its bytes, and returns its reference
    /// (`id`, `name`, `media_type`, `size`).
    ///
    /// # Errors
    /// [`PutRefused`] when it was not stored.
    fn put<'a>(&'a self, name: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<Value, PutRefused>>;
}

/// The kept files' references, the named files not kept with their reasons, and how many were unnamed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Ingested {
    /// Each kept file's `id`, `name`, `media_type`, and `size`.
    pub kept: Vec<Value>,
    /// Each named file not kept, and its closed reason.
    pub lost: Vec<(String, &'static str)>,
    /// How many entries the listing could not name.
    pub skipped: u64,
}

/// A listing's files with their sizes, its refused entries with their reasons, and its unnamed count.
type Entries = (Vec<(String, u64)>, Vec<(String, &'static str)>, u64);

fn size(value: Option<&Value>) -> Option<u64> {
    match value {
        Some(Value::Number(Number::Int(size))) => u64::try_from(*size).ok(),
        _ => None,
    }
}

fn name(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(name)) if !name.is_empty() => Some(name.clone()),
        _ => None,
    }
}

fn exactly(entry: &Value, keys: &[&str]) -> bool {
    matches!(entry, Value::Object(members) if members.len() == keys.len() && keys.iter().all(|key| entry.get(key).is_some()))
}

fn entries(listing: &Value) -> Result<Entries, ListingError> {
    let (Some(Value::Array(files)), Some(Value::Array(refused))) =
        (listing.get("files"), listing.get("refused"))
    else {
        return Err(ListingError);
    };
    let skipped = size(listing.get("skipped")).ok_or(ListingError)?;
    if files.len() > MAX_LISTED || refused.len() > MAX_LISTED {
        return Err(ListingError);
    }
    let named = files
        .iter()
        .map(|entry| {
            if !exactly(entry, &["name", "size"]) {
                return None;
            }
            Some((name(entry.get("name"))?, size(entry.get("size"))?))
        })
        .collect::<Option<Vec<_>>>()
        .ok_or(ListingError)?;
    let reasons = refused
        .iter()
        .map(|entry| {
            if !exactly(entry, &["name", "reason", "size"]) {
                return None;
            }
            let reason = match entry.get("reason") {
                Some(Value::String(reason)) if reason == "incomplete" => "incomplete",
                Some(Value::String(reason)) if reason == "over-limit" => "over-limit",
                _ => return None,
            };
            Some((name(entry.get("name"))?, reason))
        })
        .collect::<Option<Vec<_>>>()
        .ok_or(ListingError)?;
    Ok((named, reasons, skipped))
}

fn one(name: &str) -> Value {
    Value::Object(vec![
        (
            "files".to_owned(),
            Value::Array(vec![Value::String(name.to_owned())]),
        ),
        ("pending".to_owned(), Value::Number(Number::Int(0))),
        ("truncated".to_owned(), Value::Bool(false)),
    ])
}

/// What a step may know of the task's downloads so far: the completed files present, and how many still download.
///
/// Over-limit files are present too. A name outside the protocol's bound is left out and marks the list truncated, as
/// are names past the protocol's count and the entries the listing could not name.
///
/// # Errors
/// [`ListingError`] when the listing broke its shape.
pub fn present(listing: &Value) -> Result<Value, ListingError> {
    let (named, refused, skipped) = entries(listing)?;
    let present: BTreeSet<&str> = named
        .iter()
        .map(|(name, _)| name.as_str())
        .chain(
            refused
                .iter()
                .filter(|(_, reason)| *reason == "over-limit")
                .map(|(name, _)| name.as_str()),
        )
        .collect();
    let admitted: Vec<&str> = present
        .iter()
        .copied()
        .filter(|name| canonical_downloads(&one(name)).is_some())
        .collect();
    let truncated =
        skipped > 0 || admitted.len() < present.len() || admitted.len() > MAX_DOWNLOAD_NAMES;
    let pending = refused
        .iter()
        .filter(|(_, reason)| *reason == "incomplete")
        .count();
    let pending = i64::try_from(pending)
        .unwrap_or(i64::MAX)
        .min(MAX_PENDING_DOWNLOADS);
    Ok(Value::Object(vec![
        (
            "files".to_owned(),
            Value::Array(
                admitted
                    .iter()
                    .take(MAX_DOWNLOAD_NAMES)
                    .map(|name| Value::String((*name).to_owned()))
                    .collect(),
            ),
        ),
        ("pending".to_owned(), Value::Number(Number::Int(pending))),
        ("truncated".to_owned(), Value::Bool(truncated)),
    ]))
}

/// The four fields of a stored file's reference the task keeps.
fn reference(stored: &Value) -> Value {
    Value::Object(
        ["id", "name", "media_type", "size"]
            .into_iter()
            .map(|key| {
                (
                    key.to_owned(),
                    stored.get(key).cloned().unwrap_or(Value::Null),
                )
            })
            .collect(),
    )
}

/// Stores at most 8 downloads of at most 25 MiB each, one at a time; gives every other one its reason.
///
/// The sink marks each file as from the web as it stores it, and refuses once the turn was stopped, or once the task's
/// time ran out (that file and every later one are lost); no further file is then read or stored.
///
/// # Errors
/// [`ListingError`] when the listing broke its shape.
pub async fn ingest(
    listing: &Value,
    sink: &dyn DownloadSink,
    stop: &Stop,
) -> Result<Ingested, ListingError> {
    let (named, refused, skipped) = entries(listing)?;
    let mut ingested = Ingested {
        lost: refused,
        skipped,
        ..Ingested::default()
    };
    let browser_lost = |from: usize| {
        named[from..]
            .iter()
            .map(|(later, _)| (later.clone(), "browser-lost"))
            .collect::<Vec<_>>()
    };
    for (index, (name, size)) in named.iter().enumerate() {
        if stop.is_stopped() {
            break;
        }
        if index >= MAX_FILES || *size > MAX_FILE_BYTES {
            ingested.lost.push((name.clone(), "over-limit"));
            continue;
        }
        if *size == 0 {
            ingested.lost.push((name.clone(), "empty"));
            continue;
        }
        let Ok(data) = sink.read(name, *size).await else {
            ingested.lost.extend(browser_lost(index));
            break;
        };
        if stop.is_stopped() {
            // Stop arrived while the file was read: nothing more is stored.
            break;
        }
        match sink.put(name, data).await {
            Ok(stored) => ingested.kept.push(reference(&stored)),
            Err(PutRefused::Stopped) => break,
            Err(PutRefused::ReadLost) => {
                // The task's time ran out before the file was stored: it and every later file are lost.
                ingested.lost.extend(browser_lost(index));
                break;
            }
            Err(PutRefused::QuotaExceeded) => ingested.lost.push((name.clone(), "quota-exceeded")),
            Err(PutRefused::NameRefused) => ingested.lost.push((name.clone(), "name-refused")),
            Err(PutRefused::Unavailable) => ingested.lost.push((name.clone(), "store-unavailable")),
        }
    }
    Ok(ingested)
}

#[cfg(test)]
mod tests;
