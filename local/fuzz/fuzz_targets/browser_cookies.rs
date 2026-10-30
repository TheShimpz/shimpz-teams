//! A capsule's cookie store as untrusted SQLite: any bytes as `Default/Cookies` are read, within the bound of work,
//! without a panic, and a store that cannot be read as Chromium's table names every identity provider.

#![no_main]

use libfuzzer_sys::fuzz_target;
use team_local::browser::capsule::{COOKIES, Check, IDENTITY_SITES};

fn header(name: &str, kind: u8, size: usize) -> Vec<u8> {
    let mut block = vec![0_u8; 512];
    block[..name.len()].copy_from_slice(name.as_bytes());
    let octal = |value: usize, width: usize| {
        let mut field = format!("{value:0w$o}", w = width - 1).into_bytes();
        field.push(0);
        field
    };
    block[100..108].copy_from_slice(&octal(0o600, 8));
    block[108..116].copy_from_slice(&octal(0, 8));
    block[116..124].copy_from_slice(&octal(0, 8));
    block[124..136].copy_from_slice(&octal(size, 12));
    block[136..148].copy_from_slice(&octal(0, 12));
    block[156] = kind;
    block[257..263].copy_from_slice(b"ustar\0");
    block[263..265].copy_from_slice(b"00");
    block[148..156].copy_from_slice(b"        ");
    let sum: u32 = block.iter().map(|byte| u32::from(*byte)).sum();
    block[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
    block
}

fuzz_target!(|store: &[u8]| {
    if store.len() > 1 << 20 {
        return;
    }
    let mut archive = header("Default", b'5', 0);
    archive.extend(header(COOKIES, b'0', store.len()));
    archive.extend_from_slice(store);
    archive.resize(archive.len().div_ceil(512) * 512 + 1024, 0);
    let mut check = Check::default();
    check.feed(&archive).expect("a well-formed archive");
    check.finish().expect("a well-formed end");
    let hosts = check.identity_hosts();
    if store.is_empty() {
        assert_eq!(hosts.len(), IDENTITY_SITES.len());
    }
    let (sites, _) = check.sites();
    assert!(sites.len() <= 64);
});
