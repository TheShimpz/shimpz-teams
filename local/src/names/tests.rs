use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};

use strict_json::Value;

use super::{TeamNames, canonical_name, docker_instant_ns, folded, label_name};

const NETWORK_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const NETWORK_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn store() -> (tempfile::TempDir, TeamNames) {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let names = TeamNames::new(directory.path().join("inference"));
    (directory, names)
}

fn code<T>(result: Result<T, crate::http::service::Problem>) -> Option<&'static str> {
    result.err().and_then(|problem| problem.code)
}

#[test]
fn a_record_is_the_name_of_its_own_incarnation_and_absence_falls_back() {
    let (_directory, names) = store();
    assert_eq!(names.load("team_1", NETWORK_A), Ok(None));
    names.save("team_1", NETWORK_A, "Growth").expect("saved");
    assert_eq!(
        names.load("team_1", NETWORK_A),
        Ok(Some("Growth".to_owned()))
    );
    let path = names.path("team_1");
    let mode = fs::metadata(&path).expect("stat").permissions().mode() & 0o777;
    assert_eq!(mode, 0o600);
    assert_eq!(
        fs::read(&path).expect("read"),
        format!(
            r#"{{"network_id":"{NETWORK_A}","schema":1,"team_id":"team_1","team_name":"Growth"}}"#
        )
        .into_bytes()
    );
    // Never the record of another incarnation.
    assert_eq!(
        code(names.load("team_1", NETWORK_B)),
        Some("team-names-unavailable")
    );
}

#[test]
fn an_unsafe_or_foreign_record_fails_closed() {
    let record = |fields: &str| format!("{{{fields}}}");
    let base = format!(r#""network_id":"{NETWORK_A}","schema":1,"team_id":"team_1""#);
    let unsafe_contents = [
        "not json".to_owned(),
        "[]".to_owned(),
        record(&format!(r#"{base},"team_name":"Growth","extra":1"#)),
        record(&format!(
            r#""network_id":"{NETWORK_A}","schema":2,"team_id":"team_1","team_name":"G""#
        )),
        record(&format!(
            r#""network_id":"{NETWORK_A}","schema":true,"team_id":"team_1","team_name":"G""#
        )),
        record(&format!(
            r#""network_id":"{NETWORK_A}","schema":1.0,"team_id":"team_1","team_name":"G""#
        )),
        record(&format!(
            r#""network_id":"{NETWORK_A}","schema":1,"team_id":"team_2","team_name":"G""#
        )),
        record(&format!(r#"{base},"team_name":"Equipé""#)),
        record(&format!(r#"{base},"team_name":"{}""#, "x".repeat(5000))),
        record(&format!(r#"{base},"team_name":"Growth​""#)),
        record(&format!(
            r#"{base},"team_name":"Growth","team_name":"Growth""#
        )),
    ];
    for content in unsafe_contents {
        let (_directory, names) = store();
        names.save("team_1", NETWORK_A, "Growth").expect("saved");
        fs::write(names.path("team_1"), &content).expect("written");
        assert_eq!(
            code(names.load("team_1", NETWORK_A)),
            Some("team-names-unavailable"),
            "{content}"
        );
    }
}

#[test]
fn a_record_that_is_a_link_or_a_directory_fails_closed() {
    let (_directory, names) = store();
    names.save("team_1", NETWORK_A, "Growth").expect("saved");
    let path = names.path("team_1");
    fs::remove_file(&path).expect("removed");
    symlink("/nonexistent", &path).expect("linked");
    assert_eq!(
        code(names.load("team_1", NETWORK_A)),
        Some("team-names-unavailable")
    );
    fs::remove_file(&path).expect("removed");
    fs::create_dir(&path).expect("a directory");
    assert_eq!(
        code(names.load("team_1", NETWORK_A)),
        Some("team-names-unavailable")
    );
    assert_eq!(code(names.delete("team_1")), Some("team-names-unavailable"));
}

#[test]
fn writes_and_listing_fail_closed() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let root = directory.path().join("inference");
    fs::write(&root, b"").expect("a regular file");
    let names = TeamNames::new(root);
    assert_eq!(
        code(names.save("team_1", NETWORK_A, "Growth")),
        Some("team-names-unavailable")
    );
    assert_eq!(code(names.delete_all()), Some("team-names-unavailable"));
}

#[test]
fn delete_removes_only_the_team_s_own_files_and_delete_all_every_owned_one() {
    let (_directory, names) = store();
    assert_eq!(names.delete("team_1"), Ok(()));
    assert_eq!(names.delete_all(), Ok(()));
    names.save("team_1", NETWORK_A, "One").expect("saved");
    names.save("team_2", NETWORK_A, "Two").expect("saved");
    let own = format!(".{}.0123456789abcdef.tmp", TeamNames::file_name("team_1"));
    let other = format!(".{}.0123456789abcdef.tmp", TeamNames::file_name("team_2"));
    for name in [&own, &other, &"unrelated.json".to_owned()] {
        fs::write(names.root.join(name), b"{}").expect("written");
    }
    names.delete("team_1").expect("deleted");
    let left = |names: &TeamNames| {
        let mut left: Vec<String> = fs::read_dir(&names.root)
            .expect("listed")
            .map(|entry| {
                entry
                    .expect("entry")
                    .file_name()
                    .into_string()
                    .expect("utf-8")
            })
            .collect();
        left.sort();
        left
    };
    let mut expected = vec![
        other.clone(),
        TeamNames::file_name("team_2"),
        "unrelated.json".to_owned(),
    ];
    expected.sort();
    assert_eq!(left(&names), expected);
    names.delete_all().expect("deleted");
    assert_eq!(left(&names), ["unrelated.json"]);
}

#[test]
fn names_are_canonical_local_names_compared_ignoring_case() {
    assert_eq!(
        canonical_name(&Value::String("Growth".to_owned())),
        Ok("Growth".to_owned())
    );
    for refused in [" Growth", "", "Equipe\u{301}", "Tab\tName"] {
        assert_eq!(
            code(canonical_name(&Value::String(refused.to_owned()))),
            Some("invalid-team-name")
        );
    }
    assert_eq!(
        code(canonical_name(&Value::Bool(true))),
        Some("invalid-team-name")
    );
    assert_eq!(folded("MARKETING"), folded("marketing"));
    assert_eq!(folded("Straße"), folded("STRASSE"));
    assert_eq!(
        label_name(Some(&"Marketing".to_owned())),
        Some("Marketing".to_owned())
    );
    assert_eq!(label_name(Some(&String::new())), None);
    assert_eq!(label_name(None), None);
}

#[test]
fn docker_instants_are_ordered_at_nanosecond_precision_across_offsets() {
    let at = |text: &str| docker_instant_ns(text).expect(text);
    assert_eq!(at("1970-01-01T00:00:00Z"), 0);
    assert_eq!(at("1970-01-01T00:00:01.5Z"), 1_500_000_000);
    assert_eq!(
        at("2026-09-30T12:00:00.000000001Z") - at("2026-09-30T12:00:00Z"),
        1
    );
    // One instant written three ways.
    assert_eq!(at("2026-09-30T12:00:00Z"), at("2026-09-30T14:00:00+02:00"));
    assert_eq!(at("2026-09-30T12:00:00Z"), at("2026-09-29T12:01:00-23:59"));
    assert_eq!(
        at("2026-09-30T12:00:00.100Z"),
        at("2026-09-30T12:00:00.1+00:00")
    );
    assert!(at("2026-09-30T12:00:00.2Z") > at("2026-09-30T12:00:00.1Z"));
    assert_eq!(
        at("2024-02-29T00:00:00Z") - at("2024-02-28T00:00:00Z"),
        86_400_000_000_000
    );
}

#[test]
fn invalid_creation_metadata_is_refused() {
    for invalid in [
        "",
        "2026-09-30 12:00:00Z",
        "2026-09-30T12:00:00",
        "2026-09-30T12:00:00.Z",
        "2026-09-30T12:00:00.1234567890Z",
        "2026-02-30T12:00:00Z",
        "2026-09-30T12:00:00+24:00",
        "2026-09-30T12:00:00+00:60",
        "2026-09-30T12:00:00+01:99",
        "2026-09-30T12:00:00-23:60",
        "2026-09-30T12:00:00z",
        "2026-09-30T1\u{663}:00:00Z",
        "2026-09-30T12:00:00Z\n",
        "2026-09-30T24:00:00Z",
        "2026-09-30T12:00:60Z",
    ] {
        assert_eq!(docker_instant_ns(invalid), None, "{invalid:?}");
    }
}
