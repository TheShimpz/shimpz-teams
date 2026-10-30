//! Durable atomic replacement of a Team-held file, and the commit of a directory's entries.
//!
//! A replacement writes a unique, exclusively created temporary in the same directory that receives its final group
//! and mode before any byte, fsyncs it, renames it over the target, and fsyncs the directory. A crash therefore
//! leaves the complete earlier file or the complete new one, and a returned replacement survives power loss. No step
//! follows a link: the directory is opened with `O_NOFOLLOW` and every later step is relative to that descriptor.

use std::fs::{DirBuilder, File};
use std::io::{self, Write};
use std::os::fd::AsFd;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt, fchown};
use std::path::Path;

use rustix::fs::{AtFlags, Mode as RawMode, OFlags};
use team_core::entropy::{OsEntropy, token_hex};

use crate::custody::{self, DirectoryCustody, Facts};

/// Commits the entries of directory `path`, never through a link.
///
/// # Errors
/// Any failure to open or synchronize it.
pub fn fsync_directory(path: &Path) -> io::Result<()> {
    custody::open_directory(path)?.sync_all()
}

/// Atomically and durably replaces `path` with `payload`, created with `mode` and, when given, `group`.
///
/// # Errors
/// Any failure; the target then still holds its complete earlier content and no temporary is left behind.
pub fn replace(path: &Path, payload: &[u8], mode: u32, group: Option<u32>) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    replace_in_directory(
        &custody::open_directory(parent)?,
        name,
        payload,
        mode,
        group,
    )
}

/// [`replace`] of entry `name` of the open `directory`, which stays the directory written even if its path moves.
///
/// # Errors
/// As [`replace`].
pub fn replace_in_directory(
    directory: &File,
    name: &str,
    payload: &[u8],
    mode: u32,
    group: Option<u32>,
) -> io::Result<()> {
    let suffix = token_hex(&OsEntropy, 8).map_err(io::Error::other)?;
    let temporary = format!(".{name}.{suffix}.tmp");
    let written = write_temporary(directory, &temporary, payload, mode, group).and_then(|()| {
        rustix::fs::renameat(
            directory.as_fd(),
            temporary.as_str(),
            directory.as_fd(),
            name,
        )?;
        directory.sync_all()
    });
    if written.is_err() {
        // After a failure (for example a read-only volume) the cleanup must not replace the original error; after a
        // successful rename the temporary no longer exists.
        let _ = rustix::fs::unlinkat(directory.as_fd(), temporary.as_str(), AtFlags::empty());
    }
    written
}

fn write_temporary(
    directory: &File,
    temporary: &str,
    payload: &[u8],
    mode: u32,
    group: Option<u32>,
) -> io::Result<()> {
    let flags = OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let descriptor = rustix::fs::openat(
        directory.as_fd(),
        temporary,
        flags,
        RawMode::from_raw_mode(mode),
    )?;
    let mut file = File::from(descriptor);
    if group.is_some() {
        fchown(&file, None, group)?;
    }
    file.set_permissions(std::fs::Permissions::from_mode(mode))?;
    file.write_all(payload)?;
    file.sync_all()
}

/// Why a directory could not be proven private.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DirectoryError {
    /// It could not be created or examined.
    Unavailable,
    /// It exists outside its custody.
    Unsafe,
}

/// Creates `path` (and its missing parents) `0700` when absent, then requires it to be in `custody`.
///
/// # Errors
/// [`DirectoryError`] when it cannot be created or examined, or is not in custody.
pub fn ensure_directory(path: &Path, custody: &DirectoryCustody) -> Result<(), DirectoryError> {
    let facts = create_private_directory(path)
        .and_then(|()| std::fs::symlink_metadata(path))
        .map_err(|_| DirectoryError::Unavailable)?;
    if custody.holds(&Facts::from(&facts)) {
        Ok(())
    } else {
        Err(DirectoryError::Unsafe)
    }
}

/// Creates `path` and its missing parents with mode `0700`; an existing directory is left as it is.
///
/// # Errors
/// Any failure to create them.
pub fn create_private_directory(path: &Path) -> io::Result<()> {
    DirBuilder::new().recursive(true).mode(0o700).create(path)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};

    use super::{DirectoryError, ensure_directory, fsync_directory, replace, replace_in_directory};
    use crate::custody::{DirectoryCustody, Mode, current_uid, open_directory};

    fn entries(path: &std::path::Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(path)
            .expect("listed")
            .map(|entry| {
                entry
                    .expect("entry")
                    .file_name()
                    .into_string()
                    .expect("UTF-8")
            })
            .collect();
        names.sort();
        names
    }

    #[test]
    fn durable_write_excl_nofollow_fsync_rename() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("record");
        fs::write(&path, b"old").expect("written");
        let group = fs::metadata(root.path()).expect("stat").gid();
        replace(&path, b"new", 0o640, Some(group)).expect("replaced");
        assert_eq!(fs::read(&path).expect("read"), b"new");
        let metadata = fs::metadata(&path).expect("stat");
        assert_eq!((metadata.mode() & 0o7777, metadata.gid()), (0o640, group));
        assert_eq!(entries(root.path()), ["record"]);

        // A replacement is a new inode: a reader holding the earlier file keeps reading complete earlier content.
        let before = fs::metadata(&path).expect("stat").ino();
        replace(&path, b"next", 0o600, None).expect("replaced");
        assert_ne!(fs::metadata(&path).expect("stat").ino(), before);
        assert_eq!(fs::metadata(&path).expect("stat").mode() & 0o7777, 0o600);
    }

    #[test]
    fn a_failed_replacement_keeps_the_earlier_file_and_leaves_no_temporary() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("record");
        fs::write(&path, b"old").expect("written");
        // Changing to a group this process is not in fails at fchown, after the temporary exists (root may change it).
        if current_uid() != 0 {
            assert!(replace(&path, b"new", 0o600, Some(u32::MAX - 1)).is_err());
            assert_eq!(fs::read(&path).expect("read"), b"old");
            assert_eq!(entries(root.path()), ["record"]);
        }
        // A directory in the way of the rename fails it too.
        let blocked = root.path().join("blocked");
        fs::create_dir(&blocked).expect("created");
        fs::write(blocked.join("inside"), b"x").expect("written");
        assert!(replace(&blocked, b"new", 0o600, None).is_err());
        assert_eq!(entries(root.path()), ["blocked", "record"]);
    }

    #[test]
    fn replace_stays_anchored_to_the_opened_directory() {
        let root = tempfile::tempdir().expect("temporary directory");
        let opened = root.path().join("opened");
        fs::create_dir(&opened).expect("created");
        let directory = open_directory(&opened).expect("opened");
        fs::rename(&opened, root.path().join("moved")).expect("moved");
        fs::create_dir(&opened).expect("recreated");
        replace_in_directory(&directory, "record", b"anchored", 0o600, None).expect("replaced");
        assert_eq!(
            fs::read(root.path().join("moved/record")).expect("read"),
            b"anchored"
        );
        assert_eq!(entries(&opened), Vec::<String>::new());
    }

    #[test]
    fn replace_rejects_a_symbolic_link_parent_before_any_write() {
        let root = tempfile::tempdir().expect("temporary directory");
        let target = root.path().join("target");
        fs::create_dir(&target).expect("created");
        let link = root.path().join("link");
        symlink(&target, &link).expect("linked");
        assert!(replace(&link.join("record"), b"value", 0o600, None).is_err());
        assert_eq!(entries(&target), Vec::<String>::new());
        assert!(fsync_directory(&link).is_err());
        assert!(fsync_directory(&root.path().join("missing")).is_err());
        fsync_directory(root.path()).expect("synchronized");
    }

    #[test]
    fn a_private_directory_is_created_owner_only_and_refused_otherwise() {
        let root = tempfile::tempdir().expect("temporary directory");
        let private = DirectoryCustody::private();
        let nested = root.path().join("a/b");
        ensure_directory(&nested, &private).expect("created");
        assert_eq!(fs::metadata(&nested).expect("stat").mode() & 0o7777, 0o700);
        fs::set_permissions(&nested, fs::Permissions::from_mode(0o755)).expect("mode set");
        assert_eq!(
            ensure_directory(&nested, &private),
            Err(DirectoryError::Unsafe)
        );
        let file = root.path().join("file");
        fs::write(&file, b"").expect("written");
        assert_eq!(
            ensure_directory(&file, &private),
            Err(DirectoryError::Unavailable)
        );
        let other = DirectoryCustody {
            uid: Some(current_uid().wrapping_add(1)),
            gid: None,
            mode: Mode::Any,
        };
        assert_eq!(
            ensure_directory(root.path(), &other),
            Err(DirectoryError::Unsafe)
        );
    }
}
