//! The controller's HTTP surface: the frozen route table and the seam every service group answers through.
//!
//! The server itself (bounded admission, deadlines, strict parsing, bearer and assertion verification, body capture,
//! response caps, NDJSON framing, and the upgrade relay) is workstream W8's; it resolves a request with
//! [`route::resolve`], authorizes it by [`route::Authority`], and hands a [`service::Call`] to the
//! [`service::Services`] group the route names.

pub mod activity;
pub mod controller;
pub mod operation;
pub mod probe;
pub mod route;
pub mod server;
pub mod service;
pub mod token;
