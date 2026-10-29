//! The Team HTTP protocol v1 in Rust.
//!
//! Each module reproduces the Python module of the same name in `teams/protocol/http/v1`, which stays the authority
//! while Team is Python: the same public constants by name, and validators that take a strict JSON value and return
//! exactly the value Python returns (members in Python's order) or refuse it. `vectors.json` and a seeded
//! differential harness against the Python modules prove the equality; [`python`] holds the Python semantics they
//! share.

mod base64;
pub mod browser;
pub mod browser_view;
pub mod challenge;
pub mod content;
pub mod identifiers;
pub mod payload;
pub mod phrase;
pub mod preview;
pub mod progress;
pub mod purpose;
pub mod python;
pub mod routine;
pub mod routine_context;
pub mod routine_notice;
pub mod routine_proposal;
pub mod routine_run;
pub mod sign_in;
pub mod supervisor;
pub mod turn;
pub mod websocket;
