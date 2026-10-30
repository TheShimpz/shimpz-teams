//! The controller's checks of an untrusted helper answer: any JSON becomes closed content, and any bytes a header
//! size or nothing.

#![no_main]

use libfuzzer_sys::fuzz_target;
use strict_json::Value;
use team_prepare::answer::MediaType;
use team_prepare::content;

fn closed(found: &Value) {
    let kind = match found.get("type") {
        Some(Value::String(kind)) => kind.as_str(),
        _ => "",
    };
    assert!(["image", "text", "opaque"].contains(&kind));
}

fuzz_target!(|data: &[u8]| {
    for media_type in [MediaType::Jpeg, MediaType::Png] {
        let _ = content::encoded_size(media_type, data);
    }
    if let Ok(text) = std::str::from_utf8(data) {
        let _ = content::decoded(text);
    }
    if let Ok(answer) = strict_json::parse(data) {
        closed(&content::image(&answer));
        closed(&content::pdf(&answer));
        if let Some((page, count)) = content::page(&answer, 1) {
            closed(&page);
            assert!(count >= 1);
        }
    }
});
