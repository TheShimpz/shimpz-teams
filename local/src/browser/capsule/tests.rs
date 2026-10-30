use std::collections::BTreeSet;

use rusqlite::{Connection, MAIN_DB};

use super::{COOKIES, Check, IDENTITY_SITES, Limits};

/// A cookie store with Chromium's table: each host's first-party cookie, and partitioned ones set under a site.
pub(crate) fn cookie_store(
    hosts: &[&str],
    partitioned: &[&str],
    under_provider: &[&str],
) -> Vec<u8> {
    let mut connection = Connection::open_in_memory().expect("opened");
    connection
        .execute_batch(
            "CREATE TABLE cookies (host_key TEXT, top_frame_site_key TEXT, name TEXT, value TEXT)",
        )
        .expect("created");
    let rows = hosts
        .iter()
        .map(|host| (*host, ""))
        .chain(
            partitioned
                .iter()
                .map(|host| (*host, "https://site.example")),
        )
        .chain(
            under_provider
                .iter()
                .map(|host| (*host, "https://google.com")),
        );
    for (host, partition) in rows {
        connection
            .execute(
                "INSERT INTO cookies VALUES (?1, ?2, 'sid', '')",
                (host, partition),
            )
            .expect("inserted");
    }
    serialized(&mut connection)
}

fn serialized(connection: &mut Connection) -> Vec<u8> {
    connection.serialize(MAIN_DB).expect("serialized").to_vec()
}

fn octal(value: u64, width: usize) -> Vec<u8> {
    let mut field = format!("{value:0w$o}", w = width - 1).into_bytes();
    field.push(0);
    field
}

/// One ustar header block.
pub(crate) fn header(name: &str, kind: u8, size: u64) -> Vec<u8> {
    let mut block = vec![0_u8; 512];
    block[..name.len()].copy_from_slice(name.as_bytes());
    block[100..108].copy_from_slice(&octal(if kind == b'5' { 0o700 } else { 0o600 }, 8));
    block[108..116].copy_from_slice(&octal(0, 8));
    block[116..124].copy_from_slice(&octal(0, 8));
    block[124..136].copy_from_slice(&octal(size, 12));
    block[136..148].copy_from_slice(&octal(0, 12));
    block[156] = kind;
    block[257..263].copy_from_slice(b"ustar\0");
    block[263..265].copy_from_slice(b"00");
    seal(&mut block);
    block
}

/// Writes a header block's checksum.
pub(crate) fn seal(block: &mut [u8]) {
    block[148..156].copy_from_slice(b"        ");
    let sum: u64 = block[..512].iter().map(|byte| u64::from(*byte)).sum();
    block[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
}

/// A ustar archive of directories (`None`) and files, as `browser-host profile export` writes one.
pub(crate) fn archive(entries: &[(&str, Option<&[u8]>)]) -> Vec<u8> {
    let mut out = Vec::new();
    for (name, body) in entries {
        match body {
            None => out.extend(header(name, b'5', 0)),
            Some(body) => {
                out.extend(header(
                    name,
                    b'0',
                    u64::try_from(body.len()).expect("small"),
                ));
                out.extend_from_slice(body);
                out.resize(out.len().div_ceil(512) * 512, 0);
            }
        }
    }
    out.extend([0_u8; 1024]);
    out
}

fn checked_with(data: &[u8], step: usize, limits: Limits) -> Result<Check, super::CapsuleError> {
    let mut check = Check::new(limits);
    for chunk in data.chunks(step.max(1)) {
        check.feed(chunk)?;
    }
    check.finish()?;
    Ok(check)
}

fn checked(data: &[u8], step: usize) -> Result<Check, super::CapsuleError> {
    checked_with(data, step, Limits::default())
}

fn every_provider() -> BTreeSet<&'static str> {
    IDENTITY_SITES.iter().map(|(host, _)| *host).collect()
}

#[test]
fn a_profile_capsule_passes_whatever_its_chunking_and_keeps_only_the_cookie_store() {
    let cookies = cookie_store(&[".linkedin.com", "www.linkedin.com"], &[], &[]);
    let data = archive(&[
        ("Default", None),
        (COOKIES, Some(&cookies)),
        ("Default/Preferences", Some(b"{}")),
    ]);
    for step in [1, 511, 512, 4096, data.len()] {
        let check = checked(&data, step).expect("admitted");
        assert_eq!(check.cookies(), cookies);
        assert_eq!(check.identity_hosts(), BTreeSet::new());
        assert_eq!(check.sites(), (vec!["linkedin.com".to_owned()], false));
    }
}

fn found(store: &[u8]) -> BTreeSet<&'static str> {
    checked(&archive(&[("Default", None), (COOKIES, Some(store))]), 700)
        .expect("admitted")
        .identity_hosts()
}

#[test]
fn a_cookie_an_identity_provider_s_sign_in_host_receives_is_found() {
    assert_eq!(
        found(&cookie_store(&[".accounts.google.com", "x.com"], &[], &[])),
        BTreeSet::from(["accounts.google.com"])
    );
    // A parent domain's cookie is sent to the sign-in host too; a sibling's or a partitioned one is not.
    assert_eq!(
        found(&cookie_store(&[".google.com"], &[], &[])),
        BTreeSet::from(["accounts.google.com"])
    );
    assert_eq!(
        found(&cookie_store(&[".live.com", ".apple.com"], &[], &[])),
        BTreeSet::from(["login.live.com", "appleid.apple.com", "idmsa.apple.com"])
    );
    assert_eq!(
        found(&cookie_store(
            &["google.com", "mail.google.com"],
            &[".google.com"],
            &[]
        )),
        BTreeSet::new()
    );
    // A cookie partitioned under the provider's own site reaches its sign-in pages.
    assert_eq!(
        found(&cookie_store(&[], &[], &[".google.com"])),
        BTreeSet::from(["accounts.google.com"])
    );
    // Hosts are compared as Chromium lowercases them.
    assert_eq!(
        found(&cookie_store(&[".Google.COM"], &[], &[])),
        BTreeSet::from(["accounts.google.com"])
    );
}

#[test]
fn a_store_that_is_not_chromium_s_table_names_every_identity_provider() {
    let mut view = Connection::open_in_memory().expect("opened");
    view.execute_batch(
        "CREATE TABLE real (host_key TEXT, top_frame_site_key TEXT); CREATE VIEW cookies AS SELECT * FROM real",
    )
    .expect("created");
    let mut legacy = Connection::open_in_memory().expect("opened");
    legacy
        .execute_batch("CREATE TABLE cookies (host_key TEXT)")
        .expect("created");
    for store in [
        b"not a database".to_vec(),
        serialized(&mut view),
        serialized(&mut legacy),
    ] {
        assert_eq!(found(&store), every_provider());
    }
    // A query that would run past its bound of work names every provider too.
    let data = archive(&[
        ("Default", None),
        (COOKIES, Some(&cookie_store(&["x.com"], &[], &[]))),
    ]);
    let limits = Limits {
        sql_steps: 0,
        sql_every: 1,
        ..Limits::default()
    };
    let check = checked_with(&data, 700, limits).expect("admitted");
    assert_eq!(check.identity_hosts(), every_provider());
    assert_eq!(check.sites(), (Vec::new(), false));
    // So does a profile without its cookie store.
    let bare = checked(&archive(&[("Default", None)]), 700).expect("admitted");
    assert_eq!(bare.identity_hosts(), every_provider());
}

#[test]
fn another_cookie_store_is_never_let_through_unchecked() {
    let store = cookie_store(&[], &[], &[]);
    let nested = archive(&[
        ("Default", None),
        (COOKIES, Some(&store)),
        ("Default/Network", None),
        ("Default/Network/Cookies", Some(&store)),
    ]);
    let profile = archive(&[
        ("Default", None),
        (COOKIES, Some(&store)),
        ("Profile 1", None),
        ("Profile 1/Cookies", Some(&store)),
    ]);
    for data in [nested, profile] {
        assert!(checked(&data, 700).is_err());
    }
}

#[test]
fn every_hostile_or_malformed_capsule_is_refused() {
    let valid = archive(&[("Default", None), (COOKIES, Some(b"c"))]);
    let mut link = header("Default", b'2', 0);
    link[157..161].copy_from_slice(b"/etc");
    seal(&mut link);
    link.extend([0_u8; 1024]);
    let mut bad_sum = valid.clone();
    bad_sum[0] ^= 1;
    let mut trailing = valid.clone();
    trailing.push(1);
    for data in [
        link,
        archive(&[(COOKIES, Some(b"orphan"))]),
        archive(&[("../escape", Some(b"x"))]),
        archive(&[("Default/./x", Some(b"x"))]),
        archive(&[("bad\u{1}name", Some(b"x"))]),
        archive(&[("Default", None), ("Default", None)]),
        bad_sum,
        valid[..valid.len() - 512].to_vec(),
        trailing,
    ] {
        assert!(checked(&data, 700).is_err());
    }
}

type Change = fn(&mut [u8]);

#[test]
fn every_malformed_header_or_bound_is_refused() {
    let valid = archive(&[("Default", None), (COOKIES, Some(b"cookie"))]);
    let changes: [Change; 5] = [
        |data| data[156] = b'3',
        |data| data[0..2].copy_from_slice(&[0xff, 0xfe]),
        |data| data[124..136].copy_from_slice(b"zzzzzzzzzzz\0"),
        |data| data[257..263].copy_from_slice(b"gnutar"),
        |data| data[124..136].copy_from_slice(b"00000000001\0"),
    ];
    for change in changes {
        let mut data = valid.clone();
        change(&mut data);
        seal(&mut data[..512]);
        assert!(checked(&data, data.len()).is_err());
    }
    let mut padding = valid.clone();
    padding[512 + 512 + 6] = 1;
    let end_then_entry = [
        &valid[..valid.len() - 1024],
        &[0_u8; 512][..],
        &valid[512..1024],
        &[0_u8; 1024][..],
    ]
    .concat();
    for data in [padding, end_then_entry] {
        assert!(checked(&data, data.len()).is_err());
    }
    let mut check = checked(&valid, 700).expect("admitted");
    assert!(check.feed(b"\0").is_err());
    for limits in [
        Limits {
            entries: 1,
            ..Limits::default()
        },
        Limits {
            total: 2,
            ..Limits::default()
        },
    ] {
        assert!(checked_with(&valid, 700, limits).is_err());
    }
}

#[test]
fn sites_are_registrable_domains_bounded_and_marked_truncated() {
    let hosts: Vec<String> = (0..70)
        .map(|index| format!(".site{index:02}.com"))
        .collect();
    let mut all: Vec<&str> = hosts.iter().map(String::as_str).collect();
    all.extend(["www.example.co.uk", "shop.example.com.br"]);
    let data = archive(&[
        ("Default", None),
        (COOKIES, Some(&cookie_store(&all, &[], &[]))),
    ]);
    let (sites, truncated) = checked(&data, 4096).expect("admitted").sites();
    assert_eq!(sites.len(), super::MAX_SITES);
    assert!(truncated);
    assert_eq!(sites.first().map(String::as_str), Some("example.co.uk"));
    assert!(sites.contains(&"example.com.br".to_owned()));
}
