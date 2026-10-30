//! Team Browser tasks (ADR-0123, plan phase R7, workstream W7; ADR-0125): the task as a durable Holdpoint run and its
//! Brain calls, the Cua Driver MCP client, the thread and its fold, opened links, the provenance screen, and downloads. It replaces the
//! Python modules of `browser/`; the Browser image and the `browser` egress profile stay their own crates.

pub mod brain;
pub mod cua;
pub mod ingest;
pub mod read;
pub mod run;
pub mod screen;
#[cfg(test)]
mod testing;
pub mod thread;
