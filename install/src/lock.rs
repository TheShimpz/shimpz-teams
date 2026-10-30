//! Advisory file locks over private lock files for Team-owned install state (`install/lock.py`).

use std::fs::File;
use std::path::Path;

use rustix::fs::{FlockOperation, Mode, OFlags};

/// The lock could not be taken.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LockUnavailable;

/// One held `flock` on a private lock file, released when dropped.
#[derive(Debug)]
pub struct FileLock {
    file: File,
}

impl FileLock {
    /// Takes the exclusive lock on `path`, creating it (`0600`, its parent `0700`) when absent; never through a link.
    ///
    /// # Errors
    /// [`LockUnavailable`] when the file cannot be opened or locked.
    pub fn exclusive(path: &Path) -> Result<Self, LockUnavailable> {
        Self::take(path, FlockOperation::LockExclusive)
    }

    /// Takes the shared lock on `path`.
    ///
    /// # Errors
    /// [`LockUnavailable`] when the file cannot be opened or locked.
    pub fn shared(path: &Path) -> Result<Self, LockUnavailable> {
        Self::take(path, FlockOperation::LockShared)
    }

    fn take(path: &Path, operation: FlockOperation) -> Result<Self, LockUnavailable> {
        if let Some(parent) = path.parent() {
            team_storage::durable::create_private_directory(parent).map_err(|_| LockUnavailable)?;
        }
        let flags = OFlags::CREATE | OFlags::RDWR | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let descriptor = rustix::fs::open(path, flags, Mode::from_raw_mode(0o600))
            .map_err(|_| LockUnavailable)?;
        rustix::fs::flock(&descriptor, operation).map_err(|_| LockUnavailable)?;
        Ok(Self {
            file: File::from(descriptor),
        })
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = rustix::fs::flock(&self.file, FlockOperation::Unlock);
    }
}
