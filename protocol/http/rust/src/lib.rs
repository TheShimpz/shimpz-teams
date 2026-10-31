//! The Team HTTP protocol v1 in Rust.
//!
//! Each module owns one part of the protocol: public constants by name, and validators that take a strict JSON value
//! and return its canonical value (members in order) or refuse it. `teams/protocol/http/v1/vectors.json` pins them;
//! [`python`] holds the Python semantics the protocol was first written with, which it keeps exactly.

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
