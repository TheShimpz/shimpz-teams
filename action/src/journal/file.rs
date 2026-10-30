//! The journal file's custody: a `0600` regular file this process owns, with one link, in a `0700` directory it owns.

use std::fs::{DirBuilder, File};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::Path;

use rustix::fs::{Mode as RawMode, OFlags};
use team_storage::custody::{self, DirectoryCustody, Facts, FileCustody, Mode, current_uid};
use team_storage::durable;

use super::JournalError;

const UNAVAILABLE: JournalError =
    JournalError::Corrupt("Action journal private path is unavailable");

/// Proves the parent directory and any existing file, creating either when absent; whether the database is new.
pub(super) fn prepare(path: &Path) -> Result<bool, JournalError> {
    let parent = path.parent().ok_or(UNAVAILABLE)?;
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(parent)
        .map_err(|_| UNAVAILABLE)?;
    let owned = DirectoryCustody {
        uid: Some(current_uid()),
        gid: None,
        mode: Mode::Any,
    };
    if !custody::facts(parent)
        .map_err(|_| UNAVAILABLE)?
        .is_some_and(|facts| owned.holds(&facts))
    {
        return Err(JournalError::Corrupt(
            "Action journal parent is not a private directory",
        ));
    }
    std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| UNAVAILABLE)?;
    match custody::facts(path).map_err(|_| UNAVAILABLE)? {
        None => {
            create(path)?;
            durable::fsync_directory(parent).map_err(|_| UNAVAILABLE)?;
            Ok(true)
        }
        Some(facts) => {
            owned_file(&facts)?;
            if facts.permissions & 0o077 != 0 {
                return Err(JournalError::Corrupt(
                    "Action journal file permissions are not private",
                ));
            }
            Ok(facts.size == 0)
        }
    }
}

fn owned_file(facts: &Facts) -> Result<(), JournalError> {
    let owned = FileCustody {
        uid: Some(current_uid()),
        gid: None,
        mode: Mode::Any,
        size: None,
    };
    if owned.holds(facts) {
        Ok(())
    } else {
        Err(JournalError::Corrupt(
            "Action journal path has unsafe ownership or links",
        ))
    }
}

fn create(path: &Path) -> Result<(), JournalError> {
    let flags = OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let file = File::from(
        rustix::fs::open(path, flags, RawMode::from_raw_mode(0o600)).map_err(|_| UNAVAILABLE)?,
    );
    owned_file(&Facts::from(&file.metadata().map_err(|_| UNAVAILABLE)?))?;
    file.sync_all().map_err(|_| UNAVAILABLE)
}

/// Sets the opened database file to `0600`.
pub(super) fn private_mode(path: &Path) -> Result<(), JournalError> {
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).map_err(|_| UNAVAILABLE)
}
