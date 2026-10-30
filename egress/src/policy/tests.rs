use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::PathBuf;

use team_core::secret::Secret;
use team_core::testing::CountingEntropy;

use super::{PolicyError, PolicyStore, environment_map};

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    store: PolicyStore,
    entropy: CountingEntropy,
}

fn fixture() -> Fixture {
    let directory = tempfile::tempdir().expect("temporary directory");
    let root = directory.path().join("policies");
    fs::create_dir(&root).expect("created");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o750)).expect("mode set");
    let gid = fs::metadata(&root).expect("stat").gid();
    Fixture {
        store: PolicyStore::new(root.clone(), gid),
        _directory: directory,
        root,
        entropy: CountingEntropy::default(),
    }
}

fn hosts(names: &[&str]) -> Vec<String> {
    names.iter().map(|name| (*name).to_owned()).collect()
}

impl Fixture {
    fn token(&self, identity: &str) -> Secret {
        self.store
            .token(identity, true, &self.entropy)
            .expect("created")
            .expect("present")
    }

    fn policy_path(&self, token: &Secret) -> PathBuf {
        self.root.join(format!("{}.json", token.expose()))
    }
}

#[test]
fn policy_root_custody_gid_10017_mode_0750() {
    // The image's egress group is 10017; the custody is the same predicate with the test's own group.
    let fixture = fixture();
    let missing = PolicyStore::new(fixture.root.join("missing"), 0);
    assert_eq!(
        missing.token("identity", false, &fixture.entropy),
        Err(PolicyError::Unavailable)
    );
    let foreign = PolicyStore::new(
        fixture.root.clone(),
        fs::metadata(&fixture.root).expect("stat").gid() + 1,
    );
    assert_eq!(
        foreign.token("identity", false, &fixture.entropy),
        Err(PolicyError::Drift)
    );
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o700)).expect("mode set");
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Err(PolicyError::Drift)
    );
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o750)).expect("mode set");
    assert_eq!(
        fixture.store.token("", false, &fixture.entropy),
        Err(PolicyError::Drift)
    );
    // The private token directory must stay owner-only.
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Ok(None)
    );
    let tokens = fixture.root.join(".tokens");
    assert_eq!(fs::metadata(&tokens).expect("stat").mode() & 0o7777, 0o700);
    fs::set_permissions(&tokens, fs::Permissions::from_mode(0o740)).expect("mode set");
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Err(PolicyError::Drift)
    );
}

#[test]
fn token_file_exact_33_bytes_0600() {
    let fixture = fixture();
    let token = fixture.token("space\0team_1\0assistant");
    let tokens: Vec<PathBuf> = fs::read_dir(fixture.root.join(".tokens"))
        .expect("listed")
        .map(|entry| entry.expect("entry").path())
        .collect();
    let [path] = tokens.as_slice() else {
        panic!("one token file");
    };
    assert_eq!(
        fs::read(path).expect("read"),
        format!("{}\n", token.expose()).into_bytes()
    );
    assert_eq!(fs::metadata(path).expect("stat").mode() & 0o7777, 0o600);
    // The file is named by the identity's SHA-256 and reused.
    assert_eq!(
        path.file_name().and_then(|name| name.to_str()),
        Some(format!("{}.token", super::sha256_hex("space\0team_1\0assistant")).as_str())
    );
    assert_eq!(fixture.token("space\0team_1\0assistant"), token);
    for (content, mode) in [
        (b"\xff0000000000000000000000000000000\n".to_vec(), 0o600),
        (b"g0000000000000000000000000000000\n".to_vec(), 0o600),
        (b"00000000000000000000000000000000 ".to_vec(), 0o600),
        (b"0000000000000000000000000000000\n".to_vec(), 0o600),
        (b"00000000000000000000000000000000\n".to_vec(), 0o640),
    ] {
        fs::write(path, &content).expect("written");
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("mode set");
        assert_eq!(
            fixture
                .store
                .token("space\0team_1\0assistant", true, &fixture.entropy),
            Err(PolicyError::Drift),
            "{content:?}"
        );
    }
}

#[test]
fn read_nofollow_single_link_exact_size() {
    let fixture = fixture();
    let token = fixture.token("identity");
    let token_file = fs::read_dir(fixture.root.join(".tokens"))
        .expect("listed")
        .next()
        .expect("one")
        .expect("entry")
        .path();
    let second = fixture.root.join("second-link");
    fs::hard_link(&token_file, &second).expect("linked");
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Err(PolicyError::Drift)
    );
    fs::remove_file(&second).expect("removed");
    fixture
        .store
        .write(&token, &hosts(&["example.com"]))
        .expect("written");
    let policy = fixture.policy_path(&token);
    let target = fixture.root.join("target.json");
    fs::rename(&policy, &target).expect("moved");
    symlink(&target, &policy).expect("linked");
    assert_eq!(
        fixture.store.admitted("identity", &fixture.entropy),
        Err(PolicyError::Drift)
    );
}

#[test]
fn policy_file_0640_canonical_hosts() {
    let fixture = fixture();
    let token = fixture.token("identity");
    fixture
        .store
        .write(
            &token,
            &hosts(&["api.open-meteo.com", "geocoding-api.open-meteo.com"]),
        )
        .expect("written");
    let policy = fixture.policy_path(&token);
    assert_eq!(
        fs::read(&policy).expect("read"),
        br#"["api.open-meteo.com","geocoding-api.open-meteo.com"]"#
    );
    let metadata = fs::metadata(&policy).expect("stat");
    assert_eq!(metadata.mode() & 0o7777, 0o640);
    assert_eq!(
        metadata.gid(),
        fs::metadata(&fixture.root).expect("stat").gid()
    );
    for refused in [
        hosts(&[]),
        hosts(&["https://example.com"]),
        hosts(&["EXAMPLE.com"]),
        hosts(&["b.com", "a.com"]),
    ] {
        assert_eq!(
            fixture.store.write(&token, &refused),
            Err(PolicyError::Drift),
            "{refused:?}"
        );
    }
    let invalid = Secret::new("invalid".to_owned());
    assert_eq!(
        fixture.store.write(&invalid, &hosts(&["example.com"])),
        Err(PolicyError::Drift)
    );
}

#[test]
fn stored_bytes_must_equal_canonical_reencoding() {
    let fixture = fixture();
    let token = fixture.token("identity");
    fixture
        .store
        .write(&token, &hosts(&["example.com"]))
        .expect("written");
    let admitted = fixture
        .store
        .admitted("identity", &fixture.entropy)
        .expect("read")
        .expect("present");
    assert_eq!(
        (admitted.token, admitted.hosts),
        (token.clone(), hosts(&["example.com"]))
    );
    let policy = fixture.policy_path(&token);
    for tampered in [
        &b"not-json"[..],
        b"[\"example.com\"]\n",
        b"[ \"example.com\" ]",
        b"[]",
        b"[\"b.com\",\"a.com\"]",
        b"[\"evil.example\"]",
        b"[\"exampl\\u0065.com\"]",
        b"{}",
    ] {
        fs::write(&policy, tampered).expect("written");
        assert_eq!(
            fixture.store.admitted("identity", &fixture.entropy),
            Err(PolicyError::Drift),
            "{tampered:?}"
        );
    }
    assert_eq!(
        fixture.store.admitted("another-identity", &fixture.entropy),
        Ok(None)
    );
}

#[test]
fn use_time_hosts_equal_reviewed() {
    let fixture = fixture();
    let reviewed = hosts(&["api.open-meteo.com", "geocoding-api.open-meteo.com"]);
    let token = fixture.token("space\0team_1\0assistant");
    fixture.store.write(&token, &reviewed).expect("written");
    assert_eq!(
        fixture
            .store
            .validate("space\0team_1\0assistant", &reviewed, &fixture.entropy),
        Ok(token.clone())
    );
    assert_eq!(
        fixture.store.validate(
            "space\0team_1\0assistant",
            &hosts(&["other.com"]),
            &fixture.entropy
        ),
        Err(PolicyError::Drift)
    );
    assert_eq!(
        fixture
            .store
            .validate("missing", &reviewed, &fixture.entropy),
        Err(PolicyError::Drift)
    );
    // A policy rewritten behind Team's back is drift, never a new review.
    fs::write(fixture.policy_path(&token), br#"["evil.com"]"#).expect("written");
    assert_eq!(
        fixture
            .store
            .validate("space\0team_1\0assistant", &reviewed, &fixture.entropy),
        Err(PolicyError::Drift)
    );
}

#[test]
fn preexisting_token_survives_failed_write() {
    let fixture = fixture();
    let token = fixture.token("identity");
    // A directory where the policy file belongs makes the durable replacement fail.
    let policy = fixture.policy_path(&token);
    fs::create_dir(&policy).expect("created");
    fs::write(policy.join("inside"), b"x").expect("written");
    assert_eq!(
        fixture.store.write(&token, &hosts(&["example.com"])),
        Err(PolicyError::Unavailable)
    );
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Ok(Some(token))
    );
    let leftovers: Vec<String> = fs::read_dir(&fixture.root)
        .expect("listed")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .into_string()
                .expect("UTF-8")
        })
        .filter(|name| name.starts_with('.') && name != ".tokens")
        .collect();
    assert_eq!(leftovers, Vec::<String>::new());
}

#[test]
fn remove_checks_custody_then_fsyncs() {
    let fixture = fixture();
    fixture.store.remove("missing").expect("absence is success");
    let token = fixture.token("identity");
    fixture
        .store
        .remove("identity")
        .expect("removed without a policy");
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Ok(None)
    );

    let token_again = fixture.token("identity");
    assert_ne!(token_again, token);
    fixture
        .store
        .write(&token_again, &hosts(&["example.com"]))
        .expect("written");
    let policy = fixture.policy_path(&token_again);
    fs::set_permissions(&policy, fs::Permissions::from_mode(0o600)).expect("mode set");
    assert_eq!(fixture.store.remove("identity"), Err(PolicyError::Drift));
    // A refused removal removes nothing.
    assert!(policy.exists());
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Ok(Some(token_again.clone()))
    );
    fs::set_permissions(&policy, fs::Permissions::from_mode(0o640)).expect("mode set");
    fixture.store.remove("identity").expect("removed");
    assert!(!policy.exists());
    assert_eq!(
        fixture.store.token("identity", false, &fixture.entropy),
        Ok(None)
    );
}

#[test]
fn docker_environment_lists_admit_unique_names_only() {
    let list = |items: &[&str]| {
        items
            .iter()
            .map(|item| (*item).to_owned())
            .collect::<Vec<String>>()
    };
    assert_eq!(
        environment_map(&list(&["A=1", "B=x=y", "C="])),
        Some(vec![
            ("A".to_owned(), "1".to_owned()),
            ("B".to_owned(), "x=y".to_owned()),
            ("C".to_owned(), String::new()),
        ])
    );
    for refused in [&["A"][..], &["=1"], &["A=1", "A=2"]] {
        assert_eq!(environment_map(&list(refused)), None, "{refused:?}");
    }
}
