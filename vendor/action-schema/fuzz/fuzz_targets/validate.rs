//! Fuzzes payload validation: the input is a schema document, a NUL byte, and a payload document. Every admitted
//! schema validates every strict payload to an outcome, never a panic, and never an unsupported construct.
#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    let Some(split) = data.iter().position(|byte| *byte == 0) else {
        return;
    };
    let (Ok(schema), Ok(payload)) = (strict_json::parse(&data[..split]), strict_json::parse(&data[split + 1..])) else {
        return;
    };
    if action_schema::admit_schema(&schema).is_ok() {
        let result = action_schema::validate_payload(&schema, &payload);
        assert_ne!(result, Err(action_schema::PayloadError::Unsupported));
    }
});
