//! Fuzzes `admit_schema` over arbitrary strict JSON: it must admit or refuse, never panic or overflow the stack.
#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    if let Ok(schema) = strict_json::parse(data) {
        let _ = action_schema::admit_schema(&schema);
    }
});
