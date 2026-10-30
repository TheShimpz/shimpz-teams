//! Local chat (workstream W6): the Team's chat turns on its execution slot, their pauses for a person or an
//! Integration, Stop, the Space chat limit, capability planning and intent routing, and the Team's inference settings.

pub mod answer;
pub mod continuation;
pub mod continuation_store;
pub mod inference;
pub mod limit;
pub mod memory;
pub mod pause;
pub mod pending;
pub mod ports;
pub mod problem;
pub mod route;
mod routine;
pub mod segment;
pub mod service;
pub mod stop;
pub mod stored;
pub mod teardown;
#[cfg(test)]
pub(crate) mod tests;
pub mod turn;
pub mod usage;

/// Lowercase hex of `bytes`.
#[must_use]
pub fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(text, "{byte:02x}");
    }
    text
}
