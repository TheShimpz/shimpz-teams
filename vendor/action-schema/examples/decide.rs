//! The Rust side of the Action schema differential (tooling): reads records from stdin, each a kind byte followed by
//! its fields, every field a 4-byte big-endian length and its bytes, and writes one line per record.
//!
//! - `p pattern`: `program <bound> <preparation>` or `refuse`;
//! - `m pattern subject`: `true`, `false`, or the pattern error, matched under a fresh budget;
//! - `s schema`: `admit` or `refuse <reason>` (a schema that is not strict JSON is `refuse json`);
//! - `v schema payload`: the validation's outcome (`valid`, `invalid`, `exhausted`, `refused`, `arithmetic`, `shape`,
//!   or `unsupported`) and the matching work it charged, for an admitted schema.

use std::io::{self, BufWriter, Read, Write};

use action_schema::{
    Budget, MAX_PATTERN_WORK, PatternError, PayloadError, admit_schema, pattern_matches,
    pattern_preparation, pattern_program,
};

fn field(input: &mut &[u8]) -> Vec<u8> {
    let (length, rest) = input.split_first_chunk::<4>().expect("a field length");
    let length = usize::try_from(u32::from_be_bytes(*length)).expect("u32 fits usize");
    let (bytes, rest) = rest.split_at(length);
    *input = rest;
    bytes.to_vec()
}

fn text(bytes: Vec<u8>) -> String {
    String::from_utf8(bytes).expect("a UTF-8 field")
}

fn outcome(result: Result<(), PayloadError>) -> &'static str {
    match result {
        Ok(()) => "valid",
        Err(PayloadError::NotAnObject | PayloadError::TooDeep) => "shape",
        Err(PayloadError::Invalid) => "invalid",
        Err(PayloadError::Pattern(PatternError::Exhausted)) => "exhausted",
        Err(PayloadError::Pattern(PatternError::Refused)) => "refused",
        Err(PayloadError::Arithmetic) => "arithmetic",
        Err(PayloadError::Unsupported) => "unsupported",
    }
}

fn decide(kind: u8, input: &mut &[u8]) -> String {
    match kind {
        b'p' => {
            let pattern = text(field(input));
            pattern_program(&pattern)
                .zip(pattern_preparation(&pattern))
                .map_or_else(
                    || "refuse".to_owned(),
                    |(bound, preparation)| format!("program {bound} {preparation}"),
                )
        }
        b'm' => {
            let pattern = text(field(input));
            let subject = text(field(input));
            match pattern_matches(&pattern, &subject, &mut Budget::default()) {
                Ok(matched) => matched.to_string(),
                Err(error) => format!("{error:?}"),
            }
        }
        b's' => match strict_json::parse(&field(input)) {
            Ok(schema) => admit_schema(&schema).map_or_else(
                |refusal| format!("refuse {refusal:?}"),
                |()| "admit".to_owned(),
            ),
            Err(_) => "refuse json".to_owned(),
        },
        b'v' => {
            let schema = strict_json::parse(&field(input)).expect("a strict schema");
            let payload = strict_json::parse(&field(input)).expect("a strict payload");
            let mut budget = Budget::default();
            let result = action_schema::validate_payload_in(&schema, &payload, &mut budget);
            format!(
                "{} {}",
                outcome(result),
                MAX_PATTERN_WORK - budget.remaining()
            )
        }
        other => panic!("unknown record kind {other}"),
    }
}

fn main() -> io::Result<()> {
    let mut input = Vec::new();
    io::stdin().lock().read_to_end(&mut input)?;
    let mut output = BufWriter::new(io::stdout().lock());
    let mut rest = input.as_slice();
    while let Some((kind, after)) = rest.split_first() {
        rest = after;
        let line = decide(*kind, &mut rest);
        writeln!(output, "{line}")?;
    }
    output.flush()
}
