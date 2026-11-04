//! The Local Team Browser (ADR-0117, ADR-0126): the Space's one Browser session on easi.run, the Cua calls a task makes
//! on it, the watched and controlled views, a Team's Sign-in, and the `Browser` route group.

pub mod audit;
pub mod easi;
#[cfg(test)]
pub(crate) mod fake;
pub mod http;
pub mod runtime;
pub mod service;
pub mod sign_in;
pub mod sign_ins;
pub mod teardown;
pub mod view;
