//! The pinned `unicode-normalization` agrees with Python 3.14 (Unicode 16.0.0) on every code point:
//! `tests/fixtures/unicode.txt`, generated from Python 3.14, lists every non-zero canonical combining class
//! and every code point NFC or NFD changes, and every other code point is unchanged by both with class zero.

use std::collections::HashMap;
use std::path::Path;

use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::canonical_combining_class;

fn points(fields: &[&str]) -> String {
    fields
        .iter()
        .map(|hex| {
            char::from_u32(u32::from_str_radix(hex, 16).expect("hex")).expect("a scalar value")
        })
        .collect()
}

#[test]
fn normalization_matches_python_on_every_code_point() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/unicode.txt");
    let fixture = std::fs::read_to_string(path).expect("the Unicode fixture");
    let mut classes: HashMap<char, u8> = HashMap::new();
    let mut forms: HashMap<(&str, char), String> = HashMap::new();
    for line in fixture.lines().filter(|line| !line.starts_with('#')) {
        let fields: Vec<&str> = line.split(' ').collect();
        let character = points(&fields[1..2])
            .chars()
            .next()
            .expect("one code point");
        if fields[0] == "ccc" {
            classes.insert(character, fields[2].parse().expect("a class"));
        } else {
            forms.insert((fields[0], character), points(&fields[2..]));
        }
    }
    let mut checked = 0;
    for character in (0..=0x10_ffff).filter_map(char::from_u32) {
        let text = character.to_string();
        assert_eq!(
            canonical_combining_class(character),
            classes.get(&character).copied().unwrap_or(0)
        );
        let nfc: String = text.nfc().collect();
        let nfd: String = text.nfd().collect();
        assert_eq!(
            nfc,
            *forms.get(&("nfc", character)).unwrap_or(&text),
            "{character:?}"
        );
        assert_eq!(
            nfd,
            *forms.get(&("nfd", character)).unwrap_or(&text),
            "{character:?}"
        );
        checked += 1;
    }
    assert_eq!(checked, 0x11_0000 - 0x800);
}
