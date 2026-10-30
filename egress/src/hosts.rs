//! The reviewed hosts of one Assistant's egress policy: exact public DNS names, canonical as a sorted list.

/// The most hosts one Assistant may declare.
pub const MAX_ALLOWED_HOSTS: usize = 32;

/// Names under these suffixes are never public.
const NON_PUBLIC_SUFFIXES: [&str; 11] = [
    ".arpa",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
];

/// One deterministic list of exact public DNS host names (`assistant/manifest.py` `canonical_allowed_hosts`): at most
/// [`MAX_ALLOWED_HOSTS`] distinct lowercase names of two or more labels, sorted; `None` refuses the list.
#[must_use]
pub fn canonical_allowed_hosts<S: AsRef<str>>(hosts: &[S]) -> Option<Vec<String>> {
    if hosts.len() > MAX_ALLOWED_HOSTS {
        return None;
    }
    let mut canonical: Vec<String> = Vec::with_capacity(hosts.len());
    for host in hosts {
        let host = host.as_ref();
        if !public_host(host) {
            return None;
        }
        canonical.push(host.to_owned());
    }
    canonical.sort();
    if canonical.windows(2).any(|pair| pair[0] == pair[1]) {
        return None;
    }
    Some(canonical)
}

/// Whether `host` is one exact public DNS name: `(?:label\.)+last` within 253 characters, each label 1 to 63
/// lowercase letters, digits, or inner hyphens, the last starting with a letter, and no non-public suffix.
fn public_host(host: &str) -> bool {
    if host.is_empty()
        || host.len() > 253
        || NON_PUBLIC_SUFFIXES
            .iter()
            .any(|suffix| host.ends_with(suffix))
    {
        return false;
    }
    let labels: Vec<&str> = host.split('.').collect();
    let Some((last, leading)) = labels.split_last() else {
        return false;
    };
    !leading.is_empty()
        && leading.iter().all(|label| label_holds(label))
        && label_holds(last)
        && last.as_bytes()[0].is_ascii_lowercase()
}

fn label_holds(label: &str) -> bool {
    let bytes = label.as_bytes();
    let alphanumeric = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
    (1..=63).contains(&bytes.len())
        && bytes.first().is_some_and(alphanumeric)
        && bytes.last().is_some_and(alphanumeric)
        && bytes.iter().all(|byte| alphanumeric(byte) || *byte == b'-')
}

/// The exact policy bytes the Assistant egress proxy reads: the hosts as one compact ASCII JSON array.
#[must_use]
pub fn encoded_hosts(hosts: &[String]) -> Vec<u8> {
    let mut encoded = b"[".to_vec();
    for (index, host) in hosts.iter().enumerate() {
        if index > 0 {
            encoded.push(b',');
        }
        // A public host holds only letters, digits, dots, and hyphens, which JSON writes unescaped.
        encoded.push(b'"');
        encoded.extend_from_slice(host.as_bytes());
        encoded.push(b'"');
    }
    encoded.push(b']');
    encoded
}

#[cfg(test)]
mod tests {
    use super::{canonical_allowed_hosts, encoded_hosts};

    #[test]
    fn hosts_are_sorted_exact_public_names() {
        assert_eq!(
            canonical_allowed_hosts(&["geocoding-api.open-meteo.com", "api.open-meteo.com"]),
            Some(vec![
                "api.open-meteo.com".to_owned(),
                "geocoding-api.open-meteo.com".to_owned()
            ])
        );
        assert_eq!(canonical_allowed_hosts::<&str>(&[]), Some(Vec::new()));
        let label = "a".repeat(63);
        let long = format!("{label}.{label}.{label}.{}", "b".repeat(61));
        assert_eq!(long.len(), 253);
        assert!(canonical_allowed_hosts(&[long.as_str()]).is_some());
        for accepted in ["1.example.com", "x.co", "a-b.c-d", "123.io"] {
            assert!(canonical_allowed_hosts(&[accepted]).is_some(), "{accepted}");
        }
    }

    #[test]
    fn anything_but_a_public_name_is_refused() {
        let too_long = format!("{}.com", "a".repeat(64));
        let over = format!(
            "{}.{}.{}.{}",
            "a".repeat(63),
            "a".repeat(63),
            "a".repeat(63),
            "b".repeat(62)
        );
        for refused in [
            "",
            "com",
            "EXAMPLE.com",
            "https://example.com",
            "example.com.",
            ".example.com",
            "a..com",
            "-a.com",
            "a-.com",
            "a.1com",
            "a.com-",
            "a_b.com",
            "exämple.com",
            "printer.local",
            "service.internal",
            "1.0.0.127.in-addr.arpa",
            "example.test",
            too_long.as_str(),
            over.as_str(),
        ] {
            assert_eq!(canonical_allowed_hosts(&[refused]), None, "{refused}");
        }
        assert_eq!(canonical_allowed_hosts(&["a.com", "a.com"]), None);
        let many: Vec<String> = (0..33).map(|index| format!("h{index}.com")).collect();
        assert_eq!(canonical_allowed_hosts(&many), None);
        assert!(canonical_allowed_hosts(&many[..32]).is_some());
    }

    #[test]
    fn hosts_encode_as_compact_json() {
        assert_eq!(
            encoded_hosts(&["a.com".to_owned(), "b.com".to_owned()]),
            br#"["a.com","b.com"]"#
        );
        assert_eq!(encoded_hosts(&[]), b"[]");
    }
}
