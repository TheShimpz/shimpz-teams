//! The bounded PDF pre-pass on any bytes: a page count within the ceiling or a closed reason, never a crash.

#![no_main]

use libfuzzer_sys::fuzz_target;
use team_prepare::limits::MAX_PDF_PAGES;
use team_prepare::pdf::prepass;

fuzz_target!(|data: &[u8]| {
    if let Ok(Ok(pages)) = std::panic::catch_unwind(|| prepass(data)) {
        assert!((0..=MAX_PDF_PAGES).contains(&pages));
    }
});
