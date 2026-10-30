//! Durable chat turns (ADR-0125; plan B0 and B3): each turn Admin sends is recorded under its send's request
//! identity before it runs, so a turn survives a Team or Admin restart and its terminal is delivered exactly once.
//!
//! A turn is `running` from its admission, with its latest checkpoint (the secret-free continuation of a segment,
//! written after every Brain answer and before the Action receipts it supersedes are deleted); `ended` with its
//! terminal, kept until Admin acknowledges that its history committed it; then `delivered`, a tombstone of its
//! identity alone, so a repeated send is recognized and never run again. A send of a known identity attaches to the
//! turn still running in this process, receives the kept terminal, or, for a turn a previous Team process left
//! running, resumes it from its checkpoint, never from its start once a checkpoint exists. Only the session the turn
//! was admitted under may attach, acknowledge, or revoke it, and only with the same body; the turn's authority lasts
//! at most [`GRANT_SECONDS`] from its send, so no turn outlives it while Admin cannot stop it.
//!
//! Each Team's turns are one owner-only state file `<team_id>.json` on the chat-continuation state volume; a turn's
//! checkpoint and terminal are sealed under the chat-continuation keyring, the rest is identity and state.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_core::entropy::Entropy;
use team_protocol::identifiers::TEAM_ID_RE;
use team_protocol::python::json::{Options, canonical, dumps};
use team_protocol::python::value::{int, object_of, string};
use team_storage::envelope::{Bounds, Envelope};
use team_storage::private::{Keyring, read_private, write_private_json};
use tokio::sync::watch;

use super::super::hex;
use crate::http::service::Problem;

/// The state file's schema.
pub const SCHEMA_VERSION: i64 = 1;
/// The most turns one Team keeps: one running at a time, the rest ended or delivered.
pub const MAX_TURNS: usize = 16;
/// How long a turn's authority lasts from its send without Admin: Admin's session idle bound.
pub const GRANT_SECONDS: i64 = 3600;
/// The largest sealed plaintext of one turn.
pub const MAX_PLAINTEXT_BYTES: usize = 512 * 1024;
/// The largest state file.
pub const MAX_STATE_BYTES: usize = 12 * 1024 * 1024;
const AAD: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};
const BOUNDS: Bounds = Bounds {
    maximum_encoded_part: MAX_PLAINTEXT_BYTES * 2 + 128,
    maximum_plaintext: MAX_PLAINTEXT_BYTES,
};
const STATES: [&str; 3] = ["running", "ended", "delivered"];

/// A turn's terminal record: its status and body, exactly as its first stream ended.
#[derive(Clone, Debug, PartialEq)]
pub struct Terminal {
    /// The HTTP status.
    pub status: u16,
    /// The body.
    pub body: Value,
}

/// Who sent a turn and what: the session it was admitted under and the digest of its exact body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sender {
    /// The send's nonce.
    pub nonce: String,
    /// When it was sent, in seconds.
    pub issued_at: i64,
    /// The SHA-256 of the session it was admitted under.
    pub authority: String,
    /// The SHA-256 of its canonical body.
    pub body: String,
}

impl Sender {
    /// The sender of `body` (a chat body whose request identity names `nonce` and `issued_at`) under `authority`.
    #[must_use]
    pub fn of(nonce: &str, issued_at: i64, authority: &str, body: &Value) -> Self {
        Self {
            nonce: nonce.to_owned(),
            issued_at,
            authority: authority.to_owned(),
            body: hex(&Sha256::digest(canonical(body))),
        }
    }

    /// When the turn's authority ends, in seconds.
    #[must_use]
    pub const fn grant_deadline(&self) -> i64 {
        self.issued_at.saturating_add(GRANT_SECONDS)
    }
}

/// What a send of a turn comes to.
#[derive(Debug)]
pub enum Claim {
    /// A new turn: it runs now.
    New(Live),
    /// The same turn runs in this process: its terminal once it ends.
    Attached(watch::Receiver<Option<Terminal>>),
    /// The same turn ended: its kept terminal again.
    Ended(Terminal),
    /// The turn a previous Team process left running: it resumes from its checkpoint, from its start when none.
    Orphaned(Live, Option<Value>),
}

/// The running turn's hold in this process: dropped without an ending, the turn is left for a later send to resume.
#[derive(Debug)]
pub struct Live {
    turns: Arc<Turns>,
    key: (String, String),
}

impl Drop for Live {
    fn drop(&mut self) {
        self.turns.live().remove(&self.key);
    }
}

impl Live {
    /// The Team.
    #[must_use]
    pub fn team_id(&self) -> &str {
        &self.key.0
    }

    /// The send's nonce.
    #[must_use]
    pub fn nonce(&self) -> &str {
        &self.key.1
    }
}

/// The turns running in this process, by Team and nonce, each with the channel its terminal is handed on.
type Running = HashMap<(String, String), watch::Sender<Option<Terminal>>>;

/// One Team's turn as recorded.
#[derive(Clone, Debug)]
struct Record {
    sender: Sender,
    state: &'static str,
    sealed: Option<Value>,
}

/// What a turn's sealed part holds.
#[derive(Clone, Debug, Default)]
struct Content {
    checkpoint: Option<Value>,
    terminal: Option<Terminal>,
}

fn problem(status: u16, message: &'static str, code: &'static str) -> Problem {
    Problem::new(status, message, code)
}

fn unavailable() -> Problem {
    problem(503, "Team chat turns are unavailable", "turns-unavailable")
}

/// Every Team's durable turns.
pub struct Turns {
    root: PathBuf,
    keyring: Keyring,
    entropy: Arc<dyn Entropy>,
    lock: Mutex<()>,
    live: Mutex<Running>,
}

impl std::fmt::Debug for Turns {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Turns")
            .field("root", &self.root)
            .finish_non_exhaustive()
    }
}

fn aad(team_id: &str, nonce: &str) -> Vec<u8> {
    let parts = Value::Array(vec![
        Value::String("shimpz-local-chat-turn-v1".to_owned()),
        Value::String(team_id.to_owned()),
        Value::String(nonce.to_owned()),
    ]);
    dumps(&parts, AAD).into_bytes()
}

fn terminal_value(terminal: &Terminal) -> Value {
    object_of(vec![
        ("status", int(i64::from(terminal.status))),
        ("body", terminal.body.clone()),
    ])
}

fn terminal_of(value: &Value) -> Option<Terminal> {
    let Some(Value::Number(Number::Int(status))) = value.get("status") else {
        return None;
    };
    Some(Terminal {
        status: u16::try_from(*status).ok()?,
        body: value.get("body")?.clone(),
    })
}

fn record_value(record: &Record) -> Value {
    object_of(vec![
        ("nonce", Value::String(record.sender.nonce.clone())),
        ("issued_at", int(record.sender.issued_at)),
        ("authority", Value::String(record.sender.authority.clone())),
        ("body", Value::String(record.sender.body.clone())),
        ("state", Value::String(record.state.to_owned())),
        ("sealed", record.sealed.clone().unwrap_or(Value::Null)),
    ])
}

fn record_of(value: &Value) -> Option<Record> {
    let text = |name: &str| value.get(name).and_then(string).map(str::to_owned);
    let Some(Value::Number(Number::Int(issued_at))) = value.get("issued_at") else {
        return None;
    };
    let state = text("state")?;
    let state = STATES.iter().copied().find(|known| *known == state)?;
    let sealed = match value.get("sealed")? {
        Value::Null => None,
        sealed => Some(sealed.clone()),
    };
    Some(Record {
        sender: Sender {
            nonce: text("nonce")?,
            issued_at: *issued_at,
            authority: text("authority")?,
            body: text("body")?,
        },
        state,
        sealed,
    })
}

impl Turns {
    /// The turns under `root`, their sealed parts under the keyring `key`.
    #[must_use]
    pub fn new(root: &Path, key: &Path, entropy: Arc<dyn Entropy>) -> Self {
        Self {
            root: root.to_path_buf(),
            keyring: Keyring::new(key.to_path_buf()),
            entropy,
            lock: Mutex::new(()),
            live: Mutex::new(HashMap::new()),
        }
    }

    fn guard(&self) -> MutexGuard<'_, ()> {
        self.lock.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn live(&self) -> MutexGuard<'_, Running> {
        self.live.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn path(&self, team_id: &str) -> Result<PathBuf, Problem> {
        if TEAM_ID_RE.is_full(team_id) {
            Ok(self.root.join(format!("{team_id}.json")))
        } else {
            Err(unavailable())
        }
    }

    fn read(&self, team_id: &str) -> Result<Vec<Record>, Problem> {
        let path = self.path(team_id)?;
        let payload = read_private(&path, MAX_STATE_BYTES as u64).map_err(|_| unavailable())?;
        let Some(payload) = payload else {
            return Ok(Vec::new());
        };
        let state = strict_json::parse(&payload).map_err(|_| unavailable())?;
        let schema = matches!(
            state.get("schema"),
            Some(Value::Number(Number::Int(SCHEMA_VERSION)))
        );
        let Some(Value::Array(turns)) = state.get("turns").filter(|_| schema) else {
            return Err(unavailable());
        };
        turns
            .iter()
            .map(|value| record_of(value).ok_or_else(unavailable))
            .collect()
    }

    fn write(&self, team_id: &str, records: &[Record]) -> Result<(), Problem> {
        let state = object_of(vec![
            ("schema", int(SCHEMA_VERSION)),
            (
                "turns",
                Value::Array(records.iter().map(record_value).collect()),
            ),
        ]);
        write_private_json(&self.path(team_id)?, &state, MAX_STATE_BYTES).map_err(|_| unavailable())
    }

    fn seal(
        &self,
        team_id: &str,
        nonce: &str,
        content: &Content,
    ) -> Result<Option<Value>, Problem> {
        if content.checkpoint.is_none() && content.terminal.is_none() {
            return Ok(None);
        }
        let plaintext = object_of(vec![
            (
                "checkpoint",
                content.checkpoint.clone().unwrap_or(Value::Null),
            ),
            (
                "terminal",
                content
                    .terminal
                    .as_ref()
                    .map_or(Value::Null, terminal_value),
            ),
        ]);
        let payload = dumps(&plaintext, AAD).into_bytes();
        if payload.len() > MAX_PLAINTEXT_BYTES {
            return Err(problem(
                413,
                "the chat turn exceeds its durable bound",
                "turn-too-large",
            ));
        }
        let key = self
            .keyring
            .load(true, self.entropy.as_ref())
            .map_err(|_| unavailable())?;
        let envelope = Envelope::seal(&key, &payload, &aad(team_id, nonce), self.entropy.as_ref())
            .map_err(|_| unavailable())?;
        Ok(Some(envelope.to_value()))
    }

    fn open(&self, team_id: &str, record: &Record) -> Result<Content, Problem> {
        let Some(sealed) = &record.sealed else {
            return Ok(Content::default());
        };
        let envelope = Envelope::admit(sealed, BOUNDS).map_err(|_| unavailable())?;
        let key = self
            .keyring
            .load(false, self.entropy.as_ref())
            .map_err(|_| unavailable())?;
        let plaintext = envelope
            .open(&key, &aad(team_id, &record.sender.nonce))
            .map_err(|_| unavailable())?;
        let value = strict_json::parse(&plaintext).map_err(|_| unavailable())?;
        let checkpoint = match value.get("checkpoint") {
            Some(Value::Null) | None => None,
            Some(checkpoint) => Some(checkpoint.clone()),
        };
        let terminal = match value.get("terminal") {
            Some(Value::Null) | None => None,
            Some(terminal) => Some(terminal_of(terminal).ok_or_else(unavailable)?),
        };
        Ok(Content {
            checkpoint,
            terminal,
        })
    }

    /// Makes room for one more turn: delivered turns go first, oldest first, then turns whose authority ended.
    fn room(records: &mut Vec<Record>, now: i64) -> Result<(), Problem> {
        while records.len() >= MAX_TURNS {
            let delivered = records
                .iter()
                .position(|record| record.state == "delivered");
            let expired = || {
                records
                    .iter()
                    .position(|record| record.sender.grant_deadline() <= now)
            };
            match delivered.or_else(expired) {
                Some(index) => {
                    records.remove(index);
                }
                None => {
                    return Err(problem(
                        429,
                        "the Team holds too many undelivered chat turns",
                        "turns-full",
                    ));
                }
            }
        }
        Ok(())
    }

    fn hold(self: &Arc<Self>, team_id: &str, nonce: &str) -> Live {
        let (sender, _) = watch::channel(None);
        let key = (team_id.to_owned(), nonce.to_owned());
        self.live().insert(key.clone(), sender);
        Live {
            turns: Arc::clone(self),
            key,
        }
    }

    /// Admits one send of a turn at `now` (seconds): a new turn is recorded running before anything of it runs.
    ///
    /// # Errors
    /// 403 `turn-authority` for another session, 409 `turn-changed` for another body under the same identity, 409
    /// `turn-delivered` for a turn already delivered, 410 `authority-expired` past the turn's grant, and 503 when the
    /// turns cannot be read or written.
    pub fn claim(
        self: &Arc<Self>,
        team_id: &str,
        sender: &Sender,
        now: i64,
    ) -> Result<Claim, Problem> {
        if now >= sender.grant_deadline() {
            return Err(problem(
                410,
                "the chat turn's authority expired",
                "authority-expired",
            ));
        }
        let _guard = self.guard();
        let mut records = self.read(team_id)?;
        let Some(index) = records
            .iter()
            .position(|record| record.sender.nonce == sender.nonce)
        else {
            Self::room(&mut records, now)?;
            records.push(Record {
                sender: sender.clone(),
                state: "running",
                sealed: None,
            });
            self.write(team_id, &records)?;
            return Ok(Claim::New(self.hold(team_id, &sender.nonce)));
        };
        self.existing(team_id, &records[index], sender)
    }

    /// A send of a turn this Team already recorded.
    fn existing(
        self: &Arc<Self>,
        team_id: &str,
        record: &Record,
        sender: &Sender,
    ) -> Result<Claim, Problem> {
        if record.sender.authority != sender.authority {
            return Err(problem(
                403,
                "the chat turn belongs to another session",
                "turn-authority",
            ));
        }
        if record.sender != *sender {
            return Err(problem(
                409,
                "the chat turn was sent with another body",
                "turn-changed",
            ));
        }
        match record.state {
            "delivered" => Err(problem(
                409,
                "the chat turn was delivered",
                "turn-delivered",
            )),
            "ended" => {
                let terminal = self
                    .open(team_id, record)?
                    .terminal
                    .ok_or_else(unavailable)?;
                Ok(Claim::Ended(terminal))
            }
            _ => {
                let key = (team_id.to_owned(), sender.nonce.clone());
                if let Some(running) = self.live().get(&key) {
                    return Ok(Claim::Attached(running.subscribe()));
                }
                let checkpoint = self.open(team_id, record)?.checkpoint;
                Ok(Claim::Orphaned(
                    self.hold(team_id, &sender.nonce),
                    checkpoint,
                ))
            }
        }
    }

    /// Records the running turn's latest checkpoint.
    ///
    /// # Errors
    /// 503 when it could not be made durable: the turn must not go on past it.
    pub fn checkpoint(&self, live: &Live, checkpoint: Value) -> Result<(), Problem> {
        let _guard = self.guard();
        let mut records = self.read(live.team_id())?;
        let record = records
            .iter_mut()
            .find(|record| record.sender.nonce == live.nonce() && record.state == "running")
            .ok_or_else(unavailable)?;
        let content = Content {
            checkpoint: Some(checkpoint),
            terminal: None,
        };
        record.sealed = self.seal(live.team_id(), live.nonce(), &content)?;
        self.write(live.team_id(), &records)
    }

    /// Records the turn's terminal, kept until Admin acknowledges it, and hands it to every attached send.
    ///
    /// # Errors
    /// 503 when it could not be made durable; attached sends still receive it.
    pub fn end(&self, live: &Live, terminal: &Terminal) -> Result<(), Problem> {
        let recorded = {
            let _guard = self.guard();
            self.read(live.team_id()).and_then(|mut records| {
                let record = records
                    .iter_mut()
                    .find(|record| record.sender.nonce == live.nonce())
                    .ok_or_else(unavailable)?;
                if record.state != "running" {
                    // Revoked meanwhile: nothing of it is delivered.
                    return Ok(());
                }
                let content = Content {
                    checkpoint: None,
                    terminal: Some(terminal.clone()),
                };
                record.sealed = self.seal(live.team_id(), live.nonce(), &content)?;
                record.state = "ended";
                self.write(live.team_id(), &records)
            })
        };
        if let Some(running) = self.live().get(&live.key) {
            running.send_replace(Some(terminal.clone()));
        }
        recorded
    }

    /// Settles a turn as delivered (Admin's history committed it) or revoked (its session ended): only a tombstone of
    /// its identity is kept. Whether it was running in this process, which a revocation then stops, and the
    /// checkpoint it held, whose Browser task the caller discards.
    ///
    /// # Errors
    /// 403 for another session, 404 for an unknown turn, 503 when the turns cannot be read or written.
    pub fn settle(
        &self,
        team_id: &str,
        nonce: &str,
        authority: &str,
    ) -> Result<(bool, Option<Value>), Problem> {
        let _guard = self.guard();
        let mut records = self.read(team_id)?;
        let Some(record) = records
            .iter_mut()
            .find(|record| record.sender.nonce == nonce)
        else {
            return Err(problem(404, "the chat turn is unknown", "turn-unknown"));
        };
        if record.sender.authority != authority {
            return Err(problem(
                403,
                "the chat turn belongs to another session",
                "turn-authority",
            ));
        }
        let running = record.state == "running";
        let checkpoint = if running {
            self.open(team_id, record)?.checkpoint
        } else {
            None
        };
        record.state = "delivered";
        record.sealed = None;
        self.write(team_id, &records)?;
        let live = self
            .live()
            .contains_key(&(team_id.to_owned(), nonce.to_owned()));
        Ok((running && live, checkpoint))
    }

    /// Deletes a Team's turns, for its teardown; an absent file is success.
    ///
    /// # Errors
    /// 503 when it may remain.
    pub fn forget_team(&self, team_id: &str) -> Result<(), Problem> {
        let _guard = self.guard();
        match std::fs::remove_file(self.path(team_id)?) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(unavailable()),
        }
    }

    /// Deletes every Team's turns, for the Space reset.
    ///
    /// # Errors
    /// 503 when one may remain.
    pub fn forget_all(&self) -> Result<(), Problem> {
        let _guard = self.guard();
        match std::fs::remove_dir_all(&self.root) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(unavailable()),
        }
    }
}

#[cfg(test)]
mod tests;
