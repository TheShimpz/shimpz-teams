use std::sync::Arc;

use strict_json::Value;
use team_core::entropy::OsEntropy;

use super::{Claim, GRANT_SECONDS, MAX_TURNS, Sender, Terminal, Turns};

const TEAM: &str = "team_1";
const NOW: i64 = 1_800_000_000;

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn turns(directory: &tempfile::TempDir) -> Arc<Turns> {
    Arc::new(Turns::new(
        &directory.path().join("state/turns"),
        &directory.path().join("key/aes256.key"),
        Arc::new(OsEntropy),
    ))
}

fn sender(nonce: &str, body: &str) -> Sender {
    Sender::of(nonce, NOW, &"a".repeat(64), &json(body))
}

fn terminal() -> Terminal {
    Terminal {
        status: 200,
        body: json(r#"{"status":"completed","reply":"Secret reply."}"#),
    }
}

#[test]
fn a_turn_is_recorded_before_it_runs_attached_while_it_runs_and_delivered_once() {
    let directory = tempfile::tempdir().expect("directory");
    let turns = turns(&directory);
    let first = sender(&"1".repeat(32), r#"{"message":"hi"}"#);
    let Ok(Claim::New(live)) = turns.claim(TEAM, &first, NOW) else {
        panic!("new")
    };
    let Ok(Claim::Attached(mut attached)) = turns.claim(TEAM, &first, NOW) else {
        panic!("attached")
    };
    turns.end(&live, &terminal()).expect("ended");
    assert_eq!(attached.borrow_and_update().clone(), Some(terminal()));
    drop(live);
    // A send of the same turn after it ended receives its kept terminal again, until Admin acknowledges it.
    let Ok(Claim::Ended(again)) = turns.claim(TEAM, &first, NOW) else {
        panic!("ended")
    };
    assert_eq!(again, terminal());
    assert_eq!(
        turns.settle(TEAM, &first.nonce, &first.authority),
        Ok((false, None))
    );
    let delivered = turns.claim(TEAM, &first, NOW).expect_err("delivered");
    assert_eq!(
        (delivered.status, delivered.code),
        (409, Some("turn-delivered"))
    );
    // Nothing the turn said is readable in the state file.
    let state =
        std::fs::read_to_string(directory.path().join("state/turns/team_1.json")).expect("state");
    assert!(!state.contains("Secret reply"));
}

#[test]
fn only_the_same_session_and_body_may_send_a_turn_again_and_only_within_its_grant() {
    let directory = tempfile::tempdir().expect("directory");
    let turns = turns(&directory);
    let first = sender(&"2".repeat(32), r#"{"message":"hi"}"#);
    let Ok(Claim::New(_live)) = turns.claim(TEAM, &first, NOW) else {
        panic!("new")
    };
    let other_session = Sender {
        authority: "b".repeat(64),
        ..first.clone()
    };
    let refused = turns
        .claim(TEAM, &other_session, NOW)
        .expect_err("other session");
    assert_eq!(
        (refused.status, refused.code),
        (403, Some("turn-authority"))
    );
    let other_body = sender(&first.nonce, r#"{"message":"bye"}"#);
    let refused = turns.claim(TEAM, &other_body, NOW).expect_err("other body");
    assert_eq!((refused.status, refused.code), (409, Some("turn-changed")));
    let refused = turns
        .settle(TEAM, &first.nonce, &other_session.authority)
        .expect_err("other session settles nothing");
    assert_eq!(refused.status, 403);
    let late = turns
        .claim(TEAM, &first, NOW + GRANT_SECONDS)
        .expect_err("expired");
    assert_eq!((late.status, late.code), (410, Some("authority-expired")));
}

#[test]
fn a_turn_left_running_by_a_previous_process_resumes_from_its_checkpoint() {
    let directory = tempfile::tempdir().expect("directory");
    let first = sender(&"3".repeat(32), r#"{"message":"hi"}"#);
    {
        let turns = turns(&directory);
        let Ok(Claim::New(live)) = turns.claim(TEAM, &first, NOW) else {
            panic!("new")
        };
        turns
            .checkpoint(&live, json(r#"{"round":2}"#))
            .expect("checkpoint");
        // The process ends here: the turn is left running.
    }
    let turns = turns(&directory);
    let Ok(Claim::Orphaned(live, checkpoint)) = turns.claim(TEAM, &first, NOW) else {
        panic!("orphaned")
    };
    assert_eq!(checkpoint, Some(json(r#"{"round":2}"#)));
    // A revocation tombstones it, handing back its checkpoint: its ending is never delivered.
    assert_eq!(
        turns.settle(TEAM, &first.nonce, &first.authority),
        Ok((true, Some(json(r#"{"round":2}"#))))
    );
    turns.end(&live, &terminal()).expect("ended");
    assert_eq!(
        turns.claim(TEAM, &first, NOW).expect_err("revoked").code,
        Some("turn-delivered")
    );
}

#[test]
fn a_team_keeps_its_turns_within_their_grant_and_forgets_them_with_the_team() {
    let directory = tempfile::tempdir().expect("directory");
    let turns = turns(&directory);
    for index in 0..MAX_TURNS {
        let next = sender(&format!("{index:032x}"), r#"{"message":"hi"}"#);
        let Ok(Claim::New(live)) = turns.claim(TEAM, &next, NOW) else {
            panic!("new")
        };
        turns.end(&live, &terminal()).expect("ended");
        turns
            .settle(TEAM, &next.nonce, &next.authority)
            .expect("delivered");
    }
    // Even delivered, a turn within its grant keeps its tombstone: its send never runs twice.
    let one_more = sender(&"f".repeat(32), r#"{"message":"hi"}"#);
    let full = turns.claim(TEAM, &one_more, NOW).expect_err("full");
    assert_eq!((full.status, full.code), (429, Some("turns-full")));
    let first = sender(&format!("{:032x}", 0), r#"{"message":"hi"}"#);
    let again = turns.claim(TEAM, &first, NOW + 1).expect_err("delivered");
    assert_eq!(again.code, Some("turn-delivered"));
    // Once their grants ended, they go.
    let later = Sender {
        issued_at: NOW + GRANT_SECONDS,
        ..one_more
    };
    assert!(matches!(
        turns.claim(TEAM, &later, NOW + GRANT_SECONDS),
        Ok(Claim::New(_))
    ));
    turns.forget_team(TEAM).expect("forgotten");
    assert!(!directory.path().join("state/turns/team_1.json").exists());
    turns.forget_all().expect("all forgotten");
}
