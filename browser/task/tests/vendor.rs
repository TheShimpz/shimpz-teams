//! The vendored Holdpoint is exactly the packaged tag its pin names: every file listed with its SHA-256, nothing
//! else (written by the umbrella's `.scripts/vendor-holdpoint`).

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

fn files(root: &Path, directory: &Path, found: &mut BTreeSet<String>) {
    for entry in std::fs::read_dir(directory).expect("vendored directory") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            files(root, &path, found);
        } else {
            found.insert(
                path.strip_prefix(root)
                    .expect("inside")
                    .to_string_lossy()
                    .into_owned(),
            );
        }
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

#[test]
fn the_vendored_holdpoint_is_exactly_its_pinned_package() {
    let vendor = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../vendor");
    let root = vendor.join("holdpoint");
    let listing = std::fs::read_to_string(vendor.join("holdpoint.sha256")).expect("listing");
    let mut listed = BTreeSet::new();
    for line in listing.lines() {
        let (digest, name) = line.split_once("  ").expect("sha256sum line");
        let bytes = std::fs::read(root.join(name)).expect("listed file");
        assert_eq!(
            hex(&Sha256::digest(&bytes)),
            digest,
            "{name} differs from its pin"
        );
        listed.insert(name.to_owned());
    }
    let mut found = BTreeSet::new();
    files(&root, &root, &mut found);
    assert_eq!(
        found, listed,
        "the vendored tree holds exactly the listed files"
    );
    let pin = std::fs::read_to_string(vendor.join("holdpoint.upstream.json")).expect("pin");
    let vcs = std::fs::read_to_string(root.join(".cargo_vcs_info.json")).expect("vcs info");
    let commit = pin
        .split("\"commit\": \"")
        .nth(1)
        .and_then(|rest| rest.split('"').next())
        .expect("commit");
    assert!(
        vcs.contains(commit),
        "the package was built from the pinned commit"
    );
}
