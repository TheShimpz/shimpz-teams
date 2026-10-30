use super::{Detected, Kind, OCTET_STREAM, decode_text, detect, valid_json};
use crate::fixtures::{self, Format};
use crate::limits::MAX_TEXT_SOURCE_BYTES;

#[test]
fn images_and_pdfs_are_recognized_by_their_bytes_alone() {
    for (data, kind, media_type) in [
        (
            fixtures::image(Format::Jpeg, 4, 4, false),
            Kind::Image,
            "image/jpeg",
        ),
        (
            fixtures::image(Format::Png, 4, 4, false),
            Kind::Image,
            "image/png",
        ),
        (
            fixtures::image(Format::Webp, 4, 4, false),
            Kind::Image,
            "image/webp",
        ),
        (fixtures::text_pdf(&["hello"]), Kind::Pdf, "application/pdf"),
    ] {
        assert_eq!(detect("document.txt", &data), Detected { kind, media_type });
    }
}

#[test]
fn text_subtypes_follow_the_extension_only_for_valid_text() {
    assert_eq!(detect("a.csv", b"a,b\n1,2\n").media_type, "text/csv");
    assert_eq!(
        detect("a.md", "# Título\n".as_bytes()).media_type,
        "text/markdown"
    );
    assert_eq!(
        detect("a.JSON", br#"{"a": [1, 2]}"#).media_type,
        "application/json"
    );
    assert_eq!(detect("a.json", b"{not json").media_type, "text/plain");
    assert_eq!(detect("main.py", b"print('hi')\n").media_type, "text/plain");
    assert_eq!(detect(".md", b"hidden").media_type, "text/plain");
    assert_eq!(detect("a.txt", b"\xef\xbb\xbfwith a BOM").kind, Kind::Text);
}

#[test]
fn binary_and_disguised_bytes_are_opaque() {
    let opaque = Detected {
        kind: Kind::Opaque,
        media_type: OCTET_STREAM,
    };
    for (name, data) in [
        ("photo.jpg", &b"\x00\x01\x02binary"[..]),
        ("notes.txt", b"\xff\xfe invalid utf-8"),
        ("notes.txt", b"text with a NUL\x00 byte"),
        ("notes.txt", b"text with an escape \x1b[31m"),
        ("notes.txt", b"delete \x7f"),
        ("image.gif", fixtures::GIF),
        ("empty.txt", b""),
        ("bom.txt", b"\xef\xbb\xbf"),
    ] {
        assert_eq!(detect(name, data), opaque, "{name}");
    }
}

#[test]
fn text_decoding_and_json_detection_stay_within_the_text_ceiling() {
    assert_eq!(decode_text(&vec![b'a'; MAX_TEXT_SOURCE_BYTES + 1]), None);
    assert_eq!(decode_text(b"\xc3("), None);
    assert_eq!(decode_text(b"\xef\xbb\xbf"), None);
    assert_eq!(decode_text("\u{feff}é".as_bytes()), Some("é"));
    let wide = [b"[".as_slice(), &vec![b' '; MAX_TEXT_SOURCE_BYTES], b"]"].concat();
    assert!(!valid_json(&wide));
    // A long file classifies as text from its prefix alone, even with a character split at the ceiling.
    let split = [
        vec![b'a'; MAX_TEXT_SOURCE_BYTES - 1],
        "étail".as_bytes().to_vec(),
    ]
    .concat();
    assert_eq!(detect("a.txt", &split).kind, Kind::Text);
    let broken = [vec![b'a'; MAX_TEXT_SOURCE_BYTES - 1], b"\xff\xfe".to_vec()].concat();
    assert_eq!(detect("a.txt", &broken).kind, Kind::Opaque);
}
