//! One run's names: every container, network, volume, and image the suite creates is named by the run, so cleanup
//! never touches anything else on the daemon.

use crate::api::random_hex;
use crate::report::{Outcome, fail};

/// The controller's ownership labels.
pub const MANAGED_LABEL: &str = "com.shimpz.local.managed";
/// The Local profile label.
pub const PROFILE_LABEL: &str = "com.shimpz.local.profile";
/// The Space label.
pub const SPACE_LABEL: &str = "com.shimpz.local.space-id";
/// The resource kind label.
pub const KIND_LABEL: &str = "com.shimpz.local.kind";
/// The Local profile.
pub const LOCAL_PROFILE: &str = "local-v1";

/// Every named volume of one controller and its mount point there (`None`: the egress proxy's alone), read-only when
/// marked.
pub const VOLUMES: [(&str, Option<&str>, bool); 22] = [
    ("token", Some("/run/shimpz-local"), false),
    ("runtime-token", Some("/run/shimpz-brain-runtime"), false),
    ("audit", Some("/var/log/shimpz-local"), false),
    ("storage", Some("/var/lib/shimpz-local/storage"), false),
    ("inference", Some("/var/lib/shimpz-local/inference"), false),
    (
        "action-journal",
        Some("/var/lib/shimpz-local/action-journal"),
        false,
    ),
    (
        "publication",
        Some("/var/lib/shimpz-local/publications"),
        false,
    ),
    (
        "continuation-state",
        Some("/var/lib/shimpz-local/chat-continuations/state"),
        false,
    ),
    (
        "continuation-key",
        Some("/var/lib/shimpz-local/chat-continuations/key"),
        false,
    ),
    ("cosign", Some("/var/lib/shimpz-local/cosign"), false),
    (
        "integration-state",
        Some("/var/lib/shimpz-local/assistant-integrations/state"),
        false,
    ),
    (
        "integration-key",
        Some("/var/lib/shimpz-local/assistant-integrations/key"),
        false,
    ),
    (
        "stored-input-state",
        Some("/var/lib/shimpz-local/assistant-stored-inputs/state"),
        false,
    ),
    (
        "stored-input-key",
        Some("/var/lib/shimpz-local/assistant-stored-inputs/key"),
        false,
    ),
    (
        "routine-state",
        Some("/var/lib/shimpz-local/routines/state"),
        false,
    ),
    (
        "routine-key",
        Some("/var/lib/shimpz-local/routines/key"),
        false,
    ),
    (
        "sign-in-state",
        Some("/var/lib/shimpz-local/browser-sign-ins/state"),
        false,
    ),
    (
        "sign-in-key",
        Some("/var/lib/shimpz-local/browser-sign-ins/key"),
        false,
    ),
    ("supervisor-key", Some("/run/shimpz-local-supervisor"), true),
    (
        "account-egress-capability",
        Some("/run/shimpz-account-egress"),
        true,
    ),
    (
        "egress-policy",
        Some("/var/lib/shimpz-local/assistant-egress"),
        false,
    ),
    ("egress-audit", None, false),
];

/// The images under test, from the lane.
#[derive(Clone, Debug)]
pub struct Images {
    /// The Team runtime image (`SHIMPZ_TEAM_LOCAL_TEST_IMAGE`).
    pub team: String,
    /// The Team fixture image the controller runs (`SHIMPZ_TEAM_FIXTURE_TEST_IMAGE`).
    pub fixture: String,
    /// The shared egress image (`SHIMPZ_ASSISTANT_EGRESS_TEST_IMAGE`).
    pub egress: String,
}

impl Images {
    /// The images the lane names; each must be set.
    pub fn from_environment() -> Outcome<Self> {
        let named = |name: &str| match std::env::var(name) {
            Ok(value) if !value.is_empty() => Ok(value),
            _ => fail(format!("{name} must name a locally built image")),
        };
        Ok(Self {
            team: named("SHIMPZ_TEAM_LOCAL_TEST_IMAGE")?,
            fixture: named("SHIMPZ_TEAM_FIXTURE_TEST_IMAGE")?,
            egress: named("SHIMPZ_ASSISTANT_EGRESS_TEST_IMAGE")?,
        })
    }
}

/// One run's names.
#[derive(Clone, Debug)]
pub struct Names {
    /// The run's unique suffix.
    pub unique: String,
    /// The registry container.
    pub registry: String,
    /// The controller container.
    pub controller: String,
    /// The Assistant egress proxy container.
    pub egress_proxy: String,
    /// The reference Assistant's build tag.
    pub assistant_tag: String,
    /// The Space id.
    pub space_id: String,
    /// A network that carries the Space's labels but no kind label.
    pub foreign_network: String,
    /// The egress proxy's outbound network.
    pub outbound_network: String,
}

impl Names {
    /// Fresh names for one run.
    pub fn fresh() -> Outcome<Self> {
        let unique = random_hex(6)?;
        Ok(Self {
            registry: format!("shimpz-registry-{unique}"),
            controller: format!("shimpz-controller-{unique}"),
            egress_proxy: format!("shimpz-egress-proxy-{unique}"),
            assistant_tag: format!("shimpz-cloudflare-test:{unique}"),
            space_id: format!("test-space-{unique}"),
            foreign_network: format!("shimpz-foreign-{unique}"),
            outbound_network: format!("shimpz-egress-outbound-{unique}"),
            unique,
        })
    }

    /// The run's name of the volume `stem`.
    pub fn volume(&self, stem: &str) -> String {
        format!("shimpz-local-{stem}-{}", self.unique)
    }

    /// Every volume the run creates.
    pub fn volumes(&self) -> Vec<String> {
        VOLUMES
            .iter()
            .map(|(stem, _, _)| self.volume(stem))
            .collect()
    }

    /// The ownership label filters of `kind` in this run's Space.
    pub fn ownership(&self, kind: &str) -> [(String, String); 4] {
        [
            (MANAGED_LABEL.to_owned(), "1".to_owned()),
            (PROFILE_LABEL.to_owned(), LOCAL_PROFILE.to_owned()),
            (SPACE_LABEL.to_owned(), self.space_id.clone()),
            (KIND_LABEL.to_owned(), kind.to_owned()),
        ]
    }
}

/// Python's `half_cpu_set`: the first half of the daemon's processors, as the controller confines its workloads.
pub fn half_cpu_set(processors: u32) -> String {
    let available = (processors / 2).max(1);
    if available == 1 {
        "0".to_owned()
    } else {
        format!("0-{}", available - 1)
    }
}

#[cfg(test)]
mod tests {
    use super::{Names, half_cpu_set};

    #[test]
    fn names_are_unique_per_run() {
        let first = Names::fresh().expect("names");
        let second = Names::fresh().expect("names");
        assert_ne!(first.unique, second.unique);
        assert_eq!(first.volumes().len(), 22);
        assert!(
            first
                .volumes()
                .iter()
                .all(|name| name.ends_with(&first.unique))
        );
        assert_eq!(half_cpu_set(1), "0");
        assert_eq!(half_cpu_set(3), "0");
        assert_eq!(half_cpu_set(96), "0-47");
    }
}
