//! The egress policy the Python Team wrote (`local/tests/fixtures/state`) is admitted, and rewritten byte for byte.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use team_core::testing::CountingEntropy;
use team_egress::policy::PolicyStore;

fn fixture() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../local/tests/fixtures/state/var/lib/shimpz-local/assistant-egress")
}

const IDENTITY: &str = "space\0team_1\0shimpz-cloudflare";

#[test]
fn the_python_policy_is_admitted_and_rewritten_byte_for_byte() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let root = directory.path().join("assistant-egress");
    fs::create_dir_all(root.join(".tokens")).expect("created");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o750)).expect("mode set");
    fs::set_permissions(root.join(".tokens"), fs::Permissions::from_mode(0o700)).expect("mode set");
    let mut originals = Vec::new();
    for entry in fs::read_dir(fixture()).expect("listed") {
        let entry = entry.expect("entry");
        if entry.file_name() == ".tokens" {
            for token in fs::read_dir(entry.path()).expect("listed") {
                let token = token.expect("entry");
                let target = root.join(".tokens").join(token.file_name());
                fs::copy(token.path(), &target).expect("copied");
                fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).expect("mode set");
            }
        } else {
            let target = root.join(entry.file_name());
            fs::copy(entry.path(), &target).expect("copied");
            fs::set_permissions(&target, fs::Permissions::from_mode(0o640)).expect("mode set");
            originals.push((target.clone(), fs::read(&target).expect("read")));
        }
    }
    let gid = fs::metadata(&root).expect("stat").gid();
    let store = PolicyStore::new(root.clone(), gid);
    let entropy = CountingEntropy::default();
    let hosts = vec![
        "api.cloudflare.com".to_owned(),
        "dash.cloudflare.com".to_owned(),
    ];
    let admitted = store
        .admitted(IDENTITY, &entropy)
        .expect("read")
        .expect("present");
    assert_eq!(admitted.hosts, hosts);
    let token = store
        .validate(IDENTITY, &hosts, &entropy)
        .expect("reviewed hosts");
    assert_eq!(
        store.token(IDENTITY, true, &entropy).expect("read"),
        Some(token.clone())
    );
    store.write(&token, &hosts).expect("rewritten");
    let [(policy, before)] = originals.as_slice() else {
        panic!("one policy file");
    };
    assert_eq!(
        policy.file_name().and_then(|name| name.to_str()),
        Some(format!("{}.json", token.expose()).as_str())
    );
    assert_eq!(&fs::read(policy).expect("read"), before);
}
