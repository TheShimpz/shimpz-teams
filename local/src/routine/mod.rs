//! Local Routines (workstream W6): their persistence, claims, runs replayed with no model, recovery, incidents, cards,
//! proposals, notices, and diagnostics (ADR-0086, ADR-0092, ADR-0101).

pub mod contracts;
pub mod proposal;
pub mod recorder;
pub mod refine;
pub mod diagnostics;
pub mod executor;
pub mod manage;
pub mod notices;
pub mod protection;
pub mod state;
pub mod store;
#[cfg(test)]
mod testing;
