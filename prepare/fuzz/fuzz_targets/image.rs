//! The image worker on any bytes: a closed answer, through the same panic boundary the worker answers with.

#![no_main]

use std::path::Path;

use libfuzzer_sys::fuzz_target;
use team_prepare::answer::Answer;
use team_prepare::request::{Kind, encode};
use team_prepare::worker::answer;

fuzz_target!(|data: &[u8]| {
    if data.is_empty() {
        return;
    }
    let answered = answer(&encode(Kind::Image, data, None), Path::new("/nonexistent"));
    if let Answer::Image(derivative) = answered {
        assert!(derivative.bytes.starts_with(derivative.media_type.signature()));
    }
});
