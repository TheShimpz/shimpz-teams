//! The published Action file input, file invocation, and invocation vectors.

mod common;

use assistant_protocol::Value;
use assistant_protocol::input_file::{
    decode_content, files_shape_error, input_files_error, invocation_files_error, valid_name,
};

#[test]
fn every_input_file_vector_agrees() {
    common::agree("input-file.json", |case| {
        input_files_error(common::field(case, "actions"))
    });
}

#[test]
fn every_file_invocation_vector_agrees() {
    common::agree("file-invocation.json", |case| {
        invocation_files_error(
            common::field(case, "action"),
            common::field(case, "invocation"),
        )
    });
}

#[test]
fn every_schema_valid_invocation_carries_admitted_files() {
    for case in common::cases("invocation.json") {
        let files = common::field(&case, "invocation").get("files");
        if common::flag(&case, "valid") && files.is_some() {
            assert_eq!(files_shape_error(files), None, "{}", common::name(&case));
        }
    }
    assert_eq!(files_shape_error(None), Some("files_invalid"));
    assert_eq!(files_shape_error(Some(&Value::Null)), Some("files_invalid"));
}

#[test]
fn content_decoding_is_canonical_and_bounded() {
    assert_eq!(decode_content("YWI="), Some(b"ab".to_vec()));
    assert_eq!(decode_content(""), Some(Vec::new()));
    for text in ["YWJ=", "YWI", "YW I=", "-_8=", "YQ==YQ==", "Y==="] {
        assert_eq!(decode_content(text), None, "{text}");
    }
    assert_eq!(decode_content(&"A".repeat(11_184_816)), None);
}

#[test]
fn names_are_literal_bounded_data() {
    assert!(valid_name("Relatório de março.pdf"));
    assert!(valid_name(&"a".repeat(255)));
    for name in [
        "",
        " a",
        "a ",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\u{0}",
        "a\u{7f}",
        &"é".repeat(128),
    ] {
        assert!(!valid_name(name), "{name:?}");
    }
}
