//! Fuzzes `strict_json::parse` over arbitrary bytes: it must refuse or decode, never panic or overflow the stack.
#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    let _ = strict_json::parse(data);
});
