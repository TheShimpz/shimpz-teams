//! The helper's request frame: any bytes decode to an exact request or to nothing, and a decoded one re-encodes to
//! the same bytes.

#![no_main]

use libfuzzer_sys::fuzz_target;
use team_prepare::request::{decode, encode};

fuzz_target!(|data: &[u8]| {
    if let Some(request) = decode(data) {
        assert_eq!(encode(request.kind, request.data, request.page), data);
    }
});
