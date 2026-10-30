//! A Team's sealed recovery snapshots and incident records, under its held lock.

use super::*;

impl TeamLock<'_> {
    /// Seals one replayed run's immutable recovery snapshot before its first dispatch (ADR-0092): resealing the
    /// exact bytes succeeds, and any other snapshot for the run is refused.
    ///
    /// # Errors
    /// [`StoreError::Immutable`] for other bytes, or an invalid snapshot, a reset, or a failed write.
    pub fn put_recovery(&self, run: &str, payload: &[u8]) -> Result<(), StoreError> {
        let run = run_id(run)?;
        if !(1..=MAX_RECOVERY_BYTES).contains(&payload.len()) {
            return Err(StoreError::InvalidRecord);
        }
        let name = format!("{run}{}", Sealed::Recovery.suffix());
        self.sealed_write(
            &name,
            payload,
            &recovery_aad(&self.team, run),
            Sealed::Recovery,
            true,
        )
    }

    /// A run's recovery snapshot, or `None`.
    ///
    /// # Errors
    /// [`StoreError`] for one that cannot be read or authenticated.
    pub fn recovery(&self, run: &str) -> Result<Option<Vec<u8>>, StoreError> {
        let run = run_id(run)?;
        let name = format!("{run}{}", Sealed::Recovery.suffix());
        self.sealed_read(&name, &recovery_aad(&self.team, run), Sealed::Recovery)
    }

    /// Removes a run's recovery snapshot.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid run or a failed removal.
    pub fn delete_recovery(&self, run: &str) -> Result<(), StoreError> {
        self.sealed_delete(&format!("{}{}", run_id(run)?, Sealed::Recovery.suffix()))
    }

    /// The run ids with a stored recovery snapshot.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when they cannot be listed.
    pub fn recoveries(&self) -> Result<Vec<String>, StoreError> {
        self.sealed_names(Sealed::Recovery)
    }

    /// Seals one incident's compact safety evidence; it is durable before its batch is archived.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid incident or payload, a reset, or a failed write.
    pub fn put_incident(&self, incident: &str, payload: &[u8]) -> Result<(), StoreError> {
        let incident = run_id(incident)?;
        if !(1..=MAX_INCIDENT_BYTES).contains(&payload.len()) {
            return Err(StoreError::InvalidRecord);
        }
        let name = format!("{incident}{}", Sealed::Incident.suffix());
        self.sealed_write(
            &name,
            payload,
            &incident_aad(&self.team, incident),
            Sealed::Incident,
            false,
        )
    }

    /// An incident's evidence, or `None`.
    ///
    /// # Errors
    /// [`StoreError`] for evidence that cannot be read or authenticated.
    pub fn incident(&self, incident: &str) -> Result<Option<Vec<u8>>, StoreError> {
        let incident = run_id(incident)?;
        let name = format!("{incident}{}", Sealed::Incident.suffix());
        self.sealed_read(&name, &incident_aad(&self.team, incident), Sealed::Incident)
    }

    /// Removes an incident's evidence.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid incident or a failed removal.
    pub fn delete_incident(&self, incident: &str) -> Result<(), StoreError> {
        self.sealed_delete(&format!(
            "{}{}",
            run_id(incident)?,
            Sealed::Incident.suffix()
        ))
    }
}
