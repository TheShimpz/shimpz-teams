//! Fuzzes the pattern dialect over arbitrary text: the first line is the pattern, the rest the subject. Admission
//! and matching must answer, never panic, a refused pattern never matches, and exactly an admitted pattern has a
//! preparation.
#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    let Ok(text) = std::str::from_utf8(data) else {
        return;
    };
    let (pattern, subject) = text.split_once('\n').unwrap_or((text, ""));
    let admitted = action_schema::pattern_program(pattern).is_some();
    assert_eq!(admitted, action_schema::pattern_preparation(pattern).is_some());
    let result = action_schema::pattern_matches(pattern, subject, &mut action_schema::Budget::default());
    assert_eq!(admitted, result.is_ok() || result == Err(action_schema::PatternError::Exhausted));
});
