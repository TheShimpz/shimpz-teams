//! `test_routine_record.py`, `test_routine_definition.py`, and `test_routine_view.py`: a Routine definition is
//! admitted only in its closed contract, changed by revision, budgeted, and viewed exactly.

use strict_json::Value;
use team_protocol::routine::{
    MAX_PERMITTED, MAX_STEP_INPUTS, MAX_STEP_VIEW_BYTES, canonical_page, canonical_step,
    canonical_summary, encoded_bytes,
};
use team_protocol::routine_notice::{
    MAX_NOTICE_BATCH_BYTES, MAX_SUMMARY_BYTES, canonical_notice_batch,
};

use super::{
    MAX_ROUTINE_NOTICES, MAX_ROUTINES, MAX_UNDELIVERED_NOTICES, Routine, RoutineStateError,
    RunStatus, TeamRoutines, add_routine, begin_delete, change_room, complete_delete, create,
    definition_valid, first_run, mark_scope_changed, routine, scheduled, set_paused, update,
};
use crate::definition::{
    capacity, daily_steps, definition_bytes, disposition, fits_within, over_budget, page,
    run_units, state, step, steps_fit, summary,
};
use crate::json::{as_str, at, canonical, elements, int, object, set, text};
use crate::plan::{MAX_DAILY_STEPS, MAX_PLAN_BYTES};
use crate::testing::{
    ENTROPY, Fixture, PIN, ROUTINE_A, added, claimed, confirmed, default_plan, epoch, every_hour,
    full_notices, json, large_completion, nine, permitted, plan_document, repeated_plan,
    routine as fixture, routine_of, routine_scheduled,
};

fn refused<T: std::fmt::Debug>(result: Result<T, RoutineStateError>, code: &'static str) {
    assert_eq!(result.map(|_| ()).err(), Some(RoutineStateError::new(code)));
}

fn digest_pin(fill: char) -> String {
    format!("sha256:{}", fill.to_string().repeat(64))
}

fn with(change: impl FnOnce(&mut Routine)) -> Routine {
    let mut value = fixture();
    change(&mut value);
    value
}

fn invalid(change: impl FnOnce(&mut Routine)) {
    refused(
        add_routine(&TeamRoutines::default(), &with(change)),
        "routine-invalid",
    );
}

#[test]
fn only_a_closed_routine_is_admitted() {
    refused(
        add_routine(&added(&[fixture()]), &fixture()),
        "routine-exists",
    );
    invalid(|value| value.routine_id = "A".repeat(32));
    invalid(|value| value.name = String::new());
    invalid(|value| value.plan = json("{}"));
    invalid(|value| value.plan = plan_document("web", "UTC", "check", None));
    invalid(|value| value.confirmation = None);
    invalid(|value| {
        set(
            value.confirmation.as_mut().expect("a confirmation"),
            "principal",
            text("x"),
        );
    });
    invalid(|value| value.schedule = json(r#"{"kind": "daily", "time": "25:00"}"#));
    invalid(|value| set(&mut value.plan, "timezone", text("Mars/Olympus")));
    invalid(|value| value.next_run_at += 1);
}

#[test]
fn the_permitted_actions_are_exactly_the_plans_own_at_their_pins() {
    let entry = fixture().permitted[0].clone();
    let changed = |key: &str, member: Value| {
        let mut changed = entry.clone();
        set(&mut changed, key, member);
        changed
    };
    let mut extra = changed("action", text("zzz-extra"));
    set(&mut extra, "read_only", Value::Bool(false));
    let too_many = usize::try_from(MAX_PERMITTED).expect("a bound") + 1;
    for permitted in [
        Vec::new(),
        vec![entry.clone(), entry.clone()],
        vec![changed("pin", text(&digest_pin('0')))],
        vec![changed("stored_inputs", json(r#"["b", "a"]"#))],
        vec![changed("read_only", int(1))],
        vec![entry.clone(); too_many],
        // The permitted set is exactly the plan's Actions: no extra one.
        vec![entry.clone(), extra],
    ] {
        invalid(|value| value.permitted = permitted);
    }
}

#[test]
fn the_pinned_assistants_are_exactly_the_plans_own_sorted() {
    let digest = digest_pin('c');
    let pair = |assistant: &str, pin: &str| (assistant.to_owned(), pin.to_owned());
    for assistants in [
        Vec::new(),
        vec![pair("dns", &digest), pair("dns", &digest_pin('d'))],
        vec![pair("web", &digest), pair("dns", &digest)],
        vec![pair("Dns", &digest)],
        vec![pair("dns", "md5:x")],
    ] {
        invalid(|value| value.assistants = assistants);
    }
}

#[test]
fn a_step_whose_projection_outgrows_its_protocol_bound_is_refused() {
    // A plan within its byte bound whose one step's previews escape past that step's projection is never held.
    let inputs: Vec<(String, Value)> = (0..MAX_STEP_INPUTS)
        .map(|index| {
            let member = format!(
                "{}{}{}",
                char::from(b'a' + u8::try_from(index / 26).expect("a letter")),
                char::from(b'a' + u8::try_from(index % 26).expect("a letter")),
                "é".repeat(120)
            );
            (
                member,
                object(vec![
                    ("kind", text("literal")),
                    ("value", text(&"€".repeat(120))),
                ]),
            )
        })
        .collect();
    let mut plan = default_plan();
    let mut only = elements(at(&plan, "steps"))[0].clone();
    set(&mut only, "input", Value::Object(inputs));
    set(&mut plan, "steps", Value::Array(vec![only]));
    assert!(canonical(&plan).len() <= MAX_PLAN_BYTES);
    let large = confirmed(with(|value| value.plan = plan.clone()));
    let projected = step(&plan, &large.permitted, 1).expect("a projection");
    assert!(encoded_bytes(&projected) > MAX_STEP_VIEW_BYTES);
    refused(
        add_routine(&TeamRoutines::default(), &large),
        "routine-too-large",
    );
}

fn sized(routine_id: &str, steps: usize, inputs: usize) -> Routine {
    let mut plan = repeated_plan(
        steps,
        &format!(r#"{{"mode": "show", "step": "s{}"}}"#, steps - 1),
    );
    let values: Vec<(String, Value)> = (0..inputs)
        .map(|member| {
            (
                format!("m{member}"),
                object(vec![
                    ("kind", text("literal")),
                    ("value", int(i64::try_from(member).unwrap_or(0))),
                ]),
            )
        })
        .collect();
    let with_inputs = elements(at(&plan, "steps"))
        .iter()
        .map(|item| {
            let mut copy = item.clone();
            set(&mut copy, "input", Value::Object(values.clone()));
            copy
        })
        .collect();
    set(&mut plan, "steps", Value::Array(with_inputs));
    confirmed(
        Fixture {
            id: routine_id.to_owned(),
            plan,
            ..Fixture::default()
        }
        .build(),
    )
}

#[test]
fn a_definition_over_its_own_budget_or_the_teams_is_refused() {
    // 256 steps of one Action with their own inputs fit their definition's budget beside its standing scope.
    let many = sized(ROUTINE_A, 256, 2);
    assert!(fits_within(&many, crate::plan::MAX_DEFINITION_BYTES));
    assert!(add_routine(&TeamRoutines::default(), &many).is_ok());
    assert!(!fits_within(&many, definition_bytes(&many) - 1));
    // Each fits its own budget, but together they outgrow the Team's.
    let ids = [
        "00000000000000000000000000000000",
        "00000000000000000000000000000001",
        "00000000000000000000000000000002",
        "00000000000000000000000000000003",
        "00000000000000000000000000000004",
    ];
    let large: Vec<Routine> = ids.iter().map(|id| sized(id, 256, 22)).collect();
    assert!(
        large
            .iter()
            .all(|item| canonical(&item.plan).len() <= MAX_PLAN_BYTES)
    );
    let state = added(&large[..4]);
    refused(add_routine(&state, &large[4]), "routine-team-budget");
}

#[test]
fn a_routine_whose_daily_steps_outgrow_the_team_budget_is_refused() {
    // A cap of runs a day reserves every step of each run; paused Routines keep their share (scale).
    let mut hundred = confirmed(
        Fixture {
            schedule: json(r#"{"kind": "continuous", "gap": 432}"#),
            plan: repeated_plan(100, r#"{"mode": "none", "step": null}"#),
            ..Fixture::default()
        }
        .build(),
    );
    hundred.next_run_at = first_run(&hundred).expect("a firing");
    // 200 runs of 100 steps is exactly the Team's 20,000 daily steps.
    assert_eq!(daily_steps(&hundred), MAX_DAILY_STEPS);
    let state = set_paused(
        &add_routine(&TeamRoutines::default(), &hundred).expect("added"),
        ROUTINE_A,
        true,
    )
    .expect("paused");
    assert_eq!(capacity(&state.routines.iter().collect::<Vec<_>>()), 0);
    refused(
        add_routine(&state, &routine_of("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")),
        "routine-step-budget",
    );
    let mut over = hundred.clone();
    over.schedule = json(r#"{"kind": "continuous", "gap": 430}"#);
    over.next_run_at = first_run(&over).expect("a firing");
    refused(
        add_routine(&TeamRoutines::default(), &over),
        "routine-step-budget",
    );
}

#[test]
fn a_team_holds_at_most_eight_routines_whose_caps_fit_its_daily_budget() {
    let state = added(
        &(0..MAX_ROUTINES)
            .map(|index| routine_of(&format!("{index:032x}")))
            .collect::<Vec<_>>(),
    );
    refused(
        add_routine(&state, &routine_of("ffffffffffffffffffffffffffffffff")),
        "routine-limit",
    );
    // A one-step Routine every five seconds takes 17,280 of the Team's 20,000 daily steps: one every 30 seconds,
    // 2,880 more, no longer fits beside it, while a daily one does.
    let busy = added(&[routine_scheduled(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        json(r#"{"kind": "continuous", "gap": 5}"#),
    )]);
    assert!(add_routine(&busy, &fixture()).is_ok());
    let every_thirty = routine_scheduled(
        "cccccccccccccccccccccccccccccccc",
        json(r#"{"kind": "continuous", "gap": 30}"#),
    );
    refused(add_routine(&busy, &every_thirty), "routine-step-budget");
    refused(routine(&busy, &"0".repeat(32)), "routine-not-found");
}

#[test]
fn the_first_firing_is_strictly_after_confirmation() {
    let at = |anchor: i64| {
        Fixture {
            anchor,
            ..Fixture::default()
        }
        .build()
        .next_run_at
    };
    assert_eq!(at(epoch(2026, 9, 1, 9, 30, 0)), epoch(2026, 9, 2, 9, 0, 0));
    assert_eq!(at(epoch(2026, 9, 1, 9, 0, 0)), epoch(2026, 9, 2, 9, 0, 0));
    // An interval runs right after confirmation, then its gap after each start.
    let interval = Fixture {
        schedule: every_hour(),
        anchor: nine(),
        ..Fixture::default()
    }
    .build();
    assert_eq!(interval.next_run_at, nine());
}

#[test]
fn a_defined_routine_is_scheduled_from_the_instant_it_is_durable() {
    let now = epoch(2026, 9, 1, 8, 59, 45);
    let value = scheduled(
        &with(|value| (value.anchor, value.next_run_at) = (0, 0)),
        now,
    )
    .expect("scheduled");
    // A fixed schedule's first firing is its next one, however soon; a continuous Routine is due at once.
    assert_eq!(
        (value.anchor, value.next_run_at),
        (now, epoch(2026, 9, 1, 9, 0, 0))
    );
    let continuous = routine_scheduled(ROUTINE_A, json(r#"{"kind": "continuous", "gap": 5}"#));
    assert_eq!(
        scheduled(&continuous, now).map(|value| value.next_run_at),
        Ok(now)
    );
    refused(
        scheduled(
            &with(|value| value.schedule = json(r#"{"kind": "yearly"}"#)),
            now,
        ),
        "routine-invalid",
    );
}

#[test]
fn a_confirmed_card_creates_once_with_its_notice_named_as_the_routine_is() {
    let usage = json(
        r#"{"duration_ms": 6200, "models": [{"provider": "openai", "model": "gpt-6.1-sol",
        "input_tokens": 1200, "output_tokens": 80}]}"#,
    );
    let steps = vec![json(
        r#"{"seq": 1, "phase": "model", "state": "finished", "elapsed_ms": 900}"#,
    )];
    let state = create(
        &TeamRoutines::default(),
        &fixture(),
        nine(),
        Some(usage.clone()),
        steps.clone(),
        &ENTROPY,
    )
    .expect("created");
    assert_eq!(
        state
            .routines
            .iter()
            .map(|item| item.routine_id.as_str())
            .collect::<Vec<_>>(),
        [ROUTINE_A]
    );
    let [notice] = state.notices.as_slice() else {
        panic!("one notice");
    };
    // It shows what the recording that made it spent: its model usage and its Team steps.
    assert_eq!(
        (
            notice.outcome.as_str(),
            notice.run_id.as_str(),
            &notice.detail
        ),
        ("created", "", &object(vec![("steps", Value::Array(steps))]))
    );
    assert_eq!(
        (
            notice.name.as_str(),
            notice.usage.as_ref(),
            notice.protection_lost
        ),
        ("Daily DNS summary", Some(&usage), false)
    );
    // A minted id never exists twice, so a repeated confirmation never creates twice.
    refused(
        create(&state, &fixture(), nine() + 5, None, Vec::new(), &ENTROPY),
        "routine-exists",
    );
}

#[test]
fn room_for_a_change_counts_only_undelivered_notices() {
    assert_eq!(change_room(&TeamRoutines::default()), None);
    let full = TeamRoutines {
        notices: full_notices(MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES),
        ..TeamRoutines::default()
    };
    assert_eq!(change_room(&full), Some("notices-full"));
}

#[test]
fn an_update_is_the_next_revision_of_exactly_the_revision_the_card_saw() {
    let state = added(&[fixture(), routine_of("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")]);
    let state = mark_scope_changed(&state, ROUTINE_A, nine(), &ENTROPY).expect("marked");
    let state = set_paused(&state, ROUTINE_A, true).expect("paused");
    let weekly = json(r#"{"kind": "weekly", "weekday": 0, "time": "09:00"}"#);
    let changed = scheduled(
        &with(|value| (value.name, value.schedule) = ("DNS summary".to_owned(), weekly)),
        nine(),
    )
    .expect("scheduled");
    refused(
        update(&state, &changed, 2, nine(), &ENTROPY),
        "routine-revision-changed",
    );
    let busy = added(&[
        fixture(),
        routine_scheduled(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            json(r#"{"kind": "continuous", "gap": 5}"#),
        ),
    ]);
    let every_thirty = scheduled(
        &with(|value| value.schedule = json(r#"{"kind": "continuous", "gap": 30}"#)),
        nine(),
    )
    .expect("scheduled");
    refused(
        update(&busy, &every_thirty, 1, nine(), &ENTROPY),
        "routine-step-budget",
    );
    let after = update(&state, &changed, 1, nine(), &ENTROPY).expect("updated");
    let current = routine(&after, ROUTINE_A).expect("a Routine");
    assert_eq!(
        (
            current.revision,
            current.name.as_str(),
            current.paused,
            current.needs_reconfirm
        ),
        (2, "DNS summary", true, false)
    );
    let last = &after.notices[after.notices.len() - 1];
    assert_eq!(
        (last.outcome.as_str(), last.name.as_str()),
        ("changed", "DNS summary")
    );
    // An earlier version keeps the name it was written with.
    assert_eq!(
        after.notices[after.notices.len() - 2].name,
        "Daily DNS summary"
    );
}

#[test]
fn an_update_never_lands_on_a_running_or_deleting_routine() {
    let (claimed_state, _claim, _lease) = claimed();
    refused(
        update(&claimed_state, &fixture(), 1, nine(), &ENTROPY),
        "routine-busy",
    );
    let (deleting, _runs) = begin_delete(&added(&[fixture()]), ROUTINE_A).expect("deleting");
    refused(
        update(&deleting, &fixture(), 1, nine(), &ENTROPY),
        "routine-not-found",
    );
    refused(
        update(&TeamRoutines::default(), &fixture(), 1, nine(), &ENTROPY),
        "routine-not-found",
    );
}

#[test]
fn deletion_completes_with_one_deleted_notice_that_outlives_the_routine() {
    let (state, _runs) = begin_delete(&added(&[fixture()]), ROUTINE_A).expect("deleting");
    let gone = complete_delete(&state, ROUTINE_A, nine(), &ENTROPY).expect("deleted");
    assert_eq!(gone.routines, []);
    let deleted: Vec<_> = gone
        .notices
        .iter()
        .filter(|item| item.outcome == "deleted")
        .collect();
    let [notice] = deleted.as_slice() else {
        panic!("one deleted notice");
    };
    assert_eq!(
        (
            notice.routine_id.as_str(),
            notice.run_id.as_str(),
            notice.name.as_str(),
            &notice.detail,
            &notice.usage
        ),
        (ROUTINE_A, "", "Daily DNS summary", &object(vec![]), &None)
    );
    refused(
        complete_delete(&gone, ROUTINE_A, nine(), &ENTROPY),
        "routine-not-found",
    );
}

#[test]
fn a_definition_out_of_its_zone_or_revision_is_invalid() {
    assert!(!definition_valid(&with(|value| set(
        &mut value.plan,
        "timezone",
        text("Mars/Olympus")
    ))));
    // A Routine with no known zone runs on UTC by convention, its run date included; it is never another zone.
    let mut unzoned = Fixture {
        schedule: every_hour(),
        ..Fixture::default()
    }
    .build();
    unzoned.timezone_source = "none".to_owned();
    assert!(definition_valid(&unzoned));
    assert!(definition_valid(&with(
        |value| value.timezone_source = "none".to_owned()
    )));
    assert!(!definition_valid(&with(
        |value| value.timezone_source = "phone".to_owned()
    )));
    let mut clocked = unzoned.clone();
    let mut only = elements(at(&clocked.plan, "steps"))[0].clone();
    set(
        &mut only,
        "input",
        json(r#"{"day": {"kind": "run_clock", "format": "date"}}"#),
    );
    set(&mut clocked.plan, "steps", Value::Array(vec![only]));
    assert!(definition_valid(&clocked));
    let mut lisbon = unzoned;
    set(&mut lisbon.plan, "timezone", text("Europe/Lisbon"));
    assert!(!definition_valid(&lisbon));
    assert!(!definition_valid(&with(|value| value.revision = 0)));
    assert!(definition_valid(&fixture()));
}

#[test]
fn the_largest_notice_fits_a_batch_several_times_over_and_a_batch_past_its_bound_is_refused() {
    let vectors = json(include_str!("../../../protocol/http/v1/vectors.json"));
    let batches = elements(at(
        at(at(&vectors, "routine_views"), "notice_batch"),
        "valid",
    ));
    let first = elements(at(&batches[0], "notices"))[0].clone();
    let largest: Vec<Value> = (1..33)
        .map(|index: usize| {
            let mut notice = first.clone();
            set(&mut notice, "notice_id", text(&format!("{index:032x}")));
            set(&mut notice, "run_id", text(&format!("{index:032x}")));
            set(&mut notice, "detail", large_completion());
            notice
        })
        .collect();
    let size = encoded_bytes(&largest[0]);
    assert!(size > team_protocol::routine::MAX_OUTPUT_BYTES / 2);
    let fits = MAX_NOTICE_BATCH_BYTES / (size + 1);
    assert!(fits >= 4);
    let batch = |count: usize, more: bool| {
        object(vec![
            ("notices", Value::Array(largest[..count].to_vec())),
            ("more", Value::Bool(more)),
        ])
    };
    assert!(canonical_notice_batch(&batch(fits, true)).is_some());
    assert!(canonical_notice_batch(&batch(fits + 1, false)).is_none());
}

// --- Budgets (`test_routine_definition.py`) -------------------------------------------------------------------------

fn defined(change: impl FnOnce(&mut Routine)) -> Routine {
    let mut value = confirmed(Routine::new(
        ROUTINE_A,
        "DNS",
        json(r#"{"kind": "continuous", "gap": 864}"#),
        Vec::new(),
        default_plan(),
        0,
        0,
    ));
    change(&mut value);
    value
}

#[test]
fn units_are_replay_steps() {
    assert_eq!(run_units(&defined(|_| ())), 1);
    let many =
        defined(|value| value.plan = repeated_plan(17, r#"{"mode": "show", "step": "s16"}"#));
    assert_eq!(run_units(&many), 17);
    // Every start may use every unit, as often as the cap allows.
    assert_eq!(daily_steps(&many), 100 * 17);
    assert_eq!(capacity(&[&defined(|_| ())]), MAX_DAILY_STEPS - 100);
    assert_eq!(state(&defined(|_| ())), "active");
    assert_eq!(state(&defined(|value| value.paused = true)), "paused");
}

#[test]
fn definition_bytes_count_the_plan_and_every_standing_part() {
    let value = defined(|_| ());
    let plain = definition_bytes(&value);
    assert!(plain > canonical(&value.plan).len());
    let mut other = value.permitted[0].clone();
    set(&mut other, "action", text("other"));
    let wider = defined(|value| value.permitted.push(other));
    assert!(definition_bytes(&wider) > plain);
}

#[test]
fn a_budget_names_the_units_or_the_bytes_it_outgrows() {
    let busy = defined(|value| {
        value.routine_id = "b".repeat(32);
        value.schedule = json(r#"{"kind": "continuous", "gap": 5}"#);
    });
    let thirty = defined(|value| value.schedule = json(r#"{"kind": "continuous", "gap": 30}"#));
    assert_eq!(over_budget(&[&busy], &thirty), Some("routine-step-budget"));
    assert_eq!(over_budget(&[&busy], &defined(|_| ())), None);
    let heavy =
        defined(|value| value.plan = repeated_plan(201, r#"{"mode": "none", "step": null}"#));
    assert_eq!(over_budget(&[], &heavy), Some("routine-step-budget"));
    assert_eq!(over_budget(&[], &defined(|_| ())), None);
}

// --- What a Supervisor sees (`test_routine_view.py`) ----------------------------------------------------------------

fn view_plan() -> Value {
    json(&format!(
        r#"{{"version": 4, "timezone": "UTC", "steps": [
            {{"id": "zones", "assistant": "dns", "action": "list-zones", "pin": "{PIN}",
              "input": {{"page": {{"kind": "literal", "value": {{"n": "a\u202eb"}}}}}}}},
            {{"id": "records", "assistant": "dns", "action": "list-records", "pin": "{PIN}",
              "input": {{"zone": {{"kind": "step_output", "step": "zones", "pointer": "/zones/0/id"}},
                         "named": {{"kind": "step_output", "step": "zones", "pointer": "/zones",
                                    "where": {{"name": "a\u202eb.com"}}, "item": "/id"}},
                         "day": {{"kind": "run_clock", "format": "date"}}}}}}],
            "output": {{"mode": "none", "step": null}}}}"#
    ))
}

/// A plan of `count` steps cycling through `actions`, each step referring to the one before it.
fn many(count: usize, actions: &[&str]) -> Value {
    let steps = (0..count)
        .map(|index| {
            let mut inputs = vec![(
                "zone".to_owned(),
                object(vec![
                    ("kind", text("literal")),
                    ("value", text(&format!("zone-{index}.example"))),
                ]),
            )];
            if index > 0 {
                inputs.push((
                    "after".to_owned(),
                    json(&format!(
                        r#"{{"kind": "step_output", "step": "s{}", "pointer": "/id"}}"#,
                        index - 1
                    )),
                ));
            }
            object(vec![
                ("id", text(&format!("s{index}"))),
                ("assistant", text("dns")),
                ("action", text(actions[index % actions.len()])),
                ("pin", text(PIN)),
                ("input", Value::Object(inputs)),
            ])
        })
        .collect();
    object(vec![
        ("version", int(4)),
        ("timezone", text("UTC")),
        ("steps", Value::Array(steps)),
        (
            "output",
            json(&format!(r#"{{"mode": "show", "step": "s{}"}}"#, count - 1)),
        ),
    ])
}

#[test]
fn a_step_shows_its_sources_by_position_and_its_stored_inputs_by_name_only() {
    let plan = view_plan();
    let entries: Vec<Value> = permitted(&plan, true)
        .into_iter()
        .map(|mut item| {
            if as_str(at(&item, "action")) == Some("list-records") {
                set(&mut item, "stored_inputs", json(r#"["api"]"#));
            }
            item
        })
        .collect();
    let projected: Vec<Value> = [1, 2]
        .iter()
        .map(|position| step(&plan, &entries, *position).expect("a step"))
        .collect();
    let expected = json(
        r#"[{"position": 1, "assistant": "dns", "action": "list-zones", "read_only": true,
             "inputs": [{"member": "page", "source": "literal", "value": "{\"n\":\"a\\u202eb\"}"}], "stored_inputs": []},
            {"position": 2, "assistant": "dns", "action": "list-records", "read_only": true,
             "inputs": [{"member": "day", "source": "run_clock", "value": "date"},
                        {"member": "named", "source": "step_output", "step": 1, "pointer": "/zones",
                         "where": {"member": "name", "value_json": "\"a\\u202eb.com\""}, "item": "/id"},
                        {"member": "zone", "source": "step_output", "step": 1, "pointer": "/zones/0/id",
                         "where": null, "item": null}],
             "stored_inputs": ["api"]}]"#,
    );
    assert_eq!(Value::Array(projected.clone()), expected);
    for (index, projected_step) in projected.iter().enumerate() {
        let position = i64::try_from(index + 1).expect("a position");
        assert_eq!(
            canonical_step(projected_step, position),
            Some(projected_step)
        );
    }
    assert!(steps_fit(&plan, &entries));
    assert_eq!(
        disposition(&plan),
        json(r#"{"mode": "none", "step": null}"#)
    );
}

#[test]
fn a_dated_text_shows_its_template_cut_like_a_literal() {
    let dated = r#"{"days": 6, "format": "{dd}/{MM}", "lang": null}"#;
    let mut plan = many(1, &["check"]);
    let source =
        format!(r#"{{"kind": "run_clock", "format": "text", "parts": ["\u202e{{x}} ", {dated}]}}"#);
    let mut only = elements(at(&plan, "steps"))[0].clone();
    set(
        &mut only,
        "input",
        Value::Object(vec![("zone".to_owned(), json(&source))]),
    );
    set(&mut plan, "steps", Value::Array(vec![only.clone()]));
    let projected = step(&plan, &permitted(&plan, true), 1).expect("a step");
    assert_eq!(
        at(&elements(at(&projected, "inputs"))[0], "value"),
        &text(r"\u202e{{x}} {DD/MM|+6}")
    );
    assert_eq!(canonical_step(&projected, 1), Some(&projected));
    let long = format!(
        r#"{{"kind": "run_clock", "format": "text", "parts": ["{}", {dated}]}}"#,
        "x".repeat(200)
    );
    set(
        &mut only,
        "input",
        Value::Object(vec![("zone".to_owned(), json(&long))]),
    );
    set(&mut plan, "steps", Value::Array(vec![only]));
    let cut = step(&plan, &permitted(&plan, true), 1).expect("a step");
    let shown = as_str(at(&elements(at(&cut, "inputs"))[0], "value")).expect("a preview");
    assert_eq!(
        (shown.chars().count(), shown.chars().last()),
        (team_protocol::routine::MAX_PREVIEW_CHARS, Some('…'))
    );
    assert_eq!(canonical_step(&cut, 1), Some(&cut));
}

#[test]
fn a_step_whose_projection_outgrows_its_bound_does_not_fit() {
    let mut plan = many(1, &["check"]);
    let pointer = format!("/{}", "p".repeat(250));
    let inputs = (0..64)
        .map(|index| {
            (
                format!("m{index:02}"),
                object(vec![
                    ("kind", text("step_output")),
                    ("step", text("s0")),
                    ("pointer", text(&pointer)),
                ]),
            )
        })
        .collect();
    let mut only = elements(at(&plan, "steps"))[0].clone();
    set(&mut only, "input", Value::Object(inputs));
    set(&mut plan, "steps", Value::Array(vec![only]));
    assert!(!steps_fit(&plan, &permitted(&plan, true)));
}

#[test]
fn runs_of_one_repeated_action_summarize_a_long_plan() {
    let mut plan = many(120, &["check"]);
    let mut steps = elements(at(&plan, "steps")).to_vec();
    set(&mut steps[119], "action", text("notify"));
    set(&mut plan, "steps", Value::Array(steps));
    let summarized = summary(&plan, 3);
    for (key, expected) in [
        ("revision", int(3)),
        ("steps", int(120)),
        (
            "actions",
            json(r#"[["dns", "check", 119], ["dns", "notify", 1]]"#),
        ),
        ("more", int(0)),
    ] {
        assert_eq!(at(&summarized, key), &expected);
    }
    assert_eq!(canonical_summary(&summarized), Some(&summarized));
    assert_eq!(disposition(&plan), json(r#"{"mode": "show", "step": 120}"#));
    // Beyond sixteen runs the rest is counted.
    let alternating = summary(&many(256, &["check", "notify"]), 1);
    assert_eq!(
        (
            elements(at(&alternating, "actions")).len(),
            at(&alternating, "more")
        ),
        (16, &int(240))
    );
    assert!(encoded_bytes(&alternating) <= MAX_SUMMARY_BYTES);
    assert_eq!(canonical_summary(&alternating), Some(&alternating));
}

#[test]
fn pages_cover_every_step_once_in_order_within_their_bounds() {
    let plan = many(256, &["check"]);
    let entries = permitted(&plan, true);
    let (mut positions, mut offset, mut pages) = (Vec::new(), Some(0), 0);
    while let Some(start) = offset {
        let projected = page(ROUTINE_A, 4, &plan, &entries, start).expect("a page");
        assert_eq!(canonical_page(&projected), Some(&projected));
        assert_eq!(
            (
                at(&projected, "revision"),
                at(&projected, "total"),
                at(&projected, "offset")
            ),
            (&int(4), &int(256), &int(start))
        );
        positions.extend(
            elements(at(&projected, "steps"))
                .iter()
                .map(|item| at(item, "position").clone()),
        );
        offset = team_protocol::python::value::small_int(at(&projected, "next"));
        pages += 1;
    }
    assert_eq!(
        (positions, pages),
        ((1..=256).map(int).collect::<Vec<_>>(), 4)
    );
}

#[test]
fn a_page_of_large_steps_stops_at_its_byte_bound() {
    let mut plan = many(20, &["check"]);
    let steps = elements(at(&plan, "steps"))
        .iter()
        .map(|item| {
            let mut copy = item.clone();
            let mut inputs = vec![(
                "zone".to_owned(),
                object(vec![
                    ("kind", text("literal")),
                    ("value", text(&"z".repeat(4000))),
                ]),
            )];
            if let Some(after) = at(item, "input").get("after") {
                inputs.push(("after".to_owned(), after.clone()));
            }
            inputs.extend((0..40).map(|index| {
                (
                    format!("x{index}"),
                    object(vec![
                        ("kind", text("literal")),
                        ("value", text(&"y".repeat(4000))),
                    ]),
                )
            }));
            set(&mut copy, "input", Value::Object(inputs));
            copy
        })
        .collect();
    set(&mut plan, "steps", Value::Array(steps));
    let projected = page(ROUTINE_A, 1, &plan, &permitted(&plan, true), 0).expect("a page");
    assert_eq!(canonical_page(&projected), Some(&projected));
    assert!(elements(at(&projected, "steps")).len() < 20);
    assert!(encoded_bytes(at(&projected, "steps")) <= team_protocol::routine::MAX_PAGE_BYTES);
}

#[test]
fn a_run_status_reads_back_its_persisted_name() {
    for status in [
        RunStatus::Leased,
        RunStatus::Frozen,
        RunStatus::Held,
        RunStatus::Unresolved,
        RunStatus::Skipped,
    ] {
        assert_eq!(RunStatus::parse(status.as_str()), Some(status));
    }
    assert_eq!(RunStatus::parse("waiting"), None);
}
