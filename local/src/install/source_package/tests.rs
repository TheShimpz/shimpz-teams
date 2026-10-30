//! Canonical source-package v1 admission (`tests/test_local_source_package.py`).

use super::{Record, SourcePackageError, admit, build_archive, crc32, validate_icon};

fn chunk(kind: &[u8], data: &[u8]) -> Vec<u8> {
    let mut chunk = u32::try_from(data.len())
        .expect("bounded")
        .to_be_bytes()
        .to_vec();
    chunk.extend_from_slice(kind);
    chunk.extend_from_slice(data);
    let crc = crc32(&chunk[4..]);
    chunk.extend_from_slice(&crc.to_be_bytes());
    chunk
}

/// A structurally canonical PNG icon of `side`×`side` pixels.
pub(crate) fn icon(side: u32) -> Vec<u8> {
    let mut header = side.to_be_bytes().to_vec();
    header.extend_from_slice(&side.to_be_bytes());
    header.extend_from_slice(&[8, 6, 0, 0, 0]);
    let mut png = b"\x89PNG\r\n\x1a\n".to_vec();
    png.extend(chunk(b"IHDR", &header));
    png.extend(chunk(b"IDAT", b"pixels"));
    png.extend(chunk(b"IEND", b""));
    png
}

fn file<'a>(path: &str, contents: &'a [u8]) -> Record<'a> {
    Record {
        path: path.to_owned(),
        directory: false,
        contents,
    }
}

fn directory(path: &str) -> Record<'static> {
    Record {
        path: path.to_owned(),
        directory: true,
        contents: b"",
    }
}

const MANIFEST: &[u8] = b"[assistant]\nid = \"notes\"\n";

fn records(icon: &[u8]) -> Vec<Record<'_>> {
    records_with(icon, MANIFEST)
}

fn records_with<'a>(icon: &'a [u8], manifest: &'a [u8]) -> Vec<Record<'a>> {
    vec![
        directory("actions"),
        file("actions/write.py", b"print('ok')\n"),
        file("icon.png", icon),
        file("pyproject.toml", b"[project]\nname = \"notes\"\n"),
        file("shimpz.toml", manifest),
    ]
}

/// A canonical source package declaring `manifest`, with `icon` and one Action.
#[cfg(feature = "testing")]
pub(crate) fn canonical(manifest: &[u8], icon: &[u8]) -> Vec<u8> {
    package(&records_with(icon, manifest))
}

fn package(records: &[Record<'_>]) -> Vec<u8> {
    build_archive(records).expect("buildable")
}

#[test]
fn a_canonical_package_is_admitted_with_its_display_inputs() {
    let icon = icon(1024);
    let raw = package(&records(&icon));
    let admitted = admit(&raw).expect("admitted");
    assert_eq!(admitted.manifest, MANIFEST);
    assert_eq!(admitted.icon, icon);
    assert_eq!(
        admitted.digest,
        format!("sha256:{}", assistant_protocol::python::sha256_hex(&raw))
    );
}

#[test]
fn noncanonical_order_duplicates_and_unknown_paths_are_refused() {
    let icon = icon(1024);
    let mut reversed = records(&icon);
    reversed.reverse();
    let mut duplicated = records(&icon);
    duplicated.push(file("shimpz.toml", MANIFEST));
    let mut unknown = records(&icon);
    unknown.push(file("secret.txt", b"secret"));
    let mut folded = records(&icon);
    folded.insert(1, file("actions/Write.py", b""));
    let mut undeclared_directory = records(&icon);
    undeclared_directory.remove(0);
    let mut without_actions = records(&icon);
    without_actions.drain(..2);
    for mutation in [
        reversed,
        duplicated,
        unknown,
        folded,
        undeclared_directory,
        without_actions,
    ] {
        assert_eq!(admit(&package(&mutation)), Err(SourcePackageError));
    }
}

#[test]
fn widened_truncated_and_noncanonical_archives_are_refused() {
    let raw = package(&records(&icon(1024)));
    let mut widened = raw.clone();
    widened.extend_from_slice(&[0; 512]);
    let truncated = raw[..raw.len() - 512].to_vec();
    let mut noncanonical = raw.clone();
    noncanonical[100] = if noncanonical[100] == b'0' {
        b'1'
    } else {
        b'0'
    };
    for mutation in [widened, truncated, noncanonical] {
        assert_eq!(admit(&mutation), Err(SourcePackageError));
    }
}

#[test]
fn a_tampered_or_misshapen_icon_is_refused() {
    let mut tampered = icon(1024);
    let last = tampered.len() - 1;
    tampered[last] ^= 1;
    assert_eq!(validate_icon(&tampered), Err(SourcePackageError));
    assert_eq!(validate_icon(&icon(512)), Err(SourcePackageError));
    assert_eq!(
        admit(&package(&records(&tampered))),
        Err(SourcePackageError)
    );
}
