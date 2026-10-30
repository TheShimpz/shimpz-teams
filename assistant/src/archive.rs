//! One fixed read-only regular file of an immutable Assistant image, as the Docker archive endpoint returns it.
//!
//! The daemon answers a path with its stat metadata and a tar stream. The file is admitted only when the metadata
//! names exactly that file with mode `0444` and a size within the bound, and the archive holds exactly one regular
//! member of that name, mode, and size. The reader that fetched the archive must have stopped at
//! [`archive_bound`] bytes.

use strict_json::Value;

use crate::ManifestError;

const BLOCK: usize = 512;
const READ_ONLY: u64 = 0o444;

/// The most archive bytes a reader takes for a file of at most `maximum` bytes: the file plus its tar framing.
#[must_use]
pub const fn archive_bound(maximum: usize) -> usize {
    maximum + 32 * 1024
}

/// The file's bytes, when the stat `metadata` and the `archive` both prove one read-only regular file `name` of at
/// most `maximum` bytes.
///
/// # Errors
/// [`ManifestError`] for any other metadata or archive.
pub fn container_file(
    metadata: &Value,
    archive: &[u8],
    name: &str,
    maximum: usize,
) -> Result<Vec<u8>, ManifestError> {
    let size = match (
        metadata.get("name"),
        metadata.get("size"),
        metadata.get("mode"),
    ) {
        (
            Some(Value::String(found)),
            Some(Value::Number(strict_json::Number::Int(size))),
            Some(Value::Number(strict_json::Number::Int(mode))),
        ) if found == name
            && *mode == 0o444
            && usize::try_from(*size).is_ok_and(|size| (1..=maximum).contains(&size)) =>
        {
            usize::try_from(*size).unwrap_or(0)
        }
        _ => return Err(ManifestError::Invalid("package metadata")),
    };
    if archive.len() > archive_bound(maximum) {
        return Err(ManifestError::Invalid("package archive size"));
    }
    let members = members(archive).ok_or(ManifestError::Invalid("package archive"))?;
    match members.as_slice() {
        [member]
            if (member.name == name || member.name == format!("./{name}"))
                && member.regular
                && member.mode & 0o777 == READ_ONLY
                && member.data.len() == size =>
        {
            Ok(member.data.to_vec())
        }
        _ => Err(ManifestError::Invalid("package archive")),
    }
}

/// One tar member with its extended-header path and size applied.
struct Member<'a> {
    name: String,
    regular: bool,
    mode: u64,
    data: &'a [u8],
}

/// Pending extended-header overrides for the next member.
#[derive(Default)]
struct Overrides {
    path: Option<String>,
    size: Option<usize>,
}

/// Every member of a ustar, GNU, or PAX archive up to its end marker, or `None` for a malformed archive.
fn members(archive: &[u8]) -> Option<Vec<Member<'_>>> {
    let mut found = Vec::new();
    let mut offset = 0;
    let mut pending = Overrides::default();
    let mut global = Overrides::default();
    loop {
        let header = archive.get(offset..offset + BLOCK)?;
        if header.iter().all(|byte| *byte == 0) {
            return Some(found);
        }
        if !checksum_valid(header) {
            return None;
        }
        let kind = header[156];
        let size = pending
            .size
            .or(global.size)
            .map_or_else(|| octal(&header[124..136]), Some)?;
        let start = offset + BLOCK;
        let data = archive.get(start..start.checked_add(size)?)?;
        offset = start + size.div_ceil(BLOCK) * BLOCK;
        match kind {
            b'x' => pending = pax(data, Overrides::default())?,
            b'g' => global = pax(data, std::mem::take(&mut global))?,
            b'L' => pending.path = Some(text(data)?),
            _ => {
                let name = pending
                    .path
                    .take()
                    .or_else(|| global.path.clone())
                    .map_or_else(|| ustar_name(header), Some)?;
                pending = Overrides::default();
                found.push(Member {
                    name,
                    regular: matches!(kind, b'0' | 0),
                    mode: octal(&header[100..108])? as u64,
                    data,
                });
            }
        }
    }
}

/// The null-terminated text of a field.
fn text(field: &[u8]) -> Option<String> {
    let end = field
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(field.len());
    String::from_utf8(field[..end].to_vec()).ok()
}

fn ustar_name(header: &[u8]) -> Option<String> {
    let name = text(&header[0..100])?;
    let prefix = if &header[257..263] == b"ustar\0" || &header[257..262] == b"ustar" {
        text(&header[345..500])?
    } else {
        String::new()
    };
    Some(if prefix.is_empty() {
        name
    } else {
        format!("{prefix}/{name}")
    })
}

/// An octal number field, or a base-256 one (its first byte's high bit set).
fn octal(field: &[u8]) -> Option<usize> {
    if field.first().is_some_and(|byte| byte & 0x80 != 0) {
        return field[1..]
            .iter()
            .try_fold(usize::from(field[0] & 0x7f), |total, byte| {
                total.checked_mul(256)?.checked_add(usize::from(*byte))
            });
    }
    let digits: Vec<u8> = field
        .iter()
        .copied()
        .take_while(|byte| *byte != 0)
        .collect();
    let trimmed = std::str::from_utf8(&digits).ok()?.trim_matches(' ');
    if trimmed.is_empty() {
        return Some(0);
    }
    usize::from_str_radix(trimmed, 8).ok()
}

fn checksum_valid(header: &[u8]) -> bool {
    let Some(recorded) = octal(&header[148..156]) else {
        return false;
    };
    let unsigned: usize = header
        .iter()
        .enumerate()
        .map(|(index, byte)| {
            if (148..156).contains(&index) {
                usize::from(b' ')
            } else {
                usize::from(*byte)
            }
        })
        .sum();
    let signed: i64 = header
        .iter()
        .enumerate()
        .map(|(index, byte)| {
            if (148..156).contains(&index) {
                i64::from(b' ')
            } else {
                i64::from(byte.cast_signed())
            }
        })
        .sum();
    recorded == unsigned || i64::try_from(recorded).is_ok_and(|recorded| recorded == signed)
}

/// PAX records `"<length> <key>=<value>\n"`; only `path` and `size` change what this reader admits.
fn pax(data: &[u8], mut overrides: Overrides) -> Option<Overrides> {
    let mut rest = data;
    while !rest.is_empty() {
        let space = rest.iter().position(|byte| *byte == b' ')?;
        let length: usize = std::str::from_utf8(&rest[..space]).ok()?.parse().ok()?;
        let record = rest.get(space + 1..length)?;
        let record = record.strip_suffix(b"\n")?;
        let equals = record.iter().position(|byte| *byte == b'=')?;
        let (key, value) = (&record[..equals], &record[equals + 1..]);
        match key {
            b"path" => overrides.path = Some(String::from_utf8(value.to_vec()).ok()?),
            b"size" => overrides.size = Some(std::str::from_utf8(value).ok()?.parse().ok()?),
            _ => {}
        }
        rest = &rest[length..];
    }
    Some(overrides)
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::container_file;

    /// A one-member ustar archive, the way the daemon builds one.
    pub(crate) fn tar(name: &str, data: &[u8], mode: u32, kind: u8) -> Vec<u8> {
        let mut header = [0_u8; 512];
        header[..name.len()].copy_from_slice(name.as_bytes());
        header[100..108].copy_from_slice(format!("{mode:07o}\0").as_bytes());
        header[124..136].copy_from_slice(format!("{:011o}\0", data.len()).as_bytes());
        header[156] = kind;
        header[257..263].copy_from_slice(b"ustar\0");
        header[263..265].copy_from_slice(b"00");
        header[148..156].copy_from_slice(b"        ");
        let sum: u32 = header.iter().map(|byte| u32::from(*byte)).sum();
        header[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        let mut archive = header.to_vec();
        archive.extend_from_slice(data);
        archive.resize(archive.len().div_ceil(512) * 512 + 1024, 0);
        archive
    }

    fn metadata(name: &str, size: usize, mode: u32) -> Value {
        strict_json::parse(
            format!(r#"{{"name": "{name}", "size": {size}, "mode": {mode}}}"#).as_bytes(),
        )
        .expect("JSON")
    }

    #[test]
    fn one_read_only_regular_member_is_admitted() {
        let archive = tar("shimpz.toml", b"spec = 1\n", 0o444, b'0');
        let read = container_file(
            &metadata("shimpz.toml", 9, 0o444),
            &archive,
            "shimpz.toml",
            64,
        );
        assert_eq!(read.expect("admitted"), b"spec = 1\n");
        let dotted = tar("./shimpz.toml", b"x", 0o444, b'0');
        assert!(
            container_file(
                &metadata("shimpz.toml", 1, 0o444),
                &dotted,
                "shimpz.toml",
                64
            )
            .is_ok()
        );
    }

    #[test]
    fn every_other_archive_or_metadata_is_refused() {
        let good = metadata("shimpz.toml", 1, 0o444);
        for archive in [
            tar("shimpz.toml", b"x", 0o644, b'0'),
            tar("other.toml", b"x", 0o444, b'0'),
            tar("shimpz.toml", b"x", 0o444, b'2'),
            tar("shimpz.toml", b"xy", 0o444, b'0'),
            [
                tar("shimpz.toml", b"x", 0o444, b'0')[..1024].to_vec(),
                tar("shimpz.toml", b"x", 0o444, b'0'),
            ]
            .concat(),
            b"not a tar archive".to_vec(),
        ] {
            assert!(container_file(&good, &archive, "shimpz.toml", 64).is_err());
        }
        let archive = tar("shimpz.toml", b"x", 0o444, b'0');
        let mut corrupted = archive.clone();
        corrupted[0] = b'S';
        assert!(container_file(&good, &corrupted, "shimpz.toml", 64).is_err());
        for refused in [
            metadata("shimpz.toml", 1, 0o644),
            metadata("shimpz.toml", 65, 0o444),
            metadata("x", 1, 0o444),
        ] {
            assert!(container_file(&refused, &archive, "shimpz.toml", 64).is_err());
        }
        assert!(container_file(&Value::Null, &archive, "shimpz.toml", 64).is_err());
    }
}
