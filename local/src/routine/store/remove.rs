//! Listing and removing Team directories under the Routine root through verified handles, never following a link.

use std::os::fd::AsFd;
use std::path::Path;

use rustix::fs::{AtFlags, Dir, FileType, Mode, OFlags, openat, statat, unlinkat};
use rustix::io::Errno;
use team_protocol::payload::SHA256_RE;

use super::StoreError;

/// The Team directories under `root`, sorted; a symlink or file where one belongs fails closed.
///
/// # Errors
/// [`StoreError::Unsafe`] for a non-directory Team entry, [`StoreError::Unavailable`] when the root cannot be listed.
pub fn owned_directories(root: &Path) -> Result<Vec<String>, StoreError> {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(StoreError::Unavailable),
    };
    let mut owned = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|_| StoreError::Unavailable)?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !SHA256_RE.is_full(&name) {
            continue;
        }
        let kind = entry.file_type().map_err(|_| StoreError::Unavailable)?;
        if !kind.is_dir() {
            return Err(StoreError::Unsafe);
        }
        owned.push(name);
    }
    owned.sort();
    Ok(owned)
}

const DIRECTORY: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// Deletes Team directory `name` of `root`: every entry must be a regular file, removed through the directory's own
/// handle, and then the directory itself. An absent root or directory is already removed.
///
/// # Errors
/// [`StoreError::Unsafe`] for any entry that is not a regular file, [`StoreError::NotPersisted`] for a failed removal.
pub fn remove_directory(root: &Path, name: &str) -> Result<(), StoreError> {
    let root = match openat(rustix::fs::CWD, root, DIRECTORY, Mode::empty()) {
        Ok(root) => root,
        Err(Errno::NOENT) => return Ok(()),
        Err(_) => return Err(StoreError::Unavailable),
    };
    let directory = match openat(root.as_fd(), name, DIRECTORY, Mode::empty()) {
        Ok(directory) => directory,
        Err(Errno::NOENT) => return Ok(()),
        Err(_) => return Err(StoreError::NotPersisted),
    };
    let mut names = Vec::new();
    for entry in Dir::read_from(directory.as_fd()).map_err(|_| StoreError::NotPersisted)? {
        let entry = entry.map_err(|_| StoreError::NotPersisted)?;
        let entry_name = entry.file_name().to_bytes();
        if entry_name != b"." && entry_name != b".." {
            names.push(entry.file_name().to_owned());
        }
    }
    for entry in &names {
        let stat = statat(
            directory.as_fd(),
            entry.as_c_str(),
            AtFlags::SYMLINK_NOFOLLOW,
        )
        .map_err(|_| StoreError::NotPersisted)?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
            return Err(StoreError::Unsafe);
        }
        unlinkat(directory.as_fd(), entry.as_c_str(), AtFlags::empty())
            .map_err(|_| StoreError::NotPersisted)?;
    }
    unlinkat(root.as_fd(), name, AtFlags::REMOVEDIR).map_err(|_| StoreError::NotPersisted)
}
