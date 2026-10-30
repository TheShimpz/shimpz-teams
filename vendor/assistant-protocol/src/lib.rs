//! The Assistant protocol v1 reference rules (`protocol/assistant/v1/validators`) on strict JSON values.
//!
//! Each module reproduces one reference validator of the published Assistant protocol, and its golden vectors prove
//! the equality: [`route`] (a Stored Input's reviewed routes and Team's call-time match), [`failure`] (the sanitized
//! Action failure envelope), [`catalog`] (message catalogs, displayed copy, rendering, and language packs),
//! [`human_request`] (human requests, copy references, and replay transcripts), [`action_effect`] (effect classes,
//! verifiers, and idempotency), and [`input_file`] (Action file inputs and their invocation delivery). Every function
//! takes a decoded strict JSON value and answers the reference's stable reason code, or `None` when it admits the
//! value. [`unicode`] is the pinned Unicode 16.0.0 table the text rules read, and [`python`] the Python value
//! semantics the reference relies on.
//!
//! Developers' publication applies its own encoded-size guard before these rules; Team applies them to what it reads
//! and receives. Neither implementation's other policy lives here.

pub mod action_effect;
pub mod catalog;
pub mod failure;
pub mod human_request;
pub mod input_file;
pub mod python;
pub mod route;
pub mod unicode;
mod unicode_table;

pub use strict_json::{Number, Value};
