use strict_json::Value;

use super::{Attachment, LimitError, Plan, admit_message, admit_selection, plan, unread};
use crate::answer::{Reason, member, opaque};
use crate::content;
use crate::detect::detect;
use crate::fixtures::{self, Format};
use crate::limits::{
    MAX_ATTACHMENTS_FIELD_BYTES, MAX_SELECTED_ORIGINAL_BYTES, MAX_TEXT_CHARACTERS,
    MAX_TEXT_SOURCE_BYTES,
};
use crate::request::Kind;

fn attachment(index: usize, content: Value) -> Attachment {
    Attachment {
        id: format!("{index:032x}"),
        name: format!("{index}.bin"),
        media_type: "text/plain".to_owned(),
        size: 1,
        sha256: "0".repeat(64),
        content,
    }
}

fn planned(name: &str, data: &[u8]) -> Plan {
    plan(data, detect(name, data))
}

#[test]
fn text_is_read_in_the_controller_and_images_and_pdfs_go_to_the_helper() {
    assert_eq!(
        planned("notes.md", "# Ação\nItem".as_bytes()),
        Plan::Ready(content::text(Some("# Ação\nItem"), false))
    );
    assert_eq!(
        planned("a.png", &fixtures::image(Format::Png, 4, 4, false)),
        Plan::Helper(Kind::Image)
    );
    assert_eq!(
        planned("b.pdf", &fixtures::text_pdf(&["x"])),
        Plan::Helper(Kind::Pdf)
    );
    assert_eq!(
        planned("c.zip", b"PK\x03\x04binary"),
        Plan::Ready(opaque(Reason::Unsupported))
    );
}

#[test]
fn files_over_their_source_ceiling_are_opaque_without_reaching_the_helper() {
    let too_large = Plan::Ready(opaque(Reason::TooLarge));
    assert_eq!(
        planned("big.txt", &vec![b'a'; MAX_TEXT_SOURCE_BYTES + 1]),
        too_large
    );
    assert_eq!(
        planned("long.txt", &vec![b'a'; MAX_TEXT_CHARACTERS + 1]),
        too_large
    );
    let unread = unread("d", "video.mp4", 9 * 1024 * 1024, "0");
    assert_eq!(
        (unread.media_type.as_str(), unread.content),
        ("application/octet-stream", opaque(Reason::TooLarge))
    );
}

#[test]
fn per_message_ceilings_refuse_the_whole_message() {
    assert_eq!(admit_selection(&[1; 9]), Err(LimitError::TooMany));
    assert_eq!(
        admit_selection(&[MAX_SELECTED_ORIGINAL_BYTES + 1]),
        Err(LimitError::TooLarge)
    );
    assert_eq!(admit_selection(&[1; 8]), Ok(()));

    let image = Value::Object(vec![member("type", "image")]);
    let five_images: Vec<_> = (0..5)
        .map(|index| attachment(index, image.clone()))
        .collect();
    assert_eq!(admit_message(&five_images), Err(LimitError::TooManyImages));
    assert_eq!(admit_message(&five_images[..4]), Ok(()));

    let text = content::text(Some(&"a".repeat(MAX_TEXT_CHARACTERS)), false);
    let five_texts: Vec<_> = (0..5)
        .map(|index| attachment(index, text.clone()))
        .collect();
    assert_eq!(admit_message(&five_texts), Err(LimitError::TooMuchText));
    assert_eq!(admit_message(&five_texts[..4]), Ok(()));

    let wide = Value::Object(vec![
        member("type", "image"),
        member("base64", &"A".repeat(MAX_ATTACHMENTS_FIELD_BYTES)),
    ]);
    assert_eq!(
        admit_message(&[attachment(0, wide)]),
        Err(LimitError::TooLarge)
    );
    assert_eq!(LimitError::TooMuchText.code(), "attachments-too-much-text");
}

#[test]
fn the_wire_names_exactly_the_attachment_fields() {
    let wire = attachment(0, opaque(Reason::Unsupported)).wire();
    let Value::Object(members) = wire else {
        panic!("an object")
    };
    let names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    assert_eq!(
        names,
        ["id", "name", "media_type", "size", "sha256", "content"]
    );
}
