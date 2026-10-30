use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, UNIX_EPOCH};

use strict_json::{Number, Value};
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};
use team_core::testing::{CountingEntropy, FakeClock};

use super::{
    AuditError, AuditLog, GENESIS, Outcome, Principal, PrincipalClass, Settings, sha256_hex,
    sibling,
};

struct Fixture {
    _directory: tempfile::TempDir,
    path: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().expect("temporary directory");
        Self {
            path: directory.path().join("audit/audit.jsonl"),
            _directory: directory,
        }
    }

    fn log(&self, settings: Settings) -> AuditLog {
        AuditLog::new(
            self.path.clone(),
            settings,
            Arc::new(FakeClock::at(
                UNIX_EPOCH + Duration::from_secs(1_700_000_000),
            )),
            Arc::new(CountingEntropy::default()),
        )
    }
}

const SLOW: Settings = Settings {
    max_bytes: super::MAX_BYTES,
    group_commit: Duration::from_secs(60),
};

fn record(log: &AuditLog, operation: &str) -> String {
    log.record_blocking(&Principal::team().event(operation, Outcome::Ok))
        .expect("recorded")
}

/// Every event of the files, oldest first, after proving each line carries the SHA-256 of the line before it.
fn chained(files: &[&Path]) -> Result<Vec<Value>, String> {
    let mut previous = GENESIS.to_owned();
    let mut events = Vec::new();
    for path in files {
        let content = fs::read(path).expect("read");
        for line in content.split_inclusive(|byte| *byte == b'\n') {
            let event = strict_json::parse(line).map_err(|_| "a line is not JSON".to_owned())?;
            if event.get("prev_sha256") != Some(&Value::String(previous.clone())) {
                return Err("the audit chain is broken".to_owned());
            }
            previous = sha256_hex(line);
            events.push(event);
        }
    }
    Ok(events)
}

fn operations(events: &[Value]) -> Vec<String> {
    events
        .iter()
        .map(|event| match event.get("operation") {
            Some(Value::String(operation)) => operation.clone(),
            _ => panic!("an operation"),
        })
        .collect()
}

#[test]
fn each_line_carries_the_previous_lines_hash_across_restarts() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    record(&log, "first");
    record(&log, "second");
    log.close().expect("closed");
    let restarted = fixture.log(SLOW);
    record(&restarted, "after-restart");
    restarted.close().expect("closed");
    let events = chained(&[&fixture.path]).expect("chained");
    assert_eq!(operations(&events), ["first", "second", "after-restart"]);
    assert_eq!(
        fs::metadata(&fixture.path)
            .expect("stat")
            .permissions()
            .mode()
            & 0o7777,
        0o600
    );
}

#[test]
fn a_line_is_compact_sorted_ascii_json_with_its_principal() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let principal = Principal {
        id: Some("a".repeat(32)),
        class: PrincipalClass::Human,
        credential_state: Some("assertion_present"),
        trace_id: Some("b".repeat(32)),
    };
    let event = principal
        .event("file-acknowledgement", Outcome::Denied)
        .with("team_id", Value::String("team_1".to_owned()))
        .with("detail", Value::String("caf\u{e9}".to_owned()))
        .with("assistant", Value::Null);
    assert_eq!(log.record_blocking(&event), Ok("b".repeat(32)));
    log.close().expect("closed");
    let expected = format!(
        "{{\"credential_state\":\"assertion_present\",\"detail\":\"caf\\u00e9\",\"operation\":\"file-acknowledgement\",\
         \"prev_sha256\":\"{GENESIS}\",\"principal_class\":\"human\",\"principal_id\":\"{}\",\"result\":\"denied\",\
         \"service\":\"team-local\",\"team_id\":\"team_1\",\"trace_id\":\"{}\",\"ts\":\"2023-11-14T22:13:20Z\"}}\n",
        "a".repeat(32),
        "b".repeat(32)
    );
    assert_eq!(fs::read_to_string(&fixture.path).expect("read"), expected);
}

#[test]
fn a_rotated_in_file_starts_from_the_last_line_of_the_file_it_replaced() {
    let fixture = Fixture::new();
    let rotated = sibling(&fixture.path, 1);
    let tiny = Settings {
        max_bytes: 1,
        group_commit: Duration::from_secs(60),
    };
    let log = fixture.log(tiny);
    record(&log, "first");
    record(&log, "second");
    log.close().expect("closed");
    assert_eq!(
        operations(&chained(&[&rotated, &fixture.path]).expect("chained")),
        ["first", "second"]
    );
    // A restart after a rotation that wrote nothing yet continues from the newest rotated file.
    fs::write(&fixture.path, b"").expect("emptied");
    let log = fixture.log(SLOW);
    record(&log, "third");
    log.close().expect("closed");
    assert_eq!(
        operations(&chained(&[&rotated, &fixture.path]).expect("chained")),
        ["first", "third"]
    );
}

#[test]
fn rotation_keeps_two_files_and_shifts_them() {
    let fixture = Fixture::new();
    let tiny = Settings {
        max_bytes: 1,
        group_commit: Duration::from_secs(60),
    };
    let log = fixture.log(tiny);
    for operation in ["first", "second", "third", "fourth"] {
        record(&log, operation);
    }
    log.close().expect("closed");
    let (second, first) = (sibling(&fixture.path, 2), sibling(&fixture.path, 1));
    assert!(!sibling(&fixture.path, 3).exists());
    let events = chained(&[&second, &first, &fixture.path]);
    // The oldest file was dropped, so the retained chain starts mid-way: its first link is not the genesis.
    assert_eq!(events, Err("the audit chain is broken".to_owned()));
    let retained: Vec<String> = [&second, &first, &fixture.path]
        .iter()
        .map(|path| fs::read_to_string(path).expect("read"))
        .collect();
    assert!(retained[0].contains("\"second\""));
    assert!(retained[1].contains("\"third\""));
    assert!(retained[2].contains("\"fourth\""));
}

#[test]
fn a_torn_final_write_is_ended_and_keeps_its_place_in_the_chain() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    record(&log, "first");
    log.close().expect("closed");
    let mut torn = fs::read(&fixture.path).expect("read");
    torn.extend_from_slice(b"{\"torn\":");
    fs::write(&fixture.path, &torn).expect("written");
    record(&log, "after-crash");
    log.close().expect("closed");
    let content = fs::read(&fixture.path).expect("read");
    let lines: Vec<&[u8]> = content.split_inclusive(|byte| *byte == b'\n').collect();
    assert_eq!(lines[1], b"{\"torn\":\n");
    let next = strict_json::parse(lines[2]).expect("JSON");
    assert_eq!(
        next.get("prev_sha256"),
        Some(&Value::String(sha256_hex(lines[1])))
    );
    assert_eq!(
        next.get("operation"),
        Some(&Value::String("after-crash".to_owned()))
    );
}

#[test]
fn an_altered_removed_or_reordered_line_breaks_the_chain() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    for operation in ["first", "second", "third"] {
        record(&log, operation);
    }
    log.close().expect("closed");
    let content = fs::read_to_string(&fixture.path).expect("read");
    let lines: Vec<&str> = content.split_inclusive('\n').collect();
    let altered = lines[1].replace("\"second\"", "\"forged\"");
    for tampered in [
        [lines[0], altered.as_str(), lines[2]].concat(),
        [lines[0], lines[2]].concat(),
        [lines[1], lines[0], lines[2]].concat(),
    ] {
        fs::write(&fixture.path, tampered).expect("written");
        assert_eq!(
            chained(&[&fixture.path]).map(|_| ()),
            Err("the audit chain is broken".to_owned())
        );
    }
}

#[test]
fn an_acknowledged_event_is_written_before_its_group_sync() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    record(&log, "assistant-action");
    // Another reader sees the line at once: a process crash now keeps it; only a power loss could take it.
    assert_eq!(log.syncs(), 0);
    let events = chained(&[&fixture.path]).expect("chained");
    assert_eq!(operations(&events), ["assistant-action"]);
}

#[test]
fn multiple_events_share_one_durability_sync() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    for index in 0..8 {
        record(&log, &format!("burst-{index}"));
    }
    assert_eq!(log.syncs(), 0);
    log.flush().expect("flushed");
    assert_eq!(log.syncs(), 1);
    log.flush().expect("nothing more to sync");
    assert_eq!(log.syncs(), 1);
    record(&log, "next-group");
    log.close().expect("closed");
    assert_eq!(log.syncs(), 2);
}

#[test]
fn background_sync_bounds_the_acknowledged_loss_window() {
    let fixture = Fixture::new();
    let window = Duration::from_millis(20);
    let log = fixture.log(Settings {
        max_bytes: super::MAX_BYTES,
        group_commit: window,
    });
    let started = Instant::now();
    record(&log, "first");
    record(&log, "second");
    let deadline = Instant::now() + Duration::from_secs(5);
    while log.syncs() == 0 {
        assert!(
            Instant::now() < deadline,
            "the group commit never synchronized"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(started.elapsed() >= window);
    std::thread::sleep(window * 3);
    assert_eq!(log.syncs(), 1);
    log.close().expect("closed");
}

#[test]
fn concurrent_records_are_complete_and_share_the_writer() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let mut traces: Vec<String> = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..8)
            .map(|worker| {
                let log = &log;
                scope.spawn(move || {
                    (0..8)
                        .map(|index| record(log, &format!("concurrent-{worker}-{index}")))
                        .collect::<Vec<String>>()
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|worker| worker.join().expect("joined"))
            .collect()
    });
    log.close().expect("closed");
    let events = chained(&[&fixture.path]).expect("chained");
    assert_eq!(events.len(), 64);
    let mut written: Vec<String> = events
        .iter()
        .map(|event| match event.get("trace_id") {
            Some(Value::String(trace)) => trace.clone(),
            _ => panic!("a trace id"),
        })
        .collect();
    traces.sort();
    written.sort();
    assert_eq!(written, traces);
}

#[test]
fn unsafe_log_files_fail_closed_and_a_replaced_open_log_is_refused() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    record(&log, "first");
    // The file at the path is no longer the one the writer holds open.
    fs::remove_file(&fixture.path).expect("removed");
    fs::write(&fixture.path, b"replacement\n").expect("written");
    fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o600)).expect("mode set");
    assert_eq!(
        log.record_blocking(&Principal::team().event("second", Outcome::Ok)),
        Err(AuditError::Unwritten)
    );
    log.close().expect("closed");
    // A log readable by others is refused before anything is written to it.
    fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o640)).expect("mode set");
    assert_eq!(
        log.record_blocking(&Principal::team().event("third", Outcome::Ok)),
        Err(AuditError::Unwritten)
    );
    assert_eq!(fs::read(&fixture.path).expect("read"), b"replacement\n");
}

#[test]
fn an_oversized_open_log_is_synchronized_and_rotated() {
    let fixture = Fixture::new();
    let log = fixture.log(Settings {
        max_bytes: 300,
        group_commit: Duration::from_secs(60),
    });
    record(&log, "first");
    record(&log, "second");
    assert_eq!(log.syncs(), 0);
    record(&log, "third");
    assert_eq!(log.syncs(), 1);
    log.close().expect("closed");
    let rotated = sibling(&fixture.path, 1);
    assert_eq!(
        operations(&chained(&[&rotated, &fixture.path]).expect("chained")),
        ["first", "second", "third"]
    );
}

#[test]
fn audit_never_contains_body_or_secret() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let base = || Principal::team().event("assistant-action", Outcome::Ok);
    for field in [
        "body",
        "authorization",
        "api_key",
        "cookie",
        "value",
        "prompt",
        "headers",
    ] {
        let event = base().with(field, Value::String("sk-test-value".to_owned()));
        assert_eq!(
            log.record_blocking(&event),
            Err(AuditError::Inadmissible),
            "{field}"
        );
    }
    for (field, value) in [
        (
            "detail",
            Value::Object(vec![("body".to_owned(), Value::Null)]),
        ),
        ("team_id", Value::Number(Number::Int(1))),
        ("model_usage", Value::String("x".to_owned())),
    ] {
        assert_eq!(
            log.record_blocking(&base().with(field, value)),
            Err(AuditError::Inadmissible),
            "{field}"
        );
    }
    assert!(!fixture.path.exists());
}

fn refused_principals() -> Vec<Principal> {
    vec![
        Principal {
            id: Some("invalid".to_owned()),
            class: PrincipalClass::Human,
            credential_state: None,
            trace_id: None,
        },
        Principal {
            id: Some("Team".to_owned()),
            class: PrincipalClass::Machine,
            credential_state: None,
            trace_id: None,
        },
        Principal {
            id: Some("team-local".to_owned()),
            class: PrincipalClass::Absent,
            credential_state: None,
            trace_id: None,
        },
        Principal {
            id: None,
            class: PrincipalClass::Machine,
            credential_state: None,
            trace_id: None,
        },
        Principal {
            trace_id: Some("invalid".to_owned()),
            ..Principal::team()
        },
        Principal {
            credential_state: Some("bearer"),
            ..Principal::team()
        },
    ]
}

#[test]
fn record_validates_principal_metadata() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let refused = refused_principals();
    for principal in refused {
        assert_eq!(
            log.record_blocking(&principal.event("operation", Outcome::Ok)),
            Err(AuditError::Inadmissible),
            "{principal:?}"
        );
    }
    let absent = Principal {
        id: None,
        class: PrincipalClass::Absent,
        credential_state: Some("assertion_absent_or_malformed"),
        trace_id: None,
    };
    let unknown_result = absent
        .event("operation", Outcome::Ok)
        .with("result", Value::String("maybe".to_owned()));
    assert_eq!(
        log.record_blocking(&unknown_result),
        Err(AuditError::Inadmissible)
    );
    assert_eq!(
        log.record_blocking(&AuditEvent::new()),
        Err(AuditError::Inadmissible)
    );
    let trace = log
        .record_blocking(&absent.event("operation", Outcome::Denied))
        .expect("recorded");
    log.close().expect("closed");
    let events = chained(&[&fixture.path]).expect("chained");
    assert_eq!(events[0].get("principal_id"), Some(&Value::Null));
    assert_eq!(events[0].get("trace_id"), Some(&Value::String(trace)));
}

fn activity(members: &[(&str, Value)]) -> Value {
    Value::Object(
        members
            .iter()
            .map(|(name, value)| ((*name).to_owned(), value.clone()))
            .collect(),
    )
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn refused_activities() -> Vec<Value> {
    let event_id = ("event_id", text(&"a".repeat(32)));
    let requested = ("phase", text("requested"));
    let long_origin = format!("https://{}.example", "a".repeat(250));
    vec![
        activity(std::slice::from_ref(&requested)),
        activity(&[event_id.clone(), ("phase", text("done"))]),
        activity(&[event_id.clone(), requested.clone(), ("cookie", text("x"))]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("subject", text("Has Space")),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("generation", Value::Number(Number::Int(-1))),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("generation", Value::Number(Number::Int(1 << 31))),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("generation", Value::Bool(true)),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("origin", text("https://admin.example/path")),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("origin", text("admin")),
        ]),
        activity(&[
            event_id.clone(),
            requested.clone(),
            ("origin", text(&long_origin)),
        ]),
        text("not a mapping"),
    ]
}

#[test]
fn an_activity_event_keeps_its_closed_fields_and_refuses_anything_else() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let event = activity_event;
    let full = activity(&[
        ("event_id", text(&"a".repeat(32))),
        ("phase", text("failed")),
        ("subject", text("sign-in")),
        ("generation", Value::Number(Number::Int(2))),
        ("incarnation", text(&"c".repeat(64))),
        ("factor", text("passkey")),
        ("initiator_id", text(&"b".repeat(32))),
        ("origin", text("https://admin.example:8443")),
        ("outcome", text("sign-in-changed")),
    ]);
    log.record_blocking(&event("browser-sign-in-delete", full))
        .expect("recorded");
    for refused in refused_activities() {
        assert_eq!(
            log.record_blocking(&event("browser-sign-in-delete", refused.clone())),
            Err(AuditError::Inadmissible),
            "{refused:?}"
        );
    }
    log.close().expect("closed");
    assert_eq!(chained(&[&fixture.path]).expect("chained").len(), 1);
}

fn activity_event(operation: &str, activity: Value) -> AuditEvent {
    Principal::team()
        .event(operation, Outcome::Ok)
        .with("team_id", text("neo"))
        .with("activity", activity)
}

#[test]
fn only_a_closed_operation_is_an_activity_event() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let event = activity_event;
    let minimal = activity(&[
        ("event_id", text(&"a".repeat(32))),
        ("phase", text("completed")),
        ("origin", text("team")),
    ]);
    log.record_blocking(&event("browser-sign-in-expire", minimal.clone()))
        .expect("recorded");
    // An activity event carries only its Team beside the principal fields.
    let with_detail = event("memory-remember", minimal.clone()).with("detail", text("x"));
    assert_eq!(
        log.record_blocking(&with_detail),
        Err(AuditError::Inadmissible)
    );
    for operation in super::ACTIVITY_OPERATIONS {
        log.record_blocking(&event(operation, minimal.clone()))
            .expect("recorded");
    }
    for operation in ["browser-sign-in", "chat-memory", "memory-replace", ""] {
        assert_eq!(
            log.record_blocking(&event(operation, minimal.clone())),
            Err(AuditError::Inadmissible),
            "{operation}"
        );
    }
    log.close().expect("closed");
    let events = chained(&[&fixture.path]).expect("chained");
    assert_eq!(events.len(), 9);
}

#[tokio::test]
async fn the_sink_records_or_answers_unavailable() {
    let fixture = Fixture::new();
    let log = fixture.log(SLOW);
    let sink: &dyn AuditSink = &log;
    assert_eq!(
        sink.record(Principal::team().event("sink", Outcome::Ok))
            .await,
        Ok(())
    );
    assert_eq!(
        sink.record(
            Principal::team()
                .event("sink", Outcome::Ok)
                .with("body", text("x"))
        )
        .await,
        Err(AuditUnavailable)
    );
    log.close().expect("closed");
    assert_eq!(
        operations(&chained(&[&fixture.path]).expect("chained")),
        ["sink"]
    );
}
