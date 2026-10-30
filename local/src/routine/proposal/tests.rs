//! The card book, a record call's shape, and the card itself (`tests/test_local_routine_recorder.py`).

use std::sync::Arc;
use std::time::{Duration, SystemTime};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::testing::FakeClock;
use team_protocol::routine_proposal::canonical_proposal;
use team_routine::record::{Routine, scheduled};
use team_routine::recording::Recorded;

use super::card::{bound_digest, card, description_agrees};
use super::*;
use crate::routine::recorder::Started;

const PRINCIPAL: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn proposal(proposal_id: &str, replaces: Option<&str>, team: &str, person: &str) -> Proposal {
    Proposal {
        proposal_id: proposal_id.to_owned(),
        team_id: team.to_owned(),
        principal: person.to_owned(),
        incarnation: "b".repeat(64),
        routine: Routine::new("d", "n", Value::Null, Vec::new(), Value::Null, 0, 0),
        replaces: replaces.map(str::to_owned),
        expected_revision: None,
        digest: format!("sha256:{}", "0".repeat(64)),
        expires_at: 10,
        creation: (None, Vec::new()),
        refines: false,
    }
}

#[test]
fn cards_go_with_a_deleted_routine_a_team_or_a_reset() {
    let book = ProposalBook::new(Arc::new(FakeClock::at(SystemTime::UNIX_EPOCH)));
    let (a, b, c, r, other) = (
        "a".repeat(32),
        "b".repeat(32),
        "c".repeat(32),
        "r".repeat(32),
        "c".repeat(32),
    );
    book.put(proposal(&a, Some(&r), "team_1", PRINCIPAL));
    book.put(proposal(&b, None, "team_1", &other));
    book.put(proposal(&c, None, "team_2", PRINCIPAL));
    book.drop_routine("team_1", &r);
    book.drop_routine("team_2", &r);
    assert!(book.take("team_1", &a, PRINCIPAL).is_none());
    assert!(book.take("team_1", &b, &other).is_some());
    book.put(proposal(&b, None, "team_1", PRINCIPAL));
    book.drop_team("team_1");
    assert!(book.take("team_1", &b, PRINCIPAL).is_none());
    book.clear();
    assert!(book.take("team_2", &c, PRINCIPAL).is_none());
}

#[test]
fn a_card_belongs_to_its_person_and_lasts_until_it_expires() {
    let clock = Arc::new(FakeClock::at(SystemTime::UNIX_EPOCH));
    let book = ProposalBook::new(clock.clone());
    let id = "a".repeat(32);
    book.put(proposal(&id, None, "team_1", PRINCIPAL));
    // Someone else's take, or another id, leaves the card in place.
    assert!(book.take("team_1", &id, &"c".repeat(32)).is_none());
    assert!(book.take("team_1", &"e".repeat(32), PRINCIPAL).is_none());
    assert!(book.take("team_1", &id, PRINCIPAL).is_some());
    assert!(
        book.take("team_1", &id, PRINCIPAL).is_none(),
        "a card is consumed by its first take"
    );
    book.put(proposal(&id, None, "team_1", PRINCIPAL));
    clock.advance(Duration::from_secs(10));
    assert!(
        book.take("team_1", &id, PRINCIPAL).is_none(),
        "an expired card is gone"
    );
}

const RECORD: &str = r#"{"name":"DNS de shimpz.com","description":"Lista as zonas da sua Cloudflare e lê os registros DNS de shimpz.com.","replaces":null}"#;

#[test]
fn a_malformed_record_call_is_a_brain_contract_failure() {
    assert!(outcome(&json(RECORD)).is_ok());
    let (name, description) = (r#""name":"DNS""#, r#""description":"Lista o DNS.""#);
    let malformed = [
        format!(r#"{{{name},{description},"replaces":null,"op":"create"}}"#),
        format!(r#"{{"name":"a\nb",{description},"replaces":null}}"#),
        format!(
            r#"{{{name},{description},"replaces":null,"schedule":{{"kind":"continuous","gap":3600}}}}"#
        ),
        format!(r#"{{{name},{description},"replaces":null,"output":{{"mode":"show"}}}}"#),
        format!(r#"{{{name},{description},"replaces":10000000000000000000000000000000}}"#),
        format!("{{{name},{description}}}"),
        "[]".to_owned(),
    ];
    for text in malformed {
        let failed = outcome(&json(&text)).map_err(|problem| problem.code);
        assert_eq!(failed, Err(Some("brain-runtime-failed")), "{text}");
    }
}

fn chat() -> RoutineChat {
    let clock: Arc<dyn team_core::clock::Clock> = Arc::new(FakeClock::at(
        SystemTime::UNIX_EPOCH + Duration::from_secs(1),
    ));
    let root = tempfile::tempdir().expect("root").keep();
    let routines = RoutineStore::new(
        &root.join("routines/state"),
        &root.join("routines/key/aes256.key"),
        Arc::new(OsEntropy),
    );
    let diagnostics = DiagnosticStore::new(
        &root.join("diagnostics/state"),
        &root.join("diagnostics/key/aes256.key"),
        Arc::new(OsEntropy),
    );
    let (routines, diagnostics) = (
        Arc::new(routines.expect("routines")),
        Arc::new(diagnostics.expect("diagnostics")),
    );
    RoutineChat {
        routines,
        diagnostics,
        recordings: Arc::new(RecordingBook::new(clock.clone(), Arc::new(OsEntropy))),
        proposals: Arc::new(ProposalBook::new(clock.clone())),
        lost: Arc::new(crate::routine::state::none_lost),
        clock,
        entropy: Arc::new(OsEntropy),
    }
}

#[test]
fn a_refused_or_another_persons_recording_answers_its_own_code() {
    let chat = chat();
    let started = Started {
        message: "m".to_owned(),
        conversation: Vec::new(),
        timezone: None,
        locale: None,
    };
    let recording = chat
        .recordings
        .start("team_1", (PRINCIPAL, &"b".repeat(64)), &started, 1)
        .expect("started");
    let turn = |principal| Turn {
        team_id: "team_1",
        recording: &recording,
        principal,
        current: None,
        scopes: &[],
    };
    let other = "c".repeat(32);
    let code = |stopped: Result<Span, Stopped>| match stopped {
        Err(Stopped::Refused(code)) => code,
        _ => String::new(),
    };
    assert_eq!(
        code(chat.live_recording(&turn(Some(&other)))),
        "routine-recording-unavailable"
    );
    assert!(chat.live_recording(&turn(Some(PRINCIPAL))).is_ok());
    chat.recordings.refuse("team_1", &recording, "routine-x");
    assert_eq!(
        code(chat.live_recording(&turn(Some(PRINCIPAL)))),
        "routine-x"
    );
}

fn plan(output: &str) -> (Value, Vec<Value>) {
    let pin = format!("sha256:{}", "a".repeat(64));
    let document = json(&format!(
        r#"{{"version":4,"timezone":"UTC","steps":[{{"id":"s1","assistant":"dns","action":"list","pin":"{pin}","input":{{}}}},
        {{"id":"s2","assistant":"dns","action":"show","pin":"{pin}","input":{{"day":{{"kind":"run_clock","format":"date"}},
        "zone":{{"kind":"step_output","step":"s1","pointer":"/zone"}}}}}}],"output":{{"mode":"{output}","step":null}}}}"#
    ));
    let permitted: Vec<Value> = ["list", "show"]
        .iter()
        .map(|action| {
            json(&format!(
                r#"{{"assistant":"dns","action":"{action}","pin":"{pin}","read_only":true,"stored_inputs":[]}}"#
            ))
        })
        .collect();
    (document, permitted)
}

#[test]
fn a_team_changed_since_the_turn_or_in_another_incarnation_refuses() {
    let chat = chat();
    let started = Started {
        message: "m".to_owned(),
        conversation: Vec::new(),
        timezone: None,
        locale: None,
    };
    let incarnation = "b".repeat(64);
    let recording = chat
        .recordings
        .start("team_1", (PRINCIPAL, &incarnation), &started, 1);
    let recording = recording.expect("started");
    let code = |stopped: Result<Span, Stopped>| match stopped {
        Err(Stopped::Refused(code)) => code,
        _ => String::new(),
    };
    // A Team changed since the turn, or in another incarnation, refuses before anything is recorded.
    let span = chat
        .recordings
        .get("team_1", Some(&recording))
        .expect("span");
    let other_incarnation = "c".repeat(64);
    for current in [None, Some((other_incarnation.as_str(), &[][..]))] {
        let turn = Turn {
            team_id: "team_1",
            recording: &recording,
            principal: Some(PRINCIPAL),
            current,
            scopes: &[],
        };
        let refused = RoutineChat::turn_contracts(&turn, &span)
            .err()
            .map(|stopped| code(Err(stopped)));
        assert_eq!(refused.as_deref(), Some("team-context-changed"));
    }
}

fn candidate(description: &str, output: &str) -> (Routine, Recorded) {
    let (document, permitted) = plan(output);
    let schedule = json(r#"{"kind":"daily","time":"09:00"}"#);
    let mut routine = Routine::new(
        &"d".repeat(32),
        "Zonas",
        schedule.clone(),
        Vec::new(),
        document.clone(),
        0,
        0,
    );
    routine.description = description.to_owned();
    routine.permitted.clone_from(&permitted);
    let routine = scheduled(&routine, 1_800_000_000).expect("scheduled");
    let origins = vec![
        ("s1".to_owned(), Vec::new()),
        (
            "s2".to_owned(),
            vec![
                ("day".to_owned(), "clock".to_owned()),
                ("zone".to_owned(), "step".to_owned()),
            ],
        ),
    ];
    let recorded = Recorded {
        document,
        origins,
        permitted,
        schedule,
        timezone_source: "browser".to_owned(),
        chosen: Vec::new(),
        repair_from: None,
    };
    (routine, recorded)
}

#[test]
fn the_card_names_a_run_date_and_a_plain_reference_completely() {
    let (routine, recorded) = candidate("Lista o DNS.", "none");
    let id = "e".repeat(32);
    let view = card(&id, &routine, &recorded, (None, 1_800_000_900));
    // A confirmation binds the description the person read, like every other part of the card.
    let (other_routine, _) = candidate("Lê o DNS.", "none");
    let reworded_view = card(&id, &other_routine, &recorded, (None, 1_800_000_900));
    assert_eq!(view.get("description"), Some(&json(r#""Lista o DNS.""#)));
    assert_ne!(
        bound_digest(&routine, &view, None),
        bound_digest(&other_routine, &reworded_view, None)
    );
    let inputs = view
        .get("steps")
        .and_then(|steps| match steps {
            Value::Array(items) => items.get(1).and_then(|step| step.get("inputs")).cloned(),
            _ => None,
        })
        .expect("inputs");
    let expected = r#"[{"member":"day","origin":"clock","value":null,"step":null,"pointer":null,"where":null,"item":null},
        {"member":"zone","origin":"step","value":null,"step":1,"pointer":"/zone","where":null,"item":null}]"#;
    assert_eq!(inputs, json(expected));
    assert_eq!(canonical_proposal(&view), Some(&view));
}

#[test]
fn a_description_may_repeat_the_cards_settings_but_never_state_others() {
    let (routine, recorded) = candidate("Lista o DNS.", "show");
    let view = |description: &str| {
        let mut changed = routine.clone();
        description.clone_into(&mut changed.description);
        card(&"e".repeat(32), &changed, &recorded, (None, 1_800_000_900))
    };
    assert!(description_agrees(&view(
        "Lista os registros DNS de shimpz.com."
    )));
    let repeated = "Lista os registros DNS de shimpz.com em UTC e me mostre o resultado.";
    assert!(description_agrees(&view(repeated)));
    for conflicting in [
        "Lista os registros DNS de shimpz.com e não precisa mostrar.",
        "Lista os registros DNS de shimpz.com toda segunda-feira às 8h.",
        "Lista os registros DNS de shimpz.com em Europe/Lisbon.",
        "Lista os registros DNS de shimpz.com sempre que algo mudar.",
    ] {
        assert!(!description_agrees(&view(conflicting)), "{conflicting}");
    }
    let mut unzoned = view("Lista em UTC.");
    if let Value::Object(members) = &mut unzoned {
        for (name, value) in members.iter_mut() {
            if name == "timezone_source" {
                *value = Value::String("none".to_owned());
            }
        }
    }
    assert!(!description_agrees(&unzoned));
}
