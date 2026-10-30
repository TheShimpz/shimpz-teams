//! Team inference (ADR-0123, plan phase R7, workstream W6): the client of the Brain runtime over the `brain_runtime`
//! network with the Brain token, the Team's inference settings and learned memory, usage, and request aborts. It
//! replaces `inference/`.
//!
//! [`brain`] is the frozen seam chat turns, Routine segments, capability planning, intent routing, purposes, Browser
//! tasks, and Team deletion call the Brain through; [`runtime`] is a chat turn's typed Brain operations over it.

pub mod brain;
pub mod client;
pub mod config;
pub mod credential;
pub mod memory;
pub mod runtime;
#[cfg(any(test, feature = "testing"))]
pub mod testing;
pub mod token;
pub mod usage;
