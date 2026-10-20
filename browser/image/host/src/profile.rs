//! A Sign-in's profile capsule (ADR-0117, Sign-ins amendment items 4 and 6): Chromium's `/profile`, exported as one
//! bounded ustar archive after a proven clean stop and imported into a fresh Browser's empty `/profile` before
//! Chromium starts. Only regular files and directories cross, under fixed bounds; the transient locks and the render
//! and HTTP caches stay behind. Every archive this program reads is validated whole before anything is written.

use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};

const BLOCK: usize = 512;
/// The directory inside an empty profile where an import is written before it is complete.
const STAGING: &str = ".capsule";
pub const MAX_ENTRIES: usize = 4096;
/// The longest archive an export writes, and the most file bytes an import restores.
pub const MAX_TOTAL_BYTES: u64 = 128 * 1024 * 1024;
pub const MAX_FILE_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_DEPTH: usize = 16;
pub const MAX_PATH_BYTES: usize = 255;
/// Top-level entries never exported: Chromium's process locks and crash reports, and caches it rebuilds.
const SKIPPED_TOP: [&str; 4] = [
    "Crashpad",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
];
/// Caches inside the `Default` profile that Chromium rebuilds.
const SKIPPED_DEFAULT: [&str; 5] = [
    "GPUCache",
    "DawnWebGPUCache",
    "DawnGraphiteCache",
    "Cache",
    "Code Cache",
];

#[cfg(target_arch = "x86_64")]
const O_NOFOLLOW: i32 = 0o400_000;
#[cfg(target_arch = "aarch64")]
const O_NOFOLLOW: i32 = 0o100_000;

/// One archive entry: its relative path and, for a file, its size.
#[derive(Debug, PartialEq, Eq)]
enum Entry {
    Directory(String),
    File(String, u64),
}

fn skipped(relative: &Path) -> bool {
    let mut components = relative
        .components()
        .map(|part| part.as_os_str().to_string_lossy());
    match (components.next(), components.next()) {
        (Some(first), None) => {
            first.starts_with("Singleton") || SKIPPED_TOP.contains(&first.as_ref())
        }
        (Some(first), Some(second)) => {
            first == "Default"
                && components.next().is_none()
                && SKIPPED_DEFAULT.contains(&second.as_ref())
        }
        _ => false,
    }
}

/// A relative path Team may receive or write: plain UTF-8 components, no `.` or `..`, within the depth and length
/// bounds, and no control character.
fn admissible(relative: &str) -> bool {
    !relative.is_empty()
        && relative.len() <= MAX_PATH_BYTES
        && !relative.starts_with('/')
        && !relative.chars().any(char::is_control)
        && relative.split('/').count() <= MAX_DEPTH
        && relative
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// Walk `root` in a stable order and name every entry the capsule keeps, refusing anything but regular single-link
/// files and directories and every bound.
fn entries(root: &Path) -> Result<Vec<Entry>, String> {
    let mut found = Vec::new();
    // The archive's own length: its two end blocks, then each entry's header, content, and padding.
    let mut total = 2 * BLOCK as u64;
    walk(root, Path::new(""), &mut found, &mut total)?;
    Ok(found)
}

fn walk(
    root: &Path,
    relative: &Path,
    found: &mut Vec<Entry>,
    total: &mut u64,
) -> Result<(), String> {
    let mut children: Vec<_> = fs::read_dir(root.join(relative))
        .map_err(|error| format!("cannot list the profile: {error}"))?
        .collect::<Result<_, _>>()
        .map_err(|error| format!("cannot list the profile: {error}"))?;
    children.sort_by_key(|child| child.file_name());
    for child in children {
        let path = relative.join(child.file_name());
        if skipped(&path) {
            continue;
        }
        let name = path
            .to_str()
            .filter(|name| admissible(name))
            .ok_or("the profile holds a path a capsule cannot keep")?
            .to_owned();
        let metadata = fs::symlink_metadata(root.join(&path))
            .map_err(|error| format!("cannot inspect the profile: {error}"))?;
        if found.len() >= MAX_ENTRIES {
            return Err("the profile holds more entries than a capsule keeps".into());
        }
        *total += BLOCK as u64;
        if metadata.file_type().is_dir() {
            found.push(Entry::Directory(name));
            walk(root, &path, found, total)?;
        } else if metadata.file_type().is_file() && metadata.nlink() == 1 {
            let size = metadata.len();
            *total += size + padding(size) as u64;
            if size > MAX_FILE_BYTES || *total > MAX_TOTAL_BYTES {
                return Err("the profile is larger than a capsule keeps".into());
            }
            found.push(Entry::File(name, size));
        } else {
            return Err("the profile holds a link or a special file".into());
        }
    }
    Ok(())
}

fn octal(field: &mut [u8], value: u64) {
    let digits = format!("{value:0width$o}", width = field.len() - 1);
    field[..digits.len()].copy_from_slice(digits.as_bytes());
}

/// One ustar header; a path over 100 bytes is split at a `/` into prefix and name.
fn header(name: &str, size: u64, directory: bool) -> Result<[u8; BLOCK], String> {
    let mut block = [0u8; BLOCK];
    let (prefix, base) = if name.len() <= 100 {
        ("", name)
    } else {
        // Bytes, not characters: a cut inside a multi-byte character would be no string at all.
        let bytes = name.as_bytes();
        let split = bytes[..bytes.len().min(156)]
            .iter()
            .rposition(|&byte| byte == b'/')
            .filter(|&index| name.len() - index - 1 <= 100)
            .ok_or("a profile path cannot be written in ustar")?;
        (&name[..split], &name[split + 1..])
    };
    block[..base.len()].copy_from_slice(base.as_bytes());
    octal(&mut block[100..108], if directory { 0o700 } else { 0o600 });
    octal(&mut block[108..116], 0);
    octal(&mut block[116..124], 0);
    octal(&mut block[124..136], size);
    octal(&mut block[136..148], 0);
    block[156] = if directory { b'5' } else { b'0' };
    block[257..263].copy_from_slice(b"ustar\0");
    block[263..265].copy_from_slice(b"00");
    block[345..345 + prefix.len()].copy_from_slice(prefix.as_bytes());
    block[148..156].copy_from_slice(b"        ");
    let sum: u32 = block.iter().map(|&byte| u32::from(byte)).sum();
    let digits = format!("{sum:06o}\0 ");
    block[148..156].copy_from_slice(digits.as_bytes());
    Ok(block)
}

/// Write the capsule of `root` to `out`: every kept entry, then the two zero blocks that end an archive.
pub fn export(root: &Path, out: &mut impl Write) -> Result<(), String> {
    let write = |out: &mut dyn Write, bytes: &[u8]| {
        out.write_all(bytes)
            .map_err(|error| format!("cannot write the capsule: {error}"))
    };
    for entry in entries(root)? {
        match entry {
            Entry::Directory(name) => write(out, &header(&name, 0, true)?)?,
            Entry::File(name, size) => {
                write(out, &header(&name, size, false)?)?;
                let file = OpenOptions::new()
                    .read(true)
                    .custom_flags(O_NOFOLLOW)
                    .open(root.join(&name))
                    .map_err(|_| "a profile file cannot be opened".to_owned())?;
                let copied = io::copy(&mut file.take(size), out)
                    .map_err(|error| format!("cannot write the capsule: {error}"))?;
                if copied != size {
                    return Err("the profile changed while it was exported".into());
                }
                write(out, &vec![0u8; padding(size)])?;
            }
        }
    }
    write(out, &[0u8; 2 * BLOCK])?;
    out.flush()
        .map_err(|error| format!("cannot write the capsule: {error}"))
}

fn padding(size: u64) -> usize {
    (BLOCK - (size % BLOCK as u64) as usize) % BLOCK
}

fn field(block: &[u8], start: usize, end: usize) -> Result<&str, String> {
    let bytes = &block[start..end];
    let length = bytes
        .iter()
        .position(|&byte| byte == 0)
        .unwrap_or(bytes.len());
    std::str::from_utf8(&bytes[..length]).map_err(|_| "a capsule header is not text".into())
}

fn number(block: &[u8], start: usize, end: usize) -> Result<u64, String> {
    let text = field(block, start, end)?.trim_matches(|character| character == ' ');
    if text.is_empty() || !text.bytes().all(|byte| (b'0'..=b'7').contains(&byte)) {
        return Err("a capsule header holds no octal number".into());
    }
    u64::from_str_radix(text, 8).map_err(|_| "a capsule number is out of range".into())
}

/// Parse one header: its entry, refusing any type but a file or a directory and any malformed field.
fn parse(block: &[u8; BLOCK]) -> Result<Entry, String> {
    if &block[257..263] != b"ustar\0" || &block[263..265] != b"00" {
        return Err("a capsule entry is not ustar".into());
    }
    let mut summed = *block;
    summed[148..156].copy_from_slice(b"        ");
    let sum: u64 = summed.iter().map(|&byte| u64::from(byte)).sum();
    if number(block, 148, 156)? != sum {
        return Err("a capsule header's checksum is wrong".into());
    }
    let (prefix, base) = (field(block, 345, 500)?, field(block, 0, 100)?);
    let name = if prefix.is_empty() {
        base.to_owned()
    } else {
        format!("{prefix}/{base}")
    };
    let name = name.trim_end_matches('/').to_owned();
    if !admissible(&name) || !field(block, 157, 257)?.is_empty() {
        return Err("a capsule entry's path is not admissible".into());
    }
    let size = number(block, 124, 136)?;
    match block[156] {
        b'5' if size == 0 => Ok(Entry::Directory(name)),
        b'0' | 0 if size <= MAX_FILE_BYTES => Ok(Entry::File(name, size)),
        _ => Err("a capsule entry is neither a bounded file nor a directory".into()),
    }
}

fn read_block(input: &mut impl Read, block: &mut [u8]) -> Result<(), String> {
    input
        .read_exact(block)
        .map_err(|_| "the capsule ended early".to_owned())
}

/// Read a whole capsule from `input` into a staging directory inside `root`, which must be empty, validating every
/// header, bound, and duplicate as it goes; only a complete, valid archive is then moved into `root` itself.
pub fn import(input: &mut impl Read, root: &Path) -> Result<(), String> {
    if fs::read_dir(root)
        .map_err(|error| format!("cannot open the profile: {error}"))?
        .next()
        .is_some()
    {
        return Err("the profile is not empty".into());
    }
    let staging = &root.join(STAGING);
    fs::create_dir(staging).map_err(|error| format!("cannot stage the capsule: {error}"))?;
    let outcome = unpack(input, staging);
    if outcome.is_err() {
        let _ = fs::remove_dir_all(staging);
        return outcome;
    }
    for child in
        fs::read_dir(staging).map_err(|error| format!("cannot stage the capsule: {error}"))?
    {
        let child = child.map_err(|error| format!("cannot stage the capsule: {error}"))?;
        fs::rename(child.path(), root.join(child.file_name()))
            .map_err(|error| format!("cannot restore the capsule: {error}"))?;
    }
    fs::remove_dir(staging).map_err(|error| format!("cannot stage the capsule: {error}"))
}

fn unpack(input: &mut impl Read, staging: &Path) -> Result<(), String> {
    let mut seen = std::collections::HashSet::new();
    let (mut count, mut total) = (0usize, 0u64);
    let mut block = [0u8; BLOCK];
    loop {
        read_block(input, &mut block)?;
        if block.iter().all(|&byte| byte == 0) {
            read_block(input, &mut block)?;
            if block.iter().all(|&byte| byte == 0) {
                let mut rest = [0u8; 1];
                return match input.read(&mut rest) {
                    Ok(0) => Ok(()),
                    _ => Err("the capsule has bytes after its end".into()),
                };
            }
            return Err("the capsule's end is malformed".into());
        }
        count += 1;
        if count > MAX_ENTRIES {
            return Err("the capsule holds more entries than allowed".into());
        }
        let entry = parse(&block)?;
        let name = match &entry {
            Entry::Directory(name) | Entry::File(name, _) => name.clone(),
        };
        if !seen.insert(name.clone()) {
            return Err("the capsule names one path twice".into());
        }
        let path: PathBuf = staging.join(&name);
        if Path::new(&name)
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err("a capsule entry's path is not admissible".into());
        }
        let parent = path.parent().ok_or("a capsule entry has no parent")?;
        if !fs::symlink_metadata(parent).is_ok_and(|meta| meta.is_dir()) {
            return Err("a capsule entry comes before its directory".into());
        }
        match entry {
            Entry::Directory(_) => fs::create_dir(&path)
                .map_err(|error| format!("cannot restore a directory: {error}"))?,
            Entry::File(_, size) => {
                total += size;
                if total > MAX_TOTAL_BYTES {
                    return Err("the capsule is larger than allowed".into());
                }
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .custom_flags(O_NOFOLLOW)
                    .open(&path)
                    .map_err(|error| format!("cannot restore a file: {error}"))?;
                let copied = io::copy(&mut input.take(size), &mut file)
                    .map_err(|error| format!("cannot restore a file: {error}"))?;
                if copied != size {
                    return Err("the capsule ended early".into());
                }
                let mut pad = vec![0u8; padding(size)];
                read_block(input, &mut pad)?;
                if pad.iter().any(|&byte| byte != 0) {
                    return Err("a capsule file's padding is not zero".into());
                }
            }
        }
    }
}

/// Whether Chromium's last exit was the session ending its own way (`exit_type` `SessionEnded`), read from the
/// profile's preferences without following a link.
pub fn ended_cleanly(root: &Path) -> bool {
    let Ok(file) = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(root.join("Default").join("Preferences"))
    else {
        return false;
    };
    let mut text = String::new();
    file.take(16 * 1024 * 1024)
        .read_to_string(&mut text)
        .is_ok()
        && text.contains("\"exit_type\":\"SessionEnded\"")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::os::unix::fs::symlink;

    fn directory(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("profile-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn profile(root: &Path) {
        fs::create_dir_all(root.join("Default/Local Storage/leveldb")).unwrap();
        fs::create_dir_all(root.join("Default/GPUCache")).unwrap();
        fs::write(root.join("Default/Cookies"), b"cookies").unwrap();
        fs::write(
            root.join("Default/Local Storage/leveldb/000003.log"),
            vec![7u8; 1500],
        )
        .unwrap();
        fs::write(root.join("Default/GPUCache/data_1"), b"cache").unwrap();
        fs::write(root.join("Local State"), b"{}").unwrap();
        let long = format!("Default/{}/{}", "d".repeat(90), "f".repeat(90));
        fs::create_dir_all(root.join(&long).parent().unwrap()).unwrap();
        fs::write(root.join(&long), b"long").unwrap();
        symlink("container-host", root.join("SingletonLock")).unwrap();
    }

    #[test]
    fn a_capsule_keeps_the_profile_without_its_locks_and_caches_and_restores_it_exactly() {
        let source = directory("source");
        profile(&source);
        let mut archive = Vec::new();
        export(&source, &mut archive).unwrap();
        let target = directory("target");
        import(&mut archive.as_slice(), &target).unwrap();
        assert_eq!(
            fs::read(target.join("Default/Cookies")).unwrap(),
            b"cookies"
        );
        assert_eq!(
            fs::read(target.join("Default/Local Storage/leveldb/000003.log"))
                .unwrap()
                .len(),
            1500
        );
        let long = format!("Default/{}/{}", "d".repeat(90), "f".repeat(90));
        assert_eq!(fs::read(target.join(long)).unwrap(), b"long");
        assert!(!target.join("SingletonLock").exists());
        assert!(!target.join("Default/GPUCache").exists());
        assert_eq!(
            fs::metadata(target.join("Default/Cookies")).unwrap().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(&source).unwrap();
        fs::remove_dir_all(&target).unwrap();
    }

    #[test]
    fn an_export_refuses_a_link_or_a_profile_over_its_bounds() {
        let source = directory("links");
        fs::create_dir(source.join("Default")).unwrap();
        symlink("/etc/passwd", source.join("Default/pointer")).unwrap();
        assert!(export(&source, &mut Vec::new()).is_err());
        fs::remove_file(source.join("Default/pointer")).unwrap();
        File::create(source.join("Default/huge"))
            .unwrap()
            .set_len(MAX_FILE_BYTES + 1)
            .unwrap();
        assert!(export(&source, &mut Vec::new()).is_err());
        fs::remove_dir_all(&source).unwrap();
    }

    #[test]
    fn a_long_path_of_multibyte_names_splits_at_a_slash_without_panicking() {
        let name = format!("{}/{}", "é".repeat(70), "ü".repeat(40));
        let block = header(&name, 0, true).unwrap();
        assert_eq!(&block[345..345 + 140], "é".repeat(70).as_bytes());
        assert_eq!(&block[..80], "ü".repeat(40).as_bytes());
        let unsplittable = "é".repeat(120);
        assert!(header(&unsplittable, 0, true).is_err());
    }

    fn archive_of(entries: &[(&str, &[u8], bool)]) -> Vec<u8> {
        let mut out = Vec::new();
        for (name, body, directory) in entries {
            out.extend_from_slice(&header(name, body.len() as u64, *directory).unwrap());
            out.extend_from_slice(body);
            out.extend(vec![0u8; padding(body.len() as u64)]);
        }
        out.extend(vec![0u8; 2 * BLOCK]);
        out
    }

    #[test]
    fn an_import_refuses_every_malformed_or_hostile_archive_and_writes_nothing() {
        let valid = archive_of(&[("Default", b"", true), ("Default/Cookies", b"c", false)]);
        let mut wrong_type = valid.clone();
        wrong_type[BLOCK + 156] = b'2';
        let mut bad_sum = valid.clone();
        bad_sum[BLOCK] ^= 1;
        let mut trailing = valid.clone();
        trailing.push(1);
        let hostile = [
            archive_of(&[("../escape", b"x", false)]),
            archive_of(&[("Default/Cookies", b"orphan", false)]),
            archive_of(&[("Default", b"", true), ("Default", b"", true)]),
            wrong_type,
            bad_sum,
            trailing,
            valid[..valid.len() - BLOCK].to_vec(),
        ];
        for (index, archive) in hostile.iter().enumerate() {
            let target = directory(&format!("hostile-{index}"));
            assert!(import(&mut archive.as_slice(), &target).is_err(), "{index}");
            assert_eq!(fs::read_dir(&target).unwrap().count(), 0, "{index}");
            fs::remove_dir_all(&target).unwrap();
        }
        let occupied = directory("occupied");
        fs::write(occupied.join("x"), b"x").unwrap();
        assert!(import(&mut valid.as_slice(), &occupied).is_err());
        fs::remove_dir_all(&occupied).unwrap();
    }

    #[test]
    fn only_a_session_ending_exit_counts_as_clean() {
        let root = directory("prefs");
        assert!(!ended_cleanly(&root));
        fs::create_dir(root.join("Default")).unwrap();
        fs::write(
            root.join("Default/Preferences"),
            br#"{"profile":{"exit_type":"Crashed"}}"#,
        )
        .unwrap();
        assert!(!ended_cleanly(&root));
        fs::write(
            root.join("Default/Preferences"),
            br#"{"profile":{"exit_type":"SessionEnded"}}"#,
        )
        .unwrap();
        assert!(ended_cleanly(&root));
        fs::remove_dir_all(&root).unwrap();
    }
}
