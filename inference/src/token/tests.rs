use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::PathBuf;

use team_core::entropy::OsEntropy;

use super::{TOKEN_BYTES, TokenError, decode, ensure, read};

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    path: PathBuf,
    group: u32,
}

fn fixture() -> Fixture {
    let directory = tempfile::tempdir().expect("tempdir");
    let root = directory.path().join("runtime-token");
    let path = root.join("token");
    let group = rustix::process::getegid().as_raw();
    Fixture {
        _directory: directory,
        root,
        path,
        group,
    }
}

#[test]
fn create_uses_a_strong_token_and_exact_shared_group_permissions() {
    let fixture = fixture();
    let token = ensure(&fixture.path, fixture.group, &OsEntropy).expect("token");
    assert_eq!(token.expose().len(), TOKEN_BYTES * 2);
    assert!(token.expose().bytes().all(|byte| byte.is_ascii_hexdigit()));
    let root = fs::metadata(&fixture.root).expect("root");
    let file = fs::metadata(&fixture.path).expect("file");
    assert_eq!(root.permissions().mode() & 0o7777, 0o750);
    assert_eq!(file.permissions().mode() & 0o7777, 0o440);
    assert_eq!((root.gid(), file.gid()), (fixture.group, fixture.group));
    let names: Vec<_> = fs::read_dir(&fixture.root)
        .expect("listed")
        .map(|entry| entry.expect("entry").file_name())
        .collect();
    assert_eq!(names, vec!["token"]);
    assert_eq!(
        read(&fixture.path, fixture.group).expect("read").expose(),
        token.expose()
    );
}

#[test]
fn an_existing_safe_token_is_reused_without_replacement() {
    let fixture = fixture();
    let first = ensure(&fixture.path, fixture.group, &OsEntropy).expect("first");
    let inode = fs::metadata(&fixture.path).expect("file").ino();
    let second = ensure(&fixture.path, fixture.group, &OsEntropy).expect("second");
    assert_eq!(first.expose(), second.expose());
    assert_eq!(fs::metadata(&fixture.path).expect("file").ino(), inode);
}

#[test]
fn a_link_or_an_insecure_existing_file_fails_closed() {
    let fixture = fixture();
    fs::create_dir(&fixture.root).expect("root");
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o750)).expect("mode");
    let victim = fixture.root.parent().expect("parent").join("victim");
    fs::write(&victim, "do-not-read").expect("victim");
    symlink(&victim, &fixture.path).expect("link");
    assert_eq!(
        ensure(&fixture.path, fixture.group, &OsEntropy).err(),
        Some(TokenError::Unsafe)
    );
    assert_eq!(
        read(&fixture.path, fixture.group).err(),
        Some(TokenError::Unsafe)
    );
    assert_eq!(fs::read_to_string(&victim).expect("victim"), "do-not-read");

    fs::remove_file(&fixture.path).expect("unlinked");
    fs::write(&fixture.path, "a".repeat(64)).expect("token");
    fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o640)).expect("mode");
    assert_eq!(
        ensure(&fixture.path, fixture.group, &OsEntropy).err(),
        Some(TokenError::Unsafe)
    );
    assert_eq!(
        read(&fixture.path, fixture.group).err(),
        Some(TokenError::Unsafe)
    );
}

#[test]
fn an_unsafe_directory_fails_closed() {
    let fixture = fixture();
    fs::create_dir(&fixture.root).expect("root");
    fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o755)).expect("mode");
    assert_eq!(
        ensure(&fixture.path, fixture.group, &OsEntropy).err(),
        Some(TokenError::DirectoryUnsafe)
    );
    assert_eq!(
        read(&fixture.path, fixture.group).err(),
        Some(TokenError::DirectoryUnsafe)
    );
    let other = fixture.root.parent().expect("parent").join("file");
    fs::write(&other, "x").expect("file");
    assert_eq!(
        ensure(&other.join("token"), fixture.group, &OsEntropy).err(),
        Some(TokenError::DirectoryUnsafe)
    );
}

#[test]
fn a_missing_token_is_never_created_by_a_read() {
    let fixture = fixture();
    assert_eq!(
        read(&fixture.path, fixture.group).err(),
        Some(TokenError::DirectoryUnavailable)
    );
    ensure(&fixture.path, fixture.group, &OsEntropy).expect("token");
    fs::remove_file(&fixture.path).expect("removed");
    assert_eq!(
        read(&fixture.path, fixture.group).err(),
        Some(TokenError::Unavailable)
    );
    assert!(!fixture.path.exists());
}

#[test]
fn only_sixty_four_hexadecimal_digits_decode() {
    for raw in [b"short".as_slice(), &[b'g'; 64], &[b' '; 64], &[b'a'; 65]] {
        assert_eq!(decode(raw).err(), Some(TokenError::Invalid));
    }
    assert_eq!(decode(&[b'A'; 64]).expect("token").expose(), "A".repeat(64));
    let wrong = fixture();
    ensure(&wrong.path, wrong.group, &OsEntropy).expect("token");
    fs::set_permissions(&wrong.path, fs::Permissions::from_mode(0o640)).expect("mode");
    fs::write(&wrong.path, "g".repeat(64)).expect("token");
    fs::set_permissions(&wrong.path, fs::Permissions::from_mode(0o440)).expect("mode");
    assert_eq!(
        read(&wrong.path, wrong.group).err(),
        Some(TokenError::Invalid)
    );
}

#[test]
fn errors_never_carry_the_token() {
    for error in [
        TokenError::DirectoryUnsafe,
        TokenError::DirectoryNotCreated,
        TokenError::DirectoryUnavailable,
        TokenError::Unsafe,
        TokenError::Unavailable,
        TokenError::Invalid,
        TokenError::NotCreated,
    ] {
        assert!(error.to_string().starts_with("the Brain runtime token"));
    }
}
