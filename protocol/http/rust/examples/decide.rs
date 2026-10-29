//! The Rust side of `differential.py`: reads cases framed as a 4-byte big-endian length and a registry name, then a
//! 4-byte big-endian length and the JSON bytes of the input, from stdin, and writes one line per case: the canonical
//! representation of what the named call returns, `refuse`, `error:<name>`, or `unknown`/`unparsable` when the case
//! names no entry or its bytes are no strict JSON.

#[path = "../tests/protocol/support/registry/mod.rs"]
mod registry;
#[path = "../tests/protocol/support/repr.rs"]
mod repr;

use std::io::{self, BufWriter, Read, Write};

fn frame(input: &[u8]) -> (&[u8], &[u8]) {
    let (length, rest) = input.split_first_chunk::<4>().expect("a frame length");
    rest.split_at(usize::try_from(u32::from_be_bytes(*length)).expect("u32 fits usize"))
}

fn main() -> io::Result<()> {
    let mut input = Vec::new();
    io::stdin().lock().read_to_end(&mut input)?;
    let mut output = BufWriter::new(io::stdout().lock());
    let mut rest = input.as_slice();
    while !rest.is_empty() {
        let (name, after) = frame(rest);
        let (document, after) = frame(after);
        let name = std::str::from_utf8(name).expect("a UTF-8 name");
        let call = registry::find(name);
        let text = match (call, strict_json::parse(document)) {
            (None, _) => "unknown".to_owned(),
            (Some(_), Err(_)) => "unparsable".to_owned(),
            (Some(call), Ok(value)) => registry::line(&call(&value)),
        };
        writeln!(output, "{text}")?;
        rest = after;
    }
    output.flush()
}
