//! The Rust side of `differential.py`: reads documents framed as a 4-byte big-endian length and the bytes from stdin and
//! writes one line per document, `refuse` or the canonical representation of the decoded value.

#[path = "../tests/support/repr.rs"]
mod repr;

use std::io::{self, BufWriter, Read, Write};

fn main() -> io::Result<()> {
    let mut input = Vec::new();
    io::stdin().lock().read_to_end(&mut input)?;
    let mut output = BufWriter::new(io::stdout().lock());
    let mut rest = input.as_slice();
    while let Some((length, after)) = rest.split_first_chunk::<4>() {
        let (document, after) =
            after.split_at(usize::try_from(u32::from_be_bytes(*length)).expect("u32 fits usize"));
        match strict_json::parse(document) {
            Ok(value) => writeln!(output, "{}", repr::canonical(&value))?,
            Err(_) => writeln!(output, "refuse")?,
        }
        rest = after;
    }
    assert!(rest.is_empty(), "the input ends inside a frame");
    output.flush()
}
