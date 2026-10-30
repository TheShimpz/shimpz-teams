//! A Sign-in capsule as the Browser exports it: any bytes, in any chunking, are checked without a panic, and a capsule
//! the check admits ended with its two end blocks.

#![no_main]

use libfuzzer_sys::fuzz_target;
use team_local::browser::capsule::Check;

fuzz_target!(|data: &[u8]| {
    let split = data.first().map_or(1, |byte| usize::from(*byte).max(1));
    let mut check = Check::default();
    let fed = data.chunks(split).try_for_each(|chunk| check.feed(chunk));
    if fed.is_ok() && check.finish().is_ok() {
        assert!(data.len() >= 1024);
        assert!(data[data.len() - 1024..].iter().all(|byte| *byte == 0));
        let _ = check.identity_hosts();
        let _ = check.sites();
    }
});
