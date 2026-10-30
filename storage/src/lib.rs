//! Team storage (ADR-0123, plan phase R7, workstream W1), each in its current byte format: file custody and the one
//! strict read of a Team-held file, durable atomic writes, canonical JSON records, the AES-256-GCM private-state
//! envelope (plain and identified, with its `key_id`), keyring files, the encrypted record store, and the Team files
//! SQLite database. It replaces `storage/files.py` and `storage/private_state.py`.

pub mod custody;
pub mod durable;
pub mod envelope;
pub mod json_record;
pub mod private;
pub mod record_store;
