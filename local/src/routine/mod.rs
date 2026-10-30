//! Local Routines (workstream W6): their persistence, claims, runs replayed with no model, recovery, incidents, cards,
//! proposals, notices, and diagnostics (ADR-0086, ADR-0092, ADR-0101).

pub mod card;
pub mod contracts;
pub mod diagnostics;
pub mod executor;
pub mod human;
pub mod incident;
pub mod lifecycle;
pub mod manage;
pub mod notices;
pub mod proposal;
pub mod protection;
pub mod recorder;
pub mod recovery;
pub mod refine;
pub mod replay;
pub mod run;
pub mod service;
pub mod state;
pub mod store;
#[cfg(test)]
mod testing;
pub mod watchdog;
