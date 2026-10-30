//! `wipe-runtime-state <root>`: empties every runtime-state volume mounted below `root`, as the CLI's Space reset
//! runs it as the volumes' owner. First every entry below `root` must belong to the owner, never following a link; a
//! traversal error or another owner's entry stops the wipe before anything changes. Only then does the owner gain
//! `u+rwx` on its own directories below the volume roots, everything below each volume root is deleted (the roots
//! stay), and the file systems are synced.

use std::fs::{self, Metadata};
use std::io;
use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
use std::path::Path;

/// Whether `path` is a directory itself (a link never is).
fn directory(metadata: &Metadata) -> bool {
    metadata.file_type().is_dir()
}

/// Requires every entry below `path` to belong to `owner`.
fn owned_below(path: &Path, owner: u32) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.uid() != owner {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "an entry belongs to another owner",
            ));
        }
        if directory(&metadata) {
            owned_below(&entry.path(), owner)?;
        }
    }
    Ok(())
}

/// Gives the owner `u+rwx` on `path` when it lacks any of them, then on every directory below it.
fn enterable(path: &Path) -> io::Result<()> {
    let mode = fs::symlink_metadata(path)?.permissions().mode();
    if mode & 0o700 != 0o700 {
        fs::set_permissions(path, fs::Permissions::from_mode(mode | 0o700))?;
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if directory(&fs::symlink_metadata(entry.path())?) {
            enterable(&entry.path())?;
        }
    }
    Ok(())
}

/// Deletes everything below `path`, depth first, never following a link.
fn empty(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if directory(&fs::symlink_metadata(entry.path())?) {
            empty(&entry.path())?;
            fs::remove_dir(entry.path())?;
        } else {
            fs::remove_file(entry.path())?;
        }
    }
    Ok(())
}

/// The volume roots below `root`: its directories (a link or file there is only checked, never changed).
fn volumes(root: &Path) -> io::Result<Vec<std::path::PathBuf>> {
    let mut volumes = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if directory(&fs::symlink_metadata(entry.path())?) {
            volumes.push(entry.path());
        }
    }
    Ok(volumes)
}

/// Wipes the volumes below `root` for `owner`.
///
/// # Errors
/// Any traversal or removal failure, or an entry of another owner (then nothing changed).
pub fn wipe(root: &Path, owner: u32) -> io::Result<()> {
    owned_below(root, owner)?;
    let volumes = volumes(root)?;
    for volume in &volumes {
        for entry in fs::read_dir(volume)? {
            let entry = entry?;
            if directory(&fs::symlink_metadata(entry.path())?) {
                enterable(&entry.path())?;
            }
        }
    }
    for volume in &volumes {
        empty(volume)?;
    }
    rustix::fs::sync();
    Ok(())
}

/// [`wipe`] as the process's own user.
///
/// # Errors
/// As [`wipe`].
pub fn wipe_as_self(root: &Path) -> io::Result<()> {
    wipe(root, rustix::process::getuid().as_raw())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _, symlink};

    use super::wipe;

    #[test]
    fn every_volume_is_emptied_its_root_kept_and_no_link_followed() {
        let outside = tempfile::tempdir().expect("a temporary directory");
        fs::write(outside.path().join("kept"), b"x").expect("written");
        let directory = tempfile::tempdir().expect("a temporary directory");
        let root = directory.path();
        let volume = root.join("state");
        fs::create_dir_all(volume.join("a/b")).expect("created");
        fs::write(volume.join("a/b/file"), b"x").expect("written");
        fs::write(volume.join("top"), b"x").expect("written");
        symlink(outside.path(), volume.join("link")).expect("linked");
        // A directory its owner may not write is still emptied.
        fs::set_permissions(volume.join("a/b"), fs::Permissions::from_mode(0o500)).expect("mode");
        fs::write(root.join("loose"), b"x").expect("written");
        let owner = fs::metadata(root).expect("stat").uid();
        wipe(root, owner).expect("wiped");
        assert!(volume.is_dir());
        assert_eq!(fs::read_dir(&volume).expect("listed").count(), 0);
        assert!(root.join("loose").exists());
        assert!(outside.path().join("kept").exists());
    }

    #[test]
    fn another_owner_s_entry_stops_the_wipe_before_anything_changes() {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let root = directory.path();
        fs::create_dir_all(root.join("state/a")).expect("created");
        fs::write(root.join("state/a/file"), b"x").expect("written");
        let owner = fs::metadata(root).expect("stat").uid();
        assert!(wipe(root, owner + 1).is_err());
        assert!(root.join("state/a/file").exists());
        assert!(wipe(&root.join("missing"), owner).is_err());
    }
}
