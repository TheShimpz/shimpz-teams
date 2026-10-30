//! Reading a Team's files: a listed file, a released or acknowledged one, its bytes, and the metadata of a turn's
//! selection.

use super::{
    Digest, FILE_NOT_FOUND, Failure, FileMetadata, FileStore, MAX_ACKNOWLEDGEMENTS,
    METADATA_SELECTS, MetadataReader, OptionalExtension, Sha256, StorageError, StorageErrorKind,
    StoredFile, TEAM_NOT_FOUND, UNREFERENCED_GRACE_SECONDS, database, file_id, hex, params,
    principal, selected, team_id,
};

impl FileStore {
    /// The metadata of one file the Team's listing still shows, without reading its bytes or renewing its grace.
    ///
    /// A file that is absent, or unreferenced and idle past its grace period, is not found: the next sweep collects it.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], or an unsafe refusal.
    pub fn listed(&self, team: &str, file: &str) -> Result<FileMetadata, StorageError> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let limit_bytes = self.limit(&team)?;
        let connection = match self.connect(&team, false, limit_bytes) {
            Err(error) if error == TEAM_NOT_FOUND => return Err(FILE_NOT_FOUND),
            other => other?,
        };
        let row = connection
            .query_row(
                "SELECT name,media_type,size,sha256 FROM files WHERE id=? AND (referenced=1 OR idle_since>?)",
                params![id, self.now() - UNREFERENCED_GRACE_SECONDS],
                |row| {
                    Ok(FileMetadata {
                        id: id.clone(),
                        name: row.get(0)?,
                        media_type: row.get(1)?,
                        size: row.get(2)?,
                        sha256: row.get(3)?,
                    })
                },
            )
            .optional()
            .map_err(|error| database(&error))?;
        row.ok_or(FILE_NOT_FOUND)
    }

    /// Whether the file's original may leave Team for `principal`: it is not from the web, or they acknowledged it.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] for an absent file or storage; an unsafe refusal when it cannot be read.
    pub fn released(
        &self,
        team: &str,
        file: &str,
        who: Option<&str>,
    ) -> Result<bool, StorageError> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let limit_bytes = self.limit(&team)?;
        self.transaction(&team, false, limit_bytes, |connection| {
            let web: Option<i64> = connection
                .query_row("SELECT web FROM files WHERE id=?", [&id], |row| row.get(0))
                .optional()?;
            match web {
                None => Err(Failure::Storage(FILE_NOT_FOUND)),
                Some(0) => Ok(true),
                Some(_) => {
                    let Some(who) = who else {
                        return Ok(false);
                    };
                    let who = principal(who)?;
                    Ok(connection
                        .query_row(
                            "SELECT 1 FROM acknowledgements WHERE file_id=? AND principal=?",
                            params![id, who],
                            |_| Ok(()),
                        )
                        .optional()?
                        .is_some())
                }
            }
        })
        .map_err(Failure::into_storage)
    }

    /// Records that `who` accepted the risk of one listed file from the web; `record` audits it first.
    ///
    /// The acknowledgement commits only once `record` returned, so no authorization exists without its audit.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], [`StorageErrorKind::NotWeb`], [`StorageErrorKind::Quota`] at the
    /// acknowledgement limit, input or unsafe refusals, or `record`'s own error (nothing is then committed).
    pub fn acknowledge<E: From<StorageError>>(
        &self,
        team: &str,
        file: &str,
        who: &str,
        record: impl FnOnce(&str) -> Result<(), E>,
    ) -> Result<(), E> {
        let team = team_id(team)?;
        let id = file_id(file)?.to_owned();
        let who = principal(who)?;
        let limit_bytes = self.limit(&team)?;
        let now = self.now();
        let mut record = Some(record);
        let mut recorded: Result<(), E> = Ok(());
        let committed = self.transaction(&team, false, limit_bytes, |connection| {
            let web: Option<i64> = connection
                .query_row(
                    "SELECT web FROM files WHERE id=? AND (referenced=1 OR idle_since>?)",
                    params![id, now - UNREFERENCED_GRACE_SECONDS],
                    |row| row.get(0),
                )
                .optional()?;
            match web {
                None => return Err(Failure::Storage(FILE_NOT_FOUND)),
                Some(0) => {
                    return Err(Failure::Storage(StorageError::new(
                        StorageErrorKind::NotWeb,
                        "the file did not come from the web",
                    )));
                }
                Some(_) => {}
            }
            let count: i64 = connection.query_row("SELECT count(*) FROM acknowledgements", [], |row| row.get(0))?;
            if count >= MAX_ACKNOWLEDGEMENTS {
                return Err(Failure::Storage(StorageError::new(
                    StorageErrorKind::Quota,
                    "Team acknowledgement limit reached",
                )));
            }
            connection.execute(
                "INSERT OR IGNORE INTO acknowledgements(file_id, principal, created_at) VALUES(?,?,?)",
                params![id, who, now],
            )?;
            if let Some(record) = record.take() {
                recorded = record(&id);
            }
            if recorded.is_err() {
                return Err(Failure::Refused);
            }
            Ok(())
        });
        match committed {
            Ok(()) => Ok(()),
            Err(Failure::Refused) => recorded,
            Err(other) => Err(other.into_storage().into()),
        }
    }

    /// One file's metadata and bytes, proven equal to its stored size and digest.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`], or an unsafe refusal, including a failed integrity check.
    pub fn get(&self, team: &str, file: &str) -> Result<(StoredFile, Vec<u8>), StorageError> {
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let id = file_id(file)?.to_owned();
        let connection = match self.connect(&team, false, limit_bytes) {
            Err(error) if error == TEAM_NOT_FOUND => return Err(FILE_NOT_FOUND),
            other => other?,
        };
        let row = connection
            .query_row(
                "SELECT name,media_type,size,sha256,created_at,content FROM files WHERE id=?",
                [&id],
                |row| {
                    Ok((
                        StoredFile {
                            metadata: FileMetadata {
                                id: id.clone(),
                                name: row.get(0)?,
                                media_type: row.get(1)?,
                                size: row.get(2)?,
                                sha256: row.get(3)?,
                            },
                            created_at: row.get(4)?,
                        },
                        row.get::<_, Vec<u8>>(5)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| database(&error))?;
        let (stored, content) = row.ok_or(FILE_NOT_FOUND)?;
        if i64::try_from(content.len()).ok() != Some(stored.metadata.size)
            || hex(&Sha256::digest(&content)) != stored.metadata.sha256
        {
            return Err(StorageError::unsafe_(
                "stored file failed its integrity check",
            ));
        }
        Ok((stored, content))
    }

    /// One selected-file reader to keep open across a chat turn; `None` when the turn selected no file.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] for absent storage; input or unsafe refusals.
    pub fn metadata_reader(
        &self,
        team: &str,
        file_ids: &[&str],
    ) -> Result<Option<MetadataReader>, StorageError> {
        if selected(file_ids)?.is_empty() {
            return Ok(None);
        }
        let team = team_id(team)?;
        let limit_bytes = self.limit(&team)?;
        let connection = self.connect(&team, false, limit_bytes)?;
        Ok(Some(MetadataReader { team, connection }))
    }

    /// The selected files' metadata in their selected order, through `reader` or a reader of its own.
    ///
    /// # Errors
    /// [`StorageErrorKind::NotFound`] when any is absent; a reader of another Team, input, or unsafe refusals.
    pub fn metadata(
        &self,
        team: &str,
        file_ids: &[&str],
        reader: Option<&MetadataReader>,
    ) -> Result<Vec<FileMetadata>, StorageError> {
        let ids = selected(file_ids)?;
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let team = team_id(team)?;
        let Some(reader) = reader else {
            let own = self.metadata_reader(&team, file_ids)?;
            return self.metadata(&team, file_ids, own.as_ref());
        };
        if reader.team != team {
            return Err(StorageError::unsafe_(
                "metadata reader belongs to another Team",
            ));
        }
        let read = || -> rusqlite::Result<Vec<FileMetadata>> {
            let mut statement = reader.connection.prepare(METADATA_SELECTS[ids.len() - 1])?;
            statement
                .query_map(rusqlite::params_from_iter(&ids), |row| {
                    Ok(FileMetadata {
                        id: row.get(0)?,
                        name: row.get(1)?,
                        media_type: row.get(2)?,
                        size: row.get(3)?,
                        // The original digest binds a turn to these exact bytes across its continuations (ADR-0093).
                        sha256: row.get(4)?,
                    })
                })?
                .collect()
        };
        let rows = read().map_err(|error| database(&error))?;
        ids.iter()
            .map(|id| {
                rows.iter()
                    .find(|row| row.id == *id)
                    .cloned()
                    .ok_or(FILE_NOT_FOUND)
            })
            .collect()
    }
}
