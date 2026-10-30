//! A reader of the tar stream `docker export` writes (ustar with PAX or GNU long names): each entry's path, type,
//! mode, owner, and the head of its content.

use std::io::Read;

use crate::report::{Failure, Outcome, fail};

const BLOCK: usize = 512;
/// How much of each regular file's content is kept.
const HEAD: usize = 64;
/// How much of an extended header's records is kept.
const MAX_RECORDS: usize = 64 * 1024;

/// One archive entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// The path, without a leading `./` or `/`, and without a trailing `/` for a directory.
    pub path: String,
    /// The type flag (`0` file, `2` symlink, `5` directory, …).
    pub kind: u8,
    /// The permission bits, set-id and sticky bits included.
    pub mode: u32,
    /// The owner.
    pub uid: u32,
    /// The group.
    pub gid: u32,
    /// The first bytes of a regular file's content.
    pub head: Vec<u8>,
}

impl Entry {
    /// Whether it is a regular file.
    pub const fn is_file(&self) -> bool {
        matches!(self.kind, b'0' | 0)
    }

    /// Whether it is a regular file whose content is an ELF object.
    pub fn is_elf(&self) -> bool {
        self.is_file() && self.head.starts_with(b"\x7fELF")
    }
}

fn octal(field: &[u8]) -> Outcome<u64> {
    let text: String = field
        .iter()
        .take_while(|byte| **byte != 0)
        .map(|byte| char::from(*byte))
        .collect();
    let text = text.trim();
    if text.is_empty() {
        return Ok(0);
    }
    u64::from_str_radix(text, 8)
        .map_err(|_| Failure(format!("a tar number is invalid: {text:.24}")))
}

fn text(field: &[u8]) -> String {
    String::from_utf8_lossy(field.split(|byte| *byte == 0).next().unwrap_or_default()).into_owned()
}

/// The `path` record of a PAX extended header, if any.
fn pax_path(records: &[u8]) -> Option<String> {
    let mut rest = records;
    while let Some(space) = rest.iter().position(|byte| *byte == b' ') {
        let length: usize = std::str::from_utf8(&rest[..space]).ok()?.parse().ok()?;
        let record = rest.get(space + 1..length)?;
        if let Some(path) = record.strip_prefix(b"path=") {
            return Some(
                String::from_utf8_lossy(path.strip_suffix(b"\n").unwrap_or(path)).into_owned(),
            );
        }
        rest = rest.get(length..)?;
    }
    None
}

fn normalized(path: &str) -> String {
    path.trim_start_matches("./")
        .trim_start_matches('/')
        .trim_end_matches('/')
        .to_owned()
}

/// The next `size` bytes of `archive`, padding included: the first [`HEAD`] (or all of a header's records) kept.
fn content(archive: &mut impl Read, size: u64, keep: usize) -> Outcome<Vec<u8>> {
    let padded = size.div_ceil(BLOCK as u64) * BLOCK as u64;
    let kept = usize::try_from(size).unwrap_or(usize::MAX).min(keep);
    let mut head = vec![0_u8; kept];
    archive.read_exact(&mut head)?;
    let skipped = std::io::copy(
        &mut archive.take(padded - kept as u64),
        &mut std::io::sink(),
    )?;
    if skipped == padded - kept as u64 {
        Ok(head)
    } else {
        fail("a tar entry is truncated")
    }
}

/// Every entry of a complete archive read from `archive`.
pub fn entries(mut archive: impl Read) -> Outcome<Vec<Entry>> {
    let mut entries = Vec::new();
    let mut long_path: Option<String> = None;
    let mut header = [0_u8; BLOCK];
    loop {
        if archive.read_exact(&mut header).is_err() {
            return fail("the tar stream ended without its end blocks");
        }
        if header.iter().all(|byte| *byte == 0) {
            return Ok(entries);
        }
        let size = octal(&header[124..136])?;
        let kind = header[156];
        let keep = if matches!(kind, b'x' | b'L') {
            MAX_RECORDS
        } else {
            HEAD
        };
        let head = content(&mut archive, size, keep)?;
        match kind {
            b'x' => long_path = pax_path(&head).or(long_path),
            b'L' => long_path = Some(text(&head)),
            b'g' => {}
            kind => {
                let prefix = text(&header[345..500]);
                let name = text(&header[..100]);
                let path = long_path.take().unwrap_or_else(|| {
                    if prefix.is_empty() {
                        name
                    } else {
                        format!("{prefix}/{name}")
                    }
                });
                entries.push(Entry {
                    path: normalized(&path),
                    kind,
                    mode: u32::try_from(octal(&header[100..108])? & 0o7777).unwrap_or_default(),
                    uid: u32::try_from(octal(&header[108..116])?).unwrap_or(u32::MAX),
                    gid: u32::try_from(octal(&header[116..124])?).unwrap_or(u32::MAX),
                    head,
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::entries;

    fn header(name: &str, kind: u8, mode: u32, size: usize) -> Vec<u8> {
        let mut block = vec![0_u8; 512];
        block[..name.len()].copy_from_slice(name.as_bytes());
        block[100..107].copy_from_slice(format!("{mode:07o}").as_bytes());
        block[108..115].copy_from_slice(b"0023421");
        block[116..123].copy_from_slice(b"0023421");
        block[124..135].copy_from_slice(format!("{size:011o}").as_bytes());
        block[156] = kind;
        block
    }

    fn padded(content: &[u8]) -> Vec<u8> {
        let mut block = content.to_vec();
        block.resize(content.len().div_ceil(512) * 512, 0);
        block
    }

    #[test]
    fn entries_carry_their_metadata_head_and_long_names() {
        let pax = b"31 path=usr/local/bin/long-one\n";
        let mut archive = header("PaxHeaders/x", b'x', 0o644, pax.len());
        archive.extend(padded(pax));
        archive.extend(header("short", b'0', 0o555, 5));
        archive.extend(padded(b"\x7fELF!"));
        archive.extend(header("./var/lib/x/", b'5', 0o2750, 0));
        archive.extend(vec![0_u8; 1024]);
        let read = entries(archive.as_slice()).expect("entries");
        assert_eq!(read.len(), 2);
        assert_eq!(read[0].path, "usr/local/bin/long-one");
        assert!(read[0].is_elf());
        assert_eq!(
            (read[0].mode, read[0].uid, read[0].gid),
            (0o555, 10001, 10001)
        );
        assert_eq!(
            (read[1].path.as_str(), read[1].kind, read[1].mode),
            ("var/lib/x", b'5', 0o2750)
        );
        assert!(entries(&archive[..1024]).is_err());
        assert!(entries(&archive[..1536]).is_err());
    }
}
