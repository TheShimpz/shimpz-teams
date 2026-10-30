//! The ownership labels every Docker object Team creates carries, and the deterministic names of its Team networks
//! and Assistant containers. An object is Team's only when it carries every label of its kind, exactly.

use std::collections::HashMap;
use std::fmt::Write as _;
use std::hash::BuildHasher;

use sha2::{Digest, Sha256};

/// Present (`1`) on every object a Local controller manages.
pub const MANAGED: &str = "com.shimpz.local.managed";
/// The ownership profile.
pub const PROFILE: &str = "com.shimpz.local.profile";
/// The owning Space.
pub const SPACE: &str = "com.shimpz.local.space-id";
/// The object's kind: `team`, `assistant`, `prepare`, or `browser`.
pub const KIND: &str = "com.shimpz.local.kind";
/// The owning Team.
pub const TEAM: &str = "com.shimpz.local.team-id";
/// A Team network's creation name.
pub const TEAM_NAME: &str = "com.shimpz.local.team-name";
/// An Assistant container's Assistant.
pub const ASSISTANT: &str = "com.shimpz.local.assistant-id";
/// The image an Assistant container was created from.
pub const IMAGE: &str = "com.shimpz.local.image";

/// The ownership profile of every object this controller creates.
pub const PROFILE_VALUE: &str = "local-v1";

/// The longest Space id.
const MAX_SPACE_ID_CHARS: usize = 48;

/// One Space: its id (`SHIMPZ_SPACE_ID`) and the name prefix derived from it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Space {
    id: String,
    prefix: String,
}

impl Space {
    /// The Space `id`: at most 48 lowercase letters and digits in dash-separated words.
    #[must_use]
    pub fn new(id: &str) -> Option<Self> {
        let words_hold = id.split('-').all(|word| {
            !word.is_empty()
                && word
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        });
        if id.is_empty() || id.len() > MAX_SPACE_ID_CHARS || !words_hold {
            return None;
        }
        let digest = Sha256::digest(id.as_bytes());
        let mut prefix = String::with_capacity(12);
        for byte in &digest[..6] {
            // Writing to a String cannot fail.
            let _ = write!(prefix, "{byte:02x}");
        }
        Some(Self {
            id: id.to_owned(),
            prefix,
        })
    }

    /// The Space id.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The first 12 hex digits of the id's SHA-256, which every Space-scoped name carries.
    #[must_use]
    pub fn prefix(&self) -> &str {
        &self.prefix
    }

    /// The exact labels of one `kind` of object this Space owns, optionally for one Team.
    #[must_use]
    pub fn labels(&self, kind: &str, team_id: Option<&str>) -> Vec<(&'static str, String)> {
        let mut labels = vec![
            (MANAGED, "1".to_owned()),
            (PROFILE, PROFILE_VALUE.to_owned()),
            (SPACE, self.id.clone()),
            (KIND, kind.to_owned()),
        ];
        if let Some(team_id) = team_id {
            labels.push((TEAM, team_id.to_owned()));
        }
        labels
    }

    /// A Team's network: `shimpz-local-<prefix>-team-<team>`.
    #[must_use]
    pub fn network_name(&self, team_id: &str) -> String {
        format!("shimpz-local-{}-team-{team_id}", self.prefix)
    }

    /// An Assistant's container: `shimpz-local-<prefix>-<team>-assistant-<assistant>`.
    #[must_use]
    pub fn container_name(&self, team_id: &str, assistant_id: &str) -> String {
        format!(
            "shimpz-local-{}-{team_id}-assistant-{assistant_id}",
            self.prefix
        )
    }
}

/// `labels` as Docker's label map.
#[must_use]
pub fn label_map(labels: &[(&'static str, String)]) -> HashMap<String, String> {
    labels
        .iter()
        .map(|(name, value)| ((*name).to_owned(), value.clone()))
        .collect()
}

/// `labels` as a label filter.
#[must_use]
pub fn filter<'a>(labels: &'a [(&'static str, String)]) -> Vec<(&'a str, &'a str)> {
    labels
        .iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect()
}

/// Whether `actual` carries every one of `expected`, exactly.
#[must_use]
pub fn include<S: BuildHasher>(
    actual: Option<&HashMap<String, String, S>>,
    expected: &[(&'static str, String)],
) -> bool {
    actual.is_some_and(|actual| {
        expected
            .iter()
            .all(|(name, value)| actual.get(*name) == Some(value))
    })
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::{Space, include, label_map};

    #[test]
    fn a_space_id_is_strict_and_names_are_deterministic() {
        for invalid in ["", "INVALID", "-a", "a-", "a--b", "a_b", &"a".repeat(49)] {
            assert_eq!(Space::new(invalid), None, "{invalid:?}");
        }
        let space = Space::new("local-space").expect("a Space id");
        // SHA-256("local-space") starts with these 12 hex digits.
        let prefix = space.prefix().to_owned();
        assert_eq!(prefix, "6a4581b4e455");
        assert!(Space::new(&"a".repeat(48)).is_some());
        assert_eq!(
            space.network_name("team_1"),
            format!("shimpz-local-{prefix}-team-team_1")
        );
        assert_eq!(
            space.container_name("team_1", "notes"),
            format!("shimpz-local-{prefix}-team_1-assistant-notes")
        );
    }

    #[test]
    fn ownership_needs_every_label_exactly() {
        let space = Space::new("local-space").expect("a Space id");
        let expected = space.labels("assistant", Some("team_1"));
        let mut actual = label_map(&expected);
        actual.insert("other".to_owned(), "kept".to_owned());
        assert!(include(Some(&actual), &expected));
        actual.insert(super::SPACE.to_owned(), "other-space".to_owned());
        assert!(!include(Some(&actual), &expected));
        let mut partial = label_map(&expected);
        partial.remove(super::TEAM);
        assert!(!include(Some(&partial), &expected));
        assert!(!include::<std::hash::RandomState>(None, &expected));
        assert!(include(Some(&HashMap::new()), &[]));
    }
}
