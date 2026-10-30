use sha2::{Digest, Sha256};
use strict_json::Value;

use super::{decoded, encoded_size, from_opaque, hex, image, page, pdf, text};
use crate::answer::{Answer, MediaType, Reason, opaque};
use crate::fixtures::{self, Format};
use crate::limits::MAX_TEXT_CHARACTERS;

fn parsed(json: &str) -> Value {
    strict_json::parse(json.as_bytes()).expect("a JSON fixture")
}

fn answered(data: &[u8]) -> Value {
    let derivative = crate::image::prepare(data).expect("prepared");
    Answer::Image(derivative).to_value()
}

fn unreadable() -> Value {
    opaque(Reason::Unreadable)
}

#[test]
fn a_valid_image_answer_gains_the_digest_of_its_bytes() {
    let answer = answered(&fixtures::image(Format::Png, 40, 20, false));
    let content = image(&answer);
    let Some(Value::String(encoded)) = content.get("base64") else {
        panic!("an image content");
    };
    let bytes = decoded(encoded).expect("canonical base64");
    assert_eq!(
        content.get("sha256"),
        Some(&Value::String(hex(&Sha256::digest(&bytes))))
    );
    assert_eq!(content.get("width"), answer.get("width"));
}

#[test]
fn untrusted_helper_answers_are_revalidated() {
    let png = fixtures::image(Format::Png, 8, 8, false);
    let png_text = crate::answer::base64_text(&png);
    let jpeg_text = crate::answer::base64_text(&fixtures::image(Format::Jpeg, 8, 8, false));
    for bogus in [
        r#"{"type":"image","media_type":"image/gif","width":8,"height":8,"base64":"AAAA"}"#
            .to_owned(),
        r#"{"type":"image","media_type":"image/png","width":4000,"height":8,"base64":"AAAA"}"#
            .to_owned(),
        r#"{"type":"image","media_type":"image/png","width":8,"height":8,"base64":"not base64"}"#
            .to_owned(),
        format!(
            r#"{{"type":"image","media_type":"image/jpeg","width":8,"height":8,"base64":"{png_text}"}}"#
        ),
        format!(
            r#"{{"type":"image","media_type":"image/png","width":8,"height":9,"base64":"{png_text}"}}"#
        ),
        format!(
            r#"{{"type":"image","media_type":"image/jpeg","width":9,"height":8,"base64":"{jpeg_text}"}}"#
        ),
        format!(
            r#"{{"type":"image","media_type":"image/png","width":8.0,"height":8,"base64":"{png_text}"}}"#
        ),
        r#"{"type":"image","media_type":"image/png","width":8,"height":8,"base64":"","extra":1}"#
            .to_owned(),
        r#"{"type":"opaque","reason":"made_up"}"#.to_owned(),
        r#"{"type":"text","text":"an image answered with text"}"#.to_owned(),
        "[1]".to_owned(),
    ] {
        assert_eq!(image(&parsed(&bogus)), unreadable(), "{bogus}");
    }
    let valid = format!(
        r#"{{"type":"image","media_type":"image/png","width":8,"height":8,"base64":"{png_text}"}}"#
    );
    assert_eq!(
        image(&parsed(&valid)).get("type"),
        Some(&Value::String("image".to_owned()))
    );
}

#[test]
fn base64_must_be_canonical() {
    assert_eq!(decoded("YWJ="), None);
    assert_eq!(decoded("YWI="), Some(b"ab".to_vec()));
    assert_eq!(decoded("YW I="), None);
}

#[test]
fn helper_answers_outside_their_shapes_become_closed_opaque_reasons() {
    assert_eq!(text(None, false), unreadable());
    assert_eq!(text(Some("  \n"), true), opaque(Reason::NoText));
    let long = "x".repeat(MAX_TEXT_CHARACTERS + 1);
    assert_eq!(
        pdf(&Answer::Text(long).to_value()),
        opaque(Reason::TooLarge)
    );
    assert_eq!(
        pdf(&Answer::Text("Report body".to_owned()).to_value()),
        parsed(r#"{"type":"text","text":"Report body","pdf":true}"#)
    );
    assert_eq!(pdf(&opaque(Reason::Encrypted)), opaque(Reason::Encrypted));
    assert_eq!(
        from_opaque(&parsed(r#"{"type":"opaque","reason":"maybe"}"#)),
        unreadable()
    );
    assert_eq!(
        from_opaque(&parsed(r#"{"type":"opaque","reason":"no_text","x":1}"#)),
        unreadable()
    );
}

#[test]
fn a_page_answer_names_a_count_from_its_page_to_the_ceiling() {
    let derivative =
        crate::image::prepare(&fixtures::image(Format::Png, 8, 8, false)).expect("prepared");
    let (content, count) =
        page(&Answer::Page(derivative.clone(), 3).to_value(), 2).expect("admitted");
    assert_eq!(
        (content.get("type"), count),
        (Some(&Value::String("image".to_owned())), 3)
    );
    for (count, requested) in [(1, 2), (51, 1)] {
        assert_eq!(
            page(
                &Answer::Page(derivative.clone(), count).to_value(),
                requested
            ),
            None
        );
    }
    assert_eq!(page(&Answer::Image(derivative).to_value(), 1), None);
    assert_eq!(page(&Value::Array(Vec::new()), 1), None);
}

#[test]
fn the_encoded_size_is_read_from_the_header_alone() {
    let jpeg = fixtures::image(Format::Jpeg, 30, 20, false);
    assert_eq!(encoded_size(MediaType::Jpeg, &jpeg), Some((30, 20)));
    assert_eq!(
        encoded_size(MediaType::Png, &fixtures::image(Format::Png, 7, 5, false)),
        Some((7, 5))
    );
    assert_eq!(
        encoded_size(MediaType::Png, b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR"),
        None
    );
    // Fill bytes, a standalone marker, and an application segment come before the frame header.
    let padded = [&b"\xff\xd8\xff\xff\xd0\xff\xe0\x00\x04ab"[..], &jpeg[2..]].concat();
    assert_eq!(encoded_size(MediaType::Jpeg, &padded), Some((30, 20)));
    // A scan before any frame header, and a truncated frame header, declare nothing.
    assert_eq!(
        encoded_size(MediaType::Jpeg, b"\xff\xd8\xff\xda\x00\x02"),
        None
    );
    assert_eq!(
        encoded_size(MediaType::Jpeg, b"\xff\xd8\xff\xc0\x00\x11\x08\x00"),
        None
    );
}
