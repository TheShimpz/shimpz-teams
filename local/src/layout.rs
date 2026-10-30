//! Where the controller keeps every store, token, and key: the image's and the CLI-emitted Compose's paths.

use std::path::{Path, PathBuf};

/// A state directory and the separate keyring directory beside it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sealed {
    /// The state directory.
    pub state: PathBuf,
    /// The keyring directory (`aes256.key`), on its own volume.
    pub key: PathBuf,
}

/// Every path the controller reads or writes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StateLayout {
    /// Team files: `<team_id>/files.sqlite3`.
    pub storage: PathBuf,
    /// The Action journal directory (`journal.sqlite3`).
    pub action_journal: PathBuf,
    /// Inference settings, memory, names, the chat limit, and the Supervisor key pin (`supervisor.pem`).
    pub inference: PathBuf,
    /// Chat continuations.
    pub chat_continuations: Sealed,
    /// Each Team's durable Browser task ledger (`<team_id>/ledger.sqlite3`), on the chat-continuation state volume.
    pub browser_runs: PathBuf,
    /// Each Team's durable chat turns (`<team_id>.json`), on the chat-continuation state volume.
    pub chat_turns: PathBuf,
    /// Stored Inputs.
    pub stored_inputs: Sealed,
    /// Integration credentials.
    pub integrations: Sealed,
    /// Routines and their diagnostics (`diagnostics/`; key `diagnostics.key`).
    pub routines: Sealed,
    /// Sign-ins.
    pub sign_ins: Sealed,
    /// The Assistant egress policy directory the egress proxy reads.
    pub assistant_egress: PathBuf,
    /// Publication bindings, updates, residues, and icons.
    pub publications: PathBuf,
    /// cosign's TUF cache (`TUF_ROOT`).
    pub cosign: PathBuf,
    /// The hash-chained audit log.
    pub audit_log: PathBuf,
    /// The controller bearer Team issues on every start.
    pub controller_token: PathBuf,
    /// The Brain runtime bearer.
    pub brain_token: PathBuf,
    /// The published Supervisor public key.
    pub supervisor_key: PathBuf,
    /// The published Routine public key.
    pub routine_key: PathBuf,
    /// The Account egress capability.
    pub account_egress_capability: PathBuf,
}

impl StateLayout {
    /// The Local image's layout.
    #[must_use]
    pub fn local() -> Self {
        Self::under(Path::new("/"))
    }

    /// The same layout below `root` (a test's temporary directory).
    #[must_use]
    pub fn under(root: &Path) -> Self {
        let state = root.join("var/lib/shimpz-local");
        let sealed = |name: &str| Sealed {
            state: state.join(name).join("state"),
            key: state.join(name).join("key"),
        };
        Self {
            storage: state.join("storage"),
            action_journal: state.join("action-journal"),
            inference: state.join("inference"),
            chat_continuations: sealed("chat-continuations"),
            browser_runs: state.join("chat-continuations/state/browser-runs"),
            chat_turns: state.join("chat-continuations/state/turns"),
            stored_inputs: sealed("assistant-stored-inputs"),
            integrations: sealed("assistant-integrations"),
            routines: sealed("routines"),
            sign_ins: sealed("browser-sign-ins"),
            assistant_egress: state.join("assistant-egress"),
            publications: state.join("publications"),
            cosign: state.join("cosign"),
            audit_log: root.join("var/log/shimpz-local/audit.jsonl"),
            controller_token: root.join("run/shimpz-local/token"),
            brain_token: root.join("run/shimpz-brain-runtime/token"),
            supervisor_key: root.join("run/shimpz-local-supervisor/public.pem"),
            routine_key: root.join("run/shimpz-local-supervisor/routine.pem"),
            account_egress_capability: root.join("run/shimpz-account-egress/token"),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::StateLayout;

    #[test]
    fn the_local_layout_is_the_image_s() {
        let layout = StateLayout::local();
        assert_eq!(
            layout.routines.key,
            Path::new("/var/lib/shimpz-local/routines/key")
        );
        assert_eq!(
            layout.sign_ins.state,
            Path::new("/var/lib/shimpz-local/browser-sign-ins/state")
        );
        assert_eq!(
            layout.audit_log,
            Path::new("/var/log/shimpz-local/audit.jsonl")
        );
        assert_eq!(
            layout.controller_token,
            Path::new("/run/shimpz-local/token")
        );
        assert_eq!(
            StateLayout::under(Path::new("/tmp/x")).cosign,
            Path::new("/tmp/x/var/lib/shimpz-local/cosign")
        );
    }
}
