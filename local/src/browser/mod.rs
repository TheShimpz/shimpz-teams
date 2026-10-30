//! The Local Team Browser (ADR-0117; plan phase R7, workstream W7): the Space's one Browser container and its complete
//! envelope, its exec channels, the Cua Driver sessions a task runs on, the watched and controlled views, a Team's
//! Sign-in and its capsule, and the `Browser` route group.
//!
//! The Browser image and the `browser` egress profile stay their own crates; this module only runs them.

pub mod channel;
