//! What a host provides: authority for a run, proof that an earlier executor ended, and the clean release of a
//! session's executors.

use std::any::Any;
use std::fmt;
use std::sync::Arc;

use serde_json::Value;

use crate::store::Obligation;

/// Request-scoped credentials the host supplies for one drive session. Kept in memory only, never persisted,
/// logged, or put in an event or an error.
#[derive(Clone)]
pub struct Credentials(Option<Arc<dyn Any + Send + Sync>>);

impl Credentials {
    /// Credentials holding `value`.
    pub fn new<T: Any + Send + Sync>(value: T) -> Self {
        Self(Some(Arc::new(value)))
    }

    /// No credentials.
    #[must_use]
    pub fn none() -> Self {
        Self(None)
    }

    /// The held value, if it has type `T`.
    #[must_use]
    pub fn get<T: Any>(&self) -> Option<&T> {
        self.0.as_deref().and_then(|value| value.downcast_ref())
    }
}

impl fmt::Debug for Credentials {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Credentials(redacted)")
    }
}

/// The run a hook is asked about.
#[derive(Debug, Clone, Copy)]
pub struct RunInfo<'a> {
    /// The run's identifier.
    pub id: &'a str,
    /// The run's definition.
    pub definition: &'a str,
    /// The definition's version.
    pub version: u32,
    /// The run's captured input (it holds the run's originating, non-secret identity).
    pub input: &'a Value,
}

/// The host's answer about a run's authority.
#[derive(Debug, Clone)]
pub enum Authority {
    /// The run's original authority is still current; these credentials serve this drive session.
    Granted(Credentials),
    /// Credentials are not available yet; the run waits until the host wakes it.
    Wait,
    /// The run's original authority was revoked; the run ends `unrecoverable(authority-revoked)`.
    Revoked,
}

/// The host's answer about an executor obligation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Proof {
    /// The executor ended (or an executor-side guarantee refuses its every further effect).
    Ended,
    /// Not proven yet; the run waits and asks again.
    NotYet,
}

/// The hooks a host implements.
pub trait Host: Send + Sync {
    /// Revalidates the run's original authority before the session dispatches anything.
    fn authorize(&self, run: &RunInfo<'_>) -> Authority;

    /// Proves that the executor an obligation names has ended.
    fn prove_ended(&self, run: &RunInfo<'_>, obligation: &Obligation) -> Proof;

    /// Ends, or explicitly hands off, every executor the session with `fence` owns, before the session releases the
    /// run cleanly. An error leaves the release unclean: the next session must prove the executors ended.
    ///
    /// # Errors
    ///
    /// A short reason when the executors could not be ended or handed off.
    fn release(&self, run: &RunInfo<'_>, fence: u64) -> Result<(), String> {
        let _ = (run, fence);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credentials_are_typed_and_redacted() {
        let credentials = Credentials::new(String::from("sk-not-a-real-key"));
        assert_eq!(credentials.get::<String>().map(String::as_str), Some("sk-not-a-real-key"));
        assert_eq!(credentials.get::<u8>(), None);
        assert_eq!(format!("{credentials:?}"), "Credentials(redacted)");
        assert_eq!(Credentials::none().get::<String>(), None);
    }

    struct Plain;

    impl Host for Plain {
        fn authorize(&self, _run: &RunInfo<'_>) -> Authority {
            Authority::Wait
        }

        fn prove_ended(&self, _run: &RunInfo<'_>, _obligation: &Obligation) -> Proof {
            Proof::NotYet
        }
    }

    #[test]
    fn the_default_release_succeeds() {
        let input = Value::Null;
        let run = RunInfo { id: "r", definition: "d", version: 1, input: &input };
        assert_eq!(Plain.release(&run, 1), Ok(()));
        assert!(matches!(Plain.authorize(&run), Authority::Wait));
        let obligation = Obligation { id: 1, fence: 1, attempt: None, incarnation: "i".into() };
        assert_eq!(Plain.prove_ended(&run, &obligation), Proof::NotYet);
    }
}
