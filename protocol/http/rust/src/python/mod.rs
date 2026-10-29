//! Python 3.14 semantics this protocol's validators depend on, each named after the Python operation it reproduces.

pub(crate) mod base64;
pub mod datetime;
pub mod json;
pub mod regex;
mod tables;
pub mod text;
pub mod value;
