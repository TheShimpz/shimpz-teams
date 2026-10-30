//! General categories and printability from the pinned Unicode table (`validators/unicode_category.py`), never from
//! this program's own Unicode data, so every reader decides the same.

use crate::unicode_table::{CATEGORIES, STARTS};

pub use crate::unicode_table::UNICODE_VERSION;

/// The categories `str.isprintable` refuses: separators and other characters, except the ASCII space.
const NOT_PRINTABLE: [&str; 8] = ["Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"];

/// The pinned two-letter general category of `character`.
#[must_use]
pub fn category(character: char) -> &'static str {
    let index = STARTS.partition_point(|start| *start <= u32::from(character)) - 1;
    &CATEGORIES[2 * index..2 * index + 2]
}

/// Whether every character is printable under the pinned table.
#[must_use]
pub fn printable(text: &str) -> bool {
    text.chars()
        .all(|character| character == ' ' || !NOT_PRINTABLE.contains(&category(character)))
}
