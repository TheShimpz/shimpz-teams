//! What a completed run does with its result (`test_routine_output.py`): a shown result is Team's bounded, redacted
//! projection of one step's validated result; `changes` shows it only when its keyed digest differs from the last one
//! shown; `none` publishes nothing new; and a run that lost its protection shows nothing it produced.

use strict_json::Value;

use crate::definition;
use crate::json::{at, int, object, set, text};
use crate::plan;
use crate::record::{self, TeamRoutines};
use crate::runs::{complete_recovered, lose_protection};
use crate::testing::{
    ENTROPY, Fixture, NETWORK, ROUTINE_A, added, at_time, claim_bound, edit, edit_run, json, nine,
    plan_document, routine as fixture,
};

const ZONES: &str = r#"{"zones": [{"id": "z1", "name": "example.com", "paused": false, "status": "active"},
    {"id": "z2", "name": "example.org", "paused": true, "status": "pending"}], "pagination": {"page": 1, "per_page": 50}}"#;

fn shown_output() -> Value {
    let node = plan::output_safe(&json(ZONES), &json("{}"), &[]).expect("a safe form");
    plan::output_shown(&int(1), &node)
}

/// The shown step's slot of a run's cursor.
fn slot(digest: Option<&str>) -> Value {
    object(vec![
        ("step", text("check")),
        ("output", shown_output()),
        ("digest", digest.map_or(Value::Null, text)),
    ])
}

fn digest(fill: char) -> String {
    fill.to_string().repeat(64)
}

/// How a run of the one-step Routine ends: its disposition, its own earlier notice, its Routine's last digest, its
/// shown slot, and whether it lost its protection.
struct Completion<'a> {
    mode: &'a str,
    notice_version: i64,
    digest: String,
    shown: Option<Value>,
    lost: bool,
}

impl Completion<'_> {
    fn of(mode: &str) -> Completion<'_> {
        Completion {
            mode,
            notice_version: 0,
            digest: String::new(),
            shown: None,
            lost: false,
        }
    }

    /// A claimed run of a one-step Routine with this disposition and digest, finished with this slot.
    fn finished(self) -> TeamRoutines {
        let output = if self.mode == "none" {
            r#"{"mode": "none", "step": null}"#.to_owned()
        } else {
            format!(r#"{{"mode": "{}", "step": "check"}}"#, self.mode)
        };
        let plan = plan_document("dns", "UTC", "check", Some(&output));
        let state = at_time(
            &added(&[Fixture {
                plan,
                ..Fixture::default()
            }
            .build()]),
            ROUTINE_A,
            nine(),
        );
        let state = edit(&state, ROUTINE_A, |current| {
            current.output_digest = self.digest;
            current.failures = 2;
        });
        let (mut state, claimed, lease) = claim_bound(&state, nine(), NETWORK);
        let run_id = claimed.run.run_id;
        if self.notice_version != 0 {
            state = edit_run(&state, &run_id, |run| {
                run.notice_version = self.notice_version;
            });
        }
        if self.lost {
            state = lose_protection(&state, &run_id).expect("lost");
        }
        crate::runs::finish(
            &state,
            &run_id,
            &lease,
            nine() + 5,
            self.shown.as_ref(),
            &ENTROPY,
        )
        .expect("finished")
    }
}

fn output_of(state: &TeamRoutines) -> Value {
    at(&state.notices[0].detail, "output").clone()
}

fn summary_of(state: &TeamRoutines) -> Value {
    definition::summary(&state.routines[0].plan, state.routines[0].revision)
}

#[test]
fn show_publishes_the_result_every_run_and_unavailable_when_it_was_not_kept() {
    let shown = slot(Some(&digest('a')));
    let state = Completion {
        shown: Some(shown.clone()),
        ..Completion::of("show")
    }
    .finished();
    let [notice] = state.notices.as_slice() else {
        panic!("one notice");
    };
    assert_eq!(
        notice.detail,
        object(vec![
            ("plan", summary_of(&state)),
            ("output", shown_output())
        ])
    );
    assert_eq!(
        (notice.usage.clone(), notice.protection_lost),
        (Some(json(r#"{"duration_ms": 0}"#)), false)
    );
    assert_eq!(
        (
            state.routines[0].failures,
            state.routines[0].output_digest.as_str()
        ),
        (0, "")
    );
    let missing = Completion::of("show").finished();
    assert_eq!(
        output_of(&missing),
        plan::output_state(&int(1), "unavailable")
    );
    let mut wrong = shown;
    set(&mut wrong, "step", text("other"));
    let wrong_step = Completion {
        shown: Some(wrong),
        ..Completion::of("show")
    }
    .finished();
    assert_eq!(at(&output_of(&wrong_step), "state"), &text("unavailable"));
}

#[test]
fn changes_publishes_only_a_changed_result_and_records_what_it_showed() {
    let shown = slot(Some(&digest('a')));
    let changed = Completion {
        shown: Some(shown.clone()),
        ..Completion::of("changes")
    }
    .finished();
    assert_eq!(output_of(&changed), shown_output());
    assert_eq!(changed.routines[0].output_digest, digest('a'));
    let same = || Completion {
        digest: digest('a'),
        shown: Some(shown.clone()),
        ..Completion::of("changes")
    };
    let quiet = same().finished();
    assert_eq!(
        (
            quiet.notices.len(),
            quiet.runs.len(),
            quiet.routines[0].failures
        ),
        (0, 0, 0)
    );
    // A run with a notice of its own gets its terminal version, saying unchanged instead of repeating the result.
    let answered = Completion {
        notice_version: 1,
        ..same()
    }
    .finished();
    assert_eq!(
        output_of(&answered),
        plan::output_state(&int(1), "unchanged")
    );
}

#[test]
fn an_incomparable_or_unavailable_result_never_moves_the_baseline() {
    // A result too large to compare is always shown, and leaves no baseline a later result could match.
    let incomparable = Completion {
        digest: digest('a'),
        shown: Some(slot(None)),
        ..Completion::of("changes")
    }
    .finished();
    assert_eq!(output_of(&incomparable), shown_output());
    assert_eq!(incomparable.routines[0].output_digest, "");
    // An unavailable result never moves the baseline.
    let unavailable = Completion {
        digest: digest('b'),
        ..Completion::of("changes")
    }
    .finished();
    assert_eq!(at(&output_of(&unavailable), "state"), &text("unavailable"));
    assert_eq!(unavailable.routines[0].output_digest, digest('b'));
}

#[test]
fn none_publishes_nothing_new_unless_the_run_already_has_a_notice() {
    let quiet = Completion::of("none").finished();
    assert_eq!((quiet.notices.len(), quiet.routines[0].failures), (0, 0));
    let answered = Completion {
        notice_version: 1,
        ..Completion::of("none")
    }
    .finished();
    assert_eq!(
        answered.notices[0].detail,
        object(vec![
            ("plan", summary_of(&answered)),
            ("output", Value::Null)
        ])
    );
}

#[test]
fn a_run_that_lost_its_protection_shows_nothing_and_says_so() {
    let shown = slot(Some(&digest('a')));
    let state = Completion {
        shown: Some(shown.clone()),
        lost: true,
        ..Completion::of("show")
    }
    .finished();
    assert_eq!(
        output_of(&state),
        plan::output_state(&int(1), "unavailable")
    );
    assert!(state.notices[0].protection_lost);
    // The changes baseline never moves on a result the run could not show.
    let changes = Completion {
        digest: digest('b'),
        shown: Some(shown),
        lost: true,
        ..Completion::of("changes")
    }
    .finished();
    assert_eq!(changes.routines[0].output_digest, digest('b'));
    // A run that shows nothing still says it lost its protection.
    let silent = Completion {
        lost: true,
        ..Completion::of("none")
    }
    .finished();
    assert_eq!(
        (
            silent.notices[0].outcome.as_str(),
            silent.notices[0].protection_lost
        ),
        ("done", true)
    );
}

#[test]
fn the_watchdog_completes_a_run_through_the_same_disposition() {
    let due = at_time(&added(&[fixture()]), ROUTINE_A, nine());
    let (state, claimed, _lease) = claim_bound(&due, nine(), "net_1");
    let shown = slot(Some(&digest('a')));
    let (run_id, lease_sha256) = (
        claimed.run.run_id.as_str(),
        claimed.run.lease_sha256.as_str(),
    );
    let done = complete_recovered(
        &state,
        run_id,
        lease_sha256,
        nine() + 5,
        Some(&shown),
        &ENTROPY,
    )
    .expect("done");
    assert_eq!(
        at(&done.notices.last().expect("a notice").detail, "output"),
        &shown_output()
    );
}

#[test]
fn an_update_starts_a_fresh_baseline() {
    let state = Completion {
        shown: Some(slot(Some(&digest('a')))),
        ..Completion::of("changes")
    }
    .finished();
    let mut current = state.routines[0].clone();
    current.output_digest = digest('a');
    let changed = record::scheduled(&current, nine() + 10).expect("scheduled");
    let updated = record::update(&state, &changed, 1, nine() + 10, &ENTROPY).expect("updated");
    assert_eq!(updated.routines[0].output_digest, "");
}
