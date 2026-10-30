//! Team Routine logic (ADR-0123, plan phase R7, workstream W2): definitions, plans, schedules and dates, claims,
//! cursors, recordings, records, provenance, refinement, and reruns, as pure functions over the Team HTTP protocol
//! types, without I/O. It replaces `routine/`.

pub mod credential;
pub mod date;
pub mod dates;
pub mod json;
pub mod pattern;
pub mod plan;
pub mod schedule;
pub mod starts;

#[cfg(test)]
mod testing;
