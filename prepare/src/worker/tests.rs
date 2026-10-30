use strict_json::Value;

use super::{answer, run};
use crate::answer::{Answer, MediaType, Reason};
use crate::fixtures::{self, Format, pdfium_library};
use crate::limits::MAX_HELPER_INPUT_BYTES;
use crate::request::{Kind, encode};

#[test]
fn each_kind_is_answered_by_its_own_path() {
    let library = pdfium_library();
    let image = answer(
        &encode(
            Kind::Image,
            &fixtures::image(Format::Png, 64, 32, true),
            None,
        ),
        &library,
    );
    assert!(
        matches!(image, Answer::Image(ref derivative) if derivative.media_type == MediaType::Png)
    );
    let text = answer(
        &encode(Kind::Pdf, &fixtures::text_pdf(&["Hello"]), None),
        &library,
    );
    assert_eq!(text, Answer::Text("Hello".to_owned()));
    let page = answer(
        &encode(Kind::PdfPage, &fixtures::text_pdf(&["One", "Two"]), Some(2)),
        &library,
    );
    assert!(matches!(page, Answer::Page(_, 2)));
}

#[test]
fn a_misframed_request_is_unreadable() {
    let unreadable = Answer::Opaque(Reason::Unreadable);
    assert_eq!(
        answer(b"no header separator", &pdfium_library()),
        unreadable
    );
    assert_eq!(
        answer(
            br#"{"kind":"image","size":2}
x"#,
            &pdfium_library()
        ),
        unreadable
    );
}

#[test]
fn the_entrypoint_writes_exactly_one_json_answer() {
    let mut output = Vec::new();
    let request = encode(
        Kind::Pdf,
        &fixtures::text_pdf(&["Hello from the helper"]),
        None,
    );
    run(request.as_slice(), &mut output, &pdfium_library()).expect("answered");
    let parsed = strict_json::parse(&output).expect("one JSON document");
    assert_eq!(
        parsed.get("text"),
        Some(&Value::String("Hello from the helper".to_owned()))
    );

    let mut output = Vec::new();
    let oversized = vec![b'x'; MAX_HELPER_INPUT_BYTES + 1];
    run(oversized.as_slice(), &mut output, &pdfium_library()).expect("answered");
    assert_eq!(output, br#"{"type":"opaque","reason":"unreadable"}"#);
}
