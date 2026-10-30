//! The Account egress capability (`integrations/account_egress.py`): read with custody on every broker call.
//!
//! Its writer of record is the non-root Account egress initializer (uid 10023); Team reads it only through the
//! capability group. A file outside that exact custody, of any other size, changed while read, or not 64 lowercase
//! hexadecimal characters is refused.

use std::path::Path;

use team_core::secret::Secret;
use team_storage::custody::{self, FileCustody, Mode};

/// Where the Compose wiring mounts the capability.
pub const CAPABILITY_PATH: &str = "/run/shimpz-account-egress/token";
/// The group Team reads it through.
pub const CAPABILITY_GROUP: &str = "shimpzaccount-egress-token";
/// The initializer that writes it.
pub const CAPABILITY_OWNER_UID: u32 = 10023;

/// The capability is unavailable or its custody is unsafe; never carries the path or the content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CapabilityUnavailable;

impl std::fmt::Display for CapabilityUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the Account egress capability is unavailable")
    }
}

impl std::error::Error for CapabilityUnavailable {}

/// Where and in whose custody the capability is read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilitySource {
    /// The file.
    pub path: std::path::PathBuf,
    /// Its required owner.
    pub owner: u32,
    /// Its required group, resolved at each read.
    pub group: GroupSource,
}

/// How the capability group's id is found.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GroupSource {
    /// By its name in the image's `/etc/group`, as `getgrnam` would.
    Named(&'static str),
    /// A fixed id (a test's own group).
    Id(u32),
}

impl CapabilitySource {
    /// The Local image's capability.
    #[must_use]
    pub fn local() -> Self {
        Self {
            path: Path::new(CAPABILITY_PATH).to_path_buf(),
            owner: CAPABILITY_OWNER_UID,
            group: GroupSource::Named(CAPABILITY_GROUP),
        }
    }

    /// Reads the exact capability, as every outbound broker operation does.
    ///
    /// # Errors
    /// [`CapabilityUnavailable`] for an unknown group, a file outside custody (owner, group, `0440`, one link, 64
    /// bytes), a change while reading, or content other than 64 lowercase hexadecimal characters.
    pub fn read(&self) -> Result<Secret, CapabilityUnavailable> {
        let gid = match self.group {
            GroupSource::Named(name) => custody::group_id(name).ok_or(CapabilityUnavailable)?,
            GroupSource::Id(id) => id,
        };
        let custody = FileCustody {
            uid: Some(self.owner),
            gid: Some(gid),
            mode: Mode::Exact(0o440),
            size: Some((64, 64)),
        };
        let content = custody::read(&self.path, &custody, 64)
            .map_err(|_| CapabilityUnavailable)?
            .ok_or(CapabilityUnavailable)?;
        let content = zeroize::Zeroizing::new(content);
        if content.len() != 64
            || !content
                .iter()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        {
            return Err(CapabilityUnavailable);
        }
        Ok(Secret::new(
            String::from_utf8(content.to_vec()).map_err(|_| CapabilityUnavailable)?,
        ))
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};

    use super::{CapabilitySource, CapabilityUnavailable, GroupSource};

    fn source(directory: &std::path::Path) -> CapabilitySource {
        let metadata = fs::metadata(directory).expect("stat");
        CapabilitySource {
            path: directory.join("token"),
            owner: metadata.uid(),
            group: GroupSource::Id(metadata.gid()),
        }
    }

    fn install(path: &std::path::Path, content: &[u8], mode: u32) {
        fs::write(path, content).expect("written");
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("mode");
    }

    #[test]
    fn account_capability_custody_uid_10023_0440_identity_stable() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let source = source(directory.path());
        assert_eq!(source.read(), Err(CapabilityUnavailable));
        install(&source.path, &[b'a'; 64], 0o440);
        assert_eq!(source.read().expect("read").expose(), "a".repeat(64));

        // Any other mode, owner, or group is outside custody.
        fs::set_permissions(&source.path, fs::Permissions::from_mode(0o400)).expect("mode");
        assert_eq!(source.read(), Err(CapabilityUnavailable));
        fs::set_permissions(&source.path, fs::Permissions::from_mode(0o440)).expect("mode");
        let foreign = CapabilitySource {
            owner: source.owner + 1,
            ..source.clone()
        };
        assert_eq!(foreign.read(), Err(CapabilityUnavailable));
        let local = CapabilitySource::local();
        assert_eq!(local.owner, 10023);
        assert_eq!(
            local.group,
            GroupSource::Named("shimpzaccount-egress-token")
        );

        // Wrong length, encoding, or alphabet; and a link.
        for content in [
            vec![b'a'; 63],
            vec![0xff; 64],
            vec![b'g'; 64],
            vec![b'A'; 64],
        ] {
            fs::remove_file(&source.path).expect("removed");
            install(&source.path, &content, 0o440);
            assert_eq!(source.read(), Err(CapabilityUnavailable));
        }
        fs::remove_file(&source.path).expect("removed");
        let target = directory.path().join("target");
        install(&target, &[b'a'; 64], 0o440);
        symlink(&target, &source.path).expect("linked");
        assert_eq!(source.read(), Err(CapabilityUnavailable));
    }

    #[test]
    fn an_unknown_group_refuses() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let source = CapabilitySource {
            group: GroupSource::Named("no-such-shimpz-group"),
            ..source(directory.path())
        };
        install(&source.path, &[b'a'; 64], 0o440);
        assert_eq!(source.read(), Err(CapabilityUnavailable));
    }
}
