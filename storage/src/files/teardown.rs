//! Removing a Team's files, or every Team's.

use super::{FileStore, StorageError, custody, owner_only_directory, team_id};

impl FileStore {
    /// Removes one Team's storage; whether it existed.
    ///
    /// # Errors
    /// An unsafe refusal for a directory outside custody or a failed removal.
    pub fn destroy(&self, team: &str) -> Result<bool, StorageError> {
        let team = team_id(team)?;
        let directory = self.root.join(&team);
        let Some(facts) = custody::facts(&directory)
            .map_err(|_| StorageError::unsafe_("Team storage is unavailable"))?
        else {
            return Ok(false);
        };
        if !owner_only_directory(&facts) {
            return Err(StorageError::unsafe_(
                "Team storage has unsafe ownership or permissions",
            ));
        }
        std::fs::remove_dir_all(&directory)
            .map_err(|_| StorageError::unsafe_("Team storage could not be removed"))?;
        Ok(true)
    }

    /// Removes every strictly shaped Team directory from this dedicated volume; how many it removed.
    ///
    /// # Errors
    /// An unsafe refusal for an entry that is not a Team's storage, or a failed removal.
    pub fn destroy_all(&self) -> Result<usize, StorageError> {
        self.ensure_root()?;
        let mut names = std::fs::read_dir(&self.root)
            .map_err(|_| StorageError::unsafe_("storage root is unavailable"))?
            .map(|entry| {
                entry
                    .ok()
                    .and_then(|entry| entry.file_name().into_string().ok())
                    .ok_or(StorageError::unsafe_("invalid Team id"))
            })
            .collect::<Result<Vec<String>, StorageError>>()?;
        names.sort();
        let mut removed = 0;
        for name in names {
            if self.destroy(&name)? {
                removed += 1;
            }
        }
        Ok(removed)
    }
}
