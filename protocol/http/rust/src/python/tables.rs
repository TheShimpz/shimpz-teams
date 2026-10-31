//! The Unicode tables generated from Python 3.14.

mod case;
mod category;

pub(crate) use case::{CASE_IGNORABLE, CASED, CASEFOLD, LOWER, WHITESPACE};
pub(crate) use category::CATEGORY_RUNS;
