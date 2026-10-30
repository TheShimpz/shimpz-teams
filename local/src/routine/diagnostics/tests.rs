use std::os::unix::fs::PermissionsExt as _;
use std::path::PathBuf;
use std::sync::Arc;

use strict_json::Value;
use team_action::failure::ActionFailure;
use team_core::entropy::{Entropy, EntropyUnavailable, OsEntropy};
use team_protocol::python::json::{CANONICAL, Options, dumps};
use team_protocol::routine_run::canonical_failure;
use team_routine::trace::Protection;
use team_storage::envelope::{Envelope, Key};
use tempfile::TempDir;

use super::view::{StepsError, last_run, run_diagnostics, run_steps};
use super::{
    Diagnostic, DiagnosticError, DiagnosticStore, Fault, RETENTION_SECONDS, RunBinding, RunRecord,
    StepRecord, aad, evidence, protected,
};
use crate::routine::testing::json;

const NOW: i64 = 2_200_000_000;
const INCARNATION: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_INCARNATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const ROUTINE: &str = "cccccccccccccccccccccccccccccccc";
const RUN: &str = "dddddddddddddddddddddddddddddddd";
const OPERATION: &str = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6";

fn failure() -> Value {
    json(
        r#"{"error_type": "httpx.HTTPStatusError", "message": "Client error '404 Not Found'",
            "provider": "api.cloudflare.com", "http_status": 404, "response_excerpt": "{\"success\":false}",
            "redacted": false, "truncated": false}"#,
    )
}

fn diagnostic() -> Diagnostic {
    Diagnostic {
        routine_id: ROUTINE.to_owned(),
        run_id: RUN.to_owned(),
        operation_id: OPERATION.to_owned(),
        attempt: 1,
        assistant_id: "shimpz-cloudflare".to_owned(),
        action: "replace-dns-record".to_owned(),
        position: 1,
        recorded_at: NOW,
        failure: Some(failure()),
        condition: None,
    }
}

fn binding() -> RunBinding {
    RunBinding {
        routine_id: ROUTINE.to_owned(),
        run_id: RUN.to_owned(),
        revision: 2,
        plan_digest: format!("sha256:{}", "f".repeat(64)),
        total: 4,
    }
}

fn step(position: i64, status: &str) -> StepRecord {
    StepRecord {
        binding: binding(),
        position,
        assistant_id: "shimpz-cloudflare".to_owned(),
        action: "list-zones".to_owned(),
        status: status.to_owned(),
        attempt: 1,
        duration_ms: Some(812),
        recorded_at: NOW,
        inputs: Some(json(r#"[{"member": "page", "value": "1"}]"#)),
    }
}

fn run_record(reached: i64, dispatched: bool, recorded_at: i64) -> RunRecord {
    RunRecord {
        binding: binding(),
        reached,
        dispatched,
        recorded_at,
    }
}

/// Fills every byte with its index, so nonces and keys are known.
struct Counting;

impl Entropy for Counting {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        for (index, byte) in bytes.iter_mut().enumerate() {
            *byte = u8::try_from(index % 256).unwrap_or(0);
        }
        Ok(())
    }
}

struct Case {
    _directory: TempDir,
    root: PathBuf,
    store: DiagnosticStore,
}

fn case_with(entropy: Arc<dyn Entropy>) -> Case {
    let directory = TempDir::new().expect("a directory");
    let root = directory.path().to_path_buf();
    let store = DiagnosticStore::new(
        &root.join("bodies"),
        &root.join("key/diagnostics.key"),
        entropy,
    )
    .expect("a store");
    Case {
        _directory: directory,
        root,
        store,
    }
}

fn case() -> Case {
    case_with(Arc::new(OsEntropy))
}

impl Case {
    fn files(&self, team: &str) -> Vec<PathBuf> {
        let directory = self.store.team_dir(team);
        let mut files: Vec<PathBuf> = match std::fs::read_dir(&directory) {
            Ok(listed) => listed
                .map(|entry| entry.expect("an entry").path())
                .collect(),
            Err(_) => Vec::new(),
        };
        files.sort();
        files
    }

    fn only_file(&self) -> PathBuf {
        let files = self.files("team_1");
        assert_eq!(files.len(), 1);
        files[0].clone()
    }

    fn record(&self, item: &Diagnostic) {
        self.store
            .record("team_1", INCARNATION, item, &[])
            .expect("recorded");
    }

    fn record_step(&self, item: &StepRecord) {
        self.store
            .record_step("team_1", INCARNATION, item, &[])
            .expect("recorded");
    }

    fn read(&self, now: i64) -> Result<Vec<Diagnostic>, DiagnosticError> {
        self.store.read("team_1", INCARNATION, RUN, now)
    }

    fn page(&self, snapshot: &str, offset: i64) -> Result<Value, StepsError> {
        run_steps(
            &self.store,
            "team_1",
            INCARNATION,
            RUN,
            snapshot,
            offset,
            NOW + 10,
        )
    }

    fn last(&self, incarnation: &str, now: i64) -> Option<Value> {
        last_run(&self.store, "team_1", incarnation, ROUTINE, now)
    }
}

/// The diagnostics as their canonical wire views (a body read back has its members in sorted order).
fn views(items: &[Diagnostic]) -> Vec<String> {
    items
        .iter()
        .map(|item| dumps(&item.view(), CANONICAL))
        .collect()
}

fn statuses(page: &Value) -> Vec<String> {
    let Some(Value::Array(steps)) = page.get("steps") else {
        return Vec::new();
    };
    steps
        .iter()
        .map(|step| match step.get("status") {
            Some(Value::String(status)) => status.clone(),
            _ => String::new(),
        })
        .collect()
}

fn snapshot(page: &Value) -> String {
    match page.get("snapshot") {
        Some(Value::String(snapshot)) => snapshot.clone(),
        _ => panic!("a snapshot"),
    }
}

/// Every body is private, within its bound, and shows nothing of what it seals.
fn assert_private_and_opaque(case: &Case) {
    for path in case.files("team_1") {
        assert_eq!(
            std::fs::metadata(&path)
                .expect("metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        let raw = String::from_utf8(std::fs::read(&path).expect("read")).expect("ascii");
        assert!(
            raw.len() <= super::MAX_FILE_BYTES
                && !raw.contains("httpx")
                && !raw.contains("cloudflare")
        );
    }
}

#[test]
fn a_sealed_body_reads_back_only_for_its_exact_run_in_order() {
    let case = case();
    let later = Diagnostic {
        attempt: 2,
        recorded_at: NOW + 5,
        failure: None,
        condition: Some("exit-status:1".to_owned()),
        ..diagnostic()
    };
    let other_run = Diagnostic {
        run_id: "e".repeat(32),
        ..diagnostic()
    };
    for item in [&later, &diagnostic(), &other_run] {
        case.store
            .record(
                "team_1",
                INCARNATION,
                item,
                &["never-stored-secret".to_owned()],
            )
            .expect("recorded");
    }
    assert_eq!(
        views(&case.read(NOW + 10).expect("read")),
        views(&[diagnostic(), later])
    );
    let view = run_diagnostics(&case.store, "team_1", INCARNATION, RUN, NOW + 10).expect("a view");
    let Some(Value::Array(diagnostics)) = view.get("diagnostics") else {
        panic!("diagnostics")
    };
    assert_eq!(
        diagnostics[0].get("recorded_at"),
        Some(&json("\"2039-09-18T23:06:40Z\""))
    );
    assert_private_and_opaque(&case);
}

#[test]
fn a_sealed_body_is_the_exact_aes_gcm_envelope_of_its_payload() {
    let case = case_with(Arc::new(Counting));
    case.record(&diagnostic());
    let sealed = case.only_file();
    let name = sealed
        .file_name()
        .and_then(|name| name.to_str())
        .expect("a name")
        .to_owned();
    let key: [u8; 32] = std::fs::read(case.root.join("key/diagnostics.key"))
        .expect("key")
        .try_into()
        .expect("32 bytes");
    let payload = dumps(&diagnostic().view(), CANONICAL);
    let aad = aad("team_1", INCARNATION, &name);
    let expected =
        Envelope::seal(&Key::from_bytes(key), payload.as_bytes(), &aad, &Counting).expect("sealed");
    let Value::Object(mut members) = expected.to_value() else {
        unreachable!()
    };
    members.push((
        "incarnation".to_owned(),
        Value::String(INCARNATION.to_owned()),
    ));
    let written = String::from_utf8(std::fs::read(&sealed).expect("read")).expect("ascii");
    assert_eq!(written, dumps(&Value::Object(members), CANONICAL));
    assert!(written.starts_with(r#"{"algorithm":"AES-256-GCM","ciphertext":"#));
}

#[test]
fn aad_binds_team_incarnation_and_the_bodys_name() {
    let case = case();
    case.record(&diagnostic());
    // An authentic body of another incarnation is left out; it is never shown, and never mistaken for corruption.
    assert_eq!(
        case.store.read("team_1", OTHER_INCARNATION, RUN, NOW),
        Ok(Vec::new())
    );
    let sealed = case.only_file();
    let name = sealed
        .file_name()
        .and_then(|name| name.to_str())
        .expect("a name")
        .to_owned();
    let moved = case.store.team_dir("team_2");
    std::fs::create_dir_all(&moved).expect("a directory");
    std::fs::copy(&sealed, moved.join(&name)).expect("copied");
    assert_eq!(
        case.store.read("team_2", INCARNATION, RUN, NOW),
        Err(DiagnosticError::Unauthentic)
    );
    for renamed in [
        name.replace(".1.diagnostic", ".2.diagnostic"),
        name.replace(OPERATION, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7"),
        name.replace(&NOW.to_string(), &(NOW + 1).to_string()),
    ] {
        let target = sealed.with_file_name(&renamed);
        std::fs::copy(&sealed, &target).expect("copied");
        assert_eq!(
            case.read(NOW + 2),
            Err(DiagnosticError::Unauthentic),
            "{renamed}"
        );
        std::fs::remove_file(&target).expect("removed");
    }
    assert_eq!(
        views(&case.read(NOW + 2).expect("read")),
        views(&[diagnostic()])
    );
}

fn rewrite(path: &PathBuf, change: impl FnOnce(&mut Vec<(String, Value)>)) {
    let Value::Object(mut members) =
        strict_json::parse(&std::fs::read(path).expect("read")).expect("json")
    else {
        unreachable!()
    };
    change(&mut members);
    std::fs::write(path, dumps(&Value::Object(members), CANONICAL)).expect("written");
}

fn relabel(path: &PathBuf, incarnation: &str) {
    rewrite(path, |members| {
        for (name, value) in members.iter_mut() {
            if name == "incarnation" {
                *value = json(incarnation);
            }
        }
    });
}

#[test]
fn a_tampered_or_malformed_body_never_reads_as_evidence() {
    let case = case();
    case.record(&diagnostic());
    let sealed = case.only_file();
    let original = std::fs::read(&sealed).expect("read");
    // A body relabeled to look foreign fails authentication for every reader.
    relabel(&sealed, &format!("\"{OTHER_INCARNATION}\""));
    for reader in [INCARNATION, OTHER_INCARNATION] {
        assert_eq!(
            case.store.read("team_1", reader, RUN, NOW),
            Err(DiagnosticError::Unauthentic)
        );
    }
    for malformed in [r#"{"algorithm": "AES-128-GCM"}"#, "not json"] {
        std::fs::write(&sealed, malformed).expect("written");
        assert!(case.read(NOW).is_err());
    }
    for incarnation in ["\"AAAA\"", "null"] {
        std::fs::write(&sealed, &original).expect("written");
        relabel(&sealed, incarnation);
        assert_eq!(case.read(NOW), Err(DiagnosticError::Malformed));
    }
    std::fs::write(&sealed, &original).expect("written");
    rewrite(&sealed, |members| {
        members.retain(|(name, _)| name != "incarnation");
    });
    assert_eq!(case.read(NOW), Err(DiagnosticError::Malformed));
}

#[test]
fn a_directory_where_a_body_belongs_fails_closed() {
    let case = case();
    case.record(&diagnostic());
    let sealed = case.only_file();
    std::fs::remove_file(&sealed).expect("removed");
    std::fs::create_dir(&sealed).expect("a directory where a body belongs");
    assert_eq!(case.read(NOW), Err(DiagnosticError::Unsafe));
}

#[test]
fn bodies_expire_after_seven_days_and_the_same_attempt_replaces_its_body() {
    let case = case();
    case.record(&Diagnostic {
        operation_id: "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7".to_owned(),
        ..diagnostic()
    });
    case.record(&Diagnostic {
        recorded_at: NOW + 1,
        ..diagnostic()
    });
    case.record(&Diagnostic {
        recorded_at: NOW + 2,
        condition: Some("timeout".to_owned()),
        failure: None,
        ..diagnostic()
    });
    assert_eq!(case.files("team_1").len(), 2);
    let expired = NOW + RETENTION_SECONDS;
    let read = case.read(expired).expect("read");
    assert_eq!(
        read.iter()
            .map(|item| item.condition.clone())
            .collect::<Vec<_>>(),
        vec![Some("timeout".to_owned())]
    );
    case.record(&Diagnostic {
        attempt: 2,
        recorded_at: expired + 2,
        ..diagnostic()
    });
    assert_eq!(case.files("team_1").len(), 1);
}

#[test]
fn a_team_keeps_its_newest_bodies_within_its_byte_bound() {
    let mut case = case();
    for index in 0..4 {
        case.record(&Diagnostic {
            attempt: index + 1,
            recorded_at: NOW + index,
            ..diagnostic()
        });
    }
    let size = std::fs::metadata(&case.files("team_1")[0])
        .expect("metadata")
        .len();
    case.store
        .record("team_2", INCARNATION, &diagnostic(), &[])
        .expect("recorded");
    case.store.max_team_bytes = size * 2 + 1;
    case.record(&Diagnostic {
        attempt: 5,
        recorded_at: NOW + 9,
        ..diagnostic()
    });
    let read = case.read(NOW + 9).expect("read");
    assert_eq!(
        read.iter().map(|item| item.attempt).collect::<Vec<_>>(),
        vec![4, 5]
    );
    assert_eq!(case.files("team_2").len(), 1);
}

#[test]
fn only_a_bounded_secret_free_diagnostic_is_ever_sealed() {
    let case = case();
    let refused = [
        (
            Diagnostic {
                failure: None,
                ..diagnostic()
            },
            INCARNATION,
            DiagnosticError::InvalidRecord,
        ),
        (
            Diagnostic {
                routine_id: "x".to_owned(),
                ..diagnostic()
            },
            INCARNATION,
            DiagnosticError::InvalidRecord,
        ),
        (
            Diagnostic {
                run_id: "x".to_owned(),
                ..diagnostic()
            },
            INCARNATION,
            DiagnosticError::InvalidRecord,
        ),
        (diagnostic(), "AAAA", DiagnosticError::InvalidIncarnation),
    ];
    for (item, incarnation, error) in refused {
        assert_eq!(
            case.store.record("team_1", incarnation, &item, &[]),
            Err(error)
        );
    }
    let protected_values = [String::new(), "404 Not Found".to_owned()];
    assert_eq!(
        case.store
            .record("team_1", INCARNATION, &diagnostic(), &protected_values),
        Err(DiagnosticError::Protected)
    );
    assert_eq!(case.files("team_1"), Vec::<PathBuf>::new());
    assert_eq!(
        case.store.read("team_1", INCARNATION, "x", NOW),
        Err(DiagnosticError::InvalidRun)
    );
}

#[test]
fn the_keyring_must_live_outside_the_bodies() {
    let case = case();
    let bodies = case.root.join("bodies");
    assert_eq!(
        DiagnosticStore::new(
            &bodies,
            &bodies.join("diagnostics.key"),
            Arc::new(OsEntropy)
        )
        .err(),
        Some(DiagnosticError::Layout)
    );
}

#[test]
fn deleting_a_routine_a_team_or_the_space_removes_exactly_their_bodies() {
    let case = case();
    case.record(&diagnostic());
    case.record(&Diagnostic {
        routine_id: "f".repeat(32),
        run_id: "e".repeat(32),
        ..diagnostic()
    });
    case.store
        .record("team_2", INCARNATION, &diagnostic(), &[])
        .expect("recorded");
    case.store
        .delete_routine("team_1", ROUTINE)
        .expect("deleted");
    assert_eq!(case.files("team_1").len(), 1);
    case.store.delete("team_1").expect("deleted");
    case.store.delete("team_1").expect("already deleted");
    assert!(!case.store.team_dir("team_1").exists());
    assert_eq!(case.files("team_2").len(), 1);
    std::fs::write(case.root.join("bodies/unrelated"), b"kept").expect("written");
    case.store.delete_all().expect("deleted");
    case.store.delete_all().expect("already deleted");
    assert!(!case.root.join("key/diagnostics.key").exists());
    let left: Vec<String> = std::fs::read_dir(case.root.join("bodies"))
        .expect("listed")
        .map(|entry| {
            entry
                .expect("an entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    assert_eq!(left, vec!["unrelated".to_owned()]);
}

#[test]
fn the_family_directory_is_private_and_fails_closed_when_it_is_not() {
    let case = case();
    case.record(&diagnostic());
    let bodies = case.root.join("bodies");
    assert_eq!(
        std::fs::metadata(&bodies)
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    std::fs::set_permissions(&bodies, std::fs::Permissions::from_mode(0o755)).expect("chmod");
    let second = Diagnostic {
        attempt: 2,
        ..diagnostic()
    };
    assert_eq!(
        case.store.record("team_1", INCARNATION, &second, &[]),
        Err(DiagnosticError::Unsafe)
    );
    assert_eq!(case.files("team_1").len(), 1);
}

#[test]
fn a_run_shows_each_steps_latest_record_and_proves_which_never_started() {
    let case = case();
    case.record_step(&StepRecord {
        duration_ms: Some(10),
        ..step(1, "failed")
    });
    case.record_step(&StepRecord {
        attempt: 2,
        ..step(1, "done")
    });
    case.record_step(&StepRecord {
        duration_ms: None,
        inputs: None,
        ..step(2, "recovered")
    });
    // A live run proves nothing about the steps it has no record of.
    let live = case.page("latest", 0).expect("a page");
    assert_eq!(
        statuses(&live),
        ["done", "recovered", "unavailable", "unavailable"]
    );
    // Its terminal record proves the steps after the one it reached and dispatched never started.
    case.store
        .record_run("team_1", INCARNATION, &run_record(2, true, NOW + 1))
        .expect("recorded");
    let ended = case.page("latest", 0).expect("a page");
    assert_eq!(
        statuses(&ended),
        ["done", "recovered", "unavailable", "not_run"]
    );
    case.store
        .record_run("team_1", INCARNATION, &run_record(2, false, NOW + 2))
        .expect("recorded");
    assert_eq!(
        statuses(&case.page("latest", 0).expect("a page"))[2..],
        ["not_run", "not_run"]
    );
    assert_eq!(
        (ended.get("revision"), ended.get("total")),
        (Some(&json("2")), Some(&json("4")))
    );
}

#[test]
fn a_terminal_record_past_its_steps_is_refused() {
    let case = case();
    assert_eq!(
        case.store
            .record_run("team_1", INCARNATION, &run_record(5, true, NOW)),
        Err(DiagnosticError::InvalidRecord)
    );
}

#[test]
fn a_page_names_its_snapshot_and_a_changed_record_set_refuses_the_next_page() {
    let case = case();
    case.record_step(&step(1, "done"));
    let first = snapshot(&case.page("latest", 0).expect("a page"));
    assert_eq!(snapshot(&case.page(&first, 0).expect("a page")), first);
    case.record_step(&step(2, "done"));
    assert_eq!(case.page(&first, 0), Err(StepsError::Changed));
    let current = snapshot(&case.page("latest", 0).expect("a page"));
    // Expiry changes the retained set too.
    let expired = run_steps(
        &case.store,
        "team_1",
        INCARNATION,
        RUN,
        &current,
        0,
        NOW + RETENTION_SECONDS,
    );
    assert_eq!(expired, Err(StepsError::Changed));
}

#[test]
fn records_of_disagreeing_revisions_or_none_at_all_never_show() {
    let case = case();
    assert_eq!(case.page("latest", 0), Err(StepsError::NotFound));
    case.record_step(&step(1, "done"));
    assert_eq!(case.page("latest", 4), Err(StepsError::NotFound));
    case.record_step(&StepRecord {
        binding: RunBinding {
            revision: 3,
            ..binding()
        },
        ..step(2, "done")
    });
    assert_eq!(case.page("latest", 0), Err(StepsError::Unavailable));
    case.store
        .record_step("team_2", OTHER_INCARNATION, &step(1, "done"), &[])
        .expect("recorded");
    let foreign = run_steps(
        &case.store,
        "team_2",
        INCARNATION,
        RUN,
        "latest",
        0,
        NOW + 10,
    );
    assert_eq!(foreign, Err(StepsError::NotFound));
}

#[test]
fn only_a_valid_secret_free_step_record_is_ever_sealed() {
    let case = case();
    let invalid = [
        step(5, "done"),
        step(1, "not_run"),
        step(1, "unknown"),
        StepRecord {
            attempt: 0,
            ..step(1, "done")
        },
        StepRecord {
            binding: RunBinding {
                plan_digest: "x".to_owned(),
                ..binding()
            },
            ..step(1, "done")
        },
        StepRecord {
            inputs: Some(json(
                r#"[{"member": "page", "value": "1", "secret": true}]"#,
            )),
            ..step(1, "done")
        },
    ];
    for item in invalid {
        assert_eq!(
            case.store.record_step("team_1", INCARNATION, &item, &[]),
            Err(DiagnosticError::InvalidRecord)
        );
    }
    assert_eq!(
        case.store.record_step(
            "team_1",
            INCARNATION,
            &step(1, "done"),
            &["list-zones".to_owned()]
        ),
        Err(DiagnosticError::Protected)
    );
}

#[test]
fn a_protected_value_is_found_however_the_body_escapes_it() {
    let case = case();
    let ascii = Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    // As JSON text, or as a preview's escapes of an invisible character.
    for value in ["a\"b", "a\nb", "a\u{200b}b"] {
        let once = dumps(&Value::String(value.to_owned()), ascii);
        let shown = Value::String(once[1..once.len() - 1].to_owned());
        let inputs = format!(
            r#"[{{"member": "page", "value": {}}}]"#,
            dumps(&shown, CANONICAL)
        );
        let escaped = StepRecord {
            inputs: Some(json(&inputs)),
            ..step(1, "done")
        };
        let result = case
            .store
            .record_step("team_1", INCARNATION, &escaped, &[value.to_owned()]);
        assert_eq!(result, Err(DiagnosticError::Protected), "{value:?}");
    }
}

#[test]
fn deleting_a_routine_removes_its_records_and_failure_diagnostics_never_read_a_step() {
    let case = case();
    case.record_step(&step(1, "done"));
    case.store
        .record_run("team_1", INCARNATION, &run_record(1, false, NOW))
        .expect("recorded");
    case.record(&diagnostic());
    assert_eq!(case.files("team_1").len(), 3);
    case.store
        .delete_routine("team_1", ROUTINE)
        .expect("deleted");
    assert_eq!(case.files("team_1").len(), 0);
    case.record_step(&step(1, "done"));
    assert_eq!(case.read(NOW + 10), Ok(Vec::new()));
}

#[test]
fn a_record_that_does_not_open_fails_the_page_closed_and_the_latest_sequence_wins() {
    let case = case();
    case.record_step(&StepRecord {
        recorded_at: NOW + 5,
        ..step(1, "failed")
    });
    case.record_step(&step(1, "done"));
    assert_eq!(
        statuses(&case.page("latest", 0).expect("a page"))[0],
        "done"
    );
    let path = case.files("team_1")[0].clone();
    rewrite(&path, |members| {
        for (name, value) in members.iter_mut() {
            if name == "nonce"
                && let Value::String(nonce) = value
            {
                *nonce = nonce.chars().rev().collect();
            }
        }
    });
    assert_eq!(case.page("latest", 0), Err(StepsError::Unavailable));
}

#[test]
fn the_latest_run_shows_each_steps_outcome_with_the_revision_it_executed() {
    let case = case();
    case.record_step(&StepRecord {
        binding: RunBinding {
            run_id: "e".repeat(32),
            revision: 1,
            ..binding()
        },
        recorded_at: NOW - 5,
        ..step(1, "done")
    });
    case.record_step(&step(1, "done"));
    case.record_step(&StepRecord {
        recorded_at: NOW + 1,
        ..step(2, "failed")
    });
    case.store
        .record_run("team_1", INCARNATION, &run_record(1, true, NOW + 2))
        .expect("recorded");
    assert_eq!(
        case.last(INCARNATION, NOW + 10),
        Some(json(
            r#"{"revision": 2, "steps": [{"position": 1, "status": "done"}, {"position": 2, "status": "failed"},
                {"position": 3, "status": "not_run"}, {"position": 4, "status": "not_run"}]}"#
        ))
    );
}

#[test]
fn two_runs_in_the_newest_second_or_unavailable_evidence_name_no_latest_run() {
    let case = case();
    assert_eq!(case.last(INCARNATION, NOW + 10), None);
    case.record_step(&StepRecord {
        binding: RunBinding {
            run_id: "e".repeat(32),
            ..binding()
        },
        ..step(1, "done")
    });
    case.record_step(&step(1, "done"));
    assert_eq!(case.last(INCARNATION, NOW + 10), None);
    case.record_step(&StepRecord {
        inputs: None,
        recorded_at: NOW + 1,
        ..step(2, "done")
    });
    let last = case.last(INCARNATION, NOW + 10).expect("a latest run");
    assert_eq!(
        statuses(&last),
        ["done", "unavailable", "unavailable", "unavailable"]
    );
    assert_eq!(case.last(OTHER_INCARNATION, NOW + 10), None);
    assert_eq!(case.last(INCARNATION, NOW + RETENTION_SECONDS + 2), None);
}

fn action_failure(message: &str, excerpt: &str) -> ActionFailure {
    ActionFailure {
        error_type: "httpx.HTTPStatusError".to_owned(),
        message: message.to_owned(),
        provider: Some("api.cloudflare.com".to_owned()),
        http_status: Some(404),
        response_excerpt: Some(excerpt.to_owned()),
        redacted: false,
        truncated: false,
    }
}

#[test]
fn a_failure_is_redacted_against_the_runs_whole_protection() {
    let echoed = action_failure("Bad key SchemaSecretAbc rejected", "SCHEMASECRETABC");
    let protection = Protection::default().grow(["SchemaSecretAbc"]);
    let (failure, condition) = evidence(Fault::Failure(&echoed), &protection).expect("evidence");
    let failure = failure.expect("a failure");
    assert_eq!(condition, None);
    assert!(
        !dumps(&failure, CANONICAL)
            .to_lowercase()
            .contains("schemasecretabc")
    );
    assert_eq!(failure.get("redacted"), Some(&Value::Bool(true)));
    assert!(canonical_failure(&failure).is_some());
}

#[test]
fn after_the_run_lost_its_protection_only_the_status_is_kept() {
    let lost = Protection {
        lost: true,
        ..Protection::default()
    };
    let (withheld, _condition) =
        evidence(Fault::Failure(&action_failure("x", "y")), &lost).expect("evidence");
    assert_eq!(
        withheld,
        Some(json(
            r#"{"error_type": "withheld", "message": "", "provider": null, "http_status": 404,
                "response_excerpt": null, "redacted": true, "truncated": false}"#
        ))
    );
    let free = Protection::default();
    assert_eq!(evidence(Fault::Other, &free), None);
    assert_eq!(
        evidence(Fault::Condition("timeout"), &free),
        Some((None, Some("timeout".to_owned())))
    );
    assert_eq!(evidence(Fault::Condition("free text"), &free), None);
}

#[test]
fn every_injected_value_is_protected() {
    let integrations =
        json(r#"{"cloudflare": {"access_token": "token-one", "scopes": ["zone:read"]}}"#);
    let mut found = protected(["stored-one"], &integrations);
    found.sort();
    assert_eq!(found, ["stored-one", "token-one", "zone:read"]);
}
