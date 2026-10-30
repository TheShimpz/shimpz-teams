//! Team Actions (ADR-0123, plan phase R7, workstream W5): the durable Action journal and its uncertain-outcome
//! semantics, the exec RPC request bytes and its bounded frame exchange over [`team_core::exec::ExecEngine`], the
//! provider broker (ADR-0106), failure redaction, Stored Inputs, human requests, and file delivery. It replaces
//! `action/`.
//!
//! [`executor`] is the frozen seam the chat turn, the Routine run, and the direct invoke route drive.

pub mod executor;
pub mod journal;
pub mod result;
