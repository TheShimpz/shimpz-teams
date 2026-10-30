//! The controller bearer: issued afresh on every start, so a copied bearer lives only as long as one controller
//! process. Admin reads the file again when Team answers 401.

use std::os::unix::fs::DirBuilderExt;
use std::path::Path;

use team_core::entropy::{Entropy, token_hex};
use team_core::secret::Secret;
use team_storage::custody::{self, FileCustody, Mode};
use team_storage::durable;

/// The group Admin reads the bearer through.
pub const TOKEN_GROUP: &str = "shimpzteam-token";
/// The bearer's random bytes; the file holds their 64 lowercase hex digits.
pub const TOKEN_BYTES: usize = 32;
const TOKEN_LENGTH: u64 = TOKEN_BYTES as u64 * 2;

/// The bearer could not be issued; the controller must not serve.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TokenUnavailable;

impl std::fmt::Display for TokenUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the local controller token could not be issued")
    }
}

impl std::error::Error for TokenUnavailable {}

/// Atomically replaces the bearer at `path` with a fresh one, readable (`0440`) by this process and the Admin group
/// `group`, and returns it; any earlier bearer stops working.
///
/// The new file is created exclusively beside the current one in its final group and mode, written, fsynced, renamed
/// over it, and the directory fsynced, so Admin reads the complete earlier bearer or the complete new one. The issued
/// file is read back under its exact custody.
///
/// # Errors
/// [`TokenUnavailable`] when any step fails or the file read back is not the issued bearer in custody.
pub fn issue(path: &Path, group: u32, entropy: &dyn Entropy) -> Result<Secret, TokenUnavailable> {
    let parent = path.parent().ok_or(TokenUnavailable)?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o750)
        .create(parent)
        .map_err(|_| TokenUnavailable)?;
    let token = Secret::new(token_hex(entropy, TOKEN_BYTES).map_err(|_| TokenUnavailable)?);
    durable::replace(path, token.expose().as_bytes(), 0o440, Some(group))
        .map_err(|_| TokenUnavailable)?;
    let issued = FileCustody {
        uid: Some(custody::current_uid()),
        gid: Some(group),
        mode: Mode::Exact(0o440),
        size: Some((TOKEN_LENGTH, TOKEN_LENGTH)),
    };
    let read = custody::read(path, &issued, TOKEN_LENGTH)
        .ok()
        .flatten()
        .ok_or(TokenUnavailable)?;
    if read != token.expose().as_bytes() {
        return Err(TokenUnavailable);
    }
    Ok(token)
}

/// The bearer the CLI and the healthcheck present: the 64 characters of the token file, else `None`.
#[must_use]
pub fn read(path: &Path) -> Option<Secret> {
    let mut content = Vec::new();
    let file = std::fs::File::open(path).ok()?;
    std::io::Read::read_to_end(
        &mut std::io::Read::take(file, TOKEN_LENGTH + 1),
        &mut content,
    )
    .ok()?;
    let text = String::from_utf8(content).ok()?;
    (text.len() == TOKEN_BYTES * 2 && text.is_ascii()).then(|| Secret::new(text))
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    use team_core::entropy::OsEntropy;

    use super::{issue, read};

    fn primary_group() -> u32 {
        rustix::process::getegid().as_raw()
    }

    #[test]
    fn controller_bearer_reissued_each_start_0440_gid_10010() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("run/shimpz-local/token");
        let first = issue(&path, primary_group(), &OsEntropy).expect("issued");
        let metadata = std::fs::metadata(&path).expect("token");
        assert_eq!(metadata.permissions().mode() & 0o7777, 0o440);
        assert_eq!(metadata.gid(), primary_group());
        assert_eq!(metadata.nlink(), 1);
        assert_eq!(first.expose().len(), 64);
        assert!(first.expose().bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_eq!(read(&path), Some(first.clone()));
        let second = issue(&path, primary_group(), &OsEntropy).expect("reissued");
        assert_ne!(first, second);
        assert_eq!(read(&path), Some(second));
        let leftovers: Vec<_> = std::fs::read_dir(path.parent().expect("parent"))
            .expect("listed")
            .collect();
        assert_eq!(leftovers.len(), 1);
    }

    #[test]
    fn a_bearer_in_another_group_is_refused_and_a_short_file_is_no_bearer() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("token");
        // A group this process is not a member of cannot be given to the file (root may give any group).
        if !rustix::process::geteuid().is_root() {
            assert!(issue(&path, u32::MAX - 1, &OsEntropy).is_err());
        }
        std::fs::write(&path, "abc").expect("written");
        assert_eq!(read(&path), None);
    }
}
