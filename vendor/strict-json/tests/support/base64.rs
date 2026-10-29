//! Standard padded base64 decoding for the vector files.

/// Decodes standard padded base64; panics on anything else, since the vectors are generated.
pub fn decode(text: &str) -> Vec<u8> {
    assert_eq!(text.len() % 4, 0, "base64 length");
    let mut bytes = Vec::with_capacity(text.len() / 4 * 3);
    for chunk in text.as_bytes().chunks(4) {
        let padding = chunk
            .iter()
            .rev()
            .take_while(|&&symbol| symbol == b'=')
            .count();
        let group = chunk[..4 - padding]
            .iter()
            .fold(0u32, |group, &symbol| group << 6 | sextet(symbol))
            << (6 * padding);
        bytes.extend_from_slice(&group.to_be_bytes()[1..4 - padding]);
    }
    bytes
}

fn sextet(symbol: u8) -> u32 {
    let value = match symbol {
        b'A'..=b'Z' => symbol - b'A',
        b'a'..=b'z' => symbol - b'a' + 26,
        b'0'..=b'9' => symbol - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => panic!("invalid base64 symbol {symbol}"),
    };
    u32::from(value)
}
