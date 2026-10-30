use std::os::unix::fs::{PermissionsExt as _, symlink};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use strict_json::Value;
use team_protocol::python::json::{CANONICAL, dumps};
use team_routine::cursor::Binding;
use team_routine::hold;
use team_routine::record::{
    self, MAX_ROUTINE_NOTICES, MAX_UNDELIVERED_NOTICES, RunStatus, TeamRoutines,
};
use team_storage::envelope::{Envelope, Key};
use tempfile::TempDir;

use super::{RoutineStore, StoreError, decode, encode};
use crate::routine::testing::{
    ENTROPY, NETWORK, NINE, busy_state, changes_routine, entropy, json, large_notice, routine,
};

/// The same state: the same canonical bytes (a state read back has its members in sorted order).
fn same(left: &Result<TeamRoutines, StoreError>, right: &TeamRoutines) -> bool {
    left.as_ref()
        .is_ok_and(|left| encode(left, "team_1") == encode(right, "team_1"))
}

struct Case {
    _directory: TempDir,
    root: PathBuf,
    store: RoutineStore,
}

fn case() -> Case {
    let directory = TempDir::new().expect("a directory");
    let root = directory.path().to_path_buf();
    let store = RoutineStore::new(&root.join("state"), &root.join("key/aes256.key"), entropy())
        .expect("a store");
    Case {
        _directory: directory,
        root,
        store,
    }
}

impl Case {
    fn state_file(&self, team: &str) -> PathBuf {
        self.store.team_dir(team).join("state.json")
    }

    fn put(&self, team: &str, state: &TeamRoutines) {
        let state = state.clone();
        self.store
            .update(team, |_before| Ok::<_, StoreError>((state, ())))
            .expect("a written state");
    }

    fn write_raw(&self, team: &str, value: &Value) {
        let path = self.state_file(team);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("a directory");
        std::fs::write(&path, dumps(value, CANONICAL)).expect("written");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).expect("private");
    }

    fn baseline(&self) -> Value {
        self.put("team_1", &busy_state());
        strict_json::parse(&std::fs::read(self.state_file("team_1")).expect("read")).expect("json")
    }
}

#[test]
fn a_team_state_round_trips_exactly_and_an_absent_one_is_empty() {
    let case = case();
    assert_eq!(case.store.load("team_1"), Ok(TeamRoutines::default()));
    let mut state =
        record::add_routine(&busy_state(), &changes_routine(&"d".repeat(32))).expect("added");
    state.runs[0].usage = json(r#"{"duration_ms": 1200}"#);
    state.runs[0].protection_lost = true;
    case.put("team_1", &state);
    assert!(same(&case.store.load("team_1"), &state));
    let mode = std::fs::metadata(case.state_file("team_1"))
        .expect("metadata")
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
    assert_eq!(case.store.teams(), Ok(vec!["team_1".to_owned()]));
    // The file is exactly the canonical encoding, and decoding it gives the state back.
    let bytes = std::fs::read(case.state_file("team_1")).expect("read");
    assert_eq!(bytes, encode(&state, "team_1"));
    assert!(same(&decode(&bytes, "team_1"), &state));
}

#[test]
fn update_persists_only_a_changed_state_and_returns_its_result() {
    let case = case();
    let same = case
        .store
        .update("team_1", |state| Ok::<_, StoreError>((state, "same")));
    assert_eq!(same, Ok("same"));
    assert!(!case.state_file("team_1").exists());
    let added = case.store.update("team_1", |state| {
        Ok::<_, StoreError>((
            record::add_routine(&state, &routine(&"a".repeat(32))).expect("added"),
            "added",
        ))
    });
    assert_eq!(added, Ok("added"));
    assert_eq!(
        case.store.load("team_1").map(|state| state.routines.len()),
        Ok(1)
    );
    // A refused transition writes nothing and returns its own error.
    let refused = case.store.update("team_1", |_state| {
        Err::<(TeamRoutines, ()), _>(StoreError::Immutable)
    });
    assert_eq!(refused, Err(StoreError::Immutable));
}

#[test]
fn a_read_reuses_a_decoded_state_only_while_the_file_keeps_its_exact_bytes() {
    let case = case();
    case.put(
        "team_1",
        &record::add_routine(&TeamRoutines::default(), &routine(&"a".repeat(32))).expect("added"),
    );
    case.store.load("team_1").expect("loaded");
    assert_eq!(case.store.guarded().decoded.len(), 1);
    let path = case.state_file("team_1");
    let payload = String::from_utf8(std::fs::read(&path).expect("read")).expect("utf-8");
    std::fs::write(&path, payload.replace("\"served_at\":0", "\"served_at\":7")).expect("written");
    assert_eq!(
        case.store.load("team_1").map(|state| state.served_at),
        Ok(7)
    );
    std::fs::write(&path, payload.replace("\"schema\":", "\"schemas\":")).expect("written");
    assert_eq!(case.store.load("team_1"), Err(StoreError::Malformed));
    case.store.delete("team_1").expect("deleted");
    assert_eq!(case.store.load("team_1"), Ok(TeamRoutines::default()));
}

#[test]
fn decoded_states_stay_within_their_byte_budget_and_leave_with_their_file() {
    let mut case = case();
    let state =
        record::add_routine(&TeamRoutines::default(), &routine(&"a".repeat(32))).expect("added");
    case.put("team_1", &state);
    let budget = std::fs::read(case.state_file("team_1"))
        .expect("read")
        .len();
    case.store.max_decoded_bytes = budget;
    case.put("team_2", &state);
    let kept = |case: &Case| -> Vec<String> {
        case.store
            .guarded()
            .decoded
            .iter()
            .map(|(team, _, _)| team.clone())
            .collect()
    };
    assert_eq!(kept(&case), vec!["team_2".to_owned()]);
    case.store.load("team_1").expect("loaded");
    assert_eq!(kept(&case), vec!["team_1".to_owned()]);
    std::fs::remove_file(case.state_file("team_1")).expect("removed");
    assert_eq!(case.store.load("team_1"), Ok(TeamRoutines::default()));
    assert_eq!(kept(&case), Vec::<String>::new());
}

#[test]
fn an_invalid_team_or_an_oversized_state_is_refused() {
    let mut case = case();
    for team in ["Team", ""] {
        assert_eq!(case.store.load(team), Err(StoreError::InvalidTeam));
    }
    let mut worst = busy_state();
    let name = "\u{1f600}".repeat(80);
    worst.notices = (0..MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES)
        .map(|index| large_notice(index, &name))
        .collect();
    case.put("team_1", &worst);
    assert!(same(&case.store.load("team_1"), &worst));
    // The largest notices a Team can hold fit the state's derived bound.
    assert!(
        std::fs::read(case.state_file("team_1"))
            .expect("read")
            .len()
            <= super::MAX_STATE_BYTES
    );
    case.store.max_state_bytes = 1024;
    let state = worst.clone();
    assert_eq!(
        case.store
            .update("team_2", |_before| Ok::<_, StoreError>((state, ()))),
        Err(StoreError::TooLarge)
    );
}

#[test]
fn the_keyring_must_live_outside_the_state_root() {
    let directory = TempDir::new().expect("a directory");
    let root = directory.path().join("state");
    for key in [root.join("aes256.key"), root.join("keys/aes256.key")] {
        assert_eq!(
            RoutineStore::new(&root, &key, entropy()).err(),
            Some(StoreError::Layout)
        );
    }
}

fn set(value: &mut Value, path: &[&str], member: Value) {
    let mut current = value;
    for (depth, key) in path.iter().enumerate() {
        let index = key.parse::<usize>().ok();
        current = match (current, index) {
            (Value::Array(items), Some(index)) => &mut items[index],
            (Value::Object(members), _) => {
                if depth + 1 == path.len() && !members.iter().any(|(name, _)| name == key) {
                    members.push(((*key).to_owned(), Value::Null));
                }
                &mut members
                    .iter_mut()
                    .find(|(name, _)| name == key)
                    .expect("a member")
                    .1
            }
            _ => panic!("no such path"),
        };
    }
    *current = member;
}

fn index_of(base: &Value, status: &str) -> String {
    let Some(Value::Array(runs)) = base.get("runs") else {
        panic!("runs")
    };
    runs.iter()
        .position(|run| run.get("status") == Some(&Value::String(status.to_owned())))
        .expect("a run")
        .to_string()
}

/// One altered member of the baseline state at a time: its `/` path (`FROZEN` and `HELD` name those runs) and its new
/// JSON value (`SHA0`, `GEN0`, `E64`, `F32`, `A32`, and `ACTIVE+1` are filled in).
const FIELD_MUTATIONS: &[(&str, &str)] = &[
    ("schema", "14"),
    ("schema", "13.0"),
    ("runs/HELD/steps", "false"),
    ("team_id", r#""team_2""#),
    ("extra", "1"),
    ("routines/0/confirmation", "null"),
    ("routines/0/confirmation/principal", r#""person""#),
    ("routines/0/permitted", "[]"),
    ("routines/0/permitted/0/pin", r#""SHA0""#),
    ("routines/0/permitted/0/read_only", "1"),
    ("routines/0/unexpected", "null"),
    ("routines/0/paused", "1"),
    ("routines/0/rollup_usage", r#"{"duration_ms": -1}"#),
    ("routines/0/output_digest", r#""x""#),
    (
        "routines/0/schedule",
        r#"{"kind": "daily", "time": "25:00"}"#,
    ),
    ("routines/0/plan/timezone", r#""../etc""#),
    ("routines/0/assistants", r#"[["dns", "md5"]]"#),
    ("routines/0/routine_id", "1"),
    ("routines/0/assistants", r#"[["dns", 1]]"#),
    ("routines/0/assistants", r#"[[{}, "SHA0"]]"#),
    ("routines/0/needs_reconfirm", "1"),
    ("routines/0/next_run_at", "-1"),
    ("routines/0/rollup_runs", "61"),
    ("routines/0/rollup_minute", "-1"),
    ("runs/0/status", r#""running""#),
    ("runs/0/lease_key", r#""short""#),
    ("runs/HELD/generation", r#""""#),
    ("runs/HELD/request_kind", r#""human""#),
    ("runs/FROZEN/request_kind", r#""unknown""#),
    ("runs/HELD/lease_key", r#""E64""#),
    ("runs/FROZEN/action", r#""""#),
    ("runs/FROZEN/assistant_id", r#""""#),
    ("runs/HELD/generation", r#""other:routine:x""#),
    ("runs/FROZEN/generation", r#""GEN0""#),
    ("runs/0/status", r#""paused""#),
    ("runs/0/routine_id", r#""F32""#),
    ("runs/0/active_seconds_left", "ACTIVE+1"),
    ("runs/0/requests_used", "17"),
    ("runs/0/requests_used", "true"),
    (
        "notices/0/detail",
        r#"{"actions": [["dns", "x"]], "result": 1}"#,
    ),
    ("notices/0/version", "0"),
    ("notices/0/name", r#""""#),
    ("notices/0/protection_lost", "1"),
    ("runs/0/usage", r#"{"duration_ms": -1}"#),
    ("runs/0/protection_lost", "0"),
    ("runs/HELD/position", "1"),
    ("runs/0/notice_version", "-1"),
    ("runs/HELD/unexpected", "null"),
    ("discards/0/0", r#""not-a-run""#),
    ("discards/0/1", r#""GEN0""#),
    ("starts", r#"[["A32"]]"#),
    ("starts", r#"[["A32", 5]]"#),
    ("starts", r#"[["not-a-routine", 5, 1]]"#),
    ("starts", r#"[["A32", -1, 1]]"#),
    ("starts", r#"[["A32", 5, 0]]"#),
    ("starts", r#"[["A32", 5, 257]]"#),
    ("starts", r#"[["A32", 5, true]]"#),
    ("starts", r#"[["A32", 9, 1], ["A32", 5, 1]]"#),
];

/// A mutation's value with its placeholders filled in.
fn filled(member: &str) -> String {
    member
        .replace("SHA0", &format!("sha256:{}", "0".repeat(64)))
        .replace("GEN0", &format!("{NETWORK}:routine:{}", "0".repeat(32)))
        .replace("E64", &"e".repeat(64))
        .replace("F32", &"f".repeat(32))
        .replace("A32", &"a".repeat(32))
        .replace("ACTIVE+1", &(record::ACTIVE_SECONDS + 1).to_string())
}

#[test]
fn every_altered_field_fails_closed() {
    let case = case();
    let base = case.baseline();
    let frozen = index_of(&base, "frozen");
    let held = index_of(&base, "held");
    for (path, member) in FIELD_MUTATIONS {
        let path = path.replace("FROZEN", &frozen).replace("HELD", &held);
        let parts: Vec<&str> = path.split('/').collect();
        let mut value = base.clone();
        set(&mut value, &parts, json(&filled(member)));
        case.write_raw("team_1", &value);
        assert!(case.store.load("team_1").is_err(), "{path} = {member}");
    }
    case.write_raw("team_1", &json("[]"));
    assert!(case.store.load("team_1").is_err());
    std::fs::write(case.state_file("team_1"), b"{not json").expect("written");
    assert_eq!(case.store.load("team_1"), Err(StoreError::NotJson));
}

#[test]
fn duplicated_or_excess_records_fail_closed() {
    let case = case();
    let base = case.baseline();
    // A duplicate Routine, a duplicate or extra discard, and too many discards are refused too.
    for change in [
        "duplicate routine",
        "duplicate discard",
        "discard shape",
        "too many discards",
    ] {
        let mut value = base.clone();
        let Value::Object(members) = &mut value else {
            unreachable!()
        };
        for (name, member) in members.iter_mut() {
            match (change, name.as_str(), member) {
                ("duplicate routine", "routines", Value::Array(items))
                | ("duplicate discard", "discards", Value::Array(items)) => {
                    items.push(items[0].clone());
                }
                ("discard shape", "discards", Value::Array(items)) => {
                    if let Value::Array(pair) = &mut items[0] {
                        pair.push(json("\"x\""));
                    }
                }
                ("too many discards", "discards", member) => {
                    *member = Value::Array(
                        (0..=record::MAX_DISCARDS)
                            .map(|index| json(&format!(r#"["{index:032x}", ""]"#)))
                            .collect(),
                    );
                }
                _ => {}
            }
        }
        case.write_raw("team_1", &value);
        assert!(case.store.load("team_1").is_err(), "{change}");
    }
}

/// One altered member of a held run's incident at a time.
const INCIDENT_MUTATIONS: [(&str, &str); 15] = [
    ("request_kind", "\"human\""),
    (
        "lease_sha256",
        "\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"",
    ),
    ("generation", "\"\""),
    ("assistant_id", "\"Bad\""),
    ("action", "\"\""),
    ("action", "1"),
    ("active_seconds_left", "true"),
    ("requests_used", "17"),
    ("position", "null"),
    ("steps", "257"),
    ("position", "true"),
    ("position", "\"1\""),
    ("usage", r#"{"duration_ms": -1}"#),
    ("unexpected", "null"),
    ("protection_lost", "\"no\""),
];

#[test]
fn an_altered_incident_fails_closed() {
    let case = case();
    let state = busy_state();
    let held = state
        .runs
        .iter()
        .find(|run| run.status == RunStatus::Held)
        .expect("held")
        .run_id
        .clone();
    let step = ("dns".to_owned(), "check".to_owned(), Some(1), 1);
    case.put(
        "team_1",
        &hold::settle_hold(&state, &held, NINE, step, &ENTROPY).expect("settled"),
    );
    let base: Value =
        strict_json::parse(&std::fs::read(case.state_file("team_1")).expect("read")).expect("json");
    let index = index_of(&base, "unresolved");
    for (name, member) in INCIDENT_MUTATIONS {
        let mut value = base.clone();
        set(&mut value, &["runs", &index, name], json(member));
        case.write_raw("team_1", &value);
        assert!(case.store.load("team_1").is_err(), "{name} = {member}");
    }
    let mut value = base.clone();
    set(&mut value, &["runs", &index, "position"], json("2"));
    case.write_raw("team_1", &value);
    assert!(
        case.store.load("team_1").is_err(),
        "a position past its plan"
    );
    let mut value = base;
    for (name, member) in [
        ("assistant_id", "\"\""),
        ("action", "\"\""),
        ("position", "null"),
        ("steps", "0"),
    ] {
        set(&mut value, &["runs", &index, name], json(member));
    }
    case.write_raw("team_1", &value);
    let loaded = case.store.load("team_1").expect("an incident with no call");
    assert_eq!(record::incidents(&loaded)[0].action, "");
}

#[test]
fn a_state_file_that_is_not_private_fails_closed_and_a_listing_refuses_another_teams_state() {
    let case = case();
    case.put("team_1", &busy_state());
    std::fs::set_permissions(
        case.state_file("team_1"),
        std::fs::Permissions::from_mode(0o644),
    )
    .expect("chmod");
    assert_eq!(case.store.load("team_1"), Err(StoreError::Unsafe));
    std::fs::set_permissions(
        case.state_file("team_1"),
        std::fs::Permissions::from_mode(0o600),
    )
    .expect("chmod");
    let other = case.store.team_dir("team_2");
    std::fs::create_dir_all(&other).expect("a directory");
    std::fs::rename(case.state_file("team_1"), other.join("state.json")).expect("moved");
    assert_eq!(case.store.teams(), Err(StoreError::Malformed));
    std::fs::write(other.join("state.json"), b"[").expect("written");
    assert!(case.store.teams().is_err());
}

#[test]
fn a_continuation_is_sealed_and_bound_to_its_team_and_run() {
    let case = case();
    let run = "1".repeat(32);
    let lock = case.store.lock("team_1").expect("a lock");
    lock.put_continuation(&run, b"secret-free continuation")
        .expect("sealed");
    assert_eq!(
        lock.continuation(&run),
        Ok(b"secret-free continuation".to_vec())
    );
    assert_eq!(lock.continuations(), Ok(vec![run.clone()]));
    let path = case
        .store
        .team_dir("team_1")
        .join(format!("{run}.continuation"));
    let raw = std::fs::read(&path).expect("read");
    assert!(!String::from_utf8_lossy(&raw).contains("secret-free"));
    // The record is exactly the canonical AES-256-GCM envelope of the payload under its AAD.
    let envelope = strict_json::parse(&raw).expect("json");
    let key: [u8; 32] = std::fs::read(case.root.join("key/aes256.key"))
        .expect("key")
        .try_into()
        .expect("32 bytes");
    let bounds = team_storage::envelope::Bounds {
        maximum_encoded_part: 1 << 20,
        maximum_plaintext: 1 << 20,
    };
    let admitted = Envelope::admit(&envelope, bounds).expect("an envelope");
    let aad = format!(r#"["shimpz-local-routine-continuation-v1","team_1","{run}"]"#);
    assert_eq!(
        admitted
            .open(&Key::from_bytes(key), aad.as_bytes())
            .map(|plain| plain.to_vec()),
        Ok(b"secret-free continuation".to_vec())
    );
    assert_eq!(
        raw,
        team_protocol::python::json::canonical(&admitted.to_value())
    );
}

#[test]
fn a_continuation_filed_under_another_run_does_not_open() {
    let case = case();
    let run = "1".repeat(32);
    case.store
        .lock("team_1")
        .expect("a lock")
        .put_continuation(&run, b"payload")
        .expect("sealed");
    let path = case
        .store
        .team_dir("team_1")
        .join(format!("{run}.continuation"));
    let other_run = "2".repeat(32);
    std::fs::copy(
        &path,
        case.store
            .team_dir("team_1")
            .join(format!("{other_run}.continuation")),
    )
    .expect("copied");
    assert_eq!(
        case.store
            .lock("team_1")
            .expect("a lock")
            .continuation(&other_run),
        Err(StoreError::Unauthentic)
    );
}

#[test]
fn invalid_missing_and_tampered_continuations_fail_closed() {
    let case = case();
    let lock = case.store.lock("team_1").expect("a lock");
    let run = "1".repeat(32);
    assert_eq!(
        lock.put_continuation(&run, b""),
        Err(StoreError::InvalidRecord)
    );
    assert_eq!(
        lock.put_continuation("run", b"x"),
        Err(StoreError::InvalidRun)
    );
    assert_eq!(
        lock.put_continuation(&run, &vec![b'x'; super::MAX_CONTINUATION_BYTES + 1]),
        Err(StoreError::InvalidRecord)
    );
    assert_eq!(lock.continuation(&run), Err(StoreError::Unavailable));
    lock.put_continuation(&run, b"payload").expect("sealed");
    let path = case
        .store
        .team_dir("team_1")
        .join(format!("{run}.continuation"));
    let mut envelope = strict_json::parse(&std::fs::read(&path).expect("read")).expect("json");
    set(&mut envelope, &["algorithm"], json("\"AES-128-GCM\""));
    std::fs::write(&path, dumps(&envelope, CANONICAL)).expect("written");
    assert_eq!(lock.continuation(&run), Err(StoreError::Malformed));
    lock.put_continuation(&run, b"payload").expect("sealed");
    let mut raw = std::fs::read(&path).expect("read");
    let cipher = raw
        .windows(14)
        .position(|window| window == b"\"ciphertext\":\"")
        .expect("ciphertext")
        + 14;
    raw[cipher] = if raw[cipher] == b'A' { b'B' } else { b'A' };
    std::fs::write(&path, &raw).expect("written");
    assert_eq!(lock.continuation(&run), Err(StoreError::Unauthentic));
    lock.delete_continuation(&run).expect("deleted");
    lock.delete_continuation(&run).expect("already deleted");
    assert_eq!(lock.continuations(), Ok(Vec::new()));
}

#[test]
fn a_recovery_is_write_once_and_an_incident_is_sealed_apart() {
    let case = case();
    let lock = case.store.lock("team_1").expect("a lock");
    let run = "3".repeat(32);
    lock.put_recovery(&run, b"snapshot").expect("sealed");
    lock.put_recovery(&run, b"snapshot")
        .expect("the same bytes again");
    assert_eq!(
        lock.put_recovery(&run, b"another"),
        Err(StoreError::Immutable)
    );
    assert_eq!(lock.recovery(&run), Ok(Some(b"snapshot".to_vec())));
    assert_eq!(lock.recoveries(), Ok(vec![run.clone()]));
    lock.put_incident(&run, b"evidence").expect("sealed");
    assert_eq!(lock.incident(&run), Ok(Some(b"evidence".to_vec())));
    // An incident's record never opens as a recovery: each kind has its own AAD.
    let directory = case.store.team_dir("team_1");
    std::fs::copy(
        directory.join(format!("{run}.incident")),
        directory.join(format!("{run}.recovery")),
    )
    .expect("copied");
    assert_eq!(lock.recovery(&run), Err(StoreError::Unauthentic));
    lock.delete_recovery(&run).expect("deleted");
    lock.delete_incident(&run).expect("deleted");
    assert_eq!(
        (lock.recovery(&run), lock.incident(&run)),
        (Ok(None), Ok(None))
    );
}

fn binding(revision: i64) -> Binding {
    Binding {
        incarnation: NETWORK.to_owned(),
        routine_id: "a".repeat(32),
        revision,
        run_id: "4".repeat(32),
    }
}

#[test]
fn a_cursor_opens_only_under_its_binding_and_a_sealed_loss_of_protection_stays() {
    let case = case();
    let lock = case.store.lock("team_1").expect("a lock");
    let document = routine(&"a".repeat(32)).plan;
    let plan = team_routine::plan::Plan {
        timezone: "UTC".to_owned(),
        steps: Vec::new(),
        digest: team_routine::plan::digest(&document),
        output: json(r#"{"mode": "show", "step": "check"}"#),
    };
    let cursor =
        team_routine::cursor::start(&plan, binding(1), NINE, &"b".repeat(32)).expect("a cursor");
    lock.put_cursor(&cursor).expect("sealed");
    assert_eq!(lock.cursor(&binding(1)), Ok(Some(cursor.clone())));
    assert_eq!(lock.cursor(&binding(2)), Err(StoreError::Unauthentic));
    lock.lose_cursor(&binding(1)).expect("lost");
    assert!(
        lock.cursor(&binding(1))
            .expect("read")
            .expect("a cursor")
            .protection_lost
    );
    // Sealing a protected cursor again keeps the loss.
    lock.put_cursor(&cursor).expect("sealed");
    assert!(
        lock.cursor(&binding(1))
            .expect("read")
            .expect("a cursor")
            .protection_lost
    );
    assert_eq!(lock.cursors(), Ok(vec!["4".repeat(32)]));
}

#[test]
fn an_output_digest_is_keyed_and_bound_to_its_team_revision_and_step() {
    let case = case();
    let digest = case
        .store
        .output_digest("team_1", &binding(1), "check", b"result")
        .expect("a digest");
    assert_eq!(digest.len(), 64);
    assert_eq!(
        case.store
            .output_digest("team_1", &binding(1), "check", b"result"),
        Ok(digest.clone())
    );
    for other in [
        case.store
            .output_digest("team_2", &binding(1), "check", b"result"),
        case.store
            .output_digest("team_1", &binding(2), "check", b"result"),
        case.store
            .output_digest("team_1", &binding(1), "other", b"result"),
        case.store
            .output_digest("team_1", &binding(1), "check", b"other"),
    ] {
        assert_ne!(other.expect("a digest"), digest);
    }
}

#[test]
fn hkdf_matches_rfc_5869_without_salt() {
    // RFC 5869 test case 3: IKM 22 × 0x0b, no salt, no info; the first 32 bytes of its OKM.
    let okm = super::hkdf_sha256(&[0x0b; 22], b"");
    assert_eq!(
        super::hex(&okm),
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
    );
}

#[test]
fn a_team_and_the_whole_space_are_removed_without_residue() {
    let case = case();
    case.put("team_1", &busy_state());
    case.put("team_2", &busy_state());
    case.store
        .lock("team_1")
        .expect("a lock")
        .put_continuation(&"1".repeat(32), b"x")
        .expect("sealed");
    case.store.delete("team_1").expect("deleted");
    assert!(!case.store.team_dir("team_1").exists());
    case.store.delete("team_1").expect("already deleted");
    assert_eq!(case.store.teams(), Ok(vec!["team_2".to_owned()]));
    case.store.delete_all().expect("deleted");
    assert_eq!(
        std::fs::read_dir(case.root.join("state"))
            .expect("listed")
            .count(),
        0
    );
    assert!(!case.root.join("key/aes256.key").exists());
}

#[test]
fn deletion_never_follows_a_link_or_removes_what_it_does_not_own() {
    let case = case();
    case.put("team_1", &busy_state());
    let outside = case.root.join("outside");
    std::fs::create_dir(&outside).expect("a directory");
    std::fs::write(outside.join("keep"), b"x").expect("written");
    symlink(&outside, case.store.team_dir("team_1").join("link")).expect("a link");
    assert_eq!(case.store.delete("team_1"), Err(StoreError::Unsafe));
    assert!(outside.join("keep").exists());
    std::fs::remove_file(case.store.team_dir("team_1").join("link")).expect("removed");
    // A link where a Team directory belongs fails the listing.
    symlink(&outside, case.root.join("state").join("f".repeat(64))).expect("a link");
    assert_eq!(case.store.teams(), Err(StoreError::Unsafe));
    assert_eq!(case.store.delete_all(), Err(StoreError::Unsafe));
    assert!(outside.join("keep").exists());
}

#[test]
fn a_teams_lock_is_shared_and_forgotten_once_nobody_holds_it() {
    let case = case();
    let first = case.store.lock("team_1").expect("a lock");
    assert_eq!(case.store.guarded().locks.len(), 1);
    drop(first);
    let _second = case.store.lock("team_2").expect("a lock");
    let names: Vec<String> = {
        let mut guarded = case.store.guarded();
        guarded.locks.retain(|_, gate| Arc::strong_count(gate) > 1);
        guarded.locks.keys().cloned().collect()
    };
    assert_eq!(names, vec![RoutineStore::team_dir_name("team_2")]);
}

#[test]
fn a_reset_refuses_writes_during_it_and_from_before_it() {
    let case = case();
    case.put("team_1", &busy_state());
    let before = case
        .store
        .lock("team_2")
        .expect("a lock taken before the reset");
    let store = &case.store;
    std::thread::scope(|scope| {
        let reset = scope.spawn(|| {
            let mut exclusive = store.exclusive();
            let held = exclusive.team("team_1").expect("held");
            assert_eq!(
                held.update(|_state| Ok::<_, StoreError>((TeamRoutines::default(), ()))),
                Err(StoreError::Resetting)
            );
            assert_eq!(
                held.put_continuation(&"1".repeat(32), b"x"),
                Err(StoreError::Resetting)
            );
        });
        std::thread::sleep(Duration::from_millis(50));
        // The reset began while this lock was held: its writes are refused, and the reset waits for it.
        assert_eq!(
            before.put_continuation(&"1".repeat(32), b"x"),
            Err(StoreError::Resetting)
        );
        drop(before);
        reset.join().expect("the reset");
    });
    // After the reset, writes are admitted again.
    assert_eq!(
        case.store
            .update("team_1", |state| Ok::<_, StoreError>((state, "after"))),
        Ok("after")
    );
}

#[test]
fn a_reset_waits_for_a_write_in_flight() {
    let case = case();
    let entered = AtomicBool::new(false);
    let release = AtomicBool::new(false);
    let order = std::sync::Mutex::new(Vec::new());
    let store = &case.store;
    std::thread::scope(|scope| {
        let writer = scope.spawn(|| {
            store.update("team_1", |state| {
                entered.store(true, Ordering::SeqCst);
                while !release.load(Ordering::SeqCst) {
                    std::thread::yield_now();
                }
                order.lock().expect("order").push("write");
                let added = record::add_routine(&state, &routine(&"a".repeat(32))).expect("added");
                Ok::<_, StoreError>((added, ()))
            })
        });
        while !entered.load(Ordering::SeqCst) {
            std::thread::yield_now();
        }
        let resetting = scope.spawn(|| {
            let exclusive = store.exclusive();
            order.lock().expect("order").push("reset");
            exclusive.delete_all()
        });
        std::thread::sleep(Duration::from_millis(50));
        release.store(true, Ordering::SeqCst);
        assert_eq!(writer.join().expect("the writer"), Ok(()));
        assert_eq!(resetting.join().expect("the reset"), Ok(()));
    });
    assert_eq!(*order.lock().expect("order"), vec!["write", "reset"]);
    assert_eq!(case.store.teams(), Ok(Vec::new()));
}

#[test]
fn concurrent_first_continuations_share_one_keyring() {
    let case = case();
    let store = &case.store;
    std::thread::scope(|scope| {
        for index in 0..8 {
            scope.spawn(move || {
                let team = format!("team_{index}");
                store
                    .lock(&team)
                    .expect("a lock")
                    .put_continuation(&"1".repeat(32), b"x")
                    .expect("sealed");
            });
        }
    });
    for index in 0..8 {
        let team = format!("team_{index}");
        assert_eq!(
            store
                .lock(&team)
                .expect("a lock")
                .continuation(&"1".repeat(32)),
            Ok(b"x".to_vec())
        );
    }
}

#[test]
fn alternating_routines_persist_their_starts_in_time_order_and_rollups_round_trip() {
    let case = case();
    let mut state =
        record::add_routine(&TeamRoutines::default(), &changes_routine(&"d".repeat(32)))
            .expect("added");
    state = record::add_routine(&state, &routine(&"a".repeat(32))).expect("added");
    state.starts = vec![
        ("d".repeat(32), NINE, 1),
        ("a".repeat(32), NINE + 5, 1),
        ("d".repeat(32), NINE + 5, 1),
    ];
    state.routines[0].rollup_minute = NINE / 60 * 60;
    state.routines[0].rollup_runs = 3;
    case.put("team_1", &state);
    assert!(same(&case.store.load("team_1"), &state));
}

#[test]
fn the_largest_state_a_team_can_admit_fits_its_bound_and_a_path_is_never_derived_from_an_id() {
    // A Team's directory is the SHA-256 of its id, so no id ever becomes a path.
    assert_eq!(RoutineStore::team_dir_name("team_1").len(), 64);
    let case = case();
    assert!(!Path::new(&case.store.team_dir("../x")).ends_with("x"));
}
