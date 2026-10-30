//! File custody: the exact kind, link count, owner, group, mode, and size a Team-held file or directory must have, and
//! the one strict way to read such a file.
//!
//! Every read opens the path without following a link and without blocking (a FIFO or device planted at the path never
//! stalls the reader), checks the custody of the opened descriptor rather than of the path, reads at most the stated
//! bound, and proves the descriptor's identity and size did not change while it was read. Checking the descriptor
//! closes the window between a path check and the open.

use std::fs::{File, Metadata};
use std::io::{self, Read};
use std::os::fd::AsFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use rustix::fs::{Mode as RawMode, OFlags};

/// The permission bits a custody requires.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Any permission bits.
    Any,
    /// Exactly these bits (including set-id and sticky bits, as `stat.S_IMODE` reads them).
    Exact(u32),
    /// Any owner bits, but no group or other bit.
    OwnerOnly,
}

impl Mode {
    fn holds(self, permissions: u32) -> bool {
        match self {
            Self::Any => true,
            Self::Exact(mode) => permissions == mode,
            // No group or other bit: the low six bits are clear.
            Self::OwnerOnly => permissions.trailing_zeros() >= 6,
        }
    }
}

/// What kind of entry a path or descriptor names.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A regular file.
    File,
    /// A directory.
    Directory,
    /// A symbolic link, FIFO, socket, or device.
    Other,
}

/// The metadata a custody is judged on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Facts {
    /// The entry's kind.
    pub kind: Kind,
    /// Its hard-link count.
    pub links: u64,
    /// Its owner.
    pub uid: u32,
    /// Its group.
    pub gid: u32,
    /// Its permission bits (`st_mode & 0o7777`).
    pub permissions: u32,
    /// Its size in bytes.
    pub size: u64,
}

impl From<&Metadata> for Facts {
    fn from(metadata: &Metadata) -> Self {
        let file_type = metadata.file_type();
        Self {
            kind: if file_type.is_file() {
                Kind::File
            } else if file_type.is_dir() {
                Kind::Directory
            } else {
                Kind::Other
            },
            links: metadata.nlink(),
            uid: metadata.uid(),
            gid: metadata.gid(),
            permissions: metadata.mode() & 0o7777,
            size: metadata.size(),
        }
    }
}

/// The custody of a single-link regular file. `None` deliberately leaves that field unchecked; each caller states
/// every field.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileCustody {
    /// The required owner.
    pub uid: Option<u32>,
    /// The required group.
    pub gid: Option<u32>,
    /// The required permission bits.
    pub mode: Mode,
    /// The inclusive byte range its size must lie in.
    pub size: Option<(u64, u64)>,
}

impl FileCustody {
    /// An owner-only (`0600`) file this process owns, of at most `maximum` bytes: the custody of Team's private state.
    #[must_use]
    pub fn private(maximum: u64) -> Self {
        Self {
            uid: Some(current_uid()),
            gid: None,
            mode: Mode::Exact(0o600),
            size: Some((0, maximum)),
        }
    }

    /// Whether `facts` describe a single-link regular file in exactly this custody.
    #[must_use]
    pub fn holds(&self, facts: &Facts) -> bool {
        facts.kind == Kind::File
            && facts.links == 1
            && self.uid.is_none_or(|uid| facts.uid == uid)
            && self.gid.is_none_or(|gid| facts.gid == gid)
            && self.mode.holds(facts.permissions)
            && self
                .size
                .is_none_or(|(minimum, maximum)| (minimum..=maximum).contains(&facts.size))
    }
}

/// The custody of a directory. `None` leaves that field unchecked.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DirectoryCustody {
    /// The required owner.
    pub uid: Option<u32>,
    /// The required group.
    pub gid: Option<u32>,
    /// The required permission bits.
    pub mode: Mode,
}

impl DirectoryCustody {
    /// A `0700` directory this process owns.
    #[must_use]
    pub fn private() -> Self {
        Self {
            uid: Some(current_uid()),
            gid: None,
            mode: Mode::Exact(0o700),
        }
    }

    /// Whether `facts` describe a directory in exactly this custody.
    #[must_use]
    pub fn holds(&self, facts: &Facts) -> bool {
        facts.kind == Kind::Directory
            && self.uid.is_none_or(|uid| facts.uid == uid)
            && self.gid.is_none_or(|gid| facts.gid == gid)
            && self.mode.holds(facts.permissions)
    }
}

/// This process's effective user id.
#[must_use]
pub fn current_uid() -> u32 {
    rustix::process::geteuid().as_raw()
}

/// Why a custody read refused; it never carries a path or file content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CustodyError {
    /// The entry could not be opened or read.
    Unavailable,
    /// The opened entry is not in the stated custody.
    Unsafe,
    /// The content exceeds its byte bound.
    TooLarge,
    /// The file changed while it was read.
    Changed,
}

impl std::fmt::Display for CustodyError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "the file is unavailable",
            Self::Unsafe => "the file failed its custody contract",
            Self::TooLarge => "the file exceeds its byte limit",
            Self::Changed => "the file changed while it was read",
        })
    }
}

impl std::error::Error for CustodyError {}

/// The identity of one file version: a reader that saw the same identity before may reuse what it decoded then.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileIdentity {
    device: u64,
    inode: u64,
    size: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}

impl From<&Metadata> for FileIdentity {
    fn from(metadata: &Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.size(),
            modified: (metadata.mtime(), metadata.mtime_nsec()),
            changed: (metadata.ctime(), metadata.ctime_nsec()),
        }
    }
}

/// One custody read that may reuse an earlier one.
#[derive(Debug, PartialEq, Eq)]
pub enum Snapshot {
    /// No entry exists at the path.
    Absent,
    /// The file still has the identity the caller saw before; it was not read again.
    Unchanged(FileIdentity),
    /// The file's content, read under the identity it had.
    Read(FileIdentity, Vec<u8>),
}

const READ_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::NONBLOCK)
    .union(OFlags::CLOEXEC);

/// Opens a directory without following a link at its last component.
///
/// # Errors
/// The operating system's error, including `ELOOP` for a link and `ENOTDIR` for anything but a directory.
pub fn open_directory(path: &Path) -> io::Result<File> {
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    Ok(File::from(rustix::fs::open(path, flags, RawMode::empty())?))
}

/// The facts of entry `name` of the open `directory`, without following a link; `None` when it is absent.
///
/// # Errors
/// The operating system's error for anything but absence.
pub fn facts_at(directory: &File, name: &str) -> io::Result<Option<Facts>> {
    let flags = OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    match rustix::fs::openat(directory.as_fd(), name, flags, RawMode::empty()) {
        Ok(descriptor) => Ok(Some(Facts::from(&File::from(descriptor).metadata()?))),
        Err(rustix::io::Errno::NOENT) => Ok(None),
        Err(error) => Err(error.into()),
    }
}

/// The facts of `path` itself, without following a link at its last component; `None` when it is absent.
///
/// # Errors
/// The operating system's error for anything but absence.
pub fn facts(path: &Path) -> io::Result<Option<Facts>> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(Facts::from(&metadata))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

/// The content of the file at `path` in `custody`, of at most `maximum` bytes; `None` when it is absent.
///
/// # Errors
/// [`CustodyError`] when it cannot be opened or read, is not in custody, is too large, or changed while read.
pub fn read(
    path: &Path,
    custody: &FileCustody,
    maximum: u64,
) -> Result<Option<Vec<u8>>, CustodyError> {
    match read_if_changed(path, custody, maximum, None)? {
        Snapshot::Absent => Ok(None),
        Snapshot::Read(_, content) => Ok(Some(content)),
        // Without an earlier identity nothing is unchanged.
        Snapshot::Unchanged(_) => Err(CustodyError::Changed),
    }
}

/// [`read`] that skips reading when the file still has the `previous` identity.
///
/// # Errors
/// As [`read`].
pub fn read_if_changed(
    path: &Path,
    custody: &FileCustody,
    maximum: u64,
    previous: Option<FileIdentity>,
) -> Result<Snapshot, CustodyError> {
    let opened = rustix::fs::open(path, READ_FLAGS, RawMode::empty());
    snapshot(opened, custody, maximum, previous)
}

/// [`read`] of entry `name` of the open `directory`, so the file is resolved under the directory the caller checked.
///
/// # Errors
/// As [`read`].
pub fn read_at(
    directory: &File,
    name: &str,
    custody: &FileCustody,
    maximum: u64,
) -> Result<Option<Vec<u8>>, CustodyError> {
    let opened = rustix::fs::openat(directory.as_fd(), name, READ_FLAGS, RawMode::empty());
    match snapshot(opened, custody, maximum, None)? {
        Snapshot::Absent => Ok(None),
        Snapshot::Read(_, content) => Ok(Some(content)),
        Snapshot::Unchanged(_) => Err(CustodyError::Changed),
    }
}

fn snapshot(
    opened: rustix::io::Result<std::os::fd::OwnedFd>,
    custody: &FileCustody,
    maximum: u64,
    previous: Option<FileIdentity>,
) -> Result<Snapshot, CustodyError> {
    let mut file = match opened {
        Ok(descriptor) => File::from(descriptor),
        Err(rustix::io::Errno::NOENT) => return Ok(Snapshot::Absent),
        // A link at the path refuses the open with ELOOP: it is an entry outside custody, not an unavailable one.
        Err(rustix::io::Errno::LOOP) => return Err(CustodyError::Unsafe),
        Err(_) => return Err(CustodyError::Unavailable),
    };
    let before = file.metadata().map_err(|_| CustodyError::Unavailable)?;
    if !custody.holds(&Facts::from(&before)) {
        return Err(CustodyError::Unsafe);
    }
    let identity = FileIdentity::from(&before);
    if previous == Some(identity) {
        return Ok(Snapshot::Unchanged(identity));
    }
    let content = read_bounded(&mut file, maximum)?;
    let after = file.metadata().map_err(|_| CustodyError::Unavailable)?;
    if FileIdentity::from(&after) != identity || content.len() as u64 != identity.size {
        return Err(CustodyError::Changed);
    }
    Ok(Snapshot::Read(identity, content))
}

/// Reads until end of file or one byte past `maximum`, so an oversized file is refused without reading it whole.
fn read_bounded(file: &mut File, maximum: u64) -> Result<Vec<u8>, CustodyError> {
    let mut content = Vec::new();
    file.take(maximum.saturating_add(1))
        .read_to_end(&mut content)
        .map_err(|_| CustodyError::Unavailable)?;
    if content.len() as u64 > maximum {
        return Err(CustodyError::TooLarge);
    }
    Ok(content)
}

/// The id of the group named `name` in the image's `/etc/group`, as `getgrnam` would find it there; `None` when no
/// single well-formed entry names it.
#[must_use]
pub fn group_id(name: &str) -> Option<u32> {
    group_id_in(Path::new("/etc/group"), name)
}

fn group_id_in(path: &Path, name: &str) -> Option<u32> {
    let custody = FileCustody {
        uid: None,
        gid: None,
        mode: Mode::Any,
        size: Some((0, 64 * 1024)),
    };
    let content = read(path, &custody, 64 * 1024).ok()??;
    let text = std::str::from_utf8(&content).ok()?;
    let mut found = None;
    for line in text.lines() {
        let mut fields = line.split(':');
        if fields.next() != Some(name) {
            continue;
        }
        let id = fields.nth(1)?.parse().ok()?;
        if found.replace(id).is_some() {
            return None;
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};

    use super::{
        CustodyError, DirectoryCustody, Facts, FileCustody, Kind, Mode, Snapshot, current_uid,
        facts_at, group_id_in, open_directory, read, read_at, read_if_changed,
    };

    fn facts(kind: Kind, permissions: u32) -> Facts {
        Facts {
            kind,
            links: 1,
            uid: 1,
            gid: 2,
            permissions,
            size: 5,
        }
    }

    fn custody(
        uid: Option<u32>,
        gid: Option<u32>,
        mode: Mode,
        size: Option<(u64, u64)>,
    ) -> FileCustody {
        FileCustody {
            uid,
            gid,
            mode,
            size,
        }
    }

    fn refused_file_cases() -> Vec<(Facts, FileCustody)> {
        let holds = facts(Kind::File, 0o600);
        let full = custody(Some(1), Some(2), Mode::Exact(0o600), Some((0, 10)));
        let owner_only = custody(Some(1), Some(2), Mode::OwnerOnly, Some((0, 10)));
        vec![
            (facts(Kind::Directory, 0o600), full),
            (facts(Kind::Other, 0o600), full),
            (Facts { links: 2, ..holds }, full),
            (
                holds,
                custody(Some(3), Some(2), Mode::Exact(0o600), Some((0, 10))),
            ),
            (
                holds,
                custody(Some(1), Some(3), Mode::Exact(0o600), Some((0, 10))),
            ),
            (
                holds,
                custody(Some(1), Some(2), Mode::Exact(0o640), Some((0, 10))),
            ),
            (facts(Kind::File, 0o640), owner_only),
            (facts(Kind::File, 0o601), owner_only),
            (
                holds,
                custody(Some(1), Some(2), Mode::Exact(0o600), Some((6, 10))),
            ),
            (
                holds,
                custody(Some(1), Some(2), Mode::Exact(0o600), Some((0, 4))),
            ),
        ]
    }

    #[test]
    fn custody_predicate_type_links_uid_gid_mode_size() {
        let holds = facts(Kind::File, 0o600);
        assert!(custody(Some(1), Some(2), Mode::Exact(0o600), Some((5, 5))).holds(&holds));
        assert!(custody(None, None, Mode::Any, None).holds(&holds));
        assert!(custody(Some(1), None, Mode::OwnerOnly, None).holds(&holds));
        assert!(custody(None, None, Mode::OwnerOnly, None).holds(&facts(Kind::File, 0o4700)));
        for (metadata, required) in refused_file_cases() {
            assert!(!required.holds(&metadata), "{metadata:?} {required:?}");
        }
    }

    #[test]
    fn directory_custody_checks_kind_owner_group_and_mode() {
        let holds = facts(Kind::Directory, 0o2770);
        let full = DirectoryCustody {
            uid: Some(1),
            gid: Some(2),
            mode: Mode::Exact(0o2770),
        };
        assert!(full.holds(&holds));
        assert!(
            DirectoryCustody {
                uid: None,
                gid: None,
                mode: Mode::Any
            }
            .holds(&holds)
        );
        for (metadata, required) in [
            (facts(Kind::File, 0o2770), full),
            (
                holds,
                DirectoryCustody {
                    uid: Some(3),
                    ..full
                },
            ),
            (
                holds,
                DirectoryCustody {
                    gid: Some(3),
                    ..full
                },
            ),
            (
                holds,
                DirectoryCustody {
                    mode: Mode::Exact(0o770),
                    ..full
                },
            ),
            (
                holds,
                DirectoryCustody {
                    mode: Mode::OwnerOnly,
                    ..full
                },
            ),
        ] {
            assert!(!required.holds(&metadata), "{metadata:?} {required:?}");
        }
    }

    fn private_file(path: &std::path::Path, content: &[u8]) {
        fs::write(path, content).expect("written");
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).expect("mode set");
    }

    #[test]
    fn read_nofollow_single_link_exact_size() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("state");
        let private = FileCustody::private(16);
        assert_eq!(read(&path, &private, 16), Ok(None));

        private_file(&path, b"value");
        assert_eq!(read(&path, &private, 16), Ok(Some(b"value".to_vec())));

        // A link at the path is never followed, even to a file in custody.
        let link = root.path().join("link");
        symlink(&path, &link).expect("linked");
        assert_eq!(read(&link, &private, 16), Err(CustodyError::Unsafe));

        // A second hard link is outside custody.
        let second = root.path().join("second");
        fs::hard_link(&path, &second).expect("hard-linked");
        assert_eq!(read(&path, &private, 16), Err(CustodyError::Unsafe));
        fs::remove_file(&second).expect("removed");

        // Wrong mode, a directory, and an oversized file each refuse.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).expect("mode set");
        assert_eq!(read(&path, &private, 16), Err(CustodyError::Unsafe));
        assert_eq!(read(root.path(), &private, 16), Err(CustodyError::Unsafe));
        private_file(&path, b"0123456789abcdefX");
        assert_eq!(read(&path, &private, 16), Err(CustodyError::Unsafe));
        let unbounded = FileCustody {
            size: None,
            ..private
        };
        assert_eq!(read(&path, &unbounded, 16), Err(CustodyError::TooLarge));
    }

    #[test]
    fn a_fifo_at_the_path_never_blocks_the_reader() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("fifo");
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &path,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::from_raw_mode(0o600),
            0,
        )
        .expect("FIFO created");
        assert_eq!(
            read(&path, &FileCustody::private(16), 16),
            Err(CustodyError::Unsafe)
        );
    }

    #[test]
    fn an_unchanged_identity_is_not_read_again() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("state");
        private_file(&path, b"first");
        let private = FileCustody::private(16);
        let Ok(Snapshot::Read(identity, content)) = read_if_changed(&path, &private, 16, None)
        else {
            panic!("the file is read");
        };
        assert_eq!(content, b"first");
        assert_eq!(
            read_if_changed(&path, &private, 16, Some(identity)),
            Ok(Snapshot::Unchanged(identity))
        );
        // A replacement is a new identity.
        let next = root.path().join("next");
        private_file(&next, b"second");
        fs::rename(&next, &path).expect("replaced");
        assert!(matches!(
            read_if_changed(&path, &private, 16, Some(identity)),
            Ok(Snapshot::Read(_, content)) if content == b"second"
        ));
        assert_eq!(
            read_if_changed(&root.path().join("absent"), &private, 16, Some(identity)),
            Ok(Snapshot::Absent)
        );
    }

    #[test]
    fn a_read_at_stays_under_the_opened_directory() {
        let root = tempfile::tempdir().expect("temporary directory");
        let opened = root.path().join("opened");
        fs::create_dir(&opened).expect("created");
        let directory = open_directory(&opened).expect("opened");
        private_file(&opened.join("token"), b"abc");
        fs::rename(&opened, root.path().join("moved")).expect("moved");
        fs::create_dir(&opened).expect("recreated");
        private_file(&opened.join("token"), b"other");
        let private = FileCustody::private(16);
        assert_eq!(
            read_at(&directory, "token", &private, 16),
            Ok(Some(b"abc".to_vec()))
        );
        let found = facts_at(&directory, "token")
            .expect("read")
            .expect("present");
        assert_eq!(
            (found.kind, found.uid, found.size),
            (Kind::File, current_uid(), 3)
        );
        assert_eq!(facts_at(&directory, "absent").expect("read"), None);
        symlink("token", root.path().join("moved/link")).expect("linked");
        assert_eq!(
            facts_at(&directory, "link")
                .expect("read")
                .map(|link| link.kind),
            Some(Kind::Other)
        );
        assert_eq!(
            read_at(&directory, "link", &private, 16),
            Err(CustodyError::Unsafe)
        );
        // A link is never opened as a directory.
        symlink(&opened, root.path().join("dir-link")).expect("linked");
        assert!(open_directory(&root.path().join("dir-link")).is_err());
    }

    #[test]
    fn a_group_is_found_only_by_one_exact_entry() {
        let root = tempfile::tempdir().expect("temporary directory");
        let path = root.path().join("group");
        fs::write(
            &path,
            "root:x:0:\nshimpzteam-token:x:10010:\nduplicate:x:1:\nduplicate:x:2:\nbroken:x:none:\n",
        )
        .expect("written");
        assert_eq!(group_id_in(&path, "shimpzteam-token"), Some(10010));
        assert_eq!(group_id_in(&path, "root"), Some(0));
        assert_eq!(group_id_in(&path, "duplicate"), None);
        assert_eq!(group_id_in(&path, "broken"), None);
        assert_eq!(group_id_in(&path, "absent"), None);
        assert_eq!(group_id_in(&root.path().join("missing"), "root"), None);
    }
}
