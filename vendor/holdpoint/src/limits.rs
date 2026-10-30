//! Size bounds of the ledger and timing of the worker.

use crate::error::{Error, LimitKind};

/// Size bounds the store enforces on every write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    /// The largest encoded run input, step input, step result, or outcome.
    pub max_value_bytes: usize,
    /// The most events one run's history may hold.
    pub max_events_per_run: u64,
    /// The most encoded bytes one run's history may hold.
    pub max_run_bytes: u64,
    /// The most runs (open, ended, or acknowledged tombstones) the store may hold.
    pub max_runs: u64,
    /// The largest transient blob.
    pub max_blob_bytes: usize,
    /// The most transient blobs one run may hold at once.
    pub max_blobs_per_run: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_value_bytes: 1 << 20,
            max_events_per_run: 100_000,
            max_run_bytes: 64 << 20,
            max_runs: 100_000,
            max_blob_bytes: 4 << 20,
            max_blobs_per_run: 8,
        }
    }
}

impl Limits {
    /// Refuses an encoded value over [`Limits::max_value_bytes`].
    ///
    /// # Errors
    ///
    /// [`Error::Limit`] with [`LimitKind::Value`].
    pub fn check_value(&self, encoded: &str) -> Result<(), Error> {
        if encoded.len() > self.max_value_bytes { Err(Error::Limit(LimitKind::Value)) } else { Ok(()) }
    }
}

/// How a worker paces leases, waits, and abandoned attempts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerOptions {
    /// How long a drive session's lease lasts without renewal.
    pub lease_ms: u64,
    /// The longest wait (a retry delay or a timer) served inside a drive; a longer one suspends the run.
    pub inline_wait_ms: u64,
    /// How often a waiting drive looks for a cancellation or a newer event written by another process.
    pub poll_ms: u64,
    /// How long an abandoned attempt's thread gets to return before it becomes an obligation.
    pub abandon_grace_ms: u64,
    /// The most abandoned attempt threads a worker tolerates before it dispatches nothing new.
    pub max_abandoned: usize,
    /// How long a run waits before asking the host again to prove that an earlier executor ended.
    pub proof_retry_ms: u64,
    /// How many runs the background scheduler drives at once.
    pub concurrency: usize,
}

impl Default for WorkerOptions {
    fn default() -> Self {
        Self {
            lease_ms: 30_000,
            inline_wait_ms: 1_000,
            poll_ms: 250,
            abandon_grace_ms: 2_000,
            max_abandoned: 16,
            proof_retry_ms: 1_000,
            concurrency: 4,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_over_the_bound_are_refused() {
        let limits = Limits { max_value_bytes: 3, ..Limits::default() };
        assert_eq!(limits.check_value("abc"), Ok(()));
        assert_eq!(limits.check_value("abcd"), Err(Error::Limit(LimitKind::Value)));
    }

    #[test]
    fn defaults_are_bounded() {
        let options = WorkerOptions::default();
        assert!(options.inline_wait_ms < options.lease_ms / 3);
        assert!(options.poll_ms < options.lease_ms / 3);
    }
}
