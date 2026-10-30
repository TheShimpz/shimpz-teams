//! The pinned Unicode table: equal to the protocol's generated Python table, and deciding each published differential
//! code point as the vectors record.

mod common;

use std::path::PathBuf;

use assistant_protocol::catalog::public_str;
use assistant_protocol::python::{array, small_int};
use assistant_protocol::unicode::{UNICODE_VERSION, category};

#[test]
fn the_table_equals_the_published_python_table() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../protocol/assistant/v1/validators/unicode_category.py");
    let source = std::fs::read_to_string(path).expect("the reference table is readable");
    let body = |start: &str, end: &str| {
        let from = source.find(start).expect("section") + start.len();
        source[from..from + source[from..].find(end).expect("end")].to_owned()
    };
    let starts: Vec<u32> = body("STARTS = (", ")")
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(|item| item.parse().expect("start"))
        .collect();
    let categories: String = body("CATEGORIES = (", ")")
        .split('"')
        .skip(1)
        .step_by(2)
        .collect();
    assert_eq!(starts.len() * 2, categories.len());
    for (index, start) in starts.iter().enumerate() {
        let character = char::from_u32(*start).unwrap_or('\u{fffd}');
        if char::from_u32(*start).is_some() {
            assert_eq!(
                category(character),
                &categories[2 * index..2 * index + 2],
                "U+{start:04X}"
            );
        }
    }
    assert!(source.contains(&format!("UNICODE_VERSION = \"{UNICODE_VERSION}\"")));
}

#[test]
fn every_differential_code_point_decides_as_published() {
    let document = common::vectors("unicode-category.json");
    assert_eq!(common::text(&document, "unicode"), UNICODE_VERSION);
    for case in array(common::field(&document, "cases")).expect("cases") {
        let first =
            u32::try_from(small_int(common::field(case, "first")).expect("first")).expect("code");
        let last =
            u32::try_from(small_int(common::field(case, "last")).expect("last")).expect("code");
        for code in first..=last {
            let Some(character) = char::from_u32(code) else {
                continue;
            };
            assert_eq!(
                category(character),
                common::text(case, "category"),
                "U+{code:04X}"
            );
            assert_eq!(
                public_str(&character.to_string(), 1),
                common::flag(case, "public"),
                "U+{code:04X}"
            );
        }
    }
}
