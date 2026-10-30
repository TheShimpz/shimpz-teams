//! Strict Local admission of Developers-owned source-package v1 bytes (`local/install/source_package.py`).
//!
//! A staged image carries the exact canonical ustar archive Developers would accept: sorted entries, only regular files
//! and the directories they imply, a fixed header for each, two zero blocks, and nothing else. The bytes are walked
//! directly, then rebuilt canonically and required equal, so no extension-aware parser ever reads them.

use team_install::icons::MAX_ICON_BYTES;

const BLOCK: usize = 512;
const MAX_PACKAGE_BYTES: usize = 32 * 1024 * 1024;
const MAX_REGULAR_FILES: usize = 10_000;
const MAX_FILE_BYTES: usize = 8 * 1024 * 1024;
const MAX_RECORDS: usize = MAX_PACKAGE_BYTES / BLOCK;
const MAX_PATH_BYTES: usize = 256;
const MAX_PATH_COMPONENTS: usize = 16;
const MAX_NAME_BYTES: usize = 100;
const MAX_PREFIX_BYTES: usize = 155;
const REQUIRED_FILES: [&str; 3] = ["icon.png", "pyproject.toml", "shimpz.toml"];
const OPTIONAL_ROOTS: [&str; 2] = ["lib", "tests"];

/// The bytes are not one canonical source package, or its icon is not a canonical one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SourcePackageError;

/// The admitted display inputs of one package.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourcePackage {
    /// `sha256:` over the exact bytes.
    pub digest: String,
    /// `shimpz.toml`.
    pub manifest: Vec<u8>,
    /// `icon.png`.
    pub icon: Vec<u8>,
}

struct Record<'a> {
    path: String,
    directory: bool,
    contents: &'a [u8],
}

/// Validates exact canonical source-package v1 bytes.
///
/// # Errors
/// [`SourcePackageError`] for anything else.
pub fn admit(raw: &[u8]) -> Result<SourcePackage, SourcePackageError> {
    if !(3 * BLOCK..=MAX_PACKAGE_BYTES).contains(&raw.len())
        || !raw.len().is_multiple_of(BLOCK)
        || raw[raw.len() - 2 * BLOCK..].iter().any(|byte| *byte != 0)
    {
        return Err(SourcePackageError);
    }
    let records = read_records(raw)?;
    validate_records(&records)?;
    if build_archive(&records)? != raw {
        return Err(SourcePackageError);
    }
    let file = |name: &str| {
        records
            .iter()
            .find(|record| !record.directory && record.path == name)
            .map(|record| record.contents.to_vec())
    };
    let icon = file("icon.png").ok_or(SourcePackageError)?;
    validate_icon(&icon)?;
    Ok(SourcePackage {
        digest: format!("sha256:{}", assistant_protocol::python::sha256_hex(raw)),
        manifest: file("shimpz.toml").ok_or(SourcePackageError)?,
        icon,
    })
}

fn read_records(raw: &[u8]) -> Result<Vec<Record<'_>>, SourcePackageError> {
    let mut records = Vec::new();
    let end = raw.len() - 2 * BLOCK;
    let mut offset = 0;
    while offset < end {
        let header = &raw[offset..offset + BLOCK];
        let kind = header[156];
        let size = header_size(&header[124..136])?;
        let data = offset + BLOCK;
        let limit = if kind == b'5' { 0 } else { MAX_FILE_BYTES };
        if records.len() >= MAX_RECORDS
            || &header[257..265] != b"ustar\x0000"
            || !matches!(kind, b'0' | b'5')
            || size > limit
            || data + size > end
        {
            return Err(SourcePackageError);
        }
        records.push(Record {
            path: header_path(header),
            directory: kind == b'5',
            contents: &raw[data..data + size],
        });
        offset = data + size.div_ceil(BLOCK) * BLOCK;
    }
    if records.is_empty() {
        Err(SourcePackageError)
    } else {
        Ok(records)
    }
}

fn header_size(field: &[u8]) -> Result<usize, SourcePackageError> {
    if field[11] != 0 || !field[..11].iter().all(|byte| (b'0'..=b'7').contains(byte)) {
        return Err(SourcePackageError);
    }
    usize::from_str_radix(
        std::str::from_utf8(&field[..11]).map_err(|_| SourcePackageError)?,
        8,
    )
    .map_err(|_| SourcePackageError)
}

/// A NUL-terminated field read as Latin-1.
fn latin1(field: &[u8]) -> String {
    field
        .iter()
        .take_while(|byte| **byte != 0)
        .map(|byte| char::from(*byte))
        .collect()
}

fn header_path(header: &[u8]) -> String {
    let (name, prefix) = (latin1(&header[..100]), latin1(&header[345..500]));
    if prefix.is_empty() {
        name
    } else {
        format!("{prefix}/{name}")
    }
}

fn validate_records(records: &[Record<'_>]) -> Result<(), SourcePackageError> {
    if records.windows(2).any(|pair| pair[0].path > pair[1].path) {
        return Err(SourcePackageError);
    }
    let files: Vec<&Record<'_>> = records.iter().filter(|record| !record.directory).collect();
    if files.len() > MAX_REGULAR_FILES
        || files
            .iter()
            .enumerate()
            .any(|(index, file)| files[..index].iter().any(|other| other.path == file.path))
    {
        return Err(SourcePackageError);
    }
    for file in &files {
        validate_path(&file.path)?;
    }
    let folded: Vec<String> = files.iter().map(|file| file.path.to_lowercase()).collect();
    if folded
        .iter()
        .enumerate()
        .any(|(index, path)| folded[..index].contains(path))
    {
        return Err(SourcePackageError);
    }
    let has = |name: &str| files.iter().any(|file| file.path == name);
    if !REQUIRED_FILES.iter().all(|name| has(name))
        || !files.iter().any(|file| file.path.starts_with("actions/"))
    {
        return Err(SourcePackageError);
    }
    if files
        .iter()
        .any(|file| file.path == "icon.png" && file.contents.len() > MAX_ICON_BYTES)
    {
        return Err(SourcePackageError);
    }
    directories_implied(records, &files)
}

/// The directories are exactly the ones the files imply, each once.
fn directories_implied(
    records: &[Record<'_>],
    files: &[&Record<'_>],
) -> Result<(), SourcePackageError> {
    let mut expected: Vec<String> = files
        .iter()
        .flat_map(|file| {
            let parts: Vec<&str> = file.path.split('/').collect();
            (1..parts.len()).map(move |end| parts[..end].join("/"))
        })
        .collect();
    expected.sort();
    expected.dedup();
    let mut actual: Vec<String> = records
        .iter()
        .filter(|record| record.directory)
        .map(|record| record.path.clone())
        .collect();
    let count = actual.len();
    actual.sort();
    actual.dedup();
    if actual != expected || actual.len() != count {
        return Err(SourcePackageError);
    }
    Ok(())
}

fn portable(segment: &str) -> bool {
    !segment.is_empty()
        && segment
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn validate_path(path: &str) -> Result<(), SourcePackageError> {
    let parts: Vec<&str> = path.split('/').collect();
    let valid = !path.is_empty()
        && path.is_ascii()
        && path.len() <= MAX_PATH_BYTES
        && parts.len() <= MAX_PATH_COMPONENTS
        && parts
            .iter()
            .all(|part| portable(part) && *part != "." && *part != "..");
    if !valid {
        return Err(SourcePackageError);
    }
    split_path(path)?;
    let action = parts.len() == 2 && parts[0] == "actions" && {
        let name = parts[1];
        name.len() > 3
            && name.as_bytes().ends_with(b".py")
            && name
                .as_bytes()
                .first()
                .is_some_and(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    };
    if REQUIRED_FILES.contains(&path)
        || action
        || (parts.len() >= 2 && OPTIONAL_ROOTS.contains(&parts[0]))
    {
        Ok(())
    } else {
        Err(SourcePackageError)
    }
}

fn split_path(path: &str) -> Result<(&str, &str), SourcePackageError> {
    if path.len() <= MAX_NAME_BYTES {
        return Ok(("", path));
    }
    let (prefix, name) = path.rsplit_once('/').ok_or(SourcePackageError)?;
    if prefix.len() > MAX_PREFIX_BYTES || name.len() > MAX_NAME_BYTES {
        return Err(SourcePackageError);
    }
    Ok((prefix, name))
}

fn build_archive(records: &[Record<'_>]) -> Result<Vec<u8>, SourcePackageError> {
    let mut output = Vec::new();
    for record in records {
        output.extend_from_slice(&build_header(record)?);
        output.extend_from_slice(record.contents);
        output.resize(output.len().div_ceil(BLOCK) * BLOCK, 0);
    }
    output.resize(output.len() + 2 * BLOCK, 0);
    Ok(output)
}

fn octal(value: usize, width: usize) -> Result<Vec<u8>, SourcePackageError> {
    let encoded = format!("{value:0digits$o}\0", digits = width - 1).into_bytes();
    if encoded.len() == width {
        Ok(encoded)
    } else {
        Err(SourcePackageError)
    }
}

fn put(
    header: &mut [u8],
    offset: usize,
    width: usize,
    value: &[u8],
) -> Result<(), SourcePackageError> {
    if value.len() > width {
        return Err(SourcePackageError);
    }
    header[offset..offset + value.len()].copy_from_slice(value);
    Ok(())
}

fn build_header(record: &Record<'_>) -> Result<[u8; BLOCK], SourcePackageError> {
    let (prefix, name) = split_path(&record.path)?;
    let mut header = [0_u8; BLOCK];
    put(&mut header, 0, 100, name.as_bytes())?;
    put(
        &mut header,
        100,
        8,
        &octal(if record.directory { 0o755 } else { 0o644 }, 8)?,
    )?;
    put(&mut header, 108, 8, &octal(0, 8)?)?;
    put(&mut header, 116, 8, &octal(0, 8)?)?;
    put(&mut header, 124, 12, &octal(record.contents.len(), 12)?)?;
    put(&mut header, 136, 12, &octal(0, 12)?)?;
    put(&mut header, 148, 8, b"        ")?;
    put(
        &mut header,
        156,
        1,
        if record.directory { b"5" } else { b"0" },
    )?;
    put(&mut header, 257, 6, b"ustar\0")?;
    put(&mut header, 263, 2, b"00")?;
    put(&mut header, 329, 8, &octal(0, 8)?)?;
    put(&mut header, 337, 8, &octal(0, 8)?)?;
    put(&mut header, 345, 155, prefix.as_bytes())?;
    let sum: u32 = header.iter().map(|byte| u32::from(*byte)).sum();
    put(&mut header, 148, 8, format!("{sum:06o}\0 ").as_bytes())?;
    Ok(header)
}

/// The CRC-32 (ISO-HDLC) PNG chunks carry.
fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffff_u32;
    for byte in bytes {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            crc = if crc & 1 == 1 {
                (crc >> 1) ^ 0xedb8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

/// One PNG chunk: its kind and its data.
type Chunk<'a> = (&'a [u8], &'a [u8]);

fn chunks(contents: &[u8]) -> Result<Vec<Chunk<'_>>, SourcePackageError> {
    let rest = contents
        .strip_prefix(b"\x89PNG\r\n\x1a\n")
        .ok_or(SourcePackageError)?;
    let mut found = Vec::new();
    let mut offset = 0;
    while offset < rest.len() {
        let header = rest.get(offset..offset + 8).ok_or(SourcePackageError)?;
        let length = usize::try_from(u32::from_be_bytes([
            header[0], header[1], header[2], header[3],
        ]))
        .map_err(|_| SourcePackageError)?;
        let end = offset
            .checked_add(12)
            .and_then(|start| start.checked_add(length))
            .ok_or(SourcePackageError)?;
        let chunk = rest.get(offset..end).ok_or(SourcePackageError)?;
        let (kind, data) = (&chunk[4..8], &chunk[8..8 + length]);
        let recorded = u32::from_be_bytes([
            chunk[8 + length],
            chunk[9 + length],
            chunk[10 + length],
            chunk[11 + length],
        ]);
        if !kind.iter().all(u8::is_ascii_alphabetic) || crc32(&chunk[4..8 + length]) != recorded {
            return Err(SourcePackageError);
        }
        found.push((kind, data));
        offset = end;
    }
    Ok(found)
}

/// Validates one bounded canonical 1024×1024 PNG icon: its chunk structure, CRCs, and header.
///
/// # Errors
/// [`SourcePackageError`] for anything else.
pub fn validate_icon(contents: &[u8]) -> Result<(), SourcePackageError> {
    let chunks = chunks(contents)?;
    let kinds: Vec<&[u8]> = chunks.iter().map(|(kind, _)| *kind).collect();
    let count = |name: &[u8]| kinds.iter().filter(|kind| **kind == name).count();
    let shaped = chunks.first().is_some_and(|(kind, _)| *kind == b"IHDR")
        && chunks
            .last()
            .is_some_and(|(kind, data)| *kind == b"IEND" && data.is_empty())
        && count(b"IHDR") == 1
        && count(b"IEND") == 1
        && count(b"IDAT") > 0
        && count(b"acTL") == 0;
    if !shaped {
        return Err(SourcePackageError);
    }
    let header = chunks[0].1;
    if header.len() != 13 {
        return Err(SourcePackageError);
    }
    let width = u32::from_be_bytes([header[0], header[1], header[2], header[3]]);
    let height = u32::from_be_bytes([header[4], header[5], header[6], header[7]]);
    let (depth, color) = (header[8], header[9]);
    let depths: &[u8] = match color {
        0 => &[1, 2, 4, 8, 16],
        2 | 4 | 6 => &[8, 16],
        3 => &[1, 2, 4, 8],
        _ => &[],
    };
    let position = |name: &[u8]| kinds.iter().position(|kind| *kind == name);
    let palette_ok = color != 3
        || matches!((position(b"PLTE"), position(b"IDAT")), (Some(palette), Some(data)) if palette <= data);
    let valid = (width, height) == (1024, 1024)
        && depths.contains(&depth)
        && header[10] == 0
        && header[11] == 0
        && header[12] <= 1
        && palette_ok;
    if valid {
        Ok(())
    } else {
        Err(SourcePackageError)
    }
}

#[cfg(test)]
pub(crate) mod tests;
