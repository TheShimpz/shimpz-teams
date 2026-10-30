//! Team Routine logic (ADR-0123, plan phase R7, workstream W2): definitions, plans, schedules and dates, claims,
//! cursors, recordings, records, provenance, refinement, and reruns, as pure functions over the Team HTTP protocol
//! types, without I/O. It replaces `routine/`.

pub mod claim;
pub mod compose;
pub mod credential;
pub mod cursor;
pub mod date;
pub mod dates;
pub mod definition;
pub mod hold;
pub mod json;
pub mod pattern;
pub mod pin;
pub mod plan;
pub mod provenance;
pub mod record;
pub mod recording;
pub mod reference;
pub mod refine;
pub mod rerun;
pub mod runs;
pub mod schedule;
pub mod starts;
pub mod trace;

#[cfg(test)]
mod testing;
