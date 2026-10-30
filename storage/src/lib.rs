//! Team storage (ADR-0123, plan phase R7, workstream W1): the AES-256-GCM private-state envelope (plain and
//! identified, with its `key_id`), keyring files, the record store, and the Team files SQLite database, each in its
//! current byte format. It replaces `storage/files.py` and `storage/private_state.py`.
