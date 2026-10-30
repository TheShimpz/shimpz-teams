//! Run protection: every value a Routine run was given or returned in secret, in this Team process only (ADR-0101).
//!
//! A run's protection is bound once, when its cursor is first sealed in this Team boot, and then only grows: every
//! value Team holds for an attempt (Stored Inputs and Integration tokens) before its RPC, and every string at a secret
//! position of every result before the result reaches anything outward. Team never rebuilds it from current stores: a
//! run resumed in another boot, or one whose protection is missing here, has lost it, and so has a run whose protection
//! would exceed its bound. Loss is never undone.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use team_core::entropy::{Entropy, EntropyUnavailable, token_hex};
use team_routine::trace::Protection;

/// Every run's protection in this Team process, keyed by run id, with the boot it was bound in.
#[derive(Debug)]
pub struct RunProtections {
    boot: String,
    runs: Mutex<HashMap<String, Protection>>,
}

impl RunProtections {
    /// The protections of a fresh boot, named by 32 random hex digits.
    ///
    /// # Errors
    /// [`EntropyUnavailable`].
    pub fn new(entropy: &dyn Entropy) -> Result<Self, EntropyUnavailable> {
        Ok(Self::of_boot(token_hex(entropy, 16)?))
    }

    /// The protections of the boot named `boot`.
    #[must_use]
    pub fn of_boot(boot: String) -> Self {
        Self {
            boot,
            runs: Mutex::new(HashMap::new()),
        }
    }

    /// This process's boot.
    #[must_use]
    pub fn boot(&self) -> &str {
        &self.boot
    }

    fn runs(&self) -> MutexGuard<'_, HashMap<String, Protection>> {
        self.runs.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A fresh run's protection, bound in this boot; binding again keeps what it holds.
    pub fn bind(&self, run_id: &str) -> Protection {
        self.runs().entry(run_id.to_owned()).or_default().clone()
    }

    /// The run's protection, lost when it was bound in another boot or is not held here.
    pub fn current(&self, run_id: &str, boot: &str) -> Protection {
        let mut runs = self.runs();
        let held = runs.get(run_id).filter(|_| boot == self.boot).cloned();
        held.unwrap_or_else(|| {
            let lost = Protection {
                lost: true,
                ..Protection::default()
            };
            runs.insert(run_id.to_owned(), lost.clone());
            lost
        })
    }

    /// Protects more values of one run; a run with no protection here has lost it.
    pub fn grow<'v>(&self, run_id: &str, values: impl IntoIterator<Item = &'v str>) -> Protection {
        let mut runs = self.runs();
        let held = runs.get(run_id).cloned().unwrap_or(Protection {
            lost: true,
            ..Protection::default()
        });
        let grown = held.grow(values);
        runs.insert(run_id.to_owned(), grown.clone());
        grown
    }

    /// Forgets an ended run's protection.
    pub fn drop_run(&self, run_id: &str) {
        self.runs().remove(run_id);
    }

    /// Forgets every run's protection, as a Space reset does.
    pub fn clear(&self) {
        self.runs().clear();
    }
}

#[cfg(test)]
mod tests {
    use super::RunProtections;

    #[test]
    fn a_runs_protection_grows_only_in_the_boot_that_bound_it() {
        let protections = RunProtections::of_boot("b".repeat(32));
        let run = "a".repeat(32);
        assert!(!protections.bind(&run).lost);
        let grown = protections.grow(&run, ["token-one", ""]);
        assert_eq!(grown.values.iter().collect::<Vec<_>>(), ["token-one"]);
        assert_eq!(protections.bind(&run), grown);
        assert_eq!(protections.current(&run, &"b".repeat(32)), grown);
        protections.drop_run(&run);
        assert!(!protections.bind(&run).lost);
    }

    #[test]
    fn a_protection_bound_elsewhere_or_not_held_is_lost_for_good() {
        let protections = RunProtections::of_boot("b".repeat(32));
        let run = "a".repeat(32);
        protections.bind(&run);
        assert!(protections.current(&run, &"c".repeat(32)).lost);
        assert!(protections.bind(&run).lost);
        assert!(protections.grow(&"e".repeat(32), ["x"]).lost);
        assert!(protections.current(&"f".repeat(32), &"b".repeat(32)).lost);
        protections.clear();
        assert!(protections.current(&run, &"b".repeat(32)).lost);
    }

    #[test]
    fn a_protection_past_its_bound_is_lost() {
        let protections = RunProtections::of_boot("b".repeat(32));
        let run = "a".repeat(32);
        protections.bind(&run);
        let values: Vec<String> = (0..=team_routine::trace::MAX_PROTECTED_VALUES)
            .map(|index| format!("v{index}"))
            .collect();
        assert!(
            protections
                .grow(&run, values.iter().map(String::as_str))
                .lost
        );
        assert!(protections.grow(&run, ["more"]).lost);
    }
}
