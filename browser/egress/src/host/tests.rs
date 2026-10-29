use super::{hostname, inet_aton};

#[test]
fn any_public_hostname_is_admitted_in_canonical_form() {
    for (target, host) in [
        ("example.com", "example.com"),
        ("WWW.Gov.BR", "www.gov.br"),
        ("pt.wikipedia.org", "pt.wikipedia.org"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("1.0x", "1.0x"),
        ("a.1a", "a.1a"),
    ] {
        assert_eq!(hostname(target), Some(host.to_owned()), "{target}");
    }
}

#[test]
fn literals_and_malformed_names_are_refused() {
    for target in [
        "93.184.216.34",
        "0x5db8d822",
        "0x8.0x8.0x8.0x8",
        "010.010.010.0x8",
        "0x7f.1",
        "1.2.3",
        "1.0x1",
        "1.2.0xa",
        "[2606:2800:220:1:248:1893:25c8:1946]",
        "localhost",
        "example.com.",
        "-bad.example",
        "bad-.example",
        "under_score.example",
        "",
        ".example",
        "a..example",
        &format!("{}.example", "a".repeat(64)),
        &format!("{}com", "a.".repeat(127)),
    ] {
        assert_eq!(hostname(target), None, "{target}");
    }
}

#[test]
fn inet_aton_reads_glibc_s_number_forms() {
    for text in [
        "1.2.3.4",
        "1.2.3",
        "1.2",
        "1",
        "0x7f.1",
        "0x7F.0X1",
        "017.1",
        "0",
        "00",
        "4294967295",
        "1.16777215",
        "1.2.65535",
        "1.2.3.255",
        "1.2.3.4 trailing",
        "1.2.3.4\t",
        "0xffffffff",
    ] {
        assert!(inet_aton(text), "{text}");
    }
    for text in [
        "",
        "a.1",
        "1.2.3.4.5",
        "1.2.3.4.",
        "256.1",
        "1.16777216",
        "1.2.65536",
        "1.2.3.256",
        "4294967296",
        "99999999999999999999999",
        "08",
        "0x",
        "0xg",
        "1..2",
        "1.2.3.4x",
        ".1",
        "1.",
        "0x100000000",
    ] {
        assert!(!inet_aton(text), "{text}");
    }
}

#[test]
fn every_seeded_name_decides_as_the_python_profile_did() {
    let cases = include_str!("../../vectors/hostnames.txt");
    let mut count = 0;
    for line in cases.lines() {
        let (host, expected) = line.split_once('\t').expect("a case");
        let expected = (expected != "-").then(|| expected.to_owned());
        assert_eq!(hostname(host), expected, "{host:?}");
        count += 1;
    }
    assert!(count >= 2000);
}
