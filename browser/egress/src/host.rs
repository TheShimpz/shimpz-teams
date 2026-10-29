//! The Browser's destination rule: a public DNS hostname, never an address the resolver would read as a literal.

/// The longest hostname admitted.
pub const MAX_HOST_LENGTH: usize = 253;

/// One LDH label: letters, digits, and inner hyphens, at most 63 characters, `[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?`.
fn label(text: &str) -> bool {
    let bytes = text.as_bytes();
    let edge = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
    (1..=63).contains(&bytes.len())
        && bytes.first().is_some_and(edge)
        && bytes.last().is_some_and(edge)
        && bytes.iter().all(|byte| edge(byte) || *byte == b'-')
}

/// The canonical DNS hostname of a CONNECT target, or None for an IP literal or a malformed name.
///
/// A name needs at least two LDH labels, a final label that is not all digits, and no spelling the resolver reads as
/// an IPv4 address (hexadecimal, octal, or fewer than four parts, which `inet_aton` decides without a lookup); an
/// IPv6 literal's colons and brackets fail the label rule.
#[must_use]
pub fn hostname(host: &str) -> Option<String> {
    let canonical = host.to_ascii_lowercase();
    if canonical.is_empty() || canonical.len() > MAX_HOST_LENGTH {
        return None;
    }
    let labels: Vec<&str> = canonical.split('.').collect();
    let numeric = labels
        .last()
        .is_some_and(|last| !last.is_empty() && last.bytes().all(|byte| byte.is_ascii_digit()));
    if labels.len() < 2
        || numeric
        || !labels.iter().all(|part| label(part))
        || inet_aton(&canonical)
    {
        return None;
    }
    Some(canonical)
}

/// One number of glibc's `inet_aton`, as `strtoul(text, &end, 0)` reads it after a leading digit: `0x` and a hex
/// digit start hexadecimal, a leading `0` octal, any other digit decimal. The value and where it ends; None past
/// `0xffffffff`.
fn number(bytes: &[u8], start: usize) -> Option<(u64, usize)> {
    let hex_prefix = bytes.get(start) == Some(&b'0')
        && matches!(bytes.get(start + 1), Some(b'x' | b'X'))
        && bytes.get(start + 2).is_some_and(u8::is_ascii_hexdigit);
    let (radix, mut end) = match (hex_prefix, bytes[start]) {
        (true, _) => (16, start + 2),
        (false, b'0') => (8, start),
        _ => (10, start),
    };
    let mut value: u64 = 0;
    while let Some(digit) = bytes
        .get(end)
        .and_then(|byte| char::from(*byte).to_digit(radix))
    {
        value = value * u64::from(radix) + u64::from(digit);
        if value > 0xffff_ffff {
            return None;
        }
        end += 1;
    }
    Some((value, end))
}

/// Whether glibc's `inet_aton` accepts the text as an IPv4 address: one to four numbers joined by dots, each but the
/// last at most 255, the last within the bytes the earlier ones leave, ending at the text's end or at whitespace.
#[must_use]
pub fn inet_aton(text: &str) -> bool {
    const LIMITS: [u64; 4] = [0xffff_ffff, 0x00ff_ffff, 0xffff, 0xff];
    let bytes = text.as_bytes();
    let (mut parts, mut index) = (0, 0);
    loop {
        if !bytes.get(index).is_some_and(u8::is_ascii_digit) {
            return false;
        }
        let Some((value, end)) = number(bytes, index) else {
            return false;
        };
        if bytes.get(end) != Some(&b'.') {
            let terminated = bytes
                .get(end)
                .is_none_or(|byte| b" \t\n\x0b\x0c\r".contains(byte));
            return terminated && value <= LIMITS[parts];
        }
        if parts == 3 || value > 0xff {
            return false;
        }
        parts += 1;
        index = end + 1;
    }
}

#[cfg(test)]
mod tests;
